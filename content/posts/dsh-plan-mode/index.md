---
title: "计划模式：一段提示词、一个条件工具、一条等下一步的状态"
date: "2026-11-17T11:40:00+08:00"
summary: "plan mode 是记进日志的会话状态：命令选择先进待生效队列，由下一个被接受的 pre-step 追加；激活期间每次组装都带 plan:policy；exit_plan_mode 只在选中过计划模式的会话里准入，退出要过人工评审。文末对比 todo_write。"
description: "DSH plan mode 深挖：plan/mode 事件与 projection fold、待生效选择与 open turn、plan:policy 段落 order 500、exit_plan_mode 的准入与评审、/plan 三个形态、恢复与压缩，以及和 todo_write 的机制差别。"
tags: ["planning","harness","architecture"]
slug: "dsh-plan-mode"
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）。源码以 `packages/plan/plan-mode/src/index.ts`、`packages/core/system-prompt/src/index.ts` 与 `docs/subsystems/plan.zh.md` 为准；默认 `section` 文本与 `exit_plan_mode` 的入参 schema 在桌面版内置包里核对过；未做真实评审交互的端到端实验，涉及 UI 行为的部分标了推断。

计划模式常被当成一个「禁止改文件」的开关。DSH 的实现比这轻：它只做三件事，往系统提示词里加一段部署自定的文本、让一个退出工具在特定会话里可用、把选择写进会话日志。

限制文件写入不在它的职责里。沙箱与审批各自强制自己的边界，两者都不读计划状态。理解这一点，后面所有看似绕的设计都能对上：为什么选择要等下一步、为什么退出要人工批准、为什么工具准入比模式本身活得更久。

{{< fig "diagrams/01-selection-paths.svg" "两种选择路径：不在轮内立即落库并注入通知；轮内先挂起，等下一个被接受的 pre-step 写入，再随该步的请求生效。" >}}

## 1. 状态只有一条日志事件

`plan/mode` 是会话事件，载荷 `{ active: boolean }`，整值替换。它持久、可回放，但不进模型 transcript。

`plan` 这个 projection 把四类事件折成一个单元状态：

| 字段 | 含义 |
| --- | --- |
| `active` | 已提交的模式 |
| `wanted` | 命令结算后想要的目标 |
| `running` | 正在执行的 `/plan` 命令与其目标 |
| `activeAtLastHeader` | 最近一次 `request/header` 时的模式 |
| `entered` | 这个会话是否进入过计划模式 |

折叠规则很窄：`command/run`（名字为 `plan`）记下 `running`；`command/done` 若成功且目标与当前不同，写入 `wanted`；`plan/mode` 提交 `active`、清空 `wanted`、把 `entered` 置为「曾经激活」；`request/header` 把当时的 `active` 存进 `activeAtLastHeader`。

对外只暴露 `{ active, pending }`。`pending` 由 `wanted` 或运行中的命令目标与 `active` 比较得出，客户端不需要知道内部五个字段。

这条 projection 的 `stateVersion` 是 4。版本号存在的意义是：字段增删后旧快照不能悄悄按新结构解码，要么迁移，要么丢弃重建。

## 2. 选择为什么不能立刻生效

会话事件必须落在某个 turn 里。用户在空闲时敲 `/plan`，此时没有打开的 turn；在模型跑着的时候敲，turn 是开着的。两条路径因此不同：

- 无打开的 turn：`set()` 直接 `session.append('plan/mode', …)`，提交后立刻可见。若最近一次请求头描述的是另一种状态，还会 `agent.inject()` 一条 `plan-mode/notice`，告诉模型「用户切换了模式」。
- 有打开的 turn：写入 `pendingIntents`，等下一个被接受的 `agent/pre-step`。监听器先 `await next()`，只有下游接受该步（不是 reject、没有 abort）才追加。追加失败只记 warn，选择继续等待，绝不因为一次写入失败阻塞整轮。

「等下一步」有唯一正确的位置：请求组装发生在 pre-step 之后，只有在那之前写入，`plan:policy` 才能和这一段状态同属一个请求。写在请求组装之后，模型会带着旧模式跑完这一步，日志却已经显示新模式。

判断「有没有打开的 turn」不看 `agent.status`，看 `turnBoundaryOf(session).openTurnStartSeq !== null`。原因是 turn 关闭后还有收尾工作，此时 status 仍是 `running`，但不会再有任何 in-turn pre-step；用 status 判断会把选择永远挂起。

`set()` 的四个返回值覆盖了所有情况：

| 返回 | 场景 |
| --- | --- |
| `committed` | 无打开的 turn，已经落库 |
| `queued` | 有打开的 turn，已挂起 |
| `cancelled` | 目标与已提交状态相同，只是取消了相反方向的挂起 |
| `noop` | 目标与当前（含挂起）一致 |

挂起的选择只在进程内。进程在下一个被接受的 pre-step 之前退出，这次选择就丢了；日志里没有它，恢复也读不回来。

{{< fig "diagrams/02-mode-lifecycle.svg" "模式生命周期：inactive 与 active 之间隔着 pending 挂起态；取消挂起回到原态，noop 不产生事件。" >}}

## 3. 模型看到的那段

激活期间，`plan:policy` 段落参与每次系统提示词组装，位置由 `getSectionOrder('PLAN_POLICY')` 给出，常量值是 500。

段落文本来自部署配置：

```ts
interface PlanModeConfig {
  section: string
}
```

配置校验是严格的：缺字段、空字符串、非字符串、出现未知键，都在插件加载时失败。这样部署不会带着一段空指引上线，误以为计划模式生效了。

段落是一个惰性函数：组装时读 `selectedActive(session)`，激活返回配置文本，未激活返回空串。空串段落在渲染时被过滤，所以未激活的会话不会多出一个空段。

部署默认配置里的这一段，就是模型实际读到的全部内容：

> You are in plan mode. Stay in plan mode until the user approves your plan through exit_plan_mode or switches the session mode. Imperative language to implement changes means plan the implementation, not execute it. A user's conversational agreement — including an answer confirming something you asked — approves nothing and does not end plan mode; fold the confirmed decision into the plan and submit it through exit_plan_mode.
>
> The tool catalog stays the same across modes for request-cache stability. These plan-mode rules override any later tool description or guidance that suggests using mutation tools; those tools remain listed only to keep the request shape stable. Do not use todo_write to track this planning phase: it tracks implementation after an approved plan, while the plan itself belongs in exit_plan_mode.
>
> When ready, call exit_plan_mode with the complete plan markdown, starting with a # title. Make exit_plan_mode the only and final tool call in that assistant response: it presents the plan for approval, and implementation begins only in a later step after approval. Do not paste the final plan as a plain reply or ask "should I proceed?" through prose or ask_user_question. If review rejects it, incorporate the feedback and present again. If the review channel is unavailable or aborted, stay in plan mode and ask the user to switch modes manually; do not proceed with implementation.

其余三段要求先做只读探索、自己查事实别问用户、把计划写到决策完整：目标与验收标准、按子系统分组、公开 API 与数据流、边界与失败模式。

这段文本在请求里没有任何结构标记。组装时 section 的名字被丢掉，只留文本；非空 section 用空行拼成一整串系统提示词。模型收到的是普通散文，没有段落标签、没有单独的 role 消息、没有 plan 字段。

顺序 500 说明它压在哪：前面是 `DEPLOYMENT_PERSONA_PREFIX`（0），后面是 `TEAM_POLICY`（600）和全部工具指引（`TOOL_BASH` 1000 起）。它排在每条工具描述之前，所以那段文本有资格写「override any later tool description」。

模型侧没有按模式的特殊处理：没有 `tool_choice` 强制、没有 logit bias、没有专用采样参数。真正起作用的只有三点：它位于 system 消息里、措辞是命令式、同一请求的工具表多了 `exit_plan_mode`。模型有没有照做，DSH 不检查，也不重试；兜底在沙箱与审批，见第 8 节。

一个副作用是缓存：切换模式会改系统提示词前缀，切换后第一次请求的提示词前缀缓存失效。这是推断，代码没有按模式分片缓存。

因为它在系统提示词里，压缩永远碰不到它。压缩只替换 surface 上的消息节点，节点 0 是系统提示词，折叠器拒绝覆盖节点 0 的替换。

这里也是「软约束」的来源：它只改变模型读到的指令。模型仍然能调用写文件的工具，工具也不会因为 `active` 而失败。拦写入靠沙箱与审批，这段文本做不到。

## 4. 退出工具的准入比模式活得久

`exit_plan_mode` 的注册带一个 `when`：

```ts
when: ({ agent }) => this.selectedActive(agent.session) || this.planState(agent.session).entered
```

两个条件取或。`selectedActive` 是「选中状态」（挂起优先，否则已提交），`entered` 是「曾经进入过」。第二个条件让准入在整个会话余下部分保持，即使模式已经退出。

为什么不让它随模式一起消失？因为工具声明与工具执行是两套检查：

- 声明由 `when` 在步骤准备时求值，决定这一步的 schema 里有没有这个工具。
- 执行由 `tools.execute` 时的已准入集合决定，同一份集合也用于 PTC 子调用。

如果退出后工具立刻从 schema 消失，模型在退出后的下一步就看不到它；但「退出」本身是一次工具调用，结果要回到模型。更麻烦的是 PTC：`run_code` 里已经写好的子调用在批次结算时仍要执行，准入集合必须在整个批次里稳定。用 `entered` 兜住，结果是「工具一直在，但只在计划过模式的会话里在」。

工具体内还有一道检查：`loggedActive`，看的是已提交状态，不是选中状态。挂起退出期间调用它，会因为「已经不在计划模式」而失败。这个错误是刻意的，它告诉模型：退出已经批准，不要再提交一份计划。

第一次进入计划模式之前，这个工具不在声明里，模型调用它得到 `UNKNOWN_TOOL`。这个错误码与「工具不存在」不可区分，属于有意的等价。

## 5. 退出要过一个人

调用要求一份完整的 markdown 计划，且以 `#` 标题开头。校验只看首行：`/^#\s+\S/`，空计划或没有标题都直接失败。

入参只有一个必填字符串字段 `plan`，`additionalProperties: false`。计划是自由文本，没有步骤数组、没有依赖边、没有并发标记。提示词还要求它必须是该响应里唯一且最后一个工具调用，所以提交计划这一步不会和别的调用并发；想表达「第 2、3 步可以并行」，只能在 markdown 里写给人看。

模型返回的是一次普通 assistant 响应，里面一个标准 tool call：

```
exit_plan_mode({"plan": "# 重构登录重试\n## 目标..."})
```

没有 plan 类型的消息，也没有单独的 plan 字段。计划本身不产生事件：日志只追加 `plan/mode`，计划原文留在 tool call 的参数和 tool result 里，恢复与 fork 靠 transcript 回放带回来。

通过校验后，它走用户交互 seam（`ctx.get('userQuestions')`）：

{{< fig "diagrams/03-exit-review.svg" "一次退出评审：工具提交计划、界面给出两个选项、批准后记一个静默的待生效退出，由下一个被接受的 pre-step 追加。" >}}

- 批准：返回 `{ approved: true }`，同时把 `{ active: false, narrate: false }` 放进挂起队列。`narrate: false` 表示不再追加用户切换通知，因为工具结果本身已经叙述了这次转换。
- 继续规划：抛错，错误文本带上用户反馈（若有），模型据此修订并再次提交。
- 放弃评审：捕获 `ASK_CANCELLED`，`exec.concludeTurn()` 结束当前轮，返回 `{ approved: false }`。用户拿回话轮去说自己想说的话，不浪费一次模型请求。
- 没有交互通道：抛错，提示改用会话级切换。不会静默退出。
- 评审期间插件被重载：抛错，要求重新提交。否则批准的选择没有监听器去追加，会永远挂起。

批准之后，计划指引在这批工具调用剩余部分仍然有效。原因是 pending 选择要等下一个被接受的 pre-step 才写入，而当前 step 的请求已经组装完了。工具结果告诉模型「已批准，从下一步开始执行」，提示词则在下一步才切换。

## 6. `/plan` 的三个形态

命令注册在 `ctx.commands` 组合存在时生效，名字 `plan`，输入提示 `[off|message]`。

| 输入 | 行为 |
| --- | --- |
| `/plan` | 选择计划模式 |
| `/plan off` | 选择默认模式；若存在相反方向的挂起，直接取消 |
| `/plan 先读 src/app.ts` | 先选择计划模式，再用 `agent.steer()` 把文本作为普通用户消息提交 |

`off` 带附件会直接报错。其余文本带附件时，附件与文本一起进入那条 steer 消息。

`/plan off` 的返回文案区分三种状态：已提交、排队等下一步、取消了进入计划模式的挂起。重复执行 `off` 时，如果日志里仍是激活状态，返回「正在退出」而不是「已经不激活」，避免让用户以为切换失败。

命令的结算也会进日志：`command/run` 与 `command/done` 让 projection 在命令执行期间显示 `pending`，即使 `plan/mode` 还没写。

## 7. 恢复、fork 与压缩

`plan/mode` 是普通会话事件，所以：

- 恢复会话：从日志回放出 `active`，后续请求立刻带指引。
- fork：切点之前的 `plan/mode` 一起复制，子会话继承模式。
- 压缩：`plan:policy` 在系统提示词里，不受影响；`plan/mode` 是日志事件，也不会被摘要替换。

会丢的只有挂起的选择：它在 `WeakMap` 里，不是日志的一部分。恢复后 `get()` 只返回已提交状态。

还有一个边界：projection 未就绪时读状态会抛错，阻止 prompt 组装。调用方必须在跑 Agent 之前等插件激活完成。

## 8. 为什么不做成硬约束

硬约束需要回答「什么算写入」。改文件、跑 shell、发 HTTP、写数据库，边界各不相同；在 Agent 层面判断意图，误伤率高于收益。DSH 把这层留给沙箱与审批，它们按能力而不是按意图工作。

代价是模型可能无视指引。实际缓解靠三处：提示词明确要求先给计划、退出必须人工批准、工具结果把「已批准」与「继续规划」的语义写清楚。指引被无视时，用户看到的是一次没有计划的工具调用，可以在审批层拦下。

| 取舍 | 选择 | 代价 |
| --- | --- | --- |
| 强制力 | 只改提示词 | 模型可能绕过，需要沙箱/审批兜底 |
| 生效时机 | 等下一个被接受的 pre-step | 当前批次仍按旧模式运行 |
| 工具准入 | 用「曾经进入」兜住整个会话 | 退出后工具仍在 schema 里 |
| 评审通道 | 必须有 userQuestions | 无通道时退出失败 |
| 状态存储 | 只记日志，挂起在内存 | 进程退出会丢挂起选择 |

## 9. plan mode 与 todo_write 的差别

{{< fig "diagrams/04-plan-vs-todo.svg" "两者都叫计划，但一个改变模型收到的指令与工具准入，另一个只写一条事件供 UI 与回放消费。" >}}

| 维度 | plan mode | `todo_write` |
| --- | --- | --- |
| 事件 | `plan/mode`，`{ active }` | `todo/write`，整张列表 |
| 投影 | `plan`，五个字段，对外 `{active,pending}` | `todos`，`turn/start` 清空 |
| 是否进提示词 | 激活时注入 `plan:policy`（order 500） | 不进；模型只看到工具结果 |
| 是否改工具面 | 只加不减：首次进入时加入 `exit_plan_mode`，之后常驻；写工具不因模式消失 | 否，常驻工具 |
| 数据形状 | `plan` 一个 markdown 字符串 | `todos` 扁平数组，每项 `{content, status}` |
| 依赖与并发 | 无，DAG 只写在文字里 | 无，`allowParallelInProgress` 只管几个 `in_progress` |
| 谁触发 | 用户 `/plan`，或模型提交计划后经人批准 | 模型自己 |
| 生命周期 | 跨轮持续，直到退出 | 下一轮清空投影，列表随写覆盖 |
| 强制力 | 软约束，不拦工具 | 无，纯记账 |
| 失败模式 | 忘记退出、把提交当批准 | 只写不更新、清单与进度漂移 |

四点展开：

1. 上下文位置不同。plan mode 的文本进系统提示词，参与每一轮组装；todo 的内容只以工具结果形式出现在历史里。前者改变模型怎么想，后者记录模型做了什么。
2. 状态机不同。plan mode 有挂起态、有准入、有评审；todo 只有「写」与「清空」。这也是为什么 plan mode 的代码要处理 open turn，而 todo 不需要。
3. 责任不同。两者都不负责验收。plan mode 保证范围先被同意，todo 保证进度可见；交付是否正确仍看测试、产物与评审。
4. 数据形状不同。计划是一个字符串参数，清单是扁平数组，两边都没有依赖边。要真正的并发编排，用 workflow 或 subagent，这两个工具都不做调度。

判断方法：缺「该不该现在动手」用 plan mode；缺「拆没拆清楚、做到哪」用 `todo_write`。两个都缺就都用，它们不互斥。

再加一层：需要并行、或需要盲评时才会用到 team。三者的判据与作用域，见 [Team 那篇](/posts/dsh-agent-team.html) 第 11 节。

## 10. 踩坑点

1. 把 plan mode 当权限控制。它只改提示词，限制在沙箱与审批；进入后写工具仍在 schema 里，提示词只是要求别用。
2. 在轮内切换后立刻期待模型按新模式行动。挂起要等下一个被接受的 pre-step。
3. 用 `agent.status` 判断有没有打开的 turn。收尾阶段 status 仍是 running，但不会再跑 pre-step。
4. 把「提交了计划」当成「计划已批准」。批准只发生在评审返回 `{approved:true}` 之后。
5. 在退出批准后立刻再调 `exit_plan_mode`。工具体内看已提交状态，会抛错。
6. 依赖工具在退出后从 schema 消失。`entered` 让它留到会话结束。
7. 在无 `userQuestions` 的嵌入环境里依赖 `exit_plan_mode` 退出。它会失败，只能用会话级切换。
8. 以为挂起选择会被持久化。它只在内存，进程退出即丢。

## 11. 源码索引

| 位置 | 内容 |
| --- | --- |
| `packages/plan/plan-mode/src/index.ts:118` | projection 状态 schema 与 `stateVersion: 4` |
| `packages/plan/plan-mode/src/index.ts:139` | `apply`：command/run、command/done、plan/mode、request/header 四条折叠 |
| `packages/plan/plan-mode/src/index.ts:197` | `plan:policy` 段落注册与 `getSectionOrder('PLAN_POLICY')` |
| `packages/core/system-prompt/src/index.ts:139` | `PLAN_POLICY: 500` |
| `packages/plan/plan-mode/src/index.ts:215` | `agent/pre-step` 监听器：先 `next()`，接受后追加 |
| `packages/plan/plan-mode/src/index.ts:257` | `/plan` 命令的三个形态 |
| `packages/plan/plan-mode/src/index.ts:344` | `exit_plan_mode` 的 `when` 与提交校验 |
| `packages/plan/plan-mode/src/index.ts:395` | 评审批准与 `ASK_CANCELLED` 分支 |
| `packages/plan/plan-mode/src/index.ts:414` | 工具注册的 `when: selectedActive || entered` |
| `packages/plan/plan-mode/src/index.ts:445` | `set()` 的 committed/queued/cancelled/noop |
| `packages/plan/plan-mode/src/index.ts:470` | `onBoundary()`：追加挂起选择 |
| `packages/plan/plan-mode/src/index.ts:492` | `narration()`：切换通知只在请求头描述过另一状态时发 |
| `packages/todo/tool-todo/src/index.ts:120` | `todos` 投影：`turn/start` 清空 |

## 12. 最后

计划模式的全部机制可以压成一句：它把「用户想让模型先规划」这件事，写成一条可回放的会话事件，再让提示词与工具准入去读这条事件。

它不强制任何行为。拦住写入的是沙箱，让计划生效的是评审时那一次人工点击。

系列导航：[第四季导览](/posts/dsh-season-four.html) · [定时任务与 plan/todo 对比](/posts/dsh-scheduled-runs.html) · [交互面：命令、审批与提问](/posts/dsh-interaction-surfaces.html)
