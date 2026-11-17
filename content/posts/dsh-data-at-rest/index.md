---
title: "落盘：句柄、flush 与崩溃后的尾巴"
date: "2026-11-17T20:00:00+08:00"
summary: "append 不等于落盘。DSH 把持久化拆成句柄、写后窗口、flush 屏障三层，并用单写者租约把「谁来修复坏日志」变成确定的问题。"
description: "SessionPersistence 句柄模型：append/flush 语义、写后窗口、撕裂尾部、崩溃修复归属、header 与日志分离。"
tags: ["event-sourcing","architecture","concurrency"]
slug: "dsh-data-at-rest"
---


> 依据：DSH 0.2.2-rc.1（commit fcc0ad46）。`docs/subsystems/persistence.zh.md`（369 行）、`packages/session/session-persistence/src/`、`packages/session/session-persistence-jsonl/src/`、`packages/core/session/src/types.ts` 的 `SessionHeader` 与 `SESSION_FORMAT_VERSION`。未做掉电实验，崩溃语义依据文档与实现推断。

第一季讲会话日志时说过一句：日志是真源，模型历史是投影。当时没讲的是，这条日志怎么从内存走到磁盘，中途崩了会怎样。

这一层容易被当成「写文件而已」，但它决定了三件产品级的事：进程被 kill 后用户会不会丢消息、两个进程同时打开同一个会话时谁赢、坏掉的尾巴由谁来修。

{{< fig "diagrams/01-handle-model.svg" "句柄模型：create/open 返回通道，read 与 write 是同一类型；单写者租约让第二个写打开直接失败。" >}}

## 1. 持久化是一个 seam，不是一组方法

`ctx.sessionPersistence` 暴露四个方法：`create` / `open` / `stat` / `list`。前两个返回 `SessionHandle`，后两个是观察。

关键约束写在类型注释里：每一次日志读写都经由句柄，绝不经由按 id 寻址的服务方法。句柄是跨进程写租约把守的唯一入口。

```ts
interface SessionHandle extends AsyncDisposable {
  readonly id: SessionId
  readonly header: SessionHeader          // 创建/打开时固定
  readonly inheritedEventCount: SessionLogOffset
  readonly access: SessionAccess          // 'read' | 'write'
  read(offset?, length?, options?): Promise<SessionHandleReadResult>
  append(events, options?): Promise<void>
  flush(options?): Promise<void>          // 唯一的持久性屏障
  close(): Promise<void>                  // 幂等、不可取消
}
```

四种错误对应四种边界：

| 错误 | 触发 | 含义 |
| --- | --- | --- |
| `SessionReadOnlyError` | 在读句柄上 append/flush | 不是类型错误，是运行时拒绝 |
| `SessionAlreadyOwnedError` | 已有写持有者时再 `open(id,'write')` | 写租约是排他的 |
| `SessionOwnershipLostError` | 写租约丢失后 append | 所有权是外部的，不是对象状态 |
| `SessionHandleClosedError` | 关闭后的任何操作 | `close()` 是唯一拆除点 |

读句柄与写句柄是同一个类型，靠 `access` 区分。这不是偷懒：句柄的读语义（单调、不返回撕裂尾部）对两者一致，只是写多了一个方法。

## 2. append 不是落盘

类型注释里有一句很重的话：

> Persistence is best-effort: on resolution the batch is accepted, ordered, and visible to reads on this backend instance, but only a resolved `flush` promises it survives a crash.

也就是说 `append` 成功后，数据只保证「本进程内可见、有序」，不保证崩了还在。持久性屏障只有 `flush`。

为什么不让 append 直接 fsync？因为 agent loop 每步都写事件，逐条 fsync 会把延迟打到毫秒级。DSH 用写后窗口批处理：

```text
session/event（同步通知）
   → 路由到该会话的写句柄
   → 有界 write-behind 窗口（第一个事件开启固定截止时间）
   → 窗口到期：一次持久 append
   → 写入期间接纳的事件：新截止时间，形成下一批
```

三个细节决定了行为：

1. 窗口不因新事件重置。第一个待处理事件开启一个固定窗口，后来的只排队，不续期。否则高频写入会永远等不到 flush。
2. `session/flush` 取消等待并排空。loop 在领下一个普通轮次前调用它，作为顺序与错误的观察检查点。
3. 后台写失败要保留事件。失败时按序保留、暂停自动路径、logger 报告；下一次显式 flush 重试并响亮地拒绝。

{{< fig "diagrams/02-append-flush.svg" "append 与 flush 的差别：前者进窗口，后者排空并落盘；崩溃点落在窗口里就丢这一批。" >}}

### 窗口里崩了会怎样

丢的是「已 append 但未 flush」的那一批。这听起来严重，但要看调用点：loop 在轮次边界 flush，所以最坏情况是丢掉当前轮次已产生但未到边界的事件，恢复后由修复逻辑配平成一个 `interrupted` 的轮次。

比起「每条都 fsync 导致每次工具调用多 5ms」，这个代价是可接受的。要更强的一致性，provider 可以把 `append` 实现成同步落盘，seam 允许，产品默认不这么做。

## 3. 撕裂的尾巴

物理写入可能在中途被打断，留下一条不完整的记录。JSONL 后端的处理很具体：丢弃撕裂片段，但保留其中能解码的完整记录，Zstandard 帧被部分解码时，前面完整的帧仍然有效。

返回给调用方的永远是「物理上有效的连续日志」，不会返回半个事件。

然后是一个容易记错的点：持久化不负责修复。

```text
一个在轮次中途崩溃的日志 = 有 turn/start，没有 turn/end
持久化：原样返回，不截断、不补事件
修复：读方的事
```

为什么？因为一个长轮次可能包含大量步骤和工具输出，这些事件在崩溃前已经持久追加，截断会把它们全部丢掉。

## 4. 谁来修：写所有权决定

修复逻辑有两份实现，按访问方式分：

| 访问方式 | 行为 | 是否回写 |
| --- | --- | --- |
| resume（持有写句柄） | 计算 `interruptedTurnClosers`，把缺失的工具错误、未闭合的 `step/end`、合成的 `turn/end{interrupted}` 作为普通批次 append | 是 |
| 只读观察（session-query） | 在内存里用同样的闭合事件配平冷日志 | 否 |

这条分工靠写租约保证不会竞速：活跃会话的写句柄由生命周期所有者持有，并发的 `open(id,'write')` 直接 `SessionAlreadyOwnedError`，而不是让修复与活跃轮次同时写。

合成事件的顺序固定：先补工具结果，再补 `step/end`，最后 `turn/end`。`interrupted` 是唯一一个不由 loop 发出的 `TurnEndReason`，它只可能来自修复。

{{< fig "diagrams/03-crash-tail.svg" "崩溃后的三种尾巴：撕裂片段被丢弃、完整记录保留、打开的轮次由读方配平。" >}}

## 5. header 在日志外面

`SessionHeader` 不进入 `SessionEventMap`，也不会到达 `deriveMessages()`。它和日志并排存放，包含：

| 字段 | 为什么必须持久 |
| --- | --- |
| `version` | 决定用哪个解析器读正文 |
| `cwd` | 恢复时的工作目录，影响工具行为 |
| `parentSession` | 子会话的创建者，用于归属查询 |
| `isSeeded` | 是否含 fork 继承前缀 |
| `origin: 'subagent'` | 展示分类，不代表可续跑 |
| `delegationDepth` | 委派深度；只在内存里会在 resume 后归零，递归预算失效 |
| `agentPreset` | 决定工具与提示词；恢复错组合会让历史无法执行 |

最后两个字段最能说明「什么该进 header」：它们的值会影响恢复后的行为。`delegationDepth` 若只存在内存，重启一个子会话就变回顶层，递归预算形同虚设；`agentPreset` 若不入盘，resume 时可能用另一套工具重放历史。

而 `inheritedEventCount` 是存储元数据，单独放在正文旁边，不进 header、不进日志，它是「fork 切在哪」的物理事实，不是会话内容。

{{< fig "diagrams/04-header-vs-log.svg" "header、存储元数据、事件日志三者分开：只有日志参与重放，header 影响恢复时的行为。" >}}

## 6. 格式版本与代际

| 形态 | 文件名 | 说明 |
| --- | --- | --- |
| v0 | `session.jsonl[.zstd]` | 早期格式，没有版本号 |
| v1+ | `session.vN.jsonl[.zstd]` | 带代际号 |

规则是：已提交的 generation 绝不被重命名、替换或删除。恢复与迁移会发布后继文件，旧文件只覆盖到某个 seq。

迁移是逐级的：每相邻版本只做 `vN → vN+1`。没有「从 v0 直接到 v5」的跳转，那条路径需要维护的转换组合是指数级的。

只读打开用内存结果、不发布后继；写打开先编码校验，再排他发布。这条分工让「看一眼会话」永远不会改变磁盘状态。

## 7. 设计取舍

| 取舍 | 选择 | 代价 |
| --- | --- | --- |
| 每条 fsync vs 窗口批处理 | 窗口 + flush 屏障 | 崩溃丢一批；调用方要知道 flush 才是承诺 |
| 读写的类型 vs 运行时区分 | 同一句柄类型 + `access` | 类型系统不阻止误用，靠运行时拒绝 |
| 崩溃修复归属 | 读方（resume）修，持久化不动 | 只读观察者必须自己配平 |
| 撕裂尾部的处理 | 保留可解码的完整记录 | 解析器要能处理部分帧 |
| header 内容 | 只放影响恢复行为的字段 | 新增字段要论证「不持久会坏什么」 |
| 格式迁移 | 逐级转换 | 老版本要保留多级转换器 |

## 8. 踩坑点

1. 把 append 成功当成落盘。崩溃时丢的正是这一批。
2. 在轮次中途依赖 flush 的时机。窗口不因新事件续期，flush 只在边界被调用。
3. 以为持久化会截断坏尾巴。它只丢撕裂片段，打开的轮次原样返回。
4. 只读打开后手动写回。只读句柄 append 会抛 `SessionReadOnlyError`。
5. 两个进程同时写。第二个 `open(id,'write')` 直接失败，不会排队。
6. 把 `inheritedEventCount` 当 header。它是存储元数据，不参与重放。
7. 新增 header 字段却不论证恢复行为。不影响恢复的字段不该进 header。
8. 以为迁移可以跳版本。只支持逐级。

## 9. 自己搭的时候

- 把「已接受」与「已持久」分开说。两个词混用，用户会以为 append 成功就安全。
- 让屏障只有一个。`flush` 是唯一承诺点，其他方法都不承诺崩溃安全。
- 用租约而不是锁。写所有权是外部状态，进程死了自然释放；锁需要清理。
- 修复放在读方。写方只保证「原样返回」，修复策略可以随产品演进。
- header 只放影响恢复的字段。这条约束能防止元数据无限膨胀。

## 10. 源码索引

| 位置 | 内容 |
| --- | --- |
| `docs/subsystems/persistence.zh.md:1-60` | seam 定义与句柄契约 |
| `docs/subsystems/persistence.zh.md:60-130` | `SessionHandle` 方法语义 |
| `docs/subsystems/persistence.zh.md:130-175` | flush 检查点与写后窗口 |
| `docs/subsystems/persistence.zh.md:175-215` | 崩溃恢复与修复归属 |
| `packages/session/session-persistence/src/index.ts` | `SessionPersistence` 服务 |
| `packages/session/session-persistence-jsonl/src/` | JSONL 后端与撕裂尾部处理 |
| `packages/core/session/src/types.ts` | `SessionHeader`、`SESSION_FORMAT_VERSION` |
| `packages/core/session/src/repair.ts` | `interruptedTurnClosers` |
| `packages/core/session/src/index.ts:806` | `append()` 编码与提交 |
| `packages/session/session-query/src/` | 只读观察与冷读缓存 |

## 11. 最后

持久化这一层的全部设计可以压成两句话：

append 是排序，flush 才是承诺。 崩溃留下的坏尾巴不由写方修，由读方在拿到写所有权时修。

这两句决定了 API 的形状，也决定了出问题时该看哪里，如果用户说「最后一条消息没了」，先看它有没有过 flush；如果日志打不开，先看尾巴是不是撕裂的。

系列导航：[第五季导览](/posts/dsh-season-five.html) · [诊断现场](/posts/dsh-diagnostics.html) · [会话即日志](/posts/dsh-session-event-sourcing.html)
