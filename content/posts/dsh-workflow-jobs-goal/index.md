---
title: "循环之外：Workflow、Jobs、Goal、Schedule"
date: "2026-11-11T18:00:00+08:00"
summary: "Agent loop 只覆盖一个轮次。长任务需要四种循环外机制：workflow 脚本编排子代理、jobs 托管后台进程、goal 用状态驱动续跑、schedule 用宿主时钟触发。它们各自记账：成员数、进程数、轮次数、触发次数。"
description: "拆解 DSH 的四种循环外机制：各自的触发源、状态存放、失败恢复与记账单位，以及为什么 goal 必须 disarm、workflow 脚本不能碰文件系统。"
tags: ["harness", "planning", "architecture"]
slug: "dsh-workflow-jobs-goal"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准；包内 `lib/` 是构建产物，引用一律指向 `src/`。
> 这是系列的第 16 篇。系列目录：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)。

一个 agent 跑完一轮，停下来等用户。这是最常见的形态，也是所有 harness 最先写对的部分。

但你很快会碰到四种「不该等」的情况。

第一种：任务足够大，需要先派五个子代理各查一块，再汇总。你希望模型写一段编排逻辑，而不是在对话里逐步调度。

第二种：一条命令要跑二十分钟。你不希望它占住当前轮次，甚至不希望它占住当前会话。

第三种：目标很明确，但一轮做不完。你希望它在每次空闲时自动继续，直到完成或明确卡住。

第四种：任务不由人触发，而由时间触发。每天早上九点，或者每过六小时。

这四种需求看起来都是「让 agent 继续跑」，DSH 却给了四套完全不同的机制：workflow、jobs、goal、schedule。

它们共享的只有一点：都不在 agent loop 里。触发源、状态存放、失败恢复、记账单位全都不一样。混用会出事，而且出得很隐蔽。

## 0. 结论

Workflow 管的是脚本里的并发。模型写一段跑在受限 VM 里的脚本，脚本通过 `agent()` 开子代理，账本记的是启动了多少子代理。

Jobs 管进程级后台。生产者交出 `run()` 和输出源，registry 负责身份、并发、输出环与结算，账本记的是进程记录。

Goal 管状态，外加一个进程本地的续跑授权。持久的是目标和轮次计数，调度器在另一个插件里，账本记的是轮次。

Schedule 靠宿主时钟。任务不在会话里，账本记的是触发次数和投递回执。

## 1. 四条出口：先看它们各自缺了什么

把这四种机制并排，最容易看清的是它们各自不做什么，功能反而退到后面。

Workflow 不持久。它是一次工具调用里的脚本执行，run 结束就没了；结果通过 `WorkflowResult` 返回，取消走 `WorkflowRun.cancel`。

Jobs 不对话。它托管一个进程或一个输出源，没有会话、没有工具、没有模型；模型只能通过 `job_output` 看输出。

Goal 不调度。它只存目标与阶段，决定「什么时候再跑一轮」的是 goal-round-driver 这个独立插件。

Schedule 不在会话里。任务存在宿主存储，目标会话只是投递地址；读取任务从不激活会话。

{{< fig "diagrams/01-four-mechanisms.svg" "图注：四种机制按触发源、生命周期、状态存放与失败恢复排成四列；共同点只有「不在当前轮次里」，其余四行没有一格相同。" >}}

这张图从下往上看更清楚：先看失败恢复，再看状态存放。

Workflow 失败就是一次工具调用失败，错误进对话，模型自己决定重试不重试。

Job 失败是一条 `job/settled` 记录，状态 `failed`，输出环还在，模型可以读最后几百字节。

Goal 失败（更准确说是卡住）会写 `blocked` 加一个 reason code，然后 disarm，等人处理。

Schedule 投递失败保留任务并报错，没有通用重试循环。

四种「失败」根本不是一件事，所以也不能用同一套重试逻辑。

| 机制 | 触发源 | 生命周期 | 状态存放 | 失败恢复 | 记账单位 |
| --- | --- | --- | --- | --- | --- |
| workflow | 模型调用工具 | 单次工具调用 | run 内存 + 父会话展示记录 | stopReason=error，工具结果 isError | 子代理数 |
| jobs | 模型调用工具 | 进程/输出源，长于轮次 | registry + owner 会话的 job 事件 | 结算记录 + 输出环 | 进程记录 |
| goal | 空闲事件 + 授权 | 跨轮次，直到 paused/blocked/complete | 会话日志 `goal/change` | blocked + reason + disarm | 轮次 |
| schedule | 宿主时钟 | 独立于会话，可跨重启 | 宿主存储 + 投递回执 | 保留任务并报错 | 触发次数 |

## 2. Workflow：模型写脚本，脚本开子代理

Workflow 的定位：让 agent 运行一段模型编写、会启动 subagent 的编排脚本。

它是可选能力，不在 agent loop 内；和 bash 一样，每个 context 只允许一个引擎实现提供 `ctx.workflowEngine`，没有命名 provider 注册表。

请求的形状是 `WorkflowStartRequest`：

- `script`：脚本文本。
- `meta`：`{ name, description, whenToUse?, phases? }`，引擎用 schema 校验，绝不 eval 脚本文本去取它。
- `args`：普通 JSON 数据。
- `parent`：必填，脚本启动的每个子 agent 都归属于它。
- `subagentProvider` / `maxTotalAgents`：引擎级策略，脚本观察不到，也改不了。

`parent` 必填这一点很重要。脚本里的 `agent()` 不凭空造会话，它走 subagent seam，cwd、谱系、深度都从父级继承。所以 workflow 绕不过组织结构：它开的每个子代理都还在同一棵树里。

脚本的执行方式：

`packages/workflow/workflow-ptc/src/runtime.ts` 把它包成一个 async IIFE，编译成 `vm.Script`（:75），然后 `vm.createContext({})`（:83），只注入五个东西：`agent`、`parallel`、`pipeline`、`phase`、`log`，外加 `args`（:85 起）。

没有 `require`，没有 `process`，没有 `fs`，没有 `fetch`。

{{< fig "diagrams/02-workflow-timeline.svg" "图注：脚本先 fan-out 三个成员，parallel 等齐后进入下一阶段；pipeline 则是每个 item 独立跑完整条 stage 链，没有跨 stage 的屏障。" >}}

脚本不能直接碰文件系统，原因在这里：脚本是模型写的代码，不该直接拿到宿主能力。

它要读文件，就 `agent()` 开一个子代理，让子代理用它自己的工具、自己的审批策略、自己的沙箱去读。

文件策略仍然生效，但生效的位置在子代理的工具调用上，不在脚本的 `fs.readFile` 上。

这跟沙箱强弱无关，是权限归属问题：脚本没有身份，子代理有。

并发模型分两种，语义不同。

`parallel(thunks)` 有屏障：`Promise.all`，等所有项结束才继续（:285 附近）。

`pipeline(items, ...stages)` 没有跨 stage 屏障：每个 item 独立走完自己的 stage 链（:310 附近）。

两者都受 `maxItemsPerCall` 限制，`agent()` 受 `maxTotalAgents` 限制，超了抛 `AGENT_CAP`（:171）。

失败处理是这套设计里最容易漏看的一段。

脚本内钩子误用（参数错、未知选项、超出结构化输出子集的 schema、超上限、seam 启动失败、取消）都抛 `fatal: true`。

`parallel()` 与 `pipeline()` 对 fatal 直接重抛，而不是把该项映射成 `null`。

逐项 `null` 只留给「子运行失败（非 completed）」和「阶段内普通脚本错误」。

分这么细，是因为两类故障的处理方式不同。

「拼错选项」和「子代理失败」是两种故障。前者是脚本 bug，必须立刻终止，让写脚本的模型看见；后者是运行结果，可以过滤掉继续。

把 fatal 消融成 null，会让一个拼错的 `model` 参数看起来像「那个子任务没做成」，然后脚本带着错误继续跑完。

### 2.1 事件只用于观察

`workflow/start`、`phase`、`log`、`agent-start`、`agent-end`、`end` 六个事件只用于观察。

payload 以 `WorkflowRunInfo`（id 加 meta）开头，而不是活的 `WorkflowRun`，所以订阅者拿不到 `cancel` 与 `dispose`。

`workflow/end` 刻意省略 result 的 value，避免把可变别名交给观察者。

每次 emit 还对每个监听器隔离：一个订阅者抛错只记日志，不阻止后续监听器，每个监听器拿到自己的 payload 克隆。

`agent-start` 和 `agent-end` 按 `agent.seq` 配对。每个实际开始的调用在每条停止路径上都恰好出现一次；引擎终止路径会合成 `outcome: 'cancelled'`。

观察者不能因为「看到了」就获得控制权，这条边界可以照抄。

> 如果自己搭：把「脚本能做什么」做成白名单钩子，别给脚本一个 `eval` 加一堆全局对象。参数校验放在进入钩子之前，拼错就 fatal；脚本层不要提供「忽略错误继续」的开关，它会让参数错误看起来像子任务失败。

## 3. Jobs：脱离轮次的后台进程

Jobs 解决的问题是：一条命令要跑很久，但当前轮次不能一直占着。

生产者交出一个 `JobSpec`：`kind`、`label`、`owner`、`toolCall`、`outputLimitBytes`、`output` 源，以及一个 `run(job)`。

`run` 只调用一次，返回 `JobHooks { cancel, done }`。

runtime 在调用 `run` 之前做完访问预检与清理，之后提交注册，不再执行可能失败的步骤。

这个顺序保证「注册成功」意味着「生产者已经开始」，不会出现注册了但没启动的空记录。

`done` 的语义单独说一下：它在生产方释放资源之后 resolve，工作一完成并不会 resolve。

所以 `job/settled` 的时间戳描述的是「注册表提交结算的时刻」，不是 CPU 时间，也不是 OS 进程边界。

缺结算就保持不完整，消费者不得虚构结束时间。

输出是一个带绝对偏移的有界环，并不像一条流。

`JobHandle.append(text)` 整块落地，`updateProgress(line)` 只更新一行状态；拉取源（`JobOutputSource.read(fromByte)`）由 registry 按 `pumpPollMs` 泵入。

模型通过 `job_output` 用游标消耗，任意观察者用 `readAt(offset)` 按绝对偏移读，互不干扰。

`at` 是首字节的绝对偏移，永不移动；裁剪只推进 chunk 起点，所以 `next` 总是当前 total，也总是 chunk 边界。

结算时环被封住并裁到 `settledRetainBytes`（默认 16 KB），运行期保留 `retainBytes`（默认 256 KB）。

{{< fig "diagrams/03-job-lifecycle.svg" "图注：job 从 start 进入 running，kill 后进入 stopping，最终以 completed/killed/failed 结算；detach 只是观察者断开，attach 是另一个观察者接上，生产者与输出环都不受影响。" >}}

图里的 detach/attach 需要解释一下。

Job 的所有权属于 owner 会话，不属于某个 controller。

`tool-jobs` 通过 `ctx.jobs.attachController('tool-jobs')` 接上（`packages/jobs/tool-jobs/src/index.ts:312`），浏览器通过 `dsh-api-job-controller` 的 Remote 流接上。

一个 job 可以同时被多个观察者看，各自有各自的游标；谁断开都不影响 job 继续跑。

但 `start` 有一个硬条件：必须有一个 attached controller 服务该 spec 的 owner，否则拒绝。

理由是避免生产者启动一个 owner 无法收集、也无法停止的工作。

Job 与 subagent 的区别，可以归成三条：

- 进程 vs 会话：job 没有会话日志、没有对话、没有工具调用；subagent 有完整的会话与轮次。
- 可观测性：job 通过输出环与 `job_list`/`job_output` 观察；subagent 通过 `wait_agent` 与返回消息观察。
- 记账：job 记的是进程记录与字节环；subagent 记的是会话与轮次。

还有一条容易忽略：job 的 `result` 字段只服务「结果是值而不是流」的场景（workflow 渲染结果、subagent 报告）。模型结算后第一次 `read` 拿到一次，之后就没有了。

写入还有一条不显眼但重要的约定：结算之后，`append` 不抛错，只记日志并丢弃。

因为生产者在 `done` 之后常常还有一次尾部 flush；如果这时抛错，teardown 自己会被打断。

拉取源也遵循同样的分工：registry 按 `pumpPollMs`（默认 150 ms）泵每个源，结算封环前再排干一次，生产者不需要把任何东西折进 `done`。

并发上限是每个 owner 10 个（`maxConcurrentJobsPerOwner` 默认 10，`jobs-local/src/index.ts:221` 附近），按 running + stopping 统计，无主 job 共用一个桶。

结算 first-wins：`settle` 第一行就是 `if (isTerminal(job.status)) return`（:604），晚到的生产者结果无效。

## 4. Goal：状态不是调度器

Goal 常被误解成定时让 agent 继续跑。它只做两件事：记住目标，记准入过的轮次数。

持久状态是 `GoalPhase = 'active' | 'paused' | 'blocked' | 'complete'`，加一个 `GoalRef { id, revision }`，每次获准变更 revision +1。

每次变更写一条 `goal/change` 会话事件，载荷是变更后的完整快照，或者清除墓碑。

严格 fold 与持久投影只从这些事件派生生命周期状态；inbox 变更不影响 goal。

`blocked` 是唯一表示「因问题而停止」的持久状态，带 `{ code, message }`，code 是 lower-kebab-case，供路由使用。

调度在另一个插件里：`packages/goal/goal-round-driver`。

它监听 agent 的 idle 事件，判断 `drivable(goal)`（`phase === 'active' && activation === 'armed'`，:106），然后 `drive`（:146）。

`drive` 做这些事：等 fiber active、等 agent idle、等没有 competing queued；需要 checkpoint 时 flush 会话；有 subagent 树就先 `waitForTreeWork`；检查轮次预算；最后 `agent.followup(message)`。

消息是 `createUserMessage({ source: { kind: 'goal', goalId, revision, round } })`，正文来自 `<goal_round>` 模板（`prompt.ts:12`），包含 `Objective`、`Round: n/max`，并要求「same session」「treat workspace/tool results/durable state as authoritative」。

超过预算时会写 `ctx.goals.block(..., { code: 'round-limit' })`（:184），不会静默停止。

{{< fig "diagrams/04-goal-state-machine.svg" "图注：上半是持久 phase，下半是进程本地 activation；resume 同时改两者，pause/complete/block 把 phase 停下并 disarm，而 disarm 本身不改 phase。" >}}

图里最要紧的一点：activation 不是 phase。

一个 goal 可以 `active` 但 `disarmed`：目标还在，但没人有权限自动开下一轮。

`disarm` 只改进程本地状态，不改持久 phase/revision（`packages/goal/goal/src/index.ts:291`）。

`resume` 才把 phase 拉回 active 并 armed（:366），前提是轮次预算还有容量；已经 active + armed 会报 `GOAL_INVALID_TRANSITION`。

`complete` 把非 complete 目标标成 complete 并 disarm；`block` 把 active 标成 blocked 并 disarm，reason 持久。

### 4.1 为什么恢复或 fork 之后必须重新授权

因为 activation 有意不参与持久回放。

`docs/glossary.zh.md` 的定义写得很直接：目标激活是「续行消费方接纳下一个 Goal Round 的进程本地权限」，`armed`/`disarmed`，有意不参与持久回放。

所以进程重启、插件重新加载之后，没有任何代码能从日志里恢复出「之前是 armed 的」。

goal-round-driver 在加载时对 `ctx.agents.list()` 里的每个 agent 执行 `disarm`（:454 起），注释写的是 never inherits hidden automatic authority。

卸载时也 disarm，并取消运行中的 attempt（:463 起）。

这是权限模型的设计，跟防御性代码无关：自动续跑是一种授权，授权不能从磁盘上长出来。

恢复之后要重新开始，只能通过 `/goal` 或模型工具执行一次经人类授权的 resume 变更。

fork 同理：子会话拿到的是目标的持久状态，不是父会话的授权。

自建系统里可以照抄这条规则。

> 如果自己搭：把「目标状态」和「自动执行授权」拆成两个字段，后者永不落盘。恢复时默认 disarmed，必须有人点一次「继续」。反过来想：进程崩溃后自动重启，agent 带着一个三周前的目标继续花钱，却没有人批准过这次续跑。

## 5. Schedule：时钟在宿主，不在会话

Schedule 的定位是「独立于 Session 激活状态、在宿主存任务」。

七种时间类型：`after`、`at`、`every`、`daily`、`weekly`、`cron`、`rrule`。

每种都保持权威，规范化保留已提交的发生时点，不把 Cron 转成 RRULE。

Cron 用五字段加显式时区，保留 Vixie 的「日或周」匹配语义；RRULE 是有界 RFC 5545 子集，DTSTART 必须是 UTC Z 或显式 IANA TZID，拒绝浮动本地时间。

有一条语义最要紧：只追赶最新一次。

`resolveRecurringOccurrence(record, acceptedAt)`（`packages/schedule/schedule/src/domain.ts:820`）在已提交的下界内找最近一次到期时点。

对固定间隔，`resolveEveryOccurrence`（:625）直接算 `steps = Math.floor((acceptedAt - target) / interval)`，一次跳到最近一次，`occurrence + interval` 作为下一个时点。

它不枚举积压，也不补发。

所以一个每 5 分钟跑一次、停机两小时的任务，恢复后只投递一次，然后从当前时刻继续。

这条规则的理由是「补发没有意义」：积压的 24 次提醒同时到达，既吵又过期。

代价是「错过的那几次真的没了」，回执里只有最近一次。

七种时间类型各自的要求：

| kind | 必填字段 | 锚点 |
| --- | --- | --- |
| `after` | 正整数 `afterSeconds` | 提交时刻加延迟 |
| `at` | 一次性 UTC 时刻 | 显式偏移，或本地时间加时区 |
| `every` | 不小于 60 的整数 `everySeconds` | 已提交的间隔锚点 |
| `daily` | `HH:mm:ss.SSS` 加时区 | 本地墙钟 |
| `weekly` | daily 字段加 ISO weekdays | 本地墙钟，周一为 1 |
| `cron` | 五字段加时区 | Vixie 的日或周匹配 |
| `rrule` | DTSTART 加 RRULE | DTSTART 提供锚点与时区 |

本地日历规则会跳过不存在的墙钟时间，重叠时选较早时刻；固定间隔保留经过时间语义。

> 如果自己搭：存「下一次该触发的绝对时刻」，不要每次醒来重算「按 cron 应该是什么时候」。前者让补发语义变成一次减法，后者会在停机、时区变更、夏令时切换时给出不同答案。补发策略也要显式写死，不要留给调用方猜。

投递侧还有几条硬约束：

- 单任务最小投递间隔 60 秒（`runtime.ts:31`），定时与手动投递都遵守。
- 目标可以是已有 Session、`new_once`（预留后复用）、`new`（每次新建）。
- `new` 目标在创建前先持久化 `pendingDelivery.targetSessionId`，失败重试复用同一个 Session。
- 结算写 `schedule/changed` 通知已加载的相关 Agent，但通知不开启新轮次。

重启恢复的处理很细：`recoverOutstandingExecution`（:309）按 `lastDelivery.messageId` 在目标 inbox 里找同一条消息；找到就继续占执行槽，找不到才重新投递。

如果 agent 已空闲，它会 `inbox.remove` 再 `agent.followup`，保证同一条消息不会被消费两次。

崩溃仍可能重复投递，因为 Session 收件箱持久化与任务回执写入不是原子的。

仓库的 `packages/schedule/AGENTS.md` 把这条写成了规则：这些写不是原子的，崩溃可能重复投递，回执不等于模型完成。

## 6. 四种账本不能混用

回到「记账单位」。

- Workflow 记的是子代理数：`agentsStarted`，上限 `maxTotalAgents`。它不记轮次，也不记 token。
- Jobs 记的是进程记录：一个 job id、一个状态、一个输出环、一个 owner。上限是每 owner 10 个并发。
- Goal 记的是轮次：`roundsStarted`，上限 `maxGoalRounds`。无关的人类轮次不消耗它。
- Schedule 记的是触发次数与回执：每个任务保留有界历史（默认 200 条、30 天），回执只证明消息进了收件箱。

混用的后果：

把 goal 当调度器：你会期待「每分钟检查一次」，但 goal 只在 idle 时驱动，且必须 armed。一个长期 busy 的会话永远不会触发它。

把 schedule 当 goal：你会期待「跑到完成」，但 schedule 每次只投递一条消息，模型做不做、做多久，它不知道也不关心。

把 job 当会话记忆：你会期待 job 的输出能进上下文，但 `job_output` 是显式读取，不读就永远不在上下文里。

把 workflow 当长期服务：你会期待它跨重启，但它只是一次工具调用，父会话结束、取消、dispose 都会带走它。

一个具体例子：把「每 30 分钟检查一次 CI，失败就修」写成 goal。

它只在会话空闲时驱动，CI 在跑的时候 Agent 可能正在做别的事；轮次预算耗尽就 `blocked`，不会无限重试。

写成 schedule 更接近「每 30 分钟看一眼」：宿主按期投递检查消息，Agent 每次独立处理，轮次预算与 armed/disarmed 都不参与。

但 schedule 不知道「修完没有」，只知道「投递成功」。

如果需求是「修到绿为止」，结束条件由目标持有，它其实是 goal；如果需求是「定期看一眼」，结束条件由触发器持有，它是 schedule。

两者的差别在「谁判断结束」，跟触发频率无关。

如果「检查、修、复验」的步骤固定、可以写成脚本，workflow 更合适，因为结束条件是脚本的返回值。

> 如果自己搭：先给每种「循环外」能力定一个账本，再写 API。账本决定了上限、配额、可观测性和失败语义。所有能力共用一个「task」表最麻烦，最后没人说得清一个 task 到底消耗的是轮次、进程还是 token。

## 7. 设计取舍

| 取舍 | 选了什么 | 放弃什么 | 什么场景该反悔 |
| --- | --- | --- | --- |
| workflow 执行 | 受限 VM + 五个钩子 | 脚本直接读写文件、发网络请求 | 需要复杂 IO 编排时，只能多开子代理，脚本会变长 |
| workflow 失败 | fatal 立即终止，逐项失败为 null | 统一的「部分成功」结果 | 需要容错续跑时，得在脚本里自己 try/catch 并显式记录 |
| jobs 输出 | 有界环 + 绝对偏移 + spill | 完整日志、结构化事件流 | 需要事后审计全量输出时，spill 文件与外部日志要接上 |
| jobs 所有权 | owner 会话 + attached controller | 全局 job 池、跨会话共享 | 需要多会话共享后台任务时，owner 模型会挡路 |
| goal 授权 | activation 进程本地、永不持久 | 重启后自动续跑 | 无人值守的长任务，需要外部调度器重新授权 |
| goal 预算 | 按轮次计数 | 按 token/时间/花费计数 | 成本敏感场景，轮次是粗粒度代理指标 |
| schedule 补发 | 只投最新一次 | 完整补发、精确次数保证 | 计费或审计需要「每次触发都留痕」时，得另建触发日志 |
| schedule 存储 | 宿主存储，与会话解耦 | 会话内自描述、随会话迁移 | 多宿主部署时，任务归属与迁移要自己解决 |

第一行可以直接展开。

把脚本关进 VM，等于放弃了「用脚本做一切」的灵活性。模型不能写 `fs.readdir` 再过滤，只能开一个子代理让它去读，再把结果返回。

这多了一次模型调用，也多了一层上下文开销。

换回来的是权限清晰：每一次文件访问都发生在某个有身份、有工具策略、有审批记录的子代理身上。

如果让脚本直接读文件，日志里就只剩「workflow 读了什么」，没有「谁批准的、按什么策略批准的」。

第二行也说明一下。

fatal 立即终止意味着脚本作者要自己处理「某个子任务失败但整体可继续」的情况。DSH 不提供「忽略错误继续」的开关，因为那个开关一旦存在，拼错的参数就会被当成子任务失败吞掉。

代价是脚本更啰嗦，收益是错误不会被静默降级。

## 8. 踩坑点

1. 在 workflow 脚本里找 `fs`，或把 fatal 当普通失败。现象：`require is not defined`；或拼错选项后 `parallel` 返回一堆 null，脚本带着错误跑完。原因：VM 上下文只有五个钩子加 `args`；只有 fatal 才重抛，普通子运行失败映射 null。对策：把 IO 交给 `agent()` 的子任务；不要用 `try/catch` 吞掉 `WorkflowError`，让 fatal 终止并把错误暴露给模型。

2. 以为 job 会随轮次结束。现象：轮次结束了，job 还在跑，`job/settled` 出现在 `turn/end` 之后。原因：job 生命周期长于轮次，结算事件可能晚到。对策：不要在 `turn/end` 时假设所有 job 已停；用 `job_list` 或 `wait`。

3. job 输出读两次。现象：第二次 `job_output` 拿到空或重复。原因：`read` 是消耗式游标，`readAt` 才是非消耗。对策：需要重复读就用绝对偏移，别复用消耗游标。

4. 重启后期待 goal 自动继续。现象：进程重启，目标还在 `active`，但没有任何轮次开始。原因：activation 是进程本地的，加载时统一 disarm。对策：把「继续」做成显式的人类动作；无人值守场景用外部调度器重新授权。

5. 以为 resume 能突破轮次预算。现象：resume 报 exhausted。原因：`roundsStarted >= maxGoalRounds` 时 resume 被拒。对策：先调大 `maxGoalRounds` 再 resume，或把目标拆成新目标。

6. 期待 schedule 补发错过的每一次。现象：停机两小时后只收到一条提醒。原因：只追赶最新一次，不枚举积压。对策：需要「每次都要」的审计场景，另建触发日志；提醒类任务接受丢中间次数。

7. 在 schedule 目标会话里等模型完成。现象：回执有了，任务却没做。原因：回执只证明消息进了收件箱，不等于模型完成。对策：把回执当投递凭证，完成状态要另建（或让 agent 回报）。

8. 给 schedule 传一个已归档的会话。现象：保存成功，投递失败。原因：arm 时在 FIFO 内检查可用性，但归档可能发生在之后。对策：把归档与活动任务当成互斥操作，归档前先停任务。

## 9. 源码索引

| 位置 | 证明什么 |
| --- | --- |
| `packages/workflow/workflow-ptc/src/runtime.ts:75` | 脚本被包成 async IIFE 编译成 `vm.Script` |
| `packages/workflow/workflow-ptc/src/runtime.ts:83` | `vm.createContext({})` 空上下文，能力靠注入 |
| `packages/workflow/workflow-ptc/src/runtime.ts:85` | 只注入 `agent/parallel/pipeline/phase/log` 与 `args`，没有 require/process/fs |
| `packages/workflow/workflow-ptc/src/runtime.ts:171` | `maxTotalAgents` 超限抛 `AGENT_CAP` |
| `packages/workflow/workflow-ptc/src/runtime.ts:310` | `pipeline` 逐项跑 stage 链，无跨 stage 屏障 |
| `packages/goal/goal/src/index.ts:291` | `disarm` 只改 activation，不改 phase/revision |
| `packages/goal/goal/src/index.ts:366` | `resume` 是显式授权，且检查轮次预算 |
| `packages/goal/goal/src/index.ts:489` | 运行时初始 activation 是 `disarmed` |
| `packages/goal/goal/src/index.ts:610` | activation 在事件落地后才切换（pendingActivation） |
| `packages/goal/goal-round-driver/src/index.ts:106` | `drivable` = active + armed，调度器只认这两个条件 |
| `packages/goal/goal-round-driver/src/index.ts:184` | 超预算写 `blocked`，code 为 `round-limit` |
| `packages/goal/goal-round-driver/src/index.ts:454` | 插件加载时对现有 agent 统一 disarm |
| `packages/goal/goal-round-driver/src/prompt.ts:12` | `<goal_round>` 模板要求同会话、以工作区与持久状态为准 |
| `packages/jobs/jobs-local/src/index.ts:221` | 每 owner 并发上限，超限报错并提示 `job_kill` |
| `packages/jobs/jobs-local/src/index.ts:604` | 结算 first-wins，终态后直接返回 |
| `packages/jobs/tool-jobs/src/index.ts:312` | `attachController` 是 job 可被收集的前提 |
| `packages/schedule/schedule/src/runtime.ts:196` | 宿主单定时器驱动全部活动任务 |
| `packages/schedule/schedule/src/runtime.ts:309` | 重启后按 messageId 找回未完成投递 |
| `packages/schedule/schedule/src/domain.ts:625` | 固定间隔一次跳到最近一次到期，不枚举积压 |
| `packages/schedule/schedule/src/domain.ts:820` | 「只追赶最新一次」的统一入口 |

## 10. 收尾

回到开篇的四种情况。

它们看起来都是「让 agent 再跑一会儿」，但每一种跑在不同的东西上：workflow 跑在脚本的并发里，jobs 跑在进程里，goal 跑在轮次里，schedule 跑在时钟里。

DSH 没有把它们合并成一个「长任务」抽象，因为合并之后，记账就没法做对了。

一个任务可能消耗三个子代理、一个进程、两轮目标，或者一次定时触发。只有一个计数器，这笔账永远算不清。

自建 Agent 时，先给每种「继续跑」的方式定一个账本，再把授权与状态分开。加功能可以往后放。

Goal 的 disarm 把这条原则摆得最清楚：状态可以持久，授权不行。

上一篇：[15 Agent Teams：共享任务板与委派策略](/posts/dsh-agent-team.html) ｜ 下一篇：[17 记忆架构](/posts/dsh-memory-architecture.html) ｜ [系列目录](/posts/dsh-architecture-anatomy.html)
