---
title: "Coding Agent 的执行环境：SDK 污染、沙箱边界与多语言验证"
date: "2026-11-16T18:29:00+08:00"
summary: "把 Go SDK 下载进工作区后，验证器会把 SDK 里的 JavaScript 当项目源码；写沙箱能挡写，却挡不住 Maven 缓存、JVM 临时目录和本地端口。执行环境要按资源逐项建模。"
description: "复盘 Jade 的 SDK 目录污染、macOS sandbox-exec 写监狱、Maven/Spring Boot 沙箱连环失败，以及 node --check、语言投票、无 go.mod 的多语言验证细节。"
tags: ["harness", "sandbox", "codegen"]
slug: "coding-agent-sandbox"
---

> 系列：[总览](/posts/coding-agent-pitfalls.html) · [循环护栏](/posts/coding-agent-loop.html) · [执行环境](/posts/coding-agent-sandbox.html) · [会话与历史](/posts/coding-agent-sessions.html)

这篇拆自[总览](/posts/coding-agent-pitfalls.html)，讲模型代码真正落到宿主环境时，哪些边界会失效。

## 1. SDK 下载到工作区，验证器把 SDK 当项目

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


## 2. 沙箱只做写隔离，Maven 和 Spring Boot 连环失败

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

### 2.1 默认开启沙箱，本地服务反而起不来

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


## 3. 多语言验证的三个细节

### 3.1 `node --check` 只检查第一个文件

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

### 3.2 语言检测会选中“文件最多的语言”

`detect()` 统计 `.java`、`.py`、`.js`、`.go` 的数量，取最多的；数量相同按声明顺序。一个工作区混入 SDK 的 `.js` 后，JavaScript 会被误选。

所以现在只允许一个主语言，且语言检测只扫描项目文件；显式传 `language` 时优先于自动检测。跨语言项目要么拆工作区，要么明确指定入口。

### 3.3 无 `go.mod` 时 `go build ./...` 会失败

Go 1.27 默认模块模式，没有 `go.mod` 时：

```text
pattern ./...: directory prefix . does not contain main module or its selected dependencies
```

修法有两条：自动生成最小 `go.mod`，或在无模块时给 `go build`/`go run` 加 `GO111MODULE=off`。项目最后两条都保留了：优先生成 `go.mod`，老项目则走兼容模式。



---

系列其他文章：[总览](/posts/coding-agent-pitfalls.html) · [循环护栏](/posts/coding-agent-loop.html) · [会话与历史](/posts/coding-agent-sessions.html)
