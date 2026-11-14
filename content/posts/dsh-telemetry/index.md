---
title: "遥测：trace、token 与产品事件"
date: "2026-11-14T18:30:00+08:00"
summary: "三类遥测各记什么：OTel 通道记调用链、session telemetry 记会话事实、product telemetry 记产品动作；token 计数如何贯穿三者。"
description: "从一次请求的层级坐标到 cache_read/cache_write 的记账，拆遥测的采样、隐私红线与用它定位一次慢请求的方法。"
tags: ["harness", "architecture", "billing"]
slug: "dsh-telemetry"
---

> 源码版本：`deepseek-harness` v0.2.2-rc.1（commit `fcc0ad46`）。本文逐句对照 `packages/telemetry`、`packages/session/session-telemetry*`、`packages/host/product-telemetry-otel`、`packages/client/product-analytics`、`packages/llm/token-meter` 与 `packages/session/session-stats` 的当前源码。
> 本系列第 6 篇。上一篇拆了 `run_code` 怎么把工具调用塞进子进程，这一篇讲这些调用留下的记录。

先摆三个让人停一下的现象。

第一个：在全仓搜索 `startSpan`，命中数是 0。你打开任何一份 OTel 入门文档，第一段都在教 `tracer.startSpan('name')`，而这个仓库一行都没有调。

第二个：装了 `session-telemetry-otel` 之后，跑一整天会话，collector 可能一条记录都收不到。这不是配置错了，默认模式叫 `FEEDBACK_ONLY`。

第三个：一条会话记录的 OTLP 属性里找不到 `model`，也找不到 `tokens`。它们确实被上传了，只是被打包进一个叫 `content` 的字符串字段里。

三个现象指向同一个设计：遥测把已经存在的会话事件搬出去，不在每个函数里新插埋点。后面所有取舍都能从这一条推出来。

## 0. 结论

| # | 层 | 服务键 / 入口 | 写入点 | 默认 |
|---|----|--------------|--------|------|
| 1 | 工厂 | `ctx.otel` | `packages/telemetry/otel/src/index.ts` | 挂载只提供两个工厂，不建队列 |
| 2 | 会话记录 | `ctx.sessionTelemetry` + coordinator | `packages/session/session-telemetry/src/coordinator.ts` | 采集按需，默认不主动抓 |
| 3 | 会话上传 | `session-telemetry-otel` | `packages/session/session-telemetry-otel/src/index.ts` | `FEEDBACK_ONLY`，反馈才传 |
| 4 | 产品事件 | `ctx.productTelemetry` | `packages/host/product-telemetry-otel/src/index.ts` | 只挂桌面端 |
| 5 | token 记账 | `token-meter` 投影 | `packages/llm/token-meter/src/turn-usage.ts` | 纯推导，不读网络 |

路线图：`otel` 出工厂，coordinator 从会话事件造记录，脱敏 waterfall 过一遍，OTel 后端把记录发成 OTLP log。产品事件走另一条队列。token 记账根本不在这条链上，它是会话事件的纯函数。

## 1. 三层遥测的边界

先看所有权。三层的差别不在数据格式，在「谁决定写、谁承担生命周期」。

第一层 `otel` 是共享工厂。它被 `packages/telemetry/otel/src/index.ts` 里的 `class OTel extends Service` 实现，只暴露两个方法：

```ts
// packages/telemetry/otel/src/index.ts
createEventReporter(options: EventLogOptions): EventLogReporter
createSessionLogReporter(options: SessionLogOptions): SessionLogReporter
```

看这两个签名：参数是 options，返回值是 reporter。服务自身没有队列、没有身份、没有 endpoint。挂载它不产生任何网络连接，两个 reporter 也互不共享队列。文档把这条写成「只挂载 OTel 服务不分配 provider 或 transport」，代码与之对得上。

第二层 `session-telemetry` 是会话事实的记录者。它以 `abstract class SessionTelemetryBackend extends Service` 定义后端契约，service key 是 `sessionTelemetry`，并额外提供 coordinator 负责「把会话事件变成记录」。这一层不知道 OTLP 是什么，只认 `SessionTelemetryRecord`。

第三层是具体后端。仓库里挂载的是 `session-telemetry-otel`，它继承第二层的抽象类，用 SDK 的 `LoggerProvider` 把记录发成 OTLP log。注意实现的是 log，不是 trace。

产品事件不在这条链上。`product-telemetry-otel` 在宿主侧维护独立队列与 exporter，`product-analytics` 在客户端收集动作。两者只在 bundle 配置里同时受 `profileContext` 约束。

> 自己搭 Agent 时：把「记录什么」和「发到哪里」分开定义，收益是后端可换、测试可跑。代价是你在中间加了一层 record 类型，所有字段都要在这一层定型。

token 记账要单独说：它不属于任何一层遥测。`token-meter` 是一个 `session-projection` 单元，输入整段会话事件，输出 `TurnTokenUsage`。它不发送任何东西。

{{< fig "diagrams/01-three-layers.svg" "三层边界与两条独立通道：会话记录经脱敏 waterfall 进会话日志 exporter，产品事件走自己的队列" >}}

## 2. otel 只是工厂，不是采集器

为什么把工厂单独做成一个服务？看两个 reporter 的构造差异就明白：event reporter 用 `BatchLogRecordProcessor` 发遥测自身的事件，session reporter 用自定义的 `SessionLogProcessor` 做字节限流与单请求串行。两者的队列策略完全不同，硬塞进一个服务会互相拖累。

`SessionLogProcessor` 的默认参数写在 `packages/telemetry/otel/src/session-log.ts`：

```ts
// packages/telemetry/otel/src/session-log.ts
maxQueueSize: options.processor?.maxQueueSize ?? 2048,
maxExportBatchSize: options.processor?.maxExportBatchSize ?? 512,
scheduledDelayMillis: options.processor?.scheduledDelayMillis ?? 1000,
exportTimeoutMillis: options.processor?.exportTimeoutMillis ?? 30000,
```

看 `maxExportBatchSize` 与 `maxQueueSize` 这一对：批量小于队列，队列才有周转空间；构造时 `batch > queue` 直接报错，不许出现「批次永远装不满」的配置。

每条记录进队之前先序列化一次量字节：

```ts
// packages/telemetry/otel/src/session-log.ts
const size = JsonLogsSerializer.serializeRequest([record]).length
if (size > this.config.maxRequestBytes) {
  this.warn('Session log record rejected; content was not truncated')
  return
}
```

看 `rejected` 这个词。超限的记录整条丢掉，不做截断。截断会产出一条字段缺失但看起来正常的记录，下游分不清「模型没报 token」和「我们切掉了 token」。宁可少一条，不留假数据。

## 3. 一条会话记录怎么长出来

记录由 coordinator 的 `captureEvent` 造。源码在 `packages/session/session-telemetry/src/coordinator.ts:179`：

```ts
// packages/session/session-telemetry/src/coordinator.ts:179-199
const { data, ...envelope } = event
this.deliver(session, {
  record: this.redact({
    sourceEvent: { sessionId: session.id, envelope: structuredClone(envelope) },
    channel: 'ledger',
    time: event.time,
    severity: severityOf(event),
    attributes: identityOf(session, event),
    body: structuredClone(data),
  }),
  seq: event.seq,
})
```

看 `structuredClone` 出现两次：信封一次，data 一次。注释说明了原因，会话事件对象是可变的，后端要等批处理才序列化，不克隆就可能导出「被后续代码改过」的历史。这是遥测最常见的一类脏数据。

五段字段各有分工：

| 字段 | 内容 | 谁读 |
|------|------|------|
| `sourceEvent` | 会话 id + 去掉 data 的信封 | 后端判授权、拼 OTLP 事件 |
| `channel` | `ledger` 或 `ops` | 分流：事件 vs 运维 |
| `time` | 事件自带的毫秒时间 | 排序、对时 |
| `severity` | `info` / `warn` / `error` | 告警预烘焙 |
| `attributes` | 身份四件套 | collector 过滤 |
| `body` | 事件 data 的深拷贝 | 内容主体 |

`severityOf` 只认两件事：`tool/result` 的 `message.isError === true`，`turn/end` 的 `reason.kind === 'error'`。其他一律 `info`。这是有意的：插件可以合并新事件类型，coordinator 不认识的一律不猜。

`identityOf` 的必带属性只有四项：`session.id`、`session.format_version`、`event.type`、`event.seq`。可选三项：`session.cwd`、`session.parent_id`、`session.seed_length`。

除了会话事件，还有一类记录走同一个后端，`channel` 是 `ops`。它没有 `sourceEvent`，只有 `telemetry.op` 与环境事实：

| 记录 | 何时写 | 字段 |
|------|--------|------|
| `shutdown` | 会话销毁，或 coordinator 销毁时仍在活的会话 | `telemetry.op`、`session.id`，body `{ op: 'shutdown' }` |
| `agent-error` | 总线上抛 `agent/error` | `telemetry.op`、`session.id`、`agent.id`、`error.name`、`turn`、`step` |

`shutdown` 标记解决一个具体问题：collector 收到半截会话时，分不清是「进程崩了」还是「会话正常结束」。多一条可数的终点记录，两种情况的查询就能分开。注意 on-demand 模式不生成这类记录，它只读历史，不观测当下。

`agent-error` 是唯一把异常名单独提成属性的地方。因为排查线上崩溃时，你会先按 `error.name` 分组，而不是去 `content` 里搜。这条规则没有推广到别处：其他事件的字段都留在 `content` 里。

到这里，"provider、model、tokens、cache 这些属性必带"这个期待要修正一下：它们不在 `attributes` 里。OTLP 那一层的 `attributes` 只放身份字段，整条事件被序列化成 `content`：

```ts
// packages/telemetry/otel/src/session-log.ts:198-212
logger.emit({
  eventName: 'session-log',
  body: 'session-log',
  timestamp: record.event.time,
  observedTimestamp: record.event.time,
  severityNumber,
  severityText: SeverityNumber[severityNumber],
  attributes: {
    ...record.attributes,
    sessionId: record.sessionId,
    content: JSON.stringify(record.event),
  },
})
```

看 `content` 这一行：`JSON.stringify(record.event)` 把 provider、model、usage、cache 全部塞进一个字符串。collector 侧能按 `session.id`、`event.type` 建索引，想按 model 聚合就得先解析 JSON。

这是 upsert 型设计：一条记录一行，字段随事件类型变化。会话事件的 data 有几十种形状，摊平成属性会得到一张几百列、九成是 null 的表。

事件本身为什么用追加日志、怎么重放，在 [会话事件溯源](/posts/dsh-session-event-sourcing.html) 里讲过；这里只取它的读取侧。

## 4. 一次请求的嵌套：事件序替代 span

问题问的是 span 结构。老实说：这个仓库没有 span。能对应 span 父子关系的是会话事件里的坐标。

一次请求从上到下的层级：

| 层级 | 事件 | 关键字段 | 谁写 |
|------|------|----------|------|
| turn | `turn/start` / `turn/end` | `turn`、`reason` | agent loop |
| step | `step/start` / `step/end` | `turn`、`step` | agent loop |
| attempt | `assistant/attempt` / `llm/retry` / `llm/retry-started` | `retryId`、`delayMs`、`failure` | 模型层与重试插件 |
| tool | `tool/call` / `tool/result` | `callId`、`name`、`message` | 工具流水线 |

要的四个字段散在三处：provider 与 model 在 `request/context`（路由变化时才记一条）和每条 `assistant/message` 的 `source` 上；token 用量在 `assistant/message.usage`；缓存两桶是 usage 的一部分。把它们拼成「哪个模型、多少 token、命中多少缓存」，靠 `(turn, step)` 对齐。

`step/end` 才是计步权威，`assistant/message` 不是。原因写在 `packages/session/session-stats/src/projection.ts` 的模块注释里：loop 在 `finally` 里为每个进入的 step 追加恰好一条 `step/end`，取消的、失败的、撞 max-tokens 的 step 都会落一条；而组装好的 message 在取消时根本不存在。

attempt 这一层是线性的，不是树。一次 step 内可能先 `assistant/attempt` 失败，再 `llm/retry` 记下失败，再 `llm/retry-started` 重开，然后第二次 `assistant/attempt`。所以「第几次尝试」靠重试事件数位置推，不靠嵌套。

重试事件带够了排障字段，`packages/llm/llm-retry/src/types.ts` 里两种模式共用大半：

```ts
// packages/llm/llm-retry/src/types.ts
{
  retryId: RetryId
  turn: number
  step: number
  provider: string
  mode: 'normal' | 'always'
  policyKey: string
  retry: number
  maxRetries?: number
  delayMs: number
  failure: LlmFailure
}
```

看 `delayMs` 与 `failure` 并存：等待时长和失败原因在同一条记录里。事后算「这次请求有多少时间花在退避上」不需要读配置，把 `delayMs` 求和即可。`policyKey` 区分是哪条重试策略放行的，`maxRetries` 只在 `normal` 模式出现，`always` 模式没有上限。重试策略怎么决定重试、失败怎么归因，在 [错误、重试与恢复](/posts/dsh-error-retry-recovery.html) 里拆过。

{{< fig "diagrams/02-record-shape.svg" "一条 record 的六段如何映射成 OTLP log record：身份属性在外，事件全文在 content 里" >}}

如果一定要给这三个坐标一个类比：`(turn, step)` 是 span 的父子路径，`callId` 是 tool span 的 id，`retryId` 是 attempt span 的 id。区别在于 span 由进程内上下文隐式传递，这里由事件里的整数字段显式携带。好处是重放能完全重建，坏处是你必须自己写关联代码。

## 5. token 记账：四个桶与一次严格折叠

先看数据形状。`packages/llm/llm/src/types.ts:182` 的 `TokenUsage` 有一个容易读错的注释：

```ts
// packages/llm/llm/src/types.ts
/**
 * Counts are DISJOINT: `inputTokens` is uncached input only; cached input is
 * reported separately as `cacheReadTokens`/`cacheWriteTokens`
 * (billed input = sum of the three).
 */
```

看 `DISJOINT` 这个词。`inputTokens` 不含缓存命中部分。很多 provider 的 `prompt_tokens` 是包含缓存的，适配器要减出来。减法落在哪一层、怎么减，[模型层](/posts/dsh-model-layer.html) 里有逐字段的对照。计费口径因此是三项相加：

```ts
// packages/llm/token-meter/src/index.ts:56-62
function usageTokens(usage: TokenUsage): number {
  return usage.inputTokens
    + (usage.cacheReadTokens ?? 0)
    + (usage.cacheWriteTokens ?? 0)
    + usage.outputTokens
}
```

`token-meter` 不做累计器，它做折叠。`deriveTurnTokenUsage(events)` 吃一整轮的事件，吐一个 `TurnTokenUsage` 或者 `undefined`。规则很硬：

- `step/start` 打开一个 attempt。
- `assistant/attempt` 从 stream 里取 usage 并关闭它。
- `assistant/message` 用 `event.data.usage ?? streamUsage(...)`，关闭并标记 `by: 'message'`。
- `llm/retry` 关掉当前 attempt，标记 `by: 'retry'`；`llm/retry-started` 用同一个 step 重开。
- 任何缺边界、缺 usage、非安全整数、与 `totalTokens` 矛盾的轮次，整轮返回 `undefined`。

聚合时更严格：`cacheReadTokens`、`cacheWriteTokens`、`reasoningTokens` 只在每个 attempt 都报了这个桶时才出现；`routes` 只在每个计费 attempt 都有 provider/model 归属时才出现。

```ts
// packages/llm/token-meter/src/turn-usage.ts:136-147
const cacheReadTokens = cacheRead.every(isCount) ? safeSum(cacheRead) : undefined
const cacheWriteTokens = cacheWrite.every(isCount) ? safeSum(cacheWrite) : undefined
const reasoningTokens = reasoning.every(isCount) ? safeSum(reasoning) : undefined
```

看 `every` 这个词。三个 attempt 里两个报了缓存、一个没报，缓存字段整个消失，而不是当成 0。当成 0 会把「没报」记成「没有缓存」，账单上少一截，还查不出来。

还有一层推导。`totalTokens` 缺失时折叠会自己算总和，但要先过一道门槛：

```ts
// packages/llm/token-meter/src/turn-usage.ts:114-124
if (cacheReadTokens === undefined || cacheWriteTokens === undefined) return undefined
const derivedTotal = safeSum([knownPrompt, outputTokens])
if (derivedTotal === undefined) return undefined
```

看这个前置条件：两个缓存桶都得在，才允许反推。少一个就无法区分「没有缓存」与「没报缓存」，反推出来的总数会偏小。

`totalTokens` 存在时则要做一致性检查：`totalTokens - outputTokens` 必须不小于已知 prompt 之和；当两个缓存桶都有值时，还必须严格相等。任何一条不满足，这个 attempt 直接作废，接着整轮作废。

谁读这个结果？两处。`packages/client/ui-chat/src/client/conversation-nodes/turn-tail.ts:91` 渲染每轮 token；`session-stats` 用 output tokens 算 decode 速度。两者都从事件重算，不共享缓存。

{{< fig "diagrams/03-token-folding.svg" "一轮事件折叠成 TurnTokenUsage：两个 attempt、一次重试，缺一个桶整项消失" >}}

## 6. 产品事件：另一条链路

产品事件回答「用户点了什么」，与会话内容无关。它的入口是客户端服务：

```ts
// packages/client/product-analytics/src/index.ts
@Remote report(event: ProductEvent): void
```

服务默认启用，但整条链路只在桌面端挂载。`packages/bundle/web-app/cordis.patch.yml` 里两行都带同一个条件：

```yaml
disabled: ctx.get('profileContext')?.name !== 'desktop'
```

看这个 `disabled`：普通 Web 打开时，`productTelemetry` 与 `product-analytics` 两个插件都不加载。不是「加载了但不上报」，是进程里没有这个服务对象。

事件名是封闭枚举，写在 `packages/client/product-analytics/src/events.ts` 的 `ProductEventMap` 里。抽样几条：

| 事件 | 上报的属性 |
|------|-----------|
| `send_button_click` | `run_mode`、`msg_type`、`is_new_session`、可选 `model_name` / `thinking_effort` / `agent_preset` |
| `model_switch` | `switch_from`、`switch_to` |
| `context_compression` | `session_id`、`trigger_type`（`auto` / `manual`） |
| `plugin_install_click` | `input_value` |

这个类型不是文档，是编译期约束：`report` 的入参由映射类型生成，写错事件名或者多带一个字段都过不了 tsc。

身份与设备信息在首次上报时收集，`collectEnvironment({ timeoutMillis: 5000, maxOutputBytes: 16384 })`，超时或超限就把缺失原因写进 `missing_fields`，格式是 `field=reason` 的逗号串。这里同样有「宁可缺，不造假」：拿不到 device_id 就省掉字段并注明原因，不编一个占位值。

服务端接收方 `ProductTelemetry` 有自己的批次参数：`maxExportBatchSize 512`、`maxQueueSize 2048`、`scheduledDelayMillis 30000`，请求头带 `x-channel: dsh_otel_report`。注意 `30000` 与会话日志的 `1000` 差 30 倍：产品事件可以等，会话反馈不能等。

## 7. 隐私与开关

默认策略只有一条：不点反馈，什么都不上传。`session-telemetry-otel` 的 `mode` 默认 `FEEDBACK_ONLY`，此时 coordinator 用 `capture: 'on-demand'` 构造，不注册连续监听，只在反馈事件到来时按需读取。

授权判断在 `isFeedback` 里，两个条件都容易漏：

```ts
// packages/session/session-telemetry-otel/src/index.ts:42-52
if (event.seq < session.inheritedEventCount) return false
switch (event.type) {
  case 'feedback/record': return true
  case 'feedback/message-put':
  case 'feedback/message-delete': return event.data.sessionId === session.id
  default: return false
}
```

看第一行：`inheritedEventCount` 之前的序号是 fork 时从父会话继承的种子，不算本次授权。子会话点反馈，不会连带上传父会话的历史。

第二道闸更细：事件必须与 canonical 会话日志里同序号的那条是同一个对象，否则告警并忽略。防止插件在总线上伪造一条看起来像反馈的事件。

脱敏没有默认规则。`session-telemetry/record` 是 waterfall 扩展点，`redact()` 的实现就是把它跑一遍：

```ts
// packages/session/session-telemetry/src/coordinator.ts:205-207
private redact(record: SessionTelemetryRecord): SessionTelemetryRecord {
  return this.ctx.waterfall('session-telemetry/record', record, () => record)
}
```

看 `() => record` 这个兜底：没有 listener 时原样通过。包自己的注释写得很直白，「exported data is exactly as clean as the rules a deployment mounts」。脱敏规则是部署责任，不是库责任。

两个方向都做了 fail-closed：

1. 规则抛错，这一条被扣下，只留一条 warn。因为 coordinator 用 `contain()` 包住整步，而 cordis 的 `emit` 遇错停止，不包住会饿死后面所有订阅者。
2. 规则把 `sourceEvent` 删了，后端认不出这是哪条会话的事件，直接丢弃并 warn `redaction removed sourceEvent`。

按会话关闭有三个层次：

| 手段 | 作用范围 | 代码位置 |
|------|----------|----------|
| `DSH_TELEMETRY_MODE=DISABLED` | 会话日志上传 | `packages/bundle/base/cordis.patch.yml` |
| `DSH_TELEMETRY_DISABLED`（任意非空值） | 整个 `session-telemetry-otel` 行被 patch 掉 | `packages/boot/app-boot/src/profile-context.ts` |
| `config.enabled = false` | 产品事件 | `packages/client/product-analytics/src/index.ts` |

中间那行值得单说，注释原话是「ANY non-empty value (including '0'/'false') disables: a privacy switch prefers off-by-mistake over on-by-mistake」。写 `DSH_TELEMETRY_DISABLED=0` 也会关掉。隐私开关宁可误关。

没有采样。全仓搜不到 sampler 配置，控制量的手段是队列上限、单条字节上限、批处理间隔。这三者都不改变「哪些事件被记录」，只改变「什么时候发」。

## 8. 定位一次慢请求的四步

假设用户说「这一轮特别慢」。会话日志在本地，四步能定位到具体哪一段。

第一步，分桶。`session-stats` 投影给出 `llmMs`、`toolMs`、`ttftMs`、`decodeMs` 四个总时长。先看哪个占大头，不要一上来就翻 prompt。

第二步，看重试。`llm/retry` 带 `failure`、`delayMs`、`retry`，`llm/retry-started` 与之配对。三次重试每次退避 2 秒，就是 6 秒纯等待。这一步能排掉「模型慢」的误判。

第三步，看单个 step。模型时间口径是 `step/start` 到 `assistant/message`；首 token 是第一个非空 delta chunk 的时间，且跨 `llm/retry` 存活；decode 只统计报了 output tokens 的 step。注意取消的 step 不组装 message，它的部分流时间不计入任何口径，所以你看到某个 step 没有时间，先确认它是不是被取消了。

第四步，看工具对。按 `callId` 配对 `tool/call` 与 `tool/result`，找耗时最长的那对。只有 call 没有 result，就是还在跑或者被放弃了。

{{< fig "diagrams/04-slow-request.svg" "四步排查的分叉：先分桶，再依次排掉重试、单步与工具配对" >}}

这四步全部只读本地日志。约束在这里：遥测不能改变行为。四条证据：

- coordinator 的 `contain()` 吞掉所有异常，只 warn。
- OTel 后端的 `emit(_record) {}` 是空实现，直接塞进来的记录一律丢弃，只有内部 sink 能提交。
- 单条超限记录整条拒绝，不截断、不补字段。
- 关停超时只 warn，注释写着不能让遥测失败拖垮应用退出。

> 自己搭 Agent 时：把「观测」做成事件日志的纯函数，收益是线上和离线跑同一套代码，回放能复现。代价是你在热路径上多了一次深拷贝，`structuredClone` 不是免费的。

## 9. 关停顺序

遥测最容易在关停时违反「不能改变行为」。三层各有截止时间：

| 组件 | 截止 | 超时后 | 失败后果 |
|------|------|--------|----------|
| coordinator | 等 `backend.shutdown()`，无自带上限 | 取决于后端 | 只 warn |
| 会话 OTel 后端 | 3000ms（`shutdownTimeoutMillis`） | `stopPending()` 清队列 | reject，被 coordinator 吞掉 |
| 产品事件 | 21000ms | warn 丢事件 | 只 warn |

coordinator 的销毁钩子先补终点标记，再交接后端：

```ts
// packages/session/session-telemetry/src/coordinator.ts:126-135
for (const session of this.adopted) {
  this.contain(() => {
    this.deliver(session, { record: this.redact(shutdownRecord(session)) })
  })
}
try {
  await this.backend.shutdown()
} catch (error) {
  this.ctx.logger.warn(`telemetry: backend shutdown failed: ${String(error)}`)
}
```

看这个 `catch`：注释原话是「best-effort reporting must not fail application teardown」。上报是尽力而为，应用退出不许被它拖住。

后端自己的截止时间更细，超时前先清队列再报错：

```ts
// packages/session/session-telemetry-otel/src/index.ts:263-270
timer = setTimeout(() => {
  this.provider?.stopPending()
  reject(new Error(`session-telemetry-otel: provider shutdown exceeded ${this.shutdownTimeoutMillis}ms`))
}, this.shutdownTimeoutMillis)
```

看 `stopPending()` 在 `reject` 之前：先把队列清空，避免定时器回调返回后还有请求在路上。丢数据在这里是明确选择，日志里留一句 warn 就算交代。

还有一个反向约束容易被忽略：轮次结束时给后端的 flush 提示必须同步返回。coordinator 的注释写的是 `returning void (not the SDK's flush promise) is the turn-latency contract`。遥测慢不能拖住下一轮，所以这里要的是提示，不是等待。

## 10. 设计取舍

1. 选了 OTLP log 而非 trace span。代价是父子关系要靠字段重建，没有现成的瀑布图。收益是记录格式跟随会话事件，加一种事件不用改遥测代码。
2. 选了身份字段进属性、事件全文进 `content`。代价是 collector 侧按模型聚合要先解析 JSON。收益是不会出现几百列稀疏属性表。
3. 选了「缺桶就整轮不可用」。代价是统计面板偶尔空白。收益是账目上没有把未知记成零。
4. 选了默认 `FEEDBACK_ONLY`。代价是默认拿不到任何线上会话数据。收益是隐私默认值站得住。
5. 选了脱敏规则由部署挂载。代价是「装了就有脱敏」这句话不成立。收益是库不替使用者决定什么算敏感。
6. 选了产品事件单独一条队列。代价是两套关停逻辑、两个超时。收益是 30 秒批量与 1 秒批量各按自己的节奏跑。
7. 选了不做采样。代价是量大时只能靠队列上限顶住。收益是「这条会话被采样掉了」这种解释不会出现在排障里。

## 11. 踩坑点

1. 现象：接了 collector，一条记录都没有。原因：默认 `FEEDBACK_ONLY`，没有反馈事件就不采集。对策：先确认模式，再确认是否真的提交过反馈。

2. 现象：反馈提交了，仍然没有。原因：`exporter.url` 缺失或不是 http(s)，加载期就报错；或者 `DSH_TELEMETRY_DISABLED` 设了非空值，整行被 patch 掉。对策：看启动日志里的 `exporter.url is required` 与 profile patch 记录。

3. 现象：按 model 聚合的看板全空。原因：model 在 `content` 字符串里，不在属性里。对策：collector 侧加 JSON 解析，或者接受按会话聚合。

4. 现象：子会话反馈后，父会话历史也上去了。原因：预期如此才会发生，代码用 `inheritedEventCount` 挡了种子部分。对策：确认上传范围只到种子之后；如果整段都上去了，检查是不是走了 `feedback/committed` 的快照路径。

5. 现象：某轮 token 显示为空。原因：这轮有 attempt 没报 usage，折叠直接返回 `undefined`。对策：查该 step 的 `assistant/attempt` 与 `assistant/message` 是否都带 usage。

6. 现象：缓存命中数忽高忽低。原因：`cacheReadTokens` 只在每个 attempt 都报时出现，重试链里一次缺失就让整轮消失。对策：按 step 看，不要按轮看。

7. 现象：一个超大工具结果把整条记录顶掉。原因：单条上限 `maxRequestBytes`，整条拒绝。对策：先看 `Session log record rejected` 的 warn，再考虑在 `tool/result` 侧做落盘。

8. 现象：进程退出卡住。原因：关停等待队列排空。对策：`shutdownTimeoutMillis` 是硬上限，超时后 `stopPending()` 清队列；不要把它设成无穷。

9. 现象：桌面端产品事件有了，Web 端没有。原因：两个插件都带 `profileContext?.name !== 'desktop'` 的 disabled 条件。对策：确认跑的是桌面构建。

10. 现象：日志里出现 `Session log record withheld: redaction removed sourceEvent`。原因：脱敏规则把整个 `sourceEvent` 删了。对策：规则里只改 `body` 与 `attributes`，保留 `sourceEvent`。

## 12. 源码索引

| 位置 | 证明什么 |
|------|----------|
| `packages/telemetry/otel/src/index.ts` | 工厂只给两个 reporter，挂载不建队列 |
| `packages/telemetry/otel/src/session-log.ts:174-196` | 队列 2048、批次 512、间隔 1000、导出超时 30 秒的默认值 |
| `packages/telemetry/otel/src/session-log.ts:198-212` | 属性只放身份，事件全文进 `content` |
| `packages/session/session-telemetry/src/index.ts:24-40` | waterfall 是脱敏扩展点，本包不带规则 |
| `packages/session/session-telemetry/src/coordinator.ts:179-199` | 记录六段与两次 `structuredClone` |
| `packages/session/session-telemetry/src/coordinator.ts:205-207` | `redact()` 的实现与兜底 |
| `packages/session/session-telemetry/src/coordinator.ts:268-300` | severity 只认两种 error，身份四件套 |
| `packages/session/session-telemetry-otel/src/index.ts:31-52` | 两种模式、`isFeedback` 的两道判断 |
| `packages/session/session-telemetry-otel/src/index.ts:144-176` | DISABLED 不建 provider，加载期校验 endpoint |
| `packages/session/session-telemetry-otel/src/index.ts:200-207` | 缺 `sourceEvent` 时扣下记录 |
| `packages/host/product-telemetry-otel/src/index.ts` | 产品事件队列、`x-channel` 头、关停 21 秒 |
| `packages/client/product-analytics/src/index.ts` | `enabled` 策略、环境收集 5 秒 / 16KB、missing_fields |
| `packages/client/product-analytics/src/events.ts` | 事件名与属性的封闭映射 |
| `packages/boot/app-boot/src/profile-context.ts` | `DSH_TELEMETRY_DISABLED` 任意非空即关 |
| `packages/llm/llm/src/types.ts:182-196` | `TokenUsage` 的 DISJOINT 语义 |
| `packages/llm/token-meter/src/index.ts:56-62` | 计费口径三项相加 |
| `packages/llm/token-meter/src/turn-usage.ts:136-147` | `every(isCount)` 才保留缓存桶 |
| `packages/session/session-stats/src/projection.ts:1-20` | 时间口径与取消 step 不计时的原因 |

## 最后

回到开头三个现象。没有 `startSpan`，是因为这里的观测单元是会话事件，事件比 span 活得久：span 随进程消失，事件写进日志可以重放。默认不传，是因为这些事件里有完整对话，传出去需要一次显式授权。token 不在属性里，是因为它跟着事件走，而事件有几十种形状。

如果只记一件事：把遥测设计成「对已有事实的搬运」，而不是「在代码里新插一层埋点」。前者在回放、离线分析、隐私边界上都更省事，代价是采集侧必须接受事件的原始形状。

> 自己搭 Agent 时：先定事件日志的 schema，再定遥测。反过来做，你会发现每个埋点都在补事件里已经有的字段。

上一篇：[PTC：把程序当工具执行](/posts/dsh-ptc-runtime.html)｜下一篇：[存储：SQLite、JSON 与迁移的代价](/posts/dsh-persistence-and-storage.html)
