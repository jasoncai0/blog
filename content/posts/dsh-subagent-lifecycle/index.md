---
title: "子代理：从一次工具调用到独立会话"
date: "2026-11-11T16:00:00+08:00"
summary: "subagent 工具在几十毫秒里返回一个名字，真正的结果要等一条后台通知。中间发生的事是：一个新的 Session 被建出来、一个 scope 被组装、一个 turn 被投递。这篇把这条链路拆开，看父子之间到底共享什么。"
description: "拆开 DSH 的子代理创建链路：独立 Session、fork 的 cut、isSeeded 与 end-seed、setup window 的发布顺序、父取消的 cause 与 scope 边界。"
tags: ["multi-agent", "harness", "architecture"]
slug: "dsh-subagent-lifecycle"
---
> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。行号以该 commit 为准。
> 本系列第 13 篇。系列目录：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)。

第一次读 `subagent` 工具，返回值会让人愣一下：

```json
{ "kind": "activation", "name": "reviewer" }
```

没有结果，也没有句柄。父代理拿到这个名字，转头就去干别的，等一条十几秒后才到的 user message：「Background subagent reviewer finished and will do no further work unless you send it more.」这条消息不属于任何一次工具调用。它没有 tool call id，是运行时自己写进父收件箱的。

如果子代理是一次函数调用，结果应该在调用栈里；如果它是一个线程，两条栈怎么共存、谁先退出又是问题。DSH 选了第三种：一个由 harness 分配的独立 agent。父提交一次执行，拿回一个身份；结果从消息通道回来，血缘留在日志里。

下面按链路的顺序走：新 Session 怎么建、fork 的那一刀切在哪、`isSeeded` 标了什么、setup 为什么卡在两个事件之间、父按停时子收到什么。

## 0. 结论

子代理有自己的日志、自己的工具 scope、自己的预算。它和父之间剩下两样东西：消息（工具结果、结算通知、relay 消息），以及血缘数据（`parentSession`、`delegationDepth`、descriptor、目录条目）。

没有共享内存，没有共享 turn，也没有哪个「父的上下文对象」被传进去。把子代理当成同一台机器上的另一个进程，大部分设计决定就顺了。唯一的区别是两者共享同一个 Context 树，所以组合、释放、事件订阅比跨进程便宜。

{{< fig "diagrams/01-spawn-sequence.svg" "一次 subagent 调用的完整时序：父在 ③ 拿到回执就继续跑，子要到自己那一列的第 ⑩ 步才说完。工具返回点和结算点之间隔着整段子 turn，中间没有任何返回值。" >}}

## 1. 一次调用要拆成两件事：受理与结算

把链路看成两个独立事件，后面的设计基本跟着走。

| | 受理（accepted） | 结算（settled） |
|---|---|---|
| 什么时候 | 工具调用的那次 `execute` 里 | 子的 turn 结束之后 |
| 谁做的 | `manager.startLocal` | `notifySettlement` |
| 产出 | `childId`、`name`、首条消息的 `messageId` | 一条 `subagent-settled` 消息 |
| 父看到什么 | `started subagent reviewer` | 「它完成了 / 被停了 / 没做完」 |
| 失败长什么样 | 抛错，工具结果 `isError` | 不是错误，是 `stopReason` |

受理成功只说明身份建出来了、第一条任务进了收件箱。子会不会做完、甚至会不会开始，都不在这一步的保证里。`ACTIVATION_LIMIT_REACHED` 发生在受理阶段，`stopReason: 'refusal'` 发生在结算阶段，两者在父的日志里长得完全不一样。

`withResultDeliveryGuidance()` 那段话之所以加在子的首条 prompt 末尾，也是这个道理：

> Your final answer is delivered automatically if your parent is still online. Include the result and relevant evidence in that answer; the parent does not receive your full transcript, tool output, or reasoning.

子的「返回值」就是它最后一条文本。写这句话的时候，它并不知道父还在不在线，所以协议只能要求它把结论和证据塞进最终回答。父那边收到的是这条回答的一个投影，不是子的日志。

一条是子主动说的话（`agent-message`），一条是运行时报告子怎么了（`subagent-settled`）。两个消息 kind 被刻意分开。合并之后，日志里会出现「子说过、却没真说」的话。

### 1.1 回执里有什么，以及工具为什么不返回它

`SubagentActivation` 本身带了不少字段：

| 字段 | 内容 | 工具返回了吗 |
|---|---|---|
| `childId` | 持久 Session id，唯一执行身份 | 否 |
| `name` | 模型可见名，如 `reviewer/checker` | 是 |
| `result` | 结算后兑现的 promise | 否 |
| `dispose()` | 只释放这一次执行 | 否 |
| `messageId` | 首条任务被收件箱接受的凭证 | 否 |

工具只把 `name` 交出去，其余全留在服务层。理由不在「模型用不用得上」，在于给出去的东西会改变模型的行为。`result` 这个 promise 摆在返回值里，模型的下一步几乎必然是「等它」；`dispose` 摆在手里，模型就开始管理生命周期。两件事都该由运行时和宿主管，不该由一次工具调用的返回值暗示。

名字是适合交给模型的那一个：它短、可读、能出现在后续工具参数里，解析它也不需要授权（`resolveName` 只查目录，不判断能不能操作）。

## 2. 为什么必须是独立 Session

「给子代理开一个新 session」听着像实现细节，实际是整个模型的地基。四个理由，每个都能反推出一个约束。

一，追加日志只能有一个写者序列。`seq` 就是日志下标，`seq === log.length` 是硬契约。两个 agent 并发 append，`seq` 会变成竞争资源；`turn/start` 与 `turn/end` 的配对、`step` 的边界、压缩的切点，全按单个 agent 的节奏排。共享日志时，父压缩一次就会重写子的历史，子的 `turn/end` 会把已经在 idle 的父重新唤醒。

二，上下文是预算单位。子有自己的 system prompt（工具集不同、角色不同）、自己的压缩点、自己的 token 账本。共享日志意味着共享 prompt 前缀，fork 和 spawn 也就没区别了，「给子一个更窄的上下文」这个动作根本不存在。

三，生命周期要能单独释放。`AgentHandle.dispose()` 要 stop、drain、unregister、remove session、unwind scope，五件事绑在一个单位上。共享日志时「释放子」等于「从父日志里剪掉一段」，而日志是 append-only 的，剪不掉。

四，工具和权限挂在 scope 上，不在日志里。日志记录发生过什么，scope 决定还能做什么。共享 session 既不会让子获得父的工具，也不会让子失去它们；把两件事绑一起，最后两件都做不对。

独立 session 的代价很直接：父看不见子的中间步骤。DSH 用三样东西补，目录（谁存在）、lifecycle 事件（谁开始了、谁结束了）、结算通知（结局与最终文本）。信息通道谈不上最优，但「跨 agent 读内存」这个诱惑被从架构里删掉了。

### 2.1 如果共享父日志，会坏在哪

把假设推到底，能数出四种具体事故。

| 事故 | 机制 | 现象 |
|---|---|---|
| 序号竞争 | 两个写者都要 `seq = log.length` | 事件重复或跳号，重放时 turn 对不上 |
| 配对破裂 | 父的 `turn/start` 与子的 `turn/end` 交错 | 压缩找不到合法切点，恢复后多出半个 turn |
| 唤醒风暴 | 子的每次 `turn/end` 都满足父的 idle 判定 | 父被自己的子反复叫醒，空转 |
| 释放歧义 | 释放子要从日志中间删一段 | append-only 变成可变的，所有快照失效 |

这四条算不上「实现麻烦」，它们指向同一个矛盾：一个追加日志只能有一个时间线。要让两件事并行，就得有两个时间线，然后显式定义两者怎么通信。消息和血缘就是那个定义。

## 3. spawn 的十来步

`manager.startLocal` 是本地子的唯一入口，`startLocalNamed` 只是先预留名字再调它。顺序要逐条看，每一步都在防一种竞态。

```ts
// packages/subagent/subagent/src/manager.ts:302（节选，按执行顺序重排）
this.assertAdmitting(parent)
const childDepth = resolveChildDepth(parent, spec.maxDepth)
const agentOptions = resolveChildAgentOptions(parent, spec.agentOptions, childDepth)
const descriptor = snapshotSubagentDescriptor({ ... })
const policies = captureDelegatedPolicyOverrides(parent, this.ctx)
const cwd = await resolveDirectory(spec.cwd, parent, signal)
const prepared = await prepare({ sessionId: childId, cwd, parent, signal })
const inheritedEventCount = SessionLogOffset(prepared.seed?.length ?? 0)
await this.locks.run(childId, () => this.materialize(...))
```

所有「父的当下」都在第一个 `await` 之前拍完，descriptor、权限策略都在这里定格。源码里那句注释说得很直白：`Capture before the first await: a later parent switch belongs to the parent's future, not to this child.` 父在 await 期间换了模型、松了沙箱，那是父接下来要做的事，不该改变这个孩子出生时的样子。

深度和路由在锁外算，materialize 在 `ChildLock` 里串行。同一个 `childId` 的投递、释放、dispose 不能交错，否则结算通知可能发在一个已经被释放的 activation 上。锁的粒度是 child，不是树，不同子之间不互相阻塞。

目录条目最后才写：`establishCatalogChild` 在 `submitMaterialized` 之后。先能跑，再可被发现。反过来的话，`list_agents` 会列出跑不起来的条目，而模型没有办法区分「刚建好」和「建坏了」。

第 3 步到第 12 步之间，父的工具调用一直挂着。`subagent` 标了 `isConcurrencySafe: () => true` 也是这个原因：它不写文件、不抢资源，只是建一个会话，可以和其他工具调用并发。

### 3.1 十二步各防什么

| # | 动作 | 防的竞态 |
|---|---|---|
| 1 | `assertAdmitting(parent)` | 父已在关闭流程里，还往里塞新子 |
| 2 | `ancestryForChild` | 祖先链在创建后被改写 |
| 3 | `reserveName` | 两个子抢同一个名字 |
| 4 | `resolveChildDepth` | 无限递归，先把深度算死 |
| 5 | `resolveChildAgentOptions` | 父中途换模型，子拿到半新半旧的路由 |
| 6 | `snapshotSubagentDescriptor` | 同上，descriptor 与真实创建参数不一致 |
| 7 | `captureDelegatedPolicyOverrides` | 父的沙箱/审批在 await 期间变化 |
| 8 | `resolveDirectory` | cwd 不存在或不可写，直到子跑起来才发现 |
| 9 | `prepare`（fork 切日志） | 切点漂移：prepare 期间父又写了事件 |
| 10 | `inheritedEventCount` | 前缀长度与实际 seed 不符 |
| 11 | `materialize` + `setup` | 半个 scope 被发布出去 |
| 12 | `submitMaterialized` → 目录 | 目录里出现跑不起来的条目 |

第 9 步需要单独说一句。fork 的 `snapshotEvents()` 拿到的是一份冻结快照，切点基于这份快照算。父在 prepare 期间 append 的事件不在 seed 里，这正是想要的；否则「fork 到哪」会取决于两个异步操作的相对速度。

## 4. fork 的那一刀切在哪

fork 与 spawn 只差一件事：`prepareContinuable` 返回不返回 seed。

```ts
// packages/subagent/subagent-fork-in-process/src/index.ts:46
function completedTurnPrefix(parent: Agent): SessionEvent[] {
  const events = parent.session.snapshotEvents()
  const lastEnd = events.findLast(event => event.type === 'turn/end')
  return lastEnd === undefined ? [] : events.slice(0, lastEnd.seq + 1)
}
```

起作用的就一行：`findLast(... 'turn/end')`。切点只能落在一个完整 turn 的末尾。切在 `step/end` 会留下一个「工具已经调用、结果还没回灌」的前缀，子会看到父亲喊了工具却没有结果，然后试图替父把它补完，而那是一个已经不属于它的决定。切在更早的位置也一样，半个 turn 的意图是不完整的。

代价写在工具描述里，同一个工具名有两套文案（`providerWording`）：

| | fork 版 | spawn 版 |
|---|---|---|
| 描述 | seeded with all completed turns so far | does not share this conversation's context |
| prompt 指引 | build on them freely and state only what is new | include everything it needs |
| `inheritsParentContext` | `true` | `false` |
| `prepareContinuable` | 切父日志 | 返回 `{}` |

`inheritsParentContext` 是个描述性字段，不是被校验的能力。它只决定模型看到的措辞。工具、服务、scope、权限这四样，从来没有继承这一说。

还有一层语义容易忽略：fork 是快照，不是镜像。`prepareContinuable` 只在创建时调用一次，之后的冷恢复 replay 的是当时那份 seed，不会重新去父日志里切一刀。父后来聊了五十轮，子恢复时仍然只看到创建那一刻的三个 turn。这没什么不对，否则「恢复一个子」就变成「让子看到它不在场时发生的一切」，而它当时做的判断是基于旧世界的。

还有第二条构造路径。子代理的 fork 用 `completedTurnPrefix` 自己切，普通 `session/fork` 用 `buildForkSeed`，允许切在任意 boundary：

```ts
// packages/core/session/src/fork.ts:21
const prefix = events.slice(0, boundary + 1)
prefix.push({ type: 'session/end-seed', seq: SessionSeq(boundary + 1), data: { inherited: true } })
return prefix.concat(openTurnClosers(prefix, { kind: 'forked' }))
```

最后一行把收尾事件放在 marker 之后。切点落在一个未闭合的 turn 里，就要补 `turn/end`、`step/end`；这些合成事件不能算进继承前缀，否则 `inheritedEventCount` 会把它们包进去，冷恢复时被当成「父真的写过的东西」。所以布局固定成真实前缀 → marker → 合成收尾。读日志的人只要认 marker，不必逐条判断哪条是真事件。

{{< fig "diagrams/02-fork-boundary.svg" "两种子的日志形态。fork 把父日志的完整 turn 前缀搬过来，在切口放一条 inherited 的 end-seed，再写自己的事件；spawn 从空日志开始，连 marker 都没有。右边标出了 inheritedEventCount 指向的位置。" >}}

## 5. isSeeded、inheritedEventCount 与 session/end-seed

三个东西经常被混成一个，分工倒是清楚。

| | 住在哪 | 类型 | 回答的问题 |
|---|---|---|---|
| `isSeeded` | session header | 布尔 | 这个会话是不是继承来的 |
| `inheritedEventCount` | header + Session 实例 | 数字 | 继承到第几个事件为止 |
| `session/end-seed` | 日志事件 | `{inherited: true}` | 切割线在哪，重放时自解释 |

`isSeeded` 的取值只有一句：`prepared.seed !== undefined`（`manager.ts:386`）。空前缀也算。父还没有任何完成的 turn 时，fork 给出 `[]`，`isSeeded` 仍然是 `true`，`inheritedEventCount` 是 0，Session 构造时在 0 号位补一条 marker。于是「fork 了一个还没有历史的父」和「spawn 一个全新的子」在数据上可区分：前者会留下一条 `inherited: true` 的切割线。

marker 由 Session 构造器补，provider 不管这件事：

```ts
// packages/core/session/src/index.ts:695
if (seed !== undefined && mode === 'snapshot' && this.header.isSeeded && !markedSeed) {
  this.append('session/end-seed', { inherited: true })
}
```

这一行决定了 `inheritedEventCount` 指的是 marker 的位置，不是 marker 之后。因为 seed 恰好是前缀本身，追加发生在 `seq === seed.length`，而 count 也等于 `seed.length`。marker 不属于继承内容，它标记继承在哪里结束。`buildForkSeed` 走另一条路（先 push marker 再补 `openTurnClosers`），布局完全一样：`log[count]` 是 marker，或 `count === log.length`。

构造器还做四条校验，缺一条就抛：seeded 必须有 seed 和 count；unseeded 必须 count 为 0；count 不能超过日志长度；count 处必须是 inherited marker（或正好等于日志长度），且整条日志只能有一个 inherited marker。这些检查的作用是把「重建」变成纯函数：给定日志，切割线唯一确定，不需要额外的元数据。

那为什么不把 cut 存在 header 里就够了？因为日志是唯一权威源。header 来自存储元数据，可能丢、可能旧；压缩、导出、回放、UI 都只读日志。切割线必须在日志里，这些读者才能看见。

还有一个副作用：`firstLifecycleSeq` 也等于 `inheritedEventCount`。生命周期统计从切点开始算，父的 `turn/start` 不会被算进子的活跃时间。

### 5.1 一次 fork 之后，日志长什么样

父日志（已经跑完两个 turn，第三个 turn 正在生成）：

```
seq 0  session/created
seq 1  turn/start        seq 2  user message      seq 3  step/start
seq 4  assistant message seq 5  tool/call         seq 6  tool/result
seq 7  step/end          seq 8  turn/end          ← 切点
seq 9  turn/start        seq 10 user message      seq 11 step/start
```

子日志（fork，`inheritedEventCount = 9`）：

```
seq 0..8   父的完整前缀（对象被复用，没有深拷贝）
seq 9      session/end-seed { inherited: true }
seq 10..   子自己写的事件
```

spawn 子的日志里没有 `seq 9` 那一行，也没有 marker，它压根不是 seeded session。三行差异就是两种子代理的全部数据区别，其余的行为差异（能不能续聊、能不能打开）都由 descriptor 的 `mode` 决定。

`mode` 不写在 header 里，而是作为一条 log-only 事件留在日志里：`subagent/descriptor`，无 surfaceOp、不进模型历史、survive compaction。它记录「这个会话是怎么被创建的」，不回答「它现在是什么」，后者由 header 和运行时状态负责。事实进日志、状态进内存，这种分法在整个子系统里反复出现。

## 6. setup window：为什么夹在两个事件之间

`agents.create()` 的顺序是：铸出 `agentCtx` → 进入 SessionStore → 完成 `session/created` → await setup → commit → 发布 `agent/created`。契约原文很短：

> The factory awaits setup after minting agentCtx, entering the SessionStore, and completing `session/created`. The Session accepts live events during setup; the Agent remains outside its registry until setup completes.

setup 要往日志里写 `agent-preset/selected`、`subagent/descriptor`、三条策略覆盖、工作目录，所以它必须排在 `session/created` 之后：session 得先存在、可写。顺序还有一层意思，这些事件落在 seed 之后，后写的策略覆盖继承来的策略。`appendDelegatedPolicyOverrides` 的注释就是这个意思，新策略盖旧 seed，子日志能自证自己的策略，不用回头查父。

它也必须排在 `agent/created` 之前，因为 `agent/created` 是发布点。通过 `agentCtx` 注册的一切，`tools.restrict`、persona、`subagent:delegation` context、structured runtime、working directory，都得在第一个 prompt 组装之前就位。晚一步，第一个请求会带着半个 scope 发出去；早一步发布，别的 actor 拿到 agent 后立刻 drain，会看到一个还没装配完的执行体。

`materializeTracked` 的尾部把同一原则又写了一遍：先 `resident.set`，再挂 inbox 与 session 监听，最后才 `emitLifecycle('subagent/start')`。注释是：`Listeners may read the resident Agent or drain it immediately. Publish after residency and inbox listeners, as the last rollback-protected step.`

setup 内部的顺序也是固定的：

```ts
// packages/subagent/subagent/src/manager.ts:794（setup 内的关键几步）
if (create.agentPreset !== undefined) applyChildComposition(childCtx, create.agentPreset)
child.session.append('agent-preset/selected', ...)
child.session.append('subagent/descriptor', create.descriptor)
appendDelegatedPolicyOverrides(child, create.policies)
await workingDirectory.set(child, create.cwd, signal)
applyChildComposition(childCtx, undefined)
attachStructuredRuntime(child)
```

第一行和第六行是同一个函数，调两次不是笔误。先按显式 preset 挂载，让后面的 `tools.restrict` 有一个非空集合可筛；继承路径再把父的组合接上来。第三、四行决定了「子日志自证」：descriptor 与策略都写在 seed 之后，新策略覆盖旧 seed，审计时只读子日志就能回答「它出生时被允许做什么」。

commit 阶段还有一次同步检查：`AgentSetupCommit.commit()` 在所有 await 落地后、注册表发布前跑，专门兜住「setup 是异步的、发布是同步的」这条缝。任一步失败，scope 回滚、`agent/created` 不发布，已经 append 的 session 事件保留，就是上面说的「有记录、无执行」。

失败语义也由此确定：setup 抛错 → scope 回滚、不发布 `agent/created`，但已经提交的 session 事件保留。磁盘上可能出现一个「有 session、没有 agent」的子会话。这不算漏洞，是记录与执行的分离：session 是记录，agent 是执行，记录已经发生的事不会因为执行没起来而消失。

契约里还有一句容易被忽略：Setup composes, it never drives. setup 里不能 `followup`、不能等 turn、不能发消息。否则你会在发布之前驱动一个注册表里还不存在的 agent，任何按 id 反查的路径都会失败。

## 7. 父取消怎么传到子

取消有四类 cause：

```ts
// packages/core/session/src/types.ts:194
type AgentCancelCause =
  | { kind: 'user' }
  | { kind: 'parent' }
  | { kind: 'hook'; reason: string }
  | { kind: 'disposed' }
```

父级的停通过 `interrupt` 进来，authority 是 `{kind:'ancestor', agent}`，manager 把它翻成 `kind: 'parent'`：

```ts
// packages/subagent/subagent/src/manager.ts:188（节选）
const kind = authority.kind === 'user' ? 'user' : 'parent'
activation.handle.agent.cancel({ kind }, { keepInbox: true })
```

user 与 parent 要分开记，因为日志和 UI 得能回答「是谁按的停」。用户按停意味着「这件事我不要了」；上游被停意味着「你被牵连了」。重试策略、后续消息、审计口径都不一样。都记成 aborted，两种意图就被压成一种。

四个 cause 的来源各不相同：

| cause | 谁写进去 | 典型场景 |
|---|---|---|
| `user` | 用户按停，或 `interrupt(..., {kind:'user'})` | 「这个方向不对，停」 |
| `parent` | 祖先的 interrupt 被 manager 翻译 | 上游放弃，子被牵连 |
| `hook` | hook 自己发起的取消 | 超时守卫、策略拦截 |
| `disposed` | 释放路径：cancel 后等静默 | agent 被卸载、scope 拆除 |

最后一行有个精确说法：disposal 就是一次 disposed-cause cancel 加一次静默等待（`agent-loop/src/index.ts:509`）。所以「被释放」不是第五种原因，它就是 `disposed` 的全部内容。`turn/end` 的 `aborted.reason` 会原样带上这几类之一，压缩和回放才能区分「人不要了」和「进程没了」。

`keepInbox: true` 也有讲究：interrupt 停的是当前执行，工作没有被丢。未被 claim 的输入留在队列里，子 idle 之后一条 `send_message` 就能接着跑。对比 `cancel(cause)` 不传 keepInbox 的行为，清空收件箱、清掉 wake 标记，那是丢弃。

传播从来不自动。父的 turn 被取消，不会级联到子，因为子是另一个 agent、另一个 turn。要让整棵树停下来，走的是 `dispose`/`drain` 路径，在 `finishDisposal` 里才会用同样的 `{kind:'parent'}` 去停子，并且只停这一个实例、不碰它自己开的子代理。interrupt 是「停一下」，dispose 是「收掉」。

释放时这几步的先后不能乱：`resident.delete` → 释放容量槽 → `notifySettlement`（把结局写进父的收件箱）→ `emitLifecycle('subagent/end')` → `result.resolve`。源码注释说 end 的发布在「资源释放与父通知之后、结果兑现之前」。先通知，再宣告，最后兑现，任何一步提前，观察者都可能读到一个还没结算完的状态。

## 8. 运行时树：三条容易混的线

子代理的「层级」在代码里是三个字段，名字很像，用途完全不同。混着用，恢复和权限判断都会出错。

{{< fig "diagrams/03-runtime-tree.svg" "运行时的三条线：parentSession 是血缘（谁生的），delegationDepth 是预算（还能生几层），subagentDepth 是视图（当前这个 agent 该看到什么）。名字是第四条线，但它是可选地址，不是身份。" >}}

| 字段 | 住在哪 | 回答什么 | 会不会持久化 |
|---|---|---|---|
| `parentSession` | session header | 直接创建者是谁 | 是 |
| `delegationDepth` | session header | 递归预算用掉了几层 | 是 |
| `subagentDepth` | agent options | 这个 agent 运行时该看到什么 | 否，运行期传入 |
| `name` | 目录条目 | 怎么称呼它 | 是，但可缺省 |

`parentSession` 是血缘，取值是 `ancestors.at(-1)`，指直接创建者，不是根。运行时发 lifecycle 事件时用的就是这个字段（`runInfo`），所以一个二级子的 `parentSessionId` 指向一级子，而不是指向 `lead`。

`delegationDepth` 是预算，取值是 `delegationDepthOf(parent) + 1`，超过 `maxDepth` 直接抛 `SubagentDepthError`。它进 header，因为递归预算必须活过持久化和恢复：一个冷恢复的子如果忘了自己已经第三层，就会再开一层，深度上限形同虚设。

`subagentDepth` 是视图，由 `resolveChildAgentOptions` 塞进 agent options。它不进 descriptor、不进 header，因为它是「这次运行应该看到的世界」，不回答「这个会话是谁」。模型可见的 depth 提示（「current N, maximum M, remaining levels 0」）就是从这儿来的。

名字是第四条线，性质完全不同：它是地址，可以缺省，可以指向 Session id，解析它不授予任何权限。`resolveName` 只在目录里查路径；查到了也只是一个 id，能不能操作要由服务层再判一次。这条分离不能省，否则「知道名字」就等于「有权限」。

最后一条容易误解：普通 Session fork 不在这棵树里。树记录的是创建关系，普通 fork 只复制了日志，没有创建关系，所以 `listDescendants` 发现不了它，它下面的子 agent 也一样。想用目录枚举所有「从这段历史分出去的东西」，会漏。

## 9. 子的 scope 与预算：继承什么，不继承什么

这是整篇最容易搞错的部分。子从父那里继承的是路由与预算的默认值。工具、权限、上下文都不在其列，fork 的上下文另说。

{{< fig "diagrams/04-inheritance-matrix.svg" "spawn 与 fork 在十二个维度上的继承关系。绿色是继承或复制，橙色是不继承、必须显式给，灰色是只在创建时有效、冷恢复后消失。最右列是冷恢复时的来源：同一个格子，创建时拿到什么，恢复后还能拿回什么。" >}}

| 维度 | spawn | fork | 依据 |
|---|---|---|---|
| 父日志前缀 | 无 | 已完成 turn 前缀 | `prepareContinuable` |
| 历史标记 | 无 marker | `session/end-seed {inherited}` | Session 构造器 |
| provider / model | 继承父当下，可覆盖 | 同左 | `resolveChildAgentOptions` |
| reasoningEffort | 继承；换路由且未指定则删除 | 同左 | 同上 |
| maxTokens | 继承，仅本次 | 同左 | 同上 |
| 审批策略 | 固定 `never` | 同左 | `captureDelegatedPolicyOverrides` |
| sandbox | 只取父的显式 override | 同左 | 同上 |
| 工具集 | 重新组合 + `restrict` | 同左 | `applyChildComposition` |
| 深度 | 父 +1，受 `maxDepth` 限制 | 同左 | `resolveChildDepth` |
| 容量 | 与整棵树共享一个池（默认 8） | 同左 | `ActivationPool` |
| outputSchema | 不持久化 | 不持久化 | `descriptor.ts` |
| 工作目录 | 可传，默认父的 cwd | 同左 | `childSessionMeta` |

工具为什么不继承，理由从强到弱有三个。

1. 工具是 scope 注册，不是数据。子 ctx 通过 `composeFrom(childCtx, parent.ctx)` 拿到的是服务；工具是随后 `tools.restrict(toolFilter)` 之后剩下的集合。复制「父的工具列表」没有意义，列表本身不是能力，注册在 scope 上才是。
2. 审批面不同。子的 `approvalPolicy` 在有审批服务时被固定成 `'never'`，它问不了人。继承了父那些需要审批的工具，调用会静默失败，而模型会把「没人批」误读成「工具坏了」，然后开始重试。
3. 语义不同。spawn 子看不到父的对话，依赖对话状态的工具（回放上一轮、读 todo、引用刚才的文件）在子里没有意义。给它只会增加 schema 成本和误用面。

fork 子虽然有历史，工具集仍然重新组装。记忆可以复制，能力必须重新授权。两条独立的线，在 fork 里只是恰好都来自父。

预算为什么有一半不持久化。descriptor 里存 provider、mode、label、模型、persona、toolFilter，故意不存 `subagentDepth`、`outputSchema`、`maxTokens`。前者的注释说，深度以 session header 的 `delegationDepth` 为准，因为递归预算必须活过恢复；后者是「一次性预算」，结构化输出和 token 上限只对那一次委派有效，冷恢复后既不该恢复，也不该从父那里重新继承一份。冷恢复的子拿到的是一个没有 schema 约束的新任务，设计上就要这样。

### 9.1 反例：外部执行没有本地会话

「子代理一定有独立 Session」只对本地后端成立。同一个 `startActivation` 还有另一条路：provider 只实现 `start`，不实现 `prepareContinuable`。

| | 本地（spawn/fork） | 外部（Codex、Claude Code、ACP、SDK） |
|---|---|---|
| Session | 有，持久、可续聊 | 没有本地 Session |
| 输入 | inbox，可 steer | 只有开局那一次 |
| 结果 | 结算通知进父收件箱 | 句柄的 `result` promise |
| 发现 | 目录里有 `mode: 'continuable'` | 目录里 `mode: 'external'`，是叶节点 |
| 打断 | `agent.cancel(cause, {keepInbox})` | `controller.abort(cause)` + dispose |
| 能力标记 | `prepareContinuable` 存在 | 只有 `start`，`NO_START_CAPABILITIES` 全 false |

这条分支解释了为什么能力校验要在 `startActivation` 里逐项做。请求方可能传了 `agentPreset`、`outputSchema`、`maxDepth`，而目标后端一个都不支持。校验发生在 prepare 之前，缺能力就直接抛，不会先建一半再说。

也解释了 `delivery: 'parent' | 'caller'` 这个参数：外部执行如果被一段工作流代码启动，结果该回到那段代码，而不是回到某个 agent 的收件箱。`caller` 投递下，`notifySettlement` 直接返回，父永远不会收到那条「Background subagent … finished」。同一个概念，两种交付拓扑。

## 10. 迁移建议：如果你自己搭

一，把委派做成「提交作业」，返回回执。工具结果里放 `{childId, name}`，结果走收件箱消息。这样父可以在等结果的同时干别的，UI 也可以把子显示成一个独立的活动条目。代价是父必须理解「回执 ≠ 完成」，所以工具描述里要写清楚通知什么时候到、带不带最终文本。

二，给 fork 一个显式的数字 cut，外加日志内 marker。不要用时间戳、不要用事件 id、不要用摘要。`seq` 就是下标，切片不需要解释；marker 让日志自己说明「这一刀在哪」，压缩和导出都不必回查 header。三条不变量可以直接抄：cut 必须落在完整 turn 之后；前缀必须从 0 连续；空前缀也要留 marker。

三，把创建拆成 create → compose → publish 三段。compose 阶段只注册、不驱动；publish 放在最后一步，且在所有监听器挂好之后。失败时允许「有记录、无执行」的中间态，别试图回滚已经写进日志的事件，那是把 append-only 当日志用。

四，子的权限在启动时冻结，并告诉模型。审批 `never`、sandbox 只继承显式设置、深度 +1，然后把这条事实写进模型可见的 context。DSH 那段话可以照抄：

> You are a delegated subagent: your permission scope was fixed when you were started and cannot be widened from inside this session — operations that require approval are rejected automatically.

不写这句话，子会反复重试被拒的操作，并把「权限不足」理解成「再试一次就好了」。

## 11. 设计取舍

选独立 Session，放弃共享内存。好处是可单独释放、可单独压缩、可单独取消；代价是 fork 要复制 O(n) 事件，父也看不见子的中间步骤。判断标准很简单：子任务如果需要频繁读写父的实时状态，这套模型会退化成手工的分布式状态同步，不如单 agent 加一个工具。

fork 只切在 `turn/end`，换来的是「子看到的前缀一定自洽」。代价是看不到父当前那个 turn，模型会困惑「我刚说的话它怎么不知道」。fork 的正确用法是接着已经说完的话往下做，不是「看我正在干什么」。

descriptor 不存一次性配置，恢复路径就简单，不需要重放 schema；代价是冷恢复必须重新给 `outputSchema` 和 `maxTokens`。如果你的场景里子任务总是带结构化输出，这个默认值会让你每次都补一遍。

setup 的发布顺序换来的是「能拿到 agent 的观察者一定能安全 drain」，代价是创建路径更长、失败中间态更多。这条选择在调试时最不直观：你会看到 session 文件存在、agent 却不在注册表里。

什么时候该反悔。当「子」需要和父共享一个可变工作集（同一份草稿、同一个 todo、同一个编辑器缓冲区）时，别把它做成子代理。把共享状态放到 session 之外的一个显式存储里，让两个 agent 都去读写它，那样至少冲突是可见的。

## 12. 踩坑点

1. 把回执当结果。拿到 `{kind:'activation'}` 就去读子的输出，读到的是空。现象是父在下一轮说「子已经完成」，实际上子刚收到任务。对策：工具描述里明确通知的到达条件，父的提示词里要求「在给出最终答案前收集所需结果」。

2. 在 setup 里发 prompt。现象是创建卡住，或者子收到了任务但注册表里查不到它。原因是 setup 在 `agent/created` 之前，此时任何按 id 反查的路径都不通。对策：setup 只做注册与组合，第一条消息走 `submitMaterialized`。

3. fork 子在找「刚刚那句话」。现象是 fork 子回答的是上一轮的问题。原因是 cut 在最后一个 `turn/end`，当前 turn 不在 seed 里。对策：fork 的 prompt 里只引用已完成的结论；需要当前上下文就用 spawn 并把材料贴进去。

4. 冷恢复后结构化输出消失。现象是同一个子第二次任务返回自由文本。原因是 descriptor 不存 `outputSchema`。对策：每次委派都显式传 schema。

5. 子代理请求审批被自动拒绝。现象是子报告「工具不可用」。原因是 `approvalPolicy: 'never'`。对策：把需要人批准的动作留在父这一层做；子只做那些在既定权限内能完成的事。

6. 拿 name 当身份。名字可以被省略（此时用 Session id 展示），路径也可能因为某一级没名字而缺失。对策：持久化用 `childId`，展示用 name。

7. 以为取消父就停了子。父 turn 被取消，子继续跑，结算通知照样会来。对策：要停整棵树，显式 `interrupt` 每个子，或者走 dispose 路径。

8. 两个子写同一个文件。工具层检测不到冲突，后写的覆盖先写的。工具的系统提示要求把并行写拆成不相交的目录；这条要求是提示词级的，不是强制。对策：在委派时就分割文件范围，合并由父做。

## 13. 源码索引

| 位置 | 证明什么 |
|---|---|
| `packages/subagent/subagent/src/manager.ts:302` | `startLocal` 的完整准入顺序，第一个 await 之前拍快照 |
| `packages/subagent/subagent/src/manager.ts:319` | `startLocalNamed` 先预留名字，名字在创建前就确定 |
| `packages/subagent/subagent/src/manager.ts:386` | `isSeeded` 在这里由 `prepared.seed !== undefined` 决定，再交给 `childSessionMeta` |
| `packages/subagent/subagent/src/manager.ts:794` | materialize：create/resume 与 setup 的组合 |
| `packages/subagent/subagent/src/manager.ts:894-912` | 发布顺序：resident → 监听器 → `subagent/start` |
| `packages/subagent/subagent/src/manager.ts:1047,1055` | dispose 时用 `{kind:'parent'}` 停子，只停这一个实例 |
| `packages/subagent/subagent/src/manager.ts:1109` | 结算通知投递给直接父，父离线则不投 |
| `packages/subagent/subagent/src/child-agent.ts:49` | `resolveChildDepth`：父 +1，超限抛 `SubagentDepthError` |
| `packages/subagent/subagent/src/child-agent.ts:135` | `childSessionMeta`：`parentSession`/`isSeeded`/`delegationDepth` 的来源 |
| `packages/subagent/subagent/src/child-agent.ts:171` | 子级权限冻结的模型可见表述 |
| `packages/subagent/subagent/src/child-agent.ts:249` | 审批固定 `never`，sandbox 只取显式 override |
| `packages/subagent/subagent-fork-in-process/src/index.ts:46` | fork 的切点：最后一个 `turn/end` |
| `packages/subagent/subagent-spawn-in-process/src/index.ts:50` | spawn 的 `prepareContinuable` 返回 `{}`，不 seed |
| `packages/core/session/src/fork.ts:21` | `buildForkSeed`：marker 在 boundary+1，closers 在 marker 之后 |
| `packages/core/session/src/index.ts:677-700` | seeded session 的四条校验与 marker 的自动补齐 |
| `packages/core/agent/src/index.ts:66` | `CreateAgentOptions` 与 setup 契约原文 |
| `packages/subagent/subagent/src/descriptor.ts` | 为什么 descriptor 不存 depth、schema、maxTokens |
| `packages/subagent/subagent/src/continuation-messages.ts` | 两种消息 kind 的分工，以及结算文案 |

## 14. 收尾

这套设计最值钱的地方，是把一次委派拆成了三个可以独立失败的东西：创建（可能因容量或深度失败）、执行（可能被中断、超限、拒绝）、投递（父可能已经离线）。每一段都有明确的观测点和明确的恢复动作，所以整棵树出问题时是「部分失败」，不会一起挂掉。

代价是父必须学会等待。这正好是下一篇的主题：在一个没有时钟的循环里，怎么知道谁还活着、该怎么等、该怎么停。

上一篇：[12 工具结果的三种命运](/posts/dsh-tool-results.html) ｜ 下一篇：[14 多代理的控制面：发现、等待、打断](/posts/dsh-subagent-control.html) ｜ [系列目录](/posts/dsh-architecture-anatomy.html)
