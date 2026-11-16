---
title: "嵌入与网关：把 DSH 接到别的程序里"
date: "2026-11-16T21:45:00+08:00"
summary: "从进程内 SDK 到 WebSocket 网关再到 ACP，拆会话生命周期、多路复用、鉴权、背压与重连补账。"
tags: ["architecture","harness","agent"]
slug: "dsh-sdk-and-gateway"
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）。结论来自 `packages/sdk`、`packages/api/gateway`、`packages/acp`、`packages/client/connection` 的源码与同目录 README，未做跨机或高并发压测；行号在该 commit 下逐条核对，协议行为以代码为准。

接入 DSH 有三条路，选哪条取决于你的程序需要管住什么资源。进程内 SDK 把子进程交给调用方，API Gateway 把一条 WebSocket 复用给多个客户端，ACP 只承诺一组标准方法和一次协议会话。三者的会话语义、错误模型和恢复方式都不一样，混着用会踩到彼此不兼容的假设。

## 1. 三条路，三种所有权

| 路线 | 包 | 你的程序拥有 | 传输 | 典型场景 |
| --- | --- | --- | --- | --- |
| 进程内 SDK | `packages/sdk/client` | 子进程的生死 | stdio 上的 JSON-RPC | 本机 CLI、CI 脚本、桌面应用 |
| API Gateway | `packages/api/gateway` | 一条 WebSocket | `/api/remote.mux` 多路复用 | 浏览器、远程客户端 |
| ACP | `packages/acp/acp` | 一个 ACP 连接 | stdio 上的 ndjson | 编辑器、别的 harness |

三条路共用 Session log、事件类型和工具执行管线，wire 与鉴权各写各的。SDK 的 `session.event` 推的是原始事件；Gateway 的 `$events` 流推的也是同一批事件，只是外面套了 Remote 流协议；ACP 把事件翻译成 ACP 的 session update，翻译过程会丢东西。

{{< fig "diagrams/01-three-routes.svg" "三条接入路线各自的所有权边界：SDK 拥有子进程，Gateway 拥有一条 mux socket，ACP 拥有一个标准连接。" >}}

粗分一下：本机嵌入优先 SDK；浏览器或跨机客户端走 Gateway；需要和编辑器生态互通才碰 ACP。三条路都要自己处理「断线后状态怎么补」，只是补法不同。DSH 自己被别的协议嵌入时暴露了哪些面，可以对照[嵌入协议](/posts/dsh-embedding-protocols.html)那篇。

## 2. SDK：进程就是会话边界

启动参数很薄：

```ts
args: [...dshLaunch.nodeArgs, '--profile', profile, ...patches.flatMap(path => ['--patch', path])],
```

这行在 `packages/sdk/client/src/launch.ts:143`。默认 profile 是 `sdk`（`launch.ts:132`），`dshBin` 可以换成另一个 dsh 安装（`launch.ts:133-135`）。没有端口，没有 URL，进程本身就是边界。

initialize 是唯一一次握手，超时 10 秒（`launch.ts:12`）。握手确定三件事：

1. 整进程用哪个 provider 与 model。两个字段必填，一个进程只有一条 route，后面的 session 都继承它，`session/resume` 时通过 `agent.model.select` 重新应用。
2. 审批与用户问答要不要走协议。`initialize.approvalRequests=true` 时，内部审批变成 `approval/request` 请求；`capabilities.userQuestions=true` 时，提问走 `user-question/ask`。客户端只该回 `allowed-once`、`rejected`、`cancelled`，非法响应或断线会被 fail-closed 成 `unavailable`。
3. 工作目录与 profile patch。

之后是 `session/prompt`、`session/query`、`session/wait`、`session/resume`、`shutdown`。

`session/resume` 载入历史时会修复被中断的最后一轮：已经产生的工具调用保留结果，没有结果的调用标记成中断，assistant 消息补上终止原因。客户端拿到的历史因此是自洽的，不会出现「有调用没结果」的悬空状态。

关闭有阶梯：stdin EOF → SIGTERM → SIGKILL，`shutdown` 默认等 1 秒（`client.ts:498`）。对端进程卡死时，这个阶梯保证你最多等一秒多就能拿到控制权。

一个 SDK 进程里可以跑多个 session，但它们共用一个 model route、一个审批处理器、一个 stdio。要按 session 隔离模型或凭证，只能多开进程。

Python 侧的镜像在 `python/sdk` 与 `python/sdk-runtime`，包名是 `deepseek-harness-sdk` 与 `deepseek-harness-runtime-bin`。传输一样是 newline-delimited JSON-RPC over stdio，区别在启动时要求显式指定 Harness home，不会默认读 `~/.dsh`。这个约束在多用户或容器环境里省掉一类事故。

`run()` 的语义值得单独说。`api.ts:260` 的 `run()` 等的是整只 agent 下一次 idle（`:295`），不是某一轮结束。`RunResult.finalResponse` 取这个区间里最后一条已提交的 root assistant 文本，不按因果归给某个 prompt。多个 prompt 并发时，这个字段只能回答「最后说了什么」，不能回答「这条 prompt 得到了什么」。

```ts
const harness = new DeepSeekHarness({ dshBin, profile: 'sdk' })
const session = await harness.resumeSession(id)   // api.ts:115
const result = await session.run('把测试跑一遍')  // api.ts:260
console.log(result.finalResponse)
```

一次完整的嵌入会经过这些节点，客户端在每个节点能看到的东西不一样：

| 节点 | 触发 | 客户端可见信号 |
| --- | --- | --- |
| spawn | `launch()` | 子进程 pid；stderr 是唯一的安全日志通道 |
| initialize | 握手 | `serverInfo`，version 固定 `0.0.1` |
| 建 session | 首次 `session/prompt`，未知 id 惰性建 agent 与 session | `session.event` 里的 session 元数据 |
| turn 进行 | prompt 或 query | `session.status` 变 `running`，事件持续到达 |
| idle | 一轮结束 | `session/wait` 返回，status 回 idle |
| shutdown | 关进程 | stdin EOF，超时后 SIGTERM，再 SIGKILL |

表里没有「取消」这一行，因为协议里没有这个方法。要把取消写进这张表，只能在 shutdown 之后补一步「重新 resume」。

## 3. wire 的形状与缺口

`packages/sdk/protocol/src/transport.ts` 是 286 行的 JSON-RPC 2.0 实现，newline-delimited：

- `id` 加 `method` 是请求，只有 `id` 是响应，只有 `method` 是通知。
- 畸形行被忽略，不报错也不断开。
- 没有 handler 的请求回 `-32601`，handler 抛错回 `-32603`（`transport.ts:62-63`，实现 `:235`、`:242`）。
- 没有 handler 的通知被丢弃。

方法表不长，但两边的职责分得清：

| 方向 | 方法 | 说明 |
| --- | --- | --- |
| client → server | `initialize` | 握手，定 route 与审批模式 |
| client → server | `session/prompt` | 低层排队入口，立刻返回 message id |
| client → server | `session/query` | 高层入口，认 slash 命令 |
| client → server | `session/resume` | 载入历史，修复被中断的最后一轮 |
| client → server | `session/wait` | 等到 idle |
| client → server | `session/working-directory/get` / `set` | 工作目录 |
| client → server | `shutdown` | 释放 root runtime，exit 0 |
| server → client | `approval/request` | 仅 `approvalRequests=true` 时出现 |
| server → client | `user-question/ask` / `.cancelled` | 人机问答 |
| server → client | `session.event` | 每个 session 的全部事件，不过滤 |
| server → client | `session.status` | `idle` 或 `running` |
| server → client | `session.query.completed` | query 的完成回执 |
| server → client | `subagent.started` / `finished` | 仅进程内 subagent |

缺口有三处，都在协议层：

1. 没有版本协商。`serverInfo.version` 传的是 `0.0.1`，客户端不校验。升级 dsh 后 wire 变了，错误会在运行时以字段缺失的形式出现。
2. 没有 `cancel`，也没有 `session/close`。要中断一个 turn，只能关进程；要清掉一个 session，只能关进程后从历史恢复。
3. 用户问答的等待是进程内的。断线时那个 question 不会自动取消，进程会挂在那儿，直到客户端回来或者被杀。

线上长这样，一行一帧：

```text
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"approvalRequests":true}}
{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"deepseek-harness-sdk-runtime","version":"0.0.1"}}}
{"jsonrpc":"2.0","id":2,"method":"session/prompt","params":{"sessionId":"...","contentBlocks":[{"type":"text","text":"..."}]}}
{"jsonrpc":"2.0","id":2,"result":{"messageId":"..."}}
{"jsonrpc":"2.0","method":"session.event","params":{"sessionId":"...","event":{"seq":42,"kind":"..."}}}
```

请求与响应靠 `id` 配对，事件没有 `id`，只能按到达顺序和 `seq` 处理。`id` 冲突不会被协议层拒绝，配对由调用方保证。

所以中断的粒度是进程，不是 turn。这一点和 ACP、Gateway 都不一样。

自己搭的时候，如果你的产品需要「停止这一轮」，用 SDK 就得把停止实现成「关进程 + 用 `session/resume` 重放历史」，并且接受 resume 会修掉被中断的最后一轮这个行为。想在协议上加 `cancel`，得同时改 server 的 dispatch 和 agent 的取消路径，不是加一个方法就完事。

## 4. 事件与排队

`session.event` 推每个 session 的全部事件，不做 kind 过滤。消费端要做两件事：按 `kind` 过滤自己关心的，按 `seq` 排序。协议没有定义去重语义，同一个 `seq` 出现两次时，本地按 `seq` 去重比信任「只推一次」稳。

`session/prompt` 是低层入口，立刻返回 message id（`client.ts:354`），排队交给 agent。它不做 slash 命令路由，`/compact` 这类文本会被当成普通 prompt 发给模型。

`session/query` 是高层入口，`run()` 走它，认四条原生命令：`/compact`、`/permission <preset>`、`/plan`、`/goal`。混用两个入口时要注意：query 的完成回执是 `session.query.completed`，prompt 没有对应的完成帧，只能靠 `session/wait` 或者观察 `session.status`。

{{< fig "diagrams/02-prompt-lifecycle.svg" "一次 prompt 从入队到 idle 的事件路径：prompt 立刻返回 message id，事件持续推送，wait 等到整只 agent 空闲。" >}}

两个入口的差别集中在三处：

| 维度 | `session/prompt` | `session/query` |
| --- | --- | --- |
| 返回 | 立刻回 message id | 跑完整轮，回 `session.query.completed` |
| slash 命令 | 不路由，当普通文本 | 认 `/compact`、`/permission`、`/plan`、`/goal` |
| 适合 | 聊天式连续输入 | 一次性任务、脚本 |

订阅接口有两个层级。`subscribe(filter)`（`client.ts:450`）收全部通知，filter 由调用方给；`subscribeSessionTree`（`:469`）按 root session 过滤，把子 agent 的事件也带出来。子 agent 的 session 不能直接 resume，`createSession` 会拒绝，错误文案是 `subagent session "..." cannot be resumed directly; resume its root session instead`。要恢复一棵子 agent 树，从 root 恢复，让它在执行中重新派生子 agent。


## 5. Gateway：一条 socket，多条逻辑流

Gateway 的假设和 SDK 相反：连接是稀缺资源，进程不是。浏览器只能开有限条 WebSocket，每条都要过一次鉴权，所以它把多条逻辑流塞进一条物理 socket。

路径常量在 `packages/api/gateway/src/stream-protocol.ts:7`：

```ts
export const REMOTE_STREAM_MUX_PATH = '/api/remote.mux'
```

一条 mux 上跑多条逻辑流，每条有 `streamId`。客户端帧四种：

| 帧 | 字段 | 作用 |
| --- | --- | --- |
| `open` | `streamId`、`endpoint`、`payload` | 开一条逻辑流 |
| `item` | `streamId`、`value?` | 上行一条数据 |
| `end` | `streamId` | 半关闭 |
| `cancel` | `streamId` | 取消这条流 |

服务端帧三种：`item`、`error`（带 `code`、`message`、`details`）、`end`。解析用 exactKeys 白名单，多一个字段整帧拒绝，少一个必填字段也拒绝。这个选择让协议没有「以后再加字段」的空间，加能力只能加新方法或新 endpoint。

客户端这一侧的投影与连接状态怎么维护，在[客户端架构](/posts/dsh-client-architecture.html)里展开。事件流占用两个固定 endpoint：`$events` 推事件，`$events/result` 回结果（`stream-protocol.ts:10`、`:13`）。`$events` 是唯一被连接层当作 generation source 的流，只有 answering client 会注册它。

{{< fig "diagrams/03-mux-streams.svg" "一条 mux socket 上的四条逻辑流：$events 负责代际与增量，普通流按 endpoint 分派，cancel 只结束单条流。" >}}

一帧 open 加若干 item，最后 end 或 cancel：

```json
{"type":"open","streamId":"ev","endpoint":"$events","payload":{}}
{"type":"item","streamId":"ev","value":{"seq":42,"event":{"kind":"..."}}}
{"type":"item","streamId":"ev","value":{"seq":43,"event":{"kind":"..."}}}
{"type":"cancel","streamId":"ev"}
```

`streamId` 是客户端生成的字符串，只在一条连接内有效。同一连接上并行开多条流时，单条流内部的帧按产生顺序写出，不同流之间按到达顺序交错。


帧大小和压缩也有约定。`compression` 默认 `deflate`，但只对 `text.length >= 1024` 的帧启用（`stream-server.ts:329`），小帧压缩反而更费。每条流的上行窗口默认 256 KiB（`DEFAULT_STREAM_INBOX_BYTES = 262_144`，`index.ts:150`），计的是 JSON 外壳加 payload 的字节数。

## 6. 鉴权：从 token 到 Peer

Gateway 不给未鉴权的连接开流。整条链是：launch token → 签名 cookie → Peer → `remoteAccess.decide`。

进程启动时生成随机 launch token，`dsh-web-app` 打印带 `?token=` 的 URL。浏览器第一次请求带 token，服务端校验后写一个 authority 绑定的签名 cookie，再 302 到不带 token 的 `./`。cookie 默认 30 天，host-only、`Path=/`、`HttpOnly`、`SameSite=Strict`，listener 是 TLS 时加 `Secure`。签名密钥存在 `ctx.credentials` 的 `client-connection/browser-session` 记录里，落在 `$DSH_HOME/.credentials.yaml`。

信任围栏在 `src/api-request-trust.ts`：Host 必须是 loopback、等于 listener 的 bind IP，或者命中 `trustedHosts`；Origin 必须同 authority；`sec-fetch-site: cross-site` 直接拒。Host 或 Origin 不过回 403，可信但没鉴权回 401。`dsh web --host 0.0.0.0` 不支持，这也是原因之一。

`/connect?token=...&interactions=answer|none` 是给非浏览器客户端用的：立即建 Peer，302 到 mux 目的地，同时下发 API cookie。API 连接默认 `interactions: 'none'`，不能弹用户问答；一个 API socket 负责清理，重复 attach 回 409，关闭即删 Peer。

`/connect` 的完整走位是这样：

1. 客户端拿到带 `?token=` 的地址，token 来自启动时打印的那行 URL。
2. `GET /connect?token=...&interactions=answer`，服务端校验 launch token。
3. 校验通过后建 Peer，principal 绑定 owner，interactions 按 query 参数固定。
4. 响应 302 到 mux 目的地，同时 `Set-Cookie` 下发 API cookie。
5. 客户端用这条 cookie 连 `/api/remote.mux`，之后不再带 token。

第 3 步的 Peer 和 cookie 是两套东西：cookie 证明「这个浏览器曾经拿过 token」，Peer 证明「这条连接当前被授权做什么」。cookie 失效时 Peer 还在，直到连接关闭。

Peer 是授权单位。`packages/client/connection/src/peer.ts:26`，id 是随机 UUID，principal 和 interactions 在生命周期内固定。每个 Host RPC 方法和每条 WebSocket 流都要一个已鉴权的 Peer，拒绝走 `remote-access/denied`，details 是 `{action, reason}`。owner principal 的调用跳过决策，浏览器共享 Peer 就是 owner principal 加 `interactions: 'answer'`。

| 现象 | 含义 | 处理 |
| --- | --- | --- |
| 403 | Host 或 Origin 不在信任围栏内 | 改 Origin，或者把地址加进 `trustedHosts` |
| 401 | 可信但没带有效 cookie | 重新走 `/connect` 拿 cookie |
| 409 | 同一个 Peer 重复 attach | 复用已有连接，别新建 |
| `gateway/peer-disposed` | Peer 在内存状态转移后已销毁 | 重连，别重试同一帧 |
| `remote-access/denied` | 资源级授权拒绝 | 看 details 的 action 与 reason |

## 7. 背压：一条流溢出不杀整条 socket

上行没有 WebSocket 自带的流控可用，Gateway 自己实现了窗口。每条逻辑流一个 `UplinkInbox`（`stream-server.ts:362`），上限是 `streamInboxBytes`，默认 262144 字节，计的是 payload 加每帧 JSON 外壳的字节数。

```ts
push(value: unknown, frameBytes: number): void {
  if (this.failure !== undefined || this.closed) return
  if (this.ended) { this.violate(new RemoteError('gateway/protocol', '...uplink item after end')) ; return }
  if (this.bytes + frameBytes > this.maxBytes) {
    this.violate(new RemoteError('gateway/uplink-overflow', `...exceeded ${String(this.maxBytes)} buffered bytes`))
    return
  }
  this.queue.pushBack({ value, bytes: frameBytes })
  this.bytes += frameBytes
  this.signal()
}
```

（`stream-server.ts:375-399`，错误文案我截短了。）检查顺序固定：先看流是否已 end，再看字节窗口。两种违规都只终止这一条逻辑流，socket 上其他流继续跑。这也是为什么 `streamInboxBytes` 调大不是万能药：窗口越大，服务端积压的上行数据越多，客户端下行被阻塞的时间也越长。

心跳在 mux 层，默认 2 秒一次（`DEFAULT_WEBSOCKET_HEARTBEAT_INTERVAL_MS = 2_000`，`index.ts:149`）。连续错过 2 个 interval 就终止连接（`MAX_MISSED_HEARTBEATS = 2`，`stream-server.ts:42`，判定循环在 `:136-145`）。定时器 `unref` 过（`:149`），不会吊住进程退出。

压缩默认开（`deflate`），阈值 1024 字符（`stream-server.ts:329`）。事件里大块的 assistant 文本会走压缩，几百字节的控制帧不压缩。

窗口计费有个容易忽略的点：每帧 JSON 外壳也算。一条 300 字节的 payload 加上 `streamId`、类型字段和转义，实际占的窗口可能接近 400 字节。按 payload 估窗口，会在长连接上提前溢出。

这里没有按流的 credit 或暂停写入。上游写得比客户端读得快，就只能等窗口溢出，溢出后这条流死掉。自己搭的时候，上传大文件或长文本要自己做分片和确认：发一片，等一条回执，再发下一片；别指望 Gateway 在窗口满的时候给你一个「暂停」信号。

## 8. 重连与 resync：谁补哪一段

浏览器断线重连不是「重新打开页面」那么简单。客户端手里有投影、有列表、有当前打开的 Session 历史窗口，服务端那边有 Peer、有逻辑流、有 generation。恢复时要让两边对上。

连接层用 generation 标记一次连续的事件序列。只有 answering client 会把内部 `$events` 逻辑流注册为唯一的 generation source。Host 的顺序是：先装增量 listener，再发一帧

```json
{ "type": "ready", "clientId": "...", "host": { "home": "..." } }
```

然后才开始推事件。客户端在收到 ready 之前不发布 generation、不调 `onConnected`，否则基线会跑到增量观察前面，丢事件。

退避参数在 `packages/client/connection/src/recovery-config.ts`：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `backoffBaseMs` | 500 | 第一次重试的等待 |
| `backoffFactor` | 2 | 每轮倍数，必须有限且 ≥ 1 |
| `backoffMaxMs` | 10000 | 上限 |
| `generationReadyWarnMs` | 3000 | ready 迟迟不来时的警告阈值 |
| `generationReadyTimeoutMs` | 15000 | 等 ready 的超时 |

实际等待在 500ms、1s、2s、4s、8s、10s 这些档位上加 50% 到 100% 的 jitter，直到恢复。每次重试都要求 Gateway 换掉物理 socket 并重开 `$events`。`ctx.connection.reconnect()` 会中断在途工作、重置序列、立刻从第一档重新开始；浏览器 offline 时发 disconnected 并暂停自动重试，online 后从 500ms 档重来。

ready 之后的补账分两层。Session 历史的窗口由 `session.resync()` 重建：

```ts
async resync(): Promise<void> {
  if (this.openState === 'cold') return
  this.openGeneration++
  const events = this.events
  this.events = undefined
  await events?.dispose()
  this.openPromise = null
  this.openState = 'cold'
  this.baseSeq = SessionLogOffset(0)
  this.notifier.markDirty()
  await this.open()
}
```

（`session.ts:499-511`。）注意 `baseSeq` 归零，再 `open()` 重新拉窗口，`installWindow` 用 `entries[0].event.seq` 写回 `baseSeq` 和 `pendingHistory.beforeSeq`（`:669`）。往前翻历史仍然用 `beforeSeq` 分页。

投影和列表走另一条流，`manager.handleConnected()`（`manager.ts:737`）负责清空重建：清空所有 projection store、重置 list 的进行中状态、`refreshList()`、按 parent 收集需要重拉的投影、abort 掉在途的 projection 请求、再逐个 `refreshProjections(parentSessionId)`。

| 状态 | 谁补 | 怎么补 |
| --- | --- | --- |
| 事件序列 | 连接层 | 换 socket，重开 `$events`，以 ready 为界 |
| 当前 Session 历史窗口 | `session.resync()` | `baseSeq` 归零，重开窗口 |
| 投影与列表 | `manager.handleConnected()` | 清空 store，全量重拉 |
| 已打开的 Session follow 流 | API Gateway | 各流独立续上，不经过 resync |
| 本地未发送的输入 | 客户端 | 不补，重连后要用户重发 |

最后一行是设计选择，不是遗漏：上行不重放，重开的流拿不到旧的半截上传。自己搭的时候，断线期间别在本地接着递增 seq，也别把「已写入本地状态」当「服务端已确认」；回来先 resync，再按新基线续。

## 9. ACP：标准协议的另一列

ACP 走的是另一套假设：对面可能是编辑器、可能是另一个 harness，双方只认标准方法名，不认 DSH 的 endpoint。

入口是 `ndJsonStream` 直接包 `process.stdout` 与 `process.stdin`（`packages/acp/acp/src/index.ts:374`），应用通过 `createAcpAgentApp` 挂两组装载：

- `onRequest`：`initialize`、`authenticate`、`session/new`、`session/list`、`session/resume`、`session/close`、`session/setConfigOption`、`session/prompt`。
- `onNotification`：`session/cancel`。

审批走标准方法：`conn.request(methods.client.session.requestPermission, params)`（`:171`），返回 `cancelled` 时按取消处理。关闭一个 ACP session 时，`AcpSession.close` 在第一个 await 之前就同步 cancel，再 drain 子代与持久化，最后把多个错误聚合成 `AggregateError`（`:399-420`）。这个顺序是为了让「已经发出的工具调用」在取消之后不再产生新的副作用。

ACP 的定位写在 README 里：automation-only，只暴露标准 ACP v1 面。人类用的卡片、plan、标题、todo、terminal、elicitation 都不在里面。限制也很具体：

- 只有一个 primary workspace。
- 只收 PNG、JPEG、WebP、GIF，需要 durable attachment store 和图像路由。
- MCP 只接 tools，不接 resources 和 prompts，挂载细节见[MCP](/posts/dsh-mcp.html)。
- 没有 transcript 重放、session 删除、fork、`session/load`、modes、commands、plans、terminals、客户端文件系统操作、elicitation。

和 SDK 放一起看，差别不在传输，在所有权模型：

| 维度 | SDK | ACP |
| --- | --- | --- |
| 协议归属 | DSH 自己的 wire | 标准 ACP v1 |
| 连接内 session 数 | 多个，共用 route | 多个，可 create/list/resume/close |
| 模型选择 | initialize 时定死，整进程一个 | 每 session 通过 config option 选 |
| MCP | 由 profile 决定 | 连接时挂载，只接 tools |
| 中断 | 只能关进程 | `session/cancel`，按 session |
| 适合 | 本机嵌入 | 跨工具互操作 |

`session/setConfigOption` 管的是每 session 的选项，主要是模型一类的枚举。它和 SDK 的 initialize 相反：SDK 在握手时把 route 定死，ACP 允许连接内逐个 session 调。MCP 也是连接时挂载，挂上之后所有 session 共享，只接 tools。

`session/cancel` 这一条差别最大。ACP 能在不杀进程的前提下取消一轮，代价是它只暴露标准面，DSH 特有的卡片和 todo 传不过去。

## 10. 一次重连的完整走位

把前面几层拼起来，浏览器从断线到可恢复会走这些步：

1. `$events` 流结束、Remote 流报错、首帧不是 ready、事件帧畸形，任何一个都让当前 generation 失效。
2. `ConnectionController` 发 disconnected，暂停这个 generation 的事件处理，本地已渲染的状态先留着。
3. 进入退避，第一档 500ms，等待时间加 50% 到 100% 的 jitter。
4. 到点后要求 Gateway 换掉物理 socket，重开 `$events`。
5. Host 装增量 listener，发 ready 帧。
6. 客户端收到 ready，发布新 generation，调 `onConnected`。
7. 每个已打开的 Session 调 `resync()`，`baseSeq` 归零，重开历史窗口，`installWindow` 写回新基线。
8. `manager.handleConnected()` 清空投影与列表缓存，重拉 list，按 parent 重拉投影，abort 在途请求。
9. 三段状态 connecting、syncing、ready 依次切换，输入框在 ready 之前保持禁用。

顺序错一步的后果不完全一样：

| 错法 | 后果 |
| --- | --- |
| 先发事件、后发 ready | 事件落在旧 generation，被丢弃或重复 |
| ready 之前就发布 generation | 基线早于增量观察，丢事件 |
| 不做 resync 直接续 seq | 历史窗口错位，消息重复或缺失 |
| 只 resync、不重拉投影 | 列表标题与状态停在断线前 |
| 重连后重用旧的待发送帧 | 服务端按新连接处理，可能重复执行 |

最后一步的输入禁用不是体验问题。断线期间用户输入的内容没有服务端确认，恢复之后要不要重发得由用户决定，界面必须把这个状态露出来。

{{< fig "diagrams/04-reconnect-walkthrough.svg" "断线到 ready 的九个步骤：换 socket、重开 $events、等 ready、resync 历史窗口、重拉投影，最后才放开输入。" >}}

## 11. 设计取舍

1. SDK 用子进程而不是库调用。好处是崩溃隔离、依赖隔离、可以指向另一个 dsh 安装；代价是冷启动、二进制分发和 stdio 这条不能有任何杂音的通道。server README 专门写了一句：stdio 只走 JSON-RPC，部署时不能配 stdout logger。一个 `console.log` 就能往流里塞一行畸形数据，解析器会安静地忽略它，你只看到某个请求永远不回。

2. Gateway 复用一条 socket。浏览器对同源 WebSocket 的数量有限制，鉴权也只想做一次。代价是队头阻塞和自建背压：一条流的大帧会挡住后面的流，窗口满了只能杀流。

3. 帧解析用 exactKeys 白名单。多一个字段就拒绝，看起来不近人情，换来的是「字段含义不会随版本漂移」。要加能力就加新方法或新 endpoint，让老客户端在 `-32601` 上明确失败，而不是在新字段上默默误解。

4. Peer 在内存状态转移之后才 dispose。先完成状态转移再销毁 Peer，晚到的帧拿到 `gateway/peer-disposed`，不会写进一个已经半关闭的 Session。代价是关闭路径要保证「转移」和「销毁」的顺序，不能图省事在 finally 里直接删。

5. resync 清空投影而不是增量补。增量补要判断哪些事件在断线期间漏了，而漏判的代价是界面显示旧状态。清空重拉更贵，但结果是确定的。已打开的 follow 流不受影响，因为它们各自独立续。

6. SDK 不提供 cancel。进程是隔离单位，取消一个 turn 而不动进程，需要 agent 内部有一条完整的取消路径，还要处理「已经送达的输入不能撤回」。这条路径在 ACP 里有，在 SDK 里没有，取舍是让 SDK 的实现面小。

7. ACP 只做 automation 面。标准协议的表达力装不下卡片、todo、plan 这些交互，硬塞要么改标准，要么在 metadata 里塞私有字段。选择是不塞，需要这些能力的客户端走 Gateway。

## 12. 自己搭的时候

1. 先用 `session/query` 跑通一轮，再接 `session/prompt`。query 有完成回执，调试时能明确知道这一轮结束了；prompt 只能看事件流自己判断。
2. 中断策略要在第一版就定。SDK 的中断等于关进程，恢复等于 `session/resume`；如果你打算以后换成 Gateway，中断会变成「cancel 一条流」，两套语义别混在一个抽象里。
3. 事件消费者按 `kind` 建白名单，别按「不认识就忽略」写。schema 演进时，白名单会让你在升级当天发现问题，黑名单会静默丢一半。
4. 断线重连的 UI 状态要分三段：connecting（退避中）、syncing（resync 与投影重拉）、ready。把三段合成一个 loading，用户会以为卡死。
5. 把 Gateway 的 Peer 当成用户身份。Peer 只在一条连接内有效，关掉就没了；要跨连接记住「谁」，得自己在上层做映射，cookie 和 Peer 是两套东西，别混着存。

## 13. 踩坑点

1. 在 Gateway 或 SDK 的部署里配 stdout logger。stdio 是协议通道，日志必须走 stderr 或文件，否则解析器会看到一堆畸形行然后安静地忽略。

2. 以为 initialize 的 model route 是 per-session。整进程一个 route，`session/resume` 只是重新应用它；要在同一进程里换模型，只能换进程。

3. 拿 `session/prompt` 发 `/compact`。这个入口不做 slash 路由，命令会被当普通文本发给模型；要命令走 `session/query`。

4. 把 `streamInboxBytes` 当成吞吐旋钮。它只是单条流的缓冲上限，调大之后积压更久，溢出时照样杀流；实际的吞吐要靠客户端分片和确认。

5. 断线后继续用旧 `baseSeq`。resync 会把基线归零，再用旧值算增量，历史窗口会错位，表现是消息重复或缺失。

6. 开了 `approvalRequests` 却不处理失败响应。非法响应或断线会 fail-closed 成 `unavailable`，工具调用直接失败，不会自动重试。

7. 直接 resume 一个 subagent session。服务端会拒绝，错误文案明确要求恢复 root；正确做法是从 root 恢复，让它在执行中重建子树。

8. 以为心跳只影响空闲连接。心跳是两个 interval 没收到任何 frame 就终止，长时间的上行阻塞也会让连接看起来像死的，客户端下行背压是唯一的间接信号。

## 14. 源码索引

- `packages/sdk/protocol/src/transport.ts:62`：`-32601` 与 `-32603` 的注释说明。
- `packages/sdk/protocol/src/transport.ts:235`：无 handler 的请求回 `-32601`。
- `packages/sdk/client/src/launch.ts:12`：`DEFAULT_INITIALIZE_TIMEOUT_MS = 10_000`。
- `packages/sdk/client/src/launch.ts:143`：子进程参数拼装。
- `packages/sdk/client/src/client.ts:354`：`prompt` 立即返回 message id。
- `packages/sdk/client/src/api.ts:260`：`run()` 等到整只 agent idle。
- `packages/sdk/server/src/server.ts:215`：`initialize` 处理。
- `packages/sdk/server/src/server.ts:501`：dispatch 的 switch 起点。
- `packages/api/gateway/src/stream-protocol.ts:7`：`/api/remote.mux` 常量。
- `packages/api/gateway/src/stream-server.ts:375`：`UplinkInbox.push` 的两种违规。
- `packages/api/gateway/src/stream-server.ts:329`：1024 字符压缩阈值。
- `packages/api/gateway/src/index.ts:149`：心跳 2 秒与窗口 256 KiB 常量。
- `packages/api/gateway/src/index.ts:276`：`admitConnection` 与拒绝分支。
- `packages/api/gateway/src/access.ts:38`：`callResources` 的授权顺序。
- `packages/client/connection/src/recovery-config.ts:26`：退避默认值。
- `packages/api/session-controller/src/client/sessions/session.ts:499`：`resync()`。
- `packages/api/session-controller/src/client/sessions/session.ts:669`：`installWindow` 回写 `baseSeq`。
- `packages/api/session-controller/src/client/sessions/manager.ts:737`：`handleConnected()`。
- `packages/acp/acp/src/index.ts:374`：`ndJsonStream` 包 stdio。
- `packages/acp/acp/src/index.ts:399`：`AcpSession.close` 的取消与聚合错误。

## 15. 最后

选路线时先问自己：谁拥有那个进程或连接。SDK 把子进程当成隔离单位，适合本机嵌入；Gateway 把一条 WebSocket 当成分发单位，浏览器与跨机客户端挂在它下面；ACP 把一次协议会话当成互操作单位，对面可以是编辑器或另一个 harness。先用 SDK 把会话语义跑通，再上 Gateway 解决分发，需要跨工具时再接 ACP。

SDK 的缺口（没有版本协商、没有 cancel、没有 session close）是当前版本的事实，不是藏在文档角落的待办。基于它做产品，要把「关进程」当作 API 的一部分设计进错误处理和重试策略里。Gateway 的缺口在背压，窗口溢出杀流这件事必须让上层知道，否则用户看到的是「上传到一半没了」。重连那一层看起来最琐碎，实际最影响体感，ready 帧、resync、投影重拉三件事对齐了，断线才像一次短暂卡顿而不是一次数据事故。
