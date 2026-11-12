---
title: "一个 Agent Harness 的解剖：DSH 的五个平面与 370 个插件"
date: "2026-11-11T08:00:00+08:00"
summary: "把 DeepSeek Harness 拆到包一级：370 个插件、6 个核心包、14 个持久事件，怎么组成输入、控制、执行、组织、记忆五个平面。这篇是系列的地图。"
description: "DSH 架构总览：插件树如何组装、一次请求经过哪些包、三个事件域如何划分，以及五个设计取舍的代价。"
tags: ["harness", "architecture", "agent"]
slug: "dsh-architecture-anatomy"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准；包内 `lib/` 是构建产物，引用一律指向 `src/`。
> 这是系列的第 1 篇，也是目录。全系列 21 篇，按「输入 → 控制 → 执行 → 组织 → 记忆」五个平面展开，最后落回「自己怎么搭」。

一个能跑的 Agent 可以只有二十几行：

```ts
while (true) {
  const reply = await llm.stream({ messages, tools })
  messages.push(reply)
  if (reply.toolCalls.length === 0) break
  for (const call of reply.toolCalls) messages.push(await runTool(call))
}
```

这段代码适合演示，不适合交付。真跑上一天，六个问题会挨个冒出来：

1. 用户在你正跑工具时又发来一条消息，它该进当前轮还是下一轮？
2. 上下文窗口满了，砍掉哪一段？砍完模型还记不记得自己刚才在干什么？
3. 工具要删一个目录，谁批准？出了事怎么证明当时批的是什么？
4. 一个子任务太慢，想开个助手并行做，父 Agent 怎么等、怎么打断、怎么收结果？
5. 进程被重启，刚才做到哪一步？
6. 模型调用失败了，重试算不算新的一步？失败的那次要不要留痕？

六个问题对应六圈基础设施。DeepSeek Harness（下称 DSH）的 46 万行代码，大部分在回答它们。下面先把这六圈东西的边界画出来，再看 DSH 用什么手段把它们塞进同一个进程。

## 1. 先看规模：53 个组、370 个包

| 维度 | 数字 | 出处 |
| --- | --- | --- |
| 包组（`packages/<group>/`） | 53 | `ls -d packages/*/` |
| 工作区包（`@deepseek-ai/dsh-*`） | 370 | 各包 `package.json` 的 `name` |
| `src/*.ts` 行数 | 459,846 | `find packages -path '*/src/*.ts'` |
| 工具包 | 31 | `packages/*/tool-*` |
| 客户端 UI 包 | 60 | `packages/client/ui-*` |
| 子系统文档 | 69 | `docs/subsystems/*.zh.md` |
| 持久会话事件类型 | 14 | `SessionEventMap` |
| `dsh-base` 组合包的行数 / 依赖数 | 99 / 101 | `packages/bundle/base/cordis.patch.yml`、`package.json` |

这些数字不说明设计好坏。能说明的是切法：DSH 没把「Agent」写成一个不断加 `if` 的类，它把功能切成几百个可以单独卸载的插件。这套切法是整个架构里最值得看的部分。

## 2. 没有内核：连 agent loop 都是插件

多数 harness 有一个 `core`：Agent 类、消息类型、循环、工具注册表都在里面，外围才是插件。DSH 的 `core/` 里只有契约：

- `core/session`：只追加的会话日志 + 内存投影；
- `core/agent`：`Agent` 接口、活跃 Agent 注册表、`agent/*` 事件；
- `core/tools`：作用域工具注册表 + 受保护执行流水线；
- `core/system-prompt`：提示词段的注册与装配；
- `core/agent-loop`：默认驱动器，它自己也是插件，可以被换掉；
- `llm/llm`：消息/流式词汇 + 适配器 seam。

底层是 Cordis：一个围绕上下文（`Context`）、服务（`Service`）和可逆副作用搭起来的插件运行时。所有能力挂在 `ctx` 上，按字符串键查找（`ctx.tools`、`ctx.sessions`、`ctx.llm`），注册即产生一个 effect，插件卸载时自动撤销。

```ts
// packages/core/tools/src/index.ts（结构示意，非逐行原文）
export default class ToolRegistry extends Service {
  constructor(ctx: Context) { super(ctx, 'tools') }
  register(definition: ToolDefinition) {
    return this.ctx.effect(() => {
      /* 写入注册表 */
      return () => { /* 撤销 */ }
    })
  }
}
```

`ctx.effect` 是整套架构的地基：注册和生命周期在这里是同一件事。插件被禁用、热更新、或者所属 Agent 被销毁，工具会自己消失，不用谁去调 `unregister`。

代价也很直接：没有一处代码能告诉你「Agent 是什么」。你只能顺着 `ctx` 的键去找能力，顺着事件名去找时机。这是本系列要反复面对的成本。

{{< fig "diagrams/01-five-planes.svg" "DSH 的五个平面与一条日志脊柱。模型处在中心，但它看到的每一段上下文都来自记忆平面，每一次行动都要经过执行平面；控制平面负责把一次输入排空成一个 turn。组织平面是可选的外层。" >}}

## 3. 一次组装：profile → bundle → patch

DSH 启动时读的不是一个 config.json。它把若干层 patch 叠成一棵插件树。看懂这棵树，才明白为什么同一个包里既有 `disabled: true` 又有 `!!js` 表达式。

| 层 | 来源 | 作用 |
| --- | --- | --- |
| profile | Harness home 里的具名组装（web / headless / sdk / acp） | 选定一组有序的 bundle |
| bundle | npm 包，`package.json` 的 `dsh.bundle` 指向一个 patch 文件 | 一组 Cordis 配置行 + 挂载代码 |
| 包内 patch | `cordis.patch.yml` | 按 `id` 插入或整条替换配置 |
| home patch | 用户的 `cordis.patch.yml` | 覆盖任意一行 |
| `--patch` overlay | 命令行临时覆盖 | 调试/实验 |

`dsh-base` 是最底层的 bundle：它向空 profile 根插入 99 行，定义了从 `timer`、`llm`、`session` 到 `bash-sandbox`、`compaction-basic` 的全部基础行。web profile 再叠一个 `dsh-web-app`，加上 HTTP 网关和客户端资源。

patch 的语义有一个容易忽略的地方：**它替换整条 `config`，不做深合并**。

```yaml
# packages/bundle/base/cordis.patch.yml:1-10（注释原文）
# A patch replaces the targeted row's whole `config` rather than merging into
# it, so a row whose value differs by mode does NOT live here: it belongs to
# each mode bundle, keeping any single row down to one bundle layer plus the
# user's.
```

这是刻意选的：深合并会让最终生效的配置依赖叠加顺序和历史，出问题时无法复现；整条替换让每一行只有一个主人。代价是模式 bundle 必须重复写完整配置，不能只写差异。

另一个细节是 patch 里的表达式可以读 `ctx`：

```yaml
- id: hmr
  name: '@deepseek-ai/dsh-hmr'
  disabled: !!js "!ctx.get('profileContext')"
```

`disabled` 的取值在加载时求值，不是静态布尔。这让「同一个 bundle 在不同宿主里启用不同行」成为可能，也让 `cordis.patch.yml` 从数据变成了程序。调试时看到某行没生效，先确认它是不是被 `!!js` 判掉了。

{{< fig "diagrams/02-assembly.svg" "从 profile 到插件树：bundle 提供默认行，home patch 与命令行 overlay 逐层覆盖。每一行有唯一 id，后写的整条替换先写的；行顺序不承担加载语义，激活由服务可用性驱动。" >}}

## 4. 六个核心包：契约在哪里

| 包 | `ctx` 键 | 拥有什么 | 刻意不做什么 |
| --- | --- | --- | --- |
| `core/session` | `ctx.sessions` | 只追加的 `SessionEvent` 日志、内存投影、格式与迁移 | 不决定「什么算一条消息」 |
| `core/agent` | `ctx.agents` | Agent 接口、活跃注册表、`agent/*` 事件类型 | 不驱动循环 |
| `core/agent-loop` | `ctx.agentLoop` | turn/step 的默认驱动器、inbox 的 claim 语义 | 不实现工具、不实现压缩 |
| `core/system-prompt` | `ctx.systemPrompt` | 提示词段注册、每次请求的装配与渲染 | 不持有会话状态 |
| `core/tools` | `ctx.tools` | 作用域注册表、执行流水线、结果规范化 | 不实现具体工具 |
| `llm/llm` | `ctx.llm` | 消息/流式词汇、适配器注册、`prepareCall` | 不决定重试策略 |

把这六个包摊开看，没有一个对象持有 Agent 的全部状态。`Agent` 是个门面：它的 `session` 指向日志，`inbox` 指向从日志折叠出来的待处理队列，`status` 是瞬时的，`ctx` 是作用域注册表。状态只有两份：日志，和进程里的活动对象。其余都是投影。

DSH 的恢复看起来毫不费力，原因在这里：重启后读日志、重建投影、把 Agent 重新挂上去，循环从下一个 turn 继续。「模型看见的，必须被记下」在这套结构里是硬前提，不是修辞（第 2 篇展开）。

## 5. 一次请求经过了谁

把「用户提问 → 模型回答 → 调工具 → 再回答」拆成包级泳道：

{{< fig "diagrams/03-request-journey.svg" "一次 turn 的包级泳道。上半段是准入（输入进队列、pre-step 决定接纳哪些消息），中段是请求组装（提示词、工具 schema、历史派生、请求头），下半段是流式与工具。注意所有持久写入都发生在边界上：turn/start、step/start、assistant/message、tool/call、tool/result。" >}}

按时间顺序，节点只有十几个：

1. UI 调用 `agent.followup(message)`，消息进 `next-turn` 队列，驱动器被唤醒；
2. 循环写 `turn/start`，从队列 claim 一条消息；
3. 每组拟进入的 step 先调 `tools.resolve({ agent, modelSelection, signal })`，得到该 Agent 当前可见的工具集；
4. `system-prompt/assemble` 装配提示词段，`agent/pre-step` 决定接纳还是拒绝；
5. 写 `step/start`，`agent/request` 决定 provider/model/参数，`llm.prepareCall()` 绑定适配器；
6. `request/header` 记录本次请求的「信封」，`request/context` 记录窗口等上下文；
7. `deriveMessages()` 从日志投影出模型历史，冻结后交给 `llm.stream()`；
8. 流式帧通过 `agent/assistant-stream` 实时广播，成功后写 `assistant/message`；
9. 若有工具调用，`tools.execute()` 走流水线，每个调用写 `tool/call`，结果写 `tool/result`；
10. 写 `step/end`；若还有工具欠账或新输入，进入下一个 step；否则 `agent/turn-stopping`、`turn/end`。

整个流程里没有「消息数组」这个中心对象。数组在第 7 步临时派生，用完就扔，下一次请求重新派生。这一点和「维护一个 `messages: Message[]` 再 append」的写法差得最远，也是它能做压缩、能 fork、能重放的原因。

## 6. 三个事件域

DSH 没把所有事件挂在一个 emitter 上，它把事件分成三个域：

| 域 | 例子 | 是否持久 | 订阅者 |
| --- | --- | --- | --- |
| 会话事件 | `turn/start`、`user/message`、`assistant/message`、`tool/result` | 是，进 JSONL | 投影、UI、恢复、压缩 |
| Agent 事件 | `agent/inbox/inserted`、`agent/pre-step`、`agent/request`、`agent/status` | 否，进程内 | 循环、审批、UI、SDK |
| 能力事件 | `fs/write-intent`、`tools/pre-execute`、`telemetry/*` | 否 | 具体能力提供方 |

{{< fig "diagrams/04-event-domains.svg" "三个事件域的边界。会话事件是持久的、可回放的；Agent 事件携带活跃 Agent、只在进程内；能力事件属于具体 seam。跨域只允许「会话事件 → 实时通知」这一个方向，反向不成立。" >}}

分域解决三个问题：

- 导入环：`fs` 不需要 import `tools` 就能在写文件前发 `fs/write-intent`；`tools` 也不需要知道谁在听。
- 持久性不同：把 `agent/assistant-stream` 的每个 chunk 写进日志会撑爆文件；把 `turn/start` 只放在内存里又无法恢复。
- 订阅者不同：压缩只关心 `assistant/message` 和 `tool/result`；UI 关心流式帧；SDK 关心 `agent/status`。

最要紧的约束是单向的：**会话事件可以触发实时通知，实时通知不能凭空产生会话事件**。任何「模型应该看见」的东西，必须先变成一条会话事件（或者由某个持有点写进去），否则恢复后就不存在。

## 7. 五个平面：本系列的目录

| # | 平面 | 回答的问题 | 篇目 |
| --- | --- | --- | --- |
| 1 | 输入 | 人的意图怎么进来、怎么排队、怎么插话、怎么被批准 | [03](/posts/dsh-input-inbox.html) · [04](/posts/dsh-steering-and-cancel.html) · [05](/posts/dsh-human-in-the-loop.html) |
| 2 | 控制 | 一次 turn/step 怎么开、怎么关、失败怎么办 | [06](/posts/dsh-turn-step-state-machine.html) · [07](/posts/dsh-request-assembly.html) · [08](/posts/dsh-error-retry-recovery.html) |
| 3 | 执行 | 工具怎么注册、怎么执行、边界在哪、结果怎么回去 | [09](/posts/dsh-tool-registry-scope.html) · [10](/posts/dsh-tool-pipeline.html) · [11](/posts/dsh-tool-boundaries.html) · [12](/posts/dsh-tool-results.html) |
| 4 | 组织 | 子代理、任务板、工作流、后台任务 | [13](/posts/dsh-subagent-lifecycle.html) · [14](/posts/dsh-subagent-control.html) · [15](/posts/dsh-agent-team.html) · [16](/posts/dsh-workflow-jobs-goal.html) |
| 5 | 记忆 | 上下文怎么装配、怎么压缩、怎么回忆 | [17](/posts/dsh-memory-architecture.html) · [18](/posts/dsh-context-injection.html) · [19](/posts/dsh-compaction.html) · [20](/posts/dsh-recall.html) |
| — | 基底 | 插件树与事件日志 | [02](/posts/dsh-session-event-sourcing.html) |
| — | 收尾 | 从 0 到 1 的决策与坑 | [21](/posts/dsh-build-agent-from-zero.html) |

读法建议：如果只关心「为什么我的 Agent 一重启就失忆」，从第 2 篇开始；如果正在选型，直接看第 21 篇的决策表，再回头补细节。

## 8. 设计取舍

全插件化，还是留一个内核。DSH 选了全插件，换来的是连循环和日志都能换：SDK profile 可以把 UI 全部卸掉，ACP profile 可以把驱动器换成别家的。代价是可理解性，新读者找不到一条「从这里开始读」的主干，只能顺着 `ctx` 键和事件名拼。DSH 用 69 篇子系统文档补这件事，但文档本身也是成本。

日志为真源，还是让状态可变。选日志，换来恢复、fork、审计、压缩后的可重建。付出的代价在写入端：任何「模型可见」的改动都要走 `session.append`，还要处理 surface（模型可见表面）的重写语义。第 2 篇会看到，这套语义比看上去复杂。

waterfall，还是监听器列表。`agent/pre-step`、`agent/request`、`llm/stream`、`tools/pre-execute` 都是 waterfall：每个监听器拿到上一个的结果，可以改写，也可以拒绝。换来的是可组合，审批、压缩、注入各管一段。代价是顺序不显式，它由注册顺序决定，读代码时看不出来。

工具、提示词段、变量可以注册到 `agent.ctx`，只对该 Agent 可见，销毁时自动撤销。换来的是隔离，子代理不会继承父的工具。代价是「一张表看全」的直观没了，restriction 与遮蔽的规则要靠文档（第 9 篇）。

`ctx.fs`、`ctx.shell`、`ctx.sandbox` 都是 seam：一个 Service Definition、多个 Provider、多个 Consumer。换来的是「把 fs 指向远程沙箱，Bash/PTY/LSP 一起搬走」。代价是调用链不直观，`ctx.fs.readFile` 背后可能是本地磁盘，也可能是另一个进程。

## 9. 踩坑点

- 把 `ctx.effect` 当析构函数用。它返回的是 disposer，注册发生在 effect 体内；如果注册逻辑写在 `effect` 外面，卸载时不会撤销。
- waterfall 监听器忘记 `next()`。不调用 `next()` 等于吞掉后面所有监听器，且不会有报错。看到「某个插件突然不生效」，先查它前面有没有人没 `next()`。
- default export 与命名导出混用。service 包默认导出 class，function 插件必须只有命名导出；混用会让 Loader 丢掉 `inject`（仓库里有专门的 postmortem：`docs/postmortem/0001-acp-default-export-drops-inject.md`）。
- 用 `ctx.tools` 读可选服务。属性代理是拓扑敏感的，可选服务要用 `ctx.get('tools')` 读全局服务存储。
- patch 只写差异。整条替换的语义意味着「少写一个字段」等于「把它重置为默认」，不是「保持原值」。
- 在 `agent/request` 里改 provider/model。第 7 篇会讲：step 准备阶段已经冻结了选择，这里改会直接抛错，必须在 `agent.model.select()` 里改。
- 以为 `turn/end` 会 flush。循环不在 turn 边界等待落盘；持久化检查点由 `dsh-session-checkpoint-policy` 拥有，`whenIdle()` 之后还要自己 flush 才能读存储。

## 10. 源码索引

- `packages/bundle/base/cordis.patch.yml:1-12`：patch 的整条替换语义与「一行一个主人」的注释。
- `packages/bundle/base/package.json`：`dsh.bundle` 声明与 101 个依赖，说明 base 到底装了什么。
- `packages/core/agent/src/types.ts:16`：公开的 `Agent` 只有 `id`；运行时能力在 `runtime-types.ts:190` 的模块增强里。
- `packages/core/agent/src/runtime-types.ts:130-136`：`AgentStatus` 只有 `idle`/`running`，disposal 不是第三个状态。
- `packages/core/agent/src/runtime-types.ts:205-270`：`cancel` / `whenIdle` / `runMaintenance` / `send` / `followup` / `steer` / `inject` 的契约原文。
- `packages/core/session/src/types.ts:287`：`SessionEventMap`，14 个持久事件的全集。
- `packages/core/agent-loop/src/agent.ts:327`、`:435`：`turn()` 与 `step()` 的入口。
- `docs/architecture.zh.md`：组合、事件域、轮次流程、扩展点归属的总表。
- `docs/subsystems/core.zh.md`：Agent/Inbox/Model/PreStepDecision 的逐条类型说明。

三百七十个包听起来吓人，回答的却是同一个问题的不同侧面：模型只看得到这次请求里的东西，所以「这次请求里的东西」得够用、可解释，崩溃后还能重建。后面 20 篇都在展开这件事。

下一篇：[模型看见的，必须被记下：会话日志与事件溯源](/posts/dsh-session-event-sourcing.html) ｜ [系列目录](/posts/dsh-architecture-anatomy.html)
