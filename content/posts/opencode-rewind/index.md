---
title: "Rewind：OpenCode 能退到哪，Claude Code 怎么退，以及从零怎么实现"
date: "2026-11-09"
summary: "OpenCode 有 /undo 与 /redo，但它退的是「到某条消息之前」，redo 只在下次提交前有效；Claude Code 把代码与对话拆成两个开关，让用户当场选退一半。这一篇先拆开两者的语义，再给出一套从零实现 rewind 的步骤：边界、快照、日志、恢复协议、历史截断。"
description: "Rewind 不是「把文件拷回去」。它要回答四个问题：退到哪、退什么、能不能再退回来、退到一半崩了怎么办。OpenCode 与 Claude Code 在这四个问题上的答案不同，从零实现的顺序也不同。"
tags: ["opencode", "architecture", "concurrency"]
slug: "opencode-rewind"
---

> 源码版本：`opencode 2.2.5`，commit `a687c047`（2026-11-08）。代码引用以工作区 `.opencode-src/` 为基准。
> 系列第十二篇。这一篇换了个方向：前十一篇都在讲"它怎么跑"，这一篇讲"**它怎么退**"——以及如果要自己实现一套，顺序应该是什么。

## 0. 三个问题，先给答案

| 问题 | 答案 |
| --- | --- |
| OpenCode 支持 rewind 吗？ | **支持一半**。TUI/Web 里有 `/undo` 与 `/redo`，能回退对话，也能连文件一起回退；但 redo 只活到"下一条 prompt 提交或一次压缩"为止，也不能"只回代码、保留对话" |
| 从零实现要哪些步骤？ | 五件事：定边界、存快照、记日志、写恢复协议、处理历史截断。顺序不能反——先有边界才谈得上恢复 |
| Claude Code 是怎么做的？ | 每个开启一轮的 prompt 前打一个 checkpoint，编辑前存整文件副本；菜单把"代码"和"对话"拆成两个独立开关，让用户选退一半还是全退 |

一个容易先入为主的判断是"OpenCode 没有 rewind"。它其实有，只是**没有一个叫 rewind 的东西**：能力散在 `SessionRevert` 的三个动作和 TUI 的两条命令里。三篇前讲冲突时提过"快照 + revert 兜底"，这一篇把那句话拆开。

---

## 1. OpenCode：有 undo，名字叫 revert

### 1.1 一次 `/undo` 做了什么

TUI 的 `/undo`（快捷键 `<leader>u`）不是简单的"删消息"，它按固定顺序做四件事（`packages/tui/src/routes/session/index.tsx:1120-1147`）：

```text
找最后一条有文字的 user 消息
  → 把它的文本和图片读回输入框（restorePrompt）
  → interrupt 会话，再等它真的空闲（session.interrupt + experimental.session.wait）
  → revert.stage({ sessionID, messageID })
  → 把恢复出来的文本追加回 composer
```

第三步之前的 `interrupt + wait` 不是礼貌，是硬约束：`stage` / `clear` / `commit` 三个服务方法都会先查 `execution.isActive`，忙的时候直接抛 `BusyError`（`packages/core/src/session/session.ts:332-357`）。所以客户端必须先打断、再等它真的空闲。

Web 端还有一套入口：消息菜单里的 "Undo message / Revert message"、`/undo` 与 `/redo` 命令，以及成功 toast 上带一个 Undo（`session-ui` 的 `message-content.tsx:475-490`）。App 的 toast Undo 能把边界再往回挪一个（`revert.ts:273-275`），但只在 toast 的存活期内——它不是一个持久的第二层 redo。

`stage` 之后，TUI 不会立刻删消息——它把边界之后的消息置灰留在屏幕上（`messagesFromRevert`），底部给一条"N messages reverted"的面板，列出被改动的文件与增删行数（`:2260-2300`）。这个时候按 `/redo`，一切还原。

### 1.2 stage 内部：先拍"现在"，再回到"当时"

```ts
// packages/core/src/session/revert.ts:33-43（节选）
const original = input.session.revert?.snapshot ?? (yield* snapshot.capture())
const next = yield* plan(db, { sessionID, messageID })
// 上一次 revert 已经改过的文件，先放回 original
for (const file of input.session.revert?.files ?? []) restore.set(file, original)
if (input.files !== false) for (const [file, tree] of next) restore.set(file, tree)
if (restore.size) yield* snapshot.restore({ files: restore })
```

四行代码，四个决定：

1. **第一次 stage 时 capture 当前树**。这一步把"stage 之前的样子"存下来，`clear`（也就是 `/redo`）靠它把文件放回去。没有这张快照，回退就是单向的。
2. **重复 stage 复用同一份 baseline**，并先把上一次回退过的文件恢复到 original，再算这一次的目标。这样连续 `/undo` 两次，`/redo` 仍然能一次回到最初。
3. **`plan` 算的是"每个文件回到哪棵树"**，不是"回到某一条消息"。它取边界之后所有 assistant 消息，按 seq 升序，每条消息带着自己那一步的 `snapshot.start` 与 `files`，同一个文件第一次出现时的那棵树就是目标（`revert.ts:83-112`）。
4. **`files: false` 就是不碰文件**。但这个模式只有 API 与测试能到（`session-diff.test.ts:188`），TUI/Web 都没有入口。

第 3 点是这份实现里最值得抄的一处。快照是**整棵树**，不是单文件版本；恢复时要按文件回查"这个文件在哪一步被第一次碰过"，否则一个被三步改过的文件会退到中间某一步，而不是边界之前。

还有一个容易漏掉的事实：快照不是"每轮一张"，而是**每个 Step 的起止各拍一次**（`packages/core/src/session/runner/step.ts:74-76`，结果随 `Step.Ended` 存进 assistant 消息，`:252-274`）。所以 `plan` 才能按"步"而不是"轮"去查每个文件的目标版本——粒度是 Step，只是对外暴露的边界是消息。

### 1.3 commit 不在你按 undo 的时候

这是最容易被误解的地方。`stage` 只做两件事：改文件、打标记。真正的删除发生在**你提交下一条 prompt 时**（`packages/tui/src/component/prompt/index.tsx:1345`、`packages/core/src/session/session.ts:173`），或者**触发一次压缩时**（`session.ts:264`）。

删除由投影完成（`packages/core/src/session/projector.ts:1131-1161`），一次删四样东西：

```text
seq ≥ 边界 的 session_message
enqueued_seq ≥ 边界 的 session_inbox
session.revert 标记本身
instruction 状态（rewind 到边界）、不再被引用的 blob
```

所以"undo"在时间轴上被切成了两段：**文件是立刻退的，历史是延迟删的**。中间那段窗口就是 redo 的全部生存空间。

{{< fig "diagrams/01-undo-window.svg" "一次 undo 的三段：文件先动，历史后删，redo 只在中间有效" >}}

### 1.4 `/redo` 其实是 clear

```ts
// packages/core/src/session/revert.ts:59-73
if (!session.revert) return
const original = session.revert.snapshot
if (original) yield* snapshot.restore({ files: ... 每个文件 → original })
yield* bus.publish(RevertEvent.Cleared, ...)
```

它用 1.2 里存的那张 original 树把文件还原，然后撤掉标记、唤醒会话（`session.ts:344-351`）。注意它**不恢复消息**——消息本来就没被删过，只是被标记了，客户端只是过滤了展示（`packages/tui/src/routes/session/index.tsx:182-187`）。

严格说这不是"重做"，是"取消这次回退"。真正的 redo（回退提交之后，再回到回退之前）OpenCode 没有。

### 1.5 快照是怎么来的

不是简单的文件复制，是**一个独立的 git 目录**（`packages/core/src/snapshot.ts:88-118`）：

```text
gitDirectory = <global data>/snapshot/<projectID>/<hash(worktree)>
worktree     = 项目的真实路径
seed         = 真实仓库（继承 ignore 规则与历史）
```

几个直接后果：

- **项目必须是 git 仓库**。`git.repo.discover` 找不到就报 "Project is not a Git repository"，`capture` 返回 `undefined`，于是 `stage` 拿不到快照，文件恢复整体失效，只剩对话回退。
- **未跟踪且超过 2 MiB 的文件进不了快照**（`maximumUntrackedFileBytes`）。模型生成的大文件回不去。
- **符号链接、路径越界会被挡**：`restore` 会检查路径是否逃出项目（`snapshot.ts:205`）。
- 对象打包最多每 10 分钟一次，避免频繁 `git gc`；但**快照对象永不 prune**（`git.ts:1201-1209`），删掉的只是引用。
- 有开关：`snapshots` 配置项（默认开，`schema/config.ts:67-69`）。关掉它，revert 退化成纯对话回退。

### 1.6 它缺什么

| 缺口 | 具体表现 | 影响 |
| --- | --- | --- |
| redo 窗口太短 | 提交下一条 prompt、或触发一次压缩，就 commit | 用户"反悔了想撤回 undo"，只能靠自己的 VCS |
| 没有 code-only | 只有"对话+文件"与"只对话"（且只对话只有 API 能到） | 想保留推理过程、只重置代码时做不到 |
| 只支持整段边界 | API 粒度是 messageID，不能"只恢复这两个文件" | 影响面小的回退也要动全部文件 |
| 依赖 git | 非 git 项目 capture 返回 `undefined`，restore 直接抛错 | 新项目、导出目录、非代码仓库全部退化 |
| 不记录"没改动" | 没有 Claude Code 那种 "No code restore" 提示 | 用户以为文件退了，其实什么都没发生 |
| 恢复不事务 | checkout + 删除，先删后写，中途失败留半恢复状态；没有 stash、没有三方合并 | 崩溃或断电会留下一个"退了一半"的工作区 |
| 并发不设防 | staged 期间手改同一文件，redo 时会被 original 整文件覆盖 | 覆盖用户手改，且没有提示 |
| 无选择列表 | 入口是"消息菜单/命令"，不是"checkpoint 列表" | 退到第 5 轮之前要按五次，且看不到每次的改动摘要 |

最后两条值得单独说。`restore` 的实现是 `git checkout` 加"树里没有就删"（`git.ts:1138-1198`），没有备份、没有冲突标记；`stage` 也不在事务里——它先把文件改完，再发布 `Staged` 事件。所以"回退到一半失败"是一个真实存在的状态，只是概率低。

不过它有两点比 Claude Code 强，容易被忽略：

- **redo 是真的**。`clear` 会把文件放回去并唤醒会话，不只是"取消一个标记"。
- **不污染项目自己的 `.git`**。快照放在 `global.data/snapshot/...` 的独立仓库里，项目仓库的 index、stash、分支一个都不动。Claude Code 用文件副本，也不污染，但两者的磁盘代价不同：OpenCode 靠对象去重，Claude Code 靠 100 个 checkpoint 的上限。

---

## 2. 先把 rewind 定义清楚

"支持 rewind"这句话没有意义，除非先答四个正交问题：

| 维度 | 可选值 | OpenCode 的取值 |
| --- | --- | --- |
| 恢复什么 | 代码 / 对话 / 两者 / 指定文件 | 对话，或对话+全部文件 |
| 粒度 | 轮（prompt）/ 步（step）/ 工具调用 / 单文件 | 轮（messageID 边界） |
| 可逆性 | 一次性 / 可 redo（窗口内）/ 可 redo（永久） | 窗口内可 redo，直至下次提交 |
| 边界何时落定 | 立即 / 延迟提交 / 显式提交 | 延迟到下一次 prompt |

这四个维度组合出十几种产品形态。Claude Code 的菜单之所以是六个选项，就是因为它把第一维拆成了两个开关（代码、对话）再各自组合；OpenCode 把它们绑成一个动作，用一个布尔参数选一半。

选择没有对错，但有一个后果：**维度越少，用户越难表达"我只想退一部分"**。而"只退一部分"恰恰是 rewind 最常用的场景——代码写坏了，但对话里的推理还想留着。

---

## 3. 从零实现：五个零件，六个步骤

### 3.1 零件

{{< fig "diagrams/02-data-model.svg" "回退需要的三个存储对象与一个可选的改动日志" >}}

最小实现只需要三样东西：

1. **边界（boundary）**：一个会话内可定位的点。最自然的是消息的 `seq`，因为历史本来就按它排序。
2. **快照（checkpoint / tree）**：每个边界对应的文件树。可以整树存，也可以按内容寻址只存变更的文件。
3. **恢复协议**：把"当前树"变成"目标树"的过程，以及失败时怎么办。

强烈建议加第四样：**改动日志（journal）**。它记录"哪一步碰过哪些文件"。没有它也能做，但只能整树回滚；有了它才能做单文件恢复，也才能在 UI 上说清"这一步改了三个文件"。

第五样是**意图记录（intent log）**，只在你要处理崩溃时才需要：先把"我要恢复到 tree X"写下去，再动手；崩了重放时，看到这条记录就知道该继续还是该回滚。

### 3.2 六个步骤

{{< fig "diagrams/03-restore-steps.svg" "恢复的六个步骤，以及唯一允许写盘的那一步" >}}

**① 算清单**。给定边界，产出 `文件 → 目标版本` 的映射。两个细节决定正确性：同一个文件被多步改过时取"最早那次之前"；被删除的文件也要在清单里（目标是"不存在"），否则恢复完会多出文件。

**② 预检**。在碰任何文件之前，把"不能做的"先列出来：路径越界、符号链接/硬链接、文件被外部改过、目标路径被锁、文件太大没进快照。能跳过就跳过，但**必须计数并告诉用户**——静默跳过比不跳更糟。

**③ 保护现状**。capture 当前树，记进这次回退的元数据。这一步同时解决两个问题：失败可回滚、成功可 redo。

**④ 逐文件写**。这是唯一动磁盘的一步。按路径加锁（跨会话也要锁，否则另一个会话正在写同一文件），保留 BOM 与行尾，目标树里不存在的路径要删掉。

**⑤ 校验**。读回来和快照比一次。不一致就整体回滚到 ③，不要"补第二次"——补丁式的修复会让状态越来越难解释。

**⑥ 截断历史**。删边界之后的消息、收件箱、被引用的 blob。**必须在文件恢复成功之后做**：如果先删历史再恢复文件，恢复失败时你既丢了历史又没退成文件。

### 3.3 三个容易写错的地方

**顺序**。文件先回，历史后删。反过来，一旦恢复失败，边界消息已经没了，用户连"我刚才是从哪退的"都查不到。

**幂等**。崩溃后无法判断"第 4 步做到哪了"，所以恢复必须设计成可重放：给定同一个目标树，执行一次和执行三次结果相同。这也是为什么 ④ 用"写整文件"而不是"应用补丁"——整文件写天然幂等，补丁不幂等。

**部分失败**。要么全成，要么全退。允许"三个文件成功、两个失败"看起来更友好，实际上让用户无法判断当前处于哪个状态，也让 redo 失去参照。

### 3.4 采集时机：边界前，还是每次编辑前

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| 每个 Step 起止 capture（OpenCode） | 粒度是步，能精确定位到哪一步改了哪个文件；比整树，所以 shell 的改动也算得进去 | 两次 capture 之间的外部改动会被算进"模型改的" |
| 每次编辑前存副本（Claude Code） | 精确到工具调用，能只回某一个文件的某一次编辑 | 覆盖不到 shell 与外部进程；文件多时副本数量大 |

这两条不是优劣，是**你在为哪种副作用负责**。OpenCode 选了整树，所以 `git diff` 看得见的改动它都能退（包括 `sed -i`）；Claude Code 选了单文件副本，所以它明确声明"只被 bash 改过的文件不跟踪"。

一个折中做法是两条都留：整树快照负责"这一步的净效果"，单文件副本负责"这次编辑的精确回滚"，UI 上默认用前者。

### 3.5 redo 的三种做法

1. **保留 original 树**（OpenCode）：stage 时存"现在"，clear 时还原。简单，但只覆盖"回退之后、提交之前"。
2. **反向补丁**：把"恢复"本身也记成一次变更，redo 就是再应用一次反向变更。能做永久 redo，但要处理补丁冲突。
3. **快照栈 + 墓碑**：历史不物理删，只标记失效；redo 就是把标记撤掉。历史永远在，代价是存储与查询复杂度。

第三种最"正确"，也最贵。OpenCode 与 Claude Code 都选了"延迟提交"这个便宜的近似：不删，等用户下一步动作再删。差别只在等多久。

### 3.6 非 git 项目

`Snapshot.capture` 依赖 git 仓库。要支持非 git，最直接的是**内容寻址存储**：对每个文件算哈希，按哈希存内容，树就是"路径 → 哈希"的列表。首次要全量扫一遍，之后只存变化的文件。代价是磁盘与首次延迟，好处是不再依赖 VCS，也能天然去重。

### 3.7 并发与崩溃

- **会话锁**：同一个会话同一时刻只能有一次恢复。
- **路径锁**：跨会话按真实路径加锁，和 `edit`/`patch` 用的是同一把（`file-mutation.ts` 里的 KeyedMutex）。但要注意它只在进程内有效，跨进程仍会打架。
- **外部改动检测**：恢复前比一次 mtime + 内容哈希，发现被外部改过就报"会覆盖你的改动"，让用户选。
- **崩溃**：靠意图记录 + 幂等重放。启动时扫描"有意图、未完成"的记录，重放到收敛。

---

## 4. Claude Code 怎么做

{{< fig "diagrams/04-claude-code.svg" "Claude Code 的 checkpoint 时间轴与六个动作" >}}

### 4.1 触发

`/rewind`（别名 `/checkpoint`、`/undo`），或者输入框为空时连按两次 `Esc`。有文本时双 Esc 只清空草稿，不会误开菜单。

菜单六项：

```text
Restore code and conversation   两者都退
Restore conversation            只退对话，代码保留
Restore code                    只退文件，对话保留
Summarize from here             从这里往后压成摘要（不动文件）
Summarize up to here            把这里之前压成摘要（保留后面的消息）
Never mind                      什么都不做
```

两个 code 选项**只在所选点之后确实存在被跟踪的文件改动时才出现**；没有改动时只剩对话与摘要。这是个很细的设计：它用菜单本身回答了"这里有没有东西可退"。

### 4.2 采集与存储

官方文档的说法是：每个"开启一轮"的 prompt 创建一个 checkpoint，跟踪**文件编辑工具**所做的改动；保留最近 100 个 checkpoint；快照约 30 天后由 retention sweep 删除（`cleanupPeriodDays` 可调）。

更底层的存储位置来自 settings 文档与社区观察（非官方专门文档）：编辑前把整文件副本写进 `~/.claude/file-history/<session>/`，元数据以 `file-history-snapshot` 记在 `~/.claude/projects/<project>/<session>.jsonl`。

关键差异在这里：**它不需要项目是 git 仓库**，因为存的是文件副本，不是 git 树。这也是为什么它能对"非 git 目录"工作，而 OpenCode 不能。

### 4.3 三个反直觉的行为

**bash 的改动会被"连带"回退，也可能完全不回退**。官方原文：一旦 Claude 用编辑工具改过某个文件，`/rewind` 会连同其后 bash 对该文件的改动一起撤销；但一个**只被 bash 改过**的文件不在跟踪范围，保持原样。原因很简单：恢复的单位是整文件快照，只要这个文件进过快照，回退就是整文件覆盖。

**回退完直接退出，消息会回来**。因为会话是随"下一条 prompt"落盘的：回退只改了内存里的会话状态，没有重新写盘。官方给的操作建议是——要么发一条 prompt，要么接受它回来。

**轮次中途插进来的消息没有自己的 checkpoint**。菜单里标 `No code restore`，要实现回退只能选那一轮开头的 prompt，代价是把整轮的改动都退掉。

### 4.4 官方列出的限制

| 限制 | 说明 |
| --- | --- |
| Bash 改动 | 只被 bash 改过的文件不跟踪；已被工具改过的文件，其 bash 改动会被整文件覆盖 |
| 子代理 | 默认不恢复；只有前台 `context: fork` 的 skill 例外 |
| 外部改动 | 只跟踪本会话编辑过的文件；其他会话/手工改动不跟踪 |
| 轮次中途消息 | 没有 checkpoint |
| 符号链接/硬链接 | 跳过，并提示 `Restored the code, but skipped N files` |
| 保留期 | 超过 100 个或 30 天，快照没了，回退会报 `No files were restored` |
| 定位 | 官方明确："不是版本控制的替代品" |

### 4.5 和 OpenCode 的对照

| 能力 | OpenCode | Claude Code |
| --- | --- | --- |
| 只回对话 | 有（`files:false`） | 有（菜单项） |
| 只回代码 | **没有** | 有 |
| 两者一起 | 有（默认） | 有 |
| 粒度 | 消息（轮） | 消息（轮） |
| 需要 git | **是** | 否（文件副本） |
| redo | 窗口内（下次提交前） | 未找到公开说明 |
| 保留策略 | 依赖快照仓库，无明确上限 | 100 个 / 30 天 |
| 跳过文件提示 | 无 | 有（计数 + 警告） |
| 非 git 项目 | 文件回退失效 | 可用 |
| 摘要式压缩 | 独立机制（compaction） | 菜单内置（两个 Summarize） |

这张表里最值得注意的不是"谁多一个功能"，而是**两者对"回退的单位"理解不同**：OpenCode 的单位是"会话历史的一个位置"，Claude Code 的单位是"一次用户意图"。前者更像数据库事务回滚，后者更像编辑器的 undo 栈。

---

## 5. 如果给 OpenCode 补全 rewind

不需要重写，现有结构已经占了七成。按改动量从小到大：

**5.1 只回文件（code-only）**
`stage` 已经有 `files` 开关，缺的是"只恢复文件、不标记历史"。最小改法是给 `SessionRevert` 加一个 `restore({ sessionID, messageID, paths? })`：内部仍走 `plan`，但只 `snapshot.restore`，不发布 `Staged`、不写 `session.revert`。代价是没有 redo——除非顺手把 original 树存进一个临时表。

**5.2 延长 redo 窗口**
现在有三个提交点：下一条 prompt 准备成功（`session.ts:173`）、压缩（`session.ts:264`）、显式 `commit`。两个可选方向：改成显式提交（用户点"确认回退"才删），或者加超时（例如 5 分钟不操作才 commit）。前者最安全，后者最接近现在的体验。

**5.3 单文件恢复**
`plan()` 返回的本来就是 `Map<文件, 树>`，在 API 上加一个 `paths?: string[]` 过滤即可。UI 上把"N files reverted"面板做成可勾选列表。

**5.4 非 git 的兜底**
`Snapshot.capture` 失败时（非 git），退化成一次"内容寻址"的临时快照，或者至少明确提示"此项目未启用文件回退"，而不是静默只退对话。

**5.5 跳过提示**
`restore` 现在会跳过越界路径，但不告诉用户。补一个返回值：跳过了几个、为什么。Claude Code 的 `skipped N files` 就是这个位置的标准答案。

**5.6 "没有改动"的边界**
当 `plan` 返回空 map 时，UI 应该说"这一点之后没有文件改动"，而不是显示 "0 files reverted" 让人以为退成功了。

---

## 6. Rewind 的边界

不管实现得多完整，有几件事 rewind 永远做不到：

- **进程外的副作用**：`git push`、部署、发消息、写数据库、装依赖。快照只管文件，管不了世界。
- **被忽略的文件**：未跟踪且超过阈值、`.gitignore` 里的构建产物、node_modules。
- **子代理与后台任务**：它们的改动可能发生在另一条时间线上（OpenCode 的前台 subagent 会被中断，后台的不会）。
- **时间与钱**：已经花掉的 token、已经过期的缓存。

所以正确的定位是：**rewind 是会话级的恢复，不是版本控制**。两者不冲突——rewind 解决"刚才那一下"，git 解决"这三天"。

---

## 7. 证据索引

| 结论 | 证据 |
| --- | --- |
| `/undo` 的顺序：读回 prompt → interrupt → wait → stage | `packages/tui/src/routes/session/index.tsx:1120-1147` |
| `/redo` 调 `revert.clear`，且只在有 revert 标记时可用 | `packages/tui/src/routes/session/index.tsx:1148-1160` |
| stage 先 capture 现状，再按 plan 恢复 | `packages/core/src/session/revert.ts:22-56` |
| plan 取"每个文件第一次出现的 start 树" | `packages/core/src/session/revert.ts:83-112` |
| clear 用 original 树还原文件并撤标记 | `packages/core/src/session/revert.ts:59-73` |
| commit 由投影删消息/收件箱/指令状态/blob | `packages/core/src/session/projector.ts:1131-1161` |
| 提交下一条 prompt 时自动 commit | `packages/tui/src/component/prompt/index.tsx:1345`、`packages/core/src/session/session.ts:173` |
| 快照是独立 git 目录，依赖项目为 git | `packages/core/src/snapshot.ts:88-118` |
| 未跟踪文件 2 MiB 上限、每 10 分钟 pack | `packages/core/src/snapshot.ts:133-148` |
| TUI 回退面板显示文件与增删行数 | `packages/tui/src/routes/session/index.tsx:2260-2300` |
| Claude Code 的 checkpoint 时机、菜单、限制 | `https://code.claude.com/docs/en/checkpointing` |
| Claude Code 快照位置与开关 | settings 文档 + 社区观察（非官方专门文档） |

一篇写不完的东西：如果真要把 5.1–5.6 落成补丁，还需要先定 redo 的语义——是"撤销这次回退"，还是"回到回退之前再退一次"。这两个问题的答案不同，数据结构也不同。
