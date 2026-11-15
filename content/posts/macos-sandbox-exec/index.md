---
title: "macOS sandbox-exec 解剖：从 SBPL 编译到内核 MACF 判决"
date: "2026-11-15T17:20:00+08:00"
summary: "sandbox-exec 在 man page 里被标了十年 DEPRECATED，却仍是 Claude Code、Codex 这类 coding agent 在 macOS 上约束子进程的主要手段。这篇拆开它的两段式实现：SBPL 在用户态编译成规则表，内核在每次资源获取时逐条查表；并用本机实验说明规则覆盖顺序、/tmp 符号链接、嵌套失效、dyld 读根目录这些坑。"
description: "从 SBPL 编译器、规则表、MACF 钩子到 sandbox extension，逐层拆解 macOS sandbox-exec 的实现与可复现的坑。"
tags: ["sandbox", "architecture"]
slug: "macos-sandbox-exec"
---

`sandbox-exec` 的 man page 第一行就把自己标成 DEPRECATED。它没有被删除，也没有被替换：Claude Code 在 macOS 上的 Bash 沙箱仍然用 Seatbelt（[官方文档](https://code.claude.com/docs/en/sandboxing)，底层是 `@anthropic-ai/sandbox-runtime`），Codex CLI 的 `codex-rs/core/src/seatbelt.rs` 也直接调它（[第三方拆解](https://github.com/simonw/research/blob/main/codex-sandbox-investigation/notes.md)）。一个「废弃」了十几年的命令，成了 agent 工具链里最顺手的权限闸门。

这篇想回答的是：它凭什么能拦得住进程，以及为什么你写的 profile 经常不生效。

本文所有结论都在本机（macOS 15.6，24G84，arm64）用可复现的命令验证过；涉及内核内部、无法直接验证的部分，按文档与符号推断。

## 一、它其实是两个程序

`/usr/bin/sandbox-exec` 只有 102,560 字节，`otool -L` 只链了 `/usr/lib/libsandbox.1.dylib` 和 libSystem。`nm -u` 看它导入的符号：

```text
_sandbox_compile_string
_sandbox_compile_file
_sandbox_compile_named
_sandbox_create_params
_sandbox_set_param
_sandbox_apply
_sandbox_free_profile
```

没有 `_sandbox_init`。也就是说它没走那个公开但已废弃的 `sandbox_init(3)`，而是直接用底层的「编译 → 应用」两段式 API。字符串里还有一句 `profile compilation failed`，提示错误来自哪一层。

所以一次 `sandbox-exec -f p.sb cmd` 可以拆成两半：

1. **用户态**：`libsandbox` 把 SBPL 文本编译成内核能查的规则表。语法错误、未定义符号、非法 filter 都在这一步报错，进程根本起不来。
2. **内核态**：`sandbox_apply` 把规则表挂到当前进程的 credential 上；之后每次进程申请资源（`open`、`connect`、`mach_msg` 查端口），内核里的 MACF 钩子都会拿这张表判一次。

{{< fig "diagrams/01-two-stage.svg" "上：sandbox-exec 在 exec 之前完成编译与应用。编译产物是规则表，不是文本；内核每次资源获取只查表。下：规则表挂在进程 cred 上，fork/exec 继承，不能再挂第二层。" >}}

关键结论先放在这里：**SBPL 不是运行时解释的脚本**。文本只在启动时读一次，编译失败就是启动失败；运行期内核只做规则匹配。这也解释了为什么改 profile 必须重启被测进程。

## 二、最小可用 profile：echo 只需要两个权限

先看一个能跑的最小例子：

```sh
sandbox-exec -p '(version 1)
(deny default)
(allow file-read*)
(allow process-exec)' /bin/echo hi
```

输出 `hi`。`-p` 直接给字符串，`-f` 读文件，`-n` 用系统内置的旧 profile，`-D key=value` 传参数。

这个 profile 里只有两条 allow：

- `file-read*`：echo 要读自己的二进制，dyld 要读共享库。`file-read*` 是 `file-read-data`、`file-read-metadata`、`file-read-xattr` 这一族的通配。
- `process-exec`：允许 `execve`。去掉它，报错不是业务代码的错，而是 sandbox-exec 自己 exec 失败：

```text
execvp() of '/bin/echo' failed: Operation not permitted
```

`(deny default)` 把没被任何规则命中的操作全部拒掉。这是最实用的起点：先全拒，再一条条加，缺什么看日志补什么。

## 三、编译期：SBPL 是一段 Scheme

SBPL 的外形是 S 表达式。`/System/Library/Sandbox/Profiles/application.sb`（App Sandbox 的根 profile）开头是这样的：

```text
(version 1)
(import "system.sb")
(import "appsandbox-common.sb")
(define (bundle-id-can-register-service ...) ...)
(param "application_bundle_id")
```

`define`、`import`、`param`、`let` 都有，说明它是一门带函数抽象和模块加载的 Scheme 方言，不是配置文件。本机这个目录下有 459 个 `.sb`，系统进程的权限就是这些文件加上各自参数拼出来的。

编译错误的信息很具体，因为编译器知道行列号。把 `file-read*` 拼错成 `file-raed*`：

```text
sandbox-exec: unbound variable: file-raed* at /private/tmp/typo.sb, line 3, column 8
Backtrace ...
```

漏掉 `(version 1)`：

```text
sandbox-exec: no version specified
```

`-n` 给一个不存在的名字：

```text
bogus-profile: profile not found
```

这些错误发生在 `sandbox_apply` 之前，目标程序还没 exec，所以退出码是 65（EX_DATAERR），而不是被测命令的退出码。排查时先分清「profile 没编过」和「命令被拦了」，两者的日志完全不同。

## 四、运行期：label 挂在进程上，一次生效

`sandbox_apply` 改的是当前进程的 credential。之后这个进程 `fork` 出的子进程、`exec` 换掉的镜像都继承同一个 label；没有公开 API 能把它摘掉，也不能再叠一层。

我试过在已经沙箱化的进程里再跑一次 `sandbox-exec`，哪怕外层是 `(allow default)` 也失败：

```text
sandbox-exec: sandbox_apply: Operation not permitted
```

退出码 71（EX_OSERR）。这不是 bug：内核不允许给已经有 label 的进程换一个新的。Codex 的用户也踩过同一个坑——在别的 Seatbelt 里启动 Codex，它内部的 `sandbox-exec` 一律失败（[记录](https://github.com/openonion/connectonion/blob/a32a7dceccf54050a73daeba9341bd58ad0591a1/docs/testing/rem-sandbox-profile-results.md)）。

这条限制决定了工具的用法：**profile 必须一次写全**。不能先起一个宽沙箱，再在里面套一个窄的；也不能让父进程先 apply 再让子进程改。你能做的只有把规则合并成一份，或者干脆不嵌套。

## 五、规则匹配：最后写的那条赢

这是最容易猜错的地方。直觉上 `deny` 应该优先，或者「更具体的规则」优先。实测都不是。

准备一个 profile：`deny default`，放开全盘读和 exec，然后在末尾禁掉 hosts：

```text
(deny default)
(allow file-read*)
(allow process-exec)
(deny file-read-data (literal "/private/etc/hosts"))
```

`cat /private/etc/hosts` 得到 `Operation not permitted`。在文件末尾再追加一条同路径的 `allow`：

```text
(allow file-read-data (literal "/private/etc/hosts"))
```

再 cat，又能读了。规则没有变「具体」，也没有变「宽松」，只是**最后命中的那条覆盖了前面的**。反过来，把 deny 写在 allow 前面就完全无效。

所以匹配模型是：按书写顺序遍历规则表，记下最后一条命中的规则，用它的动作；一条都没命中，用 `default`。同一条规则里的多个 filter 是「或」：`(deny file-read-data (literal "/private/etc/hosts") (literal "/private/etc/passwd"))` 会同时禁掉两个文件，同目录的 `group` 不受影响。

{{< fig "diagrams/02-last-match.svg" "同一个请求依次经过规则表：R3 的 deny 覆盖前面的 allow，R4 又覆盖 R3，最终 R5 的 deny 生效；没有命中才落到 default。" >}}

实践含义：

- 例外要写在后面。`allow` 打底、`deny` 收尾，是最不容易出错的顺序。
- 别用「更具体」去推理。两条规则谁赢只看位置。
- 生成 profile 时，把「默认放开、少量禁止」和「默认禁止、少量放开」当成两种模式，不要混着写。

## 六、路径：你写的字符串不是内核看到的路径

SBPL 里的路径 filter 有四种常见写法：

```text
(literal "/private/etc/hosts")          ; 精确匹配这一个路径
(subpath "/Users/me/project")           ; 匹配该目录及其下所有内容
(regex "^/Users/me/project/.*\.log$")  ; 正则
(extension "com.apple.app-sandbox.read") ; 引用一个 sandbox extension（见第九节）
```

`literal` 和 `subpath` 的差别很直观：前者只覆盖那个路径本身，后者覆盖整棵子树。真正的坑在符号链接。

macOS 上 `/tmp` 是指向 `/private/tmp` 的符号链接，`/etc` → `/private/etc`，`/var` → `/private/var`。内核在判权限时用的是**解析后的真实路径**，而 profile 里的字符串是原样参与匹配的，编译器不会替你展开。于是：

```text
(deny file-write* (subpath "/tmp/sbx/blocked"))
```

对 `/tmp/sbx/blocked/b.txt` 的写入**不生效**；把规则改成 `(subpath "/private/tmp/sbx/blocked")`，立刻变成 `Operation not permitted`。日志里也能看到内核记录的是 `/private/tmp/sbx/blocked/b.txt`。

{{< fig "diagrams/03-path-resolution.svg" "左边是用户看到的路径，右边是内核判权限时用的真实路径。/tmp、/etc、/var 都是符号链接；profile 里写哪一边，决定规则是否命中。" >}}

另一个高频坑是根目录。用 `deny default` 只放开 `/System`、`/usr/lib`、`/bin` 这些子路径时，`/bin/echo` 会直接 `SIGABRT`（退出码 134），日志是：

```text
Sandbox: echo(...) deny(1) file-read-data /
```

补一条 `(literal "/")` 的读权限就恢复。原因是 dyld 在加载阶段会访问根目录本身（`subpath` 只覆盖目录下的内容，不覆盖目录节点自己的元数据）。所以模板里永远留一条 `(literal "/")`，成本极低。

还有两个和路径有关的边角：

- 网络 filter 里写具体 IP 会编译失败：`(remote ip "127.0.0.1:*")` 报 `host must be * or localhost in network address`；`(remote ip "localhost:*")` 可以编过，但 `localhost` 解析到哪由系统决定，不等于只放行回环。
- `-D` 传的参数不要塞进路径 filter。`(subpath (param "ROOT"))` 在这台机器上让编译器直接 abort（退出码 134，无输出）；未定义的 `(param "HOME")` 则报 `invalid data type of path filter; expected pattern, got boolean`。要动态路径，用脚本生成 profile 文本，别用 `param`。

## 七、一次 write 在内核里走了哪几步

前面都是「用户看到的现象」。真正拦住写操作的是内核里的一条链：

1. 进程发起 `write(2)`。文件描述符在 `open` 时已经拿到，所以写路径上先经过 VFS 的 vnode 层。
2. VFS 在做实际操作前调用 MACF 钩子（例如 `mac_vnode_check_write` 一类）。MACF 是 XNU 的通用访问控制框架，沙箱只是挂在上面的一组策略。
3. 沙箱策略取出当前进程 cred 上的 label，用规则表匹配「操作类型 + 路径/端口」。
4. 命中 allow 就返回 0，VFS 继续；命中 deny 或落到 `default`，返回 `EPERM`，系统调用失败。
5. 内核同时往统一日志里写一条记录，格式是 `Sandbox: 进程名(pid) deny(1) 操作 路径`。重复违规会被合并成 `N duplicate report`。

{{< fig "diagrams/04-write-path.svg" "一次 write 的判决路径：syscall 进入 VFS，MACF 钩子回调沙箱策略，规则表给出 allow/deny；deny 变成 EPERM，并异步写日志、按需通知 sandboxd。" >}}

用户态的 `sandboxd` 也在这条链上。它链接同一个 `libsandbox`，字符串里有 `HOST_SEATBELT_PORT`、`extension-class`、`backtrace`。man page 说它「代表 Sandbox 内核扩展提供服务」。从符号和 man page 推断，它至少负责两件事：处理沙箱扩展（extension 的签发与消费），以及给违规日志做符号化。内核通过 host special port 把消息递过去，所以即使 `sandbox-exec` 进程已经 exec 成别的程序，日志和扩展请求仍然有用户态服务接手。

日志里 `deny(1)` 括号中的数字我没有找到公开解释，本文不猜。排查时用：

```sh
log show --last 30s --predicate 'eventMessage CONTAINS "Sandbox:"'
```

## 八、资源面：文件之外还有五类

`sandbox-exec` 拦的不只是文件。SBPL 的操作族大致可以分成：

| 族 | 典型操作 | 被拒后的症状 |
| --- | --- | --- |
| 文件读 | `file-read-data`、`file-read-metadata`、`file-read-xattr` | `open`/`stat` 返回 EPERM，动态库加载失败 |
| 文件写 | `file-write-data`、`file-write-create`、`file-write-unlink` | 创建、写入、删除失败 |
| 文件控制 | `file-ioctl` | 终端程序、`fcntl` 密集的运行时出问题 |
| 进程 | `process-exec`、`process-fork` | exec 失败；`fork: Operation not permitted` |
| 信号 | `signal` | `kill` 失败 |
| Mach | `mach-lookup` | 查不到系统服务，XPC 调用挂起或崩溃 |
| 网络 | `network-outbound`、`network-inbound`、`network-bind` | `connect` 被拒；DNS 也走这条路 |
| 系统 | `sysctl-read`、`sysctl-write`、`iokit-open`、`ipc-posix-*` | 取系统信息失败、设备访问失败、共享内存失败 |

{{< fig "diagrams/05-operation-families.svg" "八类操作族和它们的典型拒绝症状：三张文件卡片，加上进程、信号、Mach、网络、系统信息；同一个程序往往同时需要好几族。" >}}

实测下来，不同程序的最小集合差别很大：

- `/bin/echo`：`file-read*` + `process-exec` 就够。
- `/bin/sh -c 'echo hi | cat'`：还要 `process-fork`。去掉它，报 `/bin/sh: fork: Operation not permitted`，退出码 128。
- `node -e 'console.log(1)'`：在放开 `/opt/homebrew` 读权限后，`file-read*` + `process-exec` 就能启动；我这个简单用例里 `mach-lookup`、`sysctl-read` 都不是必需。但一旦用 HTTP、worker、原生模块，缺的就不止这两条，报错还会以间接形式出现，不能拿最小用例反推生产可用。

还有一个容易忽略的：当前工作目录。profile 没放开 cwd 时，`sh` 会打印 `shell-init: error retrieving current directory: getcwd: cannot access parent directories: Operation not permitted`，不影响简单命令，但会让脚本里的相对路径行为异常。模板里要把 cwd 一起放开。

## 九、App Sandbox 与 extension：另一套入口

同样是 Seatbelt，普通 App 走的路径和 `sandbox-exec` 不一样：

- App 在签名里带 `com.apple.security.app-sandbox` entitlement。启动时 `secinitd` 看到这个 entitlement，给它装上 `application.sb`（加 bundle id、容器路径等参数），并把 HOME 指到 `~/Library/Containers/<bundle-id>`。
- App 默认只能访问自己的容器。用户通过打开/保存面板选中的文件，由 Powerbox 生成一个 **sandbox extension**（token），App 消费 token 后才拿到那个路径的读写权。
- profile 里对应的是 `(extension "com.apple.app-sandbox.read")` 这类规则；extension 由 `sandboxd` 签发和校验，token 可以序列化后传给子进程。

{{< fig "diagrams/06-app-sandbox.svg" "两条入口：sandbox-exec 直接编译并 apply 任意 profile，没有签名、容器和 extension；App Sandbox 由 entitlement 触发 secinitd 装载 application.sb，用户选文件时经 Powerbox 签发 extension。" >}}

`sandbox-exec` 没有 entitlement、没有容器、也不会给你签 extension。它就是一个「拿一段文本，编译，挂到进程上」的裸入口。这既是它好用的原因（不需要签名、不需要打包），也是它不能替代 App Sandbox 的原因（没有用户授权模型，也没有容器隔离）。

## 十、给 agent 的模板

下面这份 profile 在本机验证过：能跑 `sh`、能写 `/private/tmp`。`curl` 在这份 profile 下也失败——日志显示先被拦的是 `/etc`、`/var` 的元数据，还没走到 `connect`。把它当起点，按报错往里加：

```text
(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow sysctl-read)
(allow mach-lookup)
(allow file-read*
  (literal "/")
  (subpath "/System")
  (subpath "/usr/lib")
  (subpath "/usr/bin")
  (subpath "/bin")
  (subpath "/sbin")
  (subpath "/Library/Apple")
  (subpath "/opt/homebrew")
  (literal "/dev/null")
  (literal "/dev/urandom")
  (subpath "/private/tmp")
  (subpath "/private/var/select")
  (subpath "/Users/me/project"))
(allow file-write*
  (literal "/dev/null")
  (subpath "/private/tmp")
  (subpath "/Users/me/project"))
```

几条使用规则：

1. 路径全部写 realpath 后的形式，不要写 `/tmp`、`/etc`、`/var`。
2. 永远留 `(literal "/")` 的读权限，否则 dyld 可能让进程直接 abort。
3. 把 cwd 和要写的目录都列进 `file-write*`，否则脚本会在奇怪的地方失败。
4. 需要跑 Homebrew 装的工具时，把 `/opt/homebrew` 加进读和执行路径；Apple Silicon 上这是默认前缀。
5. 很多脚本会往 `/dev/null` 写，`file-write*` 里要单独放行它；否则报的是 `/dev/null: Operation not permitted`。
6. 需要网络就加 `(allow network-outbound)`；不要用 `(remote ip "127.0.0.1:*")`，编不过。
7. 例外规则（deny）放最后；需要「大部分放开、少数禁止」时，先 `(allow default)` 再逐条 deny，同样遵守最后命中。
8. 用真实命令做冒烟测试，别只测 `/bin/echo`。测试时同时看退出码和 `log show`。

## 十一、它不是虚拟机

最后说清楚边界，避免把它当万能隔离：

- **共享内核**。sandbox-exec 不创建 namespace、不虚拟文件系统，它和宿主是同一个内核、同一份文件树。它拦的是「这次资源获取」，不是「这个进程看到的世界」。
- **已打开的 fd 不受约束**。`sandbox(7)` 写得很直白：限制通常在获取资源时施加；如果应用已经有一个可写的 fd，它仍可以继续用这个 fd，不管后来的规则。所以「先拿到 fd 再收紧」是绕过路径。
- **规则按路径匹配**。符号链接会让「你写的路径」和「内核判的路径」不一致；这也是第六节那些坑的来源。
- **编译器在内核之外**。SBPL 的语义、操作名、filter 类型由用户态 `libsandbox` 和内核共同约定；系统升级可能改变行为。它被标 DEPRECATED 不是没有原因，只是目前没有等价替代品。
- **对定向攻击的强度有限**。它适合约束「会犯错但不算恶意」的代码（agent 生成的 shell 命令、构建脚本、第三方 CLI），不适合当多租户隔离或恶意代码沙箱。真要隔离，用虚拟机或容器加独立内核。

## 十二、尾声

`sandbox-exec` 的设计很省：文本在启动时编译一次，内核在每次资源获取时查表，规则顺序决定胜负，路径必须先解析。所有让人困惑的行为——规则不生效、/tmp 拦不住、进程直接 abort、嵌套报 EPERM——都能从这四句话推出来。

对写 agent 工具的人来说，它是 macOS 上少有的「不用签名就能约束子进程」的接口；对读它的人来说，它是一个把 Scheme 编译器、MACF 钩子和统一日志串起来的完整样本。DEPRECATED 只是状态，不是能力。
