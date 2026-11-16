---
title: "Agent 之间怎么说话：投递、唤醒与结算通知"
date: "2026-11-16T20:15:00+08:00"
summary: "子代理完成后，结果以哪种方式进入父 Agent 的上下文？拆开 send_message、followup、steer、inject 与结算通知，给出三张投递矩阵和一条完整时序，并解释为什么不能统一用 inject。"
description: "DSH 的 agent 间消息：树的命名、send_message 的投递路径、结算通知按父状态三选一、消息的持久化形态、omitClosingText 的失效条件，以及六个容易踩的坑。"
tags: ["multi-agent","harness","architecture"]
slug: "dsh-agent-messaging"
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）。`packages/subagent/*`、`packages/core/agent-loop/*` 为逐行阅读，`types.ts` 的字段按类型原文引用；未做运行时插桩，凡从代码结构推导的结论都在句子里标出。

子代理跑完，结果回到父 Agent。多数人会把这件事想成「往上下文里塞一段文本」，实现里却是一组明确的选择：消息写进哪个队列、要不要唤醒父 Agent、以谁的名义落库。选错任何一个，表现都是「模型看到了，但行为不对」。

这篇按一次结算的顺序拆：消息从哪来、投到哪、凭什么唤醒、落到日志里长什么样。

{{< fig "diagrams/01-tree-and-delivery.svg" "消息只沿树走：`send_message` 找名字，结算通知找父会话；两条路径的投递参数不同。" >}}

## 1. 先确定树：谁能给谁发

DSH 的 agent 协作是一棵树，根是主 Agent，子代理由 `spawn` 或 `fork` 创建。每个成员有两个标识：

- `session.id`：持久身份，跨恢复、跨进程不变。
- `agentAddress(path, id)`：模型面名字，形如 `reviewer/checker`，由路径与 id 拼出。

`list_agents` 返回的就是第二列。`send_message` 的 `agent` 参数只接受这种名字，`ctx.subagents.resolveName(sender, name, signal)` 负责解析；解析失败直接抛错，不会「猜一个最像的」。

树的结构来自 `meta.parentSession` 与创建时的谱系记录。`packages/subagent/subagent/src/manager.ts:563` 的 `sendMessage` 第一件事是 `this.rootOf(sender)`，第二件是拒绝自消息：

```ts
if (sender.id === targetId) {
  throw new SubagentError('send_message requires another agent in the subagent tree', 'UNAUTHORIZED')
}
```

这条限制看起来只是防呆，背后是投递语义：给同一个人发消息没有队列边界，也就没有「下一步领走」的位置。

## 2. `send_message`：消息走的是 steering

工具定义在 `packages/subagent/tool-subagent-control/src/index.ts:44`，描述很短：

```text
Send a message to an agent. A working agent receives it at its next step;
an idle agent starts a new turn with it. Returns delivery confirmation.
```

实现只有四步：

```ts
const sender = exec.agent
const targetId = await ctx.subagents.resolveName(sender, args.agent, exec.signal)
const messageId = await ctx.subagents.sendMessage(sender, targetId, message, { signal: exec.signal })
```

`sendMessage` 构造一条 `agent-message` 消息，然后把投递模式固定为 `steer`：

```ts
return this.deliverToMember(root, targetId, [...message.content], {
  signal: options.signal, delivery: 'steer', source: message.source,
  authorize: () => { this.assertAdmitting(sender) },
  onAccepted: () => { /* 记录 omitClosingText */ },
})
```

`steer` 的语义是「next-step + 唤醒」：目标在跑，就在下一个 step 边界领走；目标空闲，就开一个新 turn，消息在第一步被 claim。所以「工作中下一步收到，空闲时开新轮」来自同一个定义：`steer` 只规定目标队列与唤醒，驱动状态决定它落在哪个边界。

`deliverToMember` 还有一个恢复分支：目标不在内存里（被回收或跨进程），先 `restoreTarget` 把它从会话日志里恢复，再提交消息。这让「给一个已经结束的 continuable 子代理继续派活」成立。

## 3. 结算通知：三条路，由父状态决定

子代理自然结束时，运行时替它发一条通知。构造在 `packages/subagent/subagent/src/continuation-messages.ts:141`，投递在 `manager.ts:1109` 的 `notifySettlement`：

```ts
const parent = this.ctx.agents.get(parentId)
if (parent === undefined || this.rootOf(parent) !== activation.root) return
const message = createSettlementMessage(...)
if (this.closingTeardownFor(parent) !== undefined) {
  parent.inject(message)
  return
}
this.sendWaking(parent, message, parent.status === 'idle' ? 'queue' : 'steer')
```

`sendWaking`（`manager.ts:669`）把两个字符串映射到两个 Agent 方法：

```ts
if (delivery === 'steer') parent.steer(message)
else parent.followup(message)
```

三个分支的差别：

| 父状态 | 调用 | 队列 | 唤醒 | 为什么 |
| --- | --- | --- | --- | --- |
| `idle` | `followup` | `next-turn` | 是 | 让通知独占一轮，不和用户刚发的消息混进同一个 step |
| `running` | `steer` | `next-step` | 是 | 等当前 step 的工具批次结算完，在下一个边界领走 |
| 收尾中 | `inject` | `next-step` | 否 | 不再启动工作，只把事实留在队列里 |

{{< fig "diagrams/02-settlement-branches.svg" "同一个结算事件，三种投递：父空闲走 followup，父运行走 steer，父在收尾走 inject。" >}}

`inject` 在这里是兜底，不是常规路径。它不唤醒，所以父 Agent 不会因为一条「子代理完成」而多跑一轮；这正好符合「父已经在关闭，没有下一步可以接」的状态。

## 4. 为什么不能统一成 inject

把三条路径合并成 `inject` 会同时破坏两件事。

第一件是**推进**。子代理完成常常意味着「有结果要用」：父 Agent 需要在下一次请求里看到它，并据此决定继续、汇总或再派活。`inject` 不唤醒，父空闲时这条消息就停在队列里，直到用户再说一句才被顺带领走。表现是「子代理早跑完了，主 Agent 却像没收到」。

第二件是**边界**。`inject` 的队列是 `next-step`，父在跑时会在下一个 step 领走，看起来没问题；但父空闲时没有「下一个 step」，只有「下一轮」。`followup` 的 `next-turn` 正好补上这个位置，并且保证通知单独成轮：一轮里只有这条通知，模型不会把它和用户的新指令混成同一个请求。

反过来，统一成 `steer` 也有代价：父空闲时 `steer` 会开新 turn，但消息落在 `next-step`，被这一轮的第一步 claim。行为上接近 `followup`，差别在「这一轮如果被 pre-step 拒绝，消息还在不在」。`followup` 的 `next-turn` 在 claim 之前不会被 step 边界消耗，回退空间更大。

{{< fig "diagrams/03-three-queues.svg" "两条队列与三种投递：`next-turn` 只在轮次边界领一条，`next-step` 每个 step 整批领走；唤醒只是打开轮次，不保证消息被看见。" >}}

## 5. 通知里到底写了什么

`createSettlementMessage` 的正文由 `settlementSummary`（`continuation-messages.ts:106`）生成，按子代理的 `stopReason` 分六种口吻：

| `stopReason` | 通知开头 |
| --- | --- |
| `completed` + continuable | `Background subagent X finished and will do no further work unless you send it more.` |
| `completed` + one-shot | `Background subagent X finished. It cannot receive follow-up messages.` |
| `aborted` | `Background subagent X was stopped before it finished.` |
| `max-tokens` | `Background subagent X ran out of room before it finished.` |
| `refusal` | `Background subagent X declined the task.` |
| `error` | `Background subagent X failed before it finished.` |

之后才附内容：

```ts
content: [
  { type: 'text', text: summary },
  ...omitClosingText
    ? [{ type: 'text', text: 'It sent you a message just before finishing, so its closing message is omitted.' }]
    : closingText.length === 0
      ? [{ type: 'text', text: 'It left no closing message.' }]
      : [{ type: 'text', text: 'Its closing message:' }, ...closingText],
  ...structured !== undefined ? [{ type: 'text', text: `Structured result: ${JSON.stringify(structured)}` }] : [],
  ...diagnostic !== undefined ? [{ type: 'text', text: diagnostic }] : [],
]
```

三件事值得注意：

1. 只保留文本块。父提供方可能拒绝非文本 assistant 块，图片与工具结果不随通知走。
2. `stopReason` 是精确枚举，且 `default` 分支把未知结局报成 `ended abnormally`，不会默认成功。
3. `structured` 如果有，用 JSON 附在正文后；这是给模型的，不是给 UI 的。

## 6. `omitClosingText`：一条会被后续动作推翻的标记

子代理结束时如果「最后一个动作是给父发消息」，通知就省略它的 closing text，避免同一段话出现两次。判据是后续事件，时间戳不参与：

```ts
activation.handle.agent.ctx.on('agent/inbox/inserted', wakeSettlement)
activation.handle.agent.ctx.on('agent/inbox/claimed', () => {
  activation.omitClosingText = false
  wakeSettlement()
})
activation.handle.agent.ctx.on('agent/inbox/discarded', wakeSettlement)
activation.handle.agent.ctx.on('session/event', (_session, event) => {
  if (event.type === 'tool/call') activation.omitClosingText = false
})
```

`send_message` 之后只要又发生了一次工具调用，或者又有输入被 claim，标记就撤销。注释里写得很直接：日志里后出现的工具调用说明子代理还做了别的事，closing text 不再等于「刚发的那条消息」。

{{< fig "diagrams/04-omit-closing.svg" "`omitClosingText` 的生命周期：发送时置位，claim、discard、后续 tool/call 都会撤销；只有干净地停在「刚发完消息」才成立。" >}}

## 7. 消息的持久化形态

两条消息都是 `user/message`，差别在 `source`：

```ts
// 子代理主动发
{ kind: 'agent-message', form: 'relay', senderSessionId: sender.id }

// 运行时结算通知
{ kind: 'subagent-settled', form: 'notice', summary, stopReason, senderSessionId: childId }
```

`agent-message` 的正文前面会加一句 `Agent <name> sent a message: `，然后是发送方给的内容；`subagent-settled` 的正文是运行时口吻。类型注释解释了为什么不合并：前者是发送方选择的内容，后者是管理者对「孩子怎么了」的陈述；合在一起会把子代理没说过的话记到它头上。

`form` 是给客户端的渲染提示：`relay` 展开成一条普通消息，`notice` 折成一行通知，点开才看详情。

## 8. 并发与顺序

`send_message` 不在并发安全的白名单里。`manager.ts:903` 的注释说得很清楚：

```text
`send_message` is not concurrency safe, so a later call in its step is logged after it executes.
```

原因是它会改另一个 Agent 的 inbox，并且要维护 `omitClosingText` 这类跨会话状态。工具执行器因此把它当串行调用处理，不会和同批次的其他调用并发。

消息进入 inbox 之后，顺序由队列保证：`next-turn` 在轮次边界只领一条，`next-step` 在每个 step 边界整批领走。结算通知与用户消息可能同时到达，谁先谁后取决于入队顺序，日志里的 `agent/inbox/inserted` 就是顺序证据。

## 9. 失败与边界

| 场景 | 行为 |
| --- | --- |
| 目标不在树里 | `resolveName` 抛错，`send_message` 失败，不投递 |
| 给自己发 | `UNAUTHORIZED`，直接拒绝 |
| 目标已结束但可继续 | `deliverToMember` 先 `restoreTarget` 恢复会话，再投递 |
| 目标是一次性子代理 | 工具描述与 `list_agents` 都会提示「不能继续」；投递被拒绝 |
| 父会话已不在 | `notifySettlement` 静默返回，只留一条 warn 日志 |
| 父在收尾 | `inject`，不唤醒 |
| 子代理在收尾中调用 `send_message` | `assertAdmitting` 抛 `ACTIVATION_CLOSING` |

## 10. 相邻的三个工具

消息只是协作工具之一。同一组包里还有三个，职责边界值得分清：

| 工具 | 权限 | 是否阻塞 | 返回什么 | 副作用 |
| --- | --- | --- | --- | --- |
| `list_agents` | 任意成员 | 否 | 树成员与名字 | 无 |
| `send_message` | 任意成员，除自己 | 否 | `{ messageId }` | 写入目标 inbox，可能唤醒 |
| `interrupt_agent` | 仅祖先 | 否 | `{ accepted: true }` | 请求目标停止当前工作 |
| `wait_agent` | 任意成员 | 是 | 子代理结果 | 无，只等待 |

`interrupt_agent` 的授权由服务端判定，工具本身不加权：

```ts
ctx.subagents.interrupt(targetId, { kind: 'ancestor', agent: caller })
```

它只请求停止，不等停止完成；目标启动的孙代理也不会被一起停掉。要拿结果仍然用 `wait_agent` 或等结算通知。

`wait_agent` 与结算通知是两条路：前者同步取回结果，后者异步写进上下文。两个都用会拿到两份，选一条。

## 11. 一次往返的完整时序

把前面的碎片拼起来，父 Agent 收到子代理结果经过这些步骤：

1. 子代理最后一步产生 `assistant/message`，工具批次结算完毕。
2. `step/end`、`turn/end` 落库，子代理进入 idle。
3. manager 捕获 `terminal`，释放并发槽，触发 `subagent/end` 生命周期事件。
4. `notifySettlement` 用 `createSettlementMessage` 构造通知，按 `omitClosingText` 决定是否附收尾文本。
5. 父状态分支：`idle` 走 `followup`，`running` 走 `steer`，收尾中走 `inject`。
6. 父 driver 被唤醒（`inject` 除外），发 `turn/start`。
7. 轮次边界或 step 边界 claim：`next-turn` 领一条，`next-step` 整批领。
8. `agent/inbox/claimed` 落库；进入 step 的消息在 pre-step 通过后写成 `user/message`。
9. 下一次请求从日志派生历史，通知成为模型输入的一部分。
10. 如果父在收尾，通知留在 `next-step`，直到下一轮开始才可能被领走。

其中第 8 步是分界线：claim 只表示「出队」，`user/message` 才表示「模型将看到」。两者之间还隔着 pre-step 的准入，被拒时消息既不回队也不再出现。

## 12. 设计取舍

| 选择 | 替代方案 | 为什么选现在这个 |
| --- | --- | --- |
| 结算通知按父状态三选一 | 统一 `steer` | 父空闲时通知要独占一轮，避免与用户消息混批 |
| `inject` 只做收尾兜底 | 全部 `inject` | 不唤醒会让结果停在队列里，父 Agent 看起来「没收到」 |
| 通知用运行时口吻 | 直接转发子代理原文 | 子代理可能没留下收尾文本；运行时要说清 stopReason |
| `source` 分 `relay` / `notice` | 都用 `user` | UI 需要区分「谁说的」，审计也要能追到发送方会话 |
| `omitClosingText` 可被后续事件撤销 | 发送时一次判定 | 子代理发完消息还可能继续干活，一次判定会误删结果 |

## 13. 踩坑点

1. 把结算通知当成普通用户消息处理，UI 上会出现「用户说：Background subagent X finished」。按 `source.kind` 分支渲染。
2. 父空闲时用 `inject` 投结果，父不醒，任务停在半路。结果投递必须带唤醒。
3. 父在跑时用 `followup`，通知被推到下一轮，当前这轮已经结束，模型只能下一轮才看到。
4. 以为 `send_message` 会立刻打断对方。它只入队，目标在下一个 step 或下一轮才处理。
5. 给一次性子代理发消息。没有 continuable 能力的子代理不能恢复，投递会失败。
6. 依赖子代理的 closing text 做汇总，却在它发完消息后又有工具调用；`omitClosingText` 会被撤销，通知里没有那段文本。
7. 忘记 `source.senderSessionId`，审计时无法把消息归到具体子会话。
8. 同时用 `wait_agent` 与结算通知，同一份结果进两次上下文。

## 14. 源码索引

| 位置 | 内容 |
| --- | --- |
| `packages/subagent/subagent/src/manager.ts:563` | `sendMessage`：自消息拒绝、构造 `agent-message`、`delivery:'steer'` |
| `packages/subagent/subagent/src/manager.ts:669` | `sendWaking`：`steer` / `followup` 的映射 |
| `packages/subagent/subagent/src/manager.ts:1109` | `notifySettlement`：父状态三选一 |
| `packages/subagent/subagent/src/manager.ts:903` | `send_message` 非并发安全的注释 |
| `packages/subagent/subagent/src/continuation-messages.ts:64` | `createAgentMessage` |
| `packages/subagent/subagent/src/continuation-messages.ts:106` | `settlementSummary` 六种结局 |
| `packages/subagent/subagent/src/continuation-messages.ts:141` | `createSettlementMessage` |
| `packages/subagent/tool-subagent-control/src/index.ts:44` | `send_message` 工具定义 |
| `packages/core/agent-loop/src/agent.ts:179` | `followup` → `next-turn` + 唤醒 |
| `packages/core/agent-loop/src/agent.ts:183` | `steer` → `next-step` + 唤醒 |
| `packages/core/agent-loop/src/agent.ts:187` | `inject` → `next-step`，不唤醒 |
| `packages/core/agent-loop/src/inbox.ts:108` | `claim` 的边界 |
| `packages/subagent/tool-subagent-control/src/index.ts:118` | `interrupt_agent`：仅祖先、不阻塞 |
| `packages/subagent/tool-subagent-control/src/wait-agent.ts:91` | `wait_agent` 的阻塞语义 |
| `packages/subagent/subagent/src/manager.ts:895` | `omitClosingText` 的事件撤销点 |

## 15. 最后

子代理结果回传这件事，拆开只有三个参数：目标队列、是否唤醒、以谁的名义落库。三个参数各有默认值，也各有代价。DSH 把默认值写死在「父状态」上，把名义写进 `source`，剩下的交给日志。

自己搭多 Agent 时，先把这三件事定下来，再写消息格式。顺序反了，后面每加一种消息类型都要重新回答一遍「它该不该唤醒」。

系列导航：[上一季导览](/posts/dsh-season-three.html) · [本季导览](/posts/dsh-season-four.html) · [子代理生命周期](/posts/dsh-subagent-lifecycle.html) · [输入与队列](/posts/dsh-input-inbox.html)
