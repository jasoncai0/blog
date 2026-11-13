---
title: "模型层：适配器、路由与那条不能重放的流"
date: "2026-11-13T10:00:00+08:00"
summary: "ctx.llm 的三个角色、prepareCall 与 stream 的分工、provider/model/effort 三元组为何必须在 step 准备前定死，以及流式帧、usage 计量与配额错误码各自落在哪一层。"
description: "DSH 模型层解剖：适配器 seam、一次性调用、流式帧与 token 计量。"
tags: ["harness", "architecture", "agent"]
slug: "dsh-model-layer"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准。
> 第二季第 2 篇。上一篇：[《一次请求的完整旅程》](/posts/dsh-request-journey-end-to-end.html)；本季导览：[《第二季：把主干两侧补齐》](/posts/dsh-season-two.html)。

改模型这件事，看起来只是改一个字符串。你在设置里把 `deepseek-chat` 换成 `deepseek-reasoner`，下一轮却还是老模型在回你；或者反方向，你把适配器换成一个本地网关，重启之后模型名对了，历史里的 `replayState` 却被悄悄丢掉，prefix cache 命中率掉了一半。

这两种现象都不是 bug，它们是同一个约束的两面：模型路由属于 step 的准备阶段，而一次 provider 调用在开始之前就已经把路由、重试策略、上下文容量和适配器默认值钉死了。DSH 的模型层围绕这条约束长大，`ctx.llm` 只是它露在外面的那部分。

这篇按调用顺序拆开它：三个角色怎么分工，`prepareCall` 钉死了什么，流式帧长什么样、为什么不落库，usage 的字段为什么必须互不相交，以及配额错误为什么要分成三个码。

## 0. 结论

- `ctx.llm` 是一个 capability seam，三角色齐全：`LlmRuntime`（服务定义）、`LlmAdapter` 子类（提供方）、agent loop 与工具插件（消费方）。提供方只被要求实现 `stream()` 一个方法。
- `prepareCall()` 负责在发请求之前把所有可变量钉死：适配器注册、重试策略、模型元数据、`maxTokens` 与 reasoning effort 的默认值。它返回的 `PreparedLlmCall` 只能派发一次，配置对不上就抛 `INVALID_PREPARED_CALL`。
- `stream()` 负责执行。一次适配器调用等于一次 provider 尝试，适配器内部不重试；重试属于 agent 层，靠新开一个带编号的 turn 来记录。
- provider、model、reasoning effort 这三元组在 `preStep()` 里就固化成 `selection`，随后工具准入、系统提示词、请求头、适配器默认值全部从它派生。`agent/request` 想在之后改路由会被直接抛错。
- 流式帧 `start`/`chunk`/`end` 是进程内的瞬时通知。落库的是折叠后的紧凑记录，附在一条 `assistant/message` 或 `assistant/attempt` 事件里，一个 step 只写一次。
- `TokenUsage` 的四个计数字段互不相交，billed input 是三者之和；DeepSeek 的 `prompt_tokens` 把 cache 命中折进去了，适配器必须减出来。
- 配额错误分成 `QUOTA`、`ACCOUNT_QUOTA`、`ACCOUNT_TOKEN_INVALID` 三个码，因为恢复动作不同：等、去充值、重新登录。

## 1. 三个角色，依赖只有一个方向

seam 的定义在 `packages/llm/llm/src/index.ts`。服务定义是一个 `Service` 子类 `LlmRuntime`，它暴露四类东西：注册（`registerAdapter`、`registerConfigurableProviders`、`registerModelDiscovery`）、查询（`listProviders`、`listModels`、`resolveModelInfo`、`imageRequestPricing`）、准备（`resolveCallConfig`、`prepareCall`）和执行（`stream`）。事件只有两个，`llm/adapters-updated` 与 `llm/models-updated`，都在 `types.ts:13-32` 声明，payload 一个是空、一个是 provider 名。两者都是 emit 模式，观察者无法否决一次注册。

提供方继承 `LlmAdapter`（`index.ts:290`）。抽象成员只有一个：

```ts
abstract stream(options: GenerateOptions): AsyncIterable<StreamChunk>
```

其余方法都有默认实现：`providerInfo()` 返回空对象，`providerRetryPolicy()` 返回 undefined，`listModels()` 返回空数组，`resolveModel()` 把传入的 id 原样返回，`prepareCall()` 返回 `{ model: this.resolveModel(...), stream: options => this.stream(options) }`。这套默认值让最小可用适配器只有二十行，也把可选能力做成了显式覆盖：想声明推理强度、上下文容量、图片计价、工具 schema 的更新方式，才去实现对应方法。

消费方分两类。agent loop 是主消费方，走 `prepareCall()` 再 `stream()`；工具与外围插件走只读查询，比如 `dsh-spill-policy` 用 `imageRequestPricing()` 给图片算内联预算，面板用 `listModels()` 画模型选择器。`resolveCallConfig()` 与 `prepareCall()` 的差别在这里：前者只校验并填默认值，不绑定任何后续派发，适合预览；后者返回的句柄才带一次性保证。

依赖方向是单向的。`dsh-llm` 不 import 文件系统包，需要宿主路径时通过 `ctx.get('fs')` 结构化取用（`index.ts:1043-1061`），拿不到就退化成不带路径的句柄文本。它也不需要知道 session 长什么样，`sessionId` 只是 `GenerateOptions` 里一个可选字符串，用于归因。

{{< fig "diagrams/01-seam-roles.svg" "seam 的三个角色与依赖方向。定义层只依赖 Cordis 与自身类型；提供方实现同一抽象类；消费方通过 ctx.llm 访问，谁都拿不到别人的内部状态。" >}}

不这样会出什么事：适配器一旦能直接读 agent 或 session，它就会开始依赖循环状态，测试要先造一个会话才能验证一次 HTTP 重试。注册表也会失去唯一性保证，两条路由同名时无法在加载期失败。

### 自己搭的时候

给适配器只留一个必须实现的方法。DSH 的 `LlmAdapter` 有九个成员，只有 `stream()` 是抽象的，其余（模型目录、重试策略、图片计价、prepareCall）都有默认实现。这条设计让"接一家新 provider"的最小工作量是一段 SSE 解析，而不是实现一整套元数据接口。代价是默认实现必须足够保守：默认 `prepareCall` 只回一个模型对象，默认 `listModels` 回空数组，谁想要更准确的信息谁自己覆盖。

另一条：让服务定义不依赖任何提供方。`dsh-llm` 不 import `dsh-fs`、不 import `dsh-agent`，需要文件路径时用 `ctx.get('fs')` 结构化取。这样适配器可以单独装、单独测，也不会因为某个提供方的循环依赖装不上。

## 2. prepareCall：把可变量钉在派发之前

`PreparedLlmCall` 的字段（`index.ts:879-903`）就是一份"钉死清单"：

```ts
interface PreparedLlmCall {
  /** Detached deep-frozen config with adapter-owned defaults materialized. */
  readonly config: LlmCallConfig
  /** Immutable retry policy captured with the adapter registration. */
  readonly retryPolicy: ResolvedRetryPolicy
  readonly context?: LlmModelContext
  readonly inputModalities?: readonly string[]
  readonly systemPromptUpdate?: SystemPromptUpdate
  readonly toolUpdate?: ToolUpdate
  /** Caller-omitted fields the adapter filled in. */
  readonly adapterDefaults?: LlmCallConfigAdapterDefaults
  stream(options: GenerateOptions): AsyncIterable<StreamChunk>
}
```

`prepareCall(config, signal, configure)` 的函数体（`index.ts:948-1018`）按固定顺序做七件事：

1. `signal.throwIfAborted()`，准备阶段也可取消。
2. `this.registration(provider)`，查不到就抛 `NO_ADAPTER`，错误消息是 `no adapter registered for provider "..."`。注意这一步发生在解析模型之前。
3. `await registration.adapter.prepareCall(provider, model, signal)`，让适配器补模型元数据。
4. `configure?.(controls, modelInfo)`，同步纯函数，给调用方一次改控制参数的机会。
5. `resolveCallWithInfo()`，填 `maxTokens`、校验并落地 reasoning effort。模型没有推理强度信息却传了 effort，这里抛 `UNSUPPORTED_REASONING_EFFORT`。
6. `deepFreeze(structuredClone(...))` 复制 config 与 context。冻结的是副本，调用方手里的对象不受影响。
7. 计算 `adapterDefaults`：调用方省略、解析时被填上的字段记成 `true`。`{ reasoningEffort: true }` 表示"这次用的是适配器默认值"，不是"用户选了某个值"。

`configure` 的限制写在签名里：它拿到的是 `Readonly<LlmCallControls>` 和 `Readonly<LlmResolvedModelInfo>`，返回新的 controls。provider 和 model 不在 controls 里，所以它改不了路由。想换模型只能在准备之前换，这条限制和下一节的路由规则是同一件事的两端。

派发是一次性的。返回对象里有个闭包变量 `dispatched`，第二次调用直接抛：

```ts
if (dispatched) throw new LlmError('a prepared LLM call can only be dispatched once', 'INVALID_PREPARED_CALL')
if (!callConfigEquals(options, resolvedConfig)) {
  throw new LlmError('prepared LLM call config changed before adapter dispatch', 'INVALID_PREPARED_CALL')
}
```

第二条检查值得多说一句。它比较的是"实际发出去的 options"和"准备时冻结的 config"。agent loop 会先写请求头（`request/header`），再拿同一个 config 组请求；如果中间有插件改了 `maxTokens` 或 `temperature`，这条检查会让请求发不出去。日志里记录的参数和实际发出去的参数不可能分叉。

`retryPolicy` 也在准备期捕获。`dsh-llm-retry` 的重试决策读的是这个值，不是注册表当前值。运行中改配置只影响后续请求，正在跑的这一次仍按旧策略退避，这一点和 `llm-streaming.zh.md` 里"一个适配器调用等于一次 provider 尝试"是配套的。

### 自己搭的时候

准备阶段只做纯查询和校验，别在里面发网络请求。一个本地代理适配器最容易犯的错，是在 `prepareCall()` 里探活、拉模型列表、取 token。这些动作一旦失败，错误会出现在"准备"而不是"执行"的调用栈上，agent loop 的重试逻辑看不到它，用户看到的是一个没头没尾的 `NO_ADAPTER` 或超时。探活放到 `listModels()` 或插件加载阶段，准备阶段只读已缓存的元数据。

另一条：把 `adapterDefaults` 当成协议的一部分来实现，别当调试信息。它进了请求头，压缩、重建、跨适配器迁移都会读它。少填一个 `true`，会话恢复后就会把"默认 effort"固化成"用户明确选的 effort"，模型的思考长度从此变了。

## 3. stream：一次调用就是一次 provider 尝试

`stream()` 的入口在 `index.ts:1170`，两行：

```ts
stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
  return this.streamWithRegistration(options)
}
```

它产出的 `StreamChunk`（`types.ts:488-508`）只有七种，字段是刻意做小：

| 成员 | 关键字段 | 消费方拿它做什么 |
| --- | --- | --- |
| `block-start` | `index`、`blockType` | 开一个块，决定后续 delta 归到哪里 |
| `text-delta` | `index`、`text` | 上屏、拼正文 |
| `reasoning-delta` | `index`、`text` | 上屏、与正文分开存 |
| `tool-call-delta` | `index`、`argumentsDelta` | 拼工具参数，仍是原始 JSON 串 |
| `block-end` | `index`、`block` | 拿到装配好的块，校验完整性 |
| `usage` | `usage` | 记账、压力投影 |
| `finish` | `reason`、可选 `replayState` | 决定这一轮怎么收尾 |

`index` 是块序号，不是到达顺序，所以文本与工具调用可以交错。`finish` 是终止信号，之后不许再有帧，`usage` 必须出现在它之前。`block-end` 里的 `block` 是装配结果，delta 只是过程，两者不一致时以 `block` 为准。

`streamWithRegistration()` 把执行本身包进 `llm/stream` waterfall（`index.ts:1174-1184`）。waterfall 绑定在 `LlmRuntime` 上，适配器查找发生在终端 continuation 里，所以监听器可以短路整次调用，也可以改写成另一个请求。这一层是给测试回放和部署级路由用的，`packages/test-support/llm-replay/src/index.ts:1090` 就是靠它无 key 重放录制流。

终端 continuation 是 `adapterStream()`（`index.ts:1063-1157`），顺序如下。

先解析注册与适配器。带 `prepared` 的分支不重复解析模型，但会再查一次 `callConfigEquals`，防止有人绕过准备直接拼一个 options 进来。

然后做三个投影。文件块投影成句柄文本（`projectFilesToText`），图片在只声明文本模态的模型上投影成占位文本（`projectImagesForTextModel`），工具增删按 `modelInfo.toolUpdate` 投影（`projectToolUpdates`）。三个投影都作用在请求副本上，`GenerateOptions` 本身不可变。

接着 `dispatch(this.forAdapter(projectedOptions, adapter))`。`forAdapter()` 处理 replay 状态：如果历史消息里的 `replayState` 属于另一个适配器，就把这个字段剥掉，只保留 provider/model。回放状态是适配器私有的，换一家就不能用。

最后迭代。这里有一段容易被忽略的 try 结构：

```ts
const item = await iterator.next()   // 在 try 里
// ... catch 里 yield 一个 terminal failure chunk 然后 return
yield item.value                     // 在 try 外面
```

注释（`index.ts:1147-1148`）写得很直白：adapter 拥有的 try 必须在 yield 之前结束，否则消费方或中间件抛出的异常会被 yield 掉回这个 generator，被误判成适配器失败。一个 UI 渲染错误不应该变成 `SERVER` 错误，更不应该触发重试。

失败归一也在这一层。适配器可以抛，也可以在自己的流里发一个 `finish { kind: 'error' }`，两条路都会走到 `adapterFailureChunk()`（`index.ts:1187-1196`）：

```ts
const failure = normalizeLlmFailure(error)
return { type: 'finish', reason: signal?.aborted || failure.code === 'ABORTED'
  ? { kind: 'aborted', failure }
  : { kind: 'error', failure } }
```

结果是消费方永远只需要处理一种流：中间可能夹一个 terminal failure 帧，但不会在迭代中途炸掉。`signal` 已中止时统一报 `aborted`，即使底层错误是 `ECONNRESET`，因为原因已经由取消方确定，不该被传输层覆盖。

{{< fig "diagrams/02-prepare-stream-sequence.svg" "一次请求的准备与执行时序。上半段是纯本地计算，不产生网络 I/O；下半段的箭头才出进程。两次校验分别挡在派发前后。" >}}

流式传输还有一个看门狗，位置在适配器的传输层而不是 seam 里。两个远端适配器都暴露有限的 `streamIdleTimeoutMs`，默认五分钟；计时只在 `next()` 未决时走，拿到数据就重置。到期抛 `TIMEOUT`，而调用方在此之前取消仍然记 `ABORTED`。这条区分让"网络卡住"和"用户按了停止"在日志里不会混成一个。

### 自己搭的时候

给自己的适配器写 `stream()` 时，先写终止路径。最容易漏的三个：provider 在 `message_stop` 之前断流（要抛 `STREAM_CLOSED` 之类，不能静默结束）、provider 返回零个内容块却给了 `stop`（`EMPTY_RESPONSE`，空消息会让一个 turn 无声结束）、usage 出现在 finish 之后（契约要求 usage 在 finish 之前，晚到的 usage 只能丢掉）。这三条都不会在正常测试里出现，只会在真实网络里出现。

## 4. 路由：三元组在 step 之前定死

模型选择在 DSH 里是三个值：`provider`、`model`、`reasoningEffort`。`reasoningEffort` 可省略，省略的含义是"用适配器默认值"，和"显式选了默认值"是两回事。

选择有三个来源，优先级从高到低：本会话记录的选择、上一次请求头里的选择、`AgentOptions` 里的初始值。`AgentModel.current()`（`packages/core/agent/src/model-selection.ts:105-110`）就按这个顺序读，返回的是副本。`select()`（同文件 112-115）比较后追加一条 `model/selection` 事件，值相同就什么都不写。

`dsh-agent-default-model` 提供的是"新会话的默认值"。它的 `Config` 三个字段都是 `Volatile`，意味着可以运行时改；`currentSelection()` 返回一份脱离的快照；`saveSelection()` 走 `configEditor` 写回组合文件，没有编辑器就直接返回（部署把选择写在 `cordis.yml` 里，运行时不该覆盖它）。多次保存串在一条 promise 链上，按提交顺序落盘，前一次失败不会卡住后一次。

选择是投影出来的，不是字段。`modelSelection` 投影维护 `{ lastUsed, pending }` 两个槽（`stateVersion: 3`）：`model/selection` 事件写 `pending`，`request/header` 事件把实际发出去的选择记进 `lastUsed` 并清掉相等的 `pending`。这个设计解决一个具体问题：改了选择但这一步被拒绝或取消，选择仍然有效；改了选择且请求发出去了，投影就以请求头为准，不信内存。

投影里还有一处细节值得抄。`selectionOfHeader()` 在读请求头时，如果 `adapterDefaults.reasoningEffort === true`，就把 effort 从选择里省掉。省掉之后下次解析仍会走适配器默认值。不省的话，一次"默认 effort"的请求会把默认值固化成显式值，模型升级换了默认思考长度，这个会话还停在旧的。

### 为什么必须在 step 准备前定

`preStep()`（`packages/core/agent-loop/src/agent.ts:283-317`）第一件事就是取选择：

```ts
const selection = this.model.current()
if (selection === undefined) {
  throw new Error(`agent "${this.id}" has no provider/model: set AgentOptions.provider and AgentOptions.model or record a model selection`)
}
const toolSet = await this.loopCtx.tools.resolve({ agent: this, modelSelection: selection, signal })
const assembly = await this.loopCtx.systemPrompt.assemble({ ...assembleContextFor(this, signal), modelSelection: selection, toolSet })
```

顺序是有意的。工具准入依赖模型（不同模型支持的工具集不同，`deferLoading` 的可用性也不同），系统提示词依赖模型（`index.ts:336-337` 注册了 `provider` / `model` 两个提示词变量），两者都在 `agent/pre-step` waterfall 之前完成。`agent/pre-step` 的 payload 里带着 `modelSelection`，监听器看到的是同一个值。

再往后，`prepareRequest()`（`agent.ts:613-653`）拿这个 `selection` 组种子配置，跑 `agent/request` waterfall，然后做两次严格比较：

```ts
if (proposedConfig.provider !== selection.provider || proposedConfig.model !== selection.model) {
  throw new Error(`agent "${this.id}": agent/request proposed "${proposedConfig.provider}/${proposedConfig.model}" after step preparation fixed "${selection.provider}/${selection.model}"; use agent.model.select() before step preparation`)
}
if (proposedConfig.reasoningEffort !== selection.reasoningEffort) {
  throw new Error(`agent "${this.id}": agent/request proposed reasoning effort "..." after step preparation fixed "..."; use agent.model.select() before step preparation`)
}
```

`agent/request` 这个 waterfall 已经标记为 deprecated，理由就在这两行：它能改采样参数，却不能改路由，因为路由的上游已经用过了。留着一个"看起来能改、实际会被拒"的入口，比没有这个入口更容易误用。

路由定死之后，四样东西同时定死：请求头里的 `config`、适配器注册（决定重试策略与 `contextWindow`）、`adapterDefaults`、以及这次请求的 `request/context`（记录 `contextWindow`）。压缩和计量都读最后一项。`contextPressure` 投影的 `contextWindow` 来自 `request/context` 事件，压力值锚在 `assistant/message` 的 usage 上（`usage-projection.ts:169-199`），`projectedTokens = pressureTokens + surfaceTokens - sampledSurfaceTokens`。如果允许在 step 中途换模型，压缩可能用 A 的窗口量完，请求发给 B，采样点也错位。

{{< fig "diagrams/04-usage-and-errors.svg" "usage 的四个互不相交计数如何汇总成压力，以及 provider 错误到恢复动作的分类。左半边是计量，右半边是配额与账号错误的三个出口。" >}}

### 自己搭的时候

如果你在做一个多模型客户端，把"选择"做成一条可重放的记录，别做成内存里的 current 变量。DSH 的做法是 `model/selection` 事件加投影，好处是恢复会话时不必猜当前模型，坏处是每次读都要过一次投影缓存。切模型时还要写一条模型可见的提示消息（`installModelSelection()` 在 `agent/pre-step` 里做这件事：`[model changed: assistant turns above this point were generated by X; the session continues with Y]`），否则模型会把上半段对话当成自己写的，接着模仿一个它没写过的风格。

## 5. 帧：形状、顺序与落库边界

进程内的流式通知由 `AssistantStreamAttempt` 发（`packages/core/agent-loop/src/assistant-stream.ts`），三种帧：

```ts
{ type: 'start', attemptId, revision, turn, step }
{ type: 'chunk', attemptId, revision, index, time, chunk }
{ type: 'end',   attemptId, revision, index,
  outcome: { kind: 'committed', eventType, seq } | { kind: 'abandoned' } }
```

`attemptId` 是 `${sessionId}:${attempt}`，`revision` 单调递增用于丢弃乱序更新，`index` 是 chunk 序号。`start` 在第一个 chunk 之前发，`end` 在持久事件提交之后发，`outcome` 里带提交后的 `seq`。

`end` 的顺序是刻意反过来的：先 `append()` 落库，成功后再发帧。`settle()`（同文件 78-97）先调用传入的同步 append，拿到 `seq`，再 `emit`。append 抛异常时走 `abandon()`，发一个 `outcome: { kind: 'abandoned' }` 的 end，然后把异常继续抛出去。UI 因此不会看到"已完成"但日志里没有对应事件的帧。

落库的那一份不是帧，是折叠记录。`AssistantStreamAccumulator` 把每个 chunk 配一个安全整数时间戳，然后把同一块内连续出现的 text、reasoning、tool-argument delta 合成一条记录：`time0` 是首 delta 时间，`times` 是后续每个 delta 的相对时间，`members` 是原始 delta 数组。其他 chunk 保持原样。`assistant.message` 事件里带两个字段：

```ts
this.session.append('assistant/message', {
  turn, step, message,
  usage: live.usage,
  stream: live.stream,
}, { surfaceOp: 'append' })
```

`usage` 是最后一次上报的 `TokenUsage`，`stream` 是紧凑记录数组。没有可见消息的尝试写 `assistant/attempt`，字段相同，只是没有 message。

chunk 不逐条落库，原因有三个，都在实现里能看到。数量：一个 4K 输出的回答轻松几百个 delta，每步写几百条事件，日志体积和投影成本都按 delta 数增长。原子性：一次 step 的模型输出要么整条进 surface，要么整条不进（中断时走 `interruptedBlocks()`，带 `interrupted: true` 落一条截断消息），逐条写会让"半条消息"成为合法状态。读取：记录级读取器（`assistantStreamFirstTokenTime`、`joinAssistantStreamText`、`assembleAssistantStream`）一次扫描就够，按 delta 存则每次读都要重新折叠。

内存里的帧和日志里的记录是两套表示，中间靠 `expandAssistantStream()` 做严格校验：键、数量、索引、时间戳、工具调用身份、JSON 无损。校验失败会抛出，而不是"尽力恢复"。理由很实际：记录被破坏说明写入路径有 bug，静默修复会让 bug 藏起来。

一次尝试的收尾有三种，落在日志上是三个不同的形状：

| 收尾 | 写什么 | 谁看得到 |
| --- | --- | --- |
| 有可见消息 | `assistant/message`，带 `message`、`usage`、`stream` | 模型下一轮、回放、UI |
| 没有可见消息 | `assistant/attempt`，同样带 `usage` 与 `stream` | 重试历史、诊断 |
| 中途中断 | 一条 `interrupted: true` 的消息，块是截断的 | 模型下一轮能看到"上次被切断" |

第二种容易被忽略。一个只产出了推理、没产出正文的尝试不写 surface 消息，但它的 usage 与 stream 仍然要落库，否则重试一轮之后，账单与延迟统计会少掉一次真实请求。

{{< fig "diagrams/03-frames-persistence.svg" "帧序列与两条落库路径。左边是瞬时帧，右边是两种持久事件；虚线标出 settle 的提交点，提交之后才允许发 end 帧。" >}}

### 自己搭的时候

如果你也要做"先上屏、后落库"，把帧和记录分成两个类型，别用同一个对象兼职。兼职的代价是前端要处理只有内存里才有的字段（`revision`、`index`），而回放路径拿不到它们，于是每个渲染组件都要写两套分支。DSH 的两套类型之间有校验器，抄这个结构比抄字段更值。

## 6. usage：四个计数与一条压力曲线

`TokenUsage`（`types.ts:182-196`）只有五个字段，语义比字段数量重要：

```ts
interface TokenUsage {
  inputTokens: number
  outputTokens: number
  totalTokens?: number
  cacheReadTokens?: number
  cacheWriteTokens?: number
  reasoningTokens?: number
}
```

文档在类型上方写得很硬：计数互不相交，`inputTokens` 只算未命中的输入，缓存命中单独报在 `cacheReadTokens` / `cacheWriteTokens`，计费输入是三者之和。DeepSeek 的 `prompt_tokens` 把命中折进了总数，适配器必须减出来。`totalTokens` 只在能给出准确全量时填，拿不到就省略，别用 `input + output` 猜一个。

DeepSeek 适配器的映射在 `translate.ts:34-41`：`input_tokens` → `inputTokens`，`output_tokens` → `outputTokens`，`cache_read_input_tokens` → `cacheReadTokens`，`cache_creation_input_tokens` → `cacheWriteTokens`。总和在 `message_stop` 时算（`translate.ts:159`）：

```ts
usage.totalTokens = inputTokens + outputTokens + (cacheReadTokens ?? 0) + (cacheWriteTokens ?? 0)
```

有了不相交的计数，计量层才能做三件不同的事。

计费：`usageTokens()`（`token-meter/src/index.ts:59-60`）把四项相加。这个值决定 `measure()` 用哪个基线。

压力：`contextPressure()` 返回 `{ pressureTokens, contextWindow }`，只取输入侧。输出侧不占下一轮的窗口，混进去会让压缩提前触发。

增量：`measure()` 返回 `baseline`、`surfaceDeltaTokens` 与 `totalTokens`。基线三选一：`usage`（有可信 usage 且不小于估算锚点）、`estimated`（只有启发式估算）、`none`。`surfaceDeltaTokens` 是带符号的，压缩替换掉一大段内容后它会是负数，`totalTokens` 用 `Math.max(0, baseline + delta)` 兜底。带符号是关键：压缩之后压力必须能下降，否则触发条件永远为真。

图片走另一条计价路径。适配器实现 `imageRequestPricing(provider, model)` 返回一个可调用对象，`priceImages(images)` 给出每张图的 `{ visualTokens, text }`。offloaded 的图片按占位文本计价，因为发出去的就是那段文本。查不到计价器时返回 `undefined`，不抛错：调用方经常在给历史计价，而那条历史的路由可能已经卸载了，为一个已经不存在的 provider 抛错会让整个会话打不开。

### 自己搭的时候

把缓存读写当成独立计数，别折进 input。折进去之后，账单能对，但"这一轮省了多少"算不出来，缓存命中率也没法按请求统计。更麻烦的是压缩：判断"上下文是否真的变小"要靠输入侧的真实 token 数，缓存命中会把这个数字压得很低，看起来像上下文被清空。

另一条：`totalTokens` 要么给准，要么不给。用 `input + output` 估算会让缓存计入两次，和计费口径对不上；省略它，消费方会自己去加三项，至少加的是同一组不相交的数。
`reasoningTokens` 是输出侧的子集，用于展示"想了多久"，不进入任何预算计算。把它加进 `outputTokens` 会让两个数字对不上：provider 报的 output 已经包含推理 token。

## 7. 错误码：配额、账号、上下文

provider 的错误码不统一，所以分类发生在适配器里。`llm-deepseek/src/transport.ts:22` 的阶梯是可读的范本：

| 判定 | code |
| --- | --- |
| 401/403、`authentication_error`、`permission_error` | `AUTH` |
| `isQuotaExceededError(detail)` 或 402 | `QUOTA` |
| 429 或 `rate_limit_error` | `RATE_LIMIT` |
| `isContextWindowExceededError(detail)` | `CONTEXT_WINDOW_EXCEEDED` |
| 400/413 或 `invalid_request_error` | `INVALID_REQUEST` |
| ≥500 或 `api_error`、`overloaded_error` | `SERVER` |
| 其他 | `HTTP_<status>`，无状态码则 `SERVER` |

配额识别只能靠文本。`error.ts:97-103` 的 `isQuotaExceededError()` 只匹配终止性的额度措辞：`insufficient quota|balance|credits`、`quota|usage limit` 加 `exceeded|exhausted|reached`、`exceed ... quota`、`balance|credits exhausted|depleted`、`out of credits|budget`。它故意不匹配 `rate limit exceeded`，那条属于 429，等一会儿就好。混在一起的后果是把"稍后重试"变成"去充值"。

码之上还有一层账号语义，在 `llm-deepseek-account` 里：

- `ACCOUNT_SIGN_IN_REQUIRED`：没有 token，需要登录。
- `ACCOUNT_TOKEN_INVALID`：token 被拒，插件会顺手把本地凭据标记失效。
- `ACCOUNT_QUOTA`：账号配额耗尽，错误消息原样保留，`cause` 链上原始 `QUOTA`。

三个码对应三个界面动作，登录、重新登录、打开账单页。如果统一成 `QUOTA`，UI 只能显示"额度不足"，用户不知道该点哪里。

几个容易混的码，语义各不相同：

- `MISSING_CREDENTIAL` 与 `INVALID_CREDENTIAL`：前者没配，后者配了但不可用（格式错误）。后者被刻意排除在默认可重试集合之外，重试一个坏 key 只会把账号锁得更死。
- `CONTEXT_WINDOW_EXCEEDED`：无论来自抛出的异常还是 in-band 错误，适配器都必须归一成这一个码。压缩逻辑只认它。
- `EMPTY_RESPONSE`：可重试。零内容块的完成没有产生任何持久结果，重试安全。
- `TIMEOUT` 与 `ABORTED`：前者是传输看门狗到期，后者是调用方取消。两者都终止本次尝试，重试策略把它们当终止错误。

重试策略只有两种模式，字段差异在 `retry-policy.ts` 里：

| 模式 | 次数上限 | 退避 | 用在哪 |
| --- | --- | --- | --- |
| `normal` | 有限次（默认五次） | `initialDelayMs`、`maxDelayMs`、`jitterRatio` | 常规部署 |
| `always` | 无 | 同上 | 专用网关、离线批处理 |

默认的可重试集合是五个码：`EMPTY_RESPONSE`、`RATE_LIMIT`、`SERVER`、`TIMEOUT`、`TRANSPORT`（`retry-policy.ts:19-25`）。`INVALID_CREDENTIAL`、`CONTEXT_WINDOW_EXCEEDED`、`QUOTA` 都不在里面，前两个重试没有意义，第三个重试会把额度耗得更快。退避默认从 500 ms 起，上限 10 s，抖动 0.1。`always` 模式只改次数语义，退避参数照旧；配置从 `normal` 切到 `always` 时，遗留的 `maxRetries` 与 `retryableCodes` 会被忽略，未知键仍然报错。

`LlmRuntime.providerRetryPolicy(provider)` 读注册时的值，`llmRetryPolicyOf(stream)` 读这次派发捕获的值。两个值可能不同，运行中改配置只影响后续请求。

默认重试策略是 normal 模式、最多五次，退避参数在 `retry-policy.ts` 里解析。`always` 模式没有次数上限，只给特殊部署用。重试发生在 agent 层：每次重试开一个新的编号 turn，日志里能看到 `turn/start` 连续出现，工具结果不会跨尝试混淆。这条链路第一季已经拆过，见 [《错误、重试与恢复》](/posts/dsh-error-retry-recovery.html)。

## 8. 两个适配器的边界

`llm-deepseek` 是直连实现。它自己拼 HTTP、自己解析 SSE，说的是 Anthropic Messages 那套线协议（`message_start`、`content_block_delta`、`cache_read_input_tokens`），打的是 DeepSeek 端点。鉴权由 `resolveAuth()` 提供，返回请求头和一个可选的失败回调；传输层不判断凭据类型是 API key 还是账号 token。文件与图片有独立的上传索引和请求投影，顶层扩展字段走 `ctx.deepseekLlmApiExtensions` 注册，冲突时以 `REQUEST_EXTENSION` 失败。

`llm-pi-ai` 是库适配。它把请求交给 pi-ai 的提供方目录，覆盖多家 provider、OAuth 登录、按请求解析 profile。配置面是一个 `providers` 字典，键就是 `GenerateOptions.provider`。它额外提供 `llm-pi-ai/request-headers` 这个同步 waterfall，监听器拿到一个不可变的上下文和本次请求的 `Headers`，可以加头但不能改消息。

`llm-pi-ai/request-headers` 这个 waterfall 值得单独看一眼。它拿到的是不可变的上下文（provider、model、baseURL、sessionId、purpose）和本次请求的 `Headers`，监听器可以原地加头，但不能碰消息与鉴权字段。抛错会让请求在出网之前失败，code 是 `REQUEST_HEADERS`。这条路径的用途是给特定 provider 补观测头或路由标签，而不是改写请求内容。

两者的共同点由 seam 保证，不由代码共享保证：都继承 `LlmAdapter`，都通过 `registerAdapter()` 注册，重复路由原子失败（`DUPLICATE_ADAPTER`），每次 HTTP 请求都带 `attributionHeaders()` 生成的 `User-Agent`，`replayState` 只回给产生它的那个适配器实例。

`NO_ADAPTER` 的处理是一处刻意的宽松。`prepareCall()` 在解析模型之前就抛它，但 `prepareRequest()` 会吞掉这一个错误：

```ts
} catch (error: unknown) {
  // Middleware may serve an unregistered route; terminal dispatch still requires an adapter.
  if (!(error instanceof LlmError) || error.code !== 'NO_ADAPTER') throw error
  config = proposedConfig
}
```

吞掉之后 `preparedCall` 是 undefined，`buildRequest()` 照常写请求头，执行时退回 `this.loopCtx.llm.stream(request)`。这条路径能成立，是因为 `llm/stream` waterfall 的监听器可以完全不碰适配器就交出一条流。快照回放就是这么跑的：没有 API key，没有适配器，靠 `ctx.on('llm/stream', ...)` 重放录制的 chunk。

可以容忍的场合：回放、录制、离线评测、以及部署级网关（网关自己实现 `llm/stream`）。不能容忍的场合：真实用户请求打到没有适配器的路由。后者的表现是 `stream()` 在终端 continuation 里抛 `NO_ADAPTER`，被归一成一个 `finish { kind: 'error' }` 帧，模型看到的是一个 `NO_ADAPTER` 失败。这个失败会在 UI 上显示，不会静默成功。

## 9. 设计取舍

- 准备与执行分成两个方法。收益是日志与派发可以对齐、一次性保证可实施；代价是直接调用方要学两个入口，`resolveCallConfig()` 和 `prepareCall()` 的差别得看文档。
- 冻结用深拷贝。每次请求多一次 `structuredClone`，换来的是下游无法在派发前改配置。如果只冻结顶层，`messages` 数组仍可被 push，请求头与请求体就会不一致。
- usage 拆成不相交字段。适配器要为自己的 provider 做减法，计量层拿到的是统一语义。合并成一个 `inputTokens` 会让成本核算在 cache 场景下偏高三到十倍。
- 配额靠文本识别。多家 provider 没有统一错误模型，正则加状态码兜底是唯一可行的做法。代价是误判风险，所以匹配词表只收终止性措辞。
- 帧与记录分离。前端拿瞬时帧，回放拿折叠记录，两套类型之间用校验器连接。代价是两处代码要保持同步，收益是回放不依赖渲染层。
- 适配器内不重试。重试属于有日志、有轮次的上层。代价是直接调用 `ctx.llm.stream()` 的人只得到一次尝试。

## 10. 踩坑点

- `INVALID_PREPARED_CALL` 有两条消息，含义不同：`can only be dispatched once` 是重复派发，`config changed before adapter dispatch` 是配置被改。看消息定位，别看码。
- 把 `prepareCall()` 包在 `try` 里并吞掉所有 `LlmError`，会连 `NO_ADAPTER` 一起吞。回放场景需要这个行为，真实请求不需要。只在明确要回放时吞。
- `agent "..." has no provider/model` 出现在 `preStep()`。别去查适配器，先查这个 agent 的 `AgentOptions` 和 `model/selection` 记录，两者都没有才会抛。
- `agent/request proposed "..." after step preparation fixed "..."` 说明有插件在 waterfall 里改路由。改成在 `agent/pre-step` 之前调用 `agent.model.select()`。改 waterfall 里的值一定会被这两行拦下。
- 传了 `reasoningEffort` 但模型没有推理强度信息，抛 `UNSUPPORTED_REASONING_EFFORT`。这是能力不匹配，不是配置错误；界面上应该禁用这个选项，别让请求失败。
- 把 `adapterDefaults.reasoningEffort` 漏记，会话恢复后默认 effort 会被固化成显式值。表现是同一个模型、同一个选择，重启前后的思考长度不一样。
- 在 `stream()` 里 `yield` 之后捕获异常，会把消费方异常当成 provider 异常。用 DSH 的写法：迭代在 try 内，`yield` 在 try 外。
- 复用同一个 `AbortSignal` 发多次请求。第一次取消后 signal 永久 aborted，后续请求在 `throwIfAborted()` 处直接失败。每次请求一个 signal，来自当前 turn 的 abort。
- 在 `llm/stream` 监听器里改 `options`。loop 组好的请求是深冻结并打了 `markAgentLoopRequest` 标记的，监听器只能读；要改参数得在 `agent/request` 或更早的 `configure()` 里做。
- 在 `prepareCall()` 里读出 `maxTokens` 却在派发时又传一遍不同的值。两条 `INVALID_PREPARED_CALL` 检查会拦下，但错误出现在执行阶段，调用栈里看不到是谁改的。
- 以为 `finish` 之后还能收到 usage。契约是 usage 在 finish 之前，晚到的 usage 只能丢掉；有的 provider 会在最后一个空事件里补一次 usage，适配器必须把它提前合并。
- 把 `NO_ADAPTER` 当成可重试错误。它表示路由上没有适配器，重试同一个路由只会重复失败；要么注册适配器，要么让 `llm/stream` 监听器接管。
- 给 `assistant/message` 写 `usage` 时用 `input + output` 自己算，忽略了 cache 字段。`token-meter` 的 `usageTokens()` 会把三项都加上，两个数字从此对不上。

## 11. 源码索引

- `packages/llm/llm/src/types.ts:13-32`：`llm/adapters-updated`、`llm/models-updated` 两个事件。
- `packages/llm/llm/src/types.ts:182-196`：`TokenUsage`，四个不相交计数与 `totalTokens` 的填写条件。
- `packages/llm/llm/src/types.ts:488-508`：`StreamChunk` 联合与帧契约（usage 在 finish 之前、工具参数是原始 JSON 字符串）。
- `packages/llm/llm/src/index.ts:191-302`：`LlmAdapter` 的默认实现与唯一抽象方法 `stream()`。
- `packages/llm/llm/src/index.ts:948-1018`：`prepareCall()` 的七步与一次性派发闭包。
- `packages/llm/llm/src/index.ts:1020-1024`：`NO_ADAPTER` 的抛出点。
- `packages/llm/llm/src/index.ts:1063-1157`：`adapterStream()` 的三次投影、迭代结构与 yield 位置。
- `packages/llm/llm/src/index.ts:1170-1184`：`stream()` 与 `llm/stream` waterfall 的包裹顺序。
- `packages/llm/llm/src/index.ts:1026-1041`：`forAdapter()` 剥掉跨适配器的 `replayState`。
- `packages/llm/llm/src/index.ts:1187-1196`：`adapterFailureChunk()`，错误与取消归一成同一个 terminal 帧。
- `packages/llm/llm/src/types.ts:564-606`：`GenerateOptions` 的字段表，以及 `purpose` 与 `sessionId` 的位置。
- `packages/core/agent/src/runtime-types.ts:54-72`：`Model` 接口，`current()` 不启动 step、`select()` 替换下一步的完整选择。
- `packages/llm/llm-pi-ai/request-headers`：同步 waterfall 的上下文与 `Headers` 约定。
- `packages/llm/llm/src/retry-policy.ts`：`normal` / `always` 两种模式的解析与默认值。
- `packages/llm/token-meter/src/types.ts:16-53`：`TokenMeasurement` 的基线三态与 `TokenSurfaceNode` 的双价。
- `packages/llm/llm/src/error.ts:25-51`：上下文、配额、账号配额、空响应、凭据无效五个码。
- `packages/llm/llm/src/error.ts:91-103`：`isQuotaExceededError()` 的词表。
- `packages/llm/llm-deepseek/src/transport.ts:9-44`：provider 错误阶梯、`retry-after` 与 request id 提取。
- `packages/llm/llm-deepseek/src/translate.ts:34-41`：SSE usage 字段映射；`:159` 的总和计算。
- `packages/llm/llm-deepseek-account/src/index.ts`：`QUOTA` → `ACCOUNT_QUOTA`、401 → `ACCOUNT_TOKEN_INVALID` 的转换。
- `packages/core/agent-default-model/src/index.ts:67-94`：`currentSelection()` 与串行化的 `saveSelection()`。
- `packages/core/agent/src/model-selection.ts:105-115`：`current()` / `select()`；同文件投影定义里的 `selectionOfHeader()`。
- `packages/core/agent-loop/src/agent.ts:283-317`：`preStep()` 固化选择，工具与提示词都从这里派生。
- `packages/core/agent-loop/src/agent.ts:613-653`：`prepareRequest()` 与两次路由守卫、`NO_ADAPTER` 容忍。
- `packages/core/agent-loop/src/assistant-stream.ts:49-109`：三种帧、`settle()` 的提交顺序、`abandon()`。
- `packages/core/agent-loop/src/agent.ts:444-510`：一次 step 内准备、写头、组请求、消费流的完整循环。
- `packages/llm/token-meter/src/index.ts:59-60`：`usageTokens()`；`:151-193` 的 `measure()` 基线选择。
- `packages/llm/token-meter/src/usage-projection.ts:169-199`：`contextPressure` 的 `contextWindow` 来源与 `projectedTokens` 公式。

## 最后

模型层是整条主干里唯一会出网的一层，也是最少被日志记录的一层：日志里留下的是请求头、折叠后的流和一行 usage，网络上的字节不落库。想读懂第一季的请求组装，得先知道这里交出的是什么样的流；想让工具结果被正确截断，得知道下一层怎么计量 token。这两点分别对应 [《请求组装》](/posts/dsh-request-assembly.html) 和 [《工具结果的两种形态》](/posts/dsh-tool-results.html)。

上一篇：[《一次请求的完整旅程》](/posts/dsh-request-journey-end-to-end.html) ｜ 下一篇：[《执行环境：四个 seam、一条 bash 链路与三种截断》](/posts/dsh-execution-environment.html)
