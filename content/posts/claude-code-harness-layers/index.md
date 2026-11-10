---
title: "四层地基：Claude Code 里那些模型看不见的决定"
date: "2026-11-10T13:12:00+08:00"
summary: "把 Claude Code 拆成输入队列、主循环、工具调度、委派与持久四层。从四个真实的数据结构出发，看它怎么把一次生成变成一件能跨小时、跨进程的长期工作；末尾补上从业务问题到 workflow 的那段拆解。配十张图。"
description: "把 Claude Code 拆成输入队列、主循环、工具调度、委派与持久四层：从四个真实的数据结构出发，逐层看一次生成如何变成一件长期工作。"
tags: ["harness", "architecture", "agent", "concurrency"]
slug: "claude-code-harness-layers"
---

> 依据：Claude Code 文档（`code.claude.com/docs`，2026-11-10 抓取）、`/plugin-types` 导出的类型面（`2.1.277`，13521 行）、本机二进制 `2.1.319` / `2.1.321` 的字符串。行文里用「文档」「类型面」「二进制」「推断」标注证据等级；所有推断都在句子里写明了理由。
> 上一篇文章把四家 harness 放在一起对照过（[循环之外](/posts/harness-orchestration-compare.html)）。这一篇只拆 Claude Code 一家，往下一层看。

## 0. 同一件事的两种视角

模型看不到 harness。它看到的是：一段系统提示、一串消息、一份工具清单。它不知道自己的话被排过队，不知道工具在哪个批次里跑，不知道结果被截断过，也不知道对面那个"子代理"其实是另一个自己。

但恰恰是这些它看不见的东西，决定了它的行为边界。把同一个模型放进两个 harness，一个能连续干四小时，一个十分钟就绕回原地，差别不在模型。

| 层 | 模型以为 | 实际发生的事 |
| --- | --- | --- |
| L1 输入队列 | 用户的消息直接就到了 | 消息先入队，在工具批次结束或 turn 结束时才被合并进来 |
| L2 主循环 | 我在一轮对话里 | 一个 turn 里有 N 次请求，每次请求前都可能触发压缩 |
| L3 工具调度 | 我调用的工具立刻执行 | 权限判决、分批、沙箱、截断、落盘都发生在执行前后 |
| L4 委派与持久 | 我叫了一个助手，它给了答案 | 默认只拿到一个句柄；报告要过安检；所有消息落在 JSONL 里 |

{{< fig "diagrams/01-four-layers.svg" "四层地基。模型在中间，四层各自补上一个它没有的能力：被插话、能持续、有边界、可委派且可回放。右侧是每层失效时的典型症状。" >}}

## 1. 先看四个形状

"harness" 这个词容易被说得很虚。它做的事，其实可以落到四个真实的数据结构上；后面每一节，就是把其中一个拆开。

```js
// L1：进队列的不是字符串
{ type: "prompt", value, priority: "next", agentId?, isMeta?, skipAttachments? }

// L2：一轮不是一次请求
turn.start → turn.step → turn.complete { answer, usage, isAborted, turnId }

// L3：能不能并行，要按调用判，不按工具名
isConcurrencySafe(input)   // 只有连续的安全调用才并成一批

// L4：委派先回句柄，不回答案
{ status: "async_launched", agentId, description, outputFile }
```

这四行分别回答四个问题：话怎么进来、循环什么时候停、工具怎么执行、活怎么交出去。它们都不在模型里，也都不是"智能"的——是状态机、策略表和边界条件。后面四节，一节拆一个。

## 2. L1 输入队列：话从哪里递进去

先看一个反直觉的设定：**排队消息不等 turn 结束**。

文档写得很清楚：如果一条消息在工具调用期间入队，它会在"这批工具调用结束"时、在同一个 turn 内交给模型。斜杠命令和 `!` shell 命令是例外，它们压到 turn 结束之后，再逐条执行。

原因是协议层面的：`tool_use` 和 `tool_result` 必须成对出现，中间不能插一条用户消息。所以"立刻送达"在物理上不成立，能选的只有"在哪个边界送达"。Claude Code 选了两个边界：**工具批次结束**和 **turn 结束**。

{{< fig "diagrams/02-input-queue.svg" "三条消息、两个投递点。A 落在工具批次边界，B 是命令只能等 turn 结束，C 走 Ctrl+Enter 的立即通道；立即通道优先把可后台的工作转后台，而不是杀掉它。" >}}

队列里的条目不是字符串。二进制里能读到它的形状：

```js
{ type: "prompt", agentId, value, priority: "next",
  origin, skipSlashCommands, isMeta, skipAttachments }
```

`agentId` 说明这个队列是**可寻址**的：它不只服务主会话。另一条字符串写得更直白：`Message queued for delivery to ${agentName} at its next tool round.` 给子代理的消息，投递边界是"它自己的下一个工具轮次"。`priority: "next"`、`isMeta`、`skipAttachments` 则是给投递策略留的开关：下一条优先、这条只是元信息、这条别带附件。

队列的边界由一个小轮询器守着。下面一对日志能看出它的工作方式：

```
[InboxPoller] Session idle, delivering N pending message(s)
[InboxPoller] Cleaning up N processed message(s) that were delivered mid-turn
```

空闲时投递；turn 中途投递过的条目在结束时清掉。也就是说，队列状态和 turn 状态是两套时钟，poller 负责在边界上把两者对齐。

三个交互细节都挂在这套机制上：

- `Esc`：中断当前回复或工具调用，已完成的工作保留；队列里的消息在下一个 turn 按顺序发出。
- `Ctrl+Enter`（`2.1.275+`）：立即发送。如果 Claude 正在跑可以转后台的活（shell、子代理），活转后台继续跑，Claude 在同一个 turn 里读消息；如果它只是在写回复或跑不可后台的东西，才真正中断这个 turn。
- `↑`：从输入框第一行把队列取回来，逐条放回输入框改。

还有一个容易漏的：`/model`、`/effort`、`/fast` 不进队列，立即生效；其中 `/model` 从该 turn 的下一次请求开始用新模型。会话被转到后台时，队列跟着会话走，但命令不跟——命令依赖当前输入框和会话状态，队列条目只依赖消息本身。

这一层的难点从来不是存储。存一个数组谁都会；知道"这条消息只能在这两个时刻之一进去"，才是 L1 的全部。

## 3. L2 主循环：一次输入，几次请求，一块会满的预算

Claude Code 把 "turn" 当一等对象，不是一个 while 循环的副产品。类型面上有三个事件：

```ts
turn.start   → { turnId, agentId, ... }
turn.step    → 唯一带 streaming 的事件
turn.complete → { answer, durationMs, isAborted, turnId, agentId, usage }
```

`turn.complete` 里带 `isAborted` 和 `usage`，说明这一层要负责两件循环本身解释不了的事：**怎么结束**和**花了多少**。`turn.abort({ turnId })` 也不是杀一个 promise，而是取消这个 turn 派生出来的级联工作。

循环的骨架仍然是那个 for：

```js
while (true) {
  if (budget.exceeded()) await compact();
  const res = await stream(request);
  if (!res.toolUses.length) return finish(res);
  const results = await runTools(res.toolUses);   // L3
  append(results);
}
```

真正有意思的是 `return` 的条件。`stop_reason` 是一个八值联合：

| 值 | 含义 | 循环怎么办 |
| --- | --- | --- |
| `end_turn` | 模型说完了 | 结束 |
| `tool_use` | 模型要调工具 | 执行后继续 |
| `max_tokens` | 输出被截断 | 续写或收尾 |
| `stop_sequence` | 撞上停止词 | 结束 |
| `pause_turn` | 服务端要求暂停 | 可续状态，继续请求 |
| `compaction` | 触发压缩 | 压缩后重发 |
| `refusal` | 模型拒答 | 结束并上报 |
| `model_context_window_exceeded` | 窗口爆了 | 压缩或报错 |
| `null` | 没有原因 | 异常路径 |

（后四种的具体处理在文档里没有逐条写，表里标"压缩后重发""可续"的部分属于推断，依据是这几个值本身是可恢复语义，且引擎必须在 `tool_result` 之外还有别的继续理由。）

{{< fig "diagrams/03-turn-loop.svg" "循环的四步与八个出口。turn 的边界由用户消息划定，不由一次 API 调用划定；abort 取消的是整个 turn 的级联工作。" >}}

这里有一个设计取向值得记下来：**拒绝也是一种工具结果**。权限判决返回 deny 时，引擎不是终止循环，而是往对话里塞一条 `Permission denied for <tool>: <reason>` 的 tool_result，让模型看到"这条路被封了"，自己换一条。错误、超时、截断，全都走同一条回灌通道。循环只有一种输入形状，这是它能长期跑下去的原因。

### 上下文会满

上下文窗口是这个循环的油箱。但油箱多大，Claude Code 自己也不完全确定。类型面里有个 `ContextWindowSource` 枚举：`env / settings / clientdata / experiment / model-default / unknown-model / auto`。窗口大小来自环境变量、设置、客户端数据、实验开关，或者干脆是"未知模型的保底值"。

有了油箱，才有三档处理：

1. **microcompact**：只清工具结果，不动对话。按 `tool_use_id` 把结果落盘，保留最近 `keepRecent` 条，在消息流里插一个 `microcompact_boundary`。debug 日志是 `[KEEP-RECENT MC] context_hint trigger, cleared N tool results (~X tokens), kept last N`。
2. **auto-compact**：阈值取 `min(设置值, 模型窗口)`，可以用 `/autocompact 500k` 调。真的压缩，把前面的对话总结成一段。
3. **手动**：`/compact focus on ...` 指定重点；`/rewind` 里的 "Summarize from here / up to here" 是另一种入口。

为什么先清工具结果？因为工具结果是上下文里最便宜、最大块、最一次性的一部分。一个 `git log` 的输出可能有八千 token，看完就没用了；而"用户要的是把登录改成 OAuth"这句话只有二十个 token，删了就完了。先倒废纸，再写纪要。

压缩之后有一次**白名单重建**，这是整套机制里最像"公司规章"的地方：

- 无条件重注入：系统提示、output style、根 `CLAUDE.md` 里没有 `paths:` 的规则、auto memory、skill 描述、git status、当前 plan。
- 按需重载：带路径范围的规则、子目录的 `CLAUDE.md`，等模型真的碰到那个目录再读。
- 最多重读 5 个最近修改过的文件；单个文件超过 5000 token 就只留一行 `Referenced file` 路径。

还有一条保护：如果压缩之后很快又满、再压、再满（thrashing），压几次就停下来报错，而不是无限循环。这条比任何压缩算法都重要——它承认了"这个 turn 的信息量已经超出窗口"，而不是假装能一直压下去。

{{< fig "diagrams/04-context-budget.svg" "油表、三档压缩和压缩后的重注入白名单。阈值取设置值与模型窗口的较小者；microcompact 只动工具结果，auto-compact 才动对话。" >}}

记住这一条：压缩的难点是保留什么，不是怎么总结。

## 4. L3 工具调度：注册表、权限、沙箱、批次

这一层要同时回答四个问题：模型能用什么、能不能现在用、最多几个人同时用、用完的东西怎么交回来。

### 4.1 注册表：二十三种内置工具，其余按需加载

类型面里 `ToolName` 是 23 个内置工具的联合类型；MCP server 和插件再往里加。工具多到一个程度，把全部 schema 塞进上下文就不划算了，于是有了延迟加载：MCP 工具默认只报名字（`Deferred tools are announced by name in a message from the system`），模型真要用时再调 `ToolSearch` 取完整 schema。二进制里对应 `DeferredToolPlaceholder` 和一句 `Fetches full schema definitions for deferred tools so they can be called.`

这其实是 L2 的预算政策渗透到了 L3：工具的"说明书"也算上下文。

### 4.2 权限：三值判决，先命中先赢

判决只有三种：`allow`、`ask`、`deny`。规则匹配顺序是 **deny → ask → allow，先命中先赢**，跟规则写得多具体无关。

这条顺序会产生一个反直觉的结果：一条宽泛的 `Bash(rm *)` deny，会压过一条更具体的 `Bash(rm -rf ./build)` allow。规则不是"越具体越优先"的评分系统，而是三道闸门，第一道关了就没了。

六个权限模式（`default / acceptEdits / bypassPermissions / plan / dontAsk / auto`）改变的是"ask 这一档怎么处理"，不改变 deny 的优先级。plan 模式下除了 `ExitPlanMode`，连钩子的 allow 都不生效；钩子也压不过 deny。另外，如果 deny 规则写的是裸工具名，这个工具会直接从模型的工具清单里消失——不是"调用被拒"，是"看不见"。让模型不产生这个念头，比让它产生了再拒绝更省事。

`PermissionUpdate` 有六个动作、五个目的地（user / project / local settings、session、cliArg）。你在弹窗里点"以后都允许"，落到磁盘上就是往某个 settings 文件里加一条 allow 规则。一次性决定因此变成了持久策略，这也是为什么权限问题经常在"换了个目录"之后又冒出来。

### 4.3 沙箱：另一道正交的门

权限回答"要不要问人"，沙箱回答"就算不问，它最多能碰到什么"。两者不是一回事，Claude Code 把它们分开实现。

沙箱是操作系统级的，只管 shell 命令（Bash / PowerShell / Monitor）及其子进程：macOS 用 Seatbelt，Linux / WSL2 用 `bubblewrap` + `socat`。默认写权限是工作目录 + 每用户临时目录 + 你显式追加的目录；读权限是机器的大部分（包括 `~/.ssh`，除非你 deny）；网络没有直连，全部走本机代理，按 `allowedDomains` 逐 host 校验，默认空白名单。

沙箱默认是关的，要 `/sandbox` 或 `sandbox.enabled` 打开。打开之后有个很实用的模式：沙箱内的命令免弹窗（auto-allow），因为边界由操作系统保证，不需要人再签一次字。代价是失败时会有逃生舱——模型可以带 `dangerouslyDisableSandbox` 重试，这一次要重新批准，strict 模式可以彻底禁掉。

要记住的是**哪些东西不在沙箱里**：Read / Edit / Write 这些文件工具、MCP server、hooks、LSP、状态栏，全都跑在沙箱外，只受权限规则约束。沙箱不是万能罩子，它保的是"模型让 shell 干的事"，不是"模型让 harness 干的事"。

{{< fig "diagrams/05-permission-sandbox.svg" "两道门，两个问题。上半是权限判决：deny → ask → allow 三道闸门，先命中先赢。下半是沙箱边界：写白名单、读大部分、网络走代理；文件工具和 MCP 在边界之外。" >}}

### 4.4 并发：只合并连续的安全段

一次生成里模型可能一口气要调五个工具。哪些能并行，不是按工具名写死的，是按 **(工具, 输入)** 判定的：

```ts
isConcurrencySafe(input)  // 默认实现：isReadOnly(input)
```

基类返回 `false`；二进制里 `isConcurrencySafe(){return!0}` 出现 50 处，`return!1` 14 处，另有 3 处按输入判定（比如 Bash 要解析命令才能知道是不是只读）。分批器只合并**连续的**安全调用：

```js
if (safe && groups.at(-1)?.isConcurrencySafe) groups.at(-1).blocks.push(call);
else groups.push({ isConcurrencySafe: safe, blocks: [call] });
```

一个不安全的调用就是一道屏障：它前面的并行、它自己、它后面的并行，是三段，不能交叉。执行时安全段走并发执行器，非安全段串行。并发上限默认 10，`CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` 可调；另有一处路径把每次调用的槽位再封顶在 `min(上限, 10)`。

执行是流式的：工具调用块一边从流里出来，一边就能进批次开始跑，不用等模型把整段话生成完。钩子的粒度也印证了"批"是真实对象：`PostToolUse` 每个工具一次，`PostToolBatch` 每批一次。

{{< fig "diagrams/06-tool-batch.svg" "四个调用，三段。两个只读并行，一个写操作当屏障单独跑，后面那个只读再接着跑。批次边界同时是钩子和消息投递的边界。" >}}

### 4.5 取消与截断

`Esc` 或者 `$.turn.abort` 会级联取消这个 turn 派生的工作：正在跑的工具、子代理、后台命令。持久化那一节会呼应一句：中断时还在跑的工具，resume 之后**不会重跑**——因为"它跑到哪了"没有可靠答案，重跑可能产生重复副作用。

工具结果太长时，中间会被折叠成 `... [N characters truncated] ...`，头尾保留。特别大的结果落盘，对话里只留一个句柄；子代理的结果干脆只给 `outputFile`。截断不是 L3 的小聪明，是 L2 预算政策在执行层的落实：**上下文里每一块都要有人为它的大小负责**。

## 5. L4 委派与持久：句柄、回执、事件流

### 5.1 默认异步：委派的第一课是"别等"

Agent 工具的默认行为是后台执行。返回值的三种形状说明了这一点：

```ts
{ status: "completed", content, usage, toolStats }
{ status: "async_launched", agentId, description, outputFile }
{ status: "remote_launched", taskId, sessionUrl, outputFile }
```

只有第一种带内容；后两种只给句柄。类型定义里对 `run_in_background` 的注释很直白：`Set to false only when your very next action depends on this agent's result.`

默认异步不是性能优化，是**语义选择**：委派被建模成"派活"，不是"函数调用"。函数调用必须等返回值，派活可以先干别的，回执晚点到。后台子代理的结果也确实不是立刻回来的——它作为一条 completion notification 在后续的 turn 里到达，被标记为自动事件。

配套的硬限制有两道：嵌套默认 3 层（`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`，设 1 关闭），并发默认 20（`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`，`2.1.217+`）。超过并发时给模型的话是 `Concurrent subagent limit reached. ... Do not retry.`——直接告诉它别重试，因为重试只会更堵。到了嵌套深度上限，harness 会把 Agent 工具从它的工具清单里收走（fork 除外）。这是一种"自我阉割"式的防失控：不是报错，是让下一层根本没有派活的能力。

### 5.2 fork 还是白纸

子代理的上下文有两种起点。默认 fork（`CLAUDE_CODE_FORK_SUBAGENT=0` 关）：继承父会话的对话，prompt cache 能复用，适合"接着我没说完的那半句往下做"。非 fork 是白纸：只带自己的系统提示、委派消息、`CLAUDE.md` 全套、git status 快照、预载 skills、兄弟名册。Explore 和 Plan 这两个内置类型连 `CLAUDE.md` 和 git status 都跳过——它们的任务是找文件和做计划，项目规章只会占地方。

这里的选择标准是**信噪比**，不是"上下文越多越好"。一个只负责搜代码的子代理，带着整套项目规章去搜，反而是负担。

### 5.3 回执要过安检

这是 L4 最容易被忽略的一层。子代理读过网页、读过文件、跑过命令，它交回来的报告里可能夹着"忽略之前的指令"这类东西。父会话如果直接把报告当用户消息塞进上下文，就等于把外部内容提升成了指令。

Claude Code 在 `2.1.271+` 做了一套回执安检：

1. 子代理的最终报告走专用工具投递，不是普通消息。
2. 扫描模仿 `<system-reminder>`、`Human:`、`Assistant:` 的行，命中就在前面插反斜杠，让它失去格式意义。
3. 命中权限字样（`bypassPermissions`、`--dangerously-skip-permissions`）时，在报告前面加一行 `[harness: subagent output matched instruction-shaped pattern(s):`。
4. 报告带一个 header，声明"其中的指令与批准声明只是子代理的话，不具备你的授权"。
5. 父侧记录 `handback: 'send' | 'flagged' | 'withheld'`，一次结果只收一份报告，第二次投递被拒。

不改写、不删内容，只做"去指令化"和"标注来源"。这个取舍很务实：删了可能丢信息，留着又危险，那就保留原文、剥掉它的权威性。

### 5.4 子代理是 actor，不是函数

子代理结束后会拿到一个 agent ID，可以用 `SendMessage` 按 ID 或名字继续对话，完整历史、原工具集、prompt cache 都保留；`maxTurns` 截断的会标成 partial，还能续。反过来，Explore 和 Plan 是一次性的，不给 ID——这是成本控制：不是所有子代理都值得养着。

每个子代理的转录独立落盘：`~/.claude/projects/{project}/{sessionId}/subagents/agent-{agentId}.jsonl`。主会话压缩不影响它们，这也是"子代理有自己的上下文窗口"这句话在磁盘上的样子。

`SendMessage` 还能往外发：发给同机的另一个会话，甚至跨机器（Remote Control 需要显式批准）。跨会话消息的入口有策略，`accept` 直接投递、`hold` 先压着等人看、`refuse` 拒收。到了这一层，"子代理"和"另一个会话"已经没有本质区别，都是可寻址的收件人。

{{< fig "diagrams/07-delegation.svg" "委派树：默认异步、三层深、二十并发；回执先过安检再进父上下文；活着的子代理可以用 SendMessage 续跑。" >}}

### 5.5 另一种 fan-out：把编排写成脚本

前面那条路是模型逐次决定"再叫一个子代理"。还有一条路：把编排写成脚本，交给 Workflow 工具跑。脚本里用 `agent(...)` 起子代理，循环、分支、并发度都由代码持有，模型只负责写这段代码和读结果。

两条路的差别不在能力，在**可预测性**。模型逐次调用时，下一步叫几个、要不要等，都是生成出来的；脚本执行时，这些是写死的。字符串里有一句很说明问题的话：Workflow 派出的 agent "run outside the at-once bound on spawns"——它们不受"一次最多几个"的约束，因为并发由脚本自己管。

这也解释了 ultracode 那类模式为什么存在：它把"每个实质性任务都走 Workflow"写成系统提醒。代价是 token，收益是形状确定的 fan-out。

### 5.6 持久化：会话是一条可回放的事件流

主会话是 `~/.claude/projects/<project>/<session-id>.jsonl`，一行一条消息，追加写。项目名是 cwd 里的非字母数字字符替换成 `-`。

围绕这份日志有三组操作：

- **resume / continue**：从日志重放，接着跑。中断时还在跑的工具不会重跑。
- **fork**：从某条消息分叉出一个新会话，历史照抄，之后各走各的。
- **rewind**：checkpoint 每个 prompt 一个，保留最近 100 个，只跟踪编辑类工具；`/rewind` 给六个选项。

为什么是 JSONL 而不是数据库？因为这份数据的读取模式是"从头到尾读一遍"和"从中间截断"，不是"按 id 查一行"。追加写天然崩溃安全，一行坏了不影响前面；分叉就是复制文件；给人看也不用工具。checkpoint 和 git 是两套独立的东西——它记的是"Claude 改了什么"，不是"仓库变成了什么样"。

{{< fig "diagrams/08-persistence.svg" "一条只追加的 JSONL 时间线，和围绕它的三种操作：checkpoint 在改动处留个书签，fork 从某行分叉出一条新流，rewind 退回到某个书签。" >}}

## 6. 四层怎么咬合：一个 turn 的时序

把前面拆开的东西装回去，看一次输入从按键到落盘经过谁。图里左侧的标签，就是每一步所属的层：

{{< fig "diagrams/09-sequence.svg" "一个 turn 的完整时序。人、引擎、模型、本地文件四条泳道；每步都标了所属的层。模型只在 ③④⑫⑭ 出现，其余步骤都是引擎在做。" >}}

读这张图只需要抓住三件事：

1. **模型只有一条消息流。** 它看不到入队、分批、判决、压缩；它只看到 ③ 的请求和 ⑫ 回灌的 `tool_result`。
2. **文件只经引擎的手。** ⑥⑦ 的读可以并行；⑨ 的写要先过 ⑧ 的权限，并且要等读收尾——写操作是屏障。
3. **循环的边界由 `stop_reason` 决定。** `tool_use` 就回灌再发一次；`end_turn` 才落盘、交还控制权。

每一步都能单独把一次长任务搞砸：

| 故障 | 坏在哪层 | 现象 |
| --- | --- | --- |
| 消息插在 tool_use 和 tool_result 中间 | L1 | API 400，或者上下文里出现一条不合法的消息 |
| 压缩把当前计划丢了 | L2 | 模型反复问"我们刚才要做什么" |
| 压缩抖动没有刹车 | L2 | 一直压、一直满，token 烧完也没产出 |
| 宽泛 deny 被更具体的 allow 覆盖 | L3 | 该拦的没拦，且很难复现 |
| 把沙箱当成了权限 | L3 | 文件工具绕过沙箱，静默越界 |
| 委派同步等待 | L4 | 子代理退化成一次昂贵的函数调用 |
| 子代理报告直接当用户消息 | L4 | 外部内容获得指令权限 |

## 7. 拆解：从业务问题到可执行的 workflow

前面六节讲的都是"一次请求怎么跑完"。真实任务很少只有一次请求：需求是一句话，交付是十几个文件、几次验证，可能还要跨会话等一个后台任务。

这中间没有"拆解器"模块。拆解是模型推理，harness 做的是给推理结果一个能执行、能检查的形状——一共六个落点：

{{< fig "diagrams/10-decomposition.svg" "六个落点：输入、计划、任务图、批次、委派、脚本。左边是意图，右边是这一层真正读写的字段。" >}}

### 7.1 四种耐久度递增的产物

| 产物 | 存在哪 | 谁读它 | 缺了它 |
| --- | --- | --- | --- |
| 计划 | `~/.claude/plans/*.md` | 人、模型 | 做完才发现方向错了 |
| todo | 会话内存 | 模型自己 | 长任务做到一半忘了还剩什么 |
| 任务图 | 任务板（owner、blockedBy） | 模型、子代理、钩子 | 并行做的两件事互相踩 |
| workflow 脚本 | `scriptPath` | 引擎 | 每次跑出来的形状都不一样 |

耐久度越高，越不依赖当前这次对话。计划能给人看，todo 只活在这个会话里，任务图能被别人认领，脚本能重跑。

### 7.2 三样东西在推动拆解

**工具描述里的"何时用"。** `EnterPlanMode` 的描述列了新功能、多方案、跨文件、需求模糊；`TaskCreate` 的描述写得更直白：

```
Complex multi-step tasks - When a task requires 3 or more distinct steps
Plan mode - create a task list
Mark it as in_progress BEFORE beginning work
```

**系统提醒。** plan mode 的系统提醒里预置了一段 `code-implementation phases` 模板，可以用 `--plan-mode-instructions` 换掉，但只读约束和 `ExitPlanMode` 协议脚注不允许改。todo 的提醒靠计数器，不靠定时器：`turnsSinceLastTodoWrite` 攒到几次就提醒一次，提醒里还会带上 `## Plan (from TodoWrite state)`。

**人的否决权。** `ExitPlanMode` 给三个选项：自动接受编辑、逐条审、继续规划。批准绑定到具体的那一版计划——字符串里的原话是 `The team lead's verdict was for a different request, not this plan. Call ExitPlanMode again to resubmit it for approval.` 改完计划要重新提交。

### 7.3 走一遍

假设需求是"给结算服务加多币种"：

1. **计划**：plan mode 里派 Explore 侦察（它跳过 `CLAUDE.md`，只找文件），产出表结构、汇率源、迁移、回归四步，写进计划文件，`ExitPlanMode` 等人批。
2. **任务图**：批准后用 `TaskCreate` 建四条，`TaskUpdate` 加 `owner` 和 `blockedBy`——迁移要等表结构和汇率源，回归要等迁移。
3. **批次**：每个任务一个 turn。一个 turn 里模型可能一次要读三个文件，L3 把连续的只读合成一批并发跑，写操作单独成批。
4. **委派**：不确定的部分（比如"现有代码里有没有类似的多币种处理"）派给 Explore，默认后台，先回 `agentId`；报告回来先过安检。
5. **固化**：如果这套流程要反复跑，写成 `Workflow` 脚本：`meta.phases` 定义阶段，`pipeline(items, agent, agent)` 定义每个条目怎么走。改完 `scriptPath` 重跑，`resumeFromRunId` 会跳过没改过的 `agent()`。
6. **收尾**：跑测试、过一遍 hook、落 checkpoint。

### 7.4 这套东西的边界

- `blockedBy` 是**软依赖**。它写在任务板上，不是调度器；模型可以不等它。
- 真正的控制流只在 workflow 脚本里。任务板管"还剩什么"，不管"下一步谁跑"。
- 拆过头会炸。深度 3、并发 20、Explore 一次性、回执安检，都是给"拆得太细"兜底的。

所以拆解的产出不是一张更细的清单，而是**一个能被别人接手的状态**：计划能读，任务能认领，脚本能重跑。

## 8. 如果你要自己搭一个

每层的最小实现，按"不做就会立刻疼"的顺序：

**L1**
- 一个带收件人的队列，条目至少要有 `{target, kind, priority}`。
- 两个投递点：工具批次边界、turn 边界。命令单独一条通道，压到 turn 结束。
- 一个"现在投递"的入口，语义是"能转后台的转后台，不能的才中断"。

**L2**
- turn 对象，带 id、usage、aborted、结束原因。
- 一个显式的 `stop_reason` 处理表，别用 `if (hasToolUse)` 蒙混。
- 分级压缩：先清工具结果（可落盘、可回读），再总结对话；压缩后重注入一份白名单；加抖动刹车。

**L3**
- 三值判决 + 固定顺序的规则匹配（先 deny 再 ask 再 allow），别做"具体程度评分"。
- 并发按 `(tool, input)` 判定，只合并连续安全段，不安全调用当屏障。
- 截断和落盘放在结果进上下文之前，别等上下文爆了再补。
- 沙箱和权限分开实现，并且明确列出"哪些东西不在沙箱里"。

**L4**
- 委派默认异步，返回句柄；同步是特例，要显式声明。
- 深度和并发两个硬上限，到顶就收工具。
- 子代理的输出按不可信内容处理：标注来源、去指令化、记录处置结果。
- 追加写的事件日志 + 显式 checkpoint；fork 就是复制前缀。
- 拆解要落成可检查的产物：计划文件、任务图、脚本，别只留在对话里。

三个反直觉的结论，算是这篇的收尾：

1. **队列的难点是边界，不是存储。** 存一个数组谁都会；知道"这条消息只能在这两个时刻之一进去"，才是 L1 的全部。
2. **压缩的难点是保留什么，不是怎么总结。** 总结算法换个模型就能做；白名单、按需重载、最近文件重读，这些才是"压完还能接着干"的原因。
3. **委派的默认值应该是异步。** 一旦默认同步，子代理就只是函数调用，"派活"这层语义就没了，深度、并发、回执安检也都没有存在的必要。

这四层没有一层是"智能"的。它们是状态机、策略表和边界条件。模型负责生成，harness 负责让生成这件事在时间、权限和失忆的约束下成立。

## 9. 证据与方法

- **文档**：[How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)、[Interactive mode](https://code.claude.com/docs/en/interactive-mode)、[Context window](https://code.claude.com/docs/en/context-window)、[Subagents](https://code.claude.com/docs/en/sub-agents)、[Sandboxing](https://code.claude.com/docs/en/sandboxing)、[Permissions](https://code.claude.com/docs/en/permissions)、[Permission modes](https://code.claude.com/docs/en/permission-modes)。
- **类型面**：`/plugin-types` 导出，`2.1.277`，13521 行。`turn.*` 事件、`stop_reason` 联合、`ToolName` 联合、`PermissionUpdate`、`Workflow` 与 `Agent` 的输入形状都在这里。
- **二进制**：本机 `2.1.319` / `2.1.321` 的字符串表（68MB）。分批器、`InboxPoller`、`isConcurrencySafe` 计数、`CLAUDE_CODE_MAX_*`、截断正则、`TaskCreate` / `TaskUpdate` 的描述与字段都来自这里。
- **推断**：凡是文档和类型面都没有明说、只能从命名和相邻代码推出来的（比如 `pause_turn` 的续法、`InboxPoller` 与 turn 边界的对齐方式），文中都标了「推断」。

版本会变，机制的形状不太会。下一次你看到"Claude 好像忘了我刚说的话"，大概可以先想一下：是压缩把哪块白名单漏了，还是那条消息压根没赶上投递边界。
