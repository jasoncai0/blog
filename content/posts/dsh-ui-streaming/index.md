---
title: "流式界面：一帧如何走到屏幕"
date: "2026-11-16T22:15:00+08:00"
summary: "模型吐出的每个 chunk 要经过紧凑落盘、重连基线、客户端合流、分页窗口和三层渲染批处理，才会变成屏幕上的字。逐个拆开这些交接点。"
description: "从 StreamChunk 到 assistant-stream，再到客户端 reducer、窗口、滚动锚定与渲染节流，记录 DSH 流式链路的真实实现与取舍。"
tags: ["architecture", "harness", "agent"]
slug: "dsh-ui-streaming"
---
> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）。落盘格式、attempt 帧、重连基线与客户端 reducer 的结论来自 `packages/llm/llm`、`packages/core/agent-loop`、`packages/api/session-controller`、`packages/api/gateway` 的源码；窗口与滚动行为来自 `packages/client/ui-chat` 源码和包内 README；批处理来自 `packages/client/ui-conversation` 与 `ui-chat`。行号按该 commit 核对。本文没有做浏览器端到端计时，涉及帧数与延迟的判断只描述代码里的调度方式。

一条 assistant 回复在屏幕上增长时，背后至少有五次交接：模型适配器吐出 chunk，agent loop 把 chunk 折成 attempt，attempt 提交成会话事件，session-controller 把它压成重连基线，客户端 reducer 再把 transient 行和 durable 行合成一个列表。任何一处假设错位，表现出来都是「字重复了」「字少了」「重连后从半句开始」。

下面按数据流的顺序走一遍，最后停在渲染节流和滚动锚定。

## 1. 七个 chunk 类型

模型适配器不直接产出字符串，产出的是带 block 索引的协议帧（`packages/llm/llm/src/types.ts:496`）：

```ts
export type StreamChunk =
  | { type: 'block-start'; index: number; blockType: ContentBlockType }
  | { type: 'text-delta'; index: number; text: string }
  | { type: 'reasoning-delta'; index: number; text: string }
  | { type: 'tool-call-delta'; index: number; id: ToolCallId; name?: string; argumentsDelta: string }
  | { type: 'block-end'; index: number; block: ContentBlock }
  | { type: 'usage'; usage: TokenUsage }
  | { type: 'finish'; reason: FinishReason; replayState?: ReplayEnvelope }
```

`index` 关联交错的 block，`block-end` 携带已经拼好的块，工具参数保持原始 JSON 字符串。适配器可以抛错，`LlmRuntime.stream()` 会把失败归一成终止的 `error` 或 `aborted` finish，调用方拿到的流一定有终点。

| 类型 | 是否可见 | 落盘方式 |
|---|---|---|
| `block-start` | 建块 | 原样记录 |
| `text-delta` | 可见文本 | 合并成 text run |
| `reasoning-delta` | 推理文本 | 合并成 reasoning run |
| `tool-call-delta` | 工具参数 | 合并成 tool-call run |
| `block-end` | 定稿块 | 原样记录 |
| `usage` | 状态栏 | 原样记录 |
| `finish` | 终止原因 | 原样记录 |

只有三种 delta 会被合并。其余四种每次出现都留一条原始记录，因为它们的边界本身带语义：块从哪开始、到哪结束、usage 属于哪一段。

{{< fig "diagrams/01-frame-pipeline.svg" "同一个 chunk 分三路：紧凑记录、块组装、实时帧，客户端再按 seq 合流" >}}

## 2. 一次 attempt 产出三样东西

`AssistantStreamAttempt` 是 agent loop 里的一次模型请求（`packages/core/agent-loop/src/assistant-stream.ts:17`）。它同时维护一个紧凑累加器、一个块组装器和一个帧序号：

```ts
push(chunk: StreamChunk): void {
  const timed = this.accumulator.push({ time: Date.now(), chunk })
  this.assembler.push(timed.chunk)
  this.emit({
    type: 'chunk', attemptId: this.attemptId, revision: this.nextRevision(),
    index: this.index++, time: timed.time, chunk: timed.chunk,
  })
}
```

同一个 chunk 只快照一次，然后喂给三条路：落盘用的紧凑记录、最终消息用的块组装、实时界面用的帧。三者的时间戳相同，避免重连后时间线对不上。

终止分两种。正常结束调用 `settle()`，先同步 append 会话事件，拿到 seq 之后再发 `end` 帧；append 抛错时转 `abandon()`，发一个没有事件引用的终止帧。这样客户端看到 committed 时，对应事件一定已经在日志里：

```ts
settle(eventType: 'assistant/message' | 'assistant/attempt', append: () => SessionSeq): void {
  let seq: SessionSeq
  try {
    seq = append()
  } catch (error: unknown) {
    this.abandon()
    throw error
  }
  this.terminal = true
  this.emit({ type: 'end', attemptId: this.attemptId, revision: this.nextRevision(),
    index: this.index, outcome: { kind: 'committed', eventType, seq } })
}
```

`attemptId` 是 `sessionId:attempt`，只在当前 Agent 生命周期内唯一。进程重启后计数从零开始，所以跨进程识别 attempt 不能只看这个字符串。

## 3. 落盘：不合并 delta 的边界

会话里的 `assistant/message` 带一个 `stream` 字段（`packages/core/session/src/types.ts:349`），注释写的是「Exact timed model stream, compacted without joining delta boundaries」。它存的是紧凑记录，不是拼好的全文：

```ts
export type AssistantStreamRecord =
  | { type: 'text-chunks'; time0: number; index: number; dt: readonly number[]; texts: readonly string[] }
  | { type: 'reasoning-chunks'; time0: number; index: number; dt: readonly number[]; texts: readonly string[] }
  | { type: 'tool-call-chunks'; time0: number; index: number; dt: readonly number[]
      id: ToolCallId; name?: string; args: readonly string[] }
  | { type: 'chunk'; time: number; chunk: StreamChunk }
```

一条 run 存首帧时间 `time0`、block 索引、相邻帧的间隔数组 `dt`、以及每帧的文本或参数片段。`dt` 长度比成员数少一，这个不变量在展开时校验。

没有产出最终消息的 attempt 落成另一种事件（`packages/core/session/src/types.ts:363`）：

```ts
| { type: 'assistant/attempt'; turn: number; step: number; stream: AssistantStreamRecord[] }
```

它只有流，没有 `message`。中断、适配器报错、上游超时都走这条路径。这样重放时仍能看到模型已经吐出的部分，界面也能据此画出「未完成」的节点，而不必把它伪装成一条正常回复。

合并条件很窄（`packages/llm/llm/src/assistant-stream.ts:117`）：

```ts
const gap = previous !== undefined && previous.type === type ? safeGap(previous.lastTime, time) : undefined
if (previous !== undefined && previous.type === type && previous.index === chunk.index && gap !== undefined) {
  previous.dt.push(gap)
  previous.texts.push(chunk.text)
  previous.lastTime = time
} else {
  this.records.push({ type, time0: time, index: chunk.index, dt: [], texts: [chunk.text], lastTime: time })
}
```

类型相同、索引相同、时间差还能被安全整数精确表示，三个条件同时成立才续写。工具调用还要求 id 相同、name 的有无和值一致。空 id 或空 name 的 tool-call-delta 直接落成原始记录，因为后续 delta 无法可靠归并。

展开走 `expandAssistantStream()`，它在持久化边界上校验键集合、时间戳、`dt` 长度，出错抛 `TypeError`，消息里带具体记录。读取方默认信任静态类型，只有跨进程序列化回来的数据才走这条校验路径。

## 4. 重连基线：revision 与 startedAfterSeq

Host 侧的 `SessionAssistantStreamAccumulator` 只保留一个活动 attempt（`packages/api/session-controller/src/assistant-stream.ts:27`）。它把 Agent 帧折成一份可序列化的基线，重连时直接发给客户端：

```ts
accept(frame: AssistantStreamFrame, durableCursor: SessionSeqCursor): void {
  if (frame.type === 'start' && frame.revision === 1 && this.revision !== 0) {
    this.activeAttempt = undefined
    this.revision = 0
  }
  if (frame.revision !== this.revision + 1) {
    this.activeAttempt = undefined
    this.revision = frame.revision
    this.dirty = true
    return
  }
  this.revision = frame.revision
  // start 建 attempt，chunk 校验 attemptId 与 index，end 清空
}
```

revision 是每个 Agent 生命周期内的连续序号。收到跳号时，它清掉活动 attempt，接受新 revision，然后等下一帧。丢掉的帧不会补发，因为基线只承诺「从当前 revision 之后完整」。

`start` 帧里的 `startedAfterSeq` 记录收到该帧时最后一个已提交的会话 seq。客户端用它判断哪些 durable 事件发生在 attempt 之前，避免把旧消息当成这次 attempt 的产物。

基线只在 dirty 时重建，其余时候返回同一个对象。订阅方用引用相等判断有没有变化，这条约束省掉了每帧的深比较。

快照的 JSON 形状大致是：

```json
{
  "revision": 42,
  "activeAttempt": {
    "attemptId": "sess_01J:3",
    "startedAfterSeq": 1180,
    "turn": 7,
    "step": 2,
    "nextIndex": 96,
    "stream": [
      { "type": "text-chunks", "time0": 1760000000000, "index": 0, "dt": [12, 9], "texts": ["你", "好"] }
    ]
  }
}
```

没有 `activeAttempt` 表示当前没有进行中的 attempt，客户端收到后清掉所有 transient 行。`nextIndex` 与 `stream` 里的成员数不相等：只有三种 delta 进入 run，`block-start`、`block-end`、`usage`、`finish` 都以原始记录保存，但它们同样占用帧序号。重连后从 `nextIndex` 续接，收到的第一个 chunk 只要不是这个值，就重开窗口。

## 5. 客户端：transient 与 durable 的合流

浏览器只看到两类行：durable 的会话事件，和 `assistant/live-chunk` 合成行。`ClientAssistantStream` 负责把它们排进同一个序列（`packages/api/session-controller/src/client/sessions/assistant-stream.ts:44`）。

合成行的 seq 用分数表示，落在下一条 durable 事件之前：

```ts
seq: this.durableCursor + 1 - 1 / (this.transientInGap + 1)
```

`transientInGap` 每加一行递增，分数越来越接近 `durableCursor + 1`，但永远小于它。这样排序稳定，也不会和真实 seq 撞上。

实时帧的处理集中在 `acceptFrame()`：

```ts
case 'chunk': {
  const attempt = this.activeAttempt
  if (attempt === undefined || attempt.attemptId !== frame.attemptId) return undefined
  if (frame.index !== attempt.nextIndex) return { type: 'rebaseline' }
  attempt.nextIndex += 1
  this.transientInGap += 1
  return { type: 'transient', entry: { /* assistant/live-chunk */ } }
}
```

三种情况要区分。控制器在 attempt 中途挂载时没有 start 帧，此时未知 attempt 的 chunk 直接忽略，durable 结算仍会正常到达。attempt 已知但 index 跳号，说明中间丢帧，返回 `rebaseline` 让上层重开窗口。收到 `end` 时，客户端先去 `pending` 里找同 seq、同类型的 durable 结算；找不到也重开。

成功消息的 transient 行不会立刻撤掉。它保留到所属 step 的 `step/end` 到达，避免 durable 消息渲染前的空档。中断消息、失败的 attempt、abandoned 帧则立即撤行。

| 输入 | reducer 结果 | 界面动作 |
|---|---|---|
| `start`，本地已有 attempt | rebaseline | 丢弃本地行，重开窗口 |
| `chunk`，attempt 未知 | 忽略 | 等 durable 结算到达 |
| `chunk`，index 跳号 | rebaseline | 重开窗口 |
| `end`，没有匹配的 pending | rebaseline | 重开窗口 |
| `end`，outcome 为 abandoned | abandonment | 撤掉 transient 行 |
| committed，pending 中事件类型一致 | settlement | 保留到 `step/end` |
| durable 事件 seq 重复 | rebaseline | 重开窗口 |

rebaseline 的语义是「本地状态不可信，从 Host 重新取一份」。它不尝试修补缺口，因为缺口的形状未知：可能少一帧，也可能少一段 durable 事件。

自己搭的时候，先把「实时行」和「已提交行」建成两个独立来源，再在一个 reducer 里合并。把 live chunk 直接写进 durable 列表，重连时会出现半条消息，清理逻辑也会变得很难写。

## 6. 窗口：Turn 对齐的 50 条

首屏不会加载整个会话。服务端按消息数和 turn 边界切页（`packages/api/session-controller/src/history.ts:132`）：

```ts
for (let index = end - 1; index >= 0; index--) {
  const event = events[index] as SessionEvent
  if (turnWindow !== undefined && event.type === 'turn/start') {
    turns++
    if (count >= turnWindow.minMessages && turns >= turnWindow.minTurns) {
      cut = SessionLogOffset(index); break
    }
  }
  if (!MESSAGE_TYPES.has(event.type) || !isAppendSurfaceEvent(event)) continue
  count++
  // sourceEventSeqs 把成组事件回退到组起点
  if (count >= maxMessages) { cut = SessionLogOffset(groupStart); break }
}
```

默认 `DEFAULT_MAX_MESSAGES = 50`（`:25`）。客户端请求的是「至少 50 条消息、两个 turn start，最多 500 条」，这样向上翻页不会翻到半个 turn（`client/sessions/session.ts:54`）：

```ts
const HISTORY_PAGE_OPTIONS = { maxMessages: 500, turnWindow: { minMessages: PAGE_MESSAGES, minTurns: 2 } }
```

`loadOlder()` 只取一页；`loadThrough(seq)` 为跳转服务，循环向前翻页，最后只发一次 `prependWindow()`。中间页面先堆在 `pending` 里，避免每页都触发一次列表重排。

窗口本身是一棵拼接树，不是数组（`client/contract/events.ts:120`）。prepend 只是新建一个 concat 节点：

```ts
prepend(entries: readonly SessionEventLikeEntry[], hasMore: boolean): void {
  this.window = concat(leaf(entries), this.window)
  this.publish(hasMore, { kind: 'prepend', entries })
}
```

append 同样不复制。快照在第一次读取时物化并缓存，后续追加只更新版本号。只有 `settleAssistant()` 需要过滤 transient 行，它会物化一次再重建叶子。

分页计数只算 `user/message` 与 `assistant/message` 两类事件，且只算 append 表面上的事件。工具结果、状态变更、stream 记录都不占名额，所以「50 条消息」对应的日志长度可能远大于 50。切点落在成组事件中间时，`sourceEventSeqs` 会把起点回退到组头，避免把一次工具调用的调用和结果拆到两页。

`maxMessages: 500` 是硬上限，`turnWindow.minTurns` 是另一个停止条件。一个 turn 里塞了几百条工具消息时，按 turn 对齐会切出很大的页；上限保证翻页请求不会无限增长。`hasMore` 只表示切点之前还有内容，不表示下一页一定非空，因为成组回退可能让两页重叠。

{{< fig "diagrams/02-window-model.svg" "日志在服务端按 Turn 对齐切页，客户端用 concat 节点做 O(1) 的前置拼接" >}}

## 7. 断线重连：两条独立的游标

重连要同时对齐两样东西：日志游标和 assistant revision。`SessionEventStream.follow()` 打开时显式要求基线（`client/transport.ts:172`）：

```ts
follow({ activate: true, assistantStream: true, ...this.repairRequest(request) }, signal)
```

快照里没有 `assistantStream` 就抛 `RemoteError('gateway/internal', 'session assistant stream omitted its opted-in opening baseline')`，因为请求已经声明要它了。随后每个 assistant 帧的 revision 必须等于上一个加一，否则抛 `RemoteStreamCarrierError`：

```ts
if (frame.revision !== assistantRevision + 1) {
  throw new RemoteStreamCarrierError(`session assistant stream skipped revision ${assistantRevision + 1}`)
}
```

这条错误由传输层接住，等连接换代后用 resume 游标重新 open（`gateway/src/client/remote-stream.ts:133`）。它不复用旧连接，也不尝试补齐缺失帧。

日志游标由 `RemoteJournalStream` 管（`gateway/src/client/journal-stream.ts:305`）。收到的 entry 起点必须紧接上一条，跳号时走 `replaceThrough()`：一边保持 follow，一边读一页，把等待中的 live 帧合并进替换窗口，最后只发一次 `replace`，再回放排队的通知。重连请求复用原来的分页预算（`client/transport.ts:239`），只换游标范围：

```ts
protected override repairRequest(request: ClientSessionPageRequest): ClientSessionPageRequest {
  return {
    ...(request.maxMessages === undefined ? {} : { maxMessages: request.maxMessages }),
    ...(request.turnWindow === undefined ? {} : { turnWindow: request.turnWindow }),
  }
}
```

自己搭的时候，把「日志缺口」和「帧缺口」分开处理。日志缺口用一次替换窗口修复，帧缺口用一次 rebaseline 重建；混在一个重试逻辑里，会出现窗口已换、attempt 还停在旧 revision 的状态。

{{< fig "diagrams/03-reconnect.svg" "日志缺口与帧缺口各自修复：一个换窗口，一个换基线" >}}

## 8. 虚拟列表：只虚拟导航条

打开一个长会话，最容易想到的是把 transcript 虚拟化。DSH 没有这么做。`ChatFlow` 把每条 entry 映射成一行（`packages/client/ui-chat/src/client/chat/ChatFlow.tsx:52`），行数和窗口里的 entry 数一致。

虚拟化出现在 turn rail（`TurnNavigator.tsx:110`）：

```tsx
const virtualizer = useVirtualizer<HTMLDivElement, HTMLButtonElement>({
  count: items.length,
  enabled: items.length >= 2,
  directDomUpdates: true,
  directDomUpdatesMode: 'transform',
  useScrollendEvent: true,
  estimateSize: () => TURN_SPACING_PX,
  measureElement: () => TURN_SPACING_PX,
})
```

rail 的每一项是固定间距的刻度，高度可预测，适合用 transform 直接改 DOM。transcript 的行高度由 markdown、代码块、折叠组决定，无法在不测量的情况下估算；再叠上滚动锚定和阅读位置恢复，虚拟化的收益抵不过复杂度。

窗口上限替它兜底。一次只加载 50 到 500 条消息，向上翻页按 turn 对齐，所以 DOM 行数有界。turn rail 则可能覆盖未加载的 turn，靠 host 的 `turnOutline` 投影补齐，点未加载的刻度时先按 `turn/start` 的 seq 翻页，再落位。

| 区域 | 是否虚拟化 | 原因 |
|---|---|---|
| transcript 行 | 否 | 行高由 markdown、代码块、折叠组决定，锚定按可见行计算 |
| turn rail | 是 | 刻度固定间距，只渲染视口附近的按钮 |
| trajectory 表 | 是 | 行数多，单行高度一致 |

同一份界面里三种策略并存，判断依据是「高度是否可预测」和「是否参与滚动锚定」。两个条件任一不满足，虚拟化都会把复杂度转移给测量和恢复逻辑。

## 9. 滚动锚定：跟随与阅读位置

流式输出时，界面要判断用户是在「跟随尾部」还是「停在上面读」。视口代码把能表达阅读意图的输入列成一张表（`use-chat-viewport.ts:71`）：

```ts
const READING_INTENTS = ['wheel', 'touchstart', 'pointerdown', 'keydown', 'beforematch']
```

用户滚动后，阅读参考是当前第一个可见行，而不是滚动偏移。内容在下方增长时，参考行保持屏幕位置；用户回到接近底部，跟随重新接管。

浏览器的原生 scroll anchoring 只在跟随尾部时关掉（`ui-chat/README.md:147`）。关闭范围限定在 transcript 的 scrollport，不影响其他滚动容器。跟随状态下收到固定位置的更新，会立即更新跟随目标；其他阅读移动先挂起，等采样间隔或 `scrollend` 再结算，这样一小段手势不会被布局增长抹掉。

折叠和流式增长共用一个帧时钟（`README.md:155`）。一批行在同一帧里关闭或展开，动画期间不读取行几何；每行从自己已有的尺寸继续，不在批处理中间重新测量。

跟随逻辑单独收在 `ScrollFollow` 里（`use-scroll-follow.ts:22`）。它维护 nearBottom 阈值、采样、结算、跳转和动画中标志，组件只负责把输入事件和尺寸变化喂进去。这样做的好处是跟随判断不散落在 effect 里，滚动事件、流式追加、折叠三处不会各写一套阈值。

## 10. 批处理：三帧、三个优先级、两块尾巴

一条 chunk 到达浏览器后，不会立刻触发 React 渲染。第一层是 notifier（`client/sessions/notifier.ts:31`）：`markDirty()` 排一个 microtask，`markFrameDirty()` 排一个 `requestAnimationFrame`，同一时刻只保留一个调度，generation 计数丢弃过期回调。

第二层是 conversation assembly（`ui-conversation/src/client/conversation/assembly.ts:141`）。高频流式更新要跨过三次绘制机会才发布：

```ts
this.frame = requestAnimationFrame(() => {
  this.frame = requestAnimationFrame(() => {
    this.frame = requestAnimationFrame(() => {
      this.frame = undefined
      this.flush()
    })
  })
})
```

期间再有更新只是复用已排的帧。`immediate` 发布则取消这串 rAF，同步 flush。

第三层是每个节点的 publication 声明。assistant 节点的规则很短（`conversation-nodes/assistant.ts:332`）：

```ts
publication: (match) => {
  if (match.event.type === 'step/start') return 'none'
  if (match.event.type !== 'assistant/live-chunk') return 'immediate'
  const type = match.event.data.chunk.type
  return type === 'usage' || type === 'finish' ? 'none' : 'animation-frame'
}
```

文本、推理、工具参数的 live delta 走合帧；durable 的 `assistant/message`、工具调用与结果立即发布；`step/start` 不发布，`usage` 和 `finish` 也不发布。多种更新同时到达时，assembler 取最高优先级 `PUBLICATION_RANK`，有节点变化就提升到 `immediate`。

到组件层还有一层：markdown 渲染只重新解析尾部两个块，前面的块冻结成缓存的 React 元素，跨过冻结边界时保留源偏移 key，让 React 复用而不是重挂（`ui-primitives/src/markdown/MarkdownText.tsx:4`）。流式期间引用式链接或脚注如果定义在冻结边界另一侧，会先按字面量显示，等定稿的完整解析再修正。

| 层 | 调度单位 | 合并方式 |
|---|---|---|
| notifier | microtask 或 rAF | 同一时刻只排一个回调，generation 丢弃过期 |
| conversation assembly | 三次 rAF | 复用已排的帧，`immediate` 取消并同步 flush |
| assembler publication | 每次窗口变化 | 取所有节点里的最高优先级 |
| markdown | 每次文本变化 | 冻结前缀，只重解析尾部两块 |

三层延迟叠起来，最坏情况要等三个绘制周期。按 60Hz 计算大约 50 毫秒，肉眼看到的是连续出字，不是一段段跳。这个延迟换来的是突发 chunk 被压成一次提交，React 不会为每个 token 走一遍协调。

自己搭的时候，先给「合流」和「渲染」分两层，再谈节流。合流层保证状态正确，渲染层只决定什么时候画；把两者塞进同一个 setState，节流会变成丢数据。

{{< fig "diagrams/04-render-batching.svg" "三层调度把突发 chunk 压成一次可见更新，markdown 只重解析尾部两块" >}}

## 11. 设计取舍

实时行由客户端合成。日志里不记录每个 chunk，只记录一次 attempt 的紧凑流和最终消息。好处是会话文件小、重放确定；代价是客户端要实现 transient 行的插入、替换和撤回，还要处理 attempt 中途挂载的缺口。

落盘保留 delta 边界。把 run 拼成整段文本更省空间，但会丢掉首 token 时间和逐段耗时。保留边界的代价是格式复杂：`dt` 长度、时间戳、工具 id 的一致性都要在展开时校验。

重连基线放在 Host 内存里。它只覆盖一个活动 attempt，成本有界，客户端重连时拿到的是一段前缀而不是整段日志。进程重启后基线消失，此时只能靠 durable 事件重建，界面会从已提交的位置继续。

transcript 不虚拟化。窗口上限、Turn 对齐分页和滚动锚定三件事一起，把 DOM 行数控制在可接受范围。自己搭的时候，如果消息长度不可控，先限制窗口和分页，再考虑虚拟化；反过来做，锚定逻辑会和测量逻辑打架。

## 12. 踩坑点

1. 把 revision 当全局序号。它只在一次 Agent 生命周期内连续，新生命周期从 1 重来；Host 收到 `start` 且 revision 为 1 时会清空旧 attempt，客户端也要接受这次重置。
2. 把 `chunk.index` 当 attempt 内的帧序号。它是 block 索引，文本和推理可以交错；attempt 级序号是帧上的 `index` 与 reducer 的 `nextIndex`。
3. 在 durable 结算之前发布 `assistant/message`。客户端的 pending 机制要求 end 帧指向已经 staged 的事件；顺序反了会触发 rebaseline，界面闪一下。
4. 重连只补日志不补基线。活动 attempt 的前缀不在 durable 日志里，只补日志会让界面从半句开始。
5. 用数组 `unshift` 做 prepend。窗口是拼接树，prepend 只加一个 concat 节点；换成数组每次翻页都要复制全量，长会话会明显卡顿。
6. 每个 chunk 直接 `setState`。中间缺少 notifier 合帧和三帧 rAF，React 会把渲染压力放大到每个 token。
7. 工具参数 delta 当普通文本发布。live 工具参数走合帧，durable 的工具调用立即发布；两类更新顺序错乱时，参数会先出现再被覆盖。
8. 每次 chunk 全量重解析 markdown。只解析尾部两块，冻结块保留 key；否则长回复的解析成本会随长度线性增长。

## 13. 源码索引

| 位置 | 内容 |
|---|---|
| `packages/llm/llm/src/types.ts:496` | `StreamChunk` 七种类型 |
| `packages/llm/llm/src/assistant-stream.ts:20` | 紧凑记录 `AssistantStreamRecord` |
| `packages/llm/llm/src/assistant-stream.ts:108` | `AssistantStreamAccumulator.push()` 合并条件 |
| `packages/llm/llm/src/assistant-stream.ts:183` | `snapshot()` 冻结与拷贝 |
| `packages/core/agent-loop/src/assistant-stream.ts:60` | attempt 的 `push()` 三路分发 |
| `packages/core/agent-loop/src/assistant-stream.ts:78` | `settle()` 先提交再发 end |
| `packages/core/session/src/types.ts:349` | `assistant/message` 的 `stream` 字段 |
| `packages/api/session-controller/src/assistant-stream.ts:27` | Host 侧基线累加器 |
| `packages/api/session-controller/src/client/sessions/assistant-stream.ts:58` | 客户端 `replace()` 与基线展开 |
| `packages/api/session-controller/src/client/sessions/assistant-stream.ts:129` | 客户端 `acceptFrame()` |
| `packages/api/session-controller/src/client/sessions/notifier.ts:31` | `markDirty()` 与 `markFrameDirty()` |
| `packages/api/session-controller/src/client/contract/events.ts:158` | prepend 的拼接树实现 |
| `packages/api/session-controller/src/client/transport.ts:172` | follow 请求基线 |
| `packages/api/gateway/src/client/journal-stream.ts:338` | 日志缺口 `replaceThrough()` |
| `packages/api/session-controller/src/history.ts:132` | 服务端分页 |
| `packages/api/session-controller/src/client/sessions/session.ts:419` | 客户端 `loadOlder()` |
| `packages/client/ui-conversation/src/client/conversation/assembly.ts:141` | 三帧 rAF 发布 |
| `packages/client/ui-chat/src/client/conversation-nodes/assistant.ts:332` | 节点级 publication |
| `packages/client/ui-chat/src/client/chat/TurnNavigator.tsx:110` | turn rail 虚拟化 |
| `packages/client/ui-primitives/src/markdown/MarkdownText.tsx:4` | 尾部两块冻结策略 |

## 14. 最后

流式链路里最容易出错的是交接点，不是单个模块。帧序号、attempt 序号、日志 seq、transient 分数 seq 是四种不同的坐标，混用其中两个，症状通常要到重连或中断时才出现。自己搭的时候，先把这四种坐标写进类型名，再写 reducer。

窗口和滚动可以分开验证：关掉网络看已加载窗口是否稳定，再恢复网络看替换窗口后阅读位置是否还在。渲染节流最后调，前面两层正确之后，三帧 rAF 只是延迟，不会改变最终内容。

想继续看客户端如何组织会话状态，可以读《[客户端架构](/posts/dsh-client-architecture.html)》；想看一次请求在宿主侧的完整路径，读《[一次请求的完整旅程](/posts/dsh-request-journey-end-to-end.html)》。
