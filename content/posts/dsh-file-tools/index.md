---
title: "文件工具族：读、写、改、搜的四种失败模式"
date: "2026-11-14T17:15:00+08:00"
summary: "read/write/edit/glob/grep 五个工具的参数为什么长这样：截断、唯一匹配、陈旧读取、并发写，各自对应哪条源码约束。"
description: "从 tool-fs、tool-fs-search、tool-str-replace-editor 的契约出发，拆文件工具的失败模式与审批、沙箱的介入点。"
tags: ["harness", "architecture", "agent"]
slug: "dsh-file-tools"
---

> 依据：`deepseek-harness` v0.2.2-rc.1（commit `fcc0ad46`）。证据等级 A：逐符号核对 `packages/fs/*` 源码与 `docs/subsystems/filesystem.zh.md` 契约文档，文中行号取自该 commit 工作树。

一个模型读完 3000 行日志，上下文只剩一半；它接着写一个文件，把别人刚改的配置覆盖回去；它用 `edit` 改一个 `return null`，结果报「匹配到 3 处」；它用 `glob` 找到了 `.env.local`，换 `grep` 搜同样的名字却什么都没有。

这四件事看起来是四类 bug，翻源码会发现它们由四组不同的常量与契约决定，分散在 `tool-fs`、`tool-fs-search`、`dsh-fs-local`、`dsh-fs-observation-policy` 四个包里。参数表不是随手定的，每个上限都在替某一层兜底。

本文按「参数、截断、陈旧、并发、编码、沙箱」六条线拆这五个工具，最后给一张可以照着排查的失败模式表。

## 0. 结论

| 失败模式 | 触发条件 | 源码约束 | 模型看到什么 |
| --- | --- | --- | --- |
| 读太多 | 单文件超过 50 KiB 或行宽超 2000 字符 | `READ_MAX_BYTES`、`READ_MAX_LINE_LENGTH`、`limit` 默认 2000 | 截断 footer 加 `Use offset=N to continue.` |
| 覆盖不该覆盖的 | `write` 作用于已有文件 | `writeText` 省略 intent 即无条件覆盖 | `Updated file`，不回显旧内容 |
| 改错位置 | `old_string` 在文件里出现多次 | `replace_all` 默认 false，provider 要求唯一 | 一次失败，文件未变 |
| 搜不到 | 目标被 `.gitignore` 忽略或位于隐藏目录 | `glob` 传 `--no-ignore --hidden`，`grep` 不传 | `glob` 有结果，`grep` 无结果 |
| 改了旧内容 | 两次调用之间文件被外部修改 | 只有组合 `fs-observation-policy` 才带版本守卫 | 默认部署静默覆盖；装了策略报 `FS_STALE_VERSION` |
| 结果被截 | `glob` 超 100 条、`grep` 超 250 条 | `GLOB_MAX_RESULTS`、`GREP_MAX_MATCHES` | 计数头加 spill 路径 |
| 原始输出爆掉 | 单次 `rg` 超 20 MB | `RAW_OUTPUT_MAX_BYTES` | `SEARCH_RAW_OUTPUT_OVERFLOW`，整次失败 |

七行里只有最后一行是错误，前六行都是工具在正常工作时主动选择的行为。区分这两类，是读懂这五个工具参数的起点。

## 1. 五个工具，一张参数表

`tool-fs` 注册 `read`、`write`、`edit`，外加可选的 `apply_patch`；`tool-fs-search` 注册 `glob`、`grep`；`tool-str-replace-editor` 是另一套编辑器方言，默认组合里不启用。

| 工具 | 必填 | 可选 | 默认 | 校验位置 |
| --- | --- | --- | --- | --- |
| `read` | `file_path` | `offset`、`limit` | `offset=1`、`limit=2000` | `parseReadArgs` |
| `write` | `file_path`、`content` | 无 | 无 | `parseWriteArgs` |
| `edit` | `file_path`、`old_string`、`new_string` | `replace_all` | `false` | `parseEditArgs` |
| `glob` | `pattern` | `path` | 会话 workspace | `parseGlobArgs` |
| `grep` | `pattern` | `path`、`include` | 会话 workspace | `parseGrepArgs` |

`read` 的两个数字都是常量：

```ts
// packages/fs/tool-fs/src/read.ts:15
export const READ_LIMIT = 2000
// packages/fs/tool-fs/src/read.ts:21
export const STREAM_MIN_SIZE = 10 * 1024 * 1024
```

`limit` 的上界来自插件配置 `readLimit`，默认就是 `READ_LIMIT`。`parseReadArgs` 要求 `offset` 与 `limit` 都是正整数，`limit` 不得超过配置值，超了在参数解析阶段就失败，不会传到 provider。

`edit` 的参数校验比 `read` 严，它在进入文件系统之前先拒绝三类调用：

```ts
// packages/fs/tool-fs/src/edit.ts parseEditArgs
// file_path 非空；old_string 非空；old_string !== new_string
// 文案：old_string and new_string must differ
```

第三条容易被忽略：用 `edit` 把 A 改成 A 是参数错误，不是无操作。这样能挡掉模型自我确认式的空转调用。

## 2. read：三层上限与一个精确的总行数

`read` 的截断发生在三个互相独立的位置，任何一层先到都会停。

| 层 | 常量 | 单位 | 到达后 |
| --- | --- | --- | --- |
| 行数 | `limit`，默认 2000 | 行 | 停止保留，继续计数 |
| 单行宽度 | `READ_MAX_LINE_LENGTH`，2000 | 字符 | 该行截断并加后缀 |
| 总字节 | `READ_MAX_BYTES`，50 × 1024 | 字节 | 停止保留，继续计数 |

关键在「继续计数」。`read-render.ts` 的 `consumeLine` 在字节预算耗尽或行数超限之后仍然递增 `totalLines`，所以返回给模型的 `totalLines` 是全文行数，哪怕只显示了 2000 行。

单行超宽的后缀是固定文案：

```text
... (line truncated to 2000 chars)
```

三种 footer 对应三种结束原因，模型据它决定下一次 `offset`：

```text
(Output capped. Showing lines 1-2000. Use offset=2001 to continue.)
(Showing lines 1-500 of 4213. Use offset=501 to continue.)
(End of file - total 4213 lines)
```

`offset` 越界会报 `FS_NOT_FOUND`，不返回空结果，文案带上文件总行数：

```text
offset 9000 is out of range for "src/app.ts" (4213 lines)
```

这让「读完了」变成一个可判定的状态：模型拿到 footer 的第三种，就知道不必再翻。

大文件走另一条路。`read.ts` 的 execute 先做一次 stat，拿到 `type`、`size`、`version`；`size` 未知或大于等于 `STREAM_MIN_SIZE`（10 MiB）时改走 `streamText`，由 backend 负责跨 chunk 的 UTF-8 解码与二进制拒绝。窗口化读取与整文件读取在授权上没有区别，两者都算一次观察，因此不存在「只读了一半所以不能写」这种中间状态。

字节上限还有一处硬拒绝：`readBytes(target, signal, maxBytes)` 超过 `maxBytes` 直接报 `FS_TOO_LARGE`，不做截断。这条路径给需要完整字节的调用方用，模型侧的 `read` 不走它。

{{< fig "diagrams/01-read-window.svg" "read 的三层预算：行数、单行宽度、总字节，任何一层先到就停止保留，但 totalLines 仍然精确" >}}

> 自己搭 Agent 时：把「继续读」做成 `offset` 参数，比把整个文件塞进上下文便宜得多，也比让模型自己算行号可靠。代价是你要保证 `totalLines` 在截断后仍然准确，否则模型永远不知道该跳到哪一行。

## 3. write：一次调用就是一次覆盖

`write` 的参数只有两个，`content` 可以为空字符串，语义是清空文件。`parseWriteArgs` 只校验 `file_path` 非空，其余交给 provider。

工具的执行体里没有 stat：

```ts
// packages/fs/tool-fs/src/write.ts
// No stat — the bare default never manufactures a version basis.
```

这句注释解释了默认行为：`write` 不读文件，也不构造版本依据，它把 `content` 与一个 intent 一起交给 `ctx.fs.writeText`。intent 来自 waterfall：

```ts
ctx.waterfall('fs/write-intent', target, exec, () => undefined)
```

默认 listener 返回 `undefined`，于是 `writeText` 收到的是无条件写入。结果由 provider 的 `FsWriteOutcome` 描述：

| 字段 | 含义 |
| --- | --- |
| `operation` | `create` 或 `update` |
| `version` | 写入后的新版本 |
| `before` | 写入前内容，LF 归一化；`null` 表示新建或后端未提供基线 |
| `after` | 写入后内容，LF 归一化 |

模型看到的文本只有一行判断，不含文件内容：

```text
<path>src/config.ts</path><type>file</type><content>
Updated file
</content>
```

回显旧内容会把一次写入变成一次读取，上下文成本翻倍，而 UI 侧的 diff 卡片用的是 `before`/`after`，不需要模型再看一遍。`before` 为 `null` 时 diff 数组为空，这正是「覆盖了一个没读过的文件」在界面上的表现。

`systemPrompt` 里有一句建议：

```text
Read an existing file before overwriting it with write and prefer edit for targeted changes.
```

它是建议，不是门禁。默认组合里没有任何东西会阻止你直接覆盖一个没读过的文件。

## 4. edit：唯一匹配是一次校验

`edit` 的契约有三个互相独立的约束：

| 约束 | 违反时 | 谁检查 |
| --- | --- | --- |
| `old_string` 非空 | 参数错误 | `parseEditArgs` |
| `old_string !== new_string` | 参数错误 | `parseEditArgs` |
| 未设 `replace_all` 时必须唯一 | 编辑失败，文件不变 | provider，在锁内 |

第三条是重点。匹配是字面匹配，先按行尾归一化再比，所以模型从 `read` 里复制出来的文本可以直接用，CRLF 文件也不会因为 `\r` 而匹配失败。唯一性检查与写入在同一把锁里完成，中间没有窗口让别的进程插入。

provider 的失败文案区分两种原因：

```text
cannot edit "src/app.ts": file changed since it was read
cannot edit "src/app.ts": old_string matched 3 locations
```

第一句是版本问题，第二句是匹配问题。模型看到哪一句，决定了它下一步是重读还是缩小锚点。

`replace_all: true` 把唯一性约束降级为「至少匹配一处」，成功文案会变：

```text
The file src/app.ts has been updated. All occurrences were successfully replaced.
```

为什么用内容锚点而不是行号：行号在两次调用之间会漂，内容锚点只有在内容真的变了才失效。这是个交换，锚点越长越不容易误匹配，也越容易被无关改动打断。

{{< fig "diagrams/03-stale-version.svg" "edit 的版本守卫是一条可选链路：观察事件写入版本，intent 事件取出守卫，provider 在锁内比对" >}}

### 4.1 另一套方言：str_replace_editor

`tool-str-replace-editor` 是另一个包，接口按「命令」组织，不按「字段」组织。

| 动作 | `tool-str-replace-editor` | `tool-fs` |
| --- | --- | --- |
| 看文件 | `view` 加 `view_range` | `read` 加 `offset` / `limit` |
| 新建 | `create` | `write`，目标不存在时即创建 |
| 修改 | `str_replace`，`old_str` 唯一 | `edit`，`old_string` 唯一 |
| 插入 | `insert`，按行插入 | 无此命令，用 `edit` 扩锚点 |
| 删除 | `new_str` 省略 | `new_string` 传空字符串 |
| 超长输出 | `<response_clip>` 标记 | footer 加 `offset` 续读 |

两条最容易踩的差异：

`str_replace` 遇到多处匹配时不替换，也不猜该改哪一处，返回一句说明让模型自己收窄。`edit` 的行为一致，但它多一个 `replace_all`，模型可以在明确要全改时把它设成 true。

`view` 的 `view_range` 用 `[start, -1]` 表示读到文件尾。`-1` 是一个哨兵值，不是行号，`read` 用 `limit` 表达同一件事，没有哨兵。

两套接口并存的理由在训练分布。一部分模型对「先 view 再 str_replace」的流程更熟，另一部分对「read 窗口加锚点」更熟。工具目录可以按部署选，两边的底层都走同一个 `ctx.fs`，守卫与沙箱行为一致。

超长输出的处理没有统一。`str_replace_editor` 用 `<response_clip>` 标记把中间截掉，`tool-fs` 用尾部窗口加 footer 指路。看截断标记就能反推是哪个工具返回的。

## 5. glob 与 grep：两套 ignore 语义

这两个工具都调随包的 ripgrep，但 argv 完全不同，差别集中在一个决定上：要不要尊重 `.gitignore`。

| 维度 | `glob` | `grep` |
| --- | --- | --- |
| 子命令 | `--files` | `--json` |
| 忽略规则 | `--no-ignore` | 默认，尊重 `.gitignore` |
| 隐藏文件 | `--hidden` | 默认跳过 |
| 排序 | `--sort=modified` | 文件内按行号 |
| 结果上限 | 100 条路径 | 250 条匹配 |
| 单条上限 | 无 | 每行 2000 字节 |

`glob` 的 argv 是这样拼的：

```ts
// packages/fs/tool-fs-search/src/glob.ts:104 buildGlobCommand
const parts = ['--files', ..., `--glob=${input.pattern}`, '--sort=modified',
  '--no-ignore', '--hidden', ...GLOB_VCS_EXCLUDES.flatMap(...)]
```

`--no-ignore` 让 `glob` 能看见被 `.gitignore` 排除的构建产物与本地配置；`--hidden` 让它能看见 dotfile。代价是 VCS 元数据会淹没结果，所以 `GLOB_VCS_EXCLUDES` 硬编码排掉六个目录：`.git`、`.svn`、`.hg`、`.bzr`、`.jj`、`.sl`，每个名字用两条否定 glob，一条剪枝目录，一条挡住「搜索根就在该目录里面」的情况。

`grep` 不加这两个开关，所以它看到的是仓库的「已跟踪视图」。后果很直接：一个被忽略的文件，`glob` 找得到，`grep` 搜不到。两个集合都正确，`glob` 负责发现文件，`grep` 负责在正常源码里搜索。

`grep` 的 argv 更短：

```ts
// packages/fs/tool-fs-search/src/grep.ts:114 buildGrepCommand
const parts = ['--json', `--regexp=${input.pattern}`]
```

三个模型可控值全部作为独立 argv 元素传递，pattern 与 include 用 `--flag=value` 形式，目标路径放在 `--` 之后，所以以短横线开头的值不会被解析成 flag。整条链没有 shell，不存在引号问题。

安全上有一条必须加的开关。spawn 时 `search-core.ts` 在 argv 前插入 `--no-config`：

```ts
// packages/fs/tool-fs-search/src/search-core.ts:240
argv: [await resolveRgPath(), '--no-config', ...argv],
```

原因是搜索的 spawn 不受沙箱约束，而宿主环境变量 `RIPGREP_CONFIG_PATH` 或二进制旁边的 `rg.conf` 可以注入 `--pre`，让 ripgrep 对每个匹配文件执行任意预处理器。`--no-config` 把这条注入路径关掉。

上限与失败方式：

| 常量 | 值 | 溢出后 |
| --- | --- | --- |
| `GLOB_MAX_RESULTS` | 100 | 抽样或取头部，完整列表进 spill |
| `GREP_MAX_MATCHES` | 250 | 保留前 250 条，完整结果进 spill |
| `GREP_MAX_LINE_BYTES` | 2000 | 该行加 ` (line truncated)` |
| `RAW_OUTPUT_MAX_BYTES` | 20 000 000 | 整次失败，报 `SEARCH_RAW_OUTPUT_OVERFLOW` |
| `SEARCH_TIMEOUT_MS` | 30 000 | 进程树被终止 |

20 MB 那条选择硬失败，不截断，理由是 `rg --json` 是内部传输格式，截断一半的 JSON 无法解析出可信的匹配列表。宁可让模型收窄 pattern，也不返回半份结果。

`glob` 超限时默认取修改时间头部；本目录的配置把 `sampleOverCapGlobResults` 打开，改成按顶层条目抽样。这样 100 条里至少每个顶层目录都有一条，不会因为某个 `dist/` 里修改时间最新就把结果全占了。

{{< fig "diagrams/02-search-cap.svg" "glob 与 grep 的两条上限通道：内联页、spill 落盘、footer 指路，以及两者 ignore 语义的差异" >}}

## 6. 大结果：spill 只出现在两个工具里

`read` 没有 spill。它有 `offset`，续读是廉价的，把 50 KiB 之外的内容写进文件再让模型 `read` 一次，多了一次往返，还多了一份可能过期的副本。

`glob` 与 `grep` 没有续读接口，它们的上限是「条目数」，模型无法说「从第 101 条继续」，所以超出部分必须落到别处：

| 工具 | 内联上限 | 超限动作 | 模型看到 |
| --- | --- | --- | --- |
| `read` | 2000 行 / 2000 字符每行 / 50 KiB | 截断加 footer | 继续 offset |
| `glob` | 100 条 | 抽样加 spill | footer 带路径与读法 |
| `grep` | 250 条 | 前缀保留加 spill | 同上 |
| `bash` | 每流 64 000 字节尾部 | spill 临时文件 | 尾部加路径 |
| `terminal` | 单次结果 256 KiB | 视图截断 | 游标读取 |

两张 footer 的文案不一样：

```text
Full sorted result stored at: <locator>. <retrievalHint>
Full grep result stored at: <locator>. <retrievalHint>
```

`locator` 在本地 backend 下是路径，在远程 backend 下可能是 URI 或 key，工具只把 `retrievalHint` 原样渲染出来，不解析 locator 的结构。保存失败时降级为另一句：

```text
The complete result could not be saved; narrow pattern or path to see more.
```

spill 是可选服务。`ctx.get('spillStore')` 拿不到时 `trySaveFormattedResult` 只 warn 一次，工具照常返回截断后的内联页，不把成功调用变成错误。

## 7. 陈旧读取：默认配置里没有检测

这是本文最容易被误读的一点。`read`、`write`、`edit` 三个工具本身都不做版本比对，工具层也没有任何重试或合并逻辑。它们做的是发事件：

```ts
// packages/fs/tool-fs/src/read.ts 成功读取后
ctx.emit('fs/observed', target, { kind: 'present', version: info.version }, exec)
// packages/fs/tool-fs/src/edit.ts 编辑前
ctx.waterfall('fs/edit-intent', target, exec, () => undefined)
```

默认 listener 是 `() => undefined`，也就是「没有守卫」。把观察变成守卫的是另一个包：

```yaml
# packages/bundle/base/cordis.patch.yml:292
- id: fs-observation-policy
  name: '@deepseek-ai/dsh-fs-observation-policy'
  disabled: true
```

默认组合里它是关的。打开之后，链条才完整：

| 步骤 | 发生什么 |
| --- | --- |
| `read` 成功 | 发 `fs/observed`，携带 `version` |
| 策略记录 | `WeakMap<session, Map<targetKey, FsObservation>>` 存下 present 与 version |
| `edit` 开始 | 策略在 `fs/edit-intent` 返回 `{ version }` |
| provider 执行 | 锁内比对当前版本，不一致抛 `FS_STALE_VERSION`，文案 `file changed since it was read` |
| 模型恢复 | 重新 `read`，拿到新内容与新版本，再 `edit` |

`write` 的守卫由 `fs/write-intent` 产生，规则是「没见过或已确认不存在就给 `createIfAbsent`，见过就给 `replaceIfVersion`」。`createIfAbsent` 撞上已存在的目标报 `FS_NOT_OBSERVED`，这条错误码在默认部署里几乎不会出现，因为没有策略就没有这个 intent。

所以「谁负责发现旧内容」的答案是分层的：

- 工具层不负责，它没有版本知识，也不缓存文件。
- provider 负责检测，它是唯一在锁内看到「当前内容」的地方。
- 模型负责恢复，重读是它唯一能做的事。
- 组合负责人决定要不要这层保护，即部署方在 `cordis.yml` 里决定。

不装策略时，`write` 覆盖一个刚被改过的文件不会报错，`edit` 的 `old_string` 如果还匹配得上也会成功。这是有意的默认值，因为它让单机、单 agent 的常见场景少一次读。

> 自己搭 Agent 时：先决定版本由谁持有，再决定要不要重试。工具层拿不到版本时，重试只是把同一个失败再跑一遍；把 CAS 放进 provider 的锁里，一次比对就能给出确定结论。

## 8. 并发写：为什么工具层不排队也不合并

工具流水线按 `isConcurrencySafe` 给同批调用分组：

```ts
// packages/core/tools/src/pipeline.ts:105 executionMode
if (!tool?.isConcurrencySafe) return { kind: 'exclusive' }
const concurrencySafe: unknown = tool.isConcurrencySafe(exec.arguments)
return concurrencySafe === true ? { kind: 'parallel' } : { kind: 'exclusive' }
```

`tool-fs` 里只有两个工具声明了并行安全：

```ts
// packages/fs/tool-fs/src/read.ts:136
isConcurrencySafe: () => true,
```

`write`、`edit`、`glob`、`grep` 都没声明，因此按 `exclusive` 串行执行。`read` 敢声明并行，是因为它的观察竞态是 fail closed 的：即使两次读取交错，后续受守卫的写入也会在锁内复查版本。

串行解决了同一批里的互斥，但工具层仍然不做两件事：

1. 不合并。两个 `edit` 的 `old_string` 可能重叠，合并需要语义，工具层只有字符串。
2. 不重试。重试需要知道「上次为什么失败」，而 `FS_STALE_VERSION` 的正确恢复是重读，重读是模型的决定。

同一个回复里连续两次编辑同一文件时，第二次看到的是第一次的结果，因为 provider 每次都读当前内容。这解释了一个常见现象：第一次 `replace_all: false` 成功，第二次同样的 `old_string` 报「匹配到 2 处」，因为第一次替换把另一处也改成了相同文本。

## 9. 行号、编码、换行符：哪些是承诺

| 项 | 是否承诺 | 依据 |
| --- | --- | --- |
| 行号 1-based | 是 | `read-render.ts` 的 `formatLine` 用 `${number}: ${text}` |
| 行号相对整个文件 | 是 | `offset` 只移动窗口，不改变编号 |
| `totalLines` 是全文行数 | 是 | 截断后仍继续计数 |
| 输出按 LF 断行 | 是 | 渲染前 `stripCarriageReturn` 去掉行尾 `\r` |
| `before`/`after` 是 LF 归一化文本 | 是 | `FsWriteOutcome` 与 `FsEditOutcome` 的契约 |
| 输入是 UTF-8 | 是 | backend 负责跨 chunk 解码，非法字节按二进制拒绝 |
| 写回时保留原换行风格 | 否 | 由 provider 决定，本地 provider 写 LF |
| 编码自动探测 | 否 | 只按 UTF-8 处理 |

两个预算的单位也不同：`READ_MAX_LINE_LENGTH` 按 JS 字符串长度计，`READ_MAX_BYTES` 按字节计。一行中文在字符数没超时可能先撞上字节预算，所以同一份文件在中英文混排时截断位置会不一样。

`stripCarriageReturn` 只在渲染层工作，它不修改文件。模型看到的是去掉 `\r` 的行，`edit` 的匹配也在归一化后的文本上做，所以从 `read` 输出里复制 `old_string` 到 CRLF 文件里能匹配上。写回时是否保留 CRLF 由 provider 决定，这一点在契约里没有承诺。

`read` 的包络是固定的三段：

```text
<path>src/app.ts</path>
<type>file</type>
<content>
1: import ...
</content>
```

`presentResult` 用一条正则还原这个结构，还原失败就走通用卡片。这个设计的副作用是：模型文本与 UI 卡片共享同一份 `presentationMeta`，两者不会对「显示了哪几行」产生分歧。

## 10. 审批与沙箱插在哪一步

`write` 与 `edit` 的 execute 是固定四步，沙箱与审批插在第一步：

| 顺序 | 调用 | 归属 |
| --- | --- | --- |
| 1 | `sandbox.resolvePolicy('write' 或 'edit', args, exec)` | `FsSandboxController`，内部用 `ctx.approval` |
| 2 | `ctx.fs.resolve(path)` | `ctx.fs` seam |
| 3 | `ctx.waterfall('fs/write-intent' 或 'fs/edit-intent')` | 观察策略 |
| 4 | `ctx.fs.writeText` / `editText(..., sandboxPolicy)` | provider，可能抛 `FS_SANDBOX_DENIED` |

`FsSandboxController` 在构造时读 `ctx.fs.sandboxMode`。为 `undefined` 说明挂载的文件系统不限制，`escalationModes` 为空，schema 里不会出现 `sandbox_permissions`；有值则要求 `ctx.get('sandboxPolicy')` 存在，缺失直接抛：

```text
tool-fs: the mounted filesystem confines but ctx.sandboxPolicy is missing
```

这是「配置错误在最早可解析点失败」的一个例子：与其在第一次写入时静默放行，不如在装配阶段就崩。

`resolvePolicy` 的三种情况：

- 请求的模式与 standing mode 相同，直接复用，不问人。
- 省略了模式但带了 `justification`，报参数错误。
- 请求更宽松的模式，必须有非空 `justification`，并经 `ctx.approval` 批准这一次调用。

批准之后只替换 `sandboxPolicy` 这个值，其余参数不变。拒绝由 provider 在 `ctx.fs` 内部抛 `FS_SANDBOX_DENIED`，工具再用 `sandbox.mapError` 转成带标记的错误：

```text
[sandbox: file access denied under workspace-write mode]
```

这条标记与内核的 `FS_PERMISSION_DENIED` 分开，前者是策略拒绝，重试没有意义，后者可能是权限配置问题。

与 `tools/pre-execute` 的关系：那是流水线里 execute 之前的通用决策点，任何插件都能在那里整体拒绝或替换一次调用。文件工具的沙箱不经过它，走的是自己 execute 内的第一步。两者可以叠加，`pre-execute` 先跑，`resolvePolicy` 后跑。

{{< fig "diagrams/04-write-path.svg" "write 与 edit 的四步执行：审批在 resolvePolicy，守卫在 intent waterfall，拒绝由 provider 抛出，pre-execute 在更外层" >}}

> 自己搭 Agent 时：把「策略拒绝」与「系统权限不足」做成两个错误码。前者模型不该重试，后者模型可以换个路径再试，混在一起会让模型反复撞同一堵墙。

## 11. 失败模式表

| 症状 | 根因 | 检测点 | 正确做法 |
| --- | --- | --- | --- |
| 读到一半没了，模型以为文件就这么长 | 撞上 `READ_MAX_BYTES` 或 `limit` | footer 是 `Output capped` 或 `Showing lines` | 按 footer 给的 offset 续读 |
| 模型反复读同一段 | 忽略了 `totalLines`，自己算偏移 | 返回里有 `totalLines` | 用 `offset = 已读行数 + 1` |
| 写入后旧配置消失 | 默认无版本守卫，直接覆盖 | `operation: 'update'` 且 `before` 非 `null` | 先 `read` 再 `write`，或用 `edit` |
| 编辑报「匹配到 3 处」 | 锚点太短 | 错误文案带匹配数 | 加长 `old_string`，或显式 `replace_all` |
| 编辑报「file changed since it was read」 | 版本守卫生效，文件已被改 | `FS_STALE_VERSION` | 重读，用新内容做锚点 |
| `glob` 有、`grep` 没有 | `.gitignore` 或隐藏目录 | 对比两次调用的 pattern 与 path | 用 `glob` 定位，再直接 `read` |
| `grep` 整次失败 | 原始输出超 20 MB | `SEARCH_RAW_OUTPUT_OVERFLOW` | 收窄 pattern、path 或 include |
| 结果只显示 100 条 | `GLOB_MAX_RESULTS` 生效 | footer 有 `Showing 100 of N` | 读 spill 文件，或缩小 path |
| Windows 上 `edit` 匹配失败 | 锚点里混入了 `\r` | `read` 输出行尾干净 | 从 `read` 复制，不手打 |

表里九行，前八行都能从模型看到的文本里判断。最后一行属于另一类：模型把不可见字符带进了锚点。

> 自己搭 Agent 时：先把这张表写出来，再写 schema。每个上限都对应一行「症状」，写不出来的上限通常是拍脑袋定的。

## 12. 设计取舍

1. `read` 不做 spill，做 offset 分页。代价是模型可能要多轮；收益是不需要额外存储，续读位置可复现，也不会出现「读了旧副本」。
2. `edit` 用内容锚点而不是行号。代价是锚点要足够长；收益是行号漂移不影响正确性。
3. 唯一匹配默认开启。代价是多一次失败往返；收益是默认不会误改多处，批量替换必须显式声明。
4. 版本守卫放进可选组合。代价是默认部署没有陈旧检测；收益是单机场景少一次读，需要严格性的部署自己打开。
5. `glob` 忽略 `.gitignore`，`grep` 尊重它。代价是两个工具的视野不一致；收益是「发现文件」与「搜索源码」各自得到想要的集合。
6. 上限写成常量加 config。代价是要读代码才知道默认值；收益是同一份默认值在所有部署里一致，配置只在需要时覆盖。

前四条都在同一个方向上：把正确性交给最靠近状态的那一层，工具层只负责传参和渲染。

## 13. 踩坑点

1. 现象：`read` 返回 2000 行，模型说「文件共 2000 行」。原因：只看了 `lines.length`，没看 `totalLines`。对策：把 `totalLines` 放进包络头部，footer 只留续读提示。
2. 现象：`write` 之后 UI 没有 diff。原因：文件是新建的，`before` 为 `null`，`diffs` 是空数组。对策：新建与覆盖在卡片上分开渲染。
3. 现象：`edit` 在 CRLF 文件上偶发失败。原因：`old_string` 里带了 `\r`，归一化只处理行尾。对策：从 `read` 输出复制，或在工具描述里明确这一点。
4. 现象：`glob` 返回一堆 `node_modules` 路径。原因：`--no-ignore` 加上只排除了 VCS 目录。对策：在 pattern 里带上目录前缀，`buildGlobCommand` 会把简单目录前缀转成剪枝 glob。
5. 现象：`grep` 找不到模型刚用 `write` 创建的文件。原因：文件还没被 git 索引，且命中了 `.gitignore`。对策：用 `read` 或 `glob` 确认存在，再决定搜索方式。
6. 现象：装了 `fs-observation-policy` 之后，`write` 开始报 `FS_NOT_OBSERVED`。原因：模型没读过目标文件，策略给出 `createIfAbsent`。对策：把「先读后写」写进 systemPrompt，并让错误文案直接说「先 read」。

## 14. 源码索引

| 位置 | 证明什么 |
| --- | --- |
| `packages/fs/tool-fs/src/read.ts:15` | `READ_LIMIT = 2000` |
| `packages/fs/tool-fs/src/read.ts:21` | `STREAM_MIN_SIZE = 10 MiB`，超过走流式读取 |
| `packages/fs/tool-fs/src/read-render.ts:11` | `READ_MAX_LINE_LENGTH = 2000` |
| `packages/fs/tool-fs/src/read-render.ts:14` | `READ_MAX_BYTES = 50 KiB` |
| `packages/fs/tool-fs/src/edit.ts` `parseEditArgs` | 三个参数约束与 `must differ` 文案 |
| `packages/fs/tool-fs/src/edit.ts` execute | 「No stat」注释与四步顺序 |
| `packages/fs/tool-fs/src/write.ts` execute | 省略 intent 即无条件写入 |
| `packages/fs/tool-fs/src/sandbox.ts` `resolvePolicy` | 审批与模式升级的判定 |
| `packages/fs/tool-fs-search/src/glob.ts:25` | `GLOB_MAX_RESULTS = 100` |
| `packages/fs/tool-fs-search/src/glob.ts:104` | `--no-ignore --hidden` 与 VCS 排除 |
| `packages/fs/tool-fs-search/src/grep.ts:29` | `GREP_MAX_MATCHES = 250` |
| `packages/fs/tool-fs-search/src/grep.ts:114` | grep 的 argv 只有三个元素 |
| `packages/fs/tool-fs-search/src/search-core.ts:36` | `RAW_OUTPUT_MAX_BYTES = 20 MB` |
| `packages/fs/tool-fs-search/src/search-core.ts:240` | spawn 前插入 `--no-config` |
| `packages/fs/fs/src/types.ts:118` | `FsWriteIntent` 两种守卫与错误码 |
| `packages/fs/fs-observation-policy/src/index.ts` `writeIntent` | 观察状态如何变成 intent |
| `packages/bundle/base/cordis.patch.yml:292` | 策略默认 `disabled: true` |
| `packages/core/tools/src/pipeline.ts:105` | 未声明并行安全即串行 |

## 最后

五个工具的参数可以归到三句话：能分页的给分页（`read`），不能分页的把溢出写出去（`glob`、`grep`），能比对版本的把比对放进锁里（`edit` 的 intent）。三条各自对应一种失败，越界的调用在参数层、provider 层、组合层分别被拦下。

决定默认行为的是组合，与单个工具无关。`fs-observation-policy` 关着的时候，`write` 就是无条件覆盖，`edit` 就是「按当前内容匹配」；打开之后，同一份代码开始报 `FS_STALE_VERSION`。同一个工具在两份 `cordis.yml` 里可以有完全不同的安全性，这在读源码时很容易被忽略。

下一篇讲 shell 与终端：一次命令为什么会有一次性、持久、PTY 三种生命周期，以及输出截断如何在这三种形态里各自保住上下文。

上一篇：[00 第三季：从工具到约束](/posts/dsh-season-three.html)｜下一篇：[02 Shell 与终端：一次性、持久、PTY 三套语义](/posts/dsh-shell-and-terminal.html)

延伸阅读：[工具执行流水线：九段与三闸](/posts/dsh-tool-pipeline.html)、[工具结果：一次调用的四个投影](/posts/dsh-tool-results.html)、[沙箱、权限与审批：工具的边界](/posts/dsh-tool-boundaries.html)、[执行环境：进程、文件与网络](/posts/dsh-execution-environment.html)。
