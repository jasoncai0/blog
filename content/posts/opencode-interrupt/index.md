---
title: "打断：OpenCode 如何停止一次正在执行的会话"
date: "2026-11-09"
summary: "从一次 Esc 开始，逐层追到 HTTP 端点、SessionRunCoordinator 的 fiber、Step 的工具取消与 SQLite 里的执行声明；解释 ack 与清理为何解耦、resume 只恢复 steer、以及崩溃后 claim 如何恢复。"
description: "基于 sst/opencode v2.2.5 源码的打断机制研究：进程内 fiber 中断、执行声明、inbox 投递与崩溃恢复。"
tags: ["opencode", "concurrency"]
slug: "opencode-interrupt"
---

00 · SUMMARY

## 一句话结论

The short version

打断是**进程内、按 Session ID、针对一次 busy period 的 fiber 中断**，不是杀进程，也不是删消息。 API 在中断被接受时立即返回；finalizer、工具取消、终态事件和 claim 释放都在之后异步完成。

待投递输入不会因为打断而丢失：`steer` 在 `resume=true` 时继续投递，`queue` 保持停放，控制项（compaction / move）按序恢复。用户打断会释放写前 claim，关机与崩溃则保留它。

- **只影响本进程拥有的执行**：空闲、已收尾、或由别的进程持有的会话，调用都是 no-op。
- **确认与清理解耦**：客户端在 ack 时就可以把界面画成空闲，终态事件随后确认。
- **“停执行”和“撤输入”是两套 API**：前者是 interrupt，后者是 inbox cancel。
- **可恢复性是数据库属性**：写前 claim 不依赖 shutdown hook，崩溃后由下次启动接管。

01 · CONCEPTS

## 先分开四个词

用户口中的“打断”在 OpenCode 里至少对应四个不同动作。它们作用在不同对象上，持久化结果也完全不同。把它们混为一谈，是读这段代码时最容易出错的地方。

| 概念 | 入口 | 作用对象 | 结果 |
| --- | --- | --- | --- |
| interrupt | `POST /api/session/:id/interrupt` | 本进程内的一个 busy period | 中断执行 fiber；发布终态事件；输入保留 |
| inbox cancel | `DELETE /api/session/:id/inbox/:id` | 尚未投递的一条 inbox 行 | 删除该行并释放附件 blob；广播 cancelled |
| background | `POST /api/session/:id/background` | 阻塞中的、可后台化的工具 | 工具继续跑，turn 可以先结束 |
| shutdown | 进程退出 / Layer scope 关闭 | 进程内全部执行 fiber | claim 保留，下次启动恢复并续跑 |

本文只讨论第一个。`inbox cancel` 会在第 06 节作为对照出现；`background` 与 `shutdown` 出现在第 05、07 节。还有一个容易忽略的第五种：**回退（revert）把 interrupt 当作前置栅栏**——先打断、再 `wait` 到空闲，然后才 stage，因为“对忙碌会话 stage 会失败”。

02 · OVERVIEW

## 一次 Esc 穿过哪些层

客户端只发一个 HTTP 请求。请求进入 Server 后不做 location 解析，先用一次会话查询确认它存在，再交给 Session facade； facade 只做一层不可中断包裹，然后把请求转给进程全局的 `SessionExecution`。真正的所有权在 `SessionRunCoordinator` 里：它以 Session ID 为键，记录当前是否有一个执行 fiber，并负责把中断投递给它。fiber 随后沿 Effect 的作用域逐层收尾，直到 Runner、Step、模型流和工具。

{{< fig "diagrams/01.svg" "FIG. 1 · LAYERS **分层路径：打断请求只走控制面。**客户端发出 HTTP 调用后，Server 校验会话存在性，经 Session facade 交给进程全局的执行协调器；后者中断 owner fiber，并把终态事件经 SSE 送回。SQLite 里没有“正在运行的 fiber”，只有 inbox 行、写前 claim 和事件日志；这也是为什么中断只能由持有该 fiber 的进程完成。" >}}

这条链上有一个不对称，值得先记住：**请求是同步的，执行状态的归属却是内存的**。 `SessionExecution` 是进程级单例（`makeGlobalNode`），协调器的 map 只存在于当前进程。因此协议把返回值定义成“本 OpenCode 进程是否打断了一个活跃执行”，而不是“会话是否已被打断”。同一份数据库可以被多个客户端打开，但只有持有执行的那个进程能停它。

HTTP 契约很短（packages/protocol/src/groups/session.ts:672-689）：`POST` 带可选的 `resume` 查询参数，返回 `{ interrupted: boolean }`，唯一声明的错误是 `SessionNotFoundError`。它没有挂 location 中间件，因为协调器本来就只按 Session ID 索引；这也意味着它不关心请求来自哪个目录。

03 · COORDINATOR

## 协调器：一个 busy period 的生命周期

`SessionRunCoordinator` 只有 174 行，却定义了整套打断语义。先把四个词定住：

- **busy period**：从第一次 wake 开始，到会话“真的没事可做”为止的一段占用。一个 busy period 对应一个 fiber、一条写前 claim、以及最终的一条终态事件。
- **doorbell（pendingWake）**：执行期间记录“还有活”的门铃。drain 结束前如果门铃响过，就再 drain 一次，而不是退出；这样补上了“最后一次检查”和“转为空闲”之间无法原子化的缝。
- **stopping**：中断被接受后的状态。此时不再接受新的 join，已记录的 doorbell 被视为 “被中断意图的一部分”而清掉。
- **settle**：fiber 退出后的收尾。先跑 `settled` 钩子（发布终态、释放 claim），再根据 doorbell 决定启动后继还是删除记录，最后唤醒 `awaitIdle` 的等待者。

{{< fig "diagrams/02.svg" "FIG. 2 · STATE MACHINE **状态机：打断只是把 stopping 置位，然后把 fiber 交给 Effect 的收尾流程。** 注意两条进入“收尾”的路径：中断导致的退出，和 drain 自然结束 / 失败；两者走同一个 settle，但前者带 `interruptionReason`，后者不带。收尾期间的新 wake 属于新意图，会启动后继执行。" >}}

中断函数本身只有十几行，但每一条判断都对应一种边界（run-coordinator.ts:142-162）：

Coordinator · interrupt (excerpt, types elided)

```
const interrupt = (key, reason) =>
  Effect.sync(() => {
    const execution = executions.get(key)
    if (execution === undefined || execution.stopping) return false
    if (execution.owner === undefined) {
      // 收尾窗口：终态已定，但 settled 钩子还没跑完。
      // 不附加 reason，但要吞掉已记录的 wake，避免 settle 启动“死意图”的后继。
      execution.pendingWake = undefined
      return false
    }
    execution.stopping = true
    // 已经记录的 wake 属于被中断的意图，由这次中断认领。
    // 清理期间到达的 wake 是新投递，settle 时会正常重启。
    execution.pendingWake = undefined
    execution.interruptionReason = reason
    // fire and forget：没人需要在这里等清理，端点必须立即确认。
    fork(Fiber.interrupt(execution.owner))
    return true
  })
```

由此可以列出协调器的六条行为规则：

2. **一次 busy period 只有一个 owner fiber，也只有一条 claim。**成功、失败、用户中断都释放它；只有 shutdown 保留。
4. **wake 会合并。**执行中的多次 wake 只留下一个 `pendingWake`，作用域取更宽的 `input`（它包含 `steer`）。不会有“唤醒风暴”，也不会有遗漏。
6. **中断是幂等的。**已 `stopping` 时再调用返回 `false`；收尾窗口内调用也返回 `false`，但仍然吞掉 doorbell。重复按键不会被吞掉，只是不再产生新效果。
8. **中断后不再接受 join。**`run()` 遇到 `stopping` 会先等这次执行结束，再重新开始一次全新的执行——它不会挤进正在收尾的那条 fiber。
10. **清理期间的新 wake 会重启。**这是“打断之后马上发一条新消息”仍能工作的原因：新投递不属于旧意图， `settle` 看到 doorbell 就启动后继。
12. **确认早于收尾，收尾早于等待者。**`interrupt()` 返回时只代表“中断被接受”； `awaitIdle()` 返回时，终态事件已经发布、claim 已经处理完。

第 6 条的顺序值得展开。执行 fiber 的退出回调是两层：先 `settled`（发布 `session.execution.interrupted`、按 reason 释放 claim、取消该会话自己的 job），再 `settle`（启动后继或删除记录，并 resolve `done`）。因此 `session.wait` 返回之后再去 stage revert，不会撞上“会话仍被判定为忙碌”。

04 · SEQUENCE

## 确认与清理如何解耦

把一次用户打断拉成时序，会看到一条刻意“提前返回”的线：fiber 的收尾还没开始，HTTP 200 就已经在回程上了。这不是实现偷懒，而是协议设计——端点注明“resolves once the interruption is accepted, not when cleanup settles”。

{{< fig "diagrams/03.svg" "FIG. 3 · SEQUENCE **时序：200 在收尾之前就返回了。**协调器只等“中断被接受”就答复，fiber 的中断传播、工具退出、终态发布依次发生在下面；`SETTLED` 自调用表示发布 `session.execution.interrupted` 并处理 claim。客户端因此需要两个信号：ack 用来立即改 UI，终态事件用来确认。" >}}

把图 3 拆成可验证的六步：

2. **手势。**TUI 需要连按两次 Esc（5 秒窗口），Desktop 是 Stop / Esc / Ctrl+G， CLI 是 SIGINT。三者最终都调用同一个端点。
4. **HTTP。**带 `resume=true` 时请求体为空；服务端只做会话存在性校验。
6. **协调器。**置 `stopping`、认领并清空 doorbell、记录 reason、`fork` 一个 `Fiber.interrupt`，同步返回 `true`。
8. **确认。**HTTP 立刻回 `200 { interrupted: true }`。此时清理可能还没开始。
10. **传播。**Runner 的 fiber 收到中断，Step 的不可中断收尾区开始工作：取消工具 fiber、等待它们退出、为未结算的工具写 `aborted` 终态、把 assistant 标为 `Step interrupted`。
12. **收尾。**`settled` 发布 `session.execution.interrupted`（reason 为 `user`），释放 claim；`settle` 再决定后继或删除记录。

客户端要在这两个信号之间保持自洽。TUI mini 的做法是：收到 ack 就把界面画成空闲，但用 `executionEpoch` 做代际守卫——如果终态事件先到，epoch 已经变化，这次 ack 就不再覆盖新状态。它还会把当前等待标记为 `interrupted`，从而不再把 `reason="user"` 的终态当成错误显示。

05 · INSIDE A STEP

## 中断怎样落到 stream 和工具

一次 Step 是一次显式的 `llm.stream` 调用，加上它启动的本地工具 fiber。Step 外层是一个 `uninterruptibleMask`：工作本身可以被中断，但最后的工具与 Step 终态事件必须发完。这正是“打断不会留下半条消息”的关键。

中断到达时，Step 的收尾逻辑分四件事：

- **识别中断。**`Exit.hasInterrupts(stream)` 为真时，先调用 `interruptTools`，再 `Fiber.awaitAll` 等待所有工具 fiber 结算。
- **结算工具。**用户拒绝、流中断、工具 fiber 被中断，都会让 `classifyToolExits` 得出 `interrupted`；尚未发布终态的工具统一写 `{ type: "aborted", message: "Tool execution interrupted" }`。
- **结算 assistant。**被中断的 assistant 以 `Step interrupted` 结束，已经流出的文本保留。
- **再抛中断。**等不可中断区结束，Step 重新把中断抛给 drain；协调器据此走“中断”终态，而不是“失败”。

对模型流而言，中断沿 Effect 的作用域关闭正在读取的响应流；对本地工具而言，中断就是取消它的 fiber。两者语义不同，但对上层是同一个动作。**对外部系统则没有这种保证**：MCP 服务、远端 HTTP、已经提交的写操作，最多只能保证“本地不再等它”。

### 工具层的四种命运

| 对象 | 打断时发生什么 | 为什么 |
| --- | --- | --- |
| 前台 shell | job 被取消，进程被 kill | `jobs.block` 的 `onInterrupt` 取消 job；job 的 run fiber 退出时再 `shell.remove` |
| 后台 shell | 继续运行，完成后再发通知 | block 已经返回 `backgrounded`，job 不再挂在 turn 上；通知 fiber 在 plugin scope 里 |
| 前台 subagent | 子会话一起被中断 | subagent 工具的 `onInterrupt` 显式调用 `sessions.interrupt(child)` 并取消 job |
| 后台 subagent | 不受影响，需要单独打断 | 它是独立 Session 的 busy period，父会话的 fiber 不拥有它 |

最后一行解释了为什么 TUI 的子代理面板有独立的停止快捷键：每个子代理都有自己的 Session ID，对它调用 `session.interrupt` 与对父会话调用是两条独立路径。

还有一个不显眼但重要的入口：**拒绝权限请求**。工具在权限被拒时以 defect 抛出 `Permission.DeclinedError`；Step 收齐这些 decline 后，为每个调用写 `The user declined this tool call`，然后直接 `Effect.interrupt` 结束当前 Step。也就是说，“拒绝”不是工具级失败，而是整步中止。第 10 节会讨论这条路径在 reason 归属上的疑点。

06 · INBOX

## 打断之后，输入去了哪里

这是整套机制里最容易被忽略、也最影响用户体验的部分。OpenCode 的输入不是直接塞进对话，而是先进入 `session_inbox` 表，再由 runner 在**安全步边界**（每个 Step 开始前）提升为可见消息。因此“打断”只停执行，不会碰未投递的行。

投递模式决定打断后会发生什么。默认是 `steer`：在下一个安全步边界投递，用来“边跑边纠偏”。 `queue` 则等到空闲边界，用来排队下一轮。compaction 与 move 是控制项，它们本身构成投递边界—— 后面的 steer 不会越过一个控制项。

| 待投递项 | 投递模式 | 打断后 | resume=true 时 |
| --- | --- | --- | --- |
| 用户 / 合成消息 | `steer` | 保留在 inbox | 后继 drain 以 steer 作用域投递 |
| 用户 / 合成消息 | `queue` | 保留在 inbox | 继续停放；steer 作用域的 drain 不会提升它 |
| 控制项 compaction / move | 任一 | 保留 | 若它排在队首，会被恢复执行 |
| 已被投递的消息 | — | 已是历史 | 不撤回；编辑或回退走 revert |

`resume` 省略等价于 `false`：只打断，不尝试恢复。只有显式传 `true` 的客户端（TUI 与 Desktop）才会走上面那一列。下面这段实现只有十行，却精确表达了这个矩阵（execution.ts:151-168）：

Resume after interrupt · steer and control items only

```
const interrupted = yield* coordinator.interrupt(sessionID, "user")
if (!options?.resume) return interrupted
const next = yield* SessionInbox.nextPromotable(db, sessionID, "input")
if (next === undefined) return interrupted
if (next.delivery === "steer" || next.type === "compaction" || next.type === "move")
  yield* coordinator.wake(sessionID, "steer")
```

三个细节值得注意。第一，**恢复不是“继续上一次执行”**：旧 fiber 已经退出，这是一个全新的 busy period，只带 `steer` 作用域。第二，**只有队首是 steer 或控制项才唤醒**；如果队首是一条 queued prompt，它会继续停着，不会因为一次 Esc 被意外提升。第三，**读取 inbox 与将死的 drain 并发是安全的**：投递在不可中断的发布事务里消费行，一条 steer 要么还没被消费、此刻可恢复，要么已经被完整投递、不需要恢复。

如果用户的意图是“这条排队消息我不要了”，那应该用 `DELETE /inbox/:id`，而不是 interrupt。两者分开的好处很直接：停执行不会误删用户刚打的字，撤输入也不会把正在跑的工具一起停掉。

07 · RECOVERY

## 持久化与恢复：claim 的释放与保留

执行状态在内存里，但“我曾经在执行”这件事必须能被下一次进程读到。OpenCode 的答案是 **写前声明（write-ahead claim）**：busy period 开始时，在同一事务里发布 `session.execution.started` 并写入 claim；终态事件负责释放它。代码注释把这个设计说得很清楚：恢复是数据库的属性，不能依赖一个可能根本没机会运行的 shutdown hook。

| 结束方式 | 终态事件 | claim | 会话投影 | 下次启动 |
| --- | --- | --- | --- | --- |
| 成功 | `succeeded` | 释放 | `idle_outcome = succeeded` | 不再执行 |
| 失败 | `failed` | 释放 | `idle_outcome = failed` | 不再执行 |
| 用户打断 | `interrupted · user` | 释放 | `idle_outcome = interrupted` | 不再执行 |
| 关机 / scope 关闭 | `interrupted · shutdown` | **保留** | 不置 idle | 恢复并追加续跑指令 |
| 崩溃 / SIGKILL | 无 | **保留** | 仍显示 running | 恢复并追加续跑指令 |

重启时的恢复由 `SessionRestart` 负责。它扫描“有 claim、没有终态”的会话，先追加一条合成消息 *“The server restarted while you were working…”*，再触发 drain。每次恢复尝试都会持久计数，默认上限 10 次；超过上限则写一条 `aborted` 终态并停止，避免一个反复崩溃的 turn 把进程拖进无限重启。恢复是 at-least-once：本地协调器只能防止并发 drain，不能防止崩溃后的重复外部副作用。

投影层的差异也由此而来：`reason="shutdown"` 的中断**不会**把会话标成 idle，因为下一次启动还要接着跑；而 `reason="user"` 会。Goal 插件同样按 reason 分流：用户打断会把活跃目标暂停并记下“Stopped by the user”，关机中断则保持目标不动，交给下次启动处理。

08 · CLIENTS

## 四个客户端，四种手势

服务端只有一个端点，客户端却各自长出了不同的交互约束。共同的谨慎来自同一个事实： **ack 不等于收尾完成**，所以 UI 既不能等终态事件才响应，也不能假设 ack 之后一定空闲。

| 客户端 | 手势 | 请求 | 特殊处理 |
| --- | --- | --- | --- |
| TUI mini | Esc 两次（5 秒内） | `resume: true` | ack 即画空闲；`epoch` 守卫防旧 ack 覆盖新状态 |
| TUI classic | Esc 两次（5 秒内） | `resume: true` | 与 mini 相同的两击计数与超时重置 |
| Desktop / Web | Stop、Esc、Ctrl+G | `resume: true` | 空白 Enter 不打断；尚未发出的内容按 Stop 只取消发送，不打断会话 |
| CLI `run` | SIGINT / SIGTERM | 不带 `resume` | 撤输入 + 打断 + 取消子任务，循环等到空闲；退出码 130 |

TUI 的两击设计不是防误触的装饰，它还承担一个状态提示：第一击把 footer 提示切成 “press esc again to interrupt”，第二击才真正发请求；5 秒内没有第二次按键就自动复位。与之相对，Ctrl+C 绑定的是 `prompt.clear`， Ctrl+B 绑定的是 `session.background`——三个键对应三种完全不同的语义。

TUI mini 还有一个针对“用户 shell”的特例：当界面上等待的是一条 `!` 命令而不是模型轮次时，打断改为 abort 那个阻塞的 HTTP 请求，因为“运行中的 shell 不持有 drain，`session.interrupt` 够不到它”。服务端命令继续按自己的生命周期跑，只是失去了等待者；真正要取消它，走的是 `shell.remove`。

回退（revert）则是把打断当作栅栏的另一个用户：先 `interrupt`，再 `experimental.session.wait` 等到空闲，最后才 `revert.stage`。原因是 stage 对忙碌会话直接失败，而本地状态可能滞后于服务端；先等一拍，比猜要可靠。

09 · TRADEOFFS

## 为什么这样设计

2. **确认与清理解耦。**finalizer 可能很慢（等工具退出、写库、发事件），但用户的按键需要立刻有反馈。代价是“看起来空闲、实际仍在收尾”的窗口；OpenCode 用终态事件和 epoch 守卫来兜底，而不是让 UI 阻塞。
4. **只在安全步边界投递输入。**模型请求是一次性的、不可回滚的；如果允许在流中途插入文本，上下文会与已发出的请求错位。代价是打断的粒度较粗：已经发出的工具调用不能被“撤回”，只能被取消并记为 aborted。
6. **用 fiber 作为唯一所有权。**不需要分布式锁、心跳或租约：谁持有 fiber，谁就能打断。代价是它天然是 process-local 的；要跨进程 / 集群，就需要额外的 placement 与 fencing 协议，而代码明确说这还没有实现。
8. **claim 放在数据库，而不是关机钩子。**崩溃、SIGKILL、容器被驱逐都不会执行钩子，但数据库里的未释放 claim 是可靠信号。代价是恢复只能做到 at-least-once，无法保证外部副作用恰好一次。
10. **“停执行”和“撤输入”分成两套 API。**这是最容易被低估的一条：它让 Esc 永远不会删掉用户刚打的字，也让“撤回一条排队消息”不会顺手停掉正在跑的工具。

10 · EDGES

## 边界与待确认项

下面几条来自静态阅读，其中最后两条是需要上游确认或实测的疑点，不应被当作既定行为。

### 已明确的边界

- **外部副作用不可撤回。**中断能停止本地等待，不能撤销已经提交的写、已经发出的请求。
- **打断是协作式的。**工具必须响应 fiber 中断；一个不响应取消的插件会把收尾拖长，虽然 ack 仍然立即返回。
- **reason 的取值比生产者多。**schema 允许 `user / shutdown / superseded / inactivity`，但当前 core 只产生前两个；后两个是为将来的替换与超时预留的。
- **`jobs.cancel(sessionID)` 不是“取消该会话的所有作业”。**它按 job id 匹配，实际命中 id 恰好等于会话 ID 的作业（子代理会话自身的 job）；会话删除路径用的是 `cancelSession`。已后台化的 shell / 工具不受这次调用影响。

### 两个读代码时的疑点

**其一：权限拒绝的 reason 归属。**拒绝权限走的是 Step 内部的 `Effect.interrupt` （runner/step.ts:290-292），而不是协调器的 `interrupt()`。 `terminal(exit, reason)` 在拿不到显式 reason 时默认 `"shutdown"`（execution.ts:47-51）。按字面推断，这条路径会：保留 claim、不把会话投影为 idle、并在下一次启动时被当作崩溃恢复。若这不是本意，那么“用户拒绝”应该被归因为 `user`。这一点本轮没有运行验证，建议用一次真实的权限拒绝 + 重启来确认。

**其二：interrupt 的返回布尔值在收尾窗口里是 `false`。** `owner` 已退出但 `settled` 尚未跑完时，协调器返回 `false`，但仍然吞掉 doorbell。对调用方来说，“false”既可能表示“本来空闲”，也可能表示“刚好在收尾”；如果未来有客户端用它做重试判断，需要把这两种情况区分开。

11 · INDEX

## 代码索引

行号对应 commit `a687c04`；版本变化后以符号名为准。

| 位置 | 内容 |
| --- | --- |
| protocol/src/groups/session.ts:672 | HTTP 端点与 `resume` 参数 |
| server/src/handlers/session.ts:607 | 端点实现：校验存在性并转交 facade |
| core/src/session/session.ts:328 | facade 的 `interrupt`，用不可中断包裹 |
| core/src/session/execution.ts:151 | 中断 + 可选的 steer 恢复 |
| core/src/session/execution.ts:47 | 终态判定：中断默认 `shutdown` |
| core/src/session/execution.ts:109 | claim 与终态事件的接线 |
| core/src/session/run-coordinator.ts:142 | 中断：stopping、认领 doorbell、立即返回 |
| core/src/session/run-coordinator.ts:111 | settle：后继执行或删除记录 |
| core/src/session/runner/step.ts:141 | 不可中断的工具与 Step 终态结算 |
| core/src/session/runner/step.ts:290 | 拒绝权限 → `Effect.interrupt` |
| core/src/session/runner/llm.ts:82 | 安全步边界：提升输入 / 处理控制项 |
| core/src/session/inbox.ts:470 | `nextPromotable`：steer 优先 |
| core/src/session/inbox.ts:252 | `cancel`：撤销未投递输入 |
| core/src/session/execution/restart.ts:74 | 启动恢复、尝试上限、续跑指令 |
| core/src/session/projector.ts:838 | shutdown 不置 idle 的投影 |
| core/src/plugin/goal.ts:300 | Goal 按 reason 暂停或保留 |
| tui/src/mini/footer.ts:994 | 两击 Esc 与 5 秒窗口 |
| tui/src/mini/stream-v2.transport.ts:1867 | ack 即画空闲 + epoch 守卫 |
| cli/src/run/noninteractive.ts:259 | SIGINT 的取消循环 |
| app/src/session/revert.ts:248 | revert 用 interrupt + wait 做栅栏 |
| specs/v2/session.md:53 | 执行归属与打断语义的规范描述 |

12 · BASELINE

## 基线与复现

本文基于 `sst/opencode` 的 `v2.2.5`（commit `a687c04`）源码静态阅读。核心代码集中在 `packages/core/src/session/`：`run-coordinator.ts` 定义所有权， `execution.ts` 负责 claim 与终态，`runner/` 负责一步之内的收尾， `inbox.ts` 负责输入的去向。客户端行为分散在 `packages/tui`、 `packages/app` 与 `packages/cli`。

想自己验证一遍，最短的路径是：启动一个带长任务的会话，按一次 Esc 观察“再按一次”的提示，再按一次；同时盯 `session.execution.interrupted` 的 reason 与 `session_inbox` 中未被消费的行。若要观察恢复，需要在执行中直接杀掉服务进程，再启动，看是否出现合成续跑消息与 `resume` 计数。

文档与三张图均为本次源码阅读的产物，不是 OpenCode 官方文档。图中术语保留英文原文（busy period、claim、 steer、queue），以便与代码和事件名对应。源码行号以文中标注的 commit 为准。

版式与图示遵循 Diagram Design 的默认编辑风格：`#f5f5f5` 纸面、`#2d3142` 墨色、 `#bf4520` 单一强调色；正文 Geist / Noto Sans SC，标题 Instrument Serif / Noto Serif SC，技术标签 Geist Mono。

[GitHub](https://github.com/jasoncai0) · [RSS](/rss.xml) · [返回首页](/)
