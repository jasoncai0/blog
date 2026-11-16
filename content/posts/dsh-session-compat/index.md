---
title: "会话格式的四次换代：文件名、迁移链与回放的拒绝权"
date: "2026-11-16T20:30:00+08:00"
summary: "v0 到 v4 的会话文件如何共存、逐代迁移、拒绝不自洽的回放，以及 resume 与 fork 的切点差别。"
description: "从 session.jsonl 到 session.v4.jsonl：文件名选择、四个相邻迁移包、严格回放的四道闸、ignorable 的兼容账单，以及两个恢复切点。"
tags: ["event-sourcing","architecture","harness"]
slug: "dsh-session-compat"
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）。逐行读过 `packages/session/session-format/`、`session-format-catalog/`、`session-persistence-jsonl/src/{index,generation,format}.ts` 与 `packages/core/session/src/{index,fork,repair,surface,known-event-types}.ts`；四个迁移包只读了 codec、validation 与 README。证据等级：代码路径与错误文本为实测，`docs/subsystems/session.zh.md` 仅作对照，文档与实现冲突处按源码写。没有运行迁移或 fork，跨代行为来自静态阅读。

先假设一个具体场景。2025 年 8 月留下的会话目录里只有 `session.jsonl`，今天用 v4 的构建 `resume` 它。文件不会原地打开，也不会被就地改写：目录扫描先认出这是 v0，catalog 调出四条相邻边，逐段迁移，最后按 v4 的规则重放。中间任何一段拒绝，调用方拿到的是一个分类错误，不是一个缺了几条事件的会话。

这条路走不走得通，取决于四个设计：文件名能否独立标识代际、每条迁移边是否只负责一代、回放时敢不敢拒绝、以及切点落在哪条 seq 上。

## 1. 文件名先说话

会话文件的名字不是装饰。v0 沿用最初的 `session.jsonl`，从 v1 起每代一个后缀，规范名由 `session-format/src/filename.ts` 一个正则决定：

```ts
const CANONICAL_LOG_FILENAME = /^session(?:\.v([1-9][0-9]*))?\.jsonl$/u

export function sessionFormatLogFilename(version: number): string {
  const generation = sessionFormatVersion(version, 'Session log generation version')
  return generation === 0 ? 'session.jsonl' : `session.v${generation}.jsonl`
}
```

正则有几处刻意的窄口：`[1-9][0-9]*` 不接受 `v0`，也不接受 `v01`；小写 `v` 写死，`Session.V1.jsonl` 不算；后缀必须是 `.jsonl`，压缩名 `session.v4.jsonl.zstd` 也不直接匹配。`parseSessionFormatLogFilename` 对不合规的名字返回 `undefined`，目录扫描据此把临时文件、备份文件、别的格式排除在外。

名字的容错比想象中窄。`sessionFormatVersion` 还要求版本是非负安全整数，负数与超过 `2**53` 的数都进不来。目录里出现 `session.v2.jsonl.tmp`、`session.V2.jsonl`、`session.v02.jsonl`，扫描一律当作不认识，不会猜测它们属于哪一代。这个严格度是有意的：文件名是选择代际的唯一输入，宽松一点就会把两个不同代际的文件认成同一个。

| 代际 | 文件名 | 这一代主要改了什么 |
| --- | --- | --- |
| v0 | `session.jsonl` | 没有版本后缀，legacy 平铺布局 |
| v1 | `session.v1.jsonl` | legacy 归一化：事件改名、删字段、翻译退休的结束原因 |
| v2 | `session.v2.jsonl` | 顶层 `assistant/chunk` 内嵌进 message，seq 重排稠密 |
| v3 | `session.v3.jsonl` | system prompt 提升为 `system/message`，信封规范化 |
| v4 | `session.v4.jsonl` | 工具结果升为 tool role，补子会话证据 |

{{< fig "diagrams/01-generations.svg" "五代规范文件名与相邻代际的主要变化" >}}

## 2. 为什么版本写在文件名里

header 里其实也有 `version` 字段。两处都写，是因为它们回答不同的问题：文件名回答“这是哪一代、我该用哪个 codec”，header 回答“这个文件自己认为它是哪一代”。前者让读取者不必打开 body 就能分类，后者让写坏的文件无处可藏。

目录扫描的代码把这一点写得很直白：

```ts
for (const entry of entries) {
  const version = parseGenerationLogFilename(entry.name, this.compression)
  if (version !== undefined) {
    generations.push({ path: join(dir, entry.name), version })
    continue
  }
  if (parseGenerationLogFilename(entry.name, this.oppositeCompression()) !== undefined) {
    opposite.push(join(dir, entry.name))
  }
}
if (opposite.length > 0) throw this.encodingMismatch(opposite[0] as string)
const latest = generations.sort((left, right) => right.version - left.version)[0]
```

选择只发生在名字上：排序、取最大、算出目标路径。`header.version` 要到打开 body 时才读，而且读出来必须与文件名一致，否则 `generation.ts` 直接抛：

```ts
if (version !== sourceVersion) throw new Error(
  `resolved JSONL source filename identifies v${sourceVersion}, but its header identifies v${version}`,
)
```

header 里不存数字 cut，这是 v2 起定下的规则。写文件时 `toHeaderLine` 只接受两种组合：seeded 必须给出 cut，unseeded 的 cut 必须为 0。

```ts
if (header.isSeeded && inheritedEventCount === undefined) {
  throw new Error('seeded session header requires an inherited event count')
}
const cut = SessionLogOffset(inheritedEventCount ?? 0)
if (!header.isSeeded && cut !== 0) {
  throw new Error('unseeded session header inherited event count must be 0')
}
```

文件里存下的只有 `isSeeded`。cut 落在事件里，由最后一条 inherited 的 `session/end-seed` 的 seq 决定。读取时从事件恢复，写入时反向校验，两处用的是同一套定义。

这带来三个可用的性质。同一目录里 v0 与 v1..v4 可以并存，迁移只写后继文件，源文件字节不变，回滚就是把旧文件重新变成最高代际。原子发布、租约与崩溃尾部这些持久化细节，在 [持久化与存储](/posts/dsh-persistence-and-storage.html) 里已经拆过。`stat` 与 `list` 这类只需要元数据的操作只读 header，不读事件行，代价是常数级。名字被改坏（比如 `v2` 改成 `v3`）不会静默按 v3 解释，header 会把矛盾暴露出来。

自己搭的时候，第一步就该把“选哪个文件”和“这个文件是什么”拆成两次判断。只做一次、把版本完全交给 header，list 就得读 body；把版本完全交给文件名、不校验 header，改名就会变成数据损坏。

## 3. 四个包，四条相邻边

`packages/session/` 下并排放着四个迁移包：`session-format-v0-to-v1`、`-v1-to-v2`、`-v2-to-v3`、`-v3-to-v4`。每个包导出一个 `SessionFormatMigration`，声明 `fromVersion`、`toVersion`、`createStage`，边与边之间没有捷径。

链的构造先做两道检查。每条边必须相邻，否则构造失败：

```ts
if (to !== from + 1) {
  throw new SessionFormatError(`${migration.name} must declare adjacent v${from}->v${from + 1}`)
}
```

然后从 0 遍历到当前版本，缺一段就拒绝：

```ts
for (let version = 0; version < this.currentVersion; version += 1) {
  ...
  throw new SessionFormatUnsupportedMigrationError(`Session migration v${version}->v${version + 1} is missing`)
}
```

多出来的边（指向更早版本、或者成环）同样拒绝，报 `does not lead to current v4`。取边逻辑反而最短：

```ts
private plan(fromVersion: number): readonly SessionFormatMigration[] {
  const from = sessionFormatVersion(fromVersion, 'stored Session format version')
  if (from > this.currentVersion) {
    throw new SessionFormatUnsupportedMigrationError(
      `stored Session uses newer format v${from}; this build writes v${this.currentVersion}`,
    )
  }
  return Object.freeze(this.migrations.slice(from))
}
```

`migrations` 在构造时已按 fromVersion 排好序，计划就是一次切片。存盘版本比当前构建新时不做降级尝试，直接说“这个构建写的是 v4”。这不是防御性编程的装饰：catalog 是生成文件，`currentVersion: 4` 与四条边在编译期就固定，任何手工加边都必须过这三道检查。

四条边各管一段，职责差别很大：

| 边 | 输入形态 | 主要动作 | 风险点 |
| --- | --- | --- | --- |
| v0 → v1 | legacy 平铺事件，字段名还是旧的 | 改名（`steering/message` → `user/message`、`compact/*` → `compaction/*`）、删 `turn/start.trigger`、翻译退休的结束原因、补确定性 id | 只接受冻结清单内的事件，连 `ignorable:true` 的未知事件也拒绝 |
| v1 → v2 | 顶层 chunk 与 message 分离 | 把 chunk 内嵌进对应 message，无主 attempt 落一条 log-only 事件，seq 稠密重排，引用跟着改 | 基数会变，所有同会话引用都要重映射 |
| v2 → v3 | system prompt 还是请求信封的一部分 | 提升为 `system/message`，插入事件改变事件数与 seq，重映射本地引用 | 插入位置决定继承 cut 是否移动 |
| v3 → v4 | 工具结果是 message 内的块 | 升为 tool role，重命名 message source，补写有证据的 interrupted turn | 历史 body 恢复必须显式 child evidence |

stage 的输入是四个字段，每个都对应一条契约：

```ts
stage = migration.createStage({
  sourceHeader: header,
  targetHeader,
  sourceInheritedEventCount: inheritedEventCount,
  sourceKind: index === 0 ? 'decoded' : 'transformed',
})
```

`sourceHeader` 与 `targetHeader` 把“从哪一代来、到哪一代去”写进签名；`sourceInheritedEventCount` 是上游算好的 cut，首段来自文件头与 marker，之后每段来自上一段 stage 的声明；`sourceKind` 区分“这段输入是从盘上解出来的”还是“上一段迁移造出来的”。最后一项影响校验强度：解码出来的行还要过该代 codec 的准入，迁移造出来的行只保证符合目标代际的规则。

相邻而不是“任意版本跳到当前”，理由有三个。每条边只需要理解一种源形态，测试可以只针对这一对版本；已发布的边不必随着新版本重写，N 代只需要 N-1 条边，而不是 N² 条两两转换；拒绝的粒度落在具体一段上，`sessionFormatV1ToV2 refuses this format v1 event: ...` 能直接告诉你哪一代的假设不成立。

{{< fig "diagrams/02-migration-edges.svg" "五个编解码器与四个相邻迁移包的职责划分" >}}

## 4. 迁移链的失败点

迁移不是“尽力而为”的转换。每个 stage 有三处可以拒绝：创建 stage、转换事件、finish 收尾。三处都被同一个包装函数接住：

```ts
function throwUnsupportedRefusal(
  migration: SessionFormatMigration,
  error: unknown,
  subject = 'Session',
): never {
  if (error instanceof SessionFormatUnsupportedMigrationError) throw error
  const detail = error instanceof Error ? error.message : String(error)
  throw new SessionFormatUnsupportedMigrationError(
    `${migration.name} refuses this format v${migration.fromVersion} ${subject}: ${detail}`,
    { cause: error },
  )
}
```

拒绝被归到“读得懂，但没有受支持的迁移”这一档，与“文件损坏”分开。前者可以提示用户升级或换构建，后者只能报损坏。

第二处硬约束是继承 cut。stage 创建时声明 `headerInheritedEventCount`，finish 返回的实际值必须与声明一致：

```ts
if (this.entry.stage.headerInheritedEventCount !== undefined
  && this.entry.stage.headerInheritedEventCount !== targetCut) {
  throw new SessionFormatError(`${this.entry.migration.name} changed its predeclared inherited cut`)
}
```

这条检查防的是一类很难事后发现的错误：迁移插入或删除事件，忘了同步继承边界，会话能打开，但 fork 谱系与 surface 起点全错。声明在前、校验在后，把“我知道 cut 会怎么变”变成可执行的断言。

第三处是迁移后的目标校验。`catalog.ts` 在 `validation: 'transformed'` 下只应用已发布的 v4 校验，失败时抛：

```ts
throw new SessionFormatUnsupportedMigrationError(
  `Session migration from v${this.sourceVersion} to v${this.currentVersion} refuses the transformed artifact: ${detail}`,
  { cause: error },
)
```

`validation: 'current'` 则连当前构建自己的不变量也一起跑。两种模式的区别是“只保证历史数据符合发布时的规则”还是“还要符合本构建新增的规则”。跨代读取用前者，本代读取用后者。

把一条 v0 会话打开，实际顺序是六步：

1. 目录扫描按文件名认出 v0，算出目标路径 `session.v4.jsonl`。
2. 读 v0 header，`readHeader` 返回 `migration-required`，带上解码后的 header 与目标版本。
3. `plan(0)` 取出四条边，逐段 `createStage`，首段 `sourceKind: 'decoded'`。
4. 逐行解码 v0，把事件喂给第一段 stage；每段的输出是下一段的输入。
5. 最后一段 finish，返回 v4 的 cut；catalog 用 `restoreReleasedV4Artifact` 校验整个 artifact。
6. 发布新代际文件，源文件不动；下次打开时 v4 已是 current，只走单代解码。

任何一步抛 `SessionFormatUnsupportedMigrationError`，都会停在“读得懂但拒绝迁移”这一档，不会退化成部分重建。若抛的是 `SessionFormatError`，说明格式本身不自洽，比如 seq 不连续或 header 与 marker 矛盾。

整条链在构造时是从下游往上串的。`CompiledSessionFormatMigrationStream` 用 `entries.toReversed()` 倒着包 stage，最后一段的输出就是最终输出，输入则一路回传到第一段的解码器：

```ts
let downstream = output
for (const [offset, entry] of entries.toReversed().entries()) {
  const context = new ChainedMigrationContext(entry, downstream)
  stages[entries.length - offset - 1] = context
  downstream = context
}
```

这个写法的好处是每段只为下一个消费者负责。上游不知道最终是 v4，下游也不需要回溯自己收到的行经过了哪些改写。

还有一处失败在文件层：迁移期间源文件被改写，`JsonlGenerationSourceChangedError` 会抛 `historical session generation changed during migration: "..."`。读写路径会重试一次，仍然变化就放弃。迁移读的是历史代际，写的是新代际，源文件本不该动；这个错误说明有另一个进程在写同一个 id。

## 5. 严格回放：四道闸

回放的入口是 `restoreReleasedV4Artifact`。它先看 header 与 cut，再逐事件过闸：

```ts
const cut = sessionFormatCount(artifact.inheritedEventCount, 'format v4 inherited event count')
if (cut > artifact.events.length) throw new SessionFormatError('format v4 inherited event count exceeds its events')
if (!artifact.header.isSeeded && cut !== 0) throw new SessionFormatError('unseeded format v4 Session has inherited events')
let lastInheritedMarker: number | undefined
for (const [index, event] of artifact.events.entries()) {
  if (!knownEventTypes.has(event.type) && event['ignorable'] !== true) {
    throw new SessionFormatUnsupportedMigrationError(
      `format v4 contains unknown event type ${JSON.stringify(event.type)} at seq ${index}`,
    )
  }
  if (event.seq !== index) throw new SessionFormatError(`format v4 event ${index} is not dense`)
  ...
}
```

四道闸分别是：逐行解码（非法 JSON、负数 seq、`-0` 在更早的 `sessionFormatCount` 就失败）、代际准入（上面这段）、关系校验（`assertReleasedV4Relationships`）、surface 折叠（`foldSurface` 逐事件要求 `seq === index`）。

打开之前还有一层分类，`readHeader` 只看 header 不读 body，返回四种状态之一：

| 状态 | 触发条件 | 调用方拿到什么 |
| --- | --- | --- |
| `current` | 存盘版本等于当前版本，且 header 通过本代校验 | header，可直接开 body |
| `migration-required` | 存盘版本更旧，且存在完整迁移链 | 迁移后的 header 与源版本 |
| `unsupported` | 版本更新，或缺 codec，或迁移明确拒绝 | 原因文本，不尝试打开 |
| `malformed` | header 本身不合法（字段类型错、非无损 JSON） | 错误，带源版本 |

严格回放的核心是拒绝权。遇到不认识的必需事件，读器不允许“跳过继续”，因为跳过后重建出来的会话看起来完整，实际缺了一条会改变语义的事件。`Session` 构造函数那条错误写得更具体：

```ts
throw new Error(`seed event at index ${index} has seq ${snapshot.seq} (expected ${index}); seed must be contiguous from 0`)
```

日志为什么被当成唯一真源、surface 怎么把事件折成消息，见 [会话日志与事件溯源](/posts/dsh-session-event-sourcing.html)。那篇讲的是形状，这里讲的是形状变了以后怎么读旧文件。

recoverable 是另一条路，只用于持久化读路径：解码失败后丢弃整条坏行，保留前缀；如果后面的 `turn/end` 证明这段区域已经提交，就把解码错误抛出来。允许丢弃的只有尾部未提交的半行，已提交区域里的损坏不能吞。

{{< fig "diagrams/03-strict-replay.svg" "解码、代际准入、关系校验、surface 折叠四道闸与失败消息" >}}

## 6. ignorable：向前兼容的账单

新事件类型要被旧读器接受，唯一通道是信封上的 `ignorable: true`。读路径的规则写在 `known-event-types.ts` 的文档注释里：

```ts
if (!KNOWN_SESSION_EVENT_TYPES.has(event.type) && event.ignorable === true) return
```

未知且带 `ignorable` 的事件被留在日志里，surface 不消费它，元数据保留；未知且没有这个标记，读取直接拒绝。

插件记录走的是另一条路。`#appendRecord` 在写事件时强制加上 `ignorable: true`，插件作者无法把这个标记去掉：

```ts
const record: unknown = deepFreeze({
  type,
  seq: SessionSeq(this.log.length),
  time: Date.now(),
  data: dataSnapshot,
  ignorable: true,
})
assertPluginRecordEvent(record)
```

插件事件被当作“可省略”的一类，旧读器跳过它们不会改变内置语义。代价是插件自己也不能假设事件一定被读到。

这个机制的注释解释了两件事。为什么不静默跳过：跳过一条必需事件会重建出错误的会话。为什么不做事件名注册：注册只能说明“这个名字存在”，无法说明“省略它是否安全”，而且会让读取行为依赖运行时装了哪些包。

代价落在写新事件的人身上，具体有三条：

1. 每个新事件类型都要先回答“旧读器跳过它，会不会得到错误的会话”。会，就不能标 `ignorable`，只能升版本、加迁移边。
2. 标了 `ignorable` 的事件可能被旧读器整个丢掉，连 surface 都不进。依赖它做投影的新功能，在旧构建里等于不存在。
3. 迁移边必须处理“未知但 ignorable”的输入。v0→v1 是 alpha 边，做法最严：冻结清单之外的事件一律拒绝，哪怕带了 `ignorable`，这样那条边的输出集合是封闭的；v2 起的已发布校验则接受“已知类型 + ignorable 未知类型”。

v4 的行准入对未知 `developer/message` 有一处早退：带 `ignorable` 的未知 developer 消息跳过 developer 数据断言，直接放行。这个检查发生在 reader 丢弃可恢复尾部之前，作用正是让未来新增的开发者消息类型能在旧 v4 读器里活下来，不被误判成结构损坏。

自己搭的时候，宁可先升版本再加事件，也不要为了省一条迁移边把语义事件标成可忽略。兼容的账单不会消失，只会从读路径转到“旧客户端看到残缺状态”的工单里。

## 7. resume 与 fork：两个切点

resume 的切点固定在物理日志末尾。它先把日志全量读出来，补齐中断的轮次，再把补过的那段一起作为 seed：

```ts
const coldRead = await handle.read(0, undefined, { signal: fused })
const persisted = coldRead.events
const closers = interruptedTurnClosers(persisted)
if (closers.length > 0) await handle.append(closers)
preparation = SessionPreparation.create(this.runtime.ctx.sessions.prepare(id, {
  seed: [...persisted, ...closers],
  meta: structuredClone(handle.header),
  inheritedEventCount: handle.inheritedEventCount,
  eventState: coldRead.eventState,
}))
```

closer 由 `repair.ts` 生成：如果最后一步有工具调用没有结果，补一条合成的 `tool/result`，再补 `step/end` 与 `turn/end`。合成的工具结果带 `TOOL_NOT_STARTED` 或 `TOOL_OUTCOME_UNKNOWN` 的 code，文本明确告诉模型“只在只读或幂等时重试”。这段语义修复属于 agent 层，persistence 只负责交回物理有效的日志。

fork 的切点是调用方给的 inclusive seq，复制前缀之后立刻写入继承标记：

```ts
const prefix = events.slice(0, boundary + 1)
prefix.push({
  type: 'session/end-seed', seq: SessionSeq(boundary + 1),
  time: events[boundary]!.time,
  data: { inherited: true },
})
return prefix.concat(openTurnClosers(prefix, { kind: 'forked' }))
```

两个切点的差别集中在四件事上：

| 维度 | resume | fork |
| --- | --- | --- |
| 切点 | 物理日志末尾，调用方不能指定 | 任意连续事件 seq，默认最后一条 |
| 未闭合的 turn | 由 agent 层补 interrupted closer | 由 `buildForkSeed` 补 forked closer |
| 继承标记 | 不改，沿用 handle 上的 cut | 新写一条 `session/end-seed {inherited:true}` |
| `inheritedEventCount` | 原样取自 handle，普通会话为 0 | `boundary + 1` |

v4 的 header 不存数字 cut，两种 marker 的分工是：`{inherited:true}` 标记 fork 谱系，普通 `session/end-seed {}` 只标记种子结束。v2 codec 与 v4 校验都从最后一条 inherited marker 的 seq 推导 cut：

```ts
if (event.type === 'session/end-seed') {
  const data = jsonRecord(event.data, `session/end-seed ${event.seq} data`)
  if (data['inherited'] === true) inheritedEventCount = event.seq
}
```

{{< fig "diagrams/04-resume-fork.svg" "resume 与 fork 的切点、closer 与继承切片的差别" >}}

fork 的三类前置错误值得单独记：boundary 不是非负安全整数报 `INVALID_BOUNDARY`；超出最后一条 seq 报 `fork boundary N does not exist in session "..."`；落在空洞上报 `does not match a contiguous event seq`。子会话 id 已存在报 `SESSION_ALREADY_EXISTS`，源会话不在 live 状态报 `SESSION_NOT_LIVE`。

文档里有一句“fork API 拒绝结束于开放轮次内的前缀”，源码里 `_forkBoundary` 只校验三件事：boundary 是非负安全整数、小于事件数、落在连续 seq 上。开放轮次由 `buildForkSeed` 的 forked closer 收尾，不靠边界校验拦。按实现写，fork 可以切在轮次中间，切完由 closer 把语义补齐。

## 8. 兼容性测试怎么写

迁移代码最难测的部分不是“转换对不对”，是“拒绝得对不对”。四条测试线能覆盖大部分回归：

1. 每代一份 golden，直接用当前构建读，确认不迁移的单代路径没被新校验误伤。
2. 每条边一份 fixture，输入是上一代的真实日志，输出断言事件数、seq 稠密度、cut、以及关键字段。
3. 拒绝用例：缺边、未知必需事件、seq 空洞、header 与 marker 矛盾、stage 改动 cut，每种都该有稳定的错误类型。
4. round-trip：解码再编码，字节级允许不同，语义必须一致。

自己搭的时候，把这四条线写进 CI，尤其是拒绝用例。v3→v4 还多一道手续：那条边需要子会话证据，`createSessionFormatCatalogWithChildren(children)` 把证据绑定到边；不绑定就调 `createStage()` 会直接拒绝，空数组表示显式声明“没有子会话”。测试里最容易被忽略的是这一条：没有子会话的用例要传空数组，不传不等于空。

收集迁移前置证据时用 `historicalSessionFormatCatalog`。它把 `currentVersion` 固定在 3，只用来读 v3 的头部与事件，不会递归打开 current 会话。这个“停在上一代”的 catalog 是给 v3→v4 的 stage 取 child 证据用的，用完即弃。

## 9. 设计取舍

文件名与 header 双写版本，换来 list 与 stat 不读 body。代价是改名会制造矛盾，而矛盾必须报错，不能选一边相信。

相邻边而不是任意跳转，换来每条边可以独立测试、独立拒绝、独立发布。代价是打开一个 v0 会话要串四条 stage，中间任何一段缺失都读不了。

严格回放的收益是“重建出来的会话一定完整”。代价也直接：旧日志里一条坏行就能让整个会话打不开，恢复只能靠 recoverable 路径或人工修。

cut 存在事件里。fork 谱系因此能逐段追溯，普通会话的 header 也不必带一个恒为 0 的字段；读取则要扫到 marker，不能只看第一行。

closer 放在 agent 层而不是 persistence，换来持久化只处理物理格式，语义修复只有一处实现。代价是 resume 路径必须先 append 再 prepare，顺序不能反。

v0→v1 拒绝 ignorable 未知事件，换来那条 alpha 边的输出集合封闭。代价是 2025 年某个插件写的 v0 日志，今天可能完全打不开。

把 closer 的文本写进事件，而不是留给 UI 现拼，换来模型在 resume 后能看到“上次中断在这里、别盲目重试”。代价是这段文本一旦发布就很难改，改了会让同一份日志在不同构建里产生不同消息。

不做降级迁移，换来“旧构建不会假装读懂新日志”。代价是用户升级后再回退，只能看到 `unsupported`，不能继续工作。

## 10. 踩坑点

1. 把 `session.jsonl` 复制成 `session.v4.jsonl` 想跳过迁移。文件名变了，header 还是 0，`generation.ts` 直接报文件名与 header 不一致。
2. 在迁移 stage 里顺手补一条事件，cut 没跟着改。`finish()` 抛 `changed its predeclared inherited cut`，整次迁移失败。
3. 把 `ignorable: true` 当成“旧版本会忽略这个字段”。它只对未知事件类型生效，已知类型里的嵌套 payload 不做兼容。
4. 以为 fork 出来的子会话 `seq` 从切点继续编号。子会话的 seed 仍从 0 开始，cut 只标记哪一段是继承来的。
5. 在 resume 之前自己补 `turn/end`。agent 层会再补一次 closer，日志里出现两个结束事件，后续 step 归属全乱。
6. 用 `validation: 'current'` 打开历史迁移结果。本构建新增的不变量会把当年合法的数据判成非法，跨代读取要用 `transformed`。
7. 迁移时只写新文件却删了旧文件。校验失败后没有源文件可回退，只能从备份恢复。
8. 漏传 v3→v4 的 children 证据。这条边的 `createStage()` 不接受缺省，没有子会话也要显式传 `[]`。

## 11. 自己搭的时候

1. 先定“谁是代际的唯一权威”。文件名做主、header 做校验，两边冲突就报错；不要写“以 header 为准”的兜底，那会让改名变成静默的数据损坏。
2. 迁移边只允许 N→N+1，并且在构造期就检查缺边与多余边。把检查放在启动时，不要留到用户打开会话时才炸。
3. 让每个 stage 在创建时声明它会把 cut 变成多少，finish 时校验。插入或删除事件的迁移，cut 是最容易忘记同步的字段。
4. 回放遇到未知事件时，默认拒绝；只有写事件的人明确声明过“跳过安全”，才允许继续。这个默认值决定了三年后还能不能相信日志。
5. fork 的边界校验只做“连续 seq”这一件事，语义收尾交给 closer。边界校验越权去判断轮次是否开放，会把 fork 能切的位置越收越窄。
6. 给迁移留一个“停在上一代”的 catalog。跨代迁移里，下游 stage 有时需要按上一代的规则读子证据，用当前 catalog 会递归触发迁移。

## 12. 源码索引

- `packages/session/session-format/src/filename.ts:5`：规范文件名的正则，`.v0`、前导零、大写都被排除。
- `packages/session/session-format/src/filename.ts:26`：`parseSessionFormatLogFilename`，不合规返回 `undefined`。
- `packages/session/session-format/src/chain.ts:30`：相邻断言。
- `packages/session/session-format/src/chain.ts:68`：缺边拒绝消息。
- `packages/session/session-format/src/chain.ts:200`：stage 不得改动预先声明的 cut。
- `packages/session/session-format/src/chain.ts:244`：`throwUnsupportedRefusal` 统一包装拒绝。
- `packages/session/session-format/src/catalog.ts:45`：`readHeader` 只读 header，把结果分为 current、migration-required、unsupported、malformed。
- `packages/session/session-format-catalog/src/generated.ts:17`：`currentVersion: 4`。
- `packages/session/session-format-v3-to-v4/src/validation.ts:59`：未知类型且非 ignorable 的拒绝。
- `packages/session/session-format-v3-to-v4/src/validation.ts:64`：seq 必须等于下标。
- `packages/session/session-format-v3-to-v4/src/validation.ts:74`：seeded 的 cut 必须等于最后一条 inherited marker 的 seq。
- `packages/core/session/src/known-event-types.ts:10`：读取拒绝的完整理由。
- `packages/core/session/src/surface.ts:305`：未知 ignorable 事件不进入 surface。
- `packages/core/session/src/index.ts:643`：seed 必须从 0 连续。
- `packages/core/session/src/fork.ts:21`：`buildForkSeed` 写 inherited marker 与 forked closer。
- `packages/core/session/src/repair.ts:209`：`interruptedTurnClosers`。
- `packages/core/agent-loop/src/index.ts:797`：resume 的读取、补 closer、prepare 顺序。
- `packages/session/session-persistence-jsonl/src/index.ts:1536`：按文件名选最高代际。
- `packages/session/session-persistence-jsonl/src/generation.ts:383`：文件名与 header 的版本互校。
- `packages/session/session-format-v1-to-v2/src/codec.ts:114`：从 inherited marker 推导 cut。

## 13. 最后

判断一套会话格式能不能活过几次改版，看四个地方就够了：目录扫描能不能不读 body 就选出正确文件；迁移边是不是只对一对版本负责；回放遇到不认识的事件时敢不敢停下来；resume 与 fork 的切点是不是落在明确的 seq 上，而不是“大概最后一条”。

这四个地方任意一处含糊，短期都不会出事。等到用户拿一份两年前的会话来找你，或者线上要靠 fork 复现一个 bug，含糊的那一处就会变成一次数据修复。DSH 的选择是四处都写死，宁可拒绝打开，也不返回一个看起来正常的错误会话。
