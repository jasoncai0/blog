---
title: "Skill 子系统：目录先到，正文按需加载"
date: "2026-11-13T12:00:00+08:00"
summary: "把 SKILL.md 从磁盘到模型眼前的路径拆成三段：六个根上的发现、目录消息的增量替换、按需加载的正文。顺带回答一个常见误解：SKILL.md 并不进 system prompt。"
description: "DSH Skill 子系统实现拆解：skill / skill-filesystem / tool-skill / skill-office 的分工，rank 与层叠，frontmatter 校验，目录 digest，渐进披露的 token 账，以及与 tool、memory、subagent 的边界。"
tags: ["harness", "context-engineering", "memory"]
slug: "dsh-skill-system"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准。
> 这是第二季的第 6 篇。第一季的解剖在[《一个 Agent 框架的解剖》](/posts/dsh-architecture-anatomy.html)，本季导览在[《第二季：把子系统拆到能复现》](/posts/dsh-season-two.html)。

你在[《工具与技能：同一次请求里的两个位置》](/posts/dsh-tool-vs-skill-sequence.html)里已经回答过「什么时候用工具、什么时候用技能」：工具是可执行的函数，技能是一段按需加载的指令。那篇讲的是选择。这一篇讲实现：一段 `SKILL.md` 从磁盘到模型眼前，中间经过哪几个包、在哪一步变成什么形态、每一步花掉多少 token、哪一步出错时模型会看到什么。

先看两个容易想反的地方。第一个，很多人以为技能会像 `AGENTS.md` 那样常驻在 system prompt 里。抓一次真实请求会发现，system prompt 里跟技能有关的只有 `skill` 工具的一行 description，正文一个字都不在。第二个，以为技能目录里会有正文摘要。目录里只有 `name` 和 `description`，`description` 还被截到 500 字符，正文要等模型显式调用 `skill` 才出现。

{{< fig "diagrams/01-injection-path.svg" "图 1：三种指令文件各自走到请求的哪个位置。system prompt 里只有工具 schema，AGENTS.md 与技能目录都是 user 消息，技能正文是 tool result。" >}}

这两件事看起来像实现偷懒，实际是同一套取舍的两个面：常驻的内容付每一轮的 token 和 KV cache 代价，按需的内容付一次加载延迟和一次工具调用的决策成本。技能站在按需这一侧，于是它需要一套目录来让模型知道「有什么可加载」，还需要一套失效机制来保证目录跟磁盘一致。剩下的篇幅都在拆这两套东西。

system prompt 里跟技能有关的只有 `skill` 工具那一段描述。`ToolsService` 在构造时注册一个 `systemPrompt.tools()` provider，`assemble()` 把所有可见工具的 schema 投影成 `{name, description, parameters}`，`skill` 就是这样进去的。没有技能章节，正文也不在里面。`packages/core/system-prompt/src/index.ts:551`。

## 0. 结论

1. 四个包分工明确：`dsh-skill` 是注册表与渲染，`dsh-skill-filesystem` 是本地 provider，`dsh-tool-skill` 是面向模型的消费者，`dsh-skill-office` 是内置 provider。
2. `SKILL.md` 不进 system prompt。prompt 里只有 `skill` 工具的 schema；目录是 durable 的 user 消息；正文是 tool result 或 user 消息。
3. 目录只发 `name` 和截断后的 `description`，按 digest 比对，变化时追加一条替换消息，不原地改。
4. 渐进披露有三段：目录、正文、正文里点名的资源文件。第二段由模型或用户触发，第三段由模型自己 `read`。
5. 发现走六个根，rank 只在单层内裁决，跨层是近者覆盖。层内同名按 rank、provider 顺序、局部顺序排队，输的那个被静默跳过。
6. frontmatter 校验很严，少一个字段、拼错一个布尔值，整个技能被跳过并记一条 warn，模型只看到它从目录里消失。
7. 注册表不缓存正文，每次 `get()` 重读文件；watcher 只让发现缓存失效，不推送内容。
8. 技能没有执行权。它加载后能做什么，取决于当时可见的工具和审批策略。

## 1. 三种指令，三条路

DSH 里有三种「让模型按规则做事」的载体，它们的注入路径完全不同，混在一起谈就会得出错误结论。

工具 schema 走 system prompt。`ToolsService.register()` 把定义放进当前作用域的层，组装时 `projectToolSchema` 只投影 `{name, description, parameters, deferLoading?}`，再由 `systemPrompt.tools()` 的 provider 收进 `tools[]`。`packages/core/tools/src/schema.ts:641`。技能在这里的痕迹只有 `skill` 工具本身。

`AGENTS.md` 走 user 消息。`packages/context/agent-instructions/src/index.ts:85` 的 `apply()` 在 pre-step 里组装一条 `source.kind = 'agent-instructions'` 的 user 消息，基线版本带 `baseline: true`，后续变化带 `changes`。它有字节预算：`maxBytes` 由组合层配置，按指令链分别计算，超了会截断并在文本里写 `Workspace instruction budget <n> bytes: truncated …`。文件按目录层级发现，`AGENTS.md` 与 `CLAUDE.md` 都在候选表里，同目录 trimmed 内容相同的副本会被去重。

`SKILL.md` 走两条路。目录消息是 user 消息，`source.kind = 'skill-catalog'`；正文是 `skill` 工具的返回值，走普通 tool result。两者共用 `renderSkillContent` 的包装格式，所以模型看到的正文形态一致。

| 载体 | 谁解析 | 何时进上下文 | 形态 | 预算 |
| --- | --- | --- | --- | --- |
| 工具定义 | ToolsService | 每次请求 | `tools[]` schema | 只投影三个字段 |
| `AGENTS.md` | agent-instructions | 首个请求前、文件变化后 | user 消息 | `maxBytes` 按链截断 |
| 技能目录 | tool-skill | 首个请求前、目录变化后 | user 消息 | 每条 description 截 500 |
| 技能正文 | tool-skill 调注册表 | 模型调用 `skill` 或用户 `/name` | tool result / user 消息 | 无上限，按文件大小 |

第一季的[《工具注册表：作用域、叠加与遮蔽》](/posts/dsh-tool-registry-scope.html)讲过工具 schema 怎么从层里叠出来，那条链路在这里只作为对照。技能目录不进 `tools[]`，它没有 schema，也不参与工具名的遮蔽。

### 自己搭的时候

先给你的规则分类，再选载体。每次对话都必须遵守的约束（代码风格、禁用的命令、提交格式）放 `AGENTS.md`，它进上下文早，代价是每轮都占 token。只在特定任务里需要的步骤（发版流程、表格制作、某个内部系统的操作顺序）放 `SKILL.md`，代价是模型要先看一眼目录、再决定加载。需要实际执行动作的能力写工具，技能只能描述怎么用工具。

## 2. 四个包，四种所有权

`dsh-skill`（`packages/skill/skill/src/index.ts`，868 行）是 Service Definition。它定义 `SkillSummary`、`SkillCandidate`、`SkillDefinition`、`SkillRegistration`，实现分层注册表、发现缓存、`list/snapshot/get`，以及所有消费者共用的 `renderSkillContent`。它不认识文件系统，也不认识模型。

`dsh-skill-filesystem`（1063 行）是一个 provider。它把六个根目录里的文件变成 candidate，负责 frontmatter 解析、rank 分配、watcher 与失效。它实现 `SkillProvider` 接口：`list()` 返回候选，`get()` 返回正文。

`dsh-tool-skill`（451 行）是消费者。它注册 `skill` 工具，在 `agent/pre-step` 发布目录消息，处理 `/name` 手势。它不发现文件，只调用注册表。

`dsh-skill-office`（95 行）是另一个 provider，把包内自带的 7 个 Office 技能注册进来。它证明 provider 接口不只服务文件系统：候选可以来自任意地方，正文可以在 `get()` 时动态拼接。

注册表用 `ScopedLayers` 管理层，和工具注册表同形。全局层放 host 与 repository 插件的注册，agent preset 常驻插件的注册落在这个 preset 的层。读取时合并全局层与 scope 链，`collectFresh` 从远到近逐层 `merged.set(name, entry)`，近层直接覆盖同名，`packages/skill/skill/src/index.ts:551`。rank 只在单层内比较，跨层不比 rank。这一点和工具注册表一致，层是硬边界，rank 是层内的排序键。

provider 的注册是同步的，发现是异步的。`registerProvider(create)` 立刻调用工厂拿到 provider 对象，`create` 收到一个 control：`invalidate()` 与 `signal`。`invalidate()` 只在「这次注册仍然活着」时生效，判断条件是 `layer.providers.get(name).provider === provider`。dispose 时 signal 以 `skill provider "<name>" disposed` abort。`packages/skill/skill/src/index.ts:390`。

读取有三个入口，返回的东西不一样：

| 调用 | 返回 | 复用发现缓存 | 典型消费者 |
| --- | --- | --- | --- |
| `list(options)` | 候选数组，带 rank、locator、invocation | 是 | 需要自己过滤的调用方 |
| `snapshot(options)` | `{skills, complete}`，只留摘要 | 是 | 目录渲染 |
| `get(name, options)` | 完整定义，含正文 | 否，正文每次重读 | 加载正文 |

`list()` 与 `snapshot()` 共用同一次发现，`complete` 表示这次发现是否所有 provider 都在无并发修订的情况下完成。`get()` 不复用候选里的数据，因为候选里根本没有正文。插件也可以绕过文件系统，用 `register()` 直接塞一个内存里的技能，这类条目的 provider 固定是 `runtime`、rank 是 250，同名时后注册的被忽略并留一条 warn。

## 3. 发现：六个根与两种文件形状

项目根不是 cwd 本身，是向上找到的最近一个含 `.git` 的祖先，找不到才退回 cwd。有 `ctx.fs` 时探测走文件系统服务，不走宿主 `node:fs`，避免沙箱里看到的路径与宿主不是同一个。`packages/skill/skill-filesystem/src/index.ts:251`。

本地 provider 的根按 rank 排：

| rank | 路径 | 说明 |
| --- | --- | --- |
| 100 | `<projectRoot>/.dsh/skills` | 项目私有，随仓库走 |
| 200 | `<projectRoot>/.agents/skills` | 项目内跨工具共享 |
| 300 | `customSkillDirs` | 部署方追加 |
| 400 | `<dshHome>/skills` | 用户级，跳过 `.system` |
| 500 | `<agentsHome>/skills` | 用户级共享 |
| 600 | 包内 `bundled` | 随安装分发 |

项目根是向上找到的最近一个含 `.git` 的祖先，找不到就用 cwd。有 `ctx.fs` 时经它探测，避免沙箱把宿主路径透进来。`packages/skill/skill-filesystem/src/index.ts:251`。

每个根下认两种形状：目录包 `<name>/SKILL.md`，和平铺文件 `<name>.md`。目录包只读一层，嵌套的 `**/SKILL.md` 不递归。这个限制是有意的：递归发现会让「一个目录算一个技能」的边界变得含糊，也会让 watcher 的 depth 失去意义。

frontmatter 字段不多，但校验很硬：

```yaml
---
name: release-notes
description: Draft release notes from a range of commits.
whenToUse: The user asks for release notes or a changelog.
disable-model-invocation: false
user-invocable: true
metadata:
  owner: platform
---
```

`name` 必须是 kebab-case，`description` 必须非空。`whenToUse` 是可选的补充说明，会进 `SkillSummary`，但目录消息只用 `description`。两个开关的语义是「禁用模型调用」与「允许用户手势调用」，缺省都为 true。旧键 `disableModelInvocation`、`modelInvocable`、`userInvocable` 会被显式拒绝，报 `frontmatter field "x" is unsupported; use "y"`。布尔值接受 `true/false`、`1/0`、`'true'/'yes'/'on'` 这类写法，其它值报 `must be a boolean`。

解析失败不会让整个发现失败。`parseSkillFile`（`packages/skill/skill-filesystem/src/index.ts:811`）对每种错误记一条 warn 然后返回 undefined：缺 frontmatter 是 `ignored: missing YAML frontmatter`，YAML 语法错是 `ignored: invalid YAML frontmatter: …`，缺字段是 `ignored: frontmatter requires name and description`，名字不合规是 `ignored: invalid skill name "x"`，开关写错是 `ignored: invalid invocation frontmatter: …`。模型那边只会看到这个技能从目录里消失，原因在宿主日志里。

watcher 用 chokidar，depth 1，`awaitWriteFinish` 的稳定阈值 200ms、轮询 100ms，root 层认 `*.md` 的增删改与目录增删，二级只认 `SKILL.md` 且忽略目录事件。根不存在时从最近的存在祖先开始监视，再对缺失路径的第一段 `watchFile`。项目 watcher 有 LRU 上限，默认 128 个项目，淘汰时触发一次失效。模型自己的 `write`/`edit` 通过 `fs/observed` 事件走 `observeHostMutation`，只在该路径落在 root 直属 `.md` 或二级 `SKILL.md` 时失效。`packages/skill/skill-filesystem/src/index.ts:294`。

watcher 失败不隐藏技能。单个根读不了会 warn `skill root "<path>" skipped: …` 并把观测标成不完整，候选照常返回能读到的部分；watcher 报错会重开，重开失败只让下一次发现不完整。

### 自己搭的时候

如果你自己写 provider，别在 `list()` 里读正文。注册表只缓存候选，正文每次 `get()` 重读，这个分工让「改正文」不需要让目录失效。反过来，如果你的 provider 在 `list()` 里解析全文，一卷入大文件就会拖慢每一次 pre-step 的目录比对。

### 发现缓存与 revision

`collect()` 先算键：`JSON.stringify({cwd, scopes: 链上每个 scope 的 id, revision})`。命中就直接返回缓存条目，`cacheable: true`。键里带 scope 链，是因为同一个 cwd 在不同 preset 下可见的技能不同。

revision 由注册表在每次失效时加一，任何一个 provider 调 `invalidate()` 都会让所有键一起过期。发现过程中如果 revision 又变了（另一个 provider 在 await 期间失效），这一轮结果不写缓存，`complete` 置 false，下一次重新发现。缓存最多 128 条，超了删最早插入的那条。

## 4. 目录：模型看到的那一段


`dsh-tool-skill` 在 `agent/pre-step` 里做四件事：确认工具可见、取快照、过滤、比对 digest。工具可见性的判断是身份比较：

```ts
const toolVisible = ctx.tools.get(skillTool.name, agent) === skillTool
```

同名工具被 agent 作用域里的另一个定义遮蔽时，目录也随之停发。这个判断把「工具 schema 在不在」和「目录在不在」绑在一起，避免模型看到一份指向不存在工具的清单。`packages/skill/tool-skill/src/index.ts:217`。

目录文本是固定的模板，条目按名字排序：

```text
<system-reminder>
A skill is a reusable set of task-specific instructions. The following skills are available in this session:

<available_skills>
- `office-xlsx`: Create, read, edit, and validate Excel workbooks, data, and formulas (.xlsx).
</available_skills>

If the user names a skill, or the task clearly matches a skill's description, call the `skill` tool with the exact skill name before taking task actions. Load all applicable skills, then follow their full instructions. This catalog contains summaries only; do not infer or follow a skill's instructions until it has been loaded.
A user may also invoke a skill directly; its <skill_content> block then appears in this conversation. Follow it, and do not call the `skill` tool again for that skill.
</system-reminder>
```

最后两句是防重复加载的：用户手势已经把正文注入过一次，模型再调一次 `skill` 就浪费一轮。模板里写死这条规则，比在工具描述里劝更可靠。

`description` 先折叠空白再截断：超过 `catalogDescriptionMaxLength`（默认 500，最小 3）时取前 `max - 3` 个字符加 `...`。`packages/skill/tool-skill/src/index.ts:411`。

digest 只覆盖条目与完整性，不覆盖渲染后的文本：

```ts
const digest = sha256((incomplete ? 'incomplete\n' : '')
  + entries.map(e => JSON.stringify([e.name, e.description])).join('\n'))
```

这样模板措辞改动不会触发重发，条目顺序变化会。与 `catalogHistory(agent)` 里的 `visibleDigest` 比对，相同就不动；不同就追加一条新消息。第一次发 `renderCatalogMessage`，之后发 `renderCatalogUpdate`，后者明确写「替换本会话此前所有 available-skills 列表」。`packages/skill/tool-skill/src/index.ts:258`。

空目录与不完整是两种状态。发现完整且没有可调用技能时，如果从来没发过目录，就什么都不发；如果以前发过，就发一条空替换，明确让模型不要再用旧名字。发现不完整时，把已确认的条目照常列出，附一句「有些技能没检查到，下次请求重试」，并把 `incomplete` 写进 source 与 digest，恢复后即使条目没变也会重发一次。

目录是会话历史，不是 World State。压缩把旧消息移出可见 surface 后，`catalogHistory` 找不到可见的目录消息，下一次 pre-step 会重建一份。

### 自己搭的时候

目录条目里不要放绝对路径，也不要放正文摘要。路径会泄露宿主目录结构，还会在项目换机器后失效；正文摘要会让模型误以为已经知道内容，跳过加载。真要给提示，放在 `whenToUse` 里，它只在加载后的返回值里出现。

### 目录失效的四个触发

目录不按时间重算，它绑在每一步的 pre-step 上。四个条件会改变 digest：

1. 条目变化：新增、删除、改名。name 参与 digest，改名字等于换了一个条目。
2. 描述变化：`description` 改动，截断后的文本不同。
3. 完整性变化：某个根读不了或 watcher 挂了，`incomplete` 从 false 变 true。
4. 可见性变化：`skill` 工具被同名工具遮蔽，条目集合变空。

前两个靠 watcher 与发现，第三个靠 provider 的 `complete`，第四个靠每步一次的工具身份比较。四种都只追加一条替换消息，旧目录留在历史里，直到压缩把它移出可见 surface。

## 5. 加载：一次 get 的完整路径

`skill` 工具只有一个必填参数 `name`，描述写死了调用时机：

```text
Load the full instructions for a skill. Call it before acting on a task that names or clearly matches a skill in the session skill catalog.
```

执行时先校验名字，再用 `ctx.workingDirectory.ensure(agent, signal)` 拿 cwd，构造 `{cwd, signal, scope: exec.agent}` 去 `ctx.skills.list()` 里找。找不到报 `skill "x" is unknown or no longer available`；找到但 `modelInvocable` 为 false 报 `skill "x" is not available for model invocation`。`get()` 之后还要再查一次策略，因为从 `list()` 到 `get()` 之间文件可能被改过。`packages/skill/tool-skill/src/index.ts:84`。

返回值是正文加包装，包装由注册表统一渲染，工具与手势共用：

```text
<skill_content name="office-xlsx">
<skill_resources>
Base directory for this skill: /Users/me/.dsh/skills/office-xlsx
Resolve relative paths mentioned by this skill against the base directory before using them. Load referenced resources only as needed.
</skill_resources>

<skill_instructions>
...正文...
</skill_instructions>
</skill_content>
```

`<skill_resources>` 按 `resourceBase` 的三种形态生成：directory 给基准目录与那句「按需加载」；url 给地址；opaque 给一句描述；没有 resourceBase 时说明资源由 provider 管理。`escapeAttr` 转义 `& " <`，正文只转义 `& < >`。`packages/skill/skill/src/index.ts:170`。

正文读取每次都穿透到 provider。注册表在 `get()` 里 `waitWithAbort(provider.get(...), signal)`，拿到定义后校验 name 是否仍等于候选项的 name，不等就判定这个 provider 的候选过期，失效后返回 undefined。`packages/skill/skill/src/index.ts:500`。所以改一篇文章不需要重启，下一次调用就能读到新正文，也不会出现「目录里叫 A、加载出来叫 B」的错位。

工具结果按普通 tool result 进历史，之后跟着压缩、检索、回放走，没有特殊待遇。

加载失败不改目录。目录由快照决定，正文读失败只影响这一次工具调用；下一次 pre-step 的 digest 不变，目录不会重发。要等文件被删到发现层，条目才会从列表里消失。

### 一个文件走完这段路

假设项目里有这样一个文件：

```text
repo/
  .dsh/skills/release-notes/SKILL.md
```

目录里出现的条目是一行：

```text
- `release-notes`: Draft release notes from a range of commits.
```

模型判断用户要发版说明，调用 `skill({name: "release-notes"})`。工具拿到 cwd，在 100 号根里命中候选，`get()` 重新读文件，正文去掉 frontmatter 后包进 `<skill_instructions>`，`resourceBase` 是 `{kind:'directory', path:'.../.dsh/skills/release-notes'}`。正文写「模板见 assets/template.md」，模型下一步用 `read` 读它，路径相对基准目录解析。

如果文件是平铺形态 `.dsh/skills/release-notes.md`，`resourceBase` 指向 `.dsh/skills` 这个根目录，因为发现时 locator 的 directory 取的是 root.path。这条差异会影响正文里相对路径怎么写。

## 6. 用户手势 /name


`/name` 是另一条入口，只扫 user 消息里的文本块：

```ts
const SKILL_GESTURE = /(^|\s)\/([a-z0-9]+(?:-[a-z0-9]+)*)(?=\s|$)/g
```

按首次出现去重，逐个 `skills.get()`，`undefined` 或 `userInvocable` 为 false 就当普通文本跳过。命中的技能渲染成同一种 `<skill_content>`，包成 `createUserMessage`，source 记 `{kind:'skill-invocation', name, form:'instructions'}`，追加到 `decision.messages` 末尾，也就是最靠近这次回答的位置。`packages/skill/tool-skill/src/index.ts:429`。

这条路径是 `disable-model-invocation: true` 技能的唯一入口。模型看不到它，用户打 `/name` 仍然能用。这也是为什么目录模板要写「不要再调一次工具」：手势注入与工具加载产出同一种块，重复加载只会多一段一样的正文。

正则要求 `/` 前面是行首或空白、后面是空白或行尾，所以 `/usr/local/bin` 这种路径不会被当成技能名。误伤多来自粘贴：一段日志里出现 ` /deploy`，也会触发加载。要避免只能在粘贴前提醒用户，或者把技能名起得足够特别。

## 7. 渐进披露的 token 账

三层内容三种代价，可以分开算：

{{< fig "diagrams/02-token-budget.svg" "图 2：技能的三段 token 代价。目录每轮都在，正文只在加载后出现一次，资源文件由模型按需读取。右侧是 20 个技能全量预载的对照。" >}}

目录是每轮都付的。条目成本约等于名字加 description 的字符数，默认上限 500 字符。这个上限是给「广告位」定的：description 的作用是让模型判断要不要加载，把正文压缩一遍塞进来，目录会变贵，模型还会误以为已经知道细节。实践中 80 到 150 字符足够说清触发条件。

20 个技能、平均 300 字符，大约 6000 字符。图 2 按英文约 4 字符一个 token 估算，画成 1.5k；同一份目录换成中文，按一比一算接近 6k。这笔钱换来的是模型知道「有什么可加载」，且在目录不变时 KV cache 前缀稳定。

正文是一次性的。一个 6KB 的 `SKILL.md` 在加载那一轮进入历史，之后跟着会话走，直到被压缩移出。它不在 system prompt 里，所以不会每轮重付；但它也在历史里，所以压缩时会被当普通工具结果处理。

资源是第三层。正文里写「模板在 assets/report.md」，模型自己去 `read`。注册表不枚举目录，也不预读资源，`<skill_resources>` 只给基准路径和一句按需加载的提示。这一层把「技能包有多大」和「这一轮花多少」解耦了。

对照全量预载：20 个技能各 6KB，就是 120KB 常驻，每轮都付，还要在技能改动时让整段前缀失效。目录方案把常驻部分压到描述，代价是模型可能不加载，或者加载了不相关的技能。后者的损失是一轮工具调用，前者的损失是每轮都付。

KV cache 的行为可以直接从代码读出来：目录是追加的 user 消息，替换也是追加，旧消息不动，所以缓存断点落在新消息之前；工具 schema 只要定义与可见性不变就前缀稳定；正文进历史后是普通后缀。这也是为什么目录要按 digest 比对而不是每步重发，重发会把可复用的后缀不断推后。

## 8. 与 tool、memory、subagent 的边界

{{< fig "diagrams/03-boundaries.svg" "图 3：四种载体的边界。工具与技能的分界是执行权，技能与记忆的分界是「步骤」与「事实」，子代理的分界是隔离的执行上下文。" >}}

工具与技能的分界是执行权。工具注册时有 schema、有 executor、可以进审批；技能加载出来只是一段文本，它能让模型知道「先跑这个命令再改那个文件」，但命令本身仍要过工具、guard 与审批。把技能当成「能执行的东西」会在权限模型上出现一个洞：技能文本可以要求模型做任何事，能不能做成取决于当时的工具与策略。

技能与记忆的分界是内容类型。记忆存事实与历史：会话日志、压缩 checkpoint、检索结果，写入靠事件，读取靠装配。技能存步骤：这类任务先做什么、后做什么、踩过什么坑。第一季的[《Agent 的记忆不是一块》](/posts/dsh-memory-architecture.html)把技能归在第五层「程序性记忆」，那篇给的是位置，这篇给的是这条路径上的代码。

一个容易混的点：`AGENTS.md` 与技能都是文件，都由人写，为什么一个常驻一个按需。答案是变更频率与适用范围。`AGENTS.md` 描述的是这个仓库里一直成立的约束，技能描述的是某类任务才用到的流程。前者每轮都相关，后者大多数轮次不相关。

子代理的边界是作用域。子代理有自己的 agent scope，目录按 scope 链继承：全局层与祖先层的技能对它可见，它自己的 preset 层可以覆盖同名。目录消息按 agent 发，子代理有自己的一份；它加载的技能正文进它自己的会话，不回流到父代理。父代理要复用结论，得靠子代理的返回值或共享文件。[《子代理的生命周期》](/posts/dsh-subagent-lifecycle.html)讲了这条隔离线的另一半。

### 自己搭的时候

判断一段内容该放哪个载体，问三个问题：它每次对话都成立吗，它需要执行吗，它多久变一次。三个都否，放技能；第一个是，放 `AGENTS.md`；第二个是，写工具。技能与工具之间没有灰色地带，一个技能可以要求模型调用工具，但它自己不能被调用。

## 9. 一条技能从发现到执行的完整路径


把前面的片段接起来，一次典型的加载是十步：

{{< fig "diagrams/04-discovery-sequence.svg" "图 4：从发现到执行的时序。pre-step 取快照发目录，模型调 skill，注册表穿透 provider 重读正文，结果进历史。" >}}

1. 插件加载：`skill-filesystem` 调 `ctx.skills.registerProvider()`，工厂返回 provider，注册进当前层。
2. 首个 pre-step：`tool-skill` 确认 `skill` 工具可见，调 `skills.snapshot({cwd, scope})`。
3. 注册表算缓存键 `{cwd, scopes, revision}`，未命中就逐层 `collect`。
4. provider 列六个根，读目录项，解析 frontmatter，产出候选并按 rank 排序。
5. 注册表合并层，近层覆盖，返回 `{skills, complete}`。
6. `tool-skill` 过滤 `modelInvocable`，截 description，算 digest，与历史比对。
7. 不同就追加一条 `skill-catalog` user 消息，进这一步的请求。
8. 模型看到目录，判断任务匹配，调用 `skill({name})`。
9. 工具校验名字与策略，`skills.get()` 穿透 provider 重读文件，`renderSkillContent` 包装。
10. 结果作为 tool result 进历史；正文里点名的资源由模型后续 `read`。

第 2 步到第 7 步在每次 pre-step 都会跑，只是大多数时候 digest 相同，什么也不发。第 9 步的 `get()` 每次都是真读，没有正文缓存。这两条一起保证了「目录可能滞后一个 pre-step，正文永远是最新的」。

时间线上，第 2 到 7 步在同一个 pre-step 里串行完成。快照命中缓存时只花一次 map 查表；未命中时包含一次目录遍历与 N 次 frontmatter 解析。第 9 步多一次磁盘读与一次定义校验。目录不变的那些轮次，技能子系统的边际成本接近零，这是它敢把正文放进历史的底气。

## 10. skill-office：provider 的另一种形态

`dsh-skill-office` 只有 95 行，但把 provider 接口的另一面展示得很清楚。它在 `apply` 时同步读包内 7 个 `SKILL.md`，只解析 `description` 生成候选，rank 固定 600，provider 名 `dsh-office`，`resourceBase` 指向各自的资源目录。`list()` 直接返回这 7 个，不做文件系统扫描。

`get()` 才读正文，并且对 `office-render`、`office-recalculate`、`office-export-pdf` 追加一段运行时信息：

```text
## Installed LibreOffice Kit
{"libreofficeKit":{"node":"/usr/local/bin/node","cli":"/opt/libreoffice-kit/lib/cli.js"}}
```

这段信息每次 `get()` 重新生成，因为安装路径因机器而异。`cli: false` 时改写成「本部署禁用了 LibreOffice Kit」；打包应用没提供独立 node 时直接抛 `skill-office: packaged applications must supply a standalone Node executable`。`packages/skill/skill-office/src/index.ts:47`。

这个包说明 provider 不一定是「文件的投影」。候选可以来自包内资源，正文可以在读取时拼接运行时事实，只要 `list()` 稳定、`get()` 返回合法定义，注册表与消费者都不用改。

provider 的契约可以压成三条：`list()` 在给定 scope 下返回稳定候选，失败要么抛出要么标记不完整；`get()` 返回与候选同名、带正文的完整定义；失效由 provider 自己调 `invalidate()`，注册表不替它轮询。skill-office 三条都满足，所以在消费者眼里它与文件系统 provider 没有区别。

## 11. 三条没走的路

技能这套机制不是唯一解。对比三个替代方案，能看清它为什么长成这样。

每个技能一个工具。工具 schema 每轮都发，20 个技能就是 20 份 schema，参数还得设计成统一的空对象。技能数量一多，工具表的投影与缓存成本都上去，工具名还要跟内置工具抢命名空间。技能共享一个 `skill(name)` 入口，schema 只有一份。

只做 slash command。用户打 `/name` 才加载，模型不参与判断。实现最简单，但模型无法在任务开始时自己发现「有个技能能用」，目录也不需要了。适合全人工控制的场景，不适合让 agent 自主做事。

对 `SKILL.md` 做 RAG。把技能切片、embedding、检索 top-k 塞进上下文。检索按相似度选片段，技能加载按能力判断选整篇；切片会打断步骤顺序，同一篇技能的上下两步可能被拆开。检索结果还会随 query 变化，目录与缓存都不稳定。

这三条的取舍方向一致：把「有什么」压到极小，把「怎么做」留到一次显式加载。

## 12. 失败模式

| 情形 | 模型看到 | 宿主侧 |
| --- | --- | --- |
| 目录里没有这个技能 | `skill "x" is unknown or no longer available` | 候选被过滤，或文件被删 |
| 技能禁用了模型调用 | `skill "x" is not available for model invocation` | 目录不收录，只能 `/name` |
| 正文在两步之间被删 | 目录还在，加载报 unknown | `get()` 返回 undefined，候选失效 |
| frontmatter 写坏 | 条目从目录消失 | 一条 warn，技能被跳过 |
| 某个根读不了 | 目录带「有些技能没检查到」 | `complete: false`，不写缓存 |
| `skill` 工具被遮蔽 | 目录停发或发空替换 | 身份比较失败 |
| 压缩移走目录消息 | 下一次 pre-step 重建目录 | 历史里旧消息不可见 |

模型看到的是结果，不是原因。目录里少一个条目，可能是文件被删、frontmatter 坏了、根读不了、被更高优先级的同名技能覆盖，四种原因在模型侧长得一样。排查要去宿主日志里找 warn，模型侧的报错只覆盖「加载时不存在」这一种。

## 13. 设计取舍


目录与正文分离，而不是把正文塞进 system prompt。理由是 token 与选择权：常驻正文每轮付费，且模型没有「不加载」的自由；分离后目录便宜，加载由模型判断。代价是多一次工具调用，以及模型可能判断错。

目录用追加替换，而不是原地修改。会话日志只追加，改历史会破坏回放与缓存。代价是旧目录消息留在历史里占 token，直到压缩移走。替换消息里明确写「替换此前所有列表」，让模型不被旧条目误导。

digest 只算条目与完整性，不算渲染文本。模板措辞调整不该让所有会话重发目录。代价是模板里的语义变化不会自动触发更新，得靠版本升级或新会话。

rank 只在层内生效，跨层近者覆盖。这条与工具注册表一致，让「项目覆盖用户」「agent preset 覆盖全局」成为可预测的规则。代价是同一层里两个 provider 提供同名技能时，输的那个静默消失，只能从日志看到。

frontmatter 校验失败就跳过整个技能。半个技能（有名字没描述）会让目录与加载行为分叉，不如不给。代价是一个拼写错误就让技能静默消失，排查要看宿主日志。

正文不缓存。每次 `get()` 重读，改文件立刻生效。代价是加载延迟包含一次磁盘读，以及 provider 必须自己保证读到的内容与候选一致。

目录绑定工具可见性。工具被遮蔽时目录停发，避免指向不存在的工具。代价是「只想隐藏目录、保留工具」这件事没有配置项，要隐藏就一起隐藏。

`/name` 只扫 user 消息。模型自己写的 `/foo` 不会触发注入，避免模型自我加载。代价是用户从文件粘贴的文本里如果含 `/foo`，也会被当成手势。

目录消息用 user 角色，不用 system 角色。会话中段的 system 消息在多数 provider 上要么不合法，要么被忽略；user 消息能在历史里定位，也能被压缩。代价是它读起来像用户说的话，只能靠 `source.kind` 区分。

发现缓存以解析后的 scope 链为键，不只按 cwd。同一个 cwd 在不同 agent preset 下可见的技能不同，键里不带链就会把 A 的目录发给 B。代价是切换 preset 会让缓存整体失效一次。

## 14. 踩坑点

1. `SKILL.md` 首行必须是单独的 `---`，前面有空行或 BOM 都算缺 frontmatter，技能直接不出现。
2. 名字写成 `Release_Notes` 或 `release notes` 会被拒，规则是 `^[a-z0-9]+(?:-[a-z0-9]+)*$`。
3. 目录包里再套一层 `skills/foo/SKILL.md` 不会被发现，发现只到第二级。
4. 用了旧键 `disableModelInvocation`，技能不出现，日志里是 `frontmatter field "disableModelInvocation" is unsupported; use "disable-model-invocation"`。
5. `description` 写太长不会报错，目录里被截到 500 字符，模型可能因此判断错。
6. 两个根提供同名技能时，近的那个赢，远的整个条目消失，不会合并描述与正文。
7. 目录里出现过的技能被删除后，模型仍可能按旧名字调用，得到 `skill "x" is unknown or no longer available`，这是预期行为。
8. 技能正文里写相对路径，必须相对 `resourceBase` 解析；不写基准目录，模型只能猜。
9. 把需要审批的命令写进技能正文不会绕过审批，技能没有执行权，命令仍走工具与 guard。
10. 用户手势触发的正文与工具加载的正文长得一样，模型若再调一次 `skill`，历史里会出现两份相同正文。
11. 项目 watcher 有 LRU 上限，同时打开超过 128 个项目时，被淘汰项目的目录变化不会被感知，直到下次 `list()`。
12. 发现不完整时目录照发，但会带一条警告；这时同名技能可能由低优先级来源提供，正文与描述未必来自同一份文件。
13. `metadata` 只接受对象，写成数组会被忽略，且不会报错到模型侧。

## 15. 源码索引

| 位置 | 内容 |
| --- | --- |
| `packages/skill/skill/src/index.ts:170` | `renderSkillContent`，九行包装与资源提示 |
| `packages/skill/skill/src/index.ts:390` | `registerProvider`，control 与失效语义 |
| `packages/skill/skill/src/index.ts:439` | `register`，runtime 技能与同名忽略 |
| `packages/skill/skill/src/index.ts:500` | `get`，穿透读取与名字校验 |
| `packages/skill/skill/src/index.ts:519` | `collect`，缓存键与两次重试 |
| `packages/skill/skill/src/index.ts:551` | `collectFresh`，层合并与近者覆盖 |
| `packages/skill/skill-filesystem/src/index.ts:185` | `list`，六根发现与不完整观测 |
| `packages/skill/skill-filesystem/src/index.ts:251` | `roots`，rank 与项目根 |
| `packages/skill/skill-filesystem/src/index.ts:294` | `SkillWatchManager`，watcher 与失效 |
| `packages/skill/skill-filesystem/src/index.ts:737` | `discoverRoot`，两种文件形状 |
| `packages/skill/skill-filesystem/src/index.ts:811` | `parseSkillFile`，frontmatter 校验 |
| `packages/skill/tool-skill/src/index.ts:84` | `skill` 工具定义与执行 |
| `packages/skill/tool-skill/src/index.ts:181` | `/name` 手势监听 |
| `packages/skill/tool-skill/src/index.ts:217` | 目录监听、digest 与替换 |
| `packages/skill/tool-skill/src/index.ts:411` | `catalogDescription` 截断 |
| `packages/skill/skill-office/src/index.ts:47` | `officeRuntime`，运行时路径注入 |
| `packages/core/system-prompt/src/index.ts:136` | `SECTION_ORDERS`，没有技能章节 |
| `packages/core/system-prompt/src/index.ts:620` | `assemble`，schema 投影与排序 |
| `packages/context/agent-instructions/src/index.ts:85` | `apply`，AGENTS.md 的 user 消息注入 |
| `packages/context/agent-instructions/src/render.ts:303` | 预算截断标记 |
| `packages/core/system-prompt/src/index.ts:484` | `section` 注册，技能没有对应调用 |
| `packages/core/system-prompt/src/index.ts:551` | `tools` provider，工具 schema 进入 prompt 的唯一入口 |
| `packages/context/agent-instructions/src/index.ts:330` | pre-step 注入 AGENTS.md baseline |

## 16. 附：本地验证清单

想确认这篇的描述，可以按下面六步走一遍：

1. 在项目里建 `.dsh/skills/demo/SKILL.md`，frontmatter 只写 name 与 description，正文写一句 `Reply with the word demo.`。
2. 发一句「用 demo 技能回答」，看目录消息是否出现，条目是否只有名字与描述。
3. 让模型调 `skill`，确认 tool result 是 `<skill_content>` 包裹，正文与文件一致。
4. 改正文但不改 description，再调一次，确认读到新正文且目录没有重发。
5. 改 description，确认下一条 pre-step 追加了替换目录，旧目录仍在历史里。
6. 把同名技能再放一份到 `.agents/skills`，确认 `.dsh/skills` 那份赢，另一份整个消失，不会合并。

如果第 5 步没出现替换，先看 `skill` 工具是否被同名工具遮蔽，再看发现是否不完整。

技能子系统把「知道有什么」和「知道怎么做」拆成两次交付：目录负责前者，工具调用负责后者。这个拆分让 token 花在需要它的那一轮，也让技能的失效与文件的失效解耦。代价是模型多了一次判断，宿主多了一套发现与缓存。判断这套代价是否值得，取决于你的技能是几十个常用流程，还是三个每轮都要遵守的规则。后者更适合 `AGENTS.md`，前者才是这套机制的用武之地。
