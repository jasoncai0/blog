---
title: "审批与提问：人机回环的三条通道"
date: "2026-11-11T10:40:00+08:00"
summary: "审批、提问、命令都长着「用户输入」的脸，却是三种不同的机制：一次性决策、有生命周期的数据、绕过模型的控制面调用。"
description: "拆开 dsh 的人机回环：approval 为什么不能缓存、ask_user_question 超时为什么返回 pending、命令为什么不能变成模型消息，以及审批在工具流水线里的确切位置。"
tags: ["harness", "architecture", "agent"]
slug: "dsh-human-in-the-loop"
---

> 源码版本：0.2.2-rc.1 commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。行号以该 commit 为准。
> 本系列第 5 篇。系列目录：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)。

一个 Agent 跑到一半，界面上可能同时出现三种「等你」的东西：一个权限弹窗问「允许执行这条 `rm -rf` 吗」，一张提问卡片问「这三个方案你选哪个」，还有输入框里你敲下的 `/compact`。它们都表现为「用户给了点东西」，但在实现里，三者的生命周期、失败方式和权限含义完全不同。

把这三件事混在一起做，最常见的后果是：审批被当成可以缓存的「设置」，提问超时被当成「用户拒绝」，命令被塞进对话让模型转述。每一种都能让系统在某个边界上做出用户没有授权的动作。

这篇按三条通道拆：审批是一次性决策，提问是一段可等待的数据，命令是绕过模型的控制面。最后看权限预设如何把两个独立开关捆成一个用户可理解的选择。

## 0. 结论

审批回答「这一个动作能不能做」，答案随这次调用一起过期；提问回答「你需要什么信息」，答案是一段可以晚到、可以超时的数据；命令是用户直接调用注册表里的代码，从不进入模型上下文。

{{< fig "diagrams/01-approval-state-machine.svg" "图注：审批的状态机。requested 之后只有四种结局，其中三种都按拒绝执行；没有 expired 这个结果，进程消失由恢复流程补一条工具结果来闭合。" >}}

## 1 审批：一次决策，一次授权

### 1.1 一个请求，四种结局

审批的入口只有一个：

```ts
const outcome = await ctx.approval.request({
  agent, toolName, callId,
  reason, displayReason, signal,
})
```

`ApprovalOutcome` 是闭合的四值联合：`allowed-once`、`rejected`、`cancelled`、`unavailable`。第一个只授权「被问的那一个操作」，后三个在消费方那里一律按拒绝执行。`allowed-once` 这个命名是刻意的：它不叫 `allowed`，因为授权范围就是这一次。

请求对象里没有工具参数。应答者拿到的是 `callId`，用它把提示挂到已经流式输出的 tool-call 上。参数只存在于会话日志和 UI 的展示里，审批请求本身不复写一份，避免两份参数在展示与执行之间漂移。

请求里有两个给人看的字段，职责不同。`reason` 是审计文本，会写进 `approval/asked`，用于事后解释「为什么问」；`displayReason` 是请求方本地化好的展示字符串，只给 UI，不落日志。分开的原因是两者受众不同：审计要稳定、可检索，展示要跟随界面语言和上下文。混用一个字段，最后不是日志里出现翻译文案，就是界面上出现英文的机读标识。

每次 ask 落两条仅日志事件：`approval/asked{id,toolName,callId?,reason?}` 和 `approval/decided{id,outcome}`，用 `ApprovalRequestId` 配对。它们不进模型 transcript，也不包成 turn；模型能感知到的只是工具结果和策略上下文。

### 1.2 审计对必须在一个 turn 里

`request()` 做的第一件事是检查，还没到发问：

```ts
if (!this.approvalStateOf(session).turnOpen) {
  throw new Error(
    'approval.request() outside an open turn: the approval/asked + approval/decided audit pair '
    + 'must be turn-enclosed (a bare event between turns is crash-tail garbage on reload). '
    + 'Ask from inside the turn that needs the decision.',
  )
}
```

理由写在错误信息里：两个事件必须被 turn 包住。如果允许在 turn 之外发问，崩溃恢复时日志里会出现一条没有归属的 `approval/asked`，重放时无法判断它属于哪个轮次，也无法判断它是否已经结算。恢复流程可以给未配对的工具调用补合成结果，但没有办法给一条悬空的审批补一个可信的人类决定。

```ts
const id = ApprovalRequestId(randomUUID())
session.append('approval/asked', { id, toolName, callId, reason })
const outcome = await this.decide(req, session)
session.append('approval/decided', { id, outcome })
return outcome
```

先写 asked，再等人，最后写 decided。中间这段可以很长：人要读参数、可能去倒杯水。日志里它就是一段空档，重放时靠 `decided` 补齐。

### 1.3 never 在分发之前决定

`decide()` 的前两行是整条通道最重要的顺序：

```ts
if (signal?.aborted) return 'cancelled'
if (this.effectivePolicy(session) === 'never') return 'rejected'
```

`never` 不经过 `approval/request` 这个 waterfall，而是在服务内部直接返回 `rejected`。源码注释解释了原因：如果把它做成一个监听器，后注册的 `prepend: true` 监听器会排到 gate 前面，`never` 就无法保证「与注册顺序无关地确定性拒绝」。只有服务自己的 request 路径能给出这个保证。

同一个函数紧接着把 waterfall 放进 promise 链，再分发：

```ts
const answer = Promise.resolve().then(
  () => this.ctx.waterfall(scopeTarget(req.agent, req.agent), 'approval/request', req,
    () => Promise.resolve<ApprovalOutcome>('unavailable')),
).then(
  outcome => OUTCOMES.includes(outcome) ? outcome : 'unavailable',
  () => 'unavailable',
)
```

注释说明第二点：监听器如果在第一个 await 之前同步抛错，必须和异步抛错落到同一条 rejection 路径。`Promise.resolve(call())` 会让同步异常直接穿出 containment，变成调用方的工具失败，而不是一次「无法回答」。

第三点是归一化：应答者返回词汇表之外的值，按 `unavailable` 处理，而不是把脏值漏进调用方的闭合 switch。抛错也一样。这个 seam 对自己的回调 fail closed。

### 1.4 为什么审批不能缓存

把 `allowed-once` 按 `toolName` 缓存起来，等价于把「允许执行这一次 `git push`」变成「允许执行这一类 `git push`」。用户当时看到的是具体参数、具体目标、具体时刻；下一次调用的参数可能完全不同，而缓存里只有一个名字。这是一条安全约束，不是实现细节。

三个具体理由：

第一，审批请求故意不带参数。参数由 `callId` 关联到那条已经流式输出的 tool-call。如果要做缓存键，键里必须包含参数；而参数在模型侧是可以被后续上下文影响的。用名字做键，缓存命中时授权的对象已经不是用户看到的那个。

第二，授权有时效。人在看到参数后的那几秒里做的判断，包含了当时的仓库状态、分支、环境。十分钟后同样的命令可能指向另一个目标。审批没有 TTL 的概念，因为它根本不被保存为可复用的许可。

第三，恢复语义要求重新问。审批只在日志里留审计对，不落「已授权」的持久事实。进程崩溃后 resume，未闭合的 turn 由修复流程补一条合成的工具结果（`TOOL_OUTCOME_UNKNOWN` 或 `TOOL_NOT_STARTED`），然后新一轮如果还要执行，就重新发问。没有任何路径能把上次的 `allowed-once` 读回来。

### 1.5 一次审批在会话日志里留下什么

把一次允许的执行摊开，日志的顺序是这样的：

```
turn/start        {turn: 7}
tool/call         {callId: "call_9f3", name: "bash", args: {...}}
approval/asked    {id: "apr_2c1", toolName: "bash", callId: "call_9f3", reason: "..."}
  ... 人在读参数，可能过了几十秒 ...
approval/decided  {id: "apr_2c1", outcome: "allowed-once"}
tool/result       {callId: "call_9f3", ...}
step/end          {step: 2}
turn/end          {turn: 7, reason: {kind: "completed"}}
```

三个观察。

第一，`approval/asked` 与 `approval/decided` 之间可以隔很久，但日志里没有「等待中」这种状态。状态是 UI 的，日志只记事实：问了，答了。重放不需要人类参与，因为 `decided` 已经写下了当时的结局。

第二，两个事件都在 `turn/start` 与 `turn/end` 之间。格式偏好解释不了这条约束，它来自恢复语义：turn 是日志的闭合单位，任何属于某个决策的审计对都必须能被这个单位包住。

第三，`decided` 缺失时，恢复流程不猜。它会用 repair 补一条合成的工具结果（按调用是否已经开始，记 `TOOL_OUTCOME_UNKNOWN` 或 `TOOL_NOT_STARTED`），然后闭合 turn。审批没有被回答这件事，不会被推断成允许或拒绝；它只会变成一个「结果未知」的工具调用，由下一轮重新决定要不要再问。

自己搭 Agent 时该怎么做：把审批写成一对事件，用一个 id 配对；把「问了什么」和「答了什么」都落在 turn 内；把「没有答案」当成未知，而不是默认值。这三件事决定了崩溃恢复时系统会不会替你编造一个授权。

## 2 提问：一段可等待、可超时的数据

### 2.1 三种 intent，一个协议

`ask_user_question` 的请求是一组问题，每个问题可以带 `intent`：`plan-review` 或 `approval`。intent 只改呈现：`plan-review` 把某个选项标成「批准」，其余选项都是拒绝；`approval` 要求单问题、无多选、标签与选项一一对应。

它不改协议。答案的编码是一样的：`{id, selected, custom?}`，单选时 custom 覆盖 selected，多选时 custom 作为补充。选了空数组、也没写 custom，表示这个问题被跳过。

这一点很容易做错：为了「审批式提问」另造一套返回结构，最后两套答案在消费方汇合，边界条件对不上。dsh 的选择是让呈现层承担差异，协议保持一种。

| intent | 选项的含义 | 多选 | 计时等待 |
|---|---|---|---|
| `plan-review` | `approve` 指向的选项算批准，其余都算拒绝 | 允许 | 允许 |
| `approval` | `approve` / `reject` 与选项一一对应，标签互异 | 禁止 | 禁止 |

`approval` 的约束更紧：单问题、不能多选、不能带 `wait`。原因是它要表达的是一个二元决定，多选和计时都会让「这个回答到底算不算授权」变得含糊。

### 2.2 timed wait 的一生

提问有两种等待方式。不带 `wait` 的是阻塞请求，没有卡片 key；带 `wait:{callId}` 的是具名卡片，可以无限期挂着；带 `wait:{callId,timed:true}` 的是前台等待，客户端可以调 `attachWait` 接手。

`TimedQuestionWait` 保存四样东西：Host 时钟算出的 deadline、父 signal、超时错误、以及一个只属于这次前台 waterfall 的 signal。计时器只在没有接手方的时候跑：

```ts
private schedule(): void {
  clearTimeout(this.timer)
  this.timer = undefined
  if (this.signal.aborted || this.claims.size > 0) return
  this.timer = setTimeout(() => { this.close(this.timeout) }, Math.max(0, this.deadline - Date.now()))
}
```

`attach(signal)` 是一个异步生成器：先报一次 `{remainingMs}`，把 claim 计数加一，然后等结束或调用方 abort；最后一个接手方离开后，按原 deadline 重新排计时器。这样前端刷新、重连、换标签页都不会把剩余时间清零或延长。

超时的时候，`close()` 中止的是这个前台 signal，不是 Turn 的 signal。`ask()` 因此以 `ASK_TIMED_OUT` 拒绝，`askTimed()` 捕获它并返回：

```ts
if (error instanceof UserQuestionError && error.code === 'ASK_TIMED_OUT') {
  return { pending: true, callId }
}
```

{{< fig "diagrams/02-ask-user-timeline.svg" "图注：一次带超时的提问。前台等待由 Host 的 deadline 驱动，超时只中止这条提问自己的 signal；工具返回 pending，Turn 继续，迟到的回答以 user/message 的身份在下一个 step 进入。" >}}

### 2.3 超时返回 pending，不是拒绝

`TimedUserQuestionResult` 是 `AskUserQuestionAnswer | {pending:true, callId}`。pending 的语义在类型和文案里都写死了：问题仍然可以被回答，空答案、拒绝、确认、授权都不对。

工具结果里的提示原文是：

> No answer batch arrived before the timeout. This is pending, not a skipped answer. Continue useful independent work. The user can still answer; their reply will be a user message identified as answer_to_pending_question with this callId and the original questions. Do not treat this as permission.

三个设计点藏在这段话里。第一，模型被告知可以继续做别的事，而不是停在这里等。第二，回答的关联方式是 `callId`，不是「下一条用户消息」。第三，明确禁止把 pending 当授权。最后一条尤其重要：一个「还没回答」的审批式提问，如果在模型那里被读成「默认允许」，整套审批就漏了。

迟到的回答走 `answer()`：校验 callId 仍在可回答集合里，构造一条 source 为 `user-question-reply` 的 user message，然后 `agent.steer(message)`。这条消息在下一个 step 边界被 claim，投影在它成为 `user/message` 时把问题移入 settled。如果排队期间用户又取消了，消息被 discarded，问题回到可回答状态。

### 2.4 投影记住什么，不记住什么

`userQuestions` 投影只折叠「哪些问题还可以回答、哪些已经结算」。它不记录等待、聚焦、deadline，这些是 UI 的瞬态，不该进会话日志。

它只跟踪 timed 调用：判断依据是 `request/header` 里 `ask_user_question` 的 schema 是否声明了 `timeout` 参数。tool/call 打开问题，tool/result 里如果是 pending 载荷、或者是恢复流程补写的 `TOOL_OUTCOME_UNKNOWN`，问题标成 continued；否则按答案批次结算。迟到回复在消息获准成为 `user/message` 时才结算，而不是在 `answer()` 被调用时。

这里有一条容易忽略的规则：`answer()` 只对 continued 集合生效。已经结算的问题再回答，返回 false，不写任何事件。投影的另一个作用是回答「现在还能不能答」：UI 断线重连后不需要自己维护一份状态，读投影即可。

自己搭 Agent 时该怎么做：把「等待」建模成数据，而不是控制流。一次提问返回的应该是「答案 / 仍然挂起」两个分支，不是抛异常让上层决定；挂起的分支要带一个能找回问题的 id，让迟到的回答能落回原处；不要在超时的时候取消整个任务，除非产品语义明确要求停止。把这三件事做对，模型才能在等待期间继续做别的工作，而不是僵在原地。

## 3 命令：绕过模型的控制面

### 3.1 解析与执行

命令的语法解析是独立的：

```ts
const COMMAND = /^\/([a-z][a-z0-9_-]*)(?=$|[\t\n\r ])/u
export function parseCommand(line: string): ParsedCommand | undefined
```

名字小写、不带斜杠，后面必须是空白或行尾。解析失败返回 undefined；`execute()` 对未知命令也返回 undefined，且不写任何事件。语法有效不代表命令可用。

执行的生命周期是两个仅日志事件：

```ts
this.appendLifecycle(agent.session, 'command/run', {
  commandId, name: parsed.name,
  ...command.definition.recordInput === false ? {} : { args: parsed.rawInput },
  source: { kind: 'user' },
})
```

handler 结算后写 `command/done{commandId,kind,text?,sourceEventSeq?}`。`sourceEventSeq` 指向会话日志里更早的一条领域事件，比如压缩产生的摘要；命令本身不复制那份载荷。`recordInput:false` 用于领域事件已经自带输入的命令，避免日志里出现两份同样的文本。

附件只有在 `input.attachments === true` 时才准入了；否则直接 settle 一个 error，让 composer 保留原件。准入之后、进 handler 之前还要再查一次取消：

```ts
const cancelledDuringAdmission = cancellationOf(signal)
if (cancelledDuringAdmission !== undefined) {
  this.settleThrown(agent.session, parsed.name, commandId, cancelledDuringAdmission)
  throw cancelledDuringAdmission
}
```

这一版比入口处的 `if (signal.aborted) throw` 多做一件事：补写一条 error 的 `command/done`。因为 `command/run` 已经落盘了，如果直接抛出去，日志里会留下一条永远不结算的执行。注释写得很直白：admission 可能 await 慢存储，取消之后进 handler 会改状态。

### 3.2 为什么命令不能变成模型消息

把 `/compact` 实现成「用户发一条消息，模型决定要不要压缩」，会同时损失四样东西。

确定性。命令是注册表里的一个 handler，同样的输入得到同样的结果；模型转述会引入解释空间。

权限。命令能改权限预设、能导出会话、能触发压缩。如果它是模型消息，提示注入就可以让模型「帮你执行 /permission danger-full-access」。命令通道的存在前提是：模型看不见它，也调不动它。

成本。一条命令不该产生一次模型调用，也不该占用上下文。

审计。`command/run` 和 `command/done` 是结构化的生命周期记录，能回答「谁在什么时候执行了什么、结果是什么」。一条自然语言消息回答不了。

内建命令有六个：`/goal`、`/plan`、`/feedback`、`/compact`、`/permission`、`/export`，各自带中英文别名。它们都走同一条注册表路径，没有「模型代执行」的旁路。

自己搭 Agent 时该怎么做：给控制面单独一条入口，注册表里只放确定的 handler；把执行前后的两个事件写进日志，并让结果能引用更早的领域事件；在 handler 之前检查取消，因为准入可能 await 慢 IO。最重要的是：不要让模型看见这条通道的调用接口。控制面一旦可被提示词触达，它的权限就不再是用户的权限。

{{< fig "diagrams/03-three-channels.svg" "图注：三条通道的分层。命令面在模型之上，审批面在工具准入处，问答面在一次工具调用内部；三者共享 UI，不共享协议。" >}}

## 4 三条通道不能互相替代

把三者放在一张表里，差异就出来了：

| | 审批 | 提问 | 命令 |
|---|---|---|---|
| 谁发起 | 工具执行器 | 模型（经工具） | 用户 |
| 谁回答 | 人类或机器应答者 | 人类 | 不需要回答 |
| 生命周期 | 一次调用 | 一个问题批次 | 一次同步执行 |
| 超时 | 无（活动结束即失效） | 可选，返回 pending | 无 |
| 失败语义 | unavailable → 拒绝 | 超时 → 仍可回答 | error 结果 |
| 进入模型上下文 | 只有工具结果 | 答案作为 user message | 从不 |

三条「不能替代」的边界：

审批不能做成提问。提问可以超时、可以跳过、可以晚到；审批必须 fail closed，且不能留下可复用的授权。把审批建成一张普通问题卡，超时那一刻就得决定「算同意还是算拒绝」，而这个决定没有安全答案。

提问不能做成审批。答案是一段数据，可能被跳过，可能是用户随手写的 custom 文本。它不是许可。把 `AskUserQuestionAnswer` 当 grant 用，等于给「跳过」开了一个授权口子。

命令不能做成消息。理由在上一节：确定性、权限、成本、审计。

`intent: 'approval'` 的提问是三者里最容易被误解的一个。它长得像审批，走的却是问答通道：答案是一段数据，可能被跳过，也不会落成 `approval/decided`。

它适合「请用户确认一个计划」，不适合「授权执行一个动作」。后者必须走 `ctx.approval`，因为只有那条通道会在没有人回答时 fail closed。

## 5 审批在工具流水线的位置

工具调用的准入顺序是：`tools/pre-execute` waterfall（hooks、permission、sandbox）→ 如果 gate 说 ask，走 `ctx.approval` → 注册的单调 guards → `tools/execute`。

```ts
const denialReason = decision.kind === 'allow'
  ? this.tools.checkGuards(exec)
  : decision.reason
```

guards 只在 allow 之后跑。它们是「deny 或 abstain」的单调策略，由工具的所有者注册；审批是「问人」，两者是 AND 关系，但顺序有讲究。

ask 排在 guards 之前，有两个原因。guards 是所有者策略，不能被 UI 的应答重排或跳过。先问人、再跑 guards，最终结果仍然是「人允许且策略不禁止」；反过来先跑 guards、再问人，人会被问到一个已经被策略否决的调用，而那个否决本来不需要打扰人。更重要的原因是语义：审批记录的是「人就这个提案做的决定」，提案必须是原始的调用，不能是经过某层策略改写后的版本。

代价是：一个最终会被 guard 拒绝的调用，可能先打扰了用户。guard 拒绝很罕见，顺序的可预测性更值钱，这个代价可以接受。

取消与审批的竞速在 `serviceAsk` 之后处理：

```ts
if (this.callerCancelled(exec) && askResolution.approvalCancelled) {
  return { kind: 'post-result', result: toolAbortedBeforeDispatchResult() }
}
```

用户在审批期间按了停止：审批以 `cancelled` 结算，但调用方也已经取消。这时返回的是「派发前中止」的工具结果，而不是一条「用户拒绝了」的假消息。少了这个分支，日志里会出现一次并不存在的拒绝。

映射表本身也值得背下来：

| 审批结果 | 工具决策 | 给模型的原因 |
|---|---|---|
| `allowed-once` | allow | —（继续跑 guards） |
| `rejected` | deny | `the user rejected tool "bash"` |
| `cancelled` | deny，带 `approvalCancelled` | `approval for tool "bash" was cancelled` |
| `unavailable` | deny | `requires approval, but no approval channel is available` |

还有两条更早的 fail closed：没有 `ctx.approval` 服务时，直接 deny 并说明「尚未支持」；有服务但没有 agent 可路由时，deny 并说明「没有可以承载审批的 agent」。四种结局里只有第一种会继续往下走，其余三种都在这里变成一条可读的拒绝。

自己搭 Agent 时该怎么做：把「问人」和「策略」分成两层，最终准入是两者的 AND；让策略层只做 deny 或 abstain，不要让它去发问；给取消单独一条结果，不要在取消时伪造拒绝。这样日志里的每一条拒绝都能找到来源，而不是几种原因混成一句「不允许」。

## 6 权限预设：两个开关，一个选择

沙箱模式和审批策略是两个独立 knob：`sandbox/mode` 决定文件与命令的隔离级别，`approval/policy` 决定要不要问。用户不想分别理解它们，于是有了 `permissionPresets`。

默认表里，`workspace-write` 对应「workspace-write 沙箱 + ask」，`danger-full-access` 对应「完全访问 + never」。`custom` 是派生值：当两个 knob 的组合不匹配任何预设时，`current(session)` 返回 custom。它不是一个可以切换的目标，也从不写进事件。

`auto` 是另一类：它由 Auto review integration 在 effect 生命周期内注册，固定 `danger-full-access + ask`，不进配置 schema，dispose 就消失。切到 auto 之前要跑一次同步的 `admit` 准入。

`set()` 的顺序值得抄：

```ts
if (name === AUTO_PRESET) this.autoAdmit?.()
const current = this.current(session)
if (current !== name) session.append('permission/preset', { preset: name })
updateKnobs()
```

先准入，再记选择，最后写 knob；每个 knob 只在生效值真的变化时才写。`permission/preset` 是仅日志的用户意图，不进模型 transcript。它存在的理由很具体：两个预设可能共享同一个 knob 组合，没有这条事件就分不清用户选的是哪一个。

| 预设 | sandbox | approval | 来源 |
|---|---|---|---|
| `workspace-write` | `workspace-write` | `ask` | 默认配置表 |
| `danger-full-access` | `danger-full-access` | `never` | 默认配置表 |
| `custom` | 任意组合 | 任意组合 | 派生值，不可作为切换目标 |
| `auto` | `danger-full-access` | `ask` | 运行时集成注册，dispose 即消失 |

创建 agent 时还有一条补全规则。全新会话使用当前用户默认预设，并把 preset、sandbox、approval 三个事实一起写下；种子会话或部分初始化的会话保留已经生效的 knob，只补缺失的事实。恢复 `auto` 时必须找到仍在运行的集成，否则创建失败，而不是静默降级成某个内置预设。

这条规则解决的是恢复时的歧义：日志里可能只有 sandbox 没有 approval，或者只有 preset 没有 knob。补全逻辑必须能区分「用户从没选过」和「用户选过但事件丢了」：前者可以套默认值，后者只能保留现状。

自己搭 Agent 时该怎么做：用户界面上的「权限档位」和内核里的强制开关要分开建模。档位是给人看的名字，开关是执行点读的值；档位可以派生，派生值不要持久化；运行时才有意义的档位要能随插件生灭，而不是写进配置 schema。否则用户选过的名字会变成一个再也对不上的历史字符串。

{{< fig "diagrams/04-permission-presets.svg" "图注：预设是表层，两个 knob 是执行层。custom 由组合派生，auto 随 integration 生灭，选择事件只记录意图。" >}}

## 7 设计取舍

| 取舍 | 选择 | 放弃的东西 |
|---|---|---|
| 审批粒度 | 一次调用一个 id | 无法「记住这个工具别再问」 |
| 审批缓存 | 不缓存 | 重复询问的打扰 |
| 审批位置 | 在 guards 之前 | 偶尔打扰一个终将被策略拒绝的调用 |
| 提问超时 | 返回 pending，保留 Turn | 模型要自己处理「还没答案」的状态 |
| 提问关联 | callId + 迟到 user message | 回答必须能被日志定位，不能只靠 UI 状态 |
| 命令通道 | 独立注册表与审计事件 | 模型无法「顺手」帮用户执行命令 |
| 策略表达 | 运行时上下文快照 | 每轮多一段 user message |
| 预设 | 派生 current，不落 derived 值 | 无法从日志直接读出「当时是哪个预设」，靠选择事件补 |

两条值得展开。

第一，审批不缓存换来的是授权对象的准确性。工程上可以用「本次会话内同一命令不再问」来减少打扰，但那是产品策略，应该在 UI 层显式表达，而不是让审批服务偷偷复用上一次的 `allowed-once`。

第二，提问超时返回 pending 而不是抛错，把「等待」变成模型可以处理的数据。代价是模型必须理解 pending 的含义，并且不能把它当授权。dsh 用工具描述和 `pendingNotice` 两处文案反复强调这一点。

还有一条没有进表的取舍：三条通道没有合并成一个统一的「人机输入」事件。合并的诱惑很大：UI 上都是卡片、都要等待、都要取消。但三者的失败语义不同：审批失败必须拒绝，提问失败必须保持可回答，命令失败只是一条 error 结果。一个统一事件只能取三者失败的并集或交集，两种都会错。分开建模的代价是客户端要渲染三种卡片，收益是每一层的 default 分支都能写对。

## 8 踩坑点

1. 用 toolName 缓存审批结果。现象：第二次执行同类命令直接放行。原因：把「这一次」的授权扩成了「这一类」。对策：键里至少要有 callId；更稳妥的是完全不缓存，把减少打扰放到 UI。
2. 在 turn 之外发问。现象：`approval.request() outside an open turn`。原因：审计对没有被 turn 包住，恢复时无法归属。对策：只在需要决策的那次工具调用里问，不要在 turn 开始前预问。
3. 把 unavailable 当 allow。现象：没有应答者时工具照样执行。原因：消费方把四值联合的 default 分支写成了放行。对策：只对 `allowed-once` 放行，其余一律拒绝，并给出可读原因。
4. 把 pending 当拒绝或当同意。现象：模型停止工作，或者把没回答的审批当默认允许。原因：类型上 pending 和 answer 是两个分支，消费方只处理了 answer。对策：把 pending 当「问题还开着」，继续做独立工作，等 user message。
5. 提问超时后取消 Turn。现象：用户回来回答，消息无处落地。原因：把 UI 超时当成了任务取消。对策：超时只中止提问自己的 signal；Turn 的 signal 不动。
6. 用模型消息实现命令。现象：提示注入能改权限。原因：命令和对话共用了一条模型可见的通道。对策：独立注册表、独立审计事件，模型不可见、不可调用。
7. guards 里做 ask。现象：同一个调用被问两次，或者策略顺序被 UI 影响。原因：把「问人」塞进了单调 guard。对策：ask 留在 pre-execute 的 gate 分支，guards 只做 deny/abstain。
8. 前端自己算剩余时间。现象：刷新后倒计时漂移，或者超时早到/晚到。原因：deadline 在 Host，前端只拿到剩余毫秒。对策：`attachWait` 接手，每次重连重新取 `remainingMs`。

## 9 源码索引

- `packages/interaction/user-approval/src/index.ts:242` — `request()`：turn 检查、asked/decided 审计对。
- `packages/interaction/user-approval/src/index.ts:293` — `decide()`：abort 判定、never 提前返回、waterfall 进入 promise 链。
- `packages/interaction/user-approval/src/index.ts:301` — never 在分发之前拒绝，保证与监听器注册顺序无关。
- `packages/interaction/user-approval/src/index.ts:56` — `OUTCOMES` 四值，运行时归一化的依据。
- `packages/interaction/user-approval/src/index.ts:74` — `NEVER_SENTENCE`，模型可见的策略陈述。
- `packages/interaction/user-approval/src/index.ts:211` — `setPolicy()`：写策略并追加一条 user-approval 来源的 user message。
- `packages/core/tools/src/pipeline.ts:432` — `serviceAsk()`：审批请求的构造与 `exec.signal` 的传入。
- `packages/core/tools/src/pipeline.ts:457` — 四值到 allow/deny 的映射，cancelled 额外带 `approvalCancelled`。
- `packages/core/tools/src/pipeline.ts:207` — 审批取消与调用方取消的竞速分支。
- `packages/interaction/user-questions/src/index.ts:239` — `askTimed()`：deadline、pending 返回、超时与 abort 的区分。
- `packages/interaction/user-questions/src/index.ts:221` — `attachWait()`：接手前台等待并产出剩余毫秒。
- `packages/interaction/user-questions/src/index.ts:291` — `ask()`：intent 校验、无应答者时的 NO_PROVIDER。
- `packages/interaction/user-questions/src/timed-wait.ts:4` — `TimedQuestionWait`：claim 计数、deadline 恢复、close。
- `packages/interaction/user-questions/src/index.ts:137` — `assertLiveRoot()`：只允许活着的根会话发问。
- `packages/interaction/commands/src/index.ts:125` — `parseCommand()` 的正则与边界。
- `packages/interaction/commands/src/index.ts:361` — `execute()`：run/done 两个事件、附件准入、取消检查。
- `packages/interaction/permission-presets/src/index.ts:403` — `apply()`：准入、选择事件、按生效值写 knob。
- `packages/interaction/permission-presets/src/index.ts:429` — `pinInitialPermission()`：新会话默认值与部分初始化的补齐。

## 10 收尾

三条通道的共同点只有一个：它们都让人类在不进入模型上下文的前提下改变系统的行为。审批改变一次调用的准入，提问补上模型缺的信息，命令直接调用控制面。

把这三件事分开实现，代价是要维护三套词汇、三套日志、三套失败语义。收益是每一套都能在自己的边界上 fail closed：审批不会因为超时变成同意，提问不会因为跳过变成授权，命令不会因为模型看见就变成可注入的入口。

整篇最该记住的一条：人机回环里更危险的情况是系统在没有人回答的时候替你做决定；用户点了拒绝反而没那么危险。三条通道的设计都在防这个默认值：一次性、fail closed、pending 不算答案、命令不进模型。

下一篇进入 `turn` 与 `step` 的状态机，看这些输入最终如何被组装成一次请求。

---

上一篇：[04 插话、注入与急停](/posts/dsh-steering-and-cancel.html)　|　下一篇：[06 轮次与步骤的状态机](/posts/dsh-turn-step-state-machine.html)
