---
title: "一个典型插件长什么样：拆开 DSH 的 token 计量插件"
date: 2026-11-17T17:05:00+08:00
summary: "以 dsh-token-meter 为例，拆解 DSH 插件的四个部件：服务、投影、监听器、客户端契约。重点说明记账为什么从日志重建、两种计价路径怎么选、以及宁可不报数的三条不变量。"
description: "用一个真实插件讲清 DSH 插件写法：服务注册、会话投影、事件监听、客户端契约；含架构图、状态机、时序图与基线决策树。"
tags: ["architecture", "harness", "billing"]
slug: "dsh-token-meter-plugin"
---

> 依据：DSH `0.2.2-rc.1`，commit `fcc0ad46`。结论来自 `packages/llm/token-meter/`（11 个源文件，1,625 行）与 `docs/subsystems/token-meter.zh.md`；行号以该 commit 为准。

## 0. 结论

如果你想把「插件」理解成「一个处理函数」，DSH 会把这种印象拆掉。以计费为例，一个插件通常是四样东西的组合：

| 部件 | 在 token-meter 里 | 作用 |
| --- | --- | --- |
| 服务 | `TokenMeter extends Service`，`ctx.tokenMeter` | 长期存在的读接口 |
| 投影 | 4 个 `ProjectionDefinition` | 把日志折叠成可检查点的状态 |
| 监听器 | 一个 `session/event` 监听 | 只给已读过的会话做增量 |
| 客户端契约 | `client.ts` 导出纯函数与类型 | 浏览器安全地复用同一套折叠 |

计费插件不拦截请求，也不在请求路径上算钱。它做的是：从已经落库的事件里，把每次 provider 调用花了多少 token、按什么路由计价，重建出来。

因此它天然可回放：同一个日志重放两次，得到的 token 数必须一样。

## 1. 先澄清：DSH 里没有 billing 包

仓库里搜不到 `packages/billing`。与「计费」对应的是 `@deepseek-ai/dsh-token-meter`，它只做两件事：

1. 归集 provider 报的 usage（输入、输出、缓存读、缓存写、推理）；
2. 按路由给当前 surface 定价（图片走适配器声明的视觉 token，文件走请求时的投影文本）。

它不扣款、不算钱、不查余额。上层计费系统应该拿它的输出当输入，再加价格表。这一点先说清楚，免得把「计量」当成「账单」。

## 2. 一个插件的四个部件

### 2.1 服务：长期对象，不是回调

```ts
export class TokenMeter extends Service {
  static Config: z<TokenMeterConfig> = z.object({}) as unknown as z<TokenMeterConfig>
  static inject = ['sessionProjections']

  protected async [Service.init](): Promise<void> {
    const ctx = this.ctx
    this.measurementProjectionHandle = await ctx.sessionProjections.register(measurementProjectionDefinition)
    await ctx.sessionProjections.register(tokenUsageProjectionDefinition)
    this.contextPressureProjectionHandle = await ctx.sessionProjections.register(contextPressureProjectionDefinition)
    await ctx.sessionProjections.register(contextBreakdownProjectionDefinition)

    ctx.on('session/event', (session) => {
      if (this.states.has(session)) this._sync(session)
    })
  }
}
```

三件事值得注意：

- `Service.init` 是注册点，不是构造函数。投影注册是异步的，必须在 init 里做。
- `static inject = ['sessionProjections']` 声明硬依赖；`ctx.get('llm')` 是可选依赖，没装也能跑（只是没有图片定价）。
- `ctx.on(...)` 是 effect，服务被卸载时监听器自动摘掉。

### 2.2 投影：把「折叠」变成可检查点的定义

四个投影各管一段：

| 投影 | 状态 | 消费者 |
| --- | --- | --- |
| `measurement` | 最近一次计量的锚点（header、节点、usage） | `measure()` |
| `tokenUsage` | 会话累计的四类 token 桶 | UI、统计 |
| `contextPressure` | 最近一次 prompt 大小与窗口容量 | 压缩决策 |
| `contextBreakdown` | 按来源拆分的占用 | 上下文面板 |

投影不是缓存：它带 `stateVersion` 和 schema，日志重放时会重新折叠；检查点只是加速。

### 2.2.1 为什么用投影，而不是每次全量重算

会话可能有几千条事件。如果每次读数字都从 `seq 0` 重放，UI 每刷一次就扫一遍日志，成本随会话长度线性增长。

投影把这件事变成增量的：注册之后，框架会把检查点与后续事件喂给同一个 `apply`，读的时候只推进到当前尾巴。

```ts
while (state.consumedEvents < session.seq) {
  const event = session.eventAt(SessionSeq(state.consumedEvents))!
  // 检查点存在时不重复折叠；否则用本地的同一份 apply
  const next = projected !== undefined ? undefined : measurementProjectionDefinition.apply(state.pendingMeasurement, event)
  const measurement = next ?? projected
  if (measurement?.invalidSeq === event.seq) {
    if (event.type === 'assistant/message') planSurfaceTokens(state.surface, event)
    advanceMeasurement(measurement, event)
  }
  this._foldEvent(state, event)
  if (next !== undefined) state.pendingMeasurement = next
  state.consumedEvents = SessionLogOffset(state.consumedEvents + 1)
}
```

投影和本地折叠是同一份定义。差别只在「谁来推进」：有检查点时框架推进，没有时服务自己推。

### 2.3 监听器：只服务「已经有人读过」的会话

```ts
ctx.on('session/event', (session) => {
  if (this.states.has(session)) this._sync(session)
})
```

注释写得很直白：读过的会话，事件到达时增量跟上；没人读过的会话，不建状态。这样常驻内存的成本跟「被观察的会话数」成正比，而不是跟「进程里出现过的会话数」成正比。

### 2.4 客户端契约：同一套折叠，浏览器也能跑

`src/client.ts` 只导出纯函数与类型：

```ts
export type * from './projection.ts'
export { deriveTurnTokenUsage } from './turn-usage.ts'
```

`deriveTurnTokenUsage(events)` 不依赖 Cordis、不读时钟、不发请求，所以服务端和浏览器可以各算一遍，结果一致。

### 2.5 挂载与配置

服务包在 `package.json` 里声明 `cordis` 入口，profile 的 bundle 把它加进插件列表；`Context` 上就会出现 `tokenMeter`。

配置是空的，而且**不认识的键会被拒绝**：

```ts
function validateConfigKeys(config: TokenMeterConfig): void {
  for (const key of Object.keys(config)) {
    throw new Error(`TokenMeterConfig: unknown key "${key}" (no settings are supported)`)
  }
}
```

为什么对一个「没有配置」的服务这么严？因为拼错的键会被默认值悄悄盖住：用户以为设了 `retentionDays`，实际什么都没发生。早失败比静默生效好。

### 2.6 测试形态

`packages/llm/token-meter/tests/` 有 6 个 spec，按「输入是什么」分层：

| spec | 输入 | 断言什么 |
| --- | --- | --- |
| `token-meter.spec.ts` | 完整会话 | `measure()` 的总量与节点 |
| `route-pricing.spec.ts` | 节点 + 图片定价 | 每个节点的价与总价 |
| `turn-usage.spec.ts` | 一段 turn 事件 | 精确账或 `undefined` |
| `token-usage-projection.spec.ts` | 投影序列 | 四桶累计 |
| `measurement-projection.spec.ts` | 锚点序列 | header 序号与 delta |
| `context-breakdown-projection.spec.ts` | 来源事件 | 拆分正确 |

它们的共同点是只喂事件，不 mock 服务、不读时钟。这是「可回放」在测试层的兑现。

## 3. 数据从日志的哪一处来

{{< fig "diagrams/01-plugin-anatomy.svg" "插件的四个部件：会话日志是唯一输入，四个投影与一个服务把事件折叠成可读状态，客户端与服务端共用同一套纯函数。" >}}

计费需要的全部事实都在持久事件里：

| 事件 | 提供什么 |
| --- | --- |
| `assistant/message` | `usage`（provider 报的桶）、内嵌 stream 的最后一帧 usage、`source` 里的 provider/model |
| `assistant/attempt` | 失败尝试的 usage（用于重试累加） |
| `llm/retry` / `llm/retry-started` | 一次失败与下一次尝试的边界 |
| `request/header` | 这次请求的路由与参数（决定图片/文件怎么计价） |
| `image/offload` | 哪些图片被卸载成了引用 |
| surface 事件 | 当前消息节点，用于按位置定价 |

没有一条来自「运行时内存里的计数器」。这是刻意的：进程重启后，计量必须还能算出来。

## 4. 两条计价路径

### 4.1 精确路径：provider 说多少就是多少

`normalizeUsage` 对 usage 做严格校验，任何一条不满足就整条作废：

- `inputTokens` / `outputTokens` 必须是非负安全整数；
- `reasoningTokens` 不得大于 `outputTokens`；
- 若给了 `totalTokens`，则 `totalTokens - outputTokens` 不得小于已知 prompt 总量；
- 若缓存读、缓存写都给了，精确 prompt 必须等于三者之和。

这不是吹毛求疵。计费数字一旦自相矛盾，宁可不算，也不能算出一个「大概对」的数。

### 4.2 估算路径：没有 usage 时的保守值

`estimate.ts` 用固定启发式给消息定价；图片与文件会替换掉结构占位，改用路由声明的投影：

```ts
const prices = pricing === undefined ? [] : pricing.priceImages(images)
if (pricing !== undefined && prices.length !== images.length) {
  throw new Error(`token meter: route image pricing answered ${prices.length} prices for ${images.length} occurrences`)
}
```

数量对不上直接抛错。定价器返回少一个价格，会让后面每个节点都错位，这种错必须响，不能吞。

### 4.3 五个桶的语义

| 桶 | 含义 | 计费时的坑 |
| --- | --- | --- |
| `inputTokens` | 未命中缓存的输入 | 有的 provider 把它写成「总输入」，需要靠 `totalTokens` 反推 |
| `cacheReadTokens` | 命中缓存的输入 | 单价与普通输入不同，不能合并 |
| `cacheWriteTokens` | 写入缓存的输入 | 首次请求才有，重试可能重复计 |
| `outputTokens` | 输出总量 | 含推理 token |
| `reasoningTokens` | 输出中的推理子集 | 只能小于等于输出，不能额外加 |

`usageTokens()` 的实现是四个桶相加，但 `reasoningTokens` 不参与相加：它是 `outputTokens` 的子集，再加一次就是重复计费。

```ts
function usageTokens(usage: TokenUsage): number {
  return usage.inputTokens
    + (usage.cacheReadTokens ?? 0)
    + (usage.cacheWriteTokens ?? 0)
    + usage.outputTokens
}
```

### 4.3 什么时候信 usage，什么时候退回估算

{{< fig "diagrams/02-baseline-decision.svg" "基线选择：只有 header 完全一致、且 usage 不低于全量启发式价时，才把 provider usage 当锚点；否则退回估算并重新定价。" >}}

`measure()` 的判断可以压成三条：

1. 有锚点且 header 与锚点一致 → 比较两种价；
2. usage 总量 ≥ 锚点的完整启发式价 → `baseline.kind = 'usage'`；
3. 否则 → `baseline.kind = 'estimated'`，并对整个 surface 重新定价。

第二条是保守性来源：只有当 provider 报的数不小于我们自己的估算，才承认它；否则说明这次调用有我们没建模的部分（比如服务端截断、图片被替换），此时估算更安全。

## 5. 一次 turn 的记账状态机

{{< fig "diagrams/03-attempt-state.svg" "attempt 的生命周期：step/start 打开一次尝试，assistant/message 或 llm/retry 结算，llm/retry-started 再打开下一次；任何越界都整轮作废。" >}}

`deriveTurnTokenUsage` 只接受严格相邻的事件序列：

```
turn/start → step/start → assistant/message → step/end → turn/end
```

重试会插入 `assistant/attempt`、`llm/retry`、`llm/retry-started`：

```
step/start → assistant/attempt → llm/retry → llm/retry-started → assistant/message → step/end
```

`assistant/attempt` 先把当前尝试结算掉，`llm/retry` 把它标记为 `settled(by:'retry')`，`llm/retry-started` 再开一条新的。缺任何一个边界，函数返回 `undefined`。

这就是「宁可不报」：`TurnTokenUsage` 的字段要么全有，要么整个对象不存在。

### 5.1 聚合规则：可选桶与路由

一个 turn 可能有多次 attempt（重试），每家的 usage 字段并不齐：

- 必选桶（输入、输出、总量）每个 attempt 都必须有，否则整轮作废；
- 可选桶（缓存读、缓存写、推理）只有**所有** attempt 都报了才给，否则整体省略；
- `routes` 只有所有 attempt 都带 provider/model 归因时才给，并按 `provider\0model` 去重。

```ts
const cacheReadTokens = cacheRead.every(isCount) ? safeSum(cacheRead) : undefined
```

这条规则避免「用 0 补齐缺失桶」把账算小。缺就是缺，不能当成零。

所有求和都走 `safeSum`：累加过程中一旦超过安全整数，返回 `undefined`，整轮作废。

### 5.2 一个 turn 里换了模型

同一个 turn 里可以切路由（比如主模型失败后换备用）。这时 turn 的账要按 attempt 分别记，再合并：

- 每个 attempt 的 `route` 来自 `assistant/message.source` 的 `provider`/`model`；
- 合并时按 `provider\0model` 去重，得到一个 `routes` 数组；
- 桶是跨路由相加的，所以「总 token」不能直接乘某一个单价。

`TurnTokenUsage` 给的是用量，不是金额。金额由上层拿到每条路由的单价后，按桶分别乘再加。

## 6. 时序：从 provider 返回到像素

{{< fig "diagrams/04-measure-sequence.svg" "一次请求的记账时序：provider 帧只带瞬时 usage，assistant/message 落库后投影才推进，UI 读的是投影快照。" >}}

关键在「什么时候可见」：

- 流式帧里的 usage 不落库，只用于结算时写进 `assistant/message`；
- `assistant/message` 落库后，投影才推进；
- UI 读 `stateOf(session)`，读到的是一个不可变快照，带 `logRevision`。

`logRevision` 是「这个数字基于多少条事件」。它让「这个数是什么时候的」变成可回答的问题，而不是靠时间戳猜。

### 6.1 measure() 的成本

`measure()` 每次都会克隆整份 surface 节点并 `deepFreeze`：

```ts
return deepFreeze(structuredClone({
  logRevision: state.consumedEvents,
  baseline,
  surfaceDeltaTokens,
  totalTokens: Math.max(0, baseline.tokens + surfaceDeltaTokens),
  surfaceTokens: surface.surfaceTokens,
  nodes: surface.nodes,
}))
```

注释里写明这是 O(surface)。所以它适合「打开面板时读一次」，不适合每帧调用。需要高频读的消费者（比如顶栏计数器）应该读投影，而不是反复 `measure()`。

`Math.max(0, ...)` 也不是装饰：锚点可能是旧的，delta 可能为负，但总量不能是负的。

一个具体的读数例子（数字是示意，不是实测）：

| 字段 | 值 | 来源 |
| --- | --- | --- |
| `baseline.kind` | `usage` | 上次调用的 header 与当前一致 |
| `baseline.tokens` | 12,400 | provider 报的输入 + 输出 |
| `surfaceDeltaTokens` | +320 | 当前 surface 比锚点多出的消息 |
| `totalTokens` | 12,720 | 两者相加 |
| `surfaceTokens` | 11,980 | 按当前路由给每个节点定价的和 |
| `logRevision` | 1,842 | 消费到的事件数 |

`baseline.tokens` 与 `surfaceTokens` 是两个不同的口径：前者是「provider 说这次请求花了多少」，后者是「按当前 surface 与路由重算的值」。它们不必相等，差值正是 `surfaceDeltaTokens` 要解释的东西。

## 7. 回放与 checkpoint

`_sync()` 是从日志尾部追赶的循环。它有一个容易忽略的分支：

```ts
if (projected !== undefined && (projected.headerSeq !== (state.header?.seq ?? null)
  || (projected.anchor !== null && projected.anchor.headerSeq !== (state.anchorHeader?.seq ?? null)))) {
  this.states.delete(session)
  return this._sync(session, false)
}
```

检查点记录的是 header 与锚点的序号。如果它和重放出来的缓存对不上，说明检查点不是这份日志的，直接丢掉从头算。

这条分支保证了一件事：**投影可以快，但不能「快错」**。快照与重算不一致时，重算赢。

## 8. 写自己的计费插件，要抄的五件事

1. 只读日志。任何进入账单的数字都必须能从 `SessionEvent` 重建；内存计数器只能当缓存。
2. 区分「精确」与「估算」，并且在输出里显式标注（`baseline.kind`）。下游需要知道这个数能不能拿去结算。
3. 整轮作废，而不是部分扣减。缺一条边界就返回 `undefined`，比返回一个少算的数安全。
4. 把检查点当加速器，不当真相。校验不过就重算，代码路径要真的存在。
5. 纯函数放 client 侧。服务端与浏览器各算一遍还能一致，说明折叠没有隐藏状态。

### 8.1 怎么给新消费者加接口

两条路，按「要不要历史」选：

| 需求 | 做法 |
| --- | --- |
| 只要当前值（顶栏、面板） | 读投影 `stateOf(session)`，O(1) |
| 要按路由重新定价、要 delta | 调 `measure(session, header?)`，O(surface) |
| 要一次 turn 的精确账 | 对 turn 区间调 `deriveTurnTokenUsage(events)`，纯函数 |

不要自己遍历事件再拼一个「差不多」的和。一旦两处实现，回放时就会给出两个数。

### 8.2 从零写一个最小计量插件

不必一上来就做四个投影。最小可用版本只要一个纯函数 + 一个读取口：

```ts
// 1) 纯函数：吃事件，吐数字
export function totalTokensOf(events: readonly SessionEvent[]): number | undefined {
  let total = 0
  for (const event of events) {
    if (event.type !== 'assistant/message') continue
    const usage = event.data.usage ?? lastAssistantStreamChunk(event.data.stream, 'usage')?.usage
    if (usage === undefined) return undefined
    total += usage.inputTokens + usage.outputTokens
  }
  return total
}

// 2) 服务：只读会话，不缓存
export class MyMeter extends Service {
  static inject = ['sessions']
  total(session: Session): number | undefined {
    return totalTokensOf(session.events())
  }
}
```

先让它对，再让它快：投影、检查点、增量同步都是第二步。顺序反了，你会得到一个又快又错的实现。

## 9. 设计取舍

| 取舍 | 选择 | 代价 |
| --- | --- | --- |
| 计价时机 | 读时按日志重算 | 每次 `measure()` 是 O(surface)，换来回放一致 |
| 数据来源 | 只信 provider usage 与日志 | 没有 usage 时只能估算，精度下降 |
| 错误处理 | 矛盾即整轮作废 | 有些本可「大概算」的场景拿不到数 |
| 状态归属 | 投影挂在会话上 | 进程内要按会话数建状态，但可随会话回收 |
| 客户端复用 | 纯函数导出 | 需要把依赖严格限制在 `SessionEvent` |

## 10. 踩坑点

1. 不要把流式帧的 usage 当最终值。它可能只是最后一帧的采样，结算以 `assistant/message.usage` 为准。
2. 重试不是新请求。同一个 step 的多次 attempt 要累加，但不能把 `llm/retry` 当成 turn 边界。
3. 检查点的 headerSeq 必须与日志对齐。对不上就重算，否则会把旧路由的价格套到新路由上。
4. 图片定价按出现次数对齐。少一个价格就抛错，别用「差不多」补。
5. `contextPressure` 与计费不是一回事。前者是 prompt 大小，后者是计费桶；压缩看前者，账单看后者。
6. 别把 `totalTokens` 当唯一真值。缓存读/写的价格与普通输入不同，分桶才是可用的计费输入。
7. `reasoningTokens` 不能二次相加。它是输出子集，单独计价会让账单虚高。
8. 缺桶不要补零。可选桶只在全量上报时才出现，补零会把「不知道」写成「没有」。
9. 检查点不是真相。校验失败时必须真的走重算分支，否则旧路由的价格会套到新路由上。

## 11. 源码索引

| 位置 | 内容 |
| --- | --- |
| `packages/llm/token-meter/src/index.ts:83-296` | `TokenMeter` 服务、`measure()`、`_sync()`、`_foldEvent()` |
| `packages/llm/token-meter/src/turn-usage.ts:1-274` | `deriveTurnTokenUsage` 与 attempt 状态机 |
| `packages/llm/token-meter/src/route-pricing.ts:34-76` | `priceSurface` 与图片/文件投影 |
| `packages/llm/token-meter/src/estimate.ts` | 固定启发式定价 |
| `packages/llm/token-meter/src/usage-projection.ts:1-213` | `tokenUsage`、`contextPressure` 折叠 |
| `packages/llm/token-meter/src/measurement-projection.ts` | 锚点与 header 序号 |
| `packages/llm/token-meter/src/client.ts` | 浏览器安全导出 |
| `docs/subsystems/token-meter.zh.md` | 公开类型与语义 |
| `packages/llm/token-meter/src/usage-projection.ts:56-213` | 四桶累计与替换式更新 |
| `packages/llm/token-meter/tests/*.spec.ts` | 6 个 spec：计量、路由定价、轮次账、用量、拆分、上下文压力 |
| `packages/llm/token-meter/src/breakdown-projection.ts` | 按来源拆分占用 |

---

系列导航：[上一季索引](/posts/dsh-season-four.html) · 相关：[会话事件溯源](/posts/dsh-session-event-sourcing.html)、[请求组装](/posts/dsh-request-assembly.html)、[压缩](/posts/dsh-compaction.html)
