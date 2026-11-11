---
title: "压缩：把 transcript 当堆来回收"
date: "2026-11-11T19:50:00+08:00"
summary: "压缩不是摘要，是一次带事务的 surface 改写：三个触发入口、一次日志锁、先剪工具结果再摘要、切点退到工具成对边界。含 retainTokens 与成对性算法的源码级拆解。"
description: "DSH 的压缩实现：为什么先剪工具结果、切点怎么选、tool_call 与 result 为什么不能拆开，以及崩溃后那条没人收尾的 compaction/start。"
tags: ["context-engineering", "harness", "architecture"]
slug: "dsh-compaction"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。行号以该 commit 为准。
> 本系列第 19 篇。系列目录：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)。

一个会话跑到第 183 个事件，窗口用了 78%。模型正在改一个三千行的文件，前面有三次 `grep`，每次返回 8KB。你打开日志，删掉最早四十条消息，保存，继续。下一次请求被拒：

```
messages.3: tool_use ids were found without tool_result blocks
```

你删的那一段里，有一条 assistant 消息带了两个 `tool_call`，它们的结果在下一屏。消息删了，调用留下了。

换一种删法。你请模型先总结一遍旧对话，把总结塞回开头。请求通过了，模型也继续干活。十分钟后它问：「你刚才说的那个配置文件，是哪一个？」摘要里写的是「修改了配置文件并运行测试」。路径、命令、报错原文，全没了。

再换一种。你手动执行 `/compact`，得到一句「压缩正忙」（`busy`）。屏幕上没有进度条，没有后台任务，上一次进程是被 `kill -9` 杀掉的。日志里躺着一条 `compaction/start`，没有对应的 `end`。

这三个场景对应三件事：**成对性**、**事实保真**、**崩溃恢复**。它们都不是「摘要写得好不好」的问题。压缩的难点在于，它是一次会失败、会留下痕迹、会影响下一次请求能否发出的写操作。

## 0. 一句话结论

压缩不是摘要，是一次带事务的日志改写：先做零模型成本的回收（剪工具结果、省略图片），再按 token 预算选一段**连续**区间，用一次模型调用换成一个 checkpoint 节点，最后把「遮蔽了哪些 seq」写成可审计的记录。摘要只是其中一步；成对性与可重试性才是正确性。

## 1. 先把两个东西分开：日志与 surface

DSH 的会话是只追加的事件日志。`user/message`、`assistant/message`、`tool/call`、`tool/result`、`turn/start`、`turn/end`，每条都有连续的 `seq`。日志不原地修改，崩溃时留下的半开括号也原样保留。

模型看到的不是日志，是 **surface**：一条由 `SessionSeq[]` 组成的序列，只包含消息类事件。它由 `foldSurface(events)` 从头重放得出。你可以把日志理解成堆上的对象，surface 理解成当前活着的引用集合。

压缩只改后者。它不删除任何日志事件，只在 surface 上做一次替换：

| 维度 | 压缩前 | 压缩后 |
| --- | --- | --- |
| 日志事件数 | 183 | 186（多了 start / summary / end） |
| surface 节点数 | 41 | 36 |
| 被遮蔽的 seq | 无 | 记在 `shadowedSeqs` |
| model 请求里的消息 | 41 条 | 36 条，其中一条是摘要 |
| `replaceGeneration` | 7 | 8 |

有三个事件只写日志、**不进 surface**，这是有意设计的：

- `compaction/start { turn }`：`turn` 是自动压缩所属的轮次；手动压缩写 `null`。
- `compaction/summary { summary, shadowedRange, shadowedSeqs, shadowedTokenCount, provider, model, usage }`。
- `compaction/end { turn, error? }`。

它们不扩展 `SurfaceEventType`。理由很直接：如果 `start` 进了 surface，模型就会在请求里看到「正在压缩」这条消息，然后开始讨论压缩本身。压缩是宿主的内务，不该出现在模型的对话里。

真正改变 surface 的只有一条普通的 `user/message`，它带一个 `surfaceOp`：

```ts
// packages/core/session/src/surface.ts:505 起（结构示意）
{ op: 'replace', startSeq, endSeq }
```

这条消息的内容是 `<compacted-summary>` 包起来的八段文本。对模型来说，它就是一条用户消息；对日志来说，它是一次替换的落点。

## 2. 三个入口，一个事务

{{< fig "diagrams/01-triggers.svg" "图注：压力压缩、溢出恢复、手动命令三个入口各自决定阈值与保留量，之后都进入 compactSurfaceRegion 的同一段事务：校验、写锁、准备、摘要、提交与释放。" >}}

### 2.1 每个 step 之前的压力检查

`agent/pre-step` 在每个 step 进入前触发一次。它按**已提交的 request header** 计价，而不是按当前 surface 的临时状态：

```ts
// packages/compaction/compaction-basic/src/index.ts:157（结构示意）
ctx.on('agent/pre-step', async ({ agent, signal }, next) => {
  try {
    await this.compactIfNeeded(agent, 'pressure', signal)
  } catch (error) {
    logger.warn(`step compaction failed: ${error}; continuing the turn`)
  }
  return next()
})
```

注意这个 `catch`。压缩失败不会中断 turn，只写一条 warn。原因不是「压缩不重要」，而是「压缩是内务」：一个正在改文件的 turn 不应该因为记账失败而报废。代价是失败会被静默吞掉，所以它必须留下日志（后面会讲）。

如果配置本身有问题（窗口太小、`retainTokens` 大于阈值），抛的是 `TargetPressureConfigError`。这个错误按 `error.targetKey` 只警告一次，避免每个 step 刷一行同样的日志。

### 2.2 请求被拒之后的溢出恢复

`agent/request-error` 只接一个错误码：`CONTEXT_WINDOW_EXCEEDED_CODE`。别的错误一律 `next()`，不碰。

这个入口和压力入口有三个区别：

1. 此时 surface 是**失败前那一版**，请求已经证明它太大了。
2. 保留量被强制设为 0。尾部预算本身就是问题来源，没有理由再留 16%。
3. 恢复动作是**重发同一个 step**，不是新开一步。

重发这件事很关键。`step()` 内部的 `for(;;)` 在收到 `{ kind: 'retry' }` 后 `continue`，不会重跑 `preStep`，也不会新开 `step/start`。所以下一次请求仍然带着同一份工具定义、同一段系统提示，只是 surface 变了。

### 2.3 手动 `/compact`

`/compact` 不接受参数。它走 `agent.runMaintenance`，要求 agent 处于 idle，且没有打开的 turn。满足条件后写一对 `turn: null` 的独立括号，压缩完再 `flush` 会话；flush 失败会把整个操作报成 `persistence`。

为什么手动路径要求 idle？因为它是**独立的**：自动压缩发生在 turn 内部，括号天然被 turn 包住；手动压缩发生在 turn 之间，如果此时还有 turn 在跑，就会出现两个写者。`compactNow` 把任何 `runMaintenance` 的拒绝都包成 `ManualCompactionError('busy')`，文案是「manual compaction requires an idle agent」。

### 2.4 第四个入口：模型切换

`agent/request` 上还挂了一个 `{ prepend: true }` 的监听器。它先 `await next()` 拿到最终路由，再比较 provider 与 model 是否和已提交的 header 一致。不一致就先按新模型的窗口压一次。

失败时抛 `LlmError('MODEL_SWITCH_COMPACTION_FAILED')`，文案明确说「The previous request model is preserved」。也就是说：切模型失败不会把请求发到一个装不下的窗口里，它保留旧模型，让调用方决定。

**如果自己搭 Agent**：把压缩做成事件钩子，不要做成循环里的 `if (tokens > limit)`。钩子让「什么时候压」和「怎么压」分开，三个入口可以共享同一个事务函数；循环里的 `if` 迟早会长出第二份实现，然后两份实现对于「什么算超限」给出不同答案。

## 3. 成本序：先剪，再量，最后摘要

{{< fig "diagrams/02-before-after.svg" "图注：左侧是压缩前的十个节点，中间六个被标为摘要区间，其中一个工具结果先被剪掉中段，图片留在尾部；右侧是替换后的 surface，区间变成一个 checkpoint 消息，工具对与最近消息原样保留。" >}}

压缩有三层，成本依次上升：

| 层 | 动作 | 模型调用 | 可逆性 | 默认触发 |
| --- | --- | --- | --- | --- |
| 0 | 图片 offload（换成占位文本） | 0 | 只改投影，字节仍在 | 摘要失败且错误要求让图 |
| 1 | 工具结果剪枝（保留头尾） | 0 | 旧结果仍在日志里 | 结果超过 8192 字符 |
| 2 | 区间摘要（一次 `llm.stream`） | 1 | 被遮蔽的 seq 仍可回查 | 超过 threshold |

顺序不是审美，是成本。一次摘要请求的输入约等于被压区间的全部 token，输出还要再算一次钱；剪枝只做字符串切片，连 tokenizer 都不需要完整跑一遍。

剪枝的实现很克制。默认保留头 4096 字符、尾 1024 字符，中间插一段标记：

```
[... tool result middle pruned ...]
```

结果必须比原来小、且落在阈值内，否则抛错而不是硬切。非文本 block 原样保留且保持顺序，按 Unicode code point 切，不会劈开 surrogate pair。

更关键的是它**写日志**。每次替换前先追加一条 `compaction/prune`，记录 `shadowedSeqs: [seq]` 与被替换内容的 token 估算；然后追加新的 `tool/result`，带 `surfaceOp: { op: 'replace', startSeq: seq, endSeq: seq }`。所以剪枝不是「就地改字符串」，它和摘要共用同一种可审计的替换语义。

`compactPressure` 的流程是：

1. 按 header 计价 `measure`。低于阈值直接返回 null。
2. 有 pruner 就先剪，剪完**重新 measure**。
3. 如果已经低于阈值，返回 null。**不生成摘要。**
4. 仍然超，才进入 `for (attempt = 0; attempt <= spec.compactionRetries)`：选区间、摘要、再 measure。

第 3 步是整个子系统里性价比最高的一行。大量「上下文快满了」的会话，其实只是背着几个巨大的工具输出；剪掉中段就能回到阈值以下，一次模型调用都不用花。

**如果自己搭 Agent**：先写 pruner，再写摘要。pruner 一天能写完，且能覆盖大部分超限场景；摘要要处理失败、截断、模型选择、成对性，一周起步。反过来做，你会用最贵的手段解决最便宜的问题。

## 4. 区域选择：头不动、尾按预算、切点退让

{{< fig "diagrams/03-region-selection.svg" "图注：surface 从上到下由旧到新。头节点永远排除；从尾部向前累加 token 得到候选切点；候选切点若落在工具调用与其结果之间，就继续向前退，直到两侧都成对。" >}}

`selectCompactableRange` 只有三条规则，代码不到二十行：

```ts
// packages/compaction/compaction-basic/src/region.ts:117 起（结构示意）
const firstIdx = systemHead(surface[0]) ? 1 : 0
let keepFrom = surface.length
let kept = 0
while (keepFrom > firstIdx && kept < retainTokens) {
  keepFrom -= 1
  kept += pricedNodes[keepFrom].tokens
}
if (keepFrom <= firstIdx) return null
while (keepFrom > firstIdx && !toolPairingBalancedBefore(session, surface[keepFrom])) {
  keepFrom -= 1
}
return { start: surface[firstIdx], end: surface[keepFrom - 1] }
```

三行注释就能说清：`surface[0]` 如果是 system/message，`firstIdx` 从 1 开始；从尾往前累加，达到 `retainTokens` 就停；停下的位置如果不满足「切点之前所有工具调用都已闭合」，就再往前退一格。

第一条规则值得单独说。system prompt 永远不进压缩区间，因为它是请求的头，provider 的前缀缓存依赖它；而且它只能被 `system/message` 整体重写，不能被摘要成一段用户文本。

第三条规则是本文的核心，下一节展开。这里先说第二条：**保留最近多少，按 token 算，不按条数算**。

| 维度 | 保留最近 N 条 | `retainTokens` |
| --- | --- | --- |
| 计量单位 | 消息条数 | 估算 token |
| 稳定性 | 一条工具结果可能是另一条的 40 倍 | 与窗口约束同一量纲 |
| 极端情况 | 保留 3 条也可能是 40k token | 保留量有上界 |
| 实现成本 | 一个 `slice` | 需要 tokenMeter |
| 与阈值的关系 | 无法比较 | `retainTokens < thresholdTokens` 是配置校验 |

`retainTokens` 不是「保留多少条消息」，所以它对消息大小不敏感；代价是它依赖估算。估算会偏，偏了就会「压完仍然超阈值」，于是需要第 4 节末尾那个重新 measure 的循环。

默认值是 `retainRatio = 0.16`，乘以消息预算（窗口减去预留输出），门槛是 `thresholdRatio = 0.8`。这两个数字的推导在[预算那篇](/posts/loop-budget-dsh-opencode.html)里已经做过，这里不重复。只需要记住一点：`retainTokens` 必须小于 `thresholdTokens`，配置阶段就会校验，否则压完立刻又触发。

### 4.1 溢出路径为什么把 retain 设成 0

请求已经被拒，说明「保留 16% 的尾部」这个策略在当下不成立。此时 `selectCompactableRange(session, measurement, 0)`，尾部预算为零，切点直接落在最后一个成对边界上。

这不是「压得越狠越好」，而是「先让请求能发出去」。压完之后下一轮 `pre-step` 会按压力阈值重新评估，该保留的尾部会在后续 turn 里自然长回来。

### 4.2 选不出区间时返回 null

如果退到头也不平衡，或者 `keepFrom <= firstIdx`，函数返回 null。上层有两种处理：压力路径把 null 当作「没得压」，直接返回；溢出路径在 `requireBelowThreshold` 为真时抛错，文案是「no safe compaction range can reduce context below threshold」。

这个区分很重要。手动 `/compact` 在没有可压历史时返回 `null`，命令层显示「No compactable history yet.」，而不是报错。

**如果自己搭 Agent**：切点选择不要做成「找最老的 N 条」。做成「从尾部留一个预算，然后向前找合法切点」。前者在工具密集的会话里几乎必然切穿一对调用；后者把合法性检查放在最后一步，代价只是少压几条消息。

## 5. 成对性：一条 API 契约，不是风格问题

模型 API 对工具调用有硬约束：每个 `tool_use`（Anthropic）或 `tool_calls`（OpenAI）后面必须跟对应的结果，id 要匹配，顺序要相邻。少一个，请求直接被拒。

这个错误和上下文溢出不是一类。溢出有专门的错误码，可以被 `agent/request-error` 捕获并恢复；成对性错误是 400，落在「请求本身不合法」那一类，没有恢复路径，整个 turn 直接失败。

所以压缩必须在切点上证明一件事：**切点之前的 surface 里，没有未闭合的工具调用**。DSH 把它做成两个导出的谓词：

```ts
// packages/compaction/compaction/src/index.ts
toolPairingBalancedBefore(session, seq): boolean
toolPairingBalancedAfter(session, seq): boolean
```

名字里的 before/after 指的是「切在这个节点之前 / 之后」。两个都验证当前 surface 成员关系，都拒绝缺失的 seq 与遗留结果。

实现不用 step 标记，而是按 surface 顺序折叠一个计数：

```ts
// packages/compaction/compaction/src/tool-pairing.ts:29（结构示意）
function eventDelta(event: SessionEvent): number {
  if (event.type === 'tool/result') return -1
  if (event.type === 'assistant/message') {
    return event.data.message.content.filter((b) => b.type === 'tool-call').length
  }
  return 0
}
```

一条 assistant 消息带两个调用，delta 就是 +2；两个结果各 -1，回到 0。计数为负说明日志损坏，直接抛 corrupt surface。

为什么不用 step？因为 step 边界比工具调用粗。一个 step 里可以有多次工具调用，step 结束时它们都闭合了；反过来，一个 step 也可能因为中断而没有闭合。用 step 判断会把「可以安全切」的位置判成不安全，也会把「不安全」判成安全。

### 5.1 三种容易判错的边界

**并行调用**。一条 assistant 消息里两个 `tool_call`，两个结果分别落在后面两条事件。切点必须在 assistant 消息之前，或者在两个结果之后；夹在中间任何一个位置都不平衡。

**错误结果**。工具执行失败也会写 `tool/result`，里面是错误文本。它仍然算「已闭合」。不能因为结果是错误就把它当缺失。

**被 offload 的图片**。图片省略改的是消息内容，不删事件。`tool/result` 还在 surface 上，计数照旧。

### 5.2 为什么允许切开一个 turn

区域边界保持工具对，但不保持整个 turn。`compactSurfaceRegion` 的注释说得很清楚：过大的 turn 里，较早关闭的 step 可以被压缩。

这条设计让压缩的粒度不受 turn 限制。一个 turn 可能跑二十次工具，前半段早就结束了；如果必须整轮保留，压缩在长任务上等于失效。

**如果自己搭 Agent**：把成对性写成一个可测试的谓词，输入是「节点序列 + 切点」，输出是布尔值。不要在压缩函数里内联判断，也不要依赖「我们的 step 总是完整的」这种假设。中断、取消、崩溃都会破坏它。

## 6. 一次事务的五个阶段

`compactSurfaceRegion` 是一段显式事务。五个阶段各自负责一种失败：

| 阶段 | 写什么 | 崩溃后可见 | 谁能继续 |
| --- | --- | --- | --- |
| validate | 不写 | 无变化 | 任何入口 |
| start | `compaction/start` | 一条无配对的 start | 无人，直到人工处理或新生命周期 |
| prepare | 不写 | 仍只有 start | 本次操作 |
| summarize | 不写（模型调用） | 仍只有 start | recovery waterfall |
| commit | `compaction/summary` + 替换消息 | summary 有了，替换可能没落 | 手动路径报 `commit` |
| end | `compaction/end` | 括号闭合 | 任何入口 |

有几个细节值得盯。

**start 与校验同步相邻。** 顺序是：校验区间、检查是否有活跃压缩、然后立刻 `append('compaction/start')`，中间不 `await`。如果先校验、再 await 别的东西、最后写锁，那段时间里第二个入口可以通过同样的校验，于是两个压缩同时跑。

**durable marker 就是锁。** 判断「有没有压缩在跑」不靠内存标志，靠日志里有没有未配对的 start。进程重启后内存标志没了，日志还在。这是这套设计能跨崩溃的原因。

**summary 先于替换。** `compaction/summary` 是事实：哪个模型、花了多少 token、遮蔽了哪些 seq。替换消息是效果。先记事实再落效果，崩溃在中间时至少知道发生过什么。

**end 只有一次机会。** 失败路径尝试恰好一次写 `compaction/end { error }`。如果连这次都失败，日志里就留下无配对的 start，下次任何入口都会因为 `assertCompactionInactive` 而 busy。

**手动路径才 flush。** 自动压缩不主动刷盘，它依赖会话正常的持久化节奏；手动压缩是用户显式要求的，写完要立刻落盘，否则「压缩成功了但进程随后挂了」会得到一个说不清的状态。

## 7. 摘要的内容契约

摘要不是「让模型总结一下」。它是一份固定八段的交接文档：

1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code
4. Errors and Fixes
5. Pending Jobs
6. Current Work
7. Next Step
8. Critical Context

空段必须写 `(none)`，不能省略。必须保留路径、命令、错误原文、数值、函数签名。禁止提到「压缩过」这件事。如果输入里已经有 `<compacted-summary>`，要合并而不是照抄。

外层还有一段 preamble，把摘要定位成「已确立的背景」，要求后续模型不要复述、不要确认这个 checkpoint：

```ts
// packages/compaction/compaction-basic/src/summarizer.ts:70（节选）
// 这段摘要已经发生，不要向用户复述它，也不要确认收到
```

没有这段，模型看到一条以「以下是之前对话的摘要」开头的用户消息，很容易回一句「好的，我已经了解之前的对话」。这一句话会污染后续所有输出。

### 7.1 摘要不能比被替换的内容更大

提交前有一个检查：把摘要消息按真实路由价重新估算，如果 `framed >= shadowedRouteTokenCount`，抛错：

```
summary is not smaller than the shadowed content
```

这条检查防的是「摘要写得比原文还长」。模型完全可能把八段都填满，尤其是代码片段多的时候。压完反而更大，这次压缩就是纯亏损，还多花了一次调用。

注意这里用的是两个不同的计价：`shadowedTokenCount` 用启发式估算（shadow-price 协议，写进事件里给审计看），`shadowedRouteTokenCount` 用真实路由价（用来和摘要比较）。分开的理由是事件一旦写入就不该依赖当前模型，而「是否变小」这个判断必须用当下真实的价。

### 7.2 摘要请求本身命中前缀缓存

`buildSummarizationInput` 拼的是：system 头 + 当前请求的 tools + 区间内每条消息，摘要指令**只追加在最后**。

这不是随手写的顺序。上一次请求的前缀正好是「system + tools + 这些消息」，摘要请求只是在这个前缀后面多了一段指令。provider 的前缀缓存因此可以命中，输入侧的成本显著下降。

如果反过来，把指令放最前面，整个前缀就变了，缓存全丢。一次摘要的输入可能几万 token，这个差别不小。

**如果自己搭 Agent**：摘要 prompt 用固定 schema，不要让模型自由发挥；把「不要提压缩」写进指令；把指令放在最后而不是最前。前两条决定摘要能不能用，第三条决定它贵不贵。

## 8. surfaceOp 与 generation：位置不是数值

替换的 API 长这样：

```ts
{ op: 'replace', startSeq, endSeq }
```

`startSeq` 与 `endSeq` 是 surface 上的**位置**，不是数值区间。替换完成后，摘要消息的 seq 比被它遮蔽的部分 seq 更大，所以完全可能出现 `startSeq > endSeq`。

为什么不用数值区间？因为日志里 seq 单调递增，但 surface 的顺序由替换历史决定。一次替换可以把 seq 100 到 140 换成一个 seq 160 的消息；下一次压缩的区间起点可能是 160，终点是 150 附近的位置。数值比较在这里没有意义。

`planSurfaceEvent` 对 replace 做四件事：

1. 用 `indexOf` 找 start 与 end。找不到，或 startIdx > endIdx，抛错。
2. 校验 `sourceEventSeqs` 覆盖每一个被遮蔽的节点，必须引用更早的事件，不得重复。
3. 如果是 `tool/result` 的重写，校验它仍然对应同一个调用。
4. 如果是头节点（startIdx = 0），只允许 `system/message` 原位重写。

然后 `applySurfacePlan` 做 `splice(startIdx, endIdx - startIdx + 1, plan.seq)`，并把 `state.replaceGeneration` 加一。

`replaceGeneration` 是这套机制里最容易被忽略、但最重要的一根线。它回答的问题是：「上一次请求失败之后，surface 有没有真的变过？」请求失败恢复靠它做决定，下一节展开。

还有一条元数据规则：`replace` 的 `startSeq` / `endSeq` 必须小于事件自身的 seq。你不能引用一个还没发生的位置。反过来，非 surface-eligible 的事件带 `surfaceOp` 会直接报错，surface-eligible 的事件缺 `surfaceOp` 也会报错。两个方向都堵上，避免「看起来像替换但没生效」。

**如果自己搭 Agent**：不要用「删除消息」实现压缩。删除会立刻破坏成对性，而且不可回查。用「替换 + 遮蔽集合」，日志保留原文，surface 只记当前成员。多出来的那张映射表，就是你后来排查「模型为什么不知道这件事」的唯一依据。

## 9. 失败、重试与抖动刹车

{{< fig "diagrams/04-failure-brake.svg" "图注：正常路径是触发、写锁、摘要、提交释放；失败分摘要失败、提交失败、进程崩溃三类，各自在日志里留下不同痕迹；底部三条计数规则决定还能不能再试。" >}}

手动路径的六个错误码，基本覆盖了所有失败面：

| 码 | 含义 | 用户该做什么 |
| --- | --- | --- |
| `busy` | 有活跃压缩，或 agent 不 idle | 等，或先停掉当前 turn |
| `cancelled` | 取消优先于其他失败 | 重试 |
| `changed` | 选中历史在替换前变了 | 重试，已记录的尝试可查 |
| `summary` | 没产出有用摘要 | 重试，或换个摘要模型 |
| `commit` | 可能已部分改写 | **先检查状态再重试** |
| `persistence` | 压缩成功但存盘失败 | 检查磁盘，别当成功 |

`commit` 的文案值得单独看。它不说「失败」，说「可能已部分变更」。因为 summary 事件与替换消息是两次 append，中间崩掉就会出现「事实记了、效果没落」的状态。这时候让用户直接重试，可能压出第二份摘要。

自动路径没有这么细的码，它靠两个计数器：

- `overflowRetries`：溢出恢复次数，默认上限 1。
- `compactionRetries`：同一次压力下最多再试几轮摘要，默认 1。

清零的时机有两个：`assistant/message` 成功落盘，以及 agent 进入 idle。前者意味着「模型已经成功回答了一次」，一次成功响应足以证明当前 surface 是合法的；后者意味着这一轮结束了。

### 9.1 为什么重试可能再次触发

请求失败后，`agent/request-error` 返回 `{ kind: 'retry' }`，step 内部 `continue`，重发同一个请求。它不重跑 `pre-step`，所以压力入口不会在这个时刻再介入；能再次触发压缩的只有 `agent/request`（模型切换）与 `agent/request-error` 自己。

如果压缩没有真正改变 surface，重发的请求和上一次一模一样，还是会被拒。于是出现「重试 → 又被拒 → 再重试」的循环。刹车就是在这里：

```ts
// packages/compaction/compaction-basic/src/index.ts:225 起（结构示意）
const generation = session.surface.replaceGeneration
try {
  await compactIfNeeded(agent, 'context-overflow', signal)
} catch (error) {
  if (session.surface.replaceGeneration > generation) {
    overflowRetries += 1
    return { kind: 'retry' }
  }
  throw error
}
```

判断依据不是「压缩有没有抛错」，而是「surface 有没有前进」。哪怕摘要在最后一步抛了错，只要替换已经落进 surface，这次恢复就算有效，可以重试。反过来，压缩一声不吭地返回了，但 generation 没动，重试只会得到同一个 400，于是把原始错误还给调用方。

### 9.2 崩溃留下的那条 start

进程在 start 与 end 之间被杀，日志里就留下一条无配对的 `compaction/start`。下次任何入口都会 `assertCompactionInactive` 拒绝，手动路径报 `busy`。

这是有意的：不知道上一次改到哪一步，就不要再改。但运维上需要一条出路。代码里给的出路是「生命周期」：如果这条 start 之前还有一条更晚的 `session/end-seed`，它就被当作旧生命周期的证据忽略。

`session/end-seed` 是 fork 与 resume 写入的继承标记。换句话说，崩溃恢复不是靠超时，而是靠「这个会话已经换过一条命」这个事实。

**如果自己搭 Agent**：三个东西缺一不可。计数器（防循环）、代次（判断是否真的前进）、上限（防烧钱）。只有计数器没有代次，你会把「压缩成功但摘要失败」误判成失败；只有代次没有上限，你会在一个永远压不动的会话上无限重试。

## 10. 摘要请求的预算

摘要是一次真实的模型调用，花真实的钱。它有几个容易忽略的约束：

- 输出上限默认等于 `headroomTokens`（默认 65536）。这不是目标长度，是上限。八段写完通常远小于它。
- 如果 finish reason 是 `max-tokens`，抛 `MAX_TOKENS`，文案是「summarization truncated at the token cap (incomplete checkpoint)」。截断的摘要不能用，因为它可能少了「下一步」。
- 摘要输出里出现图片会被拒绝，只保留 text。
- 空摘要直接抛错：「summarization produced no text summary content」。
- 取消优先。abort 之后即使模型返回了内容，也不提交。

摘要用哪个模型？优先级是：配置里显式的 `summarizationProvider`/`summarizationModel` > 当前请求的 header > agent 的默认。模型切换场景下有一个例外：除非显式配置了摘要目标，否则摘要仍用**先前**的路由。因为新模型的 header 还没提交，用它摘要会让这次调用落在一个尚未生效的配置上。

还有一条预算约束在阈值里：`thresholdTokens` 取「窗口 × 0.8」与「窗口 − 预留输出 − headroom」的较小值。headroom 不是「留白好看」，它是给摘要请求本身、给估算误差、给一次工具结果突增留的余量。压到 95% 再动手，摘要请求自己就可能放不下。

## 11. 设计取舍

**选连续区间，不选语义分块。** 代价是可能压掉一段其实还有用的对话；收益是 provider 的前缀缓存、成对性判断、`surfaceOp` 都只需要处理连续段。语义分块要把 surface 打散成若干不相邻的片段，缓存全丢，替换语义也会变成多段 replace。什么时候该反悔：当工具结果占比很低、而对话本身高度冗余时，连续区间会浪费压缩机会。

**不删日志，只改投影。** 代价是会话文件持续增长，一个长会话可能几十 MB；收益是任何一次压缩都能回查原文，`traceEvent` 能画出替换链。什么时候该反悔：如果部署环境对磁盘极度敏感，需要另做归档，而不是改压缩语义。

**默认只重试一次。** 代价是可能压不到阈值以下，turn 仍然失败；收益是不会在一个压不动的会话上烧钱。什么时候该反悔：如果窗口很大而压缩比很低（比如只超了 2%），把 `compactionRetries` 调到 2 是合理的。

**摘要用固定八段。** 代价是摘要偏长，有时为了填满「Files and Code」会抄不少路径；收益是稳定，模型不会这次写诗、下次写表。什么时候该反悔：如果摘要长期逼近 `shadowedRouteTokenCount`，说明八段对这个场景太重，应该换更短的模板，而不是放宽「必须变小」的检查。

**手动压缩要求 idle。** 代价是长 turn 进行中无法手动压缩；收益是没有并发写者。什么时候该反悔：如果用户经常在长任务中间想腾空间，应该提供「取消当前 turn 再压缩」的命令，而不是放宽 idle 检查。

## 12. 踩坑点

**切在 tool_call 与 result 之间。** 现象：下一次请求 400，错误提到 `tool_use ids without tool_result`。原因：按消息条数或按字符位置切，没有验证成对性。对策：只用 `toolPairingBalancedBefore/After` 判定切点，不要自己数。

**按「保留最近 10 条」实现。** 现象：有时保留 3k token，有时保留 45k，阈值忽高忽低。原因：消息大小差异巨大。对策：改成 token 预算，并为估算误差留 headroom。

**摘完仍然超阈值，然后循环重试。** 现象：一个 turn 里连续多次摘要调用，账单异常。原因：没有在每次摘要后重新 measure，或者重试次数没有上限。对策：每轮重新 measure，`compactionRetries` 保持 1。

**摘要里写「我之前压缩过对话」。** 现象：模型开始元讨论，或者向用户解释「由于上下文限制」。原因：摘要指令没有禁止，且缺少 preamble。对策：指令里明确禁止，preamble 里把摘要定位成既有背景。

**崩溃后所有压缩都报 busy。** 现象：`/compact` 永远返回 busy，日志里有一条孤立的 `compaction/start`。原因：锁是 durable 的，没人收尾。对策：识别生命周期边界（`session/end-seed`），或者提供一个人工清除命令；不要用超时自动清锁。

**把 surface 位置当 seq 存下来。** 现象：第二次压缩时区间错位，压掉了不该压的消息。原因：替换后位置与 seq 不再单调对应。对策：位置跨度与 seq 集合分开存，`shadowedRange` 与 `shadowedSeqs` 各司其职。

**图片 offload 只数未省略的图。** 现象：第二次 offload 选错图片，`imageIndexes` 与模型看到的对不上。原因：索引基于「跳过的图片数」，而已省略的图片仍然占位。对策：深度优先数所有图片，包括已经 offload 的，索引才稳定。

## 13. 源码索引

| 位置 | 证明什么 |
| --- | --- |
| `packages/compaction/compaction-basic/src/index.ts:147` | 四个入口的注册位置与失败策略 |
| `packages/compaction/compaction-basic/src/index.ts:213` | 溢出恢复只看 `CONTEXT_WINDOW_EXCEEDED_CODE`，并在压缩前记录 generation |
| `packages/compaction/compaction-basic/src/index.ts:316` | 压力路径：先 measure，再 prune，再重新 measure，最后才摘要 |
| `packages/compaction/compaction-basic/src/index.ts:407` | 手动路径走 `runMaintenance`，把拒绝包成 `busy` |
| `packages/compaction/compaction-basic/src/region.ts:117` | 区域选择的三条规则与 null 返回 |
| `packages/compaction/compaction-basic/src/region.ts:173` | 事务顺序：校验、加锁、准备、摘要、提交、收尾 |
| `packages/compaction/compaction-basic/src/region.ts:337` | 区间两端的成对性校验与报错文案 |
| `packages/compaction/compaction-basic/src/region.ts:471` | `compaction/summary` 与替换消息的写入顺序 |
| `packages/compaction/compaction/src/tool-pairing.ts:29` | 用计数折叠判断成对，不用 step 标记 |
| `packages/compaction/compaction-tool-result-pruner/src/index.ts:83` | 头尾保留、中段替换、按 code point 切 |
| `packages/compaction/compaction-image-offload/src/image-offload.ts:16` | 深度优先数图，已省略的也计数 |
| `packages/core/session/src/surface.ts:505` | replace 的四步校验与 `replaceGeneration` 自增 |
| `packages/compaction/compaction-basic/src/summarizer.ts:32` | 八段摘要模板与「不得提压缩」 |
| `packages/compaction/compaction-basic/src/config.ts:153` | 阈值、保留量、headroom 的关系与校验 |

回到开头那三个场景。

第一个场景的 400，不是模型的问题，是切点的问题；正确性标准是「切完的 surface 仍然是一条合法请求」。第二个场景的失忆，不是摘要长度的问题，是内容契约的问题；八段里「Files and Code」和「Critical Context」就是为这种时刻存在的。第三个场景的 busy，不是 bug，是设计：它宁可拒绝，也不在一个不知道改到哪一步的会话上继续改。

压缩做到最后，你会发现它更像数据库的事务，而不是文本处理。它要保证的是一组不变量：请求合法、最近事实还在、失败可解释、重放能复现。摘要写得好不好，是这四条都成立之后才轮到考虑的事。

---

上一篇：[第 18 篇 · 上下文注入](/posts/dsh-context-injection.html) ｜ 下一篇：[回忆与重放：session-query、session-reference 与 fork](/posts/dsh-recall.html) ｜ [系列目录](/posts/dsh-architecture-anatomy.html)
