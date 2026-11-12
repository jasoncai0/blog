---
title: "插话、注入与急停：控制一个正在跑的 Agent"
date: "2026-11-11T10:00:00+08:00"
summary: "把 steer、inject、cancel 拆成一次 inbox 写入、一次 pre-step 决策与一条 signal 的死亡链：为什么插话要等一个 step 边界、注入可能永远躺着、急停会连带清空队列，以及 abort 之后唤醒为什么需要闩锁。"
description: "DeepSeek Harness 的控制面笔记：steering 在下一个 step 边界改变输入，cancel 让当前 activity 的 signal 死掉并清队列；两者共用 pre-step 通道，语义却完全不同。"
tags: ["harness", "concurrency", "architecture"]
slug: "dsh-steering-and-cancel"
---

> 源码版本：0.2.2-rc.1 commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。行号以该 commit 为准。

> 本系列第 4 篇。系列目录：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)。

一个 agent 正在跑一条十二秒的 bash。用户在输入框里补了一句「等等，先看下 package.json」，回车。界面上此刻有三个动作可选：插话（steer）、塞上下文（inject）、停止（cancel）。

三个按钮都长成「打断」的样子，行为却分属三套机制。插话不会立刻生效，它要等模型把手头这一步走完；塞上下文可能一直躺在队列里，直到下一次有人唤醒 agent；停止会把前两者一起清掉，还会顺手取消一个已经排好的唤醒。更麻烦的是停止之后再发一句：有时立刻开新轮，有时要等一个正在退出的循环收尾。

这篇把这三件事拆到同一层：它们都只是对 inbox 的一次写入，或者对一条 `AbortSignal` 的一次 abort。看清这一层，上面那些「有时」就都能解释。

## 0. 结论

steering 改的是下一个 step 的输入，cancel 撤销的是当前 activity 的授权。两者都经过 pre-step 这道门：一个提交数据，一个让 signal 死掉。

## 1. 三种投递，一条时间轴

先看实现。三个公开方法加起来三行（`agent.ts:179-189`）：

```ts
followup(input: UserMessage): void {
  this.send(input, 'next-turn', true)
}
steer(input: UserMessage): void {
  this.send(input, 'next-step', true)
}
inject(input: UserMessage): void {
  this.send(input, 'next-step', false)
}
```

`send` 把「投递」压成两个参数：目标队列，以及是否唤醒。差别全在这两个参数上，跟消息内容无关。

| 投递 | 目标队列 | 唤醒 | 被看见的时机 | idle 时 | cancel 时 |
|---|---|---|---|---|---|
| `followup` | next-turn | 是 | 下一个 turn 边界 | 立刻开新轮 | 默认被清掉 |
| `steer` | next-step | 是 | 下一个 step 边界 | 立刻开新轮，成为第一步的批次 | 默认被清掉 |
| `inject` | next-step | 否 | 下一个 step 边界 | 一直 pending | 默认被清掉 |

这张表里最容易被忽略的是第一行与第二行的区别。`followup` 等到的是 turn 边界，也就是当前这一轮彻底结束、`turn/end` 已经落盘之后；`steer` 等到的是 step 边界，当前这一轮还没结束，只是模型交出了控制权（没有更多工具调用，或者工具批次已经跑完）。

`followup` 的 JSDoc 把这层意思写得很硬：入队的消息会成为「它自己那一轮的唯一普通消息」。一条 followup 不会挤进当前轮，它拥有下一轮的第一步。

`steer` 则相反。它的 JSDoc 说：idle 时启动一轮，running 时在下一个 step 边界被消费。注意「消费」这个词，它承诺的是被这个 step 的批次领走，不承诺被模型看见。如果 pre-step 把这个批次 reject 了，消息的旅程会在 claim 处结束（后面 §2 详谈）。

拿一次具体的运行举例。第 3 步正在跑一个 `bash`，预计 12 秒。此刻依次发生三件事：

1. 用户按「插话」，`steer("先看 package.json")`。消息进 next-step，driver 已经在跑，`wakeDriver` 走 live 分支，什么都不做。
2. 工具在 12 秒后返回，batch 结算，`step/end` 写入，`turn()` 发现 next-step 非空，把 `target` 切成 `next-step`，进入第 4 步的 pre-step。
3. claim 把这条消息连同工具追加的上下文一起领走，`step/start` 之后，它出现在第 4 步请求的 user 消息里。

如果用户按的是「追问」，同样的 12 秒里，消息进的是 next-turn；第 4 步不会看到它，要等这一轮彻底结束、`turn/end` 落盘之后，下一轮的第一步才把它领走。

所以这两个方法的差别落在边界上：「这一轮内」与「下一轮」。快慢只是表面现象。把它们做成同一个 API 的不同参数，调用方就必须知道当前轮的边界在哪；做成两个命名，至少语义是显式的。

`inject` 与 `steer` 的差别更细：两者都进 next-step，都等到同一个边界，唯一区别是提交瞬间要不要唤醒。idle 时这个区别被放大：steer 立刻开一轮，inject 只留一条事件。running 时两者几乎一样，除了 inject 不会在轮次收尾时把 agent 从 idle 里叫醒。

这三个方法都返回 `void`，没有回执，也没有「已送达下一步」的 future。原因和 03 篇里 followup 不返回句柄是同一个：消息的 id 标识的是持久事实（插入、认领、丢弃），跟最终结果无关。想等结果，等的是轮次或整个 agent 的空闲，不是这一条消息。控制面因此是「发射后不管」的，调用方要自己决定在哪个边界上对账。


{{< fig "diagrams/01-three-deliveries.svg" "同一时间轴上的三种投递：followup 等到下一个 turn 边界，steer 等到下一个 step 边界，inject 不唤醒、可能无限期等待。三者的差别只有两个参数：目标队列与是否 wakeDriver。" >}}

自己搭时：不要把「发消息」做成三个语义模糊的 API（send / interrupt / notify）。把它建模成一次带两个字段的入队：目标边界（turn 还是 step）与是否唤醒。这三种投递是同一原语的三个预设，不是三套代码路径。

## 2. pre-step 是唯一入口

steering 之所以「要等」，是因为模型请求一旦组装就被冻结了。`buildRequest` 在末尾 `deepFreeze` 整个请求（`agent.ts:727` 附近），此后任何对消息数组的修改都不会影响已经发出的调用。想让模型看到新输入，唯一的办法是让它进入下一个请求。

下一个请求的入口是 `preStep`（`agent.ts:283-317`）。它的第一件事是 claim：

```ts
const claimed = this.inbox.claim(target, position.turn)
```

claim 是同步的，发生在任何 `await` 之前。领到的批次随后进入 `agent/pre-step` waterfall，监听器可以返回 `enter`（可改写 messages）或 `reject`。默认实现把 claimed 与运行时上下文拼在一起：

```ts
{ kind: 'enter', messages: context === undefined ? claimed : [...claimed, context] }
```

这里有两个反直觉的后果。

第一，claim 先于决策。消息在 pre-step 还没表态时就已经离开队列了。所以 reject 不会把批次放回去。测试 `interception.spec.ts:338` 的用例名说得很直白：`preserves input staged after the blocked batch was claimed`。被保住的是「claim 之后才 staged 的输入」，被 reject 的那批不在其中。

claim 之后还有两个 `await`：读模型选择（同步），然后 `tools.resolve()`，再 `systemPrompt.assemble()`。在这段窗口里到达的 steer 已经赶不上本批次，只能等下一个 step。所以「在 pre-step 边界插入」的截止点比直觉更早：在 claim 那一行，不在请求发出之前。


第二，`steer()` 在 pre-step 监听器里调用自己，是合法的，但不会影响当前决策。当前批次已经 claim 完了，新塞的这条进入下一次 claim。想影响当前请求，只能改 `messages` 并返回新的决策。

reject 之后，轮次并不立刻结束。`preStep` 把 decision 交回 `turn()`，后者写 `turnEnds = { kind: 'blocked' }` 并 `return false`；finally 里落一条 `turn/end`，reason 是 blocked。整条路径不长：

```ts
const decision = await this.preStep(target, { turn, step })
if (decision.kind === 'reject') {
  turnEnds = { kind: 'blocked' }
  return false
}
```

注意 `preStep` 在 reject 分支里显式 `toolSet.dispose()`，`enter` 分支的 toolSet 则由 `turn()` 的 finally 释放。每次 proposed step 都会解析一次工具集，无论这个 step 最后有没有发请求。这是「提案」与「执行」分离的代价：多一次解析，换来决策可以发生在任何副作用之前。

还有一个容易忽略的细节：`agent/inbox/claimed` 在 reject 之前就已经发出去了。跟踪消息的消费者会看到「已认领」，然后永远等不到对应的 `user/message`。claimed 只表示消息离开了队列，不表示它被模型看见，这是契约。要判断「这句输入到底有没有生效」，得看后续有没有 `user/message`。

文档在这里有一道缝。`runtime-types.ts` 的 steer JSDoc 写「被 reject 的 step 让 steering 留在 inbox 直到下一次 wake」。按实现读，这句话只对「reject 决策期间新 staged 的 steering」成立；被 claim 的那批不会回滚。两种读法差一条消息的归属，而这条消息可能正是用户刚发的那句。写集成代码时，别把「reject 会回滚」当契约。

自己搭时：把「改变下一步输入」收敛到唯一一个 pre-step 钩子，并且让这个钩子的返回值成为权威。不要在生成过程中开放「追加消息」的后门，那会逼你处理「请求已冻结」与「消息已可见」两个事实不一致的状态。

## 3. inject：不唤醒的代价

`inject` 是三者里最像「通知」的一个。它把消息放进 next-step，但不调用 `wakeDriver`。idle 时只会出现一条 `agent/inbox/spliced` 事件，没有 driver，没有 `turn/start`；队列原地等待，直到某个 followup 或 steer 把轮次打开。

running 时，它会在最近的下一个 step 边界被 claim。但「最近」的边界可能已经过去了：如果 driver 已经 claim 完这一批、正在组装请求，inject 的消息只能等再下一个 step。如果这一轮没有下一个 step（模型不再调工具），它就顺延到下一轮。

这个设计是刻意的，理由有两个。成本上，inject 的典型生产者是文件监视器、hook 通知、工具追加的上下文，没有人在等回复，不该为它们花一次模型调用。语义上，「上下文」与「指令」是两类东西：前者可以顺路带上，后者必须有人负责。

代价是它可能永远不被看见。idle 的 agent 如果没有后续输入，inject 的消息会一直 pending。JSDoc 把这句写进了契约：「可能错过 pre-step 已 claim 的批次；取消或 disposal 可能丢弃。」

一次可复现的观察：给 idle 的 agent 连发两条 inject，session 里出现两条 `agent/inbox/spliced` 与两条 `agent/inbox/inserted`，然后没有 `agent/status`、没有 `turn/start`。直到一条 followup 到达，driver 才启动，第一次 claim 把三条消息一起领走。如果这个 agent 是子 agent，而父级已经结束，这两条 context 就永远留在那里，不报错，也不会被回收。


工具追加的上下文走的是同一条路。`step()` 在工具批次结束后，把 `additionalContexts` 逐条 splice 进 next-step（`agent.ts:594` 附近），然后返回 `null` 表示「还有下一步要跑」。工具产生的上下文天然属于「顺路带上」，不额外唤醒。

自己搭时：给不唤醒的投递加一个可见性提示，别让它静默过期。至少让调用方知道「这条消息现在是 pending，没有 driver 会来领它」。如果一条消息的业务语义是「必须被看到」，它就不该走 inject。

## 4. cancel 的因果链

`cancel` 只有九行（`agent.ts:191-197`），但它是整个控制面里最容易写错的一段：

```ts
cancel(cause: AgentCancelCause, options: CancelOptions = {}): void {
  if (!options.keepInbox) {
    this.inbox.clear()
    if (this.phase.kind !== 'idle') this.phase.wakeRequested = false
  }
  if (this.phase.kind !== 'idle') this.phase.abort.abort(cause)
}
```

三步，顺序固定。

第一步清队列。`clear()` 先清 next-step 再清 next-turn（`inbox.ts:97`），每条被删的消息都发 `discarded`，splice 事件带 `outcome: 'canceled'`。日志里留下了一条可回放的取消记录，不是悄悄扔掉。

第二步丢掉闩锁。如果此刻有一个已经排好的唤醒（`wakeRequested === true`），取消会把它清掉：用户说了停，就不该在活动收敛时又冒出一个新轮。

第三步 abort。`AbortController.abort(cause)` 把 cause 对象写进 `signal.reason`，此后所有监听这条 signal 的代码都会看到同一个对象。

「第一因获胜」（first cause wins）是 `AbortController` 的语义：第二次 `abort()` 不会覆盖第一次的 reason。所以两个并发的取消源（用户按停止、父 agent 撤销、hook 拒绝）谁先到，`turn/end` 里记录的就是谁。后到的那次只剩「队列已经被清过」这一件事可做。

cause 进入持久日志之前会被拷贝一次。`abortedCancelCause()`（`agent.ts:94-110`）按 kind 重建对象，只保留 `turn/end` 需要记录的字段。原因写在注释里：活着的 reason 是调用方的对象，Node 的 fetch 会往上挂一个 `stack`，而 `Session.append` 要么把它当数据记下来，要么因为 JSON 装不下而拒绝。

{{< fig "diagrams/02-cancel-causal-chain.svg" "cancel 的因果链：一次同步的 abort 把 cause 写进 signal.reason，之后所有边界检查都抛同一个对象；turn/end 把 cause 的字段拷贝进持久日志。第二次 cancel 不会改变第一因。" >}}

abort 之后，signal 在各处生效：

- turn 循环在每一步开头 `throwIfAborted()`（`agent.ts:345`）；
- step 在请求前、流循环里、工具批次前各查一次（`agent.ts:439`、`491`、`495`、`498`）；
- 工具执行把 signal 传给工具体，长命令自己决定怎么响应；
- 审批请求把 signal 传进 `approval.request()`，abort 后立刻以 `cancelled` 结算，迟到的答案被丢弃。

这些检查抛出的都是同一个 cause 对象。turn 的 catch 拿到它，写 `turnEnds = { kind: 'aborted', reason: cause }`，再原样抛出；finally 里 `turn/end` 落盘。

### 4.1 取消不写 attempt，但可能写 assistant/message

被取消的请求不一定什么都没有留下。`step()` 的 catch 里有一条分支（`agent.ts:505-520`）：如果流已经启动、signal 已 abort、而且已经收到了非空的块，它会写一条 `assistant/message`，带 `interrupted: true`，内容是已经收到的那些块。否则写 `assistant/attempt`，一条只记录「这次尝试失败了」的日志，不进模型可见历史。

两条日志的用途不同。`assistant/attempt` 是诊断记录；`assistant/message` 是历史。用户看到的半句话如果已经流进了界面，下一轮模型就该知道它说过；如果什么都没收到，历史里就不该出现一条空的助手消息。

工具调用被取消时还有第三种情况：`tool/call` 已经记了，`tool/result` 还没记。进程如果此时崩溃，恢复时 `repair.ts` 会补一条合成的错误结果，code 是 `TOOL_OUTCOME_UNKNOWN`，正文告诉模型「这次调用被中断，结果未知，决定是否重试」。`openTurnClosers()` 负责把未闭合的 turn 补上 `step/end` 与 `turn/end`。

### 4.2 取消怎么到达审批

审批是整条链上最需要取消的环节，因为它可能无限期等待一个人。这条路径值得单独走一遍。

工具走到需要审批时，`pipeline.ts` 调 `approval.request()`，并把 `exec.signal` 一起传进去（`pipeline.ts:449-455`）。`request()` 先 append `approval/asked`，再 `decide()`。`decide()` 的第一行就是检查已经 abort 的 signal：

```ts
const signal = req.signal
if (signal?.aborted) return 'cancelled'
```

所以在等待开始之前，取消就已经能赢。等待开始之后，`decide()` 用 `Promise.race` 的形态把 signal 与应答者放在一起：监听 abort 的分支先 resolve 成 `cancelled`，应答者的 promise 之后再 resolve 也只是落在已经结算的 promise 上。迟到的答案按构造被丢弃。

客户端一侧同样监听这个 signal。`PendingApproval` 在构造时挂 `request.signal` 的 abort 监听，触发时调用自己的 `abort(reason)`，卡片随之进入 settled。`finish()` 会拒绝第二次结算，抛 `pending approval ... is already settled`。两张网各自独立，任意一张先到都能结束等待。

回到 pipeline。`serviceAsk` 把四种结果翻译成工具决策（`pipeline.ts:457-470`）：`allowed-once` 放行；`rejected` 拒绝并给出「用户拒绝了这个工具」；`cancelled` 也拒绝，但额外带一个 `approvalCancelled: true`；`unavailable` 拒绝并说明没有审批通道。然后 prepare 里还有一道检查（`pipeline.ts:207`）：

```ts
if (this.callerCancelled(exec) && askResolution.approvalCancelled) {
  return { kind: 'post-result', result: toolAbortedBeforeDispatchResult() }
}
```

这道检查区分了两种「没批」：用户明确拒绝，和调用方自己被取消了。前者应该作为一条拒绝理由进入模型上下文；后者应该作为「这次执行被中止」处理，不该让模型以为用户看过并说了不。同一个审批结果，在两种因果下要落成两种事实。

### 4.3 一次取消在日志里留下什么

把一次典型的取消展开，事件序列大致是这样：

1. `agent/inbox/spliced`（每条被清掉的消息一条，`outcome: 'canceled'`）；
2. `agent/inbox/discarded`（逐条）；
3. 可能一条 `assistant/message`，带 `interrupted: true`，或者一条 `assistant/attempt`；
4. `tool/result`（如果 step 的 catch 走了恢复路径，把 pending 的工具结果补记）；
5. `step/end`；
6. `turn/end`，reason 是 `{ kind: 'aborted', reason }`；
7. `agent/status`，`idle`。

第 3 步的「可能」取决于流有没有启动、有没有收到非空块。第 4 步的「如果」取决于工具批次有没有未结算的调用。这两处不确定性来自「已经发生的事实必须可回放」：取消不能让已经写下的东西消失，只能决定它算不算模型历史。

自己搭时：取消要落成终态，不要落成「取消标记」。写清楚三件事：哪一轮、为什么、已经产生的部分输出算不算历史。前者让回放能重建，后者让模型不会在下一轮重复或否认自己说过的话。

## 5. turn-stopping：最后一个安全点

`agent/turn-stopping` 只在一种情况下运行（`agent.ts:394-398`）：这一步已经结束，`turnEnds` 有值，并且 next-step 已经空了。它是一个 serial waterfall，payload 是 `{ turn, signal }`。

```ts
if (turnEnds && this.inbox.nextStep.length === 0) {
  await this.dispatch.serial('agent/turn-stopping', { turn, signal })
  signal.throwIfAborted()
}
if (turnEnds && this.inbox.nextStep.length === 0) break
```

注意它跑完之后又查了一次 next-step。监听器如果反对收尾，正确做法是 `agent.steer(...)`，机器会重读 inbox：有新的 steering 就再跑一步，没有才关轮。文档把这个设计说成「data decides」：监听器的顺序不能改变结果，结果由它提交的数据决定。

它是最后一个安全点，再往后就是 `turn/end`。一旦轮次关闭，steering 仍然可以写进 next-step，但那属于下一轮，不再属于「这一轮还没说完的话」。想在收尾前补一句，只有这个窗口。

轮次最终以什么理由关闭，只有五种活着的可能：

| reason | 触发点 | 模型看到了什么 |
|---|---|---|
| `completed` | 无工具调用，或 `concludesTurn`，或首批为空 | 正常的助手消息 |
| `blocked` | pre-step 返回 reject | 没有新请求；claimed 批次被丢弃 |
| `max-tokens` | 某一步到达输出上限 | 可能被续写；reason 具有粘性 |
| `aborted` | signal 被 abort | 可能是 interrupted 的半条消息 |
| `error` | LLM 失败、工具激活失败等 | 结构化 failure，不是字符串 |

`max-tokens` 有一个粘性规则（`agent.ts:373`）：一旦某一步以它结束，后续步骤即使正常完成，`turnEnds` 也不会被覆盖。原因很实际：一轮里只要撞过一次上限，整轮就应该被标记为「被截断过」，否则 compaction 或续写逻辑会误判这一轮的完整性。

`interrupted` 与 `forked` 也在这个联合类型里，但循环从不实时发出它们。前者由恢复路径补写，后者由 fork 种子构造补写。类型上它们是「轮次结束原因」，语义上它们是「事后闭合」：同一个字段承载两类事实，为的是让所有消费者只需要处理一个联合类型。

`turn-stopping` 本身没有 `next()`。它是 serial，不能像 waterfall 那样改写决策；监听器只能提交数据（steer）或观察。收尾的语义因此是单向的：你可以在最后一刻补一句输入，不能在最后一刻否决整轮的结论。否决权在更早的 pre-step，那里有明确的 reject 返回值。


这也是取消与收尾的竞争点。`turn-stopping` 是 `await` 的，期间可能发生 cancel；之后的 `throwIfAborted()` 会把这一轮标记为 aborted。反过来，如果 cancel 发生在 `turn/end` 之后，这一轮已经结算，取消只能清掉队列里属于下一轮的东西。

自己搭时：给「即将收尾」提供一个钩子，但只允许它提交数据，不允许它阻塞或否决。钩子一旦能长时间阻塞，取消就要等它，收尾的延迟会直接变成用户看到的「停不下来」。

## 6. abort 之后：闩锁还是直接开

回到开头：停止之后再发一句，有时立刻开新轮，有时要等。答案在 `send()` 的第一行（`agent.ts:170-177`）：

```ts
const wakingAfterAbort = wakeup && this.phase.kind !== 'idle' && this.phase.abort.signal.aborted
const resolvedTarget = wakingAfterAbort ? 'next-turn' : target
this.inbox.splice(resolvedTarget, Infinity, 0, [message])
if (wakeup) this.wakeDriver(wakingAfterAbort)
```

三个条件同时成立，才算「在已取消的活动上唤醒」：这次投递要求唤醒、phase 不是 idle、当前 signal 已经 abort。此时目标强制改成 next-turn，不能把新消息塞进一个已经死掉的 step 批次。然后 `wakeDriver(true)` 带着这个分类去决定：是闩锁，还是直接开。

已 abort 的活动不能接收唤醒，因为它正在退出。它的 driver 还在栈上，`kick` 的 finally 还没跑，`phase` 还是 running。此时如果直接新建一个 driver，就会有两个循环同时操作同一个 inbox 和同一个 turn 计数器；如果什么都不做，这条唤醒会在 finally 把 phase 切成 idle 时被忘掉。所以只剩第三条路：在旧 phase 上置一个布尔值，等收敛点来兑现。

`wakeDriver` 的分支（`agent.ts:230-251`）正好对应三种处境：

- idle：直接开。替换 `activityDone`，`setPhase(running)`，同步启动 `kick()`。
- maintenance / aborted：闩锁。`wakeRequested = true`，等对应的 finally。
- live running：什么都不做。driver 自己会在下一次 claim 时看到队列。

例外是 `disposed`：`abortedCancelCause(signal)?.kind === 'disposed'` 时连闩锁都不置。拆除过程不欠任何一次模型轮次，这一点在 §8 还会出现。

三种 cause 里，`user` 来自界面，`parent` 来自父 agent 撤销子 agent，`hook` 带一个 `reason` 字符串。它们走同一条 abort，区别只在日志里那个 kind。cause 必须是结构化的对象，回放时才能区分「用户按了停止」与「父级收回了委派」，两者的后续处理不一样。特殊的是 `disposed`，它在 `wakeDriver` 里被单独排除。


闩锁的兑现点有两个。`kick` 的 finally（`agent.ts:268-281`）在 turn 循环退出后先把 phase 切回 idle，再判断 `wakeRequested && hasPending`，满足就 `wakeDriver()`。`runMaintenance` 的 finally 做同样的事，顺序也一样：先 `setPhase(idle)`，再兑现。

顺序不能反。`wakeDriver()` 只在 phase 是 idle 时启动 driver；如果先唤醒再切 phase，它会走进 maintenance 分支，把这次唤醒闩到维护任务自己身上，而维护任务的 finally 已经跑过了，闩锁再也没有兑现点。

还有第三条路，藏在 turn 的尾部（`agent.ts:427-431`）：

```ts
if (!this.inbox.hasPending) return false
phase.abort = new AbortController()
phase.wakeRequested = false
phase.step = 0
return true
```

一轮正常结束后如果还有排队工作，不闩锁，也不新建 driver，而是换一个 `AbortController` 继续跑。新 controller 让旧 signal 上的闩锁自动失效，注释说得很清楚：live driver 自己领队列。

这条路径容易被误读成「唤醒」，其实没有 `wakeDriver` 调用，没有 `activityDone` 替换，也没有 `agent/status` 的 idle→running 转换（phase 一直是 running）。它只是同一个 driver 的下一轮循环。把「同一 driver 内的继续」与「从 idle 唤醒一个新 driver」混为一谈，会在取消逻辑里算错谁该负责清队列。


{{< fig "diagrams/03-wake-after-abort.svg" "abort 之后的两条唤醒路径：还在退出的活动只能闩锁，在收敛点 replay；已经 idle 的直接开新 driver。disposed 的取消两条都不走。" >}}

自己搭时：任何「唤醒」都要有一个明确的收敛点。往一个正在退出的循环里塞工作，最后一定会变成两种 bug 之一：工作丢了，或者两个循环同时跑。用一个布尔值加一个收敛点，比用队列加锁简单得多。

## 7. keepInbox 的决策表

`cancel` 的第二个参数是 `CancelOptions`，目前只有一个字段 `keepInbox`。它的作用是跳过前两步：不清队列，也不清闩锁。abort 仍然发生。

| 场景 | keepInbox | inbox | wakeRequested | 活动 | 之后的走向 |
|---|---|---|---|---|---|
| 用户按停止，队列里还有几句 | false（默认） | 清空，逐条 discarded | 清掉 | abort | 收敛到 idle，什么都不剩 |
| 停止当前轮，保留排队的话 | true | 原样 | 原样 | abort | 收敛到 idle；等下一次外部唤醒 |
| 已经 idle 时取消 | 任意 | 按参数 | 不适用 | 无 | 只影响队列，不武装后续工作 |
| 初始化失败，禁止放行 | true | 保留 | 不适用 | abort（disposed） | 队列留给失败路径处理 |

第二行值得展开。`keepInbox: true` 之后，活动仍然 abort，`kick` 的 finally 仍然把 phase 切回 idle；但 `wakeRequested` 没有被清，也没有被置。如果取消之前正好有一个闩锁，它会在收敛点兑现，把保留下来的队列带进新一轮；如果没有闩锁，队列就静静躺着，直到下一次 followup 或 steer。

所以「保留队列」只是保证消息还在，不等于「马上继续」。要不要继续，取决于有没有人再唤醒一次。

{{< fig "diagrams/04-keep-inbox-table.svg" "图注：keepInbox 的四种组合。保留队列只跳过 clear 与清闩锁两步，活动仍然 abort；没有既有闩锁时，队列会一直停到下一次外部唤醒。" >}}

两个具体场景能看出这个区别。

场景一：agent 在跑一条长命令，用户先打了一句「跑完顺便看下测试」，又按了停止。默认参数下，这句话随 `clear()` 一起被丢弃，日志里留下 canceled 的 spliced 与 discarded。用户看到的是「停止 = 连没做的事也不做了」。

场景二：同一个时刻，客户端传 `keepInbox: true`。活动仍然被 abort，命令被终止，但那句话留在 next-turn。`kick` 收敛后 `wakeRequested` 是 false，所以不会自动开新轮；直到用户再发一句，driver 被唤醒，下一轮 claim 才会把保留的那句话领走。如果中途有一个更早的闩锁（比如维护任务里排过一次唤醒），它会在收敛点兑现，结果就是「停止后立刻又跑了一轮」。这正是保留闩锁的语义，不是 bug。

所以产品上要区分两种「停止」：停当前动作，和停一切。把选择权交给调用方，比在实现里猜意图可靠。

还有一条边界：`keepInbox` 不影响已经写下的日志。清空不可逆：discarded 通知发出去之后，没有 API 能把消息按原 id 放回队列；要重发只能构造一条新消息。

自己搭时：取消 API 至少要有两个档位：停掉一切，和停掉当前活动但保留排队工作。默认选前者，用户按「停止」时，通常不希望下一句悄悄开始执行。

## 8. 取消与 disposal 的区别

disposal 在实现上是「一次 disposed 原因的 cancel，加一次静默等待」。`index.ts:509-511` 的注释写得很直接：Disposal IS a disposed-cause cancel followed by quiescence。

它和用户取消有三处不同。

第一，cause 是 `disposed`。这个 kind 让 `wakeDriver` 拒绝闩锁：拆除过程中到达的唤醒不会被 replay，因为 agent 马上要从注册表里消失，再开一轮没有意义。

第二，保留策略相反。正常拆除时 `keepInbox: !ready`：已经就绪的 agent 会清空队列；初始化失败、还没就绪的 agent 反而保留队列，避免把排队输入放进一个从未成功启动的轮次（`index.ts:642`）。

第三，disposal 不表现为 status。`AgentStatus` 只有 idle 与 running 两个值（`agent.ts:156`）；disposal 是「从注册表移除并广播 `agent/disposed`」，不是第三个可观察状态。这也是为什么 `whenIdle()` 不能拿 status 当判据：维护期间的 status 就是 idle，但活动没结束。

还有一处实现细节值得记住：disposal 在 cancel 之后会 `await machine.whenIdle()`。拆除必须等 driver 静默，才能注销作用域、detach session。用户取消不同，它是同步返回的，调用方想等就自己等 `whenIdle()`；拆除没有选择，它必须等，否则会在驱动还在跑的时候把 session 拆掉。

这也解释了为什么 `agent/disposed` 是在 driver 静默之后、session detach 之前发出的：监听器还能看到最后一批收尾事件，但不能再往里投递。拆除的可观察点是一个事件；状态机里没有「正在拆除」这一档。想把「正在拆除」画进状态机的，通常会在画完之后发现少了一个能阻塞的钩子。

自己搭时：teardown 不要复用 cancel 的「可恢复」语义。取消是「这次不做了，下次还能来」，拆除是「不会再有了」。两者的 cause、日志与唤醒策略都该分开，否则你会在关机时看到一次多余的模型调用。

## 9. 设计取舍

| 决策 | 选择 | 被放弃的方案 | 代价与适用边界 |
|---|---|---|---|
| steering 的生效点 | 下一个 step 边界 | 生成中实时改 prompt | 请求已冻结；换来请求可回放、可缓存 |
| reject 后的批次 | 保持已移除 | 回滚到 inbox | 消息归属需要文档说清；换来不重复准入 |
| inject 唤醒 | 不唤醒 | 一律唤醒 | 可能长期 pending；换来不为通知花模型调用 |
| 取消的队列 | 默认清空 | 默认保留 | 用户可能丢话；换来「停止」真的停 |
| 唤醒的投递 | 闩锁 + 收敛点 | 立即新建 driver | 多一个布尔值；换来不会双 driver |
| status | 两值 | idle/running/disposed 三态 | 无法从 status 看出拆除；换来消费者状态机简单 |
| turn-stopping | serial + 重读 inbox | 一次性广播 | 收尾多一次 await；换来数据决定结果 |

这些取舍有一个共同点：把不确定的中间态压成可回放的事实。请求冻结、批次删除、终态原因、闩锁布尔值，都是为了让取消或收尾之后还能回答「当时发生了什么」。

另一面是：任何需要跨重启保留的控制意图都不该只活在内存里。steering 消息会落盘，cause 会落盘，只有 `wakeRequested` 是易失的，它描述的是「这个 driver 还没做完的事」，重启之后本就不该存在。

## 10. 踩坑点

1. 以为 `steer()` 会立刻让模型看到。现象：调完 steer，当前这一步的请求里没有这句输入。原因：请求在组装时已经 `deepFreeze`，steer 只能进下一个 pre-step 的批次。对策：把「已提交」与「已生效」分开汇报；需要立刻影响，只能在 pre-step 里改 messages。
2. 以为 pre-step reject 会把消息退回队列。现象：被 reject 的那条消息再也没出现。原因：claim 发生在决策之前，reject 只关闭轮次，不回插。对策：不要用「reject 一次就会重试」做重试逻辑；要重试就自己重新投递。
3. 在 idle 时用 inject 送一条必须被看到的消息。现象：消息一直 pending，界面显示「已发送」。原因：没有 driver，没人 claim。对策：必须被看到就用 steer；inject 只用于顺路上下文。
4. 取消后立刻再发，结果旧消息也消失了。现象：用户先打了两句，按停止，再发第三句，前两句没了。原因：默认 `keepInbox: false`，`clear()` 清掉了整个队列。对策：产品上把「停止」与「清空输入」分开；需要保留就传 `keepInbox: true`。
5. 在 cancel 之后马上读 status，期待 idle。现象：还是 running。原因：abort 只是让 signal 死掉，driver 还要走完 catch/finally 才 `setPhase(idle)`。对策：等 `whenIdle()`，或者监听 `agent/status`。
6. 在 turn-stopping 里做异步清理。现象：用户按停止，界面卡住。原因：这个钩子是 `await` 的，而取消要等它跑完。对策：钩子里只提交数据（steer 一条、记一个事件），清理放到轮次关闭之后。
7. 取消时正在等审批，模型却看到「用户拒绝」。现象：用户按停止，工具结果写的是拒绝理由。原因：只做了 `serviceAsk` 的映射，没做 prepare 里那道 `callerCancelled && approvalCancelled` 检查，于是 cancelled 被当成普通 deny 进入模型上下文。对策：区分「人拒绝了」与「这次执行被中止」，后者应该走 aborted-before-dispatch。
8. 以为 `keepInbox: true` 会自己续跑。现象：保留了队列，停止后什么都没发生。原因：它只跳过 clear 与闩锁重置，不设置 `wakeRequested`。对策：需要续跑就显式再唤醒一次，别指望取消顺手把下一轮开了。


## 11. 源码索引

- `packages/core/agent-loop/src/agent.ts:170` — `send()`：在插入前捕获 `wakingAfterAbort`，abort 后的唤醒强制改道 next-turn。
- `packages/core/agent-loop/src/agent.ts:179` — `followup` 一行，目标 next-turn、唤醒。
- `packages/core/agent-loop/src/agent.ts:183` — `steer` 一行，目标 next-step、唤醒。
- `packages/core/agent-loop/src/agent.ts:187` — `inject` 一行，目标 next-step、不唤醒。
- `packages/core/agent-loop/src/agent.ts:191` — `cancel`：清队列、清闩锁、abort，三步固定顺序。
- `packages/core/agent-loop/src/agent.ts:94` — `abortedCancelCause`：按 kind 拷贝 cause，避免把 fetch 的 stack 写进日志。
- `packages/core/agent-loop/src/agent.ts:230` — `wakeDriver`：idle 开、maintenance/aborted 闩锁、live 不动、disposed 不闩。
- `packages/core/agent-loop/src/agent.ts:268` — `kick` 的 finally：先 setPhase(idle)，再兑现闩锁。
- `packages/core/agent-loop/src/agent.ts:283` — `preStep`：claim 在任何 await 之前，随后才是 waterfall。
- `packages/core/agent-loop/src/agent.ts:333` — turn 开头的 `throwIfAborted()`，取消在这里变成 `turn/end`。
- `packages/core/agent-loop/src/agent.ts:394` — turn-stopping：serial，跑完重读 next-step。
- `packages/core/agent-loop/src/agent.ts:427` — turn 尾部：有排队工作就换 controller 继续，不闩锁。
- `packages/core/agent-loop/src/agent.ts:505` — 取消后的落库分支：`assistant/message` 带 `interrupted` 或 `assistant/attempt`。
- `packages/core/session/src/repair.ts` — `openTurnClosers` / `TOOL_OUTCOME_UNKNOWN`：崩溃后补合成工具结果与轮次闭合。
- `packages/core/agent-loop/src/index.ts:509` — 注释：disposal 就是 disposed-cause cancel 加静默。
- `packages/core/agent-loop/src/index.ts:642` — 初始化失败时 `keepInbox: true`，不许把队列放行成新轮。
- `packages/core/tools/src/pipeline.ts:432` — `serviceAsk`：把审批结果翻译成工具决策，cancelled 额外带 `approvalCancelled`。
- `packages/core/tools/src/pipeline.ts:207` — 取消与审批的交叉检查：调用方已取消且审批被取消时，返回 aborted-before-dispatch，而不是拒绝理由。
- `packages/interaction/user-approval/src/index.ts:293` — `decide()` 先查 `signal.aborted`，再等应答者；abort 赢下竞速后迟到答案被丢弃。

## 12. 收尾

控制一个正在跑的 agent，难的地方在停下来之后：哪些事实还算数。这篇里的每个机制都在回答这个问题：队列里的消息算不算数（cancel 清不清）、已经生成的半句话算不算数（interrupted 分支）、排好的唤醒算不算数（闩锁清不清）、拆除时欠不欠一次调用（disposed 不闩锁）。

把这些边界写清楚，用户看到的「停止」才是一次确定的状态迁移；否则它就是一次碰运气的竞态。

下一篇进入人机回环：审批、提问与命令，三条都不经过模型的通道，以及它们为什么不能互相替代。

---

上一篇：[03 输入的第一公里：两条队列、三种投递与一次唤醒](/posts/dsh-input-inbox.html)　下一篇：[05 审批与提问：人机回环的三条通道](/posts/dsh-human-in-the-loop.html)
