---
title: "Agent 的记忆不是一块：五层与两个方向"
date: "2026-11-11T18:40:00+08:00"
summary: "把「记忆」拆成静态指令、会话日志、压缩工作集、检索、程序性记忆五层：写入只有一条规则（模型可见即记录），读取只有一个动作（每次请求重新装配）。"
description: "DSH 记忆平面的实现侧拆解：为什么向量库不能当真源、五层各自的失效方式、哪些层模型可见，以及 skill 为什么是程序性记忆而不是 RAG。"
tags: ["memory", "harness", "context-engineering"]
slug: "dsh-memory-architecture"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准；包内 `lib/` 是构建产物，引用一律指向 `src/`。
> 这是系列的第 17 篇，记忆平面的总览。上一篇讲工作流、后台任务与目标如何共用一条日志；这一篇把「记忆」这个词拆开，下一篇讲上下文具体怎么注入。

先看三个现场。

第一个：你在仓库根目录写了一份 `AGENTS.md`，又在 `packages/foo/AGENTS.md` 写了一份只对那个包生效的约定。子代理接了「改 foo 的构建脚本」的任务，前两轮它只读了根目录的 README 和 `package.json`，于是一直按根目录的规则改；直到它第一次 `write` 到 `packages/foo` 下的文件，那份子目录指令才在下一步的 pre-step 里出现。你去看日志，发现它没有「丢」，只是来得太晚。

第二个：一次长任务跑了四十分钟，中间压缩了两次。压缩之后模型开始重复问已经回答过的问题，还把一个早就改过的文件名又写了回去。摘要里有「实现了登录流程」，但没有路径、没有失败的命令、没有那个被否决的方案。

第三个：你接了一个向量库，把每轮对话切片、embedding、写入。一周后用户改了一个配置项，检索仍然命中旧值；你删掉一条记忆，旁边的摘要还在引用它；你想复现上周的一次请求，拼不回去。

这三个现场指向同一个误解：把记忆当成一个模块。好像只要有一个 `MemoryStore`，`write()` 进去、`search()` 出来，Agent 就记住了。真实的 Agent 里，「记忆」是五种生命周期完全不同的东西，被五套代码分别写入、分别读取、分别在什么时刻失效。把它们塞进一个接口，只会得到一个什么都存、什么都不可信的黑箱。

如果你读过[《设计 Agent 的记忆系统：五层结构、七个钩子、三速演进》](/posts/design-agent-memory.html)，那篇回答的是「应该有哪些层」。这一篇是它的实现侧对照：DSH 把哪一层放在了哪里，哪些假设在设计图上成立、落到代码里必须改写。设计侧可以拍脑袋加一层；实现侧每加一层都要回答三个问题：谁写、谁读、什么时候失效。答不上来，这一层就会变成下一次事故的现场。

## 0. 结论

DSH 没有单一的存储，记忆被分成五层：

1. 静态指令：进程启动时注册、每次请求重新渲染的 system prompt section，以及按目录层级发现的 `AGENTS.md`。它回答「规则是什么」。
2. 会话：仅追加的 `SessionEvent` 日志，模型看到的历史从它派生。它回答「发生过什么」。
3. 压缩：把一段旧 surface 替换成结构化 checkpoint，形成当前请求的工作集。它回答「这一轮还带得动什么」。
4. 检索：对持久化会话与工作区文件的按需查询，只在模型显式调用工具时进入上下文。它回答「去哪找不在这里的东西」。
5. 程序性记忆：`SKILL.md` 目录、技能目录快照、以及加载后的完整指令。它回答「这类任务怎么做」。

两个方向：

- 写：模型可见即记录。任何要进入模型请求的东西，必须先成为一个会话事件；没有事件，就没有发生过。
- 读：每次请求重新装配。loop 不维护一份权威的 `messages` 数组；它在每个 step 从日志派生历史，重新渲染 prompt，重新投影运行时上下文。

五层对应五份契约，没有五个数据库。下面逐层拆。

{{< fig "diagrams/01-five-layers.svg" "五层记忆各自的写入者、读取者与可见性。右侧标出哪些内容实际进入模型请求，哪些只停在控制面。" >}}

## 1. 为什么记忆不能是一个向量库

这个结论是后面所有设计的前提，先展开。

### 1.1 模型请求必须是日志的纯函数

DSH 的会话是一个仅追加日志：

> `Session` 是一份由类型化 `SessionEvent` 组成的**仅追加日志**，是 agent 完整交互历史的唯一真源。LLM 消息历史从日志*派生*而来，从不单独存储；回放即从同一组事件重新派生。

这条约束换来的是可验证性，跟架构洁癖无关。`deriveMessages()` 在 `packages/core/session/src/index.ts:999`，它遍历 surface 节点，把 `system/message`、`user/message`、`assistant/message`、`tool/result` 折叠成消息数组。请求信封（模型、参数、工具 schema）则单独记录在 `request/header` 里，于是每个请求都是「日志 + 头部」的纯函数。

一份典型日志折叠出来是这样：

```text
seq 12  turn/start        { turn: 3 }
seq 13  step/start        { turn: 3, step: 1 }
seq 14  user/message      "把登录改成 OAuth"           → surface 节点
seq 15  request/header    { provider, model, tools }   → 不进消息，只进请求头
seq 16  assistant/message "先看 auth.ts" + tool_call   → surface 节点
seq 17  tool/result       read auth.ts 的内容          → surface 节点
seq 18  step/end
seq 19  step/start        { turn: 3, step: 2 }
...
```

`deriveMessages()` 从 14、16、17 这些节点拼出消息数组；`request/header` 提供模型与工具 schema。两者都不依赖进程内状态，所以任何一台机器、任何一个时间点，只要拿到这段日志，就能重建同一次请求。

一旦你允许某段文本绕过日志直接进入请求，三件事同时坏掉：

- 回放坏了。同一份日志重放，模型看到的东西不一样，bug 无法复现。
- fork 坏了。子代理按 `inheritedEventCount` 截断日志来继承上下文；没进日志的记忆不会跟着走，也不会被明确排除，它只是凭空消失。
- 压缩坏了。压缩要决定「遮蔽哪些 surface 节点、保留哪些事实」。不在 surface 上的记忆没有 seq，既不能被遮蔽，也不能被摘要引用。

### 1.2 向量库是索引，真源在日志

向量库擅长的事是「给一段文本，找相似的另几段」。它不擅长、也不打算擅长：

- 顺序。消息有严格的角色交替、工具调用与结果配对、turn/step 边界。相似度排序会打乱这些。
- 归属。某句话是谁说的、在哪个 turn、是不是已经被后来说法推翻，embedding 里没有这些位。
- 表面状态。同一条消息可能仍在 surface 上，也可能已被压缩遮蔽。向量库只存一份文本，分不清 `current` 与 `shadowed`。
- 删除与更正。用户说「刚才那条作废」，日志里追加一条新事件即可；向量库要找出所有由旧事实派生的切片一并删掉，而派生关系它并不知道。

所以 DSH 里长期记忆的真源仍然是持久化的会话日志。检索层（第 4 层）是对日志的查询，索引可以重建、可以丢、可以换实现，它本身不承担记忆的角色。`session-query` 的文档把这件事写得很直白：它提供的是对「逻辑会话语料」的查询词汇，不提供第二个写入端。

### 1.3 什么时候可以用向量库

可以，把它降级成派生索引，并接受三条约束：

1. 索引里的每一条都能指回 `sessionId + seq`，或者指回一个文件路径加内容摘要；
2. 索引随时可以全量重建，重建结果不影响正确性，只影响召回率；
3. 检索结果走一次工具调用进入日志，不直接拼进 prompt，于是它可见、可回放、可被压缩。

只要违反第三条，你就会得到开头第三个现场。

### 自己搭的时候

先写日志，再谈记忆。日志的最小契约是：仅追加、每条有单调 seq、每条能序列化、能从日志派生消息、能按 seq 截断做 fork。这五条没有之前，不要引入任何「记忆存储」；有了之后，向量库只是日志之上的一个 view。

{{< fig "diagrams/02-write-read.svg" "记忆的两个方向：写入侧所有输入先落成事件再进入 surface；读取侧每次请求从日志派生、重新渲染、重新投影。" >}}

## 2. 五层各自是什么

### 2.1 第一层：静态指令

静态指令有两个来源，生命周期不同。

注册型：插件在启动或 scope 建立时调用 `ctx.systemPrompt.section({ name, order, text })`。`text` 可以是字符串，也可以是 `(context) => string` 的函数，在每次 `assemble()` 时求值。`packages/core/system-prompt/src/index.ts:620` 的 `assemble()` 把全局层与 scope 链上的层合并：同名 section 由更近的 scope 遮蔽，变量从最远的 scope 往最近解析、最近者胜，最后按 `order` 升序、同序按名字的 code unit 排序。`SECTION_ORDERS` 在 `:143-178`，从 `HARNESS_IDENTITY: -1000` 到 `DEPLOYMENT_PERSONA_SUFFIX: 10200`，中间留出大量空位给插件插入。

文件型：`AGENTS.md`。它名义上是「指令」，实现上是一条带 `source.kind === 'agent-instructions'` 的持久 user message，没有注册成 system prompt section。`packages/context/agent-instructions/src/index.ts:330` 在 `agent/pre-step` 末尾组装它：第一次是完整 baseline，之后只发变化的 scope。为什么不做成 section，第 3 节讲写入约束时会给答案。

这一层的读取者是 `assemble()` 与 `renderPrompt()`（`:296`）。它输出的是纯文本，但文本怎么排列由 order 决定，不由插件注册顺序决定。这样不同机器、不同加载顺序下的 prompt 字节才一致。

失效方式：注册随 fiber 销毁而消失；文件随内容摘要变化而重发。中间没有缓存层，也不需要缓存：每次请求都会重新求值。

### 2.2 第二层：会话

会话层是唯一「什么都知道」的一层。`turn/start`、`step/start`、`request/header`、`assistant/message`、`tool/call`、`tool/result`、`user/message`、`system/message`，加上压缩的 `compaction/*`，全部落在这条日志上。

写入者很多：loop 写轮次与请求，工具执行器写结果，`agent.inject()`（`packages/core/agent/src/runtime-types.ts:270`）写合成输入，压缩引擎写摘要与替换。但入口只有一个：`session.append(type, data, intent)`。`intent` 决定这条事件是否上 surface、以什么方式上（append 还是 replace）、以及替换谁。

读取者同样多：`deriveMessages()` 给模型拼历史；`session-query` 给人或工具做全文检索；fork 按 `inheritedEventCount` 截断；telemetry 从结算事件里抽指标。它们读的是同一份日志，所以解释得通。

失效方式：会话层不失效。它只增长。唯一让它「看起来少了」的机制是 surface 折叠：压缩用一条 `replace` 事件遮蔽一段旧节点，旧事件还在日志里，只是不再属于当前 surface。日志是事实，surface 是视图，这个区分在后面每一层都会用到。

### 2.3 第三层：压缩

压缩是记忆里最容易被误解的一层。它替换当前工作集，不删除旧消息。

一次压缩做四件事：选一段可压缩的 surface 区间、让模型生成结构化 checkpoint、把 checkpoint 作为一条 user message 追加、用 `surfaceOp: { op: 'replace', startSeq, endSeq }` 让这条消息替换掉整个区间。`packages/compaction/compaction-basic/src/region.ts:173` 是这条事务的入口，`:507` 是那次 replace。

被遮蔽的事件没有消失。`session.surface` 把它们分类成 `shadowed`，`session-query` 仍然能读到；压缩摘要的 `shadowedSeqs` 也完整记录了它替换了谁。「忘得可解释」和「真的删了」是两件事。

写入者是压缩引擎，触发点有三个：`agent/pre-step` 的压力检查（`packages/compaction/compaction-basic/src/index.ts:157`）、`agent/request` 的模型切换检查（`:183`）、`agent/request-error` 的溢出恢复（`:213`）。读取者是下一次 `deriveMessages()`：它只会看到 checkpoint，加上保留下来的尾部。

失效方式：checkpoint 自己也是 surface 节点，可以被下一次压缩再次遮蔽。所以摘要指令明确要求：遇到旧的 `<compacted-summary>` 要合并，不要原样复制。另一个失效方式是崩溃：`compaction/start` 与 `compaction/end` 不配对时，下一次压缩会拒绝启动，把「未知状态」暴露出来而不是假装成功。

### 2.4 第四层：检索

DSH 没有「自动召回」。没有每次请求都跑一遍相似度、把 top-k 拼进 system prompt 的代码路径。检索是一个显式动作。

模型侧的工具在 `packages/experimental/tool-session-query/src/index.ts`：`session_search` 跨会话找相关工作，`session_event_search` 在单个会话里搜事件，`session_trace` / `session_event_trace` / `session_event_read` 追血缘和读原文。工具描述把边界写死了：结果是 workspace-scoped、cursor-free，命中之后要自己追。`session-query` 把每条事件分成 `current` / `shadowed` / `log-only`，用的正是派生模型历史那套 `foldSurface()` 转移，检索看到的世界和模型看到的世界是同一个视图定义。

文件侧也一样：`@file` 只是把路径放进 prompt，内容要模型自己调 `read`。第 18 篇会展开这条时序。

写入者：索引构建器（SQLite 全文索引）、检索工具的结果。读取者：模型（通过工具）、人（通过 UI）。失效方式：索引落后于日志、或者索引的 surface 分类与当前实现不一致。因为索引是派生的，正确做法是重建而不是修补。

### 2.5 第五层：程序性记忆

`SKILL.md` 不是文档，是程序。「这类任务怎么做」被写成一份可加载的指令，带名字、描述、触发条件、资源目录，必要时还带脚本。

DSH 的技能系统分三段：

1. 发现：`skill-filesystem` 按固定根目录扫描，项目内 `.dsh/skills`（rank 100）、`.agents/skills`（rank 200），自定义目录（300），用户级 `dshHome/skills`（400，跳过 `.system`）、`agentsHome/skills`（500），内置（600）。同名技能由 rank 小者胜，同 rank 再按 provider 顺序与本地顺序。
2. 目录：`tool-skill` 把可被模型调用的技能压成 `name + description`，用 sha256 做 digest，作为一条持久的 `<available_skills>` user message 发布。目录里没有正文，也没有绝对路径。
3. 加载：模型调用 `skill` 工具，或用户输入 `/name`，才把整份 `SKILL.md` 读进来，包成 `<skill_content>`。

写入者是文件系统（人或 agent 改文件）与运行时 provider；读取者是目录发布逻辑与 `skill` 工具。失效方式是 `skills/change` 事件：watcher 捕获增删改，下一次 pre-step 重算目录 digest，变了就发一条「完整目录替换旧目录」的消息。

这一层为什么不算 RAG，第 8 节单独说。

### 2.6 五层放在一起看

| 层 | 定位 | 典型载体 | 谁写 | 谁读 |
|---|---|---|---|---|
| 静态指令 | 规则 | section 注册、`AGENTS.md` | 插件、人 | `assemble()`、pre-step |
| 会话 | 事实 | `SessionEvent` 日志 | loop、工具、`inject()` | `deriveMessages()`、检索、fork |
| 压缩 | 工作集 | checkpoint user message | 压缩引擎 | 下一次 `deriveMessages()` |
| 检索 | 按需查询 | 全文索引、工具结果 | 索引器、工具 | 模型（显式调用） |
| 程序性 | 做法 | `SKILL.md`、目录消息 | 文件系统、watcher | 目录发布、`skill` 工具 |

{{< fig "diagrams/03-lifecycle-table.svg" "五层生命周期对照：谁写、谁读、何时失效、持久在哪。注意「持久」一列里，只有会话是真源，其余要么是文件，要么是派生视图。" >}}

### 2.7 一次长任务里，五层分别在哪一刻动

把一次四十分钟的重构任务按时间摊开：

1. turn 1，pre-step：`assemble()` 渲染静态指令；`AGENTS.md` 首次注入，成为 baseline user message；技能目录发布；时间 context 注入第一条。
2. turn 1，step 2：模型 `read` 了子目录的 `AGENTS.md`。touch 在 `step/end` 被缓冲，下一次 pre-step 的 `compose()` 算出这个子目录的增量，插在已认领消息之后。
3. turn 1，step 5：token 用量越过阈值，pre-step 的压力压缩跑起来。它遮蔽前四个 surface 节点，把摘要放在一条替换消息里；下一 step 的 `deriveMessages()` 只看到摘要加尾部。
4. turn 2：模型想找「上周那个同类重构」，调用 `session_search`。检索工具从持久化会话索引里返回命中的 `sessionId + seq`，结果作为 `tool/result` 进入日志。
5. turn 2，step 3：用户输入 `/review`，触发一个 `userInvocable` 技能。加载的正文作为 user message 追加在所有注入之后，按「背景先放，要执行的材料最后放」的顺序。
6. turn 3：进程重启，会话恢复。`AGENTS.md` 的 `baselineIdentity` 与当前工作目录不一致，整份 baseline 重新注入；旧的 baseline 节点仍在历史里，但新消息声明「完整替换此前的指令基线」。
7. turn 4：再次压缩。上一个 checkpoint 被当作 prior checkpoint 合并，而不是复制；被遮蔽的原始消息仍然可以用 `session_event_read` 读出来。

同一段历史里，五层各自动了不止一次，但没有任何一层「拥有」这段对话。它们共享的只有 seq。

## 3. 写入方向：模型可见即记录

### 3.1 一条规则

DSH 仓库的根 `AGENTS.md` 有一行硬规则：

> **Model-visible ⟺ logged**: anything that reaches a model request must be reconstructable from the session log; a new model-visible input requires a session event.

它是准入条件，比「建议记一下」严格得多。任何插件想让模型看到新东西，只有一条合法路径：把它变成一个 surface 事件。

`agent.inject()` 看起来像旁路，走的却是同一条路。它把消息塞进 driver 的 inbox，inbox 是一个持久投影（`packages/core/agent-loop/src/inbox.ts`）：`agent/inbox/spliced` 事件记录插入位置与内容，`next-turn` / `next-step` 两个队列从事件折叠出来。pre-step 认领队列时，消息才变成 `user/message`。所以「注入」在日志里有两个时刻：排队时刻与认领时刻，两个都可回放。

### 3.2 这条规则约束了什么

不能注入不可序列化的东西。一个 live 对象、一个数据库连接、一个只在内存里存在的缓存，都进不了日志。你必须先把它渲染成文本，并为这段文本选一个 source。

不能有「只此一次」的隐藏输入。重试、恢复、fork 都会重放同一段历史。如果某条输入只在第一次请求时出现，第二次请求的模型看到的世界就变了。所以所有注入都要么幂等（同内容不重复发），要么显式记录「这是一次替换」。

不能靠副作用传递状态。「我上一个工具调用改了全局变量，模型应该知道」这种想法行不通。模型的可见状态只能来自日志；全局变量在恢复后不存在。

要为压缩做好准备。你注入的消息会被遮蔽。如果它是关键前提，摘要指令要能保留它；如果它是瞬时状态（时间、tmux 位置），就要接受它会被旧快照取代，并在文案上写成「本次采样」而不是「当前恒为」。

### 3.3 三个真实写入者

`AGENTS.md`：`packages/context/agent-instructions/src/index.ts:109` 的 `compose()` 在每次 pre-step 算出「这一 step 应该让模型看到哪些指令变化」，返回一条 `user/message`；`:330` 把它插到已认领消息之后。首次是 baseline，之后是 `set` / `replace` / `remove` 的增量。它的 source 带 `baselineIdentity`，恢复会话时如果工作目录或配置变了，旧 baseline 不再匹配，就整份替换。

技能目录：`packages/skill/tool-skill/src/index.ts:217` 的 pre-step 监听器重算目录。`:232` 算 digest，`:240` 与已发布目录比对，一样就直接返回，不一样才发 replacement。目录是持久的，不是每轮都发。

时间：`packages/context/time-context/src/index.ts:199` 的 pre-step 监听器在满足刷新间隔时追加一条 `source.kind === 'time-context'` 的 user message。它记录的是「采样时刻」，不是「现在」。

三个写入者，三种幂等策略：按 scope 增量、按 digest 全量替换、按时间间隔追加。共同点是都落成了事件。

### 3.4 `source.kind` 是记忆的类型系统

`source` 承担三件事：这条消息是谁写的、重放时该怎么解释、压缩后还要不要重发。把 DSH 里几个真实的 kind 列出来，能看出一张「写入者地图」：

| `source.kind` | 写入者 | 幂等策略 | 压缩后 |
|---|---|---|---|
| `agent-instructions` | pre-step 的指令探测器 | 按 scope 增量；baseline 按 identity 全量替换 | 摘要需保留仍然生效的规则 |
| `runtime-context` | 投影层 | 文本相同不发；为空发 CLEARED | 下一 step 重新投影 |
| `time-context` | 时间插件 | 刷新间隔 + 追加 | 旧采样仍在历史，文案写成「本次采样」 |
| `skill-catalog` | 目录发布器 | sha256 目录 digest | 摘要需保留「有哪些技能」 |
| `skill-invocation` | `skill` 工具 / `/name` | 每次调用都追加 | 正文可能被遮蔽，目录仍在 |
| `user` | 人类输入 | 不需要幂等 | 摘要的主要素材 |

一个 kind 对应一个写入者、一个去重键、一条压缩策略。新增一种注入，就是新增一行；没有这行，投影层就无法判断「这条消息是不是我说的那条」。

### 自己搭的时候

给你的注入定一个 `source.kind`，并且要求它满足三件事：能被 JSON 序列化、能在重放时得到同样文本、能在压缩后判断它是否还在 surface 上。做不到第三件，就不要把它叫记忆，它只是一次性的提示词拼接。

## 4. 读取方向：每次请求重新装配

### 4.1 loop 不持有历史

`packages/core/agent-loop/src/agent.ts:728` 在 turn 边界调用 `session.deriveMessages()`，不维护一个自增的 `messages` 数组。这个选择看起来低效：每次请求都重新折叠一遍日志。但它消灭了一整类 bug，也就是内存里的历史与日志里的历史不一致。

折叠的成本是 O(surface 节点数)，而 surface 已经被压缩控制在预算内；相比之下，一次模型调用的成本高几个数量级。用可重建性换一点 CPU，是划算的。

### 4.2 prompt 每次重新渲染

`assemble()` 在 pre-step 调一次（`agent.ts:294`），`refreshContext()` 在每次请求前再调一次（`agent.ts:446`）。前者组装 section、context、tool schema；后者只刷新「已被接受的运行时 context provider」，不重跑 waterfall，也不重新解析 section。

分成两段的原因：pre-step 之后、请求之前，还可能发生模型切换、工具集合变化、压缩。`refreshContext()` 的语义是结构不变，只把动态值取最新的。它返回的 assembly 里，section 与 tools 仍是 pre-step 的那份，context 的文本可能已经变了。

### 4.3 运行时上下文单独投影

`renderContextSections()`（`system-prompt/src/index.ts:336`）把每个 context 渲染成 `{ name, text }`，`joinContextSections()`（`:320`）拼成一段，加上一句固定的前缀：

> Current runtime context. This snapshot supersedes earlier runtime-context snapshots.

然后 `RuntimeContextProjection.project()`（`packages/core/agent-loop/src/runtime-context.ts:157`）决定要不要发一条新消息：内容与上一条快照一样就不发；内容为空但之前发过，就发一条 `Current runtime context: none. Earlier runtime-context snapshots no longer apply.`（`:20`）把旧快照作废。

「读取即装配」落到代码就是这一串动作：每次从 provider 取最新值，渲染，与已保留的快照比对，只在变化时追加一条带 `source.kind === 'runtime-context'` 的消息。状态不存进某个会话对象。

### 4.4 为什么这比「维护一个上下文对象」可靠

因为上下文对象的生命周期和进程绑定，而会话的生命周期比进程长。恢复、fork、压缩、模型切换，每一个都会让「进程内对象」与「日志里的世界」分叉。DSH 把权威放在日志，把渲染放在每次请求，把差异判断放在投影层。三层各管一件事，任何一层坏了都能从日志重放定位。

### 自己搭的时候

不要写 `this.history.push(message)`。写 `append(event)`，然后写一个纯函数 `deriveMessages(events)`。如果性能不够，缓存派生结果，但缓存的 key 必须是 `(sessionId, surfaceGeneration)`，而不是「我上次算到哪」。

## 5. 五层各自的失效方式

这一节最实用。记忆系统的 bug 大多出在「失效没设计」这一侧，存不进去反而少见。

### 5.1 静态指令的失效

- 注册失效：插件 fiber 销毁，section 消失；但已经渲染进 `system/message` 的文本还在 surface 上，下一次 `project()` 才会把它换掉。
- 内容失效：`AGENTS.md` 改了，digest 变了，但 pre-step 的 compose 可能因为「本 step 已经有 pending 消息」而复用旧消息（`compose()` 里那条 `if (no touchedPaths && pending 非空 && identity 相同) return pending[0]`）。实际更新发生在下一次 step 边界。
- 恢复失效：`baselineIdentity` 由 project root、markers、字节上限、候选文件名拼成。换了工作目录恢复会话，identity 不匹配，baseline 整份重发；否则旧规则会一直粘在历史里。
- 预算失效：`maxBytes` 是必填配置。超出时渲染会截断，并在开头加一句「指令因预算被省略或截断」。截断按 UTF-8 边界回退（`render.ts:84`），不会切出半个汉字。

### 5.2 会话的失效

- surface 替换：一个写错的 `surfaceOp` 会让节点从 surface 上消失，但事件仍在日志里。症状是「模型突然不记得上一轮」，排查要看 surface 折叠，而不是看日志条数。
- 持久化失败：append 成功但落盘失败，进程崩溃后尾部丢失。这类问题由 persistence 层负责，记忆层不重试、不补写，避免制造第二条真相。
- fork 截断：`inheritedEventCount` 之后的事件不会进入子会话。子代理看不到父代理 fork 之后发生的事，这是有意设计；但如果你把关键状态放在 fork 之后，子代理就会缺前提。

### 5.3 压缩的失效

- 摘要丢事实：最常见。摘要模板要求保留路径、命令、错误原文、数字、签名，但模型仍可能写成「修复了构建问题」。对策是把「必须保留什么」写进模板，并在摘要后保留一段原文尾部，换更大的模型解决不了这件事。
- 配对切断：工具调用与结果必须成对。`selectCompactableRange()`（`region.ts:117`）在选边界时会向前回退，直到 `toolPairingBalancedBefore` 通过，避免留下一个没有结果的 `tool/call`。
- 锁未闭合：`compaction/start` 写了、`compaction/end` 没写，说明崩溃发生在事务中间。下一次压缩会检测到未配对的 start 并拒绝启动，把状态暴露出来。
- 二次压缩：旧 checkpoint 也是 surface 节点。摘要指令要求把它当作「prior checkpoint」合并，而不是复制。否则每压一次就多一层套娃，事实在套娃里失真。

### 5.4 检索的失效

- 索引过期：文件改了、会话新增了，索引还没更新。因为检索是显式动作，模型会得到「旧答案 + 一个看起来合理的引用」，比没有答案更糟。对策是让结果带 `seq` 或内容摘要，模型能自己判断新旧。
- 视图不一致：索引把 `shadowed` 事件当成 `current` 返回，模型会引用一段已经被压缩掉的历史。`session-query` 用同一套 `foldSurface()` 分类，就是为了避免这种分叉。
- 权限越界：跨 workspace 检索必须限定在授权范围内。`session_search` 的「workspace-scoped」是安全边界，不是性能优化。

### 5.5 程序性记忆的失效

- 遮蔽：同名技能按 rank 取胜，项目级覆盖用户级。你以为在改用户级技能，实际生效的是项目里那份。
- 目录滞后：目录在 pre-step 发布。同一 step 内新建的技能文件，要等下一次 pre-step 才出现在目录里。
- 正文漂移：目录只存 `name + description`，正文在加载时才读。技能文件在「目录发布」与「模型加载」之间被改了，模型拿到的是新正文、旧描述。
- 调用策略：`disable-model-invocation` 的技能不会进目录，只能由用户 `/name` 触发。这类技能在权限上对模型不可见，跟「模型忘了」无关。

### 自己搭的时候

给每一层写一行「它怎么失效」，贴在设计文档里。写不出来，说明这一层没有生命周期，只是一个缓存。

## 6. 哪些层模型可见，哪些只是控制面

这是另一个常见混淆：把「系统里有」当成「模型看得见」。

| 内容 | 模型可见？ | 以什么形式 | 备注 |
|---|---|---|---|
| system prompt section | 是 | surface 节点 0 的 `system/message` | 每次请求重新渲染，变化时替换 |
| prompt context | 是 | 一条 user message | 整段快照，变化才追加 |
| `AGENTS.md` baseline / delta | 是 | 带 source 的 user message | 可被压缩遮蔽 |
| 会话事件 | 部分 | 只有 current surface 节点 | shadowed / log-only 不可见 |
| 压缩摘要 | 是 | 替换区间的那条 user message | 原文不可见，但可检索 |
| 会话全文索引 | 否 | 控制面 | 只有工具结果进上下文 |
| 技能目录 | 是 | user message | 只有名字与描述 |
| 技能正文 | 加载后是 | `tool/result` 或 `/name` 注入 | 未加载时不可见 |
| token 计量、surface 折叠 | 否 | 控制面 | 决定「带得动多少」，不进入文本 |
| `baselineIdentity`、digest 缓存 | 否 | 控制面 | 决定「要不要重发」 |

「我明明在系统里加了这条信息，模型为什么不用？」这张表给了答案：它停在控制面，从未被投影成 surface 事件。反过来也成立：一条消息一旦上了 surface，你就不能假装它没发生；它会被回放、被 fork、被压缩、被检索。

## 7. 五层与设计侧的五层怎么对

设计侧那篇给了 M0–M4。实现侧跟它不是一一对应，因为设计可以假设「想要什么」，实现必须回答「存在哪」。

{{< fig "diagrams/04-design-map.svg" "设计侧 M0–M4 到实现侧五层的映射。虚线表示「设计上有、实现上是别的机制」：工作记忆不是存储，语义记忆不是自动召回，资产不是记忆层。" >}}

- M0 工作记忆 → 压缩工作集 + 运行时上下文投影。设计侧说「每轮从场景图与焦点重建」；实现侧的重建发生在每次请求：`deriveMessages()` 折叠 surface，`renderContextSections()` 重渲染 context。没有一块叫「工作记忆」的内存。
- M1 情景记忆 → 会话日志。设计侧说「仅追加，唯一事实来源」；实现侧就是 `SessionEvent`，连压缩都不删除，只遮蔽。
- M2 语义记忆 → 检索层 + 日志里的命题。设计侧设想用关系库存「带证据的命题」；DSH 没有这个库。它把命题留在对话与摘要里，把「找回来」交给显式检索。这是一个明确的取舍：不做自动语义抽取，换取零双写、零抽取错误。
- M3 程序记忆 → 技能系统。设计侧的「带触发条件的版本化技能库」在实现里是目录约定 + rank + digest 目录 + 按需加载。
- M4 资产记忆 → 不属于记忆平面。品牌 token、画布状态、附件由各自的存储与工具负责；它们进入上下文的方式仍然是「作为工具结果或引用」，不是记忆层直写。

这张映射图里最值得记的一条：设计侧的「语义记忆」在实现侧被降级成了检索。原因在成本，自动语义抽取的写入成本与污染风险太高。设计侧自己也说「记忆的敌人是污染，不是遗忘」；实现侧的选择是：宁可不自动记，也不要记错。

## 8. skill 为什么是程序性记忆，不是 RAG

把 `SKILL.md` 塞进向量库、按相似度召回，看起来也能用。但它会丢掉五样东西，每一样都是「程序」而非「文本」的属性。

第一，触发靠名字，不靠相似度。技能有稳定的 `name`，目录里带 `description`，模型按任务显式调用 `skill({ name })`。RAG 的 top-k 召回没有「就是这一个」的语义；相似度 0.82 和 0.79 之间没有可解释的边界。程序性记忆要的是「这类任务对应这套步骤」，是分类，不是近邻。

第二，加载是整份的，不做切片。`renderSkillContent()`（`packages/skill/skill/src/index.ts:170`）把整份正文包进 `<skill_instructions>`，连同资源目录提示一起给模型。一个流程的步骤顺序、前置条件、失败回退，切开就不可执行。RAG 的 chunk 天生假设「片段可以独立支撑回答」，这与程序相反。

第三，来源与版本可判定。同名技能谁生效由 rank 决定：项目 `.dsh/skills` 覆盖用户级，内置最低。技能还有 `source`、`provider`、`resourceBase`，模型可以知道「这条指令来自项目，资源目录在哪」。RAG 的命中只给文本和分数，没有权威级别。

第四，加载是一个可审计的动作。模型调用了哪个技能、拿到多少内容、在哪个 step，都落在 `tool/call` / `tool/result` 里。目录变化也有 digest 与 replacement 消息。RAG 的自动召回通常不留痕：你不知道这次请求为什么多了三段文本。

第五，执行策略由权限决定，和相关性无关。`modelInvocable` / `userInvocable` 决定谁能触发。`disable-model-invocation` 的技能根本不进目录，模型无法通过「更相似的 query」把它捞出来。这是权限边界，向量库没有这个概念。

所以技能系统的形状是「目录 + 按需加载」，而不是「切片 + 召回」：目录常驻，因为你需要知道有哪些程序；正文按需，因为程序很长且大多数时候用不到。这套渐进披露（progressive disclosure）让常驻成本只有 `name + description`，同时保证加载到的是一份完整、可执行、可追溯的指令。

### 自己搭的时候

把「知识」和「做法」分开存。知识可以切片、可以检索、可以模糊；做法要整份、要命名、要版本、要权限。前者用检索工具，后者用目录 + 加载器。混在一起，你会得到一堆「看起来相关但没法执行」的片段。

## 9. 设计取舍

| 选择 | 放弃了什么 | 什么场景该反悔 |
|---|---|---|
| 仅追加日志作为唯一真源 | 就地更新与低延迟删除；日志会变大 | 合规要求「真删除」且不能保留 shadowed 副本时，需要独立的擦除通道 |
| system prompt 作为 surface 节点 | 请求头不再包含完整 prompt；调试要看 surface | 需要按请求做 prompt diff 审计、而 surface 节点被压缩遮蔽时，得额外导出 |
| 压缩用摘要替换工作集 | 原文的细节与措辞；摘要可能丢事实 | 任务强依赖逐字引用（合同、日志排障）时，应调低压缩频率或保留更长尾部 |
| 检索做成显式工具 | 自动召回的「无感」体验；多一次模型决策 | 高频、低延迟的问答场景，显式调用太慢，可做「预取但落日志」的折中 |
| 技能用目录 + 按需加载 | 模型不会「顺手看到」技能正文 | 技能很短且几乎总会用到时，目录 + 正文合并更省一次往返 |
| `AGENTS.md` 按目录层级 + 内容摘要 | 全量重发简单直接；增量逻辑复杂 | 指令文件很少变化时，全量重发的实现成本更低，也更不容易出错 |
| 语义记忆不做自动抽取 | 跨会话的「主动想起」 | 有稳定本体与人工校验管线时，可以加一层派生命题库，但仍需可追溯 |

## 10. 踩坑点

坑 1：把 `AGENTS.md` 当 system prompt。现象：重启后同一份指令出现两次。原因：它是持久 user message，恢复时 `baselineIdentity` 不匹配会整份重发；如果你又在 system section 里拼了一份，就有两份。对策：只保留一条写入路径，恢复逻辑只认 source，不认文本。

坑 2：注入不落日志。现象：fork 出来的子代理行为与父代理不一致，回放也无法复现。原因：某段上下文是运行时直接拼进请求的。对策：所有注入走 inbox 或 `session.append`，并在 code review 里把「直接改 messages」列为红线。

坑 3：摘要只写「做了什么」。现象：压缩后模型重复劳动、写回旧文件名。原因：摘要丢了路径、命令、错误原文。对策：模板里把「必须逐字保留」的类别写死，并在摘要后保留一段原文尾部作为锚点。

坑 4：把旧 checkpoint 原样复制。现象：压缩三次后摘要里全是「之前提到……」的套话。原因：没有按「合并旧 checkpoint」的指令生成。对策：摘要模板明确要求合并、去重、保留仍然为真的事实。

坑 5：用 mtime 判断指令文件是否变化。现象：`git checkout` 后所有 `AGENTS.md` 被重发一遍。原因：mtime 变了但内容没变。对策：用内容摘要（`digest.ts:14` 的 sha1）做变更判定，mtime 只作为快速路径。

坑 6：跨目录去重去掉了「作用域」。现象：子目录的 `AGENTS.md` 与根目录内容相同，被去重后子目录规则消失。原因：去重按内容全局做。对策：只在同一个 candidate group 内去重；不同目录即使内容相同也保留，因为适用范围不同。

坑 7：技能目录嵌深了。现象：`skills/foo/bar/SKILL.md` 一直不出现。原因：发现只支持 `<name>/SKILL.md` 或 `<name>.md`，不递归。对策：目录结构写进约定，加载失败时给出明确诊断，而不是静默跳过。

坑 8：以为目录实时。现象：刚写的技能，同一轮里模型说没有。原因：目录在 pre-step 发布，step 内不重算。对策：把「写完技能」与「使用技能」分到两个 step，或在工具结果里提示「下一次请求可见」。

## 11. 源码索引

- `packages/core/session/src/index.ts:999`：`deriveMessages()`，从 surface 节点折叠出模型历史；历史不单独存储。
- `docs/subsystems/session.zh.md:5`：「仅追加日志是唯一真源」的定义；`:302` 说明 `system/message` 是 surface 节点 0。
- `packages/core/agent/src/runtime-types.ts:270`：`inject(message: UserMessage): void`，合成输入的唯一入口。
- `packages/core/agent-loop/src/inbox.ts`：inbox 是持久投影，`agent/inbox/spliced` 记录插入与删除。
- `packages/core/agent-loop/src/agent.ts:294`：pre-step 调 `assemble()`；`:446` 请求前调 `refreshContext()`；`:728` turn 边界 `deriveMessages()`。
- `packages/core/system-prompt/src/index.ts:620`：`assemble()` 的合并、排序、waterfall；`:143-178` 是 section order 表。
- `packages/context/agent-instructions/src/index.ts:109`：`compose()` 组装 baseline / delta；`:330` 在 pre-step 插入。
- `packages/context/agent-instructions/src/files.ts:378`：按 project root 与祖先链发现指令文件；`:430` 按 candidate group 去重。
- `packages/context/agent-instructions/src/digest.ts:14`：整份内容的 sha1，用来判断「是否变化」。
- `packages/compaction/compaction-basic/src/region.ts:173`：压缩事务；`:507` 用 `surfaceOp: replace` 提交 checkpoint。
- `packages/compaction/compaction-basic/src/summarizer.ts:32`：摘要指令与必须保留的八个小节；`:70` 是 checkpoint 前缀。
- `packages/experimental/tool-session-query/src/index.ts`：`session_search` 等检索工具的契约，显式调用、workspace-scoped。
- `packages/skill/skill/src/index.ts:170`：`renderSkillContent()` 的整份加载格式；`:481` 目录快照。
- `packages/skill/skill-filesystem/src/index.ts:256-268`：技能根目录与 rank 顺序。
- `packages/skill/tool-skill/src/index.ts:217`：目录发布的 pre-step 监听；`:347` 目录 digest。
- `docs/subsystems/skills.zh.md`：技能层级、rank、目录格式与失效规则。

## 12. 小结

五层各自要说清的是：谁在什么时候写、谁在什么时候读、什么时候作废。存了什么反而次要；这五个问题答不上来，长任务里的 Agent 就会变成一个自信的陌生人。

下一篇拆开第二层与第四层之间的那段路：[上下文装配：AGENTS.md、时间与文件引用](/posts/dsh-context-injection.html)。再下一篇是压缩本身：[压缩：把上下文变成工作集](/posts/dsh-compaction.html)。

上一篇：[工作流、后台任务与目标：三种「稍后继续」](/posts/dsh-workflow-jobs-goal.html) ｜ 下一篇：[上下文装配：AGENTS.md、时间与文件引用](/posts/dsh-context-injection.html) ｜ [系列目录](/posts/dsh-architecture-anatomy.html)
