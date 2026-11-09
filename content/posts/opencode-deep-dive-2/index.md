---
title: "OpenCode 机制深潜 II：状态机、并发与错误边界"
date: "2026-11-09"
summary: "第二层解剖：Step 的五个变量与四个 return、claim 释放矩阵与三种中断、工具 hook 的失败语义、权限挂起与 MCP 连接状态机、指令迁移表、运行面进程状态机，以及存储的索引、迁移与快照对象图。"
description: "基于 opencode 2.2.5 源码的第二层解剖：Step 的五个变量与四个 return、claim 释放矩阵、工具 hook 的失败语义、权限挂起与 MCP 连接状态机、指令迁移表、运行面进程状态机与存储内部结构。"
tags: ["opencode", "concurrency", "architecture"]
slug: "opencode-deep-dive-2"
---

> 源码版本：`opencode 2.2.5`，commit `a687c047`（2026-11-08）。代码引用以工作区 `.opencode-src/` 为基准。
> 这是「OpenCode 解剖」系列的第五篇。[第一篇](https://www.jasoncai.site/posts/opencode-architecture.html)讲整体架构，[第二篇](https://www.jasoncai.site/posts/opencode-context-management.html)讲上下文，[第三篇](https://www.jasoncai.site/posts/opencode-interrupt.html)讲打断，[第四篇](https://www.jasoncai.site/posts/opencode-deep-dive.html)逐个模块讲机制。

## 0. 这一卷补什么

第四篇回答的是"每个模块大致怎么运转"。这一卷不再横向铺，而是纵向切：**每个模块的状态机、并发原语、错误分类和边界条件**。目标是写到"可以照着实现"的程度——给出变量表、guard 原文、常数、释放矩阵和钉住行为的测试名。

三条贯穿全卷的线索：

1. **状态都很少，但传递很深。** 一个 drain 只有五个变量，却要跨 move、reload、重试、中断四种换乘。
2. **并发靠"单 fiber + 门铃"，不靠锁。** 会话内串行、跨会话并行，所有竞争都被压到几个 `Deferred` 与 `KeyedMutex` 上。
3. **错误分三类，各有各的出口。** 可重试错误走退避，用户拒绝走两条不同的通道，中断则必须穿透成 defect 或 Cause，不能被翻译成"工具失败"。

## 1. Step 状态机：五个变量，四个 return

先看 drain 入口一次性写入的五个量（`session/runner/llm.ts:66-70`）：

```ts
let force      = input.force                    // 强制跑一个 Step，跳过 idle 资格检查
let continuing = input.continuation !== undefined // 是否在续同一个 turn
let step       = input.continuation?.step ?? 1   // 逻辑步号，跨 move 传递
let stepped    = input.stepped ?? false          // 本轮是否已经跑过 Step
let entering   = true                            // 本次边界允许提升 queue 还是只允许 steer
```

`entering` 最容易漏：它不参与循环条件，只决定"这次边界能不能把下一轮的 `queue` 拉进来"，而它又直接决定 `step` 要不要重置。

### 1.1 状态矩阵

| 事件 | force | continuing | step | stepped | entering |
| --- | --- | --- | --- | --- | --- |
| 进入 drain | input | `continuation!=null` | `cont?.step ?? 1` | input | `true` |
| 边界取待办 | — | — | — | — | 只读（决定取 `input` 还是 `steer`） |
| 非续步且待办非 steer | — | — | **=1** | — | **=true** |
| move 命中 → Moved | 导出 | — | 导出 | 导出 | — |
| Location 关闭 → Reloaded | 导出原值 | — | 导出 | 导出 | — |
| 手动压缩完成 | **=false** | — | — | — | — |
| 提升输入成功 | — | — | **=1** | — | — |
| Step 完成 | **=false** | **=runStep 返回值** | **++** | **=true** | **=false** |
| attempt 内重试/续写/压缩 | — | — | — | — | — |

两条容易写错的语义：

- **`stepped` 会跨 Location 传递。** Moved/Reloaded 只在"本轮没有 continuation"时才导出 `stepped`，目的是让新 Location 的 drain 即使无输入可提升，也补跑一次 idle hook。测试 `runs the idle hook after a move delivered at the idle boundary` 断言第一次 drain 返回 `Moved({stepped:true})` 且 `idles` 为空（`test/session-runner.test.ts:1311`）。
- **`step` 只被两件事重置**：新输入被提升，以及边界处的 queue 投递/进入。重试、续写、压缩、溢出恢复**都不重置也不自增**——它们全在同一个 `runStep` 里循环。

### 1.2 四个 return，三条 Complete 路径

`advanceToStep` 整体包在 `uninterruptibleMask` 里，出口只有这些：

| 位置 | guard（翻译） | 返回 |
| --- | --- | --- |
| `llm.ts:74` | 全新一轮且 inbox 为空 | Complete |
| `llm.ts:76` | steer 作用域遇到 `queue` 投递且不是控制项 | Complete |
| `llm.ts:86-93` | Location 已关闭 | Reloaded（先关 model transport） |
| `llm.ts:125-129` | 下一个可提升项是 move | Moved（先投递，再原子发 Moved） |
| `llm.ts:170` | 手动压缩失败 | 向上抛（先发 `Compaction.Failed`） |
| `llm.ts:177` | 边界只剩控制项，或本轮没跑过 Step | Complete |
| `llm.ts:184` | 跑过 Step、无 pending，idle hook 之后仍无输入 | Complete |
| `llm.ts:206` | 准备完成 | Ready |
| `llm.ts:214` | Ready 之外 | 原样返回 |

所以 `Complete` 有四个返回点，语义上三条路径：入口无待办（含 steer 作用域过滤）、边界只剩控制项/从未跑过 Step、idle hook 跑完仍无输入。第三条的"再查一次"不是冗余——插件可以在 idle hook 里塞入新工作。

`Ready` 分支内部还有两个硬条件：`promote` 返回 `undefined` 表示"准备期间有控制项抢占了边界"，此时回到 while 顶部重来；只有"首个未命名会话"才会起标题生成 fiber。

### 1.3 一次 attempt 的六种归宿

`runStep` 是一个 `while(true)`，反复跑 `steps.attempt`（一次 = 一次 `llm.stream`）。`recoverOverflow`、`recoverContinuation`、`retryAttempt`、`assistantMessageID` 四个字段跨 attempt 存活。

| 触发条件 | Outcome | 对 step 的影响 |
| --- | --- | --- |
| 未产出内容 + 上下文溢出 + 允许恢复 | `Compacted` | 不增；关掉本步的溢出恢复，换新消息 ID 重跑 |
| 传输层要求整包重建 + 未产出内容 | `RecoverFull` | 不增；只允许一次立即重建 |
| 可重试错误 + 未产出内容 | `Retry` | 不增；记录 attempt，退避后重发 |
| 同上但已产出内容、无拒绝、工具未中断 | `Continue` | 不增；发续写提示，换新消息 ID |
| `finish === "length"` | `OutputLimit` | 不增；见 1.5 |
| 其余（含 `toolChoice:"none"` 强制 `needsContinuation=false`） | `Completed` | 由返回值决定是否 `step++` |

关键结论：**只有 `Completed` 会推动逻辑步号**。上面五种全都留在同一个逻辑 Step 里，只是换一次"物理尝试"。这就是为什么"一个 Step"和"一次 HTTP 请求"必须分开命名。

### 1.4 重试预算：10 次、约 84 秒、谁不花额度

`model-retry.ts:22-45` 的退避是 `min(exponential 2s, spaced 10s)` 再取 `recurs(10)` 的较大值：间隔依次是 2s、4s、8s，之后 7 次 10s，**共 10 次重试、最多 11 次物理尝试**，全程 jittered（测试按 0.8–1.2 倍窗口断言），注释给的总等待约 84 秒（67–101 秒）。

三个独立预算：

- **`Retry-After`**：上限 15 分钟，且是**下限**而非上限——`Duration.max(delay, retryAfterMs)`，只对 `RateLimit` 与 `ProviderInternal` 生效。
- **无网**：独立的 1 小时窗口，2s 起、单间隔上限 30s，不消耗通用额度（`ENOTFOUND / EAI_AGAIN / ENETUNREACH / EHOSTUNREACH / ENETDOWN`）。
- **超时**：`MAX_TIMEOUT_RETRIES = 3` 的硬上限。

插件可以通过 `session.retry` hook 改延迟或直接否决。`attempt` 从 1 起、返回前自增，所以首次重试的 `attempt` 是 2。

### 1.5 OUTPUT_LIMIT 为什么是 3

判定不看内存计数，而是查持久化的行（`llm.ts:347-364`）：

```sql
select type, json_extract(data, '$.finish') as finish
from session_message
where session_id = ? and type in ('user','assistant')
order by seq desc limit 3
```

`exhausted = 最近 3 条都是 assistant 且 finish 都是 "length"`。

3 的含义是"当前这条 + 2 次续写"：查询包含刚以 `length` 结束的当前行，所以错误文案是 `Response still truncated after two output token limit continuations`。`type in ('user','assistant')` 把 synthetic 续写提示排除在外，nudge 不会自己打断连击；而任何真实用户输入都会插入一行 `user`，从而打断连击并重新计数。

测试 `caps output token limit continuations and resets for new input` 把三段都钉住了：连续 3 次截断后抛 `output-limit`、请求数 3、synthetic 2 条；replay 投影后再 resume 仍然失败（请求 4）；新输入后重新计数（`test/session-runner.test.ts:6276-6316`）。

## 2. claim 与中断：三种结束方式的账本

{{< fig "diagrams/01-claim.svg" "执行声明：写前记账，正常才销账" >}}


### 2.1 写前意图

claim 不是锁，是"上次没好好结束"的标记。它与 `Execution.Started` 在同一事务提交，落库为 `session_v2.time_suspended`；`time_updated` 被刻意钉住，避免污染会话排序；null guard 让重复 claim 变成零行 no-op。

### 2.2 释放矩阵

| 结束方式 | 释放 claim？ | 额外动作 |
| --- | --- | --- |
| 成功 | 是 | 发布 `Execution.Succeeded` |
| 失败 | 是 | 发布 `Execution.Failed` |
| 用户 interrupt | 是 | `jobs.cancel`，发布 `Interrupted{reason:"user"}` |
| shutdown 中断 | **否** | 只清 retry 状态，不写 idle |
| 进程死亡 / SIGKILL | **否** | 没有 finalizer，claim 留在库里 |
| 恢复超预算 | 是 | 发布 `Failed{RESUME_EXHAUSTED}` |

`release` 同时把 `resume_attempts` 归零。子会话的孤儿 claim 在启动扫描时统一清掉，父会话保留。

### 2.3 恢复预算与注入文本

`DEFAULT_MAX_ATTEMPTS = 10`，语义是"单轮最多被恢复 10 次"。计数在 resume **之前**先持久自增——所以恢复途中再崩也逃不掉，`attempts > max` 时改发 `Execution.Failed` 并借同一次 commit 释放 claim。

恢复时注入的文本是固定的一句：

```text
The server restarted while you were working. Continue from where you left off
without repeating completed work.
```

以 `Synthetic` 事件投递，`metadata: { notice: "restart" }`。后台 shell/图片任务另有文案，且 `resume: false`，以免复活空闲持有者。

每次 drain 起始还会跑 `settleStaleToolCalls`：把上个进程遗留的 `streaming` / `running` 工具发布为 `Tool.Failed`，错误体是 `aborted`；孤儿压缩收尾为 `compaction.interrupted`。

### 2.4 三种中断的差别

| 入口 | 走哪条路 | 结果 |
| --- | --- | --- |
| 用户 interrupt | `Session.interrupt` → `execution.interrupt` | 设置 `stopping`、清空 `pendingWake`、fork 中断后**立即返回**；随后按 inbox 下一个可提升项决定是否以 steer 作用域续跑 |
| shutdown | 不经过 coordinator，scope 直接打断 fiber | `interruptionReason` 为 undefined，默认 `"shutdown"`；不取消 job、不释放 claim |
| Location 关闭 | 不是中断，是边界换乘 | 先关 model transport，返回 `Reloaded`，由 execution 用同一组参数递归重跑 |

用户中断之所以"立即返回"，是因为调用方拿到的是"已受理"而不是"已清理"。真正清理在 fiber 的 finalizer 里异步完成，`awaitIdle` 才等得到收敛。

### 2.5 finalizer 的顺序

这个顺序决定"中断时已产生的内容会不会丢"：

```text
捕获流 Exit → publisher.streamed() → interruptTools → Fiber.awaitAll
  → 落账工具与 Step 事件 → failCause / 返回 Outcome
```

所以中断永远**先结算已产生的内容，再向上抛**。run-coordinator 的 `onExit` 顺序是：清 owner → 调 `settled` hook → `settle`，因此观察者看到 claim 释放时，忙期已经结束。

测试 `does not start another physical attempt after interruption during retry backoff` 断言：退避中被打断后请求数仍为 1、只发过一次 `session.retry.scheduled.1`、没有 `session.synthetic.1`（`test/session-runner.test.ts:6059`）。

## 3. 工具与 hook

### 3.1 snapshot 的缓存身份是一个二元组

每次 `Tool.snapshot(permissions)` 都会新建 definitions 数组与 execute 闭包，但模块级只有一个 catalog 缓存，键是 `catalog.data === data && catalog.names === names`。

- `data` 是 State 的整份注册表。任何 transform/reload 都产生**新对象**且绝不改旧对象，所以 `data` 一变就必须重建——沿用旧 catalog 会让提示词里的签名与实际 schema 分叉。
- `names` 是 CodeMode 工具名用 `"\0"` 拼起来的串，专门区出"data 没变、但权限把某些工具整只 deny 了"的情况。

代价是粗粒度：一次内容相同的 `reload()` 也会让缓存失效。直连工具的定义只在注册期转一次、跨快照共享引用。

### 3.2 hook：唯一能失败的是 before

`trigger` 串行执行、按注册顺序，并且把**同一个可变 event** 传给每个回调。

| hook | 语义 |
| --- | --- |
| `execute.before` | 唯一可失败事件（通道 `Tool.Error`）。失败即短路：工具不跑，**after 也不会触发**，step 侧只把这路调用标失败，step 继续 |
| `execute.after` | 不能"失败"，但能通过原地改写 `event.result` / `event.error` 替换结果 |
| `execute.after` 真抛错 | 变成 defect：异常逃出 `executeTool`，已算出的结果被丢弃，该调用以 `unknown` 结算 |

改名与改输入也在这里：`event.tool` 与 `event.input` 可写。解析时用"本次请求的广告名→定义"表映射回真工具，所以 hook 把 `read` 改成 `cat` 也能执行到 `read`。这里有个不对称：**before 拿到的是广告名，after 拿到的是解析后的真名**，跨两个 hook 对齐只能靠 `event.id`。两种失败文案要分清：被本请求移除但注册表里有 → `Tool is not available for this request`；压根没注册 → `No tool named ...`。

### 3.3 规范化：内容永远非空

- `normalizeContent`：字符串包成单个 text；非空数组浅拷贝；空值用 `JSON.stringify(output)` 兜底。**不合并相邻 text**（合并只发生在截断测量时）。
- 图片只在 `type:"file"` + `image/*` + `data:...;base64,` 三个条件同时满足时才处理。阈值是 2000×2000、base64 ≤ 5 MiB；PNG 在 ≤384 KiB 或 ≤256×256 时原样保留；缩小走 32 级 ×0.75，质量 80/70/55/40。
- 三种降级：resizer 不可用 → 保留原图；解码失败 / 体积过大 → 丢图，并按原因各加一条 `[N images omitted: ...]`，追加在存活项之后。

发布侧对空 content 直接 die，所以"最终 content 非空"是一条硬保证，不是一个约定。

### 3.4 截断的精确规则

截断不在工具内，而在 `snapshot.execute` 之后、发布之前：

1. 全部 text 用 `\n` 连接 → split → 整体以换行结尾则 pop（尾换行不算一行）→ 按 UTF-8 字节计。
2. 未超限只写 `metadata.truncated = false`。
3. 超限：前 maxLines 行逐行累加，`size = byteLength(line) + (已有行 ? 1 : 0)`，超 maxBytes 立即 break；全文写 `tool-output/tool_<id>`。
4. 重写时 `remaining` 用的是**字符数**而非字节数，按序截断 text、跨项扣 join 换行；`file` 一律保留且不占预算；marker 插在截断处，`N=0` 记 `0 lines`。

`metadata.truncated` 只要被生产者设过（true 或 false 都算），通用截断就整只跳过。shell 正是利用这点：它自己保留**尾部**窗口，与通用截断保留头部相反。

### 3.5 CodeMode 的边界

解释器禁用得很彻底：没有 `setTimeout/setInterval`（等永不 settle 的 Promise 会报 `no timers or sleep`），没有 process/Bun/globalThis/fs/fetch，没有 `Function` 构造器，没有 class/getter/BigInt。全局表只有 Object/Array/Math/JSON/Date/RegExp/Map/Set/URL/TextEncoder/crypto/console/search/tools。

`fetch` 由扩展注入，复用 `webfetch` 的权限：每个 URL 断言、去 fragment、禁 `file:` 与 userinfo、手动跟重定向 ≤20 跳且每跳再断言、30s 超时。

内层调用回到主管线时走的是同一个 `run(tool, ...)`，所以 before/after hook、输入校验、内容规范化一个不少；区别是 `progress` 被换成 no-op，避免盖掉外层 `execute` 的调用行。

预算有两个：目录 `INLINE_BUDGET = 2000` 字符（约 500 token），增量 `UPDATE_BUDGET = 1000` 字符在 restated 与 added 之间分摊；增量不短于整篇就整篇重述。

引擎有两个（`legacy` 与 `vm`），由 flag `codemode_vm` 决定，dev/local 默认开、release 默认关。注意主 execute 路径**不传** `ExecutionLimits`（没有超时/调用数/输出上限），只有 `Tool.check` 传。

### 3.6 四类错误的判定顺序

| 类别 | 判定 | 归宿 |
| --- | --- | --- |
| `Tool.Error` | runtime 把非 Tool.Error 一律包成它 | 模型看到 error 结果，step 继续 |
| `ToolFailure` | `extends Tool.Error`，带 cause 与 metadata | 同上，优先用 cause 的 message |
| Defect | 只 catchTag Tool.Error，die 逃出 fiber | 该调用先结算成失败（type unknown），不中断时 step 不失败 |
| Interruption | `Exit.hasInterrupts` | `TOOLS_INTERRUPTED` → `STEP_INTERRUPTED` |

结算时的判定序是 `declines > interrupted > failure > providerFailed`。唯一的"defect → typed"回退是权限拒绝与 question 取消：它们用 defect 穿过叶子工具的 `mapError`，到请求层才被 `catchCauseFilter` 还原成模型可见的失败。


## 4. 权限与 MCP

{{< fig "diagrams/03-ask.svg" "一次 always 会顺手解决另一个挂起" >}}

先修正第四篇里两处偷懒的说法：**MCP 断线后不会自动指数退避重连**（被动断开停在 failed，要人工触发）；**Unattended 不是"auto-accept 模式"**，它是一个 `Context.Reference`，由定时任务注入。

### 4.1 求值顺序：deny 在 hook 之前

`evaluate` 本身只是 last-match：`findLast` 匹配 action 与 resource，无匹配默认 `ask`。真正有意思的是 `evaluateInput` 的编排：

```text
rules = (agent.permissions ?? [{*,*,deny}]) ++ session.permissions
if 任一 resource 在 rules 下为 deny: 直接返回 deny
all  = rules ++ savedRules(projectID)          # saved 恒为 allow
effect = 任一 resource 为 ask ? ask : allow
event = hook("permission","evaluate",{effect})
if event.effect != ask 或 Unattended 未定义: 返回 event
if 每个 resource 在 (all ++ unattended) 下都是 allow: 返回 allow
否则返回 deny + "needs approval, and nobody is present"
```

两个次序含义很深：

- **deny 预判只看 configured 规则**。所以 saved allow 压不过 agent/config 的 deny，但压得过它们的 ask。
- **deny 在 hook 之前返回**。插件无法放宽配置层的 deny，只能在 allow / ask 之间改判。这不是遗漏，是权限边界。

### 4.2 ask 没有显式状态枚举

状态由 pending 表与事件推断：`created`（Deferred + `pending.set`）→ `asked`（发 `permission.asked`）→ `replied` → `settled`（从 pending 删除后 `list`/`get` 不再返回）。

- **`always` 的唤醒是 FIFO**：先写 saved，再醒自己，然后按 Map 插入序遍历剩余 pending，逐项重跑 `evaluateInput`（此时能读到刚写入的 saved），只 wake 那些变成 `allow` 的。
- **`reject` 会取消同 session 的全部 pending**，并把同一条反馈广播给它们。
- **去重只有 request id**：重复 id 直接 die；同一工具并发两次会产生两个请求，没有按 (action, resource) 的内容去重。
- `reply` 是 uninterruptible，但读-删之间仍有 yield（发事件、写库），没有额外加锁；重复提交靠 Deferred 只成功一次 + `onConflictDoNothing` 兜底。

### 4.3 saved 规则没有 effect 列

落 `permission` 表，唯一索引 `(project_id, action, resource)`，**没有 effect 列**——读出来一律当 `allow`。写入条件是 `reply === "always"` 且工具给了 `save` 列表（MCP 工具固定 `["*"]`）。

key 里的 projectID 决定作用域：**跨会话生效、跨进程保留，跨项目不生效**。合并点就在 `evaluateInput` 里，追加在 configured 之后。

### 4.4 两条拒绝通道

| 通道 | 触发 | 后果 |
| --- | --- | --- |
| `CorrectedError` | reject 且带 message | 该工具变成 ToolFailure，模型读到反馈、继续本步 |
| `DeclinedError` | reject 无 message，或会话关闭 | 转成 **defect**，穿透到请求层才还原，`failAssistant` 并直接中断整步 |
| `BlockedError` | 配置 deny | 既不是 decline 也不结束 Step，直接变 ToolFailure |

关键差别：`Declined` 不向模型产出工具结果，`Corrected` 会。前者是"这件事到此为止"，后者是"换个方式做"。

### 4.5 Unattended 是注入的，不是开关

它来自 Effect 的 `Context.Reference`，默认 `undefined`。提供点只有两类：定时任务的检查按扫描结果构造 allow 规则注入；无 UI 的 HTTP 建任务注入空数组，等于一律拒。

只有"hook 之后仍是 ask 且已定义"时才进入，要求每个 resource 显式 allow，否则降级成 deny + 固定文案上抛——不排队、不问人。

### 4.6 MCP 的状态机

{{< fig "diagrams/02-mcp-state.svg" "每个 MCP server 一台独立的状态机" >}}

状态是 `pending → connected | failed | needs_auth | disabled`。`start` 的顺序是：置 pending → 校验 URL → fork scope → 取 authProvider → `connect`（**initialize 与 tools/list 一起做**，避免连上却空表）→ connected；失败按 `NeedsAuthError` 分流到 `needs_auth`。

重连的真相：

- 触发只有四类：会话过期（HTTP 404 或 400 "Server not initialized"）、凭证切换、配置变更、显式 `connect`。
- 会话过期时前台 `recovering` 停再起并**重试一次**；后台变更触发的过期只是 `fork(recover)`。
- **没有指数退避的重连循环**。被动断开停在 failed，需要人工从 `/mcps` 重连。
- 唯一退避在 `connect` 内部：250ms 起指数、最多 2 次，受 startup deadline 约束。

断线时正在飞的调用会以 `ConnectionClosed` 拒绝，包成 `ToolCallError` 再到 `ToolFailure`——**不重试、不排队**。

### 4.7 隔离靠四层

| 层 | 机制 |
| --- | --- |
| 每个 server | 一个 entry + 一把 `KeyedMutex`，生命周期操作串行 |
| 转场 | `forkIn(root)`，客户端断连也不会让 connect 停在半路 |
| 并行 | servers scope 以 `"parallel"` fork，一个慢 server 不拖住其余 |
| 连接池 | 某 key 握手失败只标记 unpoolable 并回退私有连接 |

读者侧还有 startup / attempt 两个 latch 保护：`connected()` 只等 latch 不轮询；配置替换失败也 `ensuring(startup.open)`，避免读方永久挂住。

### 4.8 OAuth 与 token

授权码流程自带 loopback server，state 是 32 字节随机，`iss` 全程透传，也支持粘贴回调 URL。**有 PKCE**：verifier 交给 SDK 生成与校验，测试断言 token 请求带 `code_verifier`。

token 存 `credential` 表，integrationID 由 `"mcp_" + sha1(name \0 url)[:16]` 生成——同名不同 URL、同 URL 不同名都不会串号。刷新时机由 SDK 判断（过期或 401），`expires = 0` 表示不过期；同一 refresh token 的并发刷新会合并成一个请求；失效时只在 refresh 仍等于自己呈递的那枚时才删行。

### 4.9 超时

| 阶段 | 默认 |
| --- | --- |
| 初始化 / 启动 | 30s |
| 工具、提示、资源列表 | 30s（另叠最多 2 次瞬态重试） |
| 调用 | 12h；attached 固定 10min |
| 会话 terminate | 1s |
| 模型侧等启动 | 2s grace |
| attached 列表刷新/接管 | 5s |
| OAuth 401 探测 | 5s |

列表的最坏耗时因此约是 3 倍 catalog 时间——这是"首屏工具表"可能慢一拍的原因。


## 5. 记忆的状态与算法

{{< fig "diagrams/04-instruction-state.svg" "一行状态，四个字段，六个迁移" >}}

### 5.1 指令是哈希映射，不是文本

`instruction_state` 每会话一行，四个字段：`epoch_start`、`through_seq`、`initial_values`、`current_values`。两个 values 列都是 `{key: sha256}`，**文本存在 `instruction_blob`，表里只有指针**。

渲染分两路：请求开头用 `initial_values` 渲染全量基线；历史里的增量文本冻结在 durable 事件的 `text` 字段，重放时不再读源——因为再渲染需要原始 Location 作用域的源，而那时它可能已经不存在了。

### 5.2 AGENTS.md 是向上走，不是 BFS

这一点常被写反。`AGENTS.md` 的发现是 `fs.up`：从 `location.directory` 逐级向上，每层先查当前目录再退父目录，命中顺序近→远，遇到 `stop` 目录即停（`util/fs-util.ts:148-172`）。`stop` 是项目根；如果 location 在 `$HOME` 下则取 home。没有深度参数，深度由目录树决定。

拼接顺序是 `全局 → 近→远 项目 → config 配置`。同一个 key 撞车是**硬错误** `DuplicateKeyError`，不存在覆盖；同一路径内容更新则后写覆盖内容，但保留首次插入的位置。

**技能目录才是 BFS**：队列逐层推进，`MAX_DEPTH = 6`，跳过点目录与 `node_modules/.git/.venv/venv/__pycache__`；某目录一旦含 `SKILL.md` 就收下并停止下钻；depth 0 额外收根目录的 `*.md`。同 id 技能由配置源顺序决定，后加载者胜。

URL 源只有技能有：拉 `index.json` 做 schema 校验，逐项检查 name 安全、必须含 `SKILL.md` 或 `<name>.md`、路径不逃逸且同源；缓存根是 `cache/skills/<hash>`，用 `.opencode-version` 比对，版本不同时下载到临时目录、校验入口文件、再在 `uninterruptible` 段里原子换版，失败回滚。

### 5.3 diff 算法

key 是源自己的命名空间：`core/instructions`、`core/skill-guidance`、`core/mcp-guidance`、`core/codemode`、`core/date`、`core/environment`、`api/<entry key>`，按"最共享→最局部"排列，保住前缀缓存。

哈希是 **canonical JSON 的 SHA-256**（对象键排序、数组保序、无空白），所以重排文件数组会换哈希，重排对象键不会。

diff 三个分支：

- 源 `unavailable` → 跳过（首次即阻塞）；
- `removed` → 只在 previous 有该 key 时写删除哨兵，**删除是显式哨兵而不是缺 key**；
- 其余 → 算哈希，与 previous 相同就跳过，不同才写 hash 并入 blobs。

文本有三种渲染：删除、新增、修改。列表型源（技能/MCP/reference/codemode）走 `listUpdate`：三项都空（纯重排）就整表重述，否则按 added/changed/removed 分段；技能列表按 id 排序，顺序不会抖动。discovery 的修改有"取短"规则：unified diff（context=3）与整段重述比长度，取短者。

**空 diff 不发事件**：非 initial 且 delta 为空时直接 return。没有新事实的 durable 事件只会污染 seq 与历史。

### 5.4 迁移表

| 触发 | 表操作 | before → after | 发指令事件 |
| --- | --- | --- | --- |
| 首轮 / revert 后 prepare | `initialize`（INSERT onConflictDoNothing） | 无行 → 两值相等、epoch_start=through_seq=seq | 是，带 `initial=true`、无 text |
| 每个 Step 的 diff | `apply`（UPDATE through_seq、current_values） | current 变，initial 不变 | 是，带冻结 text；空 delta 不发 |
| `compaction.ended` | `advanceEpoch` | `initial := current`，两个 seq 前移 | 否 |
| `revert.committed` | `rewind`（DELETE where through_seq >= boundary） | 有行 → 无行 | 否，下次 prepare 重建 |
| `session.forked` | 读父 current，子 `initialize` | 父 current → 子 initial | 否，fork 事件自带快照 |
| `session.moved` | 不动表 | 只改 directory/project_id | 否，下次 prepare 用新位置 diff |

### 5.5 五条不变量

1. `epoch_start ≤ through_seq`，只在 initialize / advanceEpoch / revert 重建时相等。
2. `initial_values == current_values` 恰好三种时刻：建行、advanceEpoch、revert 删行后重建。
3. 基线必须存在：找不到行直接 `Effect.die`，所以 fork 必须先铺行、不能懒建。
4. **压缩摘要不得含指令内容**：基线每个请求重渲染、增量靠历史重放，摘要再写一份既重复又会过期。所以压缩提示词明确要求省略这些，并注明"下一个 agent 会单独收到当前版本"。
5. **blob 只增不删**：缺任一 hash 直接 die，任何路径都不能回收 blob 行。

### 5.6 路径触发的注入

`read` 成功后，从目标目录向上找 `AGENTS.md` 直到 location root，过滤掉 Location 本身与正在读的文件，整段包在 catch 里——**发现失败绝不让 read 失败**。

账本不在单独的表：去重靠模型可见历史里 synthetic 消息的 `metadata.instruction.paths`，再加进程内 `inFlight` claim，让同一步的并行读只注入一次。

**压缩后为什么会重注**：账本只统计"最近一次 completed compaction 之后"的 synthetic；压缩把起点推到之后，旧 synthetic 落在边界外，账本自然变空，再读同一文件就重注。测试 `re-injects nested instructions dropped from history by compaction` 直接覆盖。

### 5.7 失效与竞态

- **无 state + 源不可用 = 阻塞**：首次基线是"全或无"，已 admit 的输入保持 pending 而不是被吞。
- **有 state + 源不可用 = 静默保留**：旧哈希继续生效，不写不删；这与 `removed`（源在但值没了）语义严格区分。
- **缺失与被拒的处理相反**（这点容易记反）：任一项目文件 `NotFound`（可能正在写盘）就令**整个源** unavailable，从而不发布「删除」、首次则阻塞；而 `AccessDenied`（被沙箱拒绝）只把该文件丢掉，源仍可用，效果等价于这个文件被移除。
- **指令变化让预计算摘要作废**：`heldIndex` 比较 `firstID`、基线渲染文本、model+variant，任一不符就作废并打断旧 fiber。
- **竞态**：刷新全程 `Semaphore(1)` 串行，watcher 100ms 防抖；fork 在同一事务里读 current 与快照；`rewind` 用水位一次 DELETE，而不是逐事件回放。

## 6. 运行面

五个运行面（shell / PTY / job / schedule / worktree）结构一致：内存里一份"活状态"，磁盘上一份"可恢复账本"；每个终态只有一个写入者，其余路径只竞争"谁先记录终态"，恢复完全从账本重放。

### 6.1 shell：四个状态，一个裁判

枚举是 `running | exited | timeout | killed`，但 `killed` **不是进程内迁移出来的**：正常路径只写 `exited` 或 `timeout`；`killed` 由删除路径合成——删除 running shell 时只发 `Exited{status:"killed"}`，等待方拿到 NotFound 时再补一个合成 Info。

```text
create → spawn → running → exited | timeout      （进程内，唯一写入者是 exit watcher）
running → (remove) → 发 killed 事件 + 从 map 删除  （没有 Info 终态）
```

几个顺序有语义：

- spawn 成功后才 `commands.set`，所以 `create` 返回前该 id 不会出现在 list/get。
- `Created` 在输出流 open 之后才发；事件顺序固定 Created → Exited。
- 终态写入前先等 `outputDone`，保证读到终态的读者不会看到半截文件；先 `succeed(done)` 再淘汰，保证被淘汰的命令仍返回终态而不是 NotFound。

**超时与退出赛跑**：`ended` 这个 Deferred 是唯一裁判，`end()` 先到先得。超时 fiber 在 uninterruptible 段里先尝试记录 `timeout`，只有成功才 detached kill——一旦记了超时就必须 kill，而 kill 脱离 timer，改超时、删除、teardown 都不会取消它。exit watcher 无论超时是否已记录都继续等真实退出，所以"超时后被 SIGKILL"得到的是 `status:"timeout"` + `signal:"SIGKILL"`。

运行中改超时从调用时刻重新计时；`timeout(0)` 表示清空且不设新定时器——工具层把前台命令转后台时正是用它撤掉默认的 2 分钟超时。

输出文件在 spawn 后创建，默认 `w`（id 唯一，等价新建）；进程内保留最近 25 个已退出，超出按 FIFO 淘汰并补发 Deleted；磁盘上每小时清理一次 7 天前的文件。

### 6.2 PTY：环形缓冲与激活屏障

缓冲上限 2MB，每次 onData 追加后超限就从头切片淘汰。`cursor` 是**绝对偏移**、按 UTF-16 code unit 计，不随淘汰回退。

attach 的回放规则值得单独记：`-1` 表示只跟随后续输出；请求偏移早于保留窗口时**静默截断到窗口头**，没有 truncated 标记。

传输层顺序固定为：replay 分片（64KB）→ cursor 元帧 → activate。attach 时先登记订阅但 `active=false`，chunk 先进 pending，activate 时按序 flush 再补发一次已记录的 end——这样客户端不会把回放当成实时输出。

`teardown()` 的顺序是：dispose listener → 仅 running 才 kill → `notifyEnd`（无 exitCode）→ 清空订阅者。location 回收时先 teardown 再清表，且**不**发 `pty.deleted`。

`holdLocation` 用引用计数 retain，只防 idle eviction；reload 等其它 shutdown 仍会发 1012，让客户端立即重连到替补实例。

### 6.3 job：两个 Deferred 的竞争

`start` 写 running，同 id 已 running 则复用、不重跑。终态由 `settle` 分类：成功 → completed，只有中断 → cancelled，其余 → error。

`block` 里 `done`（终态）与 `backgrounded`（转后台）用 `raceFirst` 竞速，**输者不取消 job**。所以"调用者转了后台"和"任务自己完成了"是同一场比赛的两种结局。

转后台的顺序是：先写 SQLite，再 `Deferred.succeed(backgrounded)`。`backgroundAll` 只挑"正在阻塞该 session"的 job，避免误伤别的会话。

跨进程恢复的顺序是有讲究的：

1. 取 active 集合、pending 行、running subagent 的 child 集合；
2. `suspended = (listSuspended ∪ children) - active`，并先 `releaseChildClaims`；
3. **非 subagent 先、subagent 后**——先让 shell 结果入库，再让被恢复的子会话发第一个请求；
4. shell/image 合成一条"服务重启已取消"的 synthetic（`resume:false`，避免唤醒闲置 owner），再删行；
5. subagent 计一次 durable resume（上限 10），重启并转后台，wait 后回传；
6. 最后才对剩余的顶层会话 `execution.resume`。

回传顺序是先 `sessions.synthetic` 再 `completeBackground`：先落消息、后删 marker，崩在两者之间只会以同 id 重发一次。

### 6.4 schedule：一次 tick 的八步

1. 选行：`active` 且到期且 `retry_at` 已过，按 `next_due_at` 升序，并发 `min(4, n)`。
2. 逐行：先 `claimLeader`——**每行都重新验一次租约**。
3. flag 关：写 `retry_at = now + 1h`，保留 `next_due_at`，开启后补一次。
4. 未消费：`last_message_id` 仍在 inbox → 只 `wake` 并推进，记 `skipped`。
5. fire：算迟到、算下一个 due，有 check 就先求值（不通过只推进并记 `checked`）。
6. 记账：`run = run_count + 1`，消息 id 固定 `msg_<row>_<run>`；admit 后写 run_count、last_fired_at、next_due_at。
7. 失败退避：prompt 抛错 → 5 分钟；目录消失 → 直接 paused；check 失败 → 1 分钟且 failures+1，第 3 次暂停并留一条说明。
8. 循环：无 active 行且已启动过就释放租约睡 30 秒；否则按最近的 next_due_at 截到 30 秒。

租约不是行级而是**每库一个 leader 行**（KV `schedule:leader`，TTL 60s，心跳每 20s 续），且只在 tick 期间持有。所以租约过期约等于"可被抢占"，最坏两个进程在 60s 边界各 fire 一次，靠消息 id 幂等兜底。

错过槽只补一次、不补发整段；cron 会跳过 DST 回拨的重复槽。

### 6.5 worktree：策略、幂等与失败边界

`Strategy { id, create, remove, list }` 加一个 `Editor { add, configure }`，`add` 兼作"选中"，**最后注册者成为默认**——所以插件注册顺序决定默认策略。

create 的失败边界很具体：先 mkdir 父目录，重名依次尝试 `-2..-10` 后抛错，再调 strategy.create，最后写 DB。**strategy 与 DB 之间崩溃会留下无主目录**，下次同名走后缀——没有补偿逻辑。

refresh 在一个事务里批量 upsert 发现项并删除消失项，**仅当有变化**才发事件。remove 必须命中带 strategy 的行，否则拒绝；策略失败时 DB 行保留。


## 7. 存储的内部结构

第四篇讲了 23 张表的分类与"JSON 越界"。这一节讲结构本身：索引为什么这样建、迁移怎么保证不回滚也不跳版、快照对象图长什么样、blob 的引用计数在哪漏。

### 7.1 表的形状：三处刻意的取舍

完整列目录在调研笔记里，这里只挑三处能说明设计意图的：

- **`session_part` 有两条外键**：`message_id` 指向 `session_message`，`session_id` 指向 `session_v2`，两条都 CASCADE。多一条冗余外键，换来"按会话直接删 part"不必先 join message。
- **`session_v2.parent_id` 没有外键**：父会话可以先被删而子会话留成孤儿，由 store 的自查逻辑处理。这里选择的是"不阻塞删除"，不是"数据完整性"。
- **`job` 完全没有外键**：后台任务要在会话消失后仍留下可查的失败记录，所以宁可留悬挂引用。

反过来，`schedule.session_id` 是指向 `session_v2` 的外键并级联删除——删会话等于撤掉它的定时任务，"任务无主即死"在这里是硬约束。

`blob` 的字节存在 SQLite 自己的 BLOB 列里，而不是文件系统：备份、迁移、加密只有一个单位，代价是每次读取要把整块读进内存。

### 7.2 索引：21 条，6 条唯一

| 索引 | 服务什么 | 没有它会怎样 |
| --- | --- | --- |
| `event(aggregate_id, seq)` UQ | 事件重放 | 重放退化为全表扫；更糟的是并发分配同一 seq 会静默分叉 |
| `session_message(session_id, seq)` UQ | 消息 keyset 分页 | 一页要扫全部再排序，且失去"同会话 seq 不重复"的强制 |
| `session_message(session_id, type, seq)` | 取最后一轮 user / 按 type 过滤 | 先扫全量再过滤，命中率极低 |
| `session_part(session_id, message_id)` | 批量取一屏消息的 part | 每条消息扫一次表，退化成 N+1 |
| `session_inbox(session_id, enqueued_seq)` UQ | 收件箱 FIFO 排空 | 排序要临时表；重复序号破坏投递幂等 |
| `schedule(next_due_at) WHERE status='active'` | 到期扫描与下次唤醒 | 全表扫；partial 让索引只装活任务 |
| `session_v2(time_suspended) WHERE NOT NULL` | 崩溃恢复 | 启动时扫全部会话；partial 通常只有个位数行 |
| `blob_ref(hash)` | 释放时判孤儿、媒体可读性 | 每删一个 blob 扫全部引用行 |

两条 partial index 是这套 schema 里最"划算"的设计：它们的索引大小与**活对象数**成正比，而不是与历史总量。

### 7.3 迁移：只有 up，但用静态校验替代回滚

生成链是：`schema.json` 作为 drizzle-kit 的基线 → 生成增量 SQL → 同时从零全量生成 `schema.gen.ts` → 生成 registry。`--check` 做三重断言：无未生成迁移、全量 schema 与迁移链一致、注册表与目录一致。

**"只有 up"不是疏忽**：回滚能力被替换成"全量 schema 必须等于迁移链终态"的静态校验，任何漂移在 CI 就失败。

apply 有两条路径：

- 见到 `session` 或 `session_v2` → 增量路径；
- 一张表都没有 → 空库全量：**一个事务**跑全量建表、旧凭据导入、并把所有迁移 id 一次性标记完成（它执行的是"迁移链的终态"，不是逐条 up）；
- 有表但没有会话表 → 直接 die，宁可拒绝启动也不猜。

journal 播种只发生在 `migration` 表为空时：旧版 Drizzle journal 有 `name` 列就直接搬；没有就把 `created_at` 折成时间戳前缀去注册表里找唯一一条。**找不到就 die 并报时间戳，绝不推测已完成**——宁可启动失败，不能把没跑过的 DDL 标成跑过。

事务边界：每条迁移各自一个事务，DDL 与 journal 行写在同一事务，所以单条原子、失败整条回滚且不留记录；但迁移之间没有全局事务，中途失败会留下"前 N 条已提交"的半升级库，下次从第一条未记录的继续。

`foreignKeys: false` 的处理值得单说：SQLite 的 `PRAGMA foreign_keys` 在事务内是空操作，所以必须在事务**外**放松、事务后恢复，并 `ensuring` 保证恢复。59 条迁移里只有 1 条用到它——重建 `workspace` 的"建新表→拷贝→删旧表→改名"，不放松的话引用行会在 drop 时被级联清空。

### 7.4 快照：只有 tree，没有 commit

一个快照**只有 tree 与 blob 两类对象，没有 commit、没有 ref**。`capture` 返回的就是 `write-tree` 的 tree id，目录层级各是一个 tree，文件内容是 blob。

没有 commit 是刻意的：快照不需要作者、时间、父指针，tree 本身就是内容寻址的完整状态。代价是 Git 自带的 `gc`/`prune` 会把它们当垃圾，所以打包必须自建。

快照仓在 `$DATA/snapshot/<projectID>/<hash(worktree)>`，通过 `objects/info/alternates` 指向源仓对象库——**只有源仓不存在的新对象才真正写进快照仓**。这带来一个必须点明的风险：源仓若被 `gc --prune=now` 清掉不可达对象，旧快照会读不出来。快照仓自己的 pack 只保证本地 objects 不丢，不复制借来的对象。

打包不是 `gc`：把全部松散对象与现有 pack 的全部对象显式喂给 `pack-objects`，再用新索引逐个核对 oid 都在新 pack 里，缺一个就报错且**不删任何东西**；全部核对通过才在不可中断段里删旧的，且 `.idx` 先于 `.pack`。

revert 的恢复算法是"按源树分组、先检出后删除"：路径按来源 tree 分组，每组一次 `ls-tree` 判断存在性，存在的用 `checkout <tree> --pathspec-from-file` 一次性检出，不存在的随后并发 16 删除。顺序是刻意的——一个删除失败不会阻断其它文件恢复。

### 7.5 blob：没有计数器，引用数就是行数

状态只有三种：没有 ref（不可读、等清扫）、被 ≥1 条 ref 持有、不存在。三条转移：

- **record**：只对**已存在**的 blob 建 ref；过滤这一步是关键，给已消失的 blob 写 ref 会让它永远无法被判为孤儿，等于复活塞。
- **sync**：先 record 新集合，再对"新内容不提"的差集批量 release。
- **release**：先删 ref，再删"没有任何 ref"的 blob。顺序是唯一安全的。

两个缺口：

1. **`blob_ref.message_id` 没有外键**。删会话时 CASCADE 带走 ref 行，但**不带走 blob 行**，所以 `releaseSession` 必须显式执行；绕过 projector 的删除会留下不可读的 blob。反过来，删单条消息不调 `releaseMessage`，ref 会永久残留，blob 被"假引用"钉住。
2. **没有孤儿清扫**。代码里没有任何按时间或无引用扫描 `blob` 的作业，唯一删除路径就是 release。于是"外部化成功但消息写入失败""CASCADE 摘掉 ref 却留下 blob"这些行会永久驻留——对读路径不可见，只占字节，且没有任何 API 能列出它们。库体积只增不减，只能靠整库重建回收。

`media_blob` 关闭时，写入侧降级为 base64 内联；但**读侧不看 flag**，只认 ref。所以"先开后关"不会弄坏已有媒体，只会让新写入走内联，同一会话里两种形态共存。

### 7.6 争用：一个进程内的锁，和一条不跨进程的假设

`locks` 是按绝对路径的 `Map<string, Semaphore>`，同一进程内共享，**不跨进程**——这是全篇最重要的假设：它假定同一时刻只有一个进程在引导这个文件。

事件写入用 `KeyedMutex`（按 aggregate ID）+ `begin immediate`。为什么必须是 immediate：这个事务先读 seq 再写，deferred 会先拿读锁、写时升级，而 SQLite 在升级遇到他人持写锁时可能直接返回 `SQLITE_BUSY_SNAPSHOT`，且**不受 `busy_timeout` 重试**；immediate 一开始就拿写锁，让 busy_timeout 从头生效。

单写者假设被打破时，逐层后果是：引导竞态直接起不来（被 `orDie` 包着，不是可重试错误）；序号竞态被唯一索引挡成插入失败（用户看到崩溃而不是排队）；非事件写入仍用 deferred，"先读后写"仍可能撞 `SQLITE_BUSY_SNAPSHOT`。


## 8. 三个共享模式

把八个模块的状态机放在一起，会发现它们反复用同样三种手法。

### 8.1 单写者 + 门铃，而不是锁

| 位置 | 串行化的 key | 机制 |
| --- | --- | --- |
| 会话执行 | Session ID | 一条 fiber + `pendingWake` 门铃，trampoline 防栈增长 |
| 事件写入 | aggregate ID | `KeyedMutex` + `begin immediate` |
| 指令刷新 | 全局 | `Semaphore(1)`，watcher 事件 100ms 防抖 |
| MCP 生命周期 | server | 每 server 一把 `KeyedMutex` |
| 权限回复 | 无显式锁 | `Deferred` 只成功一次 + `onConflictDoNothing` 兜底 |

共同点：**没有跨 fiber 共享的可变状态**，所有竞争被压到"最窄的 key"上；重复请求不是排队，而是合并（门铃）或幂等（唯一索引）。代价是正确性依赖"key 选对了"，选错就会出现文章里那些标"未确认"的竞态。

### 8.2 两阶段记账

系统里几乎每个不可逆动作都是"先记意图，再做事，最后销账"：

| 动作 | 先写什么 | 什么时候销账 |
| --- | --- | --- |
| 执行一个 turn | `Execution.Started` + claim（同事务） | 成功/失败/用户打断 |
| 调用一个工具 | `Tool.Called`（副作用前） | `Tool.Success/Failed` |
| 发一次请求 | 指令 delta 先落库 | 不销账，靠 epoch 换代 |
| 跑一次定时任务 | 消息 id 由 `run_count` 派生 | 推进 `next_due_at` |

这套记账的收益是可恢复、可审计；代价是**恢复只能做到 at-least-once**——claim 能告诉你"上次没结束"，但没法告诉你"外部副作用有没有发生"。所以恢复时只能把 running 的工具判失败，而不是重放。

### 8.3 错误只有三种出口

| 类别 | 例子 | 出口 |
| --- | --- | --- |
| typed failure | `Tool.Error`、`BlockedError`、`CorrectedError` | 翻译成模型可见的失败，Step 继续 |
| defect | `DeclinedError`（被 die 包装）、结算期的非法事件 | 穿透所有 `mapError`，到请求层才还原 |
| interruption | 用户 Esc、shutdown、Location 关闭 | 不翻译；先结算已产出内容，再向上抛 |

判断标准只有一条：**模型能不能对这件事做点什么**。能，就翻译成 typed failure；不能，就别假装它是工具失败。

### 8.4 对第四篇的两处修正

写这一卷时发现上一篇有两处说法不够准确，记在这里：

1. **MCP 不会自动指数退避重连。** 被动断开后状态停在 `failed`，要人工从 `/mcps` 重连；只有"会话过期"会触发一次自动恢复，且 `connect` 内部的退避也只有 250ms × 2 次。
2. **Unattended 不是 auto-accept 模式。** 它是 Effect 的 `Context.Reference`，由定时任务注入；客户端的 auto-accept 是另一回事。两者都会让 ask 不再弹窗，但机制与作用域不同。

## 9. 证据索引

| 主题 | 主要文件 |
| --- | --- |
| drain 状态机 | `session/runner/llm.ts` |
| attempt 与 Outcome | `session/runner/step.ts` |
| 重试预算 | `model-retry.ts`、`session/runner/retry.ts` |
| claim 与恢复 | `session/execution.ts`、`execution/restart.ts`、`session/store.ts` |
| 工具与 hook | `tool.ts`、`tool/runtime.ts`、`tool-output.ts` |
| 权限 | `permission.ts`、`permission/sql.ts` |
| MCP | `mcp/index.ts`、`mcp/connection.ts`、`mcp/client.ts`、`mcp/oauth.ts` |
| 指令状态 | `session/instruction-state.ts`、`instructions/index.ts` |
| 路径注入 | `tool/plugin/read.ts`、`session/instructions.ts` |
| 运行面 | `shell.ts`、`pty.ts`、`job.ts`、`schedule.ts`、`worktree/*` |
| 存储 | `database/`、`session/sql.ts`、`snapshot.ts`、`session/blob.ts` |

---

*第二卷的结论来自按模块分工的深度调研，再由主代理回源抽查；标注"未确认"的地方是静态阅读无法断定的，保留原样。*
