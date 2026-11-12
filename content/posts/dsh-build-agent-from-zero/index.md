---
title: "从零搭建一个 Agent：五个阶段、十个决策、一张成熟度阶梯"
date: "2026-11-11T20:30:00+08:00"
summary: "不讲框架 API，只讲顺序：先有一个能跑的最小循环，再逐步加上工具流水线、事件日志、取消、压缩与子 agent。每一阶段给出验收标准与不该提前做的事。"
description: "面向初学者的 Agent 搭建指南：最小循环的 60 行代码、十个必须尽早做的决策、从 L0 到 L5 的成熟度阶梯、以及最常见的十个坑。"
tags: ["agent", "architecture", "planning"]
slug: "dsh-build-agent-from-zero"
---

> 依据：本系列前二十篇的源码结论，加上 `docs/architecture.zh.md` 与 `docs/subsystems/core.zh.md` 的接口契约。代码示例按 DSH 的形状重写，是个最小版，没有逐行照抄 DSH 源码。

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

这确实是一个 Agent，跑 demo 没问题，有些任务上表现也不错。但它离「能过夜」还差五层。每加一层，改的都是数据的形状，光多写几行代码不够。

这篇不铺框架 API，只讲顺序：什么时候加什么，哪些不能提前加，每一层的验收标准是什么。

## 1. 先定义最小 Agent

写代码之前，先认下四个定义。它们看着像废话，后面的每一个坑，都能追溯到其中一条被破坏。

输入是队列，一次调用装不下。用户可能在 Agent 跑的时候再发一条消息：它也许该插到当前步骤之后（steering），也许该等这一轮结束（follow-up）。API 如果只有 `run(prompt)`，迟早要重写。

循环的单位是轮次。一轮里可能有多次模型请求（每次带工具），也可能一次都没有（输入被拒绝）。把「请求」当循环单位，工具结果就会无处安放。

历史要按日志存。`messages.push()` 的问题是它既是真源又是缓存。一旦要压缩、要 fork、要审计，就必须回答「这条消息是哪次操作产生的」。日志答得上来，数组答不上来。

工具是一套协议。它会失败、会超时、需要审批、会产生副作用、需要被模型看见结果。把它当成 `await fn()`，错误处理就没有位置。

## 2. 十个决策

下面十个决策，越早定越好。拖到最后，要动的是数据模型。

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

一个实用的判断：第 2、3 项没想清楚，先别写第 9 项。压缩建立在「历史可重建」上；循环单位不清，压缩就不知道该压哪一段。

## 3. 阶段一：一个能跑的循环（第 0 天）

先写一个最小的循环，形状要正确。功能可以少，边界一开始就得对：

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

验收标准：模型能连续调用两次工具并给出最终回答；用户在一次运行中再发一条消息，不会丢。

此时不要做：并行工具、重试、压缩、子 agent。它们都会逼你重新设计上面的数据结构，而你现在还没有数据。

## 4. 阶段二：工具流水线（第 1 周）

工具有了副作用之后，`await fn()` 就不够用了。一次工具调用要拆成四个可插入的阶段：

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

写第一个工具之前，有三件事要定下来：

- 审批放在哪一层。想「先看参数再决定要不要执行」，审批就得在 `pre-execute`，并且能改写参数。放到执行后，副作用已经发生。
- 错误怎么分类。工具内部错误 → `isError` 结果；管道阶段错误（pre/post 抛错、结果不可序列化）→ 向上抛；取消 → 走 abort。
- 结果怎么序列化。模型能看到的，必须是能 JSON 无损表示的值。如果工具返回一个对象、一个流、一个句柄，先在 post-execute 里投影成文本/图片。

验收标准：一个会失败的工具，模型能看到错误并自行修正；被拒绝的调用不会执行；每个结果都有唯一记录。

此时不要做：动态工具集、工具间的依赖图。先把「一次调用」的生命周期走通。

## 5. 阶段三：把历史变成日志（第 2 周）

这一步最容易被跳过，也最值得做。历史别再放 `messages` 数组，改成只追加的事件日志：

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

模型看到的消息，从日志派生出来：

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

有三件事马上变简单：

- 崩溃恢复：日志尾部不平衡，补 `turn/end` 就能重新打开。
- 审计：每个模型看到的内容都能追溯到一条事件。
- fork：按 `seq` 切一段日志，就是一条分支。

后面的功能收益更大：压缩、召回、注入、子 agent 都要靠它。没有日志，这些功能只能建立在「猜哪些消息重要」上。

{{< fig "diagrams/02-log-derivation.svg" "事件日志与派生历史的关系：日志是唯一真源，模型消息、UI 视图、审计导出都是投影。投影必须是纯函数，否则同一份日志能派生出两个不同的请求。" >}}

有一条纪律必须守住：**模型可见 ⟺ 已记录**。任何要发给模型的东西，先写一条事件。连「当前时间」这种看起来随时能取的值也一样，否则重放时它变了，请求就不可重建。

验收标准：关掉进程再打开，能用同一份日志发出逐字节相同的请求（至少 `messages` 相同）；UI 能显示「这条消息来自哪次操作」。

此时不要做：压缩。你还没有稳定的 `seq` 与替换语义，压了也恢复不了。

## 6. 阶段四：取消、steering 与审批（第 3 周）

用户按下「停止」，最糟的情况是界面停了、后台还在跑。要避免这种结果，需要三个各管一段的机制：

取消：一个 `AbortController`，`abort(cause)` 之后，正在进行的请求与工具都会收到信号。cause 要留下来：用户取消、父 agent 取消、hook 拒绝，这三种原因在日志和 UI 里的含义不同。

steering：用户想在当前步骤之后插一条消息。它得进 `next-step` 队列，不能直接改 `messages`。当前请求已经发出，直接改数组模型看不到，日志里还会出现「消息排在请求之后」的错位。

审批：一次性提示，返回 `allow` / `deny` / `allow-always`。约束有三条：

- 缺失或不可答 = deny（fail closed）。
- 审批要能看见即将执行的参数，光看工具名不够。
- 「总是允许」要能撤销，否则它会变成永久后门。

```ts
agent.cancel({ kind: 'user' })          // 只 abort，不写日志；日志由循环写
agent.steer(message)                    // 进 next-step，等当前 step 结束
agent.inject(note)                      // 进 next-step，但不唤醒驱动
```

这三个方法容易混。记法：cancel 停，steer 插话并继续，inject 只是留张便条。

验收标准：取消后 100ms 内不再有新的模型请求；取消后工具不再被调度；steering 在当前步骤结束后被模型看到；拒绝的审批不会执行工具。

## 7. 阶段五：压缩与召回（第 4 周）

走到这里，会话会长到装不进上下文。压缩的最小正确实现分五步：

1. 在请求前检查预算（token 估算 + 安全余量）。
2. 选一段「可牺牲」的历史（通常是较早的工具结果，或一整段已完成的任务）。
3. 用一条替换事件遮蔽它：`{ op: 'replace', startSeq, endSeq }`，并列出被遮蔽的 seq。
4. 让派生逻辑跳过被遮蔽的节点。
5. 重试请求。

摘要质量可以先放一边，替换的语义必须先对：

- 被替换的节点不能删，只能遮蔽。删了 `seq` 会变，后续所有事件失效。
- 替换事件必须列出它遮蔽的所有节点，否则折叠器无法验证完整性。
- 节点 0（系统提示词）通常不允许被遮蔽。
- 压缩后请求还是超限，要能接着压，不能直接放弃。

召回是压缩的补集：把「压掉了但可能还有用」的内容做成索引，让模型自己取回。它比压缩难，难在判断「什么值得留」，而且取回来的结果又要占预算。

验收标准：长会话能继续；压缩后模型仍知道任务目标；被压缩的内容可以在需要时取回（哪怕是让用户重述）。

## 8. 阶段六：子 agent（第 5 周）

单 agent 的上限由上下文和专注度决定。子 agent 的作用是把一块工作隔离出去：

- 它有自己的会话历史（不继承父的全部上下文，只继承一个种子）。
- 它有自己的工具集（通常更小）。
- 它的结果以一条消息回到父会话。

有三个问题得先想清楚：

生命周期归谁。父 agent 创建子 agent，谁来销毁？DSH 的做法是创建者持有 `dispose` capability，dispose 会停循环、等退出、注销、移除会话。没有 owner 的子 agent 会泄漏。

结果怎么回。别把子 agent 的完整历史拼进父会话，那等于没隔离。回一条摘要，加上关键产物的路径。

失败怎么办。子 agent 失败不该把父 agent 拖崩。它应该变成一条错误结果，或者一个「需要重试」的信号。

验收标准：子 agent 崩溃不影响父 agent；父会话不会因为委派无限膨胀；每个子 agent 的状态与产物都看得到。

## 9. 成熟度阶梯

上面六个阶段压成一张表。每一级都有验收标准，别跳级。

| 级别 | 能力 | 验收标准 | 典型失败 |
| --- | --- | --- | --- |
| L0 | 单轮问答 | 能回答，能拒绝 | — |
| L1 | 工具循环 | 连续调用两次工具并收敛 | 无限循环、工具结果丢失 |
| L2 | 工具流水线 | 拒绝/失败/超时都有明确结果 | 副作用已发生才审批 |
| L3 | 事件日志 | 重启后请求可重建 | 「模型可见」的东西没记录 |
| L4 | 取消与审批 | 停止即停，拒绝即不执行 | 停止后仍在跑、审批可绕过 |
| L5 | 压缩与召回 | 长会话不崩，目标不丢 | 压缩后模型忘了在做什么 |
| L6 | 多 agent | 隔离、可回收、失败不扩散 | 子 agent 泄漏、上下文混合 |

{{< fig "diagrams/03-maturity-ladder.svg" "L0 到 L6 的成熟度阶梯：每一级标注能力、验收标准与最常见的失败。阶梯是单向的：L4 的取消语义依赖 L3 的日志，L5 的压缩依赖 L3 的 seq。" >}}

一个反直觉的地方：L3 最容易被跳过，也最贵。跳过它，L4/L5 的 demo 照样能做，但每个功能都会在「崩溃一次」之后出问题。补 L3 要改数据模型，加功能解决不了。

## 10. 怎么验收：五个现在就能写的测试

阶梯上的「验收标准」如果只靠手点，第三次重构之后就失效了。下面五个测试不依赖模型，用假 LLM 和假工具就能跑。每一条都对应一个实际踩过的坑。

测试一：派生是纯函数。

```ts
const log = [...events]
const a = deriveMessages(log)
const b = deriveMessages(log)
assert.deepEqual(a, b)            // 同输入同输出
assert.ok(Object.isFrozen(log))   // 派生没有改日志
```

这条挂了，说明投影里混进了时间、随机数或就地排序。它比集成测试更早暴露「模型可见 ⟺ 已记录」的破口。

测试二：取消之后不再有新请求。

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

测试三：崩溃后的日志尾部能被补全。

```ts
const truncated = log.slice(0, log.findIndex(e => e.type === 'step/start') + 1)
const repaired = openTurnClosers(truncated, { kind: 'interrupted' })
assert.deepEqual(repaired.map(e => e.type), ['tool/result', 'step/end', 'turn/end'])
assert.deepEqual(repaired.map(e => e.seq), [n, n + 1, n + 2])
```

三个断言分别防三件事：工具结果不能丢、step 必须先于 turn 关闭、合成事件的编号必须连续。

测试四：压缩只遮蔽，不删除。

```ts
const before = log.length
await compact(session, { from: 10, to: 24 })
assert.equal(log.length, before + 1)                       // 只多了一条 replace
assert.ok(log.every((e, i) => e.seq === i))                // 编号没被重排
assert.ok(!deriveMessages(log).some(m => m.seq >= 10 && m.seq <= 24))
```

第三条容易写错。要断言的是被遮蔽的节点不再出现在派生结果里；日志里它们还在。

测试五：拒绝的审批不会执行工具。

```ts
let called = 0
const tool = { name: 'rm', execute: async () => { called++; return ok } }
const result = await pipeline.run(call, { approve: async () => 'deny' })
assert.equal(called, 0)
assert.equal(result.isError, true)
assert.match(result.content[0].text, /denied/i)
```

这条测试约束的是顺序：审批必须在 `execute` 之前。把审批挪到 `post-execute`，测试仍可能通过（结果被替换了），所以还得加一个副作用计数器，也就是上面那个 `called`。

五个测试放进 CI，就有一条不随重构失效的底线。它们不衡量模型好坏，只看循环本身是否自洽。

## 11. 十个坑

1. 把 `messages` 当历史。一旦要压缩或 fork，就得重写。改法是先加日志，让 `messages` 变成派生值。
2. 在流式回调里直接改历史。流是瞬态的，`assistant/message` 才是记录。中断时要么写带 `interrupted` 的消息，要么写 attempt。
3. 工具失败直接抛。模型看不到错误，整轮终止。工具失败要变成 `isError` 结果。
4. 审批放在执行后。副作用已经发生，审批只剩通知。
5. 取消只 abort 请求，不 abort 工具。工具还在跑，可能继续写文件。
6. 重试不区分错误码。鉴权失败重试一百次，上下文超限也重试一百次。
7. 压缩删除事件。`seq` 一变，后续所有引用失效。只能遮蔽。
8. 子 agent 共享父的完整历史。隔离没了，父会话也会被撑爆。
9. 「模型可见」与「已记录」对不上。典型是时间、随机数、环境变量直接拼进提示词，重放时全变。
10. 后台任务没有 owner。进程退出时它还在跑，或者永远不退出。每个异步操作都要有生命周期控制器。

## 12. 什么时候不该自建

自建 Agent 的理由，通常是有特殊需求：

- 特殊的模型路由或私有部署。
- 特殊的工具生态（内部系统、硬件、遗留协议）。
- 特殊的合规要求（审计、数据驻留、审批链）。
- 需要深度定制循环（比如领域特定的验证器）。

自建的价值在理解，不在替代。

一条务实的路径：先用现成 harness 跑通任务，记下它哪里不够用，再决定写插件还是自建。多数情况下，写插件就够了，不必重写循环。

{{< fig "diagrams/04-build-or-extend.svg" "自建还是扩展的判定树：先问需求是否落在现有扩展点上，再问是否涉及数据模型变更，最后才考虑重写循环。" >}}

## 13. 这个系列怎么读

二十一篇不用从头读到尾，可以按路径挑：

路径 A：先建立全局图景
[01 解剖](/posts/dsh-architecture-anatomy.html) → [02 事件溯源](/posts/dsh-session-event-sourcing.html) → [06 三层状态机](/posts/dsh-turn-step-state-machine.html) → [07 请求组装](/posts/dsh-request-assembly.html)

路径 B：先解决输入与中断
[03 收件箱](/posts/dsh-input-inbox.html) → [04 steering 与取消](/posts/dsh-steering-and-cancel.html) → [05 人在环里](/posts/dsh-human-in-the-loop.html) → [08 失败与恢复](/posts/dsh-error-retry-recovery.html)

路径 C：先做工具与多 agent
[09 工具注册表](/posts/dsh-tool-registry-scope.html) → [10 工具流水线](/posts/dsh-tool-pipeline.html) → [11 工具边界](/posts/dsh-tool-boundaries.html) → [12 工具结果](/posts/dsh-tool-results.html) → [13 子 agent 生命周期](/posts/dsh-subagent-lifecycle.html) → [14 子 agent 控制](/posts/dsh-subagent-control.html) → [15 agent team](/posts/dsh-agent-team.html) → [16 workflow/jobs/goal](/posts/dsh-workflow-jobs-goal.html)

路径 D：先做记忆
[17 记忆架构](/posts/dsh-memory-architecture.html) → [18 上下文注入](/posts/dsh-context-injection.html) → [19 压缩](/posts/dsh-compaction.html) → [20 召回](/posts/dsh-recall.html)

{{< fig "diagrams/05-series-map.svg" "二十一篇的四条阅读路径。箭头是推荐阅读顺序；同一条路径上的文章可以顺读，也可以只挑与当前问题相关的一篇。" >}}

## 14. 最后

搭 Agent 最难的地方，是让它出了问题时仍然可解释，顺利跑起来反而容易：

- 为什么模型看到了这条消息？
- 为什么这个工具被拒绝了？
- 为什么这轮结束了？
- 为什么重启之后它忘了？

这四个问题分别落在日志、审批、状态机与压缩上。它们跟模型能力无关，都是数据形状的问题。

如果现在只做一件事，就把历史做成日志。后面的压缩、召回、子 agent 都建在它上面。

上一篇：[失败、重试与恢复](/posts/dsh-error-retry-recovery.html) ｜ 回到：[系列总览](/posts/dsh-architecture-anatomy.html)
