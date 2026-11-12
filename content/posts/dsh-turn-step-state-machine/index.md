---
title: "一轮到底怎么结束：turn、step 与尝试的三层状态机"
date: "2026-11-11T11:00:00+08:00"
summary: "turn 不是「一问一答」，step 也不是「一次模型调用」。DSH 用三层状态机把排空、请求与尝试分开，才使得重试、取消、工具欠账和空轮次都能被精确描述。"
description: "逐层拆解 Agent 的外部两态、turn 的七种出口、step 内的多次尝试循环，以及 turn-stopping 这个唯一允许「反悔」的窗口。"
tags: ["architecture", "agent", "concurrency"]
slug: "dsh-turn-step-state-machine"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。行号以该 commit 为准，引用一律指向 `src/`。
> 依据：`packages/core/agent-loop/src/agent.ts`（`turn()` :327-433、`step()` :435-610、`wakeDriver()` :230-280）、`packages/core/agent/src/runtime-types.ts`（事件契约）、`packages/core/session/src/types.ts:206-238`（`TurnEndReasonMap`）、`docs/agent-lifecycle.zh.md`。

「Agent 就是 while 循环」这句话对，但它把三个不同的时间尺度压成了一个。压扁之后，很多问题会变得无法回答：

- 模型调用失败了，重试算不算新的一轮？
- 工具执行到一半用户又说了一条消息，这条消息插在哪？
- 一次模型回复里发了 5 个工具调用，其中 3 个成功、2 个被拒，这一「步」结束了吗？
- 用户点了停止，此时模型已经吐了半句话，这半句要不要进历史？

DSH 把它们拆成 turn、step、attempt 三层，每层有自己的开始、结束和失败语义。

## 1. 三个时间尺度

| 尺度 | 定义 | 谁开启 | 谁关闭 | 数量关系 |
| --- | --- | --- | --- | --- |
| turn | 一次对「已接纳输入」的排空 | `followup` 唤醒 | `turn/end`（7 种原因） | 一个 agent 同时最多一个 |
| step | 一次模型请求 + 它要求的工具 | `step/start` | `step/end` | 一个 turn 内 0..N 个 |
| attempt | 一次 `llm.stream()` 调用 | 进入 `step()` 内层循环 | 流结算（message 或 attempt 事件） | 一个 step 内 1..N 次 |

三个数字关系：

- 一个 turn 可以有 0 个 step。首批判空、被 `agent/pre-step` 拒绝、或者 pre-step 抛错，turn 都会直接关闭，不花模型调用。
- 一个 step 可以有多次 attempt。它跟「重试」不是一回事：工具执行完还有欠账时，同一个 step 会继续发起下一次请求（第 5 节）。
- attempt 不独立存在。它一定属于某个 step，并且以 `assistant/message` 或 `assistant/attempt` 之一结算。

{{< fig "diagrams/01-agent-states.svg" "Agent 的外部状态只有 idle / running 两态；dispose 是一个动作，没有对应的第三态，它把 agent 从注册表移除。内部还有一个不对外暴露的 maintenance 阶段，公开状态仍报 idle。" >}}

## 2. 最外层：两个公开状态，四个内部阶段

`AgentStatus` 只有两个值：

```ts
type AgentStatus = 'idle' | 'running'
```

注释把语义定得很死：

> idle means no driver remains scheduled or active; running begins when waking input starts cancellable pre-step processing and lasts while the driver drains, closes, or checkpoints turns. Disposal removes the agent from its registry; it is not a third observable status.

`dispose` 是一种能力，不进入状态机。一个 agent 已经 disposed 之后，调 `send()` 会抛错，但 `status` 不会变成 `'disposed'`，因为它根本不该再被观察。把「已销毁」做成第三个状态，调用方就会去轮询它；正确的做法是根本不要再持有它。

公开两态之下，`AgentLoopAgent` 有一个私有的 `phase`：

```ts
phase:
  | { kind: 'idle' }
  | { kind: 'running'; turn: TurnNumber; step: number;
      abort: { controller: AbortController; signal: AbortSignal };
      wakeRequested: boolean }
```

`maintenance` 是另一条独立通道。`runMaintenance(task)` 只有在完全 idle（没有 currentActivity、也没有已排队的 maintenance）时才能拿到阶段；执行期间 `status` 仍报 `idle`，但 `whenIdle()` 会等它。它给后台整理用，比如压缩、索引重建，不该让 UI 显示「正在思考」。

四个内部阶段是：`idle` → `running.pre-step` → `running.step` → `running.closing`，再加上旁路的 `maintenance`。它们不对外暴露，因为一旦暴露，每个消费者都要处理「pre-step 失败算不算 running」这类问题。

## 3. turn 的七种出口

{{< fig "diagrams/02-turn-exits.svg" "turn/end 的七个 reason。completed / blocked / aborted / error 是运行期出口，max-tokens 是 completed 的一种特殊形态，interrupted 与 forked 只可能由恢复或 fork 写入。" >}}

`TurnEndReasonMap` 恰好七个成员：

| reason | 何时产生 | 是否消耗 step |
| --- | --- | --- |
| `{kind:'completed'}` | 模型不再要求工具，且 next-step 为空 | 是（至少一个） |
| `{kind:'blocked'}` | `agent/pre-step` 返回 `reject` | 否 |
| `{kind:'aborted', reason}` | 取消原因 `user`/`parent`/`hook`/`disposed` | 可能 |
| `{kind:'error', error}` | 未恢复的失败，带规范化 `LlmFailure` | 可能 |
| `{kind:'max-tokens'}` | 模型因输出上限停止，且没有工具调用 | 是 |
| `{kind:'interrupted'}` | 只由恢复路径补写：日志里最后一个 turn 没有配对 end | 未知 |
| `{kind:'forked'}` | 只由 fork 种子写：切点上未闭合的 turn | 未知 |

后两个是事后补写的，agent loop 自己永远不会实时发出 `interrupted` 或 `forked`。这样「崩溃/分叉」和「正常结束」在日志里就能区分开，不用额外的 `crashed: true` 标志。

`turn()` 的骨架（`:327-433`）可以读成一个带 `return` 的循环：

```ts
private async turn(): Promise<boolean> {
  this.session.append('turn/start', { turn: ++this.turnNumber })
  let turnEnds: TurnEndReason | undefined
  while (true) {
    const decision = await this.preStep(target, { turn, step })
    if (decision.kind === 'reject') {
      turnEnds = { kind: 'blocked' }
      return false                      // 没有 step，直接关轮
    }
    if (turnEnds && decision.messages.length === 0) break
    if (this.phase.step === 0 && decision.messages.length === 0) {
      turnEnds = { kind: 'completed' }
      return false                      // 轮次边界被占，但不花模型调用
    }
    this.session.append('step/start', { turn, step })
    try {
      const stop = await this.step(decision)
      if (stop) { turnEnds = stop }
      if (transitionSawNewInboxInput) break
    } finally {
      this.session.append('step/end', { turn, step })
      decision.toolSet.dispose()
    }
  }
  if (turnEnds && this.inbox.nextStep.length === 0) {
    await this.dispatch.serial('agent/turn-stopping', { turn, signal })
    if (this.inbox.nextStep.length > 0) continue   // 有人 steer 了
  }
  return false
}
```

几个容易读漏的点：

`turn/start` 在 claim 之前写。所以这一轮什么都没做（被拒绝、空批），日志里也会有一条完整的 `turn/start → turn/end`。它不是噪音：UI 要显示「你的消息被拒绝了」，而拒绝得挂在一个已存在的 turn 上。

`step/end` 写在 `finally` 里。`step/end` 存在只说明这一步结束了，不说明成功。要判断成功，得读 `assistant/message` 或 `turn/end.reason`。第一次写监控的人常在这里翻车。

`turnEnds && messages.length === 0` 才 break。这一步已经「得出结论」（比如工具返回了 `concludesTurn`），但同一步又注入了 `additionalContexts`，那批上下文要先消费掉，turn 才能关。这样结论不会把还没跑的工作短路掉。

`phase.step === 0 && messages.length === 0` 走的是 `completed`，不走 `blocked`。blocked 表示「有输入但被策略拒绝」，completed 表示「这一轮没有需要模型处理的输入」。两者都不花模型调用，对用户的解释却完全不同。

## 4. step 内部：一个 while，两个出口

{{< fig "diagrams/03-step-loop.svg" "step() 的内层循环。每次迭代准备请求、发起一次流式调用、结算成 assistant/message 或 assistant/attempt，然后执行工具；工具还欠请求就继续同一步，否则返回停止原因。" >}}

`step()` 返回 `StepEndReason | null`：

- 返回 `null`：同一步还有欠账，继续内层 `while(true)`；
- 返回 `{kind:'completed'|'max-tokens'}` = 这一步自然结束；
- 抛错 = 交给 `turn()` 的 catch 决定 `error` 还是 `aborted`。

内层每一轮做四件事：

1. `prepareRequest()`：取上一条 `request/header` 的 controls（或首次的 `options.maxTokens`），叠当前 `modelSelection`，`deepFreeze`，过 `agent/request` waterfall，再调 `llm.prepareCall()`。
2. 提交 system / user 消息：`systemPrompt.project()` 产生若干 `system/message` commit；首轮把 claim 到的消息按序写成 `user/message`。这两个动作发生在 `agent/request` 之后；如果 `agent/request` 里取消，两者都不会提交。
3. 流式调用：新建 `AssistantStreamAttempt`，`live.start()`，逐 chunk `live.push()`，帧通过 `agent/assistant-stream` 广播（瞬态，不进日志）。
4. 结算：成功写 `assistant/message`（内嵌 stream + usage）；失败写 `assistant/attempt`，然后跑 `agent/request-error` waterfall，只有返回 `{kind:'retry'}` 才 `continue`。

第 4 步的顺序有个细节：先写 attempt，再问要不要重试。重试策略自己崩了，失败证据也已经落盘。反过来先问再写，策略一抛错就丢掉整次尝试的记录。

还有一条规则不太显眼：

```ts
// 只有第一次 attempt 能开启新的 request series
const startsRequestSeries = firstAttempt && decision.startsRequestSeries
```

`startsRequestSeries` 意味着「这是一段全新模型消息序列的起点」，提供方据此决定是否重置某些缓存状态。如果重试也带这个标记，就会把同一段对话反复「重启」，某些提供方会因此丢掉 prompt cache。

## 5. 工具欠账：step 为什么可以多次请求

`tools.execute()` 返回的 `result.concluded` 决定 step 是否结束。`concluded: false` 表示这批工具调用之后，模型还欠一次响应。

出现这种情况有三种：

- 工具明确要求模型继续（例如 `run_code` 里还有未完成的子调用）；
- 工具批次里有 `additionalContexts` 需要模型看见；
- 工具执行器判断「结果还不完整」。

于是同一步里会出现第二次 `llm.stream()`。日志里 `step/start` 只有一条、`assistant/message` 却有几条，原因就在这里：step 的粒度是「模型请求 + 它要求的工具」这一整段工作，单次 HTTP 调用装不下。

{{< fig "diagrams/04-attempt-settlement.svg" "一次流式调用的结算分支：正常完成且有内容写 assistant/message；有内容但被中断写 interrupted 的 message；无内容或非中断失败写 assistant/attempt。两条路都不允许「什么都不写」。" >}}

结算分支可以画成一棵小树：

- 流正常结束 + 有可见内容 → `assistant/message`
- 流被 abort + 已有内容 → `assistant/message`，带 `interrupted: true`，丢弃未派发的 tool call
- 流被 abort + 无内容 → `assistant/attempt`
- 适配器返回 error（未中断）→ `assistant/attempt`
- 结算本身抛错 → `AggregateError`（原始错误 + 结算错误一起抛）

为什么中断时要丢 tool call？因为一个 `tool_call` 没有配对的 `tool_result`，下一次请求的消息序列就不合法。宁可丢掉「模型想做但没做成」的意图，也不能让历史变成提供方拒绝的形态。

## 6. 工具与 turn 的边界

工具执行完之后，`turn()` 还做两件事：

补写缺失的 `tool/result`。如果 `step()` 抛错，`ToolCallRecovery` 会把已经执行完但还没落结果的调用补成 `tool/result`（带 `sourceEventSeqs` 指回 `tool/call`）。如果补写也失败，抛 `AggregateError`。目的只有一个：日志里不允许出现「有 call 没 result」的悬空对。

把 `additionalContexts` 注入 next-step。顺序是：先写 `tool/result`，再把上下文 splice 进 `inbox.nextStep`。这保证了「工具结果」和「由它产生的上下文」在模型看来是相邻的，不会被别的消息插队。

## 7. `agent/turn-stopping`：唯一允许反悔的窗口

{{< fig "diagrams/05-turn-stopping.svg" "收尾时序：step 结束、next-step 为空时进入 serial 的 turn-stopping；监听器可以 steer 来反对关闭，机器重读收件箱后决定再跑一步还是关轮。" >}}

全套事件里，只有这个扩展点是串行的、没有 `next()`、并且在边界提交之前被 await。契约原文：

> The turn is about to close: the model owes no response (no live tool calls, no fresh steering). Awaited before the boundary commits — a listener that objects steers (`agent.steer(...)`) and the machine re-reads its inbox: fresh steering runs another step, none closes the turn. Data decides, so listener order cannot change the outcome.

三个设计点：

它没有 `next()`。这里的语义是「关轮之前你还有没有话说」，跟「要不要继续往下传」无关。用 waterfall 会诱导人写「不调 next 就阻断」，在这个场景里没有意义。

它靠数据反对，不靠返回值。监听器想阻止关轮，就用 `agent.steer()` 塞一条消息；机器重读 `inbox.nextStep`，有就跑下一步。这样监听器顺序不影响结果：A 先跑还是 B 先跑，只要有人 steer，结果都是继续。把「投票」变成「写入共享状态」，顺序就不敏感了。

它不能 await `whenIdle()`。`agent/created` 的文档明确禁止监听器 await `agent.whenIdle()` 或自己 owner 的 disposal，否则死锁。turn-stopping 里等 idle 一样会自锁，因为它就在 idle 的判定路径上。

反方向的「提前结束」也走数据：工具结果带 `concludesTurn`，turn 在那一步结束。它不会短路已经提交的 next-step 工作，同一步的 `additionalContexts` 或并发进来的 steering 仍要跑完，收件箱排空才关轮。

## 8. 取消与唤醒的竞态

取消有三条规则：

```ts
cancel(cause, { keepInbox }) {
  if (!this.currentActivity) return          // 没有活动：no-op，不武装后续工作
  this.phase.abort.controller.abort(cause)   // 第一个 cause 胜出
  if (!keepInbox) this.inbox.clear()
}
```

「没有活动就 no-op」常被当成 bug：用户在已经 idle 的 agent 上点停止，什么都不会发生，也不会「阻止它下次运行」。这个行为是对的。cancel 取消的是一次正在进行的工作，不会给 agent 打上「以后都别跑」的标记。

更微妙的是唤醒与 abort 的竞态：

```ts
send(message, target, wakeup) {
  const wakingAfterAbort =
    this.currentActivity?.signal.aborted === true &&
    this.currentActivity.signal.reason?.kind !== 'disposed'
  this.inbox.append(message, target)
  if (wakeup) this.wakeDriver(wakingAfterAbort)
}
```

如果当前活动正在 abort，`wakeDriver` 不会立刻起新循环，而是置 `phase.wakeRequested = true`。等当前驱动收敛、`turn/end` 写完，外层才发现「有人在 abort 期间又发了消息」，于是补一轮。

不能立刻起，是因为 abort 还没落地：旧的 `AbortController` 仍挂在 `phase` 上，这时起新循环会撞上同一个 signal，新工作立刻又被取消。`wakeRequested` 记下的是一个延迟到安全点的意图。

而 `turn()` 的 `finally` 里会新建一个 `AbortController`：

```ts
if (this.inbox.hasPending) {
  this.phase.abort = new AbortController()   // 旧 latch 失效
  this.phase.wakeRequested = false
  this.phase.step = 0
  return true
}
```

新 controller 让「上一次的 abort」不会污染下一次。这是很多人自己写 Agent 时会漏的一步：复用一个 controller，结果第二次任务一开始就处于已取消状态。

## 9. 设计取舍

外部两态 vs 内部四阶段。两态让 UI 和 SDK 保持简单，代价是「正在 pre-step」和「正在等工具」从外面分不出来。诊断只能看事件流（`step/start`、`tool/call`），status 帮不上忙。

step 可多次请求 vs 一次请求。多次请求保住了「工具调用与结果相邻」和「step 编号稳定」，代价是 `step` 的直觉被打破：它是「一段模型工作」，一次模型调用装不下。文档里得反复解释，这是真实的沟通成本。

turn/end 必写 vs 崩溃即无。`finally` 保证正常路径完整；崩溃路径靠恢复补 `interrupted`。两条路都覆盖，但「turn/end 一定存在」只在非崩溃时成立。

max-tokens 粘住整个 turn。一旦某一步以 max-tokens 结束，turn 的 reason 就定为 max-tokens，后续步骤不会「后来居上」改成 completed。好处是结果单调、UI 好判断；坏处是「先截断后补全」的场景会被标成截断。

空 turn 保留边界。被拒绝的输入也会留下 `turn/start`/`turn/end`。好处是拒绝可解释、时间线连续；坏处是高频拒绝会产生大量空轮次，需要下游按 reason 过滤。

turn-stopping 用 serial，不用 waterfall。放弃了「监听器能否决前一个监听器」的能力，换来顺序无关和不易误用。

## 10. 踩坑点

- 以为一个 step 只有一次模型调用。工具 `concluded: false` 时同一步会再请求；日志里会出现多条 `assistant/message`，夹在同一个 `step/start`/`step/end` 之间。
- 用 `step/end` 判断成功。它在 `finally` 里，异常也会写。成功与否要看 `assistant/message` 是否存在、`turn/end.reason` 是什么。
- 在 pre-step reject 后等 `error`。那是一个正常的 `blocked` 轮次。
- 在 `agent/request` 里改 model。会直接抛错，错误文案要求「use `agent.model.select()` before step preparation」。这个 waterfall 已被标记 deprecated，新代码不该挂。
- 在 turn-stopping 里 await `whenIdle()`。会自锁，它就在关轮判定路径上，等的是自己。
- cancel 后立刻期待新消息开跑。会先被记录成 `wakeRequested`，等 abort 收敛、新 controller 建好才跑。
- 以为 `cancel` 会给 agent 打「停止」标记。没有活动时它是 no-op，也不会影响下一次运行。
- 复用同一个 `AbortController`。下一轮一开始就是 aborted；必须每轮新建。
- 以为 `turn/end` 已落盘。检查点是按请求的，turn 边界不 flush。

## 11. 源码索引

- `packages/core/agent-loop/src/agent.ts:162-180`：`send` 的 abort-latch 逻辑。
- `:199-228`：`runMaintenance` 与「公开状态保持 idle」。
- `:230-280`：`wakeDriver` 的循环与 `wakeRequested` 收尾。
- `:327-433`：`turn()` 的完整出口判定与 `finally` 新建 controller。
- `:435-610`：`step()` 的内层循环、stream settlement、`request-error` 重试。
- `:613-653`：`prepareRequest` 的 selection 一致性与 `NO_ADAPTER` 容忍。
- `packages/core/session/src/types.ts:206-238`：七个 `TurnEndReason`。
- `packages/core/agent/src/runtime-types.ts`：`agent/turn-stopping`（serial）、`agent/pre-step`（waterfall）、`agent/request-error`（waterfall）、`agent/created`（禁止 await whenIdle）。
- `docs/agent-lifecycle.zh.md`：官方时序图。

把三层分开之后，「Agent 是一个 while 循环」可以换成更精确的说法：一个排空循环（turn），里面套工作循环（step），再里面套尝试循环（attempt）。每层有自己的失败语义，日志是它们共同的语言。

上一篇：[会话日志与事件溯源](/posts/dsh-session-event-sourcing.html) ｜ 下一篇：[请求是怎么装出来的](/posts/dsh-request-assembly.html)
