---
title: "多代理的控制面：发现、等待、打断"
date: "2026-11-11T16:40:00+08:00"
summary: "父代理同时在跑三个子，接下来要回答四个问题：谁还活着、我在等谁、怎么等不烧钱、要停怎么停。DSH 把答案做成四个工具，其中最关键的一个是把「等」变成一次阻塞的工具调用。"
description: "拆开 DSH 的子代理控制面：list/wait/interrupt/message 四个动作、wait 的 park 与超时、interrupt 与 cancel 的分层、宿主 join 与两种上限。"
tags: ["multi-agent", "concurrency", "harness"]
slug: "dsh-subagent-control"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。行号以该 commit 为准。
> 本系列第 14 篇。系列目录：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)。

父代理在一条 assistant message 里开了三个子，拿回三行回执：`started subagent reviewer`、`started subagent tester`、`started subagent researcher`。下一步它想「看看进度」，于是调用 `list_agents`；三个都是 `running`。它再调一次，还是 `running`。第三次之后，它烧掉的 token 已经超过三个子的第一轮输出。

这不是模型笨。**agent loop 里没有「等」这个原语。** 循环要么在跑一个 turn，要么 idle；两次检查之间没有任何东西能让时间「过去」。模型想等，只能反复做动作——每次动作都是一次完整的推理。

DSH 的解法是把等待做成一次工具调用：`wait_agent`。它挂起的是一个工具调用，父的 turn 停在这一步，事件循环里只有一个 `setTimeout` 在走。直到子的收件箱落进新东西，或者超时，这一行才返回。

这篇讲控制面的四个动作，以及它们为什么必须都是工具。

## 0. 一句话结论

控制面是四个显式动作：`list_agents` 发现、`wait_agent` 等待、`interrupt_agent` 打断、`send_message` 通信。四个都注册在工具层，所以模型能调用、审批能拦截、日志能审计。

运行时不做隐式调度。没有「父自动挂起等所有子」这回事，也没有「子完成时自动叫醒父」之外的魔法——那条叫醒是一封普通的收件箱消息，和用户插话走同一条队列。

{{< fig "diagrams/01-control-surface.svg" "控制面的四个动作各自回答一个问题，输入输出都被压到最小：list 只给状态，wait 只给一个布尔，interrupt 只给 accepted，message 只给 messageId。没有任何一个动作返回子的输出。" >}}

## 1. 四个动作，四个问题

| 动作 | 回答什么 | 实现层 | 返回 | 失败形态 |
|---|---|---|---|---|
| `list_agents` | 树上有谁，活着吗 | 读父会话的目录投影 | `name/status/description` | 诊断条目 `corrupt`/`unsupported` |
| `wait_agent` | 我下一步有新输入吗 | 在工具调用内部阻塞 | `{timedOut}` | `WAIT_UNAVAILABLE` |
| `interrupt_agent` | 让某个子停下 | 服务层发 cancel 信号 | `{accepted:true}` | 解析不到名字 |
| `send_message` | 给某个子送话 | 服务层投递到收件箱 | `{messageId}` | `AGENT_NAME_NOT_FOUND` |

四个动作有一个共同点：**都不返回子的输出。** 输出只从结算通知或 `send_message` 的回信里来。控制面管的是「谁、在哪、还在不在」，不管「它说了什么」。

### 1.1 发现：状态只有两个值，而且不代表完成

`list_agents` 的输出以根 `lead` 开头，后面是每个可见子代的 `name`、`status`、`description`。状态由一行代码决定：

```ts
// packages/subagent/tool-subagent-control/src/list-agents.ts:22
function statusOf(agents, id): 'running' | 'inactive' {
  return agents.get(id)?.status === 'running' ? 'running' : 'inactive'
}
```

`inactive` 的含义是「没有 turn 在跑」，不是「任务完成」。子可能刚被创建还没开始、可能被中断、可能已经失败退出。工具描述里把这句话写死了：

> running means it is working; inactive means no turn is executing; it does not indicate task completion, success, or failure.

那模型怎么知道完成？靠通知，不靠查询。同一段描述接着说：*While you remain active, you will be notified when a direct child finishes; there is no need to keep checking its status.*

目录本身由父会话拥有：它是一条投影，按父日志里的事件顺序列出本地子和 parent 投递的外部子，不读子日志。这也是为什么 `list_agents` 很便宜——它只读一个已经物化好的列表。

`project` 还会隐藏两种条目：非 `continuable` 的（one-shot、external）不出现在结果里，模型永远选不到它们；读不出来的目录则变成 `{kind:'diagnostic'}`，`status` 直接是 `corrupt`、`unavailable` 或 `unsupported`。隐藏但仍然遍历，因为要通过它们找到后代。

### 1.2 等待：返回值只有一个布尔

`wait_agent` 的 schema 只有一个参数 `timeout_ms`（10 秒到 1 小时，默认 30 秒），输出只有一个字段：

```ts
{ timedOut: boolean }
```

`timedOut: false` 表示「有新的待处理输入」，`true` 表示「这段时间里没有」。它不告诉你输入是什么——那些消息会在下一步作为普通输入交付。工具描述里那句 *Pending input is delivered to the model after code execution finishes* 说的就是这个顺序：工具先返回，消息后进上下文。

这个设计把「等待」和「读取」拆开了。等待是一个可中断、可超时的阻塞；读取是下一轮模型调用自然会做的事。合并两者会让工具结果里塞进任意长度的消息，还要处理「等到了三条，但只有一条是给我的」这种过滤。

### 1.3 打断：发信号，不等结果

`interrupt_agent` 立即返回 `{accepted:true}`。`accepted` 的语义很窄：**信号已发出**，不代表目标已经停。真正停下来发生在目标的 turn 边界，那里会写一条 `turn/end`，原因 `aborted`。

描述里还有两句关键：本地子被打断后仍可以用 `send_message` 续聊；而**它自己开的子代理会继续跑**。后者经常被误解，下一节讲。

### 1.4 通信：三种投递，一个工具

`send_message` 把一段文本送到目标的收件箱。目标状态决定它怎么被消费：正在跑的收 steer（下一步就看到），空闲的开一个新 turn，冷会话可以恢复。判定在服务层，工具只做两件事：把名字解析成 id，把内容交出去。

返回的 `messageId` 是「已入队」的凭证，不是「已读」。父拿它做不了什么，除了在日志里对账。

### 1.5 地址：名字怎么变成目标

四个动作里三个要指定目标：`interrupt_agent`、`send_message`、以及 `list_agents` 输出里的 `name`。目标可以写成一个路径，比如 `reviewer/checker`；也可以写成 Session id。解析规则只有三条：

- `lead` 永远是根；
- 含斜杠的按目录逐段查，某一级不存在就抛 `AGENT_NAME_NOT_FOUND`；
- 不含斜杠的原样当作 Session id 返回。

**解析成功不等于有权操作。** 源码注释是 *Resolution grants no authority*。`resolveName` 只回答「这个名字指向谁」，`interrupt` 拿到 id 之后还要再判一次实时血缘，`send_message` 还要判发送方是否在线、是否同根。两层分开，名字才能随便给模型用。

名字本身也有约束：小写字母、数字、连字符，最长 32，`lead` 保留；同一父下由 `reserveName` 保证唯一。路径是拼出来的，所以一个没名字的子会以 Session id 出现在列表里，也就无法被路径寻址——这是可接受的降级，不是错误。

## 2. 为什么 wait 不能是 busy-loop

假设没有 `wait_agent`，模型只有两种选择：反复查，或者拿一个 sleep 工具睡一会儿。两种都贵，贵在不同地方。

**查一次就是一次推理。** 每次 `list_agents` 都要把整个上下文重新发一遍，输出几个 token，再写一轮日志。三个子跑两分钟，父查二十次，账单上父比子贵。

**每一查都写一对 turn 事件。** 父的日志膨胀得比子快，压缩点提前到来，压缩本身又要一次模型调用。查询的代价会自我放大。

**检查和到达之间有窗口。** 查完是空的，0.2 秒后消息到了；父正在生成下一句「还没有」，等这句话写完，消息才被看到。窗口大小取决于模型速度，不可控。

把账算粗一点。父的上下文按 40k token 估，一次查询的输入就是 40k，输出几百；跑三轮查询大约是 120k 输入 token。三个子各自跑一轮 15k，加起来 45k。**父「看看进度」的开销可以是子全部工作的两倍多。** 这还没算每轮查询写进日志、再由压缩重新处理的那部分。

**sleep 也不解决问题。** 就算给模型一个 `sleep(30)`，它仍然要花一次推理去调用、再花一次推理醒来；30 秒的粒度还会让「消息在第 2 秒就到了」变成「白等 28 秒」。更麻烦的是可观测性：日志里只有一串 sleep，看不出父在等谁、为什么等。

还有一条更硬的问题：**busy-loop 让父永远不 idle。** 宿主的退出条件是「父 idle 且树里没有待办」；一个每三秒查一次的父永远不满足第一条。机器不能关，进程不能退，只能靠人按停。

{{< fig "diagrams/02-wait-compared.svg" "两种等待的时序对比。左边轮询：每次检查都是一次完整的模型 turn，检查之间是空的；右边 park：一次工具调用挂起，父的 turn 停住，消息到达时工具返回，下一步直接看到输入。右边下半段是宿主侧的 join：它等的是「树里还有没有在推进的工作」。" >}}

## 3. wait_agent 到底在等什么

等待函数不长，但要同时接四个信号：

```ts
// packages/subagent/tool-subagent-control/src/wait-agent.ts:26（节选）
const previous = reported.get(agent)
const timeout = setTimeout(() => finish({ timedOut: true }), timeoutMs)
const offInbox = ctx.on('agent/inbox/inserted', (message) => {
  if (message.recipient === agent) onInput()
})
const offDisposed = ctx.on('agent/disposed', (disposed) => {
  if (disposed === agent) reject(new AgentWaitError('wait_agent ended because the calling agent was disposed', 'WAIT_UNAVAILABLE'))
})
```

**四个终止条件。** 收件箱有新输入、调用者被 dispose、工具调用的 `signal` 被 abort、插件被卸载（`lifetime` abort）。加上超时，五选一，谁先到谁结束。缺任何一个，等待都可能永远不返回：调用者已经没了，工具还挂在那里。

**只等 `nextStep`，不等 `nextTurn`。** 收件箱里排着两类东西：下一步就要注入的（steering、通知），和排队等下一轮的新任务。父等的是「下一步能看到什么」，所以只看前者。等 `nextTurn` 会让父在子还没开始干活时就醒，白等一轮。

**先订阅，再读 pending。** 源码注释写得很直白：*Subscribe before reading pending input so an arrival cannot fall between them.* 顺序反了，就会丢一个恰好落在「读完」和「订阅」之间的消息，然后父睡到超时。

**`reported` 防的是自旋。** 这个 `WeakMap<Agent, {rootCallId, ids}>` 记着「同一次根工具调用里，上一次 wait 已经报过哪些 pending」。场景是 `run_code`：一段程序里循环调用 `wait_agent`，如果不记账，第二次 wait 会立刻在同一批未消费的消息上返回 `timedOut: false`，循环空转。记账之后，只有**新**的输入才结束等待。

**超时下限是 10 秒。** 一次模型调用通常比这更久，比 10 秒更短的等待没有意义，还会诱导模型把 `wait_agent` 当 `sleep` 用。上限 1 小时，防止一次调用挂死整棵树。

**等待返回之后，消息在哪。** 工具结果先落地，收件箱里的消息在下一步被组装进 prompt。两条通道分开的好处是工具结果保持定长（一个布尔），不会被一条长消息挤爆；坏处是模型可能在同一步里既看到「有输入」又看不到输入本身，得等下一步。工具描述里那句 *Wait for input at your next step* 就是提前告诉模型这个时序，免得它以为工具坏了。


## 4. 谁在计时

这是最容易想错的一处：park 的不是 agent，是**这一次工具调用**。

工具执行期间，agent 的 phase 是「跑工具」，状态仍是 `running`。循环停在 `await tool.execute(...)` 这个 promise 上，不需要任何唤醒——promise 一 resolve，循环自然继续到下一步。没有额外的调度器，也没有心跳。

计时的是工具自己创建的 `setTimeout`。它属于插件的 scope，插件卸载时 `ctx.effect` 会 abort `lifetime`，等待立刻以 `WAIT_UNAVAILABLE` 结束。用户按停时，父 turn 的 abort 传到 `exec.signal`，同样结束等待。

所以「谁在计时」的答案是三层：**超时归 `setTimeout`，取消归工具的 abort signal，卸载归插件的 lifetime。** 三者都必须在 `finish()` 里清干净——源码里那个函数同时做 `clearTimeout`、解绑 inbox、解绑 disposed、移除 abort 监听。漏一个就是内存泄漏，或者更糟：等待结束后还往一个已经 settle 的 promise 上写。

## 5. 宿主侧的等待：join 一棵还在动的树

工具层的 `wait_agent` 解决「父想等子」；宿主还要解决「进程什么时候可以退出」。这两个问题看起来一样，其实不同：父 idle 不代表树里没活，树里没活也不代表父不会立刻被叫醒。

`bundle/headless` 的退出循环只有六行：

```ts
// packages/bundle/headless/src/index.ts:429
while (true) {
  await agent.whenIdle()
  const idleSeq = agent.session.seq
  if (!await subagents?.waitForTreeWork(agent) && agent.status === 'idle' && agent.session.seq === idleSeq) break
}
```

**关键在第三个条件：`seq` 没变。** 子结算会先给父投一条消息，父可能因此被唤醒、开始新 turn。`waitForTreeWork` 返回 false（没有观察到新工作）和父重新忙起来之间有一个窗口；用 `seq` 快照把窗口封住，避免「刚判断完可以退出，父又被叫醒了」。

**`whenIdle` 自己也是个循环。** 它的实现是反复 `await activityDone`，直到一次等待结束后没有新的活动：

```ts
// packages/core/agent-loop/src/agent.ts:253（结构）
while (true) {
  const activity = this.activityDone
  await activity
  if (activity === this.activityDone) return
}
```

不循环就会漏掉「刚 idle 又被唤醒」的竞态：宿主在 t 时刻看到 idle，t+1 收到一条消息开始新 turn，退出判断却还拿着 t 时刻的结论。`await` 之后再比一次引用，成本很低，但能把窗口关掉。

`waitForTreeWork` 内部按成员类型分三路：

| 成员状态 | 宿主怎么等 |
|---|---|
| 正在 materialize | 等它的 settled promise |
| 本地子非 idle | `agent.whenIdle()` |
| external 或 closing | 等 `result.promise` |
| idle 但有 parked inbox | **直接跳过，不阻止退出** |
| 其余 | `race(result.promise, poke.promise)` |

第四行是这套设计里最容易被忽略的一条：**空闲且收件箱里有待处理输入的成员，会保持驻留，但不阻止宿主完成。** 子等着父回话，父已经没事了——这不是死锁，是一个可以保存下来、下次再继续的状态。如果让这种成员阻塞退出，任何一次「子问了个问题」都会把进程钉住。

`poke.promise` 是给「成员状态变化」准备的：新子被创建、旧子进入 closing，都会打断当前这轮等待，让循环重新枚举。所以这个方法在一个 `while` 里反复收集成员、反复等，直到一整轮没有观察到任何变化。

把它压成伪代码，结构是三层循环：

```ts
// packages/subagent/subagent/src/manager.ts:245（结构，非原文）
while (true) {
  const members = collect(root)              // materializations + resident
  await Promise.all(members.filter(local).map(a => a.whenIdle()))
  for (const m of members.filter(active)) {
    if (m.idle && m.hasPending && !m.failed) continue   // parked：不阻塞
    await Promise.race([m.result, m.poke])              // 否则等它变化
  }
  if (!grew(members)) return observed
}
```

**`observed` 是返回值，不是「树空了」。** 它回答「这一轮里有没有看到在推进的工作」。宿主拿它和 `seq` 快照一起判断能不能退出；如果它直接返回「树空了」，那 parked 的子会被误判成已完成。

**为什么要重新枚举。** 等一个子的时候，另一个子可能开了孙，或者父自己又开了一个新的。成员集合在等待期间会变，所以每一轮都要重收一次。这里的代价是 O(成员数) 的重复遍历，换来的是一致性——没有快照，就没有过期快照。


## 6. 打断：interrupt 与 cancel 不是一回事

两个词经常混着用，在 DSH 里它们是两层。

| | `interrupt` | `cancel` |
|---|---|---|
| 是谁 | 子代理服务的 API，`interrupt_agent` 工具调它 | Agent 实例的方法 |
| 参数 | `(targetId, authority)` | `(cause, {keepInbox})` |
| 返回 | 立即返回，信号发出即结束 | 同步改状态，turn 结束时落日志 |
| 权限 | 服务层校验实时血缘 | 调用者本来就持有 agent |
| 收件箱 | 默认保留未 claim 的工作 | 默认清空 |
| 级联 | 不级联到孙 | 不级联，只作用于这个实例 |

`interrupt` 的核心是三道校验：

```ts
// packages/subagent/subagent/src/manager.ts:188（节选）
if (ctx.agents.get(caller.id) !== caller) unauthorized('requires the exact live ancestor agent')
if (caller.id === targetSessionId) unauthorized('cannot interrupt itself')
const activation = this.resident.get(targetSessionId)
if (activation === undefined) return          // 目标已经不在，no-op
if (rootOf(caller) !== activation.root || !activation.ancestors.includes(caller.id)) unauthorized(...)
```

**第一道区分「同一个 id」和「同一个活着的 agent」。** 冷恢复会造出同 id 的新实例；旧实例的引用不能拿来授权。

**第二道禁止自我中断。** 一个 agent 停自己会绕开 turn 的正常收尾路径，日志里留下一个没有 `turn/end` 的 aborted。

**第三道是血缘。** 调用者必须和目标同根，且是目标的祖先。注意它校验的是**实时**的 `ancestors` 数组，不是名字、不是目录条目——目录可能过期，血缘不会。

**停在日志里长什么样。** 子的那次 turn 会写一条：

```ts
// packages/core/session/src/types.ts:201
{ type: 'turn/end', data: { reason: { kind: 'aborted', reason: { kind: 'parent' } } } }
```

`reason` 里嵌套的那个 cause 就是「谁按的停」。用户按的记 `user`，祖先按的记 `parent`，释放记 `disposed`，hook 取消记 `hook` 加原因。四类都不可互换——回放时它们是四种完全不同的意图。

通过之后，authority 被翻译成 cancel cause：`{kind:'ancestor'}` 变成 `{kind:'parent'}`，用户来的变成 `{kind:'user'}`。然后本地子走 `agent.cancel(cause, {keepInbox: true})`，外部子走 `controller.abort(cause)` 加 `dispose`。

**为什么保留收件箱。** 停的是「这次执行」，不是「这份工作」。子手里可能还有一条没跑的任务，或者父刚发来一条补充说明；清掉它们，父就得重新委派一次，还要重新建立上下文。保留的代价是「停了还在」的错觉——父可能以为 interrupt 之后子什么都不剩，实际它 idle 在那里，等下一条消息。

**keepInbox 保的是哪一部分。** 收件箱里有两类工作：已经被某个 turn claim 的，和还排着队的。interrupt 只发信号，已经 claim 的那部分不会被重新入队——那个 turn 的 `turn/end` 会带着 aborted 落日志，它没做完的部分就是没做完。未 claim 的留在队列里，子 idle 之后 `send_message` 一来就能接着跑。

对比 `cancel` 不传 `keepInbox`：它直接 `inbox.clear()`，连 `wakeRequested` 一起清掉。那是「这份工作不要了」，不是「先停一下」。两个动作在日志里的区别只有 cause，但在收件箱里的区别是「还剩多少」。

**为什么不级联。** 子自己开的孙是另一个 activation、另一个 session。打断一个节点就默默扩大半径，会让「停一个跑偏的子」变成「停掉半棵树」。要收整棵树，走 `drain`/`dispose` 路径，那是资源回收，不是控制信号。

{{< fig "diagrams/03-interrupt-paths.svg" "两条打断路径。左边是用户的 interrupt：authority 是 user，cause 也是 user；右边是祖先的 interrupt：authority 是 ancestor，落地时翻译成 parent。两条都只停目标实例，它自己开的子代理继续跑。最下面的 dispose 是另一条路：回收资源时才逐个 cancel。" >}}

## 7. 子已经死了，父还在等

这是并发代理最典型的故障：父挂在一个永远不会来的结果上。DSH 里「死」有三种，信号各不相同。

| 死法 | 留下的痕迹 | 父怎么知道 |
|---|---|---|
| 子报错退出 | `stopReason: 'error'` | 结算通知，`failed before it finished` |
| 子被 dispose | `resident` 里没有它，目录可能还在 | 结算通知先发；父若已 closing 则只 inject 不唤醒 |
| 进程崩、目录损坏 | 目录读不出来 | `list_agents` 出现 `status: corrupt` |

三种都不是靠 `wait_agent` 发现的。`timedOut: true` 只说明「这段时间没有新输入」，它和死亡没有必然关系——一个跑得很久的子也会让父超时一次。

真正的权威信号是结算通知。`notifySettlement` 在释放资源之后、`subagent/end` 之前，把一条 `subagent-settled` 消息投进父的收件箱：

- 父在线且 idle → `queue`，下一轮消费；
- 父在线且忙 → `steer`，下一步注入；
- 父正在 closing → `inject`，不唤醒，只留在日志里。

消息里带 `summary` 和 `stopReason`，但不带子的完整输出——那段文本要么在 summary 里被引用，要么在子最后一条消息里。**父等待的对象应该是这条通知，不是子的状态位。**

反过来，父自己被杀时，`wait_agent` 会以 `WAIT_UNAVAILABLE` 结束（`agent/disposed` 监听）。所以一个挂着的等待不会在父已经消失之后还留着。

把一次典型事故按时间摊开，会看到信号是怎么接上的：

| 时刻 | 发生什么 | 父看到什么 |
|---|---|---|
| t0 | 父调用 `wait_agent`，超时 30 秒 | 工具挂起，turn 停在这一步 |
| t1 | 子的工具抛错，`agent/error` 记下失败点 | 无 |
| t2 | 子走到 `turn/end`，`captureLocalResult` 判 `error` | 无 |
| t3 | `finishDisposal` 释放资源，`notifySettlement` 投递 | 收件箱多一条 `subagent-settled` |
| t4 | 等待被 inbox 事件唤醒，返回 `{timedOut:false}` | 工具结果落地，下一步看到通知 |
| t5 | 父读通知：`failed before it finished` | 决定重试还是换人 |

整条链上没有一次轮询。父从 t0 一直挂到 t4，中间不产生任何推理；t5 的决策基于一条带 `stopReason` 的消息，而不是 `inactive` 这个状态位。

最麻烦的是互相等：父 `wait_agent` 子，子 `send_message` 父之后又 `wait_agent` 父。DSH 不给这种结构做特殊检测，靠的是 `timeout_ms` 兜底。设计上要避免「子等父的下一步」——子的输入应该来自任务描述，不是父的实时状态。

{{< fig "diagrams/04-runaway.svg" "三种失控形态。左边是父用查询代替等待，横向烧 token；中间是父等子、子又等父，形成环，只能靠超时解开；右边是委派链一直向下，每层都开新子，靠深度上限截断。" >}}

## 8. 两种上限：并发与深度

控制面除了「怎么停」，还有「最多能开多少」。

| 上限 | 默认 | 在哪检查 | 失败 | 防什么 |
|---|---|---|---|---|
| `maxActiveSubagents` | 8 | 受理时占容量槽 | `ACTIVATION_LIMIT_REACHED` | 资源耗尽、账单失控、上下文洪水 |
| `maxDepth` | 1 | `resolveChildDepth` | `SubagentDepthError` | 递归委派指数爆炸 |

**并发上限是整个服务共享一个池。** 不是每个父 8 个，是这个实例上同时活着的 activation 一共 8 个。这个选择让「一个 host 最多占多少资源」可预测；代价是多个根共享同一份额度，一个很能生的父会挤掉别人的名额。

**深度上限在创建时就算死。** `delegationDepth + 1` 超过 `maxDepth` 直接抛，连 session 都不会建。默认 1 意味着根只能开一层：子不能再开子。这个默认值很保守，理由是「大部分任务一层就够，需要两层应该是显式决定」。

两个上限防的方向不同：并发是横向的宽度，深度是纵向的长度。只限一个，另一个方向仍然可以指数增长——八个子各开八个孙，64 个；再加一层就是 512。

深度上限还有一半工作在模型侧：当 `depth >= maxDepth`，系统提示里会注入一句 *Delegation depth … remaining levels 0. Do not call `subagent` at this depth; carry out the work directly.* 模型提前知道自己没有额度，就不会先规划一次委派再被拒。

### 8.1 撞上上限时，模型看到什么

两个上限都在受理阶段拒绝，错误文案是给模型看的，不是给日志看的：

- 容量：`subagent limit reached (active child limit: 8); wait for an existing child to finish or complete this work with the current agents`
- 深度：`subagent depth 2 exceeds maxDepth 1`（`SubagentDepthError`）

第一句特意带了「等一个现有的子结束」这个替代动作。模型读到它，下一步多半是 `wait_agent`，而不是原样重试。**拒绝的时候要给出去路**，否则模型只会换个参数再撞一次。

第二句在工具执行时抛出，通常不会发生——因为系统提示已经提前注入了 `remaining levels 0`。真正会撞上的是「模型没读提示」，或者 `maxDepth` 被配成了更小的值而提示还没来得及更新。两处冗余，一处是提示，一处是执行，覆盖不同的失败模式。

预算（`maxTokens`）不属于控制面，但它和这两个上限是一组：并发限「同时几个」，深度限「几层」，预算限「每个花多少」。三者都缺一不可，前两个在受理时拒绝，最后一个在执行中截断。

## 9. 为什么控制工具也是工具

`interrupt_agent` 完全可以做成 UI 上的按钮，或者宿主 API。DSH 把它注册成工具，有三个直接后果。

**可审批。** 审批策略作用在工具调用上，`interrupt_agent` 和其他工具走同一条路径。一个只想让模型读文件、不允许它停别人的部署，可以用同一条规则表达。

**可审计。** `tool/call` 进父的日志，谁在什么时候停了谁，回放时一目了然。做成隐式 API，这条记录就只在服务内部，日志里查不到。

**模型能自救。** 发现一个子跑偏了，模型可以自己停它，然后重新委派。如果停子是 UI 特权，模型只能报告「它跑偏了，请人工处理」，把一次自动恢复变成一次人工介入。

代价是权限必须收在服务层。工具只传 `{kind:'ancestor', agent: caller}`，真正的判定在 `manager.interrupt` 里，用的是实时血缘。工具本身不加一分权——源码注释说得很清楚：*The service authorizes the exact live caller against the target's recorded lineage; the tool adds no authority of its own.*

**工具还是可见性开关。** `interrupt_agent` 的注册由配置控制，默认开；关掉之后模型根本看不到这个工具，服务 API 仍然可用。同一个开关，对模型是「有没有这个动作」，对宿主是「允不允许这类操作」——两种语义落在同一个注册点上，不需要两套权限系统。

这也是为什么 `resolveName` 只解析、不授权。名字是一个地址；能不能对这个地址做事，是另一层判断。两件事分开，才能既让模型方便地引用子，又不让「知道名字」变成「有权限」。

## 10. 迁移建议：如果你自己搭

**一、把「等」做成一次可取消的阻塞调用。** 输入是超时和取消信号，输出是一个布尔。不要在提示词里教模型「每隔几秒查一次」，那是在用推理买时间。等待函数要同时接四个信号：数据到达、调用者消失、调用被取消、插件被卸载。

给等待原语一个最小接口，大概是这样：

```ts
async function waitForInput(opts: {
  agent: Agent
  timeoutMs: number
  signal: AbortSignal      // 调用被取消
  lifetime: AbortSignal    // 插件被卸载
}): Promise<{ timedOut: boolean }>
```

四个输入里有两个是 abort signal，这不是冗余：取消来自调用方，卸载来自宿主，生命周期不同，必须分开接。返回类型刻意只有一个布尔——任何更丰富的结果都会诱导调用方在等待里做业务判断。

**二、把「活着」和「完成」拆成两个通道。** 查询接口只回答执行状态（running/inactive），完成走事件或通知，并且带上 `stopReason`。合并两者，模型迟早会把 `inactive` 当成功，然后拿着半截结果继续。

**三、中断分两层：stop 与 dispose。** stop 发信号、保留队列、允许续聊；dispose 收资源、清注册表、不保证可恢复。只做一层，要么停不干净，要么停一次就丢了所有上下文。

**四、控制动作全部走工具层，authority 由服务层判。** 工具负责解析名字和转发，服务负责校验「这个调用者现在有没有资格」。这样审批、审计、模型自救三件事可以同时成立。

## 11. 设计取舍

**park 工具调用，而不是自动挂起父代理。** 换来的是「等」变成一次显式决策，模型知道自己在等，日志里也有这一笔；付出的是模型可能选择不 wait，继续空转。缓解只能靠提示词，不能靠运行时强制——强制挂起会让「父还有别的事要做」这种正常情况无法表达。

**目录只报两个状态。** 换来的是接口稳定、不用维护进度百分比；付出的是模型容易误读。DSH 用两处文案补：工具描述里写死「inactive 不代表完成」，系统提示里写「子完成时你会收到通知」。

**打断保留收件箱。** 换来的是可续聊、不丢工作；付出的是「停了还在」的状态需要父自己理解。如果某个场景要求「停了就清空」，那应该走 dispose，而不是给 interrupt 加一个清空参数。

**上限给固定默认值。** 换来的是可预测；付出的是复杂任务要显式调高。默认 1 层 8 个，是按「大多数委派是叶子任务」这个假设定的；如果你的场景天然是三层研究树，这个默认值第一天就会挡路。

**不做自动重试。** interrupt 之后，运行时不会替父重新委派，也不会把没跑完的子重新排队。换来的是「停」这个动作没有副作用，父可以自己决定下一步；付出的是父要显式处理 `aborted`，忘了就会以为任务还在。自动重试听起来贴心，实际会把一次人工止损变成一场静默的拉锯。

**什么时候该反悔。** 当父的主要工作就是「等子」时，别再优化等待，改任务划分：把三个子合并成一个，或者让父自己也参与一部分。控制面再精细，也救不了一个本质上串行的任务图。

## 12. 踩坑点

**1. 在循环里查状态。** 现象是父的 token 消耗远高于子，日志里全是 `list_agents`。原因是每次查询都是一次完整推理。对策：用 `wait_agent`，让运行时替你挂起。

**2. 把 inactive 当完成。** 现象是父拿着空结果继续，或者对刚创建还没跑的子说「它失败了」。原因是状态位只表示「没有 turn 在跑」。对策：完成只认结算通知里的 `stopReason`。

**3. 把 `timedOut: true` 当失败。** 现象是父在子正常跑长任务时反复重试或重新委派。原因是超时只说明「这段时间没有新输入」。对策：超时后先 `list_agents`，还 running 就继续等。

**4. interrupt 之后立刻 send_message。** 现象是新消息被当成 steer 插进正在收尾的 turn，或者子停在了半截。原因是 interrupt 是异步的，turn 边界才生效。对策：等结算通知，或者先确认目标已经 inactive。

**5. 以为 interrupt 会停掉整棵树。** 现象是父停了子，孙还在写文件。原因是打断只作用于目标实例。对策：要收整棵树走 dispose/drain，或者逐个 interrupt。

**6. 在 `run_code` 里裸写 wait 循环。** 现象是同一批消息让循环瞬间转几十圈。原因是等待返回的是「有输入」，不是「输入已消费」。对策：记录已报过的消息 id，或者干脆不要在代码里循环等待。

**7. 让子等父的实时状态。** 现象是两边都挂着，直到超时。原因是父在等子的结果，子在等父的下一步。对策：子的输入来自任务描述；需要双向协调时，用消息而不是等待。

**8. 把 `maxDepth` 调大当「更灵活」。** 现象是账单和日志一起爆炸，且很难定位是哪一层开的。原因是深度是乘数。对策：默认值不动，确实需要时按任务显式调，并在提示词里说明为什么。

## 13. 源码索引

| 位置 | 证明什么 |
|---|---|
| `packages/subagent/tool-subagent-control/src/index.ts:25` | `interrupt` 开关：默认注册工具，关掉后服务 API 仍在 |
| `packages/subagent/tool-subagent-control/src/index.ts:120` | `interrupt_agent` 只传 ancestor authority，不加权 |
| `packages/subagent/tool-subagent-control/src/list-agents.ts:22` | 状态只有 running/inactive 两个值 |
| `packages/subagent/tool-subagent-control/src/list-agents.ts:27` | 隐藏非 continuable 条目，诊断条目用 reason 当 status |
| `packages/subagent/tool-subagent-control/src/wait-agent.ts:26` | 等待函数：四个信号 + 超时 |
| `packages/subagent/tool-subagent-control/src/wait-agent.ts:39` | `reported` 记录同一次根调用的已报输入，防自旋 |
| `packages/subagent/tool-subagent-control/src/wait-agent.ts:110` | 超时默认 30 秒，越界抛 `WAIT_INVALID_TIMEOUT` |
| `packages/subagent/subagent/src/manager.ts:188` | interrupt 的三道校验与 cause 翻译 |
| `packages/subagent/subagent/src/manager.ts:233` | `drain`：先 abort 全部 materialization，再收根 |
| `packages/subagent/subagent/src/manager.ts:245` | `waitForTreeWork`：parked inbox 不阻止宿主完成 |
| `packages/subagent/subagent/src/manager.ts:1047` | dispose 用 `{kind:'parent'}` 停子，只停这个实例 |
| `packages/subagent/subagent/src/manager.ts:1109` | 结算通知按父的状态选 queue/steer/inject |
| `packages/subagent/subagent/src/index.ts:185-197` | `maxDepth` 默认 1、`maxActiveSubagents` 默认 8 |
| `packages/core/agent-loop/src/agent.ts:199` | maintenance 期间状态仍算 idle，结束后按需唤醒 |
| `packages/core/agent-loop/src/agent.ts:253` | `whenIdle` 循环等待，防「idle 后又被唤醒」的竞态 |
| `packages/core/agent-loop/src/index.ts:517` | disposal 就是 disposed-cause cancel 加静默等待 |
| `packages/core/session/src/types.ts:194` | 四类 cancel cause，`turn/end` 原样记录 |
| `packages/bundle/headless/src/index.ts:429` | 宿主退出循环：whenIdle + join 树 + seq 快照 |

## 14. 收尾

控制面这个词听起来像管理后台，实际它是**给模型的一组动词**。四个动词各自很小：查一下、等一下、停一下、说一句。小到可以写进 schema，小到可以被审批规则覆盖，小到模型能自己组合。

组合出来的行为却不小：父可以开三个子、等第一个回来、发现第二个跑偏就停掉、给第三个补一句约束。整个过程没有隐式调度，每一步都在日志里。

下一篇换一个方向：当子代理之间需要互相说话、共享一个工作目录、甚至组成一个团队时，这些动词还够不够用。

上一篇：[13 子代理：从一次工具调用到独立会话](/posts/dsh-subagent-lifecycle.html) ｜ 下一篇：[15 代理团队](/posts/dsh-agent-team.html) ｜ [系列目录](/posts/dsh-architecture-anatomy.html)
