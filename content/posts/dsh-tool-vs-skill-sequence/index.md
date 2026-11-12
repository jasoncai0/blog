---
title: "工具与技能：同一次请求里的两个位置"
date: "2026-11-12T15:45:00+08:00"
summary: "工具是框架要发的声明，技能是模型要读的说明书。一个在请求头，一个在消息尾；一个由框架执行，一个由模型遵循。把两条链路摆到同一条时间轴上，就能看出为什么它们的注册、发现、激活长得完全不一样。"
description: "从 register 到 tool/result，从 registerProvider 到 skill_content：两条链路的六次与五次握手、请求里的位置差异、缓存代价、可见性与目录发布的联动，以及八处容易踩空的地方。"
tags: ["harness", "architecture", "context-engineering", "agent"]
slug: "dsh-tool-vs-skill-sequence"
---

> 源码版本：`deepseek-harness` v0.2.2-rc.1（commit `fcc0ad46`）。本文对照 `packages/core/tools/src/{index.ts,pipeline.ts,batch.ts,contracts.ts}`、`packages/core/agent-loop/src/agent.ts`、`packages/core/system-prompt/src/index.ts`、`packages/llm/llm/src/{content.ts,types.ts}` 与 `packages/skill/{skill,tool-skill,skill-filesystem}/src/index.ts`。
> 工具执行平面（第 9–12 篇）讲的是流水线内部；这一篇把工具和技能放进同一条时间轴，看它们在请求里的位置差在哪。

先看三个会让人愣一下的现象。

第一个：你在项目的 `.dsh/skills/` 下放了一个 `SKILL.md`，模型下一轮就"知道"它存在了，可它在 `tools[]` 里查不到；反过来，你 `ctx.tools.register` 一个工具，模型立刻能调，但 `<available_skills>` 里永远没有它。两个都叫"扩展能力"，进场方式却完全不搭边。

第二个：删掉一个工具，下一次请求的 `tools` 数组少一项——整个前缀缓存作废，账单上能看见；而"加载一个技能"往会话里追加了两条消息，缓存一点没动。

第三个：技能正文里写着"运行 `python -m pytest`"，但那条链路上没有任何一行代码去执行它。真正执行的是 `bash` 工具，技能只提供了"该跑什么"。

这三个现象指向同一件事：**工具和技能不是同一层的两种实现，它们是同一次请求里两个不同的位置。** 位置决定了谁能看见它、谁负责执行它、改动它的代价是多少。

## 0. 先给结论

| 维度 | 工具（Tool） | 技能（Skill） |
|---|---|---|
| 本质 | 一段可被调用的能力：有 schema、有执行体 | 一段给模型读的指令：只有文本 |
| 谁执行 | 框架：batch 调度 + 四段流水线 | 模型：照着正文做，动手的还是别的工具 |
| 发现发生在 | 框架内部：注册表派生视图，模型看不见过程 | 模型上下文里：摘要被发布成一条消息 |
| 模型看到的东西 | `{name, description, parameters}`，在 `tools[]` 里 | 一行 `- name: description`，在 messages 里 |
| 变化怎么表达 | 新的 `request/header` + 一条 `tool-addition` developer message | 一条 `skill-catalog` user message（首次全量，之后 update） |
| 在请求里的位置 | system 之后的前缀 | messages 的尾部 |
| 层内同名 | 抛错：部署错误 | 按 rank 排序后静默丢弃：优先级表达 |
| 激活成本 | 一次 `tool_call` | 一次 `tool_call` + 一份正文进上下文 |
| 治理手段 | restrict / when / guard / approval / timeout / pre-execute / post-execute | modelInvocable / userInvocable + 目录绑定工具可见性 |
| 失败的典型形态 | 注册就崩，或这一步准备失败 | 目录里没有它，模型压根不知道 |

一句话版本：**工具靠协议进场，技能靠上下文进场。前者要能被机器调用，后者要能被模型读懂。**

## 1. 工具的一生：六次握手

下面按顺序走一遍，每一步都标出它失败时是什么样子。

{{< fig "diagrams/01-tool-sequence.svg" "一次工具调用的六次握手。声明在 pre-step 被装配进请求头，执行发生在模型返回之后；模型全程只看见前三步的产物。" >}}

### 1.1 注册：一个 effect，不是一次 push

`register(definition, options)` 在 `packages/core/tools/src/index.ts:128`。它只做三件事，但三件都是"错了就别启动"级别的：

第一，`output` 必须有 `schema` 和 `render`，少一个直接 `TypeError`，文案是 `tool "<name>" must declare output { schema, render, presentationMeta? }`。第二，`parameters` 要过 `assertSupportedJsonSchema`，并且 `timeoutMs` 必须是正有限数。第三，`run_code` 是保留名，既不能注册也不能被遮蔽。

最后它落在 `this.layers.effect(this.ctx, layer => layer.tools.insert(name, { definition, when: options.when }), { label: 'tools.register()' })`。注册是一个 Cordis effect，返回 disposer——这解释了为什么插件卸载时工具会干净消失：不是有人去删数组，是 effect 的反向执行。

同一层里同名会直接抛错。这不是疏忽，是取舍：工具是"能力契约"，两个实现抢一个名字，静默选一个比崩掉更危险。

### 1.2 可见：视图是算出来的

`view(scope)`（`index.ts:199-227`）把 global 层和祖先链上的层从远到近叠起来，近的覆盖远的，再过一遍 restrictions，产出 `visible / registrations / knownNames / restrictableNames`。

这里没有缓存。因为它本来就是纯派生：几次 Map 查找，比维护一份失效逻辑便宜。

`restrict()` 只能用在 scoped ctx 上，而且只能收紧不能放宽——allow/deny 按集合求交，写一个不存在于祖先层的名字会直接报错。这是"能力只能被收回，不能被凭空发明"的写法。

### 1.3 准入：`when` 是每步一次的门

`resolve(context)`（`index.ts:264-297`）对每个可见工具跑一遍 `when`。可见不等于可用：`when` 返回 false 就不进声明表，抛错则包成 `ToolActivationError`（`TOOL_ACTIVATION_FAILED`），让这一步的准备失败。

这是"声明不便宜"的第一个证据：如果准入可以静默跳过，模型会看到一个时有时无的工具集；DSH 选择让它显式失败，因为你宁可这一步不跑，也不要模型按一份错的清单去规划。

返回的 `ResolvedToolSet` 是冻结的，它的 `view(scope)` 每次还会重新校验注册是否仍然存活——工具在 await 期间被卸载，这一步就看不见它了。

### 1.4 声明：白名单投影

`executor.describe(tools)` 拿到的 schema 经过 `projectToolSchema`，只留 `name`、`description`、`parameters`（外加 `deferLoading`）。`timeoutMs`、`isConcurrencySafe`、`output.schema` 这些字段永远不发给模型——`contracts.ts:63-68` 把这条写成了注释。

然后 `system-prompt` 的 `assemble()` 收集所有 provider 的 schema，按配置的 `toolOrder` 排序，未列出的按码点序插进 `<unlisted-tools>` 段，最后落进 `request/header.tools[]`。

### 1.5 调用：批调度加四段流水线

模型一次响应可能带多个 `tool_call`。进执行前先分类：`exclusive` 形成屏障，`parallel` 用有界滚动池（默认并发 10）。dispatch 可以重叠，但策略裁决、结果提交和上下文注入都按模型给出的顺序来；中途 abort，没启动的调用会被合成一条 `ABORTED_BEFORE_DISPATCH`，而不是留个空洞。

每个调用走四段：`prepare` 快照并冻结参数、跑 `tools/pre-execute`（allow / deny / cancel / ask）、再跑 guard；`dispatch` 包在 `tools/execute` waterfall 里，进 body 后再查一次注册表（查不到就是 `UNKNOWN_TOOL`），然后 `await tool.execute`；`complete` 先 `projectContent` 再 `tools/post-execute`（accept 可换 content 或 value，block 转成错误加反馈）；`finish` 做最后一次 `finalizeContent`，物化，发 `tools/result`。

成功值要过 `output.schema`，不合法抛 `INVALID_TOOL_OUTPUT`。**到这一步才算"成功"，不是 `execute` 返回了就算。**

### 1.6 结算：一条 `tool/result`

结果 append 进会话，之后的每一步请求都从会话里投影出来。仓库有一条硬约定：Model-visible ⟺ logged——模型能看见的，必须已经落账；反过来，落账的东西才是模型能看见的。

于是"工具变了"这件事也有账可查：`buildRequest` 比较前后两次请求头的工具名集合，有差异就追加一条 developer message，内容形如 `{ type: 'tool-addition', toolName }` 或 `{ type: 'tool-removal', toolName }`，source 是 `{ kind: 'tool-registry' }`。会话投影时（`packages/core/session/src/tool-history.ts`）把它和请求头折在一起，还原出"那一步模型到底有哪些工具"。

## 2. 技能的一生：五次握手

技能这条链路短一级，但它横跨两个阶段：注册期只有 provider，对话期才谈得上候选和正文。

{{< fig "diagrams/02-skill-sequence.svg" "一次技能激活。目录在 pre-step 被发布到消息尾部，模型据此发起一次普通的 skill 工具调用；正文以 tool/result 的形式回到上下文。" >}}

### 2.1 注册：注册的是"怎么找"，不是"有哪些"

`registerProvider(create)`（`packages/skill/skill/src/index.ts:390-428`）收一个工厂，工厂拿到 control（一个 `signal`，一个 `invalidate()`）。注册的是"发现能力"，不是"技能清单"。

所以发现的产物是 `SkillCandidate`：摘要（name / description / 是否可被模型或用户调用）+ `rank` + 一个不透明的 `locator`。正文不在里面。

`register()` 是另一条路：直接在运行时登记一个技能，scope 内同名 first-wins，默认 `modelInvocable` 与 `userInvocable` 都为真，provider 记为 `runtime`。

六个根按优先级排：项目 `.dsh/skills`（100）、项目 `.agents/skills`（200）、自定义目录（300）、`$DSH_HOME/skills`（400）、`$DSH_AGENTS_HOME/skills`（500）、内置（600）。数字越小越优先。

### 2.2 发现：一次合并，一层缓存

`snapshot({ cwd, scope })` 触发 collect。缓存键是 `{ cwd, scopes, revision }`；命中就直接返回，不碰磁盘。revision 在收集过程中变了就重试，最多两次，还变就标 `cacheable: false`——宁可下次重新算，也不给一份自相矛盾的快照。

某个 provider 抛错呢？只 warn，把结果标成 incomplete，整体不失败。这条很关键：技能发现是"尽力而为"的，一个坏掉的目录不该让整个会话起不来。

层内按 rank 升序、provider 注册序、本地序排；同名只保留第一个并 warn。跨层则是近者覆盖远者——实现里注释写得很直白：**"the tools registry's shadowing rule"**。

### 2.3 发布：把摘要挂到消息尾部

这是技能最特别的一步。`tool-skill` 在 `agent/pre-step` 上挂了一个监听器（`packages/skill/tool-skill/src/index.ts:217-255`），每一步都算一次目录的 sha256 digest（条目 + incomplete 标记），和会话里最后一条 `skill-catalog` 消息比对。一样就什么都不发；不一样就发一条 user message：首次是全量 `<available_skills>`，之后是 update。

两个细节值得记：描述会被截断到 500 字符，超出加省略号；目录里明确写着"这里只有摘要，别照着摘要办事，先 load"。

还有一个隐藏开关：只有 `ctx.tools.get('skill', agent) === skillTool` 时才发布。也就是说，技能目录的可见性绑定在"skill 这个工具还活着"上。

### 2.4 激活：一次普通工具调用

`skill({ name })` 是 `tool-skill` 注册的普通工具，参数只有一个字符串。它的 execute 依次做：校验名字合法 → `workingDirectory.ensure` 拿到 cwd → `list()` 找到候选 → 校验 `isModelInvocable` → `get(name)` → 交给 provider 的 `get(locator)` 取正文 → 校验返回的 name 和候选一致（不一致就 invalidate 并返回 undefined）。

`get` 里有个细节：provider 的加载和调用方的 `signal` 赛跑（`waitWithAbort`）。provider 卡住不会挂住整个回合，但也不会被强杀。

另一条入口是用户的 `/name` 手势：pre-step 扫 user 消息里的文本块，用 `SKILL_GESTURE` 正则匹配 `/kebab-case`，命中就取正文、注入成一条 `source: { kind: 'skill-invocation' }` 的 user message。这条只走 `userInvocable`，也是 `disable-model-invocation` 技能的唯一入口。

### 2.5 遵循：正文进上下文之后

正文被 `renderSkillContent` 包成 `<skill_content name="…">`、`<skill_resources>`（说明相对路径的 base）、`<skill_instructions>`，作为 `tool/result` 回到会话。从此它和任何工具结果没有区别：都是历史的一部分。

然后模型照着做。真正动手的是 `bash`、`read`、`write`——**技能这一步没有任何执行权**。

## 3. 同一个请求里的两个位置

把两条链路叠到"一次请求"这张图上，差异一目了然。

{{< fig "diagrams/03-two-positions.svg" "两条能力在请求里的位置。工具声明在 system 之后的前缀，技能目录与正文在 messages 的尾部；改头和加尾，缓存代价差一个数量级。" >}}

工具在 `tools[]`。它是请求结构的一部分，位置在 system 之后、messages 之前。增删一个工具，等于改写前缀——从 `tools` 段开始的所有 token 都要重算。所以 DSH 才需要 `deferLoading`：新增的工具先以"延迟加载"的形态声明，等模型真的要用，再由一条 `tool-addition` 把它"点亮"，而不是每次都重写整张声明表。

技能在 messages 的尾部。目录是一条 user message，正文是一条 tool/result，全是 append。append 不改前缀，缓存照常命中。所以这里不需要任何"补丁协议"——但也因此需要另一种克制：digest 去重，目录没变就一条都不发。

这就是为什么两条链路的技术栈完全不同：

- 工具的变更信号是**结构性的**（新 header + developer message + provider 的原生 `tool_addition` 语义），因为它必须改结构才能表达"多了一个可调用的东西"。
- 技能的变更信号是**对话性的**（一条 user message），因为它只需要"告诉模型一件事"。

## 4. 六级台阶与五级台阶

把两条链路压缩成状态，是两列台阶。

{{< fig "diagrams/04-ladder.svg" "两条状态阶梯：工具六级、技能五级。中间的虚线是唯一的硬耦合——skill 工具不可见时，目录也不会发布。" >}}

工具：注册 → 可见 → 准入 → 声明 → 调用 → 结算。六级，每一级都能失败，每一级的失败形态不一样（启动崩、视图为空、整步失败、声明缺失、批调度取消、输出不合 schema）。

技能：注册 → 发现 → 发布 → 激活 → 遵循。五级，少的那一级恰好是"执行"——因为执行不归它管。

两处不对称值得单独说：

**层内同名。** 工具抛错，技能静默丢弃。这不是双标：工具是契约，两个实现抢一个名字是部署事故；技能是知识，同名意味着"我有一个更优先的版本"，rank 就是用来表达这件事的。

**跨层遮蔽。** 两边一致：近者覆盖远者。技能的实现里甚至直接引用了工具注册表的规则名。

唯一的硬耦合在中间那条虚线上：`skill` 工具被 restrict 掉或被遮蔽，目录就不再发布。一个开关控制两件事——你不需要单独设计"技能的作用域"。

## 5. 为什么会长成这样

五条机制，能解释掉上面几乎所有的差异。

**第一，调用协议决定载体。** 要被模型调用，就必须有机器可读的契约：名字、参数 schema、返回值 schema。这三样东西只能放在请求的结构化字段里，不能混在自然语言里。技能不需要被"调用"，它只需要被"读懂"，所以它可以是一段 markdown。

**第二，缓存经济学。** 前缀改动贵，尾部追加便宜。工具声明在前缀，所以要想办法少改（deferLoading、addition-only）；技能在尾部，所以要想办法少写（digest 去重）。同一个"少动"的原则，落到两个位置就是两套实现。

**第三，执行权决定治理强度。** 工具能删文件、能发请求，所以有 guard、有 approval、有 timeout、有 pre/post 两道裁决。技能只能提供文字，所以治理只剩"谁能激活它"（两个 invocation 开关）和"它能不能出现"（目录绑定工具可见性）。

**第四，冲突语义反映权威来源。** 工具的名字来自代码，冲突是事故；技能的名字来自目录约定，冲突是优先级。所以一个崩，一个排序。

**第五，发现成本决定缓存策略。** 工具的视图是内存里几次合并，不需要缓存；技能的发现可能要扫六个目录、读 frontmatter、甚至走网络（provider 可以是任意实现），所以必须有 revision 缓存、重试上限和 incomplete 降级。

## 6. 交汇点：技能是工具的用户

如果只记一件事，记这个：**技能这条链路本身，是工具链路的一个用户。**

`skill` 是一个普通工具：注册在 tools registry 里，走同一条 batch、同一条四段流水线、同一个 `tool/result` 通道、同样的 schema 校验。它唯一的特殊之处，是它自己会在 pre-step 里往会话里塞消息。

于是产生了一个不对等的依赖：技能目录的可见性，取决于工具注册表里 `skill` 这个名字还在不在。restrict 掉它，目录一起消失；反过来，已经注入历史的 `<skill_content>` 不会回滚——它是历史，历史不撤回。

这也解释了一个常见的困惑："我关了技能，为什么模型还在按技能做？"因为它已经读过了。控制"能不能读"和"读过之后还记不记得"是两件事，后者只能靠新消息去纠正。

## 7. 自己搭 Agent 时该怎么做

**第一，先分清"能力"和"知识"。** 需要被调用、需要参数校验、需要权限控制的，做成工具；只是"告诉模型怎么做"的，做成可加载的文档。硬把一个流程塞进工具，会让 schema 变成一堆没人看的字段；硬把能力写成文档，模型会照着文档编造调用。

**第二，把"发现"和"加载"拆成两段。** 常驻上下文里的应该只有摘要，正文按需拉取。这条对成本和准确率都是正收益：摘要让模型知道"有这个"，正文只在真的要做事时才占 token。

**第三，让变更信号走对通道。** 结构化能力的变化，走结构化的更新（新声明 + 一条说明消息）；知识的变化，走对话（一条新消息）。两者混用，就会出现"工具在 system prompt 里被描述成一段话"这种既不能调也不能读的东西。

**第四，给"发现"留降级路径。** 发现可能失败（目录不存在、provider 超时、frontmatter 写错）。失败应该让目录变小、并标明不完整，而不是让整个会话起不来。

**第五，把可见性做成一个开关，而不是两套配置。** 技能的可见性绑在工具可见性上，省掉了一整套"技能作用域"的设计；代价是耦合，收益是少一套要记的规则。

## 设计取舍

1. **工具声明用"全量 schema + deferLoading 补丁"，而不是"按名检索"。** 代价是要维护一条 `tool-addition` 的会话投影，还要处理不支持增量更新的路由（退化成全量声明）。收益是模型永远有一份完整的清单，不需要先"搜工具"再"用工具"。

2. **技能用"摘要常驻 + 正文按需"两段式。** 代价是要多一次工具调用、多一轮往返；收益是几百个技能的目录也只占几 K token，而正文只在需要时进上下文。

3. **目录发布绑定工具可见性。** 代价是耦合（想在不再发布目录的前提下保留 skill 工具，做不到）；收益是不需要设计第二套作用域。

4. **同层重名：工具崩、技能排序。** 代价是两套冲突语义，学习成本高一点；收益是每种语义都贴合它治理的东西。

5. **技能没有执行权。** 代价是"技能"这个概念比很多人预期的弱——它不能自己跑命令；收益是权限模型只需要管工具一处。

6. **取消不放弃已启动的调用。** 代价是取消之后还要等静止（quiescence）；收益是不会出现"模型以为没执行、实际执行了"的分叉。

## 踩坑点

1. **把技能当执行器。** 现象：写了个"部署技能"，指望它自己跑脚本。原因：技能只返回文本，执行的是后面的工具。对策：把动作写进正文，让模型用 `bash` 去跑；或者干脆做成工具。

2. **注册工具时漏了 `output`。** 现象：进程起不来，报 `tool "<name>" must declare output { schema, render, presentationMeta? }`。原因：结果要投影成"模型的 content"和"UI 的 meta"两份，`render` 是必填。对策：先写 `render` 再写 `execute`，强制自己想清楚模型该看到什么。

3. **指望 `deferLoading` 一定省 token。** 现象：某些路由下声明照样全量发。原因：`deferLoading` 依赖 provider 支持 `tool-addition`；不支持的路由会把所有声明去掉 `deferLoading` 变成立即可用。对策：把它当优化，不当契约。

4. **改了 `SKILL.md` 以为下一轮自动生效。** 现象：目录更新了，但模型还在按旧正文做。原因：目录靠 digest 会刷新，已注入的正文是历史，不会重发。对策：让模型重新调用 `skill({name})`，或在新消息里明确纠正。

5. **技能里写相对路径却没声明 base。** 现象：模型把 `./scripts/x.sh` 解析到了项目根，而不是技能目录。原因：正文里的相对路径要靠 `resourceBase` 说明基准。对策：在正文开头写清"以下路径相对本技能目录"，并让 provider 给出 `resourceBase`。

6. **给技能加了 `disable-model-invocation` 又抱怨模型不用它。** 现象：目录里没有它，模型从不主动加载。原因：这个开关只保留 `/name` 手势入口。对策：要么让用户显式调用，要么别关。

7. **以为 restrict 掉 skill 工具，历史里的技能也会消失。** 现象：目录不发了，模型还在按之前的技能做事。原因：目录是"未来"的可见性，`<skill_content>` 是"过去"的历史。对策：需要纠正就发一条新消息，别指望撤回。

8. **在 `when` 里做重活。** 现象：每步都变慢，或者工具时有时无。原因：`when` 每一步都会跑，而且抛错会让整步准备失败。对策：只做轻量判断，重的探测放到注册期或缓存里。

## 源码索引

| 位置 | 证明什么 |
|---|---|
| `packages/core/tools/src/index.ts:128-160` 的 `register` | output 必填、`run_code` 保留名、注册是 effect |
| `packages/core/tools/src/index.ts:199-227` 的 `view` | 全局 + 祖先链合并、restrict 过滤、纯派生无缓存 |
| `packages/core/tools/src/index.ts:264-297` 的 `resolve` | `when` 求值与 `TOOL_ACTIVATION_FAILED` |
| `packages/core/tools/src/index.ts:316-318` 的 `schemas` | 只投影 name/description/parameters |
| `packages/core/tools/src/batch.ts:52-107` | exclusive 屏障、并行滚动池、`ABORTED_BEFORE_DISPATCH` |
| `packages/core/tools/src/pipeline.ts:190-389` | prepare / dispatch / complete / finish 四段与各自的失败码 |
| `packages/core/system-prompt/src/index.ts:622-700` 的 `assemble` | 收集 schema、`orderTools`、未列出者的插入位置 |
| `packages/core/agent-loop/src/agent.ts:656-745` 的 `buildRequest` | 请求头、`tool-addition` / `tool-removal` 的生成与 headerSeq |
| `packages/llm/llm/src/content.ts:435-500` 的 `projectToolUpdates` | 延迟声明如何被点亮；无 toolUpdate 时的退化 |
| `packages/core/session/src/surface.ts:382-392` | addition 必须对应头里完整定义，且 `deferLoading` 为真 |
| `packages/skill/skill/src/index.ts:390-460` | `registerProvider` / `register`，runtime 同层 first-wins |
| `packages/skill/skill/src/index.ts:519-549` 的 `collect` | 缓存键、两次重试、`cacheable:false` |
| `packages/skill/skill/src/index.ts:551-565` 的 `collectFresh` | 跨层近者覆盖，注释点名 tools 的遮蔽规则 |
| `packages/skill/skill/src/index.ts:170-183` 的 `renderSkillContent` | `<skill_content>` / `<skill_resources>` / `<skill_instructions>` 的包络 |
| `packages/skill/tool-skill/src/index.ts:84-164` | `skill` 工具的 schema、execute 与输出校验 |
| `packages/skill/tool-skill/src/index.ts:217-255` | 目录发布：digest 比对、首次全量、之后 update、绑定工具可见性 |
| `packages/skill/tool-skill/src/index.ts:181-208` | `/name` 手势：扫 user 文本、只走 `userInvocable` |
| `packages/skill/skill-filesystem/src/index.ts:251-270` | 六个根与它们的 rank |

## 最后

工具和技能经常被放在一张图里讨论，好像它们是同一层里的两种插件。实际上它们连"在哪一层"都不一样：工具在协议层，技能在内容层。

协议层的东西必须严格：schema 校验、可见性、准入、裁决、超时、取消。内容层的东西可以松：摘要、正文、相对路径、优先级。把严格的那套套到内容上，会得到一堆没人维护的 schema；把松的那套用到协议上，会得到一堆模型编造的工具名。

下次设计 Agent 的扩展机制，先问一句：这个东西需要被**调用**，还是需要被**读到**？

答案会决定它住在请求头的哪一段，也会决定你未来要维护的是哪一套缓存。
