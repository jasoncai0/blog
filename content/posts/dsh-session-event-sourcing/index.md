---
title: "模型看见的，必须被记下：会话日志与事件溯源"
date: "2026-11-11T09:00:00+08:00"
summary: "DSH 不维护 messages 数组。它只写一条只追加的事件日志，每次请求现场投影出模型历史。这套选择决定了恢复、压缩、fork 与审计能不能同时成立。"
description: "拆解 SessionEvent、surface 投影、deriveMessages 与 JSONL 持久化：日志为什么是唯一真源，以及围绕它的五个坑。"
tags: ["event-sourcing", "harness", "architecture"]
slug: "dsh-session-event-sourcing"
---

> 依据：`packages/core/session/src/{types.ts,index.ts,surface.ts}`、`packages/session/session-persistence-jsonl/`、`docs/subsystems/session.zh.md`（1431 行）。源码版本 `0.2.2-rc.1`。

先看一个几乎所有 Agent 教程都会写的结构：

```ts
const messages: Message[] = []
messages.push({ role: 'user', content: prompt })
while (true) {
  const reply = await llm(messages)
  messages.push(reply)
  if (!reply.toolCalls.length) break
  for (const call of reply.toolCalls) messages.push(await run(call))
}
```

问题出在那个变量上：唯一的真相被放进了随时会变的内存里。进程一崩，所有上下文没了；要压缩，只能就地改数组，改完再也说不清模型当时到底看到了什么；要 fork，只能深拷贝一个瞬间，拷完两边就分叉，无法回答「这个分叉是从哪条消息开始的」；要审计，得另写一套日志，然后祈祷它和 `messages` 一致。

DSH 把这个变量删了：只写日志，历史每次现算。

## 1. 日志的形状：14 种事件

`SessionEventMap` 是会话的全部词汇，恰好 14 个成员：

| 类别 | 事件 | 进入模型历史 |
| --- | --- | --- |
| 边界 | `turn/start`、`turn/end`、`step/start`、`step/end` | 否 |
| 输入 | `user/message`、`developer/message`、`system/message` | 是 |
| 输出 | `assistant/message`、`assistant/attempt` | 前者是，后者否 |
| 工具 | `tool/call`、`tool/result` | 后者是，前者否 |
| 请求信封 | `request/header`、`request/context` | 否（但决定请求） |
| 生命周期 | `session/end-seed` | 否 |

一条日志条目长这样：

```ts
type SessionEvent<T> = {
  type: T
  seq: SessionSeq          // = log.length，连续且单调
  time: number             // epoch ms
  data: SessionEventMap[T]
  ignorable?: true         // 未知类型能否安全跳过
} & (T extends SurfaceEventType
  ? { surfaceOp: SurfaceOp; sourceEventSeqs?: SessionSeq[] }
  : { surfaceOp?: never; sourceEventSeqs?: never })
```

有三个设计点值得停下来看。

`seq` 直接取数组下标。于是「事件序号」和「日志位置」是同一个东西，fork 的切点、替换范围、游标全都可以用同一个整数表达，不需要额外的映射表。代价是日志不能有洞，追加失败必须整体回滚，不能留下半个事件。

判别式联合建在 `type` 上，没有 `{type, data}` 两个独立字段。`switch (event.type)` 就能窄化 `event.data`，不用类型断言。这让新增一种事件的编译期成本很低：在 `SessionEventMap` 里合并一个成员，所有 `switch` 的 `default` 分支会自动接住它。

`ignorable` 默认缺席，缺席即「必需」：读取器遇到不认识的类型且没有这个标记，必须拒绝重建整个会话，不能跳过。理由写在注释里，一个不认识的必需事件可能改变后面所有事件的解释方式，静默丢弃等于恢复出一个被掏空的会话。宁可拒绝，也不要假装成功。

还有一条运行时约束：`append()` 会用 `snapshotJsonValue()` 对 `data` 和 surface 元数据各做一次快照，再做 `deepFreeze`，最后 `validateSessionEventData()`。任何不可 JSON 无损表示的值（`undefined` 字段、`Map`、类实例、`NaN`）在写入点就被拒绝，不会等到落盘时才炸。

{{< fig "diagrams/01-log-to-surface.svg" "日志是线性的，surface 是折叠出来的。每个 message 事件声明自己如何进入 surface（append 或 replace），折叠器按顺序推进节点列表；模型历史再从节点列表逐条投影，空内容节点投影为 null。" >}}

## 2. Surface：日志只增，消息可被遮蔽

日志只能追加，压缩却要把 100 条消息变成 1 条摘要，靠的是 surface。日志里什么都不删，改的是「哪些事件还在 surface 上」。

```ts
type SurfaceOp =
  | 'append'
  | { op: 'replace'; startSeq: SessionSeq; endSeq: SessionSeq }
```

- `append`：挂到尾部，用户/助手/工具消息的常规路径。
- `replace`：遮蔽 `[startSeq, endSeq]` 这段当前 surface 节点，把新事件插在原位。端点必须已经存在于当前 surface，必须早于替换事件；`startSeq === endSeq` 就是替换单条。

`sourceEventSeqs` 是可选的「引用证据」：替换节点必须列出它遮蔽的每一个节点，这样「这条摘要概括了哪几条消息」是可查的，不用靠猜。

`SessionSurface` 只暴露三个只读字段：

```ts
interface SessionSurface {
  readonly nodes: readonly SessionSeq[]
  readonly replaceGeneration: number   // 位置替换的次数
  readonly contentGeneration: number   // 位置替换 + 插件内容改写的次数
}
```

两个 generation 分开是有原因的。`replaceGeneration` 只关心「节点列表的形状变了」；`contentGeneration` 还包含「节点没变，但某条消息的内容被插件改写了」（比如图片被 offload 成占位符）。增量消费者用前者判断要不要重排，用后者判断要不要重算内容。

system prompt 是 surface 的第 0 号节点。这条规则比看起来重要：系统提示词属于历史，不在请求头里。压缩可以遮蔽后续系统节点，但不能替换第 0 号节点，折叠器会拒绝任何覆盖节点 0 的 `replace`。否则一次压缩就可能把「你是谁」这段指令吃掉，而模型完全不会报错。

空内容节点也有一套语义：空的 system/developer 节点保留 surface 位置但不产生 wire 消息（「休眠」）；空 assistant 节点同样不产生消息，但它承载 max-tokens 那一步的 `usage`。所以日志里出现一条空 assistant message 时，它是计费的落点，不是 bug（第 6 节）。

## 3. 派生：哪些事件真的变成消息

`deriveEventMessage()` 是唯一的历史投影函数，逻辑短得可以背下来：

```ts
switch (event.type) {
  case 'user/message': return event.data           // 保留 producer 的 framing
  case 'system/message':
  case 'developer/message':
  case 'assistant/message':
    if (event.data.message.content.length === 0) return null
    return event.data.message
  case 'tool/result': return event.data.message
  default: return null                              // 边界、attempt、日志记录
}
```

`default` 是故意非穷尽的：`SessionEventMap` 可以被插件合并扩展，`switch` 里不能用 `assertNever`，否则每加一个插件事件就要改一次核心。仓库的一般规则是 closed union 用 assertNever，这里反过来，是扩展性对封闭性的例外。

空内容过滤同样有原因：

```ts
// A surface node is one of the five message-producing types, but an
// empty-content assistant/message (a max-tokens step that hosts only
// usage) derives to null and must not enter the transcript.
if (msg) this.derived.push(msg)
```

一个只为了记 `usage` 而存在的空 assistant 事件，如果进了 wire messages，提供方会看到一条 `role: assistant, content: []`，轻则浪费 token，重则被 API 拒绝。

`developer/message` 是另一个不常见但有用的角色：工具集变化时，loop 会写一条 `content: [{type:'tool-addition'|'tool-removal', toolName}]` 的 developer 消息，告诉模型「你的工具变了」。它进历史，但不占 user/assistant 的角色位。「工具变更」这类控制面事件，只能通过它变成模型可见的事实（第 7 篇）。

## 4. 失败也留痕：`assistant/attempt`

只有成功的调用写 `assistant/message`。失败的那次写 `assistant/attempt`，专门对应「一次没有产生任何 surface 消息的模型尝试」：

```ts
'assistant/attempt': { turn: number; step: number; stream: AssistantStreamRecord[] }
```

它内嵌完整的紧凑原始流（`AssistantStreamRecord[]`，带每帧时间），但不产生模型历史。这样三件事同时成立：

- 失败可诊断：能看到模型在挂掉前到底吐了什么；
- 历史不被污染：重试不会让模型看见「上一次失败的自己」；
- 计费可对账：失败的 attempt 也有 token 记录。

与之对称的是 `interrupted: true` 的 `assistant/message`：turn 在流式途中被取消，已经交付的文本/推理前缀会被固化成一条正常的 assistant 消息，但打上中断标记；尚未派发的 tool call 被丢弃。丢弃的原因是，一个没有结果的 tool call 会让下一轮请求的消息序列不合法（提供方要求 tool_call 与 tool_result 成对）。

这里有一条容易忽略的顺序：`assistant/message` 的 `stream` 是内嵌的，且禁止带 `sourceEventSeqs`。别的 surface 事件可以「引用」更早的事件作为证据，assistant 消息不行，它自己就带着精确的 provider 证据，再加引用会制造两条互相矛盾的证据链。

## 5. 一次 append 到底发生了什么

{{< fig "diagrams/02-append-pipeline.svg" "append 的提交管线：入口检查（存储附着、非重入、公告完成）→ JSON 快照 → 冻结 → 数据校验 → surface 计划校验 → 提交 → 投影推进 → 检查点。任何一步失败都不产生半条事件。" >}}

按 `packages/core/session/src/index.ts:806` 的顺序：

1. `publicationEntry()`：会话必须附着在 `SessionStore` 上；`session/created` 尚未完成时拒绝；检测重入（在 append 里再 append）。
2. 对 `data` 与 surface 元数据分别 `snapshotJsonValue()`，失败即抛「carries non-JSON-serializable data」。
3. `deepFreeze` 整个条目，`seq = log.length`，`time = Date.now()`。
4. `validateSessionEventData()`：校验 header 规范性、developer 角色、tool 失败元数据、surface 端点存在性等。
5. `commit()`：推进 surface 投影（`replaceGeneration`/`contentGeneration`），排队持久化。
6. 返回事件本身，调用方拿到 `seq` 用于后续引用（比如 `tool/result` 的 `sourceEventSeqs`）。

第 1 步的两个拒绝条件都是真实踩过的坑：`session/created` 监听器里 append 会抛「cannot append before session/created completes」；在 append 触发的投影回调里再 append 会抛重入。这两条把「事件发布」和「事件处理」的边界钉死了。

第 5 步之后不保证已经落盘。`turn/end` 的注释写得很直白：

> The loop does not await a flush at turn boundaries: `dsh-session-checkpoint-policy` owns the per-request durability checkpoint, and consumers that read storage after `whenIdle()` flush themselves.

所以跑完一轮就去读 JSONL 文件，可能少一条。检查点策略是独立插件，按请求落盘，不按 turn：每次工具调用都等 IO 的代价被避开了，最多丢一轮。

## 6. 持久化：代际、迁移、崩溃

JSONL 后端把日志写成 `session.jsonl`（v0）或 `session.vN.jsonl`（v1+），可 zstd 压缩。三条规则决定了它的安全性。

已提交的代际不可变。写打开时先编码并校验，再排他发布一个新的 generation 文件；绝不重命名、替换或删除已提交的代际。读打开只产生内存结果，不发布后继。

迁移只做相邻一步。每个 migration 包只负责 vN → vN+1，加载时链式跑完。这样每个迁移可以单独测试，也能在任意中间版本停住。

崩溃会留下孤儿 turn。如果日志最后一条是 `turn/start` 而没有配对的 `turn/end`，恢复时不能假装它完成了。两种处理：agent-loop 恢复时补写 `{kind:'interrupted'}`（并保留崩溃前所有事件），或者 `session-query` 在冷读时合成同一个标记。`interrupted` 永远不会由 loop 实时发出，只可能来自事后补写。这条约束让「异常结束」和「正常结束」在日志里可区分。

{{< fig "diagrams/03-three-reads.svg" "同一份日志的三种读法：resume 从上次中断处继续（补 interrupted）、fork 在指定事件序号处切一刀（补 forked）、query 只读不写（冷读时合成结束标记）。三者共享同一套 fold。" >}}

fork 的切点用 `inheritedEventCount`，一个精确的事件数。消息条数不行，原因在上一节：消息是投影出来的，同一个消息数可能对应不同的事件前缀；只有事件序号能唯一确定切在哪。子会话在切点写 `session/end-seed {inherited:true}`，再为自己的语法补上 `forked` 结束标记。源会话的事件一条不动。

## 7. 可重建性：一个请求是日志的纯函数

把前面几节拼起来，得到 DSH 最重要的一条不变量：

> 给定一份日志，`deriveMessages()` 的输出是确定的；给定日志 + `request/header` + `request/context`，下一次请求也是确定的。

这条不变量把「模型到底看见了什么」变成了一个可复现的问题。线上出了一个诡异的回答，你可以拿日志重放那一次请求；用户说「它明明答应过我」，你可以指出是哪条事件、在第几个 seq。

维护这条不变量的成本落在每个插件身上：

- 任何模型可见的新输入，要么 `append` 一条 surface 事件，要么注册一个纯 `SessionMessageProjection` 去改写已有消息；
- 任何只影响控制面的状态，写 log-only 事件（不能带 `surfaceOp`），并且要考虑 `ignorable`；
- 任何派生缓存（UI 列表、token 计数、索引），必须能从日志重建，且不能反过来成为写入源。

message projection 是第二条通道，也是最容易被滥用的：

```ts
interface SessionMessageProjection<T> {
  type: T
  project(event, context): ReadonlyMap<SessionSeq, Message>
}
```

它必须是纯函数：给定同一段历史，输出同一组消息；必须保留消息 identity，返回不可变副本；必须在完整决策到达后才返回更新。图片 offload 插件就是这么做的：把大图替换成占位符，同时在日志里留一条自己的事件解释「这里原本是什么」。卸载这个插件后，读取会直接拒绝，因为投影器缺失时那段历史无法被正确解释。

{{< fig "diagrams/04-write-paths.svg" "三条合法写入通道与一条非法通道。左：append 一条 surface 事件；中：注册 message projection 改写内容；右：append 一条 log-only 事件。下方红色：直接改内存里的 messages 或把流式 chunk 塞进下一次请求。" >}}

## 8. 设计取舍

事件溯源和定期快照之间，DSH 选了前者：每次请求都从日志重算历史，读放大会随会话长度增长。两层缓存抵消了一部分：`deriveMessages()` 内部按 `contentGeneration` 增量追加（`derivedNodes` 游标），持久化层用检查点。换来的是可重建、可 fork、可审计。代价是当前状态永远是一个 fold 的结果，读不到一份「现在的样子」。

surface 只遮蔽，不物理删除，日志永远只增。压缩前的原文还在，可以回答「摘要丢掉了什么」。磁盘代价随之而来：一份长会话的日志只会变大，压缩只减少模型看到的 token，不减少存储。第一次看 DSH 的人常在这里误解。

14 种事件是强类型的，每个 `append` 都有编译期检查，surface 元数据的存在性由类型系统保证（surface 事件必填 `surfaceOp`，log-only 事件禁止带）。代价是加一个事件要改核心类型，虽然有 declaration merging，仍然要理解合并点。

`assistant/attempt` 让日志体积增加，每次重试都带一份完整流，换来的是线上可诊断性。只记成功的话，「为什么重试了三次」就永远查不到。

JSONL 可流式写、可 `tail -f`、可手工修、diff 友好；查询慢，所以另有一个 `session-query` 子系统建索引。SQLite 单文件、事务强，但并发写和跨版本迁移更麻烦。DSH 选 JSONL 做真源，把查询降级为可选投影。

## 9. 踩坑点

- 以为 `assistant/message` 为空是 bug。它是 max-tokens 那一步的 `usage` 载体，派生时被过滤掉，但审计时要读它。
- 在压缩里替换节点 0。折叠器会拒绝，但更常见的是「没意识到系统提示词也是 surface 节点」，于是压缩后指令悄悄失效。
- 给未知事件补 `ignorable: true` 图省事。这个标记只应给「丢了也不影响重建」的信息性记录；给错等于允许读取器静默掏空会话。
- 在 `session/created` 监听器里 append。会抛「cannot append before session/created completes」，因为此时会话还没完成公告。
- 在 append 的投影回调里再 append。重入检测会抛；正确做法是把要写的东西排到当前提交之后。
- 用消息条数做 fork 切点。必须用事件序号；消息是投影，条数不唯一。
- `whenIdle()` 之后直接读文件。检查点是按请求的，可能还差最后一次 flush；读之前自己 flush。
- 把 `tool/call` 当成模型可见历史。它不进 `deriveMessages()`，模型看到的是 `tool/result` 里的消息；两者靠 `callId` 配对。

## 10. 源码索引

- `packages/core/session/src/types.ts:206-238`：`TurnEndReasonMap` 七个变体与 `TurnEndReason`。
- `packages/core/session/src/types.ts:279`：`RequestHeaderReason` 四值。
- `packages/core/session/src/types.ts:287-380`：`SessionEventMap` 全文（含 `session/end-seed` 的 fork 语义）。
- `packages/core/session/src/surface.ts:120-152`：`deriveEventMessage` 的投影规则与空内容过滤。
- `packages/core/session/src/surface.ts:594`：`foldSurface`；`:612` `SurfaceManager`。
- `packages/core/session/src/index.ts:806-838`：`append` 的快照、冻结、校验、提交。
- `packages/core/session/src/index.ts:999-1020`：`deriveMessages` 的增量缓存与空消息跳过。
- `docs/subsystems/session.zh.md`：`SurfaceOp`、`SurfaceIntent`、projection、header 规范性的完整定义。
- `docs/subsystems/persistence.zh.md`：JSONL 代际、迁移与崩溃恢复。

日志在 DSH 里是唯一的事实，其余全是投影，顺手记一下的说法不成立。理解这一点，后面看起来奇怪的设计就好解释了：压缩为什么改 surface、fork 为什么按 seq 切、失败为什么也要写一条 attempt。

上一篇：[一个 Agent Harness 的解剖](/posts/dsh-architecture-anatomy.html) ｜ 下一篇：[输入平面：排队、投递与收件箱](/posts/dsh-input-inbox.html)
