---
title: "按需加载：opencode、Claude Code、hermes、DSH 的 Skill 机制对照"
date: "2026-11-10T00:30:00+08:00"
summary: "四个 harness 都认同一份约定：一个目录、一个 SKILL.md、一行描述常驻上下文，正文等你真正要用时再加载。差别在三个位置：编目放在哪、谁能触发、加载后给什么。逐条对照源码与二进制字符串后的机制笔记。"
description: "四个 harness 的 Skill 机制对照：编目放在哪、谁能触发、加载后给什么。"
tags: ["harness", "agent", "context-engineering", "architecture"]
slug: "harness-skill-compare"
---

> 源码版本：opencode `2.2.5`，commit `a687c047`（2026-11-08）；Claude Code 本机二进制 `2.1.319`（没有可读源码，只有 `strings` 捞出来的字符串）；hermes-agent commit `8a5a7ac`（2026-11-09）；DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`。
> 前三个仓库在工作区 `.opencode-src/`、`harness-research/hermes-agent/`、`harness-research/deepseek-harness/`。Claude Code 的结论一律来自二进制字符串与行为对照，引号里的英文是原文，中文是我补的。

## 0. 先说结论

四个 harness 都实现了同一份约定：一个目录，里面一个 `SKILL.md`，frontmatter 写 `name` 和 `description`，正文是给模型看的操作手册。四家都只把「名字 + 一行描述」放进上下文，正文等真正要用时再加载。这套东西现在叫渐进披露（progressive disclosure），说白了就是：**先让模型知道有这个技能，再决定要不要读它**。

所以真正的设计差异不在「支不支持技能」，而在三个位置：

1. **编目放在哪**：system prompt、指令前缀、持久 user 消息，还是每轮现算的附件。
2. **谁能触发**：模型自己判断、用户点名、宿主预载，以及各自的闸门。
3. **加载后给什么**：只给正文，还是连基目录、文件清单、工具授权、执行位置一起给。

一表看完：

| | 编目放哪 | 一条的成本上限 | 变更怎么生效 | 加载后附带 |
| --- | --- | --- | --- | --- |
| opencode | system 指令块 | 无 | 追加差异：新增 / 改动 / 删除 | 基目录 + 至多 10 个文件路径 |
| Claude Code | meta user 附件 | 整块 1% 上下文，单条 1536 字符 | 每轮重算，超预算按优先级截断 | 正文转成 user 消息 + 基目录（+ fork / 工具授权） |
| hermes | system prompt 的 `## Skills` 段 | 单条 60 字符 | 会话内冻结，下个会话生效 | JSON：正文 + 附属文件清单 |
| DSH | 持久 user 消息 | 单条 500 字符 | digest 变了原位替换 | 基目录 |

{{< fig "diagrams/01-lifecycle.svg" "技能的五段生命周期。02 与 03 是四家分歧最大的位置；04 之后都是把 SKILL.md 正文塞进对话，区别只在附带什么。" >}}

三个判断，后面逐节展开。

**第一，编目位置决定了技能能不能在会话中途被改掉。** system prompt 是缓存前缀，动它等于整段作废。hermes 因此选择冻结：系统提示在会话内字节稳定，装完新技能要等下个会话。opencode 同样把编目放在 system 里，但它把「变更」写成追加的差异块，只让末尾失效，不重写整段。DSH 与 Claude Code 把编目放在消息里，于是可以替换、重算、甚至被压缩掉再重建。

**第二，预算有四种解法，只有 Claude Code 真的在管总量。** Claude 按上下文比例给整块预算，超了就把低频技能退化成只有名字；DSH 管单条 500 字符，不管总量；hermes 管单条 60 字符，外加按分类整类降级；opencode 不设预算，只在变更时发差异。条数一多，后三家的成本是线性涨的。

**第三，只有 Claude Code 的技能会改变工具集和执行位置。** `allowed-tools`、`hooks`、`model`、`context: fork` 都写在 SKILL.md 的 frontmatter 里，加载即生效。其余三家的技能是纯文本：加载只是往对话里追加一段说明，权限模型、工具表、模型选择都不动。

## 1. 先对齐：四家说的 skill 不是同一个东西

共同的部分是文件格式。这套格式现在被好几家共用，Anthropic 的 Agent Skills 规范、opencode 的兼容目录、hermes 的 `skills/AGENTS.md`、DSH 的 `docs/subsystems/skills.md` 描述的是同一件事：

- 一个目录，目录名就是技能 id（DSH 还允许根目录下的扁平 `<name>.md`）。
- `SKILL.md` 开头是 YAML frontmatter，至少要有 `name` 和 `description`。
- 正文是 Markdown，写给模型看。
- 可选 `references/`、`templates/`、`assets/`、`scripts/`，正文里用相对路径引用。

各家在这之上加了不同的东西：

| | 模型调用的工具 | 参数 | 正文从哪来 | 模型能写吗 |
| --- | --- | --- | --- | --- |
| opencode | `skill` | `id` | 磁盘读、或 `skill://` 虚拟文件 | 否 |
| Claude Code | `Skill` | `skill`、`args` | 磁盘读、插件、MCP、记忆库 | 否（技能由人或插件提供） |
| hermes | `skills_list` + `skill_view` | `name`、`file_path` | 磁盘读，返回前去预渲染 | 能，`skill_manage` |
| DSH | `skill` | `name` | 磁盘读 | 否 |

两个容易混的点。

一是 **skill 与 slash command 不是一回事**。Claude Code 里技能会注册成 `/name`，但 `/name` 的含义取决于它出现的位置：在消息开头是直接执行，在正文中间只是「授权模型调用」，完全不写斜杠才算提及——中间那种情况会生成一条 `skill_mention` 附件，调不调用仍由模型判断。hermes 只有一种语义：`/name` 直接构造一条 user message，正文立刻进上下文。同一个手势，两套规则。

二是 **hermes 把技能当可写的记忆**。`skill_manage` 支持 create / patch / edit / delete / write_file，一次调用是一个操作数组，原子提交：任一失败全部回滚。它有一套锁、ledger、写闸门、写后扫描，默认却几乎全开（`skills.write_approval` 与 `skills.guard_agent_created` 都默认关），靠 curator 与用户锁兜底。这是四家里唯一让「做完任务顺手把流程存成技能」变成工具调用的。

## 2. 发现：四个入口，同一份哨兵

{{< fig "diagrams/02-discovery.svg" "四家的发现来源。自上而下优先级递减；扫描深度与同名规则写在每列底部。" >}}

四家都拿 `SKILL.md` 当哨兵，而且有一条共同的硬规则：**目录里有 `SKILL.md` 就不再往下找**。opencode 的 BFS 深度上限是 6，DSH 是 1，hermes 靠目录分类，Claude Code 在固定目录下按目录名找。这条规则解释了一个常见困惑：为什么把技能嵌套在 `skills/team/deploy/` 里，opencode 找得到、DSH 找不到——DSH 只认 `<root>/<name>/SKILL.md` 这一层。

### 2.1 扫到哪

opencode 从 cwd 一路向上，把每个祖先目录的 `.opencode`、`.claude`、`.agents` 都收进来，再加全局的 `~/.config/opencode`。文档说「到 git worktree 为止」，这一版的实现是走到文件系统根，没有 git 边界。

DSH 的边界是最近一个含 `.git` 的祖先（没有就用 cwd），然后按六个 rank 取项目级与用户级目录。Claude Code 是项目 `.claude/skills/` + 用户 `~/.claude/skills/` + 插件 + claude.ai 同步 + MCP + 内置。hermes 是四个 tier：受信项目目录 → `~/.hermes/skills` → `skills.create_dir` → `skills.external_dirs`。

### 2.2 同名怎么办

四种处理，正好代表四种取舍。

- **opencode 按 id 后写覆盖**。所有来源折叠进一个 `Map<ID, Info>`，后注册的赢。项目 `.opencode` 晚于全局配置处理，所以项目赢；内置技能最早注册，同 id 会被任何来源覆盖。注意去重键是 id 而不是显示名：id 由目录名（或根目录下的文件名）决定，两个 `review` 目录会互相覆盖；反过来，两条不同 id 的技能可以把 frontmatter 的 `name` 写成同一个，编目里就会出现两个同名项。
- **DSH 按 rank 小者胜**。`100` 项目 `.dsh/skills`、`200` 项目 `.agents/skills`、`300` custom、`400` 用户 `~/.dsh/skills`、`500` 用户 `.agents/skills`、`600` bundled。rank 相同再按 provider 注册顺序，最后按 provider 内顺序。
- **hermes 拒绝猜**。同一 tier 里两个不同技能共用一个名字，直接判 ambiguous 不列出，报错里给可用的 `load_name`：`Ambiguous skill name 'x': use one of …; refusing to guess.` 只有路径 resolve 到同一个文件、或内容字节一致，才算同一技能的副本。
- **Claude Code 用命名空间**。插件技能是 `plugin:skill`，目录作用域技能是 `apps/web:deploy`；同名的作用域与无作用域版本同时存在时，工具描述要求模型挑「目录包含当前文件」的那个。

### 2.3 信任

hermes 是唯一把「信任」做成发现阶段硬闸门的。项目的 `.hermes/skills`、`.agents/skills` 只有在 `skills.trusted_project_dirs` 里才加载，而且每个项目技能目录先过一遍 `skills_guard`，判 dangerous 就隔离，扫描器自身异常也 fail-closed。Hub 安装更严：先落隔离区，静态扫描，按来源信任级别决定放行，最后才原子移动、写 lockfile、记审计。

Claude Code 有一个相近但更窄的规则：`.claude/skills/*` 会同时按插件加载，未信任的工作区、或「进了 worktree 且开过 Bash 沙箱」的情况下跳过插件加载这一步，技能本身照常可用。

opencode 与 DSH 没有这一层：发现即加载，权限模型只作用在「调用 skill 工具」这个动作上。

## 3. 编目：把「知道」压成一行

{{< fig "diagrams/03-catalog.svg" "编目的四种落点。前两家把它当提示词，后两家把它当消息——这决定了它能不能在会话中途被替换。" >}}

编目就是那段「有哪些技能可用」的文本。四家都要解决同一个矛盾：写得太短模型判断不了该不该用，写得太长每轮都在烧钱。

### 3.1 四段文本

opencode 的编目是一个 instruction source，最终拼进 system 数组的第二项。它的开头三句是固定的：

```text
Skills provide specialized instructions and workflows for specific tasks.
Use the skill tool to load a skill when a task matches its description.
The user may also invoke a skill directly. When that happens, its instructions appear in the conversation as a <skill_content> block, the same shape the skill tool returns. A skill that is already present this way does not need to be invoked again.
```

然后是 `<available_skills>`，每条含 `<id>`、`<name>`、`<description>` 三项。空列表时写 `No skills are currently available.`，不是省略整段。

DSH 的编目是一条持久的 user 消息，外壳是 `<system-reminder>`，开头是：

```text
A skill is a reusable set of task-specific instructions. The following skills are available in this session:

<available_skills>
- `deploy`: 把当前分支发布到预发环境，跑完 smoke test 再切流量
</available_skills>
```

每条一行，固定格式 `` - `name`: description ``，只含 name 与 description。后面还有两句行为约束，一句说「用户点名或任务匹配时先调 `skill` 工具」，一句说「用户直接调用过的技能，其 `<skill_content>` 块已经在对话里，不要重复调用」。

hermes 的编目是 system prompt 里的一节，前言比前两家都强硬：

```text
## Skills
Before replying, scan the skills below. If a skill matches or is even partially relevant to your task, you MUST load it with skill_view(name) and follow its instructions. Err on the side of loading — it is always better to have context you don't need than to miss critical steps, pitfalls, or established workflows.
```

列表按 category 分组，单技能的分类省略分类头，多技能先写 `category:` 再缩进。分类太多时整类降级成 `category [names only]: a, b, c`，并在末尾解释「这些分类不在当前编码上下文里，描述省略，但技能仍可正常加载」。

Claude Code 的编目是一条 `isMeta` 的 user 附件，开头是 `The following skills are available for use with the Skill tool:`。处于 coordinator / code-mode 时换成另一句，告诉模型「可以用脚本 `await tools.Skill({skill: "name"})` 加载，指令会作为脚本结果后的消息到达」。

四段文本有一个共同点：都明确告诉模型「已经加载过的不要重复加载」。区别是这个约束靠什么执行——Claude 的工具描述让模型自己看 `command-name` 块，DSH 靠 pre-step 的注入顺序，opencode 只靠这句话本身。

### 3.2 预算

Claude Code 是唯一按上下文比例算总账的。默认参数是「上下文 200k token × 4 字节/token × 1%」，得到约 8000 字符的整块预算；`SLASH_COMMAND_TOOL_CHAR_BUDGET` 可以覆盖，设置里对应 `skillListingBudgetFraction`。单条描述另有 1536 字符上限，超了截断加省略号。

超预算时的策略写在遥测字段里：`truncation_mode: "priority"`。内置技能与 override 固定项优先保留完整描述，其余退化成只有名字的一行。日志会打一句很直白的话：

```text
Skill listing over budget: N skills, X chars > Y budget — descriptions will be truncated. Run /skills to disable some, or raise skillListingBudgetFraction in settings.
```

hermes 管单条：`SKILL_PROMPT_DESC_LIMIT = 60`，按词边界截；整条就是一个长词、找不到边界时直接硬截，再补 `...`。它不管总量，`hermes prompt-size` 只是诊断工具，量一下 `<available_skills>` 占了多少字节，从不裁剪。

DSH 管单条 500 字符（`catalogDescriptionMaxLength`，最小 3），先压空白再截，超长写 `...`。发现阶段不截，只在渲染编目时截。

opencode 什么都不管。它只在「变更」这件事上省钱：编目变了不重发全量，按 key 做 diff，只发新增、改动、删除三种块。

把四家放到同一个假设下——100 个技能、平均描述 120 字符——大概是这样：

| | 每轮常驻 | 折算 token（约） |
| --- | --- | --- |
| opencode | 约 12k 字符 | 3k |
| Claude Code | 封顶 8k 字符 | 2k |
| hermes | 60 字符/条，约 7k 字符 | 1.8k |
| DSH | 500 字符/条封顶，典型约 12k 字符 | 3k |

这个数量级值得记住：技能编目不是免费的。它和系统提示、AGENTS.md、工具定义挤在同一个前缀里，100 个技能就是几千 token，每一轮都付。

### 3.3 刷新

编目什么时候更新，四家给了四种答案。

opencode 是**追加差异**。`Instructions.listUpdate` 比较前后两份列表，没变化时重述全量并以 `supersedes` 开头，有变化时只输出三类块：

```text
New skills are available in addition to those previously listed:
- `release`: ...

The following skills have changed since the previous list:
- `deploy`: ...

The following skill IDs are no longer available and must not be used: old-deploy
```

差异块追加在指令末尾，前面的字节不动，缓存前缀因此还能复用一部分。这是「既要 system 级常驻、又要能变」的一个折中。

DSH 是**原位替换**。pre-step 算一遍 catalog，用 `[name, description]` 的 sha256 做 digest；digest 没变就什么都不做，变了就找见上一条 `skill-catalog` 消息，用 `replaceMessage` 换掉。压缩如果把旧编目藏进了摘要，下一个 pre-step 发现「消息不在了」就重新追加一条。

Claude Code 是**每轮重算**。附件在消息组装时生成，预算与截断也在那时算，所以技能列表变了下一轮就体现，不需要额外的失效逻辑。

hermes 是**会话内冻结**。系统提示一旦发出就保持字节稳定（仓库注释的原话是 prompt caching is sacred），装完新技能默认「下个会话生效」，`--now` 或 `/reload-skills` 才主动失效。它的 prompt 索引有进程内 LRU，key 里带上每个 SKILL.md 的 `(mtime, size)`，跨进程安装也能察觉——但察觉了也不改当前会话的字节。

四种做法没有对错，取决于你更怕什么：怕缓存失效，就冻结；怕技能不生效，就重算；想两头都要，就写差异。

## 4. 激活：闸门与落点

{{< fig "diagrams/04-activation.svg" "激活的通用结构与四家对 /deploy 的解读。闸门决定谁能触发，落点决定注入成什么角色。" >}}

### 4.1 模型侧

工具签名都很小。

opencode 只有 `id` 一个参数，描述两句：「任务匹配描述时，把技能指令与资源加载进当前对话」「技能 ID 必须匹配可用技能，或用户显式引用的技能」。执行时先查注册表，再断言 `action: "skill"` 的权限，最后渲染。

DSH 只有 `name` 一个参数，描述是「加载一个技能的完整指令，在开始一个点名或明显匹配某个技能的任务之前调用」。执行时依次校验名字格式、在实时列表里精确匹配、检查 `isModelInvocable`、读正文、再校验一次策略。

hermes 是两个工具：`skills_list` 只给 name + description，`skill_view` 才返回正文。这个拆分比另外三家多一次往返，但换来了「列表」与「加载」的分离——`skills_list` 的结果不进系统提示，按需调用。

Claude Code 的工具参数是 `skill` + `args`。描述里有一段值得抄的措辞：技能覆盖的任务要先调这个工具，「技能的指令会加载进本轮，替代你的默认做法」；有些技能在子代理里跑并直接返回结果；后台运行的技能只返回代理名字，结果稍后以任务通知到达，期间不要等、也不要再调一次。

一个容易被忽略的差异：**能不能加载编目里没有的技能**。opencode 可以——过滤只发生在编目层，执行层不做白名单，模型只要猜到 id 就能加载。DSH 也可以，工具只查实时 `list()`，不校验编目消息的成员。Claude 的工具描述说「只有编目里的名字，或用户显式输入的，才有效」；字符串里另有 `is not in this session's skills allowlist`，说明配了白名单时是硬拒绝，默认情况下仍只是提示词约束。hermes 的 `skill_view` 按名字解析发现结果，名字歧义时拒绝。

### 4.2 用户侧

同一个 `/deploy`，四家四种语义：

- **opencode**：`/skills` 面板选中后往输入框插入字面 `@deploy`，同时挂一个 `skills` 附件；服务端追加一条技能消息，正文直接进上下文。不经过模型判断。
- **Claude Code**：分三种位置。消息开头写 `/deploy` 是直接执行；正文中间写只是「授权模型调用」，此时生成 `skill_mention` 附件，告诉模型「用户在提及它，如果是在要求执行就调用工具」；完全不写斜杠就只是提及。从命令面板选中则走命令展开，正文以 user 消息注入。
- **hermes**：每个技能自动注册 `/<slug>`，调用时直接构造一条 user message：`[IMPORTANT: The user has invoked the "deploy" skill, indicating they want you to follow its instructions. The full skill content is loaded below.]`，后面跟正文、绝对目录、以及「用 terminal 以绝对路径运行脚本」的提示。
- **DSH**：用户消息文本里任何位置出现空白边界的 `/deploy`，pre-step 就注入一条 `<skill_content>` 用户消息。名字没注册也不报错，当普通文本处理。

方向并不一致：opencode 与 hermes 认为用户点名就是执行；Claude 看位置，开头执行、中间只算授权；DSH 不猜语义，找到就注入。

### 4.3 闸门

拦住模型调用的是 frontmatter 字段，但四家并不齐：

- Claude Code 与 DSH 是一对开关：`disable-model-invocation: true` 让模型不能调、只有用户能触发；`user-invocable: false` 让用户看不见斜杠命令、只有模型能调。
- opencode 只有前者，没有 `user-invocable`。
- hermes 两个都没有，靠平台、禁用列表与项目信任做过滤。

DSH 对这两个键最严格，只认连字符写法，写成驼峰直接报错：`frontmatter field "disableModelInvocation" is unsupported; use "disable-model-invocation"`，布尔值接受 `true/false`、`yes/no`、`on/off`、`1/0`。opencode 也认 `disable-model-invocation`，但没有 `user-invocable`，并且把 `metadata["opencode/autoinvoke"]` 放在更高优先级。Claude Code 的 schema 描述是「若为 true，模型不能通过 Skill 工具调用，只有用户能敲斜杠命令」与「若为 false，隐藏用户的斜杠命令，只有模型能调用」。

再往外一层是运行时权限。opencode 用通用 ruleset，`action: "skill"`、`resources: [skill.id]`，允许后落一条持久规则；DSH 在工具内校验调用策略，权限本身仍走 executor/sandbox；hermes 有 `skills.write_approval` 这样的写闸门，读路径只查平台、禁用、信任。

## 5. 执行：同一篇正文，四种包装

{{< fig "diagrams/05-payload.svg" "同一篇 SKILL.md 的四种包装。只有 Claude Code 的包装会连带改变工具集和执行位置。" >}}

### 5.1 包装

opencode 返回一个文本块，正文原样，前面加 `# Skill: name`，后面加基目录与一句「相对路径相对于这个目录」，再附 `<skill_files>` 清单：

```ts
// packages/core/src/skill.ts:40-57（节选）
return [
  `<skill_content name="${skill.name}">`,
  `# Skill: ${skill.name}`,
  "",
  skill.content.trim(),
  "",
  `Base directory for this skill: ${directory}`,
  "Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.",
  "Note: file list is sampled.",
  "",
  "<skill_files>",
  ...files.map((file) => `<file>${file}</file>`),
  "</skill_files>",
  "</skill_content>",
].join("\n")
```

文件清单在**加载时现算**：对技能目录跑一次 `fs.scan("**/*")`，排除 `SKILL.md`，排序后截前 10 条。所以它是快照，不是索引。

DSH 是两层 XML：`<skill_resources>` 放基目录，`<skill_instructions>` 放正文。基目录有三种形态——`directory` 给绝对路径，`url` 给基 URL，`opaque` 给一句描述；没有基目录时写 `Resources for this skill are managed by provider "…".`。

hermes 返回 JSON，正文、`skill_dir`、`linked_files`、`usage_hint` 都在里面：

```json
{
  "success": true,
  "name": "deploy",
  "content": "<正文，已预渲染>",
  "skill_dir": "/Users/…/.hermes/skills/deploy",
  "linked_files": { "references": ["…"], "scripts": ["…"] },
  "usage_hint": "To view linked files, call skill_view(name, file_path) where file_path is e.g. 'references/api.md' or 'assets/config.yaml'"
}
```

Claude Code 是两步。工具先回一条 `Launching skill: deploy`，随后一条 user 角色消息把正文包进 `<command-name>/deploy</command-name>`，前面加一行 `Base directory for this skill: …`。正文跨轮驻留、不重读；重复调用且字节相同，只加一句 `is already loaded above; instructions unchanged.`。省下来的部分在 frontmatter 里，由运行时另行消费。

### 5.2 资源

渐进披露的第三层是「正文引用的文件」。四家的指路方式不同：

- opencode 给 10 条采样路径，模型用 read 工具按绝对路径取；URI 技能的路径是 `skill://id/file`，read 工具会为它建一个内存虚拟 FS，并同时断言 `skill` 与 `read` 两条权限。
- DSH 只给基目录，正文里写什么就读什么；没有文件清单，也没有资源枚举。
- hermes 给完整的 `linked_files` 清单，模型再调一次 `skill_view(name, file_path)` 读单个文件；越界路径报 `Path traversal ('..') is not allowed.`，二进制文件返回 `[Binary file: x, size: N bytes]`。
- Claude Code 只给基目录，靠模型自己 Read；MCP 提供的技能是例外，会明确告诉模型「用 Read 工具、带 server 与 uri 读附属文件」。

hermes 还有两件别家没有的事。一是**加载时预渲染**：`${HERMES_SKILL_DIR}` 模板变量与 `` !`cmd` `` 内联 shell 在返回前就展开（内联 shell 默认关，且社区来源的技能永不执行）。二是**加载即装依赖**：frontmatter 里声明 `deps: [ffmpeg]`，`skill_view` 会调 `pm.ensure()` 现场安装，失败只写一条 `deps_note`，不阻断正文。

### 5.3 只有一家改变工具集

Claude Code 的 frontmatter 里有一组字段，加载后会改变当前回合的能力边界：

| 字段 | 效果 |
| --- | --- |
| `allowed-tools` | 本回合的预授权，不是硬白名单；下一条用户消息失效 |
| `disallowed-tools` | 本回合移除工具，下一条用户消息时清除 |
| `model` | 覆盖模型（`haiku`/`sonnet`/`opus`/`fable`/完整 ID/`inherit`） |
| `hooks` | 技能活跃期间注册钩子，形状同 settings.json |
| `context: fork` | 在子代理里执行，`agent` 指定代理类型，`background` 决定是否后台跑 |
| `effort` | 思考档位 |

`allowed-tools` 常被误读成沙箱。它只是 turn 级预授权：没列出的工具仍可走正常权限流程，deny / ask 规则压过它，下一条用户消息到达时授权即失效。真正改变执行位置的是 `context: fork`——正文作为子代理的 prompt、不带对话历史、默认后台跑。

另外 `${CLAUDE_SKILL_DIR}` 与 `${CLAUDE_PROJECT_DIR}` 会在正文与 `allowed-tools` 里做字符串替换；如果技能没有目录却引用了 `${CLAUDE_SKILL_DIR}`，那条工具授权会被丢弃并打点 `skill_load_no_skill_dir_grants_dropped`。

其余三家没有等价物。DSH 的文档明确说技能不改变权限模型，后续调用仍走 executor/sandbox；opencode 加载技能的唯一副作用是往对话里追加文本；hermes 的 `allowed-tools` 只作为扫描器的信息项记录，不生效。想在 DSH 里做「加载技能后多一个工具」，目前唯一的路子是实验性的 action-cards：以 skill 成功加载为条件，让 `propose_action` 从下一步起可见。

## 6. 渐进披露的真实成本

三层结构听起来很省，但省的是「正文」，不是「编目」。

| 层 | 内容 | 什么时候进上下文 | 谁在控制 |
| --- | --- | --- | --- |
| 1 | name + description | 每一轮 | 编目预算 |
| 2 | SKILL.md 正文 | 激活时，一次 | 三家无上限；CC 只对共享记忆技能设 128KB |
| 3 | 引用文件 | 模型按需读 | 文件清单与路径 |

三个实践中的坑。

**一是正文没有上限。** DSH 的 README 直说「加载的指令正文没有大小上限」；opencode 全文进 tool result；hermes 对已存在的巨型 SKILL.md 不截断，只在 Hub 安装时对超过 100KB 的文件警告；Claude Code 只对共享记忆里的技能设了 128KB 上限。一篇 5000 行的 SKILL.md 会把上下文吃穿，而且它进的是历史，之后每一轮都带着。

**二是重复加载不去重。** opencode 同一个技能调两次，会追加两份完整的 `<skill_content>`，只有编目里那句「不需要再调用」在劝阻。Claude Code 会消掉字节相同的重复调用（日志里写 `eliding byte-identical re-invocation`），但只在同一轮内，跨轮仍可重复。DSH 的注入发生在 pre-step，天然只注入一次。hermes 有一个专门的 `skills_tool_dedup`，按 `(task, name, file_path)` 缓存 `skill_view` 的结果摘要，字节相同就返回桩消息。

**三是编目会被压缩吃掉。** DSH 的编目是消息，压缩把它藏进摘要后，下一个 pre-step 会重建；opencode 压缩时会剥掉消息上的 `skills` 附件，但正文已经在历史里；hermes 压缩后清空 `skill_view` 去重缓存，避免返回指向已删除内容的桩。Claude Code 压缩时给每个技能保留最近 5000 token、合计 25000，超出的部分截断，并留一句让模型 `use Read on the skill path` 的提示。

还有一个沉默的失败模式：Claude Code 的 `disable-model-invocation: true` 不只是拦住模型调用，它让这条技能连描述都不进编目；`user-invocable: false` 则相反，斜杠命令藏起来，模型仍能看到。这两条如果写错方向，症状都是「技能凭空消失」，日志里什么都没有。

## 7. 边界

**坏 frontmatter** 有四种处理，宽容度依次上升：

- opencode：解析失败只记一条 debug 日志，技能静默消失。
- DSH：单条 `logger.warn` 后丢弃，兄弟技能不受影响；模型侧没有任何诊断。
- hermes：读路径对坏 YAML 退化成逐行 `key: value`，技能仍会出现在列表里；写路径严格校验，缺 `---`、缺闭合、缺 name/description、frontmatter 后没正文，各自有明确报错。
- Claude Code：`Malformed skill frontmatter` 时仍加载，但所有字段丢弃——name 回落目录名、description 回落正文首行，`allowed-tools`/`model`/`disable-model-invocation` 静默失效。

最后一种最危险：技能看起来能用，限制全没了却没人知道。

**会话中变更**也是四种：opencode 的 watcher 100ms 防抖后全量 refresh，下一次请求发差异；DSH 的 chokidar 只监听根层目录、一层 `.md`、两层 `SKILL.md`，`references/` 改动不触发编目，但正文改动下一次调用就能读到；hermes 靠 30 秒 TTL 与目录签名，原地改 SKILL.md 只能等 TTL；Claude Code 没有 watcher 的字符串证据；按「每轮重算」的模型推断，下一轮就能看到。

**符号链接**：opencode 跟随（先 stat 求真实类型），DSH 跟随（读文件走 realpath），hermes 目录遍历跟随、但 Hub 安装拒绝含 symlink 的包，Claude Code 明确不跟随（日志里写「是链接或无法确认是否为链接，链接不被跟随」）。

## 8. 如果要自己实现

把四家的选择摊开，其实只有七个问题要回答：

1. **编目放 system 还是消息？** 要缓存就放 system，要会话中可变就放消息；想两头都要，就把变更写成追加的差异块（opencode）。
2. **单条描述多长？** 60 字符够模型判断「是不是这一类」，500 字符能写清触发条件。超过 200 字符的描述，边际收益下降得很快。
3. **管不管总量？** 管，就按上下文比例给预算，超了先保内置与固定项，其余坍缩成名字（Claude）；不管，就接受 100 个技能 3k token 的常驻成本。
4. **同名怎么办？** 要么给命名空间（`plugin:skill`、`apps/web:deploy`），要么按优先级覆盖（opencode、DSH，后写胜出），要么拒绝猜（hermes）。最差的是覆盖了还不说——这正是 opencode 与 DSH 的做法。
5. **用户点名算不算执行？** 算，就注入正文（hermes、DSH）；看位置，就按开头/中间分开处理（Claude）。这个选择决定了用户对 `/name` 的信任。
6. **加载后给不给工具授权和执行位置？** 给，就要处理授权的作用域与回收（Claude 的 `disallowed-tools` 在下一轮清除）；不给，技能就只是文本。
7. **写不写？** 让模型写技能（hermes 的 `skill_manage`）等于让它改自己的提示词，需要锁、ledger、写闸门与写后扫描；不写，就只做读路径，简单得多。

最小可用版本可以很小：扫 `<root>/<name>/SKILL.md`，把 `- name: description` 拼进 system prompt，提供一个只接受 `name` 的工具，返回正文加基目录。这大概两百行。剩下的复杂度，都是为了回答上面七个问题。

## 9. 源码位置

| 主题 | 位置 |
| --- | --- |
| opencode 编目与差异 | `packages/core/src/skill/instructions.ts`、`packages/core/src/instructions/index.ts` |
| opencode 发现 | `packages/core/src/config/plugin/skill.ts`、`skill-scan.ts`、`skill/discovery.ts` |
| opencode 执行 | `packages/core/src/skill.ts`、`packages/core/src/tool/plugin/skill.ts` |
| Claude Code 编目/预算 | 二进制字符串：`skill_listing`、`skillListingBudgetFraction`、`truncation_mode` |
| Claude Code 执行 | 二进制字符串：`Base directory for this skill:`、`context: fork`、`allowed-tools` |
| hermes 编目 | `agent/prompt_builder.py` 的 `_render_skills_index` |
| hermes 发现/解析 | `agent/skill_utils.py`、`tools/skills_tool.py` |
| hermes 自管理 | `tools/skill_manager_tool.py`、`hermes_cli/skills_hub.py` |
| DSH 编目与工具 | `packages/skill/tool-skill/src/index.ts` |
| DSH 渲染与注册表 | `packages/skill/skill/src/index.ts`、`packages/skill/skill-filesystem/src/index.ts` |

四家里 Claude Code 是唯一没有可读源码的。这篇里它的每一条结论都来自二进制字符串，能逐字引用的我都引了；涉及行为的（比如「下一轮就能看到变更」）是推断，行文里标了出来。这类逆向笔记的价值不在于精确到行号，而在于把「它大概是怎么想的」和另外三家可读的实现摆在一起——能对上的部分，通常就是这类系统绕不开的结构。
