---
title: "工具结果的一生：截断、落盘、配对与呈现"
date: "2026-11-11T15:20:00+08:00"
summary: "工具结果同时服务模型、UI 与日志，三者需求冲突。DSH 用 projectContent、post-execute、finalizeContent 三段把它们分开，再用 spill、配对与恢复，把一次返回值变成可审计、可读回、可重放的东西。"
description: "从原始 value 到 tool/result：结果投影、策略改写、最后收口、三种截断策略、spill 落盘与 locator、additionalContexts 的注入时机、call/result 配对、缺失结果补齐，以及 UI 呈现为什么不进入模型上下文。"
tags: ["harness", "context-engineering", "architecture"]
slug: "dsh-tool-results"
---

> 源码版本：`deepseek-harness` v0.2.2-rc.1（commit `fcc0ad46`）。本文对照 `packages/core/tools/src/{pipeline.ts,presentation.ts,contracts.ts}`、`packages/spill/*`、`packages/fs/tool-fs/src/*` 与 `docs/subsystems/spill.zh.md`。
> 本系列第 12 篇。上一篇讲工具的边界，这一篇讲边界之内产生的东西，怎么活着离开一次调用。

先看三个会让人怀疑"结果到底有几份"的现象。第一个：`bash` 的长输出被截断成尾部，而 `read` 的长文件给的是头部 + offset。同样是截断，为什么方向相反？第二个：模型收到的是一段带 `<content>` 标签的文本，UI 显示的却是带行号的代码卡片，日志里还有一份完整 JSON。三者对不上，是 bug 吗？第三个：一次进程被中断之后，模型收到了一条 `tool/result`，说"结果未知，不要盲目重试"，可那个工具根本没有返回过。

这三个现象指向同一个事实：工具结果并非"一个字符串"，它是三个消费者对同一件事的三份投影。模型要的是能继续推理的短文本，UI 要的是能排版的结构，日志要的是能重放的完整记录。把它们塞进同一个字段，任何一方满意都意味着另外两方受损。

DSH 的解法是切开三段，不找"都能接受"的中间格式：`projectContent` 让工具作者决定模型看什么，`tools/post-execute` 让策略决定最终留什么，`finalizeContent` 让工具作者在最后一步收口。三段之外，还有落盘、配对与恢复，负责让"结果"在离开进程之后仍然可追溯。

## 0. 结论

先看三个消费者的需求冲突。这张表解释了为什么"把 value 直接 JSON.stringify 给模型"在 demo 里能用，在产品里一定出问题。

| 消费者 | 需要的东西 | 不能接受的东西 | 典型失败 |
|---|---|---|---|
| 模型 | 短、语义完整、下一步可操作 | 实现细节、UI 词汇、重复 framing | 截断在关键行；错误不可诊断 |
| UI | 结构、行号、diff、可折叠 | 模型专用文本、丢失 meta | 只能显示一坨等宽文本 |
| 日志 | 完整、可重放、稳定 id | 被展示策略改写 | 回放不出当时模型看到的内容 |

三段缝的分工如下。注意它们的顺序按所有权排，不按实现顺序：越靠后，越接近唯一权威结果。

| 缝 | 谁拥有 | 何时运行 | 能做什么 | 不能做什么 |
|---|---|---|---|---|
| `projectContent` | 工具作者 | 规范化结果进入 post 之前 | 把结构化 value 投影成 `content[]` | 改 value；绕过后面的策略 |
| `tools/post-execute` | 任意策略插件 | project 之后 | accept content / value，或 block | 改参数；把失败改成成功值 |
| `finalizeContent` | 工具作者 | 所有改写之后、无损物化之前 | 最后一次收口 content | async；抛错；改 value |

四份数据各归各的消费者：value 是程序的，content 是模型的，meta 是 UI 的，事件是日志的；它们共用一次调用，不共用一份数据。

{{< fig "diagrams/01-result-pipeline.svg" "图注：原始 value 依次经过 projectContent、post-execute、finalizeContent，再被物化成 tool/result；下方标出每一步抛错时结果会变成什么，以及 UI 与日志从哪一步分叉。" >}}

## 1. 一个结果，三个消费者

### 1.1 模型要的是"下一步能用的东西"

模型不关心 `stdout` 和 `stderr` 是两个字段还是两个对象，它关心的是"命令成功了吗、错在哪、我接下来该改什么"。所以送给模型的内容要用任务语言，而不是传输语言。一个 `bash` 结果里出现 `{"stdout":{"text":"...","truncated":true}}` 这种结构，模型要多花 token 解析，还容易把字段名当成命令输出的一部分。

这也是 `ToolDefinition` 把 `output.schema`、`execute`、`presentCall` 这些字段全部挡在模型请求之外的原因：模型只该看到 `{name, description, parameters}`。结果的投影同理：`content` 是专门为模型准备的那一份。

### 1.2 UI 要的是"能还原现场的东西"

UI 需要行号、文件路径、diff 的旧文本与新文本、终端的 cwd、搜索的命中位置。这些信息如果只存在于模型文本里，UI 就只能做正则解析，脆而且会随文案改动而坏。DSH 的做法是让工具返回一个结构化的 `ToolResultView`，由 presenter 产出卡片；模型文本和 UI 视图从同一个结果出发，但走两条路。

`read` 的 `FsReadMeta` 是一个典型：它把 `{path, offset, lines, totalLines, lang}` 存进 `tool/result.meta`，UI 重放时直接拿到行号和总行数，不需要重新读文件。模型侧看到的则是 `<path>…</path><type>file</type><content>…</content>` 的文本包络。

### 1.3 日志要的是"当时到底发生了什么"

日志不能只存模型看到的那份 content。原因很简单：模型看到的是策略改写后的结果，而审计要回答的是"原始结果是什么、谁改的、改成什么"。所以 `tool/call` 与 `tool/result` 是两条独立的 durable 事件，`tool/result` 里带 `isError`、`error.info`、`meta`，并且在需要时用 `sourceEventSeqs` 指回它依赖的 `tool/call`。

这三份数据一旦混在一起，就会出现一种很隐蔽的 bug：UI 为了好看改了 content，模型下一轮据此推理，日志里却记着"原始输出"，复盘时永远对不上。DSH 把"改 content"限制在明确的缝里，就是为了让这种分叉可追踪。

## 2. 三段缝：谁在什么时候改写

### 2.1 projectContent：工具作者解释自己的值

`projectContent(exec, result)` 的输入是已经规范化的结果，输出是 `content[]` 或 `undefined`。它在 `tools/post-execute` 之前运行，所以策略看到的是投影后的内容；策略仍然可以覆盖它。

```ts
const project = contentProjectors.get(exec)
delete contentProjectors.get(exec)
const content = project?.(exec, result)
if (content !== undefined) result = materializeFinalResult({ ...result, content })
```

看回调这一行：它在 `createExecution` 时就捕获了，并非等到结果回来时才去查注册表。这样即使工具在 await 期间被卸载，这次调用仍然用它开始时的那份投影逻辑，不会出现"执行到一半换了实现"。

还有一个容易忽略的点：deny 结果也走 projectContent。`prepare()` 在拒绝时返回的是 post-result，不是 final-result，所以被 guard 拒绝的调用会依次经过 project、post、finalize。这就是为什么一个被拒绝的 `bash` 调用里，模型仍可能看到工具自己写的摘要，投影函数对错误结果也要有定义。

### 2.2 post-execute：策略的最后一票

`tools/post-execute` 是 waterfall，返回值只有三种形状：`accept`（可带 content 或 value）、`block`。它不能改参数，因为参数已经落账、已经展示给用户；它也不能把失败结果换成一个成功 value，源码里直接抛 `TypeError('tools/post-execute cannot replace the value of a failed result')`。

`block` 的语义经常被误解。它表示把结果改写成一条错误反馈，并不删除结果：content 变成策略给的 feedback，`isError` 变 true，error.message 从 feedback 提取。原 value 被移除，但调用本身仍然有一次 `tool/result`。对模型来说，这是一次失败的工具调用；对日志来说，这是一次被策略阻断的调用。两者都成立。

### 2.3 finalizeContent：在所有改写之后收口

`finalizeContent` 是同步的、恰好一次的、必须不抛错的。它在 `finish()` 里运行，位置在所有 post 改写之后、无损物化之前：

```ts
const final = applyFinalContent(exec) ?? result
const materialized = materializeFinalResult(final)
```

看 `applyFinalContent` 这一行：它拿到的 `result` 已经是策略改写后的版本，所以它施加的边界是对最终结果的边界，并非对工具原始输出的边界。这正是 AGENTS.md 里"Apply bounds to the complete result"的落点。

它还必须覆盖"绕过 post 的失败路径"：如果 `tools/execute` 的 wrapper 抛错，pipeline 会直接进 final-result，跳过 project 与 post，但 finalize 仍然跑。`tool-jobs` 的 `job_output` 就是这么处理失败的：即使结果是 `Error: ...`，它也会按本次调用的 maxBytes 再裁一次。

### 2.4 顺序为什么不能换

把顺序换一下，会立刻出现三类问题：

- finalize 放在 post 之前：策略改写后的内容不再受边界约束，一个插件可以返回任意长的 content，最后一次收口被绕过。
- post 放在 project 之前：策略看到的是原始 value，无法基于"模型将看到什么"做决策；而 deny 结果的投影也永远不会发生。
- project 放在 normalize 之前：工具拿到的可能是异常对象或 wrapper 返回的任意形状，投影函数要自己处理所有非法输入。

所以顺序本身就是契约：**normalize → project → post → finalize → materialize**。每一步都假设前一步已经把自己的形状收拾干净了。

### 2.5 把 read 走一遍

以 `read` 为例，看三份投影如何从同一个 value 分叉：

1. 工具返回 `{path, offset, lines, totalLines, truncatedByBytes?}`。这是 value，也是唯一一份"真相"。
2. `output.render(args, value)` 把它变成模型文本：`<path>…</path><type>file</type><content>…</content>`，行号与 footer 都在里面。
3. `projectContent` 可以做进一步裁剪（比如按语言包一层），但不能再改 value。
4. `spill-policy` 在 post 里判断 token 预算；超了就把完整文本落盘，返回头尾 + notice。
5. `finalizeContent` 如果存在，会在所有这些改写之后再加一次硬边界。
6. `presentResult` 拿 `{content, isError, meta}` 生成行号卡片；`meta` 里的 `FsReadMeta` 是 UI 重放的依据。
7. `tool/result` 落账，`sourceEventSeqs` 指回那次 `tool/call`。

同一次读取，模型看到的是一段带包络的文本，UI 看到的是带行号的窗口，日志看到的是 content + meta + 事件。三者没有互相解析，也不需要。

## 3. value 与 content：程序值只活一次

`ToolExecutionSuccess` 里同时有 `value` 和 `content`，但它们的生命周期完全不同。`content` 会被物化、落账、发给模型；`value` 只存在于执行期，durable 事件里故意不写。

```ts
export interface ToolExecutionSuccess {
  readonly isError: false
  readonly value: unknown
  readonly content: ContentBlock[]
  readonly meta?: JsonValue
  readonly concludesTurn: true
}
```

看 `value` 这一行：它在类型上存在，但在 `materializeFinalResult` 里被剥掉，只留下 `{content, meta?, additionalContexts?}`。这样做的原因在于程序值可能不可序列化、可能很大、可能含敏感信息，省空间只是附带效果；它属于调用方，不属于对话历史。

这带来一个使用上的约束：`post-execute` 的 `accept.value` 只能替换成功结果的值，而且替换后会重新跑 `output.render()` 与 schema 校验。它表示换一个程序值再重新投影，并非"改一下模型看到的文本"。如果目标只是改展示，应该用 `accept.content`。

反过来，`accept.content` 也不是保密手段。文档写得很直白：content 替换是展示策略，不是保密策略；要隐藏程序值，必须 block 或替换 value。因为 value 仍然可能被同一次调用里的其他消费者（比如 PTC 程序）读到。

> 自己搭 Agent 时该怎么做：把"程序值"和"展示值"在类型上分开，而不是靠约定。即使你的第一版只用 JSON，也先定义 `value` 与 `content` 两个字段，并明确哪一个进历史、哪一个只活在调用栈里。等到你需要接 UI 或做审计时，这个分离会省掉一次大改。

## 4. 截断：三种策略，不是一个函数

"结果截断"最容易被实现成 `text.slice(0, N)`。这个实现会在四个地方出问题：多字节字符被切成半个、头尾信息丢失、截断后无法读回、截断发生在策略改写之前导致最终结果仍然超限。DSH 没有统一的 `truncate()`，而是三种场景三种策略。

| 场景 | 保留 | 续读方式 | 为什么 |
|---|---|---|---|
| `read` | 头部 + 行窗口 | `offset` / `limit` | 文件是随机访问的，读者知道下一段在哪 |
| `bash` | 尾部 | 完整输出落盘，给 `spillPath` | 错误通常在尾部，且进程输出不可重放 |
| `spill-policy` | 头 + 尾 | 不透明 `locator` + `retrievalHint` | 超 token 预算，但两端都可能有关键信息 |

### 4.1 read：保头，给窗口，给续读参数

`read` 的边界是行数与字节数双重的：单行超过 `READ_MAX_LINE_LENGTH = 2000` 会被截断并加后缀，整个结果超过 `READ_MAX_BYTES = 50 * 1024` 会停在一个行边界上。footer 有三种精确文案，分别对应"字节截断""行窗口未到文件尾""读完了"。

它最重要的设计是截断后仍然给出精确的 totalLines。`buildWindow` 即使已经超预算，也会把文件扫完，得到真实总行数；否则模型无法判断"我读到的是不是全部"。这是一种"多花一点 CPU，换一个可继续操作的上下文"的取舍。

`read` 还把行号放进 value 与 meta，而不是让模型自己数。行号是 UI 的坐标系，也是后续 `edit` 的定位依据；让模型从文本里推行号，迟早会错一行。

### 4.2 bash：保尾，全文落盘

`bash` 的选择相反：保留尾部。原因写在工具描述里："Long output is truncated to its tail; the full output is saved to a file whose path is reported when available." 编译错误、测试失败、栈回溯通常在最后几屏；保头会把最有用的部分丢掉。

保尾的代价是"前面发生了什么"不可见，所以它必须配一个 `spillPath`。这个路径是真实文件路径，不是 locator，模型可以用 `read` 打开它。注意这里的责任划分：`bash` 负责"把完整输出找个地方放下"，`spill-policy` 负责"模型上下文里只留多少"。两者不是同一层。

### 4.3 spill：保头保尾，给不透明 locator

当一份结果超过模型上下文的预算，`spill-policy` 会在 `tools/post-execute` 里动手：把完整内容写进 spill store，返回 `head + GAP + tail + notice`。head 与 tail 各拿一半预算，尽量保留两端；中间被省略的字节数与图片数写进 notice。

它保留的 locator 是品牌化的不透明句柄，消费方只能按 `retrievalHint` 渲染，不能解析成路径。这和 `bash` 的 `spillPath` 是两种不同的可见性模型：一个是"你可以直接读这个文件"，一个是"你可以按提示读回这份内容，但不要假设它是什么"。

### 4.4 边界要施加在完整结果上

三种策略共同的前提是：边界必须对最终要发出去的那份结果施加，不能对工具刚返回的原始值施加。因为 post-execute 可能追加 notice、可能替换 content、可能把结果包一层；只在工具内部裁一次，最终仍然可能超限。

`finalizeContent` 的存在就是为了补这一刀。它是同步的，因为它在物化路径上；它必须 total，因为抛错会把成功结果变成错误；它返回 `undefined` 表示"不改"，而不是"清空"。

### 4.5 边界测试该测什么

截断的 bug 大多出现在边界值附近，"正常长文本"很少能触发。下面五类输入应该成为固定回归集：

| 输入 | 想抓的 bug |
|---|---|
| 刚好等于上限 | off-by-one，差一个字节就多截一行 |
| 上限 + 1 字节 | 是否仍有 notice / 续读提示 |
| 单个超大块 | 无法按块切分时是否退回按字符切 |
| 多字节字符边界 | surrogate 对是否被切成半个 |
| 全是图片 / 全是空行 | 图片不可分、空文本块的保留策略 |

第五类最容易被漏掉。图片没有"切一半"的选项，所以预算不够时只能整块丢弃；如果代码假设"总能切出一点"，就会在纯图片结果上返回空内容或抛错。

> 自己搭 Agent 时该怎么做：不要写一个通用的 `truncate(text, n)` 然后到处调用。先列出你的结果有哪几种形状（文本、行、图片、结构化块），再为每种形状定义"保留哪一端、如何续读、边界怎么测"。通用的字符串裁剪看起来省事，实际上把三种不同的语义压成了一个错误的抽象。

### 4.6 为什么不用统一的 token 计数

一个自然的想法是让所有截断都按 token 算，这样看起来就统一了。问题在于 token 计数依赖 provider 与模型，而工具结果在生成时未必知道最终会发给谁。同一个结果可能被主模型、子 agent、PTC 程序消费，三者的预算不同。

所以 DSH 把边界分成两层：工具内部用字节/行数做**硬边界**（`READ_MAX_BYTES`、单行长度），保证结果不会无限增长；spill-policy 用当前 provider 的计价做**软边界**，保证模型上下文不超预算。硬边界与模型无关，软边界与模型有关，两者不能合并。

这也是 `finalizeContent` 存在的另一个理由：它是硬边界的最后一道，运行在知道完整结果的地方，但不依赖模型计价。

## 5. 落盘与 locator：把"读完"变成"可再读"

spill 的核心是给模型一个稳定的读回协议，存文件只是手段。`saveText` 接收完整的 content，返回 `{locator, bytes, retrievalHint}`；locator 由后端生成，模型只负责把它原样带进下一次 `read` 或提示里。

```ts
const ref = await spillStore.saveText({
  owner: { sessionId },
  source: { kind: 'tool', toolName, callId, label: 'result' },
  suggestedName: `${toolName}.txt`,
  content: fullText(content),
})
```

看 `source` 这一行：它带 `toolName` 与 `callId`。这让落盘文件不仅能被读回，还能被审计，"这份 spill 是哪次调用的结果"是一个可回答的问题，而不是靠文件名猜。

本地后端的路径是 `<root>/session-<sha256(sessionId)>/<random>-<safeName>`，root 权限 0700，文件用 `open(path, 'wx', 0o600)` 创建。`wx` 很关键：它拒绝覆盖已存在的文件，从而防住"预先放一个符号链接，让 spill 写到别处"的攻击。`suggestedName` 只是命名建议，后端会清洗成单段安全名，不允许路径分隔符。

还有一条常被忽略的规则：保存失败不能把成功调用变成错误。spill-policy 捕获所有异常，记一条 warn，然后保留原始内联内容。原因是 spill 是优化，不是正确性的一部分；为了省 token 而把一次成功的 `bash` 变成失败，是错误的优先级。

{{< fig "diagrams/02-spill.svg" "图注：完整结果写入 spill store 后，模型上下文只保留头、尾与一段 notice；locator 是不透明句柄，读回时按 retrievalHint 走，而不是解析路径。" >}}

## 6. additionalContexts：FIFO 与"记录后才注入"

工具结果不总是只回答"这次调用做了什么"。有些工具会产生需要后续模型看到的上下文：比如一个 `todo` 工具写完清单，希望模型下一轮记得清单；一个 PTC 子调用产生了图片，需要以用户消息的形式补进去。

这些内容走 `additionalContexts`，而不是塞进 `tool/result` 的 content。原因有两个：第一，它们不是这次调用的"结果"，塞进去会污染结果的语义；第二，它们的注入时机有严格要求。

批次结束时，`BatchSettlement` 按调用顺序收集 `additionalContexts`，主循环在所有 `tool/result` 都记录完之后，才把它们作为 `user/message` 注入。这就是"记录后才注入"：模型看到上下文的顺序，必须晚于它看到对应工具结果的顺序，否则会出现"上下文引用了还没发生的结果"。

顺序是 FIFO：先完成的调用先入队，注入时也先出现。`deferredContexts` 更特殊：它们在 dispatch 阶段被工具主动延后，pipeline 会把它们放在归一化结果的 contexts 之前。这样"执行中产生的上下文"与"结果自带的上下文"有一个稳定的相对顺序。

PTC 子调用是例外：它省略 `additionalContexts`。原因是子调用的 call/result 必须保持相邻，插入一条 user message 会打断配对，让 provider 认为工具调用悬空。子调用的图片改为走 `tool/ptc-dispatch-log` 的异步落盘路径，不进入主对话。

### 6.1 一个反例：把上下文塞进结果

假设你写了一个 `todo` 工具，想在结果里顺便告诉模型"当前清单"。最直接的实现是把清单文本拼进 content。这在单次调用里没问题，但会在三个地方坏掉：

- 模型看到的 result 变长，而且每次调用都重复同一份清单；
- UI 的 todo 卡片和 content 里的清单是两份数据，改一处不同步；
- 日志里的 result 带了"当时的清单"，但清单本身还有一条 `todo/write` 事件，回放时不知道以哪个为准。

正确做法是把清单放进 `additionalContexts`，让它在 result 之后作为 user message 注入一次；`todo/write` 仍然是唯一的 durable 状态。这样 content 只回答"这次调用做了什么"，上下文回答"你现在该知道什么"。

## 7. 配对：call 先落账，result 后落账

工具调用在模型协议里是一对：assistant message 里的 `tool-call` block，和随后的一条 `tool` 角色消息。缺任何一半，provider 都会拒绝请求。所以配对是硬要求：不配对就不能继续。

DSH 的顺序是：

1. `startCall` 先 append `tool/call`，拿到 `seq`；
2. `prepare` → `dispatch` → `complete`；
3. `commit` 再 append `tool/result`，并带 `sourceEventSeqs: [callSeq]`。

先落 call 的原因是可恢复性：如果进程在 execute 期间崩溃，日志里至少有一条"这个调用开始过"的记录，恢复逻辑才知道要补什么。先落 result 再落 call 则会产生一个无法解释的"结果先于调用"的历史。

`sourceEventSeqs` 把两者显式关联起来。它不是装饰：`tool/result` 只有在 `isError: true` 时才能带 `error`，而 `assistant/message` 禁止带 `sourceEventSeqs`，这些规则由 session 层校验，不是约定。

批次内还有一层顺序保证：`commitReady` 只提交连续就绪的 slot。并行调用可以乱序完成，但结果必须按模型给出的顺序落账。这样模型看到的 tool result 顺序与它发出的 call 顺序一致，配对不会因为并发而错位。

### 7.1 三种完成顺序，一个提交顺序

假设模型一次发了 A、B、C 三个调用，实际完成顺序可能是 C、A、B。`commitReady` 会等 A 就绪才提交 A，再等 B，再等 C。中途如果 A 一直不返回，B 和 C 即使完成也不能提交，这就是"模型顺序"对"完成顺序"的约束。

| 场景 | 完成顺序 | 提交顺序 | 模型看到 |
|---|---|---|---|
| 全并行、耗时均匀 | A B C | A B C | 与发出顺序一致 |
| 全并行、C 最快 | C A B | A B C | 仍是 A B C |
| A 是 barrier | A 完成后才发 B C | A B C | 与发出顺序一致 |

代价是：一个慢调用会拖住它后面所有已完成调用的提交，即使它们可以并行。收益是模型永远不需要在历史里"找"某个结果对应哪次调用，位置本身就是配对信息。

如果想让慢调用不拖累后面，只能把它拆成独立的 step。这是协议限制，不是实现限制：tool result 在消息序列里必须紧跟对应的 assistant tool-call 组，乱序插入会破坏配对。

> 自己搭 Agent 时该怎么做：把"完成顺序"和"提交顺序"当成两个独立的队列来设计。完成队列用 Promise.race 驱动，提交队列用连续下标驱动；不要因为某个结果先到就先写历史。写历史的地方只有一个，而且它必须知道"下一个该谁"。

## 8. 缺失的结果：ToolCallRecovery

即使有先落 call 的保证，仍然会出现"call 有、result 没有"的历史。原因可能是进程被杀、turn 被中断、会话被 fork。provider 不接受悬空调用，所以必须有人补一条结果。

`ToolCallRecovery` 做这件事。它在每个 step 里订阅 `session/event`：

- `assistant/message` 里的每个 `tool-call` block 记一条 pending；
- `tool/call` 给对应 entry 填 `callSeq`；
- `tool/result` 只在 `surfaceOp === 'append'` 且 turn/step 相同时删除 pending；
- `turn/start`、`turn/end`、`step/end` 清空 pending。

`results()` 按 Map 插入序（也就是 assistant 消息里的顺序）补出 `ToolResultMessage`，`isError: true`，并根据"有没有见过 `tool/call`"分成两种错误码：

| 情况 | 错误码 | 文案要点 |
|---|---|---|
| 见过 call，没有 result | `TOOL_OUTCOME_UNKNOWN` | 结果未知；只读或幂等才重试，有副作用先核实外部状态 |
| 没见过 call | `TOOL_NOT_STARTED` | 在记录为 started 之前就中断；需要的话可以重试 |

这两条文案不是客套话，它们是在教模型怎么做决策。把"未知"说成"失败"，模型会盲目重试一个有副作用的操作；把"没开始"说成"未知"，模型会去做不必要的外部核实。错误码和文案必须一起设计。

在 `agent.ts` 的 `step()` 里，恢复发生在 catch 分支：step 抛错时，先把 pending 结果补记，再决定是否抛 `AggregateError`。如果补记本身也失败，抛出的错误会同时带上原始错误与恢复错误，因为此时"step 失败"和"历史不合法"是两件事，不能只报一件。

> 自己搭 Agent 时该怎么做：恢复逻辑要在"每个 step 开始时"建立观察点，而不是在"进程启动时"扫一遍历史。前者能覆盖本 step 内新增的 pending，后者只能处理上次崩溃留下的尾巴。同时准备两种失败：补记成功、补记失败；后者必须让错误可诊断，而不是吞掉。

{{< fig "diagrams/03-pairing.svg" "图注：tool/call 与 tool/result 的配对状态机，标出正常路径、中断补齐、fork 补齐与补记失败四条分支；每条分支最终都必须让 assistant 的调用有且只有一条结果。" >}}

## 9. UI：presentCall 与 presentResult 为什么不参与模型内容

`presentCall(args)` 与 `presentResult(args, result)` 是纯函数，返回 `ToolCallView` / `ToolResultView`。它们的输入只有参数和 `{content, isError, meta}`，输出是卡片：generic、terminal、diff、search、read、web。它们不进入模型内容，也不应该进入。

原因有三层。第一，UI 需要的信息比模型多：`diff` 要旧文本与新文本，`terminal` 要 cwd，`read` 要行号窗口。把这些塞给模型是纯浪费。第二，UI 需要的信息比模型少：模型要的是一段可读文本，卡片结构对它没有意义。第三，也是最重要的：presenter 是可重放的。日志里存的是 `meta`，UI 重放时用同一个 presenter 重新生成卡片；如果 presenter 的输出进了模型内容，重放就会改变历史。

六种卡片对应六种"结果形状"，而不是六种工具：

| card | 典型来源 | 依赖的展示信息 |
|---|---|---|
| generic | 任意工具 | title、content |
| terminal | `bash` | stdout/stderr、cwd、exit code |
| diff | `edit` / `write` | `FileDiff{path, oldText, newText}` |
| search | `grep` | 命中文件与行号 |
| read | `read` | `ReadFileLine{number, text}` 窗口 |
| web | `web_fetch` / `web_search` | 标题、URL、摘要 |

`FileDiff.oldText` 允许为 `null`，表示新建或覆盖。presenter 在调用时看不到旧内容，旧文本必须由工具在 value 里带出来。这也是"UI 需要的信息比模型多"的具体例子：模型不需要旧文本，diff 卡片需要。

`read` 的 `FsReadMeta` 是这条规则的正面例子：它持久化在 `tool/result.meta` 里，`readMetaFromMeta` 会对重放数据做语义校验（offset ≥ 1、行号严格递增、不超过 totalLines）；校验失败就退回 generic 卡片，而不是抛错。UI 可以降级，历史不能坏。

反面例子是把"给用户看的摘要"写进 content，再让 UI 去解析它。这样做一开始很省事，等到文案改一次、UI 的解析全坏，就会发现模型和 UI 被同一份字符串绑死了。

{{< fig "diagrams/04-three-consumers.svg" "图注：模型、UI、日志三条消费路径从同一个结果分叉；标注每一条读的是 content、meta 还是 durable event，以及 presentCall/presentResult 只属于哪一条。" >}}

## 10. 同一结果的三份副本

理解了三条路径，就能解释三个看起来像 bug 的行为。

第一，deny 的结果仍然经过 project 与 post。拒绝本身也是一种结果，模型需要知道为什么被拒绝，UI 需要显示拒绝卡片，日志需要记录拒绝原因。如果 deny 直接跳过投影，模型只会看到一句裸错误，UI 也拿不到结构化的 info。

第二，`tools/ptc-dispatch-log` 只改 durable log 的副本。PTC 程序已经拿到了完整 value，模型两者都看不到；这个 waterfall 改的是日志里那份 content，用于审计与回放。它不能改程序拿到的值，也不影响模型，这是"日志消费者"独立存在的直接证据。

第三，`tools/result` 是只读通知。它在 `finish()` 里发出，参数是冻结的 `exec` 和结果；监听器的返回值被丢弃，抛错只记 warn。原因是此时结果已经物化，任何改写都会让"已通知"与"已落账"不一致。观察者可以记录、可以 metrics、可以触发副作用，但不能改结果。

这三条合起来，就是"一个结果、三份副本"的边界：模型副本经过投影与策略，UI 副本经过 presenter 与 meta，日志副本经过 durable event 与可选的水印。它们共享同一个 `callId`，但不共享同一份可变数据。

这也解释了一个常见的排查困难：用户说"模型看到的和界面不一样"。这句话本身不是 bug 报告，因为两份副本本来就可以不一样；要查的是"哪一份被谁改过、为什么"。日志里的 `sourceEventSeqs`、UI 的 `meta`、模型侧的 content，三者合起来才能回答这个问题。

## 11. 自己搭 Agent 时该怎么做

第一，先定义三个消费者，再定义结果类型。不要从 `{stdout, stderr, exitCode}` 开始设计；先问"模型需要看到什么、UI 需要渲染什么、日志需要重放什么"，再决定哪些字段进 content、哪些进 meta、哪些根本不落账。三份需求会自然逼出 `projectContent` 这样的缝。

第二，把"截断"实现成有续读协议的操作。至少提供一种读回方式：offset/limit、文件路径、或 locator。只给一段被裁掉的文本，等于把"结果太长"变成"结果丢了"。同时要保证截断发生在策略改写之后，否则最终结果仍可能超限。

第三，让恢复逻辑成为 loop 的一部分，而不是运维脚本。每次 step 开始时订阅事件、维护 pending，step 失败时补记。补记的结果必须带稳定的错误码和"该不该重试"的文案，并且要和原始错误分开报告。等到线上出现悬空调用再补，成本要高一个数量级。

第四，UI 投影保持纯函数，并且可重放。presenter 只读 args 与 `{content, isError, meta}`，不读全局状态、不发网络请求、不依赖当前时间。重放时用同一份 meta 重新生成，失败就降级成 generic，而不是让整条历史不可读。

第五，把"结果太大"当成一等失败模式来测试。构造刚好等于上限、上限加一、单个超大块、多字节边界、全是图片、全是空行这些输入。边界 bug 几乎都藏在这些角落里，正常长度的测试永远发现不了。

## 设计取舍

1. content 与 value 分离。模型只看 content，程序只拿 value。代价是工具作者要写两次投影（`output.render` 与 `projectContent`）；收益是模型上下文不会被程序结构污染，敏感值也不会因为"要展示"而被迫序列化。

2. 三段缝而不是一个 hook。代价是顺序复杂、文档必须写清楚谁先谁后；收益是每一段的所有权明确：工具作者管语义，策略管最终留什么，框架管物化。一个 hook 会让所有责任挤在一起，最后没人知道该在哪改。

3. 截断策略按场景分化。`read` 保头、`bash` 保尾、spill 保两端。代价是没有一个统一的"截断函数"可以复用；收益是每种场景都保留了最可能有用的那一端，而不是为了抽象牺牲效果。

4. spill 是尽力而为。保存失败保留内联，不把成功调用变错误。代价是极端情况下上下文仍然会超预算；收益是正确性不受存储可用性影响。

5. `tools/result` 只读。代价是观察者无法"最后再改一下"；收益是通知与落账不会分叉。需要改写结果的人应该去 post-execute，而不是在通知阶段动手。

6. UI 投影不进入模型内容。代价是同一个结果要维护两套呈现逻辑；收益是 UI 可以自由重放、降级、改版，而历史不变。

## 踩坑点

1. 在 `projectContent` 里改 value。现象：投影函数"顺手"把 value 规整了一下，后面的 post-execute 看到的值和工具返回的不一致。原因：`projectContent` 的契约只允许返回 content。对策：需要改值就改工具实现，或在 post 用 `accept.value` 并接受重新校验。

2. 在 `finalizeContent` 里做异步操作。现象：偶尔不生效，或者结果顺序错乱。原因：finalize 是同步的，运行在物化路径上。对策：异步工作放到 post-execute 或工具内部，finalize 只做纯计算。

3. 把 spill 的 locator 当路径解析。现象：换一个后端就坏，或者读回失败。原因：locator 是品牌化的不透明句柄，只有 `retrievalHint` 是给消费方的。对策：原样传递，按提示读回，不 split、不拼路径。

4. 截断后不更新 totalLines。现象：模型以为读完了，实际还有几百行；或者 `offset` 越界。原因：`buildWindow` 的 totalLines 是扫完文件得到的，手写的截断常常只算当前窗口。对策：要么扫完，要么明确告诉模型"总数未知"。

5. 在 tool result 里塞 `additionalContexts`。现象：provider 报 tool call 悬空，或者上下文出现在结果之前。原因：上下文必须在所有 result 记录之后注入，且 PTC 子调用不能带。对策：用 `exec.deferContext()` 或批次返回的 `additionalContexts`，不要在结果里硬塞。

6. 恢复结果只写文案，不给错误码。现象：模型看到"出错了"就重试，把一个有副作用的操作做了两次。原因：没有区分"未开始"和"结果未知"。对策：至少两个码，文案里写清楚该不该重试、先核实什么。

7. presenter 依赖全局状态。现象：重放时卡片和当时不一样，或者测试之间互相污染。原因：presenter 读了当前 cwd、当前时间或单例。对策：所有上下文从参数或 meta 来，纯函数。

8. 在 `tools/result` 里试图改结果。现象：返回值被忽略，日志和 UI 对不上。原因：通知发生在物化之后，且返回值被丢弃。对策：改写去 post-execute；通知只做记录与指标。

## 源码索引

| 位置 | 证明什么 |
|---|---|
| `packages/core/tools/src/pipeline.ts:336-364` 的 `complete` | project 在 post 之前，finalize 在 post 之后 |
| `packages/core/tools/src/pipeline.ts:486-525` 的 `postExecute` | accept content/value/block 三种形状与两类 TypeError |
| `packages/core/tools/src/pipeline.ts:374-389` 的 `finish` | 两次物化、finalize 恰好一次、失败转 isError |
| `packages/core/tools/src/pipeline.ts:400-419` 的 `notifyResult` | 冻结结果、返回值丢弃、观察者失败隔离 |
| `packages/core/tools/src/pipeline.ts:591-606` 的 `materializeFinalResult` | value 被剥掉，只留 content/meta/contexts |
| `packages/core/tools/src/contracts.ts:322-332` | `ToolExecutionSuccess.value` 只存在于执行期 |
| `packages/core/tools/src/contracts.ts:291-307` | `UNKNOWN_TOOL` 与 `reachableFrom` |
| `packages/core/tools/src/batch.ts:237-284` 的 `BatchSettlement` | call 先 append、result 后 append、`sourceEventSeqs` |
| `packages/core/session/src/repair.ts` 的 `ToolCallRecovery` | pending 的观察、补齐顺序与两个错误码 |
| `packages/core/agent-loop/src/agent.ts` 的 `step` catch | 补记失败抛 `AggregateError`，不掩盖原始错误 |
| `packages/spill/spill-policy/src/index.ts` 的 `bound` | 计价、spill、保留头尾、失败保内联 |
| `packages/spill/spill-policy/src/retention.ts` 的 `retainContent` | 头尾各半预算、二分裁剪、surrogate 安全 |
| `packages/spill/spill-policy/src/notice.ts` 的 `formatSpillNotice` | notice 的精确文案与可识别性 |
| `packages/fs/tool-fs/src/read-render.ts` | `READ_MAX_*`、三种 footer、精确 totalLines |
| `packages/fs/tool-fs/src/read.ts` 的 `readMetaFromMeta` | 重放校验失败退回 generic |
| `packages/shell/tool-bash/src/index.ts` 的结果描述 | 保尾 + `spillPath` 的公开承诺 |
| `packages/core/tools/src/presentation.ts` | 六种 card 与纯展示投影的类型边界 |
| `packages/core/tools/src/executor.ts` 的 `ToolBatchResult` | `additionalContexts` 随批次返回，而不是随单次结果 |
| `docs/subsystems/tools.zh.md` 的展示段 | `presentCall` 只依赖 args；presenter 不参与模型内容 |
| `docs/subsystems/spill.zh.md` | locator 的不透明性与“尽力而为”语义 |
| `docs/subsystems/session.zh.md:592` | `sourceEventSeqs` 与 `tool/result.error` 的约束 |

## 最后

工具结果的一生，比它看起来长得多：它从工具的一个返回值开始，经过投影、策略、收口、物化、落账、通知，最后分成三份，分别去模型、UI 和日志。每一段都有明确的 owner，每一段都可能失败，每一段都必须留下可追踪的痕迹。

下次看到一个"结果不对"的 bug，先别急着改工具。问三个问题：模型看到的是哪一份？UI 重放的是哪一份？日志里记的是哪一份？三份的答案往往不一样，而 bug 就藏在它们的缝里。

一份结果并不存在统一的"最终形态"：模型、UI、日志各自拿到停在该停位置的那一份。

上一篇：[11 沙箱、权限与审批：工具的边界](/posts/dsh-tool-boundaries.html)｜下一篇：[13 子 agent 生命周期](/posts/dsh-subagent-lifecycle.html)
