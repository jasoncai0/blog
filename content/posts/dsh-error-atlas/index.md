---
title: "错误的地图：异常在哪一层变形，模型又能看到什么"
date: "2026-11-16T20:45:00+08:00"
summary: "HarnessError 的 code 体系、两处规范化边界、重试决策顺序，以及模型与 UI 各自能看到什么。"
description: "从 provider 适配器到 turn/end：错误类的继承树、工具与轮次两处规范化、可重试 code 的判断顺序，以及一份可见性矩阵。"
tags: ["harness","architecture","agent"]
slug: "dsh-error-atlas"
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）。逐行读过 `packages/llm/llm/src/{error,index,retry-policy}.ts`、`packages/llm/llm-retry/src/index.ts`、`packages/core/tools/src/{pipeline,contracts,executor,batch}.ts`、`packages/core/agent-loop/src/agent.ts`、`packages/core/session/src/{types,index}.ts`。子类清单来自 `grep -rn "extends HarnessError"`，逐个回读构造器确认 code。证据等级：类定义、错误文本与决策分支为实测；重试插件的运行时行为来自阅读，没有跑一次真实 429。

一次 429 从 provider 返回，到模型下一次推理之间，会穿过六层：适配器、请求恢复、agent 循环、工具流水线、会话日志、消费方。每个地方都会改变它的形状。最后模型读到的是一句 `Error: rate limit exceeded`，日志里留下的是 `{name: 'LlmError', code: 'RATE_LIMIT'}`，UI 上显示的可能又是另一种措辞。

这份地图要回答三件事：异常在哪一层被折叠、哪些信息在折叠时留下、谁会读到留下的部分。

## 1. 基类只有两个字段

整个体系挂在一个很小的基类上：

```ts
export class HarnessError extends Error {
  /** Stable machine-routable failure class (e.g. `RATE_LIMIT`); route on this, never by parsing `message`. */
  readonly code: string

  constructor(message: string, code: string, options?: ErrorOptions) {
    super(message, options)
    this.code = code
    this.name = new.target.name
  }
}
```

`code` 与 `message` 分工明确。`code` 是机器路由用的稳定分类，`message` 给人看，允许改写、翻译、拼接上下文。注释里那句 `route on this, never by parsing message` 是整套设计的约束：任何按字符串匹配 message 来判断错误类型的代码，都会在第一次改文案时失效。

`name` 取 `new.target.name`，子类一般不用手写（`LlmError` 还显式赋了一次，结果相同）。日志里打印出来的是 `ToolNotFoundError` 而不是 `Error`，聚合时不用再维护一张 name 映射表。`cause` 走标准的 `ErrorOptions`，所以链式原因不需要自定义字段。`errorChain` 负责把这条链渲染成文本，遇到循环引用给 `<circular cause>`，遇到 `AggregateError` 展开成员，遇到 `toString` 会抛的值给 `<unrenderable value>`。这段防御不是洁癖：错误对象来自 provider SDK、用户代码、被序列化的日志，任何一处都可能抛。

基类放在 `dsh-llm` 这个叶子包里，理由写在当时的架构笔记里：它是所有包都能引用、又不会引入新依赖边的位置。工具、文件系统、沙箱、子代理都往这里挂，不需要互相依赖。

配一个 `isHarnessError` 守卫，跨包边界时先判断再取 code。`instanceof` 在同一个 realm 里可靠，跨进程或反序列化后的普通对象不能靠它，所以日志重放时读的是事件字段，不会把 JSON 还原成类实例。

## 2. 六层：形状怎么变

{{< fig "diagrams/01-error-layers.svg" "异常从 provider 到模型上下文的六层，橙色标记两处规范化边界" >}}

第一层是适配器。它把 HTTP 状态、SDK 异常、空响应翻译成 `LlmError`，`code` 用 provider 无关的词汇：`RATE_LIMIT`、`SERVER`、`TIMEOUT`、`TRANSPORT`、`EMPTY_RESPONSE`、`NO_ADAPTER`。`LlmError` 额外带一个冻结的 `failure` 对象：

```ts
this.failure = Object.freeze({
  message,
  code,
  ...options?.status === undefined ? {} : { status: options.status },
  ...options?.providerRetryAfterMs === undefined ? {} : { providerRetryAfterMs: options.providerRetryAfterMs },
  ...options?.requestId === undefined ? {} : { requestId: options.requestId },
  ...options?.offloadImages === undefined ? {} : { offloadImages: options.offloadImages },
})
```

构造器对每个字段都做校验：`status` 必须是 100 到 599 的整数，`providerRetryAfterMs` 必须是正有限数，`requestId` 必须非空。这些值会被写进日志、被重试策略读取、被 UI 展示，所以入口处就挡住脏数据。

第二层是请求恢复。`agent/request-error` 瀑布把 `failure`、`retryPolicy`、`signal` 交给订阅者，由重试插件决定是否再来一次。

第三层是 agent 循环。轮次结束时，异常被折成 `turn/end` 的 `reason.error`。

第四层是工具流水线。工具抛出的值被折成 `ToolExecutionResult`，模型看到文本，日志看到 `name` 与 `code`。

第五层是会话日志。`tool/result` 的 `error` 字段与 `turn/end` 的 `reason` 落盘，成为重放与诊断的输入。

第六层是消费方。模型只读 surface 派生出的消息，UI 与排障工具读同一份日志的不同字段。

橙色标出的两处是规范化边界：工具流水线的 `toolErrorResult`，agent 循环的 `turnEnds`。它们是异常变成结构化字段的地方，也是信息开始分叉的地方。

适配器层常见的 code 与默认重试态度：

| code | 触发 | 默认可重试 |
| --- | --- | --- |
| `RATE_LIMIT` | 限流，通常带 Retry-After | 是 |
| `SERVER` | 5xx | 是 |
| `TIMEOUT` | 请求超时 | 是 |
| `TRANSPORT` | 连接失败或重置 | 是 |
| `EMPTY_RESPONSE` | 正常结束但零内容 | 是 |
| `INVALID_CREDENTIAL` | 凭据格式错误 | 否 |
| `CONTEXT_WINDOW_EXCEEDED` | 上下文超限 | 否 |
| `QUOTA` / `ACCOUNT_QUOTA` | 额度或余额耗尽 | 否 |
| `NO_ADAPTER` | 没有注册适配器 | 否，属于配置错误 |

这张表是策略的输入，不是错误类自己的属性。同一个 `RATE_LIMIT` 在自定义策略里可以被改成不重试，`LlmError` 不会因此改变形状。

## 3. 工具边界：模型看到一句 Error 文本

{{< fig "diagrams/02-normalize-points.svg" "工具流水线与轮次收尾两处规范化，各自折叠掉不同的字段" >}}

工具抛错时，pipeline 不做类型判断，先取一句人能读的消息：

```ts
export function toolErrorMessage(error: unknown): string {
  try {
    if (error instanceof Error) return error.message
    if (typeof error === 'object' && error !== null
      && 'message' in error && typeof error.message === 'string') {
      return error.message
    }
    return String(error)
  } catch {
    return '<unprintable thrown value>'
  }
}
```

这段代码对“敌意抛值”做了防御：`instanceof`、属性访问、`String()` 任何一步都可能抛，所以整体包在 try 里，兜底返回一个固定字符串。注释把理由写得很清楚，错误规范化是最外层的安全边界，它的兜底本身必须是全函数。

然后单独取结构化信息，只在 `instanceof HarnessError` 时给：

```ts
function errorInfo(error: unknown): ToolErrorInfo | undefined {
  try {
    return error instanceof HarnessError ? { name: error.name, code: error.code } : undefined
  } catch {
    return undefined
  }
}
```

最后组装成结果：

```ts
function toolErrorResult(error: unknown): ToolExecutionResult {
  const info = errorInfo(error)
  const message = toolErrorMessage(error)
  return {
    content: [{ type: 'text', text: `Error: ${message}` }],
    isError: true,
    error: { message, ...info ? { info } : {} },
  }
}
```

注意 `info` 是嵌套的，`error.info.code` 才是那个稳定分类。写进 `tool/result` 事件时再展开一层：

```ts
message: createToolResultMessage({ callId, content, isError }),
...result.error?.info ? { error: result.error.info } : {},
```

工具结果如何组装、批次如何提交、`surfaceOp` 如何指向调用事件，在 [工具流水线](/posts/dsh-tool-pipeline.html) 里有完整拆解。

事件里的形状是 `{name, code, reason?}`。`reason` 可选，pipeline 自己构造的 `errorInfo` 只填 name 与 code，只有明确知道用户可读细节的调用方才补它。`reason` 的注释写明它只给人和持久化投影看，模型侧的 content 里不含它。这条约束让“给模型看的文本”与“给排障看的细节”可以各自演化。

`ToolOutputError` 对应另一种情况：工具跑完了，但返回的内容不满足声明的输出契约。它带 `violations`，code 是 `INVALID_TOOL_OUTPUT`。这类失败重试同一个调用通常没用，消息里要让人看出是结构不对还是内容不对。

工具层没有统一的失败 code。`UNKNOWN_TOOL`、`INVALID_ARGS`、`INVALID_TOOL_OUTPUT`、`TOOL_ACTIVATION_FAILED`、`TOOL_EXECUTOR_UNAVAILABLE` 各管一类，没被分类的抛错只留 message，`error` 字段整段缺席。全仓搜不到 `TOOL_EXECUTION_FAILED` 这个 code，写文档时容易凭印象造一个不存在的名字。

还有两个特殊 code 来自取消：`TOOL_ABORTED` 与 `TOOL_ABORTED_BEFORE_DISPATCH`，`name` 都记 `'AbortError'`。它们让“用户按了停止”与“工具真的失败”在日志里可区分。

自己搭的时候，把 `name` 与 `code` 一起存，不要只存 message。message 迟早会被改写或本地化，到时候靠它做聚合统计就全乱了。

## 4. 轮次边界：turn/end 的 reason

轮次失败时，agent 循环做一次更粗的折叠：

```ts
const turnEnds = {
  kind: 'error',
  error: error instanceof LlmError
    ? error.failure
    : {
        message: errorChain(error),
        code: error instanceof ToolActivationError ? error.code : 'UNKNOWN',
      },
} as const
```

三种情况分得很清楚。`LlmError` 保留整个 `failure`，包括 `status` 与 `requestId`；`ToolActivationError` 保留 code，message 走 `errorChain`；其余异常一律 `UNKNOWN`，只留渲染后的文本。源码注释把这条规则写成一句可执行的话：每个失败都被结构化，`LlmError` 保留事实，工具激活失败保留 code，其余压平成文本加 `UNKNOWN`。

折叠之后仍然把原异常抛出去，`turn/end` 在 `finally` 里落盘。这样调用方拿到的是原始异常，日志拿到的是结构化摘要，两边都不吃亏。写在 `finally` 还有一个效果：取消、超时、回调里再抛，都不会让轮次缺一条结束事件。

上游还有一段更细的处理。流失败或被中断时，先把半截流落成 `assistant/attempt`，再走 `agent/request-error`：

```ts
live.settle('assistant/attempt', () => this.session.append('assistant/attempt', { turn, step, stream: live.stream }).seq)
const action = await this.dispatch.waterfall('agent/request-error', { ..., failure: finish.failure, retryPolicy: preparedCall?.retryPolicy, signal }, () => Promise.resolve(undefined))
signal.throwIfAborted()
if (action?.kind !== 'retry') {
  throw new LlmError(finish.failure.message, finish.failure.code, finish.failure)
}
```

这里有一处容易看漏：重试动作不写 `turn/end`。循环回到同一步再发一次，失败的尝试留在 `assistant/attempt` 里，轮次继续。只有确定放弃时才写结束事件。这也是为什么 `assistant/attempt` 在可见性矩阵里对模型不可见：它是同一次推理的内部重试记录。

取消走单独的分支：`{kind: 'aborted', reason: cause}`。它不折叠成 `UNKNOWN`，也不带 `failure`，因为取消是调用方的决定，不该被当成 provider 或工具的失败。

`max-tokens` 是另一种收尾。它不是错误，但会让轮次记成 `max-tokens`，并且只要轮内任一步命中，整轮都记这个原因。

`turn/end` 的 reason 是一个判别联合，七种收尾各有字段：

| kind | 含义 | 附带字段 |
| --- | --- | --- |
| `completed` | 正常结束 | 无 |
| `error` | 失败 | `error: {message, code, ...}` |
| `aborted` | 用户或上层取消 | `reason` |
| `max-tokens` | 轮内命中 token 上限 | 无 |
| `interrupted` | 崩溃遗留，由 resume 补写 | 无 |
| `forked` | fork 边界处的开放轮次，由 seed 补写 | 无 |
| `blocked` | 被策略或前置条件阻止 | 视写入方而定 |

`completed`、`error`、`aborted`、`max-tokens` 出现在 agent 循环写的事件里；`interrupted` 由 resume 补写，`forked` 由 fork seed 补写。读日志时先看 `kind`，再决定去哪个字段找细节。

自己搭的时候，别把“异常对象”和“事件字段”混成一个类型。异常可以带 cause、堆栈、非 JSON 值；事件必须是可无损序列化的 JSON。两套形状分开，日志才不会被一个循环引用卡住。

## 5. 重试决策：五个判断

{{< fig "diagrams/03-retry-decision.svg" "重试策略的五个判断与每个否分支的去向" >}}

默认策略写在 `retry-policy.ts` 顶部：

```ts
const DEFAULT_MAX_RETRIES = 5
const DEFAULT_INITIAL_DELAY_MS = 500
const DEFAULT_MAX_DELAY_MS = 10_000
const DEFAULT_JITTER_RATIO = 0.1
const DEFAULT_RETRYABLE_CODES = Object.freeze([
  EMPTY_RESPONSE_CODE,
  'RATE_LIMIT',
  'SERVER',
  'TIMEOUT',
  'TRANSPORT',
])
```

插件按顺序判断。没有配置策略，交回下游；`mode: 'always'` 先跑下游再决定是否覆盖；code 不在可重试集合里，交回下游；normal 模式下重试次数到顶，交回下游。

```ts
} else if (!policy.retryableCodes.includes(failure.code)) {
  return next()
}
...
if (policy.mode === 'normal' && previousRetry >= policy.maxRetries) return next()
```

`providerRetryAfterMs` 的处理值得单独看。它有效且不超过 `maxDelayMs` 时，直接用它作为延迟；超过上限时，normal 模式放弃，`always` 模式退回本地指数退避。服务端说“等 120 秒”，而策略上限是 10 秒，继续等的收益通常不如把控制权交回去。

`always` 模式先让下游处理，失败被吞掉时只记一条 warn：

```ts
const downstream = await settleDownstream(next)
if (fusedSignal.aborted) return
if (downstream.type === 'error') {
  ctx.logger.warn(
    `llm-retry: provider "${provider}" always policy ignored a downstream recovery failure: %o`,
    downstream.error,
  )
}
if (downstream.type === 'decision' && downstream.decision?.kind === 'retry') {
  return downstream.decision
}
```

这段顺序保证取消优先：一旦 `signal` 中止，后面的决策不再改写状态。

计数按 provider 与策略键分开记，投影从 `agent.session` 里读。重试时复用同一个 `RetryId`，一次失败引发的多次尝试在日志里能串成一条线。事件有两条：`llm/retry` 记录决策与延迟，`llm/retry-started` 记录实际开始。

退避时间上乘一个抖动系数，默认比例是 0.1。多个会话同时被限流时，固定间隔会让它们在同一毫秒一起重发，抖动把这一批请求摊开。

配置在解析期就收紧。`resolveRetryPolicy` 拒绝未知字段、要求 `retryableCodes` 非空且去重、检查 `initialDelayMs` 不超过 `maxDelayMs`、`jitterRatio` 落在 0 到 1。这些错误在启动时暴露，不会等到第一次 429 才发现策略写错。

`INVALID_CREDENTIAL` 故意不在默认可重试集合里。凭据格式错误在每次尝试上都失败，重试只是把同一个错误重复五遍。`CONTEXT_WINDOW_EXCEEDED`、`QUOTA`、`ACCOUNT_QUOTA` 同理，它们需要改配置、等额度或换账号，重试解决不了。

`EMPTY_RESPONSE` 反而在集合里。provider 偶尔返回一个正常结束但零内容的 completion；空 assistant 消息会静默结束轮次，所以适配器把它分类成失败。这次尝试没产出任何持久内容，重试是安全的。

按默认值算，5 次重试加起来的等待约 15 秒。超过这个量级还没恢复，问题通常不在瞬时抖动。

重试与续跑的上层流程，包括预算怎么算、什么情况下不再重试，见 [错误重试与恢复](/posts/dsh-error-retry-recovery.html)。本篇只到“这一次请求要不要重发”为止。

自己搭的时候，把“可重试”定义成 code 集合，不要定义成状态码区间。429、500、503 之外还有超时、连接重置、空响应，按状态码分类会漏掉一半。

## 6. 模型看到什么，看不到什么

{{< fig "diagrams/04-visibility-matrix.svg" "七类错误痕迹在模型、日志与 UI 三个消费面上的可见性" >}}

模型历史由 `deriveMessages` 从 surface 生成，只遍历产消息的事件。`tool/result` 是其中之一，但进历史的只有 `content` 与 `isError`；`error` 字段留在事件上，不参与消息派生。`turn/end`、`assistant/attempt`、`llm/retry` 都不是消息事件，整段不进历史。

surface 是会话日志的折叠结果，只保留会影响模型历史的状态变更。`assistant/attempt` 记的是同一步里一次没成的流，它既不是消息也不改变消息序列，所以模型历史里没有它。`llm/retry` 属于插件事件，只进日志。`turn/end` 改变轮次状态，但不产消息。

如果把这些事件也折进历史，模型会看到“我上一次尝试失败了”，进而在下一轮里解释自己为什么失败。这类自我指涉的上下文通常只会让输出更差。

所以模型能直接看到三类失败痕迹：

1. 失败工具的 `Error: ...` 文本，作为 tool result 的内容。
2. 中断时已经流出的一半 assistant 内容，标了 `interrupted: true`。
3. resume 或 fork 补写的合成工具结果，文本里写明“只在只读或幂等时重试”。

模型看不到 `code`、`reason`、`status`、`requestId`，也看不到重试发生过几次。这个选择有一个可观察的后果：模型无法区分“第一次就成功”和“重试三次后成功”，它对延迟与稳定性的判断只能来自最终结果。有人会担心这会让模型误判，但把内部错误码塞进上下文，换来的往往是模型对着 `RATE_LIMIT` 编故事。

UI 与诊断走另一条路。code 到文案的映射放在读取端，所以改界面文案不用重写历史日志。它们读同一份日志，取 `error.name`、`error.code`、`error.reason` 与 `turn/end` 的 reason，再配上 `errorChain` 渲染的 cause 链。`reason` 在这里才有用武之地：它是原始的用户可读细节，落盘保留，但不进 content。

这张矩阵也是一份契约。新增一个事件字段，就要在三个消费面上各确认一次：模型会不会读到、日志会不会保留、UI 会不会展示。三处答案都写下来，后续改动才有对照。

有一处边界值得记住：`tool/result.error` 只允许出现在 `isError: true` 的消息上。类型注释写明了这个约束，写入路径也按此组装。想给成功结果附加诊断信息，得另找字段。

自己搭的时候，先画这张矩阵再动手。每个新字段都要回答：它是给模型、给日志、还是给 UI 的。三个答案可以同时为真，但“只给模型”通常是最危险的一档，因为模型上下文会被压缩、裁剪、跨模型重放。

## 7. 一次 429 的轨迹

把六层按顺序走一遍。场景是首次请求被限流，随后两次尝试分别遇到 5xx 与空响应，第四次成功。字段名来自类型定义，具体文案随 provider 变化。

1. 适配器拿到 429 与 `Retry-After: 2`，抛出 `LlmError`，code 为 `RATE_LIMIT`，`status` 是 429，`providerRetryAfterMs` 是 2000。
2. 流以失败结束，循环先把半截流落成 `assistant/attempt`，再触发 `agent/request-error`。
3. 重试插件读到 `failure`：code 在集合里，计数为 0，2000ms 没有超过 10s 上限，延迟直接采用 2000ms，写 `llm/retry`，重新发起同一轮的同一步。
4. 第二次返回 503，code 是 `SERVER`。计数变成 1，走本地指数退避，写第二条 `llm/retry`。
5. 第三次返回正常结束但零内容，适配器改判 `EMPTY_RESPONSE`。它在默认可重试集合里，计数变成 2。
6. 第四次成功，写 `assistant/message`，轮次以 `completed` 收尾，没有 `turn/end` 错误。
7. 如果第四次仍然失败，循环写入 `turn/end`，`reason.error` 保留最后一次的 failure：`code`、`message`，可能还有 `status` 与 `requestId`。

模型只看到最终那条 assistant 消息，前面三次尝试对它不存在。日志里有三条 `llm/retry`、三条 `assistant/attempt`、一条 `turn/end` 或一条完成的 `assistant/message`。UI 的重试计数从同一条日志的投影读出来，不需要额外埋点。

这条轨迹也解释了为什么 `assistant/attempt` 必须落盘。没有它，重试次数只能从 `llm/retry` 推断，而 `llm/retry` 是插件写的，卸载插件后这段历史就消失了。`assistant/attempt` 属于循环，永远在。

没装重试插件时，这条路短得多：第 2 步之后没有 `llm/retry`，第一次 429 就成为 `turn/end` 的失败原因，轮次结束。同一个异常，装不装插件决定了它是被消化掉还是被上报，日志形状也随之不同。

这也是排障时先看有没有 `llm/retry` 事件的原因。没有这条事件，不代表没有重试，只代表没有插件在重试。

## 8. 谁继承 HarnessError，谁不继承

继承树不是按包整齐划分的，按用途看更清楚：

| 归属 | 子类 | 代表性 code |
| --- | --- | --- |
| LLM | `LlmError` | `RATE_LIMIT`、`NO_ADAPTER`、`EMPTY_RESPONSE` |
| 工具 | `ToolArgsError`、`ToolNotFoundError`、`ToolOutputError` | `INVALID_ARGS`、`UNKNOWN_TOOL`、`INVALID_TOOL_OUTPUT` |
| 工具执行 | `ToolActivationError`、`ToolExecutorUnavailableError` | `TOOL_ACTIVATION_FAILED`、`TOOL_EXECUTOR_UNAVAILABLE` |
| schema | `JsonSchemaError` | `UNSUPPORTED_SCHEMA` |
| 文件 | `FsError` | 15 个 `FS_*`，从 `FS_NOT_FOUND` 到 `FS_PATCH_MISMATCH` |
| 沙箱 | `SandboxInvalidRootError`、`SandboxUnsupportedPolicyError` | `SANDBOX_INVALID_ROOT`、`SANDBOX_UNSUPPORTED_POLICY` |
| 子代理 | `SubagentError` | `DUPLICATE_CHILD`、`UNSUPPORTED_CAPABILITY`、`UNAUTHORIZED` |
| 编排 | `GoalError`、`WorkflowError`、`TeamError` | 各自枚举，`WorkflowError` 另带 `fatal` |
| PTC | `CodeRunFailedError`、`CodeResetFailedError` | 代码执行与重置失败 |

有些子类只是标记，不增加字段：`LspError`、`WebError`、`PatchToolError`、`SearchError` 都是空壳，构造时传入 code。它们存在的意义是让 `instanceof` 能区分领域，同时共享基类的 `code` 与 `name` 行为。

`JsonSchemaError` 与 `ToolArgsError` 都带 `violations: string[]`，分工不同。前者管工具声明的 schema 本身用了不支持的构造，发生在准备阶段；后者管某次调用的参数不满足 schema，发生在执行之前。两者都把违规项列出来，调用方不用解析 message 才知道哪一条不合法。

`FsError` 的 code 是一个 15 项的联合，从 `FS_NOT_FOUND`、`FS_NOT_DIRECTORY` 到 `FS_STALE_VERSION`、`FS_AMBIGUOUS_EDIT`、`FS_PATCH_MISMATCH`。它覆盖的不只是“文件不存在”，还有并发写冲突与补丁上下文不匹配。工具层据此决定是让模型重读文件，还是直接放弃这次编辑。

沙箱错误的 message 里带具体路径。`SandboxInvalidRootError` 拼的是 `Invalid additional writable directory "<path>": <detail>`，code 是 `SANDBOX_INVALID_ROOT`。把路径放进 message 而不是单独字段，是因为它只给人看，重试决策只看 code。

`SubagentError` 的 code 围着生命周期转：`DUPLICATE_CHILD` 是子代理名冲突，`UNSUPPORTED_CAPABILITY` 是请求了宿主没开的能力，`UNAUTHORIZED` 是调用方越权。这三个都在创建或调用子代理时抛出，父代理据此决定是改名重试还是直接放弃。

`GoalError` 与 `TeamError` 的 code 由各自的运行时枚举，形态与 `SubagentError` 一致：消息给人，code 给调度逻辑。


`WorkflowError` 多一个 `fatal` 布尔，默认 true，配 `isFatalWorkflowError` 判断。工作流里有的失败可以继续跑后续步骤，有的必须整体中止，这个字段就是给调用方分流用的。

反例同样重要。`ToolBatchExecutionError` 继承的是 `Error`，不是 `HarnessError`，因为它携带的是 `partial: ToolBatchResult`，一个批次的部分结果，而不是一个可路由的失败分类。agent 循环捕获它之后注入 `additionalContexts`，再把 `cause` 抛出去，包装层不进入日志。

工具层还有一类“没有 code”的失败：第三方工具直接 `throw new Error('...')`。pipeline 会兜住，模型看到文本，事件里没有 `error` 字段。这类失败在聚合统计里只能按 message 归类，所以自己写工具时值得多包一层 `HarnessError`。

## 9. 设计取舍

基类放在 `dsh-llm`，换来所有包都能继承而不引入新的依赖边。代价是 `dsh-llm` 成了一个事实上的公共基础包，任何改动都要按公共 API 对待。

`code` 与 `message` 分离，换来路由稳定、文案可改。代价是每个新失败都要想一个名字，而且名字一旦发布就不能随便改。

工具边界把异常折成模型可读文本，换来模型能理解“这个工具失败了”。代价是模型永远看不到 `code`，也就无法据此改变策略。

轮次边界保留 `failure` 整个对象，换来日志里有 `status` 与 `requestId` 可查。代价是 `turn/end` 的形状依赖 `LlmFailure`，加字段要同步投影与校验。

重试放在插件里，换来核心循环不认识重试这件事。代价是重试行为依赖装了哪个插件，默认构建与自定义构建的恢复能力可能不同。

`ToolBatchExecutionError` 不继承基类，换来它能携带任意形状的 partial 结果。代价是它不能被统一的 `errorInfo` 识别，必须在 agent 层单独拆包。

`reason` 留在事件里、不进 content，换来模型上下文干净。代价是同一个失败有两套文本，改写时容易不同步；这也要求写入方克制，只放确实需要人读的细节。

拒绝把错误码写进模型可见文本，换来跨模型重放时行为一致。代价是模型无法据此选择替代方案，只能按文本里的建议行动。

把 `EMPTY_RESPONSE` 当成失败，换来轮次不会静默结束。代价是个别 provider 的合法空回复会被重试一次，多花一次调用。

## 10. 踩坑点

1. 按 `error.message` 判断错误类型。provider 改一句文案，分支就失效；应该读 `code`。
2. 以为 `toolErrorResult` 会把 code 放在顶层。它在 `error.info.code`，写进事件时才展开成 `error.code`。
3. 给成功的工具结果塞 `error` 字段。类型上不允许，投影也不会保留。
4. 在 `turn/end` 里期待拿到原始异常。落盘的是 `{message, code}` 或 `failure`，堆栈与 cause 对象不在里面。
5. 把重试当成新轮次。重试发生在同一轮内，`turn/start` 不会重写，失败尝试只留 `assistant/attempt`。
6. 用 `mode: 'always'` 兜住所有失败。凭据错误、上下文超限、额度耗尽都会被反复重试，直到次数用完。
7. 以为 `providerRetryAfterMs` 一定被采纳。超过 `maxDelayMs` 时 normal 模式直接放弃。
8. 在子代理里抛普通 `Error`。父级只能看到 message，聚合时无法按 code 区分失败类型。

## 11. 自己搭的时候

1. 先定一个基类，只放 `code` 与 `cause`，不要放 `retryable`、`userMessage` 这类会随场景变化的字段。可重试性属于策略，不属于错误。
2. 在“异常变成持久数据”的那两个边界写显式映射函数，并在函数上标注它丢弃了什么。丢弃是必然的，写清楚才不会有人以为日志里有堆栈。
3. 给模型看的文本单独生成，不要复用 `error.message`。模型文本要稳定、可读、不含内部标识；`message` 要给人排障，允许带细节。
4. 重试决策只读 code 集合与策略配置，不读 message、不读状态码区间。再加一条按 provider 的计数上限，避免一个坏 provider 拖住整个会话。
5. 如果某个错误必须中止整个 run，给它一个显式标记，像 `WorkflowError.fatal` 那样。靠 `instanceof` 猜中止语义，迟早会猜错。
6. 给取消单独一条路径。取消不是失败，别让它和真实错误共用 code，否则统计里永远分不清“用户停了”和“服务挂了”。
7. 给 UI 留一张 code 到文案的映射表，别让界面直接渲染 `error.message`。同一条日志会被不同版本的 UI 读到，message 里的细节可能已经过时，code 不会。

## 12. 源码索引

- `packages/llm/llm/src/error.ts:16`：`HarnessError` 基类，`code` 与 `name` 的来源。
- `packages/llm/llm/src/error.ts:25`：`CONTEXT_WINDOW_EXCEEDED` 常量。
- `packages/llm/llm/src/error.ts:42`：`EMPTY_RESPONSE` 与它为何被视为可重试失败。
- `packages/llm/llm/src/error.ts:117`：`errorChain` 渲染 cause 链与 `AggregateError`。
- `packages/llm/llm/src/index.ts:96`：`LlmError` 与冻结的 `failure`。
- `packages/llm/llm/src/index.ts:1022`：`NO_ADAPTER` 的抛出点。
- `packages/llm/llm/src/retry-policy.ts:18`：默认可重试 code 集合。
- `packages/llm/llm/src/retry-policy.ts:149`：`resolveRetryPolicy` 的配置校验。
- `packages/llm/llm-retry/src/index.ts:212`：code 不在集合内时交回下游。
- `packages/llm/llm-retry/src/index.ts:220`：normal 模式的重试次数上限。
- `packages/llm/llm-retry/src/index.ts:184`：写 `llm/retry` 事件。
- `packages/core/tools/src/pipeline.ts:686`：`errorInfo` 只在 `HarnessError` 时给结构化信息。
- `packages/core/tools/src/pipeline.ts:695`：`toolErrorResult` 的组装。
- `packages/core/tools/src/contracts.ts:270`：`ToolErrorInfo` 与 `reason` 的用途注释。
- `packages/core/tools/src/batch.ts:250`：`tool/result` 提交时展开 `info`。
- `packages/core/session/src/types.ts:383`：`tool/result` 事件的 `error` 字段约束。
- `packages/core/agent-loop/src/agent.ts:412`：`turnEnds` 的三路折叠。
- `packages/core/agent-loop/src/agent.ts:548`：`agent/request-error` 瀑布与放弃时的重抛。
- `packages/core/tools/src/executor.ts:89`：`ToolBatchExecutionError` 继承 `Error` 的反例。
- `packages/core/session/src/index.ts:999`：`deriveMessages` 只遍历 surface 消息事件。

## 13. 最后

看一套 agent 的错误处理是否可用，不必读完全部代码。抽查四处就够：异常进入日志时保留了哪些字段；模型看到的文本是否与内部 code 解耦；重试决策是否只依赖稳定分类；中止与放弃是否有独立的收尾事件。

四处都清楚，排障路径就是确定的：从 `turn/end` 的 code 找到层，从 cause 链找到 provider 或工具，从 `tool/result.error` 找到那次调用的参数。任意一处含糊，能用的就只剩 message，而 message 是最容易被改写的一层。
