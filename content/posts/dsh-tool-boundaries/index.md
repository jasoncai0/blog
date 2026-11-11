---
title: "沙箱、权限与审批：工具的边界"
date: "2026-11-11T14:40:00+08:00"
summary: "模型 schema、注册表过滤、审批、OS 沙箱是四层不同的边界，各自能挡的攻击面并不重叠；把任意一层当成全部，都会留下一条可走的旁路。"
description: "拆解 DSH 的四层防御：schema 只影响模型、过滤让工具等同于不存在、审批只授权一次、沙箱只关进程；再补上 FS provider 的策略围栏，解释为什么路径检查必须由 executor 层强制。"
tags: ["harness", "architecture", "agent"]
slug: "dsh-tool-boundaries"
---

> 源码版本：`deepseek-harness` v0.2.2-rc.1（commit `fcc0ad46`）。本文对照 `docs/subsystems/{sandbox,filesystem,approval}.zh.md` 与 `packages/fs/fs-sandbox`、`packages/shell/bash-sandbox`、`packages/interaction/user-approval`。
> 本系列第 11 篇。上一篇讲一次调用在流水线里的顺序，这一篇讲每一道边界到底能挡住什么。

先看三个容易吵起来的问题。第一个：在 `workspace-write` 模式下，模型 `write` 到工作区外会被拒绝，但用 `read` 读 `/etc/passwd` 却成功了——这算不算沙箱失效？第二个：用户刚点了"允许一次"执行某条 `bash`，模型紧接着又发一条，还是弹窗；用户说"不是刚允许过吗"。第三个：你在工具实现里认真检查了 `path.startsWith(workspace)`，reviewer 仍然说"这条检查不算数"。

三个问题的共同点是：大家把"边界"当成了一个东西。实际上 DSH 里有四层边界，外加一层容易被误认成沙箱的路径围栏。它们防的不是同一类攻击，失效方式也各不相同。

本文按"一次 `bash` 调用从模型到进程"的顺序走一遍，再回头逐个说清每层的能防与不能防。最后用三个路径逃逸案例，看各层分别怎么反应。

## 0. 先给结论

| 层 | 机制 | 挡住什么 | 挡不住什么 |
|---|---|---|---|
| 1 模型 schema | 工具描述、参数 schema、系统提示 | 模型的误用、参数形状错误 | 被注入的模型、直接调用、恶意参数 |
| 2 注册表过滤 | scope + restriction | 能力暴露；被过滤工具拒绝执行 | 已可见工具的参数滥用；同 scope own 注册 |
| 3 审批 | 一次性 `approval.request` | 未经同意的这一**次**调用 | 用户点错；下一次调用；工具内部越权 |
| 4 OS 沙箱 | `confine(argv)` → 内核策略 | 进程写工作区外、执行期越界 | FS API 调用；读取；网络 |
| 4.5 FS 围栏 | provider 内 canonicalize-then-contain | 经 FS 服务的越界写入、symlink 逃逸 | 不经 FS 服务的进程；读取；TOCTOU 残余 |

第 1 层是"让模型不知道"，第 2 层是"让模型看不见也调不到"，第 3 层是"让这一次需要人点头"，第 4 层是"即使模型骗过了前面所有层，进程也出不去"。第 4.5 层不是内核边界，它解决的是另一类问题：`write` 工具根本不启动进程，OS 沙箱对它是无效的。

一句话版本：**schema 管模型，过滤管可见性，审批管单次授权，沙箱管进程，FS 围栏管路径。**

这四层不是同心圆，而是四个不同的问题：模型知不知道、能不能调到、这一次允不允许、进程实际能碰什么。把它们画成同心圆会诱导一个错误直觉——"里面的层守住了，外面的层就无所谓"。实际恰恰相反：任意一层单独存在，都有一条完整的旁路。schema 可以被人为绕过（模型被注入），过滤挡不住已可见工具的参数滥用，审批挡不住用户点错，沙箱挡不住 FS API。

{{< fig "diagrams/01-four-layers.svg" "图注：四层边界从模型侧到内核侧依次排开，每层标注它能挡住的攻击面与典型旁路；最右侧补上 FS provider 的策略围栏，说明它为什么不在 OS 沙箱之内。" >}}

## 1. 一次 bash 调用：从 schema 到进程

把顺序展开，才知道每层在哪里生效。

1. **声明**：`tool-bash` 只在挂载了 confining executor 时，往 schema 里加 `sandbox_permissions` 和 `justification`。没有沙箱的组合根本不宣传这两个参数。
2. **模型生成参数**：模型看到 schema，决定是否请求升级。它可能不请求，也可能请求一个比当前更宽的模式。
3. **工具参数校验**：`validateBashArgs(args, standingPolicy?.mode)` 检查参数形状，包括两个升级参数必须成对出现。
4. **pre-execute**：`tools/pre-execute` 上的 hooks、permission 插件可以拒绝或要求审批。
5. **升级审批**：如果模型请求了 `sandbox_permissions`，`approveBashEscalation()` 在**任何东西执行之前**调用 `ctx.approval.request()`。
6. **guards**：注册的单调 guard 做最后检查。
7. **execute／body**：`tool-bash` 重新解析 standing policy，把批准后的 mode 合进本次调用的 `policy` 副本。
8. **`ctx.shell.execute()`**：shell 服务把请求交给 bash executor。
9. **`bash-sandbox`**：`danger-full-access` 直接 spawn 原始 argv；其余模式先 `ctx.sandbox.confine(['bash','-c', command], policy, signal)`，拿到 `ConfinedArgv` 再 spawn。
10. **结果分类**：先看 runner failure（命令根本没跑起来），再看 denial（沙箱挡住了命令），最后才看退出码。

```ts
const standingPolicy = resolveSandboxPolicy(exec)
validateBashArgs(args, standingPolicy?.mode)
if (args.sandbox_permissions !== undefined) {
  approvedMode = await approveBashEscalation(args, standingPolicy, exec)
}
const policy = { ...standingPolicy, mode: approvedMode ?? standingPolicy.mode }
```

哪一行是关键：`approveBashEscalation` 在 `ctx.shell.execute` 之前。审批的对象是"即将发生的这一次执行"，不是"这个工具"，也不是"这个会话"。它拿到的 `effectiveMode` 是当前调用的真实模式，不是部署默认值。

{{< fig "diagrams/02-bash-sequence.svg" "图注：一次带升级请求的 bash 调用从 schema 校验、pre-execute、审批、guards 到 confine 与 spawn 的时序；审批发生在 spawn 之前，拒绝路径不会创建进程。" >}}

## 2. 审批是一次性授权，不是永久权限

`approval.request()` 的返回值是一个闭合集合：`allowed-once`、`rejected`、`cancelled`、`unavailable`。没有 `allowed-always`。

```ts
export type ApprovalOutcome = 'allowed-once' | 'rejected' | 'cancelled' | 'unavailable'
```

哪一行是关键：`allowed-once` 这个名字本身就是契约。它只对当前 `callId` 生效，调用结束即作废。DSH 没有"记住我的选择"这种持久授权；如果用户想放宽，那是改会话的沙箱模式（`sandbox/mode`），是另一条显式的状态变更路径，不是审批的副作用。

审批服务还有几条容易忽略的规则：

- `request()` 要求会话处于未结束的轮次。没有打开的 turn，就没有可归属的授权。
- 先 append `approval/asked`，再 append `approval/decided`。两条都只进日志，不进模型 transcript。模型永远不知道用户被问过。
- 请求里**不带工具参数**。UI 通过 `callId` 关联已经流式呈现的那次调用，避免参数在审批界面和实际执行之间出现两个版本。
- `reason` 是审计文本，`displayReason` 是本地化 UI 文本。前者给日志，后者给人。
- 审批服务缺失、不负责这个请求、抛异常、返回词汇表之外的值，一律 `unavailable` → 拒绝。abort 则是 `cancelled` → 拒绝。整个路径 fail-closed。
- `ApprovalPolicy = 'never'` 在 waterfall 之前就返回 `rejected`，任何 prepend 的应答者都绕不过。这是部署层的硬开关，不是"默认拒绝"的软约定。

四种结果在流水线里的落点也不一样：

| 审批结果 | 含义 | 调用走向 |
|---|---|---|
| `allowed-once` | 用户同意这一次 | 继续 guards |
| `rejected` | 用户点了拒绝 | deny，理由是 `the user rejected tool "x"` |
| `cancelled` | 审批被中断 | deny，且 `approvalCancelled` 为真 |
| `unavailable` | 没有通道/没有 agent/服务异常 | deny，不重试、不降级 |

注意 `unavailable` 不是"问一下再决定"，而是直接拒绝。一个没有审批服务的部署，不会因为"没人能批"就放行需要审批的调用；它会让这个调用失败，并且在错误里说明原因。

升级审批还多一层检查：请求的模式必须**严格宽于**当前模式。`read-only` 只能升到 `workspace-write` 或 `danger-full-access`；`workspace-write` 只能升到 `danger-full-access`；请求当前模式本身直接返回，不弹窗。

```ts
if (mode === effectiveMode) return mode
if (!WIDER_MODES[effectiveMode].includes(mode)) {
  throw new Error(`sandbox escalation to "${mode}" is not strictly wider than this call's current "${effectiveMode}" mode`)
}
```

为什么要"严格更宽"？因为审批界面要回答的问题是"这次要不要放宽"，而不是"要不要换一个模式"。允许平级或降级会让审批语义漂移：用户以为在授权更宽，实际可能收窄了后续行为。

审批还会留下两条只写日志、不进模型 transcript 的事件：`approval/asked` 和 `approval/decided`。模型看不到"用户犹豫了 30 秒"或"用户第一次点了拒绝"，它只看到工具结果。这是刻意的：审批是给人和审计看的，不是给模型推理用的额外上下文。否则模型会学会"多申请几次总能过"，把审批当成一个可以优化的奖励信号。

## 3. 沙箱模式的语义差异

`SandboxMode` 有三个值，但它们不是"同一把尺子的三档"。

| 模式 | 写入 | 工作区 | 额外目录 | 读取 | 实现路径 |
|---|---|---|---|---|---|
| `read-only` | 拒绝 | 不可写 | 不可写 | 允许 | 仍走 confine，写入被后端拒绝 |
| `workspace-write` | 允许 | 可写 | `additionalWritableRoots` 可写 | 允许 | confine 到这些根 |
| `danger-full-access` | 允许 | 可写 | 任意 | 允许 | **不调用 confine**，直接 spawn |

`danger-full-access` 最容易被误解。它不是"沙箱配置得很宽"，而是"根本没有沙箱这一层"：消费方直接 spawn 原始 argv，不调用 `ctx.sandbox.confine()`。这也是为什么它不能被发给 provider 作为"受限模式"——没有 `ConfinedArgv`，就没有 `enforcement`、`denialSignatures`、`runnerFailureRules` 这些元数据。

`read-only` 也不是"什么都不让做"。它允许读、允许执行只读命令，只是所有写入都被后端拒绝。POSIX runner 的做法是给写入一个 `/dev/null` 接收器；Windows ACL runner 则没有显式可写根，并且会报告 `enforcement: 'partial'`。

`enforcement` 只有 `full` 和 `partial` 两个值。partial 不是"差不多"，而是一个必须向上暴露的事实：旧版 Landlock ABI、Windows ACL 对硬链接和读取的限制不完整、AppContainer 的边界差异，都会让它变成 partial。上层可以据此拒绝执行，而不是假装隔离完整。

模式本身也不是全局常量，而是逐调用解析出来的：

```ts
const policy = ctx.sandboxPolicy.resolve({ session, mode: approvedMode })
```

哪一行是关键：`resolve` 的优先级是"已批准的显式 mode > 会话最后一条 `sandbox/mode` > 部署默认"。`workspace-write` 的边界就是会话 cwd；没有 agent 的调用（比如初始化钩子）用配置的 root。这意味着同一个进程里，不同会话可以有不同的工作区边界，沙箱策略不能缓存成模块级单例。

还有两个失败要分清：

- **denial**：沙箱正常工作，并且挡住了命令。这是"边界生效"的证据。
- **runner failure**：命令根本没跑起来（spawn 失败、runner 自身错误）。这不是边界生效，是执行失败。

`ConfinedArgv` 同时带 `denialSignatures` 和 `runnerFailureRules`，消费方必须先查 runner failure，再查 denial。把它们混成一个"失败了"，会让模型以为"沙箱不让"，从而去申请一个根本没用的升级。

## 4. 为什么"工具自己检查路径"不够

假设你写一个 `write_file` 工具，在 `execute()` 里做了这件事：

```ts
const abs = path.resolve(args.path)
if (!abs.startsWith(workspaceRoot)) throw new Error('outside workspace')
await fs.writeFile(abs, args.content)
```

这段代码有三个问题，每一个都不是"再仔细一点"能解决的。

第一，**检查和使用之间隔着时间**。`resolve` 之后、`writeFile` 之前，路径上的某一级目录可能被换成符号链接。你检查的是旧身份，写的是新身份。这就是 TOCTOU。

第二，**它不是唯一的入口**。同一个 `FileSystem` 服务会被 `write`、`edit`、`patch`、`move`、`delete` 多个工具调用；未来还会有插件直接调用 `ctx.fs.writeText()`。你在一个工具里检查，绕过它的调用者不受约束。安全决定必须在"真正做操作的那个对象"上执行。

第三，**字符串前缀不是包含关系**。`/workspace-evil` 以 `/workspace` 开头；Windows 有 8.3 别名和大小写不敏感；`..` 在 `resolve` 之前和之后含义不同。自己写 `startsWith` 几乎一定会漏。

DSH 的做法是把围栏放进 provider：`SandboxedFileSystem extends LocalFileSystem`，覆写五个变更操作，每次调用先 `checkedTarget()`。

```ts
protected override async writeText(target: FsTarget, ...) {
  const fenced = await this.checkedTarget(target, sandboxPolicy)
  return super.writeText(fenced, ...)
}
```

哪一行是关键：`checkedTarget` 返回的是**重新解析后的 target**，后续变更用的就是它。检查的身份就是变更的身份，不存在"检查这里、写到那里"。

`checkedTarget()` 的逻辑很短：

- `danger-full-access`：原样返回，不围栏。
- `read-only`：抛 `FS_SANDBOX_DENIED`，文本是 `cannot write "..." : file access denied under read-only mode`。
- `workspace-write`：先 `this.resolve(target.displayPath)` 重新 realpath（拿到 `fresh`），再对每个可写根做 `isPathUnder(fresh.targetKey, root)`；不在任何根下就抛，命中就返回 `fresh`。

`isPathUnder` 也不是字符串前缀。它先走 `isLexicallyUnder` 快路径，拼写不同时用 `stat(root, {bigint:true})` 取 `dev + ino`，沿 `dirname` 上溯 target 的现存祖先做 identity 比较。这能识别 Windows 8.3 别名、大小写差异，以及"路径字符串不同但指向同一个 inode"的情况。

但这段代码的注释非常诚实：

> The fence is a policy check in TRUSTED code over a MODEL-CONTROLLED path, NOT a kernel boundary … This is containment, not a security boundary; kernel-grade isolation of untrusted CODE stays ctx.shell's job.

也就是说：它防的是"模型给了一个越界路径"，不是"模型在你的进程里执行任意代码"。后者只能靠 OS 沙箱。残余的 TOCTOU（祖先符号链接在重新检查和 syscall 之间被换掉）被"委托前立刻重新 canonicalize"缩窄，但仍然存在，并且被明确接受——因为威胁模型里只有路径不可信。

## 4.5 注册表过滤：最像权限，也最不像

四层里，注册表过滤最容易被误解成"权限系统"。它确实决定模型能看到哪些工具，也确实会在执行时拒绝被过滤的调用，但它不是授权：它没有"这一次"的概念，也没有用户参与。

它的机制在上一篇已经拆过，这里只保留和边界有关的部分：

- `view(scope)` 先合并 global 与祖先链上的工具，再对继承名字逐层 `admits(name)`；任一层过滤掉，这个名字就不进 `visible`。
- 被过滤的工具**既不出现在提示词里，也在执行时被拒绝**，而且拒绝的形状和"工具不存在"完全一样：`UNKNOWN_TOOL`。模型无法区分"没有这个工具"和"这个工具被你的 scope 过滤了"。
- `own` 注册不受 restriction 约束。也就是说，一个 scope 可以注册一个和全局同名、但实现更窄的工具，而全局那个仍被过滤。这是"受控变体"的正规写法，也是 restriction 不是绝对黑名单的原因。

```ts
if (layers.every(layer => layer.admits(name))) visible.set(name, registration)
```

哪一行是关键：`every`。多层 restriction 取交集，不是并集；父层的 deny 会参与子层解析，所以子 agent 不能通过"自己再 allow 一次"把父层禁掉的工具拿回来。

它能挡什么？能力暴露。一个只该读代码的子 agent，不应该在提示词里看到 `deploy`；看不到，模型就不会去编造参数、不会在失败后反复尝试、也不会被提示注入引导去调用。它挡不住什么？已经可见的工具被滥用。`bash` 对子 agent 可见，不代表子 agent 的每次 `bash` 都安全；那要靠审批和沙箱。

这也是为什么 DSH 不把"工具自己检查权限"当作边界。一个工具的 `execute()` 里可以检查调用者身份，但那个检查发生在工具内部，绕过它的调用者（另一个工具、一个插件、一次 PTC 子调用）不受约束。真正的强制点在 executor：`executable()` 在执行时应用当前 `toolSet`，没有集合时排除条件注册；注册被替换或新 restriction 生效，都会让未开始的调用直接变成 `UNKNOWN_TOOL`。

## 5. 三个路径逃逸案例

### 5.1 `..` 穿越

`write` 收到 `../../etc/passwd`。`resolve` 会把它规范化成绝对路径，`checkedTarget` 在 `workspace-write` 下重新 realpath，发现不在任何可写根下，抛 `FS_SANDBOX_DENIED`。模型看到的是带 mode 的标记，而不是一句"permission denied"。

关键点：拒绝发生在**变更提交时**，不是参数解析时。所以即使路径在解析后被换掉，围栏仍按提交时的真实身份判断。

### 5.2 符号链接

攻击者在工作区内放一个 `link -> /etc`，然后让模型写 `link/passwd`。`resolve` 会跟随符号链接，得到 `/etc/passwd`；`checkedTarget` 重新 realpath 后仍然得到 `/etc/passwd`，不在可写根下，拒绝。

如果模型写的是工作区内一个**指向工作区外的新文件**的符号链接，`write` 的目标在创建时会跟随链接吗？`FileSystem` 的写入语义是"目标已跟随符号链接"，所以围栏看到的是最终目标。要拒绝"源是符号链接"的场景（比如 patch/delete/move 的源必须是普通文件），必须在 `resolve` 之前 `lstat`——这也是文档里明确写的：目标已跟随链接，所以拒绝符号链接源要在 resolve 前 lstat。

### 5.3 绝对路径

`write` 收到 `/tmp/x`。如果 `/tmp` 不在 `additionalWritableRoots` 里，`workspace-write` 拒绝；`read-only` 更早拒绝。`danger-full-access` 直接放行——这一层根本没有围栏。

这里有一个不对称，值得单独说：**FS 工具没有升级参数**。`sandbox_permissions` 是 `bash` 的 schema 特性，`write` 不宣传它。所以 `write` 被沙箱拒绝之后，模型不能"申请一次更宽的模式重试"，只能向用户说明，或者改用 `bash`（而 `bash` 的升级又要单独审批）。这个设计减少了 FS 层的攻击面，但也意味着 FS 拒绝是"终局"的，模型不应该反复重试同一个路径。

{{< fig "diagrams/04-path-escapes.svg" "图注：三种路径逃逸在 resolve、checkedTarget、内核 confine 三层分别被哪一层拦下；符号链接案例标出 lstat 必须发生在 resolve 之前。" >}}

### 5.4 三个案例的共同结构

把三个案例放在一起，会发现它们不是三种不同的攻击，而是同一个结构的三次实例化：

| 案例 | 模型看到的输入 | 攻击面 | 被哪一层拦下 |
|---|---|---|---|
| `..` 穿越 | 一个看起来相对的路径 | 字符串前缀检查 | provider 围栏（canonicalize 后判断） |
| 符号链接 | 一个位于工作区内的路径 | 路径解析顺序 | provider 围栏（realpath 后判断） |
| 绝对路径 | 一个明显越界的路径 | 无（最容易被误以为已挡住） | provider 围栏或 read-only 更早拒绝 |

共同点：**判断的对象必须是最终目标，而不是模型给的字符串**。任何"检查 `startsWith(workspace)`"的实现都会被前两个案例绕过；任何"检查发生在 resolve 之前"的实现都会被第二个案例绕过，因为 resolve 之后目标已经变了。

这也是 `checkedTarget` 返回 `fresh` 而不是只返回布尔值的原因：检查的身份必须就是随后用于变更的身份，否则检查和写入之间还有一次替换机会。


## 6. 沙箱只管进程，不管 FS API

这是最容易被忽略的一条边界。`ctx.sandbox.confine()` 的输入是 argv，输出是一个被内核策略约束的 argv；它约束的是**这个进程及其子进程**能做什么。它完全不知道 `ctx.fs.writeText()` 的存在。

所以 DSH 有两条互不替代的写入路径：

- **进程路径**：`bash` 启动 `sh -c 'echo x > /outside/file'`。这里靠 OS 沙箱。`bash-sandbox` 在 spawn 前 confine，写入越界会被内核拒绝，stderr 里出现 denial signature，工具把它分类成 `denied`。
- **API 路径**：`write` 调用 `ctx.fs.writeText()`。这里根本没有进程，OS 沙箱没有机会介入。靠的是 `SandboxedFileSystem` 的策略围栏。

如果只做其中一条，就会留下一条完整的旁路。只做 OS 沙箱，模型可以用 `write` 工具绕过；只做 FS 围栏，模型可以用 `bash` 绕过。两条都做，才覆盖"文件被写的两种方式"。

`bash-sandbox` 的 confine 发生在 spawn 的回调里，而不是 spawn 之前：

```ts
if (mode === 'danger-full-access') return super.execute(spec)
const result = await this.executeArgv(spec, async (signal) => {
  const prepared = await this.confine(spec.command, { ...policy, mode }, signal)
  signal.throwIfAborted()
  confined = prepared
  return prepared.argv
}, ctx)
```

哪一行是关键：`return prepared.argv`。传给 spawn 的是 confine 之后重新构造的 argv，不是原始命令；`confined` 同时被记下来，供后续 `classifyDenial` 使用。中间那次 `throwIfAborted` 保证审批/取消到达时不会再启动一个已经不该跑的进程。

还有两个更细的缺口，文档没有掩饰：

- **读不受限**。`SandboxedFileSystem` 的读操作完全不拦，所有模式都允许读。`read` 工具可以读工作区外的文件，只要宿主进程有权限。这是有意的：读的风险远低于写，而且很多任务需要读系统文件；但它意味着"沙箱"不等于"信息隔离"。
- **网络不受限**。`SandboxMode` 只管控文件系统效果。`bash` 里的 `curl` 能不能出去，取决于 OS 沙箱后端是否额外限制了网络——Landlock 本身是文件系统策略。不要把"workspace-write"理解成"断网"。

### 6.1 为什么网络不能顺手交给"沙箱"

很多团队的第一版会想："既然已经有沙箱了，把网络也关掉不就行了。" 这个想法会遇到三个问题。

第一，网络策略不是二值的。模型可能需要访问包管理器、文档站、内部 API，也可能需要访问一个用户指定的地址。一个全局开关会把这些需求一起砍掉，最后只能被用户关掉。

第二，网络是一个**出口**而不是一个**路径**。文件围栏可以回答"这个 inode 在不在根下"，网络策略要回答"这个域名/端口/IP 段算不算允许"，粒度、解析时机、DNS 重绑定都不一样。把它塞进 `confine(argv)` 的返回值里，会让沙箱后端承担它没有的能力。

第三，也是最关键的：即使把网络关了，`write` 仍然可以往工作区写一个脚本，下一次会话再执行它。网络隔离和文件隔离解决的是不同的问题，不能互相替代。

所以 DSH 的选择是：`SandboxMode` 只承诺文件系统效果，网络留给部署层（容器网络策略、代理、防火墙）。这是一个"少承诺"的设计——它不会让你误以为关掉了网络，但也意味着你不能只靠 `SandboxMode` 完成数据外泄防护。

{{< fig "diagrams/03-approval-vs-sandbox.svg" "图注：审批与沙箱在时间轴上的先后关系；审批只覆盖单次调用，沙箱覆盖进程生命周期，两者之间没有隐式的权限传递。" >}}

## 7. `fs/write-intent` 与 `fs/edit-intent` 在哪

这两个事件经常被和沙箱混为一谈。它们回答的不是"能不能写这里"，而是"这次写是不是基于我观测到的版本"。

`fs/write-intent`、`fs/edit-intent`、`fs/delete-intent` 都是**单槽决策 waterfall**：工具在变更前分发，带一个默认 thunk（返回 `undefined` 表示"交给裸 provider"）；监听方可以完全决策而不调 `next()`。槽位按注册顺序先到先得，由一个策略插件占据是部署约定，不是强制不变式。

`fs-observation-policy` 监听这些意图，用 `WeakMap<owner, Map<targetKey, FsObservation>>` 记录"这个会话见过哪个版本"。写决策把"未见/缺失"映射到 `createIfAbsent`，把"已存在"映射到 `replaceIfVersion`；编辑/删除把"未见"映射到 `FS_NOT_OBSERVED`，把"缺失"映射到 `FS_NOT_FOUND`，把"存在"映射到版本守卫 (`FS_STALE_VERSION`)。

顺序因此是三层，而不是一层：

1. **意图层**（tool body 内）：决定"这次变更是否需要先观测/是否允许创建"。
2. **provider 围栏**（提交时）：决定"目标在不在允许的根下"。
3. **OS 沙箱**（仅进程）：决定"这个进程能不能碰这个路径"。

三个意图事件的默认决策也值得对照：

| 事件 | 谁分发 | 默认 | 策略插件通常问什么 |
|---|---|---|---|
| `fs/write-intent` | `writeText` / `moveFile` 目标 | 裸 provider | 目标存在吗？版本是我见过的吗？ |
| `fs/edit-intent` | `editText` / `patchText` / `moveFile` 源 | 裸 provider | 我观测过这个文件吗？还是只在磁盘上存在？ |
| `fs/delete-intent` | `deleteFile` | 裸 provider | 缺失算成功还是失败？ |

"默认是裸 provider"意味着：没有策略插件时，这些意图不改变行为。这是刻意的——意图事件是给策略留的缝，不是强制的版本控制。装了 `fs-observation-policy` 之后，未见过的编辑会变成 `FS_NOT_OBSERVED`，这是策略的选择，不是 FS 服务的固有语义。

`fs/observed` 是成功之后的即发即弃记录事件，普通 `ctx.emit`，监听方必须同步、只能做副作用。工具不捕获它的异常——观察失败不应该让一次成功的写入变成失败。

## 8. 边界矩阵：谁挡谁

把常见攻击面铺开，能看清每层的位置。

| 攻击 | schema | 注册表过滤 | 审批 | OS 沙箱 | FS 围栏 |
|---|---|---|---|---|---|
| 模型误传参数 | 提示 + 校验 | — | — | — | — |
| 调用不该存在的工具 | — | 不可见 + 拒绝 | — | — | — |
| 读工作区外文件 | — | — | — | — | **不挡** |
| `write` 越界 | — | — | — | **无效**（无进程） | 挡 |
| `bash` 越界写 | — | — | — | 挡 | — |
| `bash` 读任意文件 | — | — | — | **不挡** | — |
| `bash` 联网外传 | — | — | — | **不挡**（FS 策略） | — |
| 未经同意执行危险命令 | — | — | 挡一次 | — | — |
| 绕过工具直接调 FS 服务 | — | — | — | — | 挡 |
| 用户点错"允许" | — | — | **不挡** | 挡后续越界 | — |

这张表最重要的两行是"读"和"网络"。它们提醒你：沙箱这个名字容易让人以为"关起来了"，实际它关的是"写"和"进程行为"，不是"信息流动"。

## 8.5 案例：一次升级请求的完整轨迹

把一条真实路径走完，能看清每层在哪里交接。假设会话模式是 `workspace-write`，模型要执行 `npm install`，它判断需要写 `~/.npm`，于是请求 `danger-full-access`。

1. 模型看到 `bash` 的 schema 里 `sandbox_permissions` 的 enum 是 `['workspace-write', 'danger-full-access']`，`justification` 的说明是"一句话解释为什么这条命令需要更宽的模式"。
2. 模型输出 `{ command: 'npm install', sandbox_permissions: 'danger-full-access', justification: 'npm needs to write its global cache' }`。
3. `validateBashArgs` 检查两个参数成对、justification 非空；`resolveSandboxPolicy(exec)` 得到当前模式 `workspace-write`。
4. `approveBashEscalation` 调 `approveEscalation`：`danger-full-access` 严格宽于 `workspace-write`，继续。
5. `ctx.approval.request({ agent, toolName:'bash', callId, reason:'escalate sandbox to danger-full-access: ...', displayReason, signal })`。
6. 用户看到的是"这条命令请求更宽的文件权限"，以及模型给的理由；审批请求里没有命令参数，UI 用 `callId` 关联已经流式显示的那条调用。
7. 用户点"允许一次"，得到 `allowed-once`。审批服务写两条日志事件，模型看不到。
8. `tool-bash` 把 `policy` 复制成 `{...standingPolicy, mode:'danger-full-access'}`，只对本次调用有效，不写回会话状态。
9. `ctx.shell.execute()` 进 `bash-sandbox`，看到 `danger-full-access`，直接 spawn 原始 argv，不调用 `confine`，结果标注 `{ mode:'danger-full-access', denied:false }`。
10. 下一条 `bash` 调用重新 `resolveSandboxPolicy`，拿到的仍是 `workspace-write`；如果它也要写 `~/.npm`，需要再申请一次。

这条轨迹里有两个容易被产品化需求破坏的点。第一，第 8 步的模式是**调用副本**，不是会话状态；任何"把批准的模式写回 session"的实现都会把一次性授权变成永久提权。第二，第 10 步必须重新解析，不能复用上一条调用的 policy；否则一次批准会静默地覆盖后续所有调用。

## 9. 自己搭 Agent 时该怎么做

> **自己搭 Agent 时该怎么做**：把"能力可见性"和"能力授权"分成两个独立决定。可见性回答"模型知不知道有这个工具"，授权回答"这一次能不能执行"。DSH 用注册表过滤做前者、用 pre-execute + approval 做后者。混在一起，你会得到"工具可见但永远失败"或者"工具不可见但某个路径仍能调到"的错位。

> **自己搭 Agent 时该怎么做**：任何路径检查都放在"真正做 IO 的那个对象"里，并且让检查返回规范化后的目标，后续操作只用这个返回值。不要在工具层写 `startsWith`，也不要在检查之后再用原始字符串拼路径。检查和使用必须是同一个身份。

> **自己搭 Agent 时该怎么做**：审批的返回值只做 `allowed-once`，不要做 `allowed-for-session`。如果产品需要"记住选择"，把它建模成一次显式的策略变更（改会话模式、改配置），让它在日志里留下一条独立的事件。把"记住"塞进审批结果，会让审计再也说不清"这一次到底被授权了什么"。

> **自己搭 Agent 时该怎么做**：把 `enforcement: full | partial` 暴露到调用方，并在 partial 时决定"拒绝执行"还是"执行但标注"。不要默认 partial 等于 full；Windows ACL 和旧内核的差异是真实存在的，静默降级比直接失败更危险。

## 设计取舍

1. **四层而不是一层。** 每层只解决一类问题，组合起来才完整。代价是排障时要判断"这次失败是哪一层"，收益是任何一层失效都不会直接变成"任意读写"。DSH 把这四层放在不同的包里，正是为了不让某一层偷偷替另一层做决定。

2. **FS 围栏放在 provider，不放在工具。** 这样所有走 `FileSystem` 服务的调用都被覆盖，包括未来的新工具。代价是 provider 必须知道 `sandboxPolicy`，并且要处理 `danger-full-access` 的"不围栏"分支；收益是不存在"某个工具忘了检查"的旁路。

3. **读不设围栏。** 这是一个明确的产品选择：读的系统文件通常不构成破坏，而限制读会让大量任务无法完成。代价是"沙箱"不能防信息外泄；要防外泄，得在工具层或网络层另做限制。

4. **升级只发生在 bash，且必须严格更宽。** 代价是 FS 拒绝没有"重试一次"的杠杆；收益是升级路径只有一条，审批界面容易解释，模型也不容易把"申请更宽"当成万能重试。

5. **审批不带参数。** 参数已经在流式呈现里给用户看过，审批请求只带 `callId` 和理由。代价是审批服务必须能关联到那次调用；收益是避免"审批界面显示 A、实际执行 B"的参数分叉。

6. **模式由部署默认值决定，而不是由工具自己声明。** 一个工具不能要求"我必须在 `danger-full-access` 下运行"；它只能声明自己需要写、需要执行，然后由调用方按当前策略决定给不给。代价是某些工具在 `read-only` 部署里根本不可用；收益是权限的来源只有一个，不会出现"某个插件偷偷把全局模式调宽"。

7. **拒绝文案带模式名，而不是统一的 permission denied。** `[sandbox: file access denied under workspace-write mode]` 比 "permission denied" 长，但它告诉模型和用户"哪一层拒绝了、当前是什么模式"，从而避免模型换一种写法重试。代价是文案泄漏了部署配置；收益是失败可诊断。

## 踩坑点

1. **以为 `workspace-write` 会限制读。** 现象：`read /etc/passwd` 成功。原因：fs-sandbox 只围栏五个变更操作，读全部放行。对策：不要把沙箱当信息隔离；需要限制读就另做路径白名单。

2. **以为 `danger-full-access` 是"宽一点的沙箱"。** 现象：拿不到 `enforcement` / `denialSignatures`，denial 分类永远不触发。原因：这个模式根本不调用 `confine`，消费方直接 spawn。对策：在消费方按模式分支，不要假设 `confine` 一定被调用。

3. **把 runner failure 当 denial。** 现象：模型被告知"沙箱不让"，于是申请升级，升级后仍然失败。原因：spawn 失败和策略拒绝是两类错误。对策：先查 `runnerFailureRules`，再查 `denialSignatures`；runner failure 应该报"执行环境不可用"。

4. **在工具层检查路径后继续用原始字符串。** 现象：检查通过，写入却落到别处；或者 review 直接打回。原因：检查与使用不是同一个身份，中间可能被 symlink 换掉。对策：让检查函数返回规范化 target，后续只用它。

5. **给 `write` 加 `sandbox_permissions`。** 现象：schema 里加了参数，execute 里也处理了，但审批服务和策略都没准备好。原因：升级语义是 bash executor 的一部分，不是通用工具协议。对策：FS 的放宽走会话模式变更，不要复制一套升级参数。

6. **审批通过后复用决定。** 现象：同一轮里第二次调用没有弹窗，或者弹窗被跳过。原因：`allowed-once` 只对当前 `callId` 有效，代码里若缓存了 `true` 就破坏了契约。对策：把审批结果当成一次性 token，不落任何跨调用状态。

7. **`ApprovalPolicy = 'never'` 被当成"默认拒绝但可覆盖"。** 现象：prepend 的应答者仍然拿不到请求。原因：`never` 在 waterfall 之前就返回 `rejected`。对策：把 `never` 当部署硬开关，不要在插件里试图绕过。

8. **partial enforcement 静默通过。** 现象：Windows 上某些越界写成功了，日志里只有 `partial`。原因：上层没有检查 `enforcement`。对策：在消费方显式处理 partial，宁可拒绝执行，也不要把"部分隔离"当"完整隔离"。

## 源码索引

| 位置 | 证明什么 |
|---|---|
| `packages/sandbox/sandbox/src/index.ts` 的 `WIDER_MODES` | 升级目标在**执行时**检查，不烘焙进 schema |
| `packages/sandbox/sandbox/src/escalation.ts` 的 `validateEscalationArgs` | 两个升级参数必须成对，justification 不能为空 |
| `packages/sandbox/sandbox/src/escalation.ts` 的 `approveEscalation` | 平级不弹窗、非严格更宽直接抛、缺审批服务即拒绝 |
| `packages/sandbox/sandbox/src/index.ts` 的 `sandboxDenialMarker` | 模型看到的拒绝标记带当前模式，提示"不要换一种方式重试" |
| `docs/subsystems/sandbox.zh.md` | 三种模式的语义、`full/partial`、denial 与 runner failure 的区分 |
| `packages/fs/fs-sandbox/src/index.ts` 模块注释 | 围栏是 trusted code 的策略检查，不是内核边界；TOCTOU 被接受 |
| `packages/fs/fs-sandbox/src/containment.ts` 的 `isPathUnder` | 词法快路径 + dev/ino 身份比较，覆盖 8.3 别名与大小写 |
| `packages/fs/fs-sandbox/src/index.ts` 的 `checkedTarget` | `workspace-write` 重新 realpath 并返回 fresh target |
| `docs/subsystems/filesystem.zh.md` 的意图表 | `fs/write-intent` 等是单槽决策 waterfall，不是沙箱 |
| `docs/subsystems/filesystem.zh.md` 的观测段 | `WeakMap<owner, Map<targetKey, FsObservation>>` 与三种版本决策 |
| `packages/shell/tool-bash/src/index.ts` 的 `execute` | 先解析策略、校验参数、审批升级，再 `ctx.shell.execute` |
| `packages/shell/bash-sandbox/src/index.ts` 的 `execute` | `danger-full-access` 直接 spawn；其余先 confine 再分类 denial |
| `packages/interaction/user-approval/src/*` | `allowed-once` 的闭合集合与 `ask/never` 策略 |
| `docs/subsystems/approval.zh.md` | 审批只写日志、不进模型 transcript；缺通道即拒绝 |
| `packages/sandbox/sandbox/src/index.ts` 的 `ESCALATION_TARGETS` | schema 宣传的是升级目标，不是当前模式 |
| `packages/core/tools/src/pipeline.ts` 的 `serviceAsk` | 审批结果如何映射成 allow/deny 与 `approvalCancelled` |

## 最后

把边界想成四层之后，"这算不算沙箱失效"就不再是正确的问题。正确的问题是：**这条路径经过了哪几层，哪一层本该拦住它，为什么没有。**

`read /etc/passwd` 成功不是沙箱失效，是因为读从来不在围栏范围内；用户被重复弹窗不是审批坏了，是因为授权只对一次调用有效；reviewer 说你的路径检查不算数，是因为检查的位置和使用的对象不是同一个。

下一篇讲结果：一次工具调用结束之后，原始值如何变成模型看到的文本、UI 看到的卡片、日志里的一条记录，以及这三者为什么必须分开。

在那之前，可以先记住这一篇的判据：当一次调用越界时，先问"它经过哪几层"，而不是问"沙箱为什么没拦住"。层与层之间的缝隙，比任何单层的强度都更值得检查。

上一篇：[10 工具执行流水线：九段与三闸](/posts/dsh-tool-pipeline.html)｜下一篇：[12 工具结果的一生：截断、落盘、配对与呈现](/posts/dsh-tool-results.html)
