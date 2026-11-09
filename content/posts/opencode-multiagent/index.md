---
title: "OpenCode 的多智能体：委派、并行与「没有编排器」"
date: "2026-11-09T18:37:00+08:00"
summary: "OpenCode 的 multi-agent 是人格切换 + 一层委派 + 大量并行：子会话怎么开、深度为何默认 1、完成怎么回传，以及它刻意没有的编排器、黑板与投票。"
description: "OpenCode 的 multi-agent 是人格切换 + 一层委派 + 大量并行：子会话怎么开、深度为何默认 1、完成怎么回传，以及它刻意没有的编排器、黑板与投票。"
tags: ["opencode", "multi-agent"]
slug: "opencode-multiagent"
---

> 源码版本：`opencode 2.2.5`，commit `a687c047`（2026-11-08）。代码引用以工作区 `.opencode-src/` 为基准。
> 系列第九篇。前八篇讲的是系统怎么运转、怎么改代码；这一篇讲一个被问得最多的问题：**它的 multi-agent 到底是怎么做的**。

## 0. 先说清楚它不是什么

如果你带着 LangGraph、CrewAI、AutoGen 的印象来看，会找不到对应物。OpenCode 里：

- **没有编排器**。没有一个"调度 agent"去拆任务、分配、汇总；父会话只是普通地调用了一个叫 `subagent` 的工具。
- **没有共享黑板**。子会话看不到父的 transcript，父也看不到子的中间步骤；两边都只交换文本。
- **没有角色协商**。不存在多个 agent 对同一问题投票、辩论、互相评审的机制。

它有的是三件更朴素的东西：

| 原语 | 作用 | 本质 |
| --- | --- | --- |
| `subagent` 工具 | 开一个子会话去干一件事 | 层级委派，单向窄管道 |
| 并行工具调用 | 一个 Step 里同时跑多个工具 | 同一条 fiber 里 fork 多个子 fiber |
| 后台任务 | 把长任务转成异步，完成再通知 | job + synthetic 消息唤醒 |

再加一个容易忽略的第四件：**agent 切换**。同一个会话可以在不同人格之间切（build / plan / explore），这不是新进程，只是换了 system prompt、权限与模型。

所以更准确的说法是：**OpenCode 的「多智能体」= 多个人格 + 一层委派 + 大量并行**，而不是一个编排框架。

## 1. agent 不是进程，是人格

一个 agent 在数据结构上就是一条记录（`packages/schema/src/agent.ts:24-36`）：

```text
id / name / description / color
model? / request / system?
mode: "subagent" | "primary" | "all"
hidden / steps? / permissions
```

它没有进程、没有线程、没有独立的内存。所谓"切换 agent"，只是把这条记录换掉，然后：

- **system 段变了**（人格提示）；
- **权限变了**（工具目录跟着变）；
- **模型可能变了**（如果 agent 指定了 model）。

历史、会话、快照都不动。

### 1.1 内置的七个

| agent | mode | 能做什么 | 不能做什么 |
| --- | --- | --- | --- |
| `build` | primary | 默认人格，全工具，`question` 显式放行 | 无特殊限制 |
| `plan` | primary | 探索、讨论、在 `~/.opencode/plan` 写计划 | `edit` 被 deny（该目录除外）；但 **shell 没被 deny** |
| `general` | subagent | 通用多步任务 | 不能问用户（`question` deny）、不能再开子代理（`subagent` deny） |
| `explore` | subagent | 搜索：grep/glob/read/webfetch/websearch/shell | 先 deny 一切再逐项放行，所以不能编辑、不能再开子代理 |
| `compaction` / `title` / `summary` | primary + hidden | 压缩摘要、起标题、总结 | 不可被选中；后两个 deny 全部工具 |

`plan` 值得单独说：它的只读不是靠提示词，而是靠权限——`edit` 规则被 deny，只有 plan 目录例外；再加一个 `execute.after` hook，把"被权限拦住"翻译成一句更好懂的话（"不能用 edit 改 Plan 目录之外的文件"）。

但注意它**不是沙箱**：`shell` 没有被 deny，所以 plan 模式仍然能执行命令、仍然能通过 `bash` 改文件。所谓"只读"约束的是编辑工具，不是整个进程。

所以"plan 模式"本质上是**一套权限 + 一段提示 + 一个 hook**，不是另一个运行时。

### 1.2 mode 决定了它能不能被委派

`mode` 只有三个值，但真正起作用的是两个判断：

- 能不能被用户选中：`mode !== "subagent" && !hidden`；
- 能不能当子代理：`mode !== "primary"`。

`"all"` 两头都能。内置的 `build` 是 primary，所以它永远不能当子代理——你没法让 build 去"被委派"，只能让它去委派别人。

### 1.3 切换的代价

会话中途换 agent，立刻发生的是：system 段变（缓存前缀失效）、权限变（工具快照重建，但只影响下一个 Step——正在飞行的 Step 保留它开始时采样到的 agent）。不发生变化的是：消息历史、Step 计数、快照链。

还有一个坑：`switchAgent` **不校验 agent 是否存在**，未知 id 要到下一次请求才以 `AgentNotFoundError` 失败。

所以"先用 plan 想清楚，再切 build 去改"这个工作流是成立的——同一个会话，历史连续，只是人格换了。


## 2. 委派：subagent 是一条窄管道

{{< fig "diagrams/01-delegation-timeline.svg" "委派的两种等待方式" >}}

### 2.1 输入与输出

`subagent` 工具的参数很克制：`agent`、`prompt`、`description`、可选 `model`、可选 `sessionID`、可选 `background`。

返回有两种形态：

- 前台（默认）：`{sessionID, status: "completed", output}`——`output` 不是"子会话返回的某个值"，而是子会话**最近 20 条消息里最后一条 completed 的 assistant 文本**；内容包装成 `<subagent sessionID="…" state="completed">…</subagent>`；
- 后台：立刻返回 `{sessionID, status: "running"}`，子会话在后台继续。

失败时也带着 `sessionID` 一起回来——这是刻意的：模型可以拿这个 id 再调一次 `subagent` 接着聊，而不是从零重来。

### 2.2 深度默认是 1

深度不是"子会话记了个 depth 字段"，而是**沿 parentID 链往上数**：

```text
let depth = 0, current = parent
while (current.parentID) { depth++; current = get(current.parentID) }
limit = config.experimental.subagent_depth ?? 1
if (depth >= limit) fail("Subagent depth limit reached")
```

（`tool/plugin/subagent.ts:121-139`）

默认 `1` 意味着**默认只允许一层委派**：父可以派子，子不能再派孙。想嵌套得显式调 `experimental.subagent_depth`。

这个默认值很能说明设计取向：它不鼓励"agent 树"，只鼓励"父把一件独立的事包出去"。

### 2.3 谁能当子代理

`agents.resolve(input.agent)` 之后有一道硬检查：**`mode === "primary"` 的 agent 不能当子代理**。

所以 build（默认 agent）永远不能被子代理调用，能用的是 general / explore 这类可被委派的人格。权限上还有一道：`permission.assert({action: "subagent", resources: [agent.id], save: [agent.id]})`——**按 agent id 审批**，不是按任务内容。选 `always` 之后，同一个 agent 以后都不再问。

还有第二道闸，藏在内置 agent 自己的权限里：`general` 与 `explore` 都带了一条 `{action:"subagent", resource:"*", effect:"deny"}`。也就是说**即使把 depth 调到 2，这两个内置子代理也不会再往下开**。想真正嵌套，得自定义一个允许 `subagent` 的 agent。

两种"被拒"的后果不同：配置层 deny 会变成一条模型可见的 `ToolFailure`（"Subagent denied: X"），它能改道；用户交互式拒绝走 defect 通道，最终结束当前 Step——模型连"被拒绝"这句话都看不到。

权限的继承还有个容易搞错的点：**子会话继承父的 session 级 permissions，但不继承父 agent 的权限**。它自己的权限来自"子 agent 的 ruleset + 继承来的 session ruleset + saved approvals"，后者能覆盖前者，但推翻不了配置层的前置 deny。

### 2.4 复用同一子会话

传 `sessionID` 时不会新建，而是复用——但有两个前提（注意 `agent` 仍然必填，即使只是续聊）：

1. 该会话必须存在，且 `parentID` 等于当前会话（否则报"not a child of the current session"）；
2. 允许顺带换 agent / 换模型：换了 agent 才把 agent.model 纳入优先级，没换就只认显式 model。

这条路径的用途是"接着上次的子任务继续"，而不是每次重新开一个。

### 2.5 模型优先级

```text
新会话：  显式 model  >  agent.model  >  父会话的 model
复用会话：显式 model  >  （换了 agent 才取）agent.model  >  该会话原有 model
```

（`subagent.ts:183` 与上面的复用分支）

也就是说：**新建的子会话默认继承父的模型**（除非 agent 自己指定了）。所以"父用便宜模型、子用贵模型"要显式传 `model`；反过来同理。

复用时要小心：只有"换了 agent"才会采纳新 agent 的 model，否则沿用该子会话原来的模型——避免"接着聊"时模型被悄悄换掉。

### 2.6 第一句话

新建子会话时，prompt 的前面会拼一句固定前缀：

```text
You are a subagent spawned by another session.
<prompt>
```

复用时不再拼。所以子代理知道自己"是被派来的"，但它拿不到父的上下文——**这句话就是它全部的来历**。


## 3. 并行与争用

### 3.1 一个 Step 里可以并行跑工具

模型一次可能返回多个 tool-call。runner 的处理是：**每个调用 fork 一个 fiber**，全部等齐再结束 Step。

```text
每个 tool-call → forkScoped(执行 + 截断 + 发结果事件)
最后 → uninterruptibleMask 里 Fiber.awaitAll
```

（`session/runner/step.ts:122-158`）

`Tool.Called` 在 fork 之前就已经落库，所以"调用记录"的顺序等于 provider 返回的顺序；而执行完成的顺序是任意的。

这里有个容易写错的细节：**结果发布并不是串行的**。没有共享写者队列，各工具 fiber 并发发布自己的结果，安全性靠两条约定撑着：

1. 状态检查与标记是同步完成的（check 与 mark 之间不许 yield）；
2. 每条 fiber 自身的事件顺序就是它的来源顺序，消费端按 id/ordinal 折叠，不依赖全局位置。

真正串行的只有 Bus 对**同一 aggregate** 的提交——同一个会话的 Step/Tool 事件不会交错，但不同会话之间照常并行。

`uninterruptibleMask` 的作用也不是"排队"，而是保证即使已经被取消，仍能把终态事件写完。

### 3.2 跨会话是真并行

`SessionRunCoordinator` 的锁粒度是 Session ID：同一个会话串行，不同会话各跑各的 fiber。所以两个子会话、或者一个父一个子，确实在同时推进。

它们共享的是：同一个 SQLite（单写者）、同一个 Bus、同一个进程的 CPU。

### 3.3 争用点

| 资源 | 机制 | 并行时的表现 |
| --- | --- | --- |
| 事件写入 | 每 aggregate 一把 KeyedMutex + `begin immediate` | 同会话的事件串行，不同会话的在库级排队 |
| 文件写入 | 按解析后路径的进程内锁 | 同一文件串行；不同文件并行 |
| 文件读改写 | `edit`/`patch` 用模块级多键锁把"读→算→审批→写→格式化"整段串行；`write` 只锁 commit，预览读在锁外 | 同进程内 `edit` 冲突会变成 oldString 不匹配的 ToolFailure；并发 `write` 则是后写静默覆盖 |
| 模型调用 | 无并发上限 | N 个子会话同时跑 = N 份 token 同时烧 |

第三行的边界要说准。`edit`/`patch` 确实把整段读改写包在锁里，所以同进程内两个子会话改同一个文件不会撕裂写——第二个会看到第一个的结果，若 `oldString` 已不匹配就返回 ToolFailure。**冲突被转成模型可见的错误，而不是静默覆盖**，这是好的一侧。

坏的一侧有三点：

- `write` 只锁 commit，diff 预览用的读发生在锁外，两次并发 write 就是后写覆盖、没有丢更新检测；
- 锁是**进程内**的、按 realpath 取的，跨进程写、或经软链别名，都会落到不同的锁上；
- 锁键在 `withLock` 里用 realpath，而 `write` 内层用绝对路径，两者不一致时同一个文件可能拿到两把锁（读码推断，未见测试）。

还有个更隐蔽的冲突：两个子会话改**不同但相关**的文件（比如一个改接口、一个改调用方），锁帮不上忙，只能靠任务拆分时避免。

### 3.4 冲突：锁、匹配与三个缺口

{{< fig "diagrams/03-conflict.svg" "两个会话改同一个文件时会发生什么" >}}

一句话：**它不解决语义冲突，只保证写不撕裂，并把冲突变成模型看得见的一次失败。**

| 防线 | 做什么 | 位置 |
| --- | --- | --- |
| 路径锁 | `edit`/`patch` 把"读→匹配→预览→审批→写→格式化"整段包进模块级 `KeyedMutex`，key 是 `FSUtil.resolve` 后的真实路径 | `file-mutation.ts:69-83`、`edit.ts:355`、`patch.ts:275` |
| 匹配即检测 | 第二个 `edit` 在锁内读到第一个的结果，`oldString` 已不匹配 → 返回 `ToolFailure`（附带 missHint），模型必须重读再改 | `edit.ts:340-346` |
| 快照兜底 | 按 Step 拍的快照 + `revert`，最坏情况整步回滚 | `snapshot.ts`、`session/revert.ts` |

锁的取法有一个容易被忽略的细节：先把目标去重、按解析后的路径排序，再 `reduceRight` 逐层加锁（`file-mutation.ts:80-83`）。**多文件补丁按固定顺序取锁，避免死锁**；`transactionLocks` 是模块级的，所以同一个文件即使分属不同 Location 实例也互斥。

冲突的表现形态是关键：**不是覆盖，而是失败**。两个会话同时改同一个函数，第二个会收到 `oldString` miss，于是重读文件、重新生成编辑。这是一次"乐观并发"——只不过版本号换成了文本匹配。

三个缺口：

1. **`write` 只锁 commit**。它的 diff 预览读发生在锁外（`file-write.ts:92-100`），两次并发 `write` 就是后写静默覆盖，没有丢更新检测。所以 `edit` 比 `write` 安全，不只是语义更精确。
2. **锁只在进程内**。外部进程（编辑器、`git checkout`）不参与；注释写得很直白："Mutation locking is process-local and serializes cooperating OpenCode changes; external writes can still race."（`file-mutation.ts:71-74`）软链别名还可能让同一个文件落到两把锁上。
3. **`shell` 改文件完全绕过锁**。`sed -i`、`git apply`、格式化脚本都不经过 `FileMutation`——"一个会调用 edit、另一个用 shell 改"的组合，锁帮不上忙。

还有一类锁根本管不了的：两个子会话改**不同但相关**的文件（改接口签名 vs 改调用方）。这不叫写冲突，叫语义冲突，只能靠任务拆分避免。

**为什么不做更强的方案**（版本号、三方合并、检测外部改动就拒绝）？因为写入者是模型，不是人。模型能读、能改、能重试，所以"报错让它重来"比"自动合并"更便宜也更可预测——自动合并一旦合错，模型不会察觉；报错它一定会看到。

实践上就三条：同一个文件永远只交给一个会话；并行拆任务按**目录/文件**切，不按"功能"切；需要隔离时用 `worktree`，而不是让两个会话在同一工作区互相踩。

顺带一个对照：协同编辑领域常见的三种解法是 OT、CRDT 与 LWW（有[一篇专门的笔记](https://www.jasoncai.site/posts/ot-crdt-lww.html)讲这个）。OpenCode 三种都不用——它选的是**悲观锁 + 冲突即失败 + 模型重试**。原因还是那个：合并算法要能判断"语义上对不对"，而这里唯一能判断的是模型自己。


## 4. 没有的东西，以及为什么

{{< fig "diagrams/02-framework-contrast.svg" "左边是框架的构件，右边是它的替代物" >}}

### 4.1 没有调度器

父会话不会"把任务拆成子任务再分配"。它只是普通地调用了一次 `subagent` 工具——拆不拆、拆几个、派给谁，全由模型在那一轮决定。

有人会把 `plan` agent 当成调度器，这是个误会：`plan` 是一个**只读人格**（不能改文件），它的作用是"先出方案"，不是"分配执行"。

### 4.2 没有共享黑板

子会话的历史从零开始。父看不到子的工具调用、中间推理、失败重试；子也看不到父的上下文。两边只交换两段文本：

- 进去：一句"你是被派来的子代理" + 任务描述；
- 出来：子最后那条文本，或后台的一条合成消息。

子会话之间更是完全隔离——A 子会话不知道 B 子会话存在。

### 4.3 没有投票与互评

不存在"三个 agent 各写一版，再互相打分"的机制。想做类似的事，只能靠模型自己在一个会话里写三版、或开三个子会话再把结果贴回来——后者需要父自己做汇总，harness 不提供。

唯一近似"第二意见"的是 `/goal` 插件：它在 idle 钩子里让一个 reviewer 模型对当前进展给 JSON 判词，决定继续、暂停还是完成（上限 50 轮、连续 3 次停滞就停）。但这是**单会话内的 critic**，不是多个 agent 的 panel，也不参与任务分解。

### 4.4 为什么不做

三个理由，从代码里能看出来：

1. **编排会引入新的状态机**。任务图、依赖、重试、部分失败——每一样都要持久化与恢复，而 OpenCode 已经有一套"会话"状态机了；再叠一层，复杂度翻倍。
2. **模型自己会拆**。给一个能读能搜能改的 agent 加一个"派活"工具，它就能按需分解；把分解逻辑写死在代码里，反而限制了它。
3. **可观测性靠会话，不靠图**。每个子会话都是一等公民：有自己的消息、自己的成本、自己的快照。这比"编排层的执行计划"更容易调试。

代价是：**协作质量取决于模型的判断**，而不是框架的保证。同一个任务，不同模型拆出来的子任务可能差别很大。

### 4.5 内核没有，但插件可以

说"没有编排器"要补一句边界：**插件面足以在外部造一个编排器**。

插件可以创建并 prompt 任意会话、切 agent/model、订阅全部事件、调用无会话的 generate——这些合起来就是一套编排 API。`/goal` 插件正是"插件即编排器"的实例：它在 idle 钩子里让 reviewer 判进展，循环推进同一个会话。

所以准确的说法是：**内核不提供通用编排，但留了扩展点**；要不要编排、编排成什么样，交给插件作者。

顺带一个限制：`subagent` 工具本身是 `codemode: false`，所以**不能在 CodeMode 的程序里 fan-out 子代理**——程序化编排只能靠插件，不能靠模型写一段 JS。


## 5. 实践：什么时候该拆子代理

### 5.1 适合拆的三种情况

| 情况 | 例子 | 为什么适合 |
| --- | --- | --- |
| 上下文可以独立 | "在这个仓库里找出所有调用 X 的地方" | 子只需要自己的搜索历史，不需要父的编辑历史 |
| 结果可以压缩成一段话 | "评估这三个方案，给结论" | 父只要结论，不要过程 |
| 天然可并行 | "分别检查 a/b/c 三个目录" | 三个子会话互不依赖 |

### 5.2 不适合拆的三种情况

| 情况 | 为什么 |
| --- | --- |
| 需要同一份上下文反复改 | 子看不到父的编辑意图，容易改歪 |
| 要改同一个文件 | 进程内锁能防覆盖，但**语义冲突**防不了（一个改接口、一个改调用方） |
| 需要来回确认 | 委派是单向的：父只能在子结束后看结果，中途无法干预 |

### 5.3 并行的正确用法

模型一次返回多个 `subagent` 调用时，它们会在同一个 Step 里并行 fork。所以"fan-out 三个搜索"是可行且便宜的——但要注意：

- 每个子会话都是一份**独立上下文**，token 成本是相加的；
- 没有并发上限，十个子会话就是十份同时燃烧的请求；
- 子会话改文件时，按**目录/文件**切分任务，别让两个子碰同一个文件。

### 5.4 后台的用法

`background: true` 适合"启动一个长任务，父继续干别的"。父会在本轮结束时拿到 `{status:"running"}`，子完成后以 synthetic 消息唤醒父。

注意三点：

- 后台子的失败**不会**让父的工具调用失败（父早就返回了），失败信息只在合成消息里；
- 父被中断也不会取消后台子——只有删除会话才会连带取消；
- **只有后台子会话会写进 `job` 表**（前台子不落行），所以重启后能恢复的是后台那一类，前台子最多被清掉孤儿 claim。


## 6. 代价与边界

**信息在边界上被压扁。** 子会话的中间过程（读了哪些文件、试了哪些方案、失败了几次）在回传时全部丢失，只剩最后一段文本。父如果想验证，只能再派一个子去查。

**重复劳动。** 没有共享黑板，两个子会话可能读同一批文件、得出同一个结论。父无法"把已知的东西同步给所有子"，只能在 prompt 里重复描述。

**成本不可控。** 没有并发上限，也没有"子会话预算"。一个热情的模型可以一次 fan-out 十个子任务，父的账单上就是十份输入 + 十份输出。

**协作质量取决于模型。** 拆得好不好、派得对不对、汇总得全不全，全靠模型那一轮的判断。harness 只保证"派出去能跑完、能拿回来"，不保证"派得合理"。

**调试要靠会话树。** 出问题时得顺着 `parentID` 找到子会话，逐个看它们的消息与工具调用——这也是为什么每个子会话都是完整的一等会话，而不是一个轻量任务对象。

一句话：**OpenCode 把多智能体做成了"会话的树 + 工具调用"，而不是"编排的图"。** 它用更少的机制换来了更好的可观测性，代价是把协作的智能完全交给了模型。


## 7. 证据索引

| 主题 | 主要文件 |
| --- | --- |
| 委派工具 | `tool/plugin/subagent.ts` |
| 子会话创建与复用 | `session.ts`（create/fork）、`tool/plugin/subagent.ts` |
| 深度限制 | `tool/plugin/subagent.ts`（parentID 链）、config `experimental.subagent_depth` |
| 父子等待与回传 | `job.ts`、`session/subagent-job.ts`、`session/subagent-completion.ts` |
| 并行工具 | `session/runner/step.ts`（forkScoped + awaitAll） |
| 跨会话并发 | `session/run-coordinator.ts`、`session/execution.ts` |
| 写冲突 | `file-mutation.ts`、`tool/plugin/{edit,write,patch}.ts` |
| 人格 | `agent.ts`、`plugin/agent.ts`、`plugin/plan.ts` |
| 后台恢复 | `session/execution/restart.ts`、`session/background.ts` |

---

*这一篇的结论来自源码阅读与按模块分工的调研；标注"未确认"的地方是静态阅读无法断定的。*
