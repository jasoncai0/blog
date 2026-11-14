---
title: "协作工具：todo、提问、呈现与交付物"
date: "2026-11-14T18:00:00+08:00"
summary: "四类不产生副作用的工具如何改变会话：todo 写事件、ask_user 阻塞等人、present 出卡片、deliverables 产出文件。"
description: "拆 todo、user-questions、present、deliverables 的契约：哪些状态持久、哪些只影响 UI、超时与迟到回答怎么处理。"
tags: ["harness", "planning", "collaborative-editing"]
slug: "dsh-collaboration-tools"
---

> 依据：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准；包内 `lib/` 是构建产物，引用一律指向 `src/`。证据等级：源码与 `docs/subsystems/` 逐条对照，运行行为取自包内测试；未跑端到端实验。
> 第 3 季第 4 篇。上一篇 [03 网络工具](/posts/dsh-web-and-browser.html)，本季导览 [dsh-season-three](/posts/dsh-season-three.html)。

一个工具不读文件、不写文件、不发请求，也可能改变整个会话：它写一条事件、挂起一次调用，或者把某个文件的路径登记成交付物。这篇拆的就是这类工具，四个包，四种回执。

先给结论，再逐个拆契约。

{{< fig "diagrams/01-four-tools.svg" "四类协作工具的写入对象、读者与失败方式：todo 与 present 只追加会话事件，ask_user 挂起等待，deliverables 从事件与工作树推导" >}}

## 0. 结论

| 工具 | 持久化了什么 | 谁读 | 失败时模型看到 |
| --- | --- | --- | --- |
| `todo_write` | 一条 `todo/write` 事件，带完整清单 | 投影、面板、恢复后的会话 | `Error:` 加参数校验失败原因 |
| `ask_user_question` | 无（问题本体在工具调用与投影里） | 客户端卡片、`userQuestions` 投影 | `Error:` 加 `NO_PROVIDER`、`CALLER_NOT_LIVE` 之类 |
| `present` | 一条 `deliverables/presented` 事件 | 轮末交付卡片 | `Error:` 加路径或轮次问题 |
| `deliverables` | 无独立登记；读了日志与工作树快照 | 客户端 | 不报错，推导不出来就没有卡片 |

四条支撑结论：

1. `todo/write` 写的是事件，不是内存列表。状态从日志回放得到，面板与恢复后的会话读同一份投影，模型看到的参数与写进日志的载荷是同一个东西。
2. `ask_user` 的阻塞靠 waterfall 加一个 `Promise` 完成，等待期间不占线程；计时模式把「等不到」变成一个明确的 pending 结果，问题继续可答。
3. `present` 不复制文件。它只校验路径、解析成绝对路径、在工具结果成功之后写一条事件；卡片由客户端在轮末根据这条事件渲染。
4. `deliverables` 没有登记入口。一半来自 `present` 声明，一半来自轮内工作树与文件工具编辑的对比，Host 不保存渲染结果。

## 1. 四类工具的共同契约

先看它们的共同点，这决定了它们为什么能放进同一篇文章。

都不对文件系统与网络产生副作用。`todo_write` 追加一条会话事件，`present` 追加一条会话事件，`ask_user` 只等答复，`deliverables` 完全不提供工具。用「副作用」来分类，这四类工具与 `bash`、`write`、`web_fetch` 站在两边：前者的输出是给模型与界面的信息，后者的输出是外部世界的变化。

都要求调用方是一个活着的 agent。`present` 在没有 agent 时抛 `present requires an agent Session`；`todo_write` 在没有 agent 时抛 `todo_write requires an owning agent session`；`ask_user` 在调用方不是活着的根 agent 时抛 `CALLER_NOT_LIVE`，在被另一个 agent 拥有的子 agent 里调用时抛 `DELEGATED_CALLER`。这三条错误属于契约，不是防御性编程：事件要写进某个会话，提问要绑定某个能继续跑的人。

都遵循同一条显示规则：模型可见的内容一定进日志。工具结果进 transcript，界面状态从同一份事件推导，不另存一份快照。于是恢复一个会话时，面板上的 todo、交付卡片、待回答问题都可以从日志重建，不需要额外的状态同步协议。这条规则在第一季的 [会话事件溯源](/posts/dsh-session-event-sourcing.html)里已经拆过，这里看它在四个包里的具体形态。

界面回执是可选的。卡片渲染失败、客户端不认识某个事件、Web 端没有挂载对应组件，工具结果本身仍然完整，模型不会因此拿不到回执。

| 包 | 工具 | 写入位置 | 是否有 `presentCall` |
| --- | --- | --- | --- |
| `packages/todo/tool-todo` | `todo_write` | `todo/write` 事件 + 投影 | 有，通用卡片 |
| `packages/interaction/tool-ask-user` | `ask_user_question` | 无事件 | 无，由问题投影驱动界面 |
| `packages/deliverables/tool-present` | `present` | `deliverables/presented` 事件 | 无，由持久事件驱动 |
| `packages/deliverables/workspace-changes` | 无工具 | `workspace/changes` 事件 | 无 |

## 2. todo：为什么写事件而不是写内存列表

`packages/todo/tool-todo/src/index.ts` 的模块头把设计写得很直白：每次调用把一份完整清单追加到调用方 agent 的会话里，回放时按最后一次覆盖，界面从会话事件渲染。

类型只有两个字段：

```ts
// packages/todo/tool-todo/src/types.ts
export interface TodoItem {
  readonly content: string
  readonly status: 'pending' | 'in_progress' | 'completed'
}
```

没有 id，没有优先级，没有 `activeForm`。原因在文档里：清单只支持整体替换，从未支持增量更新，条目不需要稳定身份。三状态已经覆盖模型与界面需要的全部生命周期。

### 事件与投影

写入走一行 `session.append`：

```ts
// packages/todo/tool-todo/src/index.ts（结构摘录）
exec.agent.session.append('todo/write', { todos })
```

投影把它折成「当前清单」：

```ts
// packages/todo/tool-todo/src/index.ts（结构摘录）
ctx.sessionProjections.register({
  key: 'todos',
  init: () => null,
  apply: (state, event) =>
    event.type === 'todo/write' ? event.data.todos
    : event.type === 'turn/start' ? null
    : state,
  stateVersion: 2,
})
```

{{< fig "diagrams/02-todo-event.svg" "todo_write 的完整路径：校验后整表追加事件，投影折出最新清单给界面与恢复流程，下一次 turn/start 把它清空" >}}

三个细节值得注意。

`init` 返回 `null` 而不是空数组，区分「从没写过」与「写过一份空清单」。客户端对这两个状态的渲染不同：前者不出现面板，后者可能就是用户清空了。

`turn/start` 把清单清空，`turn/end` 不清。一轮结束之后，已完成的清单还留在面板上，用户可以回看这一轮做了什么；下一轮开始才归零。这是一条产品决策，写在投影的 `apply` 里，没有别处可以改。

`stateVersion: 2` 与 `summary: false` 一起写在注册里。清单不进 Session summary 载荷：摘要给模型看的是一段压缩过的历史，todo 是界面状态，混进摘要只会让模型看到一份过期的进度表。模型要看进度，自己调用 `todo_write` 的返回值就够了。

图里画的三个读者对应这三种用途：投影给界面与恢复流程，工具输出给模型，日志给回放。三者读的是同一批字节。

### 校验为什么拒绝重复 content

`toTodoList` 做三件事：trim 后非空、content 不重复、在做的任务数不超过上限。第三条由配置控制：

```ts
// packages/todo/tool-todo/src/index.ts（结构摘录）
if (!allowParallel) {
  throw new Error(`invalid todos: at most one task may be in_progress (got ${active})`)
}
```

`allowParallelInProgress` 是必填的布尔配置，没有默认值。对并发型的 agent（子 agent、后台命令、workflow 的分支）设成 true，描述词会要求标出所有正在做的任务；设成 false 恢复「同时只做一件事」的纪律。没有默认值是有意的：这个值取决于部署里有没有并发，猜错会让模型反复收到校验错误。

重复 content 被拒绝，表面上只是数据卫生，实际绑着一个客户端实现：`TodoPanel.tsx` 用 `key={item.content}` 渲染列表。React 的 key 要求唯一，重复的 content 会让两次渲染复用同一个节点，状态串位。工具层拒绝重复，等于把这个约束前移到模型能收到错误的地方，而不是留给界面静默出错。

### 与 plan mode 的区别

两者都写事件，都不进 transcript，容易混。区别在写法与效果。

| 维度 | `todo/write` | `plan/mode` |
| --- | --- | --- |
| 载荷 | 完整任务清单 | 一个布尔值 `{ active }` |
| 谁写 | 模型的工具调用 | 命令、评审结果、插件 |
| 对提示词的影响 | 无 | 激活期间每个请求都带部署持有的指引 |
| 退出方式 | 下一轮自动清空 | 经用户评审的 `exit_plan_mode` |
| 强制力 | 无 | 无（沙箱与审批另算） |

计划模式是逐 agent 的协作状态，由 `ctx.planMode` 持有，激活时贡献一段提示词，退出要经过 `plan-review` 意图的用户评审。todo 是模型给自己看的进度表，随时整表重写，不改变任何准入规则，也不改变提示词。

> 自己搭 Agent 时：把 todo 存进内存变量是最省事的第一步，代价是恢复会话后进度表消失、面板与日志对不上。存成事件后，回放、fork、压缩三条路径共用一套逻辑，代价只是「整表替换」这一个约束。这个约束比增量更新便宜得多。

## 3. ask_user：一次提问就是一次前台等待

`ask_user_question` 有两种模式，由插件配置选：

```ts
// packages/interaction/tool-ask-user/src/index.ts
export interface Config {
  readonly mode?: 'legacy' | 'timed'   // 默认 legacy
  readonly timeout?: number            // 默认 120 秒，-1 表示无限期
}
```

`legacy` 是阻塞式：工具挂起，直到界面返回一批答案。`timed` 是计时式：在一个有界窗口内等待，窗口到点先返回 pending，问题继续可答。

### 挂起是怎么实现的

服务层的入口是 waterfall：

```ts
// packages/interaction/user-questions/src/index.ts（结构摘录）
return await ctx.waterfall(scope, 'user-questions/request', request, noAnswerer)
```

`noAnswerer` 抛 `NO_PROVIDER`，消息是 `no user-questions answerer accepted the request`。「没人能接这个提问」是一个明确的错误，而不是挂起一辈子。界面客户端监听这个 waterfall，渲染卡片，等用户操作，再调 `answer()` 把批次送回等待点。`ask()` 返回的 `Promise` 在此时 resolve。

等待期间不占线程：挂起的是异步调用，不是事件循环。这一点在多个问题并存时尤其重要，每个调用各自挂在自己的 `Promise` 上，互不阻塞。

### 问题与答案的形状

问题条目的字段可以按用途记：

| 字段 | 作用 |
| --- | --- |
| `id` | 答案批次的对齐键，必须在一次调用内唯一 |
| `question` | 给用户看的正文 |
| `header` | 卡片标题，可选 |
| `options` | 选项列表，每个有 `label` 与可选 `description` |
| `multiSelect` | 是否允许多选 |
| `intent` | 只影响呈现，分 `plan-review` 与 `approval` 两类 |

`intent` 是 tagged union，携带批准与拒绝的标签。客户端用它渲染「批准 / 继续规划」这种专用按钮，不认识的 tag 走通用卡片。它只改呈现，不改协议，服务端校验的仍然是 `label` 是否落在 `options` 里。

答案的形状是 `{ id, selected: string[], custom?: string }`。三条语义写在文档里：单选时 custom 覆盖 selected 且 selected 为空；多选时 custom 补充 selected；selected 为空且没有 custom 表示用户明确跳过这个问题。

第三条是模型最容易读错的。跳过表示用户主动不回答，既不是空白，也不是默认值。工具描述里专门写了一句：

> An answer item with empty selected and no custom means the user skipped that question: continue without it, do not ask it again, and do not treat the skip as permission.

### 谁能提问

`assertLiveRoot` 挡两类调用方：

```ts
// packages/interaction/user-questions/src/index.ts（结构摘录）
if (ctx.agents.get(agent.id) !== agent) throw new UserQuestionError(CALLER_NOT_LIVE, ...)
if (agent.session.header.origin === 'subagent') throw new UserQuestionError(DELEGATED_CALLER, ...)
```

第一条要求传入的 agent 是注册表里活着的同一个实例，克隆出来的、已经释放的都过不去。第二条禁止子 agent 直接向用户提问，错误消息里给了替代方案：把没解决的问题写进子 agent 的最终结果，由父 agent 决定要不要问。

这两条合用把提问收敛到「根 agent 的当前调用栈」上。否则一个 workflow 里十个分支各弹一张卡片，用户面对的是十个互不相关的问题，而模型各自以为自己只在等一个答复。

### 取消的语义

调用方 signal 被 abort 时，`ask()` 抛 `ASK_ABORTED`。计时模式下窗口到点不是取消：`TimedQuestionWait` 到点后把前台等待的 signal abort，`askTimed` 捕获 `ASK_TIMED_OUT` 并返回 `{ pending: true, callId }`，工具结果仍然成功。这一轮不被取消，agent 继续做别的工作。

两件事不要混：窗口超时结束的是「工具这次等多久」，不是「这个问题作废」。取消结束的是整个调用，问题也不会留在可回答集合里，除非它已经进入 `continued` 状态。

> 自己搭 Agent 时：问用户的工具如果不设上界，任务会被一个离开屏幕的人无限挂起。给等待设上界、把超时做成可继续的结果而不是错误，长任务才有机会跑完。超时若直接报错，模型要么原地重试，要么放弃整件事，两种都不好。

## 4. 迟到回答：pending 之后发生什么

计时模式的意义在于给「用户暂时不在」一个可恢复的形态。这条路径的每一步都有明确的落点。

{{< fig "diagrams/03-ask-user-sequence.svg" "提问经 waterfall 到界面，答案按问题 id 对齐回到调用点；超时先返回 pending，迟到回答转成用户消息送进会话" >}}

### 工具结果

窗口到点时，工具返回：

```json
{ "pending": true, "callId": "...", "message": "No answer batch arrived before the timeout. This is pending, not a skipped answer. Continue useful independent work. The user can still answer; their reply will be a user message identified as answer_to_pending_question with this callId and the original questions. Do not treat this as permission." }
```

这段文字要放进结果字段，而不是旁边的散文说明。理由是记录下来的结果文本会被整体当 JSON 读回：投影靠它判断调用仍然可回答，客户端问题行靠它判断记录的是超时而不是一批答案。

### 投影与可回答集合

`userQuestions` 投影把日志折成两个集合：`active` 与 `settled`。原生 `tool/call` 打开一个问题；`tool/result` 只有在带 pending 载荷、或者 Session resume 修复补写的 `TOOL_OUTCOME_UNKNOWN` 合成结果时，才把调用标成 `continued`；回答批次把它连同该批次移进 `settled`，其他结果与失败都直接丢弃。

只有请求头里声明了 `timeout` 参数的 timed schema，投影才跟踪原生调用。阻塞式的 legacy 工具从不声明这个参数，它的调用不会出现在可回答集合里，因为它在等的时候根本不需要迟到回复这条路径。

### 回答如何落地

`answer()` 只接受 `state === 'continued'` 的调用。批次必须与问题 id 一一对应，多一个少一个都抛：

```
the answer batch for <callId> must name each of its N questions exactly once
```

通过之后，服务构造一条用户消息，source 是 `user-question-reply`，正文是结构化载荷 `answer_to_pending_question`，然后 `agent.steer()` 把它送进会话。模型在下一轮看到的是「用户回答了之前那个问题」，而不是工具结果被改写。

这里有一条硬规则：工具结果写入之后不再修改。transcript 是 append-only 的，补写历史会让事件溯源失效。迟到的答案只能作为新消息进入，这也是 `answer_to_pending_question` 载荷存在的原因：模型需要知道这条消息对应哪次提问。

### 多个问题并存

每个调用有自己的 `callId`，`answer()` 按 `callId` 找到对应批次。界面上可以同时存在多张卡片，用户按自己的顺序回答。服务端的约束只有一条：同一调用的第二次提交抛 `REPLY_QUEUED`，消息是 `a reply is already queued for this question`。

排队的回复在获准成为用户消息之前可以撤回，撤回之后问题回到可回答状态。Host 重启后 Session 重开，Remote 层恢复根 agent，之后的迟到回答作为新一轮进入，仍然能对上 `callId`。

> 自己搭 Agent 时：把「用户晚点回答」实现成修改历史，会在 transcript、客户端缓存、回放三处各留一套补丁逻辑。把迟到答案当成新消息，语义边界只剩一条：已经写进去的内容不能再改。这条边界保住了，恢复与 fork 都不用为「半结算的提问」写特例。

## 5. present：把路径变成交付物

`present` 的设计目标写在工具描述里：声明已经存在的文件是最终交付物，宿主可能只在这一轮结束后才展示，所以定稿之后再调；用户需要提前看，就在消息里链接。

它不复制内容。用户打开的是当前路径上的文件，卡片只是入口。这一点决定了后面所有的限制：文件被改，卡片打开的是新内容；文件被删，卡片就打不开了。

### 校验的顺序

```ts
// packages/deliverables/tool-present/src/index.ts（结构摘录）
if (ctx.agents.turnBoundaryOf(session).openTurnStartSeq === null) {
  throw new Error('present requires an open turn')
}
if (files.length < 1 || files.length > maxFiles) {
  throw new Error(`present accepts 1 to ${maxFiles} files`)
}
```

每一层都有对应的失败文案，写在表里更好查：

| 检查 | 失败消息 |
| --- | --- |
| 没有 agent | `present requires an agent Session` |
| 这一轮已经结束 | `present requires an open turn` |
| 文件数量 | `present accepts 1 to 8 files` |
| 空路径 | `present requires a non-empty file path` |
| 不是普通文件 | `Cannot present <path>: not a regular file` |
| 文件不存在 | `Cannot present <path>: file not found. Check the path, create the file if needed, and retry.` |

`present requires an open turn` 这条容易被误读成「不能在最终回复里用」。它的真实含义是：交付物属于某一轮，轮结束之后再声明，卡片没有归属，只能丢。要在一轮内声明，就得在工具循环里调。

路径先按会话当前工作目录解析，再统一成绝对路径存进事件。后续切换目录不会让已交付的条目指错。绝对路径可以指向工作区外，`/tmp`、Downloads 都行；shell 沙箱私有 `/tmp` 里的文件不在会话文件系统里，要先写到能访问的位置。

### 声明什么时候写

`present` 的执行阶段只做校验，把待写的数据挂在 WeakMap 上；事件的写入发生在 `tools/result` 监听器里：

```ts
// packages/deliverables/tool-present/src/index.ts（结构摘录）
ctx.on('tools/result', (exec, result) => {
  const data = pending.get(exec)
  pending.delete(exec)
  if (data === undefined || result.isError) return
  data.session.append('deliverables/presented', {
    turn: data.turn,
    callId: exec.callId,
    files: data.files,
  })
})
```

两件由此可得。失败的调用不产生声明，卡片不会出现一个打不开的文件。嵌套调用各记各的 `exec`，内层工具调用也会触发监听器，所以每个实例只记录自己实际执行过的那次。

用 `WeakMap` 而不是普通 Map，是因为键是工具执行对象，寿命由调用方决定；执行结束、对象被回收，待写数据跟着消失，不需要清理。

### 为什么没有 presentCall

`present` 不定义 `presentCall`、`presentResult`、`presentationMeta`。这三个是 `defineTool` 的可选字段，用法是让工具在调用发生时立刻给出卡片参数；`present` 走的是另一条路：持久事件加客户端轮末渲染。

两条路的差别在时机。`presentCall` 是同步的、无状态的，适合「参数里就够画一张卡」的工具；`present` 的卡片要合并同一轮多次声明、按路径取最新一次、还要在轮末判断哪些是最终交付物，这些都需要跨调用状态。把它做成事件，客户端在轮末自己合并，Host 就不必记住一轮内调了几次。

代价是模型看不到卡片渲染结果。工具结果里只有文件列表，卡片长什么样、有没有渲染成功，模型不知道。这是产品选择：卡片是给人看的东西，模型只需要知道声明成功。

## 6. deliverables：两半推导，一个渲染口

交付物由 deliverables 包组拥有，分两半。

{{< fig "diagrams/04-deliverables-two-halves.svg" "交付物的两半：present 声明的文件与轮内工作树对比，两半都只写会话事件，由客户端在轮末渲染" >}}

### 声明的一半

这一半就是上一节的事件：`deliverables/presented` 带 `turn`、`callId`、`files`。客户端按 turn 累加，同一路径取最新一次声明。

### 推导的一半

另一半完全不依赖模型调用。每一轮开始与结束时各拍一次工作树快照，对比出增删行数，写一条 `workspace/changes` 事件。

实现上有三条线：

git 覆盖得到的改动。每轮开始用临时 index 做 `add --all` 加 `write-tree` 得到基线 tree id，对象目录指向 Session 自己的临时目录，仓库的 index、objects、worktree、refs 一个都不动。轮末再做一次快照，用 `diff-tree -r -M --numstat` 出结果。

git 覆盖不到的文件。文件工具（`write`、`edit`、`apply_patch`、有修改作用的 `str_replace_editor`）在写入前把原文件复制进临时目录，每轮每路径只留第一次；轮末再复制一次，按内容 SHA-1 命名去重。`fs/write-intent`、`fs/edit-intent`、`fs/delete-intent` 三个 waterfall 负责在委托防护之后抓这份副本。

两次都漏掉的路径。只在 shell 里被改动、又不在任何快照覆盖范围内的文件，不会出现在摘要里。这是已知限制，文档里写明了。

事件只带轮号，摘要与对比由 Host 服务按需提供：

```ts
ctx.workspaceChanges.summary(sessionId, seq)
ctx.workspaceChanges.diff(sessionId, seq, index, signal)
```

Session 被释放后，临时目录删除，摘要与对比都取不到。Host 重启后，旧轮既没有卡片也没有对比。

### 配置与上限

`workspace-changes` 的默认值都写在一个配置里：

| 项 | 默认 | 作用 |
| --- | --- | --- |
| `timeoutMs` | 30,000 | 单次 git 命令 |
| `outputMaxBytes` | 8,388,608 | 命令输出上限 |
| `maxFiles` | 500 | 摘要里最多列多少个文件 |
| `maxFileBytes` | 2,097,152 | 单个文件超过就不做逐行对比 |
| `diffTimeoutMs` | 100 | 逐行对比预算，超时退化成整文件替换 |

最后一项有个可观察的后果：逐行对比超时后，文件标记为 `coarse`，客户端只能显示「整个文件变了」，不能显示具体哪几行。对超大生成文件，这是常态。

### 客户端只认三类编辑

`turn-deliverables.ts` 里，produced 路径只来自 `write`、`edit`、`str_replace_editor` 三类 mutation。别的工具改了文件，推导半不会把它算作「这一轮产出的文件」，除非它落在 git 快照里。

畸形事件直接忽略。`isPresentedData` 要求 turn 是正整数、callId 非空、files 是数组；`isPresentedFile` 要求 path 非空。不满足就整条丢弃，界面不崩，也不显示半截卡片。

### 两半互不影响

present 的文件不在仓库里也能显示；工作树对比出来的文件没有人声明也会出现在改动列表。一半失败，另一半照常。合并由客户端在轮末做，Host 不生成最终视图。

> 自己搭 Agent 时：交付物最容易做成「工具里存一份清单」。存清单就要处理文件改名、内容变化、会话恢复三种失效；改成从事件与快照推导，失效面收敛到推导规则一处，代价是每次渲染都要重算。

## 7. 四类失败模式

按「哪一环断掉」列，比按工具列清楚。

| 失败模式 | 具体表现 | 模型看到 | 用户看到 |
| --- | --- | --- | --- |
| 模型看不到回执 | 工具结果没进 transcript，或客户端不认事件 | 什么都没发生，可能重复调用 | 卡片出现两次或没出现 |
| 用户不答 | 阻塞式工具一直挂着；计时式到点返回 pending | 一条 pending 结果，或长时间无结果 | 卡片留在界面上 |
| 卡片渲染失败 | 客户端不认识事件、数据畸形、组件缺失 | 工具结果正常 | 没有卡片，文件本身在磁盘上 |
| 文件没落地 | 路径写错、被沙箱挡住、写完又被删 | `Error:` 加具体原因 | 卡片指向一个打不开的路径 |

第一行在 DSH 里被一条规则挡住：model-visible 与 logged 是同一件事。工具结果进 transcript 是执行管线的一部分，不依赖客户端。会掉的是「界面回执」，它属于可选的第二层。

第二行分两种。阻塞式的等待没有期限，用户不答就一直是 pending 状态，这时候取消工具调用是唯一出路。计时式把「不答」变成一个正常结果，代价是模型必须理解 pending 不是拒绝。

第三行只在客户端成立。工具已经写完了事件，卡片能不能画出来取决于客户端版本与事件形状；老客户端遇到新事件会忽略。

第四行是唯一会让模型看到错误的。路径校验在 Host 侧，`present` 返回 `Error:`，模型有机会改路径重试。

## 8. 设计取舍

取舍一：todo 用整表替换。好处是回放只取最后一次、不用处理增量合并、事件载荷自解释；代价是每次调用都要重发全表，清单很长时 token 开销明显。DSH 选择让模型自己控制清单长度，工具不替它做增量 diff。

取舍二：todo 与 plan mode 分开。好处是进度表可以随便改，计划状态的变更要经过用户；代价是模型要理解两个概念，提示词里得分别解释。合并成一个会让「改一下进度」也变成需要评审的动作。

取舍三：提问有两种模式而不是一种。阻塞式语义干净，适合必答的问题；计时式适合长时间任务，用户可能离开一会儿。两种模式共用一个服务与一套投影，配置只切换工具层的行为，代价是模型要在描述词里区分 pending 与跳过。

取舍四：迟到回答走用户消息。好处是 transcript 保持 append-only，事件溯源不被破坏；代价是模型要在新消息里自己关联 `callId`，而不是从工具结果里直接读到答案。DSH 接受这个代价，因为补写历史会破坏回放。

取舍五：present 不复制文件。好处是零存储开销，打开的是当前版本；代价是文件之后被改、被删、被移走，声明仍然指向原路径。版本化交付需要写时复制存储，这块文档里明确标为延期。

取舍六：交付物没有登记接口。好处是没有第二份真相，present 与工作树对比都只是事件；代价是模型无法主动「登记」一个 git 看不见的文件，只能靠 present 声明。

取舍七：推导半依赖 git。好处是行数统计全、不额外占空间；代价是不在 git 仓库里的会话没有改动列表，只有 shell 改动的文件彻底看不到。文档把这条写成已知限制，没有用文件系统扫描去补。

## 9. 踩坑点

坑 1：以为 `todo/write` 会合并增量。
它只接受整表。少传一条，那条就消失了；传空数组，清单被清空。模型如果按「更新第 2 条」的思路调用，会得到一份只剩一条的清单，而工具不会报错。

坑 2：重复 content 被拒绝，但原因不在服务端。
错误消息只写 `invalid todos: duplicate content <json>`，看代码才知道是为了 `TodoPanel` 的 React key。改客户端 key 之前，先想清楚重复条目在界面上本来也无法区分。

坑 3：把 `turn/start` 清空当成 bug。
跨轮看 todo 面板变空，是投影按 `turn/start` 归零的结果。想在下一轮继续同一张表，只能在下一次 `todo_write` 里重新提交；没有「保留到下一轮」的配置项。

坑 4：给子 agent 挂 `ask_user_question`。
工具会注册成功，调用时报 `DELEGATED_CALLER`。这是服务层的规则，不是工具层的开关，所以在工具配置里找不到对应选项。正确做法是让子 agent 把问题写进最终结果。

坑 5：把计时模式的 pending 当跳过。
pending 的消息里写得很清楚：这不是跳过答案，也不要当成授权。反过来，`{ selected: [], custom: undefined }` 才是明确的跳过。两种结果长得像，语义相反。

坑 6：以为 `answer()` 能改工具结果。
它只能把答案作为新消息送进会话。已经写入的结果不会被修改，界面上也不会出现「工具结果被更新」这种事。

坑 7：在没有打开的轮里调 `present`。
在最终回复之后、或者在一个已经结束的轮里调用，会得到 `present requires an open turn`。交付声明必须发生在工具循环内。

坑 8：把 `workspace/changes` 当文件系统监听。
只有 git 看得见的改动和文件工具的编辑会被记录。脚本里 `mv`、`rm`、`sed -i` 改的文件，如果不在 git 快照里，摘要里就没有。要覆盖这些，得让脚本也走文件工具，或者接受遗漏。

坑 9：以为会话释放后还能取 diff。
`summary()` 与 `diff()` 在 Session 释放后返回 undefined，临时对象目录已经删了。要长期保存改动，得自己导出。

## 10. 源码索引

| 位置 | 证明了什么 |
| --- | --- |
| `packages/todo/tool-todo/src/types.ts` | `TodoItem` 只有 content 与 status，故意最小 |
| `packages/todo/tool-todo/src/index.ts` 模块头 | 整表替换、回放 last-write-wins、非 agent 调用被拒 |
| `packages/todo/tool-todo/src/index.ts` `toTodoList` | 空、重复、多活跃三条校验与原文 |
| `packages/todo/tool-todo/src/index.ts` 投影注册 | `key: 'todos'`、`turn/start` 清空、`stateVersion: 2` |
| `packages/client/ui-conversation/src/client/skeleton/TodoPanel.tsx` | `key={item.content}`，解释重复 content 为何被拒 |
| `packages/interaction/tool-ask-user/src/index.ts` | 两种模式、`legacy` 默认、`timed` 的 timeout 校验 |
| `packages/interaction/tool-ask-user/src/timed.ts` | pending 结果形状与 `pendingNotice` 原文 |
| `packages/interaction/user-questions/src/index.ts` `ask` | waterfall、`NO_PROVIDER`、`EMPTY_QUESTIONS`、intent 校验 |
| `packages/interaction/user-questions/src/index.ts` `answer` | `continued` 才能答、`BAD_ANSWER`、`REPLY_QUEUED`、`steer` |
| `packages/interaction/user-questions/src/index.ts` `askTimed` | `BAD_TIMEOUT`、`DUPLICATE_WAIT`、超时返回 pending |
| `packages/interaction/user-questions/src/index.ts` `assertLiveRoot` | `CALLER_NOT_LIVE` 与 `DELEGATED_CALLER` 原文 |
| `docs/subsystems/user-questions.zh.md` | 答案语义、三种 wait 形态、投影的 active / settled |
| `packages/deliverables/tool-present/src/index.ts` `execute` | 轮次、数量、路径校验与失败文案 |
| `packages/deliverables/tool-present/src/index.ts` `tools/result` | 只在成功时写 `deliverables/presented` |
| `packages/deliverables/tool-present/src/types.ts` | `PresentedFile` 与事件载荷声明 |
| `packages/deliverables/workspace-changes/src/index.ts` | 临时对象目录、轮末 diff、`coarse` 退化 |
| `packages/deliverables/workspace-changes/README.zh.md` | 五个默认值、两条采集线、已知限制 |
| `packages/client/ui-deliverables/src/presented.ts` | `isPresentedData`/`isPresentedFile` 忽略畸形事件 |
| `packages/client/ui-deliverables/src/client/turn-deliverables.ts` | 只认三类编辑 mutation、同路径取最新声明 |
| `packages/deliverables/deliverable-file-references/README.zh.md` | 正文链接文件的提示词规则 |
| `docs/subsystems/todo.zh.md` | 最小字段集、整表替换、事件只写日志 |
| `docs/subsystems/deliverables.zh.md` | 两半结构、事件与 Host 服务的分工 |
| `docs/subsystems/plan.zh.md` | plan mode 的事件、提示词与退出评审 |

## 最后

四类工具把「会话状态」拆成三种可观察对象：模型可见的 transcript、只追加的事件日志、从事件推导的界面状态。三者各有各的读者，谁都不复制谁。

这样拆之后，恢复、fork、压缩三条路径不需要特殊处理：日志在，状态就能重建；卡片没渲染出来，工具结果仍然完整。代价是所有跨调用的展示逻辑都要写成纯函数，放在客户端或投影里，Host 不能留一份「当前 todo 列表」这种便利缓存。

上一篇 [03 网络工具](/posts/dsh-web-and-browser.html)讲的是怎么从外面取信息；这一篇讲的是信息怎么在会话里留下痕迹。两篇共同的规则是模型可见与日志一致，区别只是一个向内、一个向外。人机交互的等待语义在 [human-in-the-loop](/posts/dsh-human-in-the-loop.html)有更完整的讨论，界面层的呈现方式见 [交互面](/posts/dsh-interaction-surfaces.html)。

上一篇：[03 网络工具](/posts/dsh-web-and-browser.html)｜本季导览：[dsh-season-three](/posts/dsh-season-three.html)
