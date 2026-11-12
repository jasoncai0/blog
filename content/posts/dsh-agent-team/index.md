---
title: "Agent Teams：共享任务板与委派策略"
date: "2026-11-11T17:20:00+08:00"
summary: "DSH 的 Team 只在既有 subagent 树上加一块持久任务板：4 个任务状态、8 个动作、每任务 revision CAS、每棵树的串行事务队列。换来重启后仍能回答「谁认领了哪块活」，代价是没有 lease、没有超时、没有自动回收。"
description: "拆解 DSH 的 team 子系统：任务板为什么存快照日志、claim 的原子性从哪来、成员失败后谁收拾，以及 team 与 subagent 的职责边界。"
tags: ["multi-agent", "architecture", "planning"]
slug: "dsh-agent-team"
---
> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准；包内 `lib/` 是构建产物，引用一律指向 `src/`。
> 这是系列的第 15 篇。系列目录：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)。

第一次给多代理系统加任务板，多数人会写一张表：`id, title, status, assignee`。

跑起来很顺，直到进程重启。表里有一行 `status=running, assignee=agent-3`，而 agent-3 的会话已经不存在了。

你是把它改回 `pending`，还是交给某个「恢复流程」？改回去，原来那个 agent 万一还活着就会重复干活；不改，任务永远卡住。

再往下走还有一串类似的问题。两个成员同时看到一条 `pending`，谁先 claim？一个成员 claim 之后死了，他的任务算谁的？任务做完了，谁负责把结果并进主线、什么时候算整棵树收工？

多代理系统里，难的一直是这些归属与收敛问题，「开一个子代理」反倒简单。

DSH 的答案很小：不给 team 发明新的成员、新的会话、新的消息通道，只在已有的 subagent 树上叠一块持久任务板。这篇文章拆这块板子，以及它刻意留下的那些空洞。

## 0. 结论

Team 由两样东西组成：一棵 subagent 树，一条任务事件日志。

任务以完整快照追加进根会话日志，用 `revision` 做 compare-and-set，用每棵树一条 promise 队列串行化读改写。

它不共享对话历史，不自动回收 owner，也不判断整棵树什么时候算完。

持久化只保证重启之后事实还在。「谁来接手」始终是 Lead 的判断，运行时不做这种猜测。

## 1. Team 没有成员表：根会话就是组织

`TeamId` 在类型上是一个 brand 过的 `SessionId`（`packages/team/agent-team/src/types.ts`）。

projection 初始化时直接写 `id: TeamId(rootId)`（`projection.ts:72`）。

所以 team 的身份是「谁的根会话」，而不是一张单独签发的组织 ID。根会话活着，这棵树就在。

这解释了几个看起来奇怪的设计。

成员不由 Team 创建。`TeamService` 的 `inject` 列表里是 `agents`、`sessions`、`sessionPersistence`、`sessionProjections`、`subagents`（`index.ts`），里面没有成员管理器。

成员就是 subagent：`subagent` 工具新建一个全新会话，`subagent_fork` 新建一个继承已完成轮次的会话，返回的名字（如 `reviewer`）用于寻址。

team 只认这些会话的 id。

{{< fig "diagrams/01-team-architecture.svg" "图注：根会话同时是 Team 身份与任务日志的宿主；任务板由根日志投影而来，成员是各自持有会话日志的 subagent；共享的只有任务事实与工作区文件。" >}}

图里画了两条不交叉的线。

任务事实从根会话日志流向任务板投影，再流向浏览器；成员的身份、状态、收件箱则由 subagent runtime 单独维护。

两者只在「任务归属写的是某个成员 id」这一点上相交。

`isEnabled` 也体现了这个结构：它先 `rootOf(caller)` 找到根，再读根会话的 projection（`index.ts:94`）。

所以团队模式没有「每个成员各自开关」这回事，后代共享根的选择。

切换模式也只有根能发起，非根调用 `setEnabled` 直接报 `TEAM_LEAD_REQUIRED`。

选择变化才追加一条 `team/mode`，追加后立刻生效，然后才等 `sessions.flush`（`index.ts:112`）。

先让模型看见，再谈落盘，顺序不能反。

类型里还留着一处历史包袱。

`TeamMemberPhase = 'provisioning' | 'active' | 'failed'` 仍然留在类型里，但它只服务历史名册的解码。

当前实现不维护成员阶段，成员的生命周期完全由 subagent runtime 决定。

少一份名册，就少一份会和真实会话状态打架的缓存。

## 2. 任务板存的是一串快照

任务变更写进根会话日志，事件名 `team/task`，载荷是 `{ version: 2, teamId, task }`，其中 `task` 是变更后的完整快照。

快照的形状决定了这块板子能表达什么：

```ts
interface TeamTaskSnapshot {
  id: TeamTaskId            // task-<n>，每棵树自增
  revision: number          // 每次变更 +1，CAS 的比较值
  subject: string
  description: string
  status: 'pending' | 'in_progress' | 'completed' | 'deleted'
  ownerId?: SessionId       // 成员会话 id
  ownerName?: string        // 展示名，来自 subagents.namesOf
  blockedBy: TeamTaskId[]   // 依赖，必须是未删除任务且无环
  writeScopes: string[]     // 规范化路径前缀，提示性
}
```

关键在 `revision` 与完整快照的组合。

如果事件只记增量（`{op: 'claim', owner: 'x'}`），读的一方就必须从任意一个历史点开始重放，才能知道当前值。

完整快照让「当前值」独立于重放顺序存在，代价是同样的 subject/description 会被反复写进日志。

`journal.appendAndFlush` 是唯一出口（`journal.ts:62`）。

它做两件事：把 `team/task` append 到根会话，然后 `await this.ctx.sessions.flush(root.session)`。

append 只保证事件进了内存日志，flush 才等持久化参与者确认。

没有参与者、或者 flush 失败，就抛 `TEAM_TASK_PERSISTENCE_UNCONFIRMED`，错误消息里带任务 id 与 revision，并明确要求「重试前先读」。

存完整快照之外，revision 还得连续，原因在 projection：它只做一件事，按 `revision` 连续追加（`projection.ts:133` 附近）。

首见必须是 1，之后每次必须等于前值的 `+1`，否则整块 projection 置 `failure`，任务读写被拒。

这让「日志里出现了一个跳号」变成显式故障，而不是悄悄算错。

`deleted` 是墓碑，不做物理删除。

`list` 与 projection 都会过滤它，但日志里保留，因为依赖它的任务可能还存在。

删除一个被依赖的任务会直接报 `TEAM_TASK_HAS_DEPENDENTS`。

视图层只在非删不可的地方保留墓碑：`buildTeamProjection` 过滤 deleted，再对每条剩余任务算 `ready` 与 `writeScopeWarnings`。

这两个字段都不落盘，纯函数派生（`task-view.ts`）。

## 3. 两种关系分开记：依赖图与写范围

任务之间有两条完全不同的关系，DSH 把它们分开表达。

第一条是 `blockedBy`，表示「必须先完成谁」，参与就绪判定，有硬校验。

第二条是 `writeScopes`，表示「我打算改哪些路径」，只产生重叠警告，没有强制力。

`blockedBy` 在写入前要过一张图。

`dependencies()` 做三件事：自引用直接报 `TEAM_TASK_DEPENDENCY_CYCLE`，重复项报 `TEAM_INVALID_ARGUMENT`，指向不存在或已删除的任务报 `TEAM_TASK_NOT_FOUND`。

然后 `assertTaskGraph` 对整张候选图跑一次环检测（`task-graph.ts:26`）。

自阻塞的错误文案是 `cannot block itself`，环路的文案是 `task dependency cycle includes "<id>"`。

这三类校验属于任务板能用的前提，不是防御性编程。

只要允许环，`ready` 就永远为假，任务永远 claim 不到，而系统里没有任何东西会报错。

允许指向已删除的任务，同样会让一条链静默死掉：blocker 的墓碑被视图过滤，后置任务看起来在等一个不存在的 id。

`writeScopes` 则是刻意的弱约束。

它做规范化（去重、路径前缀对齐），然后在视图里和其它 `in_progress` 任务的 scope 比较，重叠就写一条 `write scopes overlap with task-7`（`task-view.ts`）。

README 把它列为 advisory，因为 Bash、formatter、代码生成器都能绕过路径前缀。

这个设计有明确的理由：硬锁需要跨进程、需要持有者存活、需要崩溃后释放，而 DSH 的 team 是单进程的。

弱约束至少不会制造「以为自己被保护」的错觉，前提是读的人真的读了警告。

| 关系 | 字段 | 校验 | 违反后果 | 谁负责 |
| --- | --- | --- | --- | --- |
| 顺序 | `blockedBy` | 无环、不重复、指向未删除任务 | 写入被拒，错误码明确 | 任务板 |
| 写冲突 | `writeScopes` | 规范化、去重、重叠警告 | 只警告，不阻止 | Lead 与 reviewer |

## 4. 为什么必须持久化：重启之后谁认领

先给结论：持久化不能让重启后的认领变简单，它只是让「谁认领了」这个问题有答案可查。

任务事件落在根会话日志里，而根会话由 `sessionPersistence` 负责。

进程重启后，projection 从日志重建，你会看到一个 `in_progress` 任务，`ownerId` 指向某个会话 id。

那个会话可能已经冷掉、失败、甚至从未被恢复。

运行时不认识「过期 owner」这个概念，因为任务上没有 `leaseUntil`、没有 `heartbeatAt`、没有 `attempt` 计数。

它唯一能说的就是：这条任务归这个 id。

这看起来像缺陷，换一个角度却是责任边界。

谁有权改任务？`task-board.ts` 的 `update` 里只有两个身份：`lead = caller === root`，`owner = current.ownerId === caller.id`；其余一律 `TEAM_TASK_UNAUTHORIZED`（`task-board.ts:135` 附近）。

如果运行时能凭「我觉得他死了」就回收任务，它就必须回答两个问题：凭什么判断死了，回收后原 owner 又活了怎么办。

DSH 把这两个问题留给 Lead：你观察到成员失败，就用 `release` 把任务放回 `pending`，或用 `reassign` 换人。

> 如果自己搭：把「持久化」和「回收」分成两个决定。持久化只保证事实不丢；回收一定要有一个明确的授权者（Lead、调度器或人），并且回收动作本身要写进同一条日志。最糟的组合是「任务持久化 + 运行时按超时自动改 owner」：重启后没人说得清上一次自动回收是第几次尝试。

还有一个细节决定了这条路径能不能用：persistence 失败不能「当没发生」。

事件已经 append，任务对模型可见；`flush` 失败只说明它可能没落盘。

此时抛错并让你先读，是在避免「重试一次 create」造出两条一样的任务。

同样的道理适用于模式开关：`setEnabled` 先 append 再 flush，但它在 append 之后就立即生效。

如果 flush 失败，模式已经切了，事件可能没落盘；重试时因为「选择没变」不会再追加，于是不会出现两条 `team/mode`。

这两个例子的共同点是可见性先于持久性，错误必须显式。

## 5. claim 的原子性：单写者队列 + revision CAS

`claim` 要做三件事：确认任务还没被别人拿走、确认依赖都完成、把自己写成 owner。

这三步必须在同一个临界区里，否则两个成员会同时通过检查。

DSH 的临界区没有用数据库事务，用的是按根会话 id 串起来的一条 promise 链。

`journal.transact(rootId, operation)` 维护 `tails: Map<SessionId, Promise<void>>`，同一棵树的读-检查-追加排在上一次之后（`journal.ts:44`）。

`create` 与 `update` 都整段包在 `transact` 里，所以对同一块板子，运行时内不存在并发读改写。

第二道保险是 `revision`。

```ts
if (current.revision !== request.expectedRevision) {
  throw new TeamError(
    `stale team task "${current.id}" revision ${request.expectedRevision}; ...`,
    'TEAM_TASK_STALE_REVISION',
  )
}
```

这段来自 `task-board.ts` 的 `update`（:128 附近），它是 CAS 的第一道门。

模型侧的工具 schema 把 `expected_revision` 设成必填。

`team_task_update` 的描述写得很直白：「Compare-and-set a shared task action using the latest revision」（`packages/team/tool-agent-team/src/index.ts:197`）。

也就是说，模型必须先 `team_task_get` 读一遍，再带着那个 revision 来改。

第三道是 claim 自己的前置条件（`task-board.ts:142` 起）。

`ownerId` 存在且不是自己 → `TEAM_TASK_ALREADY_CLAIMED`。

`status` 不是 `pending`，或 `taskReady(state, task)` 为假（任一 blocker 未 `completed`）→ `TEAM_TASK_BLOCKED`。

通过后写入 `status: 'in_progress'`、`ownerId: caller.id`，再从 `ctx.subagents.namesOf(caller).self` 取一个人类可读的名字塞进 `ownerName`。

{{< fig "diagrams/02-task-state-machine.svg" "图注：任务的 4 个状态与 8 个动作；虚线是「owner 死亡」这条没有迁移的路径，状态原地不动，只能由 Lead release 或 reassign 才回到可认领。" >}}

图里那条虚线标着关键的一处。

成员失败不会触发任何迁移，`in_progress` 就是 `in_progress`。

「重新入队」在 DSH 里对应两个显式动作：Lead 用 `release` 清掉 owner、状态回 `pending`；或者 Lead 用 `reassign` 直接换一个 owner（省略 owner 时等于清空并回 `pending`）。

`reassign` 只有 Lead 能做，`release` 则 owner 与 Lead 都能做。

claim 的原子性有前提：单进程、单写者。

跨进程就不成立，没有文件锁、没有数据库行锁、没有 fencing token。

README 的 Known limitations 第一条就是 One process，明确不支持 worktree、远程成员、merge 与文件锁。

这条限制写在 README 的 Known limitations 里，是范围声明。

> 如果自己搭：先决定你的并发模型是「单进程单写者」还是「多写者」。单写者，一条按聚合根串行的队列加 revision CAS 就够了，别引入分布式锁；多写者，CAS 必须落到共享存储（唯一约束、条件更新、或者带 fencing token 的 lease），且每次重试都要重新读。把这两种情况混在一套 API 里，最后一定会在「本地测试永远通过、部署后偶发双 claim」上翻车。

## 6. 一次委派的完整时序

把上面的机制串起来，一次典型的两成员委派长这样：

| # | 根 | 成员 A | 成员 B | 任务板 |
| --- | --- | --- | --- | --- |
| 1 | 创建 task-1，写清 subject/description | | | pending，无 owner |
| 2 | 创建 task-2，`blockedBy: [task-1]` | | | pending，未就绪 |
| 3 | 起 A、B 两个 subagent | 启动 | 启动 | 不变 |
| 4 | 提示两个成员「去板子上认领」 | `team_task_list` | `team_task_list` | 两条可见 |
| 5 | | claim task-1（rev 1） | | in_progress，owner=A |
| 6 | | | claim task-1（rev 1） | 拒绝：ALREADY_CLAIMED |
| 7 | | | `team_task_list` 看到 task-2 未 ready | 不变 |
| 8 | | 干活、提交、complete（rev 2） | | completed |
| 9 | `wait_agent` 收 A 的结果 | 返回 | | 不变 |
| 10 | | | `team_task_list` 看到 task-2 ready | ready=true |
| 11 | | | claim task-2（rev 3） | in_progress，owner=B |
| 12 | `wait_agent` 收 B 的结果 | | complete（rev 4） | completed |
| 13 | 复核 diff、跑测试、给最终答复 | | | 全部 completed |

这张表里有三个容易被忽略的细节。

第一，第 6 步的拒绝是正常的并发控制，不是错误状态。成员 B 应该重读任务，而不是重试同一个 revision。

第二，第 9 步与第 12 步的 `wait_agent` 是根唯一能拿到成员结果的方式。任务 `completed` 本身不携带结果，结果在成员的返回里。

第三处不同：第 13 步没有对应的任务动作。复核、合并、收尾都是根自己的活，任务板不建模。

再看失败变体：假设第 8 步 A 在 complete 之前失败。

任务停在 `in_progress`，owner 仍是 A。

根的 `wait_agent` 拿到失败结果，于是执行 `team_task_update`，`action: 'release'`，`expected_revision: 2`。

任务回到 `pending`，owner 清空，revision 变 3。

如果根想直接换人，用 `reassign` 指定 B；如果根想自己接手，`reassign` 的 owner 写自己。

如果根判断这条任务本身没意义了，就 `delete`，但要注意有没有别的任务在 `blockedBy` 里等它。

## 7. 成员失败之后：没有 lease，只有 Lead 收拾

把上一节的动作连起来，失败处理长这样：

1. 成员在 `in_progress` 状态下失败或被杀，任务不动。
2. 根通过 `wait_agent` 的结果、subagent 的失败回执，或者 `list_agents` 看到成员不在了。
3. 根对任务执行 `release`（owner 或 Lead 可做）或 `reassign`（仅 Lead），任务回到 `pending` 或换人。
4. 下一个空闲成员 `claim`。

{{< fig "diagrams/03-member-lifecycle.svg" "图注：上半是 subagent 自己的生命周期，下半是任务归属的寿命；两条线不同步，成员 disposed 之后任务仍然记着它的 id，直到 Lead 显式改写。" >}}

这张图想说明的是「归属寿命 ≠ 成员寿命」。

subagent 的生命周期由 runtime 管：创建、跑、空闲、被 followup 唤醒、失败、销毁。

任务归属只会被任务动作改写。

两者之间没有自动联动，因为一旦联动，任务状态就变成成员状态的函数，而成员状态是易变的、进程本地的、可能丢的。

这里有个容易误解的点：subagent 失败并不等于会话消失。

成员可能只是这一轮失败，会话还在，下一次 followup 还能继续；也可能被冷存，之后恢复。

任务 owner 写的是会话 id，所以「成员暂时不活动」和「成员永远不来」在任务板上长得一模一样。

Lead 要根据对话结果判断，而不是根据状态字段猜。

> 如果自己搭：给任务加 `attempt` 与 `last_error` 这类字段之前，先想清楚谁来写它们。让 owner 自己写，死掉的 owner 写不了；让调度器写，它就得持有判断权。更稳的做法是把「尝试」放在任务之外（执行记录、job 记录），任务只保留归属，这样重试策略可以换，归属语义不用动。

## 8. 为什么不共享对话历史

Team 里每个成员都有自己的会话日志。

父会话看不到成员的中间消息，只看到工具结果与成员最终的返回。

成员之间也不共享上下文，要传话就 `send_message`，要并行探索就各自开新会话。

原因有三层。

最上面一层是日志所有权。DSH 的不变量是「模型看见的，必须能从日志重建」。

每个会话日志只记录自己模型看见的东西；如果两个会话共享一段历史，回放时就得做跨日志合并，合并顺序、裁剪时机、谁看见哪一版都变成新的状态。

第二层是成本。团队里五个成员各自跑几十轮，把全部历史塞进每个成员的上下文，token 会先于任务爆炸。

第三层是权限。`docs/glossary.zh.md` 明确说，带作用域的注册不会向下继承给 subagent，子树行为通过 lineage 数据表达，从不通过 scope 结构。

也就是说，DSH 里「同属一棵树」从来不是一种可见性授权。

team 共享的是任务板与工作区。

任务板给「做什么、谁做、依赖谁」，工作区给「做到哪了」。

这两样都是外部事实，成员通过工具读写，不经过对话。

`subagent_fork` 是唯一的例外通道：它把父会话已完成的轮次作为种子，显式复制给子会话，复制发生在创建时，之后两边独立。

> 如果自己搭：不要用「共享上下文」实现协作。把协作状态外化成可读写的对象（任务、文件、工单），让每个 agent 在自己的上下文里重新读一遍。共享上下文省下的那点重复阅读，会在压缩、并发写、权限和回放上连本带利还回去。

## 9. 两种策略的分层：任务策略 vs 模型策略

Team 的工具分成两组。

一组是普通 subagent 控制工具：`subagent`、`subagent_fork`、`send_message`、`wait_agent`、`list_agents`、中断。

它们永远可用，负责身份、发现、通信与等待。

另一组是 Team 工具：`team_task_create`、`team_task_list`、`team_task_get`、`team_task_update`。

它们只在 `isEnabled(agent)` 为真时注册（`when` 条件），负责任务策略。

任务策略写在 `TEAM_POLICY` 里，作为系统提示的一个 section 注入（`tool-agent-team/src/index.ts:27`）。

原文是这样的：

> Task owners are agent names from list_agents. The root can reassign tasks to itself or any continuable descendant; other agents claim ready tasks themselves.
> Split write work into disjoint scopes, record expected write scopes on shared tasks, and use task dependencies when work must be ordered. Write-scope overlap is advisory, not a lock.
> Shared-task workflow is list, get, claim with the current revision, perform the work, then complete. Task readiness never starts an owner. The root agent must wait for required teammates before giving the final answer.
> Task changes do not end wait_agent calls, add inbox messages, or start idle agents. Use send_message when another agent needs to act on a task.

四句话对应四件事：谁能改、怎么切、怎么走流程、什么时候叫醒人。

最后一句说的是：任务变更不产生任何唤醒。

这是刻意的，任务板不是消息队列。

与此并列的是模型策略，它根本不在这块板上。

任务没有 `model`、`provider`、`reasoning_effort` 字段，因为这些属于「创建一个成员」时的选择：用哪个 provider、哪个模型、多深的推理。

由 subagent 创建参数决定，由 subagent runtime 执行。

Team 不参与，也不应该参与。

一个任务可以被任何成员认领，而不同成员可以用不同模型；把模型写进任务，反而会把「谁做」和「用什么做」绑死。

{{< fig "diagrams/04-team-vs-subagent.svg" "图注：左列是 subagent runtime 的职责，右列是 team 的职责，中间只有任务归属与工作区两处交接；模型选择在左列，任务语义在右列，互不越界。" >}}

> 如果自己搭：把「组织结构」和「执行配置」分成两层。组织层只存任务、依赖、归属、可见范围；执行层（模型、工具白名单、沙箱、预算）在创建执行者时决定。这样换模型不用改任务，拆任务不用动配置，两边的变更频率也对得上。

## 10. 收敛判定：没有「team 完成」这个事件

最后一个问题：什么时候算 team 完成。DSH 没有这个判定。

没有 `team/complete` 事件，没有「所有任务完成自动解散」，也没有「任务清空就唤醒 Lead」。

`TeamProjection` 只有 `enabled`、`tasks`、`failure` 三个字段。

`ready` 是逐任务的（pending 且 blockers 全 completed），不是全局的。

收敛是根模型的责任，靠一句策略约束：「The root agent must wait for required teammates before giving the final answer.」

它得自己 `team_task_list` 看还有没有未完成任务，`wait_agent` 收齐成员结果，判断哪些任务真的做完，然后给最终答复。

系统提供的只有事实：任务状态、依赖是否满足、成员是否还在。

不做自动收敛的原因很直接：「完成」不是一个可以由状态字段推导的谓词。

任务都 `completed`，结果未必并进了主线；成员都 idle，也不代表没有下一步。`maxTasks` 只是上界，不是完成信号。

自动判定会制造一个看似权威、实际经常错的结论。

DSH 选择把结论留给能读结果的人。

一个可操作的收尾清单：

- 列出所有未删除任务，确认没有 `pending` 或 `in_progress`。
- 对每个 `completed` 任务，确认结果已经在工作区或对话里被消费。
- 处理所有失败成员名下的任务：`release`、`reassign` 或 `delete`。
- 检查 `writeScopes` 重叠警告，确认没有互相覆盖的改动。
- 跑一次构建与测试，而不是相信任务状态。
- 最后才给最终答复。

这份清单看着笨，但它把「状态」与「事实」分开了。

任务板负责前者，根负责后者。

## 11. 设计取舍

| 取舍 | 选了什么 | 放弃什么 | 什么场景该反悔 |
| --- | --- | --- | --- |
| 组织身份 | 复用根 SessionId（`TeamId` 是 brand） | 独立的组织/成员 ID 空间 | 需要跨进程、跨机器共享一块板时，会话 id 不再是全局唯一事实 |
| 成员权威 | 用 subagent 树做唯一名册 | 第二份成员表、成员阶段、成员容量 | 需要「成员角色」参与权限判断（如 reviewer 才能 approve）时，得另建权限层 |
| 任务状态 | 追加完整快照 + revision 单调 | 增量事件、可变行、就地更新 | 任务字段很多、日志体积成为瓶颈时，快照会放大写入 |
| 并发 | 单进程按根串行 + CAS | 跨进程锁、lease、fencing | 多写者部署时，必须把 CAS 落到共享存储 |
| 归属回收 | 无自动回收，Lead 显式 release/reassign | 超时回收、心跳、自动重试 | 成员数量大、失败频繁、Lead 来不及收拾时，需要外部 supervisor |
| 写范围 | `writeScopes` 只做重叠警告 | 真锁、路径预留、merge | 多成员会改同一批文件时，警告不够，得靠分支/worktree 或串行化 |
| 通知 | 任务变更不唤醒、不入收件箱 | 任务板当消息总线 | 需要「有人做完就自动接力」时，得显式 `send_message` 或改调度层 |

这张表里最容易被低估的是最后一行。

很多人对任务板的期待是「像看板一样，卡片一动就通知相关人」。

DSH 明确拒绝了这个类比：任务板是状态存储，通知是另一条通道。

两者混在一起，任务写入就得承担投递语义，投递失败又得回滚任务，最后既不像数据库也不像队列。

反过来，最容易被高估的是 `writeScopes`。

它只是把「我打算改这些路径」写进任务，让同板的其他任务在视图里出现一条警告。

Bash、formatter、代码生成器都能绕过它，所以 README 把它列为 advisory。

它的价值在协调，不在隔离。

还有一处要展开：无自动回收。

表面上看，加一个 `leaseUntil` 就能解决死 owner 的问题。

但 lease 需要时钟、需要续租、需要处理「续租请求还在路上，任务已经被别人拿走」的窗口。

在单进程里，这些复杂度换来的只是「不用 Lead 动手」；而 Lead 本来就要读成员结果，顺手 release 一次的成本很低。

在多进程里，lease 又不够，还得有 fencing token 防止旧 owner 的写入生效。

DSH 把这个选择留给了部署者。

## 12. 踩坑点

1. 把 claim 当锁用。现象：两个成员都觉得自己拿到了任务。原因：第二个成员的 `expected_revision` 已经过期，但调用方吞掉了 `TEAM_TASK_STALE_REVISION` 继续干活。对策：把 stale 当正常分支处理，重读任务后重新决策，不要重试同一个 revision。

2. 用 `writeScopes` 防并发写。现象：两个任务声明了不相交的目录，实际还是互相覆盖。原因：scope 只是提示，Bash 与格式化工具不受约束。对策：把 scope 当 review 清单，要隔离就靠 worktree、分支或串行执行。

3. 成员失败后等任务自动回队。现象：任务永远停在 `in_progress`，新成员 claim 不到。原因：没有 lease、没有超时、没有自动释放。对策：根在收到成员失败结果时立即 `release` 或 `reassign`，把这一步写进自己的收尾清单。

4. 指望任务完成唤醒空闲成员。现象：任务 `completed` 了，下游成员一直不动。原因：任务变更不加收件箱、不结束 `wait_agent`、不启动 idle。对策：需要行动就 `send_message`，把「通知」当独立动作。

5. 关掉 team 模式以为任务清空。现象：重新打开后旧任务还在。原因：关闭只隐藏后续 step 的 Team 工具，任务与 subagent 都保留。对策：把模式当「工具可见性」，不是「数据生命周期」；要清理就显式删除任务。

6. 把 `TEAM_TASK_PERSISTENCE_UNCONFIRMED` 当失败重试。现象：重试创建后出现两条一样的任务。原因：append 已成功、flush 结果未知，任务其实已经存在。对策：捕获这个错误后先 `team_task_get`，确认没有才重试。

7. 在 fork 出来的子会话里期待继承 team 模式。现象：子会话看不到 Team 工具。原因：新会话默认关闭；被独立恢复的子代理没有活的根提供模式时，`isEnabled` 直接返回 false。对策：需要团队能力就在根上开启，并保证根会话活着。

8. 以为任务板跨进程。现象：两个 DSH 进程各有一块板，互相看不见。原因：任务存在各自的根会话日志里，没有共享存储。对策：单机单进程内用 team；要跨进程就自己实现共享任务存储，别指望这层。

## 13. 六个反模式

把上面的边界反过来看，就是六种常见的错误建模。

反模式一，任务板当消息队列。任务状态变化不产生投递，等待也不会被任务结束。需要接力就显式发消息，否则整条流水线会在「完成了但没人知道」处停住。

反模式二，把 `writeScopes` 当锁。它只在视图里产生警告，任何不经过任务板的写入都能绕过。要真隔离就用工作区隔离，要协调就靠 review。

反模式三，自动回收 owner。没有时钟、没有租约、没有 fencing 的自动回收，会在重启与并发下产生双 owner。宁可让 Lead 显式收拾，也不要一个「大多数时候对」的回收器。

反模式四，把模型选择写进任务。任务描述「做什么」，成员描述「谁来、用什么模型」。混在一起后，换模型要改任务，重试要重建任务，依赖关系也会被模型差异污染。

反模式五，让 team 拥有成员生命周期。一旦 team 负责创建/销毁成员，它就得复制 subagent 的容量、冷恢复、结算与中断语义。DSH 的 team 不创建 agent，这是它能保持薄的原因。

反模式六，把「任务全完成」当收敛。任务完成只说明成员提交了结果，不说明结果被消费、被验证、被合并。收敛判定必须读结果，不能只读状态。

## 14. 源码索引

| 位置 | 证明什么 |
| --- | --- |
| `packages/team/agent-team/src/types.ts` | `TeamTaskStatus` 只有 4 态、`TeamTaskSnapshot` 带 `revision`、`TeamProjection` 只有 `enabled/tasks/failure` |
| `packages/team/agent-team/src/task-board.ts:117` | `update` 的入口就是一次 `journal.transact`，整段读改写串行 |
| `packages/team/agent-team/src/task-board.ts:128` | revision 不等即 `TEAM_TASK_STALE_REVISION`，CAS 的第一道门 |
| `packages/team/agent-team/src/task-board.ts:142` | `claim` 的前置条件：无主、pending、`taskReady` |
| `packages/team/agent-team/src/task-board.ts:188` | `reassign` 仅 Lead，省略 owner 时清空回 pending |
| `packages/team/agent-team/src/task-board.ts:226` | 所有动作最后统一 `revision + 1` 再 `appendAndFlush` |
| `packages/team/agent-team/src/task-graph.ts:26` | 依赖图的三态校验：缺失、重复、环 |
| `packages/team/agent-team/src/journal.ts:44` | 按根会话 id 的 promise 链，`tails` map 是临界区 |
| `packages/team/agent-team/src/journal.ts:62` | append 后 `flush`，失败或无监听器抛持久性未确认 |
| `packages/team/agent-team/src/projection.ts:133` | revision 必须连续，跳号即 `failure` |
| `packages/team/agent-team/src/projection.ts:152` | `enabled ?? true`：历史任务在无显式选择时视为开启 |
| `packages/team/agent-team/src/index.ts:94` | `isEnabled` 从根会话 projection 读，后代共享 |
| `packages/team/agent-team/src/index.ts:112` | 只有根能 `setEnabled`，追加 `team/mode` 后立刻生效、再 flush |
| `packages/team/agent-team/src/task-view.ts` | `ready` 与 `writeScopeWarnings` 都是纯函数派生，不落盘 |
| `packages/team/tool-agent-team/src/index.ts:27` | `TEAM_POLICY` 四段话，定义任务策略与唤醒边界 |
| `packages/team/tool-agent-team/src/index.ts:197` | `team_task_update` 强制 `expected_revision`，模型侧 CAS |

## 15. 收尾

回到开篇那张表。

DSH 把表拆成了两样东西：一条只追加的事件日志，一套只认 owner 和 Lead 的动作。它没有把表做得更聪明。

日志让「谁认领了」在重启后有答案；动作让答案只能被有权限的人改写。

中间那些自动化的诱惑（超时回收、心跳续租、完成即通知）都被明确留在了外面。

如果只留一条结论：多代理的任务板首先是责任记录，其次才是调度工具。

先把归属、依赖、权限写清楚，再谈自动收敛；顺序反了，系统会在第一次成员失败时露馅。

上一篇：[14 子代理控制：等待、中断与收件箱](/posts/dsh-subagent-control.html) ｜ 下一篇：[16 循环之外：Workflow、Jobs、Goal、Schedule](/posts/dsh-workflow-jobs-goal.html) ｜ [系列目录](/posts/dsh-architecture-anatomy.html)
