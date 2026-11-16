---
title: "谁串行，谁并发：一个 Agent Harness 的串行点、CAS 与顺序保证"
date: "2026-11-16T21:15:00+08:00"
summary: "四个串行点、一个滚动池和三处 CAS：从 driver 的 phase 到 inbox 的 claim，再看取消时醒来的是哪一轮。"
description: "DeepSeek Harness 的并发拆解：tool batch 的 rolling pool 与 barrier、goal revision 的乐观锁、subagent 的 slot，以及 abort/wakeRequested/wakingAfterAbort 三条竞态路径。"
tags: ["concurrency","harness","architecture"]
slug: "dsh-concurrency-and-ordering"
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46，工作区 `harness-research/deepseek-harness/`）。行号以该 commit 为准。代码片段逐字摘自源码，取消时序对照了已提交的录制会话 `snapshots/acp/cancel-tool-calls/session.v4.jsonl`。没有跑测试套件，没有连真 provider，关于调度交错的结论来自代码路径加录制事件顺序，不是压测结论。

并发问题在一个 harness 里出现得很晚。单轮单工具时，一切按顺序发生，代码读起来像脚本。等到模型一次给出五个工具调用、用户在中途插话、子 agent 同时在跑、goal 又被人改了一版，代码里那些「反正不会同时发生」的假设才开始一个个爆掉。

DSH 的答案不是加一把大锁。它把并发切成几块，每块用不同的手段：driver 用状态判别，日志用同步提交加禁重入，工具批处理用滚动池加有序提交，goal 用 revision 比较交换，子 agent 用信号量加每子一把串行锁。下面按「哪里必须串行」到「哪里可以并发」的顺序走一遍，最后回到取消和顺序这两个最容易写错的地方。

工具执行本身的九个阶段见[《工具执行流水线：九段与三闸》](/posts/dsh-tool-pipeline.html)，turn 与 step 的层级见[《一轮到底怎么结束》](/posts/dsh-turn-step-state-machine.html)。这篇只看它们之间的时序关系。

{{< fig "diagrams/01-serial-points.svg" "四个必须串行的位置与三个可以重叠的面：串行点都在状态边界上，重叠面都在无共享状态的调用之间" >}}

## 1. 先把串行点标出来

一个 agent 跑到中途，可能被四个地方挡住。这四个点的共同特征是：都守着一段共享状态，且都只允许一个执行者进入。

| 位置 | 串行单位 | 机制 | 被破坏时的症状 |
|---|---|---|---|
| driver | 每个 agent | 单一 `phase` 判别 | 两个 turn 交错，step 计数错乱 |
| inbox | 每个 agent | `claim()` 里一次 splice 加一次 append | 同一条消息被两轮各拿一次 |
| session | 每个 session | `append()` 同步提交加重入检测 | seq 撞号，投影错位 |
| tool batch | 每个 group | `commitReady` 只跨连续槽位 | 结果顺序与模型给出的不一致 |

可以重叠的面也有三个：同一个 batch 里不同工具的派发与执行、不同 agent 之间（各自独立的 session 与 driver）、工具内部的异步工作（后台 job）。这三处没有共享可变状态，重叠不需要额外协调。

下面逐个看串行点是怎么实现的。第一个点连互斥量都没有。

## 2. driver 的 phase 就是锁

`Agent` 上没有 mutex，也没有队列。它只有一个 `phase` 字段，判别式联合把「现在处于什么状态」和「允许做什么」编码在一起（`packages/core/agent-loop/src/agent.ts:53`）：

```ts
type Phase =
  | { kind: 'idle'; lastTurn: number }
  | { kind: 'maintenance'; abort: AbortController; lastTurn: number; wakeRequested: boolean }
  | { kind: 'running'; abort: AbortController; turn: number; step: number; wakeRequested: boolean }
```

`wakeDriver()` 检查 `this.phase.kind !== 'idle'` 就返回，效果等同于 `tryLock` 失败。所有状态转移都过一个方法，顺便发出对外的状态通知：

```ts
private setPhase(next: Phase): void {
  const previousStatus = this.status
  this.phase = next
  const status = this.status
  if (status !== previousStatus) {
    this.dispatch.emit('agent/status', { status })
  }
}
```

循环只有一处，`kick()` 里一行 `while`：

```ts
private async kick(): Promise<void> {
  try {
    while (await this.turn()) {}
  } catch (_error) {
    // Reported failures and cancellation are contained at the driver boundary.
  } finally {
    /* v8 ignore next -- kick owns a running phase until this driver boundary */
    if (this.phase.kind === 'running') {
      const { turn, wakeRequested } = this.phase
      this.setPhase({ kind: 'idle', lastTurn: turn })
      if (wakeRequested && this.inbox.hasPending) this.wakeDriver()
    }
  }
}
```

`turn()` 返回 `true` 表示还有下一轮要跑，返回 `false` 表示收工。判据在 `agent.ts:427` 附近：一轮结束后如果 `inbox.hasPending` 为真，就换新的 `AbortController`、把 `wakeRequested` 清零、`step` 归零，继续循环。

这里的并发保证是「同一 agent 同时只有一条 `kick` 在跑」。这里没有计数器。保证来自 `phase` 只有一个槽位：想要第二条 `kick`，必须先把 `phase` 改回 `idle`，而唯一改回 `idle` 的地方就在第一条 `kick` 的 `finally` 里。

`whenIdle()` 值得单独说。它等的是 `activityDone` 这个 promise 的稳定值，不是等某个布尔量：

```ts
async whenIdle(): Promise<void> {
  let activity: Promise<void>
  do {
    await (activity = this.activityDone)
  } while (activity !== this.activityDone)
}
```

循环条件是「等完之后承诺没变」。await 期间如果有新的 wake 开了新 `phase`，`activityDone` 会被换成新 promise，于是再等一轮。这样 `whenIdle()` 的语义是「等到真的没有后续活动」，不会在链式唤醒的间隙里提前返回。

## 3. inbox 的 claim：一次 splice，一条事件

第二个串行点在 `preStep`（`agent.ts:283`）。它做的第一件事就是 `claim`，在解析工具集、组装 prompt、跑 `agent/pre-step` 瀑布之前：

```ts
private async preStep(target: InboxTarget, position: { turn: number; step: number }): Promise<PreparedStep> {
  /* v8 ignore next -- private callers establish the running phase before proposing a step */
  if (this.phase.kind !== 'running') throw new Error(`agent "${this.id}": pre-step outside running phase`)
  const signal = this.phase.abort.signal
  const claimed = this.inbox.claim(target, position.turn)
```

顺序是刻意的：先取消息，再让任何人有机会看这批消息。`claim` 的实现（`packages/core/agent-loop/src/inbox.ts:108`）分两步：先把 `next-step` 整个清空，再从 `next-turn` 取一条。

`mutate()` 里不直接改内存数组，而是先 append 一条 `agent/inbox/spliced`。投影注册表同步折叠这条事件，所以 `Session.append()` 返回时，活的内存投影已经反映这次变更（`packages/core/agent-loop/README.md:71`）。这一步让 claim 有了回滚语义：append 抛错，内存就没动过。

`interception.spec.ts` 里有一条测试专门钉这件事：

```ts
vi.spyOn(agent.session, 'append').mockImplementationOnce(() => {
  throw new Error('append unavailable')
})
expect(() => { send(agent, 'blocked prompt') }).toThrow('append unavailable')
expect(events(agent)).toEqual([])
expect(agent.inbox.nextTurn).toEqual([])
expect(agent.inbox.nextStep).toEqual([])
expect(agent.status).toBe('idle')
```

断言了四件事：没有留下事件、两个队列都空、状态回到 idle。如果 append 先改了内存再写日志，第二条或第三条会挂。

`claim` 不需要额外的锁，因为调用它的 `preStep` 已经在 `running` 相位里，而相位本身保证了同时只有一个 `preStep`。

## 4. session append：同步提交，重入即抛

第三个串行点是 `append()`（`packages/core/session/src/index.ts:806`）。它的写法能看出设计意图：整条路径没有一个 `await`。

```ts
append<T extends SessionEventType>(
  type: T,
  data: SessionEventMap[T],
  ...opts: T extends SurfaceEventType ? [opts: SurfaceIntent<T>] : []
): SessionEvent<T> {
  this.publicationEntry()
  // …snapshot 与校验…
  const entry = this.publicationEntry()
  const event = deepFreeze({
    type,
    seq: SessionSeq(this.log.length),
    time: Date.now(),
    data: dataSnapshot,
    ...(surfaceMetadataSnapshot as { surfaceOp?: unknown; sourceEventSeqs?: unknown }),
  } as unknown as SessionEvent<T>)
  validateSessionEventData(event, `session event "${type}" at seq ${event.seq}`)
  this.commit(event as SessionEvent, entry)
  return event
}
```

三个细节值得注意。

`seq` 取的是 `this.log.length`。seq 是位置，不是自增计数器，所以不可能出现两个事件拿到同一个 seq：想拿到同一个位置，必须让数组长度在两次读之间不变，而提交会立刻加长它。

`publicationEntry()` 被调了两次。注释写明了原因：读取 payload 时，getter 可能在快照过程中把 session 从 store 里摘掉。第二次检查兜住这种情况。

`publicationEntry()` 里那三条错误，正好是三个不同的越界方式：

```ts
private publicationEntry(): SessionEntry | undefined {
  const entry = attachments.get(this)
  if (entry === null) {
    throw new Error(`session "${this.id}" cannot append outside a SessionStore`)
  }
  if (entry !== undefined && entry.announcement !== 'complete') {
    throw new Error(`session "${this.id}" cannot append before session/created completes`)
  }
  if (entry?.appending) {
    throw new Error('session append cannot reenter while another append is being published')
  }
  return entry
}
```

第三条是并发保护。监听器在 `session/event` 回调里再调 `append()`，会直接命中这一条。因为提交路径里没有 `await`，JavaScript 的单线程让「检查 `appending` 到置位 `appending`」这段不会被别的任务插进来，一个布尔量就够了。

代价写在同一段文档里（`index.ts:764` 起）：append 是同步的，热路径上不阻塞 I/O，持久化插件只能异步缓冲。「Once the event enters the log, the append is committed」说明落盘是后面的事，日志里的事件已经是事实。

广播也有讲究。`AgentEventDispatch.emit()`（`packages/core/agent/src/dispatch.ts:120`）绕开了 Cordis 的 `map`，逐个调用监听器，把同步异常和返回 promise 的拒绝分别接住并记日志。一个订阅者写挂了，不会让后面的订阅者收不到事件。这条与串行无关，但它决定了「一次 append 之后所有观察者的视图是否一致」。

## 5. tool batch：滚动池

前面三个串行点都在单 agent 的控制面上。需要并发的部分在工具批处理，`executeToolBatch`（`packages/core/tools/src/batch.ts:50`）。

模型一次给出多个调用时，调度器按组推进。并发安全的一批调用合成一个 group，group 内用一个有界滚动池：

```ts
const fillPool = async (): Promise<void> => {
  while (!aborted && nextToStart < group.length && inFlight.size < maxParallelToolCalls) {
    // Re-read later modes after ordered commits so registry changes can create a barrier.
    const nextCall = group[nextToStart]!
    if (nextToStart > 0 && mode === 'parallel'
      && pipeline.executionMode(nextCall.exec).kind !== 'parallel') break
    await startCall(nextToStart)
    nextToStart++
    throwSchedulerFailure()
    await commitReady()
    throwSchedulerFailure()
    // Abort may arrive while pre-execute awaits.
    if (signal.aborted) aborted = true
  }
}
```

三个条件同时成立才启动新调用：没有中止、还有没启动的、在飞的少于上限。上限来自本次批次的参数，缺省时由执行器给，`packages/core/tool-executor-native/src/index.ts:11` 的默认值是 10。`agent-loop/README.md:55` 说明了另一层：loop 每批读一次 `maxParallelToolCalls` 配置，批次参数优先，改配置对下一批生效，填 1 就是全串行。

池子里只有「派发与执行」重叠。「准备」是排队的：`startCall` 里先同步写 `tool/call` 事件拿到 seq，再 `await pipeline.prepare()`，这一段是有序的。`batch.ts:203` 的注释说得很直白：有序的 pre-execute 可能 await，重叠的只有 dispatch 和 body。

## 6. commitReady：只有连续槽位能提交

并发之后必然要收回顺序。`runGroup` 用三个游标管这件事：`nextToStart`、`started`、`committed`。收回顺序的地方是 `commitReady`：

```ts
// `committed` advances only across contiguous model-order slots.
const commitReady = async (): Promise<void> => {
  while (committed < group.length) {
    const slot = slots[committed]
    if (slot === undefined) break
    const call = group[committed]
    const result = await pipeline.complete(slot.exec, slot)
    // oxlint-disable-next-line typescript/no-non-null-assertion -- bounded index
    settlement.commit(call!.block, result, callSeqs[committed])
    concluded ||= result.concludesTurn === true
    committed++
  }
}
```

`slots` 是按模型顺序排的数组，每个槽位在对应调用完成时被填上。`commitReady` 从 `committed` 这个游标开始往后吞，遇到第一个空槽就停。所以第二个调用先跑完也没用，它的事件必须等第一个调用提交完才能写。

这就是「派发可以乱序，结果必须有序」的实现方式。没有排序队列，没有缓冲池，只有一个从 0 开始、绝不跳号的游标。

`commitReady` 在 `fillPool` 的循环里每启动一个调用就调一次，也在 `Promise.race` 每落定一个调用后调一次。也就是说：只要有新结果落定，就尝试把能提交的都提交掉。

{{< fig "diagrams/02-rolling-pool.svg" "滚动池的四条时间线：派发重叠、提交串行，游标遇到空槽就停，慢调用挡住后面已完成的结果" >}}

`commit` 会把 `tool/result` 和之前那条 `tool/call` 关联起来，靠的是 `callSeqs[index]`，也就是 `settlement.start()` 返回的 seq。事件里的 `sourceEventSeqs` 指向它。这解释了为什么 `startCall` 的第一行是写事件而不是派发：

```ts
const call = group[index]!
callSeqs[index] = settlement.start(call.block)
started++
const prepared = await pipeline.prepare(call.exec)
```

先有调用事件，后有执行。反过来的话，工具执行失败时就没法给结果找一条合法的来源。

## 7. barrier 与派发前的重分类

不是所有调用都能并发。主循环按第一个调用的执行模式决定这一组怎么切：

```ts
const mode = pipeline.executionMode(first.exec).kind
const group = mode === 'parallel' ? planned.slice(next) : [first]
```

第一个调用独占时，`group` 只有它自己。一个 group 跑完再进下一个，这就是 barrier。独占调用把前后的并行段切开，两侧的并行段各自成池。

更细的一层在 `fillPool` 里那句重新读取：`pipeline.executionMode(nextCall.exec).kind !== 'parallel'` 就 `break`。执行模式可以在前面的调用提交之后变，源码给的注释是「Re-read later modes after ordered commits so registry changes can create a barrier」（`batch.ts:193`）。一个调用入池时算出来的模式，等到真派发时按当时的注册表重算，可能已经不成立。

注意条件是 `nextToStart > 0`。组里第一个调用不再检查，它的模式已经在切组时用过一次。后续每个调用都要复查，因为前面的调用可能改了注册表状态。

这三层叠起来的语义是：并发许可以单个调用为单位，判定时机在每次派发之前，声明之后仍可能被改写。

自己搭的时候，重分类这一步最容易被当成多余检查删掉，因为多数工具在执行前不改自己的模式。删掉之后出问题的场景很窄：一个组合工具展开出独占子调用，而它恰好排在批次第二位。

## 8. 取消：drain 与 skip

中止信号到达时，`batch.ts` 做三件事，顺序不能换。

停止补充：`fillPool` 的循环条件里有 `!aborted`，`startCall` 之后还有一次 `if (signal.aborted) aborted = true`，因为 `prepare` 可能 await 到取消之后。

排空已派发：`while (inFlight.size > 0)` 里正常走完 `Promise.race` 与 `commitReady`。已经派发的调用不会被打断到「结果不明」的状态，它要么给出正常结果，要么给出 `ABORTED`。

补齐未派发：

```ts
if (aborted) {
  // Started calls and accepted context settle first; every remaining model
  // call then receives an ordered synthetic result before the turn aborts.
  for (const call of group.slice(started)) settlement.skip(call.block)
  return { consumed: group.length, aborted: true, concluded }
}
```

`skip()` 不是简单地记一条错误。它先调用 `this.start(block)` 补一条 `tool/call`，再写 `tool/result`，内容固定为 `Error: tool call aborted before dispatch`，错误码 `ABORTED_BEFORE_DISPATCH`（常量在 `packages/core/tools/src/contracts.ts:267`）。

这段逻辑的收益在回放时体现。录制会话 `snapshots/acp/cancel-tool-calls/session.v4.jsonl` 里的顺序是：

| 顺序 | 事件 | 关键字段 |
|---|---|---|
| 1 | `tool/call` | `callId: call_wait` |
| 2 | `job/registered` | bash 起了后台 job |
| 3 | `job/settled` | `status: "killed"` |
| 4 | `tool/result` | `code: "ABORTED"` |
| 5 | `tool/call` | `callId: call_skipped`，由 `skip()` 补写 |
| 6 | `tool/result` | `code: "ABORTED_BEFORE_DISPATCH"` |
| 7 | `step/end` | |
| 8 | `turn/end` | `reason.kind: "aborted"` |

第 5 行是代价也是保险。一个从未派发的调用按理没有 `tool/call`，但日志的读者是回放器和投影，它们从结果反查调用。少一条调用事件，第 6 行就指向一个不存在的 seq，回放会当成数据损坏。

## 9. wakingAfterAbort：醒来的是哪一轮

取消最容易写错的地方不在工具，在输入。用户点取消，同时一条消息正在路上。

`cancel()` 先清队列再中止（`agent.ts:191`）：

```ts
cancel(cause: AgentCancelCause, options: CancelOptions = {}): void {
  if (!options.keepInbox) {
    this.inbox.clear()
    if (this.phase.kind !== 'idle') this.phase.wakeRequested = false
  }
  if (this.phase.kind !== 'idle') this.phase.abort.abort(cause)
}
```

清 `wakeRequested` 是因为那个 latch 属于被取消的这一轮，留着会在收尾时又醒来一次。

接着 `send()` 要处理一个时间差：消息插入时，当前 phase 可能已经中止，但还没走完收尾。这时候把它放进 `next-step` 是错的，因为下一个 step 属于下一轮，而 `next-step` 会在轮次结束时被清掉。

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

分类在插入之前算完。如果先插入再判断，splice 的观察者有机会在中间触发一次取消，同一条消息就会被判成两种目标。

`wakingAfterAbort` 还要传给 `wakeDriver`，因为中止中的 driver 不会自己再醒来：

```ts
private wakeDriver(wakeAfterAbort = false): void {
  if (this.phase.kind !== 'idle') {
    const reason = abortedCancelCause(this.phase.abort.signal)
    if (reason?.kind !== 'disposed' && (this.phase.kind === 'maintenance' || wakeAfterAbort)) {
      this.phase.wakeRequested = true
    }
    return
  }
  // …开新 phase，跑 kick…
}
```

latch 的条件有三个。`reason?.kind !== 'disposed'` 保证销毁流程不留下任何待跑的轮次，否则 teardown 会等一个永远不会有人跑的 turn。`phase.kind === 'maintenance'` 覆盖后台维护期间来的唤醒。`wakeAfterAbort` 覆盖取消正在收尾期间来的唤醒。三者都不满足时，wake 被丢弃：因为 `cancel()` 已经清过队列，那条消息也不在了，两个动作必须一起生效。

`abortedCancelCause()`（`agent.ts:94`）只拷贝 `{kind}`，hook 类原因还要单独拷贝 `reason`。注释说明原因：Node 的 fetch 会往 abort reason 上挂一个 `stack`，这个不可序列化的字段会让 `Session.append` 记日志时炸掉或者直接拒收。

{{< fig "diagrams/03-wake-latch.svg" "取消之后的六个时刻：清队列、输入改道、置 latch、收尾、排空、下一轮，右侧是 wakeRequested 的取值变化" >}}

## 10. goal 的 revision 是乐观锁

工具批处理和取消都在一轮之内。跨轮的共享状态是 goal，它要让人类在 agent 跑的时候也能改。

`GoalService` 的每个变更方法（`edit`、`pause`、`resume`、`complete`、`block`、`clear`）开头都是同一对动作：先 `prepareMutation(agent)`，再 `expectCurrent(state, ref)`。后者（`packages/goal/goal/src/index.ts:458`）比 `id` 和 `revision`：

```ts
private expectCurrent(state: GoalProjection | null, ref: GoalRef): GoalProjection {
  if (state === null) throw new GoalError('no current goal', 'GOAL_NOT_FOUND')
  const current = state.goal
  if (ref.id !== current.id || ref.revision !== current.revision) {
    throw new GoalError(
      `stale goal ref "${ref.id}" revision ${ref.revision}; current is "${current.id}" revision ${current.revision}`,
      'GOAL_STALE_REVISION',
    )
  }
  return state
}
```

错误码 `GOAL_STALE_REVISION`。没找到当前 goal 时是另一条：`no current goal`，码 `GOAL_NOT_FOUND`。

写侧的约束在折叠函数里（`packages/goal/goal/src/fold.ts:192`）：`next.revision` 必须等于 `current.revision + 1`，否则抛 `goal ${operation} must advance the current goal by one revision`。这条把「读到的版本」和「写入的版本」绑成一个原子步骤：读到 r7，只能写 r8；别人已经写了 r8，你的写入就会被这条拒绝。

这套 CAS 的用户不只有人类。goal 的轮次是排队进入 agent 的，所以驱动程序还要在自己这一侧再验一次。`goal-round-driver` 的 `validReservation()`（`src/index.ts:371`）注释写着 "Fail closed unless the queued prompt still owns the exact live revision"，逐项比：fiber 仍 ACTIVE、没有在停、reservation 处于 `claimed`、没有 stale、队列消息还是同一条、goal id 对得上、revision 对得上、phase 仍是 `active` 加 `armed`，以及 `source.round === goal.roundsStarted + 1`。

验一次不够，要验两次。`agent/pre-step` 的钩子里（`src/index.ts:386`），`validReservation` 在瀑布之前跑一次，不合法就把这轮标成 stale、把它之外的已 claim 消息补回队列，然后返回 reject：

```ts
if (!valid) {
  const attempt = state.attempt
  if (attempt !== undefined && sameRound(source, attempt)) {
    attempt.stale = true
    state.attempt = undefined
  }
  restoreOtherClaimed(agent, messages, submitted.id)
  requestDrive(state)
  return { kind: 'reject' }
}
```

两次检查之间隔着整个 `agent/pre-step` 瀑布，中间可能有人改了 goal。瀑布之后同一个函数再调一次 `validReservation`，这次失败按「进入前被拒」处理：调用 `ctx.goals.block(...)`，错误码 `prompt-rejected`，消息是 `Goal round was rejected before entering its step.`。

{{< fig "diagrams/04-goal-cas.svg" "两方同时改同一版 goal：都读到 r7，先写入的推进到 r8，后写入的撞上 stale 拒绝；排队轮次在进 step 前再验一次" >}}

这个模式值得记下来：排队 + 版本校验 + 消费前复验。排队让写入方不必等，版本校验让过期的排队作废，消费前复验补上「排队期间又变了」的那段窗口。

## 11. subagent：信号量加每子一把锁

子 agent 的并发限制是一个计数信号量，实现只有二十来行（`packages/subagent/subagent/src/activation.ts:15`）：

```ts
class ActivationPool {
  private readonly slots = new Set<symbol>()
  reserve(capacity: number): () => void {
    if (this.slots.size >= capacity) {
      throw new SubagentError(
        `subagent limit reached (active child limit: ${capacity}); wait for an existing child to finish or complete this work with the current agents`,
        'ACTIVATION_LIMIT_REACHED',
      )
    }
    const slot = Symbol()
    this.slots.add(slot)
    return () => { this.slots.delete(slot) }
  }
}
```

用 `Set<symbol>` 而不是计数器，好处是释放可以做成幂等的闭包。同一个 symbol 删两次无害，计数器的递减不是。错误信息直接告诉模型该怎么做，而不是只说超限。

池子是每个 root agent 一个，`manager.ts` 里用 `rootPools = new WeakMap<Agent, ActivationPool>()` 挂载。这样子 agent 之间的额度互不影响，root 被回收时池子跟着走。

第二个结构是 `ChildLock`（`activation.ts:163`），每个子会话一把串行锁：

```ts
run<T>(childId: SessionId, operation: () => Promise<T>): Promise<T> {
  const previous = this.tails.get(childId) ?? Promise.resolve()
  const result = previous.then(operation, operation)
  const tail = result.then(() => undefined, () => undefined)
  this.tails.set(childId, tail)
  void tail.then(() => {
    if (this.tails.get(childId) === tail) this.tails.delete(childId)
  })
  return result
}
```

三个细节：`previous.then(operation, operation)` 两个处理器都是 `operation`，前一个失败不阻塞后一个；`tail` 把结果抹平成 `void` 并吸收拒绝，否则一次失败会顺着链传给所有后续调用者；最后那个 `if` 只在尾部仍是自己时才删，避免删掉别人刚挂上的新尾巴。

子会话的生命周期细节见[《子 agent 的一生》](/posts/dsh-subagent-lifecycle.html)。`manager.ts:146` 的注释说明这把锁覆盖三条路径：投递、释放、销毁。这三条都会碰同一个子会话的 resident 记录，串行化之后不需要在每条路径里各写一套状态判断。

## 12. 顺序保证有三层

并发的地方各自收口，收口方式不同。

| 保证 | 单位 | 由谁实现 | 常见的破坏写法 |
|---|---|---|---|
| 结果按模型给出的顺序 | 一个 group | `commitReady` 只提交连续槽位 | 用 `Promise.all` 收集，谁先完成谁先写 |
| 事件按 seq 递增 | 一个 session | `append` 同步提交加禁重入 | 异步 append 队列，失败时留下空洞 |
| 投影随内容变化单调递增 | 一个 surface | `replaceGeneration` 与 `contentGeneration` | 原地改节点，订阅者收不到通知 |

第一层见过。第二层的 `seq` 是 `log.length`，前面提过。第三层在 `packages/core/session/src/surface.ts:561`：替换节点同时递增 `replaceGeneration` 和 `contentGeneration`，投影节点只递增 `contentGeneration`。两个计数都是单调的，消费方比较计数就知道自己有没有落后，不需要逐节点比对。

三个计数器加起来的效果是：任何观察者都能用一个整数判断「我看到的是不是最新的」，而不需要把整个状态复制一份做 diff。

## 13. 设计取舍

取舍集中在五处，每一处都能换一种写法，换完要知道自己接下了什么。

| 取舍 | 选了什么 | 放弃了什么 |
|---|---|---|
| 状态表达 | 单槽位 `phase` | 并发判断必须集中，散开就失效 |
| 提交时机 | 同步进日志，异步落盘 | 崩溃时最后几条可能丢 |
| 并发许可 | 每个调用单独判定 | 每次启动多读一次执行模式 |
| 结果提交 | 连续槽位游标 | 慢调用会挡住后面已完成的提交 |
| 冲突处理 | revision 比较交换 | 调用方必须处理 stale 后重读 |

自己搭的时候，这五行里最容易被跳过的是第一行。相位写成散落的布尔量，`running`、`cancelling`、`waking` 各一个字段，组合状态立刻爆炸，取消和唤醒的竞态会变成一组布尔运算。

## 14. 踩坑点

1. 取消后到达的消息要落到 `next-turn`。落在 `next-step` 会被轮次结束时的清理吃掉，表现是「用户点了取消又发了消息，消息消失」。

2. `skip()` 必须补 `tool/call`。少了这条，`tool/result` 的 `sourceEventSeqs` 指向不存在的 seq。单测里看不出来，回放时才炸。

3. `commitReady` 的游标不能跳号。想优化成「谁先完成谁先写」，就得在别处重新排序，等于把复杂度搬了个地方。

4. 池子上限要每批采样。`maxParallelToolCalls` 是可变配置，固化在构造时就再也改不动，而且批次参数优先于执行器配置这条规则会失效。

5. `prepare` 之后必须复查执行模式。声明并发安全的工具展开后可能包含独占子调用，不复查就会把 barrier 拆掉。

6. append 里不能有 `await`。一旦有，`appending` 这个布尔量的检查与置位之间就会出现空档，禁重入失效。

7. 子会话的串行锁要吸收拒绝。`tail` 不 absorb 的话，一次失败会顺着 promise 链传染给后面所有无关的调用。

8. 相位收尾里的 latch 要看清原因类型。`disposed` 时必须不 latch，否则 teardown 会等一个永远不会跑的 turn。

## 15. 源码索引

- `packages/core/tools/src/batch.ts:50` `executeToolBatch()` 签名
- `packages/core/tools/src/batch.ts:56` 上限校验，`maxParallelToolCalls must be a positive integer`
- `packages/core/tools/src/batch.ts:139` `commitReady()` 只跨连续槽位
- `packages/core/tools/src/batch.ts:154` `startCall()`，先写 `tool/call` 再派发
- `packages/core/tools/src/batch.ts:186` `fillPool()` 与派发前的模式复查
- `packages/core/tools/src/batch.ts:225` 中止分支的 `skip()`
- `packages/core/tools/src/batch.ts:237` `BatchSettlement` 的 `start` / `commit` / `skip`
- `packages/core/tools/src/contracts.ts:267` `TOOL_ABORTED_BEFORE_DISPATCH`
- `packages/core/tool-executor-native/src/index.ts:11` `maxParallel` 默认 10
- `packages/core/agent-loop/src/agent.ts:53` `Phase` 判别式联合
- `packages/core/agent-loop/src/agent.ts:170` `send()` 与 `wakingAfterAbort`
- `packages/core/agent-loop/src/agent.ts:191` `cancel()` 清队列与 latch
- `packages/core/agent-loop/src/agent.ts:230` `wakeDriver()` 的 latch 条件
- `packages/core/agent-loop/src/agent.ts:268` `kick()` 的循环与收尾
- `packages/core/agent-loop/src/inbox.ts:108` `claim()`
- `packages/core/session/src/index.ts:806` `append()`
- `packages/core/session/src/index.ts:872` `publicationEntry()` 的三条拒绝
- `packages/core/session/src/surface.ts:561` `applySurfacePlan()` 的计数递增
- `packages/goal/goal/src/index.ts:458` `expectCurrent()` 与 `GOAL_STALE_REVISION`
- `packages/subagent/subagent/src/activation.ts:163` `ChildLock.run()`

## 16. 最后

这套设计的共同点是把并发边界画在状态上，而不是画在函数上。`phase`、`committed`、`revision`、`slots` 这四个东西各自守着一段状态，所有并发判断都写成对它们的读。读的地方集中，漏判就容易发现；读的地方分散，锁再多也不安全。

自己搭的时候，可以先只实现两件事：一个单槽位的相位，一条同步的 append。这两条立住之后，取消、唤醒、结果顺序都变成在它们之上的推导。剩下的取舍（滚动池多大、CAS 还是悲观锁、子 agent 额度怎么算）都是可以后补的参数。
