---
title: "上下文装配：AGENTS.md、时间与文件引用"
date: "2026-11-11T19:20:00+08:00"
summary: "注入的做法是注册一个 context section，由 loop 在每次 pre-step 时投影。本文拆开 DSH 的装配管线，看 AGENTS.md 的层级与 digest、时间的请求时区、@file 的指针语义，以及 runtime-context 为什么必须单独投影。"
description: "从 system prompt 的每次重渲染讲到 AGENTS.md 的增量注入、时间上下文的刷新时机与 @file 的解析时序：一套可复用的上下文装配心智模型。"
tags: ["context-engineering", "harness", "architecture"]
slug: "dsh-context-injection"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准；包内 `lib/` 是构建产物，引用一律指向 `src/`。
>
> 这是系列的第 18 篇。上一篇把记忆拆成五层，这一篇只看其中一层的入口：上下文是怎么被装配进一次请求的。

改完 `AGENTS.md` 里的一句约定，重新发消息，模型还是按老规矩来。你去翻会话记录，发现旧内容明明还在历史里，新内容也「应该」加进去了，实际没有。或者反过来：你在 system prompt 里塞了一段项目说明，第一轮有效，第二轮就消失了，因为那段文本只存在于你手工拼的那一次请求里。

这类现象看起来是「模型没看见」，背后通常是三个不同的问题：注入的时机不对（你在 turn 开始时算了一次，之后文件又变了）、去重不对（同样的内容被追加了三次，或者被更早的副本挡住）、失效不对（内容改了，但没有任何东西告诉装配管线它该重发）。

DSH 把这三个问题收进同一条管线：注入的动作是注册一个 context section，由 loop 在每次 pre-step 时投影，往 prompt 里加文本只是结果。section 是声明，投影是动作；声明可以来自任何插件，动作只有一个执行者。这篇就把这条管线从头到尾走一遍，再看 AGENTS.md、时间、`@file` 三个真实注入者分别怎么用它。

## 0. 结论

一条模型可见的上下文，从注册到进入请求要经过五个阶段：

1. 注册：插件调用 `systemPrompt.section()` 或 `systemPrompt.context()`，给出 `name`、`order` 和 `text`（字符串或 thunk）。
2. 装配：`assemble()` 在每次 pre-step 合并全局层与 scope 链上的 section、context、变量和工具 schema，再跑一次 `system-prompt/assemble` waterfall。
3. 渲染：`renderPrompt()` 把 section 拼成 system prompt 文本；`renderContextSections()` 把 context 拼成运行时快照。
4. 投影：loop 在 `agent/pre-step` 的 fallback 里追加一份 runtime-context，随后在 `step()` 里按需刷新并替换它。
5. 提交：system prompt 作为 `system/message` 落在 surface 节点 0；runtime-context 作为 `user/message` 落在本轮用户消息之后。

「模型有没有看见」由第 4、5 步决定：它被放在历史的哪个位置、有没有重复、下一次还会不会重发。第 1 步写了什么反而不重要。后文所有的坑，几乎都能归到这三件事上。

## 1. 注入是一条管线，不是一个函数

### 1.1 两种注册形状：section 与 context

DSH 的 `SystemPrompt` 服务接受两种声明。`PromptSection` 是「稳定的、属于系统提示的那部分」：

```ts
interface PromptSection {
  name: string
  order: number
  text: string | ((context: AssembleContext) => string)
  interpolate?: boolean
  complete?: boolean
}
```

`PromptContext` 是「易变的、属于本次请求的那部分」：

```ts
interface PromptContext {
  name: string
  order: number
  text: string | ((context: AssembleContext) => string)
  interpolate?: boolean
  required?: boolean
}
```

两者只差一个 `required` 和一个 `complete`，落点完全不同：section 进 system prompt，context 进 runtime-context 快照。判断标准是「它是否随每次请求变化」，「内容重不重要」不参与判断。工作目录、Web GUI 地址、当前时间这类东西，放进 section 会让 system prompt 每轮都变，缓存全废；放进 context，system prompt 保持稳定，变化被隔离在一条 user 消息里。

`text` 可以是 thunk，这很关键。thunk 在装配时求值，注册时不求值。于是「Web GUI 的当前 URL」「这个 agent 有没有 read 工具」这类判断可以延迟到实际渲染的那一刻；工具被移除后，section 自然渲染成空字符串，不需要谁去反注册。

### 1.2 order 决定位置，名字决定并列时的顺序

section 和 context 各有一套 order 表。`SECTION_ORDERS`（`packages/core/system-prompt/src/index.ts:143-178`）从 `HARNESS_IDENTITY: -1000` 到 `DEPLOYMENT_PERSONA_SUFFIX: 10200`；`CONTEXT_ORDERS`（同文件 `181-188`）从 `WORKING_DIRECTORY: 100` 到 `WEB_SURFACE: 210`。排序函数只有一行：

```ts
const comparePromptSections = (a, b) =>
  a.order - b.order || compareNames(a.name, b.name)
```

并列时按名字的 code unit 比较，而不是 `localeCompare`。原因很实际：装配结果要跨机器、跨进程、跨 Node 版本可复现；locale 排序会随 ICU 数据变化，同一份日志在两台机器上可能派生出不同的 prompt。名字比较是确定性的，代价是「看起来不自然的字母序」，这个代价值得付。

order 也不该随便写。它是层级契约：身份在最前，策略在中间，工具说明在后，交付物引用接近末尾。你把一个 section 写成 `order: 0`，它会挤到 persona 前面，把「你是谁」和「你能做什么」的叙述顺序打乱。表里预留了间隔（1000、5000、9000），就是给插件留插入空间。

### 1.3 assemble：一次 pre-step 只装配一次

`assemble()`（`packages/core/system-prompt/src/index.ts:620-689`）做四件事：

- 合并变量：全局层先铺，scope 链从最远到最近覆盖，最近的赢。
- 合并 section 与 context：全局 + scope 层，同名时近层遮蔽远层。
- 收集工具 provider：`structuredClone` 参数，`knownNames` 取并集，再按配置的 tool order 排。
- 跑 `system-prompt/assemble` waterfall：任何插件都能在这里改写最终 assembly；返回的文本是权威的。

装配发生在 `agent/pre-step` 之前（`packages/core/agent-loop/src/agent.ts:294`），每次 pre-step 一次。注意是「每次 pre-step」，不是「每个 turn」：一个 turn 里可能有多个 step（每次工具调用后进入下一个 step），每次 step 都可能换模型、换工具集、换上下文。装配结果 `assembly` 随后被传给 pre-step 的 waterfall，工具注入、AGENTS.md 探测、时间注入都在这个 waterfall 里决定要不要往 `decision.messages` 里塞消息。

{{< fig "diagrams/01-context-pipeline.svg" "上下文装配管线：provider 注册 section/context，assemble 合并并跑 waterfall，render 生成文本，loop 在 pre-step 与 step 两次投影，最后提交为 system/message 与 user/message。" >}}

图里有两条容易被忽略的回路。第一条：`assemble` 之后还有 `refreshContext`（`agent.ts:446`），它只重新求值已注册的 context provider，不会重跑 waterfall，也不会重新收集 section。也就是说，waterfall 里临时加进去的 context，在 step 阶段保持 pre-step 时的值；而注册过的 context（比如 Web GUI URL）会在实际发请求前再取一次最新值。第二条：`renderPrompt` 在 step 开始时只算一次（`agent.ts:442`），但 context 在每个 attempt 前都可能重算（`agent.ts:449-450`）。system prompt 稳定、runtime context 新鲜，这是两条不同的刷新策略。

## 2. system prompt 是每次请求重新渲染的

### 2.1 system prompt 是一条消息

很多框架把 system prompt 当作请求参数：`{ system: "...", messages: [...] }`。DSH 不这么做。system prompt 被渲染成文本，然后作为 `system/message` 追加到会话日志，成为 surface 的节点 0。模型看到的是派生历史里的这条消息，而不是请求里的一个字段。

这个选择带来一个反直觉的结果：系统提示也在「历史」里，也会被压缩、被 fork、被重放。但它不会被普通压缩选中：`selectCompactableRange` 从 `firstIdx = systemHead(...) === undefined ? 0 : 1` 开始（`packages/compaction/compaction-basic/src/region.ts`），节点 0 永远保留。压缩可以遮蔽节点 1 之后的任何东西，但不能动身份与策略。

把 prompt 放进消息，是因为「模型可见即已记录」这条规则没有例外。如果 prompt 是请求字段，那么一次请求的完整输入就无法只从日志重建；fork 出来的会话、崩溃恢复、转写、审计都会缺一块。放成消息之后，`deriveMessages()` 只需要折叠 surface，就能得到与当初完全一致的输入。

### 2.2 三种提交策略

`SystemPromptProjection.project(rendered, { inHistory, startsSeries })`（`packages/core/agent-loop/src/runtime-context.ts:93`）把「渲染结果」翻译成「日志提交」，只有四种结局：

- 没有头节点：追加渲染结果，哪怕是空字符串。空节点占住节点 0，给后续替换留位置。
- 需要重置：`!inHistory`、渲染为空、或新序列开始且文本不同时，清空后续所有非空节点，再按需改写头节点。这就是「同一段 prompt 不会在历史里堆 N 份」的机制。
- 文本相同：不提交任何东西。
- 需要追加：`systemPromptUpdate: 'in-history'` 且文本非空时，把新渲染作为一条新的 `system/message` 追加，形成「继续序列」。

`startsSeries` 的判断在 `agent.ts:452-456`：第一次 attempt 且 decision 声明开始新序列、surface 内容代际变了、或者工具集变了但准备好的调用没有声明工具更新。三者任一成立，就归一化到节点 0。工具集变化也会触发 prompt 重写，因为工具说明本身就在 prompt 里。

### 2.3 会话开头写死会怎么坏

假设你在会话开始时渲染一次 prompt，之后永远复用。坏掉的方式至少有三种。

工具集变了。子 agent 被派出去、某个工具被禁用、PTC 模式切换，工具 schema 变了，prompt 里的工具说明却还是旧的。模型会按旧说明调用一个已经不存在的工具。

scope 变了。agent 从主会话切到子会话，工作目录、persona、策略 section 都可能不同。写死的 prompt 把主会话的身份带进了子会话。

恢复与 fork 是第三种。进程重启后，内存里没有「当初渲染的那份文本」；你只能从日志里找。如果 prompt 不在日志里，就找不回来；如果它在日志里但你不重新渲染，就无法解释「为什么这条消息的内容和当前配置不一致」。

重新渲染换来的是 prompt 成为配置的函数。配置没变，渲染结果相同，提交被跳过，成本接近零；配置变了，渲染结果不同，提交发生，历史里多一条可解释的变更。

### 自己搭的时候

把 system prompt 当消息存，不要当请求参数。每次请求前重新渲染，但只在文本变化时提交；提交策略至少要有「替换头节点」和「追加更新」两种，否则要么历史膨胀，要么模型看不见变更。判断「是否变化」要用渲染后的完整文本，不要用配置对象的引用相等：thunk 的求值结果可能变，配置对象却没变。

## 3. AGENTS.md：层级、去重与 digest

### 3.1 发现顺序：从用户全局到当前目录

`discoverInstructionFiles`（`packages/context/agent-instructions/src/files.ts:323-376`）的扫描顺序是固定的：

1. 用户全局：`dshHome/AGENTS.md`，以及 `agentsHome` 下的同名文件。两者合成一个候选组。
2. 项目根到 cwd 的祖先链：每一层先找 `AGENTS.md`、`CLAUDE.md`，再找 `AGENTS.local.md`、`CLAUDE.local.md`。
3. 工具触碰过的子目录：由 `descendantDirsBetween(root, touchedPath)` 算出 cwd 到目标文件之间跨过的目录，浅的在前。

项目根由 `findProjectRoot` 决定：从 cwd 向上找第一个含 `.git` 的目录；找不到就退回 cwd。所以 monorepo 里每个包自己的 `.git` 会截断祖先链，`AGENTS.md` 的继承范围随之变化。这是有意的：项目边界应该由版本控制边界定义，而不是由文件系统层级碰巧决定。

顺序不只是「先读谁」，它还决定了同名冲突时谁赢。后读的不会覆盖先读的，只会在候选组内做内容去重；跨组则各自保留。

### 3.2 去重：组内去重，组间不合并

`dedupInstructionFilesByCandidateGroup`（`files.ts:430-453`）的规则可以压缩成一句：不同组永不合并，即使内容完全相同；同一组内保留发现顺序最早的候选，渲染它的原始字节。

组是怎么分的？用户全局的两个目录算一组；项目祖先链上的每个目录各算一组。于是：

- 根目录 `AGENTS.md` 和 `packages/foo/AGENTS.md` 内容一样，两份都保留。因为它们属于不同组，适用范围不同：一份管全仓，一份只管那个包。
- 同一个目录下 `AGENTS.md` 和 `CLAUDE.md` 内容一样，只保留先发现的 `AGENTS.md`。它们属于同一组，是同一份规则的两个入口。
- `AGENTS.local.md` 和 `AGENTS.md` 内容一样，也只保留先发现的那份（按候选顺序是 `AGENTS.md`）；local 文件只是同组的另一个候选，不构成覆盖。

`candidateScopeKey` 用 NUL 分隔目录与候选名，保证 `AGENTS.md` 与 `CLAUDE.md` 永远不会被误判成同一个 scope。这个细节很小，但少了它，去重会把两个不同文件当成同一个。

### 3.3 baseline 只发一次，之后发增量

第一次 pre-step 时，`compose()`（`packages/context/agent-instructions/src/index.ts:109-243`）读出完整的 baseline，渲染成一条 user 消息，source 标记为：

```ts
{ kind: 'agent-instructions', form: 'instructions', baseline: true, baselineIdentity, changes }
```

`baselineIdentity` 是 `workspaceBaselineIdentity()` 的 JSON：项目根相对路径、marker 列表、字节预算、候选文件名。它用来判断「恢复出来的 baseline 还是不是同一个工作区的」。identity 不匹配，baseline 就要重发；匹配，就信任日志里现有的那份。

之后的注入不再发全文。工具成功触碰文件后（`read`、`write`、`edit`，且不是错误、未被取消），`projectTouch` 把路径记进 inbox；`step/end` 后统一 `compose`，写进下一步的 next-step 队列。`reconcileInstructionContext` 对每个相关 scope 做三件事：

- 探测：`probeScopeInstruction` 返回 `unavailable`、`absent` 或文件及其 `FsVersion`。
- 比版本：`metadataUnchanged = cached.path === displayPath && cached.version === probedFile.version`。版本没变且 digest 没变，跳过。
- 比 digest：`instructionContentSha1` 算精确内容，`trimmedInstructionDigest` 算 trim 后的内容。前者判断「字节变没变」，后者判断「语义上是不是同一份」。

版本缓存只存 `{ path, version, digest, trimmedDigest }`，不存正文。注释写得很直白：instruction prose is deliberately not retained。正文每次需要时重新读，缓存只负责「要不要读」。

### 3.4 digest 变化如何触发重新注入

变化最终落成三种 action：

- `set`：这个 scope 之前没有可见版本，现在有了。
- `replace`：路径没变（或之前是 remove），内容变了。
- `remove`：文件消失，或该 scope 不再适用。

`renderInstructionChanges`（`packages/context/agent-instructions/src/render.ts:280`）把它们渲染成一段带 `<system-reminder>` 的增量消息；`state.ts` 里再包一层 `reconcile` 的结果。这里有一行容易忽略：如果渲染文本为空，或者 changes 为空，直接返回 `undefined`，不提交。注释解释了原因：未提交的版本会让下一轮重试，而不是反复发「通知类」空消息。这个点很容易写错：检测到变化但渲染不出内容时，正确动作是「这轮不发」，发一条「有变化」的通知只会变成噪声。

还有一个隐蔽的规则：同一组里靠后的文件如果和前面某份的 `trimmedDigest` 相同，会被当作重复移除，但保留 metadata。这样下次文件再变时，仍然能基于缓存判断，而不是重新读一遍。去重和失效在这里是耦合的：你不能为了去重把版本信息一起丢掉。

### 3.5 为什么增量消息放在用户消息之后

`agent/pre-step` 监听器（`index.ts:330-356`）拿到 `decision.messages` 后，先找最后一个被认领的消息：

```ts
const lastClaimedIndex = decision.messages.findLastIndex(message => messages.includes(message))
const entered = decision.messages.toSpliced(lastClaimedIndex + 1, 0, desired)
```

注释说明了位置理由：Fold the context right after the claimed batch, so the direct prompt precedes it and the driver-appended runtime context follows it。翻译过来：用户直接说的话在前，指令变更在中间，驱动追加的运行时上下文在最后。模型读到的顺序是「你要我做什么 → 项目规则有什么变化 → 当前环境是什么」，规则不会插到用户问题前面去当附注。

{{< fig "diagrams/02-agents-hierarchy.svg" "AGENTS.md 的层级与去重：用户全局一组，项目根到 cwd 每层一组，工具触碰的子目录按需探测；组内按内容去重，组间不合并；baseline 只发一次，之后按 digest 发增量。" >}}

### 自己搭的时候

按目录 scope 组织指令，每个 scope 维护 `{ path, version, digest }` 三元组，不要把整份 `AGENTS.md` 每轮重发。baseline 用 identity 校验，确保恢复会话时不会把「另一个工作区的规则」当成自己的。增量渲染为空时不要提交通知消息；下一次探测会重试，比留下噪声更便宜。

## 4. 时间上下文：为什么不能只在 turn 开始注入

### 4.1 采样点在 pre-step 的末尾

`time-context` 的监听器注册在 `agent/pre-step`，并且是 `prepend: true`（`packages/context/time-context/src/index.ts:199`）。它先 `await next()`，拿到其他监听器（包括 AGENTS.md 增量）处理完的 `decision`，再决定要不要追加时间消息。追加的是一条 `user/message`（`source.kind === 'time-context'`），落在历史尾部，不进 system prompt。先等下游跑完再追加，是为了让时间快照排在指令增量与 runtime-context 之后：模型先读任务与规则，最后读「现在几点」，时间戳也因此最接近实际发请求的时刻。

采样文本包含三行：

```text
Time sampled while preparing turn 3, step 2: 2026-11-11T19:20:03+08:00[Asia/Shanghai]
Browser time zone for this request: Asia/Shanghai. Interpret otherwise-unqualified dates and times in this zone.
Elapsed since the preceding step context: 4m 12s
```

第一行是绝对时间，带偏移和 IANA 区域名；第二行是请求时区；第三行是相对上一条「模型可见消息」或「上一步上下文」的间隔。

### 4.2 按间隔节流，不按 step 发

默认 `refreshIntervalMs = 600_000`（10 分钟），设为 0 则每个 eligible step 都发。间隔的作用是让时间戳成为有信息量的信号，省 token 只是顺带：同一分钟内的两个 step，时间几乎相同，多发一条只会让历史更长、缓存更容易失效。

节流的判断在 `now - lastInjection < refreshIntervalMs` 时直接返回原 decision（`index.ts:209-213`）。注意 `lastInjection` 是「上一次实际注入时间」，不是「上一次 step」的时间。工具跑了很久但没跨过间隔，就不会重复注入；跨过了，下一次 step 会补上。

### 4.3 请求时区，不是进程时区

这是整节最容易搞错的一条。`request-zone.ts` 从消息的 `source` 里取时区，而不是读 `process.env.TZ`：

```ts
reportedBrowserTimeZone(message) // 仅 source.kind === 'user' 且 rpcId/clientTimeZone 都是 string
```

浏览器在发消息时把 `clientTimeZone` 带上来，服务端用 `canonicalizeTimeZone` 校验：必须是 UTC 或 `Area/Location` 形式的 IANA 名，否则抛 `browser time zone must be canonical: …`。一个 turn 里如果出现了多个不同时区（比如两个客户端同时发消息），`deriveBrowserTimeZoneContext` 返回 `{ kind: 'mixed', timeZones }`，渲染文本会明确写 mixed，并要求模型「ask the user to clarify otherwise-unqualified dates and times」。缺失时返回 `missing`，同样要求澄清，而不是猜一个默认值。

不能用进程时区，因为进程时区是部署细节：容器默认 UTC、开发者本机是 Asia/Shanghai、CI 可能是 UTC。用户说「明天下午三点」，指的是他所在时区的明天下午三点。用进程时区解释，轻则差八小时，重则在 DST 切换日差一小时。把时区跟着请求走，时间语义才是可复现的：同一份日志在任何机器上重放，得到的绝对时间一致。

`formatTimestamp` 的输出格式也为此服务：`2026-11-11T19:20:03+08:00[Asia/Shanghai]`。偏移让模型能直接做算术，区域名让它在 DST 边界上有依据。只给 `+08:00` 不够，因为夏令时地区的偏移会变；只给区域名也不够，因为模型不一定知道当前偏移。

### 4.4 elapsed 的两个基准

`step === 1` 时，基准是 `lastMessageTime`（上一条模型可见消息的时间）；之后的 step，基准是 `lastTurnInjectionTime`（本 turn 上一次注入的时间）。这个区分是为了让第一行 elapsed 回答「用户等了多久」，后续行回答「距离上次上下文过了多久」。基准不存在时渲染 `unavailable`，不编造一个 0。

{{< fig "diagrams/03-time-refresh.svg" "时间上下文的刷新时机：turn 开始、每次工具 step 结束、pre-step 末尾采样；10 分钟节流窗口内跳过，跨窗口补发；时区随请求携带并在混合时要求澄清。" >}}

### 自己搭的时候

把时区存进请求，而不是读进程环境。每条时间消息都带偏移和区域名，并明确写出「未限定日期按此时区解释」。一个 turn 内出现多个时区时，宁可让模型追问，也不要静默选一个。elapsed 的基准要区分「用户消息」和「上一步上下文」，否则长任务里模型会误判自己已经等了多久。

## 5. file-reference：@ 只产出指针

### 5.1 语法：@ 必须是一个 token 的开头

`activeAtToken(line, cursorCol)`（`packages/context/file-reference/src/grammar.ts`）只认两种形态：

```text
@path/to/file
@"path with spaces"
```

正则要求 `@` 出现在行首或空白之后。所以 `foo@bar.com` 不会触发补全，`a@b` 也不会：`@` 在另一个 token 内部时只是普通字符。引号形态允许路径里带空格；`formatFileMention` 对目录会返回 `@"dir/`，故意不闭合引号，让用户继续往里输入。

这个语法的边界很窄，是刻意的。补全器不需要理解 Markdown、代码块或自然语言，只需要知道「光标前是不是一个正在输入的 @ 引用」。窄语法意味着低误报，用户才敢用。

### 5.2 它不读文件内容

这一点最容易被误解。`@file` 的产物是一段文本，不是文件内容。服务只负责发现路径，选中的值作为普通 prompt 文本发出去。文件内容仍然在模型-facing 的 `read` 工具后面。

`search.ts` 的模块注释写得很直接：The index contains paths only: selected values remain ordinary prompt text and file contents stay behind the model-facing `read` tool。补全索引里只有路径，没有正文；读取、截断、行号、分页都由 `read` 工具负责，超长输出走 spill。

不做「选中即注入」，有三个原因：

第一，注入会绕过读取的审计面。模型读了哪个文件、读了哪一段、有没有截断，都应该在 `tool/result` 里留下记录。直接把内容塞进用户消息，等于在日志里伪造了一次「用户说的」，来源就不可信了。

第二，用户选中一个文件，不等于要读它的全部。一个 5000 行的日志文件，选中只是为了说「看这个」，模型应该先 `read` 头部或 `grep` 关键行，而不是把 5000 行灌进上下文。

第三，截断策略属于读取，不属于引用。上下文预算的决策应该发生在 `read` 工具里，那里知道当前剩余预算、知道怎么分页；引用层不知道，也不该猜。

### 5.3 section 的可用性跟着工具走

`FILE_REFERENCE_PROMPT` 不是无条件注册的。`file-reference-local` 在每个 agent 的 scope 里注入 section（`packages/context/file-reference-local/src/index.ts:70-74`）：

```ts
scope.systemPrompt.section({
  name: 'context:file-reference',
  order: scope.systemPrompt.getSectionOrder('FILE_REFERENCE'),
  text: () => agent.ctx.tools.get('read', agent) === undefined ? '' : FILE_REFERENCE_PROMPT,
})
```

`text` 是 thunk：没有 `read` 工具时返回空字符串。空 section 在 `renderPrompt` 里被过滤掉，于是「@ 引用该怎么处理」的说明不会出现在一个根本没有读取能力的 agent 里。这比在注册时判断更稳：工具可能在运行中被移除，thunk 每次装配都会重新求值。

同时，这个注入是 per-agent 的 scoped registration，agent 销毁时 fiber 一起销毁，不会泄漏到别的会话。

### 5.4 补全索引的边界

本地实现默认 `maxResults: 20`、`maxEntries: 50_000`，排除 `.git`、`node_modules`、`dist`、`build`、`coverage`、`target`、`.next`、`.venv`、`__pycache__` 等目录。`lib` 被刻意排除在外：Ruby gem 和 npm 包的源码经常放在 `lib` 里，禁掉它会误伤真实项目。

索引在每次 `tool/result` 后失效。模型刚写完一个文件，用户马上 `@` 它，补全应该能看见新文件，而不是等 watcher 的下一个周期。

{{< fig "diagrams/04-file-reference.svg" "@file 的解析时序：输入 @ → 语法匹配 → 路径候选 → 选中后只把路径写进 prompt → 模型决定是否调用 read → 内容与截断发生在工具结果里。" >}}

### 自己搭的时候

把「引用」和「读取」拆成两个动作。引用层只产出路径文本，读取层负责内容、截断和审计。不要在选中文件时自动注入正文；如果你担心模型不读，应该改 prompt 里的一行说明，而不是替它做读取决策。

## 6. runtime-context：为什么单独投影，且只投影一次

### 6.1 pre-step 的 fallback 是最后一个投影点

`agent/pre-step` 的 waterfall 有一个 fallback（`packages/core/agent-loop/src/agent.ts:299-307`）：

```ts
const sections = renderContextSections(assembly)
const context = this.runtimeContext.project(joinContextSections(sections), sections)
return {
  kind: 'enter',
  messages: context === undefined ? claimed : [...claimed, context],
}
```

它在所有监听器之后执行。这个顺序不能反：监听器可能追加 AGENTS.md 增量、技能目录、时间消息，甚至修改 section；只有在它们都跑完之后，才能把最终的 context 渲染出来，并放到消息列表末尾。

`joinContextSections` 会加一行头部：`Current runtime context. This snapshot supersedes earlier runtime-context snapshots.` 这一行不是装饰，它告诉模型：历史里可能有多份快照，只有最新这份有效。没有这一行，模型会把旧的工作目录或旧的 URL 也当成仍然成立的事实。

### 6.2 step 里重新投影，并替换掉 pre-step 那份

pre-step 的投影只是「让 decision 里有一条 context 消息」。实际发请求前，`step()` 还会再来一次（`agent.ts:446-472`）：

```ts
const currentContext = this.loopCtx.systemPrompt.refreshContext(assembly, { ... })
const sections = renderContextSections(currentContext)
const context = this.runtimeContext.project(joinContextSections(sections), sections)
```

然后遍历 `decision.messages`：遇到 `source.kind === 'runtime-context'` 的消息，改用刚投影出来的 `context` 追加进去，并把 `contextAdmitted` 置为 true；如果新的 `context` 是 `undefined`（与已保留的快照相同），就什么都不追加。遍历结束后，如果 `context` 存在且还没 admitted，再追加一次。

结果是：一次请求里最多只有一份 runtime-context，而且是最新的那份。pre-step 那份如果过期，会被丢弃；如果内容没变，`project` 返回 `undefined`，也不会重复。

pre-step 之后还要再投影一次，因为 pre-step 到实际发请求之间还有事发生：模型选择可能已经确定、工具集可能变化、`prepareRequest` 可能改写调用。`refreshContext` 只刷新注册过的 context provider，不重跑 waterfall、不重新收集 section，它是廉价的，适合在每次 attempt 前跑。代价是 waterfall 里临时加的 context 不会刷新，这个取舍在文档里写明了：waterfall 加的 context 保持 accepted 值。

### 6.3 retained 状态怎么恢复

`RuntimeContextProjection` 不拥有提交，只跟踪「最后一份保留的快照」。构造时从 `session.snapshotEvents()` 反向扫描，找到 surface 上最新的 runtime-context `user/message`，记下 `{ seq, text }`（`runtime-context.ts:119-146`）。它监听 `session/event`：

- 自己范围内的 `user/message` 被 append，更新 retained。
- 某个 replace 事件的 `sourceEventSeqs` 包含 retained.seq，说明旧快照被替换掉了，retained 置空。

`project`（`runtime-context.ts:157-160`）在两种情况下返回 `undefined`：从未有过快照且当前为空；或者当前文本与 retained 完全相同。当前为空但曾经有过快照时，它返回一条 `CLEARED` 消息：`Current runtime context: none. Earlier runtime-context snapshots no longer apply.` 这条消息显式撤销旧快照，免得模型继续用上一次的工作目录。

### 6.4 全放进 system prompt 的问题

原因在缓存和语义两件事。

缓存上，system prompt 是前缀，最值得稳定。把每分钟变一次的时间、每个请求可能变的 URL 放进去，前缀每轮都变，provider 的 KV cache 基本失效。runtime-context 是 user 消息，位于历史后部，变化只影响尾部，前缀仍然可复用。

语义上，system prompt 是「规则」，runtime-context 是「事实」。规则应该长期稳定，事实应该随时间更新。把事实混进规则，模型会分不清「这是永久约束还是当前状态」。分开之后，`required: true` 的 context 还能在 `suppressRuntimeContext()` 打开时继续保留：操作性的上下文（工作目录、沙箱策略）不被静默关掉，可选的上下文（Web URL、时间）才受影响。

### 6.5 「只投影一次」约束的是副本数量

它约束的是历史里的副本数量。如果 pre-step 追加一份、step 再追加一份，历史里就有两份快照；下一轮再各追加一份，就有四份。模型会看到互相矛盾的旧事实，压缩还要为这些重复内容付 token。DSH 的做法是：pre-step 只负责让 decision 有一条占位消息，step 负责用最新值替换它，最终只 append 一次。提交点只有一个，历史里就只有一份。

回到第 1 节那张管线图：pre-step 的 fallback 在 waterfall 之后投影一次，step 在发请求前刷新并替换；system prompt 只在文本变化时提交，runtime-context 每次请求最多一份。同一个管线，两个提交点，各自负责一种稳定性。

### 自己搭的时候

把易变事实从 system prompt 里拿出来，放进「每次请求一份、后写覆盖前写」的快照消息。快照头部明确写「本快照取代之前的快照」，并在清空时发一条显式的 none 消息，而不是静默省略。投影函数要能返回 `undefined`，让调用方知道「不需要提交」，这比提交一份相同文本更省历史。

## 7. 去重与 order：几条容易写错的规则

### 7.1 五层去重，各有各的键

| 去重层 | 键 | 作用范围 | 冲突时 |
| --- | --- | --- | --- |
| section | `name` | 全局 + scope 链 | 近层遮蔽远层 |
| context | `name` | 全局 + scope 链 | 近层遮蔽远层 |
| 指令文件 | 候选组 + 内容 SHA-1 | 一个 workspace | 组内保留发现最早的一份 |
| 注入消息 | `content` + `source` 深比较 | 一个 session | 复用已有消息，删除重复 pending |
| 技能目录 | catalog digest | 一个 session | 最后发布的目录替换旧的 |

这五层不能互相替代。section 去重解决的是「同一个名字被注册两次」；指令文件去重解决的是「同一份规则出现在多个目录」；消息去重解决的是「同一份内容被 append 两次」。少了任何一层，都会在某个真实场景里出现重复。

### 7.2 空文本等于不存在

`renderPrompt` 会过滤空字符串，`renderContextSections` 也会。所以「渲染成空」是一种合法的删除方式：不需要反注册，也不需要维护 enabled 标志。file-reference 的 section 就是这么做的：没有 `read` 工具时返回空串，section 自动消失。

但空文本不能用于「通知」：如果你检测到变化、却渲染不出内容，正确动作是不提交，而不是提交一条「有变化」的空消息。AGENTS.md 的 `reconcile` 在这里专门做了保护：`rendered.text.length === 0 || rendered.changes.length === 0` 时返回 `undefined`。

### 7.3 complete section 是唯一的

`complete: true` 的 section 至多一个，多于一个直接抛错。它的语义是「整个 system prompt 由我接管」：assemble 之后，如果存在 complete section，它会被恢复成唯一的 prompt section，其他 section 都不参与渲染。这是给「完全自定义 prompt」的场景留的逃生舱，不是常规写法。常规写法应该是注册多个 section，用 order 排序。

### 7.4 required 与 suppress 是控制面

`required: true` 的 context 在 `suppressRuntimeContext()` 打开时仍然保留；可选的 context 被跳过。抑制器注册在每一层的 `runtimeContextSuppressors` 里（`system-prompt/src/index.ts:404`），`suppressRuntimeContext()` 返回一个释放函数，调用后恢复。它关掉的是「这次装配里的可选上下文」，不删历史里的消息。控制面的事情留在控制面，不污染历史。

### 7.5 order 并列时不要依赖注册顺序

section 和 context 各自排序，order 相同则按 name 的 code unit 比较。注册顺序不参与排序。于是两个插件都写 `order: 1000`，谁在前只取决于名字，不取决于谁先加载。这提高了可复现性，但也要求你给 section 起一个能表达归属的名字，而不是 `section-1`、`section-2`。

## 8. 设计取舍

| 选了什么 | 放弃了什么 | 什么场景该反悔 |
| --- | --- | --- |
| system prompt 作为日志里的消息 | 请求字段的简单直接 | 需要把 prompt 完全排除在历史之外、由网关统一注入时 |
| 每次 pre-step 重新 assemble | 一次装配、全程复用的便宜 | 装配成本远大于请求成本，且 section 几乎不变时，可缓存 assembly |
| runtime-context 每次请求一份 | 让模型看到上下文的变化历史 | 需要审计「模型当时看到的是哪份快照」时，应同时保留事件序列 |
| AGENTS.md 按 digest 发增量 | 每轮重发全文的简单 | 指令很短（几百 token）且变更频繁时，重发全文更省心 |
| @file 只做指针 | 选中即注入的顺滑 | 模型没有 read 工具，或读取成本高到需要预取时 |
| 时间按请求时区 | 进程时区的零配置 | 单租户、单时区部署，且请求里拿不到客户端时区时 |

这张表里没有「绝对更好」的选项，只有「在这个约束下更好」。比如「每次 pre-step 重新 assemble」，在单机、section 很少的场景里确实是浪费；但一旦支持多 agent、scope 遮蔽、工具动态变化，缓存 assembly 的失效逻辑会比重新装配更复杂。DSH 选择了简单可复现的那一侧。

## 9. 踩坑点

### 1. 在 turn 开始时算一次上下文

现象：长任务里模型反复使用旧的工作目录，或者看不到刚改的 `AGENTS.md`。
原因：注入点选在 turn 开始，之后的 step 不再重新投影。
对策：把注入挂到 `agent/pre-step`，并且在 step 发请求前再 `refreshContext` 一次；易变事实用 context，不要用 section。

### 2. 每次请求都 append 一份 system prompt

现象：历史里出现十几条内容几乎相同的 `system/message`，token 暴涨，压缩频繁触发。
原因：只实现了「追加」，没实现「文本相同则跳过」和「序列开始则归一化」。
对策：提交前比较渲染文本；相同不提交，不同按 `inHistory` 决定替换头节点还是追加更新。

### 3. 改了 AGENTS.md 却没有发增量

现象：文件确实变了，模型仍然按旧规则。
原因：版本缓存只比较了 mtime，或者变化被去重逻辑当成「同组重复」吞掉了。
对策：内容 digest 与 `FsVersion` 一起比较；去重时保留 metadata；`remove` 也要渲染成一条变更，不能静默删除。

### 4. 用 localeCompare 给 section 排序

现象：同一份日志在两台机器上派生出的 prompt 顺序不同，缓存命中率飘忽。
原因：locale 排序依赖 ICU 数据，跨环境不稳定。
对策：order 相同用 code unit 比较，或者给每个 section 分配不重复的 order。

### 5. 时间用进程时区，或者每个 step 都发

现象：用户说「明天」被解释成服务器时区的明天；或者历史里堆满只差几秒的时间戳。
原因：时区来源错了；节流没做。
对策：时区随请求携带并校验 IANA 名；设置最小刷新间隔，跨间隔才补发；混合时区要求澄清。

### 6. @file 选中就注入正文

现象：一次引用把整个文件灌进上下文，或者模型声称读过但日志里没有 `read` 调用。
原因：把「引用」实现成了「预读」。
对策：引用层只产出路径；读取走 `read` 工具，截断和分页留在工具里；prompt 明确写「不要声称已检查」。

### 7. runtime-context 在 pre-step 和 step 各留一份

现象：历史里同一轮出现两条 runtime-context，内容还略有不同。
原因：只做了追加，没做替换；或者投影函数在内容相同时仍返回消息。
对策：step 遍历 decision 时识别 `source.kind === 'runtime-context'` 并替换；`project` 在文本相同时返回 `undefined`。

### 8. 空渲染也提交一条消息

现象：模型不断收到「工作区指令有变化」但没有实际内容的消息。
原因：把「检测到变化」当成「必须提交」。
对策：渲染为空或 changes 为空时返回 `undefined`；让下一轮探测重试，而不是用通知填空。

## 10. 源码索引

- `packages/core/system-prompt/src/index.ts:143-178`：`SECTION_ORDERS` 的完整顺序，身份在 -1000，persona 后缀在 10200。
- `packages/core/system-prompt/src/index.ts:181-188`：`CONTEXT_ORDERS`，工作目录 100 到 Web surface 210。
- `packages/core/system-prompt/src/index.ts:296-305`：`renderPrompt()`，按 order 拼接、过滤空串、`\n\n` 分隔。
- `packages/core/system-prompt/src/index.ts:320-325`：`joinContextSections()`，加上「本快照取代之前快照」的头部。
- `packages/core/system-prompt/src/index.ts:336`：`renderContextSections()`，返回 name + text 的结构化 section。
- `packages/core/system-prompt/src/index.ts:588-611`：`refreshContext()`，只刷新已接受的 context provider。
- `packages/core/system-prompt/src/index.ts:620-689`：`assemble()`，变量覆盖、scope 遮蔽、tool provider、waterfall、complete section。
- `packages/core/agent-loop/src/agent.ts:294`：每次 pre-step 调一次 `assemble()`。
- `packages/core/agent-loop/src/agent.ts:299-307`：pre-step fallback，在 waterfall 之后投影 runtime-context。
- `packages/core/agent-loop/src/agent.ts:442`：`renderPrompt(assembly)`，每个 step 只算一次。
- `packages/core/agent-loop/src/agent.ts:446-450`：发请求前 `refreshContext` + 再次投影。
- `packages/core/agent-loop/src/agent.ts:459-472`：用最新 context 替换 decision 里的占位，保证一轮最多一份。
- `packages/core/agent-loop/src/runtime-context.ts:93`：`SystemPromptProjection.project()`，四种提交结局。
- `packages/core/agent-loop/src/runtime-context.ts:119-160`：retained 恢复、事件跟踪、相同则返回 `undefined`、清空时发 `CLEARED`。
- `packages/context/agent-instructions/src/index.ts:109-243`：`compose()`，baseline + 增量 + 去重账本。
- `packages/context/agent-instructions/src/index.ts:330-356`：pre-step 监听器，增量插在 claimed batch 之后。
- `packages/context/agent-instructions/src/files.ts:323-376`：发现顺序，用户全局 → 项目根到 cwd → 触碰的子目录。
- `packages/context/agent-instructions/src/files.ts:430-453`：按候选组去重，组间不合并。
- `packages/context/agent-instructions/src/state.ts:168`：`baselineInstructionState()`，记录 `set` 与 digest。
- `packages/context/agent-instructions/src/render.ts:280`：`renderInstructionChanges()`，把 set/replace/remove 渲染成增量。
- `packages/context/time-context/src/index.ts:199-225`：pre-step 末尾采样，节流后追加时间消息。
- `packages/context/time-context/src/request-zone.ts`：从 `clientTimeZone` 取时区，resolved / mixed / missing 三态。
- `packages/context/time-context/src/timestamp.ts`：`formatTimestamp()` 输出 `YYYY-MM-DDTHH:mm:ss+08:00[Asia/Shanghai]`。
- `packages/context/file-reference/src/index.ts`：`FILE_REFERENCE_PROMPT`，明确「@ 是路径，不是内容」。
- `packages/context/file-reference/src/grammar.ts`：`activeAtToken()` 与 `formatFileMention()`，引号与目录的处理。
- `packages/context/file-reference-local/src/index.ts:70-74`：section 是 per-agent 注入，没有 `read` 工具时渲染为空。
- `packages/context/harness-dev-context/src/index.ts`：同一份信息拆成稳定 section + 每请求求值的 context，thunk 每次装配重新取值。

## 11. 小结

上下文装配的难点在「什么时候写、写在哪、下次还要不要写」，写什么反而容易。section 负责稳定的规则，context 负责易变的事实；system prompt 每次重渲染但只在变化时提交，runtime-context 每次请求最多一份、后写覆盖前写。把这三件事分开，AGENTS.md、时间、`@file` 就只是同一条管线上的三个注册者。

---

上一篇：[Agent 的记忆不是一块：五层与两个方向](/posts/dsh-memory-architecture.html) ｜ 下一篇：[压缩：把历史折叠成工作集](/posts/dsh-compaction.html) ｜ [系列目录](/posts/dsh-architecture-anatomy.html)
