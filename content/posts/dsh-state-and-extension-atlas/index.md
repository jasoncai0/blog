---
title: "状态与扩展点地图：谁持有、谁写、加功能挂哪里"
date: 2026-11-13T13:40:00+08:00
slug: dsh-state-and-extension-atlas
summary: "把 DSH 的状态所有权与扩展点摊成一张表：哪些状态只有一份、谁写、何时失效，以及新增一类能力应该挂事件、服务、工具还是 skill。"
tags: ["architecture", "harness", "agent"]
---

> 依据：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`。行号取自该 commit 的 `packages/**/src` 与 `docs/`，没有运行时观测；凡由类型定义与调用点合并得出的判断，会在句子里标明。

两季拆到这里，单个部件的细节已经够了，缺的是一张横向对照表。[第 1 篇](/posts/dsh-architecture-anatomy.html)讲 Cordis 插件树，[session 篇](/posts/dsh-session-event-sourcing.html)讲日志怎么长出来，[工具篇](/posts/dsh-tool-registry-scope.html)讲注册表怎么分层，[team 篇](/posts/dsh-agent-team.html)讲任务板怎么串行化，[workflow 篇](/posts/dsh-workflow-jobs-goal.html)讲四种循环外机制。每一篇都回答了「这个东西怎么工作」，没有回答「它和别的状态是什么关系」。

关系问题的实际形态很具体。加一个功能时，你通常先找到一个能挂的钩子，代码跑通，过两周发现重启后状态对不上，或者两个 agent 互相看见对方的工具。这类故障的根因在挂载点，不在实现。

这篇给三样东西：11 项状态的所有权，6 类扩展点的注册规则，一张用来选挂载点的决策树。读法可以按需：查状态跳到第 2 节，查扩展点跳到第 3 节，动手前看第 4、5 节。

## 0. 结论

DSH 把状态按寿命分成三类，把扩展点按层分成三层，两者交叉决定新代码该放哪。持久状态只有一份真源，进程内状态不假装可恢复，可重建状态必须能从日志折叠出来。

| 症状 | 先查 | 不要先改 |
|---|---|---|
| 重启后状态对不上 | 第 1 节的三类寿命 | 给进程内状态补持久化 |
| 两个插件写同一份数据 | 第 2 节的所有权表 | 加锁或加队列 |
| 新功能不知道挂哪 | 第 4 节的决策树 | agent loop |
| 偶发重复执行或丢尾巴 | 第 5 节的失效矩阵 | 重试逻辑 |

后文按「寿命、所有权、扩展点、决策、失效」展开。所有表格里的「唯一持有者」指运行时实际能改这份状态的对象，不指类型定义所在的位置。

## 1. 三种寿命，三种失效方式

判定一个状态属于哪类，只问两个问题：进程退出后它还在不在；如果不在，能不能从别的东西算回来。

| 类 | 判定 | 崩溃后 | 例子 |
|---|---|---|---|
| 持久 | 有独立介质，重放后得到同一视图 | 读回最后一个 flush 前缀 | session 日志、goal、team 任务板、schedule 任务 |
| 可重建 | 不单独写盘，但能由持久事件折叠 | 重新折叠，结果与崩溃前一致 | surface、token 计数、compaction 游标 |
| 进程内 | 只活在对象、闭包、WeakMap 里 | 消失，日志里只剩审计事件 | job 注册表、workflow run、审批等待、goal activation |

{{< fig "diagrams/01-three-lifetimes.svg" "图注：三类寿命各占一栏。持久状态从介质读回，可重建状态从日志折叠，进程内状态随进程消失；崩溃箭头指向每一栏各自的结局。" >}}

### 1.1 持久：日志是唯一真源

会话日志的契约写在 `session-persistence` 的抽象类注释里。事件从 seq 0 连续、永不重写；撕裂的物理尾巴不会返回给读者，写路径在下一次 append 前把它截断；读只接受当前格式，未知词汇直接拒绝。

```ts
// packages/session/session-persistence/src/index.ts:124-132
/**
 * Durable append-only session storage addressed through per-session handles.
 *
 * Storage semantics shared by every backend: events are contiguous from seq 0
 * and never rewritten; a torn physical tail is never returned to a reader and
 * is truncated by the write path before its first append; reads validate
 * current-format records only and refuse unknown vocabulary fail-closed.
 * `append` persists best-effort; `flush` — per handle or service-wide — is
 * the durability barrier.
```

这段契约解释了为什么 `append` 是 best-effort 而 `flush` 才是屏障。崩溃后能恢复的边界落在最后一次成功 flush，最后一条事件本身不算数。团队任务板把这一点写进了 API：`appendAndFlush()` 在 append 之后等 flush，失败就抛 `TEAM_TASK_PERSISTENCE_UNCONFIRMED`，让调用方先重读再决定是否重试。

会话日志里还有两个边界标记。`session/end-seed` 标出 fork 的继承切口，只有构造器与 fork seed 会写；普通插件写它，会把此前仍在进行中的 turn 误判成祖先历史。`turn/start` 与 `turn/end` 则给崩溃恢复提供闭合点：`interruptedTurnClosers()` 只补尾部未闭合的 turn 和缺失的 tool result，不碰压缩括号。

### 1.2 进程内：不假装能恢复

jobs-local 的文件头把话说得很直：job 生命周期、输出环、模型游标全在内存，交出去的是投影和 chunk 拷贝。注册与结算会写 `job/registered`、`job/settled` 两个审计事件，这两个事件不进 surface，也没有对应的投影。重启后能查到「曾经跑过」，查不到「现在还在跑」。

workflow 走同一条路。`WorkflowEngine` 的注释写着 a live run is holder-owned, its result never rejects, disposal waits for script and child cleanup。运行态由 provider 持有，子 agent 的 session 才落盘。审批等待也只是 `request()` 里一个挂起的 Promise，进程内没有待审批队列。

```ts
// packages/workflow/workflow/src/index.ts:150-160
/**
 * Workflow Service Definition contract. Invalid requests throw before publication; a live
 * run is holder-owned, its result never rejects, and disposal waits for script
 * and child cleanup. Lifecycle
 * listener failures are contained, and `workflow/end` fires exactly once as the
 * result settles.
 */
export abstract class WorkflowEngine extends Service {
  constructor(ctx: Context) {
    super(ctx, 'workflowEngine')
  }
}
```

goal 的 activation 是另一个例子。目标是持久的，授权是进程本地的：`GoalRuntimeState` 存在 `WeakMap<Session, …>` 里，`agent/created` 时一律 disarm，恢复或 fork 后要人类重新 resume。这个选择牺牲了便利，换来的是一条硬约束：崩溃不会让一个无人看管的目标自己继续跑。

### 1.3 可重建：折叠函数必须公开

surface 是日志之上的有序视图，`SessionSurface` 只有 `nodes`、`replaceGeneration`、`contentGeneration` 三个字段。它不写自己的存储，每次从日志折叠；压缩只是往日志里追加一条带 `surfaceOp: replace` 的消息，把旧区间遮掉。

```ts
// packages/core/session/src/surface.ts:244-252
/** Readonly live projection of the message-producing session events. */
export interface SessionSurface {
  /** Current surface event sequences in model-visible order. */
  readonly nodes: readonly SessionSeq[]
  /** Monotonic count of committed positional replacements. */
  readonly replaceGeneration: number
  /** Monotonic count of committed replacements and plugin-owned message changes. */
  readonly contentGeneration: number
}
```

token 计数同理。`TokenMeter` 用 `WeakMap<Session, ReplayState>` 缓存，注册 measurement、usage、contextPressure、breakdown 四个投影；`contextPressure()` 读投影，`measure()` 按 surface 重算。计数没有独立写入口，usage 跟着 `assistant/message` 一起落盘。

```ts
// packages/core/session/src/types.ts:349-357
  'assistant/message': {
    turn: number
    step: number
    message: AssistantMessage
    /** Exact timed model stream, compacted without joining delta boundaries. */
    stream: AssistantStreamRecord[]
    usage?: TokenUsage
    interrupted?: true
  }
```

这类状态的主要风险是折叠函数和写入口对不上。投影的 `apply` 一旦比 `append` 宽松，重放就会得到与实时不同的结果；inbox 的投影在坏 splice 上直接抛 `invalid persisted inbox splice at session seq …`，让这种不一致在恢复时立刻暴露。

## 2. 状态所有权表

下面 11 项按寿命分三张表。每张表的列相同：谁持有、从哪写、从哪读、怎么失效或重建、崩溃后剩下什么。

### 2.1 持久状态

会话内状态的词汇表是 `SessionEventMap`，14 个类型全部是持久事实。`assistant/message` 把消息、紧凑流、usage 放在同一条记录里，`session/end-seed` 单独标出 fork 切口。事件类型不在表里，`Session.append()` 在编译期就拒绝。

```ts
// packages/core/session/src/types.ts:287-295
export interface SessionEventMap {
  /**
   * Opens turn `turn` before the loop claims queued input or runs pre-step.
   * Rejection, empty input, cancellation, or failure may close it with no
   * step; otherwise the following identified `user/message` event or batch
   * records the messages entering the step.
   */
  'turn/start': { turn: number }
```

| 状态 | 唯一持有者 | 写入通道 | 读路径 | 失效或重建 | 崩溃后 |
|---|---|---|---|---|---|
| session 日志 | Session 实例；落盘由 `SessionPersistence` 的单写句柄负责 | `session.append()`；句柄 `append()` 与 `flush()` | `session.events`、投影、冷读 | 重放 seq 0；撕裂尾巴在下次 append 前截断 | 恢复到最后一个 flush 前缀 |
| inbox | `ReactLoopInbox` 与 `inbox` 投影 | `splice()` 写 `agent/inbox/spliced` | `agent.inbox` 读投影 | 重放 splice；坏 splice 抛错 | 待处理输入仍在 |
| goal | `GoalService`；每会话 `goal/change` | `commit()` 写日志后 emit | `goal` 投影与 goal 工具 | 重放；activation 不重放 | 目标在，授权不在 |
| team 任务板 | Lead 的 Session 与 `TeamTaskBoard` | `journal.appendAndFlush()` | `team` 投影 | 重放 `team/task` | flush 过就在 |
| approval 审计 | `ApprovalService` | `request()` 先 asked 后 decided | 日志回放；`approvalState` 只投影策略与 turn 开关 | 重放 | 可能只剩 asked |
| schedule | 宿主 `storageDomain` 与 `ScheduleRuntime` | 服务方法写表，成功后 emit | list/get 与 `schedule/changed` | 宿主启动重读并重排 | 任务在，未完成投递按 inbox id 找回 |

schedule 是表里唯一不把任务存进会话日志的。`ScheduleService` 把任务放进宿主 storage domain，记录按 zod schema 解码；宿主重启时 `recoverOutstandingExecution()` 用上一条投递的 messageId 去目标会话的 inbox 里找消息，找到就删掉再以同一身份 followup，让空闲 agent 重新被唤醒。它不保存模型完成状态，只恢复「还排在队列里的那条投递」。

inbox 的持久化只存 splice，不存整个队列。`agent/inbox/spliced` 记录目标队列、起点、删除数、插入消息与可选的 canceled 结果；恢复时从 seq 0 重放这些 splice，坏区间直接抛错。队列的每次增删都有日志位置，崩溃后待处理输入不会凭空消失。

```ts
// packages/core/agent/src/types.ts:110-117
    'agent/inbox/spliced': {
      target: InboxTarget
      start: number
      removedCount?: number
      inserted: UserMessage[]
      outcome?: 'canceled'
    }
```

goal 的 commit 把日志序号与进程内授权绑在一次写里。append 前先记下 pendingActivation 的 offset，append 返回后只有 seq 对得上才更新 activation；对不上说明这条事件没落在预期位置，授权保持旧值。emit 在提交之后，监听者看到的一定是已落日志的状态。

```ts
// packages/goal/goal/src/index.ts:609-625
  private commit(agent: Agent, runtime: GoalRuntimeState, change: GoalChangeMeta, activation: GoalActivation): void {
    const ref = goalChangeRef(change)
    runtime.pendingActivation = { offset: agent.session.seq, activation }
    try {
      const event = agent.session.append('goal/change', change)
      /* v8 ignore next -- Session.append returns the event committed at the pre-append seq. */
      if (SessionSeq(runtime.pendingActivation.offset) === event.seq) runtime.activation = activation
    } finally {
      runtime.pendingActivation = undefined
    }
    const goal = this.view(this.state(agent.session), runtime)
    const notification: GoalChanged = {
      operation: change.operation,
      ref: { ...ref },
      ...goal === undefined ? {} : { goal },
    }
    agentEvents(this.ctx, agent).emit('goal/changed', { change: notification })
  }
```

### 2.2 可重建状态

| 状态 | 唯一持有者 | 写入通道 | 读路径 | 失效或重建 | 崩溃后 |
|---|---|---|---|---|---|
| surface | Session 内的 `SurfaceManager` | `append(..., {surfaceOp})`；压缩写 replace | `session.surface`、`deriveMessages()` | 从日志重新折叠 | 重建，无独立丢失 |
| token 计数 | `TokenMeter` 的 WeakMap 与四个投影 | 无独立写入口，usage 随 `assistant/message` | `contextPressure()`、`measure()` | 重放或按 surface 重算 | 重算 |
| compaction 游标 | 日志里的 `compaction/start` / `compaction/end` 与 checkpoint 消息 | compaction 引擎 append | `assertNoActiveCompaction()` 反向扫描 | 更晚的 `session/end-seed` 才作废旧锁 | 锁可能留下 |

压缩游标更像一对括号加一条替换消息。`compaction/start` 与 `compaction/end` 之间如果崩溃，日志里就留下未配对的 start。`assertCompactionInactive()` 只有在发现更晚的 `session/end-seed` 时才认为它属于上一个生命周期，否则抛 busy。`repair.ts` 不关闭这个括号，所以这个会话之后的压缩会一直失败，直到 fork 出新 seed。

```ts
// packages/compaction/compaction-basic/src/region.ts:308-322
function assertCompactionInactive(
  unmatchedCompactionStart: SessionEvent<'compaction/start'> | undefined,
  latestEndSeedSeq: SessionSeq | undefined,
  stage: string,
): void {
  if (unmatchedCompactionStart === undefined
    || (latestEndSeedSeq !== undefined
      && latestEndSeedSeq > unmatchedCompactionStart.seq)) return
  throw new ManualCompactionError(
    'busy',
    `${stage}: compaction already in progress; the session compaction lock is already active`,
  )
}
```

### 2.3 进程内状态

| 状态 | 唯一持有者 | 写入通道 | 读路径 | 失效或重建 | 崩溃后 |
|---|---|---|---|---|---|
| job 注册表 | `LocalJobRegistry` 的 `Map<JobId, TrackedJob>` | `start()` 与 `settle()` | `ctx.jobs`、`job_*` 工具 | 无重建；owner 销毁时取消 | 消失，日志留 registered 与 settled |
| workflow run | `WorkflowEngine` 的 provider；`PtcWorkflowRun` 持 children | 脚本 hook 启动子 agent | `WorkflowRun.result` 与 `workflow/*` | 无重建；dispose 等脚本与子 agent 清理 | run 消失，子 session 在 |
| approval 等待 | `request()` 里挂起的 Promise | 由 `approval/request` 监听者 resolve | 调用方 await | 无重建 | Promise 消失 |
| goal activation | `GoalRuntimeState` 的 WeakMap 字段 | `setActivation()` | 服务工作内存 | `agent/created` 时 disarm | 一律 disarmed |

job 的审计在 append 失败时只 warn。代码注释说明取舍：记录失败不能让已经启动的工作变成孤儿，也不能阻止 waiter 释放。日志因此可能缺一条 settled，job 不会因为记账失败而卡住。

```ts
// packages/jobs/jobs-local/src/index.ts:444-463
  private recordSessionEvent(
    job: TrackedJob,
    settlement?: Pick<JobSettledData, 'finishedAt' | 'status' | 'cause' | 'producerReported' | 'detail'>,
  ): void {
    const owner = job.owner
    if (owner === undefined) return
    const session = owner.session
    const data: JobRegisteredData = {
      jobId: job.id, owner: owner.id,
      kind: job.kind, label: job.label, startedAt: job.startedAt,
      ...job.toolCall !== undefined ? { toolCall: job.toolCall } : {},
    }
    try {
      if (settlement === undefined) session.append('job/registered', data)
      else session.append('job/settled', { ...data, ...settlement })
    } catch (error: unknown) {
      // Recording cannot orphan already-started work or prevent waiter release.
      this.selfCtx.logger.warn(`jobs: failed to record lifecycle for ${job.id}: ${String(error)}`)
    }
  }
```

这张表里有两项共享同一个对象却分属两种寿命。approval 的等待是进程内的，审计对是持久的；goal 的目标是持久的，activation 是进程内的。按对象整体分类会出错，所有权要按字段拆。

### 2.4 所有权按字段拆

approval 和 goal 都说明同一点：对象可以同时持有一段进程内状态和一段持久状态。判断归属时不能问「这个对象归谁」，要问「这个字段归谁、从哪写、崩溃后要不要回来」。

把这条规则用到设计上，会得到一张字段级的表：`GoalRuntimeState.activation` 是进程内，`Goal` 快照是持久；`ApprovalService` 的 pending Promise 是进程内，`approval/asked` 与 `approval/decided` 是持久。两张表分开写，恢复逻辑就不会把授权当成目标的一部分。

## 3. 扩展点全表

扩展点分三层。seam 决定「谁来提供实现」，事件决定「在哪一步拦截或观察」，注册表决定「多出什么条目」。三层都能加代码，能改的东西完全不同。

{{< fig "diagrams/02-extension-layers.svg" "图注：上层是分发，往下依次是注册表、事件、seam。注册表加条目，事件加拦截或观察，seam 换实现；箭头标出每一层允许的改动方向。" >}}

### 3.1 Cordis 服务 seam

| 服务键 | 注册方式 | 生效范围 | 生命周期 | 典型误用 |
|---|---|---|---|---|
| `ctx.llm` | `registerAdapter(providers, adapter)` | 进程全局 | 插件 effect | 在适配器里改消息；按会话注册 |
| `ctx.tools` | `register(definition)` | 全局或 `agent.ctx`，scoped 遮蔽全局 | 返回 disposer | 把单 agent 的工具注册到全局 |
| `ctx.fs` | 注册 provider | 全局 | effect | 在 provider 里做策略，绕过 `fs/*-intent` |
| `ctx.shell` | 注册后端 | 全局 | effect | 直接 spawn，绕过 sandbox 的 argv 包装 |
| `ctx.sandbox` | 注册后端 | 全局 | effect | 在工具层判策略，shell 仍可绕 |
| `ctx.jobs` | 注册表 | 全局，按 owner 隔离 | 随服务；owner 销毁即取消 | 跨 agent 持有 job 引用 |
| `ctx.goals` | 服务 | 全局，按 session | effect | 把 activation 当持久状态 |
| `ctx.agentTeams` | 服务与 task board | 全局，按 Lead session | effect | 直接 append `team/task` |
| `ctx.approval` | `request()` | 全局，按 session 与 turn | 一次调用 | 在 turn 外请求；假设重启后还能批 |
| `ctx.commands` | `register(definition)` | 全局或 agent scope | disposer | 在命令里跑模型轮次 |
| `ctx.sessionPersistence` | 实现抽象类 | 全局后端 | effect | 返回撕裂尾巴；未知格式不拒绝 |

`agent.ctx` 是这套表里唯一能把注册收窄到单个 agent 的机制。工具、命令、skill 都按这个规则分层：scoped 遮蔽全局，限制只做交集。`tools.restrict()` 在全局上下文直接抛错，错误串写明理由：a context-global restriction would mask every agent。

### 3.2 事件

| 族 | 模式 | 能做什么 | 典型误用 |
|---|---|---|---|
| `session/*` | `session/event` 是提交后 emit；turn、step、tool 是持久事件 | 观察、投影、审计；监听器抛错被包含 | 在监听器里 append 同一 session；把它当可 veto 的钩子 |
| `agent/*` | created 与 turn-stopping 是 serial；disposed、status、inbox 是 emit；pre-step、request、request-error 是 waterfall | 改输入、改调用参数、重试、停轮次、续跑 | 在 emit 里长时间阻塞；waterfall 不调 next |
| `fs/*` | write、edit、delete-intent 是 waterfall；observed 是 emit | 写前决策、记录观察 | 在 observed 里做异步；返回 intent 却不调 next |
| `tools/*` | pre-execute、execute、post-execute 三个 waterfall | 拒绝、替换执行、改结果 | 在工具函数里做策略，漏掉 MCP 工具 |

四个族里 `agent/*`、`tools/*` 与 `fs/*-intent` 能改变一次既有调用的结果。`session/*` 是提交后的广播，监听器抛错会被包含，不会让 append 失败；`fs/observed` 要求同步记录器，返回 Promise 不会被 await。选事件前先确认自己要的是 veto 还是记账。

### 3.3 注册表与分发

| 扩展 | 注册方式 | 生效范围 | 生命周期 | 典型误用 |
|---|---|---|---|---|
| 工具 | `ctx.tools.register()`，必须声明 output | 全局或 agent scope | disposer | 占用 `run_code`；同名跨层遮蔽 |
| skill | `ctx.skills.register()` 或 provider | 全局或 agent scope，按 rank 分层 | disposer；重名 runtime skill 只 warn | 以为重名会覆盖 |
| preset | `AgentPreset` 插件，服务行加 `isolate` | 单个 preset 的 realm | 随 preset 挂载 | 服务行漏 isolate，mount 抛错 |
| bundle | `package-bundle`，profile 按序安装 | 整个 profile | 安装顺序 | 当成可 import 的库 |
| client 插件 | package.json 声明 `dsh.client`，导出 `./client` | Web 客户端 | Loader entry 生命周期 | 在 client 里持有 host 状态 |

preset 的 isolate 是最容易踩的注册表错误。registry 在 mount 结束后审计泄漏的服务，一旦发现就抛错。这个错误发生在启动阶段，不会留到运行时。

```ts
// packages/preset/agent-preset-registry/src/mount.ts:211-213
  const leaked = leakedServices(ctx, ctx.fiber)
  if (audit.failed.length > 0) throw new Error(audit.failed.join('\n'))
  if (leaked.length > 0) throw new Error(`Preset services require isolate realms: ${leaked.join(', ')}`)
```

### 3.4 从事件到实现的路径

一次模型调用的分发链把四层串起来。`agent/pre-step` 决定是否进入请求系列，`agent/request` 在 prepareCall 之后调整控制参数，`llm/stream` 是适配器的流式入口，`assistant/message` 把结果写回日志。工具调用走另一条：`tools/pre-execute` 决策，registry 找到实现，`tools/execute` 包住执行，`tools/post-execute` 看结果。

这条链解释了为什么「在工具函数里加策略」会漏。策略挂在函数内部，只覆盖经过这个函数的调用；`tools/*` 事件挂在分发器上，内置工具、MCP 工具、未来新增的 provider 都会经过。同样，`agent/request` 拿不到消息改写权，因为消息在 pre-step 阶段已经定稿；要改输入只能用 `agent/pre-step`。

选层时按「改什么」定位：改实现挂 seam，改一次调用的决策挂事件，增加可被模型选择的能力挂 tools，增加人可触发的入口挂 commands。


## 4. 决策树

把新能力拆成七个问题，每类只有一个正确挂载点，兜底落到进程内服务。判断顺序从上到下：先问它是否面向模型，再问它是否改变已有调用，最后问它是否需要跨重启。

{{< fig "diagrams/03-decision-tree.svg" "图注：左侧七个问题，右侧对应落点，最后一个否分支落到进程内服务。每个落点下面标出不该挂的位置。" >}}

| 需求 | 挂载点 | 不能挂的点 | 原因 |
|---|---|---|---|
| 新模型提供方 | `ctx.llm.registerAdapter` | 工具层、agent loop | 流式协议、重试、能力元数据都在 adapter |
| 新工具 | `ctx.tools.register`，单 agent 用 `agent.ctx` | 系统提示词、命令 | schema 要进注册表才能分发、限制、审批、校验 |
| 新存储后端 | 实现 `SessionPersistence`；宿主状态用 storage domain | 插件自带数据库 | fork、重放、冷读都假设日志是唯一真源 |
| 新 UI 面板 | client module 与 `ConversationNodeDefinition` | host 服务 | host 不渲染 UI；数据要经投影暴露 |
| 新拦截策略 | `agent/*`、`tools/*`、`fs/*-intent` | 包一层工具函数 | 包装只覆盖经过它的调用，漏掉内置与 MCP |
| 新持久状态 | 扩展 `SessionEventMap` 与注册投影 | WeakMap、侧表 | 日志是唯一可重放词汇表；未知事件拒绝 |
| 新调度器 | 宿主服务与 storage domain，照 schedule | agent loop | 需要无会话的时钟与重启恢复 |

### 4.1 工具与命令的分界

面向模型、需要 schema 与执行器的能力挂 `ctx.tools`；由人触发、不需要模型轮次的能力挂 `ctx.commands`。命令注册在 `commands/src/index.ts:285`，返回 disposer；重名错误会提示用该 agent 的 `agent.ctx` 挂 command-injected 子插件。把命令写成工具，模型会多出一个它不该自己调用的入口；把工具写成命令，模型看不到 schema。

### 4.2 新存储后端与旧会话

实现 `SessionPersistence` 的验收标准在一致性，不在「能存」。契约要求 seq 连续、永不重写、撕裂尾巴不返回、未知格式拒绝。做不到这四点，故障会出现在某个历史会话的 fork 或冷读上，而不在新会话上。

### 4.3 新持久状态的代价

扩展 `SessionEventMap` 意味着这个事件要活到所有旧会话里。事件类型必须可 JSON 无损序列化，surface 类型必须声明 `surfaceOp`，投影的 `apply` 要能处理任意历史前缀。只为当前功能方便而加的事件，会在下一次格式升级时变成迁移负担。

### 4.4 一个例子：只读检索工具

假设要给某个研究 agent 加一个只读的 `search_notes` 工具，不污染主 agent。

1. 写 `ToolDefinition`：name、parameters、`output.schema` 与 `output.render` 都必填。`tools.register()` 校验 output，缺了直接抛 `tool "..." must declare output { schema, render, presentationMeta? }`。

2. 在创建该 agent 的插件里用 `agent.ctx.tools.register()` 注册。放全局会让主 agent 也看到 schema，模型可能在不该检索的时候调用。

3. 如果只有部分 preset 需要它，把注册放进 preset 的插件列表，并给服务行加 `isolate`；mount 会审计泄漏。

4. 需要人类也能触发同一能力时，另注册一个 command，不要把 command 塞进工具。命令走 `ctx.commands`，不产生模型轮次。

5. 验证 scope：在同一进程里开第二个 agent，列出 tools，确认它看不到 `search_notes`；再在第一个 agent 里调一次，确认 `tools/pre-execute` 与 `tools/post-execute` 都能看到这次调用。

这个例子的关键是第 2 步与第 3 步：注册位置决定可见性，preset 的 realm 决定服务是否泄漏。两者都不在工具实现里。

## 5. 失效模式矩阵

下面五种失效在 code review 里最难看出来，因为单看写路径都合法。矩阵给的是现象与检测点。

{{< fig "diagrams/04-failure-matrix.svg" "图注：五行失效，三列分别是机制、现象、检测点。最右列是可以在启动、恢复或代码审查时直接执行的检查。" >}}

| 失效 | 机制 | 现象 | 检测 |
|---|---|---|---|
| 重复写 | 两个插件注册同名条目，或两条路径 append 同一语义事件 | 工具被遮蔽；目标轮次翻倍；revision 跳号 | 注册重名错误；`tools/change`；同 turn 两条 `goal/change` |
| 写到错误所有者 | 全局 ctx 注册单 agent 的工具；绕过 journal 写 `team/task` | 工具泄漏给所有 agent；任务板看不到或 revision 冲突 | `scopeOf(ctx)`；`assertAccess`；投影与日志不一致 |
| 错误生命周期持有引用 | 长命插件持有 Agent 或 Session；job 跨 owner 销毁 | job 被取消，reason 为 owner disposed；dispose 后仍写日志 | dispose 顺序；closed session 写入报错 |
| 跨 agent 泄漏 | preset 服务行漏 isolate；scoped 注册写成全局 | mount 抛错；所有 agent 看到同一工具 | `leakedServices` 审计；`scopeOf(ctx)` |
| 崩溃半写 | append 成功 flush 失败；start 无 end；asked 无 decided | 任务可见但未确认；压缩一直 busy；审计对不完整 | `TEAM_TASK_PERSISTENCE_UNCONFIRMED`；`assertNoActiveCompaction`；配对扫描 |

重复写与写到错误所有者经常一起出现。`tools.register()` 在同一层重名直接抛错，跨层则按 scoped 遮蔽全局；`skill.register()` 对同层重名只 warn 并返回空 disposer。两个注册表的严格程度不同，同一个「重名」在工具上是启动失败，在 skill 上是静默保留第一个。

崩溃半写里最贵的是 compaction。`compaction/start` 已经落盘，`compaction/end` 没来，`repair.ts` 不补这个括号，`assertCompactionInactive()` 就把它当成活跃锁。之后任何压缩都抛 busy，只有 fork 产生的 `session/end-seed` 能让它过期。

### 5.1 日志侧能查到的信号

很多失效不用跑起来就能查。拿到一个会话日志，可以按下面的顺序扫：

- 未配对的 `compaction/start`：从尾部反向找最近的 start 或 end，若 start 更近且后面没有 `session/end-seed`，这个会话的压缩锁仍然活跃。

- `approval/asked` 没有对应的 `approval/decided`：崩溃发生在等待期间，审计对不完整。

- `job/registered` 没有 `job/settled`：进程在 job 结算前退出。

- 同一个 turn 出现两条 `goal/change`：可能有两个写入口，或者续跑逻辑被触发两次。

- `team/task` 的 revision 跳号：说明有写路径绕过了 journal 的读-检查-append。

这些检查都只读日志，不需要额外索引。把它们做成启动审计，比等到用户报告「状态不对」再查便宜。

## 6. 自己搭 Agent 时的四条建议

1. 先定日志词汇，再写服务。把需要跨重启的事实逐个列出来，每个事实对应一个事件类型、一个投影、一个写函数。DSH 的会话事件表只有 14 个类型，surface 可接受的只有 5 个；词汇表越小，投影越好写。

2. 每个持久状态只留一个写函数。`Session.append()` 是会话内状态的唯一写通道，`GoalService.commit()`、`ReactLoopInbox.splice()`、`TeamJournal.appendAndFlush()` 都是它的包装。包装层负责校验、序号对齐、flush 确认，绕过包装直接 append 会跳过这些检查。

3. 进程内状态要在 UI 上标出寿命。job 注册表、workflow run、审批等待都不持久，界面需要给它们一个「重启后消失」的标记。把进程内状态画成持久状态，用户会在重启后把正常行为当成 bug。

4. 给锁和半写留恢复路径。compaction 的锁是反例：崩溃后没有自动清理，只能等 fork。自己设计锁时，要么让锁带 TTL，要么在恢复时用一条明确的「上次未完成」事件把它作废，不要让下一次操作永久失败。

## 7. 设计取舍

### 7.1 日志当数据库

DSH 不为查询建第二套存储。projection 是读模型，坏了可以重建；代价是每次读都要折叠，长会话的冷读成本落在恢复路径上。

### 7.2 job 不持久

job 的价值是托管进程，不在跨重启恢复。持久化一个正在跑的进程没有意义，DSH 只持久化它的生命周期事实。代价是重启后无法回答「那个任务跑完没有」，只能看审计尾巴。

### 7.3 goal 授权不持久

目标状态持久、授权进程本地，是一条安全边界。代价是每次恢复都要人类再点一次 resume；收益是崩溃不会让 agent 自己续跑。

### 7.4 compaction 锁不自动清

保守的锁避免在半截摘要上继续压缩，代价是崩溃后会话卡住。DSH 选择让人用 fork 解决，避免自动丢弃未完成的压缩。

### 7.5 approval 不排队

审批等待是一个 Promise，没有队列。进程崩溃后未决请求消失，只剩 asked 审计。代价是不能「重启后接着批」，收益是不会把过期审批当成新请求执行。

## 8. 踩坑点

1. 服务键是 `ctx.approval`，没有 `ctx.approvals`。声明在 `user-approval/src/index.ts` 的 `super(ctx, 'approval')`。

2. `approval.request()` 必须在 open turn 内，否则直接抛错。错误串写明 turn 外的审计对是 crash-tail garbage，恢复时会被当成垃圾尾巴。

3. `tools.restrict()` 只能在 `agent.ctx` 下调，全局调用直接抛。限制的语义是交集，scoped 注册不受影响。

4. preset 的服务行漏 `isolate`，mount 阶段抛 `Preset services require isolate realms`。这个错误发生在启动，不会留到第一次调用。

5. job 的 owner 被销毁，job 会被 `cancelForTeardown(owned, 'owner disposed')` 取消。不要把 job 句柄存到比 owner 更长的插件里。

6. 压缩中途崩溃会留下持久锁，`repair.ts` 不补；只能 fork。排障时先扫未配对的 `compaction/start`。

7. `agent/request` 已标记 deprecated，改输入用 `agent/pre-step`。前者保留给既有监听器，新代码不应再加监听。

8. `session/end-seed` 只有构造器与 fork seed 能写。插件补这个事件会把此前的 turn 误判成继承历史，`interruptedTurnClosers()` 不再补它。

## 9. 源码索引

| 文件 | 看什么 | 行 |
|---|---|---|
| `docs/architecture.zh.md` | 新行为归属表 | 148-174 |
| `docs/glossary.zh.md` | seam、scope、turn/step | 全文 |
| `packages/core/session/src/types.ts` | 14 个持久事件与 surface 类型 | 287-452 |
| `packages/core/session/src/index.ts` | append 契约与 surface 读取 | 531-533, 776-800 |
| `packages/core/agent/src/runtime-types.ts` | agent/* 事件 | 290-429 |
| `packages/core/agent-loop/src/inbox.ts` | inbox 投影与 splice | 20-60 |
| `packages/goal/goal/src/index.ts` | commit 与 activation | 160-172, 609-625 |
| `packages/jobs/jobs-local/src/index.ts` | 进程内 job 与审计 | 444-463, 657 |
| `packages/team/agent-team/src/journal.ts` | appendAndFlush | 62-76 |
| `packages/compaction/compaction-basic/src/region.ts` | 压缩锁 | 308-322 |
| `packages/interaction/user-approval/src/index.ts` | 审批审计与请求 | 90-121, 242-260 |
| `packages/schedule/schedule/src/runtime.ts` | 重启后找回投递 | 309-345 |
| `packages/core/tools/src/index.ts` | register 与 restrict | 128-162 |
| `packages/preset/agent-preset-registry/src/mount.ts` | isolate 审计 | 211-213 |
| `docs/subsystems/client-modules.zh.md` | dsh.client 扫描 | 94 |
| `packages/workflow/workflow/src/index.ts` | holder-owned 运行态 | 157-170 |
