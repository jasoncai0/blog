---
title: "执行环境：四个 seam、一条 bash 链路与三种截断"
date: "2026-11-13T11:00:00+08:00"
summary: "ctx.fs、ctx.shell、ctx.terminals、ctx.sandbox 四个 seam 如何分工，一次 bash 调用从工具参数走到 subprocess 的九步，沙箱决策树与批准流程，以及输出在什么条件下落盘、模型看到的是指针还是内容。"
description: "DSH 执行环境解剖：文件、shell、终端、沙箱四个 seam 与 bash 调用全链路。"
tags: ["harness", "architecture", "agent"]
slug: "dsh-execution-environment"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准。
> 第二季第 3 篇。上一篇：[《模型层：适配器、路由与那条不能重放的流》](/posts/dsh-model-layer.html)；本季导览：[《第二季：把主干两侧补齐》](/posts/dsh-season-two.html)。

跑一个测试，输出几百行。模型看到的是最后几十行加一句"完整输出在 /tmp/…"，还是前五十行加后五十行？这两个选择差别很大：前者能直接读到失败断言，后者能读到开头编译错误和结尾失败摘要，中间的依赖安装日志被丢掉。

这个问题没有唯一答案，但它必须由某一层明确决定。DSH 里决定它的是 spill 策略，而策略能成立，是因为更下面两层已经分好了工：subprocess 负责"完整输出存在哪"，shell 负责"进程怎么起、什么时候算失败"，sandbox 负责"这个进程被允许动什么"。

这篇沿着一次 bash 调用往下走，把四个 seam 的边界和它们之间的缝隙都摊开。

## 0. 结论

- 执行环境有四个模型可见的 seam：`ctx.fs`（文件）、`ctx.shell`（一次性命令）、`ctx.terminals`（长驻 PTY 会话）、`ctx.sandbox`（约束提供方）。配套的还有 `ctx.subprocess`、`ctx.sandboxPolicy`、`ctx.spillStore`、`ctx.approval`、`ctx.shellEnv`、`ctx.workingDirectory`、`ctx.jobs`。
- fs 与 shell 分开，因为两者的约束对象不同：fs 约束"路径与版本"，每次写都带目标身份和期望版本；shell 约束"运行环境"，起进程之前要把 argv 包进沙箱。合成一个 seam，文件工具就得自己拼命令行。
- 一次 bash 调用有九步，其中四步在起进程之前：参数校验、策略解析、可选的批准、策略合成。批准只发生在模型主动请求更宽权限时，且只能逐级放宽。
- 沙箱只约束文件效果。网络与进程可见性不在它的范围内，`danger-full-access` 直接不经过 `ctx.sandbox`。
- 输出有两条存储：进程内环形缓冲只留尾部，落盘文件保留完整流。落盘上限是 64 MiB，内存上限默认 64 KB 每条流。
- 模型看到的通常是内容；超出预算时看到头尾摘录加一个不透明定位符，全文永远不进上下文。
- 远端沙箱是整套提供方替换：fs、subprocess、sandbox、terminal 一起走 SSH，执行坐标全部落到远端。

## 1. 四个 seam 与它们背后的提供方

四个 seam 的接口定义各自独立，靠 `ctx` 组合，没有一个总入口。

`ctx.fs` 是 `FileSystem` 抽象类（`packages/fs/fs/src/index.ts`）。它管 16 个抽象方法，分四组：解析（`resolve`、`processPath`、`processPathFromHostPath`、`fileUrl`、`contains`）、读（`stat`、`lstat`、`readText`、`streamText`、`readBytes`、`readByteRange`、`listDir`、`watch`）、写（`writeText`、`editText`、`patchText`、`deleteFile`、`moveFile`）、以及 `targetKey` 这种不透明身份的生成。写方法的签名里都带 `expected?: { version: FsVersion }` 与 `sandboxPolicy?`，两个参数分别解决并发覆盖与路径越权。

`ctx.shell` 是 `ShellExecutor`（`packages/shell/shell/src/index.ts:64`）。它只有两个动作：`resolve(request) → ShellExecSpec` 和 `execute(spec) → Promise<ShellExecution>`；前者按实现补默认值，后者返回一个句柄，前台用 `.result()` 等结果，后台直接读同一句柄的增量输出。拆成 resolve 与 execute，是为了让工具层能在起进程之前把"这次要跑什么、在哪个目录、什么沙箱策略"变成一份完整、可检查的 spec。

`ctx.terminals` 是 `TerminalSessionService`（`packages/terminal/terminal/src/index.ts`）。它管的是长驻会话：`spawn`、`read`、`signal`、`kill`、`list`、`hasOwnerActivity`，所有权是具体的 `Agent` 实例。读操作带等待原因（`stdin_read`、`inferred_idle`、`timeout`、`session_exit`），因为 PTY 没有"读完"这个状态。

`ctx.sandbox` 是 `SandboxProvider`，只有一个核心方法 `confine(argv, policy, signal) → ConfinedArgv`。它不执行任何东西，只把裸 argv 包成带约束的 argv，并附上这次约束的元数据：`enforcement`（`full` 或 `partial`）、`denialSignatures`（后端自己的拒绝特征）、`runnerFailureRules`（runner 自己挂掉的识别规则）。

{{< fig "diagrams/01-capability-map.svg" "四个 seam 与它们的能力地图。左列是消费方，中列是 seam，右列是本地与远端提供方；底部一行是七个共用服务，它们不在模型可见面上，但每次调用都会碰到。" >}}

提供方的分层在包名上是可见的：

| seam | 定义包 | 本地提供方 | 远端提供方 | 消费方 |
| --- | --- | --- | --- | --- |
| `ctx.fs` | `fs/fs` | `fs/fs-local` | `ssh/fs-ssh` | `tool-fs`、LSP、附件 |
| `ctx.shell` | `shell/shell` | `shell/bash-local` | 经 `subprocess-ssh` 间接 | `tool-bash`、hooks |
| `ctx.terminals` | `terminal/terminal` | `terminal/terminal-bash` | `subprocess-ssh` 的终端 | `tool-terminal` |
| `ctx.sandbox` | `sandbox/sandbox` | `sandbox/sandbox-local` | `ssh/sandbox-ssh` | `bash-local`、`fs-sandbox` |

`ctx.subprocess` 不在模型可见面上，但它是三家共用的地基。它定义 `resolveExecutable`、`spawn`、`spawnTerminal`、`terminalEnvironment` 四个方法，`bash-local`、`fs-local` 的某些路径、终端后端都从它起进程。它也是唯一知道"进程组、PTY、TERM→KILL 升级"这一套细节的地方。

`ctx.spillStore` 是内容溢出的出口，`ctx.approval` 是人工批准的通道，`ctx.shellEnv` 是一个注册表（`register`、`collect`、`list`），插件往里面注册受管事实，执行时一次性收集成快照；进程自带的同名变量先被清掉，快照最后合并，所以宿主 shell 的残留不会盖掉当前事实。

`ctx.shellEnv` 管 `DSH_*` 环境变量的注入，`ctx.workingDirectory` 保证会话工作目录存在，`ctx.jobs` 管后台任务。这些都不直接面向模型，但每一次 bash 调用都会碰到其中至少四个。

### 自己搭的时候

给文件与命令各留一个 seam，别做一个 `ctx.env.exec(cmd)` 兼管两者。一旦合起来，文件工具要调用 shell 才能写文件，路径转义、引号、Windows 盘符三件事会同时压在一个字符串上；沙箱策略也只能在进程边界生效，写文件时反而没有逐路径裁决的机会。分开之后，文件工具的约束是"目标 + 版本 + 策略"，命令工具的约束是"argv + 工作目录 + 策略"，两个模型各自简单。

## 2. 一次 bash 调用：九步

`tool-bash` 的 `execute()` 在 `packages/shell/tool-bash/src/index.ts:478-531`。函数体没有包装层，顺序就是执行顺序，每一步失败时的语义都不一样。

先看它接受什么。模型能填七个字段，其中三个带条件：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `command` | 是 | 交给 `bash -c` 的完整命令，非空 |
| `description` | 是 | 卡片标题，非空；它同时是给人看的摘要 |
| `timeoutMs` | 否 | 正有限数；实际值会被实现夹到配置的上下限之间 |
| `workdir` | 否 | 覆盖工作目录；工具说明要求用它替代 `cd` |
| `run_in_background` | 否 | 只在部署开启了后台执行且注册表存在时可用 |
| `sandbox_permissions` | 否 | 只在挂载了会约束的 executor 时出现在 schema 里 |
| `justification` | 否 | 提权理由，与上一项配对 |

第一步，读标准策略。`resolveSandboxPolicy(exec)`（同文件 214-215）只有一行：

```ts
return this.ctx.get('sandboxPolicy')?.resolve({ session: exec.agent.session })
```

`resolve()` 的优先级是：本次显式批准的模式 > 会话最后一条 `sandbox/mode` 事件 > 部署默认。它读的是会话，不是进程全局变量，所以两个会话可以并行跑在不同模式下，而 `danger-full-access` 也不会因为某个进程设了环境变量就泄漏给另一个会话。

第二步，校验。`validateBashArgs(args, standingPolicy?.mode)` 检查命令与描述非空、`timeoutMs` 为正有限数、提权字段配套。有一处细节：如果 `sandbox_permissions` 恰好等于当前模式，它直接返回，因为这次提权是无操作；只有真的要放宽时才要求理由。

第三步，按需批准。代码里的条件是两个字段都在：

```ts
const approvedMode = args.sandbox_permissions !== undefined && args.justification !== undefined
  ? await approveBashEscalation(args.sandbox_permissions, args.justification, exec, standingPolicy)
  : undefined
```

`approveBashEscalation()` 把请求转给 `approveEscalation()`（`packages/sandbox/sandbox/src/escalation.ts:172-200`），后者先做三项检查：请求模式是否严格更宽（`WIDER_MODES[effectiveMode].includes(mode)`，否则报 `sandbox escalation to "..." is not strictly wider than this call's current "..." mode`）、有没有批准服务（否则 `... requires approval, but no approval service is composed`）、这次调用有没有 agent 可以路由（否则 `... but the call has no agent to route it through`）。三项都过，才向用户发起一次 `approval.request`，理由是 `escalate sandbox to <mode>: <justification>`。

第四步，合成策略。批准的模式覆盖到本次请求的副本上，原策略对象不动。用户拒绝时工具不抛异常，而是把拒绝原因和当前模式交给模型，模型可以换一条不越权的路径。

第五步，准备目录。`ctx.workingDirectory.ensure(agent, signal)` 保证会话工作目录存在（会话恢复后目录可能已被删），`resolveWorkdir(args.workdir, cwd)` 让模型传入的相对目录落到会话目录下。工具说明里那句 `pass workdir instead of using cd` 就是这一层的对外承诺：每次调用都是新 shell，`cd` 不会留下状态。

第六步，收集环境。`ctx.shellEnv.collect(exec)` 返回一份 `DSH_*` 快照。合并顺序写在请求类型上：先清掉进程自带的 `DSH_*`，再合并调用方的普通 `env`，最后合并快照。这样宿主的 shell 不能把上一层的 `DSH_SESSION_ID` 带进来，插件也不能用同名变量顶掉受管事实。

第七步，组请求并解析。拼出来的 `ShellExecRequest` 只带非空字段，然后 `ctx.shell.resolve()` 补默认值：

```ts
const stdoutMaxBytes = request.stdoutMaxBytes ?? this.config.maxOutputBytes.get()
return {
  command: request.command,
  workdir: request.workdir ?? this.config.cwd.get() ?? process.cwd(),
  timeoutMs,
  onExpiry: request.onExpiry ?? 'kill',
  stdoutMaxBytes,
  ...
}
```

`timeoutMs` 在这里被 `clampTimeout` 夹过，所以模型写 `timeoutMs: 1e9` 不会真的等十一天。前台分支传 `signal`，后台分支传 `onExpiry: 'none'`，两个分支的 spec 都从这一处产出。

第八步，执行。三条分支：后台（要求 `enableRunInBackground` 与 job 注册表，缺一个就抛专门的消息）、前台挂到 job 上（`promoteOnTimeout` 为真时，命令一开始就注册成 job，超时后继续存活而不是被杀）、普通前台（`execute(spec)` 然后 `await foreground.result()`）。起进程的是 `bash-sandbox` 的 `execute()` 覆盖（`packages/shell/bash-sandbox/src/index.ts:88-135`）：

```ts
private confine(command: string, policy: SandboxPolicy, signal?: AbortSignal): Promise<ConfinedArgv> {
  return this.ctx.sandbox.confine(['bash', '-c', command], policy, signal)
}
```

`danger-full-access` 在这里直接短路，调父类的 `execute`，`ctx.sandbox` 一次都不碰。另外两个模式把 argv 包好再 spawn，进程起来之后把这次约束的元数据记在句柄上，等结果出来再判别。

第九步，收敛。`foreground.result()` 给出 `ShellRunResult`，其中 `exitCode` 在被信号杀死或准备阶段超时的情况下是 `null`。`aborted` 为真时抛 `toolAborted()`（`HarnessError`，code 为 `TOOL_ABORTED`，`name` 被改成 `AbortError`），这次调用没有结果，只有取消。否则 `canonicalBashResult()` 复制出 `{cwd, exitCode, signal, timedOut, aborted, timeoutMs, stdout, stderr, sandbox}`，交给 `renderResult` 拼成模型可见文本，或交给 `presentResult` 拆成卡片。

{{< fig "diagrams/02-bash-nine-steps.svg" "九步的时间轴，右侧标出每一步由哪个 seam 执行。前七步全在进程之外；第八步之后命令已经跑过，副作用无法回滚。" >}}

这九步里的错误分三类，处理方式不同：

- 参数错：在第二步抛，模型重试就能修，不改环境。
- 策略错：在第三步或第四步拒绝，工具返回拒绝理由，模型改用别的路径。
- 执行错：第八步之后，进程真的跑了，退出码与输出都是事实。

第一季拆过工具结果的三段式（`content`、`isError`、`render`），这里对应的是第三类：进程退出码非零属于"命令跑了但失败"，工具不该把它包成异常。相关细节见 [《工具结果的两种形态》](/posts/dsh-tool-results.html)。

有一段代码值得单独看。发布前的 `bash` 与 `pwsh` 共用同一套退出码渲染：`renderResult` 在正文末尾追加 `[exit code: N]` 或 `[killed by signal: X]`，`presentResult` 再用 `parseExitStatus` 把它解析回来，拆成卡片上的 exit pill。渲染与解析是一对互逆函数，改了一边必须改另一边。

### 自己搭的时候

把"解析 spec"和"执行 spec"分成两个调用点，而不是一个 `run(command)`。分开之后，后台、前台、超时晋升三条路径共享同一份 spec，日志里也只需要记一次"这次要跑什么"。DSH 里 `resolve()` 是同步的，`execute()` 是异步的，这个签名差异就是在提示调用方：前者不该有 I/O，后者才动手。

另一条：把 `onExpiry` 做成显式字段。前台默认 `kill`，后台必须 `none`。如果把它藏在实现里按"有没有 job id"推断，前台超时后命令会被杀掉，而调用方以为它还在后台跑。

## 3. 沙箱：只约束文件效果

`SandboxMode` 只有三个值：`read-only`、`workspace-write`、`danger-full-access`。文档里写得很明确：模式只治理文件效果，网络访问与进程可见性不在范围内。

这条限制决定了实现形态。`danger-full-access` 根本不经过 `ctx.sandbox`，消费方直接 `subprocess.spawn`；只有前两个模式会调 `confine()`。所以 `ctx.sandbox` 的抽象里没有"允许网络"这种字段，也就不存在"设了沙箱但仍然能 curl"的矛盾承诺。

`confine()` 返回的 `ConfinedArgv` 带三样东西：

- `argv`：包装后的命令（bwrap、sandbox-exec、Landlock 启动器等）。
- `enforcement`：`full` 或 `partial`。partial 表示后端只能覆盖部分效果，比如只读挂载做了、但某些系统调用没拦。
- `denialSignatures` 与 `runnerFailureRules`：判断失败原因用的特征串。

拒绝特征是按后端写的（bwrap 只读绑定下是 `EROFS`，Landlock 下是 `EACCES`，Seatbelt 下是 `EPERM`），文档特意说明它不是跨后端并集。消费方必须先用 `runnerFailureRules` 判断"runner 自己没起来"，再判断"命令被拒绝"。顺序反了会把沙箱不可用误报成权限拒绝，模型于是去申请提权，而根因是 runner 没装。

`enforcement` 是给部署方的诚实标记。`full` 表示后端能覆盖该模式承诺的全部文件效果，`partial` 表示只覆盖了一部分（比如只读挂载生效、但某些系统调用没拦）。消费方不能用它做安全判断，它的用途是展示与诊断：同一个 `workspace-write`，在两个后端上的实际强度可能不同，UI 上应该能看出来。

没有可用后端时抛 `SandboxUnavailableError`，code 是 `SANDBOX_UNAVAILABLE`。后台任务在前台受限而无法启动时也会走到这里（`SANDBOX_UNAVAILABLE` 有专门的包装）。文档里有一句硬话：静默降级成不受约束的执行，任何情况下都不合法。

{{< fig "diagrams/03-sandbox-decision-tree.svg" "策略 → 批准 → confine 的五个阶段。上面一行是主干，三种模式里只有两种会进入 confine；下方三个卡片是执行之后的判别顺序：runner 失败、命令被拒、正常返回。" >}}

### 自己搭的时候

把"沙箱不可用"和"命令被沙箱拒绝"分成两个错误码，并且在 UI 上给出不同文案。这两个状态在日志里长得很像（都是进程非零退出、stderr 有一段英文），但用户动作完全相反：前者要装后端或降级，后者要改命令或申请提权。混成一个码，帮助文档只能写成"如果失败请检查沙箱"。

另一条：批准要绑定到具体调用。DSH 的批准带 `callId`，一次性有效。如果批准只绑工具名，模型可以拿一次批准跑任意命令；如果批准绑会话，用户点过一次"允许"之后，后面每个命令都在更宽的模式里跑。

## 4. 输出环：内存留尾，落盘留全

`bash-local` 的默认值是两条：`maxOutputBytes` 64_000（每条流），`maxSpillBytes` 64 MiB（每个溢出文件）。这两个数字决定了模型能看到什么。

起进程时，`bash-local` 给 stdout / stderr 各配一个 `SubprocessCollect`：

```ts
private collect(maxBytes: number): SubprocessCollect {
  return { maxBytes, spill: { maxBytes: this.config.maxSpillBytes.get() } }
}
```

`maxBytes` 是内存上限，`spill.maxBytes` 是落盘上限。`subprocess` 的实现按流分别处理：内存里保留尾部（超出时丢头部，标记 `lossy`），落盘文件从第一个字节开始完整写，直到自己的上限。所以"模型看到的"和"盘上有的"是两个不同的切片，前者是后者的后缀。

为什么内存留尾而不是留头。命令的失败信息通常在末尾：断言、退出摘要、编译错误计数。留头会让模型看到一堆进度日志却不知道结果。为什么落盘留全。模型可以自己 `read` 那个文件，读窗口从任意偏移开始，头部信息没有丢，只是没有自动进上下文。

`readFrom(fromByte)` 是偏移读，不消费。这一点对后台任务很关键：同一个 job 可以被轮询多次，每次拿到"从上次偏移到现在"的增量，而偏移由调用方保存。如果读是消费式的，第二次轮询就什么都拿不到了。

结果进入模型上下文之前，还有一层预算。`tool-bash` 的 `presentResult` 会把 `[exit code: N]` 从正文里剥出来做成卡片上的 exit pill；`renderResult` 则把它追加回正文末尾。这个标记是由 `renderResult` 产生的，`parseExitStatus` 再把它反解回来，两个工具（bash 与 pwsh）共用同一套解析。

### 三条截断路径

把链路上所有"内容变短"的地方列出来，只有三处，各自解决不同的问题。

第一条，进程输出环。上限是 `maxBytes`（默认 64 KB 每流），超出丢头留尾，同时完整流写进溢出文件（上限 64 MiB）。这一层解决"输出可能无限长"，代价是头部的信息不在内存里。

第二条，工具结果预算。`dsh-spill-policy` 在结果进入上下文之前跑一遍，超过 `maxInlineTokens` 就调 `ctx.spillStore.saveText()` 存全文，然后把结果替换成三块：保留的头部、`GAP` 标记、保留的尾部，最后附一行定位提示。图片按 route 的 `imageRequestPricing` 折算 token，offloaded 的图按占位文本计。

第三条，模型回读。模型看到定位符后，用 `read` 工具按窗口读文件。`fs` 的 `readByteRange` 与 `streamText` 支持从偏移读，`readBytes` 带 `maxBytes` 上限，超了抛 `FS_TOO_LARGE` 而不是静默截断。这一层解决"上下文装不下但模型需要细节"。

{{< fig "diagrams/04-spill-paths.svg" "三条截断路径与上下文边界。第一条在进程与内存之间，第二条在工具结果进上下文之前，第三条由模型按偏移回读磁盘；每条都有独立的开关与失败降级。" >}}

第二条路径是 best-effort 的。`spill-policy` 的 `catch` 只打一行 `spill-policy: <err>; keeping the inline content`，然后保留原结果。理由很实在：工具调用已经成功了，因为"存不下"把它翻成 `isError`，模型会以为命令失败并重试一次，副作用可能跑两遍。

定位符是不透明的。`SpillLocator` 是 branded 类型，本地后端把它渲染成路径，远端后端可以渲染成 URI 或对象键。消费方不许解析它，只许原样展示。这条规则让"把工作区搬到 SSH 主机"不需要改任何上层代码。

### 自己搭的时候

把"截断"和"失败"分开建模。截断是一个正常结果，带 `truncated: true` 和一个可选的定位符；失败是 `isError`。很多实现把超出预算当成错误返回，模型看到的是"命令执行失败"，于是去调小输出、加重定向，最后仍然失败。

另一条：给回读留一个稳定入口。定位符如果是一次性的临时链接，模型下一步读的时候可能已经过期；如果路径依赖进程内的随机根目录，重启后就读不到。DSH 的本地后端把文件放进按会话哈希分区的目录，权限 0700，文件用 `wx` 打开，既有稳定位置，也不会覆盖已有文件。

## 5. 文件：目标、版本与原子发布

文件 seam 的写法和使用方式，都围绕一个东西：`FsTarget`。

```ts
export interface FsTarget {
  targetKey: FsTargetKey
  displayPath: string
}
```

`targetKey` 是 branded 类型，语义是"同一个文件永远得到同一个身份"，消费方不许解析它。`displayPath` 只用于展示与日志。这个拆分的直接好处是远端后端：路径在 SSH 主机上是 `/srv/app/x.ts`，在本地可能根本不存在，但目标身份仍然可以稳定地穿过 seam。`resolve()` 是异步的，因为远端后端需要一次往返才能把路径映射成身份。

版本是另一个核心概念。`stat()` 返回 `FsInfo`：`{version, type, size?}`。`version` 是不透明的新鲜度令牌，`type` 让调用方在读之前就能拒绝目录，`size` 决定用 `readText` 还是 `streamText`。写入分两种意图：

```ts
export type FsWriteIntent =
  | { kind: 'createIfAbsent' }
  | { kind: 'replaceIfVersion'; version: FsVersion }
```

`writeText` 接受 `expected?: FsWriteIntent`，返回 `FsWriteOutcome {operation, version, before, after}`。`editText` 与 `patchText` 接受 `expected?: {version}`，版本在匹配之前检查，内容已经变了就报 `FS_STALE_VERSION`，不会把新内容按旧偏移改掉。删除与移动也带同样的 guard。

这里有一个容易忽略的入口：`fs/write-intent` 是一个 waterfall（`packages/fs/fs/src/index.ts:66`），在写入之前让策略层决定这次写入应该用哪种意图。`fs-observation-policy` 就是挂在它上面的可选插件：要求模型先读再写。它在 `dsh-base` 里默认关闭，所以"读后写"是部署选择，不是硬约束。

原子性由后端保证：单文件发布要么完整生效，要么不生效。移动的实现允许保留源文件，文档里明确说了这一点，所以调用方不能假设移动之后源一定不存在。

约束发生在写路径上，不在工具层。`fs-sandbox` 围栏了五个方法（`writeText`、`editText`、`patchText`、`deleteFile`、`moveFile`），`read-only` 全拒，`workspace-write` 只放行会话工作区与平台临时根，`danger-full-access` 不设限。拒绝时抛 `FS_SANDBOX_DENIED`，消息形如 `[sandbox: file access denied under workspace-write mode]`。移动会先检查两端，任何一端越权都不产生副作用。

读路径有几个刻意的限制。`readBytes(target, signal, maxBytes)` 带硬上限，超了抛 `FS_TOO_LARGE`，不静默截断，因为截断的二进制没有意义。`readByteRange(target, {offset, length})` 才是分段读的入口。`listDir` 返回 `FsDirEntry[]`，按名字稳定排序且不带内容，避免为了列目录把整棵树的元数据都拉回来。`lstat` 是路径级探测，可以报告 `symlink`，供信任边界规则在解析之前拒绝仓库里的链接。

`watch()` 在基类里有默认实现：直接以 `FS_IO_ERROR` 拒绝，消息是 `Filesystem watching is not supported by this provider.`。需要监听的消费方必须自己判断后端是否支持，不能假设所有后端都能推变更。

### 自己搭的时候

用不透明版本令牌，别用 mtime。mtime 的精度在不同文件系统上不一致（有的只到秒），同一秒内两次写入会得到相同的时间戳，乐观锁因此失效。DSH 的 `FsVersion` 由后端生成，本地后端可以用 mtime 加大小实现，但只要不把它写进对外契约，换后端时就不用改调用方。

另一条：把目录与文件的判断放在读之前。`stat` 返回 `type` 就是为了这个。直接 `readText` 一个目录，错误信息通常是 EISDIR，模型看不懂，也不知道下一步该 `listDir`。

## 6. 终端与进程：长驻会话

`ctx.terminals` 管的是另一类执行：开一个会话、反复读写；一次性命令与等待结果属于 `ctx.shell`。

所有权是具体的 `Agent` 实例，不是会话 id，也不是全局注册表。`spawn(owner, request)` 返回会话 id、首屏输出与状态，之后每个操作都要带 owner，所有权不符直接拒绝。这条设计让多个 agent 并行开终端时不会互相读到对方的输出。

读的语义比文件复杂，因为 PTY 没有"结束"这个明确信号。`read()` 返回 `waitReason`，四种取值：

| waitReason | 含义 |
| --- | --- |
| `stdin_read` | 前台进程阻塞在读 stdin，通常是在等输入 |
| `inferred_idle` | 一段时间没有新输出，推断空闲 |
| `timeout` | 等待超时 |
| `session_exit` | 会话已退出 |

`sessionStatus` 单独给出，`{kind: 'running'}` 或 `{kind: 'exited', exitCode, signal}`。`exitCode` 为 `null` 表示被信号杀死。`truncated` 说明这次读有内容被丢弃，可能是本次操作超限，也可能是滚动缓冲已经滚掉。

发送用 `startSend()` 返回一个操作句柄，而不是直接返回结果，因为一次发送可能同时包含写入与等待输出。信号只允许五个：`SIGINT`、`SIGTERM`、`SIGKILL`、`SIGTSTP`、`SIGHUP`，与 `subprocess` 的终端信号成员一致，两个包各自声明但必须同步改。

后端的实现很薄。`terminal-bash` 注入 `['terminals', 'sandboxPolicy', 'subprocess', 'workingDirectory']`（`packages/terminal/terminal-bash/src/index.ts:27`），起 PTY 的是 `ctx.subprocess.spawnTerminal`（同文件 187-205）。终端自己不管进程，PTY 能力复用 subprocess：UTF-8 传输、前台进程组、`TERM` 升级到 `KILL`、整个会话退出前等待静默，这些都在 subprocess 层实现一次。

`subprocess` 的契约值得单独记三条。`resolveExecutable()` 只接受绝对路径或裸命令名，带路径分隔符的相对路径直接拒绝，裸命令名走一份清洗过的 PATH 查找。`spawn()` 同步返回句柄，seam 不施加任何默认值，`argv` 不会被 shell 解释，要 shell 语义就自己传 `['bash', '-c', cmd]`。`done` 只给出 `{exitCode, signal}`，不做超时或取消的分类，也不带输出，因为原因只有调用方知道。

### 自己搭的时候

把"等待原因"暴露给模型，别只返回一段文本。模型看到 `timeout` 会去调大超时或改用非交互命令；看到 `stdin_read` 会知道要发输入；看到 `session_exit` 会换新会话。只给文本，模型只能猜。

另一条：PTY 与管道别共用一条路径。管道读到的字节流没有回显、没有提示符、没有行缓冲差异，用来驱动交互式程序会一直等。DSH 让终端走 `spawnTerminal`，普通命令走 `spawn`，两者在 subprocess 里是不同实现。

## 7. 后台任务与作业环

后台分支不是"让命令继续跑"这么简单，它要把一个进程句柄接进通用的 job 注册表，让模型能用 `job_output` 反复取增量，也能在别的地方看到这个 job。

`tool-bash` 的 `background.ts` 承担适配：`processJob()` 把一个 `ShellProcess` 包成 job 描述，`processSources()` 把 `observed` 偏移暴露成拉取源，`ringDelta()` 把一次增量读渲染成文本，`processOutcome()` 把退出状态映射成 job 的终止原因。

终止原因的映射有一条规则值得抄：非零退出码映射成 `completed` 加 detail，不是 `failed`。理由和前台一样，命令跑了、失败了，这不是 job 系统故障。被杀的进程保留 `killed`，detail 里带信号。这条区分让"命令自己失败"和"job 被外部终止"在 UI 上不会混。

沙箱事实会额外拼两条标记。runner 自己挂掉时：

```
[sandbox: the sandbox runner itself failed under <mode> mode — the command did not run; this is a sandbox problem, not a command failure]
```

命令被策略拒绝时，先拼 `[sandbox: file access denied under <mode> mode]`，如果这次组合对外宣告了提权字段，再加一句：

```
[sandbox: escalation available — retry this exact command once with sandbox_permissions (the narrowest wider mode that suffices) + justification; the approval prompt asks the user]
```

这两条都放在"决定点"上，而不是工具描述里。模型被拒绝的同一轮就能看到该怎么做，不需要回忆起几十行前的工具说明。

前台挂到 job 上的分支还有一处细节：命令超过超时时间之后，工具停止等待，但进程继续跑，返回的内容是 `ringDelta` 渲染的环形缓冲增量，外加一个 job id 让模型稍后再取。这就是 `promoteOnTimeout` 的语义：不杀进程，把等待变成轮询。

### 自己搭的时候

把 job 的输出接口做成偏移读，别做成"取全部"。模型可能轮询十次，每次只要新内容；取全部的实现在第十次会把整个输出重新灌进上下文，token 花在重复内容上。DSH 的 `observed` 是一个字节偏移，读操作不消费，偏移由调用方保存。

另一条：job 的终止原因要和命令的退出码分开建模。一个 job 可能因为用户取消、进程被杀、注册表清理而结束，这些都不是"命令失败"。混在一起之后，统计"命令成功率"会把取消算进去。

## 8. 为什么 fs 和 shell 必须分开

最容易提出的合并方案是：只留 `ctx.shell`，文件读写用 `cat` 与 `printf`。这个方案在三个地方会坏。

路径转义。写文件要构造命令行字符串，路径里的空格、引号、换行、`$` 都要按目标 shell 转义。一旦转义漏一个字符，写文件就变成执行任意命令。文件 seam 里没有字符串拼接，`targetKey` 是不透明身份，`resolve` 之后直接按字节写。

逐路径裁决。沙箱模式是按根目录裁决的，`workspace-write` 允许会话工作区与临时目录。如果写文件走 shell，裁决发生在进程级别，命令里 `> /etc/hosts` 与 `> ./out.txt` 看起来一样；`fs-sandbox` 则在每次写之前检查目标，越权报 `FS_SANDBOX_DENIED`，移动文件时两端都检查。

版本与原子性。`writeText` 带 `expected.version`，版本不符就拒绝，这是并发编辑的基础；写入是单文件原子发布。shell 重定向做不到这一点，写到一半进程被杀会留下半个文件。第一季讲工具边界时提过这条分工，见 [《工具边界的划法》](/posts/dsh-tool-boundaries.html)。

反过来，文件 seam 也做不了 shell 的事。编译、测试、装依赖都需要起进程，需要环境变量、超时、信号、退出码，这些是 `ctx.shell` 的语义。两个 seam 的分界线是"有没有进程"。

有一个例外值得注意：`processPathFromHostPath()`。它把宿主路径变成 `FsTarget`，只在本地后端可用。`llm` 的 `fileReadPath()` 需要这个能力给模型提供"只读路径"，它通过 `ctx.get('fs')` 结构化取用，拿不到就退化成不带路径的句柄文本。这是唯一一处允许"能力可选"的地方，因为文件块本来就不是每家 provider 都支持。

## 9. 远端沙箱：一起搬走的能力

把执行搬到 SSH 主机，最容易低估的是"哪些东西必须一起搬"。只搬 subprocess 是不够的：如果 fs 还在本地，模型 read 的是本地文件、bash 改的是远端文件，两边看到的世界不一致。

DSH 的做法是整族替换。`fs-ssh` 提供 `ctx.fs`，`subprocess-ssh` 提供 `ctx.subprocess`（同时提供可执行文件查找、进程管理、控制通道与终端），`sandbox-ssh` 提供 `ctx.sandbox`，终端后端经由 `subprocess.spawnTerminal` 落到远端 PTY。替换之后，五个执行坐标一起指到远端：

- 文件身份（`FsTarget` 与 `targetKey`）
- 可执行文件查找（`resolveExecutable`，PATH 在远端）
- 进程工作目录
- 沙箱工作区根（`SandboxExecutionPolicy.workspaceRoot`）
- LSP 的文件 URL

这五个坐标之外还有一份能力清单，能看出"一起搬走"的边界：

| 能力 | 本地 | 远端 | 谁提供 |
| --- | --- | --- | --- |
| 文件读写 | 本机磁盘 | SSH 主机磁盘 | `fs-local` / `fs-ssh` |
| 进程启动 | 本机 | SSH 主机 | `subprocess-local` / `subprocess-ssh` |
| 沙箱约束 | bwrap / Seatbelt / Landlock | 远端同名后端 | `sandbox-local` / `sandbox-ssh` |
| 终端 PTY | 本机 PTY | 远端 PTY | `terminal-bash` 经 subprocess |
| 可执行文件查找 | 本机 PATH | 远端 PATH | subprocess 提供方 |

而 harness 本身、模型传输、会话存储留在本地。这个分界线的判据是"这个东西是否描述执行"，不是"这个东西是否碰文件"。

有两处能力在远端不可用，原因值得记下来。`processPathFromHostPath()` 被明确关掉，理由是"安装远端产物不意味着任意主机路径可移植"；依赖它的 `NodePtcRuntime` 改用一份经过校验的远端启动配置。另一处是 Web 工作区视图，它假设自己能读宿主文件系统，跨 SSH 需要单独集成。

连接管理还有两条硬规则。数据流走单独认证的转发 Unix socket，每条流一个 SSH channel window；管理 RPC 走 helper 的 exec 流。SSH 断开时，未确认的操作被标记为"结果未知"，客户端在重连后不会自动重放，因为命令可能已经执行过。这条规则来自第一季讲过的恢复原则，见 [《错误、重试与恢复》](/posts/dsh-error-retry-recovery.html)。

### 自己搭的时候

如果你在做一个"本地 + 容器 + 远端"三档的执行环境，把 seam 的替换做成运行时装配，别做编译期的条件分支。DSH 的每个提供方都是独立插件，靠 `cordis.yml` 组合；同一份 agent 代码在本地和 SSH 上跑，差别只在插件清单。编译期分支会让"哪些能力在远端缺失"变成散落各处的 if，最后没人说得清。

另一条：给"不可用的能力"一个显式的、类型上可见的出口，别返回空实现。`processPathFromHostPath()` 的签名是 `string | undefined`，远端不提供时调用方拿到 `undefined`，TS 会逼着他写另一条路。空实现（返回一个假路径）会把错误推迟到用户点开文件时才出现。

## 10. 设计取舍

- 四个 seam 而不是一个执行门面。收益是每类能力有自己的约束模型与测试面；代价是装配复杂，一个 bash 调用要碰七个服务。
- `resolve` 与 `execute` 分开。收益是起进程前能看到完整 spec，后台与前台共享同一份描述；代价是调用方要写两次。
- 沙箱只管文件效果。收益是承诺可验证，"允许网络"这种字段不会出现；代价是网络隔离要靠别的手段（容器、代理白名单），文档里必须说清。
- 输出留尾。收益是失败信息通常在最末；代价是编译错误的开头需要模型回读文件。
- 溢出文件保留完整流，上限 64 MiB。收益是回读可用；代价是磁盘会被一次 `yes` 写满到上限，需要清理策略。
- 定位符不透明。收益是本地与远端共用一套上层代码；代价是任何想"解析一下路径"的便利代码都被禁止。
- spill 失败保留原结果。收益是不会把成功调用翻成失败；代价是超长结果可能直接进上下文，撑爆一次请求。
- 约束做在写路径上，不做在工具层。收益是绕过工具的调用方（补丁、LSP、插件）同样受约束；代价是每个后端都要重新实现一遍围栏，`fs-local` 与 `fs-ssh` 的规则必须对齐。
- 批准要求"严格更宽"。收益是模型不能用同级模式换一条路径绕过拒绝；代价是同一个模式的重试必须先回到标准策略，不能借批准走捷径。
- 后端能力不隐藏。`watch()` 基类直接拒绝，`processPathFromHostPath()` 在远端抛错。收益是缺失能力在调用点暴露；代价是所有消费方都要写降级分支。

## 11. 踩坑点

- 把 `[exit code: N]` 当成普通文本处理。它是 `renderResult` 追加的标记，`presentResult` 会把它剥掉放进卡片；如果 UI 又渲染一次正文，用户会看到两遍退出码。
- 在 `execute()` 之前自己拼 `ShellExecRequest` 而不走 `resolve()`。后台分支拿不到 spec，`onExpiry` 会退回默认的 `kill`，长命令被超时杀掉，而调用方以为是命令自己失败。
- 只判断退出码，不判断 `timedOut` 与 `aborted`。两者互斥且都是"第一原因"，都可能导致非零退出；把它们当普通失败会误导模型去改命令。
- 在提权被拒时抛异常。DSH 返回拒绝理由与当前模式，模型可以换路径；抛异常会让这次工具调用变成 `isError`，模型通常直接放弃。
- 忘记清理 `DSH_*` 环境变量。子进程继承宿主的 `DSH_SESSION_ID` 后，脚本里读到的会话与实际不符，日志会指向另一个会话。
- 把 `SANDBOX_UNAVAILABLE` 和权限拒绝混为一谈。前者是 runner 没起来，后者是命令越权；恢复动作相反。
- 用 `readBytes` 读大文件。它会因为超过 `maxBytes` 抛 `FS_TOO_LARGE`；要读大文件用 `streamText` 或 `readByteRange`。
- 在远端场景调用 `processPathFromHostPath()`。它在 SSH 下不可用，会抛错；需要路径时从 `resolve()` 拿 `FsTarget`。
- 用 `bash-local` 换掉 `bash-sandbox`，以为还受约束。`bash-local` 的 `resolve()` 里 `sandboxPolicy` 是惰性字段，注释写得很清楚：这个执行器从不约束。装错之后 `sandbox_permissions` 不会出现在 schema 里，命令直接跑在宿主上，没有任何报错。
- 只装了会约束的 executor 却没装策略服务。`tool-bash` 在加载时就会抛 `tool-bash: the mounted bash executor confines but ctx.sandboxPolicy is missing`，不要把这个错误当成配置警告忽略。
- 以为移动文件之后源一定不存在。`moveFile` 的实现允许保留源文件，跨设备复制时尤其常见；需要原子性就自己检查目标。
- 在 `danger-full-access` 下期待 `ctx.sandbox` 仍然生效。`bash-sandbox` 在这个模式下直接短路调父类，`confine()` 一次都不调用；这是类型层面的约定，`SandboxPolicy` 只接受两个受限模式。
- 把 `[sandbox: escalation available ...]` 当成固定文案写进 prompt。它只在组合对外宣告了提权字段时才出现，而且提示的是"原样重试一次"，不是"换个更宽的模式一直用"。
- 把 spill 定位符当路径拼进命令。它是 `SpillLocator` branded 类型，本地后端渲染成路径，远端后端可能是 URI；拼进 `cat` 只在本地后端碰巧能用。
- 让模型传 `cd` 再跑命令。每次调用都是新 shell，`cd` 不留下状态；用 `workdir` 参数，或者在同一条命令里串起来。
- 以为 `enforcement: 'partial'` 等于"没有约束"。它表示后端覆盖不全，仍然会拦一部分；把它当无约束会让调用方绕过策略自行放宽。
- 在远端组合里只换 `ctx.fs` 不换 `ctx.subprocess`。文件在远端、命令在本地，模型看到的路径与命令能访问的路径对不上。
- 把 `ctx.shell.sandboxMode` 当成"当前模式"。它是后端默认值的声明，会话覆盖会让实际模式更窄或更宽，每次调用都要 `resolve()`。
- spill 预算按字符数算。中文、emoji 与代码的 token 密度差别很大，按字符数配预算会让中文结果过早溢出，或英文结果撑爆上下文。`maxInlineTokens` 用的是 token 估算，代价是估算本身有误差。

## 12. 源码索引

- `docs/subsystems/filesystem.zh.md`：`FsTarget`、`FsVersion` 与 17 个抽象方法；`:398-624` 是完整签名。
- `docs/subsystems/shell.zh.md`：`ShellExecRequest`/`ShellExecSpec` 的字段表与 `onExpiry` 语义。
- `docs/subsystems/sandbox.zh.md`：三个模式、`ConfinedArgv`、拒绝特征与 runner 失败的判别顺序。
- `docs/subsystems/subprocess.zh.md`：`SubprocessCollect` 的 `maxBytes` 与 `spill.maxBytes` 两层上限。
- `docs/subsystems/spill.zh.md`：`SpillStore.saveText`、`SpillLocator` 不透明约定、本地后端路径与权限。
- `docs/subsystems/terminal.zh.md`：`TerminalSessionService` 的九个方法与等待原因。
- `packages/fs/fs-sandbox/README.zh.md`：五个被围栏的写方法、`FS_SANDBOX_DENIED`、与 bash 共用同一套策略解析。
- `packages/shell/tool-bash/src/index.ts:34`：`inject` 列表；`:207-215` 分体装配的报错与策略解析；`:228-248` 提权；`:478-531` 九步主干。
- `packages/shell/bash-local/src/index.ts`：`maxOutputBytes`（64_000）、`maxSpillBytes`（64 MiB）、`collect()` 与 `spawnSpec()`。
- `packages/sandbox/sandbox/src/escalation.ts:172-200`：严格放宽检查与三条报错文案。
- `packages/spill/spill-policy/src/index.ts`：`maxInlineTokens`、头尾摘录、图片计价、失败降级。
- `packages/terminal/terminal-bash/src/index.ts:187-205`：终端后端经由 `ctx.subprocess.spawnTerminal` 起 PTY。
- `packages/fs/fs/src/index.ts`：`FileSystem` 抽象类与 `readBytesWindow`。
- `packages/shell/shell/src/index.ts`：`ShellExecutor.resolve/execute` 与 `ShellProcess.readOutput`。
- `docs/subsystems/ssh.zh.md`：fs/subprocess/sandbox/terminal 四个提供方一起搬走，以及"结果未知不重放"。
- `packages/fs/fs/src/index.ts:66`：`fs/write-intent` waterfall，读后写策略的挂载点；`:103` `FileSystem` 定义；`:148` `resolve`；`:244` `readBytes`；`:259` `readByteRange`；`:282` `writeText`。
- `packages/fs/fs/src/types.ts:76`：`FsInfo` 的三个字段；`:123` `FsWriteIntent` 的两种意图。
- `packages/shell/bash-sandbox/src/index.ts:88-135`：`execute()` 覆盖、`danger-full-access` 短路、runner 失败优先于拒绝；`:187-189` 把命令包成 `['bash','-c',command]`。
- `packages/shell/tool-bash/src/background.ts:44`：`processOutcome()` 的终止原因映射；`:67` `processSources()`；`:86` `ringDelta()`。
- `packages/sandbox/sandbox/src/index.ts:40`：`SandboxExecutionPolicy`；`:77` `SandboxPolicy` 收窄到两个受限模式；`:192` `confine()` 签名。
- `packages/sandbox/sandbox/src/escalation.ts:44-87`：沙箱文本三件套（拒绝标记、提权提示、参数说明）。
- `packages/terminal/terminal/src/index.ts:154`：`spawn()`；`:263` `read()`；`:285` `kill()`。
- `packages/terminal/terminal/src/types.ts:29`：四种等待原因；`:36` 五个允许的信号。
- `packages/shell/shell/src/types.ts:61`：`ShellExecRequest`；`:111` `ShellExecSpec`；`:226` `ShellExecution.result()`。
- `packages/shell/tool-bash/src/index.ts:73`：`validateBashArgs()`；`:146` `resolveWorkdir()`；`:478-531` 九步主干。
- `packages/subprocess/subprocess/src/index.ts`：`resolveExecutable`、`spawn` 与 `spawnTerminal` 的契约。
- `packages/shell/shell-env/src/index.ts`：`ShellEnvRegistry` 的注册与收集。
- `packages/spill/spill-local/src/index.ts`：本地溢出后端的目录分区、权限与 `wx` 打开。
- `packages/fs/fs-sandbox/src/index.ts`：五个写方法的围栏与 `FS_SANDBOX_DENIED`。
- `packages/ssh/`：`fs-ssh`、`subprocess-ssh`、`sandbox-ssh` 与 `ssh-helper-runtime` 的装配关系。

## 最后

执行环境是整条主干里唯一会改变外部世界的一层。它也是唯一一个「失败可能已经发生」的层：命令被杀时副作用可能已经落盘，SSH 断开时远端可能已经改完文件。所以这一层的每个 seam 都带着「先确认，再重试」的痕迹。模型层决定说什么，执行环境决定做什么；前者可以重放，后者不能。理解了这条界线，再看工具管线的准入与结果渲染，很多"为什么这里要拒绝"就说得通了，那部分在 [《工具管线》](/posts/dsh-tool-pipeline.html)。

上一篇：[《模型层：适配器、路由与那条不能重放的流》](/posts/dsh-model-layer.html) ｜ 下一篇：[《客户端：60 个 ui-* 包怎么拼出 GUI》](/posts/dsh-client-architecture.html)
