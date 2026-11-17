---
title: "定时任务：宿主时钟、追赶策略与低峰期排班"
date: "2026-11-17T10:30:00+08:00"
summary: "Schedule 不挂在会话里，它是宿主级任务库：七种时间类型、一次 tick 的完整路径、三种投递目标，以及停机后为什么只补最新一次。文末对比 plan mode 与 todo_write。"
description: "DSH Schedule 深挖：schedule.json 与 v2 存储、cron/RRULE 求值、drive 循环与投递槽、60 秒投递下限、追赶最新一次、崩溃重复窗口，以及计划模式与 todo 工具的区别。"
tags: ["planning","harness","architecture"]
slug: "dsh-scheduled-runs"
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）。源码以 `packages/schedule/schedule/src/{runtime,domain,index}.ts` 与 `packages/util/time/src/{cron,rrule}.ts` 为准；未跑真实到点投递，涉及定时精度与重启的结论会标明推断。

定时任务的常见做法是「在进程里挂一个 `setInterval`，到点给 Agent 发一条消息」。这在单进程、单会话的演示里够用，一旦要求「低峰期跑」「重启后不丢」「同一个任务不要并发两份」，就会暴露三个问题：定时器归谁、任务存在哪、错过之后补几次。

DSH 的答案是把定时做成宿主级领域，和会话解耦：任务存在宿主的 `schedule.json` 里，一个 Host 定时器扫描到期任务，投递时再决定叫醒哪个会话。这篇按一次 tick 的顺序拆开，最后对比两个常被混淆的「计划」机制。

{{< fig "diagrams/01-host-vs-session.svg" "Schedule 在会话之外：任务属于宿主，投递时才解析目标会话；三种目标对应三种会话创建策略。" >}}

## 1. 定时任务不在会话里

`ctx.schedule` 是宿主服务，不属于任何 Agent。任务的存储位置是 `<DSH_HOME>/storages/schedule.json`（SQLite 后端则是 `u_schedule_tasks` 的整单元 JSON），领域版本 `2`，兼容 `1`。读取任务不激活它的目标 Session，这也是管理界面能在不启动任何 Agent 的情况下列出任务的原因。

这条设计带来两个直接后果：

- 定时器只依赖 Host，不依赖某个会话是否活着。会话归档、进程重启、Agent 空闲，都不影响任务本身的存在。
- 任务与执行分离。任务只记「什么时候、对谁、说什么」，不记「模型做完了没有」。回执确认的是消息写入目标收件箱，不是任务完成。

任务记录有五种时间类型之外的公共字段：`id`、`title`、`instruction`、`timing`、`target`，以及 `status`（`active` / `paused` / `ended`）。`instruction` 是投递时那段提醒文本的来源，不是模型指令的模板。

## 2. 七种时间类型

`timing` 是判别联合，七个变体各自保留权威时间字段：

| 类型 | 权威字段 | 说明 |
| --- | --- | --- |
| `after` | `afterSeconds` + 已提交 UTC 目标 | 一次性延迟，可短于 60 秒 |
| `at` | 已提交 UTC 目标 | 显式时刻，或本地日期 + 时间 + 时区 |
| `every` | `everySeconds`（≥60）+ 锚点 | 固定间隔，按经过时间对齐 |
| `daily` | 本地 `time` + 显式时区 | 每天同一墙钟时间 |
| `weekly` | `daily` 字段 + `weekdays` | 周一为 1，周日为 7 |
| `cron` | 五字段表达式 + 显式时区 | Vixie 语义，不转 RRULE |
| `rrule` | 一条 `DTSTART` + 一条 `RRULE` | RFC 5545 子集，`COUNT=1` 表示一次 |

每个已存的 `scheduledAt` 都是规范 RFC 3339 UTC 时刻，毫秒精度。解码不会按当前时区重算它；改了系统时区，已提交的发生时点不漂。

几个容易忽略的规则：

- Cron 的「或」语义：月内日期与星期两个字段都受限时，任一匹配即可；任一字段以 `*` 开头时，两组都要匹配。这是 Vixie 的原义，DSH 保留它，不把表达式规范化成另一种含义。
- 夏令时：本地日历规则跳过不存在的墙钟时间，重叠时选较早的时刻；固定间隔保留经过时间，不做墙钟对齐。
- 一次性延迟可以短于 60 秒，但首次投递之后，单任务仍受最小投递间隔约束。
- 规则耗尽后任务进入 `ended` 状态并保留，不自动删除。

## 3. 一次 tick 的完整路径

Host 上只有一个定时器，指向「所有活动任务里最早的可投递时刻」。到点后走一条固定路径：

{{< fig "diagrams/02-tick-sequence.svg" "一次 tick：扫描到期任务、恢复未确认投递、解析目标、写收件箱、落回执、释放槽位、重设定时器。" >}}

1. 清定时器：`drive()` 先 `clearTimeout`，避免重入。
2. 扫描到期：过滤 `status === 'active'`、没有在执行中、且 `scheduledAt <= now` 的任务。
3. 按目标会话分组：同一条会话上多个到期任务合成一条消息（batch framing），减少打扰；`after` / `at` 单独投递。
4. 准入检查：每个成员先看 `nextAdmissionAt`（上次回执 + 投递间隔），再看 `recoverOutstandingExecution`：如果上次投递的消息还在收件箱里、且没被消费，就继续占着槽，不重复发。
5. 解析目标：`session` 目标检查存在、未归档、不是委派子 Agent；`new_once` 首次创建后转为 `session`；`new` 每次投递创建。
6. 渲染提醒：按 `source.kind = 'schedule'` 构造 user 消息；文本来自任务标题与指令，加上计划时刻。
7. 写收件箱：`agent.followup(message)`：next-turn + 唤醒。紧接着 `ctx.sessions.flush()`，等持久化确认。
8. 落回执：写入 `scheduledAt`、`deliveredAt`、`messageId`、`targetSessionId`，然后推进到下一个未来时点。
9. 释放槽位：非 `new` 目标从入队起占槽，直到目标 Agent 整体空闲（`whenIdle()`）才释放；`new` 目标不占槽，允许多个并行。
10. 重设定时器：取所有活动任务 `max(scheduledAt, nextAdmissionAt)` 的最小值，钳到 `MAX_TIMER_DELAY_MS`（2,147,483,647 ms，约 24.8 天）。

「槽位」是这里最容易被忽略的机制。它的目的与限流无关，是保证同一条会话上同一个任务不会有两份未消化的投递。模型跑得慢，下一枪不会叠上来；错过的那次会被合并（见下一节）。

## 4. 三种投递目标

{{< fig "diagrams/03-delivery-targets.svg" "session 复用已有会话；new_once 预留一个 id 后复用；new 每次新建，失败重试复用同一 id。" >}}

| 目标 | 创建时机 | 复用 | 适用 |
| --- | --- | --- | --- |
| `session` | 不创建 | 固定会话 | 给正在维护的会话加提醒 |
| `new_once` | 首次投递前预留 id | 之后保存为 `session` | 周期报告、固定工作台 |
| `new` | 每次投递 | 不复用（重试复用同一 pending id） | 一次性并行任务、隔离实验 |

`new` 目标在创建前先把 `pendingDelivery.targetSessionId` 持久化，所以创建失败或进程重启后重试仍用同一个 id，不会多出一个孤儿会话。创建出的会话会加入其目录已注册的 Workspace（如有），标题为空时用任务标题；`new` 还会在标题后加该规则时区的创建分钟。

省略目标时默认 `new_once`。如果嵌入环境没有提供 Session 创建能力（例如精简 SDK 组合），新增 `new` / `new_once` 会在保存前以 `invalid_rule` 拒绝，已有 `session` 目标不受影响。

## 5. 低峰期怎么排

「低峰期执行」在实现上就是三件事：一个带时区的 cron 表达式、一套错过后的合并策略、一个宿主必须活着的约束。

```text
0 3 * * *        # 每天 03:00
30 2 * * 1-5     # 工作日 02:30
0 4 1 * *        # 每月 1 日 04:00
```

时区必须显式给，不能用服务器本地时区碰运气。DSH 的 cron 输入是 `{ expression, time_zone }`，规范里保留 IANA 名称；夏令时切换按上文的跳过/取早规则处理。

错过之后怎么办？三种常见策略：全部补、补最新一次、只补有意义的。DSH 选只补最新一次：`resolveRecurringOccurrence(record, now)` 取不晚于 `now` 的最后一个发生时点，投递一次，然后把 `scheduledAt` 推进到第一个未来时点。停机三天，不会醒来连发三条。

这个策略的代价要写清楚：

- 低峰期任务通常「每天一次」，补最新一次等于只跑当天那次，语义正确。
- 如果是「每小时采样」，停机五小时只会补一次，中间四次的语义直接丢失。需要逐次语义的任务应改成每次投递新建会话并自行记录，或用 `every` + 外部补偿。
- `every` 的停机合并同理：合并错过的间隔，不逐次重放。

另一个约束是投递间隔。单任务最小 60 秒（`MIN_DELIVERY_INTERVAL_MS`），`new` 目标可用 `newSessionIntervalSeconds` 提高，最大一周。间隔的锚点选择有讲究：如果后继时点本身已经保持间隔，用上一次计划时刻做锚；否则用上一次确认时刻做锚。这样投递延迟不会把规则锚点整体推后。

最后一条最容易被低估：Host 必须运行。定时器在宿主进程里，进程不在就没有 tick。这不算实现缺陷，是「宿主级调度」的固有边界；需要跨机器可靠触发时，应该由外部调度器调用管理 API 的 `run({ id })`。

## 6. 失败与恢复

| 故障 | 行为 |
| --- | --- |
| 收件箱已写、回执未写 | 两份存储之间崩溃，重启后可能重复投递一次 |
| 上次投递仍在收件箱 | `recoverOutstandingExecution` 找到同一 `messageId`，继续占槽，不重发 |
| 目标会话已归档 | 投递前检查归档状态；归档操作会删除指向它的活动任务 |
| 时钟回拨 | 解析目标后再次按当前时间过滤，未来的成员不创建会话，保留定时器义务 |
| 规则无未来时点 | 启用时拒绝；已存在的任务在规则耗尽后进入 `ended` |
| 编辑冲突 | `expectedUpdatedAt` 做乐观比较，不一致返回 `schedule_conflict` |
| 管理写入失败 | 保留全部行；`unknown-state` 失败可能已移除行，隔离领域直到重新打开 |

回执历史默认保留 200 条、30 天，同时保留最新一条。历史按追加顺序，不受时钟回拨影响；查询用消息 id 游标，从新到旧。删除任务不会撤回已经入队的消息，那条消息已经属于会话日志。

还有一个语义边界：投递成功不等于任务完成。回执只证明「提醒进了收件箱」。模型有没有执行、执行得对不对，不在 Schedule 的观测范围内。

## 7. plan mode 与 todo_write 的区别

这两个机制都叫「计划」，但一个管能不能动手，一个管打算做什么。

{{< fig "diagrams/04-plan-vs-todo.svg" "plan mode 是策略状态，进入后改写提示词并卡住退出工具；todo_write 是模型自己维护的清单，只落日志与 UI。" >}}

| 维度 | plan mode | `todo_write` |
| --- | --- | --- |
| 状态形态 | `plan/mode`，`{ active }`，整值替换 | `todo/write`，整张列表替换 |
| 存在位置 | 会话事件日志，可回放 | 会话事件日志，可回放 |
| 是否进模型上下文 | 激活时注入 `plan:policy` 段落（order 500） | 不注入；模型只看到工具返回 |
| 谁改 | 用户 `/plan`，或模型调 `exit_plan_mode` 后经人工批准 | 模型自己调工具 |
| 目的 | 开工前对齐范围，限制「先别动手」 | 记录任务分解与进度 |
| 强制力 | 软约束：只改提示词，不拦工具 | 无约束：纯记账，不影响任何准入 |
| 退出方式 | 批准计划、`/plan off`、或下一条用户消息 | 下一次 `turn/start` 清空投影；列表随写覆盖 |
| 失败模式 | 忘记退出、把「已提交计划」当成「已获批准」 | 只写不更新、清单与实际进度漂移 |
| 消费者 | 提示词组装、`exit_plan_mode` 准入、UI | UI 投影、回放、`todos` 视图 |

几个具体差别值得展开：

1. plan mode 会改变模型看到的东西：激活期间每个请求都带 `plan:policy` 段落，段落文本由部署配置；未激活时该段落不存在。`todo_write` 不改变系统提示词，它只写一条事件，模型在下一次请求里看到的是自己的工具结果，不是注入的指令。
2. plan mode 有准入逻辑：`exit_plan_mode` 从「计划状态首次为激活的那一步」开始进入工具声明，之后一直保持准入；在进入计划模式之前调用它返回 `UNKNOWN_TOOL`。`todo_write` 是常驻工具，没有状态门槛。
3. plan mode 需要人：`exit_plan_mode` 要求一份以 `#` 开头的完整 markdown 计划，通过用户交互 seam 呈交；批准后记录一个静默的待生效退出，由下一个被接受的 pre-step 追加。没有交互通道时调用失败，而不是静默退出。`todo_write` 不需要任何人批准。
4. 两者都不负责验收：plan mode 保证范围先被同意，todo 保证进度可见；交付是否正确仍要看测试、产物与评审。

选择上可以用一个判断：如果缺的是「模型该不该现在动手」，用 plan mode；如果缺的是「模型有没有把活拆清楚、做到哪了」，用 `todo_write`。两个都缺就都用；它们不互斥，plan mode 激活期间模型照样可以写 todo。

## 8. 设计取舍

| 取舍 | 选择 | 代价 |
| --- | --- | --- |
| 任务归属 | 宿主级，与会话解耦 | Host 不在就不触发 |
| 错过处理 | 只补最新一次 | 高频任务的中间语义丢失 |
| 并发控制 | 非 `new` 目标占槽到 Agent 空闲 | 慢任务会推迟后续投递 |
| 投递间隔 | 单任务 60 秒下限 | 秒级提醒需要拆任务或外部触发 |
| 回执语义 | 只确认入队 | 无法从 Schedule 判断任务完成 |
| plan 与 todo | 一个改提示词，一个只记账 | 两者都不能强制工具行为 |

## 9. 踩坑点

1. 把定时任务当会话内定时器。会话归档后任务还在，但投递会被归档门挡住。
2. 用服务器本地时区写 cron。跨时区部署会在夏令时切换时整体偏移。
3. 以为停机后会逐次补跑。只补最新一次；高频任务要自己设计补偿。
4. 把回执当完成。回执只证明消息进了收件箱。
5. 在 `new` 目标上依赖会话复用。`new` 每次新建，只有重试复用同一个 pending id。
6. 忘记 `run({ id })` 是手动立即投递，不改变计划时刻与生命周期。
7. 用 plan mode 当权限控制。它只改提示词；限制在沙箱与审批。
8. 把 `todo_write` 当计划审批。它不经过人，也不阻塞任何工具。

## 10. 源码索引

| 位置 | 内容 |
| --- | --- |
| `packages/schedule/schedule/src/runtime.ts:194` | `drive()`：扫描、分组、投递、重设定时器 |
| `packages/schedule/schedule/src/runtime.ts:228` | `deliverDue()`：准入、恢复、渲染、提交、释放 |
| `packages/schedule/schedule/src/runtime.ts:366` | `send()`：`followup` + `flush` |
| `packages/schedule/schedule/src/runtime.ts:287` | `nextAdmissionAt()`：60 秒下限与锚点选择 |
| `packages/schedule/schedule/src/runtime.ts:310` | `recoverOutstandingExecution()`：重启后找回未消费消息 |
| `packages/schedule/schedule/src/domain.ts:42` | `MIN_EVERY_INTERVAL_SECONDS = 60` |
| `packages/schedule/schedule/src/index.ts:229` | `newSessionIntervalSeconds` 范围 60–604800 |
| `packages/util/time/src/cron.ts` | Cron 解析与 Vixie 匹配 |
| `packages/util/time/src/rrule.ts` | RRULE 子集与有界枚举 |
| `packages/plan/plan-mode/src/index.ts:199` | `plan:policy` 段落 order |
| `packages/core/system-prompt/src/index.ts:139` | `PLAN_POLICY: 500` |
| `packages/todo/tool-todo/src/index.ts:120` | `todos` 投影：`turn/start` 清空 |
| `packages/todo/tool-todo/src/types.ts` | `TodoItem` 三态与整表替换 |

## 11. 最后

定时模式做的事是把时间从会话里拿出来，变成宿主的一等公民：任务独立存储、求值器统一、投递时才绑定会话、错过只补一次。

理解这一点之后，plan mode 与 `todo_write` 的差别也就清楚了：前者是模型上下文里的一段策略，后者是模型自己写的一页清单。一个改变它怎么想，一个记录它做了什么。

系列导航：[第四季导览](/posts/dsh-season-four.html) · [Workflow / Jobs / Goal / Schedule 对照](/posts/dsh-workflow-jobs-goal.html) · [Plan 与 Todo 所在的交互面](/posts/dsh-interaction-surfaces.html)
