---
title: "从能跑到跑对：搭一个本地 Coding Agent 踩过的坑"
date: "2026-11-15T16:40:00+08:00"
summary: "一个本地 Coding Agent 从 demo 做到可交付，真正耗时的不是 agent loop，而是验证边界、执行环境和失败归因。十轮上限、.toolchain 污染、Maven 沙箱、语言检测，这些坑比循环本身更难。"
description: "复盘 Jade 项目的十阶段循环、工具/技能发现、四语言验证、macOS 沙箱、会话恢复与连续追问；包含 MAX_STEPS 误判、SDK 下载污染、Maven/Spring Boot 沙箱失败、node --check 多文件只查第一个等具体错误与修法。"
tags: ["harness", "codegen", "architecture"]
slug: "coding-agent-pitfalls"
---

> 项目：`jasoncai0/java-coding-agent`（私有仓库），Jade 1.0.0，commit `01697db`。
> 环境：macOS 15.6，JDK 23，Maven 3.9.9，Python 3.13，Node 23，Go 1.27。
> 这不是 API 教程，是一份故障复盘。项目内 `docs/BLOG_MEMORY.md` 会继续追加新坑，本文也会同步更新。

## 0. 结论

这个项目最后能做的事不复杂：本地起一个 Web 控制台，把任务交给 DeepSeek，模型通过工具改工作区，运行时用真实命令编译、运行、验证，最后把过程通过 SSE 推给浏览器。

真正花时间的，是下面这些判断：

1. **`MAX_STEPS` 不是错误原因。** 它只说明循环在第 10 轮停了；工件可能已经跑通，也可能根本没编译过。
2. **源码发现就是安全边界。** 把 Go SDK 下载进工作区后，验证器会把 SDK 里的 JavaScript 当成项目源码。
3. **写沙箱不是容器。** Maven 的共享缓存、JVM 的临时目录、下载网络、本地端口，各自都是独立假设。
4. **编译通过不等于运行通过，运行通过不等于测试通过。** 验证器只证明它实际执行过的那一步。
5. **工具和技能的“发现”必须像代码一样被测。** ServiceLoader 的 provider 名字写错，生产不会报错，只会少一个工具。
6. **会话恢复不是把 JSON 读回来。** 事件序号、终止事件、进行中状态和日志生命周期都要一起恢复；`run.finished` 只结束一轮，不结束会话。
7. **Trace 很有用，也很敏感。** 请求体里有完整任务、上下文和工具输出，不该和 API key 一起被记录。

## 1. 长什么样

一次任务启动后，`AgentService` 建一个独立工作区，`AgentLoop` 每个 turn 固定走十个阶段：

`INTAKE → CONTEXT → SKILLS → PLAN → MODEL → TOOLS → OBSERVE → VERIFY → COMPACT → CHECKPOINT`

模型只负责提案：调用哪个工具、写什么文件、什么时候停。文件真的落盘，`javac` / `python3` / `node` / `go` 真的执行，输出再作为 tool message 回灌给模型。默认最多 10 个 turn。

{{< fig "diagrams/01-architecture.svg" "Jade 的运行结构：Web/CLI 只观察；AgentService 管 run 生命周期；AgentLoop 管十阶段循环；工具和技能注册表提供能力；工作区与验证器负责把“说了”变成“做了”。" >}}

这个结构有两个刻意的选择。

第一，**模型不接触宿主机**。它只能拿到 `Workspace` 包装过的路径；`read_file`、`write_file`、`update_plan` 都走同一层。第二，**验证不靠模型自述**。`compile_code` / `run_code` 执行的是真实进程，验证结果写成事件，也写进 `RunSnapshot`。

## 2. 坑一：`MAX_STEPS` 会掩盖两种完全不同的失败

这是最容易被误判的一个坑。

第一次跑 Go 快排，任务结束在 `MAX_STEPS`，10/10 轮，48,272 tokens。看起来像“代码没写出来”。打开工作区才发现，`go build`、`go test`、`go run` 全部通过，模型甚至补了随机数组测试；它只是没来得及写最终回答。

把上限调到 12 再跑一次，9/12 轮，`SUCCEEDED`，200 个随机数组与 `sort.Ints` 结果一致。

同一种终态，换到 Spring Boot 就完全是另一回事。`run-d28944ce` 也是 10/10，但验证结果是 `Maven compile failed`，真实错误是：

```text
java.nio.file.FileSystemException:
/Users/caizhensheng/.m2/repository/org/springframework/boot/spring-boot-maven-plugin/3.5.7:
Operation not permitted
```

这不是“再多给几轮就能写好”的问题。Maven 缓存不可写、网络被拒、JDK 临时目录越界，任何一项都会让构建失败。

{{< fig "diagrams/02-max-steps-triage.svg" "同样是 MAX_STEPS：Go 快排的工件已经成立，缺的是最终回答；Spring Boot 的工件未成立，根因在沙箱边界。先读 verification、events.jsonl 和工作区，再决定补轮数还是修环境。" >}}

现在我的检查顺序固定为：

1. 看 `RunSnapshot.verification`，它是最后一个真实命令的结论。
2. 看 `.agent/events.jsonl` 的 `tool.end`，确认最后执行了什么。
3. 进工作区手动跑构建/测试，确认工件是否存在。
4. 最后才看 `MAX_STEPS` 和 token 数。

`MAX_STEPS` 是“循环结束”，不是“代码失败”。

## 3. 坑二：SDK 下载到工作区，验证器把 SDK 当项目

失败的 Go 任务里，模型没有找到系统 Go，于是把 Go 1.22.5 的 67,305,977 字节 tarball 下载到工作区：

```text
agent-workspaces/run-.../.toolchain/go/
```

然后执行：

```bash
gofmt -l . && go vet ./... && go test ./... && go run .
```

第一段就炸了。`gofmt -l .` 递归扫到了 Go SDK 自带的测试数据，打印 756 个文件，并在故意写坏的示例上退出 2：

```text
test/syntax/semi5.go:13:2: expected '}', found 'EOF'
```

因为用了 `&&`，后面的 `go vet`、`go test`、`go run` 一次都没执行。更隐蔽的是第二层：自动验证器不认识 `.go`，却在 `.toolchain` 里发现了 `.js`，于是选择 JavaScript，并对 SDK 模板文件执行 `node --check`：

```text
const sampleIDs = [{{range .SampleTypes}}'{{.}}', {{end}}];
SyntaxError: Unexpected token '{'
```

这两层错误都不是快排代码的问题。修复分三块：

- `Workspace.SKIP_DIRS` 增加 `.toolchain`、`.sdk`、`.sdkman`、`.nvm`、`.rustup`、`.cargo`、`vendor`、`third_party` 等目录。
- 源码扫描、树展示、验证器共用同一套跳过规则，不能各写一份。
- 系统提示明确禁止把 SDK/toolchain 装进工作区；Go 运行用宿主已安装的 `go`。

```java
private static final Set<String> SKIP_DIRS = Set.of(
    ".git", ".agent", ".agent-build", "target",
    ".toolchain", ".sdk", ".sdkman", ".nvm",
    ".rustup", ".cargo", "vendor", "third_party"
);
```

这里还有一个反直觉点：`gofmt -l .` 不是编译检查，它会扫描所有 Go 文件，包括 SDK 的坏样例。对项目做格式检查时，应该只传项目源码，不要传整个工作区。

## 4. 坑三：沙箱只做写隔离，Maven 和 Spring Boot 连环失败

最初给 `CommandRunner` 加的是 macOS `sandbox-exec` 写监狱：允许读，只允许写工作区、`.agent-build/tmp` 和 `/dev/null`；网络默认关闭。

这个策略对 `javac`、`python3`、`node` 是够的，对 Maven/Spring Boot 不够。

{{< fig "diagrams/03-sandbox-boundary.svg" "沙箱的四道边界：工作区写入、Maven 共享缓存、JVM 临时目录、网络与本地端口。Spring Boot 重试实际撞到了后三者；读取仍宽放，所以它不能被描述成容器级隔离。" >}}

| 资源 | 默认策略 | 实际后果 | 处理 |
|---|---|---|---|
| 工作区 | 允许写 | 源码和构建产物正常 | `writableRoot` 只给 run 目录 |
| `~/.m2` | 只读 | 插件目录 `Operation not permitted` | 预热缓存，或 `-Dmaven.repo.local` 指到工作区 |
| Maven 网络 | 拒绝 | `curl` 返回 `000`，依赖下载失败 | 需要时显式开 `AGENT_SANDBOX_NETWORK=true` |
| `java.io.tmpdir` | 拒绝 | Tomcat/Jansi/Maven 临时文件失败 | `JAVA_TOOL_OPTIONS=-Djava.io.tmpdir=...` |
| `127.0.0.1:8080` | 需要 bind | Spring Boot 启动失败或端口冲突 | 让应用用 8080，Jade 退到 8081 |

沙箱修好后，还要注意“成功”的定义。`run_command` 的退出码可能是 0，因为 shell 里后面的命令成功了；前一条写失败不代表最后一条会失败。测试必须直接断言文件是否存在，而不是只看 exit code。

最后的实现是 `CommandSandbox`：

- `AGENT_SANDBOX=off|auto|required`，现在默认 `off`，本地执行优先。
- `AGENT_SANDBOX_NETWORK=false`，显式启用写监狱时默认禁网。
- 写白名单只包含工作区、临时目录和 `/dev/null`。
- `JAVA_TOOL_OPTIONS` 把 JVM 临时目录也拉回工作区。
- 路径先 `toRealPath()` 再写进 profile，避免 `/var` 与 `/private/var` 的符号链接差异。

这里要诚实：它只是**写/网络策略**，不是 VM，不是容器。读取仍然宽放，恶意代码仍可能读到宿主文件；真要隔离，需要 VM 或独立用户/容器。

### 4.1 默认开启沙箱，本地服务反而起不来

沙箱方案刚落地时默认值是 `AGENT_SANDBOX=auto`。Spring Boot 会话继续追问“启动应用”时，模型已经写出了项目、测试也通过，但应用进程一 bind 就失败：

```text
AGENT_SANDBOX_ACTIVE=1
AGENT_SANDBOX_NETWORK=0
java.net.SocketException: Operation not permitted
curl http://127.0.0.1:8080/health -> HTTP 000
```

这不是端口冲突，也不是 Spring Boot 配置错误。`sandbox-exec` 的网络开关在进程创建时就固定了，模型在 shell 里 `export` 不能改；写监狱同时让 Maven 的共享缓存和 JVM 临时目录出问题。对一个“本地 coding agent”来说，默认把本地服务关进无网络监狱，和它的主要用途冲突。

因此默认值改成了 `off`：命令直接在宿主用户下运行，Maven、Spring Boot、localhost 端口都按本机行为工作。需要写/网络隔离时，显式设置：

```bash
AGENT_SANDBOX=auto       # 有 sandbox-exec 时套写监狱
AGENT_SANDBOX=required   # 没有沙箱就拒绝启动
AGENT_SANDBOX=off        # 默认，本地执行
```

改完后用同一个 Spring Boot 会话重跑：`AGENT_SANDBOX_ACTIVE=0`，应用监听 `8080`，`GET /health` 返回 200，`/`、`/hello`、`/hello?name=Spring` 也都恢复。

教训不是“沙箱没用”，而是默认值要匹配产品的主路径。安全能力应该在需要时开启，并用 `required` 在缺失时明确失败，而不是把本地开发默认变成不可用。

## 5. 坑四：多语言验证的三个细节

### 5.1 `node --check` 只检查第一个文件

最初 JavaScript 验证是：

```bash
node --check a.js b.js
```

`node --check` 只处理第一个参数。`a.js` 合法、`b.js` 非法时，命令仍然通过。

修法是对每个 `.js` 文件单独执行：

```java
for (Path source : sources) {
    CommandResult result = runner.run(
        List.of(node, "--check", relative), root, timeout);
    if (!result.success()) return failure;
}
```

测试里写了 `a.js` 合法、`b.js` 非法，断言必须失败并包含 `b.js`。

### 5.2 语言检测会选中“文件最多的语言”

`detect()` 统计 `.java`、`.py`、`.js`、`.go` 的数量，取最多的；数量相同按声明顺序。一个工作区混入 SDK 的 `.js` 后，JavaScript 会被误选。

所以现在只允许一个主语言，且语言检测只扫描项目文件；显式传 `language` 时优先于自动检测。跨语言项目要么拆工作区，要么明确指定入口。

### 5.3 无 `go.mod` 时 `go build ./...` 会失败

Go 1.27 默认模块模式，没有 `go.mod` 时：

```text
pattern ./...: directory prefix . does not contain main module or its selected dependencies
```

修法有两条：自动生成最小 `go.mod`，或在无模块时给 `go build`/`go run` 加 `GO111MODULE=off`。项目最后两条都保留了：优先生成 `go.mod`，老项目则走兼容模式。

## 6. 坑五：工具/技能“发现”不是有文件就行

工具注册用了双通道：

- 内置工具在 `BuiltInTools` 里显式注册。
- 外部工具用 `ServiceLoader<ToolProvider>` 发现。

测试里踩了一个很典型的坑。ServiceLoader 的服务文件写的是 `EchoToolProvider`，实际嵌套类是 `EchoProvider`，编译直接报：

```text
ToolRegistryTest.java:[41,17] 找不到符号
  类 EchoToolProvider
```

生产代码不会因为名字写错而报错，只会少一个 provider。因此注册表必须有：

- 重名拒绝。
- 外部 provider 与内置工具冲突拒绝。
- 发现数量断言，而不是只断言“能跑”。
- 一个真实 `META-INF/services` 测试文件，覆盖发现路径。

技能是另一套：`SKILL.md` 在启动时扫描，解析 frontmatter；目录清单进系统提示，按任务关键词选 top 2 全量注入；模型还可以用 `load_skill` 按需加载。技能不是“有 markdown 就会生效”，它同时依赖发现、排序和注入。

## 7. 坑六：验证器只证明编译，不证明运行

最早 `run_java` 在编译成功后就把 `verified` 置为 true，然后才启动 Java 进程。结果是：如果程序运行时报错，工具会返回错误，但模型可以立刻结束，验证器看到 `verified && !dirty`，说“已经验证过”，最终状态仍可能是 `SUCCEEDED`。

这暴露了一个边界：**编译、运行、测试是三件事**。

要真正收口，至少要做到：

- `run_java` / `run_code` 的 exit code 非 0 不标 verified。
- 任务要求执行时，验证门必须包含真实运行。
- 任务要求测试时，测试命令必须显式执行；没跑测试不能声称测试通过。

本项目目前仍停留在“编译成功就标 verified”，运行失败可以被模型忽略。这一条还没有完全修完，先把它写进坑里，避免下一次又把 `SUCCEEDED` 当成行为正确。

这也是 `MAX_STEPS` 之外的第二类误判：`SUCCEEDED` 也可能只代表“编译过”，不代表“行为对”。

## 8. 坑七：会话恢复的顺序问题

会话保留最初只存在内存里。重启后 `/api/runs` 为空，但每个工作区其实已经有：

```text
.agent/transcript.json
.agent/events.jsonl
.agent/final.md
```

恢复时踩了两个顺序问题。

第一，`AgentLoop` 在 `finally` 里先持久化、再发 `run.finished`、再关闭日志。这样写出的 `events.jsonl` 里没有终止事件，恢复时无法判断 run 是正常结束还是中断。修法是先持久化快照，再写终止事件，最后再持久化一次事件日志。

第二，恢复 `RUNNING` 状态不能原样信。服务重启后旧 run 不可能还在跑，必须改成 `FAILED`，并写清楚“Run was interrupted by a server restart”。

恢复出来的是只读历史，还是可以继续追问？最初是只读，后来改成了连续会话。这个取舍放在下一节讲。

### 8.1 连续追问：`run.finished` 不是会话终点

把“恢复出来能看”改成“恢复出来能继续追问”时，真正的问题不是界面加一个输入框，而是事件流的生命周期。

旧实现里，`AgentLoop` 每一轮 `finally` 都关闭 `RunEventLog`，UI 收到 `run.finished` 就关闭 EventSource。于是：

- 第二轮没有地方写事件，浏览器也收不到。
- `run.finished` 被当成整个会话的终点，但它其实只代表“这一轮用户请求结束”。
- 恢复出来的日志是关闭状态，直接追加会丢事件，重新打开又可能和 UI 的 `Last-Event-ID` 对不上。

修法是把“轮”和“会话”分开：

- `run.finished` 每轮都发，但日志不关；关闭只发生在服务停机。
- 恢复的日志先保持关闭；收到 follow-up 时 `open()`，从旧序号继续追加。
- UI 收到 `run.finished` 不再断开 SSE；收到 `turn.started` 才重置本轮 step/phase/plan，并在时间线插入用户消息。
- 跨轮累积 usage，单轮重置验证状态；transcript 追加而不是覆盖。
- 浏览器重连时带 `?after=<lastSeq>`，避免历史事件重复插入。

验证用的是一段真实的 Java 会话：同一个 `run-c66e8593`，先让程序打印 `jade-v1`，追问改成 `jade-v2`，服务重启后再追问改成 `jade-v3`。三轮共用 `run-20261115-171012-47495a` 工作区，最终文件、transcript 和 73,139 tokens 的累计 usage 都对得上。后端集成测试也覆盖了“关闭服务 -> 恢复 -> 追问”的路径。

### 8.2 审批卡不能藏在滚动区里

权限流程第一次联调时，后端状态已经是 `WAITING_APPROVAL`，`GET /api/runs/<id>/permissions` 也返回了 pending 请求，但浏览器里看不到任何批准按钮。现场很容易误判成 SSE 丢了事件。

实际原因是卡片被 `append` 到 Live Trace 时间线的末尾，而时间线本身有最大高度和独立滚动条。窗口没有滚到底时，卡片就在可视区下面；`scrollTimeline(true)` 也没有可靠地把它带出来。后来把卡片 `prepend` 到时间线顶部，仍会遇到下一个问题：当页面顶部被 run header 和 phase card 占满时，卡片标题能看见，操作按钮却落在 950px 视口之外。

最终修法不是在滚动区里继续调 `scrollTop`，而是把审批卡移出时间线：

- `showPermissionCard` 把卡片直接挂到 `document.body`，不再作为 timeline 的子节点。
- CSS 用 `position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%)`，配 `max-height: calc(100vh - 48px); overflow: auto`。
- 卡片带 `role="dialog"` 和 `aria-live="assertive"`；按钮提交时禁用，避免重复批准。
- 刷新或恢复会话时仍用 `GET /permissions` 补拉 pending 请求，不能只依赖 SSE。

验证场景是真实 DeepSeek 生成 `src/Fixed.java`：第一次写文件弹 `FILE_WRITE`，选 `Allow for session`；第二次运行弹 `EXECUTE`，选 `Allow once`。两次审批都有 `.agent/permissions.jsonl` 审计记录，最终 stdout 为 `fixed-card-ok`，run 状态 `SUCCEEDED`。


## 9. 坑八：Trace 很有用，但别把 key 写进去

模型请求体 trace 是排障利器。UI 里每次 `llm.request` 默认折叠，点开才显示完整 JSON：`model`、`temperature`、`messages`、`tools`、`tool_choice`。

实现上有一个必须守住的边界：trace 只记请求体，不记 `Authorization` header。请求体里也可能有 API key 以外的敏感内容：完整任务、用户上下文、工具输出。因此：

- trace 默认折叠，不主动展开。
- 不把请求体写进普通日志。
- `.agent/events.jsonl` 和 run 目录按敏感数据管理。
- 结构化输出用 `response_format` / strict tool schema 约束格式，但不要把它当成业务正确性的证明。

## 10. 一份检查清单

| 现象 | 先看 | 不要做 |
|---|---|---|
| `MAX_STEPS` | verification、events、工作区 | 直接判定代码失败 |
| 语言选错 | SDK/依赖目录、扩展名统计 | 只看文件数量 |
| Maven 失败 | `~/.m2` 权限、网络、tmp | 只加 turn 上限 |
| 启动失败 | 端口、临时目录、bind 权限 | 只看编译日志 |
| 工具缺失 | ServiceLoader 服务文件、重名 | 只测内置工具 |
| 技能没生效 | frontmatter、排序、注入 | 只检查文件存在 |
| `SUCCEEDED` 但行为不对 | 是否真的 run/test | 相信模型自述 |
| 重启后 run 消失 | `run.finished`、恢复顺序 | 只恢复内存 Map |
| 会话不能追问 | 日志/SSE 是否在每轮关闭 | 每轮另起一个 run |

## 11. 后续

这篇不是一次性总结。项目里新增了 `docs/BLOG_MEMORY.md` 和项目级 `AGENTS.md`：以后在这个项目里遇到新的坑，先把“症状 → 根因 → 修法 → 对应章节”记进项目记忆，再更新这篇文章并重新发布。记忆只留在项目里，不写全局配置。

如果这篇能留下一个最有用的习惯，那就是：**状态、日志、工件，三者分开看。** Agent 说“完成了”不算证据，编译器、运行时和测试进程说完成，才算。
