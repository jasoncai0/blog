---
title: "编排的账本：DSH 多 Agent 的任务状态、唤醒与重规划"
date: "2026-11-25T18:05:00+08:00"
summary: "DSH 里没有编排器，也没有计划对象。计划内容由模型写进 tool payload，任务状态落在根 Session 的事件日志里，调度只做一件事：在正确的时刻把下一条消息放进 inbox。拆开 todo、team task、workflow、goal 四个载体和两条队列，看「下一轮」和「重规划」到底由谁决定。"
description: "DeepSeek Harness 多 Agent 编排的状态归属：todo 的回合级投影、team task 的 CAS 任务板、subagent 结算通知、goal round 驱动器、inbox 的 next-turn/next-step 两条队列，以及「重规划」为什么不是一个函数。"
tags: ["multi-agent", "planning", "harness", "architecture"]
slug: "dsh-orchestration-ledger"
---

多 Agent 编排最容易问错的一个问题是「调度器在哪」。这个问法预设了一个组件：它持有计划，按依赖挑出下一个任务，派给某个 agent，失败后重排。

DSH 里没有这个组件。翻完 `packages/` 能找到的是四样东西：模型写下的计划内容、根 Session 的事件日志、从日志重建的投影、以及在特定条件下往 inbox 塞一条消息的几个驱动器。

这四个问题分开看，归属立刻清楚：

| 问题 | 谁维护 | 落在哪 |
|---|---|---|
| 计划内容：先做什么、依赖谁 | 模型 | `todo_write` 参数、`team_task_*` 的 subject/blockedBy、workflow 脚本、goal objective |
| 每个任务的状态 | Harness 存储，模型变更 | 根 Session 的 `team/task` 事件，`TeamTaskBoard` 做 CAS |
| 下一轮输入 | Harness | `agent.inbox` 的 `next-turn` / `next-step` 两条队列 |
| 什么时候重规划 | 模型 | 没有钩子；Harness 只负责把新状态和新消息送到模型面前 |

两者的分工：Harness 负责什么时候再给一轮，模型负责这一轮做什么。

{{< fig "diagrams/01-ownership.svg" "计划、状态、唤醒、重规划的归属：每个箭头都是一次显式读写；右侧列出的组件在源码里不存在。" >}}

## 1. 循环只认 inbox，不认计划

`Agent` 的主循环只有一行：

```ts
// packages/core/agent-loop/src/agent.ts:268
private async kick(): Promise<void> {
  try {
    while (await this.turn()) {}
  } catch (_error) {
    // 失败与取消在驱动器边界收口
  }
}
```

`turn()` 返回布尔值：inbox 里还有待处理消息就返回 true，接着开下一轮；没有就返回 false，回到 idle。循环里没有计划对象，也没有任务表，更没有 step 上限。

一个 turn 内部的取件顺序写死在 `turn()` 里：

```ts
// agent.ts:342
let target: InboxTarget = 'next-turn'
while (true) {
  const step = phase.step + 1
  const decision = await this.preStep(target, { turn, step })
  if (decision.kind === 'reject') { turnEnds = { kind: 'blocked' }; return false }
  // ...执行一步：一次模型调用 + 它的工具批次
  target = 'next-step'
}
```

第一步取 `next-turn`，之后每一步只取 `next-step`。`next-step` 是插队通道：模型正在跑的时候，新消息从这一步的下一步就进来，不必等整轮结束。`next-turn` 是排队通道：一次只放一条进来，独占一整轮。pre-step 拒绝时整轮直接结束，已经 claim 的消息不会放回队列；调用方（例如 goal 驱动器）自己负责重新排队。

谁往队列里写、什么时候写，决定了多 Agent 的行为。

## 2. 计划有四个载体，生命周期各不相同

「计划」在 DSH 里不是一种东西。四个载体的生命周期和读者都不同，混着用就会得到「任务列表刷新了但没人动」这类现象。

| 载体 | 生命周期 | 谁能读 | 谁清空 | 会不会自动进入模型上下文 |
|---|---|---|---|---|
| `todo_write` | 单个 turn 的展示 | 人（UI 投影）、模型（自己上次的 tool call 参数） | 下一个 `turn/start` 清零投影 | 不会 |
| team task | 根 Session 全程 | 树上任意成员调 `team_task_list` | 只能显式 delete 成墓碑 | 不会 |
| workflow | 一次运行 | 脚本自己、观察事件的宿主 | 进程结束即消失 | 不会 |
| goal | 跨轮，直到完成或清除 | `get_goal` / 轮次提示 | `complete` 或 clear | 自动续跑时注入 `<goal_round>`（需 idle + 树排空） |

{{< fig "diagrams/02-lifecycles.svg" "三种状态的生命周期（workflow 运行态不进日志，不在此图）：team task 由 claim/complete/release/reopen 驱动并带墓碑；goal 的 phase 持久、activation 只活在进程内；todo 是回合级投影，turn/start 清零，turn/end 保留给人看。" >}}

### todo 是给人看的进度条

`todo_write` 每次都替换整张表，执行时追加一条事件：

```ts
// packages/todo/tool-todo/src/index.ts:200
const todos = toTodoList(args.todos, allowParallel)
exec.agent.session.append('todo/write', { todos })
```

投影 `todos` 的折叠规则只有三条：收到 `todo/write` 就换成新表，收到 `turn/start` 就变回 `null`，其他事件原样返回。所以它是一块「本轮进度」的展示屏：轮次结束还留着，让用户看到刚做完什么；下一轮一开始就清空。

模型看不到这块投影。它下一轮能参考的，是 transcript 里自己上次那个 tool call 的参数。工具返回的文本只有一句 `Updated todo list: 3 pending, 1 in progress, 2 completed.`，不重复清单内容。

这条设计的含义：`todo_write` 不是跨轮记忆，也不是调度输入。它不参与任何唤醒判断。

### team task 是唯一可被重新读取的共享账本

Team 默认关闭，根 Agent 显式打开后才注册四个工具。任务板挂在根 Session 上，`TeamId` 就是根 `SessionId`。每条任务存整快照：

```ts
// packages/team/agent-team/src/types.ts:64
interface TeamTaskSnapshot {
  readonly id: TeamTaskId            // task-1, task-2 ...
  readonly revision: number          // 每次变更 +1
  readonly subject: string
  readonly description: string
  readonly status: 'pending' | 'in_progress' | 'completed' | 'deleted'
  readonly ownerId?: SessionId
  readonly ownerName?: string
  readonly blockedBy: TeamTaskId[]
  readonly writeScopes: string[]
}
```

`ready` 不落库，是读的时候算出来的：`blockedBy` 里每个任务都 `completed`，这个任务才 ready。

```ts
// packages/team/agent-team/src/task-view.ts:22
export function taskReady(state: TeamState, task: TeamTaskSnapshot): boolean {
  return task.blockedBy.every(id =>
    state.tasks.find(candidate => candidate.id === id)?.status === 'completed')
}
```

`claim` 要求 `pending` 且 ready，写 `in_progress` 和负责人；`complete` 要求 `in_progress`；`release` 退回 `pending`；`reopen` 从 `completed` 回 `pending`；`reassign` 只有 Lead 能做。`deleted` 是墓碑，仍有下游任务引用它时删除会被拒绝。

模型侧的流程写在工具的系统提示里：list、get、用当前 revision claim、干活、complete。同一段还写了两条边界：**任务 ready 不会启动负责人；任务变更不会结束 `wait_agent`、不会加 inbox 消息、不会唤醒空闲 agent。** 需要另一个 agent 对任务变化做出反应，必须显式 `send_message`。

### workflow 的计划就是那段脚本

`workflow` 让模型写一段 JS，引擎在 `vm` 里跑：

```ts
new vm.Script('(async () => {\n' + body + '\n})()', {
  filename: `workflow:${name}`,
  lineOffset: -1,
})
```

脚本拿到 `agent / parallel / pipeline / phase / log / args` 六个注入。`agent()` 受两个上限约束：总 agent 数 `maxTotalAgents`、并发数 `maxConcurrentAgents`（FIFO 等槽）。子 agent 失败返回 `null`，脚本自己决定重试还是换路。

`phase(title)` 只改一个标签并通知观察者，不改变执行结构。引擎发出 `workflow/start`、`workflow/phase`、`workflow/agent-start`、`workflow/agent-end`、`workflow/end` 这些只读事件，后台运行时再镜像进 job 的输出环。

所以 workflow 的计划不落在任何可恢复的表里。进程没了，运行状态就没了；能重放的只有 Session 日志里那些工具调用与结果。

### goal 是唯一跨轮的目标

Goal 的持久部分只有四个字段：objective、phase（active/paused/blocked/complete）、maxGoalRounds、revision。activation（armed/disarmed）是进程本地的，重启后一律 disarmed，等一次人工 resume。

它不保存步骤，也不保存子任务。它保存的是一句目标和一个轮次额度，剩下的由每一轮的模型重新决定。

## 3. 状态写进日志，读的时候再算

任务板的所有变更走同一个日志入口：

```ts
// packages/team/agent-team/src/journal.ts:44
async transact<T>(rootId: SessionId, operation: () => Promise<T>): Promise<T> {
  const prior = this.tails.get(rootId) ?? Promise.resolve()
  const run = prior.then(operation, operation)
  const tail = run.then(() => undefined, () => undefined)
  this.tails.set(rootId, tail)
  try { return await run } finally {
    if (this.tails.get(rootId) === tail) this.tails.delete(rootId)
  }
}

// journal.ts:62
async appendAndFlush(root: Agent, task: TeamTaskSnapshot): Promise<void> {
  root.session.append('team/task', { version: 2, teamId: TeamId(root.id), task })
  try {
    const participated = await this.ctx.sessions.flush(root.session)
    if (!participated) throw new Error('No session durability listener participated')
  } catch (error: unknown) {
    throw new TeamError(
      `task "${task.id}" revision ${task.revision} is visible, but persistence was not confirmed; read the task before retrying`,
      'TEAM_TASK_PERSISTENCE_UNCONFIRMED',
      { cause: error },
    )
  }
}
```

两个细节决定了并发行为。

`transact` 按根 Session 串行化。同一块任务板上的「读当前状态 → 校验 → 追加」不会交错，哪怕多个 agent 同时改。

`appendAndFlush` 先追加事件，再等落盘。失败时区分两种状态：事件已经可见，持久化没有确认。这时直接重试可能基于一个已经提交的新 revision 再提交一次，所以错误文案要求先 read 再决定。这是「先发布、后确认」的典型代价，也是它必须显式报错的原因。

并发写靠 CAS 拦。`update` 要求调用方带上读到的 revision：

```ts
// packages/team/agent-team/src/task-board.ts:128
if (current.revision !== request.expectedRevision) {
  throw new TeamError(
    `stale team task "${current.id}" revision ${request.expectedRevision}; current revision is ${current.revision}`,
    'TEAM_TASK_STALE_REVISION',
  )
}
```

版本对不上就整次拒绝，不合并、不猜测。模型拿到错误后重新 list，再决定改哪张任务。

任务板的权威副本只有 Session 日志。内存里没有第二份任务表；`state(root)` 每次都从投影读，投影坏了直接抛错，不降级返回空表。

## 4. 下一轮输入：两条队列，三个写入者

`inbox` 是一个投影，也是一个命令面。两条队列的语义写在同一处：

```ts
// packages/core/agent-loop/src/inbox.ts:108
claim(target: InboxTarget, turn: number): UserMessage[] {
  const claimed = this.mutate('next-step', 0, this.nextStep.length, [], false)
  if (target === 'next-turn') claimed.push(...this.mutate('next-turn', 0, 1, [], false))
  for (const message of claimed) {
    this.dispatch.emit('agent/inbox/claimed', { message, turn })
  }
  return claimed
}
```

不管取哪条队列，先把 `next-step` 全部取空，再按需取一条 `next-turn`。所以插队消息总是排在排队消息前面，并且和它同批进入模型请求。

三个写入者：

**人**。Web/CLI 的输入走 `followup()`（排队，唤醒）或 `steer()`（插队，唤醒）。`inject()` 只插队不唤醒，给不需要开新一轮的内部通知用。

**子 agent 管理器**。子 agent 结算时，结果先落盘、再发通知，最后才发 `subagent/end` 事件：

```ts
// packages/subagent/subagent/src/manager.ts:1095
const terminal: SubagentResult = failure === undefined
  ? result
  : { output: [], stopReason: 'error' }
this.notifySettlement(activation, result)
this.host.emitLifecycle('subagent/end', { ...this.runInfo(activation), stopReason: terminal.stopReason })

// manager.ts:1109
private notifySettlement(activation: Activation, terminal: SubagentResult): void {
  if (!activation.accepted || activation.delivery === 'caller') return
  const parent = this.ctx.agents.get(activation.ancestors.at(-1) as SessionId)
  if (parent === undefined || this.rootOf(parent) !== activation.root) return
  const message = createSettlementMessage(/* child id, 名字, 结果 */)
  this.sendWaking(parent, message, parent.status === 'idle' ? 'queue' : 'steer')
}
```

父 agent 空闲时，结算通知进 `next-turn` 并唤醒，等于「子 agent 干完了」直接变成父 agent 的下一轮开头。父 agent 正在跑时，通知进 `next-step`，在当前这轮的下一步就能看到。

注意顺序：通知发生在资源释放之后、`subagent/end` 之前。父 agent 收到消息时，子会话已经收尾，读它的结果不会读到半截。结算消息的摘要由 `createSettlementMessage` 从子 agent 的 `result` 生成；代码里的 `terminal` 只用于 `subagent/end` 事件的 `stopReason`。

**goal 驱动器**。它的触发条件最窄：agent 处于 idle、没有正在进行的驱动、后代 subagent 树已经排空。满足之后先 flush，再排一条 `<goal_round>`：

```ts
// packages/goal/goal-round-driver/src/index.ts:172
// 省略 checkpoint、attempt 复核与 disarm 分支
const goal = currentGoal(state)
if (!drivable(goal)) return
const subagents = ctx.get('subagents')
if (subagents !== undefined) {
  await Promise.race([subagents.waitForTreeWork(agent), unloading.promise])
  if (!readyAfterCheckpoint(state)) return
}
if (goal.roundsStarted >= goal.maxGoalRounds) {
  ctx.goals.block(agent, goalRef(goal), {
    code: 'round-limit',
    message: `Goal reached its configured limit of ${goal.maxGoalRounds} rounds.`,
  })
  return
}
const round = goal.roundsStarted + 1
const message = createUserMessage({
  content: renderGoalRoundPrompt(goal, round),   // Objective + Round n/N
  source: { kind: 'goal', goalId: goal.id, revision: goal.revision, round },
})
agent.followup(message)
```

`<goal_round>` 的正文很短：目标、`Round: 3/16`、要求核对当前工作区与持久状态、继续做、有证据才标完成。它不告诉模型下一步做什么。

驱动器为每一轮维护一条进程本地的 attempt 记录，checkpoint 是一次强制 flush 的标记。额度不是排队就扣：`agent/pre-step` 会复核这条消息还该不该被接受，检查 attempt 是否仍处于 claimed、是否过期、goal 是否仍 active 且 armed、`source.round` 是否等于 `roundsStarted + 1`。不合法就拒绝这一步，避免「消息排进去了但被拦下，额度却涨了」。

`wait_agent` 是读取方，不是写入方。它监听 `agent/inbox/inserted`，等新的 `next-step` 消息出现，但不消费；同一个 tool call 里已经报告过的消息不会让它再次返回，防止空转。

## 5. 重规划：没有钩子，只有信号

源码里搜不到 `replan` 这个标识符。把连字符也算上，`re-plan` 只出现在法语 locale 的 `arrière-plan`（后台）里，和重规划无关。

也没有「计划失效」的判断。Harness 不知道一个子 agent 的结论会不会推翻原计划，也不知道某个 blocker 是否真的还阻塞。它做的是两件事：把新状态写进日志，把新消息放进队列。重规划发生在模型下一次读到这些输入的时候。

会改变计划判断的输入，按出现频率排：

1. **子 agent 结算通知**。`subagent-settled` 消息带一行结果摘要。父 agent 空闲时它就是下一轮的第一条输入；正在跑时它在下一步插进来。这是最常见的「计划需要改」的时刻。
2. **`team_task_list` 的返回值**。依赖完成了，`ready` 变 true；别人改过，revision 变了。模型必须主动 list 才看得到，没有推送。
3. **`<goal_round>` 轮次提示**。每轮都要求核对工作区和持久状态，等于强制一次「当前进度 vs 目标」的对账。
4. **`wait_agent` 返回**。有新的插队消息，通常意味着用户改向或另一个 agent 发来请求。
5. **用户 steer**。直接插进当前这轮，优先级最高。
6. **上下文压力**。压缩、`max-tokens`、turn 结束都会打断当前节奏，给模型一次重新组织的机会。

反过来，下面这些事**不会**触发任何重规划：

- 任务状态变化（claim、complete、release）只写日志，不发消息。
- 任务 ready 不会启动负责人。
- 子 agent 在跑但还没结算时，父 agent 收不到「进度」通知。
- goal 还 active 但树没排空时，驱动器不会插话。

所以「什么时候重规划」的准确答案是：模型每次拿到新输入时都可能重规划；Harness 不判断旧计划是否还成立，它保证新输入能送到模型面前。

## 6. 一次完整的多 Agent 任务

把上面的机制串起来，走一遍「三个任务、两个子 agent、一条依赖」的场景。

{{< fig "diagrams/03-sequence.svg" "一次多 Agent 任务的时序：Lead 建任务图、派两个子 agent、结束本轮；子 agent 结算把 Lead 唤醒；Lead claim 任务、发消息让另一个子 agent 开工；goal 驱动器在树排空后再给一轮。" >}}

1. 用户给目标，Lead 的第一个 turn 开始。目标文本进 `next-turn`，被 `preStep` claim。
2. Lead 调三次 `team_task_create`，写成 `task-1`、`task-2`（blockedBy task-1）、`task-3`。每次创建都走一次 `transact` + `appendAndFlush`。
3. Lead 调两次 `start_subagent`。工具立刻返回 `started subagent agent-1`，子会话在后台跑；父 agent 不阻塞。
4. Lead 这一轮没有更多工具要调，turn 结束，agent 回到 idle。此时任务板上有三条 pending，两个子 agent 在跑，没有任何东西在「调度」。
5. `agent-1` 完成。管理器释放它的资源，构造结算消息，发现父 agent idle，于是进 `next-turn` 并唤醒。
6. Lead 被唤醒，开始新 turn。第一件事通常是 `team_task_list`，看到 task-1 ready、revision 是 1。
7. Lead 调 `team_task_update`：`claim` + `expected_revision: 1`。任务板校验 ready 与 revision，写 `in_progress`，负责人是 Lead。
8. Lead 需要 `agent-2` 处理 task-2，但 task-2 还 blocked。Lead 用 `send_message` 告诉它先做别的，或者等。**任务板不会替 Lead 发这条消息。**
9. `agent-2` 也结算后，subagent 树排空。goal 驱动器发现 Lead idle 且树空，flush 后把 `<goal_round>` 排进 `next-turn`。
10. 下一轮 Lead 继续：complete task-1，task-2 变 ready。ready 只改视图，不产生消息；Lead 要么自己 claim，要么再 `send_message` 叫醒 `agent-2`。

这十步里，Harness 只做了三次主动动作：第 5 步投递结算通知、第 6 步唤醒、第 9 步排入 goal 轮次。其余全部是模型调工具造成的状态变更。

## 7. 围栏：哪些事 Harness 必须管

计划交给模型，不等于什么都不管。DSH 在四个位置设了硬边界，都是「模型做错会留下坏状态」的地方。

**并发写**。同一块任务板的变更按根 Session 串行，配合 revision CAS。模型拿到 `TEAM_TASK_STALE_REVISION` 必须重新读，不能盲改。

**落盘确认**。`appendAndFlush` 失败抛 `TEAM_TASK_PERSISTENCE_UNCONFIRMED`，明确说「已可见、未确认」。重试前必须先读，避免基于旧 revision 重复提交。

**轮次额度**。goal 的 `maxGoalRounds` 默认 256（`packages/goal/goal/src/index.ts:239`），到顶记 `round-limit` blocker 并停下。额度只在消息真正进入 step 时消耗，pre-step 复核防止白扣。

**结构校验**。任务图在每次写入前查环、查缺失 blocker、查重复；`blockedBy` 指向不存在的任务直接拒绝。`deleted` 任务仍有下游引用时拒绝删除。

另外三个上限属于部署配置，不是模型能改的：`TeamService.maxTasks` 默认 256、workflow 的 `maxTotalAgents` / `maxConcurrentAgents`、subagent 的深度与并发。`writeScopes` 只是给模型的提示，用来减少冲突，不是锁；任务工具的系统提示原文写的是 `Write-scope overlap is advisory, not a lock.`

## 8. 为什么把计划交给模型

三个理由，按重要性排。

**计划的有效期比一轮短。** 子 agent 返回「这个目录是空的」，原计划的第三步就作废了。Harness 若持有 DAG，需要一条规则判断「什么变化让哪条边失效」，这条规则只有模型能写。写在代码里，等于把业务语义硬编码进编排层。

**任务语义只有模型懂。** 什么叫完成、哪个 blocker 是真的、两个任务能不能合并，字段层面看不出来。Harness 能做的是校验结构（有没有环、revision 对不对），不是判断内容。

**恢复只依赖日志。** 崩溃重启后，任务板、goal、inbox 都能从根 Session 的事件重放出来。workflow 是例外：它的一次运行状态不进日志，进程退出即丢。把计划放进日志而不是调度器内存，至少让前三个载体只剩一个状态源。

代价也要写清楚：

- 没有全局最优调度，谁先跑取决于模型和事件到达顺序。
- 没有公平性保证，一个 agent 可以连续抢任务。
- 任务变化不唤醒，Lead 可能在一个已经过时的视图上做决定。
- 计划只存在于根 Session 的作用域里，跨 session 不共享。
- workflow 的运行状态不可恢复，进程退出就丢。

这五条是上述取舍的代价。要做一个真正的调度器，至少需要：持久任务图、状态机、唤醒规则、失败重试、公平性策略、可观测的执行历史。DSH 只把唤醒留在 Harness；公平性和可观测的执行历史没有对应实现。

## 9. 自己实现时的检查清单

1. 先决定计划存在哪。模型上下文、事件日志、独立数据库，三选一，不要两份都当权威。
2. 每次状态变更带 revision，读改写用 CAS；冲突返回可读错误，不要静默覆盖。
3. 同一聚合根的写操作串行化，否则 CAS 也挡不住读到的中间态。
4. 写日志和确认落盘分开建模。失败信息要区分「没写成」和「已可见但未确认」。
5. 唤醒条件写成显式判断：agent 是否 idle、后代是否排空、是否已有待处理轮次。
6. 任务状态变化默认不唤醒任何人；需要谁动，就显式发消息。这条能消掉大半「任务卡住」的 bug。
7. 给自动续跑设额度，并在消息真正被接受时扣，不要在排队时扣。
8. 计划视图设过期策略。回合级清单在下一轮清空，持久任务板靠 revision 判新旧。

## 10. 证据与版本

源码取自 `deepseek-harness` 仓库 `0.2.2-rc.1`，HEAD `fcc0ad46`。本机运行的桌面版打包在 `app.asar` 里，无法逐行比对，所以行号只对应该 tag。

| 机制 | 位置 |
|---|---|
| 主循环与 turn | `packages/core/agent-loop/src/agent.ts:268`、`:327`、`:342` |
| inbox 两条队列与 claim | `packages/core/agent-loop/src/inbox.ts:27`、`:108` |
| 任务板事务与落盘 | `packages/team/agent-team/src/journal.ts:44`、`:62` |
| 任务状态机与 CAS | `packages/team/agent-team/src/task-board.ts:128` |
| ready 计算 | `packages/team/agent-team/src/task-view.ts:22` |
| 任务工具与策略文本 | `packages/team/tool-agent-team/src/index.ts:27` |
| 子 agent 结算投递 | `packages/subagent/subagent/src/manager.ts:1095`、`:1109` |
| goal 轮次驱动 | `packages/goal/goal-round-driver/src/index.ts:172`、`:387` |
| goal 轮次提示 | `packages/goal/goal-round-driver/src/prompt.ts` |
| todo 投影与工具 | `packages/todo/tool-todo/src/index.ts:124`、`:200` |
| workflow 执行器 | `packages/workflow/workflow-ptc/src/runtime.ts:75` |

判断都来自这些文件的逐行阅读；「没有调度器」「没有 replan API」是搜索结论，不是推断。仓库里确实不存在以这两个名字命名的模块、接口或事件。
