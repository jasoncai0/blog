---
title: "Shell 与终端：一次性、持久、PTY 三套语义"
date: "2026-11-14T17:30:00+08:00"
summary: "bash、persistent bash、PTY 终端三种执行形态各自解决什么问题，输出环、超时、后台化与沙箱分别插在哪一层。"
description: "拆 shell、terminal、subprocess、spill 四个包：为什么一次命令会有三种生命周期，以及输出截断如何保住上下文。"
tags: ["harness", "architecture", "concurrency"]
slug: "dsh-shell-and-terminal"
---

> 依据：commit fcc0ad46（0.2.2-rc.1）。证据等级 B：结论取自 `packages/shell`、`packages/subprocess`、`packages/jobs`、`packages/spill` 的源码，以及 `docs/subsystems/shell.zh.md`、`terminal.zh.md`、`spill.zh.md`、`subprocess.zh.md`；命令行为没有在沙箱里逐条复现，容量数字都标了来源文件。

同一次会话里跑 `npm test`，三种工具给出三种结果。

`bash` 卡了九十秒，工具说超时，命令却没死，变成了后台任务 `7c1f`。下一次调 `bash`，工作目录回到 workspace 根，上一条命令里的 `cd packages/core` 没留下。

换 `bash-persistent`，`cd` 保住了，可一旦超时，整个 shell 被重置，下一次又从根目录开始。再换 `terminal_open`，`vim` 能开，输出带颜色，可工具只给你一页 scrollback，往上翻不到更早的报错。

三种形态都在同一个 `ctx.shell` 家族里，差异落在生命周期归属：进程归谁、状态存在哪、失败后怎么恢复。

## 0. 结论

| 问题 | 一次性 bash | 持久 bash | PTY 终端 |
| --- | --- | --- | --- |
| 每次调用新建进程 | 是 | 否，每 agent 一个会话 | 否，每次 `terminal_open` 一个 |
| cwd 与环境变量 | 只在本条命令内有效 | 跨调用保留 | 跨调用保留 |
| 超时后做什么 | 默认转后台 job，不杀 | 重置会话，从头开始 | 工具层不设超时，`send` 有等待窗口 |
| 退出码来源 | `exitCode` / `signal` / `timedOut` 三字段 | 命令尾部 `$?` 与标记行 | `sessionStatus.exited` |
| 输出保留 | 收集器尾部 64 KB，溢出写 spill | scrollback 一页 1000 行 | 一页有界 scrollback，`read` 默认 500 行 |
| 典型任务 | 编译、测试、一次性查询 | 需要 activate、export、连续 cd 的流程 | REPL、TUI、需要 tty 的程序 |

三套语义各自成立，混用才会出事。把 `cd` 写进一次性 bash 的下一条命令，或者拿终端去跑一个要三分钟的超时任务，都会掉进后面列的坑里。

先给结论的边界：这里的「持久」只指进程与 shell 状态的存续，不保证输出完整；「一次性」也不等于无状态，每条命令仍会拿到一份当次的环境快照。

## 1. 五种形态的对照

工具目录里能摸到的 shell 入口有五个，加上终端是六个。

| 形态 | 包 | 进程粒度 | 超时默认值 | 备注 |
| --- | --- | --- | --- | --- |
| `bash` | `packages/shell/tool-bash` | 每条命令一个新 shell | 120 s，上限 600 s | 有 job 注册表时可转后台 |
| `bash`（persistent） | `packages/shell/tool-bash-persistent` | 每 agent 一个 PTY | 300 s | 只接受 `command`，没有 workdir |
| `pwsh` | `packages/shell/tool-pwsh` | 每条命令一个新进程 | 同 bash 的夹取方式，默认值见 pwsh-local 的 Config | Windows 方言，`C:\` 路径 |
| `pwsh`（persistent） | `packages/shell/tool-pwsh-persistent` | 每 agent 一个 PTY | 同上 | 持久 bash 的 Windows 对应物 |
| `terminal_*` | `packages/experimental/tool-terminal` | 每次 `terminal_open` 一个 | 工具层没有 | opt-in，默认组合不含 |

一次性 `bash` 的 schema 有五个字段：`description`、`command`、`timeoutMs`、`workdir`、`run_in_background`。没有 `stdin`、没有 `env`、没有 `stdoutMaxBytes`。

后三个只在 `ShellExecRequest` 里存在，供进程内插件使用，模型侧不暴露。模型要喂标准输入，得用 heredoc 或管道，让命令自己解决。

这个裁剪是刻意的。`stdin` 一旦开放，模型就得为每条命令管理一个流；`env` 一旦开放，`DSH_*` 命名空间会被覆盖，后面的工具读到的就是伪造的快照。

### 为什么拆成五个包

路径分隔符、变量语法、信号词汇、退出码约定，每一项在 POSIX 与 Windows 上都要写两遍。拆开之后，模型看到的是几份干净的契约，不用在一份 schema 里判断自己跑在哪个平台。

拆开的另一个好处是失败语义可以单独演进。`pwsh` 在 Windows 上被强杀时没有信号名，这条差异只影响 `tool-pwsh`，不会漏进 bash 的描述。

{{< fig "diagrams/01-three-lifecycles.svg" "三种执行形态的进程、状态与输出边界" >}}

三列从左到右，进程存活时间递增，工具层对输出的控制力递减。

## 2. 一次性 bash：每条命令一个新 shell

每次调用 spawn 一个新进程，命令结束进程消失。这条规则简单，代价是 `cd`、`export`、`source` 全部不留。

想要目录，就在参数里给 `workdir`。想要变量，就在命令里内联 `NODE_ENV=test npm test`。这两条是使用约束，也是并发安全的前提。

### 2.1 请求与规格

`LocalBashExecutor` 的 `resolve()` 把工具层的请求补齐成一份完整规格。

`timeoutMs` 用 `clampTimeout(request.timeoutMs, 120_000, 600_000)` 夹住，`stdoutMaxBytes` 缺省取 `maxOutputBytes`，`workdir` 缺省取会话 workspace，`onExpiry` 缺省为转后台。

请求与规格分成两个类型：`ShellExecRequest` 的字段几乎都可选，`ShellExecSpec` 的字段全部就位。工具层只填自己知道的，执行器负责补全与封顶。

这样分工之后，读工具代码不用猜默认值从哪来，读执行器代码不用猜调用方传了什么。

### 2.2 环境变量快照

每次模型 shell 调用，`ShellEnvRegistry` 都会重建 `DSH_*` 命名空间：先丢弃从父进程继承的 `DSH_*`，再注入当前快照。

先丢弃再注入这一步不能省。宿主的 shell 里可能残留上一版 harness 的变量，模型如果拿到过期值，会按错误的 workspace 或 session id 拼命令。

快照本身是只读的。命令可以读 `$DSH_SESSION_ID` 之类的值，改不了注册表，下一条命令看到的仍是同一份。

### 2.3 退出状态

结果对象有四个独立字段：

| 字段 | 含义 | 容易误读的地方 |
| --- | --- | --- |
| `exitCode` | 进程退出码，可能为 `null` | 命令捕获了 SIGTERM 时，超时也能是 0 |
| `signal` | 终止信号 | Windows 上经常为空 |
| `timedOut` | 是否撞上 deadline | 与 `aborted` 互斥，只报第一个原因 |
| `aborted` | 是否被调用方取消 | 取消来源是 `exec.signal` |

`timedOut` 与 `exitCode` 不互斥。命令自己 `trap` 了 TERM、清理完正常退出，执行器仍然记 `timedOut: true`、`exitCode: 0`。

判断「这次算不算失败」要两个字段一起看。只看 exit code，会把超时当成功。

模型可见的文本由 `renderResult` 拼：先 stdout，再 `[stderr]` 段（stderr 非空才出现），没有输出时给 `(no output)`，最后追加状态标记。

标记有三种：`[timed out after 120000ms]`、`[killed by signal: SIGKILL]`、`[exit code: 1]`。退出标记必须落在最后一行。

原因在解析侧。`parseExitStatus` 从尾部反向解析，`presentResult` 靠它把正文和状态 pill 分开；标记跑到中间，界面卡片就会把正文当成状态。

> 自己搭 Agent 时：给工具结果留一个稳定的尾标记，别把状态混在正文里。模型读结果时不区分渲染层，混进去的标记迟早会被当成命令输出的一部分。

{{< fig "diagrams/02-spawn-chain.svg" "一次 bash 调用经过的四个包" >}}

四层各管一件事：工具拼参数，执行器夹上限，subprocess 管进程与管道，收集器决定留多少字节。

## 3. 持久 bash：拿一个 PTY 当执行器

`tool-bash-persistent` 每个 agent 只开一个 `ctx.terminals.spawn(owner, {type, cwd})` 会话，之后的 `bash` 调用都发进这个会话。

它没有 `timeoutMs`、`workdir`、`run_in_background` 三个参数，只有 `command`。目录与超时是会话级的，在配置里定，调用参数里没有这两个字段。

这条裁剪让 schema 更小，也让「工作目录」这件事只有一个来源。代价是同一个 agent 的命令必须排队。

### 3.1 会话建立

会话建立后先发一条初始化命令：`stty -echo; set +H`。

回显不关，scrollback 里会混进命令本身的副本，解析时得再剔一遍。历史展开不关，`!` 开头的命令会被 shell 意外替换成历史里的内容。

初始化保留 backend 自己的提示符检测。工具不猜提示符长什么样，只认自己打的标记。

会话的所有权按 `Agent` 实例比较，按对象身份，不按名称或 id。同一个 agent 的会话跨后端与工具插件 reload 存活。

这条规则挡住了一类越权：另一个 agent 拿着同样的 session id 字符串发 `terminal_send`，服务层查所有权时对不上，请求被拒。

### 3.2 命令包裹

每条命令包成一行：

```
printf '%s\n' <start>; eval -- '<command>'; __dsh_status=$?; printf '%s%s\n' <end> "$__dsh_status"
```

前后两个随机 nonce 标出边界，退出码从 `$?` 读，不靠猜。

工具轮询 scrollback（`POLL_INTERVAL_MS = 25`），看到结束标记就把两个 nonce 之间的内容当输出，尾部追加 `[Command finished with exit code N]`。

`eval --` 的 `--` 是必要的。命令以 `-` 开头时，没有这个分隔符，`eval` 会把它当选项。

### 3.3 失败恢复

保留量与一次性 bash 不同。`maxOutputChars` 是 16_000 个字符，超出截断并加 `<response clipped>` 提示。

scrollback 一页 1_000 行，更早的内容被终端自己丢掉。工具检测到开头缺失时，补一句 `<NOTE>The beginning of this command output was dropped by the terminal scrollback limit`。

超时的处理是重置。文案写得很直白：`Your command timed out after 300 seconds or experienced an OOM error. Below is partial output:`。

随后告诉模型：`The persistent bash shell was reset; the next bash call starts from the workspace with a fresh current directory and environment.`

| 失败 | 处理 | 下一次调用的起点 |
| --- | --- | --- |
| 输出超 16 000 字符 | 截断，保留头部 | 会话不动 |
| scrollback 丢头 | 补 NOTE，保留可见部分 | 会话不动 |
| 超时 | 杀会话并重建 | workspace 根目录，空环境 |
| 发送失败或 abort | 同上 | 同上 |

串行化靠 `WeakMap<Agent, Promise<void>>` 维护一条队列。同一个 agent 的两次调用不会并发进入 PTY，第二个等第一个结算。

这条约束来自终端本身：一个会话同时只允许一个活动的 send，`TerminalSessionService.startSend` 会在第二个请求进来时拒绝。

代价很明确。持久会话没有并行，一条 `npm run dev` 卡住，后面的命令全排在它后面。

它换来的是状态：`source .venv/bin/activate`、`export NODE_ENV=test`、`cd services/api` 都能留到下一次。

> 自己搭 Agent 时：持久会话必须配一条串行队列，否则两个调用会抢同一个 PTY 的输入。队列的键取 Agent，不取 session id，会话重建后队列才跟着换。

## 4. pwsh 与 pwsh-persistent

`tool-pwsh` 与 `tool-bash` 的参数逐项对应，差异都在方言层。

路径用 `C:\` 原生形式，环境变量写 `$env:NAME`，`DSH_*` 快照按 PowerShell 语法注入。参数名保持一致，模型换平台时不用重学 schema。

有一处行为差异会影响判读。Windows 上被强杀的命令以 `[exit code: 1]` 结算，没有 `[killed by signal: ...]` 标记。

同一个 `renderPwshResult` 在 POSIX 上能给出信号名，在 Windows 上只能给码。模型看到 `1` 时无法区分「命令自己退出 1」和「被沙箱或超时杀掉」。

工具描述里因此写了一句：这种情况应视为中断。判据要看 `timedOut` 与 job 状态，不能只看码。

`pwsh-persistent` 是持久 bash 的对应物，部署侧要提供 pwsh 方言的 PTY 后端。它在 Windows 上承担和 `bash-persistent` 相同的职责：保住 cwd、保住已导出的变量。

没有把三种方言塞进一个工具，原因在参数之外。信号词汇表不同，退出码约定不同，连「进程被杀」这件事的可观测性都不同，合成一份契约只会让描述变长、判读变糊。

## 5. PTY 终端：管道给不了的三样东西

管道只有字节流。PTY 多出三样东西。

第一样是交互。`ssh` 的密码提示、`python` 的 REPL、`psql` 的续行提示符，都要求对面是一个 tty，否则程序直接退出或降级成非交互模式。

第二样是颜色。`isatty(1)` 为假时，大多数 CLI 关掉 ANSI 转义。日志里的红绿对模型没意义，但 `git diff`、`pytest` 的输出结构靠它区分。

第三样是进度。`\r` 覆盖同一行的进度条在管道里会变成一长串残片，在 PTY 里由终端模拟器处理成一行。

这也是 `terminal` 要有独立 seam 的原因。会话的生命周期长于一次工具调用。

`terminal_open` 返回 `sessionId`，之后的 `terminal_send`、`terminal_read`、`terminal_signal` 都带着它。PTY 状态和原始字节只存在进程内，跨后端与工具插件 reload 存活。

它们不写进会话日志。模型输入与有界输出走既有的 tool/call、tool/result 与 job 结果路径，PTY 层不重复记录事件。

### 5.1 等待原因与会话状态

`TerminalWaitReason` 有四个取值，和会话状态是两个维度：

| `waitReason` | 含义 | 会话还在吗 |
| --- | --- | --- |
| `stdin_read` | 检测到程序在读标准输入 | 在 |
| `inferred_idle` | 静默一段时间，推断空闲 | 在 |
| `timeout` | 等待窗口到期 | 在 |
| `session_exit` | shell 退出 | 不在 |

只有 `session_exit` 说明 shell 死了。`terminal_send` 返回 `timeout` 时，程序可能只是跑得慢。

把这个返回值当成崩溃信号，会在下一次 `terminal_read` 时发现会话仍然活着。反过来，看到 `inferred_idle` 就以为命令结束，也可能在命令还没写完输出时就收尾。

读取按 newest-relative offset 取一页：`offset` 默认 0（最新），`count` 默认 500，backend 有上限。一页有界，模型不会因为一次 `terminal_read` 把整个 scrollback 拉进上下文。

启动失败时有一处细节：如果清理没做完，`spawn` 抛 `TerminalBackendCleanupError`，把清理失败原样保留，不替换调用方的取消原因。否则一次取消会掩盖一个没杀干净的进程。

### 5.2 六个工具的边界

| 工具 | 作用 | 边界 |
| --- | --- | --- |
| `terminal_open` | 建会话 | `type` 通常是 `shell`，`cwd` 可指定 |
| `terminal_send` | 发文本 | `submit` 默认 true；发控制字符时设 false |
| `terminal_read` | 读一页 | `offset` 相对最新，`count` 默认 500 |
| `terminal_signal` | 发信号 | 对 shell 的 SIGKILL 被拒，改用 close |
| `terminal_list` | 列会话 | 只列调用者拥有的会话 |
| `terminal_close` | 关会话 | 幂等 |

schema 里没有 TUI 控制、没有具名按键序列、没有 BEL、没有 resize、没有自动启动。

这些缺失是有意的。TUI 控制需要屏幕模型，模型拿到的是文本，给一套按键语义反而增加猜测空间。

`terminal_send` 的 `submit` 默认 true，因为它最常见的用法是执行一条命令。发 Ctrl-C 或不完整的 REPL 输入时传 false，文本会原样写入，不追加回车。

这套工具放在 `packages/experimental`，默认组合不启用。终端带来的状态复杂度不适合作为所有部署的默认面。

> 自己搭 Agent 时：把「发送成功」和「命令结束」分成两个信号。PTY 的 send 返回只能说明这一批字节写完了，命令有没有结束要看会话状态。

## 6. 输出环：三层容量，三种归属

「输出截断」在 DSH 里由三个不同的环负责，容量和溢出行为都不一样。

| 层 | 容量 | 溢出行为 | 谁读得到 |
| --- | --- | --- | --- |
| subprocess 收集器 | 每流 64_000 字节 | 保留尾部，完整流写 spill 文件，上限 64 MB | 执行器、`finalOutput` |
| jobs ring | live 256 KiB，结算后 16 KiB | 从头部淘汰，记 `gapBefore` | `job_output`、工具渲染 |
| terminal scrollback | 一页 1 000 行 | 终端自己丢最早的输出 | `terminal_read` 取一页 |

三层都在进程内存里留一个有界窗口，区别在窗口给谁用。

### 6.1 收集器

一次性 bash 走第一层。64_000 字节是内存里保留的尾部窗口。

命令输出 3 MB 时，前 2.9 MB 进 spill 文件，`CollectedOutput.text` 只留最后一段，`truncated: true`，`spillPath` 指向临时文件。

模型看到的是尾部文本加一行 `[output truncated; full output: /tmp/...]`。头部丢了，尾部保住了，因为报错与失败信息通常在末尾。

### 6.2 ring

转成后台任务后走第二层。`jobs-local` 的 `OutputRing` 按绝对字节偏移存 chunk，`trim(cap)` 从头部淘汰。

单个超大 chunk 只保留 UTF-8 安全的尾部，并打上 `gapBefore` 标记。偏移在淘汰后不移动，所以 `readFrom(from)` 能算出 `lossy: from < earliest`。

结算时的裁剪公式值得单独看一眼：

```
job.ring.trim(Math.max(settledRetainBytes, job.ring.total - job.modelCursor))
```

`settledRetainBytes` 是 16 KiB。取 `max` 的意思：如果模型还有 200 KiB 没读过，环就保留 200 KiB，不因为任务结算就裁到 16 KiB。

第一次终态读取之后，才按上限收紧。这个顺序保证「任务结束」不会先把还没读的输出删掉。

### 6.3 spill 何时落盘

spill 在三个时机落盘：收集器溢出（bash 的 64_000 字节）、搜索结果超限（glob 100 条、grep 250 条）、结果 token 数超 `maxInlineTokens`（由 `dsh-spill-policy` 消费方决定）。

落盘后模型拿到的是一个不透明的 `locator` 和一句 `retrievalHint`。本地 backend 渲染成路径并提示用 `read` 或 `grep` 取。

存储侧只做一件事：把文本原样写进一个私有目录。根目录权限 0700，文件用 `wx` 打开、权限 0600，防止预置符号链接。

存不下时保留原始内联结果，不把一次成功的调用变成 `isError`。这个选择偏保守：宁可占上下文，也不让调用方以为工具坏了。

> 自己搭 Agent 时：给截断留一个可操作的地址，别只给一句「输出太长」。模型能拿这个地址去读第二页，截断才不丢信息。

## 7. 超时、取消、后台：三条边界

三个动作容易被混成一个，边界不一样。

| 动作 | 触发者 | 对进程做什么 | 模型看到 |
| --- | --- | --- | --- |
| 超时 | `timeoutMs` 到期 | 默认转后台 job；`onExpiry` 设为杀进程时才终止 | `[timed out after Nms]` 或 `[still running after Nms; moved to background job X]` |
| 取消 | `exec.signal` 中止 | 终止进程，`aborted: true` | 工具调用报 ABORTED |
| 后台 | `run_in_background: true` | 不设超时，进程独立跑 | 立即返回 job id |

### 7.1 超时

`clampTimeout` 把默认值定在 120 秒，上限 600 秒。工具 schema 里 `timeoutMs` 的描述写明了到期行为：`on expiry the command moves to the background as a job instead of being killed`。

这是默认开关 `promote`。没有 job 注册表时退化成直接杀。

转后台之后，工具返回 `[still running after 120000ms; moved to background job 7c1f]`，之后用 `job_output` 增量读。

前台调用本身也注册成一个 job。工具在它上面 `registry.wait(id, timeoutMs, owner)`，等待超时后做一次消费读，让 `job_output` 从同一游标继续，不重复吐已经给过的字节。

### 7.2 取消

取消的来源是 `exec.signal`，由上层在模型中断、会话拆除、用户取消时触发。

执行器把信号透传给 subprocess，进程收到后进终止流程。`ShellRunResult.aborted` 与 `timedOut` 互斥，只报第一个到达的原因。

终端侧的取消更细。`TerminalSendOperation.cancel()` 发 SIGINT，只打断当前这一批输入，会话继续活着。要连会话一起杀，用 `terminal_close`。

### 7.3 后台

`run_in_background: true` 的调用不设超时。工具立即返回 job id，进程按 `maxConcurrentJobsPerOwner`（默认 10）排队。

后台任务的生命周期归 `ctx.jobs`，不归 shell seam。`ctx.shell` 只返回进程句柄，不注册任务；注册发生在工具层。

这条分工让后台任务可以脱离 shell 工具的调用栈活下去，也让 `job_output`、`job_kill` 这些工具不必知道命令是用 bash 还是 pwsh 跑的。

`ShellProcess.done` 承诺永不 reject。进程非零退出、被信号杀掉、被 kill() 停掉，都走 resolve，原因放在 `exitCode` 与 `signal` 里。等待方不用写 try/catch，也就不会把「命令失败」当成「等待失败」处理。

哪些命令该转后台：跑得比超时长、但结果一会儿要用的，比如构建和全量测试。

哪些该被 kill：交互式程序、`tail -f`、`npm run dev` 这类不会自己结束的。判断依据是命令会不会自己退出，耗时长短不作数。

{{< fig "diagrams/03-job-promote.svg" "前台命令转后台任务时的两次游标读取" >}}

## 8. 沙箱怎么插进 spawn

沙箱在 spawn 之前介入，入口是 `ShellExecRequest.sandboxPolicy`。

顺序是这样：

1. 工具层调 `ctx.sandboxPolicy.resolve()` 得到当前站立模式对应的策略，放进请求。
2. 执行器把策略传给 subprocess 层，由 runner 决定包装方式：进程内限制、外部 runner、或拒绝。
3. 结果里的 `ShellSandboxInfo` 带回 `mode`、`denied`、`enforcement`、`runnerFailed` 四个字段。
4. `renderResult` 把 `denied` 渲染成 `[sandbox: file access denied under workspace-write mode]` 这样的标记，并附一次性升级提示。

这里有一条和第一季 `sandbox` 决策树一致的规则：策略在操作发生的那一步执行，不靠 schema 省略或工具描述来「阻止」。

模型拿不到 `sandbox_permissions` 字段时，调用根本表达不出升级请求。给了字段，也要 `ctx.approval` 在执行任何操作前批准这次确切调用。

后台进程的失败通道不一样。`SANDBOX_UNAVAILABLE`（受限模式没有可用后端）在前台直接抛。

后台进程已经返回了 job id，只能经事实通道报 `runnerFailed`。渲染成 `[sandbox: the sandbox runner itself failed under workspace-write mode]`，后面还有一句说明命令没有运行。

把这句和命令失败分开，是为了让模型不要反复改命令。沙箱后端挂了，改多少次参数都一样。

升级是一次性的。批准之后只替换本次调用的 `mode`，站立模式不变，下一条命令仍从原模式起步。

这个设计让「临时放宽」不会沉淀成默认。代价是模型每次都要重新申请，收益是权限边界不会因为一次例外而移动。

> 自己搭 Agent 时：把拒绝做成带原因的失败，别做成静默的成功。模型看不到拒绝标记，就会以为命令跑了但没有输出。

## 9. 命令卡住时的排查顺序

按这个顺序走，多数情况在前两步就能定位。

1. 先看是不是已经转后台。`job_list` 里有 id，说明进程还活着，去 `job_output`，别再重跑。
2. 看 `job_output` 的 `lossy`。为真说明请求的偏移已经被环淘汰，缺口只能从 spill 文件恢复，重读也没用。
3. 看 `sandbox.runnerFailed`。为真说明命令根本没跑，问题在沙箱后端，改命令无效。
4. 看 `timedOut` 与 `exitCode` 的组合。`timedOut: true` 且 `exitCode: 0` 是命令自己处理了信号。
5. PTY 场景看 `waitReason`。`stdin_read` 说明程序在等输入，`timeout` 只是等待窗口到了。
6. 看命令是否在等一个不存在的 tty。管道里 `ssh`、`sudo`、`git commit` 没带 `-m` 都会挂住。
7. 最后才 `kill`。杀掉之后输出停在半截，之前的时间白花。

顺序背后的原则：先确认进程状态，再确认数据完整性，最后才动进程。反过来做，会把「还在跑」误判成「卡死」。

第 1 步之后不要立刻 `job_output` 第二次。增量读按游标推进，重复读同一段会得到空 delta，看起来像「没有输出」，实际是已经读过了。

第 4 步的组合判断值得写成一张小表：

| `timedOut` | `exitCode` | 判读 |
| --- | --- | --- |
| false | 0 | 成功 |
| false | 非 0 | 命令失败，看输出 |
| true | 0 | 命令处理了信号，按超时处理 |
| true | 非 0 | 超时且未清理干净 |

{{< fig "diagrams/04-stuck-command.svg" "排查顺序与每步要看的事实字段" >}}

## 10. 设计取舍

1. 一次性 bash 不保留 cwd。代价是多步流程每次都要传 `workdir`；收益是两个并行调用不会互相改目录。共享 cwd 的 shell 在并发下必然互相踩。
2. 持久 bash 用 PTY 而不是长驻子进程加管道。代价是要处理提示符检测、回显抑制、scrollback 丢头；收益是 `source`、`activate`、REPL 都能用。
3. 输出环按字节偏移存，不按行。代价是可能从半行开始读；收益是增量读取有稳定坐标，淘汰后偏移不移动。
4. 超时默认转后台，不默认杀。代价是慢命令可能积累成进程泄漏；收益是长构建不用重跑。上限交给 `maxConcurrentJobsPerOwner` 兜。
5. 终端独立成 seam 且 opt-in。代价是多一层会话管理与所有权检查；收益是默认工具集不背上 PTY 的状态复杂度。
6. spill 尽力而为。代价是保存失败时信息真的丢了；收益是一次成功的调用不会因为存储问题变成 `isError`。

## 11. 踩坑点

1. 超时后以为命令死了，换个写法重跑，两个进程同时写同一份产物。原因：默认 `promote` 把前台命令留成了后台 job。对策：先 `job_list`，有 id 就收结果。
2. 在一次性 bash 里 `cd` 完就结束，下一条命令找不到目录。原因：每次调用新 shell。对策：改用 `workdir` 参数，或换 `bash-persistent`。
3. 大输出只看到尾部，头部那几行报错没了。原因：收集器保留尾部 64_000 字节。对策：从 `spillPath` 读，或把命令输出 `| head -100`。
4. Windows 上命令被杀只看到 `[exit code: 1]`，当成普通失败去改参数。原因：没有信号标记。对策：结合 `timedOut` 与 job 状态判断。
5. `terminal_send` 返回 `timeout` 就关闭会话。原因：把等待窗口到期当成 shell 退出。对策：看 `sessionStatus`，`running` 就继续 `terminal_read`。
6. 沙箱拒绝后换一种写法重试，仍然拒绝。原因：拒绝按模式判定，与写法无关。对策：读提示里的模式，走 `sandbox_permissions` 申请一次放宽。

## 12. 源码索引

| 位置 | 证明什么 |
| --- | --- |
| `packages/shell/bash-local/src/index.ts` 的 `LocalBashExecutor` | 120 s / 600 s 夹取、64_000 字节每流、64 MB spill 上限、`graceMs` 默认值 |
| `packages/shell/bash-local/src/index.ts` 的 `resolve()` | 请求到规格的补齐位置，缺省值只在这里出现 |
| `packages/shell/bash-local/src/index.ts` 的 `finalOutput()` | 收集器如何投影成 `CollectedOutput` |
| `packages/shell/tool-bash/src/index.ts` 的 `execute` | 何时注册 job、何时 `promote`、`registry.wait` 的用法 |
| `packages/shell/tool-bash/src/render.ts` 的 `renderResult` | 状态标记的顺序，退出标记为何在最后 |
| `packages/shell/tool-bash/src/render.ts` 的 `renderPromoted` | 转后台时给模型的那一行 |
| `packages/shell/tool-bash-persistent/src/index.ts` 的 `SCROLLBACK_PAGE_LINES` | 持久 bash 一页 1 000 行的来源 |
| `packages/shell/tool-bash-persistent/src/index.ts` 的 `serialized()` | 每 agent 一条队列的串行化实现 |
| `packages/shell/tool-bash-persistent/src/index.ts` 的初始化命令 | `stty -echo; set +H` 关回显与历史展开的位置 |
| `packages/shell/tool-bash-persistent/src/index.ts` 的超时分支 | 重置会话的三条文案 |
| `packages/shell/tool-pwsh/src/index.ts` 的 `renderPwshResult` | Windows 上缺信号标记的分支 |
| `packages/jobs/jobs-local/src/ring.ts` 的 `OutputRing.trim` | `gapBefore`、`lossy`、偏移不移动 |
| `packages/jobs/jobs-local/src/index.ts` 的 `settle()` | 结算后 `max(16 KiB, 未读字节)` 的裁剪 |
| `packages/subprocess/subprocess/src/types.ts` 的 `SubprocessSpawnSpec` | `argv` 不经 shell、stdio 全显式、`graceMs` 约束 |
| `packages/subprocess/subprocess-local/src` 的 `spawnTerminal` | 唯一分配控制终端的原语 |
| `packages/terminal/terminal/src/index.ts` 的 `TerminalWaitReason` | 四个等待原因与会话状态分离 |
| `packages/experimental/tool-terminal/src/index.ts` | 六个终端的 schema 与 `submit` 默认值 |
| `packages/shell/shell/src/index.ts` 的 `parseExitStatus` | 从尾部逆解析 `[exit code: N]` |
| `packages/spill/spill/src/types.ts` 的 `SpillRef` | `locator` 不透明、消费方只用 `retrievalHint` |

## 最后

三种形态的选择可以概括成一句：命令之间需不需要共享状态。

不需要，用 `bash`；需要，用 `bash-persistent` 或 `pwsh-persistent`；需要 tty，用 `terminal`。

输出这一侧的规律也简单：每一层只保留尾部一个有界窗口，超出部分进 spill 文件或由终端自己丢。模型看到的通常是「尾部 + 一句去哪取」。

判断信息是否完整，看 `truncated`、`lossy`、`spillPath` 三个字段，不看输出看起来有多长。

> 自己搭 Agent 时：把「进程状态」和「输出完整性」拆成两个字段回传。合并成一个 `success` 布尔，模型就没法区分「命令还在跑」「命令被杀了」「输出被截断了」，而这三件事的下一步动作完全不同。

上一篇 [文件工具族](/posts/dsh-file-tools.html) 讲读写的失败模式，这一篇讲执行。两篇共用的那条线：超时与截断都发生在固定的层，工具实现里没有各自一套。

执行环境的分层见 [执行环境](/posts/dsh-execution-environment.html)，工具调用的管道见 [工具管道](/posts/dsh-tool-pipeline.html)，后台任务的完整生命周期见 [工作流与后台任务](/posts/dsh-workflow-jobs-goal.html)。
