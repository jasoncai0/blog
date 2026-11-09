---
title: "OpenCode 的执行机制：一条循环、六个出口、两种停法"
date: "2026-11-09"
summary: "执行不是一次请求，而是一条按会话串行的循环：准入把消息写进收件箱，drain 决定还要不要下一次模型调用，Step 的六个出口各自处理失败、溢出与截断。以及进程崩溃后，凭什么能接着跑。"
description: "执行不是一次请求，而是一条按会话串行的循环：准入把消息写进收件箱，drain 决定还要不要下一次模型调用，Step 的六个出口各自处理失败、溢出与截断。以及进程崩溃后，凭什么能接着跑。"
tags: ["opencode", "concurrency", "planning"]
slug: "opencode-execution"
---

> 源码版本：`opencode 2.2.5`，commit `a687c047`（2026-11-08）。代码引用以工作区 `.opencode-src/` 为基准。
> 系列第十一篇。上一篇讲"怎么规划"，这一篇讲规划的对面：**一条消息怎么真的跑起来，又怎么停下来**。

## 0. 先把结论摆出来

"执行"在 OpenCode 里不是一次 API 调用，而是一个**循环**。三个问题各自有清楚的位置：

| 问题 | 答案 | 一句话 |
| --- | --- | --- |
| 消息怎么开始跑 | 落进 per-session 的收件箱，再用一个门铃叫醒执行器 | 准入不改历史，投递才改 |
| 什么时候继续、什么时候停 | 只看一件事：**还需不需要下一次模型调用** | 六个 Step 出口，三个 drain 出口 |
| 进程崩了怎么办 | 不靠 shutdown 钩子，靠数据库里的一条认领 | 恢复是数据库的属性 |

还有一个更根本的观察：**执行里没有"计划"这个变量**。没有任何数据结构叫 plan，也没有调度器读它。规划能影响执行的方式只有三种——改工具与权限、往历史里加消息、在 idle 时再塞一条消息。上一篇讲的三个规划机制，落到执行层就是这三件事的不同组合（见 §7）。

{{< fig "diagrams/01-drain-loop.svg" "执行是一条带分支的循环" >}}

## 1. 准入：回车之后发生的事

用户按下回车，第一件事不是调模型，甚至不是拼 prompt，而是**把消息持久化**。

`Session.prompt` 整个包在 `uninterruptibleMask` 里（`packages/core/src/session/session.ts:155-191`），顺序固定：

1. 用调用方给的 `id` 做 `admission.reconcile`：先查既有 inbox 条目，再查是否已投递成消息（`packages/core/src/session/inbox.ts:177-189`）。命中就**幂等返回**既有条目——重试时传的 payload、delivery 全被忽略；同一个 `id` 换 type 或换 session 则抛 `LifecycleConflict`，API 层转成 `PromptConflictError`。
2. 没命中才走 `SessionPrompt.prepare`，插件钩子可以改写文本、附件、agent、metadata 以及 **delivery**（默认 `steer`，`packages/core/src/session/prompt.ts:42-53`）。
3. 若会话里有暂存的 revert，先 `SessionRevert.commit`，再 `admission.admit`。
4. admit 发布 durable 事件 `session.inbox.enqueued`，投影在**同一事务**里插一行 `session_inbox`，`enqueued_seq` 直接取事件的持久序号（`packages/core/src/session/projector.ts:1037-1048`）。
5. 最后 `execution.wake(sessionID)`。`resume: false` 时只准入不唤醒。

这套设计的关键是把两件事拆开了：**消息被接收**与**消息进入模型上下文**。准入只写收件箱，历史一动不动；真正变成上下文是在投递时——`session.inbox.delivered` 的投影在同一事务里"删 inbox 行 + 插消息"（`projector.ts:1008-1035`）。所以收件箱表里永远只有未消费的工作，崩溃后也不会出现"收到了但没进历史"的空洞。

### 1.1 wake 只是门铃

`wake` 本身不启动任何东西，它按 sessionID 记账（`packages/core/src/session/run-coordinator.ts:131-140`）：

- 该会话**没有**在跑的 execution → `start`：fork 一个 fiber，先 `yieldNow` 让出一次调度，再跑 `started` 钩子（发布 `Execution.Started`、写入认领），然后进 drain 循环。
- 已经在跑 → 只写一个 `pendingWake`（scope 合并，`input` 吞掉 `steer`），当前 drain 结束时由 trampoline 再 drain 一次。

于是"开始跑"的充要条件是：**收件箱里有可投递的项，且本进程没有该会话的执行**。同一个会话同时最多一个 drain、一条 fiber；`Moved` / `Reloaded` 由 `execution.drain` 递归续上（`packages/core/src/session/execution.ts:103-107`），仍在同一条 fiber 上。并发的粒度是会话，不是工具——工具在字符串里并行，会话在结构上串行。

### 1.2 三个终态，一次结算

一个 busy period（从 `Execution.Started` 到终态）内可能合并了多次 drain，但**只有一次终态结算**（`run-coordinator.ts` 的 `settled` 钩子）。结算逻辑很短（`execution.ts:115-146`）：

- 成功 → `Execution.Succeeded` + 释放认领；
- 中断 → `Execution.Interrupted`，reason 为 `user` 时还要 `jobs.cancel` 并释放认领，**wake 时带 `?resume=true` 的例外**（见 §6.3）；
- 失败 → `Execution.Failed` + 释放认领。

三个终态都必须写，因为认领的释放挂在它们的提交钩子上。写漏一个，这个会话下次启动就会被当成"上次没跑完"。

### 1.3 steer 与 queue

`delivery` 只有两种，差别在**等多久**：

- `steer`（默认）：等**下一个 step 边界**。`advanceToStep` 在每个边界调 `promote(scope = entering && !continuing ? promotable : "steer")`——只有 turn 的第一拍允许 `input` 语义，之后一律只吸收 steers（`packages/core/src/session/runner/llm.ts:187-207`）。一批 steers 在同一边界一次性投递，`promoted > 0` 时把 `step` 重置为 1。
- `queue`：只在 turn 开始或空闲边界交付，而且空闲边界**一次只交付一条**。

所以"执行中插话"不是打断，是排队等下个边界。模型在下一步的上下文里看到它，而不是等本轮结束。测试里写得很直白：turn 中途 move 之后，恢复的 turn"只吸收 steers；queued input 等 turn 结束"（`packages/core/test/session-runner.test.ts:1694`）。

收件箱是单一 FIFO（按 `enqueued_seq`），没有优先级字段。唯一的例外是 control 项：`compaction` 与 `move` 是**屏障**——steer 批投递止于第一个 control；如果 steer 队列里存在 compaction，它会被提到该批最前面，先压缩，再把更早的 steer 以原文投递，且绝不跨过 move（`inbox.ts:591-624`）。

## 2. drain：循环在哪里转弯

drain 的主循环朴素得有点意外：

```
while (true) {
  advanceToStep()   // 边界：清场、投递、压缩、move、idle
  runStep()         // 一个 Step
  step++
}
```

（`llm.ts:212-220`）

真正复杂的是 `advanceToStep`，它由 `uninterruptibleMask` 包住，只在选上下文、压缩、`promote` 这几处 `restore` 出可中断区（`llm.ts:82-206`）。每个边界按顺序做四件事：

1. **location 关了没有**。关了就先关 transport，返回 `Reloaded`——同一个 turn 换个 location 接着跑，`step` 编号带过去，配额不重置。
2. **有没有控制项**。`move` 直接返回 `Moved`；`compaction` 先执行压缩，成功后 `force = false` 继续循环。
3. **清崩溃残留**。`settleStaleCompactions` 把遗留的 `running` 压缩标成失败，`settleStaleToolCalls` 把 `streaming`/`running` 的工具标成 `aborted`（`llm.ts:79-80`）。注意这一步发生在**每次 drain 的开头**，不是进程启动时——只要这个会话再次被拉起来，残留就会被清掉。
4. **该不该收工**。条件是：非 force、非 continuation、没有待投递项（或只剩 queue 而本次 scope 是 steer）。此时如果本轮真的跑过 step，就触发插件钩子 `experimental.idle`，把本会话及其子会话里还在跑的后台任务一起交出去（`llm.ts:175-186`）；钩子返回后再查一次收件箱，仍为空才 `Complete`，否则 `continue`。

`drain` 有三种返回，`execution.drain` 只认得其中两种需要递归（`execution.ts:103-107`）：

| 返回 | 含义 | 之后 |
| --- | --- | --- |
| `Complete` | 这个 turn 结束 | 释放认领，会话回 idle |
| `Moved` | 会话换了 location | 用同一个 `step` 在新 location 继续 |
| `Reloaded` | 配置/生命周期要求重载 | 保留 `force`，重新进 drain |

### 2.1 idle 钩子是唯一能"反悔"的地方

第 4 步那个钩子值得单独说，因为它是整套设计里唯一的例外：**drain 本来要结束了，插件还能把它留住**。

钩子拿到 `background`（本会话及后代里 running 的 shell/image/subagent，`packages/core/src/session/background.ts:24-51`），返回值不参与判定——判定只看钩子跑完之后收件箱里有没有新输入。`/goal` 就是在这里把 continuation 作为 synthetic 塞回队列，于是同一个 busy period 继续下一圈，`Execution.Succeeded` 都不会发（`packages/core/src/plugin/goal.ts:198-216`）。测试用"Session 只 idle 一次"钉住了这点。

反过来说，**后台任务本身不阻塞 drain**。"还有 shell 在跑"不会让会话保持忙碌；要不要等，由钩子决定。没有插件时，会话该空闲就空闲。

## 3. 一个 Step 的六段

`SessionStep.attempt` 的职责边界很硬：**只发一次 `llm.stream`，把流写成事件，等本地工具跑完，选一个 outcome**。它绝不发起下一次调用（`packages/core/src/session/runner/step.ts:73-138`）。"下一步"由外层依据**重新加载的历史**决定——每次 attempt 前都重新 `prepareContext` + `context.load`（`llm.ts:243`），所以每一步看到的都是落库后的历史，包含上一步的工具结果。仓库的 `AGENTS.md` 把这条写成了硬规矩：每个 step 是一次显式调用，禁止内存里的工具循环。

{{< fig "diagrams/02-step.svg" "一个 Step 的六段" >}}

几个容易被忽略的细节：

- **快照在最前面**。这一步用哪个人格、哪些工具、什么模型，在 step 开始时采样，之后不再看配置。所以运行中切换 agent 只影响**下一个** step（`packages/core/src/session/context.ts:126-139`）。
- **最后一步强制文本**。条件是 `agent.steps !== undefined && step >= agent.steps`，命中后追加一条 assistant 消息 `MAX_STEPS_PROMPT` 并把 `toolChoice` 设为 `"none"`，但**保留工具定义**——注释写明是为了护住 provider 的缓存前缀（`llm.ts:268-272`）。本地还有第二道闸：`toolChoice` 为 `none` 时任何 tool call 直接返回 `Tools are disabled after the maximum agent steps`（`step.ts:99-100`）。所以强制收尾有三重保障，provider 硬发也只会拿到一条错误结果。
- **工具是抢先并发的**。`tool-call` 一到就 fork 一个 scoped fiber 开始执行，不等流结束；`Fiber.awaitAll` 在流读完才等齐（`step.ts:122-156`）。测试里 5 个调用同时活跃数就是 5。
- **工具抛异常不失败整步**。它被就地转成该工具的错误结果，模型能看见并改道（`step.ts:233-241`）。
- **delta 不落库**。文本与推理的增量是 ephemeral 事件，只有 `Text.Ended` 落权威全文；工具参数的 delta 干脆不发，只在结束时存完整参数和前 2048 字符的 excerpt（`publish-llm-event.ts:257-270, 607-619`）。

## 4. 六个出口

Step 结束时只回答一个问题：**还需不需要一次模型调用**。六个 outcome 就是六种答案（`step.ts:30-41, 141-301`）：

| 出口 | 触发条件 | 之后做什么 |
| --- | --- | --- |
| `Completed{needsContinuation}` | 正常收尾 | 为真就再跑一步；为假时顺手启动后台摘要预计算 |
| `Retry` | 可重试错误，且**还没输出任何内容** | 退避后同一个 assistant 消息重来 |
| `Continue` | 可重试错误，但**已经吐过字** | 退避 + 一条 synthetic 续写提示，换新消息继续 |
| `Compacted` | 上下文溢出且未输出，压缩成功 | 换新消息，继续 |
| `RecoverFull` | transport 要求整步重试，且未输出 | 整步重来一次（有 flag 防二次） |
| `OutputLimit` | `finish === "length"` | 有工具待续就继续；否则看最近 3 条 |

`Retry` 与 `Continue` 的分界线是"有没有输出过"——这是有实际意义的划分：没输出时重试是免费的，输出过之后重试会浪费已生成的 token，所以改成"接着写"。失败的那条消息保留 `finish: "error"`，新消息另起一条。

`needsContinuation` 的算法也值得一看：它等于"存在非宿主执行、且已调用或已结算的工具"再与 `toolChoice !== "none"` 相与（`step.ts:297`）。因为最后一步的 `toolChoice` 必为 `none`，所以**步数用尽的那一步必然是 `Completed(false)`**，不会再续——不需要额外的特判。

### 4.1 重试的预算

退避是 2s、4s、8s，之后 10s×7，共 10 次，加抖动后总窗口约 67–101 秒（`packages/core/src/model-retry.ts:22-40`）。`retry-after` 头只抬高不降低，上限 15 分钟；DNS/无路由这类不可达错误走独立预算（2s 起、间隔 ≤30s、窗口 1 小时）；transport 超时最多 3 次。

哪些错误重试，规则在 `packages/ai/src/schema/errors.ts:197-230`：`x-should-retry` 头优先；限流、provider 内部错误、未知 provider 可重试；认证、额度耗尽、内容策略、非法请求、不支持的操作、超时一律不重试——超时由上面那 3 次独立预算管。

一个反直觉的点：**重试不消耗 step 配额**。退避循环嵌在 `runStep` 内部，只有外层才 `step++`。测试用 `steps=2` 却发出 3 个请求来固定这个行为（`session-runner.test.ts:6721`）。

### 4.2 输出截断的第三次才认输

`OutputLimit` 不是马上失败。它先看最近 3 条 user/assistant：如果全是 assistant 且 `finish` 都是 `length`，才报 `output-limit`（"Response still truncated after two output token limit continuations"）；否则发一条 synthetic 提示"别道歉、别复述，把剩下的拆小"，换新消息继续（`llm.ts:345-375`）。也就是说，容忍两次续写，第三次才放弃。

## 5. 压缩也是执行的一部分

很多人把压缩当成"上下文管理"的独立功能。在这个实现里，它其实是**执行循环的一个分支**，因为压缩点全部长在 drain 的边界上。

预算公式（`packages/core/src/session/compaction.ts:1160-1183`）：

```
window   = min(limit.input, limit.context)      // 缺一个就用另一个，都为 0 即 Infinity
reserve  = max(floor(window * 0.15), window >= 32k ? 16_000 : 0)
threshold = window - reserve
```

`reserve` 可被 `compaction.buffer` 覆盖（`"20%"` 或绝对 token 数），buffer ≥ 窗口则忽略。注意它与模型的 output limit 无关：100k 窗口下，output 设 10k 还是 30k，阈值都是 84_000。

估算口径只有一个：**字符数 ÷ 4**（`util/token.ts`）。`estimatePrompt` 取最近一条同 provider 的 assistant 锚点，用它的输入、缓存读、缓存写、输出、推理五项相加，再加上之后每条消息的估算；system prompt、工具名与 JSON schema 全算进去；图片按 1500、PDF 按 2000（`compaction.ts`）。粗糙，但稳定——同一份历史每次估出来的值一样，压缩判定才不会抖。

三个触发点：

- **auto**：每个 step 发请求前先估算，超阈值就压缩；压缩成功就换一条 assistant 消息、重新加载历史再发（`llm.ts:245-252`）。
- **overflow**：请求已经发出去，且"一个字都没输出"时收到上下文溢出，压缩后重试一次（`step.ts:150-172`）。已经输出了就不压缩重试，直接走 `Continue`。
- **manual**：用户手动压缩，走收件箱的 `type: "compaction"` 控制项，是屏障。

压缩产物是一段结构化摘要，装进单条 user 消息 `<conversation-checkpoint><summary>…<recent-context>…`，默认保留最近 15k token 原文（`metadata.keepID`）。历史读取从最近一次成功的 compaction 起算。装不下时按 0.85 → 0.7 → 0.5 → 0.35 逐级缩短输入重试（`SHRINK_STEPS`，`compaction.ts:127`），最新一轮交换永远不丢。

成本上有个容易忽略的点：**压缩本身是一次模型调用，也要钱**，而且失败或中断也照样记账（`UsageRecorded{source:"compaction"}`）。另外 TUI 里那个 "N tokens / X% used" 的分母是**原始上下文窗口，不扣预留**——所以看到 85% 时，实际已经接近触发点了。

## 6. 两种"停下来"

{{< fig "diagrams/03-recovery.svg" "中断与崩溃的两条恢复路径" >}}

### 6.1 中断点由掩码决定，不是随时可断

`interrupt` 只是设一个 fiber 中断，所以"哪里能断"完全由 `uninterruptibleMask` 的边界决定（`packages/core/src/session/runner/step.ts:141-143`）：

- **流中可断**：`restore(providerStream)` 之外都在掩码内。但注释要求把流读到结束而不是读到 finish 事件，因为下一次请求要复用同一份响应。
- **工具执行可断，落账不可断**：每个工具 fiber 是 `uninterruptibleMask(restore(executeTool(...)) pipe ...)`——只有执行本身可中断，`truncate` 与结果发布不可中断（`step.ts:125-137`）。这就是"工具跑到一半被杀，但一定会留下一条终态"的机制来源。
- **工具等待可断**：`restore(Fiber.awaitAll(...))` 可中断，但中断后必须走结算。
- **step 之间**：`advanceToStep` 整体不可中断，只在选上下文、压缩、投递这几处 `restore`。

为什么不能随时断？因为消息与事件必须成对（`Tool.Input.Started → Ended`、`Tool.Called → 终态`、`Step.Started → Ended/Failed`），在两次发布之间切断会留下半条记录；而"认领存在但没有终态"正是判断崩溃的唯一签名，乱断会污染它。

### 6.2 三种停在半路的方式

| 停法 | 认领 | 未结算的工具 | 下次启动 |
| --- | --- | --- | --- |
| 用户中断 | 释放 | 进程活着，就地补 `Tool.Failed` | 不恢复（本就没打算恢复） |
| 优雅关闭 | **保留** | 同上 | 扫描到认领，续跑 |
| 崩溃（SIGKILL/驱逐） | 保留 | 停在 `streaming`/`running` | 扫描到认领，先补终态再续跑 |

优雅关闭故意不释放认领（`execution.ts:128-133`），`terminal()` 还把"没有显式 reason 的中断"一律当成 shutdown（`execution.ts:49`）。这是一个很务实的选择：与其赌 shutdown 钩子能跑完，不如让"有没有终态"自己成为证据。

### 6.3 用户中断的一个开关

`interrupt(sessionID, {resume})` 默认只中断。传 `resume: true` 时，它会看一眼收件箱，只把 **steer 输入、compaction、move** 三类重新唤醒；排队的 next-turn prompt 继续停着（`execution.ts:150-166`）。语义是"停下手上的活，但如果你刚插了话，就按插话继续"。

### 6.4 崩溃之后：三条出路

启动时 `installRestartContinuity` 会 fork 一次扫描（`packages/server/src/process.ts:250-252`）。扫描的对象是：

- `listSuspended()`：`time_suspended` 非空且 `parent_id is null` 的**顶层会话**；
- 加上 `job` 表里状态为 running 的后台 subagent 子会话；
- 减去本进程已经在跑的。

顺序上先结算 `job` 表的后台通知（非 subagent 排前面），再让恢复的会话发第一次请求；顶层会话之间是无界并发，彼此没有顺序保证。

每条认领先 `countResume` 原子 +1（**在恢复之前落库**，所以恢复途中再崩也会计数），然后三选一：

1. `attempts <= 10` → 投一条 synthetic：`The server restarted while you were working. Continue from where you left off without repeating completed work.`，然后 `execution.resume`。UI 上显示 "Continuing after restart"。
2. `attempts > 10` → `Execution.Failed`，错误是 `Execution was interrupted repeatedly and will not be resumed automatically.`，并在同一个提交里释放认领、清零计数。此后不再自动重试。
3. 用户中断本身不恢复。

预算按**轮**计：只有终态释放会清零。所以一个永远跑不完的 turn 不会跨重启无限循环，也不会拖累下一个 turn。

### 6.5 孤儿工具：补一条终态，而不是假装没发生

恢复续跑之前必须先结算上一轮留下的空缺，否则模型会看到一个没有结果的 tool call。`settleStaleToolCalls` 遍历"还没完成、且最近一次压缩之后"的 assistant 消息，把 `streaming`/`running` 的工具逐条发 `Tool.Failed`：

```
Tool execution interrupted: <tool name>
Tool execution interrupted: subagent (sessionID: ses_xxx)
```

`running` 状态保留 `metadata`，`executed` 沿用原值——所以"provider 侧执行过"与"本地跑过"在终态里仍然可区分。压缩的孤儿则补一条 `{type: "compaction.interrupted"}`，因为进程死亡会跳过压缩的 finalizer。

### 6.6 后台任务：只有一部分会重跑

| 类型 | 重启后 |
| --- | --- |
| shell | 只补一条通知（运行中被杀 → `Command cancelled because the server restarted`），**不重跑** |
| image | 同上（`Image request for <path> was cancelled because the server restarted.`） |
| 后台 subagent | **真重跑**：`execution.resume(childSessionID)`，完成后给父会话发一次 `<subagent>` 通知 |
| 前台 subagent | **不恢复**：它走 `jobs.block`，从未进 job 表，没有任何 DB 行 |

前台 subagent 那条尤其值得记住：父会话那条工具会被孤儿结算成 `Tool execution interrupted: subagent (sessionID: ...)`，用户至少还能拿着子会话 ID 手动跟进，但子会话本身不会自己复活。

### 6.7 恢复是"至少一次"

`restart.ts` 的注释写得很清楚：进程内协调防的是并发 drain，防不了崩溃前已经发生的副作用。所以恢复语义是 at-least-once，**工具必须自己幂等**。这条约束不是实现缺陷，是任何"崩溃后继续"的设计都躲不掉的成本。

## 7. 计划与执行之间，只有一根管子

上一篇讲了三个尺度的规划机制。把它们放到执行这条线上看，谁真的改变了执行一目了然：

| 规划物 | 形态 | 进执行路径？ | 生效点 |
| --- | --- | --- | --- |
| Plan 的提醒 | synthetic/user 消息 | 否，纯提示 | `context` 钩子 |
| Plan 的权限规则 | Permission 规则 | **是**，直接拒绝 | `Permission.assert` |
| Plan 的错误改写 | tool 钩子 | 否，只改文案 | `execute.after` |
| `todowrite` | 工具结果 | 否，无状态、无读者 | 只有 UI 渲染 |
| `/goal` 的 kickoff / continuation | synthetic 输入 | **是**，admit + promote | idle 钩子 |
| `/goal` 的目标提醒 | 请求内 user 消息 | 只影响这一次请求 | `context` 钩子 |
| `subagent` 前台调用 | job 阻塞 | **是**，挂起父 step | `jobs.block` |
| `agent.steps` | agent 配置 | **是**，禁用工具 | 边界判定 |

一句话分工：Plan 是"权限 + 文案"，todo 是"书签"，goal 是"把 continuation 塞回同一个 drain"，subagent 是"把父 step 借出去"，steps 是"终止条件"。

这里有个几乎没人会预料到的耦合：**`/goal` 的续写会把 `agent.steps` 的配额清零**。因为 continuation 走的是 `promote`，而 `promoted > 0` 时 `step = 1`。也就是说 `agent.steps` 管不住 goal 循环——真正管住它的是 `MAX_ROUNDS=50` 与 `MAX_STALLS=3`。两条预算在不同的层，作用对象也不同。

## 8. 代价与边界

**每个 step 都要重付一次输入。** 这不是实现浪费，而是循环结构的必然：每一步都重新加载历史、重新组装请求，模型才能看见上一步的工具结果。工具越多，step 越多，重放的上下文越多。能省的只有缓存前缀——这也是为什么最后一步要保留工具定义、goal 的提醒不落库、plan 的提醒插在最后一条 user 之前。

**执行层不认识"计划"。** 想影响执行，只有三个入口：工具与权限、历史消息、idle 时的收件箱。任何"编排框架"想要的能力，最终都要翻译成这三件事之一；翻译不了的，就不在这个系统里。

**恢复的边界是 job 表。** 只有同时有 `recovery` 与 `notificationID` 的后台任务能被重新拉起；前台的一切（包括前台 subagent）都不恢复。

**未确认的地方**：

- `SessionRestart.Options.maxAttempts` 是否通过配置暴露——只看到构造参数，节点用的是默认 `layer()`；
- managed server 的注册锁与 PID 轮询细节没读，所以"未注册的 server 共享数据库时永不扫描"只按注释陈述；
- `MAX_STEPS_PROMPT` 为什么作为 assistant 消息而不是 system 追加，代码与注释都没写原因。

## 9. 证据索引

| 主题 | 位置 |
| --- | --- |
| 准入与幂等 | `packages/core/src/session/session.ts:155-191`、`inbox.ts:177-189` |
| 投影与投递 | `projector.ts:1008-1048` |
| 门铃与合并 | `run-coordinator.ts:92-162` |
| 认领与终态 | `execution.ts:78-146`、`store.ts:207-237` |
| drain 循环 | `runner/llm.ts:64-220` |
| 六个出口 | `runner/step.ts:30-41, 141-301` |
| 重试预算 | `packages/core/src/model-retry.ts:22-70`、`packages/ai/src/schema/errors.ts:197-230` |
| 压缩阈值 | `session/compaction.ts:1160-1183` |
| 崩溃恢复 | `session/execution/restart.ts:26-107, 225-256` |
| 孤儿结算 | `runner/llm.ts:388-441`、`history.ts:360-384` |
| 计划耦合 | `plugin/plan.ts:33-73`、`plugin/goal.ts:198-231`、`tool/plugin/todo.ts:63-78` |

调试入口建议从上往下读：先看 `run-coordinator` 理解"为什么没跑"，再看 `llm.drain` 的四个边界判断理解"为什么停了"，最后看 `step.ts` 的 outcome 分支理解"为什么重试/续写"。这三层各自有清晰的单一职责，出问题时能很快定位到是哪一层。
