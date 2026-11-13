---
title: 命令、计划、待办、提问：四个交互面怎么落账
date: 2026-11-13T12:30:00+08:00
summary: 命令直接执行、计划模式写日志、待办整表替换、提问带计时等待。四个入口的状态归属、模型可见性与失败姿态各不相同。
description: 从 /compact 的两条日志、exit_plan_mode 的准入条件、todo/write 的整表替换到 ask_user_question 的 pending 返回，拆开 DSH 的四个交互面，并说明 permission presets 如何把它们捆到同一组旋钮上。
tags: ["harness", "planning", "agent"]
slug: dsh-interaction-surfaces
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准。
> 第二季第 7 篇。上一季：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)；本季导览：[《第二季：把主干两侧补齐》](/posts/dsh-season-two.html)。

在输入框里敲 `/compact`，屏幕上会多出两条记录。翻会话日志，它们叫 `command/run` 与 `command/done`。模型那边什么都没收到：没有新的 `user/message`，也没有 `turn/start`。

计划模式里，`exit_plan_mode` 在你还未进入计划模式时是 `UNKNOWN_TOOL`。提交过一次计划之后，它在本次会话剩下的步骤里一直可用，即使用户已经批准、计划模式已经关掉。

`todo_write` 每次传的是整张清单，条目没有 id。`ask_user_question` 等到超时会返回 `{ pending: true, callId }`，工具调用结束，Turn 继续往下跑。

四个交互面，四种完全不同的落账方式。它们恰好是「用户想介入」的四条路径，也恰好是客户端最容易写错的地方。

## 0. 结论

| 交互面 | 入口 | 状态写在哪 | 模型看到什么 | 失败姿态 |
| --- | --- | --- | --- | --- |
| 命令 | 输入框的 `/name` | `command/run`、`command/done` | 什么都看不到，除非 handler 自己发消息 | 返回 error result，不写日志 |
| 计划 | `/plan`、`exit_plan_mode` | `plan/mode`，仅日志 | 请求里的一段 `plan:policy` | 待生效，或评审失败 |
| 待办 | `todo_write` 工具 | `todo/write`，整表替换 | 工具结果里的计数 | 工具报错，清单不动 |
| 提问 | `ask_user_question` 工具 | 问题集合与结算集合 | 工具结果里的答案 | `pending`、超时或中止 |

三条结论。审批与提问的界面形态在上一季 [《审批与提问：人机回环的三条通道》](/posts/dsh-human-in-the-loop.html) 里已经拆过，这里只讲它们各自的状态与落账。

第一，每个交互面都在「用户动作」与「模型上下文」之间插了一层日志。命令直接调 handler，不经过模型；其余三个都走工具调用，因此天然落在某个 Turn 里。

第二，状态归属决定恢复能力。写在会话日志里的状态，重连、fork、压缩都能重建；放在进程内存里的状态，进程一重启就没了。这也是 todo 为什么必须落成事件。

第三，软指引与硬强制分开。计划模式只改请求文本，沙箱模式与审批策略才是强制的；批准一次操作与回答一个问题，走的是两套服务。

{{< fig "diagrams/01-four-surfaces.svg" "图 1：四个交互面的入口、日志与状态归属。竖线是日志边界，横线是模型可见性。" >}}

## 1. 命令：注册表加直调

### 1.1 一次 `/compact` 的两次 append

命令的生命周期只有两条事件。`packages/interaction/commands/src/types.ts:105` 声明了第一条：

```ts
'command/run': {
  commandId: CommandId
  name: string
  args?: JsonValue
  source: CommandSource
}
```

handler 跑完之后，`settle()` 追加第二条 `command/done`（`types.ts:112`），里面带 `kind`、可选的 `text`、以及可选的 `sourceEventSeq`。两条都是仅记日志的事件，没有 Turn 包裹，也不进 transcript。

顺序很关键：`command/run` 在 handler 调用之前写。进程在 handler 中途崩掉，日志里会留下一条没有 `command/done` 的运行记录，回放时能看出命令被发起过。反过来先执行后记录，崩溃就等于命令从没发生。

### 1.2 解析只认行首

`parseCommand`（`packages/interaction/commands/src/index.ts:125-136`）用一条正则切名字与参数：

```ts
const match = /^\/([a-z][a-z0-9_-]*)(?=$|[\t\n\r ])/u.exec(line)
if (!match) return null
return { name: match[1], rawInput: line.slice(match[0].length) }
```

名字必须小写，允许数字、下划线与连字符。分隔符是空格、制表符或换行，`rawInput` 原样保留分隔符之后的所有内容。因此 `/plan off` 的参数是 `" off"` 还是 `"off"`，取决于 handler 要不要自己 trim。`/plan` 的 handler 第一行就调了 `rawInput.trim()`（`packages/plan/plan-mode/src/index.ts:239`）。

解析发生得比注册表查询早。`parseCommand` 不知道有哪些命令，它只负责把一行文本拆成 `{ name, rawInput }`。名字不存在时 `execute()` 直接返回错误，日志里连 `command/run` 都不会出现。

### 1.3 执行的固定顺序

`execute()`（`index.ts:362` 起）把一次调用压成一条直线：

1. 解析出 `{ name, rawInput }`
2. 用 `view(agent).get(name)` 查注册表
3. 检查 `signal` 是否已中止
4. mint `commandId`
5. 追加 `command/run`
6. 受理或拒绝附件
7. 调 handler
8. `settle()` 追加 `command/done`

第 6 步在 handler 之前。命令没有声明 `input.attachments` 时，带附件的调用直接得到 `/${name} does not accept attachments`，handler 根本不会跑。声明了但组合里没有附件服务，错误是 `attachments are unavailable because no attachment store is composed`。两条错误都在 `command/done` 里落成 `{ kind: 'error' }`。

`recordInput: false` 只影响第 5 步：这类命令的 payload 已经由领域事件拥有，再在 `command/run` 里存一份输入就是重复。`/feedback` 用了这个开关（`packages/feedback/command-feedback/src/index.ts:124`），反馈正文由反馈服务自己记录。

### 1.4 为什么命令不走模型

命令是一个注册表，加一个同步调用。把它做成模型调用的代价很直接：多一次请求、多一段上下文、多一个回合。

还有个更硬的理由：命令必须有确定的回执。`CommandResult` 只有两种形状，成功带可选文本与 `sourceEventSeq`，失败带文本。客户端拿到这个结构就能更新界面，不需要读模型输出、也不需要猜命令有没有生效。

命令与轮次的关系也干净。`command/run` 不创建 Turn，不消耗轮次预算。用户敲 `/plan off` 时期望马上生效：没有开放轮次就当场提交，有开放轮次则排队到下一个 pre-step，回执文本会写明这次是哪种。

### 1.5 客户端怎么合并回执

`command/done` 里的 `sourceEventSeq` 是一个引用：成功时指向产出这次结果的那条领域事件。`/compact` 把 `result.summarySeq` 放了进去（`packages/compaction/command-compact/src/index.ts:72`），节点上因此记着摘要事件的位置。

把命令行与压缩事务合并起来的是 `commandId`。压缩提交时写下的替换检查点携带 `sourceCommandId`，客户端在 `command.ts:80` 把它读出来，`match` 遇到同一个 id 就返回 `{ id, role: 'update' }`（`command.ts:185`）。命令行不是靠相邻位置贴在摘要前面，它由 id 认领。`sourceEventSeq` 留在节点上作为注解，插入位置由检查点的 seq 决定。

`commands/change` 是另一条通道，用于通知注册表变了。它是 emit 通知，观察者抛错会被包含住，不能否决注册。客户端据此刷新命令面板，与执行路径无关。

### 1.6 自己搭的时候

如果你的 agent 有一组「用户随时想按」的动作，先问它们要不要经过模型。凡是不需要模型决策的动作（切换模式、清空上下文、导出、切换模型），让它们走命令注册表，直接对 agent 执行。

实现上有三个建议。命令 ID 由调用方 mint，方便在一次执行里贯穿日志；handler 收 `signal` 并在长操作里检查它；返回值只给结构化结果，别让 UI 去正则匹配人话。

## 2. plan mode：状态在日志里，约束在请求装配里

### 2.1 一条只记日志的事件

`plan/mode` 的定义在 `packages/plan/plan-mode/src/index.ts:53` 做声明合并，payload 只有一个布尔值：

```ts
'plan/mode': { active: boolean }
```

它仅记日志、整值替换、绝不进 transcript。`plan` 这个 session 单元把已提交模式、命令结算结果与最近一次请求头记录的模式折叠在一起，客户端只看到 `{ active, pending }`。

因此 resume、fork、compaction 之后计划模式都还在。压缩把前面的消息压掉了，`plan/mode` 事件还在日志里，折叠结果不变。这里依赖的是 [《模型看见的，必须被记下：会话日志与事件溯源》](/posts/dsh-session-event-sourcing.html) 里的那条规则：只在日志里有的状态，才可能被重建。

### 2.2 选择为什么先挂起

用户在轮次进行中选择进入计划模式，这次选择不能立刻生效。请求可能在选择之前就已经派生，事后再改状态，日志里的事件顺序与实际发给模型的内容就对不上。

`set(agent, active)` 因此返回四种结果之一：

| 返回值 | 含义 |
| --- | --- |
| `committed` | 没有开放轮次，直接 append `plan/mode` |
| `queued` | 有开放轮次，记进 `pendingIntents`，等下一个 pre-step |
| `cancelled` | 待生效的条目被取消 |
| `noop` | 状态没变 |

处于 `queued` 时，`get(agent)` 返回 `{ active, pending? }`。追加发生在下一个被接受的轮内 pre-step，且在请求派生之前。选择不强制续行：用户选了计划模式但没有发消息，下一轮就不会自己开始。pre-step 的位置与轮次边界见 [《一轮到底怎么结束：turn、step 与尝试的三层状态机》](/posts/dsh-turn-step-state-machine.html)。

这条路径有一个已知缺口，源码注释写得很直白：在轮末 pre-step 之后做出的选择，如果进程随即退出，这次选择会丢。补救办法只能是重连后由 UI 再读一次状态。

### 2.3 工具可见性靠准入，不靠隐藏

`exit_plan_mode` 在插件 apply 时就注册进工具表（`index.ts:281`），但对模型的可见性是渐进的。工具 schema 的 presence 检查读 `loggedActive(agent.session)`：计划状态第一次进入激活的那个步骤起，它在本次会话剩下的部分一直可用。

`execute()` 第一行再查一次（`index.ts:305`）：

```ts
if (!this.loggedActive(agent.session)) {
  throw new Error('exit_plan_mode is only available in plan mode')
}
```

这层检查不是防御性代码。工具表在会话中途可以变，缓存过的 schema 可能还留着一个已经不该出现的工具。准入放在执行的第一行，任何调用路径都绕不过去。

进入计划模式之前调用它，模型看到的是 `UNKNOWN_TOOL`，因为那个步骤里它还没进可用集合。

### 2.4 计划本身怎么进请求

计划模式激活期间，每个模型请求包含一段部署持有的 `plan:policy` 指引，注册在 `index.ts:198`，order 50。它的文本回调按当前是否激活返回 section 或空串。

这段指引是软约束。它告诉模型现在处于计划模式、先给方案再动手。强制的限制在别处：沙箱模式限制能写哪里，审批策略限制哪些调用要问人。计划状态不读也不写这两个服务。

### 2.5 退出条件

退出只有一个正式入口：`exit_plan_mode`。它对参数有硬要求，计划必须以 `#` 开头的标题起头（`index.ts:308` 用 `/^#\s+\S/` 校验）。没有标题的调用直接失败。

通过校验后，工具经 user-questions 通道把计划交给用户评审。三条分支：

| 用户动作 | 工具结果 | 之后发生什么 |
| --- | --- | --- |
| 批准 | `{ approved: true }` | 记录一条待生效的退出，静默追加，不叙述 |
| 继续规划（带反馈） | 失败调用，错误文本是反馈 | 计划模式保持，模型继续改 |
| 放弃 | `{ approved: false }` | 结束当前轮，计划模式仍激活到用户下一条消息 |

批准之后不当场退出，理由与 2.2 相同：当前轮已经派生过请求，状态变更要等下一个 pre-step。日志里记录的是一条待生效退出，而不是立即翻转的 `plan/mode`。

评审期间如果交互通道缺失或服务重载，调用失败，不会静默退出。这点看起来苛刻，但它保证了一件事：计划模式只会在有人明确点过批准之后才关闭。

`/plan off` 走另一条路：把状态设为未激活，有开放轮次时同样先进 pending，并取消尚未追加的条目。不带参数的 `/plan` 选择计划模式；带消息的 `/plan <text>` 先选模式，再把文本交给 `agent.steer()`。

{{< fig "diagrams/02-plan-state-machine.svg" "图 2：计划模式的状态机。pending 是「已选择、未追加」，评审通过后同样先进入待生效退出，再由下一个 pre-step 追加。" >}}

### 2.6 自己搭的时候

把「模式」做成日志事件，别做成进程内开关。做法是给模式一个只有几个字段的事件类型，一份折叠函数，读写都经过同一个控制器。

准入检查放在执行入口，不要只靠过滤工具列表。工具列表会被缓存、会被子 agent 继承、会被 replay 用到，任何一处漏掉都是绕过。

## 3. todo：一次写入，投影重建

### 3.1 没有 id 的清单

`TodoItem` 只有两个字段：

```ts
interface TodoItem {
  content: string
  status: 'pending' | 'in_progress' | 'completed'
}
```

没有 id、没有优先级、没有 `activeForm`。源码注释解释了原因：每次写入都是整表替换，条目不需要稳定身份。清单是 last-write-wins 的，谁也不需要追踪「第三条从 pending 变成 completed」。

`todo/write` 事件携带完整列表，仅记日志。写入方是 `todo_write` 工具（`packages/todo/tool-todo/src/index.ts:200`），它要求参数是「The COMPLETE task list, replacing any previous list.」。

工具没有 agent 时直接抛错。清单是 per-agent-session 的状态，没有会话就没有地方挂。

### 3.2 投影怎么折叠

投影注册在 `tool-todo/src/index.ts:120-136`，key 是 `todos`：

```ts
ctx.sessionProjections.register<'todos', TodoItem[] | null>({
  key: 'todos',
  init: () => null,
  apply: (state, event) =>
    event.type === 'todo/write' ? event.data.todos
    : event.type === 'turn/start' ? null
    : state,
  wire: { viewSchema, view: (s) => s, summary: false },
  stateVersion: 2,
})
```

三条规则：写入替换、`turn/start` 清空、其他事件不动。注意 `turn/end` 不在里面。一轮结束时清单保留，用户还能看到刚刚做完的事；下一轮开始时才清空。这是「standing plan」的折叠方式，注释里写得很清楚。

`summary: false` 表示这份投影不进压缩摘要。清单是短期状态，压进摘要里只会占位置。

### 3.3 为什么不是内存列表

做成内存列表的实现最快，也最容易在四个地方翻车。

重连：客户端刷新页面，内存没了，服务端如果也只有内存，清单就消失。落成事件后，`todo/write` 在日志里，重连的 baseline 一带就回来了。

回放：历史会话里那一轮到底计划了什么，只有日志能回答。内存列表只保留最后一次。

压缩：压缩会重写 transcript，但不会删事件。清单可以随时从事件重建，不依赖被压缩掉的那段文本。

多端：两个客户端看同一个会话，各自的组件状态不可信，投影是共享事实。

### 3.4 工具结果与卡片

`todo_write` 的返回值是清单加计数：

```ts
{ todos, counts: { pending, inProgress, completed } }
```

计数同时被渲染成一行文本，模型因此不需要自己数。这一行也是压缩后仍能看见的那部分。

客户端卡片不读这份文本。`todo_write` 的 `presentCall` 返回一个 generic 视图，标题 `Update todo list`，`rawInput` 是传进来的数组；但 `presentCall` 的结果不进浏览器，客户端从 `todos` 投影自己画清单。模型侧的那一行文本与界面上的清单都派生自同一条事件，谁也不解析谁。

{{< fig "diagrams/03-todo-pipeline.svg" "图 3：待办的写入、投影与渲染。表是唯一事实来源，工具结果、模型文本、界面清单都从它派生。" >}}

### 3.5 自己搭的时候

清单类状态一律用「整表替换 + 事件」的写法。别做增量 patch，别给条目加 id 再维护增删改。整表替换的代价是每次多传几十个字，收益是崩溃、乱序、重复投递都不会让状态分叉。

折叠函数里要显式写出重置条件。`turn/start` 清空这条规则如果藏在 UI 里，服务端与客户端就会各算各的。

## 4. ask-user：等待、超时、取消

### 4.1 三种等待

`ctx.userQuestions` 的调用有三种形态，区别在 `wait` 字段：

| 形态 | 参数 | 行为 |
| --- | --- | --- |
| 阻塞 | 无 `wait` | 一直等，直到有人回答或中止 |
| 具名卡片 | `wait: { callId }` | 无限期，卡片常驻，可以稍后回答 |
| 计时前台 | `wait: { callId, timed: true }` | 有 deadline，超时返回 `pending` |

计时形态的返回值是一个联合类型：

```ts
type TimedUserQuestionResult =
  | AskUserQuestionAnswer
  | { pending: true; callId }
```

`pending` 不等于失败。它表示「这次没等到，但问题还挂着」。Turn 不取消，工具结果照常返回，模型可以继续别的步骤。

### 4.2 剩余时间归客户端算

Host 建立 deadline 后不持续推剩余时间。客户端调 `attachWait(agent, callId, signal)`（`packages/interaction/user-questions/src/index.ts:221`）接手这次等待，流里发的是 `{ remainingMs }`，倒计时用客户端自己的时钟走。

`timed-wait.ts:64` 每次 yield 都重算：

```ts
yield { remainingMs: Math.max(0, this.deadline - Date.now()) }
```

焦点、正在输入、用户点了「慢慢回答」，这些都归本地，不进日志。最后一个接手方离开后，Host 恢复原来的 deadline，不会因为客户端关页面就提前超时。

超时的判定在 `timed-wait.ts:47`：没有 claim 且已过 deadline，立即结算。工具侧拿到 `ASK_TIMED_OUT`，错误文本是 `ask_user_question timed out before the user answered`（`index.ts:251`），对外则折成 `{ pending: true, callId }`。

### 4.3 迟到的回答去哪了

问题超时后，投影里的状态从 `open` 变成 `continued`。此时再回答，不再改工具返回值，改为往 agent 排一条用户消息。

`answer` Remote 方法往 agent 排一条消息，source 是 `user-question-reply`，outcome 是 `answered`，正文带一个固定标记 `answer_to_pending_question`。这条消息获准成为 `user/message` 时，问题才移进 `settled`。

因此计时调用的工具结果里记的是超时，而 transcript 里那一行显示的是用户最终选了什么。两个事实都真，分别属于不同的记录位置。

排队中的回复可以在准入前取消。重复提交被 `REPLY_QUEUED` 拒绝，批次漏题或重题被 `BAD_ANSWER` 拒绝。其他失败码还有 `EMPTY_QUESTIONS`、`BAD_INTENT`、`NO_PROVIDER`、`ASK_ABORTED`、`ASK_TIMED_OUT`。

### 4.4 与 approval 的边界

`ctx.approval` 回答一个问题：这个具体操作能不能继续。它的结果只有四种，`allowed-once`、`rejected`、`cancelled`、`unavailable`，调用方对后三种一律按拒绝处理。

`ctx.userQuestions` 回答的是内容：选哪个方案、计划行不行、要不要继续。答案是一组选项加自定义文本。

两者的接口刻意不同。`ApprovalRequest` 不带工具参数，应答者用 `callId` 把提示挂到已经流式过来的那次调用上。userQuestions 必须自带问题文本与选项，因为它要展示的东西不在工具调用里。

审批策略 `ask` 在没人应答时 fail-closed 到 `unavailable`；`never` 在服务内部、waterfall 之前就返回 `rejected`，后注册的应答者也绕不过。

审批的界面通道可以复用提问组件：`AskUserQuestionIntent` 里有一个 `kind: 'approval'` 的 tag，只改呈现，协议与答案编码不变。计划评审用的是另一个 tag `plan-review`，同样只改呈现。

附一条实现细节：`userQuestions` 投影只折叠「问题集合」与「已结算集合」，等待、焦点、剩余时间都不进投影。这些是界面状态，断线重连后由客户端重新接手，不需要持久化。

只有当工具的 `request/header` 里声明了 `timeout` 参数，投影才跟踪这次调用的计时状态。阻塞式的旧工具从不声明这个参数，因此不会被误判成计时调用。

这条规则让「是否计时」只由请求本身决定，不依赖客户端是否在线。

### 4.5 自己搭的时候

给「等人」设计状态时，先把三种结局写出来：有人答、超时、被取消。三者都要有确定的结果类型，别用异常代替。

超时之后问题不能消失。留一个 `continued` 状态，让迟到的回答还能进日志，否则用户点了选项却什么都没发生。

倒计时交给客户端算，服务端只存 deadline。服务端每秒推一次剩余时间，既费带宽又会在断线时卡住。

{{< fig "diagrams/04-ask-user-timing.svg" "图 4：一次计时提问的三条结局。回答走返回值，超时走 pending，迟到回答走一条排队用户消息。" >}}

## 5. permission presets：把两个旋钮捆成一个选项

### 5.1 两个独立的强制点

`ctx.permissionPresets` 把两个 knob 捆成具名预设：沙箱模式 `sandbox/mode` 与审批策略 `approval/policy`。客户端因此只显示一个 Permissions 选择器，而不是两个下拉框。

默认表有两条：

| 预设 | 沙箱 | 审批 |
| --- | --- | --- |
| `workspace-write` | `workspace-write` | `ask` |
| `danger-full-access` | `danger-full-access` | `never` |

`custom` 与 `auto` 是保留名，配置它们会在插件加载时抛异常。

`auto` 由 `registerAuto(admit)` 注册，固定在 `danger-full-access` 加 `ask` 的组合上，排在配置预设之后。它只对当前会话有效，effect dispose 就消失，也绝不进 `permission.defaultPreset` 的 schema。

### 5.2 custom 是算出来的

`current(session)` 不读某个「当前预设」字段，它折叠沙箱模式、审批策略与已记录的选择，再反查哪条预设匹配。优先取仍匹配的那个选择，其次第一条匹配的配置条目，都不匹配时返回 `custom`。

`custom` 可以显示为当前值，但它不是切换目标，也不会出现在任何事件 payload 里。用户手动改了沙箱模式，预设就自动变成 `custom`，不需要额外写一条「预设失效」的事件。

### 5.3 切换时写什么

`set(session, name)` 的顺序是固定的：

1. 解析预设，未知名称抛异常
2. 如果是 `auto`，先跑准入
3. 若该预设还不是当前生效值，追加 `permission/preset`
4. 调 `setSandboxMode`
5. 调 `setApprovalPolicy`

第 4、5 步只在生效值真的变化时才写。同一轮里，选择事件排在两个旋钮事件之前。

多写一条 `permission/preset` 的理由是：两个预设可能共享同一组旋钮值。只看 `sandbox/mode` 与 `approval/policy`，分不出用户点的是 `workspace-write` 还是另一个同值的自定义预设。这条事件记录的是意图。

它是仅记日志的，不进 transcript，也不影响模型。

### 5.4 与四个交互面的关系

预设只碰两个服务，不碰命令、计划与待办。计划模式是否激活与预设无关；审批策略影响的是 approval 的判定路径；提问通道是 approval 的一种呈现方式，但不改它的结果语义。

因此调整预设不会让计划模式失效，也不会清空待办。反过来，进入计划模式不会放宽沙箱。

## 6. 设计取舍

一，命令不做成工具。代价是命令注册表要单独维护一套 UI 发现机制，收益是执行确定、回执结构化、不占上下文。命令参数不做 schema 校验，只有 `input.hint` 给输入框做提示，校验责任在 handler。

二，计划状态不进 transcript，只留 policy 段。代价是模型看不到「我之前被拒绝过两次」，收益是压缩与 fork 不会重复注入同一段话。policy 段每次请求重新拼，代价是那几十个 token。

三，退出计划模式要重新走一次评审。代价是多一次往返，收益是「模型自己说计划做完了」与「用户同意计划做完了」被区分开。放弃时结束当前轮，也是为了让用户的下一条消息决定方向。

四，todo 用整表替换。代价是长清单每次全量传，收益是没有 id、没有 diff、没有乱序合并。`turn/start` 清空这条规则牺牲了跨轮的长期清单，换来「一次只专注一轮」的语义。需要长期清单的场景应该用 goal 或记忆，而不是把 todo 拉长。

五，提问超时返回 `pending` 而不是抛错。代价是调用方必须处理一个三态结果，收益是 Turn 不被一次没人应答的提问卡死。这个取舍只在「问题可以稍后回答」时成立；阻塞形态仍然是真等。

## 7. 踩坑点

1. `command/run` 写在 handler 之前。想改成「成功才记录」的话，崩溃恢复就分不清「没执行」与「执行了没记」。
2. `parseCommand` 不 trim `rawInput`。`/plan off` 的 `rawInput` 带前导空格，handler 不处理就会把 `" off"` 当成未知子命令。
3. `plan/mode` 的 pending 是进程内状态。进程在轮末退出会丢选择，重连后要靠读取服务端状态补回来。
4. `exit_plan_mode` 的标题校验是 `/^#\s+\S/`。计划必须以一级标题加空格加内容起头；`##` 开头会被拦下，纯文本更过不了，错误信息里得说清楚要什么格式。
5. `todo/write` 的投影在 `turn/start` 清空。如果客户端自己缓存了清单，会看到「服务端空了、界面还在」的错位。
6. 提问超时后工具结果记的是超时。界面要显示用户的最终选择，得从 `settled` 里读，不能读工具结果。
7. `approval` 的 `never` 在服务内部短路。用 prepend 注册一个应答者试图覆盖它，不会生效。
8. `permission/preset` 与两个旋钮事件同轮出现时，顺序是选择在前。客户端按到达顺序合并，反了就会闪一下 `custom`。

## 8. 源码索引

| 路径 | 看什么 |
| --- | --- |
| `packages/interaction/commands/src/index.ts` | `parseCommand` :125、`execute` :362、`settle` |
| `packages/interaction/commands/src/types.ts` | `command/run` :105、`command/done` :112 |
| `packages/plan/plan-mode/src/index.ts` | `plan/mode` :53、policy :198、`exit_plan_mode` :281、准入 :305、标题校验 :308 |
| `docs/subsystems/plan.zh.md` | 计划模式的完整语义与待生效规则 |
| `docs/subsystems/commands.zh.md` | 命令注册表、回执与附件规则 |
| `packages/todo/tool-todo/src/index.ts` | 投影 :120-136、工具 :200、`presentCall` :211 |
| `packages/interaction/user-questions/src/index.ts` | `attachWait` :221、超时 :251、`ask` :256 |
| `packages/interaction/user-questions/src/timed-wait.ts` | deadline :37、超时结算 :47、剩余时间 :64 |
| `packages/interaction/user-approval/src/index.ts` | 应答者 waterfall、`ask` 与 `never` 的短路 |
| `packages/interaction/permission-presets/src/index.ts` | `current` 的派生、`set` 的写入顺序 |
| `packages/interaction/tool-ask-user/src/index.ts` | 工具 schema 与 `pending` 结果的组装 |
| `docs/subsystems/user-questions.zh.md` | 三种等待、迟到回答、错误码 |
| `docs/subsystems/approval.zh.md` | 审批的四态结果与审计事件 |
| `docs/subsystems/todo.zh.md` | 整表替换与「无 id」的理由 |
| `docs/subsystems/permission-presets.zh.md` | 预设表、`custom` 与 `auto` |

## 最后

四个交互面各自解决一类介入：立刻执行、先看方案、记录进度、停下来问。它们的共同点是都把用户动作落成日志里的一条记录，而不是只在内存里改一个变量。

第二季的下一篇会往上走一层，看这些交互面在 Host 侧是怎么被组合进同一个进程的。

