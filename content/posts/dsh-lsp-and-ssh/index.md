---
title: "LSP 与远程执行：跨进程的两种接法"
date: "2026-11-14T19:30:00+08:00"
summary: "LSP 把编辑器能力借给 agent，SSH 把执行搬到另一台机器：两种跨进程接法的生命周期、断线与权限差别。"
description: "拆 lsp-stdio 与 ssh 四件套：诊断如何进上下文、连接如何复用、密钥怎么管、失败时模型看到什么。"
tags: ["harness", "architecture", "concurrency"]
slug: "dsh-lsp-and-ssh"
---

> 依据：仓库 commit `fcc0ad46`（0.2.2-rc.1）。证据等级 A：结论来自 `packages/lsp/*`、`packages/ssh/*` 源码、包内 README 与 `docs/subsystems/lsp.zh.md`、`docs/subsystems/ssh.zh.md`。没有起真实语言服务器，也没有连真实 SSH 主机，凡涉及运行时表现（延迟、远端进程是否被杀掉）的描述都以代码路径为准，未做实测。

三个现象放在一起看，会发现问题不在协议，在生命周期。

`lsp` 工具返回的是编辑器里的定义与引用位置，server 进程却不属于任何一个会话。同一个工作区开两个会话，只起一个 server；会话结束，server 继续活着，直到插件卸载。

SSH 那套更彻底。`read` 打开的文件在另一台机器上，`bash` 启动的进程也在另一台机器上，`sandbox` 的判定同样在远端做。模型侧看不到任何新工具，工具名和参数都没变。

两者都会中途断。LSP server 崩了，下一次查询会透明重试一次；SSH 连接断了，正在跑的远端进程结果变成未知，客户端不会重连重放。这两种失败策略的差别，是本篇后半段的重点。

## 0. 结论

| 维度 | LSP（`lsp` + `lsp-stdio`） | SSH（`ssh` + 四件套） |
| --- | --- | --- |
| 模型看到什么 | 一个工具，四种操作 | 零个新工具，旧工具换了落点 |
| 跨的是什么 | 语言服务器进程 | 整台远端主机 |
| 生命周期单位 | workspace target | 一个 `SshConnection` |
| 起停时机 | 首次查询懒启动，插件卸载时统一拆 | 插件加载时连接并等 hello，`dispose` 时拆 |
| 失败策略 | 传输失败重试一次，查询只读 | 不重连、不重放，结果标记未知 |
| 上下文成本 | 有上限：100 个位置、16000 字符 | 无新增上下文，路径语义变了 |

两条接法的共同点是接缝选择：都实现已有的 seam（`ctx.lsp`、`ctx.fs`、`ctx.subprocess`、`ctx.sandbox`），不新造模型协议。代价也相同，失败要翻译成模型能读的结果，不能抛成宿主异常。

启用成本不一样。LSP 完全可选，没配 `lsp-stdio` 时 `lsp` 工具根本不存在，模型回到搜索；SSH 一启用就同时换掉三个 seam，回退要把组合改回去。后面的章节按这个顺序展开。

## 1. 两种跨进程，两种边界

LSP 借的是能力。它只读源码、只回答四个问题，不写文件、不执行命令。这条边界让很多设计变简单：查询可以重试，实例可以共享，不需要沙箱，也不需要审批。

SSH 搬的是执行。文件系统、子进程、沙箱判定三件事一起搬走，模型仍然以为自己在本地工作。这条边界带来的问题正好相反：路径含义变了，失败后果变重，凭据要重新安排。

`docs/subsystems/lsp.zh.md` 把 LSP 定义成「把语言服务器的查询能力暴露给模型」，并明确它不接管编辑、不接管诊断。`docs/subsystems/ssh.zh.md` 的定位是「部署方拥有的远程执行环境」，并强调 Harness、模型传输、会话存储都留在本地。

一个提供查询，一个提供执行环境。前者是只读旁路，后者是执行面的整体替换。后面所有差别都能从这句话推出来。

寿命也不一样。LSP 实例随工作区存在，一台语言服务器可以连续服务几十次查询；SSH 连接随插件配置存在，一条连接同时承载文件、进程、沙箱三类调用。两者都不采用「一次请求一条连接」，原因相同：握手贵，状态在远端。

## 2. LSP 的接法：四种操作与一条注册规则

seam 的操作集合是一个封闭联合：

```ts
// packages/lsp/lsp/src/types.ts（LspOperation）
type LspOperation =
  | 'goToDefinition'
  | 'findReferences'
  | 'goToImplementation'
  | 'hover'
```

闭集是刻意的。加第五种操作要同时改 provider 接口、工具 schema、每个 provider 实现和渲染函数，编译器会把这些点全部指出来。代价是这个版本没有 documentSymbol、没有 callHierarchy、没有 rename。模型想要工作区符号列表，只能退回搜索。

坐标在内部是零基 UTF-16，工具对外是一基。这个换算只发生在 `tool-lsp` 一层，provider 拿到的始终是零基。`LspQueryRequest` 的四个字段 `operation`、`filePath`、`position`、`workspaceRoot` 都是必填，没有 `resolve()` 第二段。

provider 注册有一条硬规则：原子。`registerProvider` 先预留 id 和扩展名表里的每一项，任何冲突都不发布，直接抛 `LSP_CONFLICT` 或 `LSP_INVALID_PROVIDER`。半注册的 provider 会让后续查询按扩展名选到一个空壳，这类错误很难在运行时定位。

选择是按查询做的，与注册顺序无关。扩展名到语言的映射来自 provider 自己，只用来同步临时文档，不用来选 provider。选不到 provider 时抛 `LSP_UNAVAILABLE`，工具层把它渲染成一条错误结果。

`LSP_MALFORMED_RESPONSE` 单独存在，说明 provider 返回了结构不合法的位置。这类错误不该被当成「没找到」，否则模型会以为代码里真的没有定义。

六个错误码各有分工，调用方按 code 路由，不按 message：

| code | 出现位置 | 模型应当怎么读 |
| --- | --- | --- |
| `LSP_UNAVAILABLE` | 没有 provider 认领该扩展名 | 这个文件类型没人管，换搜索 |
| `LSP_INVALID_PROVIDER` | provider 声明的映射为空或非法 | 配置错误，不是查询问题 |
| `LSP_CONFLICT` | id 或扩展名已被占用 | 两个 provider 抢同一个扩展名 |
| `LSP_DISPOSED` | 插件已卸载、实例已拆 | 环境没了，需要重启插件 |
| `LSP_UNSUPPORTED_OPERATION` | server 不声明该能力 | server 不支持，不是位置问题 |
| `LSP_MALFORMED_RESPONSE` | provider 返回结构不合法 | server 行为异常，可重试一次 |

工具的系统提示写得克制，原文只有四句：

```
Use search/read for ordinary navigation. Use lsp when textual matches are
ambiguous or before a change requires precise definitions, implementations,
or references. Positions are one-based line and character (UTF-16) at the
cursor; an off-symbol position may return no results. findReferences always
includes the declaration.
```

第一句在给成本判断：搜索便宜，跳转精确。最后一句提前说明「引用里包含声明」，避免模型把声明当成多余结果删掉。位置说明写一基与 UTF-16，是因为换行与宽字符会让字符数偏移，按字节算会错开一列。

## 3. 生命周期：按项目起，按需起

{{< fig "diagrams/01-lsp-lifecycle.svg" "图注：会话、工作区与语言服务器实例的关系。实例按 workspace target 缓存，首次查询时创建，同一工作区的查询排队，不同工作区并行。" >}}

`LocalLspProvider.instances` 的键是 `workspace.target.targetKey`，取值来自文件系统 provider。它按项目分，不按会话分。两个会话打开同一个仓库，共用一台 server；换一个仓库，才会新建。

创建是懒的。注册 provider 时不 spawn，第一次有匹配的查询才走 `createInstance`。这一步做完 `initialize` 握手，之后这个实例一直活着，直到插件卸载或传输层坏掉。

握手时 `processId` 传 `null`。注释里写了原因：subprocess provider 可能把进程放在另一个 PID 命名空间甚至另一台机器上，宿主 PID 反而会让 server 去监控一个不相关的进程。`rootUri` 用工作区的 `fileUrl`，`positionEncoding` 只接受 `utf-16`，server 返回别的编码直接拒绝。

客户端声明的能力只有几项：`positionEncodings: ['utf-16']`、工作区文件夹、`workspace/configuration`、hover 的 markdown 与 plaintext、definition 与 implementation 的链接支持、references 的空对象。动态注册关掉。这张能力表就是 host 愿意承担的全部义务，server 不能要求更多。

查询排队按工作区分。`enqueue` 把任务挂到该工作区的链尾，链尾永不 reject，被取消的等待者不会把后面的任务一起拖死。同一工作区串行，不同工作区并行，跨工作区没有全局锁。

串行的理由在文档生命周期。每次查询都临时 `didOpen` 一个文档、查完 `didClose`，两个查询交叠会让同一文档的版本号互相覆盖。串行是这里最省事的正确解。

结构可以压成十几行：

```ts
// 结构示意，非逐字源码
query(req, signal) {
  const key = workspaceKey(req.workspaceRoot)   // 来自 ctx.fs，按项目不按会话
  return enqueue(key, signal, async () => {
    const instance = await this.instanceFor(key) // 懒创建，复用，或发现死亡后重建
    return instance.query(req, signal)           // 临时 didOpen → 请求 → didClose
  })
}
```

`workspaceKey` 由文件系统 provider 算，不用字符串直接比。远端工作区下同一个仓库可能有两种写法（`/home/me/repo` 与 `/home/me/repo/`），规范化之后才能命中同一个实例。这条规则对本地与远程都成立。

实例死亡有三种来源：进程关闭、已 dispose、连接层报错。查询结束后发现 `instance.dead` 就 dispose 并从 Map 里删除，下次查询重建。传输错误允许透明重试一次，因为查询是只读的，重放没有副作用。

重试次数是一次，不是三次。传输层错误通常意味着 server 已经不可用，重试一次是为了覆盖「连接被回收但进程还活着」这一类瞬时故障；连续失败两次就说明实例有问题，继续重试只会把延迟乘上去。

自己搭 Agent 时，这条重试规则要跟着你的操作集合走。只读查询可以重试，格式化、code action、rename 这类有副作用的操作不能，重试一次可能改两遍。

`disposeAll` 会先停路由再拆实例，收集所有失败后抛单个错误或 `AggregateError`。插件卸载期间某台 server 卡住，不会让其余实例漏拆。

## 4. 一次查询：临时打开、四次预算

{{< fig "diagrams/02-lsp-query.svg" "图注：一次 lsp 查询的四段。读文件走 ctx.fs，临时 didOpen 全文，发请求，finally 里 didClose；预算卡在文档大小、位置数、结果字符数与工具超时四处。" >}}

工具先要一个会话工作区。`exec.agent` 没有 cwd 时抛 `LSP_WORKSPACE_REQUIRED`，不会退化成用进程当前目录。理由是 `workspaceRoot` 必须来自会话，工具参数里的路径只用来定位文件。

一次查询的顺序是固定的：

1. 用 `ctx.fs` 读文件，路径相对工作区解析，并做包含性检查。越界报 `resolves outside the workspace`。
2. 文件超过 `maxDocumentBytes`（400 万字节）报错，不会截断后送去解析。
3. `didOpen` 发全文，版本号固定为 1。
4. 发 `textDocument/definition`、`references`、`implementation` 或 `hover`。`findReferences` 强制带 `includeDeclaration: true`。
5. `finally` 里 `didClose`，只有真的打开过且实例没死才发。

临时打开的理由很实际。server 不知道 harness 的编辑，也不保证文件监听可用；每次把当前磁盘内容送进去，结果与磁盘一致，不依赖 server 的缓存状态。

不是所有 server 都支持这种用法。`supportsTransientOpen` 检查 `textDocumentSync`，不支持就抛 `LSP_UNSUPPORTED_OPERATION`，错误信息写明「server does not support the transient textDocument/didOpen this host requires」。这里选择报错，不选择退化成「打开但不关闭」。

取消路径也围绕这条生命周期设计。请求被取消时先发 `$/cancelRequest`，等 `killGraceMs`（2000 ms）看 server 是否收手；不收手就拆掉整个实例。理由写在 `raceAbort` 附近：一个忽略取消的 server 会让下一次排队查询撞上还没关闭的文档。

还有一类消息容易被忽略：server 反向发来的请求。host 只答四类：

- `workspace/configuration`：按请求里的每一项，回配置里的静态值。
- `window/workDoneProgress/create`、`client/registerCapability`、`client/unregisterCapability`：回 `null`，不改变任何状态。
- `workspace/applyEdit`：拒绝，理由是「workspace/applyEdit is not permitted by this host」。
- 其余：一律拒绝，错误信息带上方法名。

这张清单决定了 host 的只读边界。server 想让客户端改文件、注册动态能力、弹进度条，都做不到。反过来说，server 如果依赖动态注册才能提供某能力，在这里会表现为能力缺失，而不是报错。 

预算有四处，彼此独立：

| 预算 | 默认值 | 超了怎么办 |
| --- | --- | --- |
| 单文档大小 | 4 MB | 报错，不截断 |
| 位置数 | 100 | 截断并追加省略说明 |
| 结果字符 | 16000 | 截断并追加省略说明 |
| 工具超时 | 60 s | 走 timeout-policy，结果是 `TOOL_TIMEOUT` |

前两条在 `lsp-stdio`，后两条在 `tool-lsp`。工具注册时把 `timeoutMs` 交给工具注册表，`timeout-policy` 从注册表读，这条链路在上一篇讲过。所以 `lsp` 的 60 秒不在工具执行里写死，它来自一个可被配置覆盖的字段。

截断是显式的。`render.ts` 在位置数组后面追加 `… N more locations omitted (limit 100).`，字符截断追加 `… <label> truncated (limit N characters).`。省略说明本身算在预算内，不会出现「说明比内容还长」的边界情况。

两条导航结果带着 `resolvedWorkspaceUri` 一起返回。这个字段由实例填，取自 provider 的 `workspaceUri`，不用宿主自己拼。原因是 URI 语法归文件系统 provider 管，远程工作区下可能是 `ssh://` 或别的 scheme，本地 `file://` 规则套不上。

hover 会被规范化一次再交给工具。server 可以返回 `MarkupContent`、`MarkedString` 或数组，host 统一折成带 kind 的对象。规范化失败按 `LSP_MALFORMED_RESPONSE` 处理，不把原始 JSON 直接塞给模型。

## 5. 诊断为什么不进上下文

这一节回答一个常见期待：LSP 能不能把编译错误直接喂给模型。

这个版本不能，而且是明确不做。`packages/lsp/lsp/README.md:12` 写的是导航能力「deliberately excludes ... diagnostics」。`packages/lsp/lsp/README.zh.md:128` 给了理由：诊断需要独立的新鲜度与累积规则。`packages/lsp/lsp-stdio/src/connection.ts:248` 的注释更直接：server 发来的通知（诊断、日志）被这个 MVP host 忽略。

所以三层都不存在：没有诊断缓存，没有失效规则，没有进入上下文的预算。诊断从来没被收进来，也就没有「什么时候清掉」的问题。

代价是模型看不到类型错误。它要知道代码有没有编译过，只能跑 `tsc --noEmit` 或项目自己的构建命令，再读输出。这条路慢，但结果确定：退出码和 stderr 是同一份证据。

如果要做，至少三件事：按文件加版本来缓存诊断，`didChange` 之后标记为过期，进上下文前做条数与字符预算。缺任何一条，上下文里都会留下已经修好的错误，模型会照着旧错误改代码。

模型自己跑构建时，看到的证据形态不一样：退出码、被截断的 stderr、以及行号。这三样都要模型自己解析，好处是不会过期，坏处是要多花一轮。两种做法各有代价，这一版选了后者。

自己搭 Agent 时，push 型通知（诊断、lint、watch 事件）进上下文之前，先把失效规则写出来。没有失效规则的推送，等于给模型一份会过期的待办清单。

## 6. SSH 四件套：换的是 seam

{{< fig "diagrams/03-ssh-topology.svg" "图注：本地三个消费者通过一条 ssh 连接落到远端 helper。管理 RPC 走 exec 流，数据流走转发的 Unix socket 并加了 TLS-PSK；文件、进程、沙箱判定都在远端。" >}}

四个包各换一个位置：

| 包 | 替换的 seam | 消费者看到什么 |
| --- | --- | --- |
| `ssh` | `ctx.ssh` | 连接、RPC、转发流 |
| `fs-ssh` | `ctx.fs` | 同一套文件 API，路径是远端路径 |
| `subprocess-ssh` | `ctx.subprocess` | 同一套进程 API，输出有上限 |
| `sandbox-ssh` | `ctx.sandbox` | `confine` 返回远端给出的 argv 与执行事实 |

第五个包 `ssh-helper-runtime` 不是 seam，是可执行分发：远端没有 Node 时，用打包好的可执行文件跑 helper。

模型侧一个工具都没多。`read` 还是 `read`，`bash` 还是 `bash`，只是路径与进程落在远端。这带来一个容易忽略的要求：所有消费者都要遵守 provider 的路径语义。`processPath` 返回的是远端路径，把它拼进本地 `path.join` 就会得到无意义的字符串。

配置在加载期就校验完：`host` 必须是已存在的 OpenSSH 别名，`helper` 与 `workspace` 必须是绝对路径，`helperHash` 是 64 位十六进制，`launch` 二选一。校验失败直接拒绝加载，不会出现「连上了但 helper 起不来」的半可用状态。

一份最小配置大概是这样：

```yaml
ssh:
  host: build-box
  launch:
    kind: node-script
    node: /usr/local/bin/node
  helper: /opt/dsh/ssh-helper.mjs
  helperHash: 9c1f...c0
  workspace: /srv/repo
  requestTimeoutMs: 30000
  leaseMs: 30000
```

几个字段值得说明。`host` 只写别名，不写 `user@host:port`，那些属于 ssh_config。`workspace` 是远端的绝对路径，所有相对路径都相对它解析。`leaseMs` 同时决定心跳间隔与租约长度，配得太短会在慢网络上误判死亡，配得太长会拖慢断线发现。

包 README 里写着「模型体验：无」。这一行是设计决定，不是遗漏。host 别名、认证方式、流 token 都是部署细节，进了提示词只会给模型增加无法验证的信息。

`ssh-helper-runtime` 值得单独说一句。它的作用是在目标机没有 Node 时仍能跑 helper，做法是把 Node 与 helper 打成一个可执行文件。这让部署少一个前置条件，代价是制品变大，升级时要连运行时一起换。

## 7. 连接：一个 master、一条 RPC、多条流

`start()` 起一条本地 ssh 进程，参数逐条都有用：

```
ssh -T -M -S <tmp>/master \
  -o ControlPersist=no \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o ForwardAgent=no \
  -o ClearAllForwardings=yes \
  -o ServerAliveInterval=10 \
  -o ServerAliveCountMax=3 \
  <host> <command>
```

`-T` 关掉伪终端分配。这条连接只跑命令与转发，不需要 tty；拿到 tty 反而会让远端程序以为自己在交互环境里，输出格式跟着变。

`-M -S` 开连接复用，control socket 放在私有临时目录。`ControlPersist=no` 让 master 随进程退出而消失，不留后台连接。`BatchMode=yes` 禁止一切交互式提问，密码和口令短语的提示会直接失败，而不是挂在那里等输入。`StrictHostKeyChecking=yes` 不自动接受新主机键。`ForwardAgent=no` 和 `ClearAllForwardings=yes` 关掉代理转发与配置里可能存在的端口转发。最后两行是 keepalive。

这里没有 `IdentityFile`。用哪把私钥由用户的 `~/.ssh/config` 决定，DSH 不复制、不解析、不落盘任何密钥。这条选择让凭据管理回到 OpenSSH 手里，代价是排障时要去看 ssh_config 而不是 DSH 配置。

远端启动方式有两种。`node-script` 走 `<node> --disable-sigusr1 <helper>`，`executable` 走 `NODE_OPTIONS='--disable-sigusr1' <helper>`。`--disable-sigusr1` 是给 Node 运行时的开关，避免 SIGUSR1 被解释成「打开调试器」，把一个信号变成端口监听。

helper 起来后先握手。hello 里带 helper 摘要、启动方式、bootstrap 摘要，客户端逐项比对：摘要不符报 `SSH helper digest differs from the configured artifact`，启动方式不符报 `SSH helper runtime differs from the configured launch kind`。校验通过才开始收 RPC。

活性靠心跳。间隔 `floor(leaseMs / 3)`，单次超时 `leaseMs / 2`，同一时刻只有一个心跳在飞。心跳失败直接 `fail()`，把整条连接标记为不可用。半开的 TCP 不会报错，心跳把它变成一次可观测的超时。

管理 RPC 走 SSH 的 exec 流，请求有 `maxFrameBytes`（64 MiB）与 `maxPending`（128）两道闸。数据流不走 RPC：`connectStream` 用 `ssh -O forward -L` 在本地开一个 Unix socket，转发到远端的同名 socket，再套一层 TLS-PSK。

`maxPending` 是挂起请求的上限，不是并发上限。超了直接拒绝新请求，不排队。理由是这些请求大多来自进程 stdio，排在一个无界队列里只会把内存吃满，早拒绝反而能让调用方尽快看到错误。

方法按域分组，名字直接对应消费者：`fs.*` 是文件操作，`process.prepare`、`process.start`、`process.done` 是进程三段，`sandbox` 做执行判定，`close` 让 helper 收尾。每个方法带自己的 zod schema，请求结构与响应结构都在客户端校验一遍，避免远端版本不一致时拿到半截对象。

分层的原因是可观测性与隔离。RPC 帧要可解析、可限长；进程的 stdin/stdout 是原始字节流，混进 RPC 会互相拖累。每条转发流有自己的 SSH channel window，但带宽仍然是共享的，一条大输出会挤慢同连接上的其他流。

TLS-PSK 的密钥通过管理 RPC 下发，不作为流的前缀发送。同机其他用户即使连上那个 socket，没有密钥也解不开。远端路径还要过两道检查：必须在 hello 返回的 root 之下，且不能含 `:`、回车、换行、NUL。

## 8. 断线：未知是一种结果

{{< fig "diagrams/04-ssh-failure.svg" "图注：SSH 断线时正在执行的远端进程。客户端把挂起操作全部失败，模型收到一条错误结果；远端是否跑完、清理是否发生，客户端只能报未知。" >}}

ssh 子进程退出时，`fail()` 用一句固定文案结束所有等待者：

```
SSH helper disconnected; remote outcomes and cleanup are unknown
```

这句话是整篇最该记住的一句。它没有说「失败」，说的是「不知道」。远端进程可能已经跑完、可能写了一半、可能还在跑，客户端都没有证据。

于是有三条行为：

1. 不重连。重连之后远端状态无法对齐，重放一次 `prepare` 可能起第二个进程。
2. 不重放。已经发出的请求一律以失败结束，不会自动再发一遍。
3. 清理尽力而为。`dispose()` 的顺序是固定的：

   1. 发 RPC `close`，让 helper 拆掉受管进程。
   2. 发 SIGTERM 给本地 ssh。
   3. 等 `requestTimeoutMs`，还没退就 SIGKILL。
   4. 等子进程关闭，删临时目录。

   连接已经断了的时候，第一步做不了，只能做到本地进程收尾。

远端一侧有自己的兜底：helper 收到 EOF、信号，或租约到期，会开始清理受管进程。但这条路径客户端看不见，只能相信它会发生。

租约与心跳是两套机制。心跳证明「现在通」，租约证明「多久没消息就当我死了」。只有心跳，网络半开时会一直等；只有租约，恢复通信后旧租约已经过期，helper 可能已经清理过一轮。两个都要。

断线发生在命令执行中间时，模型看到的是两段东西：已经流回来的 stdout 片段，和一条 `isError` 的结果。片段可能是半行，也可能是半条 JSON。工具层不做拼接，也不猜完整性，原样交给模型。模型要不要重跑，由它自己判断。

自己搭 Agent 时，远程执行的结果别用布尔。至少三态：成功、失败、未知。未知要原样传给模型，让它决定是重跑还是先检查现场。把它折叠成失败，模型会以为命令没执行，于是再跑一遍。

## 9. 密钥与凭据：交给 ssh_config

四个问题分开答。

用哪把钥匙：由 `~/.ssh/config` 的 `Host` 块决定，包括 `IdentityFile` 与 `IdentityAgent`。DSH 只传别名，不传密钥路径。

密码与口令短语：`BatchMode=yes` 让一切交互式认证失败。DSH 不内置密码输入，也不缓存口令短语。要用密码登录，得先配好 `ssh-agent` 或 `ControlMaster` 复用。

agent 转发：显式关闭。远端拿不到本地 agent，也就拿不到你所有已加载的密钥。需要转发时在 ssh_config 里自己开，DSH 不会替你打开。

known_hosts：`StrictHostKeyChecking=yes`，首次连接不会自动接受指纹。第一次用某台主机之前，先在终端里 ssh 一次把指纹存下来。

流密钥是另一层。TLS-PSK 的 256 位密钥经管理 RPC 下发，只存在内存里；每条流转发前先完成握手，握手失败就关 socket。

边界也要说清。TLS-PSK 防的是同机进程冒充与路径替换，防不住能读你进程内存的 root 或同用户进程。helper 摘要钉的是制品内容，不认证主机身份；主机是不是可信，仍然由 known_hosts 回答。

四个问题可以压成一张表：

| 问题 | 答案归谁 | DSH 做什么 |
| --- | --- | --- |
| 用哪把钥匙 | ssh_config 的 Host 块 | 只传别名 |
| 密码与口令 | ssh-agent 或连接复用 | BatchMode 直接失败 |
| 是否转发 agent | ssh_config | 显式关闭 |
| 主机是否可信 | known_hosts | 严格校验，不自动接受 |

## 10. 何时远程，代价是什么

适合远程的三种场景：

- 大仓库。本地磁盘放不下，或者构建要跑几十分钟，远端机器更强。
- 受限网络。目标环境只能从跳板机到达。
- 隔离执行。不信任的代码不该在开发机上跑。

代价有四条，都不小。

延迟。每条文件操作是一次 RPC，目录遍历、批量读、编辑器式的多次 stat 都会放大往返次数。`fs-ssh` 的接口按单次操作设计，没有批量预取。

延迟还会叠加。一次补丁应用可能包含读、改、写三步，每步一次 RPC，再加一次沙箱判定，四五个往返才完成一次工具调用。本地接法里这是一次系统调用加两次内存拷贝。

路径映射。远端路径在本地没有意义，工具输出里的绝对路径不能直接喂回本地工具。工作区设在远端仓库根，是唯一省心的用法。

平台与部署。两端都要 Linux 或 macOS，没有 Windows 端点；helper 不会自动安装，`workspace` 与 `helper` 都得事先放好。

视图假设。Web 端的工作区视图仍假设文件在宿主上，远程工作区下这些视图会失效。这条写在包 README 的已知限制里。

还有两种情况不适合远程。需要图形界面的工具（浏览器接管、截图对比）依赖本机显示与浏览器配置；高频小文件操作（补丁应用、逐行编辑）在 RPC 往返下会明显变慢。

混合也要谨慎。同一个会话里一边读本地文件一边跑远端命令，模型很容易把两边的路径混用。要么全本地，要么全远程，中间态需要额外的路径映射层，这一版没有。

动手前可以按四个问题判断：

1. 代码与构建产物在哪台机器上？答案在远端，就该远程。
2. 本地能不能访问目标网络？不能，且没有别的代理，才考虑 SSH。
3. 执行的东西可信吗？不可信，远程隔离才有意义。
4. 操作频率高吗？高到每条命令几十次小文件读写，远程会拖慢整体节奏。

## 11. 两条接法的共同约束

把 LSP 与 SSH 放在一起看，能抽出三条对任何跨进程接法都成立的约束。

第一条，目标身份要可寻址。LSP 用 workspace targetKey，SSH 用 host 别名。两者都不是会话 id，因为跨进程资源比会话活得久。用会话 id 做键，第二个会话会重新起一份，第一个会话关闭又会拆掉共用的那份。

第二条，失败要变成模型可见的结果。LSP 抛 `LspError`，工具层折成 `isError` 结果；SSH 断线把挂起请求全部失败。两条路径都不让异常穿透到宿主循环。宿主循环被异常打断，模型看到的是崩溃，没有可修正的信息。

第三条，预算要在接缝处写死。LSP 有文档大小、位置数、字符数、超时四处；SSH 有帧大小、挂起数、请求超时、租约四处。没有预算的跨进程接口，延迟和内存都由对端决定。

再加一条只对远程成立的：路径必须由 provider 给，不能自己拼。本地接法里这条错误不明显，远程接法里它会把每个工具都变成 ENOENT。

## 12. 设计取舍

1. LSP 选了封闭操作集合，放弃可扩展性。四种操作覆盖「跳转与悬停」，符号搜索、调用层级、重命名都没有。加操作要改四处，编译器会强制对齐。
2. LSP 选了临时打开文档，放弃增量同步。每次查询送全文，省掉 `didChange` 的版本管理，代价是 4 MB 上限与大文件直接报错。
3. LSP 选了进程常驻，放弃按查询起停。省掉每次几百毫秒的握手，代价是空闲进程占内存，且实例死亡要重建。
4. 诊断选了不做，而不是做一半。没有缓存、没有失效规则、没有预算，就不承诺能力；模型改用构建命令拿证据。
5. SSH 选了 OpenSSH 做传输，放弃自研协议。复用多路复用、转发、known_hosts、ssh_config，代价是依赖本机 ssh 客户端与它的配置。
6. SSH 选了不重连，放弃可用性。断线后所有挂起操作失败，结果标记未知，宁可让模型重试，也不冒重放副作用的风险。
7. SSH 选了零模型工具，放弃可发现性。模型不需要学新工具，代价是 host、认证、路径这些信息只能靠系统提示或文档提供。

## 13. 踩坑点

1. 现象：`lsp` 查询返回空数组。原因：光标位置落不在符号上，或者文件类型没有 provider。对策：先确认扩展名在 provider 的映射里，再把位置挪到标识符上。
2. 现象：同一工作区两个会话，第二个会话查询很慢。原因：两个会话共用一台 server，查询按工作区串行。对策：这是设计行为，把重查询拆开或换工作区副本。
3. 现象：`LSP_UNSUPPORTED_OPERATION`。原因：server 不支持临时 `didOpen`，或没声明对应能力。对策：换 server，或在 `initializationOptions` 里打开能力。
4. 现象：大文件查询直接报错。原因：超过 4 MB 的文档上限。对策：没有配置项放宽，先确认是不是读错了文件。
5. 现象：结果被截断但没有提示。原因：提示被下游再次截断，或渲染层不是 `render.ts`。对策：检查 `maxLocations` 与 `maxResultChars` 的生效位置。
6. 现象：SSH 加载时报 helper 摘要不符。原因：远端 helper 与配置里的 `helperHash` 不是同一份制品。对策：重新分发 helper，更新摘要，别手动改摘要绕过。
7. 现象：首次连接卡住或直接失败。原因：`BatchMode=yes` 下无法回答密码与主机指纹提问。对策：先在终端 ssh 一次，或用 ssh-agent。
8. 现象：远端命令跑完但客户端报未知。原因：连接在结果返回前断开。对策：按未知处理，检查远端现场，不要假设没执行。
9. 现象：本地路径传给远端工具后报 ENOENT。原因：`processPath` 是远端路径，拼了本地前缀。对策：只用 provider 返回的路径，不自己拼。
10. 现象：目录遍历特别慢。原因：每条 `stat` 一次 RPC。对策：减少遍历深度，或用一条远端命令拿列表。
11. 现象：`LSP_CONFLICT`，插件起不来。原因：两个 provider 声明了同一个扩展名或 id。对策：删掉重复声明，注册是原子的，冲突时一个都不会生效。
12. 现象：远程工具报 `FS_ABORTED` 但用户没取消。原因：连接已断，信号被统一映射成取消。对策：查连接日志与心跳，不要只重试工具。

## 14. 源码索引

| 位置 | 证明什么 |
| --- | --- |
| `packages/lsp/lsp/src/types.ts` | `LspOperation` 封闭联合、零基 UTF-16 坐标、查询与结果类型 |
| `packages/lsp/lsp/src/index.ts` | `LspService.registerProvider` 原子注册、按扩展名选 provider |
| `packages/lsp/lsp-stdio/src/index.ts` | `servers` 配置、加载期全量解析可执行文件、`LocalLspProvider` 的实例池与队列 |
| `packages/lsp/lsp-stdio/src/instance.ts` | 握手参数（`processId: null`、utf-16）、临时 `didOpen`、`$/cancelRequest` 与拆实例 |
| `packages/lsp/lsp-stdio/src/host.ts` | 工作区规范化、包含性检查、4 MB 文档上限 |
| `packages/lsp/lsp-stdio/src/connection.ts:248` | server 通知（含诊断）被忽略 |
| `packages/lsp/lsp-stdio/src/abort.ts` | 取消与 `killGraceMs` 的等待语义 |
| `packages/lsp/tool-lsp/src/index.ts` | 一基坐标换算、会话工作区要求、`timeoutMs` 注册 |
| `packages/lsp/tool-lsp/src/render.ts` | 位置数与字符数预算、显式省略文案 |
| `packages/lsp/lsp/README.md:12` | 诊断被明确排除 |
| `packages/lsp/lsp/README.zh.md:128` | 诊断需要独立的新鲜度与累积规则 |
| `packages/ssh/ssh/src/index.ts:300-302` | ssh 启动参数：多路复用、BatchMode、StrictHostKeyChecking、ForwardAgent=no |
| `packages/ssh/ssh/src/index.ts` | hello 校验、心跳、`fail()` 文案、转发流与 TLS-PSK |
| `packages/ssh/fs-ssh/src/index.ts` | `SshFileSystem` 逐方法 RPC、路径与策略透传 |
| `packages/ssh/subprocess-ssh/src/index.ts` | `RemoteProcess` 的 prepare→streams→start、输出上限与 `wait` |
| `packages/ssh/sandbox-ssh/src/index.ts` | `confine` 由远端返回 argv 与执行事实 |
| `docs/subsystems/lsp.zh.md` | 只读查询的边界与不接管编辑、诊断 |
| `docs/subsystems/ssh.zh.md` | 执行坐标、管理 RPC、断线语义 |
| `docs/tool-catalog.zh.md`（lsp 小节） | 模型侧参数与四个操作的说明 |

两处未核实：仓库 README 说 SSH 不支持 Windows 端点，代码里没有平台判断，实际限制可能来自 helper 的构建产物；Web 工作区视图在远程工作区下的表现没有实测，只能确认 README 把它列为已知限制。

## 最后

上一篇 [护栏：超时、重复提醒与钩子](/posts/dsh-guards-and-hooks.html) 讲的是调用前后的约束，这一篇讲调用落到哪里。下一篇 [全局：一次调用的成本与一次越界的代价](/posts/dsh-cost-and-safety.html) 把两季的机制收束成成本与安全两条线。

延伸阅读：

- [执行环境：工作区、沙箱与进程](/posts/dsh-execution-environment.html)：`ctx.fs`、`ctx.subprocess`、`ctx.sandbox` 三个 seam 的原始语义。
- [子 agent 生命周期](/posts/dsh-subagent-lifecycle.html)：实例与作用域的关系，和 LSP 按 workspace 缓存是同一类问题。
- [嵌入协议](/posts/dsh-embedding-protocols.html)：跨进程边界上如何定义请求与结果。
- [工具流水线](/posts/dsh-tool-pipeline.html)：`tools/pre-execute` 与 `tools/post-execute` 的位置。
