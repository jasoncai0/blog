---
title: "一次请求的完整旅程：从按键到第一个 token，再回到屏幕"
date: "2026-11-13T09:30:00+08:00"
summary: "把浏览器、网关、会话控制器、agent loop 与模型适配器连成一条线：一次输入经过四次转换、两个持久化点，流式帧如何在不落库的情况下先到屏幕，取消与重连各自从哪一层开始。"
description: "DSH 端到端时序：输入、请求、流式帧、工具回程、取消与重连。"
tags: ["harness", "architecture", "concurrency"]
slug: "dsh-request-journey-end-to-end"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准。
> 第二季第 1 篇。上一篇：[《第二季：把主干两侧补齐》](/posts/dsh-season-two.html)；下一篇：[《模型层》](/posts/dsh-model-layer.html)。

你在输入框里敲一行字，回车。半秒后光标还在闪，两秒后第一个字开始往外蹦。这中间发生了什么，大多数 harness 文章会从 `while (true)` 开始讲。可真实系统里，`while (true)` 收到的第一条消息，是浏览器经过一次 RPC、一次入队、一次 claim 之后才递到它手上的；它吐出的第一个 token，也要经过一次帧转换、一次推送、一次投影，才会变成屏幕上的一个字符。

这篇按时间顺序走一遍，把四层之间的转换点标出来。读完你应该能回答四个问题：一条输入在哪一刻变成日志，一次请求在哪一刻变成网络流，一个 delta 为什么可以先上屏却不进日志，取消与重连分别从哪一层发起。

## 0. 结论

一次请求经过四层、四次转换：

| 层 | 输入 | 输出 | 关键契约 |
| --- | --- | --- | --- |
| 浏览器 | 按键、粘贴、斜杠命令 | RPC 参数 | 客户端只提交内容块，不提交消息 |
| 网关/会话控制 | RPC | `agent.followup()` / `steer()` | 回执是入队凭据，不是完成信号 |
| agent loop | 收件箱 | 一次组装好的请求 | 模型可见的必须已记录 |
| 模型适配器 | 请求 | 帧流 | 帧是临时的，结算才是事实 |

两个持久化点：入队时的 `agent/inbox/spliced` 与 `agent/inbox/inserted`，以及结算时的 `assistant/message`。其余全是内存态或瞬时帧。

## 1. 四层，四次转换

{{< fig "diagrams/01-four-layers.svg" "四层与四次转换。向下是输入被逐层规范化，向上是结果被逐层物化；两个圆点是持久化点。" >}}

四层的职责各用一个动词就能概括：

- 浏览器负责采集与渲染。它不知道消息在日志里的形状，也不该知道。
- 网关负责路由与多路复用。它把一条 WebSocket 拆成多条逻辑流，按会话地址转发。
- 会话控制器负责身份与窗口。它知道哪个会话开着、窗口从哪个 seq 开始、断线后从哪补。
- agent loop 负责记账与边界。它决定哪些输入进这一步、请求长什么样、结果怎么落库。

模型适配器不属于这四层里的任何一层，它是 loop 调用的下游。把适配器也算进来，是五层；但它的契约最简单：收一个 `GenerateOptions`，吐一串 `StreamChunk`，失败时抛 `LlmError`。

## 2. 第一段：按键到一条 user/message

浏览器提交的不是一条消息，是一组内容块：

```ts
export interface SessionQueryParams {
  /** SDK-owned Session identity; an unknown id creates its Agent. */
  sessionId: SessionId
  /** Ordered prompt blocks; a native command occupies its only text block and may accompany encoded images. */
  contentBlocks: SdkPromptContentBlock[]
}
```

这段类型来自 `packages/sdk/protocol/src/types.ts:99-104`。注意 `sessionId` 的注释：未知 id 会创建一个 Agent。客户端不需要先建会话再发消息，一次调用就够了。

服务端的返回只有两种形状（`types.ts:111-113`）：

```ts
export type SessionQueryResult =
  | ({ kind: 'prompt' } & SessionPromptResult)
  | { kind: 'command'; execution: CommandExecution }
```

`SessionPromptResult` 里只有一个 `messageId`，注释写得很清楚：这是入队回执，不代表会话工作结束。命令则不同，它返回 `CommandExecution`，后续还有生命周期事件。

### 2.1 从 RPC 到收件箱

会话控制器收到请求后，找到或创建对应的 Agent，把内容块交给它。loop 侧的入口是 `followup()`：

```ts
followup(input: UserMessage): void { this.send(input, 'next-turn', true) }
```

这是 `packages/core/agent-loop/src/agent.ts:179`。三个公开投递方法的实现都只有一行，差别在目标队列与是否唤醒：

```ts
steer(input: UserMessage): void {
  this.send(input, 'next-step', true)
}

inject(input: UserMessage): void {
  this.send(input, 'next-step', false)
}
```

`send()`（`agent.ts:170-177`）做三件事：如果是唤醒投递且当前活动正在 abort，把它改投 `next-turn`（唤醒输入不能加入一个已经中止的活动）；`inbox.splice(...)` 插入；`if (wakeup) wakeDriver(...)`。

入队不是静默的。结构化投递会先发 `agent/inbox/spliced`（描述这次变更），再发 `agent/inbox/inserted`（逐条新消息）。这两条事件是持久化的，所以「用户说过这句话」在模型看到它之前就已经是事实。

### 2.2 回执与完成是两件事

这一点在客户端最容易误判。`session/query` 的返回只是说「收到了，排在 next-turn 或 next-step 里」。实际的完成信号是 `session.status` 变成 `idle`，或者命令场景下的 `session.query.completed`。

Python SDK 的 `run()` 就是按这个约定写的（`python/sdk/src/deepseek_harness/api.py:260-272`）：拿到 prompt 回执后进入循环，直到收到该会话的 `session.status` 且状态为 `idle` 才退出。命令场景则等待带 `commandId` 的 `session.query.completed`。

{{< fig "diagrams/02-request-swimlane.svg" "从按键到 idle 的六泳道时序。虚线是返回路径，末段标出哪些事件落库、哪些只在内存里。" >}}

## 3. 第二段：日志到一次 provider 请求

这一段第一季的 07 篇拆过，这里只标出它与前后两段的接缝，避免把整篇重写一遍。

三个接缝值得记住：

组装发生在 claim 之后、pre-step 之前。 `preStep()` 的顺序是 `claim` → `model.current()` → `tools.resolve()` → `systemPrompt.assemble()` → `agent/pre-step` waterfall（`agent.ts:287-294`）。先 claim 再组装，意味着被 reject 的那一步也已经付过一次组装成本；这个代价换来的是「pre-step 能看到这一步真实的消息批次」。

请求是每次 attempt 重新构建的，组装不是。 `step()` 在循环外 `renderPrompt(assembly)`，循环内每次 attempt 只重跑 `refreshContext` 与 `renderContextSections`。重试不会重跑 `system-prompt/assemble` 的 waterfall，也就不会让工具 schema 在两次 attempt 之间漂移。

模型历史从日志派生，不从内存数组追加。 `buildRequest()` 调 `session.deriveMessages()`，再逐条 `deepFreeze`。这条规则是整个系统的地基：只要日志能重建 surface，请求就能重建；只要请求能重建，恢复与审计就成立。

## 4. 第三段：帧到像素

这一段的形状最容易被想当然。模型返回的不是一整段文本，是一串增量帧。DSH 的 `StreamChunk` 有六种（`packages/llm/llm/src/types.ts:496-508`）：

```ts
export type StreamChunk =
  | { type: 'block-start'; index: number; blockType: ContentBlockType }
  | { type: 'text-delta'; index: number; text: string }
  | { type: 'reasoning-delta'; index: number; text: string }
  | { type: 'tool-call-delta'; index: number; id: ToolCallId; name?: string; argumentsDelta: string }
  | { type: 'block-end'; index: number; block: ContentBlock }
  | { type: 'usage'; usage: TokenUsage }
  | { type: 'finish'; reason: FinishReason; replayState?: ReplayEnvelope }
```

loop 收到帧后做两件事：转发一份瞬时的，攒一份持久的。

瞬时的那份走 `agent/assistant-stream`，帧类型是 start/chunk/end，带 `attemptId` 与 `revision`。持久的那份在提供方调用结束后才写，即 `assistant/message`，里面内嵌一份紧凑的 stream 与 usage。

### 4.1 为什么帧不落库

三个原因，按重要性排：

1. 帧太密。一次回答可能几百个 delta，逐条写 JSONL 会让日志膨胀一个数量级，而这些中间态对恢复没有价值。
2. 帧不可重放。网络抖动导致的重复帧、乱序帧、被取消的半个块，都不该成为事实。
3. 结算才有语义。只有 `finish` 到达后，才知道这次调用是 committed、aborted 还是 error；把结算前的帧写进去，恢复时无法判断它们属于哪个结局。

所以帧只用于显示，结算用于记账。第一季 02 篇讲的「空 assistant 节点承载 usage」也是同一条规则的延伸：usage 到了，但内容为空，节点仍要留位置。

### 4.2 客户端拿到的是变更，不是快照

GUI 的会话控制器不轮询、也不自己拼消息。它订阅一条按会话地址路由的日志变更流，类型只有四种（`packages/api/session-controller/src/client/sessions/session.ts:648-666`）：

```ts
switch (change.type) {
  case 'replace': ...
  case 'prepend': ...
  case 'append': ...
  case 'assistant-stream': ...
}
```

`append` 是持久事件追加，`prepend` 是往前翻历史，`replace` 是窗口重建，`assistant-stream` 是瞬时帧。客户端把前三类交给事件源，把最后一类交给一个专门的 assistant stream 状态机（`session.ts:706-740`）：

```ts
if (result?.type === 'settlement') { this.eventSource.settleAssistant(result.attemptId, result.entry) ... }
if (result?.type === 'abandonment') { this.eventSource.settleAssistant(result.attemptId) ... }
if (result?.type === 'publish') { this.appendLive(result.entry) ... }
else if (result?.type === 'transient') { this.eventSource.append(result.entry) ... }
```

这段分支是整条链上最值得读的二十行：它把「流式显示」与「持久事实」分开处理。`transient` 只进事件源，用于渲染；`settlement`/`publish` 才换成持久节点。用户看到的文字在结算前就已经在屏幕上了，但它还不是日志的一部分。

{{< fig "diagrams/03-frame-to-pixel.svg" "帧到像素的四段管线。中间标出哪一步是瞬时、哪一步转成持久，以及重连时哪些内容会被重建。" >}}

## 5. 第四段：工具调用的回程

模型返回 `tool-call` 之后，回程比文本长得多，因为它跨了「模型 → 日志 → UI → 执行 → 日志 → 模型」两次往返。

{{< fig "diagrams/04-tool-roundtrip.svg" "一次工具调用的往返。执行前先落 tool/call，是崩溃后能补齐结果的前提；结果按模型给出的顺序回填。" >}}

顺序上只有两条硬约束：

- `tool/call` 必须在执行之前写。否则进程在审批中途崩掉，日志里既没有调用也没有结果，恢复逻辑连「这里曾经有个调用」都不知道。
- `tool/result` 必须按模型给出的调用顺序写。并行执行的结果可能乱序完成，但落库前要按原顺序提交，否则模型看到的历史与它给出的调用对不上。

UI 的卡片状态跟着这两个持久点走：`presentCall(args)` 在 `tool/call` 之后，`presentResult(args, result)` 在 `tool/result` 之后。执行中的进度（比如 bash 的增量输出）走 transient 通道，和 assistant 的 delta 同类。

### 5.1 一次 bash 调用在日志里的样子

把上面几节拼起来，一次带工具调用的 step 在日志里长这样（省略瞬态帧）：

| 序号 | 事件 | 谁写 | 说明 |
| --- | --- | --- | --- |
| 1 | `turn/start` | loop | 轮次开始，之后所有事件都属于这个 turn |
| 2 | `step/start` | loop | 第 0 步，带上本步的 toolSet 快照 |
| 3 | `system/message` | loop | 组装结果，只写有变化的节点 |
| 4 | `user/message` | loop | 本轮输入，首轮才提交 |
| 5 | `request/header` | loop | 首次请求，或路由、工具集变化 |
| 6 | `assistant/message` | loop | 含 `tool-call` 块，模型历史从此多一条 |
| 7 | `tool/call` | loop | 执行之前，参数已冻结 |
| 8 | `tool/result` | loop | 规范化之后的唯一可见结果 |
| 9 | `user/message` | loop | 只有工具带了 `additionalContexts` 才有 |
| 10 | `step/end` | loop | 本步结算，toolSet 释放 |
| 11 | `assistant/message` | loop | 同一 step 的第二次请求，不再写 `step/start` |
| 12 | `turn/end` | loop | `completed`，或带失败的 `error` |

第 11 行是最容易被误解的一步：一次 step 可以发多次请求，每次都有新的 `assistant/message` 与 `request/header`，但没有新的 `step/start`。工具结果不是返回值，它是第 8 行的事件；模型在 11 行看到的是「历史里多了一条 tool 消息」，而不是「函数返回了」。



## 6. 两条难走的路径：取消与重连

正常路径读一遍就够了，异常路径才是这套设计最花力气的地方。

### 6.1 取消从哪一层开始

取消有三个入口：用户点停止、父 agent 撤销子 agent、插件在 hook 里拒绝。它们最终都收敛到 `agent.cancel(cause, { keepInbox })`（`agent.ts:191-197`）：

```ts
cancel(cause: AgentCancelCause, options: CancelOptions = {}): void {
  if (!options.keepInbox) {
    this.inbox.clear()
    if (this.phase.kind !== 'idle') this.phase.wakeRequested = false
  }
  if (this.phase.kind !== 'idle') this.phase.abort.abort(cause)
}
```

三件事按这个顺序发生：默认清空收件箱并撤销待兑现的唤醒，然后 abort 当前的 `AbortController`，把 `cause` 作为 `signal.reason` 交给下游。`keepInbox: true` 时只 abort，排队与 steering 保留。下游谁在听这个 signal，谁就要自己收尾：provider 流会中断，工具批次会 drain 已启动的调用、对未启动的调用写 `TOOL_ABORTED_BEFORE_DISPATCH`，loop 会写 `assistant/attempt` 或带 `interrupted` 的 `assistant/message`。

这里有一个容易漏的点：`abort()` 是同步的，但收尾是异步的。所以在 `cancel()` 返回之后立刻 `whenIdle()`，拿到的是「收尾完成」而不是「已停止」。

### 6.2 重连补的是窗口，不是消息

客户端断线重连时不会重放全部历史。它保留一个从 `baseSeq` 开始的连续窗口，重连后按需往前 `prepend`，往后 `append`。窗口被地址替换（比如 fork 出了一个子会话）时，走 `resync()`（`session.ts:499-511`）：

```ts
async resync(): Promise<void> {
  if (this.openState === 'cold') return
  this.openGeneration++
  const events = this.events
  this.events = undefined
  await events?.dispose()
  this.openPromise = null
  this.openState = 'cold'
  this.openError = null
  this.baseSeq = SessionLogOffset(0)
  await this.open()
}
```

注意它只重建窗口，不动投影状态。注释写得很明确：`projection state belongs to the independently reconnecting control stream and remains untouched`。投影（标题、inbox、plan、usage 这些折叠值）有独立的重连通道，窗口（事件）有窗口的通道。两条流分开，才不会出现「事件补回来了但投影还是旧的」这种半状态。

{{< fig "diagrams/05-cancel-resync.svg" "取消与重连两条异常路径。上半是 abort 的传播方向，下半是窗口与投影各自的重建边界。" >}}

### 6.3 三种取消的差别

| 场景 | cause | keepInbox | 结果 |
| --- | --- | --- | --- |
| 用户点停止 | `user` | 默认清空 | 当前 turn 以 `aborted` 结束，排队输入丢弃 |
| 父 agent 撤销 | `parent` | 默认清空 | 子会话同样收尾，父会话不受影响 |
| hook 拒绝 | `hook{reason}` | 由调用方决定 | `reason` 进 `turn/end`，便于审计 |
| 进程退出 | `disposed` | 清空且不再唤醒 | 不会再开新轮，恢复时补 `interrupted` |

`keepInbox: true` 的用途是「停下来改个方向，但别丢我已经排好的消息」。它不写 `discarded` 事件，因为消息没有被放弃，只是留在队列里等下一次唤醒。

### 6.4 一次中途取消的日志轨迹

用户在工具执行到一半时点停止，日志会留下这样一串（省略瞬态帧）：

| 顺序 | 事件 | 内容 |
| --- | --- | --- |
| 1 | `turn/start` | 轮次打开 |
| 2 | `step/start` | 第 0 步 |
| 3 | `assistant/message` | 含两个 `tool-call` |
| 4 | `tool/call` | 第 1 个调用，已开始 |
| 5 | `tool/call` | 第 2 个调用，尚未开始 |
| 6 | `tool/result` | 第 1 个：已启动，走 drain，记工具真实返回的内容 |
| 7 | `tool/result` | 第 2 个：`TOOL_ABORTED_BEFORE_DISPATCH`，未启动 |
| 8 | `assistant/attempt` | 没有可见内容，只记失败结算 |
| 9 | `step/end` | 本步关闭 |
| 10 | `turn/end` | `aborted`，`reason` 是同一个 cause 对象 |

第 6 行是这套设计的边界：已经启动的调用无法撤回，只能等它自己收尾；日志记的是它真实返回的结果，而不是「被取消」这个意图。第 8 行保证「取消」也有一条结算记录，不会让某个 attempt 在日志里凭空消失。第 10 行的 `reason` 与第 6 行的结果共同回答一个问题：这次中断是用户按的，还是父会话撤的。


## 7. 延迟预算

把一条请求拆成可测量的段，才能判断优化该往哪放。

| 段 | 典型成本 | 能否削减 |
| --- | --- | --- |
| 浏览器采集到 RPC | 亚毫秒到几毫秒 | 前端批处理，收益有限 |
| RPC 到入队 | 本地一次函数调用 | 不能，必须落 `agent/inbox/*` |
| claim 到组装 | 本地，取决于 section 数量与工具数 | 可以缓存 section，但组装必须每步一次 |
| 请求构建到派发 | 本地序列化，与历史长度成正比 | 前缀稳定时可以复用 cache |
| 首 token | 取决于 provider 的 TTFT 与 prompt 长度 | 缩短 prompt、提高缓存命中 |
| 帧到屏幕 | 每帧一次状态更新 | 合并渲染，避免每 delta 一次重排 |
| 工具回程 | 工具自身耗时 | 与 harness 无关，能做的是并行与超时 |

只能等的只有两段：首 token 与工具执行。其余都是本地计算，优化空间在「少做一次」而不是「做快一点」。这也解释了为什么压缩与缓存比微优化更重要。

## 8. 设计取舍

| 取舍 | 选了什么 | 放弃什么 | 什么时候该反悔 |
| --- | --- | --- | --- |
| 回执与完成分离 | 回执只证明入队 | 客户端要自己等 idle | 如果产品需要「发送即可靠」的强语义，要在回执里加持久化确认 |
| 帧不落库 | 日志只留结算 | 无法回放逐字生成过程 | 做训练数据采集时需要另开一条采样通道 |
| 事件推全量 | 客户端拿完整 `SessionEvent` | 带宽与解析成本 | 移动端弱网可改为服务端投影差分 |
| 窗口与投影分流 | 两条流各自重连 | 客户端要维护两套游标 | 单流实现更简单，但一致性更难保证 |
| 取消用 `AbortSignal` | 标准、可组合 | 取消原因只能靠 `reason` 传递 | 需要结构化取消原因时，要另建映射表 |

## 9. 踩坑点

1. 把 `session/query` 的回执当成完成。 它只返回 `messageId`，会话可能还要跑十步。
2. 把 `assistant-stream` 当持久事件处理。 它没有 seq，重连后不会补发。
3. 在客户端拼消息。 一旦客户端自己把 delta 拼成消息，它就和日志里的 `assistant/message` 成了两份事实。
4. 用 `session.event` 驱动 UI 状态机。 事件是事实，不是视图状态；投影才是视图该读的东西。
5. 取消后立刻认为已停止。 `cancel()` 同步 abort，收尾异步完成。
6. 重连时重放全部历史。 窗口机制的存在就是为了避免这件事，代价是客户端要处理 `prepend`。
7. 忽略 `session.status` 的 running/idle 转换。 它是判断「还在跑」的唯一公开信号，不要用有没有新事件来猜。
8. 在 `append` 与 `assistant-stream` 之间假设顺序。 两者来自不同通道，顺序由客户端状态机保证，不由网络保证。

## 10. 源码索引

- 协议：`packages/sdk/protocol/src/types.ts:99`（`SessionQueryParams`）、`:111`（`SessionQueryResult`）、`:193-207`（通知表）、`:210-219`（请求表）
- 网关：`packages/api/gateway/src/stream-server.ts:1`、`stream-protocol.ts:6`
- 客户端窗口：`packages/api/session-controller/src/client/sessions/session.ts:96`（`baseSeq`）、`:499`（`resync`）、`:648`（`acceptEventChange`）、`:706`（`publishAssistantEntry`）、`:746`（`prependWindow`）
- 帧类型：`packages/llm/llm/src/types.ts:496-508`
- 投递：`packages/core/agent-loop/src/agent.ts:162-197`
- 组装顺序：`packages/core/agent-loop/src/agent.ts:287-294`
- SDK 等待语义：`python/sdk/src/deepseek_harness/api.py:214-272`
- 相关篇目：[02 事件日志](/posts/dsh-session-event-sourcing.html)、[06 状态机](/posts/dsh-turn-step-state-machine.html)、[07 请求组装](/posts/dsh-request-assembly.html)、[12 工具结果](/posts/dsh-tool-results.html)

## 最后

这条链上没有一个「总控」。每一层只做一种转换，每一种转换只依赖上一层的契约。四层分开的代价是调试要跨进程看日志，收益是任何一层都可以被替换：换客户端、换传输、换模型，另外三层不需要知道。

系列导航：上一篇 [S2-0 第二季导览](/posts/dsh-season-two.html)；下一篇 [S2-2 模型层](/posts/dsh-model-layer.html)。
