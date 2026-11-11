---
title: "计划住在哪里：两种 harness 的规划载体"
date: "2026-11-11T13:58:00+08:00"
summary: "OpenCode 把计划留在对话里，Claude Code 把它抄到文件里。拆开两边的入口、边界与产物后会发现：循环和规划本身没变，变的是计划以什么形态离开这次对话，以及谁有权说开始。"
description: "对照 OpenCode 与 Claude Code 的规划机制：计划停留在对话、清单、文件、判词四个位置，差别不在循环，在载体与边界设施。"
tags: ["planning", "harness", "agent", "architecture"]
slug: "plan-carriers"
---

> 依据分三类，正文里逐条标注：
> **源码**：opencode `2.2.5`，commit `a687c047`（2026-11-08），工作区 `.opencode-src/`。
> **二进制**：本机 Claude Code `2.1.321`，用 `strings` 从可执行文件里抽出的工具描述与提示词原文。
> **捕获**：社区记录的 Claude Code `2.1.274` 系统提示词，仅用于佐证措辞，单独标出。
> Claude Code 没有可读源码，凡涉及"它内部怎么判断"的句子都写成推断，并在句子里给出依据。

上一篇讲 OpenCode 怎么执行一条消息：准入、drain、Step、六个出口。这一篇换个问法——**计划这东西，在 agent 里到底住在哪里？**

## 0. 先说结论

规划不是模块，计划也不是一个对象。它是四张纸，分别停在不同地方：

{{< fig "diagrams/01-carriers.svg" "四种载体：手稿、清单、文件、判词。越往右越不依赖当前这次对话，也越不容易被模型顺手改掉。" >}}

| 问题 | OpenCode | Claude Code |
|---|---|---|
| 怎么进入规划态 | 切换 agent（一条 agent 记录） | 调用 `EnterPlanMode`，用户点头 |
| 规划期靠什么拦 | 权限 deny 三个编辑工具 + 一条提醒 | 提示词里的只读强制 + 工具授权面 |
| 计划落在哪 | 对话历史里的一段话 | 对话 + `plans/` 下的一份文件 |
| 怎么退出 | 用户切走 agent，没有别的条件 | 调用 `ExitPlanMode`，用户批准 |
| 会不会自己检查"计划还对吗" | 不会，只补提醒 | 不会，文件不会自己宣告作废 |

三句话：

1. **两者的循环是同一个**。规划期都是一次只读的 ReAct：探索、读文件、想方案。计划是探索完后写下的文字，不是依赖图。
2. **两者的差别不在循环，在边界设施**：谁有权说开始、计划以什么形态离开这次对话。
3. **"存下来"和"还对不对"是两件事**。文件解决了前者，两者都没解决后者。

## 1. 计划不是一个对象

同一个词在四个地方指四样东西，先对齐：

**手稿**——对话里的一段自然语言。模型自己写，"我打算先做 A 再做 B"。它不属于任何数据结构，只是 token。下一次请求时它还在上下文里，就还算数。

**清单**——一次工具调用的参数。`todowrite` 传进去的数组会被渲染成界面上的勾选框。它落在消息里，所以比手稿"硬"一点：有结构、有顺序、能被界面读。

**文件**——磁盘上的一篇 markdown。它离开了上下文，所以能被人看、被下一个会话读、被别的进程改。

**判词**——另一个模型的一句话裁决。`/goal` 每轮跑一次评审，返回 `done / continue / blocked / paused / wait`。它不描述计划，只回答"还该不该继续"。

这四样可以同时存在。决定一个 agent 好不好用的，往往不是它用哪一种，而是**有没有在两种之间对齐**——比如 OpenCode 有手稿有清单，却没有把清单和手稿对齐的机制；Claude Code 有手稿有文件，却没有把文件和执行对齐的机制。

## 2. OpenCode：计划留在对话里

### 2.1 进入规划态，只是换了一条 agent 记录

Plan 在 OpenCode 里不是模式，是一条 `agent` 记录。插件启动时往它身上追加三条权限（`packages/core/src/plugin/plan.ts:48-56`）：

```text
{ action: "question", resource: "*",                     effect: "allow" }
{ action: "edit",     resource: "*",                     effect: "deny"  }
{ action: "edit",     resource: "~/.opencode/plan/*",    effect: "allow" }
```

然后往历史里塞一条 `system-reminder`，原文开头是「You are in Plan mode. Discuss the plan with the user directly in the conversation」。就这么两步。切换 agent 的事件只有 `session.agent.selected`（`packages/core/src/session/session.ts:99-105`），**不校验、不审批、不产生计划产物**。

{{< fig "diagrams/02-two-gates.svg" "左边是模式开关：切了就进，没有批准；右边是两道门：进去要同意，出来要批准。" >}}

### 2.2 提醒是"对账"用的，不是"配置"用的

Plan 的 `context` 钩子（`plan.ts:76-100`）每一步都做同一件事：找出历史里最后一条 enter/leave 提醒，和当前 agent 比对。

- agent 是 plan，但最后一条不是 enter → 补一条
- agent 不是 plan，但最后一条是 enter → 补一条 leave

为什么要这样做？因为提醒会被两件事冲掉：**压缩**会把旧消息换成摘要，**回滚**会直接删消息。冲掉之后 agent 字段还是 `plan`，但模型已经看不到"你在只读模式"这句话了。

所以这个钩子是必要的补丁，但它补的是**通知**，不是**计划**。压缩把"先改 A 再改 B"摘要成"讨论了重构方案"之后，提醒能把"你在 Plan 模式"补回来，补不回那两句具体的话。

### 2.3 三个漏口

- **shell 没被 deny**。权限只拦了 `edit`，`bash` 不在里面。plan 之下照样可以 `sed -i`、`git apply`。
- **子代理没被拦**。提醒里写了「Do not modify any other files or ask a subagent to do so」，但它是一句话，不是一条权限。
- **事后补的批准翻不了案**。权限按 agent → session → saved approvals 的顺序合并，后者覆盖前者；但对 agent 和 session 两级有一段前置 deny 检查，saved 之后也推不翻（`packages/core/src/permission.ts:176-192`）。

结论：OpenCode 的 Plan 不是沙箱，是"编辑门禁"。它拦住的是最顺手的那条路。

### 2.4 todo 是一份没有读者的清单

`todowrite` 的 `execute`（`packages/core/src/tool/todo.ts:63-78`）只做三件事：断言权限、把参数原样返回、拼一个摘要。没有存储、没有钩子、没有校验。

它唯一的读者是界面。TUI 把最近一次调用的数组渲染成侧栏，Web 端干脆不渲染，改在输入框上方显示一个胶囊。**没有任何代码读 todos 来影响执行**——模型可以永远不更新，也可以写一份和实际完全不符的清单。

这不是缺陷，是定位：它是给人和模型看的便签，不是状态机。

### 2.5 /goal 是唯一会自己转的轮子

`/goal` 在 idle 钩子里做裁决（`packages/core/src/plugin/goal.ts`）。评审模型返回五个判词之一，`continue` 时把一段 continuation 以 `synthetic` 消息塞进收件箱，于是同一个 busy period 继续下一圈——`Execution.Succeeded` 都不会发。

硬上限：`MAX_ROUNDS = 50`、`MAX_STALLS = 3`、`WAIT_LIMIT = 30 分钟`。停滞的判定很朴素：这一轮的理由和上一轮相同，或者这一轮一个工具都没调。

它不产生计划，只产生"继续/停"的决定。所以它是判词，不是清单。

## 3. Claude Code：计划写在文件里

### 3.1 进去要批准

`EnterPlanMode` 的工具描述（二进制字符串，`2.1.321`）里有三句硬话：

```text
This tool REQUIRES user approval - they must consent to entering plan mode
Only the main session can enter plan mode.
EnterPlanMode tool cannot be used in agent contexts
```

第三句很关键：子 agent 上下文里调用会直接抛错，规划权被收在主会话手里。OpenCode 那边相反：子会话继承的是父会话的 session 级权限，而 plan 的只读来自 agent 级权限，所以子会话不会自动进入只读。

进去之后注入的提醒（同一份二进制）是：

```text
Entered plan mode. You should now focus on exploring the codebase
and designing an implementation approach.
...
Remember: DO NOT write or edit any files yet.
This is a read-only exploration and planning phase.
```

### 3.2 只读是"最高优先级"的

plan mode 生效时的那段提示词，原文是：

```text
Plan mode is active. The user indicated that they do not want you to execute yet
-- you MUST NOT make any edits, run any non-readonly tools (including changing
configs or making commits), or otherwise make any changes to the system.
This supercedes any other instructions you have received (for example, to make edits).
```

`This supercedes any other instructions` 这句是重点：它不是加在规则列表末尾的一条，而是覆盖前面所有指令的一条。同一段字符串还有一个变体，专门给允许写计划文件的场景用，括号里写着 `with the exception of the plan file mentioned below`。

### 3.3 提示词是三段拼的

二进制里有一条 CLI 选项的说明，直接把这套结构说破了：

```text
Custom workflow body for plan mode. Replaces the default code-implementation phases
in the plan-mode system reminder; the read-only enforcement preamble and the
ExitPlanMode protocol footer are always kept.
```

三段是：**只读强制前言** + **实现阶段正文（可替换）** + **ExitPlanMode 协议脚注**。默认正文是五阶段工作流；另有"稀疏模式"，脚注会变成"用 `AskUserQuestion` 提问、用 `ExitPlanMode` 提交，**永远不要用文字或提问工具问要不要批准**"。

也就是说：审批是一个工具调用，不是一个问题。这条设计消除了"模型问你'可以开始了吗'、你回'可以'、它当成批准"的歧义。

### 3.4 出来也要批准

`ExitPlanMode` 的描述只有一句：`ExitPlanMode inherently requests user approval of your plan`。界面上的卡片写着 `Plan Approval Request from {name}` 和 `Plan file: {path}`。

两个结果的文案都是定死的：

```text
批准：You can now proceed with implementation. Your plan mode restrictions have been lifted.
拒绝：Please revise your plan based on the feedback and call ExitPlanMode again.
```

团队场景里还有一句 `Only the team lead can approve plans`——批准权被绑在角色上，而不是绑在"谁先看到"。

### 3.5 计划文件是这次规划唯一的产物

`plans/` 目录可以配（`plansDirectory` 设置项）。计划文件在只读期被特许写——这是"只读"唯一的例外，也是整段流程里唯一会离开上下文的产物。

它同时解决三件事：人能读、下一个会话能读、审批时有东西可看。

## 4. 四次压力测试

把两个载体放进四种真实压力里，结果不一样：

{{< fig "diagrams/03-pressure.svg" "压缩、崩溃、开子会话、换人接手。文件活得更久，但不会自己宣告过期。" >}}

| 压力 | 对话里的手稿 | 磁盘上的文件 |
|---|---|---|
| 上下文压缩 | 被摘要吞掉；提醒能补一条，内容补不回 | 不受影响 |
| 进程崩溃 | 事件是持久化的，历史还在；在飞的那一步要重来 | 还在，重启后按路径找回 |
| 开子会话 | 不继承；计划得在 prompt 里重讲 | 可读，前提是父会话把路径写进去 |
| 换人接手 | 只能读 transcript，自己重新提炼 | 能直接接手，但可能已经过期 |

前两列是工程问题，后一列不是。

## 5. 差别的本质：三个属性

抛开实现，计划这种东西只有三个属性值得设计：

**持久性**——它能不能活过一次压缩、一次崩溃、一次换人。对话里的手稿是易失的，文件是持久的，清单介于两者之间（它活在消息里，所以跟着上下文一起被压缩）。

**可校验性**——有没有人能拿它和现实对一遍。文件可以被 diff、被审、被测试引用；手稿只能被读。OpenCode 的 `/goal` 是一种弱校验：它不检查计划，只检查"有没有进展"。Claude Code 的验证是在批准之后另行派 agent 逐条核对——它校验的是执行结果，不是计划文本。

**否决点**——谁能在什么时候说停。OpenCode 的否决点是"用户切走 agent"，随时可以，但没有门；Claude Code 的否决点是两道门，进去和出来各一次。

三者里最容易被忽略的是第三个。很多人以为"计划模式"的价值在于让模型多想一会儿，其实它的价值在于**把"想"和"做"切成两个可以分别叫停的阶段**。

## 6. 为什么 OpenCode 不这么做

不是没想到，是取舍不同。

OpenCode 的定位是"可以嵌进任何东西的 harness"：TUI、Web、桌面端、workerd 里跑的服务。它没有"会话主人"这个概念——插件可以自己起会话、子代理可以继承父会话的权限、`/goal` 能在无人值守时自己转。在这种设定下，"等一个人点头"没法作为流程的必经环节。

所以它选了软约束：一条提醒 + 三个工具的权限。代价是边界不硬，收益是不依赖人。

Claude Code 的设定反过来：它假设有一个坐在终端前的人，假设会话可能横跨几小时，假设团队里别人会接手。这三条假设里任何一条成立，"计划落成文件 + 两道门"就是划算的。

## 7. 代价

**文件会腐化。** 计划写完就冻结，执行却一直在动。三天后有人打开 `plans/xxx.md`，看到的是当时的想法，不是现在的状态。没有任何机制会让它自己失效。

**审批会疲劳。** 每加一道门，就有一次"看都没看就点同意"。门越多，每次的注意力越少。

**写了不等于做了。** 计划文件是意图的证据，不是完成的证据。它能让别人接手，也能让别人误以为已经做过。

**软约束会漏。** OpenCode 这边，shell 没被拦、子代理没被拦、压缩之后只剩一条"你在 Plan 模式"的提醒。它靠的是模型看见提醒后愿意遵守。

## 8. 如果要自己设计

四个问题，按顺序回答：

1. **计划存在哪？** 只活在上下文里，就接受它会被压缩吞掉；要活过压缩，就得有一份上下文之外的东西。
2. **谁能改它？** 如果模型能随手改，它就不是承诺，是便签；如果要显式动作才能改，就得定义那个动作是什么。
3. **什么时候重新检查？** 这是最少人回答的问题。文件不会自己过期，所以要么定期重读，要么在执行的关键节点强制对账——OpenCode 对提醒做了这件事（每步对账），对计划没做。
4. **失败的默认值是什么？** 提醒丢了是补一条还是当作没进过规划态？计划过期是继续执行还是停下来问？默认值决定了系统在边界情况下的样子。

## 9. 证据索引

| 结论 | 证据 | 等级 |
|---|---|---|
| Plan 是 agent 记录 + 三条权限 | `.opencode-src/packages/core/src/plugin/plan.ts:48-56` | 源码 |
| 提醒每步对账 | `plan.ts:76-100` | 源码 |
| todo 无读者 | `packages/core/src/tool/todo.ts:63-78` | 源码 |
| `/goal` 五个判词与三个上限 | `packages/core/src/plugin/goal.ts:16-19, 31` | 源码 |
| EnterPlanMode 需批准、只限主会话 | 二进制 `2.1.321`，工具描述 | 二进制 |
| plan mode 只读且优先级最高 | 二进制 `2.1.321`，第 174805 行 | 二进制 |
| 三段式提示词 | `--plan-mode-instructions` 说明 | 二进制 |
| ExitPlanMode 触发审批 | 二进制，工具描述 | 二进制 |
| 云端规划会话"不要派子 agent" | 社区捕获 `2.1.274` | 捕获 |

源码之外的引用：[Claude Code 文档](https://code.claude.com/docs)、[claude-code-system-prompts](https://github.com/piebald-ai/claude-code-system-prompts)。

系列的上一篇是[《OpenCode 的执行机制》](/posts/opencode-execution.html)，再往前是[《OpenCode 的任务规划》](/posts/opencode-plan.html)。
