---
title: "从零搭建一个 Agent：五个阶段、十个决策、一张成熟度阶梯"
date: "2026-11-11T20:30:00+08:00"
summary: "不讲框架 API，只讲顺序：先有一个能跑的最小循环，再逐步加上工具流水线、事件日志、取消、压缩与子 agent。每一阶段给出验收标准与不该提前做的事。"
description: "面向初学者的 Agent 搭建指南：最小循环的 60 行代码、十个必须尽早做的决策、从 L0 到 L5 的成熟度阶梯、以及最常见的十个坑。"
tags: ["agent", "architecture", "planning"]
slug: "dsh-build-agent-from-zero"
---

> 依据：本系列前二十篇的源码结论，加上 `docs/architecture.zh.md` 与 `docs/subsystems/core.zh.md` 的接口契约。代码示例是「按 DSH 的形状重写的最小版」，不是 DSH 源码。

如果你已经读过一些 Agent 教程，多半见过这样的代码：

```ts
while (true) {
  const response = await llm.chat(messages, tools)
  messages.push(response)
  if (!response.toolCalls) break
  for (const call of response.toolCalls) {
    messages.push({ role: 'tool', content: await run(call) })
  }
}
```

这确实是一个 Agent。它能跑 demo，也能在一些任务上表现不错。但它离「能过夜」还差五层东西，而每一层都不是加几行代码，而是**改变数据的形状**。

这篇不讲框架 API，讲顺序：什么时候该加什么，为什么不能提前加，以及每一层的验收标准。

## 1. 先定义最小 Agent

在写代码之前，先接受四个定义。它们看起来是废话，但后面每一个坑都源于其中一个被破坏。

**输入是一个队列，不是一次调用。** 用户可能在 Agent 跑的时候再发一条消息；这条消息可能应该插到当前步骤之后（steering），也可能应该等这一轮结束（follow-up）。如果 API 只有 `run(prompt)`，你迟早要重写。

**循环的边界是「轮次」，不是「一次请求」。** 一轮里可能有多次模型请求（每次带工具），也可能一次都没有（输入被拒绝）。把「请求」当循环单位，会导致工具结果无处安放。

**历史是日志，不是数组。** `messages.push()` 的问题是：它既是真源又是缓存。一旦你要压缩、要 fork、要审计，就必须回答「这条消息是哪次操作产生的」。日志能回答，数组不能。

**工具是协议，不是函数调用。** 工具会失败、会超时、需要审批、会产生副作用、需要被模型看见结果。把它当 `await fn()`，错误处理就没有位置。

## 2. 十个决策

下面十个决策，越早做越好。做晚了不是改代码，是改数据模型。

| # | 决策 | 选错的代价 |
| --- | --- | --- |
| 1 | 消息如何表示（角色 + 内容块） | 工具结果、图片、推理内容无处安放 |
| 2 | 历史真源（数组 / 事件日志） | 压缩、fork、审计全部要重写 |
| 3 | 循环单位（请求 / 步骤 / 轮次） | 工具批次无法归属，取消语义混乱 |
| 4 | 工具协议（schema / 执行 / 结果） | 审批、超时、错误分类没有挂点 |
| 5 | 并发模型（串行 / 并行 / 屏障） | 有副作用的工具互相踩踏 |
| 6 | 审批位置（前置 / 执行中 / 后置） | 要么批得太早，要么已经造成副作用 |
| 7 | 失败分类（可重试 / 需修复 / 终止） | 重试风暴，或该重试的没重试 |
| 8 | 取消语义（丢弃 / 保留 / 唤醒） | 用户按停止后 Agent 还在跑 |
| 9 | 上下文预算（截断 / 压缩 / 召回） | 长会话必然崩，且崩得很难看 |
| 10 | 持久化格式（JSONL / DB / 对象存储） | 崩溃恢复、迁移、并发写全部要补 |

{{< fig "diagrams/01-decision-map.svg" "十个决策的依赖关系：消息表示与历史真源是地基，循环单位与工具协议是骨架，并发/审批/失败/取消是行为层，预算与持久化是运营层。越靠下越难改。" >}}

一个实用的判断：**如果你还没想清楚第 2 和第 3 项，就不要开始写第 9 项。** 压缩是建立在「历史可重建」之上的；循环单位不清，压缩就不知道该压哪一段。

## 3. 阶段一：一个能跑的循环（第 0 天）

先写一个最小的、但形状正确的循环。关键不是功能，是**边界**：

```ts
type Turn = { id: number; steps: number }

async function runTurn(agent: Agent, input: UserMessage[]): Promise<void> {
  const turn = nextTurnId++
  let pending = input

  while (true) {
    if (agent.inbox.nextStep.length > 0) {
      pending = [...pending, ...agent.claimNextStep()]
    }
    if (pending.length === 0) break

    const step = { turn, step: turn.steps++ }
    const result = await runStep(agent, step, pending)
    pending = []

    if (result.kind === 'completed' && agent.inbox.nextStep.length === 0) break
  }
}
```

`runStep` 只做三件事：把消息发给模型、收集工具调用、把结果写回历史。

```ts
async function runStep(agent: Agent, step: Step, input: UserMessage[]) {
  const request = { messages: [...agent.history, ...input], tools: agent.tools.schemas() }
  const stream = await llm.stream(request)
  const message = await collect(stream)
  agent.history.push(...input, message)

  if (!message.toolCalls?.length) return { kind: 'completed' as const }

  for (const call of message.toolCalls) {
    const result = await agent.tools.run(call, { signal: agent.signal })
    agent.history.push(result)
  }
  return { kind: 'continued' as const }
}
```

**验收标准**：能让模型连续调用两次工具并给出最终回答；用户在一次运行中再发一条消息，不会丢。

**此时不要做**：并行工具、重试、压缩、子 agent。它们都会迫使你重新设计上面的数据结构，而你现在还没有数据。

## 4. 阶段二：工具流水线（第 1 周）

一旦工具有副作用，`await fn()` 就不够了。你需要把一次工具调用拆成四个可插入的阶段：

```
model 产生 tool-call
  → tool/call 记录（执行前）
  → pre-execute（权限、审批、沙箱、hook）
  → execute（真正的函数，可超时、可重试）
  → post-execute（接受/替换/追加上下文）
  → tool/result 记录（唯一模型可见结果）
```

最小实现只要两个钩子：

```ts
async function runTool(call: ToolCall, ctx: ToolContext): Promise<ToolResult> {
  const decided = await ctx.preExecute(call)          // 可 deny / ask / rewrite args
  if (decided.kind === 'deny') return denyResult(decided.reason)

  let result: ToolResult
  try {
    result = await withTimeout(tool.execute(decided.args, ctx), decided.timeoutMs)
  } catch (error) {
    result = { isError: true, content: [{ type: 'text', text: describe(error) }] }
  }

  const final = await ctx.postExecute(call, result)    // 可 block / replace / add context
  return final
}
```

三个决定必须在写第一个工具之前定下来：

- **审批在哪一层。** 如果想「先看参数再决定要不要执行」，审批必须在 `pre-execute`，且要能改写参数。放到执行后，副作用已经发生。
- **错误怎么分类。** 工具内部错误 → `isError` 结果；管道阶段错误（pre/post 抛错、结果不可序列化）→ 向上抛；取消 → 走 abort。
- **结果怎么序列化。** 模型能看到的必须是可 JSON 无损表示的值。如果工具返回一个对象、一个流、一个句柄，先在 post-execute 里投影成文本/图片。

**验收标准**：一个会失败的工具，模型能看到错误并自行修正；被拒绝的调用不会执行；每个结果都有唯一记录。

**此时不要做**：动态工具集、工具间的依赖图。先把「一次调用」的生命周期走通。

## 5. 阶段三：把历史变成日志（第 2 周）

这是最容易被跳过、也最值得做的一步。不要再用 `messages` 数组当历史，改成一个只追加的事件日志：

```ts
type Event =
  | { type: 'turn/start'; seq: number; turn: number }
  | { type: 'step/start'; seq: number; step: number }
  | { type: 'user/message'; seq: number; message: UserMessage }
  | { type: 'assistant/message'; seq: number; message: AssistantMessage }
  | { type: 'tool/call'; seq: number; call: ToolCall }
  | { type: 'tool/result'; seq: number; result: ToolResult }
  | { type: 'step/end'; seq: number }
  | { type: 'turn/end'; seq: number; reason: TurnEndReason }
```

然后**从日志派生**模型看到的消息：

```ts
function deriveMessages(events: Event[]): Message[] {
  return events.flatMap(event => {
    switch (event.type) {
      case 'user/message': return [event.message]
      case 'assistant/message': return [event.message]
      case 'tool/result': return [event.result]
      default: return []
    }
  })
}
```

这带来三个立刻可见的好处：

- 崩溃恢复：日志尾部不平衡，补 `turn/end` 就能重新打开。
- 审计：每个模型看到的内容都能追溯到一条事件。
- fork：按 `seq` 切一段日志，就是一条分支。

但真正的收益在后面：**压缩、召回、注入、子 agent 都要靠它**。没有日志，这些功能只能建立在「猜哪些消息重要」上。

{{< fig "diagrams/02-log-derivation.svg" "事件日志与派生历史的关系：日志是唯一真源，模型消息、UI 视图、审计导出都是投影。投影必须是纯函数，否则同一份日志能派生出两个不同的请求。" >}}

一个必须遵守的纪律：**模型可见 ⟺ 已记录**。任何你要发给模型的东西，先写一条事件。哪怕是「当前时间」这种看起来随时能取的值——否则重放时它变了，请求就不可重建。

**验收标准**：关掉进程再打开，能用同一份日志发出逐字节相同的请求（至少 `messages` 相同）；UI 能显示「这条消息来自哪次操作」。

**此时不要做**：压缩。你还没有稳定的 `seq` 与替换语义，压了也恢复不了。

## 6. 阶段四：取消、steering 与审批（第 3 周）

用户按「停止」时，最糟的结果是「看起来停了，其实还在跑」。要避免它，需要三个正交的机制：

**取消**：一个 `AbortController`，`abort(cause)` 后所有正在进行的请求与工具都收到信号。关键细节是 **cause 要留下来**：是用户取消、父 agent 取消、还是 hook 拒绝？三者对日志与 UI 的含义不同。

**steering**：用户想在当前步骤之后插一句话。它必须进 `next-step` 队列，而不是直接改 `messages`——否则当前请求已经发出，模型看不到它，日志也会出现「消息在请求之后」的错位。

**审批**：一个一次性提示，返回 `allow` / `deny` / `allow-always`。三个约束：

- 缺失或不可答 = deny（fail closed）。
- 审批要能看见**即将执行的参数**，不是工具名。
- 「总是允许」要能撤销，否则它会变成永久后门。

```ts
agent.cancel({ kind: 'user' })          // 只 abort，不写日志；日志由循环写
agent.steer(message)                    // 进 next-step，等当前 step 结束
agent.inject(note)                      // 进 next-step，但不唤醒驱动
```

这三个方法的区别很容易混。一个记忆法：**cancel 停，steer 插话并继续，inject 只是留个便条。**

**验收标准**：取消后 100ms 内不再有新的模型请求；取消后工具不再被调度；steering 在当前步骤结束后被模型看到；拒绝的审批不会执行工具。

## 7. 阶段五：压缩与召回（第 4 周）

到这一步，会话已经会长到超出上下文。压缩的最小正确实现是：

1. 在请求前检查预算（token 估算 + 安全余量）。
2. 选一段「可牺牲」的历史（通常是较早的工具结果，或一整段已完成的任务）。
3. 用一条替换事件遮蔽它：`{ op: 'replace', startSeq, endSeq }`，并列出被遮蔽的 seq。
4. 让派生逻辑跳过被遮蔽的节点。
5. 重试请求。

关键不是摘要质量，是**替换的语义**：

- 被替换的节点不能删除，只能遮蔽——否则 `seq` 会变，后续所有事件失效。
- 替换事件必须列出它遮蔽的所有节点，否则折叠器无法验证完整性。
- 节点 0（系统提示词）通常不允许被遮蔽。
- 压缩后如果请求还是超限，要能继续压，而不是放弃。

召回是压缩的补集：把「被压掉但可能有用」的内容做成可检索的索引，让模型主动取回。它比压缩难得多，因为它需要判断「什么值得留」，而且检索结果会再次占用预算。

**验收标准**：长会话能继续；压缩后模型仍知道任务目标；被压缩的内容可以在需要时取回（哪怕是让用户重述）。

## 8. 阶段六：子 agent（第 5 周）

单 agent 的上限是上下文与专注度。子 agent 解决的是「把一块工作隔离出去」：

- 它有自己的会话历史（不继承父的全部上下文，只继承一个种子）。
- 它有自己的工具集（通常更小）。
- 它的结果以一条消息回到父会话。

三个必须想清楚的问题：

**生命周期谁拥有。** 父 agent 创建子 agent，谁负责销毁？DSH 的答案是「创建者持有 `dispose` capability」，且 dispose 会停循环、等退出、注销、移除会话。没有 owner 的子 agent 会泄漏。

**结果怎么回。** 不要直接把子 agent 的完整历史拼进父会话——那等于没隔离。回一条摘要 + 关键产物路径。

**失败怎么办。** 子 agent 失败不应该让父 agent 崩。它应该变成一条错误结果，或者一个「需要重试」的信号。

**验收标准**：子 agent 崩溃不影响父 agent；父会话不会因委派而无限膨胀；能看到每个子 agent 的状态与产物。

## 9. 成熟度阶梯

把上面六个阶段压成一张表。每一级都有明确的验收标准，不要跳级。

| 级别 | 能力 | 验收标准 | 典型失败 |
| --- | --- | --- | --- |
| L0 | 单轮问答 | 能回答，能拒绝 | — |
| L1 | 工具循环 | 连续调用两次工具并收敛 | 无限循环、工具结果丢失 |
| L2 | 工具流水线 | 拒绝/失败/超时都有明确结果 | 副作用已发生才审批 |
| L3 | 事件日志 | 重启后请求可重建 | 「模型可见」的东西没记录 |
| L4 | 取消与审批 | 停止即停，拒绝即不执行 | 停止后仍在跑、审批可绕过 |
| L5 | 压缩与召回 | 长会话不崩，目标不丢 | 压缩后模型忘了在做什么 |
| L6 | 多 agent | 隔离、可回收、失败不扩散 | 子 agent 泄漏、上下文混合 |

{{< fig "diagrams/03-maturity-ladder.svg" "L0 到 L6 的成熟度阶梯：每一级标注能力、验收标准与最常见的失败。阶梯是单向的——L4 的取消语义依赖 L3 的日志，L5 的压缩依赖 L3 的 seq。" >}}

一个反直觉的观察：**L3 是最容易被跳过、也最贵的一级。** 跳过它，你依然能做出 L4/L5 的 demo，但每一个功能都会在「崩溃一次」之后暴露问题。而补 L3 不是加功能，是改数据模型。

## 10. 怎么验收：五个现在就能写的测试

阶梯上的「验收标准」如果只靠手点，会在第三次重构后失效。下面五个测试不依赖模型，用假 LLM 与假工具就能跑，而且每一条都对应一个真实踩过的坑。

**测试一：派生是纯函数。**

```ts
const log = [...events]
const a = deriveMessages(log)
const b = deriveMessages(log)
assert.deepEqual(a, b)            // 同输入同输出
assert.ok(Object.isFrozen(log))   // 派生没有改日志
```

如果这条挂了，说明投影里混进了时间、随机数或就地排序。它比任何集成测试都更早暴露「模型可见 ⟺ 已记录」的破口。

**测试二：取消之后不再有新请求。**

```ts
let requests = 0
const fake = { stream: async () => { requests++; await sleep(50); return done } }
const run = agent.followup(msg)
await tick()                       // 让第一次请求发出
agent.cancel({ kind: 'user' })
await run
assert.equal(requests, 1)          // 取消后没有第二次
assert.equal(agent.status, 'idle')
```

注意 `await run` 是必须的：取消是异步生效的，只断言「立刻没请求」会漏掉延迟调度。

**测试三：崩溃后的日志尾部能被补全。**

```ts
const truncated = log.slice(0, log.findIndex(e => e.type === 'step/start') + 1)
const repaired = openTurnClosers(truncated, { kind: 'interrupted' })
assert.deepEqual(repaired.map(e => e.type), ['tool/result', 'step/end', 'turn/end'])
assert.deepEqual(repaired.map(e => e.seq), [n, n + 1, n + 2])
```

三个断言分别防三件事：工具结果不能丢、step 必须先于 turn 关闭、合成事件的编号必须连续。

**测试四：压缩只遮蔽，不删除。**

```ts
const before = log.length
await compact(session, { from: 10, to: 24 })
assert.equal(log.length, before + 1)                       // 只多了一条 replace
assert.ok(log.every((e, i) => e.seq === i))                // 编号没被重排
assert.ok(!deriveMessages(log).some(m => m.seq >= 10 && m.seq <= 24))
```

第三条断言容易写错：要看的是「被遮蔽的节点不再出现在派生结果里」，而不是「日志里没有它们」。

**测试五：拒绝的审批不会执行工具。**

```ts
let called = 0
const tool = { name: 'rm', execute: async () => { called++; return ok } }
const result = await pipeline.run(call, { approve: async () => 'deny' })
assert.equal(called, 0)
assert.equal(result.isError, true)
assert.match(result.content[0].text, /denied/i)
```

这条测试的价值在于它约束的是**顺序**：审批必须在 `execute` 之前。把审批挪到 `post-execute`，测试仍然可能通过（因为结果被替换了），所以还要加一个「副作用计数器」——上面这个 `called` 就是。

把这五个测试放进 CI，你就有了一条不会随重构失效的底线。它们不测试模型好不好，只测试你的循环是否自洽。

## 11. 十个坑

1. **把 `messages` 当历史。** 一旦要压缩或 fork，就必须重写。改法是先加日志、让 `messages` 变成派生值。
2. **在流式回调里直接改历史。** 流是瞬态的；只有 `assistant/message` 才是记录。中断时要么写带 `interrupted` 的消息，要么写 attempt。
3. **工具失败直接抛。** 模型看不到错误，整轮终止。工具失败要变成 `isError` 结果。
4. **审批放在执行后。** 副作用已经发生，审批只是通知。
5. **取消只 abort 请求，不 abort 工具。** 工具还在跑，可能继续写文件。
6. **重试不区分错误码。** 鉴权失败重试一百次，上下文超限重试一百次。
7. **压缩删除事件。** `seq` 变化会让后续所有引用失效；只能遮蔽。
8. **子 agent 共享父的完整历史。** 隔离失效，父会话被撑爆。
9. **「模型可见」与「已记录」不一致。** 典型是时间、随机数、环境变量直接拼进提示词。重放时全变。
10. **没有 owner 的后台任务。** 进程退出时还在跑，或者永远不退出。每个异步操作要有一个生命周期控制器。

## 12. 什么时候不该自建

自建 Agent 的合理性来自「你有特殊需求」：

- 特殊的模型路由或私有部署。
- 特殊的工具生态（内部系统、硬件、遗留协议）。
- 特殊的合规要求（审计、数据驻留、审批链）。
- 需要深度定制循环（比如领域特定的验证器）。

如果只是「想让模型读文件、跑命令、改代码」，现成的 harness 已经把这些坑踩过了。自建的价值在于**理解**，不在于**替代**。

一个务实的路径：先用现成 harness 跑通你的任务，记录它在哪些地方不够用；再决定是写插件，还是自建。多数情况下，答案是在扩展点上写插件，而不是重写循环。

{{< fig "diagrams/04-build-or-extend.svg" "自建还是扩展的判定树：先问需求是否落在现有扩展点上，再问是否涉及数据模型变更，最后才考虑重写循环。" >}}

## 13. 这个系列怎么读

二十一篇不是线性教程，而是三条路径：

**路径 A：先建立全局图景**
[01 解剖](/posts/dsh-architecture-anatomy.html) → [02 事件溯源](/posts/dsh-session-event-sourcing.html) → [06 三层状态机](/posts/dsh-turn-step-state-machine.html) → [07 请求组装](/posts/dsh-request-assembly.html)

**路径 B：先解决输入与中断**
[03 收件箱](/posts/dsh-input-inbox.html) → [04 steering 与取消](/posts/dsh-steering-and-cancel.html) → [05 人在环里](/posts/dsh-human-in-the-loop.html) → [08 失败与恢复](/posts/dsh-error-retry-recovery.html)

**路径 C：先做工具与多 agent**
[09 工具注册表](/posts/dsh-tool-registry-scope.html) → [10 工具流水线](/posts/dsh-tool-pipeline.html) → [11 工具边界](/posts/dsh-tool-boundaries.html) → [12 工具结果](/posts/dsh-tool-results.html) → [13 子 agent 生命周期](/posts/dsh-subagent-lifecycle.html) → [14 子 agent 控制](/posts/dsh-subagent-control.html) → [15 agent team](/posts/dsh-agent-team.html) → [16 workflow/jobs/goal](/posts/dsh-workflow-jobs-goal.html)

**路径 D：先做记忆**
[17 记忆架构](/posts/dsh-memory-architecture.html) → [18 上下文注入](/posts/dsh-context-injection.html) → [19 压缩](/posts/dsh-compaction.html) → [20 召回](/posts/dsh-recall.html)

{{< fig "diagrams/05-series-map.svg" "二十一篇的四条阅读路径。箭头是推荐阅读顺序；同一条路径上的文章可以顺读，也可以只挑与当前问题相关的一篇。" >}}

## 14. 最后

搭 Agent 最难的部分不是让它在顺利时跑起来，而是让它在不顺利时**仍然可解释**：

- 为什么模型看到了这条消息？
- 为什么这个工具被拒绝了？
- 为什么这轮结束了？
- 为什么重启之后它忘了？

这四个问题，分别对应日志、审批、状态机与压缩。它们没有一个是「模型能力」问题，全是**数据形状**问题。

所以如果你只从这篇带走一句话：**先把历史做成日志，其余的一切都会变简单。**

上一篇：[失败、重试与恢复](/posts/dsh-error-retry-recovery.html) ｜ 回到：[系列总览](/posts/dsh-architecture-anatomy.html)
