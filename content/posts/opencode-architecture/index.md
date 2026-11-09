---
title: "OpenCode 架构总览"
date: "2026-11-09T03:31:00+08:00"
summary: "一个本地服务 + 多个瘦客户端的整体架构：五层依赖、事件溯源与投影、按会话串行的执行模型、工具与权限、扩展机制，以及这套设计换来了什么、付出了什么。"
description: "基于 opencode 2.2.5 源码：一个本地服务 + 多客户端的整体架构，事件溯源与投影、会话执行模型、工具与权限、扩展机制与工程化。"
tags: ["opencode", "architecture"]
slug: "opencode-architecture"
---

> 源码版本：`opencode 2.2.5`，commit `a687c047`（2026-11-08）。
> 代码引用以工作区的 `.opencode-src/` 为基准，正文里省略该前缀；行号会随版本漂移，定位时以符号名为准。

## 0. 先给结论

OpenCode 不是一个"会调用工具的 CLI"，而是**一个本地服务 + 多个瘦客户端**。理解这一点，后面所有设计都顺了：

- **会话、工具、权限、文件操作全部在服务端一侧**。终端 TUI、浏览器、桌面端、编辑器都只做两件事：发 HTTP 请求、收 SSE 事件。客户端代码不被允许 import `core` 或 `server`。
- **依赖是单向的**：`schema`（领域模型与事件）→ `core`（运行时）/ `protocol`（HTTP 契约）→ `server`（组装与传输）→ `client / tui / app / desktop`。`protocol` 不依赖任何实现，所以客户端可以脱离服务端独立生成。
- **状态是事件溯源出来的**：任何状态变化先成为一条 durable event，再由 projector 在同一事务里折叠成 `session_message` / `session_part` 读模型。"事件已发布"等价于"视图已更新"，两者不会漂移。
- **输入与执行解耦**：`prompt` 先把输入落成 `session_inbox` 一行并立即返回，再由按会话串行的执行 fiber 拉起。同一会话串行、不同会话并行，打断只影响本地拥有执行权的那条 fiber。
- **每个 Step 都是一次独立的 `llm.stream`**，上下文从投影后的历史重新装载。代码里**没有**内存中的工具循环——这是它与多数 agent 框架最不一样的地方。
- **可恢复性靠写前声明**：一次执行开始前写入 claim，正常结束才释放；进程崩溃后 claim 还在，重启时据此恢复会话，并把遗留的 `running` 工具调用判失败。
- **扩展面有五种粒度**：plugin hook 改行为、MCP 加工具、skill 加知识、command 加入口、agent 加人格。

一句话概括：**会话是事件流，执行是进程内的 fiber，多个客户端共享同一个本地事实来源。**

## 1. 它是什么：形态与边界

OpenCode 对外有五种入口，背后是同一套 HTTP API：

| 形态 | 位置 | 说明 |
| --- | --- | --- |
| 终端 TUI | `packages/tui` | 默认入口，SolidJS + OpenTUI 渲染在终端里 |
| 浏览器 | `packages/app` | SolidJS SPA；构建产物被内嵌进 CLI，由 server 直接托管 |
| 桌面端 | `packages/desktop` | Electron 外壳，renderer 直接复用 `@opencode/app` |
| ACP | `packages/cli/src/acp` | 用 stdio 的 NDJSON 讲 Agent Client Protocol，供编辑器接入 |
| 无头 / SDK | `opencode run`、`@opencode/sdk` | 一次性执行；或把 server 跑在进程内，不开端口 |

默认命令 `opencode` 做的事情很容易误解。它不是"起一个前台服务再画界面"，而是：

1. `ServerConnection.resolve()` 找一个可用的后台 daemon（`packages/cli/src/services/server-connection.ts:18`）；
2. 找不到就 `spawn opencode serve --service`，等它把 URL、端口、密码写进注册文件；
3. 然后 attach 上去，拉起 TUI（`packages/cli/src/commands/handlers/default.ts:35`）。

所以同一台机器上通常只有一个服务进程在跑，多个终端窗口共享它；`--standalone` 才会为当前命令起一个私有 server，`--server <url>` 则直接连远端。这也解释了为什么你在 A 窗口新建的会话，B 窗口刷新就能看到——它们读的是同一个 SQLite。

## 2. 技术底座

| 技术 | 用在哪 | 换来的能力 |
| --- | --- | --- |
| Bun | 运行时、打包、测试、单文件编译 | 启动快；`Bun.build` 直接产出各平台可执行文件 |
| TypeScript + Effect | 全仓 | 结构化并发、可中断的 fiber、显式依赖注入（Layer / Context）、流式处理 |
| SQLite + Drizzle | 持久化 | 单文件、WAL、事务内投影；迁移可回滚 |
| SolidJS | TUI 与 Web | 两端共享响应式心智，细粒度更新适合流式 token |
| models.dev | 模型目录 | 能力、价格、上下文窗口等元数据的外部来源 |

Effect 是这里最"重"的选择，也是最值得先理解的。仓库里几乎所有服务都是 `Context.Service` + `Layer`，副作用被包在 `Effect` 里；并发用 fiber，取消用中断，超时和重试是一等公民。好处是"打断一次会话"不需要自己维护状态机——`Fiber.interrupt` 会把清理逻辑按 Scope 逐层执行完。代价是读代码前得先接受这套心智模型。

## 3. 仓库地图与依赖方向

35 个 workspace 包，按职责可以压成五层：

{{< fig "diagrams/01-layers.svg" "依赖方向：领域模型 → 运行时 → 服务 → 多端" >}}

| 层 | 包 | 职责 |
| --- | --- | --- |
| 表现层 | `tui`、`app`、`desktop`、`web` | 渲染与交互；只通过 client 访问服务端 |
| 接入层 | `protocol`、`client`、`sdk` | HTTP 契约、生成客户端、进程内组合 |
| 服务层 | `server` | 组装 HttpApi、鉴权、Location 解析、SSE、PTY socket |
| 运行时 | `core`、`ai` | 会话执行、工具、权限、Provider、MCP、插件、存储 |
| 基础层 | `schema`、`util` | 领域模型与 durable 事件、Effect 工具集 |

依赖规则写在仓库根 `AGENTS.md` 里，而且是被 CI 强制执行的口径：

- 运行期依赖只能 **Schema → Core / Protocol → Server**；
- `client` 的运行时代码可以依赖 `schema` 与 `protocol`，**永远不能**依赖 `core` 或 `server`；
- `sdk` 是唯一允许同时组合 client、core、server 的包。

这条规则的实际收益是：`protocol` 里只有 endpoint 声明、错误 schema 和中间件身份，没有一行实现，所以 `packages/client/script/generate.ts` 能直接把它编译成三套客户端（Promise、Effect、Solid 连接层），而 `server` 只要实现同一个 `HttpApi` 就自动获得 OpenAPI 与类型校验。

顺带一提，`packages/core/src` 下有近 500 个源文件，`server` 只有 60 个左右——**重活在 core，server 很薄**。这是有意的：server 只负责"把 HTTP 翻译成对 core 服务的调用"，业务规则不下沉到传输层。

## 4. 进程与连接：一个守护进程，多个工作区

{{< fig "diagrams/02-topology.svg" "运行时拓扑：一个本机守护进程，多个客户端，多个工作区" >}}

服务端是单进程的，但它同时服务多个项目目录。这里的关键抽象叫 **Location**：一次请求携带一个 `directory`（外加可选的 `workspaceID`），server 用它解析出 Project，再为这个目录构建一整套 Location 作用域的服务——配置、Agent、工具、Provider、文件系统视图都是"按目录"的。

解析顺序很短（`packages/server/src/location.ts:71`）：query 的 `location[directory]` → 请求头 `x-opencode-directory` → 进程 cwd。目录不存在会得到 404，而不是静默回落到别的项目。

会话还有一层：`SessionLocationMiddleware` 用路由里的 `sessionID` 读出会话所属的 placement，再用 `Instance.provide(session)` 选中该会话对应的那组服务（`packages/server/src/middleware/session-location.ts:16`）。**执行是进程级的、按 Session ID 索引；但执行时用到的工具、模型、文件系统是 Location 级的。** 这句话是理解"会话移动"（move）功能的前提：换目录时只换 Location，会话历史不搬。

认证都围绕同一个服务端密码展开，凭据有三种形态，服务于"本地优先、但允许远程"：

- **密码**：`opencode serve` 生成一个密码，客户端用 HTTP Basic（用户名固定 `opencode`）；
- **会话 token**：`<过期时间>.<HMAC>`，密钥由密码派生，所以改密码等于吊销所有已发 token，有效期 30 天；
- **配对码**：`POST /api/pair` 生成 16 字节随机码，5 分钟内单次有效；浏览器访问 `/auth/connect/:code` 会换到 httpOnly Cookie 并 302 回首页。

WebSocket 升级（PTY、MCP client）不能带自定义头，所以走 `?ticket=` 一次性票据，由 handler 自己消费校验。

## 5. 概念模型

| 概念 | 是什么 | 代码位置 |
| --- | --- | --- |
| Project | 一个被解析出来的项目（含 git 信息、canonical 路径） | `packages/core/src/project` |
| Location | `directory + workspaceID` 的请求作用域 | `packages/core/src/location.ts` |
| Instance | 按 Session 选中的一组 Location 服务，可缓存 | `packages/core/src/instance.ts` |
| Session | 持久会话，挂在 Project 上 | `packages/core/src/session/session.ts` |
| Message / Part | 会话里的消息；assistant 正文进一步拆成 part | `packages/schema/src/session-message.ts` |
| Inbox | 尚未投递给模型的输入（用户输入、压缩、移动等控制项） | `packages/core/src/session/inbox.ts` |
| Step | 一次逻辑 LLM 调用（一次 `llm.stream`） | `packages/core/src/session/runner` |
| Execution | 一个会话的一段"忙期"，由一条 fiber 承载 | `packages/core/src/session/run-coordinator.ts` |

有两点容易混淆：

1. **Step 不等于"轮"**。仓库里的 "turn" 被保留给"从用户输入 promote 开始，到会话即将 idle 为止"的一组 Step；一次 Step 只覆盖一次模型可见的往返。
2. **消息不等于请求里的消息**。存储有 11 种消息类型（user、synthetic、system、skill、shell、assistant、compaction、idle、agent-selected、model-selected、location-switched），而进入模型请求时会被"翻译"成不同的角色与顺序——存储结构服务 UI 与投影，请求结构服务模型，两者不必同构。

## 6. 一次 prompt 的完整旅程

{{< fig "diagrams/03-prompt-flow.svg" "一次 prompt 的旅程：先落库，再唤醒，每步重载" >}}

把一次 `POST /api/session/:id/prompt` 拆开看：

1. **准入**。`Session.prompt` 先做幂等对账，再 `SessionPrompt.prepare` 解析附件、skill、插件 hook，然后发布 `session.inbox.enqueued`。这个事件的投影会在同一事务里插入一行 `session_inbox`。
2. **返回**。注意：到这里请求就返回了。它返回的是"已准入"，不是"模型答完了"。
3. **唤醒**。`SessionExecution.wake(sessionID)` 在进程内找到或创建该会话的执行 fiber。忙的话不新起，只按一下门铃（`pendingWake`），让当前执行多排空一轮。
4. **投递**。执行 fiber 在 drain 开始时把 inbox 行投递成一条可见消息（`session.inbox.delivered`），同时把它从 `session_inbox` 删掉——这张表只存"未消费的工作"。
5. **装载**。runner 重新读历史、解析 agent 与模型、准备指令、物化工具快照。再次强调：不是从内存里的 messages 数组继续，而是从投影后的表里重建。
6. **估算与压缩**。在发请求前估算整个请求的 token；超过模型窗口就触发压缩，把旧历史换成"摘要 + 最近上下文"，然后继续同一个逻辑 Step。
7. **请求与流**。`llm.stream` 发出请求；文本、reasoning、tool-call 各自变成事件。
8. **工具**。每个 tool-call 起一个 `forkScoped` fiber 并发执行，先做权限断言，再执行，再把结果落成 durable 事件；所有 fork 在 `uninterruptibleMask` 里等齐，保证不会带着"半截工具"结束 Step。
9. **投影与推送**。事件经 projector 落成消息与分片；server 的 EventFeed 订阅同一条总线，把公共事件编码成 SSE 帧推给所有客户端。
10. **继续或结束**。有工具结果就回到第 5 步开新 Step；否则发终态事件，会话进入 idle。

这条链路上没有"agent 循环"这个对象。循环是**数据驱动**的：库里还有没处理完的工具结果，就再 drain 一次。

## 7. 执行模型：串行、可打断、可恢复

### 7.1 准入与投递

输入进 `session_inbox` 后有两种投递方式：

- `steer`（默认）：在下一个**安全 Step 边界**按入队顺序投递，用户可以边跑边补充；
- `queue`：会话能继续时保持挂起，到 idle 边界才投递一项，投递后 runner 重新判断是否继续。

压缩和移动是**控制项**，它们形成投递边界：控制项之后的 steer 不会跨越它。这条规则避免了"一边压缩一边插话"导致前缀错乱。

### 7.2 每会话一条执行 fiber

`SessionRunCoordinator`（`packages/core/src/session/run-coordinator.ts`）是一个 key 为 Session ID 的协调器：

- 同一会话的新 wake 不新起执行，只按门铃，让当前执行 loop 再排空一次；
- 显式 `resume` 会 join 到正在跑的那次执行，等它的结果；
- 不同会话互不阻塞，真并行；
- `interrupt` 只发中断信号、不等清理完成，清理在 fiber 的 finalizer 里异步收尾。

这就是"打断"能在毫秒级 ack、而数据库里的状态稍后才收敛的原因。打断不会删除待投递的输入，恢复执行时它们还在。

### 7.3 Step 内部

一个 Step 的可观测结构是：

```text
Step.Started
  ├─ 请求装配：system（agent 提示或默认提示）+ instructions 基线 + 工具定义 + 历史投影
  ├─ 估算 token；超预算 → compaction（摘要 / 原生窗口）
  ├─ llm.stream → 文本 / reasoning / tool-call 增量事件
  ├─ tool-call → permission.assert → execute → 结果事件（先落库，再算完成）
  └─ Step.Ended（finish reason、cost、tokens、快照、文件变更）
```

几个细节值得记：

- **最后一步没有工具**。当达到 agent 的步数上限时，runner 会用 `toolChoice: "none"` 再请求一次，并追加一段"请收尾"的提示；这样工具定义那段前缀不变，prompt cache 仍然命中。
- **工具结果先落库**。每个本地调用在产生副作用之前就已经有了一条 durable 的调用记录；这也是崩溃后能把 `running` 判失败、而不是盲目重放的前提。
- **流式输出是增量事件**，但终态只有一条。客户端先收到 `message.part.updated` 之类的增量，再收到 `Step.Ended`，UI 不需要自己推断"什么时候算结束"。

### 7.4 重试与溢出

重试分两层：

- **传输层**（`packages/core/src/model-retry.ts`）：只对可重试的错误退避重试——限流、provider 内部错误、未送达的传输错误、被判定为"流不完整"的输出；鉴权、配额、内容策略、请求非法不重试。退避是指数 + jitter，并尊重响应里的 `Retry-After`（有上限）。
- **会话层**（`packages/core/src/session/runner/retry.ts`）：在传输层之上再叠一层策略，允许插件通过 `session.retry` hook 改延迟或取消，并把"要重试了"作为 `session.retry.scheduled` 事件暴露给 UI。

上下文溢出是另一条路径：如果还没产生任何 durable 输出就命中溢出，runner 会做一次压缩再重建同一个 Step；第二次溢出、或已经产生输出后再溢出，就是终态失败。

### 7.5 崩溃恢复

执行开始时，`Execution.Started` 的投影与 `store.claim` 写 `time_suspended` 在同一个事务里；正常结束、失败、用户打断都会释放 claim，**而进程崩溃不会**。

于是重启时的恢复逻辑很直接：找出仍带 claim 的顶层会话，按 `resume_attempts` 计数恢复（默认上限 10 次，超了就落 Failed），并先把遗留的 `running` 工具调用判失败。它是"至多一次语义的近似"，不保证 provider 请求或工具副作用恰好执行一次——这点代码注释写得很清楚，不装作有分布式事务。

## 8. 事件、投影与推送

{{< fig "diagrams/04-events.svg" "一次状态变更加两条读路径：投影与广播" >}}

一次状态变更走两条路：

**写路径（事实）**：`Bus.publish` 分配聚合内单调序号，把事件写入 `event` 表；同事务里运行 projector，把事件折叠成读模型。projector 是幂等的，重复投影会抛 `SessionAlreadyProjected` 而不是默默写两遍。

**读路径（广播）**：server 的 `EventFeed` 订阅同一条总线，做公共事件过滤 + schema 编码 + JSON 编码，**只做一次**，然后把同一个不可变字符串塞进每个连接自己的 `Queue.dropping`（容量 4096 帧）。慢消费者只会把自己那条连接撑爆并收到 `SubscriberOverflowError`，不会拖住发布者，也不会影响其他连接。

这替代了更常见的两种做法：每连接各自编码（N 倍 CPU），或者共享一个有界 PubSub（慢订阅者拖垮所有人）。取舍写在 `specs/v2/event-stream-architecture.md` 里，是一份少见的、把"为什么不选另一个方案"写清楚的决策记录。

数据库一共 23 张表、59 个迁移。核心几张：

| 表 | 作用 | 关键点 |
| --- | --- | --- |
| `event` / `event_sequence` | durable 事件与聚合序号 | `(aggregate_id, seq)` 单调 |
| `session_v2` | 会话主表 | 含 agent、model、cost、token 累计、`time_suspended`、`resume_attempts` |
| `session_message` | 消息 | `(session_id, seq)` 唯一 |
| `session_part` | assistant 内容分片 | 主键 `(message_id, ordinal)` |
| `session_inbox` | 未消费输入 | 投递即删 |
| `project` / `workspace` / `worktree` | 项目与工作区 | Location 的持久化底座 |

assistant 的正文落在 part 行里，消息行只留一个空 content 的外壳——这是为了让流式增量可以只写 part，而不反复重写整条消息。

SSE 本身没有断点续传：协议明确写着"慢消费者会溢出、断开期间的事件会丢"。需要严格回放时走另一条接口 `session.log?after=<seq>&follow=true`，从 durable 日志按序号续读。这是有意的分工：**实时流追求低延迟，durable 日志追求不丢。**

## 9. 工具系统与权限

工具不是硬编码的函数表，而是"注册进作用域的值"：

- 插件通过 `ctx.tool.transform(editor => editor.add({...}))` 注册，注册时校验名字、把 schema 编译成 `ToolDefinition`；
- 每次请求前生成一个 **snapshot**：按权限规则过滤，把工具分成"原生工具"和"Code Mode 工具"两桶；
- 调用时先跑 `execute.before` hook（可改输入、可拒绝），再按名派发，执行结果经 `execute.after` hook、内容规范化、图片压缩，最后落库。

内置工具大致是这样一组（名称就是模型看到的名称）：

| 工具 | 职责 |
| --- | --- |
| `read` / `write` / `edit` / `patch` | 读文件（带行号）、整文件写、精确替换、补丁式修改 |
| `glob` / `grep` | 路径匹配、内容检索 |
| `shell` | 执行 shell 命令（前后台、超时） |
| `todowrite` | 维护任务清单 |
| `webfetch` / `websearch` | 抓网页、搜索 |
| `subagent` | 起子会话执行子任务 |
| `skill` | 按需加载技能正文 |
| `question` | 向用户提问 |

另有一组挂在 `opencode` 命名空间下的工具（`session_rename`、`worktree_create`、`artifact_*`、`schedule_*`、`list_mcp_resources` 等），以及 MCP 动态注册的 `<server>_<tool>`。

**权限模型**是 `{action, resource, effect}` 三元组，`effect` 只有 `allow | deny | ask`。求值规则是"最后一条匹配的规则胜出"，没有匹配时默认 `ask`。一次工具调用的路径是：

1. 工具自己声明要检查的 action 与 resources；
2. `permission.assert` 先看 deny，再看已保存的 allow，再逐 resource 求值；
3. 任一 resource 是 `ask` 就创建一个挂起的请求，发 `permission.asked` 事件，然后 `Deferred.await`；
4. 客户端弹窗，用户选 `once | always | reject`，回 `POST .../permission/:requestID/reply`；
5. `always` 会写入 saved 规则并唤醒其他可放行的挂起请求；`reject` 连带拒绝同会话所有 pending。

默认策略是"全允许，但读 `.env` 要问"；内置 agent 则反过来，用 deny-all + 逐项 allow。用户配置里的 `permissions` 会追加到每个 agent 的规则末尾，所以天然可以覆盖默认。

还有一个容易被忽略的设计：**Code Mode**。默认情况下大部分工具**不**进 provider 的原生工具表，而是被渲染成一份"工具目录"放进 instructions，模型只看到一个 `execute` 工具，用它写一小段受限的 JS 来组合调用。`read/write/edit/shell` 这些才留在原生表里。这样做的动机是省 token（工具 schema 很贵）和减少模型的工具选择错误，代价是多了一层解释器和权限传播的复杂度。

## 10. 扩展机制：五种不同的粒度

| 机制 | 粒度 | 来源 | 生效方式 |
| --- | --- | --- | --- |
| Plugin | 行为 | 内置、npm 包、本地目录、config 的 `plugins` | 注册 hook 与工具；本地目录可热重载 |
| MCP | 工具 | 本地 stdio 或远端 HTTP 的 MCP server | 工具被发现后动态注册，列表变化时 reload |
| Skill | 知识 | `{skill,skills}` 目录、config 路径或 URL | 由 `skill` 工具按需把正文注入上下文 |
| Command | 入口 | `{command,commands}` 目录、config | 斜杠宏，支持 `$1..$n`、`$ARGUMENTS` 与内联 shell 插值 |
| Agent | 人格 | 内置 + config + `{agent,agents,mode,modes}/**/*.md` | 决定 system prompt、模型、步数上限、权限 |

Plugin 的 hook 一共 19 个，分五个域：`tool`（`execute.before/after`）、`permission`（`evaluate`）、`shell`（`create.before`）、`session`（`prompt`、`context`、`compaction`、`generate`、`title`、`model.request`、`http.request/response`、`retry`、`experimental.*`）、`aisdk`（`sdk`、`language`）。只有 `tool.execute.before` 允许让调用失败，其余 hook 的错误会被吞掉——这是为了让"插件坏了"不至于让整个会话挂掉。

一个值得注意的空白：**LSP 目前只有配置和设置界面，没有运行时实现**。`core` 里有三处 TODO 明确写着"等 LSP runtime 存在后再通知与收集诊断"（`packages/core/src/tool/plugin/edit.ts:258`、`write.ts:38`、`file-mutation.ts:138`）。所以它现在不做 hover、跳转、诊断注入，写文件后也不会主动告诉语言服务器。这不是遗漏，是还没做。

## 11. 模型层

统一抽象是 `@opencode/ai` 里的 `LanguageModel`。上层只关心"给一段消息和一个工具表，流式返回事件"，不关心底下是哪家协议。实现有两条路：

- **原生 provider 包**：每个 provider 自己导出 `configure / model / routes`，直接实现该家的协议（Anthropic Messages、OpenAI Responses/Chat、Gemini、Bedrock Converse 等）；
- **AI SDK 兼容**：包名带 `aisdk:` 前缀，走 `@ai-sdk/*` 的 `LanguageModelV3`，再被包装成同一条路由。

模型的能力、价格、上下文窗口来自 models.dev 的目录，启动时灌入并订阅刷新。持久化只记 provider + model 的**引用**（`ref`），不记解析后的路由对象——这样换 provider 配置不会让旧会话失效。

错误分类集中在 `AIError.isRetryable`：它决定哪些错误值得重试，以及是否允许覆写（响应头 `x-should-retry`）。把这套判断放在 schema 层而不是每个 provider 里，是为了让"重试语义"全仓一致。

## 12. 服务端与协议

`packages/protocol` 声明了 32 个 group、约 170 个 endpoint。请求进入 server 后的顺序是：

```text
CORS / 站点校验（fetch 层）
  → HttpRouter 路由
  → Authorization（Basic / Cookie / token / ticket 豁免）
  → SchemaError（解码失败 400，编码失败 500）
  → Location / SessionLocation 中间件（注入该目录或该会话的服务）
  → handler（薄薄一层，调用 core service）
  → 错误翻译（core 的 TaggedError → protocol 错误 → HTTP 状态码）
```

几个工程上的点：

- **错误是 schema**。每个错误都是带 `httpApiStatus` 注解的 `TaggedError`，endpoint 用 `error: [...]` 声明；客户端 codegen 据此生成错误联合，前端能按类型分支而不是猜字符串。
- **CORS 不在 HttpApi 里**，而在更外层的 fetch 中间件；对 `/api` 还额外拒绝 cross-site 请求。这是防御纵深，避免以后新增 endpoint 忘了加。
- **鉴权中间件不碰业务**：它只判断"这个请求有没有资格进来"，配对、ticket 的豁免在它内部显式列出，方便审计。
- **OpenAPI 是副产品**：`openapiPath: "/openapi.json"` 直接从同一份 `HttpApi` 生成，所以文档和实现不会分叉。

## 13. 客户端

- **TUI**：`@opentui/solid` 渲染，事件经 SSE 进来后先入队、按约 10ms 一批 `batch()` 刷进响应式图，避免每个 token 都触发一次重绘；有 idle watchdog 判断流是不是卡死。
- **Web**：SolidJS + Vite，路由是 `/`、`/settings`、`/server/:serverKey/session/:id`、`/new-session`；支持多 server 切换。它的静态资源在构建期被内嵌进 CLI 二进制（brotli 归档 + 偏移索引），`opencode serve` 直接把它们吐给浏览器——所以 Web UI 不需要单独部署。
- **Desktop**：Electron 主进程自己管理 daemon（拼 `serve --service` 再 `Service.ensure`），密码由主进程持有，代理请求时注入 `Authorization`，renderer 完全拿不到明文。
- **ACP**：把编辑器的 JSON-RPC 请求翻译成 client 调用，再把 server 事件翻译回 ACP 通知，中间有一层 `translate`。
- **SDK**：把 server 的 router 跑在进程内、不开监听端口，供外部程序嵌入。

所有客户端共享同一个 `@opencode/client`，它的三个入口（`promise`、`effect`、`solid`）都是同一份生成代码的不同包装。这也是为什么"在 TUI 里能做的事，在 Web 里也能做"——它们调的是同一批 endpoint。

## 14. 工程化

- **构建**：`packages/cli/script/build.ts` 用 `Bun.build` 的 compile 产出各平台单文件可执行（含 baseline 与 musl，共 12 个 target），内嵌 Web UI、模型快照与 bytecode；`pack.ts` 另外生成 npm 平台包与 `@opencode/cli` 入口。
- **依赖**：bun catalog 统一版本，`bunfig.toml` 设 exact 安装与冷却期；核心依赖有 `patches/` 兜底。
- **质量门禁**：`bun run check` = oxlint + ast-grep 规则 + 类型检查；CI 用官方 `tsc`（`typecheck:tsc`）再跑一遍，两者不一致时以 `tsc` 为准。
- **测试**：`bun test` 在包目录内跑（有 guard 禁止在仓库根跑），Web 有 Playwright e2e，组件有 story 测试。
- **CI**：`check.yml`（类型）、`check-test.yml`（全量测试 + e2e + 隔离用例）、`release.yml`（构建、签名、发布三段，签名阶段才接触密钥）、`deploy*.yml`（sst 部署官网、更新服务、文件服务）、`nightly-beta.yml`（定时 beta）。
- **发布**：npm、单文件归档、ghcr Docker、桌面安装包（dmg/zip/nsis/AppImage/deb/rpm）、Homebrew、AUR、winget。

## 15. 设计取舍

**得到的东西：**

- **单一事实来源**。事件是唯一真相，投影是视图，客户端不需要自己维护"服务端现在是什么状态"。
- **可恢复**。claim 让"进程死了"变成一个可处理的状态，而不是丢会话。
- **多端一致**。同一套 API + 同一套事件，TUI / Web / Desktop 的行为天然对齐。
- **可测试**。协议不依赖实现，可以对着 `HttpApi` 写契约测试；执行是 fiber，打断与超时可以被确定性地测。

**付出的代价：**

- **事件溯源的学习曲线**。新贡献者要先理解"为什么改状态要发事件，而不是直接 UPDATE"；投影写错会导致读模型和事实不一致，而这类 bug 很隐蔽。
- **Effect 的心智负担**。Layer 组合、Scope 生命周期、fiber 取消语义，都要学；写错不会报错，只会在边界情况下泄漏或死锁。
- **单写者**。SQLite 决定了同一时刻只有一个进程能写；多机协作、集群调度目前不在设计里（代码注释也明确说 execution 是 process-local，等 clustering）。
- **没有 LSP**。代码智能目前只靠 grep/glob 与模型自己读文件，和 IDE 的体验有差距。
- **客户端必须能连上服务端**。这是"本地优先"的必然：离线时 TUI 也没法工作。

和同类产品对比，最有意思的差异是**进程模型**：多数 coding agent 是"一个 CLI 进程里包一个 agent loop"，OpenCode 把它拆成"常驻服务 + 哑客户端"。这带来了多端与可恢复，也带来了"必须管理一个 daemon"的运维成本（端口、密码、版本不匹配、残留进程）。它赌的是：agent 会变成一种长期运行的基础设施，而不是一次性的命令。

## 16. 阅读路线

| 你想改什么 | 从哪里开始 |
| --- | --- |
| 加一个工具 | `packages/core/src/tool/plugin/*.ts`，照现有工具的 `make` 写 |
| 改上下文 / 压缩 | `packages/core/src/session/compaction.ts`、`runner/llm.ts` |
| 加一个 HTTP 接口 | `packages/protocol/src/groups/*.ts` → `packages/server/src/handlers/*` → `cd packages/client && bun run generate` |
| 改 TUI 交互 | `packages/tui/src/routes`、`component` |
| 改 Web UI | `packages/app/src/session`、`shell/routes` |
| 写插件 | `packages/plugin/src/effect/plugin.ts` 的 ctx 与 `core/src/plugin/hooks.ts` 的 hook 表 |
| 接一个新模型厂商 | `packages/ai/src/providers/`，或直接用 `aisdk:` 兼容层 |

## 17. 附录：模块速查

| 目录 | 一句话 |
| --- | --- |
| `packages/core/src/session` | 会话、消息、压缩、指令、执行、投影 |
| `packages/core/src/tool` | 工具注册、执行管线、内置工具 |
| `packages/core/src/permission` | 规则求值与 ask 往返 |
| `packages/core/src/mcp` | MCP 客户端与工具发现 |
| `packages/core/src/plugin` | 插件加载、hook、系统提示 |
| `packages/core/src/database` | Drizzle schema、迁移、SQLite 适配 |
| `packages/core/src/location*` | Location 解析与服务映射 |
| `packages/ai/src` | LLM 协议、路由、流式事件、provider 实现 |
| `packages/protocol/src` | HttpApi、错误、中间件身份 |
| `packages/server/src` | 路由组装、鉴权、SSE、PTY |
| `packages/client/src` | 生成的客户端与连接层 |
| `packages/tui/src` | 终端 UI |
| `packages/app/src` | Web UI |
| `packages/desktop/src` | Electron 外壳 |
| `specs/v2/` | 会话、工具、事件流的契约文档，读代码前先读它 |

---

*本文的结构性结论来自源码与 `specs/v2/` 的契约文档；具体数值（170 个 endpoint、23 张表、59 个迁移、4096 帧队列、30 天 token、5 分钟配对码）为 2.2.5 时点的实测，版本升级后可能变化。*
