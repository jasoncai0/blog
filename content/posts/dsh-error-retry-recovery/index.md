---
title: "失败、重试与恢复：把异常也变成可回放的数据"
date: "2026-11-11T13:00:00+08:00"
summary: "Agent 一定会在半夜挂掉。DSH 的做法是让每次失败留下可重放的结算记录，重试由策略统一管，不散落在 try/catch 里。"
description: "四类失败位置、LlmFailure 的可路由分类、provider 拥有的重试策略与持久化退避、assistant/attempt 的结算矩阵、工具失败与批次部分失败，以及崩溃后如何用合成事件收尾孤儿轮次。"
tags: ["architecture", "agent", "concurrency"]
slug: "dsh-error-retry-recovery"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。行号以该 commit 为准，引用一律指向 `src/`。
> 依据：`packages/core/agent-loop/src/agent.ts:435-610`（尝试与结算）、`packages/llm/llm-retry/src/index.ts`（重试策略执行器）、`packages/llm/llm/src/retry-policy.ts`（默认值）、`packages/llm/llm/src/error.ts`（失败分类）、`packages/core/session/src/repair.ts`（崩溃收尾）。

一次 Agent 运行会以四种方式出错，处理方式各不相同：

| 出错位置 | 典型原因 | 谁来处理 |
| --- | --- | --- |
| 请求前 | 上下文超限、工具准备失败、参数非法 | 循环 / 压缩插件 |
| 请求中 | 限流、超时、5xx、鉴权失败 | provider 策略 / 用户 |
| 流式中 | 连接中断、内容为空 | 结算逻辑 |
| 工具中 | 命令失败、权限拒绝、超时 | 工具结果本身 |

四类混在一起用一个 `try/catch` 包住，是最常见的写法。它们的恢复语义不同：有的能原地重试，有的必须先压缩上下文，有的应该变成一条错误结果交给模型，有的只能终止。

## 1. 失败是一个对象，不是一段字符串

```ts
interface LlmFailure {
  readonly message: string
  readonly code: string
  readonly status?: number
  readonly providerRetryAfterMs?: number
  readonly requestId?: ProviderRequestId
  readonly offloadImages?: number
}
```

五个字段各有用途：

- `code`：稳定、与提供方无关的机器路由码。重试策略只认它，绝不解析 `message`。
- `status`：HTTP 状态，便于诊断，但不直接决定重试。
- `providerRetryAfterMs`：提供方明确要求的等待时间。策略必须尊重它，但不能无条件信任。超过 `maxDelayMs` 时要么放弃，要么退回本地退避。
- `requestId`：用于向提供方追查。
- `offloadImages`：只在 `IMAGE_OFFLOAD_REQUIRED` 时出现，告诉图片卸载插件「还要卸几张」。

码表里几个常用的：

| code | 含义 | 默认可重试 |
| --- | --- | --- |
| `RATE_LIMIT` | 限流 | 是 |
| `SERVER` | 5xx | 是 |
| `TIMEOUT` | 超时 | 是 |
| `TRANSPORT` | 连接层失败 | 是 |
| `EMPTY_RESPONSE` | 正常结束但零内容块 | 是 |
| `CONTEXT_WINDOW_EXCEEDED` | 上下文超限 | 否（走压缩） |
| `IMAGE_OFFLOAD_REQUIRED` | 图片超预算 | 否（走卸载） |
| `AUTH` / `INVALID_CREDENTIAL` | 凭据缺失/畸形 | 否 |
| `QUOTA` / `ACCOUNT_QUOTA` | 额度耗尽 | 否 |
| `NO_ADAPTER` | 没有注册适配器 | 否（但循环容忍它，交给中间件） |

上下文超限的识别最麻烦，不同提供方的措辞完全不同。DSH 用一组正则把「context length exceeded」「too large for the model's context window」「input exceeds the model context」等说法归一成 `CONTEXT_WINDOW_EXCEEDED`，匹配范围收得很窄，只认明确指向模型上下文的措辞：「exceeds」后面必须跟 context，否则可能是「exceeds rate limit」。

{{< fig "diagrams/01-failure-loci.svg" "四类失败位置与各自的处理者：请求前失败可由压缩或工具准备修复，请求中失败交给 provider 重试策略，流式中失败进入结算，工具失败变成结果。" >}}

## 2. 重试是 provider 的策略，不是调用方的参数

DSH 把重试策略放在适配器注册时，请求里不传这个参数：

```ts
type RetryPolicyConfig =
  | { mode: 'normal'; maxRetries?: number; retryableCodes?: string[]; backoff?: BackoffConfig }
  | { mode: 'always'; backoff?: BackoffConfig }
```

默认值：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `maxRetries` | 5 | 首次请求之后最多重试 5 次 |
| `initialDelayMs` | 500 | 首次退避基数 |
| `maxDelayMs` | 10000 | 单次等待上限 |
| `jitterRatio` | 0.1 | 对称抖动范围 |
| `retryableCodes` | 上表五个 | 只有这些码走重试 |

退避公式：

```ts
const exponent = Math.min(retry - 1, 1024)
const exponential = Math.min(initialDelayMs * 2 ** exponent, maxDelayMs)
const jitter = 1 - jitterRatio + 2 * jitterRatio * random()
return Math.min(exponential * jitter, maxDelayMs)
```

`exponent` 封顶 1024 是为了防止 `2 ** retry` 溢出成 `Infinity`；`jitter` 是对称乘性抖动（0.9–1.1），不是加性偏移。乘性抖动在延迟很大时更均匀，也更不容易让一群客户端同时醒来。

`mode: 'always'` 是无界重试，只到成功、取消或插件卸载为止。它看起来危险，对「本地模型还在加载」「网关短暂 502」这类场景却是必要的。它也不会跳过下游：`always` 仍然先调 `next()`，让下游策略有机会接管；只有 downstream 抛错或返回 undefined 时才自己重试。

{{< fig "diagrams/02-retry-decision.svg" "重试决策树：策略缺失→交给下游；策略存在但码不匹配→交给下游；匹配则读提供方 Retry-After，超过上限时 normal 放弃、always 退回本地退避；最后检查 maxRetries 与取消信号。" >}}

## 3. 重试状态为什么进投影

重试计数不能只放内存，进程可能在任何时刻挂掉。DSH 把状态注册成会话投影：

```ts
ctx.sessionProjections.register({
  key: 'llmRetry',
  stateVersion: 1,
  stateSchema,
  init: () => ({}),
  apply: (state, event) => {
    if (event.type === 'step/start' || event.type === 'turn/end') return {}
    if (event.type !== 'llm/retry') return state
    ...
  },
})
```

两条规则：

- `step/start` 与 `turn/end` 重置状态。重试预算按「步」重置，不跨轮累积；否则一个长会话跑到后面就永远不能重试了。
- 只有 `llm/retry` 事件推进计数，且用 `retryId` 去重（同一条事件重放不会重复计数）。

投影的 key 是 `provider + policyKey`。`policyKey` 是策略的规范化 JSON（含 mode、maxRetries、排序后的 codes、退避参数）。这样同一 provider 换了策略，计数不会串。

## 4. 先落账，再等待

重试里最要紧的是落账顺序，退避计算反而简单：

```ts
agent.session.append('llm/retry', eventData)
if (!await cancellableDelay(delayMs, fusedSignal)) return
agent.session.append('llm/retry-started', { retryId, turn, step, retry })
return { kind: 'retry' }
```

先写 `llm/retry`（包含完整 failure、delayMs、retry 序号），再等待，等待成功后才写 `llm/retry-started`。这带来三个性质：

- 崩溃发生在等待中：日志里有一条「计划在 X 毫秒后重试第 N 次」的记录，恢复时可以判断这是第几次，而不是从零开始。
- 取消发生在等待中：`cancellableDelay` 返回 `false`，不写 `retry-started`，也不返回 `retry`。循环看到 `undefined` 就保留原错误终止。
- 等待成功但进程随即崩溃：`retry-started` 已经落账，下一次恢复知道「这次重试已经开始了」，不会把同一次重试算两遍。

`fusedSignal` 是 `AbortSignal.any([turnSignal, pluginLifetime])`。插件卸载也会中止等待，避免热重载时留下悬空的定时器。

{{< fig "diagrams/03-retry-ledger.svg" "重试的落账顺序：失败 → 写 llm/retry（含 delay/序号/failure）→ 可取消等待 → 写 llm/retry-started → 返回 retry。任何一步崩溃，日志都能说明重试进行到哪。" >}}

## 5. 尝试的结算矩阵

`step()` 里对一次流式尝试有四种结局，每种都要落账：

| 结局 | 写什么 | 进模型历史 |
| --- | --- | --- |
| 成功（有内容） | `assistant/message` + 内嵌 stream/usage | 是 |
| 成功但零内容 | `assistant/message`（空内容） | 否（派生时返回 null） |
| 被取消且有部分内容 | `assistant/message` + `interrupted: true` | 是 |
| 被取消且无内容 / 失败 | `assistant/attempt` | 否 |

失败也要有记录。没有 `assistant/attempt`，日志里就只剩一个 `step/start` 和一个 `tool/call`，重建时分不清「模型没回」和「回了但没记」。`assistant/attempt` 携带 `settlement`：

```ts
type AssistantAttemptSettlement =
  | { kind: 'error'; failure: LlmFailure }
  | { kind: 'cancelled'; reason?: string }
```

还有一个边界：流在第一个 chunk 之前就失败时，`live.start()` 还没执行，没有可结算的尝试，直接抛。这样不会为「连接都没建立」写一条空 attempt。

结算本身也可能失败（比如 JSON 序列化失败），此时抛 `AggregateError`，同时保留原错误：记账失败不该把原来那个失败吞掉。

## 6. 重试的边界：哪些失败不该重试

一条经验法则：**重试的前提是「重复执行不会改变结果语义」**。

- `INVALID_CREDENTIAL`：凭据畸形，重试一百次也一样。
- `QUOTA`：额度耗尽，重试只会更糟。
- `CONTEXT_WINDOW_EXCEEDED`：必须压缩，重试同样的请求必然再失败。
- `IMAGE_OFFLOAD_REQUIRED`：必须先卸载图片，重试前请求内容已经变了。
- `NO_ADAPTER`：配置错误，重试无用；但循环容忍它，因为中间件可能服务未注册的路由。

后两类走的是另一条恢复路径：DSH 把它们放在 `agent/request-error` 的 waterfall 上，让不同插件按 `code` 认领：

```
CONTEXT_WINDOW_EXCEEDED → compaction-basic：压缩后重试
IMAGE_OFFLOAD_REQUIRED  → compaction-image-offload：卸载后重试
其余可重试码            → llm-retry：退避后重试
```

这个 waterfall 的语义很特别：监听器返回 `{kind:'retry'}` 且不调用 `next()`，表示它接管了恢复。如果调了 `next()` 且下游返回 undefined，则由下游决定终止。默认（无人接管）是终止。

{{< fig "diagrams/04-attempt-settlement.svg" "尝试结算的决策树：流是否已开始、是否有内容、是否被取消、finish 是否为 error/aborted。四条路径分别写 assistant/message、带 interrupted 的 message 或 assistant/attempt。" >}}

## 7. 工具失败不是异常

工具执行失败不该抛到循环外面，应该变成一条 `isError: true` 的工具结果，让模型看到并决定下一步。Agent 和普通程序在这里分得很开：错误是给模型的输入，不是控制流的终点。

流水线里，工具抛错会被规范化成结果：

```ts
catch (error) {
  return { content: [{ type: 'text', text: errorMessage(error) }], isError: true }
}
```

但有三种情况仍然会向上抛：

- 管道阶段本身失败（pre/post waterfall 抛错、结果无法无损快照）。这是插件 bug，不是工具失败。
- 批次被取消。取消要走 turn 的 abort 路径，不能伪装成工具结果。
- 批次部分失败：`ToolBatchExecutionError` 携带 `partial`，循环会先把已产生的 `additionalContexts` 注入，再把 cause 抛出。不能因为一个工具炸了，就丢掉别的工具留下的上下文。

`concludesTurn: true` 是另一种数据决定控制流：某个工具结果声明「这一步应该结束轮次」。它是个标记，随结果携带，循环读到才生效。这样「提前停」也能回放。

## 8. 崩溃恢复：用合成事件收尾

进程被杀时，日志尾部可能停在任意位置：

```
turn/start 1
  step/start 1
    assistant/message (tool-call tc_1)
    tool/call tc_1
    ← 进程死了
```

下次打开会话，`interruptedTurnClosers()` 会扫描日志，补出：

1. 未答复的 `tool/result`（按 assistant 顺序，插入序保持 transcript 顺序）
2. 未关闭的 `step/end`
3. `turn/end { reason: { kind: 'interrupted' } }`

合成结果的文案区分两种生命周期事实：

```ts
// 记了 tool/call，但没有结果
'The tool call was interrupted after it was recorded, but no result was durably recorded. Its outcome is unknown. Decide whether to retry from the tool semantics: retry only if the operation is read-only or idempotent; if it may have side effects, first verify external state or ask the user. Do not retry blindly.'

// 连 tool/call 都没有
'The tool call was interrupted before the Harness recorded it as started. Retry it if it is still needed.'
```

这段文案是给模型的操作指令：先判断工具是否幂等，再决定重试还是核实外部状态。错误码也分开：`TOOL_OUTCOME_UNKNOWN` 与 `TOOL_NOT_STARTED`。这比一句「工具执行失败」有用得多。

合成事件的时间戳复用最后一条真实事件的时间，seq 顺序递增，因此恢复是确定性的：同一份日志，任何进程恢复出的收尾事件都一样。

fork 用同一套机制，但 cause 是 `forked`，文案强调「父会话可能在切点之后完成了它」。因为 fork 的切点可能落在父会话的开放轮次中间。

{{< fig "diagrams/05-crash-closers.svg" "崩溃收尾：日志尾部停在 tool/call 之后，恢复器补出合成 tool/result（两种文案）、step/end 与 turn/end{interrupted}；时间戳复用最后一条真实事件，seq 递增。" >}}

## 9. 取消、超时与「谁赢了」

取消的语义在并发下最容易出错。DSH 的规则是：

- `cancel(cause)` 只 abort 当前活动的 `AbortController`，不直接写日志。日志由循环在 catch 里写。
- 如果已经 abort 且原因不是 `disposed`，后续 `send()` 会记 `wakingAfterAbort`，等这一轮收尾后再起驱动。
- `turn/end` 的 reason 是 `{kind:'aborted', reason: cause}`，`cause` 是同一个 TS 对象，它既是进程内信号，也是持久化的原因。
- 已经发出的工具调用不会被静默取消：它们会带着结果回来，或者由恢复器补一条「结果未知」。

超时是另一层：`tools/execute` 的 waterfall 可以包装超时，但超时后工具可能仍在后台跑。超时结果的文案要说明「已超时」，不能写成「已取消」，两者对模型的含义不同。

## 10. 设计取舍

重试放在循环层，不放适配器层。适配器只管一次调用；循环知道 turn/step、知道日志、知道取消。代价是适配器做不了「连接级重连」，那属于它内部的传输细节。

重试状态进投影，不放内存。崩溃后仍能知道这是第几次，避免无限重试。代价是每次重试多两条事件，投影还要处理重置。

失败写 attempt，不静默。日志变长，但重建时能区分「没回」与「回了但空」。代价是 `assistant/attempt` 不能进模型历史，派生逻辑要显式跳过。

崩溃后合成收尾，不拒绝打开会话。会话仍可继续，模型能看到「结果未知」并自行判断。代价是日志里出现非真实事件，必须用 `interrupted` reason 标记。

工具错误变成结果，不向上抛。模型能自我修复，但要求工具作者写清错误文案。代价是有些 bug 会被当成「工具失败」咽下去，所以管道阶段失败仍然要抛。

## 11. 踩坑点

- 用 `message` 做重试判断。提供方改一次文案就失效；只认 `code`。
- 在重试等待里用不可取消的 `setTimeout`。取消后定时器还在，醒来又发一次请求。
- 先等待再落账。崩溃在等待中就丢了「第几次」的信息。
- 把 `CONTEXT_WINDOW_EXCEEDED` 加进 `retryableCodes`。同样的请求必然再失败，只是浪费配额。
- 重试计数不按 step 重置。长会话跑几十步后预算耗尽，再也无法重试。
- 工具失败直接抛。模型看不到错误，整轮终止；正确做法是 `isError` 结果。
- 批次失败时丢掉 `partial.additionalContexts`。别的工具留下的上下文被吞掉，模型失去线索。
- 合成收尾不复用最后时间戳。会产生「未来时间」的事件，排序和审计都会乱。
- 取消后立刻 `send()` 却期望马上跑。会进入 `wakingAfterAbort`，等收尾后才驱动。
- 把超时当取消写进结果。模型会以为操作没发生，实际上可能已经产生副作用。

## 12. 源码索引

- `packages/llm/llm/src/types.ts:48`：`LlmFailure` 六个字段。
- `packages/llm/llm/src/error.ts:1-90`：`HarnessError`、码表、上下文超限的四组正则。
- `packages/llm/llm/src/retry-policy.ts:1-90`：默认值、`normal`/`always`、`ResolvedRetryPolicy`。
- `packages/llm/llm-retry/src/index.ts:96-280`：投影、退避、`llm/retry` 先落账、`always` 的下游优先、取消与卸载。
- `packages/core/agent-loop/src/agent.ts:520-610`：流式尝试的结算分支与 `AggregateError`。
- `packages/core/agent-loop/src/agent.ts:360-380`：`ToolCallRecovery` 的订阅与异常补写。
- `packages/core/session/src/repair.ts:1-200`：`openTurnClosers`、两种码、文案、确定性时间戳。
- `packages/core/tools/src/executor.ts:89`：`ToolBatchExecutionError.partial`。
- `packages/core/tools/src/contracts.ts:331`：`concludesTurn`。

错误处理是 Agent 工程里最不性感的部分，也是最容易决定成败的部分。一个跑得通的 demo 和一个能过夜的 Agent，差别往往就在这些结算、落账与收尾上。

上一篇：[请求是怎么装出来的](/posts/dsh-request-assembly.html) ｜ 下一篇：[从零搭建一个 Agent](/posts/dsh-build-agent-from-zero.html)
