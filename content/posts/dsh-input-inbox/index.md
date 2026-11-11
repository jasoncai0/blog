---
title: "输入的第一公里：两条队列、三种投递与一次唤醒"
date: "2026-11-11T09:20:00+08:00"
summary: "把一次用户输入拆成一条 splice 事件、两条队列、三种投递与一个唤醒闩锁；解释 followup 为何返回 void、steer 为何可能错过一个 step、abort 之后的消息为何必须改道 next-turn。"
description: "DSH 输入平面源码剖析：next-turn/next-step 双队列、followup/steer/inject 三种投递、wake 收敛与只取两个值的 AgentStatus。"
tags: ["harness", "architecture", "concurrency"]
slug: "dsh-input-inbox"
---

> 源码版本：0.2.2-rc.1 commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。行号以该 commit 为准。
> 本系列第 3 篇。系列目录：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)。

在 Web 输入框里敲一句话，回车。浏览器发出一个 HTTP 请求，服务端调用 `agent.followup(message)`，然后——什么也没返回。这个方法签名是 `void`，没有 promise，没有 receipt id，连「收到」都没有。你只能盯着界面上的转圈，等一个 `turn/start` 事件从另一条通道飘回来。

更别扭的是同一句话的三种发法。`followup` 让它独占下一轮；`steer` 让它挤进最近的下一步；`inject` 干脆不唤醒，只在某个未来的 step 边界被顺路捡走。三个方法体都只有一行，都转调同一个 `send()`，语义却差出一个数量级。

还有第三种反直觉：用户按下停止，再立刻发一条消息，这条消息不会进入 `next-step`，而是被强行改道到 `next-turn`。代码里为此专门在插入之前读了一次 `phase.abort.signal.aborted`，注释写着「在插入前捕获，避免 splice 观察者里的重入 cancel 重新分类」。

这篇拆开输入平面的第一公里：一条消息如何变成持久事件，两条队列各自在什么边界被消费，三种投递在什么条件下等价、在什么条件下分叉，以及一次唤醒如何在不引入锁的情况下收敛。

## 0. 一句话结论

输入在 DSH 里不是函数调用，而是持久队列上的一次状态迁移：`send()` 先把消息作为 `agent/inbox/spliced` 提交进会话日志，再决定要不要叫醒 driver；消息最终被看见的位置由「进了哪条队列」和「哪个边界先到」共同决定。三种投递只是两个参数的语法糖：目标队列（`next-turn` / `next-step`）与是否唤醒（`wakeup`）。

## 1. 一次输入就是一次 append

`send()` 是整个输入平面唯一的入口，全文六行（`packages/core/agent-loop/src/agent.ts:170`）：

```ts
send(message: UserMessage, target: InboxTarget, wakeup: boolean): void {
  // Waking input cannot join an aborted activity, so it starts the next turn.
  // Captured before the insertion so a reentrant cancel from a splice observer cannot reclassify it.
  const wakingAfterAbort = wakeup && this.phase.kind !== 'idle' && this.phase.abort.signal.aborted
  const resolvedTarget = wakingAfterAbort ? 'next-turn' : target
  this.inbox.splice(resolvedTarget, Infinity, 0, [message])
  if (wakeup) this.wakeDriver(wakingAfterAbort)
}
```

四步，顺序不能换。第一步读当前 phase，判断这条唤醒输入是不是撞上了一个已经 abort、但还没收敛的活动；第二步据此改写目标队列；第三步把消息交给 inbox 做规范化插入；第四步才决定唤醒。

`Infinity` 不是随手写的。`ReactLoopInbox.mutate()`（`packages/core/agent-loop/src/inbox.ts:186`）对 start 做截断：`NaN` 归零，负数从尾部算，正数 clamp 到数组长度。`Infinity` 因此稳定地表示「追加到末尾」，不需要在调用点区分 append 与 splice。

关键在于第三行：`inbox.splice()` 不是内存操作，它先构造并提交一条会话事件。`mutate()` 的尾部写着（`inbox.ts:223`）：

```ts
const event = this.session.append('agent/inbox/spliced', splice)
if (discardRemoved) {
  for (const message of removed) this.dispatch.emit('agent/inbox/discarded', { message })
}
for (const message of event.data.inserted) {
  this.dispatch.emit('agent/inbox/inserted', { message })
}
```

`session.append()` 是同步的，inbox 投影在它返回之前就已经把事件折叠进状态；实时通知 `agent/inbox/inserted` 排在提交之后。这条顺序是硬约束：任何监听器收到「插入了」时，日志里一定已经有这条记录。否则消费者会看到一个尚未持久化的事实，崩溃后无法重建。

返回值因此可以是 `void`。回执不是方法的返回值，而是事件流里的 `MessageId`。`docs/subsystems/core.zh.md:150` 把这层意思写得很直白：`MessageId` 标识的是持久的「插入 / 认领 / 丢弃」事实，而不是之后的助手输出或轮次结束。如果你要等某条消息被处理完，`followup()` 的返回值帮不了你，你得跟踪这条 id 在事件流里的下一次出现。

### 事件长什么样

`agent/inbox/spliced` 的 payload 只有五个字段（`packages/core/agent/src/types.ts`）：

```ts
'agent/inbox/spliced': {
  target: InboxTarget
  start: number
  removedCount?: number
  inserted: UserMessage[]
  outcome?: 'canceled'
}
```

`removedCount` 省略表示纯插入，`outcome: 'canceled'` 只在 `discardRemoved` 为真且确实删了东西时出现。注意它描述的是「一次数组操作」，不是「一条消息的一生」——投影靠按顺序折叠这些操作重建队列，而不是靠解析单条事件。

投影本身还有一层防御（`inbox.ts:26`）。fold 只接受 `agent/inbox/spliced`，校验 start 与 removedCount 是安全整数、坐标在界内、`toSpliced` 能执行，再检查两条列表合起来没有重复 id。任何一条不满足，错误会被包成 `invalid persisted inbox splice at session seq ${event.seq}`——它告诉你哪一段历史坏了，而不是在冷启动时悄悄丢一条消息。

wire 形态是两份 `JsonValue[]`，而不是 `UserMessage[]`。原因写在注释里：`UserMessage.source` 是一个可扩展的联合类型，其他插件可以往里加 `unknown` 的回放字段，这种值不能跨 typert 的 Remote 边界。所以投影表只承诺 JSON 安全的部分，服务端与客户端各自在本地恢复成 `UserMessage`。

**自己搭时**：不要先 push 进内存数组、再异步落盘。先把「入队」提交成一条持久事件，再从事件派生内存队列；否则进程在 push 与落盘之间崩溃，用户的消息就凭空消失了。内存队列只能是日志的投影，不能是事实本身。

## 2. 两条队列，两个消费边界

`InboxTarget` 只有两个值（`packages/core/agent/src/types.ts`）：

```ts
type InboxTarget = 'next-turn' | 'next-step'
```

它们不是优先级，而是两个不同的消费边界。

`next-step` 在每一个 step 的 `agent/pre-step` 之前被整批领走。`next-turn` 只在轮次边界上被领走，而且一次只领一条。`claim()` 把这件事写死在六行里（`inbox.ts:108`）：

```ts
claim(target: InboxTarget, turn: number): UserMessage[] {
  const claimed = this.mutate('next-step', 0, this.nextStep.length, [], false)
  if (target === 'next-turn') claimed.push(...this.mutate('next-turn', 0, 1, [], false))
  for (const message of claimed) dispatch.emit('agent/inbox/claimed', { message, turn })
  return claimed
}
```

两个细节值得停一下。

第一，顺序是先 `next-step` 再 `next-turn`。所以轮次第一步的批次排列是「所有已 staged 的 next-step 项，再接一条 next-turn 消息」。这解释了一个容易踩的现象：idle 时先 `inject()` 一条文件变更通知，再 `followup()` 一句「看看这个」，模型看到的顺序是通知在前、问题在后。这不是巧合，是 claim 的拼接顺序。

第二，`mutate(..., false)` 的最后一个参数是 `discardRemoved`。claim 用纯删除 splice，不发 `agent/inbox/discarded`。这条区别是下一篇（04）的核心，这里先记住结论：被 claim 不等于被取消。

### 两个边界的对照

| | turn 边界 | step 边界 |
| --- | --- | --- |
| 触发点 | `turn()` 开头，`target='next-turn'` | 每个 `step()` 之前，`target='next-step'` |
| 领多少 | next-step 全部 + next-turn 一条 | next-step 全部（next-turn 不动） |
| 消息去向 | 进入本轮第一个 step 的批次 | 进入当前 step 的批次 |
| 未进入请求时 | 轮次可能以空批次关闭（`completed`） | 批次可能被 pre-step 改写或 reject |
| 谁决定「有没有下一步」 | `turn()` 尾部的 `hasPending` | `step()` 返回值 + `turnEnds` |

这张表解释了一个常见误解：`next-step` 不是「下一步专用」，它同样会在轮次的第一步被领走。所谓「steer 挤进最近的 step」，在轮次还没开始时，最近的 step 就是第一步。

{{< fig "diagrams/01-inbox-two-queues.svg" "状态机：两条队列与一条消息的四种归宿。消息先按投递方式进入 next-turn 或 next-step；轮次边界从 next-turn 领一条，step 边界把 next-step 整批领走；认领后进入当前 step 的准入，未被准入就停在 claimed（既不算丢弃，也不会成为 user/message）。只有显式删除、clear() 或取消才发 discarded。" >}}

为什么需要两条队列，而不是一条带优先级的队列？因为「独占一轮」和「并入下一步」是两种无法互相表达的意图。`next-turn` 的消息承诺自己是一个 turn 的唯一普通消息（`followup` 的 JSDoc 原话：the item becomes the sole ordinary message of its own turn），这样一轮的模型请求不会把两条无关的用户指令混进同一个 prompt。`next-step` 的消息则承认自己要和别的输入共享一个 step，它没有「独占」的语义，只有「尽快被看见」。

**自己搭时**：如果你的 Agent 只有一条输入队列，你最终一定会用「优先级 + 合并策略」去模拟这两种意图，然后在某个边界上把两条用户指令合并进同一次请求。拆成两条队列，等于把「独占」和「搭车」变成类型，而不是运行时的 if。

## 3. 三种投递只是两个参数

三个公开方法的全部实现（`agent.ts:179`）：

```ts
followup(input: UserMessage): void { this.send(input, 'next-turn', true) }
steer(input: UserMessage): void    { this.send(input, 'next-step', true) }
inject(input: UserMessage): void   { this.send(input, 'next-step', false) }
```

`followup` 与 `steer` 都唤醒，区别只在目标队列；`steer` 与 `inject` 目标相同，区别只在是否唤醒。四种组合里唯一没有公开方法的是 `next-turn + 不唤醒`——那等于把消息永久停放在一个没人会来看的队列里，没有语义。

| 投递 | 目标 | 唤醒 | idle 时 | running 时 | 可能错过 |
| --- | --- | --- | --- | --- | --- |
| `followup` | next-turn | 是 | 立即开新 turn | 排队，当前 driver 收尾时领走 | 不会（但可能等一整轮） |
| `steer` | next-step | 是 | 立即开新 turn，消息进第一步批次 | 下一个 step 边界领走 | 当前 step 已 claim 完 |
| `inject` | next-step | 否 | 停在 pending，直到别的投递唤醒 | 下一个 step 边界领走 | 同上，且可能永远等不到唤醒 |

「唤醒」这个词在这里有精确含义：它只保证 driver 会被启动或继续，不保证这条消息被看见。`wakeDriver` 的注释说得很清楚：idle 时的唤醒总会打开 turn 边界，即使消息在 driver claim 之前被清掉。也就是说，唤醒是对「打开一次轮次」的承诺，不是对「这条消息会被消费」的承诺。

`inject` 的不唤醒是刻意的。它的典型生产者是文件监视器、hook 通知、工具追加的上下文——这些东西没有「用户在等我回复」的语义，不该为它们花一次模型调用。`docs/subsystems/core.zh.md` 对此的表述是：running driver 在最近的后续 step 边界领取，idle driver 让它 pending 直到 followup 或 steer 唤醒。

### 一个文档与实现的缝

`steer` 的 JSDoc 有一句：「A rejected step leaves steering parked in the inbox until the next wake」。按字面读，被 reject 的 step 应该把整个 steering 批次放回 inbox。实现不是这样：`preStep()` 先 `claim()` 再走 waterfall，reject 时批次已经离开队列，`turn()` 直接 `return false`，没有任何回插。

测试 `preserves input staged after the blocked batch was claimed`（`packages/core/agent-loop/tests/interception.spec.ts:338`）给出了实际语义：被 claim 的那条（blocked prompt）不会出现在下一次请求里，而在 pre-step 挂起期间新 staged 的 context 与 steering 会留在 `next-step`，等下一次唤醒被领走。

所以那句话要读成「reject 决策期间新 staged 的 steering 会留在 inbox」，而不是「被 reject 的批次会回滚」。这两种读法差一条消息的归属，而这条消息可能正是用户刚发的那句。写集成代码时，别把「reject 会回滚」当成契约。

{{< fig "diagrams/02-steer-vs-inject.svg" "时序对比：steer 与 inject 在同一时间轴上的分叉。两者都写入 next-step 并在下一个 pre-step 被 claim；差别在写入瞬间——steer 会调用 wakeDriver，所以在 idle 时立刻打开 turn 边界，而 inject 只留下一条 spliced 事件，队列原地等待。图中同时标出「错过已 claim 批次」的位置。" >}}

### 一次运行里的三种到达

三条消息可以同时落进一个正在跑的轮次。设 turn T 的 step 1 正在等模型，调用方先 `inject` 一条 runtime context，再 `steer` 一句补充，最后 `followup` 一个新问题：前两条按到达顺序追加进 `next-step`，第三条进 `next-turn`。此时没有 driver 需要启动——`steer` 与 `followup` 的 `wakeDriver` 都直接返回，`inject` 根本不调它。

step 1 结束时，只要 `next-step` 非空，轮次就不收尾（`turnEnds && inbox.nextStep.length === 0` 才 break）：driver 原地进 step 2，`preStep()` 把 inject 与 steer 整批 claim，两条一起进入同一次请求的 `user/message`。step 2 结束、`turnEnds` 为真而 `next-step` 已空，才写 `turn/end`；又因为 `next-turn` 里还有 followup，同一个 driver 换一个 controller 接着跑，`turn/start(T+1)` 之后把 followup 领成新轮的唯一一条普通消息。两次 claim 之间隔着一次真实的模型调用，中间没有调度器——只是循环回到 `preStep()`。

{{< fig "diagrams/03-three-deliveries-sequence.svg" "时序：三条消息落进同一个正在运行的 turn。inject 写入 next-step 后不唤醒；steer 与 followup 的 wakeDriver 因为 driver 还活着而直接返回。step 1 结束、next-step 非空，轮次多走一个 step，inject 与 steer 被整批 claim 进同一次请求；step 2 结束且不再调工具、next-step 为空，才写 turn/end，随后同一 driver 开启 turn T+1，followup 在那里被领走。两种等待的分界就是 step 边界与 turn 边界。" >}}

两种等待的分界就在边界：`next-step` 等 step 边界，`next-turn` 等 turn 边界。这也解释了一个容易看反的地方——运行中的 inject 不需要 steer 来「拖」它进下一次请求，只要 `next-step` 非空，当前轮次就会为它多走一个 step。inject 真正会输的场景是到达时轮次已经 idle：没有唤醒，它停在 pending，直到某条 followup 或 steer 打开新的 turn。

## 4. 一次 followup 从 send 到 turn/start

把一次 `followup` 拆成时序，会撞见一件反直觉的事：这个返回 `void` 的同步方法，在返回之前就已经把轮次打开、把消息认领完了。「方法返回」与「轮次开始」之间没有间隔。

1. 调用方构造 `UserMessage`（带 `MessageId` 与 `source`），调 `agent.followup()`。
2. `send()` 判定 `wakingAfterAbort`，目标为 `next-turn`。
3. `inbox.splice()` → `mutate()` → `session.append('agent/inbox/spliced')`。此刻消息已经可回放。
4. `dispatch.emit('agent/inbox/inserted')`，UI 可以画出一条「待处理」。
5. `wakeDriver(false)`。若 phase 是 idle，替换 `activityDone`，`setPhase({kind:'running', ...})`，`agent/status` 发 `running`。
6. `withInitiator(this, () => this.kick())`。`withInitiator` 直接调用回调并原样返回，所以 `kick()` 此刻同步启动，一路跑到它自己的第一个 `await`。
7. `turn()`：`signal.throwIfAborted()`，`session.append('turn/start', {turn})`，写回 `phase.turn`。
8. `preStep('next-turn', {turn, step:1})`：`inbox.claim()` 是同步的，领走 next-step 全部 + next-turn 一条，逐条发 `agent/inbox/claimed`。
9. 到这里才遇到第一个真正的挂起点：`await tools.resolve(...)`。之后是 `systemPrompt.assemble()` 与 `agent/pre-step` waterfall，再往后才是 `step/start` 与请求组装。
10. 返回发生在第 9 步的第一个 `await` 处：`kick()` 同步跑到 `tools.resolve` 才让出。调用方拿到 `void` 时，日志里已经有 `turn/start` 和 `claimed`，但 `agent/pre-step` 还没跑、请求还没组装。

{{< fig "diagrams/04-followup-timeline.svg" "时序：一次 followup 从 send 到 step/start。前八步全部发生在方法返回之前——`kick()` 被同步调用并跑到第一个 await，所以 turn/start 与 claim 都已经落盘；第一个真正的挂起点是 `tools.resolve`。这也解释了为什么在 inserted 监听器里 cancel() 能影响本次唤醒。" >}}

第 5 步里有个容易忽略的分支：`wakeDriver` 只有在 phase 是 idle 时才真的启动。如果此刻 driver 正在跑（比如上一轮还没收尾），它什么也不做——注释说 live driver 自己会领队列。这正是「排队」在实现上的样子：没有队列管理器，没有调度器，只有一个已经在跑的循环，和它下一次 claim 时自然会看到的数组。

`turn/start` 的写入点在 claim 之前，这一点很重要。它意味着即使 claim 回来是空批次（消息在唤醒与 claim 之间被删掉了），轮次边界也已经打开，随后以 `{kind:'completed'}` 关闭，不花一次模型调用。`turn()` 里对此有专门的分支（`agent.ts:356`）：

```ts
// A removed waking message or an enter decision rewritten to empty
// still owns the initial turn boundary, but it spends no model call.
if (phase.step === 0 && decision.messages.length === 0) {
  turnEnds = { kind: 'completed' }
  return false
}
```

为什么要保留这个空轮次？因为「唤醒」的承诺是打开边界。UI 可能已经把状态切成「正在处理」，插件可能已经在 `turn/start` 上挂了初始化；如果因为消息被清掉就整轮不出现，这些消费者会永远等不到收尾。空轮次是一次代价为零的兑现。

**自己搭时**：把「开始」与「有活干」分开。先写边界事件，再判断边界内有没有内容；不要用「有内容」作为「开始」的前置条件，否则取消与清空会把状态机卡在中间态。

## 5. 为什么 `wakingAfterAbort` 必须在插入前捕获

现在回到开头那个奇怪的三元表达式。它要解决的问题是：一条唤醒输入到达时，当前活动可能已经被取消，但还没有收敛到 idle。

`phase` 有三种形态（`agent.ts:53`）：

```ts
type Phase =
  | { kind: 'idle'; lastTurn: number }
  | { kind: 'maintenance'; abort: AbortController; lastTurn: number; wakeRequested: boolean }
  | { kind: 'running'; abort: AbortController; turn: number; step: number; wakeRequested: boolean }
```

`aborted` 不是一种 phase，而是 `running` 或 `maintenance` 上那个 `AbortController` 的状态。取消之后、driver 退出之前，phase 仍然是 `running`，signal 却已经死了。此时如果按默认目标把消息放进 `next-step`，会发生什么？

消息入队 → 旧 driver 还活着，可能正在某个 step 的尾部 → 它回到循环顶部，`target` 变成 `next-step` → `preStep()` 先 claim，把这条消息领走 → `signal.throwIfAborted()` 抛出 → `turn/end` 记为 aborted。消息被 claim 了，但永远不会成为 `user/message`。对用户来说，这就是「我发了，但它没了」。

所以 `send()` 把这类唤醒输入改道到 `next-turn`，并且把 `wakingAfterAbort` 传给 `wakeDriver`。后者在活动未收敛时把 `wakeRequested` 闩上，等收敛时再 replay（第 7 节）。消息留在 `next-turn` 里，等下一个真正活着的 driver 来领。

那为什么必须在插入前捕获，而不能在插入后、唤醒前再读一次？注释给的答案是「splice 观察者里的重入 cancel」。`inbox.splice()` 会同步发 `agent/inbox/inserted`，监听器是任意插件代码，完全可能在里面调用 `agent.cancel()`。如果在插入之后才读 phase，读到的可能是「刚刚被这个观察者 abort 的活动」，于是同一条消息被分类成两种目标：先按 `next-step` 落库，再按 `next-turn` 唤醒。日志里留下一条落在错误队列的记录，而唤醒闩锁却按另一条路径设置——两个事实对不上。

先读后写还有一层好处：分类与插入之间没有 await，也没有回调，所以这个判断是原子的。JS 单线程在这里帮了忙，但前提是你不在中间调用任何可能重入的函数。

### 重入的三种形态

**在 `inserted` 监听器里取消。** 这是注释点名的场景。`send()` 已经算好 `wakingAfterAbort=false`（因为读的时候活动还活着），插入触发的监听器调用 `cancel()`，活动被 abort。接下来 `wakeDriver(false)` 看到 phase 已 abort，但 `wakeAfterAbort` 是 false，于是不闩锁——消息进了 next-step，driver 正在死，没人会来领它。这是一条真实的丢失路径，但它不是 `send()` 的 bug：监听器在输入落库之后取消了整个活动，`cancel()` 的清队列逻辑本应把这个活动队列里的东西一起清掉。之所以没清，是因为消息是在 `clear()` 之后插入的——同一个同步栈里的因果倒置。

**在 `discarded` 监听器里重新 `send()`。** 一条消息被删除，监听器决定「那我重新发一次」。新消息会走完整的 `send()`，按当时的 phase 重新分类。如果旧活动已经 abort，新消息会被正确改道到 next-turn；如果还没 abort，它会落回同一个正在死的活动，然后再丢一次。对策不是禁止重发，而是不要在 `discarded` 里同步重发——把它交给一个独立的唤醒，让状态先收敛。

**在 `agent/pre-step` 里 `steer()` 自己。** 这是合法的：监听器拿到 `messages`，可以再往 next-step 塞一条，机器会在本次 claim 之后的下一个边界看到它。但要注意当前批次已经 claim 完了，新塞的这条不会进入 `decision.messages`，而是进入下一次 claim。想影响当前请求，必须改 `messages` 并返回新的决策，不能靠 `steer()`。

**自己搭时**：任何「先判断状态、再修改状态、修改会触发回调」的路径，都要把判断提到修改之前，并且保证中间没有用户代码。这不是防御式编程，是重入的基本功。

## 6. claim 为什么用纯删除 splice，不发 discarded

`mutate()` 的签名里有一个布尔参数 `discardRemoved`。`splice()`、`replace()`、`remove()`、`clear()` 传 `true`，`claim()` 传 `false`（`inbox.ts:108`）。差别只在两行：

```ts
const outcome = discardRemoved && actualDeleteCount > 0 ? 'canceled' : undefined
...
if (discardRemoved) for (const message of removed) dispatch.emit('agent/inbox/discarded', { message })
```

`splice` 事件的 `outcome: 'canceled'` 与随后的 `discarded` 通知是一对语义：这条消息被移出队列，并且原因是「不要了」。`claim` 的删除不是「不要了」，是「拿走了」。它随后逐条发 `agent/inbox/claimed`，把消息交给当前 turn。两个通知的消费者完全不同：`discarded` 的消费者在释放资源（比如 user-questions 服务收到 `agent/inbox/discarded` 就释放排队回复的登记），`claimed` 的消费者在记录归属（同一个服务用它记下「这条回复属于第几轮」）。

如果 claim 也发 `discarded`，会发生什么？一条刚刚被认领、马上要变成 `user/message` 的用户回复，会先收到「已丢弃」，再收到「已认领」。释放资源的监听器已经把登记删了，认领监听器再来记 turn 就会落空；UI 上则会出现一闪而过的「已取消」。更糟的是重放：`outcome: 'canceled'` 是持久字段，冷启动折叠日志时也会看到「这条消息被取消过」，而它实际上进了模型上下文。

`discarded` 的另一个消费者是 UI 的「消息撤回」。把 claim 混进去，等于告诉用户「你刚发的话被撤回了」，而它其实正在被处理。

那 claim 的删除为什么还要写进日志？因为它必须可回放。冷启动时 inbox 投影需要知道「这条消息已经不在队列里」，否则重启后会把已经被消费的消息再发一遍。纯删除 splice 保留了「移除了什么」的事实，只是不带「为什么」的判决。

**自己搭时**：给「移除」至少分两种语义——「被取走」和「被放弃」。它们在日志里可以是同一种结构，但通知与副作用必须分开；否则你会在某个消费者里把「正在处理」误判成「已取消」。

## 7. 一次唤醒如何收敛

`wakeDriver()` 是输入平面唯一会改变 `phase.kind` 的函数（`agent.ts:230`）：

```ts
private wakeDriver(wakeAfterAbort = false): void {
  if (this.phase.kind !== 'idle') {
    const reason = abortedCancelCause(this.phase.abort.signal)
    if (reason?.kind !== 'disposed' && (this.phase.kind === 'maintenance' || wakeAfterAbort)) {
      this.phase.wakeRequested = true
    }
    return
  }
  const driver = Promise.withResolvers<void>()
  this.activityDone = driver.promise
  this.setPhase({ kind: 'running', abort: new AbortController(), turn: this.phase.lastTurn, step: 0, wakeRequested: false })
  this.loopCtx.agents.withInitiator(this, () => this.kick()).then(driver.resolve, driver.reject)
}
```

三个分支对应三种收敛路径。

**idle**：直接开。新的 driver promise 替换 `activityDone`，新 `AbortController`，`turn` 从 `lastTurn` 续上，`step` 归零。

**maintenance**：闩锁。维护任务期间 public status 仍是 idle，但它占着 agent；如果这里直接开 driver，就会有两个活动同时改 inbox、同时递增 turn 计数器。所以只置 `wakeRequested = true`，等 `runMaintenance` 的 finally 收尾时 replay。

**running 且已 abort**：只有 `wakeAfterAbort` 为真时才闩锁。为什么不是所有唤醒都闩？因为活着的 driver 不需要别人替它记「还有活」——它下一次 claim 自然会看到队列。替它闩上反而会在收尾时多启动一个 driver，把同一批消息领两遍（好在 claim 是原子的，第二遍会是空批次，但会多出一个空轮次）。

**disposed 的例外**：`reason?.kind !== 'disposed'`。取消原因是 disposal 时，不闩锁、不 replay。teardown 不该等一次模型调用，也不该在作用域已经注销之后再把 driver 拉起来。

闩锁的兑现点在两个 finally 里。`kick()`（`agent.ts:268`）：

```ts
finally {
  if (this.phase.kind === 'running') {
    const { turn, wakeRequested } = this.phase
    this.setPhase({ kind: 'idle', lastTurn: turn })
    if (wakeRequested && this.inbox.hasPending) this.wakeDriver()
  }
}
```

`runMaintenance()` 的 finally 同构，只是 `lastTurn` 来自 maintenance phase（`agent.ts:211`）：

```ts
finally {
  this.setPhase({ kind: 'idle', lastTurn: maintenance.lastTurn })
  const cause = abortedCancelCause(maintenance.abort.signal)
  if (cause?.kind !== 'disposed' && maintenance.wakeRequested && this.inbox.hasPending) this.wakeDriver()
  done.resolve()
}
```

顺序里有一条硬约束：`setPhase(idle)` 必须在 `wakeDriver()` 之前。后者只在 phase 是 idle 时启动 driver；如果先唤醒再迁移 phase，它会走进 maintenance 分支，把这次唤醒闩到维护任务自己身上，而维护任务的 finally 已经跑过了，闩锁再也没有兑现点。`done.resolve()` 放在最后，是让「等维护任务」在语义上等于「等整个活动」，包括它释放出来的唤醒。

两处 finally 都检查 `hasPending`：闩锁只记录「有人想唤醒」，真正要不要开，取决于队列里还有没有东西。这是第二道去抖——消息可能在闩锁之后、兑现之前被 `clear()` 清掉。

还有第三道：`turn()` 的尾部（`agent.ts:427`）。

```ts
if (!this.inbox.hasPending) return false
phase.abort = new AbortController()
// A fresh controller makes a latch set on the old one stale: the live driver claims the queue itself.
phase.wakeRequested = false
phase.step = 0
return true
```

一个 turn 结束时如果队列还有活，driver 不退出，而是换一个 controller、清掉闩锁、原地开下一轮。换 controller 是因为旧的已经 abort 或即将被丢弃，`wakeRequested` 是挂在旧 controller 上的判断；不换就会把上一轮的闩锁状态带进下一轮。

{{< fig "diagrams/05-wake-convergence.svg" "状态机：wake 的收敛。idle 收到唤醒直接开 driver；maintenance 与「running 但已 abort」只能置 wakeRequested；disposed 的取消连闩锁都不置。闩锁在两个 finally 里兑现，兑现前还要再查一次 hasPending；turn 尾部换 controller 时清闩锁。" >}}

**自己搭时**：不要在「活动未收敛」时启动第二个执行者。用一个闩锁记录意图，在唯一的收尾点兑现，并在兑现前重新检查前提。这套模式比锁简单，也比「等一个 promise 再重试」可靠——后者会在收尾点之后留下竞态窗口。

## 8. 唤醒的实现：一个句柄、一个循环、三个入口

第 7 节回答的是「什么条件下允许启动」。这一节看启动本身：`wakeDriver()` 的 idle 分支只有四条语句，agent 的活动生命周期却都由它们定义。

### 启动：四条语句的顺序

```ts
const driver = Promise.withResolvers<void>()
this.activityDone = driver.promise
this.setPhase({ kind: 'running', abort: new AbortController(), turn: this.phase.lastTurn, step: 0, wakeRequested: false })
this.loopCtx.agents.withInitiator(this, () => this.kick()).then(driver.resolve, driver.reject)
```

顺序不是风格问题。

**先造句柄，再启动。** `kick()` 一旦跑起来，就可能在任意一个 await 后结束；完成信号必须提前存在。`Promise.withResolvers()` 先把 resolve/reject 拿到手，再让执行体去跑，这样「结束」永远不会早于「登记」。

**句柄先换，status 后发。** `activityDone` 替换之后才 `setPhase()`；后者会同步发 `agent/status`，监听器可能在 `wakeDriver()` 还没返回时就跑起来。句柄写在前面，监听器看到的 status 与句柄指向同一次活动；反过来，status 已经宣布 running，`whenIdle()` 进来却先拿到上一轮已经 resolve 的句柄——do-while 会在下一拍纠正，但 status 与 `whenIdle()` 的等待对象在同一个同步段里先对不上一次。

**判断与迁移之间没有 await。** 从 `phase.kind !== 'idle'` 到 `setPhase(running)` 是同一段同步代码，没有回调，也没有挂起点。两个先后到达的 `followup()` 不可能各起一个 driver：第一个把 phase 改成 running，第二个看到 running 就返回，把队列留给第一个。单 driver 的唯一性靠这段原子性，不靠锁。

**`withInitiator` 同步调用 `kick()`，并接上完成信号。** `withInitiator()` 把这次执行登记到 agent 的 initiator 作用域（`AsyncLocalStorage`），原样返回 `kick()` 的 promise。`kick()` 的同步段因此一路跑到第一个 await——通常是 `tools.resolve()`——`turn/start` 与 `claim` 已经落盘；第 4 节说的「方法返回与轮次开始之间没有间隔」就是这一行的结果。末尾的 `.then(driver.resolve, driver.reject)` 把 driver 的结束接到句柄上，而 `wakeDriver()` 自己不 await、也不返回 promise。

### 循环与句柄

```ts
private async kick(): Promise<void> {
  try {
    while (await this.turn()) {}
  } catch (_error) {
    // Reported failures and cancellation are contained at the driver boundary.
  } finally {
    if (this.phase.kind === 'running') {
      const { turn, wakeRequested } = this.phase
      this.setPhase({ kind: 'idle', lastTurn: turn })
      if (wakeRequested && this.inbox.hasPending) this.wakeDriver()
    }
  }
}
```

**启动的是 driver，不是 turn。** `turn()` 的返回值是「还要不要下一轮」：`turn/end` 之后如果 `inbox.hasPending`，它换 controller、清 `wakeRequested`、`step` 归零，返回 `true`；否则返回 `false`，while 退出。一次唤醒可以连续跑完 next-turn 排出的多轮，中途不再经过 `wakeDriver()`。

**异常在 driver 边界收住。** `catch` 吞掉 `turn()` 抛出的所有异常：可上报的在更内层已经由 `throwError()` 发过 `agent/error`，取消则只留下 `turn/end` 的 `aborted` 原因。因此 `driver.reject` 是兜底——只有 `finally` 自身再抛错（比如事件监听器）才会让句柄 reject——不是常规错误通道。

**`activityDone` 是「当前活动」的句柄。** 它初始是 `Promise.resolve()`：从未被唤醒的 agent，`whenIdle()` 立即返回。每次 `wakeDriver()` 启动 driver、每次 `runMaintenance()` 开始维护，都会替换它。它的 resolve 发生在 `kick()` 的 finally 之后；如果 finally 里兑现闩锁又起了一个 driver，旧句柄 resolve 时 `activityDone` 已经指向新活动——`whenIdle()` 的 do-while 引用比较就是为这个窗口准备的（见「`whenIdle()` 为什么要 do-while」一节）。句柄不携带结果：成功没有返回值，常规失败已经上报；真正会让 `whenIdle()` 抛错的是上面那条兜底 reject。

### 入口：三处调用，三个反例

| 调用点 | 条件 | 做什么 |
| --- | --- | --- |
| `send()`（`agent.ts:176`） | `wakeup === true` | idle 时启动；否则按第 7 节闩锁，或交给活 driver |
| `kick()` 的 finally（`agent.ts:278`） | `phase` 仍是 running，且 `wakeRequested && hasPending` | driver 收尾后兑现闩锁 |
| `runMaintenance()` 的 finally（`agent.ts:216`） | phase 已设回 idle，取消原因不是 `disposed`，且 `wakeRequested && hasPending` | 维护收尾后兑现闩锁 |

三个反例同样说明了边界：

- `inject()` 不调用 `wakeDriver()`，只写队列；idle 时它就一直等。
- `cancel()` 清队列、清闩锁（`keepInbox` 时跳过），然后 abort；它不启动任何东西，收敛由被 abort 的 driver 在 finally 里完成。
- disposal 走 `cancel({ kind: 'disposed' })`，再 `await whenIdle()`（`agent-loop/src/index.ts:517`）。`wakeDriver()` 对 disposed 原因不闩锁，注释写的是「disposal never latches, so teardown waits on no model turn」——拆掉一个 agent 时，不能再替它开一轮。

**唤醒不做什么。** 它不扫描队列，不知道队列里有哪些消息，也不挑选；调用方在兑现闩锁前查一次 `hasPending`，真正决定「要不要多走一个 step 或 turn」的是循环里的 claim 与 `turn()` 的尾部。它不等待、不重试：启动即返回，没有「唤醒失败后再试」的分支，可恢复性来自闩锁的下一次兑现。它也不创建第二个 driver：`wakeDriver()` 只在 idle 时启动，其他情况要么交给活 driver，要么闩锁。最后，它不保证消息被消费——这是第 3 节就划清的承诺边界：唤醒打开的是边界，不是消息的入场券。

**自己搭时**：把「启动」与「执行」拆成两个函数。启动函数只做所有权转移——判断、换句柄、迁移状态、同步拉起执行体；执行体是一个独立的 async 循环，自己决定何时结束，并在唯一的收尾点兑现待办。别让启动函数 await 执行体，也别让执行体在结束时递归调用启动函数而不经过状态检查。

## 9. 为什么 `AgentStatus` 只有两个值

公开的 status 只有 `idle` 与 `running`（`agent.ts:156`）：

```ts
get status(): AgentStatus {
  return this.phase.kind === 'idle' || this.phase.kind === 'maintenance' ? 'idle' : 'running'
}
```

maintenance 被折叠进 idle。这不是偷懒，是一个刻意的不透明化：维护任务不是轮次，不该让消费者以为「模型在工作」；但它又确实占着 agent。对外说 idle，对内的 `whenIdle()` 仍然会等它——两个问题用两个接口回答。

disposal 不是第三个状态。`dispose()` 做的是把 agent 从注册表移除、发 `agent/disposed`、注销作用域，它不是 phase 的一次迁移。`docs/subsystems/core.zh.md:150` 明确说：dispose 移除 agent 并发事件，不是终态 status。原因有三层。

其一，status 是给「还能不能接收输入」这个问题用的。disposed 之后 `send()` 仍然可调用（对象还在），但没有任何 driver 会来领；这时候报一个 `disposed` 状态，反而暗示存在某种可恢复的终态。

其二，status 的变化由 `setPhase()` 统一发布，而 `setPhase()` 只处理 phase。disposal 发生在 driver 静默之后、session detach 之前，跟 phase 没有关系；把它塞进 phase 会让 `wakeDriver` 的三分支多出一个永远不该进入的分支。

其三，`agent/status` 是实时协调事件，不是持久事实。disposal 有自己的一次性事件 `agent/disposed`，消费者不需要在 status 流里再识别一次。

**自己搭时**：状态机只放「会被反复进出」的状态。一次性、不可逆的生命周期终点用独立事件表达，别塞进 status 枚举；否则每个 switch 都要处理一个永远不会再变的 case。

## 10. `whenIdle()` 为什么要 do-while 比较 `activityDone`

```ts
async whenIdle(): Promise<void> {
  let activity: Promise<void>
  do {
    await (activity = this.activityDone)
  } while (activity !== this.activityDone)
}
```

第 8 节说过，`activityDone` 会随着每次 driver 或维护被替换；`whenIdle()` 要等的正是「当前活动的句柄」，所以它不能只 await 一次。

只 await 一次会漏掉什么？设想 driver A 正在收尾。它的 `kick()` finally 先 `setPhase(idle)`，发现闩锁为真且有 pending，于是调用 `wakeDriver()` 启动 driver B，`activityDone` 被替换成 B 的 promise；然后 A 的 promise resolve。此时 `await` 返回，但 B 正在跑。单次 await 会让 `whenIdle()` 在 B 刚开始时返回，调用方以为「已经静默」，实际上模型正在生成。

do-while 的比较是引用相等：如果 await 返回后 `activityDone` 已经不是刚才那个 promise，说明在等待期间有替换工作被启动，必须再等一轮。它不关心 promise 的 resolve 值，也不关心 phase——只关心「我等的那个活动，是不是还是当前活动」。

这个写法还有一个边界：如果替换发生得非常快（A 收尾时启动 B，B 又在收尾时启动 C），循环会继续跟着走到 C。它不是「等两个」，而是「等到引用稳定」。

不能用轮询 `status === 'idle'` 代替：maintenance 期间 status 就是 idle，但活动没结束；也不能用计数器，因为计数器无法表达「我等的那个具体活动」——A 结束与 B 开始可能发生在同一个微任务里。

### 它会等过头吗

会，而且这是设计。`whenIdle()` 等的是「整个 agent 静默」，不是「我关心的那条消息结算」。如果一条 steer 在等待期间又触发了三个 step，它会把这三个 step 都等完；如果 driver 在收尾时启动了替换工作，它也会跟过去。

`docs/subsystems/core.zh.md:150` 因此提醒：只有调用方明确拥有「回执到空闲」这个区间时，才能把 `whenIdle()` 当成一次 run 的结束。UI 上「停止按钮变灰」适合用它；「这条消息处理完了吗」不适合，后者要跟踪 `MessageId` 的 `claimed` 与随后的 `user/message`。

**自己搭时**：实现 `whenIdle()` 时，等待的对象必须是「当前活动的句柄」，而不是「空闲这个布尔值」。句柄会被替换，布尔值会骗你。

## 11. 设计取舍

| 取舍 | 选了什么 | 放弃了什么 | 什么时候该反悔 |
| --- | --- | --- | --- |
| 输入的持久化时机 | 先 append 再唤醒，同步提交 | 每次输入一次日志写 | 输入量极大且允许丢失时，可改为批量提交，但要接受崩溃丢消息 |
| 队列数量 | 两条固定队列 | 任意优先级 / 多级队列 | 需要「先处理系统通知再处理用户」时，得在 claim 里加排序，而不是加第三条队列 |
| 投递 API | 两个参数（目标 + 唤醒） | 每种语义一个专用方法 | 语义继续增殖（比如「必须在本轮内回答」）时，应引入新的 target 而不是布尔组合 |
| claim 的通知 | claimed 与 discarded 分离 | 一次「移除」事件 | 消费者只需要「还在不在」时，两套通知会显得啰嗦，但别合并 |
| status 取值 | 两值，maintenance 归 idle | 一个能区分维护的公开状态 | 需要给用户展示「正在整理上下文」时，应加独立事件而不是扩枚举 |
| 唤醒收敛 | 闩锁 + 唯一收尾点兑现 | 立即重试的简单写法 | 收尾点变多时，闩锁会散落；此时应把兑现收敛到一个 `converge()` |

## 12. 踩坑点

**1. 以为 `followup()` 返回 void 就等于「只是排了个队」。** 现象：调用返回后读 `agent.status` 已经是 `running`，日志里已有 `turn/start` 与 `claimed`，但模型请求还没发出去。原因：`wakeDriver()` 同步调用 `kick()`，`kick()` 同步跑到 `turn/start` 与 `claim`，再在 `tools.resolve` 处挂起。对策：别把「方法返回」当「还没开始」的信号；要等处理完就 `whenIdle()` 或跟踪事件流，不要读一次 status 就下结论。

**2. 在 `agent/inbox/inserted` 监听器里调用 `cancel()`。** 现象：消息落在 next-step，却收到了 next-turn 的唤醒，日志里两个事实对不上。原因：`send()` 在插入前捕获分类，插入时的重入 cancel 不会改变已经算好的目标；但如果你在监听器里再 `send()` 一条，那条会按新状态重新分类。对策：监听器里只做观察，不要在插入通知里改输入平面。

**3. 用 `inject()` 送一条「必须被看到」的消息。** 现象：消息一直 pending，直到下一次 followup 才出现。原因：`inject` 不唤醒，idle 时没有任何东西会来 claim。对策：需要被看到就用 `steer`；`inject` 只用于「顺路带上」的上下文。

**4. 在工具体内 `inject()`，以为会插到当前 step。** 现象：上下文出现在下一次请求，而不是刚结束的这次。原因：当前 step 的批次已经在 pre-step 被 claim 完了，`inject` 只能进 next-step 等下一个边界。对策：如果确实要影响当前请求，应该在 pre-step 监听器里改 `messages`，而不是在工具里 inject。

**5. 认为 `claim` 之后消息一定进入模型。** 现象：日志里有 `claimed`，但没有对应的 `user/message`。原因：pre-step 可能 reject（`turn/end` 记 `blocked`），或者 step 在准入前被 abort。claimed 只表示「离开了队列」，不表示「进入了请求」。对策：要确认消息被看见，盯 `user/message`，不要盯 `claimed`。

**6. 用 `clear()` 做「撤销上一条」。** 现象：撤销了一条，顺带清掉了所有排队输入。原因：`clear()` 先清 next-step 再清 next-turn，语义是「全部取消」，不是「撤回一条」。对策：单条撤回用 `remove(messageId)`，它会发 `discarded`。

**7. 在 maintenance 里发 `followup()`，然后等它执行。** 现象：消息入队了，但直到维护任务结束才开轮。原因：`wakeDriver` 在 maintenance 期间只闩锁，兑现点在 `runMaintenance` 的 finally。对策：需要维护期间就处理输入，应该把工作拆成 turn 内的工具调用，而不是 maintenance。

## 13. 源码索引

- `packages/core/agent-loop/src/agent.ts:53` — `Phase` 三态；`aborted` 是 controller 的状态，不是 phase。
- `packages/core/agent-loop/src/agent.ts:116` — `activityDone` 的初始句柄；每次启动 driver 或维护都会替换。
- `packages/core/agent-loop/src/agent.ts:156` — status 只有 idle/running，maintenance 折叠进 idle。
- `packages/core/agent-loop/src/agent.ts:161` — `setPhase` 只在状态真的变化时发 `agent/status`。
- `packages/core/agent-loop/src/agent.ts:170` — `send()` 的四步顺序，含 `wakingAfterAbort` 的插入前捕获。
- `packages/core/agent-loop/src/agent.ts:179` — 三种投递只是两个参数的组合。
- `packages/core/agent-loop/src/agent.ts:191` — `cancel()` 清队列、清闩锁、abort 的顺序。
- `packages/core/agent-loop/src/agent.ts:230` — `wakeDriver` 的 idle/maintenance/aborted 三分支与 disposed 例外。
- `packages/core/agent-loop/src/agent.ts:241` — idle 分支的四条启动语句：句柄、phase、同步 `kick()`。
- `packages/core/agent-loop/src/agent.ts:250` — `withInitiator` 同步启动 `kick()`，并把完成信号接到句柄。
- `packages/core/agent-loop/src/agent.ts:253` — `whenIdle()` 的 do-while 引用比较。
- `packages/core/agent-loop/src/agent.ts:268` — `kick()` 的 driver 循环、异常收束与 finally 里的闩锁兑现。
- `packages/core/agent-loop/src/agent.ts:283` — `preStep()` 先 claim 再走 waterfall。
- `packages/core/agent-loop/src/agent.ts:327` — `turn()` 的空批次分支与换 controller。
- `packages/core/agent-loop/src/inbox.ts:97` — `clear()` 先 next-step 后 next-turn。
- `packages/core/agent-loop/src/inbox.ts:108` — `claim()` 纯删除 splice、顺序、逐条 claimed。
- `packages/core/agent-loop/src/inbox.ts:186` — `mutate()` 的坐标截断、跨列表查重、先提交后通知。
- `packages/core/agent-loop/src/index.ts:517` — disposal 先 `cancel({ kind: 'disposed' })`，再 `await whenIdle()`。
- `packages/core/agent/src/index.ts:346` — `withInitiator` 的同步语义与 initiator 归属。
- `packages/core/agent/src/types.ts` — `InboxTarget`、`InboxState`、`agent/inbox/spliced` 的 wire 形态。
- `docs/subsystems/core.zh.md:150` — running 覆盖整个 driver 排空区间；dispose 不是 status。
- `docs/subsystems/core.zh.md:283` — 纯删除 splice 与 claimed 的语义，以及 `discarded` 的适用面。
- `docs/subsystems/core.zh.md:349` — `CancelOptions` 与 `AgentCancelCause`。

回到开头那个问题：为什么 `followup()` 不返回句柄？因为在这个模型里，输入的价值不在于「函数调用成功」，而在于「它在日志里留下了哪条事实」。一条消息会依次留下 spliced、inserted、claimed，可能还有 discarded，最后以 `user/message` 进入请求。任何一步都可能分叉，任何一步都可以从日志重建。

这也是自建 Agent 时最值得抄的一点：把输入做成事件，而不是调用。调用只有成功与失败；事件有位置、有顺序、有可以被别人观察的中间态。你需要的所有「插话」「排队」「取消」「重放」，都长在这些中间态上。

上一篇：[模型看见的，必须被记下：会话日志与事件溯源](/posts/dsh-session-event-sourcing.html) ｜ 下一篇：[插话、注入与急停：控制一个正在跑的 Agent](/posts/dsh-steering-and-cancel.html) ｜ [系列目录](/posts/dsh-architecture-anatomy.html)
