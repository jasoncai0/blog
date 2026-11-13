---
title: "回退：删掉一段历史，还是换一个投影"
date: "2026-11-13T16:40:00+08:00"
summary: "回退插件把两件事绑在同一个坐标上：对话侧追加一条带 surfaceOp 的空消息遮蔽尾部，文件侧按同一条用户消息的 seq 分组备份，还原时取每个路径最早的一条。含两道时序图、还原计划、回退深度与崩溃恢复的源码级拆解。"
description: "dsh-rewind-plugin 的实现原理：surfaceOp 怎么改写模型可见历史、客户端凭什么知道该隐藏哪些节点、文件检查点为什么不挂 pre-execute、以及还原为什么必须先写意图日志。"
tags: ["harness", "architecture", "event-sourcing"]
slug: "dsh-rewind"
---

> 源码版本：`dsh-rewind-plugin` `0.21.2`（`~/.dsh/profiles/desktop/node_modules/dsh-rewind-plugin`）。行号以该版本的 `lib/index.js` 为准；宿主侧的校验在 DeepSeek Harness `0.2.2-rc.1` 的 `packages/core/session/src/surface.ts`。
> 系列之外的一篇插件拆解。背景：[会话日志与事件溯源](/posts/dsh-session-event-sourcing.html)、[压缩](/posts/dsh-compaction.html)、[系列目录](/posts/dsh-architecture-anatomy.html)。

一个会话跑了三个小时。模型先把 `src/auth.ts` 换成 JWT 校验，又把 `src/legacy.ts` 删了，测试跑了一半。你想回到「动手之前」那条消息，换个说法重来。

要退的是两样东西：模型看到的对话，和磁盘上的两个文件。前者决定它下一步推理的起点，后者决定它下一步读到的内容。只退一样，接下来必定错位。

回退插件把这两件事绑在同一个数字上。对话侧追加一条空消息，把「目标消息到末尾」这一段从 surface 里换掉；文件侧把锚点大于等于目标的备份还原回去，把之后新建的删掉。

## 0. 结论

一句话：**回退不改历史，只换投影；文件侧不做快照，只做差分。**

四条更具体的：

1. **对话侧是补偿，不是删除。** 日志只追加，回退靠一条 `user/message` 事件完成：它的正文是 `(empty message)`，携带一个 `surfaceOp: replace` 和一份被遮蔽的 seq 清单。模型看到新投影，日志里旧事件一条没少。
2. **文件侧只备份改过的文件，备份的是整份副本。** 每次 `write`/`edit` 之前把原件抓下来，按「属于哪条用户消息」分组；同一份内容重复出现只存一次（`ref` 链接）。还原时每个路径取最早的一条，再和现在的磁盘比一次，只动不一致的。
3. **两侧共用一个坐标。** 那个坐标是用户消息在日志里的 `seq`。它不是为回退新造的主键，是日志本来就有的事实，所以对话和文件天然对齐。
4. **两侧都有失败路径，也都为失败设计了顺序。** 对话侧先停跑再写标记；文件侧先写意图日志再动磁盘。顺序错了，崩溃后就说不清做到哪一步。
5. **回退的粒度是用户消息，不是单次编辑。** 一轮里同一个文件改了五次，退回去是退到这五次之前。深度只受备份窗口限制，不受改动次数限制——3.5 节把这条摊开算。

| | 对话 | 文件 |
| --- | --- | --- |
| 数据形态 | 只追加的事件日志 | 每会话一棵按锚点分组的目录 |
| 回退动作 | 追加一条带 `surfaceOp` 的标记 | 逐文件 restore / delete |
| 生效方式 | 下一次请求现场投影 | 直接改磁盘，再广播观察事件 |
| 可撤销 | 是（再点一次更早的） | 是（只要备份还在） |
| 失败恢复 | 标记没写进去就什么都没发生 | journal 记到哪一步，启动时收尾 |

## 1. 一个坐标：anchorSeq

先定义这个数字。

会话日志里每个事件有一个单调递增的 `seq`。用户敲一条消息，日志里多一条 `user/message`，它带一个 `seq`。回退插件把这个 `seq` 叫 **anchor seq**：这一轮里所有文件改动，都记在这个锚点下面。

```ts
// anchorSeqOf：从尾部往前找最近一条 user/message
function anchorSeqOf(session, cache) {
  const events = session.snapshotEvents()
  if (cache.anchor !== undefined && cache.eventsLength === events.length) return cache.anchor
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === "user/message") {
      cache.anchor = events[i].seq
      cache.eventsLength = events.length
      return cache.anchor
    }
  }
  return undefined
}
```

（结构示意。）注意它找的是「最近一条 `user/message`」，不是「最近的 `turn/start`」。两者经常同一个时刻，但不总是：重试、自动续跑都会开新回合，而那一轮并没有人类输入。

更关键的是**谁有资格当边界**。日志里的 `user/message` 不止一种来源：

| 来源 | `source.kind` | 能当回退边界吗 | 为什么 |
| --- | --- | --- | --- |
| 人敲的 | `user` | 能 | 一次点击对应一个完整意图 |
| 插件注入的上下文 | 插件自己的 kind | 不能 | 不是用户说的，回退它没有语义 |
| 压缩 checkpoint | `compact-checkpoint` | 不能 | 它是派生结果，退到它等于退到半截 |
| 工具结果回填 | `tool` 一类 | 不能 | 它属于上一轮，不是新意图 |

所以判定写得很窄：

```ts
function isHumanUserMessageEvent(event) {
  return event.type === "user/message" && event.source?.kind === "user"
}
```

那条标记自己的 `source.kind` 是 `"dsh-rewind"`，天然被排除在外。这不是巧合：上面那条空消息如果被当成边界，连点两次回退就会退到第一次的标记上，区间立刻塌成零长度。

图 1 是两个存储的全貌。左边纵向是日志，右边是文件目录；两边用同一个 `41` 对齐。

{{< fig "diagrams/01-two-stores.svg" "图注：左边只追加，seq 41 到 63 被一条新事件遮蔽但仍在文件里；模型看到的 surface 是 1–40 加 64。右边按锚点分目录，38 那一组不在还原范围内，41 和 44 要在回退到 41 时处理。" >}}

图注之外补一句：这个「遮蔽区间」是**闭区间**，`[41, 63]`，还原范围是 `anchorSeq >= 41`。回退到某条消息，连它自己那一轮造成的变化也一起退。想保留自己那轮，退到再往前一条。

## 2. 对话：一次 append 换掉一段 surface

### 2.1 为什么不能删

会话日志是只追加的。删事件会同时破坏三样东西：工具调用与结果的成对性（删一半，下一次请求直接被拒）、审计（说不清模型当时看到了什么）、以及并发读者（另一个视图可能正在按 seq 索引）。

DSH 的解法是给日志加一个「投影」概念：日志是全集，`surface` 是模型这次实际看到的那部分。压缩就是这么做的（把一段换成 checkpoint），回退只是把同一种能力用在另一个方向上。

区别在于：压缩换进去的是一段摘要，回退换进去的是一条空消息。**换进去的内容不重要，重要的是「哪些 seq 被换掉了」这个事实被记下来了。**

### 2.2 规划：只能在 surface 上回退

`planRewind` 做三件事：找到目标、确认它还在 surface 上、算出遮蔽区间。

```ts
// lib/index.js:454（结构示意）
const targetIndex = surface.indexOf(targetSeq)
if (targetIndex === -1) {
  throw new RewindError("not-on-surface",
    "the target message is no longer in the model context (shadowed by compaction)")
}
const shadowedSeqs = surface.slice(targetIndex)
return {
  targetSeq,
  surfaceStart: shadowedSeqs[0],
  surfaceEnd: shadowedSeqs[shadowedSeqs.length - 1],
  shadowedSeqs
}
```

`surface.indexOf` 这一步是整个设计的闸门。如果目标已经被上一次压缩遮蔽，你在界面上还能看到它（客户端按日志渲染），但它不在 surface 上，回退它没有意义——模型本来就看不见。这时候报 `not-on-surface`，而不是「假装成功」。

范围是 `[目标, surface 末尾]`，前缀保留。这一点容易想错：回退不是「回到某个时间点」，是「让模型只看见目标之前的对话」。中间被压缩过的段落也在遮蔽范围里，因为它们本来就不在 surface 上。

### 2.3 先停下来，再写

标记一旦写进去，模型下一次请求看到的就是新投影。如果此刻 turn 还在跑，它会用一份刚被替换掉的上下文继续推理，工具调用和结果可能对不上。

所以执行顺序是硬的：

1. `inflight` 集合按 session 去重，同一条会话不允许两次回退并发。
2. `agent.status !== "idle"` 时 `agent.cancel({ kind: "user" }, { keepInbox: true })`，然后 `waitForAgentIdle` 等它真的停下（默认 15 秒，超时报 `stopFailed`）。
3. `dropPendingSteering`：遍历 `agent.inbox.nextStep`，把还没被读走的插话逐条 `remove`。
4. 最后才 `append`。

`keepInbox: true` 值得单说。取消时默认会把收件箱一起清掉，但这里要保留：队列里可能有用户刚敲的「等一下，先别删那个文件」。保留之后由第 3 步显式清空——因为那些话是针对**旧上下文**说的，回退之后它们会指错对象。

图 2 是这条路径的完整时序。

{{< fig "diagrams/02-chat-sequence.svg" "图注：11 步里只有第 07 步在写。前面是置位、规划、取消、等 idle、清插话；后面是宿主把新事件的 seq 通过命令结果回传给客户端，客户端据此算遮蔽区间、隐藏节点、回填输入框。" >}}

### 2.4 一次 append，两个副作用

```ts
const event = agent.session.append("user/message", marker, {
  surfaceOp: {
    op: "replace",
    startSeq: plan.surfaceStart,
    endSeq: plan.surfaceEnd
  },
  sourceEventSeqs: [...plan.shadowedSeqs]
})
```

两个字段各管一件事：

- `surfaceOp` 决定**模型之后看到什么**。宿主在投影时执行它：把 `[startSeq, endSeq]` 这段从 surface 里摘掉，换成这条新事件。
- `sourceEventSeqs` 决定**审计时怎么解释**。它记录「这条事件由哪些更早的事件派生而来」，也就是被它遮蔽的那些。投影本身只按 `surfaceOp` 走。

宿主侧对这两个字段有一组校验，写在 `packages/core/session/src/surface.ts` 里。几条值得抄下来当规格：

| 校验 | 失败文案 |
| --- | --- |
| 能进 surface 的类型必须带 `surfaceOp` | `session event "user/message" is surface-eligible and requires a surfaceOp marker` |
| 不能进的不许带（`turn/start` 这类） | `session event "turn/start" is not surface-eligible and cannot carry surfaceOp` |
| `assistant/message` 不许带 `sourceEventSeqs`（它自己嵌了 provider 流） | `assistant/message embeds its source stream and cannot carry sourceEventSeqs` |
| 清单非空、不重复、全部指向更早的事件 | `sourceEventSeqs must reference earlier events: 64 >= current seq 64` |
| `startSeq` / `endSeq` 必须真的在 surface 上 | `surface replace: start seq 41 not found in surface` |
| **清单必须覆盖每一个被遮蔽的节点** | `surface replace: sourceEventSeqs must include every shadowed surface node; missing 57` |

最后一条是这两个字段之间唯一的强约束：范围是 `[41, 63]`，清单就必须正好是 41 到 63 里仍在 surface 上的每一个 seq，少一个都不行。少写一个，事件被遮蔽了却没人记账；多写一个，记账说遮蔽了，实际还看得见。两种都会让「模型到底看见了什么」这个问题失效。

「只引用更早的事件」这条是防环：一条事件如果能遮蔽未来的事件，投影就不是一次线性扫描了。

`marker` 本身长这样：

```ts
const REWIND_MARKER_CONTENT = [{ type: "text", text: "(empty message)" }]
const REWIND_MARKER_SOURCE = Object.freeze({ kind: "dsh-rewind" })
```

内容是固定的。它确实会进 surface：`replace` 之后它占住 41 到 63 原来的位置，模型看到的就是一条正文为 `(empty message)` 的用户消息，也就是「从这里重新开始」。界面上它被标成 hidden——它是一条记账，不是用户真说了这句话。

### 2.5 客户端凭什么知道该藏谁

这是整套机制里最容易做错的一环。宿主只追加了一条事件，它没有通知任何人「请把 41 到 63 藏起来」。客户端得自己算。

线索在命令的结果里。宿主执行完返回：

```ts
return { kind: "success", text, sourceEventSeq: event.seq }
```

`sourceEventSeq` 就是那条标记的 seq（上例是 64）。客户端拿到命令节点之后：

```ts
// client.js:155（结构示意）
function rewindCutsOf(nodes) {
  const cuts = []
  for (const node of nodes) {
    if (node.kind !== "command" || isProbeCommand(node)) continue
    if (node.outcome?.kind !== "success") continue
    const marker = node.outcome.sourceEventSeq
    const target = targetSeqOfArgs(node.data.args)   // 从 "@41" 抠出 41
    if (target !== undefined && target <= marker) cuts.push({ target, marker })
  }
  return cuts
}
```

有了 `{target: 41, marker: 64}`，遮蔽规则只有一条：

```ts
if (cut.target <= node.anchorSeq && node.anchorSeq <= cut.marker) hidden(node)
```

注意判定用的是 `anchorSeq`——每个节点自己带的「我属于哪条用户消息」。这样整轮的节点（用户消息、assistant 回复、tool_call、tool_result）会被一起藏掉，不需要维护「一个区间里有哪些节点」的映射。

图 3 是一张节点表被投影前后的样子。

{{< fig "diagrams/03-client-projection.svg" "图注：左边是节点表，右边是四条判定规则。hidden 不是删除，只是给节点加一个 visibility 字段；节点仍在 store 里，所以输入框还能读到原文。第 65 条是回退之后重发的新消息，它落在区间外，照常显示。" >}}

三个细节：

**探针命令不算。** `/rewind-plugin ... preview` 和列候选这两种调用也会产生命令节点，但它们没有真正回退，不产生切点。

**刷新重放要得同一个结果。** 这些切点是从日志里的命令节点算出来的，不是从内存里的「刚才点过」算出来的。所以刷新页面、换设备、重放历史，看到的隐藏区间完全一致。

**它是私有 seam。** 客户端没有「隐藏节点」的公开 API。实现方式是用 `ctx.effect` 包住 `uiConversation` 已注册的 chat 视图 builder，在它返回的节点流上包一层。注册表的形状变了就得跟着改，所以代码里对返回结构做了一次形状校验，不匹配就原样放行。

### 2.6 回填：只认新事件

回退之后，原文要回到输入框，让用户改一改再发。这一步有两个陷阱。

第一个是**别重复回填**。加载历史时如果扫到旧的 `rewind` 命令就回填，那么每次切会话、每次重启，输入框都会被灌一遍旧文本。

所以逻辑是事件驱动的：先记下当前已知的命令节点（`knownCommandSeqs`），发命令，然后等一个新出现的、`isExecutedRewindCommand` 为真的节点，最多等 20 秒。

```ts
const known = knownCommandSeqs(session)
const result = await session.command(`/rewind-plugin @${seq} ${mode}`)
if (!result.ok || result.value?.matched !== true) return
await waitForCommand(session, chatOf, node => isExecutedRewindCommand(node, seq) && !known.has(node.seq), 20_000)
```

第二个是**别覆盖草稿**。输入框里已经有用户写了一半的东西，回填会把它们冲掉。所以先读一次，非空就直接放弃回填（但回退本身已经生效了）。

图片要单独处理：`messageAttachmentsAt(seq)` 读回 image 块，再逐张还原成可发送的附件。文本和图片来自同一份快照，不会一个有一个没有。

### 2.7 未读插话是另一条路

用户可以在模型跑的时候插话（steering）。这些消息已经进了收件箱，但还没被任何一次请求读走。

回退它们**不能**走上面那条路。原因很实际：它们还没变成日志事件，没有 `seq`，`planRewind` 根本看不见它们；而且为了撤几条话去打断一个正在跑的 turn，代价太大——turn 停了，在飞的工具调用也白做了。

所以客户端另有一条 `retractPending`：走会话自己的 `updateQueue` 通道，把目标及之后所有未读的插话撤回，再把文本填回输入框。它不打断运行，只是让这些消息在下一个 step 边界之前消失。

失败码里有一个值得记住的：`queue-item-not-found`。它的意思是「你点的时候，step 边界刚把这批消息取走了」——此时消息已经被模型读过，属于历史，应该改由已读行的回退接手。这种情况下不回填文本，否则用户会发两遍。

### 2.8 压缩过的消息，退得回去吗

不能。

压缩和回退用的是同一个机制，差别只在入口：压缩把 `[start, end]` 换成一条 checkpoint，`source.kind` 是 `compact-checkpoint`，那一段用户消息从此不在 surface 上。`planRewind` 第一步就是 `surface.indexOf`，找不到就抛 `not-on-surface`，命令返回失败，文件侧也不会执行。

界面上这些行还在。压缩卡不替换被遮蔽的行（`ui-chat` 的 `CompactionItem.tsx` 第一行注释就写着这一点），所以旧的用户消息仍然看得见，↶ 按钮也还在。点下去会得到一条失败提示，而不是静默无事发生。这个取舍是明确的：能当回退目标的点，必须真的在模型当前的上下文里，否则「退回去」只是把一段模型从没见过的对话塞回输入框。

想回到压缩之前，只有宿主层的路：从日志的任意 `seq` fork 一个新会话（事件还在日志里，只是不在 surface 上），或者直接拿原始 JSONL 重放。插件不做这两件事，README 里也写了不做 fork / 分支回退。

压缩保留下来的尾部不受影响：那些消息仍在 surface 上，可以照常回退。

## 3. 文件：不是快照，是差分

### 3.1 为什么不做整树快照

三个理由，从强到弱：

**时机。** 快照要在写之前拍，而「写之前」只有工具执行的瞬间知道。等一轮结束再拍，你拍到的是改完之后。

**代价。** 一个带 `node_modules` 的仓库，整树快照一次就是几百 MB、十几万个文件。用户每点一次回退不该付这个成本。

**语义。** 整树快照回答不了「这个文件是谁改的」。而回退需要知道「哪一轮改的」，才能算出该退到哪个版本。

所以插件只做一件事：**在写之前，把被写的那个文件抓一份。**

先说清楚标题里的「差分」指什么：指只备份被改过的文件，不备份整棵树。它不指补丁格式——存下去的是整份字节，3.6 会展开。

### 3.2 五个时刻

{{< fig "diagrams/04-checkpoint-lifecycle.svg" "图注：捕获在写之前，提交在写之后，重扫在每条用户消息的边界。三段时间错开，各自解决一个问题：捕获解决「原件」，提交解决「归属」，重扫解决「外部改动」。" >}}

**捕获：`tools/execute`。** 插件挂的是 around-dispatch 阶段，不是 `tools/pre-execute`。这个选择在代码注释里写得很清楚，三条理由：

1. 别的插件可能在这个工具上挂 `ask`，用户拒绝就短路，后面的 pre-execute 监听器根本不会跑。
2. 被拒绝的调用不会 dispatch，为它做备份是白做。
3. 只有到了 `post-execute` 才拿得到 `result.isError`，那时才知道这次写到底成没成。

而 around-dispatch 阶段的 `arguments` 已经是最终值（`ask` 之前的改写都发生过了），拿到的 `file_path` 就是要写的路径。

捕获本身很克制：

```ts
// 结构示意
const target = await fs.resolve(path, { cwd, signal })
if (fs.processPathFromHostPath(target.displayPath) === undefined) return  // 不是宿主路径
const info = await stat(target.displayPath)
if (!info.isFile()) return                                              // 目录、设备
if (store.isOverFileCap(info.size)) return                               // 超过单文件上限
// 文件还不存在：记一条 backup = null，还原时表示「应该删掉」
```

`backup = null` 这一支是「创建」的记录。没有它，回退时你只知道要还原被改过的文件，不知道该删掉新建的。

**提交：`tools/post-execute`。** 拿到 `result.isError` 之后才落盘。失败了就把暂存删掉——一次没成功的写入不该在历史里留下痕迹。

提交时算归属：`anchorSeqOf(session)` 给出这条改动属于哪条用户消息，然后写进 `<anchorSeq>/` 目录。

**重扫：`session/event` 的 `user/message`。** 这是唯一能覆盖「不是通过 write/edit 改的」那部分文件的时刻。用户在下一条消息之前跑了一次 `prettier`，或者模型用 `bash` 里的 `sed -i` 改了文件，插件都不知道——直到下一条用户消息到来，它把已跟踪的文件逐个和上次记录的内容比一遍。

```ts
// 结构示意
for (const path of tracked) {
  if (await probe.isUnsafeTarget(path)) continue
  if (store.isOverFileCap(await probe.size?.(path))) continue
  if (await probe.matches(store.lastKnownContent(path), path)) continue  // 没变
  // 变了：补一条 recheck 记录
  await store.recordBackup(sessionId, { callId: `recheck-${anchorSeq}-${hashPath(path)}`, anchorSeq, path }, copied, { dedup: false })
}
```

补记必须 `dedup: false`。去重是按内容哈希连到已有条目上的，如果外部改动恰好改回了某个历史版本，去重会把它连到那条旧记录，锚点就错了——还原时会以为这个内容属于更早的一轮。

补记记的是**边界那一刻的现状**，不是改动之前的字节。所以它的作用是让还原不拿更旧的版本去覆盖现状（写了等于没写），而不是把 `bash` 的改动退回去——插件从没见过那份字节。要这类改动能退，只能让改动走 `write`/`edit`。

**还原：`rewind` 命令。** 见下一节。

### 3.3 落盘长这样

```
.dsh/rewind-snapshots/session-3f7c.../
  format.json                     {"version": 2}
  41/                             锚点 = 第 41 条用户消息
    call-9a.json                  {"path":"src/a.ts","before":{"file":"call-9a.before"},"size":1204,"mode":420}
    call-9a.before                原始字节
    call-9b.json
    call-9b.before
  52/
    recheck-52-1f.json            边界重扫补记
    recheck-52-1f.before
  rescue/<opId>/                  还原前的抢救副本
  journal-<opId>.json             还原意图日志
```

几个决定：

**原件用 sidecar，不塞进 JSON。** 二进制文件过一遍字符串编解码就可能变，而还原要求字节级一致。所以内容单独一个 `.before` 文件，用 `copyFile` 直接拷字节。

**同一轮的多次写共用一个目录。** 排序按 `time`（同毫秒再按自增计数）。这样「这一轮改了哪几个文件」在磁盘上就是一次 `readdir`，不用扫全表。

**内容相同的存 ref。** 第二次写回同样的内容，条目不重复存字节，只存一个指向已有条目的 `ref`。编辑器的自动保存、反复格式化都会命中这条。

**目录名必须是数字。** `readdir` 结果里非数字的一律跳过，这样 `.pending`、`rescue`、`journal-*` 这些兄弟目录不会被误当成锚点。

### 3.4 一百组之后

每个会话只保留最新的 100 个锚点组。超过就按时间剪掉最老的。

上限选 100 不是随手定的：回退候选列表的默认上限也是 100（`DEFAULT_CANDIDATE_LIMIT`）。两者对齐的意思是——**界面上能选到的，一定有备份**。如果候选列表比备份长，用户选了一条却没有文件可退，那是更糟的体验。

自动清理默认关闭。开了之后按「失活天数」清理，默认 30 天，且不会碰当前活跃会话的目录。

剪掉之后的表现值得说清楚：界面上每条用户消息仍然有 ↶，点了对话侧照常回退，文件侧却已经没有条目可退，预览会显示 0 个文件。「每个节点都能点」是 UI 的宽容，不是备份的承诺。

### 3.5 能退多深，粒度在哪

把 `src/a.ts` 的几次改动摊开，每条用户消息各改一次，内容依次是 A、B、C、D：

```
A ──[41]──> B ──[44]──> C ──[52]──> D
```

当前磁盘上是 D。退到不同的点，写回的字节不同：

| 退到 | 写回 | 撤销了哪几轮 |
| --- | --- | --- |
| 41 | A | 41、44、52 |
| 44 | B | 44、52 |
| 52 | C | 52 |

规则一句话：**退到 X ＝ 保留 X 之前的所有改动，撤销 X 及其之后的所有改动。** 实现就是 4.1 那条「每个路径取 anchor 最小的一条」——退到 44 时 `anchor >= 44` 的条目只有 44（before = B）和 52（before = C），取 44 的 B，正好是 44 动手之前的样子。改过十次也一样，只认最早那份。

深度的上限不在次数，在窗口：

| 想退到 | 做得到吗 |
| --- | --- |
| 41，或 41 之前的任意一条 | 能，条目还在窗口里就行 |
| 41 之后的第十次编辑之前 | 能，锚点仍是消息 |
| 41 这一轮里第三次动手之前的中间状态 | 不能，一轮内所有条目同锚点，只能整体退到这一轮之前 |
| 41，但 41 的条目已被 100 组上限剪掉 | 对话能退，文件侧没有条目，预览显示 0 个 |

粒度是消息，不是编辑。一轮里同一个文件改了五次，退回去是退到这五次之前，中间那四次的状态取不到——捕获只在工具执行时发生，而锚点只认用户消息。

反过来也成立：某个文件在目标之后一次没被碰过，它就没有任何 `anchor >= 目标` 的条目，还原连写都不写它，`mtime` 都不动。

### 3.6 存的是副本，不是补丁

直觉上会把「按锚点还原」想成一条 diff 链，像 undo log 那样由新往旧逐条回滚。这里不是：每个条目存的是**整份文件字节**，还原时直接覆盖。

链是读的时候现算的。第 52 次改动的 before 恰好是第 44 次改完的结果，所以序列看起来连成一条；存储里没有 next / prev 指针，关系由捕获时刻决定，不是存下来的。好处是每条条目独立可退——中间某条被剪掉，其它条目照样能还原到各自那一刻。

和 undo log 比，差别在这几处：

| | MySQL undo log | 这里 |
| --- | --- | --- |
| 存什么 | 逆操作（改前的行镜像、索引项） | 整份文件字节 |
| 回到某个版本 | 从新往旧逐条 apply | 排序取一条，直接覆盖 |
| 跳过中间版本 | 不行，必须按序回滚 | 可以，任意锚点独立 |
| 生命周期 | 提交后由 purge 回收 | 保留到被剪掉或自动清理 |
| 去重 | 没有 | 同一份内容存 `ref` 链接 |

材料上更接近 git 的对象库：全量 blob、按内容去重，不是补丁包。区别在寻址方式——git 用内容哈希，这里用 `(anchorSeq, time)` 排序加字节比较。

成本模型也跟着变：占用约等于「这份文件出现过多少种不同内容」乘以文件大小，而不是「编辑次数」乘以差分量。40 KB 的文件改 10 次、每次内容都不同，约 400 KB；只在 A、B 两个版本之间反复切换，去重后只存两份。这也是为什么必须设单文件上限和 100 组窗口——undo log 靠事务边界回收，这里得靠窗口。

## 4. 还原：一个纯函数加一本日志

### 4.1 计划：取最早的一条，再和磁盘比一次

还原的第一步不是动手，是算。

对每个路径，取「锚点 ≥ 目标」里**最早**的那条记录。最早的那条就是「回到那一刻时，这个文件应该是什么样」。

{{< fig "diagrams/05-restore-plan.svg" "图注：左边是条目，同一个文件在 41 和 44 各有一份，取 41 的 A；中间是磁盘现状，比较是字节级的流式比较；右边是动作，四种结局：还原、删除、不动、跳过。" >}}

举例：`src/a.ts` 在 41 存了 A，在 44 存了 B，现在磁盘上是 B 且和 A 不同——还原成 A。`src/b.ts` 在 41 的记录是 `null`（当时不存在），现在存在——删掉。`src/c.ts` 在 41 存了 C，磁盘上还是 C——什么都不做，连 mtime 都不碰。

最后一条常被忽略。如果「还原」写成「无脑覆写」，每次回退都会把没变的文件也重写一遍，mtime 全变，`make` 重跑，IDE 重新索引。所以比较必须在写之前。

比较是流式的：`sameFileBytes` 逐块读，不把整个文件读进内存。大文件也不会把内存打爆。

### 4.2 四类东西一律不碰

| 目标 | 为什么不动 |
| --- | --- |
| 符号链接 | 写回去会把内容送到链接指向的地方，可能完全在项目外 |
| 硬链接（`nlink > 1`） | 改一个名字会连带改掉同一 inode 的所有名字；pnpm 的 `node_modules` 里大量是硬链接 |
| 目录 / FIFO / 设备 | 一个会递归，一个会阻塞，一个会无限流 |
| 父目录被重新指向 | 捕获时记过父目录的位置，还原时解析到别处就拒绝，防止把写/删引到另一个目录 |

第四类最容易被忽略：`parent` 记的是捕获时 `dirname(path)` 解析到的位置。用户中途把 `src/` 换成指向 `vendor/src/` 的链接，还原 `src/a.ts` 就会写到 `vendor/src/a.ts`。所以执行前再解析一次，对不上就跳过。

这四类都进 `skipped`，不中止整轮。

### 4.3 先写意图，再动手

真正改磁盘之前，有两样东西先落盘：

1. **抢救副本**：把每个将被改动的文件当前的内容复制到 `rescue/<opId>/`。还原本身可能改错（比如用户手工改过又想要回来），有抢救副本就能再救一次。
2. **意图日志**：`journal-<opId>.json`，记下这次要做的每一个动作，原子写（写临时文件再 rename）。

然后才逐文件执行。每完成一个，更新日志里的状态。崩溃在任何一点，下次启动读到未完成的日志，就知道该继续还是回滚。

{{< fig "diagrams/06-restore-sequence.svg" "图注：01–05 是只读的，预览接口和真正还原共用同一个 planRestore；用户确认之后才发命令。08 在 09 之前：对话侧先完成，文件侧失败也不会让标记消失——回退一半也有意义。" >}}

三个执行期约定：

**单文件失败不中止。** 一个文件 `EACCES`，剩下的照做，最后在回执里列出来。中止只会让状态更难解释。

**执行时发现路径被重指，记 `failed`，跳过。** 计划阶段检查过一次，执行阶段再检查一次——两者之间用户可能动了手脚。

**文件已经不在了，记 `done`。** 计划要还原而已删除：直接写回。计划要删除而已经不存在：目标已达成，记 `done` 而不是失败。

日志写失败不致命：`saveJournal` 的异常被吞掉，降级成「没有日志的还原」，不会因为写不了日志而拒绝还原。

### 4.4 预览和执行是同一个函数

`POST /api/dsh-rewind/preview` 和真正的还原调的是同一个 `planRestore`。差别只在后者会执行，前者只返回动作列表。

这是刻意的：如果预览用一套逻辑、执行用另一套，两者迟早会不一致——用户看到「将还原 3 个文件」，点下去实际动了 5 个。共用一份计划，预览就永远是真的。

预览还有一个用途：告诉用户哪些文件**退不回去**。超过单文件上限没备份的、被四类规则跳过的，都会出现在 `uncovered` 里，浮层上明说。这比点完之后发现少了几个文件要好。

## 5. 边界与代价

| 限制 | 表现 | 原因 |
| --- | --- | --- |
| 不追踪子代理 | 子会话里没有回退按钮 | 子代理的写属于它自己的会话，主会话没有可绑定的锚点；宿主也拒绝子会话的通用 RPC |
| 不做整树快照 | 没被 write/edit 碰过的文件退不回去 | 见 3.1，代价与时机都不成立 |
| 回退已读消息会打断回合 | 运行中的 turn 被取消 | 不取消就会用旧上下文继续推理 |
| 只认 write/edit | `bash` 里 `>`、`sed -i`、格式化器改的文件，要等下一条用户消息边界才补记 | 工具层只有这两个有明确路径参数 |
| 磁盘会增长 | 每个改动文件一份原件 | 每条会话上限 100 组，自动清理默认关 |
| 还原是 O(entries) | 改动很多时点开浮层有延迟 | 要读全部条目并和磁盘比较 |

最后一条值得量化：一次回退要 `readdir` 所有锚点目录、读所有 `*.json`、对每个路径 stat 并比较。100 组、每组几个文件，是几百次文件系统调用，几十毫秒量级；一次改了几百个文件的重构，会到秒级。

## 6. 如果自己做一个

三条经验，按重要性排：

1. **复用一个已有的坐标，别新造 id。** 回退能用同一个 `seq` 对齐对话和文件，是因为那个 `seq` 本来就是日志的事实。如果为文件备份另造一个 `roundId`，两者迟早对不上——尤其是压缩、fork、崩溃恢复之后。
2. **写操作先落意图。** 还原先写 journal 和抢救副本，再动磁盘；对话先停跑，再写标记。顺序错了，崩溃之后就只剩「大概做到一半」。
3. **投影层要能表达「不显示」，而不是「删除」。** 删除是不可逆的，隐藏是可逆的。整条链路里唯一不可逆的是磁盘写入，所以它被单独包了一层日志。

## 源码索引

| 位置 | 看什么 |
| --- | --- |
| `lib/index.js:454` | `planRewind`：切点、遮蔽区间、`not-on-surface` |
| `lib/index.js:2890` | `executeRewind`：停跑、清插话、append、按 mode 调还原 |
| `lib/index.js:2698` | `captureBefore`：路径解析、大小上限、`backup = null` 表示创建 |
| `lib/index.js:2737` | `commitEntry`：锚点归属、`isError` 丢弃 |
| `lib/index.js:2449` | `reconcileTracked`：边界重扫，补记外部改动 |
| `lib/index.js:1347` | `planRestore`：最早条目、四类跳过、字节比较 |
| `lib/index.js:1429` | `restoreAfter`：journal、rescue、逐文件执行 |
| `lib/index.js:3264` | `session/event` 钩子：`user/message` 边界触发重扫 |
| `lib/client.js:155` | `rewindCutsOf`：从命令结果算出遮蔽区间 |
| `lib/client.js:805` | 客户端投影：包住 chat builder，给节点加 `visibility` |
| `packages/core/session/src/surface.ts` | 宿主侧对 `surfaceOp` 与 `sourceEventSeqs` 的校验 |
