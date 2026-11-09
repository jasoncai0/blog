---
title: "OpenCode 机制深潜 IV：事件目录与投影"
date: "2026-11-09T13:56:00+08:00"
summary: "48 个 durable 事件的全目录、投影矩阵、publish 的完整路径、订阅者拓扑，以及一个反直觉的事实：事件行默认不落库，只有 workerd 打开 persist。"
description: "基于 opencode 2.2.5 源码的第四层解剖：48 个 durable 事件的全目录、投影矩阵、publish 的完整路径、订阅者拓扑、事件持久化的开关，以及这套账本的代价。"
tags: ["opencode", "event-sourcing"]
slug: "opencode-deep-dive-4"
---

> 源码版本：`opencode 2.2.5`，commit `a687c047`（2026-11-08）。代码引用以工作区 `.opencode-src/` 为基准。
> 系列第七篇。前六篇分别在讲架构、上下文、打断、模块、状态机、装配；这一篇讲它们共同依赖的那本账。

## 0. 为什么单独讲事件

前面每一卷都会碰到同一句话：「先发事件，再投影」。但事件到底是什么、有多少种、各自落到哪张表、谁在听——一直没系统列过。这一卷补上，做成一份可以当索引用的东西。

三个问题会在这里得到答案：

- 我看到的这条 UI 状态，对应哪一条 durable 事件？（§2 的矩阵）
- 为什么有些增量刷新一下页面就没了？（§1 durable 与 live 的界线）
- 事件表会不会无限增长、读模型会不会落后？（§6、§7）

顺带修正前几卷一个不精确的说法：之前写"所有消息以追加事件写入 SQLite"，更准确的是"所有状态变化都会**投影**进 SQLite；事件本身是否留档，取决于 `persist` 开关"。

## 1. 两类事件：账本与广播

### 1.1 构造时就分了类

事件不是运行时才决定要不要存——**定义时就分好了**。`Event.durable({type, aggregate, version, schema})` 与 `Event.ephemeral({type, schema})` 是两个不同的构造函数：前者必须声明聚合键怎么从数据里算出来，后者不需要。

按 schema 包里的定义数：**48 个 durable、27 个 live**（`packages/schema/src/*event*.ts`）。durable 里 47 个定义在 `session-event.ts`，第 48 个是 `worktree.resolved`（聚合键是 projectID）；live 散在 session / mcp / lsp / location / server / tui / vcs / workspace / worktree / installation 十几个文件里。

### 1.2 一个容易搞反的点

「durable 才广播」是错的。实际是：**所有事件都进 live 通道，只有 durable 的才可能落库**。

而"可能"这个词比想象中更重：Bus 有一个 `persist` 开关，**默认是关的**（`options?.persist ?? false`），服务端只在 workerd 运行时显式打开（`events: { persist: true }`），其余环境——包括你本机跑的 `opencode serve`——都不写 `event` 行。

关掉时发生什么？durable 事件仍然会按聚合分配 `seq`、仍然会跑 projector、仍然会广播；唯独不写 `event` 表那一行。所以默认部署里：

- 读模型（消息、分片、收件箱）是完整的；
- 事件日志是空的；
- `session.log` 这类回放接口拿不到东西。

这不是 bug，是权衡：事件行只服务"重放与审计"，而这两件事在本机单进程场景里价值有限，却要为每个事件多一次插入和一份存储。**投影才是常态，日志是可选**。

```text
publish(event)
  ├─ notify：listeners → live PubSub → 按 type 的订阅者   （所有事件都走）
  └─ 若 durable 且 persist：事务内跑 projector + 写 event 行
```

{{< fig "diagrams/01-publish-paths.svg" "一次 publish 分出的两条路" >}}

顺序上有个细节：`notify` 里先给 live 订阅者、再给按 type 的订阅者。注释解释了原因——按 type 的订阅者会就地 publish 后续事件，先发 live 能让外部观察者看到"因"在"果"之前。

### 1.3 live 里都是什么

session 下正好六条：`text.delta`、`reasoning.delta`、`tool.input.delta`、`tool.progress`、`compaction.delta`、`usage.updated`。每条都紧贴一个 durable 的 `*.ended` 边界——schema 里的注释写得直白：「流片段是 live-only，`Text.Ended` 才是可重放的完整值边界」。

另外还有两条不在这个文件里但同属易失的：`session.status` 与 `session.idle`。也就是说"这个会话现在在不在跑"本身就是易失状态，重连后必须另找接口确认。

六条里有两条名不副实，值得单独记：

- **`tool.input.delta` 没有发布点。** schema 与客户端 fold 分支都在，但发布端构造 fragments 时只传了 `ended`、没传 delta 回调。结果是工具参数**不流式**，`Tool.Input.Ended` 一次带全文。这是"schema 先行、发布端后补"的残留。
- **`tool.progress` 没有节流。** 工具每调一次 `progress` 就发一条，与文本增量的 100ms 批量不同。

代价也直接：断线期间丢失、刷新后要重新拉。客户端不靠增量重建状态，而是靠消息分页接口重新拿投影后的消息。这解释了为什么"刷新一下，正在输出的那段就变成了完整的一段"——增量没了，但终态在库里。

还有一个容易归错账的细节：服务端那条 4096 帧的队列**不是 durable 专用队列**，它是每个 SSE 连接一条，live 帧与 durable 帧混装。溢出时整条连接被断开（`SubscriberOverflowError`），而不是只丢某一类。


## 2. durable 事件目录

全仓运行时认识的 durable 类型共 **48 个**：47 个挂在 session 聚合上，1 个是 `worktree.resolved`（聚合键是 projectID）。`session-event.ts` 里定义了 51 条，其中 45 条 durable + 6 条 ephemeral，另有 2 条 durable 不进公开清单，合计 47。

| 域 | 事件 | 数量 |
| --- | --- | --- |
| 会话生命周期 | `created` `moved` `renamed` `metadata.updated` `permissions` `viewed` `archived` `unarchived` `deleted` `forked` `agent.selected` `model.selected` | 12 |
| 用量与内容 | `usage.recorded` `message.content.updated` | 2 |
| 外围输入 | `synthetic` `skill.activated` `shell.started` `shell.ended` | 4 |
| 收件箱 | `inbox.enqueued` `inbox.delivered` `inbox.cancelled` `inbox.delivery.changed` | 4 |
| 执行 | `execution.started` `execution.succeeded` `execution.failed` `execution.interrupted` | 4 |
| 指令 | `instructions.updated` | 1 |
| Step | `step.started` `step.streamed` `step.ended` `step.failed` | 4 |
| 文本 / 推理 | `text.started` `text.ended` / `reasoning.started` `reasoning.ended` | 4 |
| 工具 | `tool.input.started` `tool.input.ended` `tool.called` `tool.success` `tool.failed` | 5 |
| 重试 | `retry.scheduled` | 1 |
| 压缩 | `compaction.started` `compaction.ended` `compaction.failed` | 3 |
| 回滚 | `revert.staged` `revert.cleared` `revert.committed` | 3 |

session 下另有 6 条 ephemeral：`usage.updated`、`text.delta`、`reasoning.delta`、`tool.input.delta`、`tool.progress`、`compaction.delta`。流式增量刻意不落库，只留 started / ended 两个边界——所以"一段文本"在库里是一条 ended，在界面上是一串 delta。

其余模块（location / mcp / lsp / installation / vcs / server / tui / workspace）在 2.2.5 里**全是 ephemeral**：它们描述连接状态与缓存失效，不需要回放。

### 2.1 三个不显眼但重要的约定

1. **版本不在内存里的 type 上**。内存与客户端看到的是 `session.deleted`，落库时拼成 `session.deleted.2`。当前是 v2 的只有五个：`deleted`、`forked`、`instructions.updated`、`tool.success`、`tool.failed`。
2. **`aggregate` 是 payload 里的键名**，不是类型。`aggregate: "sessionID"` 表示取 `data.sessionID`；取不到非字符串就 die。它决定 seq 分配、锁粒度、以及删除聚合时的删除单位。
3. **`session.moved` 会路由两份**：旧 location 与新 location 各投一次，因为订阅方按 location 过滤。这是唯一在路由上特殊处理的事件。

两个容易被误用的细节：`usage.recorded` 是内部事件，**永不进公开流**（所以 `compaction.ended` 才要重复带一份 cost/tokens 给客户端展示）；`message.content.updated` 标了 replay-only，当前代码不再产生它，只在折叠旧历史时出现。

## 3. 一次 publish 的路径

durable 事件的 publish 全部在 `Effect.uninterruptible` 里，外面套一层按聚合键的 `KeyedMutex`，里面是一个 `begin immediate` 事务。按代码顺序：

1. **算聚合键**。从 `definition.durable.aggregate` 拿一个函数，用事件数据算出 `aggregateID`。算不出来或与传入值不符，直接 die。
2. **读当前序号**。`event_sequence` 表里该聚合的 `seq`，没有就是 -1。
3. **处理重放输入**。如果调用方显式给了 `seq`（重放场景）且 `seq <= latest`：内容完全一致就幂等返回，不一致就 die——这就是"同一个事件重放两次不会写两遍，但写岔了会立刻炸"。
4. **分配新序号**。`seq = latest + 1`；显式给了不连续的序号也 die。
5. **查重**。持久化模式下先查 event 表里这个 event id 是否已存在，存在就 die。
6. **跑投影**。按 `versionedType(type, version)` 找到注册的 projector 列表，依次执行。投影写在同一个事务里。
7. **跑调用方的 commit 钩子**。这是给"投影之外还要顺手写点别的"用的（比如 claim 的写入）。
8. **推进序号表**。`insert ... onConflictDoUpdate set seq = max(seq, ?)`。
9. **写事件行**。`type` 存的是 `versionedType(type, version)`，不是裸 type。
10. **提交后广播**（`notify`）：listeners → live PubSub → 按 type 的订阅者。

注意 6 与 9 的顺序：**投影先跑，事件行最后写**。但因为在同一事务里，外部看到的仍然是"事件与读模型一起出现"。真正决定顺序的是事务边界，不是语句顺序。

`publishAll` 是批量版本：多个事件先各自算好聚合键与 payload，再在一个事务里批量插入（`bus.ts:631`），避免逐条提交的开销。


## 4. 投影矩阵

投影注册在 `session/projector.ts` 的 `layer` 里，用 `bus.project(定义, 函数)` 逐个注册。看这五十来行注册，能分出三种形态。

### 4.1 三种投影形态

| 形态 | 事件 | 写什么 |
| --- | --- | --- |
| 直接写表 | `created` `moved` `renamed` `metadata.updated` `permissions` `viewed` `archived` `unarchived` `usage.recorded` `revert.staged` `revert.cleared` | `session_v2` 的对应列 |
| 走 message-updater 折叠 | 其余绝大部分：step / text / reasoning / tool / synthetic / shell / skill / retry / compaction / revert.committed | `session_message` + `session_part` |
| 走状态机 | `instructions.updated`、`compaction.ended` | `instruction_state` / `instruction_entry` / `instruction_blob` |

`run(db, event)` 是那个统一入口：把事件交给 `SessionMessageUpdater.update`，由它按事件类型更新或插入对应的 message/part 行。

### 4.2 几个特殊投影

- **`created`** 用 `insert ... onConflictDoNothing ... returning`，如果没插进去（说明已经存在），直接 die `SessionAlreadyProjected`。
- **`compaction.ended`** 在 `run` 之后额外调 `InstructionState.advanceEpoch`，把基线重置到这条事件的 seq——这就是"压缩后指令基线换代"的落点。
- **`revert.committed`** 是唯一做删除的投影：按边界消息的 seq 删掉其后的消息、收件箱项，再 `rewind` 指令状态、`releaseFrom` 释放 blob 引用。它在一个投影里干了四件事。
- **`execution.*` 三个终态** 都走同一个 `projectIdle`——成功、失败、被打断在会话行上留下的 `time_idle` 与 `idle_outcome` 是同一套字段。

### 4.3 幂等：大部分靠 publish，小部分靠写法

显式判"已存在"的只有两个：`created` 与 `forked`——都用 `insert ... onConflictDoNothing ... returning`，返回空就 die `SessionAlreadyProjected`。捕获它的只有 `Session.create`（降级为"返回已存在会话"）与 `Session.import`（转 `ImportConflictError`）；**`Session.fork` 不捕获**，重复 fork 直接硬失败。

其余投影的幂等来自两层：

- **publish 层**：`(aggregate_id, seq)` 唯一 + 重放时的等值判断（§3 第 3 步），保证同一条事件不会被投影两次；
- **写法本身**：UPDATE 覆盖、`max()` 单调水位、`onConflict`、按 `ordinal` 覆盖写 part，都是天然可重复的。

唯一的例外是**成本与 token 的 `+=`**（`usage.recorded`、`step.ended/failed`）——这类累加不可重复执行。它能成立，完全依赖"链路上没有第二个入口"。

### 4.4 消息的 seq 直接取事件 seq

`run()` 折叠消息时，消息的 `seq` 不是另算的，直接用 `event.durable.seq`。所以"事件顺序"与"消息顺序"天然一致，不需要额外的排序字段或时间戳兜底。

但两者不能互相替代：消息 seq 是**投影产物**，会因为有的事件不产生消息而出现空洞，fork 复制父消息后还会预留水位。所以它能当排序键，不能当重放游标——重放游标必须用事件 seq。

这也是为什么投影能安心用 `Match.discriminatorsExhaustive` 逐个事件类型分支：顺序由 seq 保证，它只需要关心"这条事件该改哪个字段"。

### 4.5 批量：两个"500"不是一回事

- `ForkBatchSize = 500` 是 fork 复制时的**读页大小**，不是插入批量；
- 真正的插入批量由 `DatabaseBatch.insertBatches` 按"单语句 ≤ 500 个绑定参数"切分，于是 `session_part` 每语句 12 行、`session_message` 14 行、`blob_ref` 24 行、`blob` 14 行（列数越多，每批越少）。

写多行的投影有四个：`forked`、`revert.committed`、`message.content.updated`、`deleted`。

### 4.6 事务与失败

投影跑在 publish 的 `begin immediate` 事务里，与序号推进、事件行写入同生共死。所以投影里 die（比如 `created` 的重复、`revert` 找不到边界消息）会让整个 publish 回滚——事件没写进去，读模型也没变。

这是"要么都成要么都不成"的实现方式：不是靠补偿，是靠同一个事务。


## 5. 订阅者拓扑

全仓有 **23 处**订阅、**48 处**投影注册。按"收到事件之后做什么"可以分成四类。

| 类别 | 代表 | 特点 |
| --- | --- | --- |
| 投影 | `session/projector.ts` 里的 48 个 `project` | 只在 durable 事件上、事务内、提交前 |
| 派生 | Step/Usage 三个事件 → 重新汇总 → 发 `usage.updated`；Provider/Credential → 重算 → 发 `provider.updated` / `model.updated` | 收到事件后**再发事件** |
| 转发 | EventFeed（SSE）、event-logger（5 种 `*.updated` 打日志）、plugin host（插件订阅） | 不改状态 |
| 副作用 | location-activity 打卡、location-lifecycle 懒激活、config 重载、plugin supervisor 重新激活插件、MCP 工具表 reconcile、PTY/搜索清理 | 去重载或清理某个注册表 |

三条通道的分发规则决定了顺序：

```text
project（事务内，提交前，按注册序）
  → listen（提交后，同步，按注册序）
    → live 通配（提交后）
      → typed 订阅（提交后，各自 fiber，顺序不确定）
```

`listen` 的 durable 回调被 `observe` 隔离，抛错只记日志；`subscribe` 的 typed 组之间是并发恢复的，**不能假设谁先处理完**。

### 5.1 一个事件最多被几个人处理

`session.deleted` 有 6 个消费者：投影删行、websearch 清映射、PTY 结束会话、location-activity 打卡、location-lifecycle 激活 Project、EventFeed 转发。

`credential.switched` 更多：config、policy、provider、provider/refresh、google-vertex 各一处，而 config/provider 这些是 `makeLocationNode`，**每个 Location 各有一份订阅**——同一条事件会被 N 个 Location 的 N 份回调各处理一次。

这解释了为什么"改个凭据感觉全系统都动了一下"：它确实动了，而且每个 Location 动一次。

### 5.2 三个不订阅的东西

排查时容易找错方向：

- **统计**（`session/stats.ts`）不订阅事件，它直接 SQL 查 event 表与消息行；
- **标题生成**只发事件（`usage.recorded`、`renamed`），不消费；
- **后台任务与子会话**走 `Job.Service` 的 `wait/background`，不是事件驱动；**调度器**是定时器直接 `wake`，也不是。

所以"事件驱动"只覆盖状态与注册表的同步，长任务的编排走的是另一套（job + 定时器）。

还有一条更强的结论：**没有任何订阅者会调 `execution.wake` 或 `sessions.resume`**。在 2.2.5 里不存在"某条事件触发一次执行"的路径；子代理完成也不是靠监听事件，而是 `SubagentCompletion.deliver` 直接发一条 synthetic 消息。事件总线是**状态的传播通道**，不是任务调度器。

## 6. 版本、重放与兼容

### 6.1 存储带版本，线缆不带

写进 event 表的是 `versionedType(type, version)` = `` `${type}.${version}` ``——库里的 type 是 `session.deleted.2`。而发出去的 payload 里，`type` 仍是 `session.deleted`，版本藏在信封 `durable: {aggregateID, seq, version}` 里。

所以老客户端按 `type` 分派不会被打断；代价是线缆上**没有"我可能看不懂"的显式标记**。

版本号写在定义里：session 事件默认 v1，五个是 v2——`deleted`、`forked`、`instructions.updated`、`tool.success`、`tool.failed`。信封里的 `version` 是 `Schema.Literal(v)`，与 payload 硬绑定，用 v2 定义去解 v1 数据必定失败。

### 6.2 升版本的代价：旧行变孤儿

这里有个容易误判的地方。直觉是"升版本后，旧事件交给旧版本的 projector 处理"；实际是——**仓库不留旧版本定义**。

- `latest()` 只取每个 type 的最高版本，manifest 里查不到 `session.tool.success.1`；
- 旧版本行仍在 event 表里，但对新进程"不存在"：读日志时**静默跳过**，游标继续前进；
- 只有显式 `replay` 撞上未知类型才会 die `Unknown durable event type`。

换句话说，升版本不是"兼容两种形状"，而是"让旧行变成不可解码的历史"。真正兜住字段变化的是另一层：`SessionLegacy` 在唯一解码入口把旧字段升级成当前形状（`state` → `native` 这类），所以"改名"不需要升版本，只有"改类型"才需要。

### 6.3 重放是幂等的，但不是宽容的

重放走同一个 `commitDurableEvent`，撞上第 3 步的判断：

- 同一 id、type、created、升级后 data 深等 → 幂等返回，不重复投影；
- 任何一项不同 → die `Replay diverged at aggregate ... sequence ...`。

有意的：重放要么证明历史一致，要么立刻失败。宁可炸，也不要悄悄产生两条不同的历史。

### 6.4 客户端怎么重放

SSE 只保证"在线时看到增量"。需要严格回放时走 `session.log?after=<seq>&follow=true`：

- `after` 是**开区间**（只取 `seq > after`），读到当前水位是**闭区间**（`seq <= through`）；
- 按 seq 升序，默认每页 512 条；
- `follow` 时先订阅再回放，并在水位处发一个同步标记；
- 解码不了的未知类型被跳过，但游标仍按原始 seq 前进，不会卡死。

前提仍然是 §1.2 那个开关：**默认 `persist=false` 时日志是空的**，`follow` 只能等到"从此刻起"的新事件。

现实中的采用也很有限：全仓只有 TUI 的 devtools 在用这条接口。正常客户端断线后走的是"重连 → `session.connected` → 拉一次消息分页做全量对账"，而不是按 seq 续读。所以它更像一个诊断工具，而不是主流的同步路径。

### 6.5 不认识的事件怎么处理

四种场合四种态度：

| 场合 | 行为 |
| --- | --- |
| 服务端读日志 | `Durable.get(type)` 为空就跳过，游标用原始 seq 继续 |
| 服务端显式 `replay` | 直接 die |
| 服务端出站 SSE | 过 `isOpenCodeEvent`，不满足就丢弃（内部事件如 `usage.recorded` 因此永不外发） |
| 插件订阅 | `logWarning` 后忽略 |
| 客户端消费 | 事件是闭合 union，没有"跳过未知"的分支；真收到不认识的类型，会走连接层的错误路径（重连）而不是静默忽略 |

"读日志跳过、重放报错"的差别是有道理的：读日志是日常路径，不能因为一条老数据卡住整个回放；重放是显式断言"历史可重现"，解不了就该失败。


顺带一个反例：`event-logger` 用的是**裸 type**（`agent.updated` 这类，没有版本后缀），所以它不受版本机制影响——也正因如此，它只能看到那五种 inventory 事件。
### 6.6 v1 迁移是搬投影，不是搬事件

v1 的数据迁移不读事件：它先把 event 表按 1000 行一批**全删掉**，再用旧 `session` / `message` / `part` 三张表重建成 `session_message` 行，并把 `event_sequence.seq` 预置成"迁移出的消息数 − 1"作为水位。旧历史只以投影行的形式存在，事件日志从水位之后重新开始。

解不出的行不阻塞迁移：按 `invalid-message` / `invalid-part` / `orphan-part` 记 warning 后丢弃。幂等靠 `kv` 里的迁移游标，重跑不会重复导入。

## 7. 这套账本的代价

**写放大**。一条工具成功事件 = 1 次事件插入 + 至少 1 次 part 更新 + 可能的 session 行更新。会话越长、工具越多，写入次数越接近"事件数 × 常数"。

**事件表只增**（在打开 `persist` 的环境里）。聚合被删时（删会话）会连带删掉它的全部事件与序号行，但**活着的会话的事件永远不回收**。一个长跑会话的事件表体积与"它做过的所有事"成正比，没有压缩、没有归档。默认配置下这张表干脆是空的——省了空间，也就没有了回放。

**订阅没有背压**。live 与 typed 两套都是 `PubSub.unbounded`，publish 永不因慢订阅者阻塞，代价是内存无界；真正限制住增长的只有"订阅者自己读得快"。服务端的 4096 帧队列是另一层，只保护 SSE 连接。

**投影是同步的**。它在 publish 的事务里跑，意味着写事件这条路的延迟包含投影成本。好处是一致性免费拿到；坏处是投影里任何慢查询都会拖慢发布——虽然目前投影都是主键/索引命中。

**重放不是免费的**。`session.log` 从头读一个大会话，等于把历史重新解一遍；它适合"补一小段"，不适合"每次启动全量重建"。

**读模型不会落后**。这一点反而是省下来的：因为同事务，不存在"事件写了、投影没跟上"的窗口，也就不需要后台补偿任务。代价是整个系统的写入路径被绑在 SQLite 的单写者上。

一句话：这套设计的核心交易是——**用写入时的同步开销，换读取时的一致性保证**。


## 8. 证据索引

| 主题 | 主要文件 |
| --- | --- |
| 事件定义 | `packages/schema/src/session-event.ts`、`event.ts` |
| live 定义 | `location-event.ts`、`mcp-event.ts`、`lsp-event.ts`、`tui-event.ts` 等 |
| publish / seq / 重放 | `packages/core/src/bus.ts` |
| 投影注册 | `packages/core/src/session/projector.ts` |
| 消息折叠 | `packages/core/src/session/message-updater.ts` |
| 指令状态 | `packages/core/src/session/instruction-state.ts` |
| 事件表结构 | `packages/core/src/event/sql.ts` |
| 用量派生 | `packages/core/src/session/usage.ts` |
| SSE 转发 | `packages/server/src/event-feed.ts` |
| 事件日志 | `packages/core/src/event-logger.ts` |

---

*这一卷的结论来自按模块分工的调研与主代理回源核对；标注"未确认"的地方是静态阅读无法断定的。*
