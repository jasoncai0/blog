---
title: "存储：SQLite、JSON 与迁移的代价"
date: "2026-11-14T18:45:00+08:00"
summary: "同一种会话状态可以有三种落法：JSONL 追加、SQLite 表、内存快照；选型的差别不在速度，而在崩溃点与迁移路径。"
description: "拆 storage 四层与 session-persistence 的实现：写放大、并发写、schema 迁移、损坏恢复与备份。"
tags: ["harness", "architecture", "event-sourcing"]
slug: "dsh-persistence-and-storage"
---

> 依据：源码 commit `fcc0ad46`（版本 `0.2.2-rc.1`），`docs/subsystems/storage.zh.md`（276 行）、`docs/subsystems/persistence.zh.md`（428 行）、`packages/storage/{storage,storage-json,storage-sqlite,storage-domain}/`、`packages/session/session-persistence-jsonl/src/{index.ts,storage.ts,lease.ts,generation.ts,format.ts}`、`packages/session/session-checkpoint-policy/src/index.ts`、`packages/session/session-projection-cache/src/{index.ts,spec.ts}`、`packages/session-query/session-query-sqlite/src/schema.ts`。证据等级：A（逐行读过上述实现，并与子系统文档对照）；推断处单独标注。

凌晨三点，进程被 kill。重启之后，用户看到一个会话停在半句话上，工具调用没有结果，界面上多了一个永远转圈的 spinner。这不是界面 bug，是持久化设计在回答一个问题：崩溃发生的那一刻，磁盘上到底有什么。

DSH 的答案分成几层，每层对这个问题给出不同的截断点。读完这一篇，你应该能判断自己的 Agent 该把哪一段落到哪种介质上，以及换存储时要付出什么。

## 0. 结论

三种落法，三种崩溃粒度：

| 落法 | 一次写入的代价 | 掉电最多丢 | 迁移与恢复 | 用在哪 |
|---|---|---|---|---|
| JSONL 追加日志 | 一批事件一个 zstd 帧，一次 `fsync` | 上次 `fsync` 之后的事件 | generation 只增不改，撕裂尾可截断 | 会话的唯一权威日志 |
| JSON 整文件 | 重写整个 unit 文件，临时文件 + rename + 目录 `fsync` | 未发布的那次写入 | 文件级替换，坏记录可跳过 | 小型 KV，产品默认 |
| JSON 每记录 | 重写一条记录文档 | 该文档的未发布写入 | 记录级 `backup-and-skip` | 投影缓存这类可重建数据 |
| SQLite 行 | 一条语句或一个事务，WAL 提交 | 未提交事务 | `user_version` 不符直接拒绝 | 需要行级查询与批量事务的域 |
| 内存快照 | 不落盘 | 全部 | 无 | 已创建但未实体化的会话 |

四条更具体的结论：

- 会话日志是唯一权威格式，而且只有一种第一方实现。仓库曾经交付过 SQLite 版会话存储，后来删掉了，旧库当前 build 打不开。
- 写放大最小的路径是追加日志：一条事件只写自己那几个字节，永不重写历史；最贵的是 JSON `single` 布局，每次写都重写整个 unit 文件。
- 跨进程靠一把不到 150 行的文件锁，锁没有过期时间，靠内核在进程死亡时释放。撕裂尾部由下一个拿到写权的进程修。
- 所有版本不匹配都往「拒绝」走，唯一的例外是可重建的派生索引，它直接 DROP 重建。

## 1. 同一种状态，三种落法

一个会话在 DSH 里会经历四个位置：内存里的 prepared Session、磁盘上的 JSONL 日志、派生出来的投影缓存、查询用的 FTS 索引。后两个都可丢弃，前两个才是事实。

产品默认把根目录放在 harness home 下（`packages/bundle/base/cordis.patch.yml:138-141`）：

```text
$DSH_HOME/
  sessions/
    --Users-me-project--/            # 规范化 cwd 的可读项目目录
      01J...abc/                     # 会话自有目录
        session.v4.jsonl.zstd        # 当前权威日志
  storages/
    session_projcache/
      01J...abc.json                 # 投影缓存，可重建
```

布局规则来自 `packages/session/session-format/src/filename.ts`：v0 叫 `session.jsonl`，之后按代际叫 `session.vN.jsonl`；启用压缩时后缀再加 `.zstd`。项目目录由规范化 cwd 生成，没有 cwd 时落到 `_no-cwd/`；会话 id 经过单射转义，保证不能穿越目录，也不会两个 id 撞到同一路径。

{{< fig "diagrams/01-three-media.svg" "三种介质与各自的崩溃截断点。追加日志只丢最后一个未同步的帧，整文件替换丢一次未发布的写入，内存快照在崩溃时整段消失。右侧标出每种介质的恢复动作：截尾、回滚内存、从未存在。" >}}

SQLite 在这个默认组合里根本不出现。它注册在 `ctx.storage` 下，供 domain KV 按需选择（`packages/storage/storage-sqlite/src/index.ts`），与会话日志无关。会话日志的 abstract Service 仍然允许仓库外实现别的 provider，但仓库自己只维护 JSONL 一条持久化路径。

三种介质的差别，落到运维上是一张表：

| 介质 | 写入单位 | 掉电丢什么 | 恢复动作 |
|---|---|---|---|
| JSONL | 一批事件一个 Zstandard 帧 | 上次 fsync 之后的字节 | 截断撕裂尾，重写能解码的记录 |
| JSON 整文件 | 一个 unit 的完整文件 | 未发布的临时文件 | 丢弃临时文件，读旧文件 |
| SQLite | 一行或一个事务 | 未提交的事务 | 回滚日志或 WAL 重放 |
| 内存快照 | 无 | 整段状态 | 从未存在 |

表里的 SQLite 指 domain 存储，会话日志没有 SQLite 后端。选型的实际问题是：这份数据丢了以后，谁能重建。

## 2. ctx.storage 的四层

`storage` 这一组有四个包，职责不能混：

| 包 | 提供的服务 | 负责 | 不负责 |
|---|---|---|---|
| `dsh-storage` | `ctx.storage` | backend 名表、挂载、解析 `storageBackendServiceKey` | 任何磁盘 IO |
| `dsh-storage-json` | backend `json` | `single` 与 `per-record` 两种布局的读写 | 域语义、版本兼容策略 |
| `dsh-storage-sqlite` | backend `sqlite` | 一个数据库、每行一文档 | 域语义、迁移 |
| `dsh-storage-domain` | `ctx.storageDomain` | 类型化域、写入链、版本与坏记录策略 | 物理布局 |

后端契约很短（`packages/storage/storage/src/backend.ts`）：

```ts
interface StorageBackend {
  readonly kv?: KvFacet
  close(): Promise<void>
}

interface KvFacet {
  open(descriptor: KvUnitDescriptor): Promise<KvUnit>
}
```

`KvUnit` 有 `loadAll`、`putRecord`、`deleteRecord`、`deleteRecords`、`setGlobal`、`close`。这里没有事务对象，也没有游标。契约把几件事写死了：整文档写和单行写是原子的；`deleteRecords` 是一批一次，通常整批生效或整批不生效；逐记录批量写到中途失败时，前面几条可能已经落盘，后端不承诺回滚；一旦无法确定批次状态，unit 进入 `unknown-state`，此后所有操作拒绝，直到关闭重开。

domain 层才是使用方。一个 `DomainSpec` 声明名字、版本、`compatibleVersions`、`invalidRecords` 策略、布局、表与 global 槽。它的写入顺序固定：

1. 入调用方的写链排队，同一个域同一时刻只有一次写。
2. 先落介质，等后端确认。
3. 成功后更新内存快照。
4. 最后发 `domain/changed`。

这个顺序解释了为什么读是同步的：内存快照在事件发出之前已经更新；也解释了为什么写失败不会污染内存。`update` 在写链里做 read-modify-write，`deleteRecords` 一批只占一个槽位，调用方拿到的记录是存储里的对象，不做防御性拷贝。

{{< fig "diagrams/02-storage-layers.svg" "四层的依赖方向。上层只认服务名与域描述符，物理格式在后端内部；domain 是后端契约的唯一 Consumer，业务代码不直接打开 unit。" >}}

打开一个域时，任何一步失败都会让整次调用失败，而不是降级：域已打开、backend 不存在、backend 不支持 kv facet、版本不匹配、介质损坏、坏记录无法按策略处理。产品组合把 backend 固定成 `json`（`cordis.patch.yml:187-190`），`root` 指向 `dshHomePath('storages')`。想换 SQLite 只改这两行，域代码不动，这就是分层的收益。

后端契约的成员如下（`packages/storage/storage/src/backend.ts`，节选）：

```ts
interface KvUnit {
  loadAll(): Promise<{ tables: Record<string, Record<string, unknown>>; global: unknown }>
  putRecord(table: string, key: string, value: unknown): Promise<void>
  deleteRecord(table: string, key: string): Promise<void>
  deleteRecords(table: string, keys: readonly string[]): Promise<void>
  backupRecord?(table: string, key: string): Promise<string>
  setGlobal(value: unknown): Promise<void>
  close(): Promise<void>
}
```

`loadAll` 把整个 unit 读进内存，之后领域层的读都走内存。四个写原语和可选的 `backupRecord` 不改内存，只把变更写到介质；成功返回后，领域层才更新自己的快照。`close` 释放 unit 名，后端据此允许重新打开。契约里没有「批量写」以外的顺序保证：同一 unit 的两个写并发时，介质上的先后由调用方决定，后端不做串行化。

## 3. 会话日志：一次 link 发布，逐批 fsync

会话是延迟实体化的。`create(header)` 返回一个写句柄，磁盘上什么都没有。第一次 `append` 才发布文件，发布序列在 POSIX 上长这样（`packages/session/session-persistence-jsonl/src/index.ts:1264-1339`）：

```text
mkdir(root), fsync(parent)                     # 逐级创建并同步父目录
mkdir(projectDir), fsync(root)
mkdir(sessionDir), fsync(projectDir)
writeSyncedTempFile(sessionDir/.<uuid>.tmp)    # wx, 0600, write, fsync
link(tmp, session.v4.jsonl.zstd)               # EEXIST 即拒绝，绝不覆盖
fsync(sessionDir)
rm(tmp)                                        # 尽力而为
```

为什么用 `link` 不用 `rename`：`rename` 会安静地覆盖已存在的目标。同一个会话如果被两个进程同时实体化，`link` 的 `EEXIST` 会让第二个写者失败，而 `rename` 会让两份日志互相盖掉。代码注释把这条理由写在 `materializePosix` 里，`generation.ts:817-845` 的 `publishCurrentExclusive` 复用同一个不覆盖协议。

发布完成后，每个批次走 `appendLines`（`index.ts:1409-1451`）：

```text
open(path, 'a')
before = handle.stat().size
writeFile(frame)        # 整批一个 zstd 帧，带 checksum
handle.sync()
close()
```

写或同步失败时，先关闭，再 `truncate(before)` 加 `fsync`，把文件回滚到调用前长度，然后把错误抛给调用方。已经提交的事件永不重写。

压缩帧的细节决定恢复能力。编码器打开 `ZSTD_c_checksumFlag: 1`（`generation.ts:54`），每帧自带校验和。扫描时 `scanZstdFrames` 找到最后一个不完整帧的起点 `tornStart`，`decompressZstdPrefix` 可以把这一帧里已经完整的 JSONL 记录解出来。写句柄在第一次新批次之前做三件事：截掉撕裂字节、把恢复出的完整记录重新持久写一遍、再追加新批次。因此一次掉电不会让「已经写进去但只写了一半」的记录永久消失，只会让最后那条没写完的记录消失。

`flush()` 在没有任何事件时也会发布一个只有 header 的帧。这让「创建了会话但还没有消息」这种情况有确定语义：不 flush，崩溃后它从未存在；flush 过，它存在且为空。

发布与追加是两条路径，代码里的顺序值得逐行看。

实体化（`session-persistence-jsonl/src/index.ts:1264-1339`，POSIX 分支）：

```ts
const tmp = await this.writeSyncedTempFile(finalPath, content)  // wx 0600 + sync
try {
  await link(tmp, finalPath)            // EEXIST 即失败，不覆盖
} finally {
  if (!linked) await rm(tmp, { force: true })
}
await this.syncDirPosix(dir)            // 目录项落盘
await rm(tmp, { force: true })          // 尽力清理，失败不拒绝发布
```

追加（`index.ts:1409-1451`）：

```ts
const handle = await open(path, 'a')
const { size: before } = await handle.stat()
try {
  await handle.writeFile(content)        // 整批一个 zstd 帧
  await handle.sync()
} catch (error) {
  await closeAppendHandle()
  await this.rollbackAppend(path, before)  // truncate(before) + sync
  throw error
}
```

回滚本身（`index.ts:1453-1462`）：

```ts
private async rollbackAppend(path: string, size: number): Promise<void> {
  const handle = await open(path, 'r+')
  try {
    await handle.truncate(size)
    await handle.sync()
  } finally {
    await handle.close()
  }
}
```

用 `link` 而不是 `rename` 是刻意的：`rename` 在目标已存在时会静默替换，`link` 返回 `EEXIST`。会话日志是证据，覆盖一次比失败一次更糟。代价是发布路径多一次目录 `fsync`，以及一个必须清理的临时文件。

追加失败时先回滚再抛错。回滚把文件截回本批之前的长度并 `fsync`，所以调用方看到的失败与盘上的状态一致：这一批要么完整存在，要么完全不存在。

## 4. 写放大：一条事件写几次盘

先给结论：正常路径下，一批事件一次 `fsync`，一个会话一生只做一次实体化发布。表里是具体次数（POSIX，压缩开启）：

| 操作 | 系统调用 | 掉电丢什么 |
|---|---|---|
| 实体化发布 | 3 次目录 `fsync` + 临时文件 `write`/`fsync` + `link` + 目录 `fsync` | `link` 之前崩溃：会话不存在；`link` 之后目录未同步：极端情况下目录项丢失 |
| 追加一批事件 | `open` + `stat` + `write` + `fsync` + `close` | 上次 `fsync` 之后的批次；若帧写了一半，撕裂尾被截断 |
| 显式 `flush()` | 空批次时发布 header 帧；有事件时等同追加 | 同追加 |
| 投影缓存写一次 | 单文档重写（per-record）：临时文件 + `fsync` + `rename` + 目录 `fsync` | 本次未发布的缓存更新；缓存可重建 |
| SQLite 一次单行写 | 一条 upsert 语句，WAL 提交 | 未提交事务 |
| SQLite 一次批量删 | `BEGIN IMMEDIATE` + N 条 delete + `COMMIT` | 整个事务 |

关键在于「一条事件」从来不单独落盘。写句柄把所有待写事件放进一个有界窗口（`storage.ts:36`）：

```ts
const LIVE_WRITE_BATCH_MAX_DELAY_MS = 200
```

第一个待写事件开启一个固定 200ms 的定时器，后续事件加入但不重置截止时间。到期后整批 `splice` 出来，走 `persistContiguous`（`storage.ts:318-346`）：只读句柄拒绝、确认租约、校验连续性、先截撕裂尾、重写恢复记录、`persistBatch`、推进 cursor。结果是高频流式事件不会每个都触发一次 `fsync`，最多每 200ms 一次。

批处理只解释延迟，不解释正确性。正确性来自检查点策略（`packages/session/session-checkpoint-policy/src/index.ts:36-82`）：

- `llm/stream` 在适配器派发请求之前 `flush`。
- `tools/execute` 只在顶层调用（`parent === undefined`）时、执行工具体之前 `flush`；abort 发生在派发前，则返回 `Error: tool call aborted before dispatch`。
- `agent/pre-step` 每一步 `flush`。

于是有两条顺序保证：模型看见的请求一定已经落盘；有副作用的工具执行前，触发它的消息一定已经落盘。掉电最多丢掉「最后一次检查点之后发生的事」，而最后一次检查点之后模型还没看到任何东西。

{{< fig "diagrams/03-event-durability.svg" "从事件产生到模型看见之间的落盘屏障。事件先进 200ms 窗口，窗口到期成一个 zstd 帧并 fsync；模型请求与工具执行各自是一道强制 flush 屏障。任何屏障之后的崩溃都不会让模型看过的东西消失。" >}}

投影缓存是另一条写路径，方向相反：它先取检查点快照，再 flush 会话，最后 `put`（`session-projection-cache/src/index.ts:249-273`）。顺序不能反，否则缓存会领先于日志，日志回滚后缓存留下一个不存在的状态。强制写点是创建、`turn/end` 与 disposal，其余按 `writeEveryEvents: 200`、`writeIntervalMs: 5000` 节流。

自己搭 Agent 时，先量清楚你要的是哪种写放大。会话日志这种只追加结构，写放大接近 1，代价是读取要扫；JSON `single` 结构读得快，代价是每次写都重写整个文件，一个 2MB 的 unit 每秒写十次就是 20MB/s 的写放大。很多项目在会话量上来之后才换存储，换的其实是这一项。

SQLite 的写放大介于两者之间：只有被改的行页进入 WAL，提交时按 `synchronous` 设置同步。代码只设置了 `journal_mode` 与 `foreign_keys`（`storage-sqlite/src/schema.ts:80-88`），没有改 `synchronous`，也没有设 `busy_timeout`。前者意味着提交默认走完整同步，后者意味着并发写冲突会直接报错而不是排队。

一次事件从产生到落盘，会经过这些 fsync：

| 时点 | 触发者 | 写什么 | fsync 次数 |
|---|---|---|---|
| 创建会话 | `create` | header 与首帧 | 1（文件）+ 1（目录） |
| 事件入队 | `session/event` | 无 | 0 |
| 窗口到期 | 写句柄 | 一整批事件 | 1 |
| 模型请求前 | checkpoint policy | 排空待写批次 | 1（若已有待写） |
| 工具执行前 | checkpoint policy | 同上 | 1（若已有待写） |
| close | teardown | 排空 | 1（若已有待写） |

把一条事件单独拎出来，路径是这样：

1. 事件产生，`session/event` 同步通知到后端，事件被 `structuredClone` 后放进待写队列。
2. 队列空时启动一个 200ms 定时器；队列非空时只追加，不重置定时器。
3. 定时器到期，`drainBuffered` 取走整批，`persistContiguous` 校验连续性与租约。
4. 一批事件序列化成一个 Zstandard 帧，追加到文件尾，`fsync` 一次。
5. 写成功，推进 cursor 与 materialized 标记；写失败，整批拼回队首并暂停自动路径。
6. 模型请求或工具执行前的 flush 会跳过等待，立即做第 3 到第 5 步。

高频流式事件因此被摊平：一个 200ms 窗口里来 50 条事件，盘上多一个帧、一次 `fsync`。

## 5. 并发：进程内排队，跨进程租约

DSH 的并发控制分三层，每层解决的问题不同：

| 层 | 机制 | 防的是 |
|---|---|---|
| domain | 每域一条写链 | 同进程内两个写交错，内存与介质顺序不一致 |
| 会话句柄 | 进程内写入 claim + 跨进程文件租约 | 两个进程同时追加同一份日志 |
| 存储 unit | 不排队，顺序归调用方 | 不防；后端明确把顺序责任交出去 |

跨进程锁在 `packages/session/session-persistence-jsonl/src/lease.ts`（135 行）。POSIX 用 `tryLockExclusive(fd)`，底层是原生模块暴露的非阻塞 `flock(2)`；Windows 没有等价的文件锁，改用由路径派生的命名内核信号量。争用统一映射成 `SessionAlreadyOwnedError`。锁文件叫 `session.lock`，放在会话目录里。

两个设计细节值得抄：

- 锁没有过期时间。过期租约会在持锁进程仍然活着但卡住时被另一个进程抢走，两边同时追加，日志直接撕裂。宁可让后来者报错，也不赌持锁者死了。
- 释放只 `close`，不删锁文件。删掉会让下一个创建者拿到新 inode，而旧持有者可能还锁着旧 inode，两个「锁」同时有效。POSIX 上锁的是 inode，所以代码在拿到锁之后还要 `stat` 一次，确认路径仍指向刚锁住的 inode，不是就重开重试，最多 3 次。

读者从不碰锁。`open(id, 'read')` 不加锁，只返回验证过的连续前缀，所以一个只读的旁观进程不会阻塞写入者，也不会看到撕裂尾部。browser worker 里原生 flock 被打桩成立即成功，因为那里只有一个进程，进程内 claim 已经足够。

存储 unit 自己不做并发串行化，这一点在契约里写明：写入顺序属于调用方。domain 用写链补上，所以业务代码看到的是「同一个域不会有两个写同时进行」。SQLite 后端另加了一个同进程 double-open 守卫：同名 unit 已打开时直接抛 `kv unit '<name>' is already open (double-open is a caller bug)`，把误用变成显式错误。

租约的获取顺序（`session-persistence-jsonl/src/lease.ts`）：

```ts
for (let attempt = 0; attempt < 3; attempt += 1) {
  const handle = await open(path, 'w')
  await tryLockExclusive(handle.fd)            // 争用抛 EAGAIN
  const held = await handle.stat({ bigint: true })
  const current = await stat(path, { bigint: true })
  if (current.ino === held.ino && current.dev === held.dev) return lease
  await handle.close()                         // 锁住的已不是路径上的文件，重来
}
```

POSIX 用 `flock(2)` 的非阻塞模式，Windows 用命名内核信号量。锁不设过期时间：持有者进程崩溃时内核释放，进程卡死时不释放。后者会让其他写者拿到 `SessionAlreadyOwnedError`，这是有意的取舍，征用一个卡住的写者会让日志撕裂。

读方从不碰锁。只读打开只做帧扫描与前缀验证，所以一个正在写的会话可以同时被 session-query 读取，读到的永远是某个已提交前缀。

## 6. 迁移：三种版本策略，三种代价

DSH 里有三套独立的版本号，各自对应一种迁移策略：

| 版本 | 存放位置 | 不匹配时 | 为什么 |
|---|---|---|---|
| 会话格式 `SESSION_FORMAT_VERSION` | 日志文件名与 header | 发布新的 generation，旧文件不动 | 已提交的日志是证据，不能改 |
| 存储域版本 | `units.version` 行或每记录文档 | 在 `compatibleVersions` 内接受，否则拒绝 | 域数据可以读旧写新，但不能猜 |
| SQLite 物理布局 | `PRAGMA user_version` | 直接拒绝，不做原地迁移 | 布局变了就没有安全的读法 |
| 派生索引版本 | `PRAGMA user_version` + `application_id` | DROP 后重建 | 索引可丢弃，重建最便宜 |

会话格式当前是 4（`packages/core/session/src/types.ts:90`），迁移链是 `v0→v1`、`v1→v2`、`v2→v3`、`v3→v4`，每个包只负责一段。迁移的产物是一个新的 generation 文件，源文件的路径、字节与 inode 都保持不变。这条约束来自仓库的持久化规则：已提交的代际只能新增后继，不能被移动、覆盖或删除。代价是磁盘上会同时存在多个代际，收益是任何一次迁移都可以回退到「用旧 build 读旧文件」，而不需要备份。

读取时选择数值最高的已知代际。如果磁盘上出现一个比当前 build 更高的代际，即使还有更旧的可读文件，也直接报 `SessionFormatUnsupportedError`。理由很直接：更高代际里可能有当前 build 不认识的必读语义，退回旧文件会给出一个看似成功、实际缺事件的会话。

{{< fig "diagrams/04-generation-migration.svg" "代际链与四种版本策略。会话格式独占发布新代际、旧文件不动；域版本在兼容区间内接受；SQLite 布局不匹配直接拒绝；派生索引不匹配则 DROP 重建。右下是被删除的 SQLite 会话存储分支：旧库当前 build 不打开。" >}}

存储域的兼容区间是一个显式列表，比如投影缓存的域声明 version 7、`compatibleVersions: [3,4,5,6]`（`session-projection-cache/src/spec.ts:102-108`）。读到区间内的旧记录时，第一次成功写入会把当前版本戳与数据放在同一个事务里提交；SQLite 后端用 `BEGIN IMMEDIATE` 做这件事（`storage-sqlite/src/unit.ts:147-185`），JSON per-record 后端则写在每份文档里。区间外的版本不迁移，直接失败。

坏记录有独立策略。投影缓存用 `backup-and-skip`：解析失败的记录被移到 `<key>.json.bak.<stamp>`，域继续启动，少一条缓存而不是整个会话列表打不开。这个策略只适用于可重建数据；会话日志没有这一项，因为丢一条事件就是丢事实。

代价最重的一次迁移发生在仓库内部。`@deepseek-ai/dsh-session-persistence-sqlite` 曾经交付过，后来被删除（`.agents/notes/implemented/simplification/2026-08-30-jsonl-only-session-persistence.zh.md`）。理由写在决策记录里：同一个逻辑服务维护两种物理格式，会让每项会话契约、事件封套变更、恢复规则、平台测试矩阵都翻倍，而交付 profile 并不选它。删除的后果也写得很清楚：当前 build 不打开、也不迁移旧 provider 写出的数据库，操作者必须先用旧 build 导出逻辑会话再升级。

这次删除解释了本篇标题里的「迁移的代价」：迁移的成本不只在写迁移代码，还在永久维护第二套崩溃语义。删掉一个后端，等于删掉一整条测试矩阵。

代际链每一项都是一次独立迁移，源文件保持不动：

- `session-format-v0-to-v1`：补上事件封套。
- `session-format-v1-to-v2`：引入 `turn` 与 `step` 的边界事件。
- `session-format-v2-to-v3`：加入工具结果的错误结构。
- `session-format-v3-to-v4`：当前版本，补齐可忽略事件的标记。

每个迁移包只读源 generation，写出新 generation。失败时源文件仍是完整的，用户可以用旧 build 打开。迁移不做原地改写，因为原地改写会让「升级失败」变成「数据损坏」。

## 7. 损坏恢复：截尾、补洞、隔离

不同损坏形态对应不同动作，混在一起处理会丢数据：

| 症状 | 检测者 | 动作 | 丢什么 |
|---|---|---|---|
| 最后一个 zstd 帧不完整 | `scanZstdFrames` 的 `tornStart` | 截断撕裂字节，重写能完整解出的记录 | 最后一条没写完的记录 |
| 最后一行原始 JSONL 不完整 | 逐行解码 | 丢弃该行 | 该行 |
| 完整帧校验和/解压/结构失败 | 帧解码器 | 报损坏，拒绝读取 | 不丢；拒绝而不是截断 |
| 日志以未配对 `turn/start` 结束 | resume | 经写句柄补 closer 事件 | 不丢；补的是缺失的收尾 |
| 会话格式代际过高 | header/文件名 | `SessionFormatUnsupportedError` | 不丢；拒绝 |
| 投影缓存记录解析失败 | 域加载 | 移到 `.bak.<stamp>`，继续 | 该条缓存 |
| KV 批次状态不明 | 后端 | `unknown-state`，关闭前全拒 | 不丢；隔离 |

第三行与第二行的区别是整个恢复设计的分界：只有在「物理尾部没写完」时才可以截断；一旦一个完整帧被读出来但校验失败，说明是介质损坏或人为改动，截掉它等于销毁证据。代码把这两种情况分成 `SessionPersistenceCorruptionError` 与 `SessionFormatUnsupportedError`，前者说数据坏了，后者说数据没坏但这一版读不了。

未配对 `turn/start` 是逻辑中断，不是物理损坏，所以不截断。resume 通过写句柄读日志，算出需要补的 closer：缺失的工具错误结果、未闭合的 `step/end`、一条合成的 `turn/end {reason:{kind:'interrupted'}}`，然后作为普通批次追加，再发布 Session 给上层。`interrupted` 是唯一不由 agent loop 发出的 TurnEndReason。只读观察方（session-query）只在内存里配平，不回写，因为它没有写租约。

备份与恢复没有专门的命令，恢复动作就是文件操作：

1. 停掉所有写者，或者先 `flush` 再停，确保没有 200ms 窗口里的事件。
2. 复制整个会话目录。多代际一起复制，别只拿最高代际，旧 build 可能只认旧的。
3. 恢复时把目录放回同一个根下，保持 `--cwd--/session-id/` 的相对结构，文件名不要改。
4. 重启后先 `list` 与 `stat`，确认最高代际与 revision 符合预期，再 `open`。
5. SQLite 库要连同 `-wal`、`-shm` 一起复制；更稳妥的做法是停进程后复制，或者用 SQLite 自己的 backup 接口。

自己搭 Agent 时，把「损坏」拆成至少三类再设计恢复：尾部没写完、完整记录坏了、版本读不懂。第一类可以自动修，第二类必须拒绝并保留现场，第三类要能明确告诉用户该用哪个版本。三类混成一个 `catch`，最后一定会在某次升级后静默丢数据。

一次真实的恢复过程按这个顺序发生：

1. `open(id, 'read')` 读到文件尾，发现最后一个 Zstandard 帧不完整。
2. 帧扫描器返回 `tornStart`，解码器尽力解出该帧里完整的记录。
3. 读方只返回验证过的连续前缀，撕裂部分不进 Session。
4. `resume` 拿到写句柄，先 `truncate(tornStart)` 并 `fsync`，再重写能解码的记录。
5. 计算 `interruptedTurnClosers`，补齐缺失的工具错误、未闭合的 `step/end`、合成的 `turn/end`。
6. 这些补齐事件作为普通批次追加，和正常事件走同一条写路径。

第 4 步的顺序不能反：先截断再重写。反过来会在撕裂帧后面追加新帧，形成一个永远解不开的洞。

## 8. 选型表

| 部署 | 会话日志 | domain KV | 说明 |
|---|---|---|---|
| 单机 CLI | JSONL，压缩开 | JSON `single` 或 `per-record` | 一个进程写，锁几乎不争用；文件可直接 `cat`/`jq` 排障 |
| 桌面（GUI host） | JSONL，压缩开 | JSON `per-record` 优先 | 多个窗口可能同进程多会话；per-record 写放大与文件大小无关 |
| 服务端单机多会话 | JSONL，压缩开 | SQLite（单进程持有） | 每会话一把租约，跨进程安全；SQLite 后端没有 `busy_timeout`，别让两个进程写同一个库 |
| 服务端共享目录 | JSONL，压缩开 | 不建议 JSON `single` | 租约能保护会话日志；`single` 的 last-write-wins 只对「每进程一个写者」成立 |
| 需要全文检索 | JSONL + `session-query-sqlite` | 独立库 | 索引可随时 DROP 重建，不承担权威存储职责 |

两个容易选错的点：

- 会话量大不等于该换 SQLite 存会话。DSH 自己在有 SQLite 可用的情况下仍只保留 JSONL 会话存储，因为第二套格式的维护成本落在每一次格式变更上。
- 需要行级查询的是派生数据，不是事实。把 FTS 索引当权威存储，会在索引重建的那一天发现事实没了。

三种部署的默认答案：

- 单机 CLI：JSON domain 加 JSONL 会话日志。没有并发写者，`single` 布局的整文件重写可以接受。
- 桌面：同一套，但把 `writeEveryEvents` 调小，减少崩溃时重放的事件数；备份靠复制 `$DSH_HOME`。
- 服务端：一个会话一个写者，多副本靠上层复制。当前仓库没有多写者协调，也没有共享文件系统上的锁语义，跨机共享同一个会话目录不在支持范围。

如果要换 SQLite，先确认换的是哪一层。会话日志换 SQLite 需要重写整个持久化 provider，仓库里已经删掉过一版；domain 存储换 SQLite 是配置改动，`backend: sqlite` 加一个数据库路径即可。

## 9. 设计取舍

- **延迟实体化**：`create` 不写盘，换来创建零成本与「从未 append 的会话不存在」的干净语义。代价是 `stat`/`list` 需要能处理「逻辑存在、物理不存在」，`flush` 成为显式的存在性承诺。
- **追加而不重写**：日志只增，写放大接近 1，读取要扫全文件。为了让扫描可接受，才需要 header 分离、`stat().revision` 令牌与投影缓存。
- **`link` 而不是 `rename`**：多花一次目录 `fsync`，换到不覆盖的发布语义。会话是证据，静默覆盖比失败更糟。
- **锁不过期**：卡住的写者会让后来者报错，用户需要手动处理。换来的是任何时刻最多一个写者，日志不会因为「以为对方死了」而撕裂。
- **拒绝高代际**：宁可打不开，也不退回旧文件给出缺事件的会话。诊断信息必须点名绝对路径，否则用户不知道该删哪个文件。
- 三种版本策略并存：权威数据只增不改，域数据允许兼容区间，派生数据直接重建。用一套策略处理三种数据，不是过度保守就是丢数据。
- 删除 SQLite 会话后端：减少一条持久化路径与整套平台测试，代价是失去 WAL 的强事务选项，且旧库需要旧 build 导出。

## 10. 踩坑点

- 只 `fsync` 文件不 `fsync` 目录，掉电后文件内容在、目录项不在。实体化路径逐级同步父目录就是为了这个。
- 用 `rename` 发布会话，两个进程同时创建时后写的静默覆盖先写的。`link` 的 `EEXIST` 才是正确行为。
- 追加失败后不回滚文件长度，下一次追加会接在半条记录后面，整段日志从此不可读。`rollbackAppend` 必须在重抛之前完成。
- 把撕裂帧整帧丢掉。帧里可能已经有若干条完整记录，正确做法是先解出完整前缀再截断。
- 把完整帧的校验失败也当撕裂尾截掉，等于在介质损坏时销毁证据。
- 在 `session/flush` 之外依赖 200ms 定时器做持久性保证。后台写失败后 `drainPaused` 会停掉自动路径，只有下一次显式 flush 才重试并把错误抛给调用方。
- 以为 `create` 之后磁盘上一定有文件。没有 append 也没有 flush 的会话，崩溃后不存在。
- 以为 `stat().revision` 跨进程可比。它只承诺「相等即未变」，不承诺「不等即变了什么」。
- 在 JSON `single` 布局上开多进程写同一个 unit。后端契约把顺序交给调用方，last-write-wins 的前提是每进程一个写者。
- 手工改 SQLite 的 `user_version`。改成 0 会让空库被当成新库盖戳，改成别的值会让整个库打不开。

- 把 `single` 布局用在事件量大的域上。每次写重发整个文件，写放大与 unit 大小成正比。
- 在 `deleteRecords` 之后立刻读同一批键。批量删除是原子的，但并发的读可能看到删除前或删除后的快照，取决于它进队列的时机。
- 用 `stat().revision` 判断「内容有没有变」以外的语义。它只保证相等时未变，不保证不等时一定变了。

## 11. 源码索引

- `packages/storage/storage/src/backend.ts`：`StorageBackend`、`KvFacet`、`KvUnit` 契约与错误分类。
- `packages/storage/storage/src/registry.ts`：backend 名表、挂载与注销、`storageBackendServiceKey`。
- `packages/storage/storage-domain/src/`：`DomainSpec`、写链、`unknown-state` 隔离、`domain/changed`。
- `packages/storage/storage-json/src/atomic.ts:1-57`：临时文件、`fsync`、`rename`、目录同步。
- `packages/storage/storage-json/src/single-unit.ts`：整文件重发布与内存回滚。
- `packages/storage/storage-json/src/per-record-unit.ts:230-300`：批量删除的 rename 出集与恢复。
- `packages/storage/storage-sqlite/src/schema.ts:31-115`：`user_version` 盖戳、journal mode、`STRICT` 表。
- `packages/storage/storage-sqlite/src/unit.ts:147-185`：`BEGIN IMMEDIATE`、版本戳同事务、rollback 失败转 `unknown-state`。
- `packages/storage/storage-sqlite/src/index.ts:63-120`：double-open 守卫、版本校验、failure 常驻。
- `packages/session/session-persistence-jsonl/src/lease.ts:1-135`：`flock` 租约、inode 校验、Windows 信号量。
- `packages/session/session-persistence-jsonl/src/generation.ts:715-845`：临时文件发布、`link` 独占、代际扫描。
- `packages/session/session-persistence-jsonl/src/index.ts:1264-1339`：`materializePosix` 的目录同步与不覆盖发布。
- `packages/session/session-persistence-jsonl/src/index.ts:1409-1465`：`appendLines`、`rollbackAppend`、`repair`。
- `packages/session/session-persistence-jsonl/src/storage.ts:36,259-346`：200ms 批窗口、`persistContiguous`、失败保留批次。
- `packages/session/session-checkpoint-policy/src/index.ts:36-82`：模型请求与工具执行前的强制 flush。
- `packages/session/session-projection-cache/src/index.ts:249-273`：先 flush 再写缓存的顺序。
- `packages/session/session-projection-cache/src/spec.ts:102-108`：域版本、兼容区间、`backup-and-skip`。
- `packages/session-query/session-query-sqlite/src/schema.ts`：派生索引的 `application_id`、版本重置与 FTS5 表。
- `docs/subsystems/storage.zh.md`、`docs/subsystems/persistence.zh.md`：契约与崩溃语义的规范描述。
- `.agents/notes/implemented/simplification/2026-08-30-jsonl-only-session-persistence.zh.md`：删除 SQLite 会话存储的决策与后果。

- `packages/storage/storage-json/src/per-record-unit.ts:230-300`：批量删除的 rename 出集与失败恢复。
- `packages/session/session-persistence-jsonl/src/lease.ts:1-135`：flock、Windows 信号量、inode 校验。
- `packages/session/session-checkpoint-policy/src/index.ts:36-82`：三个检查点。
- `packages/session/session-projection-cache/src/spec.ts:102-108`：缓存域版本与备份策略。
- `packages/session-query/session-query-sqlite/src/schema.ts`：FTS5 派生索引与重建条件。
- `.agents/notes/implemented/simplification/2026-08-30-jsonl-only-session-persistence.zh.md`：删除 SQLite 会话后端的决策。

## 最后

存储选型最容易被当成性能题，实际先是一道语义题：这个字节丢了，谁知道？会话日志的答案是「模型还没看见的人不知道，模型看见的必须已经在盘上」，于是有了检查点屏障、200ms 窗口和不覆盖发布。JSON 整文件的答案是「内存说了算，盘上是副本」，于是可以有 last-write-wins。SQLite 的答案是「事务边界就是真相边界」，于是版本不对就整库拒绝。

自己搭 Agent 时，先写清楚每个字节的归属，再挑介质。介质换起来通常只要一天，崩溃语义换起来要重写所有恢复路径，还要把测试矩阵翻倍。DSH 删掉 SQLite 会话后端这件事，就是这个价格的账单。

相关阅读：[会话日志与事件溯源](/posts/dsh-session-event-sourcing.html)、[检索与召回](/posts/dsh-recall.html)、[宿主组合](/posts/dsh-host-composition.html)。
