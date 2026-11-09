---
title: "OpenCode 机制深潜 III：一次请求的装配与落地"
date: "2026-11-09T13:43:00+08:00"
summary: "配置的六层来源与排序、agent 与模型的三级解析、一次请求的七道装配、三家 wire 的翻译差异、用量与成本的算法、缓存断点与失效条件，以及这一层特有的失败模式。"
description: "基于 opencode 2.2.5 源码的第三层解剖：配置的六层来源与排序、agent 与模型的三级解析、一次请求的七道装配、三家 wire 的翻译差异、用量与成本的算法、缓存断点与失效条件。"
tags: ["opencode", "architecture"]
slug: "opencode-deep-dive-3"
---

> 源码版本：`opencode 2.2.5`，commit `a687c047`（2026-11-08）。代码引用以工作区 `.opencode-src/` 为基准。
> 系列第六篇。[架构总览](https://www.jasoncai.site/posts/opencode-architecture.html) · [上下文管理](https://www.jasoncai.site/posts/opencode-context-management.html) · [打断](https://www.jasoncai.site/posts/opencode-interrupt.html) · [机制深潜 I](https://www.jasoncai.site/posts/opencode-deep-dive.html) · [机制深潜 II](https://www.jasoncai.site/posts/opencode-deep-dive-2.html)

## 0. 这一卷填的是中间那段

前两卷拆了"会话怎么跑"和"状态怎么迁移"，但有一个环节始终是个黑盒：**从一堆配置、指令、工具和历史，到一份真正发给 provider 的请求，中间发生了什么**。这一卷就讲这段。

它同时回答三个日常问题：

- 为什么我改了配置却没生效？（§1 的优先级）
- 为什么这次请求这么贵 / 缓存没命中？（§5、§6）
- 为什么换个模型后工具行为变了？（§2 的能力裁剪）

## 1. 配置：一份有序的文档列表

{{< fig "diagrams/02-config-layers.svg" "配置不是一个对象，是一份有序的文档列表" >}}

先纠正一个常见误解：OpenCode **没有一个"把多份配置深合并成一个大对象"的函数**。`Config` 只产出一份按优先级排好序的 `Entry[]`（`Document | Directory`），真正的合并分散在各领域插件的 fold 里。这解释了为什么"同一个字段在不同地方语义不一样"——因为它根本不由同一段代码处理。

### 1.1 来源

| 来源 | 位置 |
| --- | --- |
| 系统目录 | `/etc/opencode`、`/Library/Application Support/opencode`、`%ProgramData%\opencode` |
| macOS MDM | `ai.opencode.managed.plist` |
| 全局目录 | `~/.config/opencode`（受 `OPENCODE_CONFIG_DIR` 影响） |
| 项目 | 从 Location 向上逐级找 `opencode.json(c)` 与 `.opencode/` |
| 显式文件 | `OPENCODE_CONFIG` |
| 内联内容 | `OPENCODE_CONFIG_CONTENT` |
| 远端 | wellknown（KV 注册的 origin）与 Console 推入的集成文档 |

另外还有两个只做替换与兼容的东西：`{env:VAR}` / `{file:path}` 在前置阶段展开（缺失替换为空串），`.claude` / `.agents` 只作技能发现根、不产出 JSON。

**插件不是来源**。各插件通过 `ConfigEntryObserver` 读 `entries()` 然后改自己的 editor，从不回写配置。

### 1.2 优先级就是数组顺序

从低到高：集成文档 < wellknown < 全局目录 < 显式文件 < 项目 `opencode.json(c)` < 项目 `.opencode` < content < managed。

- 同目录内 `opencode.json` 先、`.jsonc` 后，后者胜；
- 项目向上遍历是远祖先在前、近目录在后，所以"就近覆盖"；
- 机制是"后加载覆盖先加载"，由数组顺序 + `findLast` / fold 顺序实现，**没有显式 priority 数值**。
- 唯一的例外是 policies：它反向，早加载者胜（managed 最高），实现是先 `toReversed()` 再 `findLast`。

### 1.3 合并语义按领域分

| 领域 | 语义 |
| --- | --- |
| 单值字段（`shell`、`model`、`snapshots`） | 取最后一个定义者，整份替换，不回填更早文档的叶子 |
| `providers` | 字段级 overlay，`settings` / `body` 是唯一递归合并的，headers 大小写不敏感 |
| `agents`、`mcp` | 命名 map 逐名覆盖 |
| permissions、plugins、skills、instructions | 按文档顺序累积 |
| 经 `latest` 读取的数组 | 整体替换 |

所以"数组是追加还是替换"没有统一答案，得看具体字段——这是读这份代码最容易踩的坑。

`plugins` 的语义还更细一点：字符串是 add、`-pkg` 是 remove、对象是 `{package, options}`；显式项最后应用，以便删除自动发现项。

### 1.4 归一化发生在解码之前

每份文档在 schema 解码前先过 `normalize`。代表性迁移：

- `snapshot → snapshots`、`autoupdate → update`、`attachment → media`；
- `agent / mode → agents`，`small_model` 变成 title agent 的模型；
- `command → commands`、`plugin → plugins`、`provider → providers`（含旧 provider ID 重命名）；
- `tools` + `permission` 扁平成 `permissions` 规则数组；
- MCP 数字 timeout → `{catalog, execution}`。

拒绝分三层粒度：字段级丢弃（warning）、新旧冲突只记 debug、整份文档仅在根不是对象 / JSONC 解析失败 / schema 校验失败时丢弃。

在配置层收口的理由：下游只面对一种形状；冲突要可诊断而非静默覆盖；远端文档只能碰白名单键。

### 1.5 重载是"原地"的

Config 是 location node，每个 Location 一个实例。配置变更时**原地 reload，不重建 Location**：watcher 汇总事件 → 100ms 防抖 → `Semaphore(1)` 串行重新 discover、全量 load、reconcile 订阅 → 与旧值 deep-equal 就不发事件。

下游靠 `config.updated` 事件重读 entries、调用各自的 `reload()`。而 instruction 与 skill 有**独立的 watcher**：改 `AGENTS.md` 或 `SKILL.md` 不会触发 `config.updated`，也就不会重建 provider/agent；反过来改配置会连带刷新它们。这个不对称是刻意的——文件内容的变化比配置结构的变化频繁得多。


## 2. Agent 与模型：先选人，再选路由

### 2.1 agent 也是一张注册表

内置 agent 有七个，通过插件注册进 `Agent` 的 State：`build`（默认）、`general`、`explore`、`compaction`、`title`、`summary` 在 `plugin/agent.ts`，`plan` 在 `plugin/plan.ts`。字段面只有 model / request / system / mode / hidden / color / steps / permissions，**没有 `editable` 这种字段**——可控的只是 `hidden`（是否出现在切换器）、`mode` 与 `disabled`。

config 的 `agent.<id>` 与 `{agent,agents,mode,modes}/**/*.md` 覆盖它们。覆盖顺序靠插件注册序：内置在 `pre`、config 插件在 `post`，所以后者后写覆盖前者。

选择逻辑在 `Agent.select`（`agent.ts:38-64`）：

```text
可选的 = mode !== "subagent" && !hidden
默认 = editor.default(id) 指定的，且可选
     ?? build
     ?? 第一个可选的
```

所以「隐藏」与「子代理模式」两个标记决定的是它会不会出现在切换器里，而不是能不能被调用。

### 2.2 模型的优先级只有三层

`SessionRunnerModel.resolve`（`runner/model.ts:71-104`）写得很直白：

| 情况 | 行为 |
| --- | --- |
| 会话已选模型 | 必须在当前可用目录里，找不到就 `ModelUnavailableError`；用它的 variant 解析 |
| 会话未选、agent 有模型 | **只有该模型仍在目录里**才用（agent 的 model 可能来自旧配置）；否则退回默认 |
| 都没有 | `resolver.resolve()` 取默认；仍没有则 `ModelNotSelectedError` |

注意第二行：agent 的 `model` 不是"配置了就一定用"，而是一个带存在性检查的偏好。这避免了"agent 指向一个已下线的模型"让整个会话不可用。

`variant` 跟着模型引用一起持久化（`ref` 里带 `variant`），所以恢复会话时能回到同一个推理档位。

### 2.3 capabilities 决定了三件事

能力表来自 models.dev v2，运行时实际被用在三处：

1. **图片**：`unsupportedParts` 按输入模态裁掉不支持的类型（xAI 还会额外限制 image MIME 白名单），再做字节预算；
2. **上下文窗口**：压缩触发线是 `window - max(15%, 16k)`，`maxTokens` 也按 `limit.output` 算；
3. **推理档位**：`capabilities.reasoning` 决定 variant 列表怎么生成。

有一个反直觉的点：**`capabilities.tools` 在核心请求链里没有被读取**，只有 UI 在用。也就是说"模型不支持工具"不会让工具从表里消失，服务端裁工具只靠权限。真要兼容不支持工具的模型，目前得自己在配置里收窄工具集。

### 2.4 换模型时不会重置什么

换模型会改变请求前缀（缓存失效）、可能改变媒体裁剪，但**不会重置 Step 配额**——配额只在"新输入被提升"时重置，与第二卷的结论一致。切到同一个 provider + id + variant 是 no-op。

工具快照跟着 **agent** 走，不跟模型走：换 agent 会按新权限重建工具表，换模型不会。

还有一个隐蔽的前缀变化：当 `agent.system` 为空时，第 0 段 system prompt 会按模型家族挑模板（`optimize.ts`）。所以从一家模型换到另一家，即使什么都没改，system 段也可能整段变化——这比"换个模型"这个动作本身对缓存的打击更大。


## 3. 装配线：七道工序

{{< fig "diagrams/01-assembly.svg" "一份请求要经过七道装配" >}}

### 3.1 两个 system 段

请求的 system 不是一段，是两段（`session/model-request.ts:105-125`）：

```ts
system: [
  SystemPart.make(agent.system ?? SessionSystemPrompt.make(toolNames)),
  { ...SystemPart.make(Instructions.join(initial)),
    metadata: { experimental: { instructions: initial } } },
].filter(part => part.text.length > 0)
```

第一段是 agent 的人格（没自定义就用默认模板，按实际工具名渲染操作指引）；第二段是指令基线（AGENTS.md 等）。分开的理由是稳定性：人格几乎不变，指令基线按 epoch 变，两者各自成为可缓存的前缀片段。

基线内部也按"共享面从大到小"排：Code Mode 目录 → MCP → reference → skill → 项目发现 → 内置 → 会话条目。日期、环境这类天天变的内容排最后，就是为了让前面那段尽可能长地保持稳定。

### 3.2 prepare 的七步

`SessionModelRequest.prepare`（`model-request.ts:260` 起）是装配的核心。按执行顺序：

1. **深拷贝工具定义**。每个 hook 拿到的是一份 `JSON.parse(JSON.stringify(inputSchema))` 的副本——因为 hook 可能原地改 schema，而 definitions 跨 Step、跨会话共享。
2. **跑 `session.context` hook**（叫 `shape`）。它能看到并改写 `system`、`messages`、`tools`、`options`（maxTokens 等）。
3. **按身份回收被改名的工具**。hook 通过移动 key 来改名，所以回收时先按对象身份匹配、再按 key 匹配；两者都不中的条目视为 hook 凭空发明的，直接丢弃。`t.name` 始终保留真名，执行时据此把改名映射回去。
4. **拆分生成参数与 providerOptions**。`GENERATION_KEYS` 里的进 `generation`，其余整体进 `providerOptions`——这是"通用参数"与"这家 provider 私有参数"的分界。
5. **拼头部与亲和性**。`x-opencode-session-id`、父会话 id、两个拼写的 affinity（`x-session-affinity` 与 `X-Session-Id`）、`User-Agent`、project、client，以及 `promptCacheKey`。后者直接取 affinity，只有在它恰好是 `ses_` + 64 位十六进制时才剥掉前缀（对齐 OpenAI 的 64 字符上限）；当前 ID 是 26 字符，所以实际发出去的是原样 ID，那段 `slice(4)` 是给老格式留的兼容路径。
6. **能力裁剪 + 媒体预算**。消息先过 `unsupportedParts`（按 model capabilities 去掉不支持的多模态），再过 `boundImages`（按 token 预算裁图）。
7. **`session.model.request` hook**。它可以改 baseURL（走 `route.with`）与 headers；如果还有 `http.request` / `http.response` hook，会再包一层 transport，让插件能看/改原始 HTTP 请求。

最后得到 `PreparedRequest`，交给协议层。到此为止不再读数据库。

### 3.3 输出上限是算出来的

`options.maxTokens` 不是常数：primary 请求用 `outputLimit(model.limit, inputTokens, WINDOW_HEADROOM)`，compaction 用不带 headroom 的版本，标题与 generate 则**不给**——注释写得很直白：那两种调用的推理很难预算。

具体算法：取"catalog 输出上限或 32k"与 256k 的较小者，再按窗口剩余空间与 1.15 倍的估算误差收紧，最低 1024。所以一次请求的 maxTokens 随输入长度浮动，而不是固定的 32k。

### 3.4 顺序为什么不能换

system → tools → messages 这个顺序决定了缓存前缀的粒度：

- system 变了（改人格、改指令基线）→ 整段前缀全失效；
- tools 变了（加插件、改权限导致工具消失）→ system 之后全失效；
- 只有 messages 变了 → 前缀仍可复用。

压缩之所以能"复用缓存"，就是因为它只动 messages 这一段，system 与 tools 原样不动。反过来，`toolChoice: "none"` 的最后一步宁可保留工具定义，也是同一个道理。

### 3.5 两个容易忽略的细节

**运行中的指令变更不进 system 字段。** 它被写成一条 `role:"system"` 的历史消息，插在当时的位置——这样 system 前缀不被重写，只有尾部变化。如果把它塞回 system 字段，每次改 AGENTS.md 都会让整段缓存失效。

**prepare 返回时把 `event` 丢掉了。** 调用侧只保留 `request / options / retry / executeTool` 四项。原因是 hook 事件里存着裁剪前的全部消息副本（含截图），而 Step 活得比请求久，留着等于多占一份全量请求的内存。

### 3.6 媒体：替换，而不是删除

图片预算是**字节级**的：超过 25 MiB 触发裁剪，目标 15 MiB；只处理 `image/*`，按 base64 长度算（`bytes → ceil/3*4`），url 与 ref 记 0。超预算的图片不是被删掉，而是**替换成一段固定提示文本**——删掉会让模型以为这里本来就没有图，替换成提示它才知道"有图但太大"。

顺序是先 `unsupportedParts`（按 capabilities 去不支持的类型），再 `boundImages`（按字节预算裁）。库里存的是 `media:<hash>` 引用，读历史时才内联成 data URI，裁剪只作用于内存中的这一份请求。


## 4. 协议层：从统一请求到三家 wire

{{< fig "diagrams/03-stream.svg" "三家的事件流，一个出口" >}}

### 4.1 四个抽象，一层套一层

| 抽象 | 是什么 | 变的是什么 |
| --- | --- | --- |
| `LanguageModel` | 一个值对象：id、provider、route、options、capabilities | 不含任何 wire 知识 |
| `Route` | 一次具体部署：protocol + endpoint + auth + framing + headers | baseURL、认证、预设参数 |
| `Protocol` | 这家 API 长什么样：body schema、`from`、事件 schema、`step` | 请求体与流事件 |
| `Executor` | HTTP 执行缝 | 把 4xx/5xx 与传输失败变成 `AIError` |

实现上是"接口 + 工厂函数"，没有类继承。好处可以量化：50 个协议文件撑起 50 个 provider 门面与 84 个录制目录；一个 `OpenAIChat.protocol` 被 DeepSeek、Groq、Cerebras、Together 等十几个门面共用，每个门面只声明 baseURL、认证和几处 `compatibility`。

"同协议、不同怪癖"的差异被压进 `LanguageModelCompatibility`（`sanitizer`、`maxTokensField`、`reasoningField`、`cacheFormat` 等），而不是复制一份协议。

### 4.2 同一个语义，六种写法

| 维度 | Anthropic | OpenAI Responses | Gemini |
| --- | --- | --- | --- |
| system | `system: [{type:"text", cache_control}]` | `instructions` 单个字符串（`\n` 拼） | `systemInstruction.parts` |
| 工具结果 | user 消息里的 `tool_result` block，连续结果合并 | 顶层 item `function_call_output` | user 的 `functionResponse` part，并行结果挤进同一 turn |
| tool_choice | `auto/any/none/tool` | `auto/none/required/function` | `AUTO/NONE/ANY` + `allowedFunctionNames` |
| 图片 | base64 block（也支持 url/file_id） | `input_image` 的 **data URL** | `inlineData` / `fileData` |
| 工具壳 | `input_schema` | `function` / `namespace` / `custom` 混合 | `functionDeclarations` + `parametersJsonSchema` |
| 思考回放 | `thinking{thinking,signature}` | 顶层 `reasoning` item + `encrypted_content` | `thought:true` + `thoughtSignature` |

同一段内部推理，在三条 wire 上是三种"一等公民"：Anthropic 要签名，Responses 要加密串，Gemini 混在普通 part 里。

### 4.3 流式归一

管道是：bytes → SSE 切帧 → 每帧 JSON 解码 → `step(state, event)` → `LLMEvent[]`；`terminal` 决定何时停，`onHalt` 在流末尾 flush。解码失败转 `InvalidProviderOutputError`，末尾强制"必须有 finish"。

工具参数是**按原始 JSON 字符串累加**的：首次产出 `tool-input-start`，之后每个分片产 `tool-input-delta`（它的 `input` 是 lazy getter，用 partial-json 解析"到目前为止"的近似对象，供 UI 提前渲染）；结束时一次性 `parseToolInput`——成功转 `tool-call`，本地工具解析失败转 `tool-input-error`（原文交回模型自纠）。

finish reason 只保留 6 个值：`stop | length | tool-calls | content-filter | error | unknown`，原始值永远留在 `raw` 里。

usage 的契约是一句话："**输入含缓存、输出含推理、拆解不重叠**"。Anthropic 原生给拆解、要相加得总数；OpenAI/Gemini 给含总数、要相减得拆解；Gemini 的 `candidatesTokenCount` 不含 thoughts，还得再加上 `thoughtTokensCount`。

### 4.4 错误只有四个入口

| 入口 | 变成什么 |
| --- | --- |
| HTTP 4xx/5xx | `classifyProviderFailure`：溢出/413→InvalidRequest，402/配额→QuotaExceeded，401/403→Authentication，429→RateLimit，5xx→ProviderInternal，其余 4xx→InvalidRequest |
| 传输失败 | `TransportError`，区分 request 与 read |
| 流内 error 事件 | **仍然走**同一套分类，所以流内 429 还是 RateLimit |
| 解析失败 / 提前断流 | `InvalidProviderOutputError`（断流带 `incomplete-stream`） |

可重试性集中在 `AIError.isRetryable`：RateLimit、ProviderInternal、UnknownProvider 可重试；Transport 看 `delivery`（rejected 不可重试，accepted 只有 read 可重试）；`incomplete-stream` 可重试；鉴权、配额、内容策略、请求非法、超时不可重试。`x-should-retry` 响应头可强制覆盖。

`UnknownProvider` 默认可重试是刻意的：分类只记录"确定性证据"，瞬时故障恰好长得不像任何已知形状。

### 4.5 缓存有三种表达方式

同一个 `CachePolicy` 落到不同协议上是三样东西：

- **内联标记**：Anthropic 系把 hint 翻成 `cache_control:{type:"ephemeral", ttl?}`；
- **请求级参数**：OpenAI 系走 `prompt_cache_key` 与 `prompt_cache_retention`；
- **带外资源**：Gemini 的 `cachedContent` 是 providerOptions 传入的资源名，前缀缓存隐式，所以 Gemini **不在白名单**，策略直接跳过以免发无用标记。

`compatibility.cacheFormat` 还能覆盖白名单（比如 alibaba 只打 system 与尾部、跳过工具定义，因为 Qwen 把工具和 system 一起缓存）。

### 4.6 AI SDK 兼容路径是一条"手搓的 Route"

`aisdk:<package>` 的模型被包成满足 `AnyRoute` 的对象字面量：protocol 叫 `"ai-sdk"`、endpoint 指向占位地址、`transport.execute` 返回空 frames、`streamPrepared` 直接调 SDK 的 stream。**没有 Framing、没有协议状态机**——帧切分与事件折叠都在 SDK 里，opencode 只做 `parseToolInput` 与错误归一的兜底。

因为共用 `LanguageModel` / `Route` 接口，上层的 `LLMClient`、`ai.stream/loop`、`ResponseFold` 对两条路径完全无感。这也是"用 AI SDK 接一家新 provider 很快"的原因：快在跳过了整条协议层。


## 5. 用量与成本：数字从哪来

### 5.1 先把 token 拆成五类

`SessionUsage.tokens` 把 provider 的 usage 归一成五个数（`session/usage.ts:9-17`）：

| 字段 | 来源 | 含义 |
| --- | --- | --- |
| `input` | `nonCachedInputTokens` | 真正按输入价计费的 token |
| `output` | `visibleOutputTokens` | 可见输出 |
| `reasoning` | `reasoningTokens` | 推理 token，**按输出价计费** |
| `cache.read` | `cacheReadInputTokens` | 命中缓存的输入 |
| `cache.write` | `cacheWriteInputTokens` | 写入缓存的部分 |

最容易忽略的是第三行：推理 token 不计入 `output`，但在算钱时被加回输出那一项。

### 5.2 优先信 provider 报的价

`record()` 的顺序很明确（`usage.ts:44-52`）：

```ts
cost: usage?.cost === undefined
  ? calculateCost(costs, normalized)   // 用 models.dev 目录价估
  : Money.USD.make(usage.cost)         // 直接用对方报的
```

注释解释了为什么：**目录价看不到峰谷、订阅与路由**，而对方报的是实际账单。还有一个细节——"报 0"是权威值，不会被当成"没报"而回退到目录价。

### 5.3 目录价怎么算

`calculateCost` 先按上下文长度选档：`cost.tier.type === "context" && context > tier.size` 的条目里取 size 最大的一个，没有就取无档位的默认项。注意这里是**严格大于**：models.dev 把 `gt` 与 `gte` 都折成 `size`，所以恰好等于阈值时不会进这一档——如果某个模型用 `gte` 定义档位，边界上会少算一档。然后：

```text
input × 输入价
+ (output + reasoning) × 输出价
+ cache.read × 缓存读价
+ cache.write × 缓存写价
（全部除以 1e6）
```

所以"缓存命中便宜"不是一句口号，而是四个独立价格列各算各的。

### 5.4 聚合：增量 SQL，不重算

每次 Step 结算时 `SessionUsage.record` 产出一份 `{tokens, cost}`，但落到会话表不是"读出来加一遍再写回"，而是 `Step.Ended` / `UsageRecorded` 在事务里用 SQL `+` 增量累加到 `session_v2` 的六个列。所以并发写不会互相覆盖，也不存在"重算导致漂移"。fork 出的子会话从 0 起算。

缺失 usage 时不估算：整份记 0。流中断的情况要分两种——**如果 `step-finish` 已经到达**，那一段 usage 仍然会被记录（压缩测试里 `50 = 10 + 10 + 30` 就是断流前已到部分照计的证据）；**如果断在 `step-finish` 之前**，这一次就完全不计。所以"失败调用不计费"是个过于笼统的说法：能不能计，取决于断在哪。

### 5.5 额度与统计是两套口径

**额度**（usage-limits）只读：从 Console 拉 budgets 与 Go 的 rolling/weekly/monthly 用量，15 秒超时；未登录或 403 就返回空。它**不做本地拦截**——真正的限制在服务端，拒绝后映射成 `go-limit` / `budget-limit` / `model-budget-limit`，对应的 `QuotaExceeded` 不可重试，UI 只负责刷新额度并弹窗。

**统计**是另一套：`/api/experimental/session/stats` 从 `session_message` 的 JSON 重新聚合（31 天窗口，按模型/天/时区），compaction 再从 event 表按 `source='compaction'` 补进总量。三个口径差异值得记：

- title 调用的成本进 `session.cost`，但**不进** stats；
- compaction 只进总量，不进"按模型"那一行；
- 按天只数步数。

还有一个容易找错地方的点：**统计页只在 TUI 的 `/stats`**，App 与 Desktop 没有 stats 页面；App 里那个 Usage 对话框是额度，不是统计。


## 6. 缓存与亲和性：什么会打断前缀

### 6.1 affinity 是"哪条会话线"

```ts
affinity = session.parentID ?? session.fork?.sessionID ?? session.id
```

（`session/affinity.ts:5`）

三个分支对应三种关系：子会话共享父会话的 affinity；fork 共享 fork 源的；普通会话用自己的。目的只有一个——让同一棵会话树上的请求落到同一个缓存路由上。

它同时出现在四个请求头里（`x-session-affinity`、`X-Session-Id`、`x-opencode-session`，以及父会话 id）。不同 provider 的"亲和"通道还不一样：xAI 用 `x-grok-conv-id`、Vercel AI Gateway 用 `x-session-affinity`、ChatGPT/Codex 用 `session-id`。

两个坑。其一，**嵌套 fork 不回溯根**：fork 的 fork 的 affinity 指向中间那层，拿不到根的缓存，代码里挂着一条 TODO。其二，传输层也有个叫 affinity 的东西——WebSocket 通道的 key 是 url + headers 的 sha256，换 header 就重开 socket，与 prompt cache 无关。

### 6.2 断点放在哪

`CachePolicy` 的默认值是 `"auto"`（`ai/src/cache-policy.ts:27-31`）：

```ts
{ tools: true, system: true, messages: { tail: 1 } }
```

落到请求上就是四个断点：**最后一个工具定义、第一段与最后一段不同的 system、对话尾部**。断点总数有硬上限 `BREAKPOINT_CAP = 4`，手动标注的 `CacheHint` 先占位，auto 只填剩下的槽。

这个布局对应真实的复用模式：工具表与系统提示几乎不变（长期前缀），对话尾部随工具结果推进（近期前缀）。

### 6.3 什么会打断它

先立一条原则：**失效范围是从改动点往后，改动点之前的断点仍可能命中**。所以下表要按"从哪开始"读，而不是简单的"失效/不失效"。

| 改动点 | 影响 | 原因 |
| --- | --- | --- |
| 换模型 | 全失效 | provider 按模型分区缓存，新模型没有已写入的前缀；工具表与 system 也可能随能力变 |
| 改 system prompt（切 agent、升级版本） | 全失效 | system[0] 字节变了，前缀就没了 |
| 改 AGENTS.md | **不失效** | 基线在一个 epoch 内冻结，变更以增量形式**追加**成一条 system 消息，只影响尾部 |
| 改工具 | 多数不失效 | 线上工具表是排序后的 direct 工具 + `execute`；CodeMode 目录在指令文本里、更新也是追加。只有 direct 工具增减或 `execute` 定义变才从头失效 |
| 压缩 | 全失效 | checkpoint 替换整段历史，且 `advanceEpoch` 把基线重置，system 也变了。所以压缩后第一步必然全量写缓存 |
| move 换目录 | 不失效 | 不改会话 ID，环境变化走一条 `location-switched` 消息与指令增量 |
| fork | 通常命中 | affinity 指向 fork 源，前缀逐字节复制；嵌套 fork 退化 |
| 权限 `always` | 不失效 | saved 规则只影响 `evaluate`，不改任何 prompt 字节；工具可见性由 agent/session 的 ruleset 决定 |
| 改 reasoning effort | 会被规避 | 顶层 effort 会作废缓存，所以改为冻结请求级参数、下发 per-message effort 标记 |
| 图片裁剪 | 有滞回 | 每移动 (trigger − target) 字节才挪一次裁切点，注释明说是为了缓存不变 |

最后两行是"用设计绕开失效"的例子：同一件事既能破坏缓存，也能被改造成不破坏——区别在于把变化放在请求级参数里，还是放进会被缓存的字节里。

### 6.4 命中率可观测吗

链路是完整的，但只到 token 粒度：

1. 协议层解析：OpenAI 系有四种落点（`prompt_tokens_details.cached_tokens`、DeepSeek 的 `prompt_cache_hit_tokens`、Zai 的 `cached_tokens`、DigitalOcean 的 `cache_read_input_tokens`）；Anthropic 用 `cache_read_input_tokens` / `cache_creation_input_tokens`。
2. 归一到 `Usage.cacheReadInputTokens` / `cacheWriteInputTokens`。
3. 投影器增量写 `session_v2.tokens_cache_read/write`。
4. 暴露面：`session.step.ended` 的 tokens、`session.usage.updated`、OTel span 的 `gen_ai.usage.cache_read.input_tokens`、以及 stats 接口。

**没有"命中率"这个一等概念**：没有字段、没有 miss 事件、没有专门日志。TUI 只把 cache 读写算进"上下文占用"的总量，不显示百分比。要算命中率，只能自己拼：

```text
cache.read / (input + cache.read + cache.write)
```

这也解释了为什么"这次贵了"很难一眼看出来——数字都在，只是没人替你把它们相除。

## 7. 这一层的失败模式

装配与协议层不产生"会话状态"，所以它们的 bug 表现为"结果不对"，而不是"状态坏了"。按出现频率排：

| 现象 | 根因 | 排查入口 |
| --- | --- | --- |
| 改了配置没生效 | 被更高优先级文档覆盖；或数组是累积而你以为替换；或被归一化拒绝（有 warning） | 看 `entries()` 顺序，别只看自己那份文件 |
| 缓存命中率骤降 | system 或 tools 变了：改 AGENTS.md、装插件、权限让工具整只消失 | 看 usage 里的 cache read 占比 |
| 成本对不上 | 目录价看不到峰谷/订阅；或推理 token 按输出计价；或失败 attempt 不计费 | 看 provider 是否回报 `cost` |
| 模型看不到图 | 能力表不支持该类型，或超 15 MiB 被替换成提示文本 | 看 lowering 后的 messages |
| 工具参数解析失败 | 流分片拼出的 JSON 不合法 → `tool-input-error`，原文回喂模型自纠 | 看原始 tool-input 事件 |
| 流"提前结束" | 没有 finish 事件 → `incomplete-stream`，通常可重试 | 看 finish 是否为 `unknown` |
| 重试把故障放大 | `UnknownProvider` 默认可重试 | 看 `isRetryable` 与 `x-should-retry` |

最后一行值得展开：把"未知错误"默认可重试，是在"宁可多试"与"别把 5xx 放大"之间选了前者。代价是持续性服务端故障会被重试预算放大成 10 次请求，收益是瞬时故障不会直接失败。


## 8. 证据索引

| 主题 | 主要文件 |
| --- | --- |
| 配置来源与排序 | `config.ts`、`config/discovery.ts`、`config/normalize.ts` |
| 领域合并 | `config/plugin/*.ts`、`provider.ts`（`mergeOverlay`） |
| agent 注册与选择 | `agent.ts`、`plugin/agent.ts` |
| 模型解析 | `session/runner/model.ts`、`model-resolver.ts` |
| 装配 | `session/model-request.ts`、`session/runner/to-llm-message.ts` |
| 指令排序 | `session/context.ts`、`instructions/index.ts` |
| 媒体预算 | `session/model-request.ts`（`boundImages`）、`tool.ts` |
| 协议抽象 | `ai/src/route/{client,protocol,executor}.ts` |
| 请求翻译 | `ai/src/protocols/{anthropic-messages,open-responses,gemini}.ts` |
| 流归一 | `ai/src/protocols/utils/tool-stream.ts`、`ai/src/schema/events.ts` |
| 错误分类 | `ai/src/provider-error.ts`、`ai/src/schema/errors.ts` |
| 缓存策略 | `ai/src/cache-policy.ts`、`session/affinity.ts` |
| 用量与成本 | `session/usage.ts` |

---

*这一卷的结论来自按模块分工的调研与主代理回源核对；标注"未确认"的地方是静态阅读无法断定的。*
