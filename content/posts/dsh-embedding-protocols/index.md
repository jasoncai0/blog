---
title: "嵌入一个 harness：三种边界，一条事件流"
date: "2026-11-13T13:20:00+08:00"
summary: "SDK、ACP、gateway 都把同一个 agent loop 接到别人的进程里，差别在谁启动谁、事件转发给谁、断线后怎么补。三条路径共用一份 session 事件日志，却给出三种不同的补发承诺。"
description: "拆解 sdk/protocol、sdk/client、sdk/server 的分层，session-controller 与 gateway 的职责边界，鉴权与多租户的位置，以及 headless、sdk-minimal 的裁剪取舍。"
tags: ["harness", "architecture", "concurrency"]
slug: "dsh-embedding-protocols"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准。
> 第二季第 9 篇。上一季：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)；本季导览：[《第二季：把主干两侧补齐》](/posts/dsh-season-two.html)。

`@deepseek-ai/dsh-sdk-client` 的 README 第一句是「把 harness 嵌进你的应用」。可打开 `client.ts` 会发现，它做的是 `spawn` 一个 `dsh` 子进程，然后往 stdin 写 JSON。这算嵌入吗，还是算远程调用换了个传输层。

第二个困惑来自 ACP。编辑器里发一次 `session/prompt`，回来的是一串 `session/update`：消息块、工具调用、用量。Web 那边也有消息块和工具调用，但走的是 WebSocket 上的 `emit` 帧。两套东西看起来在描述同一件事，字段却不一样。

第三个困惑是断线。SDK 的事件通知里没有游标，重连只能重新 `session/resume`；Web 的 `follow` 却先给一个带 cursor 的快照，再逐条推事件，缺口还会报协议错误。同一个 session 日志，为什么三种表面给出三种承诺。

这篇按这三条边界走：谁启动谁、事件怎么过线、断了之后谁负责补齐。

## 0. 结论

- 三种嵌入都跨进程。SDK 是同机子进程加 stdio；ACP 是编辑器启动 agent 加 stdio；gateway 是 dsh 自己起 HTTP 服务器，浏览器连上来。
- 分层的核心是 `sdk/protocol` 只放 wire 类型，`sdk/server` 把 Context 里的事件桥到 transport，`sdk/client` 管进程与请求关联。协议包不依赖运行时的任何东西。
- 事件过线有三种语义：SDK 无条件转发全 runtime 的 session 事件；ACP 只转发自己拥有的 session，并转成标准 update；gateway 按 principal 与资源授权过滤后，走多路复用流。
- gateway 不懂 session，session-controller 不懂传输。前者负责鉴权、描述符分发、流复用；后者负责快照、cursor、缺口检测与命令。
- 鉴权分三层：connection 决定谁进得来，remote-access 决定谁能调什么，controller 决定返回哪些资源。多租户是资源级的，不是进程级的。
- 补发能力与传输绑定：SDK 与 ACP 都没有重放，只能 resume 重建；Web 有快照加严格 seq，缺口靠分页修复。headless 与 sdk-minimal 是裁剪，不是另一种协议。

## 1. 三种边界

先看谁启动谁。这决定了生命周期归谁、标准输入输出归谁、错误往哪写。

| 形态 | 谁启动 | 传输 | 客户端数 | 事件范围 |
| --- | --- | --- | --- | --- |
| SDK | 宿主应用 `spawn` 一个 `dsh` | stdio，换行分帧 JSON-RPC | 单客户端 | runtime 内所有 session |
| ACP | 编辑器启动 agent 进程 | stdio，JSON-RPC | 单客户端 | 本 ACP 拥有的 session |
| gateway | dsh 自己启动，监听端口 | HTTP POST 加 WebSocket mux | 多客户端 | 按 principal 过滤 |

SDK 的「嵌入」是进程级嵌入：宿主应用拿到的是一个子进程句柄，不是一个库。好处是隔离彻底，宿主用什么语言、什么运行时都不影响 agent；代价是每次启动都要付一次完整的 boot，冷启动就是上一篇文章里那条九步序列。

### 协议版本怎么谈

`initialize` 返回的 `serverInfo` 只有名字与版本，没有协商列表。客户端把版本记下来，在 `launch.ts` 里与自己的常量比对；不等就拒绝启动，而不是尝试兼容。

这个做法牺牲了向后兼容的余地。协议改字段就得同时升级两端，跨版本组合会直接失败。好处是运行期不需要分支判断版本，每个客户端只面对一种 wire 格式。

对第三方实现来说，这条规则意味着锁版本。想同时支持多个 dsh 版本，就得在宿主侧维护多份适配，而不是在协议里加 capability 开关。

ACP 的边界由编辑器定义。编辑器是 client，agent 是 server，方向与 SDK 相反：SDK 里宿主是 client，dsh 是 server。两者都用 stdio，都按 JSON-RPC 分帧，但请求表的形状完全不同。ACP 的请求名是 `session/prompt`、`session/resume`、`session/cancel`，更新走 `session/update` 通知；SDK 的请求名是 `session/prompt`、`session/query`、`session/wait`，事件走 `session.event`。

gateway 是唯一一个 dsh 主动监听端口的形态。它要同时服务浏览器、桌面壳、可能的远程客户端，所以传输从 stdio 换成了 HTTP 加 WebSocket。客户端数从一个变成了多个，鉴权也从「进程边界即信任边界」变成了显式的 token、cookie、Peer、授权判定。

{{< fig "diagrams/01-three-boundaries.svg" "三种嵌入的边界：SDK 由宿主 spawn 子进程，ACP 由编辑器启动 agent，gateway 由 dsh 监听端口；三者的进程归属、传输与事件范围各不相同。" >}}

一个容易忽略的共同点：三者最终都落到同一套 agent 与 session 服务上。`session/prompt` 在 SDK 与 ACP 里都是往 inbox 塞一条消息，然后等 turn 结束；gateway 的 `follow` 读的是同一份 session 日志。协议不同，底下那张表只有一张。

### 为什么没有进程内 SDK

把 harness 做成一个可 `import` 的库，看起来更「嵌入」。仓库没有这么做，原因有三个：进程级的全局状态（配置、credentials、sandbox）难以多实例共存；依赖图通过 runtime resolution 固定，宿主进程的解析规则会与它冲突；崩溃隔离在同一个进程里做不到。

子进程方案把这些都推给操作系统。每个 agent 有自己的 profile、自己的 home、自己的解析表，互不干扰。宿主只需要会读写 JSON 行，不需要理解 Cordis，也不需要跟版本升级同步内部类型。

代价是 IPC 序列化与进程管理。高频小调用不适合这种边界，比如逐 token 的流式回传；协议的做法是把流式合并成消息块事件，牺牲一点实时性换吞吐。

### SDK：一次冷启动的成本

宿主应用调用 `HarnessClient.spawn` 时，发生的事和命令行跑一次 `dsh` 没有本质区别：解析 profile、算解析表、叠 patch、激活整棵树、等 `appReady`。这段时间里宿主只能等，协议没有「部分可用」的状态。

`launch.ts` 在起进程前做两件事：找到 dsh 的可执行文件，校验它的版本与客户端期望的协议版本一致。版本不一致直接拒绝，不会尝试兼容。这个选择很硬，但省掉了「新旧字段混用」的一整类问题。

冷启动的成本决定了 SDK 适合「长会话、少量启动」的场景。每次任务起一个进程、跑完就退的模式，会把 boot 时间摊到每个任务上；如果任务本身只有几秒，boot 就占了大头。

### ACP：能力协商与权限

ACP 在 `initialize` 时公布 `sessionCapabilities`：`close`、`list`、`resume`，没有 `load`。编辑器据此决定界面上给不给「打开历史会话」的入口。公布什么就支持什么，没公布的能力调用会失败，不会静默降级。

权限请求是反向的：agent 要执行一个需要批准的工具时，向编辑器发 `session/request_permission`，编辑器回一次性允许或拒绝。这个决定不跨会话复用，下一次同类调用还要再问。ACP 没有实现「记住这个工具总是允许」，那是编辑器自己的策略层。

`session/resume` 恢复的是日志与能力，不是界面。编辑器拿到 session id 之后可以继续发 prompt，但断线期间的 `session/update` 不会补发，界面只能从下一次 update 开始更新。

### ACP resume 的界面语义

编辑器断开再恢复时，agent 侧的 turn 可能还在跑。`session/resume` 返回的会话句柄是活的，后续 update 会继续推送。编辑器要做的是把界面与 agent 的真实状态对齐：已完成的工具调用要显示结果，正在跑的步骤要显示进行中。

协议不提供「当前状态查询」。编辑器只能从后续 update 里推断，或者自己保存断线前最后一条 update 的序号。这是 ACP 的取舍：它假设编辑器自己维护文档与会话视图，agent 只负责推增量。

如果编辑器完全丢掉状态，恢复后就只能看到一个空界面，直到下一次 update 到来。这不是 bug，是协议没承诺的事情。要补这个能力，需要一个新的 `session/load` 或状态查询方法，而当前版本明确不支持。

### gateway：多客户端与心跳

gateway 的 WebSocket 在 Client 插件激活时打开，空闲时靠心跳保活。Host 每 `websocketHeartbeatIntervalMs`（默认 2000ms）发一次 Ping，连续两个间隔没有活动就终止连接。这个阈值比大多数反向代理的空闲超时短，目的是让死连接尽快被回收，而不是等 TCP 自己发现。

压缩默认开，算法 `deflate`，只对 1024 字符以上的 JSON 文本生效。小帧不压缩，因为压缩头本身的开销比省下的字节多。这个阈值是固定的，不随连接数调整。

上行帧有字节预算：每条流最多缓冲 `streamInboxBytes`（默认 262144）字节，超了报 `gateway/uplink-overflow`。这是为了防止一个客户端把 Host 的内存吃满；代价是客户端必须自己控制发送速率，不能假设 WebSocket 的背压会自动传导。

## 2. sdk 的三层

先说「嵌入」在这里的意思。宿主应用不需要链接 dsh 的代码，也不需要与它共享内存；它启动一个进程，用一份有版本的 JSON 协议说话。这个定义把集成成本压到「实现一个 JSON-RPC 客户端」，也把故障域隔开：agent 崩了，宿主还在。

`packages/sdk/` 下有三个包，职责切得很干净。

`protocol` 只有类型。它定义 `HarnessSdkRequestMap`、`HarnessSdkNotificationMap`、`HarnessSdkHostRequestMap`，以及每个方法的参数与结果。没有运行时依赖，没有 Context，也不 import 任何 agent 包。这样 Python SDK 可以照着同一份定义实现 wire 层，而不必把 TypeScript 运行时拖进去。

`server` 是桥。它挂到 Context 上，监听 `session/event`、`agent/status`、`session/created`、`subagent/end`，把内部事件转成协议里的通知，再交给 transport。它还负责把请求分派到 `agents`、`sessions`、`commands` 这些服务。

`client` 是进程管理器。`HarnessClient.spawn`（`client.ts:223`）解析 dsh 的可执行文件，校验版本与协议一致，起子进程，接上 stdin/stdout，维护请求 id 与 Promise 的对应关系，再把通知分发给订阅者。

三层的依赖方向是单向的：client → protocol，server → protocol，protocol 不依赖任何人。这条规则让协议可以被别的语言重新实现，也让「加一个方法」变成三处同步修改，而不是在传输层里打补丁。

### server 侧的桥

`SdkJsonRpcServer` 注入的是 `sdkAppStartup` 与 `loader`。前者提供这一代进程的启动参数，后者让服务器可以在需要时等树稳定。它在 `appReady` 之后开始读 stdin，在那之前到达的字节留在管道里。

桥接的重点是「不要把内部对象漏出去」。`session/event` 转发的是 `SessionEvent` 的 JSON 形态，不是 Session 实例；`session/prompt` 接受的是内容块，不是 Message 对象。这条纪律让协议在内部类型重构时保持稳定，代价是每次都要做一层显式转换。

服务器的错误也做了映射。agent 抛出的领域错误被包成 JSON-RPC error，带 code 与 message；`gateway/internal` 这类传输层错误不在 SDK 的码表里，它们不会出现在 stdio 上。客户端拿到的错误码因此是稳定的，不会因为内部换了异常类型而变。

一次 `session/prompt` 的线上形状是这样：

```json
{"jsonrpc":"2.0","id":7,"method":"session/prompt","params":{"sessionId":"s-1","content":[{"type":"text","text":"读一下 README"}]}}
{"jsonrpc":"2.0","id":7,"result":{"messageId":"m-42"}}
{"jsonrpc":"2.0","method":"session.event","params":{"sessionId":"s-1","event":{"type":"turn/start","seq":31}}}
{"jsonrpc":"2.0","method":"session.event","params":{"sessionId":"s-1","event":{"type":"step/start","seq":32}}}
```

注意 `result` 只回 `messageId`（`types.ts:93`），不带 turn 结果。这是一个入队回执：消息已经进了 inbox，durable 地记下来了；它不表示模型已经回答。要等回答，客户端读 `session.event` 里的 `turn/end`，或者调 `session/wait`。

这个设计把「提交」与「完成」拆成两个 RPC。好处是调用方不会因为一次长 turn 而超时；代价是调用方必须自己维护「我提交的那条消息对应哪个 turn」的映射，协议不提供这个关联。

### 请求关联与并发

JSON-RPC 的 `id` 由客户端分配，服务器原样回填。SDK client 用一个 Map 维护 `id → Promise`，收到 result 或 error 时结算。通知没有 id，走另一条分发路径。

并发是允许的：客户端可以同时发多个 `session/prompt`，服务器按 session 串行处理，不同 session 之间并行。协议不限制在途请求数，客户端要自己做限流，否则一次发几百条 prompt 会把 inbox 撑大，turn 排到很后面。

服务器侧还有一个 `maxTokensAsSuccess` 的开关，默认 true。它的作用是：模型因为达到 max tokens 而结束时，把这一轮当成成功而不是错误。这个选择影响客户端的分支：当成错误的话，客户端会重试，而重试通常只会再撞一次同样的上限。

```ts
// 通知分发，简化
for await (const note of client.notifications()) {
  if (note.method === 'session.event') {
    applyEvent(note.params.sessionId, note.params.event)
  }
}
```

通知是异步的，可能在 result 之前到，也可能在之后。客户端不能假设「先 result 后事件」的顺序；它只能假设同一 session 的事件按 seq 递增。

## 3. 事件怎么过线

三种形态对事件的处理方式，正好覆盖三种典型策略。

SDK 是全量转发。server 里的监听器不判断 session 归属：

```ts
ctx.on('session/event', (session, event) => {
  this.transport.notify('session.event', {
    sessionId: String(session.id),
    event,
  })
})
```

注释写得很明确：每一个 session 的事件都会转发，包括不是 SDK 创建的那些。理由是同一个 runtime 里可能有子代理、有别的客户端建的会话，订阅方应该能看到全貌。代价是事件量可能很大，客户端要自己过滤。

转发出去的事件本体带 `seq`，但通知信封里没有 cursor。也就是说，客户端能发现「seq 从 31 跳到 33」，却没有任何 RPC 可以让服务器补发 32。这个缺口只能靠 `session/resume` 重建整个会话视图，或者靠客户端自己持久化。

ACP 是过滤加翻译。`index.ts:136` 的监听器先判断这个 session 是不是本 ACP 拥有的，再交给 `session.ts:320` 的 `onSessionEvent`。后者把 durable 事件映射成标准 ACP update：`agent_thought_chunk`、`agent_message_chunk`、`tool_call`、`tool_call_update`、`usage_update`。

翻译层只发已提交的语义更新，不发 raw delta。模型流式输出的中间片段不会原样过线，只有落成消息块之后的内容才会出现。这让编辑器的渲染简单很多，代价是首字延迟变大：用户看到第一个字符的时间，取决于消息块什么时候提交。

ACP 的 `session/update` 按会话串行。同一个 session 的更新不会交错，跨 session 可以并行。这个约束让编辑器可以用一个队列消费 update，不必处理同一会话内的乱序。

gateway 是过滤加复用。事件不以 `session.event` 的名义发送，而是走 `emit` 帧，事件名由 Host 侧装配时决定。接收方是 Peer，Peer 在 upgrade 时绑定 principal；发不发、发什么，取决于这个 principal 的资源授权。

`waterfall` 帧是 gateway 独有的。有些事件需要客户端参与决策（比如审批），Host 不能自己拍板。这时候 Host 发一个 `waterfall` 帧，带 `eventId` 与 `agentId`，等客户端通过 `$events/result` 回一个 outcome：`next`、`result` 或 `rejected`。这是一个跨进程的同步调用，只是借用了事件通道。

{{< fig "diagrams/02-rpc-wire.svg" "一次 RPC 与一次事件过线：请求按行分帧写入 stdin，服务器回 result 后异步推 session.event；事件本体带 seq，信封不带游标。" >}}

不这样会出什么事：如果 SDK 把事件信封也做成带 cursor 的流，服务器就要为每个客户端维护重放缓冲，进程内存随客户端数与断线时长增长。如果 ACP 直接转发 raw delta，编辑器的渲染层要自己拼消息块，拼错一次就留下半句话。如果 gateway 让 Host 自己决定审批，多租户下就没有统一的策略点。

### 背压与顺序

三种传输的背压能力不同。stdio 有操作系统的管道缓冲，写满之后 `write` 会返回 false，Node 的 stream 会等你 drain；WebSocket 有 `bufferedAmount`，但 gateway 没让它传导到事件生产端，而是用字节预算直接拒绝。

顺序保证只到 session 级。同一个 session 的事件按 seq 递增，跨 session 没有全局顺序。客户端如果要把两个 session 的事件合并成一条时间线，需要自己按时间戳排序，而时间戳的精度不一定够。

ACP 的串行保证更强：同一个 session 的 update 一条一条发，发完一条才发下一条。这让编辑器的渲染不会出现「工具调用结果比调用本身先到」。代价是一个慢消费者会拖住整个 session 的更新。

### 转发选择

gateway 的事件不是全部转发。Host 侧装配时决定哪些事件转发、转给谁，key 与 `ctx.remote.$on()` 订阅的 key 对应。这个选择在启动期固定，运行期不随客户端变化。

SDK 没有转发选择，全部转发。ACP 的选择是「只转发本会话」。三种策略对应三种客户端模型：SDK 的订阅方要全貌，ACP 的编辑器只要自己的会话，gateway 的多租户客户端只能看授权范围内的。

选择越窄，过滤成本越高。gateway 要为每个事件判定资源与 principal，SDK 只做一次序列化。这也是为什么 gateway 的帧协议比 SDK 复杂得多：它要在同一个连接上承载多个会话、多种流、以及反向的 waterfall。

## 4. session-controller 与 gateway 的边界

这两个包经常被一起提到，职责其实不重叠。

`SessionController extends TypertRemoteService`（`packages/api/session-controller/src/index.ts:135`）。它是 session 的远程门面：`createSession`、`commands`、`queue`、`cancel`、`events`、`observeEvent`、`observeProjections`、`state`、`wait`、`follow`、`control`。所有方法都按 typert 的描述符暴露，客户端拿到的签名与服务端实现是同一份。

`Gateway` 是传输与分发。它持有 `remoteAccess`，为每次调用解析 Peer，按描述符找到目标方法，做参数校验与资源判定，再把结果封装成 `RemoteResult<T>`。它不 import session 的任何类型，也不知道 `follow` 的帧长什么样。

typert 描述符在这条链上扮演「共同语言」。服务端由生成器产出方法名、参数 schema 与返回类型，客户端拿同一份产物生成调用桩。gateway 只按描述符分发，不关心方法属于哪个服务；controller 只实现方法，不关心调用从哪来。

这套机制让「加一个客户端方法」不需要改 gateway。代价是描述符必须在两侧版本一致：严格模式下，Host 找不到描述符会直接报错，不会退回反射调用。开发期可以用 SRC 回退，但它只允许 JSON 安全值，生产路径不该依赖它。

| 关注点 | gateway | session-controller |
| --- | --- | --- |
| 连接与流复用 | 拥有 | 不感知 |
| Peer 与鉴权判定 | 拥有 | 不感知 |
| 描述符分发与参数校验 | 拥有 | 声明 |
| 会话生命周期 | 不感知 | 拥有 |
| 快照、cursor、缺口检测 | 不感知 | 拥有 |
| 命令执行与队列 | 不感知 | 拥有 |

一次 `follow` 的时序因此是：浏览器发 HTTP 请求 → connection 认证并建 Peer → gateway 按描述符调到 `SessionController.follow` → controller 开一个 `using observation`，读历史、拼快照、设 cursor、挂监听器 → 先把 snapshot 帧写回 → 后续事件逐条写入同一个流 → gateway 把流编码成帧发给浏览器。

`WebController.follow`（`web/controller.ts:32`）里有一个细节值得抄：它先 `listener.setCursor(opening.cursor)`，再 `yield opening`。顺序反过来的话，快照与第一批事件之间会有一个窗口，窗口里发生的事件要么丢、要么重复。先设游标再发快照，监听器在快照写出之前就开始缓冲，缺口检测才有意义。

缺口检测本身在 `history/listener.ts`：`nextExpectedSeq = cursor + 1`，收到 `seq < next` 丢弃，`seq !== next` 直接抛 `RemoteError('gateway/internal', 'session event stream skipped seq N')`。它不尝试修补，因为修补需要知道缺了哪些事件；它把问题交给上层，让客户端重新 follow。

客户端那一层确实会修。`RemoteJournalStream`（`gateway/src/client/journal-stream.ts:90`）在重连后拿到的新 opening cursor 如果落后于本地 lastCursor，就用分页请求补齐；完整重复的条目丢弃，部分重叠或倒置区间直接报协议错误。这条路径把「缺口」从错误变成一次可控的补页。

### 另外两条读路径

`follow` 不是唯一的读法。`observeEvent`（`:554`）是纯流，不带快照，适合已经持有完整状态的客户端；`observeState`（`:587`）推的是完整被动状态替换，每次给一份新的 state，适合只看当前值的面板；`control`（`:631`）是 baseline 加替换帧，按 `remoteAccess.allows` 过滤 session，适合需要控制面的客户端。

三条路径的共同点是都不做重放。`observeEvent` 没有 cursor，断了就断了；`observeState` 每次都是全量，断了之后下一次全量就是新状态；`control` 有 baseline，但 baseline 只在流开始时给一次。

`follow` 之所以特殊，是因为它同时给了快照与游标，并且承诺快照之后的帧是连续的。这个组合需要 controller 在发出快照之前就把监听器挂好，也就是前面说的「先设 cursor 再 yield」。另外两条路径没有这个约束，因为它们不承诺连续性。

选哪条路径取决于客户端要什么：要完整时间线用 `follow`，要当前值用 `observeState`，要控制面用 `control`。用 `follow` 去做面板，会收到大量它不关心的事件；用 `observeState` 去做聊天记录，会丢掉中间过程。

自己搭的时候，把「传输」与「会话语义」分成两个包，中间只留描述符和帧类型。一旦让 gateway 认识 session，补发逻辑就会散在传输层里，换一种传输就要重写一遍。

## 5. 鉴权与多租户在哪层

浏览器第一次打开 Web GUI 时，URL 上带一个 `?token=`。这个 token 是进程启动时随机生成的，写在 `ctx.credentials` 管理的 grant 里，重启就换。

`connection` 用这个 token 换一个签名 cookie。cookie 绑定 authority（host 加 port），默认 30 天，HttpOnly、SameSite=Strict，TLS 下加 Secure。签名密钥也存在 credentials 里，条目名是 `client-connection/browser-session`，落在 `$DSH_HOME/.credentials.yaml`。

认证之前还有两道检查：Host 是否在 `trustedHosts` 里，`sec-fetch-site` 是不是 `cross-site`。Host 不信任返回 403，未认证返回 401。顺序很重要：先判断来源可不可信，再判断你是谁，否则一个跨站请求可以拿你的 cookie 去试。

通过之后，每个请求建一个 Peer。Peer 带 principal 与 interactions，principal 通常是 `browserPeer`，也就是 owner。`/connect?token=...&interactions=answer|none` 是另一条入口，给非浏览器客户端建 API Peer，可以选择是否接收需要人工回答的交互。

### principal 从哪来

浏览器路径的 principal 是固定的 owner：这个进程的服务对象就是本机用户。API Peer 的 principal 由建连时决定，可以是 owner，也可以是一个受限身份。`/connect` 的 token 与浏览器 token 同源，但换出来的 Peer 不共享 cookie。

Peer 一旦建立，principal 不再变化。这样做有一个实际理由：让授权判定可以缓存：`snapshotFor(principal)` 算一次，整个连接期间复用，权限变更靠 `invalidate` 主动失效。如果 principal 可以中途换，每次调用都要重算，缓存也就没意义了。

`interactions` 决定这个 Peer 收不收需要人回答的请求。`answer` 表示它愿意弹审批与提问，`none` 表示跳过。一个后台任务用 `none` 建连，就不会因为一个需要批准的 shell 命令而永久挂起。

`remote-access` 是第二层。owner 不检查；非 owner 的每次调用都要 `decide({principal, action, resources, args})`。判定依据是 role 与 binding：role 声明「对某类资源可以做什么 action」，binding 把 role 绑到具体资源上，condition 再做一次过滤。资源之间独立，`global` 上的授权不蕴含其他资源。

第三层在 controller。`follow` 与 `control` 会按 `remoteAccess.allows` 过滤 session；不在授权范围内的 session 不会出现在帧里。这一层不是安全边界，它是减少信息泄漏的面：即使 gateway 判过了，也不该把无关 session 的标题推给一个只能看一个会话的客户端。

多租户因此是资源级的，不是进程级的。一个 Host 可以服务多个 principal，各自看到不同的 session 与工具。硬隔离要靠独立的 Host 加独立的 `DSH_HOME`，因为 `session/attachment` 的读权限被定义为「整个附件存储」：只要在任意一个 session 资源上被授予，就能读其他 session 的图片。

| 层 | 回答的问题 | 失败结果 |
| --- | --- | --- |
| connection | 这个请求从哪来、是不是本人 | 403 / 401 |
| remote-access | 这个 principal 能对这个资源做什么 | `remote-access/denied` |
| session-controller | 这个客户端能看到哪些会话 | 过滤，不报错 |

不这样会出什么事：把鉴权放进 controller，每个方法都要重复一遍 principal 检查，漏一个就是一个越权读；把过滤放进 gateway，gateway 就要理解 session 的归属关系，传输层被业务语义污染。

### 威胁模型里没有的东西

cookie 绑定 authority，所以同一个 host 换个端口就不认。这防的是「另一个本地服务冒充」，不防「同一 authority 下的恶意页面」；后者靠 SameSite 与 `sec-fetch-site` 检查。

token 在进程启动时生成，写在 credentials 里。能读 `$DSH_HOME/.credentials.yaml` 的进程已经拿到了本机用户的权限，token 保护不了这种情况。它保护的是「浏览器里的其他页面」与「局域网里的其他机器」。

`trustedHosts` 为空时，Host 头只接受回环地址与配置的 publicUrl。把服务暴露到局域网要显式加 host，否则请求会被 403。这个默认值是有意的：宁可让配置多一步，也不让一个 `--host 0.0.0.0` 顺手把控制面开放出去。

多租户的隔离粒度到资源为止。两个 principal 共享同一个进程、同一个工具执行器、同一个 `DSH_HOME`。一个 principal 触发的工具执行如果越出 workspace，影响的是整台机器，不是它自己的租户空间。硬隔离只能靠独立进程加独立 home。

## 6. headless 与 sdk-minimal 砍掉了什么

裁剪不是换协议，是换组合。同一个 `dsh` 可执行文件，靠不同的 bundle 层决定挂哪些行。

| 能力 | base | web-app | headless | acp-app | sdk-app | sdk-minimal |
| --- | --- | --- | --- | --- | --- | --- |
| HTTP 服务器 | 无 | 有 | 无 | 无 | 无 | 无 |
| 浏览器客户端 | 无 | 有 | 无 | 无 | 无 | 无 |
| session 持久化 | JSONL | JSONL | JSONL | JSONL | JSONL | 未压缩 JSONL |
| shell 工具 | 有 | preset | 有 | 有 | 持久 shell | 有 |
| 文件工具 | 有 | preset | 有 | 有 | 有 | 仅 read_image |
| 子代理 | 有 | preset | 有 | 有 | 有 | 无 |
| skill / jobs | 有 | preset | 有 | 有 | 有 | 无 |
| 设置与托管凭据 | 有 | 有 | 关 | 有 | 有 | 无 |
| HMR | 有 | 有 | 关 | 关 | 关 | 无 |
| 遥测 | 有 | 有 | 有 | 有 | 有 | 无 |

headless 砍掉的是「常驻」与「界面」。它没有 Web 服务器，没有浏览器客户端，跑完一个任务就退出。启动时插入 `headless-startup` 与 `headless-runner`，后者从 `ctx.headlessStartup` 读 task、sessionId、json 三个配置。`--json` 让输出变成一行行 JSON，`--session-id` 让第二次调用接着上一次的日志跑。

它保留的东西比想象中多：完整的工具集、子代理、持久化、凭据。因为它要跑真实任务，砍掉这些就没法工作。它砍掉的是 hmr、credentials 的交互部分、authorization、deepseek-account，这些在一次性进程里没有意义。

sdk-minimal 砍得更狠。它不用 base，一个 patch 就是完整的树。保留的能力只有 shell、read_image、working_directory，加上未压缩的 JSONL 持久化。没有 skill、没有 jobs、没有 subagent、没有 workspace 指令、没有设置与托管凭据、没有遥测。

这组取舍服务的是「嵌进别人的产品」：宿主自己管工具、自己管凭据、自己管上下文，只需要一个能跑 shell 与读图的循环。包体小、启动快、行为可预测，代价是能力边界很窄，想要什么都要宿主自己加。

sdk-app 在两者之间。它保留完整工具面，加上 plugin-manager、持久 shell、`sessions` 的 `compression: none`，并插入 `sdk-jsonrpc-server`。它适合「在自己的进程模型里跑一个完整 agent」的场景。

### 裁剪的边界在哪

裁掉一行，可能连带裁掉它的 registry。`tool-jobs` 依赖 background-job registry，`tool-skill` 依赖 skill registry，`tool-subagent` 依赖 subagent registry 与后端。把工具行删了但 registry 还在，是浪费；把 registry 删了但工具行还在，是启动期注入失败。

所以 bundle 的裁剪顺序是「先删工具行，再删 registry，最后删服务」。sdk-minimal 的 patch 里没有 base，等于从零写一份完整树，每一行都要自己保证依赖闭合。这也是它最容易随上游漂移的原因：base 新增一个必需服务，它不会自动获得。

headless 的裁剪方向不同。它保留完整工具面，只删「常驻」与「界面」：没有 webserver、没有 client modules、没有 connection。它仍然需要 agent-loop、tool-executor、session 持久化，否则跑不完一个任务。

自己搭的时候，先确认要删的行有没有被别人 `inject`。删掉一个被依赖的服务，症状很少是启动失败，更多时候是一堆条目停在 PENDING，最后表现为「某个工具不见了」。

{{< fig "diagrams/03-bundle-trim.svg" "bundle 裁剪对比：能力为行、组合包为列；实心点表示该 bundle 自带，空心点表示由 preset 或上层补齐，横杠表示没有。" >}}

自己搭的时候，先写「必须有的能力」清单，再决定从哪个 bundle 起。从 base 加行容易，从 sdk-minimal 往上补很难，因为很多行依赖 base 里的服务，补一个工具要连带补它的 registry 与策略。

## 7. 断线重连与事件补发

三种形态对「断线」的承诺完全不同。

SDK 没有重放。重连的唯一动作是 `session/resume`：服务器从持久日志重建 session，客户端拿到新的快照。断开期间的事件不会补发，客户端要么自己持久化，要么接受视图跳变。`messageId` 是入队回执，不能当去重键：重发 `session/prompt` 会再入队一条。

ACP 也没有重放。`session/resume` 恢复日志与能力，但不回放旧 update。编辑器的界面会停在断线前的状态，agent 继续跑，下一次 update 到达时直接跳到新状态。`session/load` 不支持，所以编辑器不能主动拉历史。

Web 有补发，但补发由客户端驱动。Host 侧只保证「从快照 cursor 开始的事件是连续的」，缺口会报错；客户端负责在重连后比较 cursor，落后就分页补，重叠就丢弃，倒置就报错。

```ts
// 客户端重连后的判断，简化
if (opening.cursor < lastCursor) {
  await repairPageRequest()      // 分页补齐到 lastCursor
} else if (opening.cursor === lastCursor) {
  // 正好接上
} else {
  throw new RemoteError('gateway/protocol', 'stream cursor moved backwards')
}
```

重连的节奏在 connection 里：500ms、1s、2s、4s、8s，上限 10s，带 50% 到 100% 的抖动。`reconnect()` 可以立即重试并重置序列，浏览器 offline 时暂停。每次重试都是一次全新的物理连接，不复用旧 socket。

generation 是这套重连的锚点。Host 在连接建立后先发一个唯一的 `ready` 帧，带 `clientId` 与 `host.home`，之后才发事件。客户端用这个帧判断「这是新连接还是旧连接的重放」，并把本地状态切到新一代。没有这个帧，客户端无法区分「重连成功但还没有事件」与「连接建立但握手没完成」。

### 一次重连的完整动作

浏览器侧的动作序列是：socket 关闭 → 进入重试队列 → 等退避时间 → 新建连接 → 收 `ready` 帧 → 用新 clientId 重开所有活跃流 → 每个流拿新的 opening → 比较 cursor → 补页或丢弃 → 恢复 UI。

Host 侧的动作是：upgrade 时认证 → 建 Peer → 分配 clientId → 发 `ready` → 把该 Peer 的流重新挂上 → 每条流按请求重新走一遍 controller。Host 不保存「这个客户端上次看到哪」，它只保证「你告诉我 cursor，我从那里往后发」。

`RemoteJournalStream` 的 `resumeCursor` 跨物理代次保留。新连接建立后，它拿旧 cursor 与新 opening 比较：落后就补页，相等就接上，超前就报错。这个比较是整个补发逻辑的核心，错一位就会重复或丢事件。

退避的上限是 10s，抖动 50% 到 100%。抖动的作用是避免多个客户端在同一时刻重连，把刚恢复的 Host 再打挂。`reconnect()` 是手动入口，会重置序列并立即重试，适合用户点「重新连接」。

### 为什么没有服务端重放

为 SDK 与 ACP 加重放，需要在服务器为每个客户端保存一段事件缓冲，并在重连时按 cursor 回放。stdio 的连接生命周期与进程绑定，客户端断开通常意味着进程也要退出，缓冲没有意义。

Web 不一样。浏览器标签页可以休眠、网络可以切换、笔记本可以合盖，连接断开的频率高得多。为它维护 cursor 与缓冲是值得的，而且缓冲可以放在客户端：Host 只保证「从你给的 cursor 往后是连续的」，不保存每个客户端的状态。

这个分工让 Host 的内存不随客户端数增长。代价是客户端必须自己持久化 cursor，并在重连时正确地比较与补页。`RemoteJournalStream` 把这套逻辑封装了一次，任何 Web 客户端都可以复用它。

| 形态 | 断线期间的事件 | 重连动作 | 客户端责任 |
| --- | --- | --- | --- |
| SDK | 丢失 | `session/resume` | 自己持久化，接受跳变 |
| ACP | 丢失 | `session/resume` | 接受界面停在上次 update |
| Web | 缓冲后按 cursor 补 | 重新 `follow` 加补页 | 比较 cursor、丢弃重复、报错 |

{{< fig "diagrams/04-gateway-follow.svg" "gateway 与 session-controller 的 follow 时序：认证建 Peer，controller 先设游标再发快照，事件逐条入流；缺口由 listener 报错、由客户端分页修复。" >}}

自己搭的时候，先决定「断线可不可接受」。一次性任务用 headless 或 SDK，断了重跑；交互式产品用 Web，因为只有 Web 的协议承诺了 gap-free；编辑器插件用 ACP，接受 update 不重放，因为编辑器自己会保存文档状态。

## 8. 设计取舍

SDK 用子进程，不用进程内库。隔离与语言无关性换来的是启动成本与 IPC 序列化。如果做成进程内库，宿主崩溃会带走 agent，版本冲突也无法隔离。

协议包只放类型。这让 Python SDK 能独立实现，代价是类型与运行时行为可能漂移，需要靠快照测试对齐两侧的期望输出。

ACP 只发已提交的语义更新。编辑器的渲染简单了，首字延迟变大了。如果直接转发 delta，编辑器要自己拼消息块，拼错一次就留下半句话。

gateway 把鉴权放在传输层，把会话过滤放在 controller。安全判定集中，信息过滤分散。代价是两层都要知道 principal，任何一层漏判都会出问题。

补发只做在 Web 上。SDK 与 ACP 的客户端模型是「一次调用一个结果」，为它们加重放缓冲会让服务器内存随客户端数增长。Web 的客户端模型是「长期订阅」，值得为它维护 cursor。

sdk-minimal 不复用 base。重复的配置换来更小的启动面与更少的意外激活。代价是 base 修一个安全默认值时，sdk-minimal 要手动跟。

把重放能力绑定到传输，而不做成统一的 session 服务。Web 需要 cursor，stdio 不需要；统一做会让 SDK 的每次调用都带上一份重放缓冲。代价是三种形态的恢复语义不一致，客户端换形态时要重写恢复逻辑。

鉴权放在 connection，授权放在 remote-access，过滤放在 controller。三层各管一段，任何一层都能独立测试。代价是排查一次 403 要跑三个地方，日志里得能串起同一个请求。

## 9. 踩坑点

1. `session/prompt` 的 result 是入队回执。把它当最终回答，会在长 turn 上一直等不到结果。
2. SDK 的事件通知没有 cursor。想检测缺口只能读事件本体的 `seq`，而协议没有补发接口。
3. `session/resume` 不重放事件。断线期间的状态变化要客户端自己补，或者接受跳变。
4. ACP 的 `session/update` 按会话串行。跨会话并行，别用一个全局队列消费。
5. ACP 不发 raw delta。首字延迟与消息块提交时机绑定，别按 token 流做 UI。
6. gateway 的 Peer 在 upgrade 时固定。连接建立后再改 principal 不会生效，要重新建连接。
7. `observeEvent` 没有 cursor。用它做断线恢复会丢事件，恢复要用 `follow`。
8. `control` 的 baseline 只在流开始时给一次。断线重连后要重新拿 baseline，不能复用旧值。
9. `waterfall` 需要客户端回 `$events/result`。不回会让 Host 侧的调用一直挂着，直到超时。
10. `follow` 的 cursor 要先设再发快照。顺序反了会丢窗口内的事件。
11. 缺口不会自动修。listener 直接抛 `session event stream skipped seq N`，修复在客户端。
12. `session/attachment` 的授权是整个附件存储。多租户硬隔离要独立 Host 与独立 `DSH_HOME`。
13. sdk-minimal 没有 skill 与 subagent。从它往上加能力，要连带补 registry 与策略行。
14. headless 的 `--session-id` 是续跑，不是恢复界面。它不重放，只接着日志继续。
15. gateway 的事件转发选择在启动期固定。运行期改配置不会改变已连接客户端的订阅集合。
16. `session/query` 与 `session/prompt` 的返回结构不同。把两者都当字符串处理，会在 command 分支上拿到一个对象。

## 10. 源码索引

- SDK 协议：`packages/sdk/protocol/src/types.ts:18`（`InitializeParams`）、`:60`（反向请求）、`:93`（`SessionPromptResult`）、`:152`（`SessionEventNotification`）、`:194`（通知表）、`:210`（请求表）
- SDK server：`packages/sdk/server/src/server.ts`（`session/event` 转发、`agent/status`、`subagent.started/finished`）
- SDK client：`packages/sdk/client/src/client.ts:190`、`:223`；`packages/sdk/client/src/launch.ts`
- ACP：`packages/acp/acp/src/index.ts:136`（所有权过滤）、`:189`（能力）、`:241`（resume）；`session.ts:320`；`updates.ts:52`
- gateway：`packages/api/gateway/src/stream-protocol.ts`（mux 路径与帧）、`stream-server.ts:30`、`:92`；`client/journal-stream.ts:90`
- session-controller：`packages/api/session-controller/src/index.ts:135`、`:554`、`:587`、`:610`、`:631`；`web/controller.ts:32`；`history/listener.ts`
- 客户端会话状态：`packages/api/session-controller/src/client/sessions/session.ts:96`、`:499`、`:672`
- connection：`packages/client/connection/README.zh.md`（token、cookie、Peer、重连退避）
- remote-access：`packages/api/remote-access/src/index.ts`（role/binding/condition、`decide`、`snapshotFor`）
- 裁剪：`packages/bundle/headless/cordis.patch.yml`、`packages/bundle/sdk-minimal/cordis.patch.yml`、`packages/bundle/acp-app/cordis.patch.yml`、`packages/bundle/sdk-app/cordis.patch.yml`
- ACP 会话：`packages/acp/acp/src/session.ts:320`（事件翻译）、`packages/acp/acp/src/updates.ts:52`（`tool_call` 映射）
- 裁剪对照：`packages/bundle/headless/README.zh.md`、`packages/bundle/sdk-minimal/README.zh.md`、`packages/bundle/sdk-app/README.zh.md`
- 相关篇目：[01 解剖](/posts/dsh-architecture-anatomy.html)、[02 会话事件溯源](/posts/dsh-session-event-sourcing.html)、[S2-8 宿主与组装](/posts/dsh-host-composition.html)

## 最后

三种嵌入共用一条 session 事件流，差别在边界画在哪里：进程边界、编辑器边界、网络边界。边界决定传输，传输决定能不能重放，重放能力又反过来决定客户端要保存什么。

系列导航：上一篇 [S2-8 宿主与组装](/posts/dsh-host-composition.html)；下一篇 [S2-10 模型与工具](/posts/dsh-model-and-tools.html)。
