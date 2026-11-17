---
title: "Jade P0 补齐计划：先让长会话活下来，再让只读工具并发"
date: "2026-11-17T13:25:00+08:00"
summary: "把两个 P0 拆成 8 个可回滚的切片：工具输出落盘、溢出后压缩重试、尾部保留、并行只读，每一步都有测试与停点。"
description: "Jade Phase 1 执行计划：S1–S8 的文件、接口、测试、验收、回滚与工时，约 3 个工程日。"
tags: ["harness", "planning", "concurrency"]
slug: "jade-p0-context-parallel-plan"
---

> 依据：Jade `5772293`；配套评估见[《DSH 与 Jade：一个本地 coding agent 该补什么》](/posts/dsh-vs-jade-gap-analysis.html)。完整计划文档在 [`docs/superpowers/plans/2026-11-18-dsh-gap-closure.md`](https://github.com/jasoncai0/Jade/blob/main/docs/superpowers/plans/2026-11-18-dsh-gap-closure.md)。

两个故障场景，决定了这轮只做两件事。

第一个：一次 `mvn test` 返回 42k 字符，Jade 只留前 30k。失败堆栈在最后 8k，模型看到的是「Tests run: 312, Failures: 0」，然后告诉你构建通过。第二个：模型同一轮给出 5 个 `read_file`，每个 200ms，墙钟时间 1 秒；如果并行，300ms 就够。

前者是正确性问题，后者是交互问题。两者都不需要重写循环，只需要在现有十阶段上开两个窄口子。下面是执行计划，不是概念设计。

## 0. 这轮只做 P0

**范围：** Phase 1 = S1–S8（上下文安全网 + 并行只读工具），约 3 个工程日。S8 之后停下报告，不自动进入 P1。

**五个已定决策：**

1. **Spill 保留：** 所有工具输出文件放在 `.agent/tool-results/<runId>/`，v1 不自动删除；文档给清理命令。备选是 7 天 / 每 run 100MB。
2. **Inline 窗口：** 头 20,000 + 尾 9,000 字符，总 30k，和今天的上限一致。备选 16k + 8k。
3. **溢出策略：** 每步一次压缩 + 一次重试；激进压缩可能丢旧细节。备选是快速失败，让用户新开会话。
4. **并行策略：** 上限 4；白名单 `list_files/read_file/search_text/load_skill`；写、未知、MCP 工具让整批退回串行。备选上限 8，或只开 `read_file`。
5. **回滚：** 一个切片一个 commit；保留 `parallelToolCalls` 设置，关掉就回到串行；spill 与 overflow recovery 各有开关。

**S8 后要报的四个数：**

- 120k 字符工具输出：全文可回读，inline 头尾正确，不硬失败。
- 模拟 `context_length_exceeded` 400：一次恢复后成功；第二次溢出给出可操作错误。
- 4 个 200ms 只读调用：并行 ≤300ms，串行约 800ms；5 个调用最大并发不超过 4。
- `mvn -q test`：73 个既有测试 + 新增测试全绿；无新依赖、无 SQL 迁移、无 SSE 改名。

## 1. 全局约束

- 不加运行时依赖；测试库只在 `pom.xml` 已有范围内。
- 保留 73 个既有 `@Test`；每个切片先写失败测试。
- MySQL 变更只增不删、幂等；本轮其实没有 SQL 变更。
- 不把 API key、`.env`、环境变量、完整文件正文写进 spill、SSE、日志或审计。
- spill 路径必须落在 run workspace 下；拒绝绝对路径、`..`、符号链接、NUL。
- 权限检查留在主循环线程；并行不能绕过 `PermissionManager`。
- 保持 `tool.start/tool.end/assistant/usage` 事件名；新事件只增不改。
- `mcp_<server>_<tool>` 命名不改。
- 一个切片一个 commit；不做顺手格式化。

## 2. Task 1：上下文安全网（P0）

**目标：** 一次上下文窗口错误之后，run 还能继续；大工具输出保留尾部；完整输出随时可回读。

**为什么先做：** 这是唯一一个会把「本来能成功的长任务」直接变成硬失败的缺口。它也顺手解决「测试错误在尾部，模型却猜错」这类问题。

### 2.1 工具输出落盘 + `read_tool_output`

工具结果超过 `toolInlineChars`（默认 30,000 字符，沿用现有 `MAX_TOOL_OUTPUT_CHARS`）时，把完整 UTF-8 文本写到：

```
<run-workspace>/.agent/tool-results/<runId>/<seq>-<tool>.txt
```

Inline 结果 = 头 20,000 字符 + 标记 + 尾 9,000 字符。标记格式：

```
... [output truncated: <originalChars> chars; full text:
.agent/tool-results/<runId>/<seq>-<tool>.txt; read with read_tool_output] ...
```

写入失败时保留今天的「只留头」行为，并追加 `[spill failed: <reason>]`；不能因为 spill 失败让工具调用失败。

新增 `read_tool_output` 内置工具：

- `path`：必须以 `.agent/tool-results/` 开头；
- `startChar`：0 基，默认 0，上限 5,000,000；
- `maxChars`：1..65,536，默认 16,384；
- 用 `BufferedReader.skip/read`，不整文件加载；
- 返回文本 + `{path, startChar, returnedChars, totalChars}`；
- 通过 `ToolContext.workspace()` 解析路径，强制前缀；不需要注入 store。

保留策略：先全部保留，文档写清理命令；不在这个任务里加后台回收。

**接口：**

```java
record ToolOutputRef(String relativePath, long bytes, int originalChars, boolean spilled) {}

final class ToolOutputStore {
    ToolOutputRef save(String runId, long seq, String toolName, String output);
    String read(String relativePath, long startChar, int maxChars);
    String inline(String output, ToolOutputRef ref);
}
```

`read` 对越界路径、非普通文件抛 `WorkspaceException`。

**验收：**

- 40k 输出触发 spill；inline 含前 20k、后 9k 和标记。
- 模拟测试失败的最后一行在 inline 里。
- `read_tool_output` 精确返回 30,000–31,000 字符；拒绝 `../`、绝对路径、符号链接。
- POSIX 下文件权限 owner-only；路径用 `CREATE_NEW`，不跟随链接。
- spill 失败时工具结果仍可用。
- 73 个既有测试不变。

**风险：** 磁盘增长（写文档、不静默删）；输出里可能有密钥（放在 `.agent`，不进 SSE）；Windows 路径（`Path.resolve` + 归一化前缀检查）；工具名进文件名（过滤 `[A-Za-z0-9_.-]`，截 40 字符，回退 `tool`）。

### 2.2 溢出识别 + 压缩重试

`LlmException` 增加可选 `code`：

```java
LlmException(String message, int statusCode, String code, Throwable cause)
String code()
boolean isContextOverflow()
```

`isContextOverflow()` 对 `context_length_exceeded`、`CONTEXT_WINDOW_EXCEEDED`，或消息匹配 `context length|maximum context|context window|too many tokens|token limit`（忽略大小写）返回 true。

`DeepSeekClient` 从非 2xx JSON body 里尽力解析 `error.code`/`error.type`，传给 `LlmException`；400/401 仍在 HTTP 层不重试。

`AgentLoop` 的模型调用包一层每步一次性的恢复：

1. 捕获 `LlmException`；
2. 若 `settings.overflowRecovery()` 且 `isContextOverflow()` 且未重试过：
   - 发 `context.overflow`，带 `{step, message, beforeTokens, beforeChars}`；
   - 调 `compactor.compactForOverflow(messages, summarizer)`；
   - 发 `context.compacted`，带 `{removed, microCompacted, beforeTokens, afterTokens, summaryTokens}`；
   - 重试同一次模型请求；
3. 否则重新抛出。

`ContextCompactor.compactForOverflow`：

- 所有超过 2,000 字符的 `TOOL` 消息裁成头 1,000 + 尾 500；
- 从最旧组开始丢，直到 system + 首 user + 最新 user 组（允许少于 3 组）；
- 用现有 `LlmContextSummarizer` 摘要被丢组（上限 1,200 token）；
- 非工具消息截到 4,000 字符；
- 仍超预算就返回 best effort，让重试决定。

`AgentSettings` 加 `overflowRecovery`（默认 true，环境变量 `AGENT_OVERFLOW_RECOVERY`），在 `--check` 和 `/api/config` 暴露；保留旧构造器重载。

重试再失败，错误消息追加：`Context still exceeds the model window after one compaction; reduce the task or start a new session.`

**验收：**

- Fake `LlmClient` 先抛一次 `LlmException(...,400,"context_length_exceeded")`，再返回正常消息；run 成功；`context.overflow` 与 `context.compacted` 各一次。
- 第二次溢出直接失败，没有第三次调用。
- 非溢出 400 立即失败，不压缩。
- 429 仍走原 HTTP 重试，不被 overflow recovery 二次重试。
- 激进压缩保留最新 user 和首个 user 任务；摘要失败回退确定性 note。
- `--check` 打印 `overflowRecovery=true/false`。

**风险：** provider 错误文本不统一（先看 code，日志只记 code 不记完整 body）；激进压缩会丢细节（事件日志必须可见）；死循环（每步硬编码一次，测试覆盖）。

### 2.3 循环边界保留尾部

把 `AgentLoop.java:508` 的 `truncate(result.output(), MAX_TOOL_OUTPUT_CHARS)` 换成 `ToolOutputStore.inline(...)`；旧 `truncate` 只留给 `llm.request` 请求体和事件字段。

- 不 spill 的输出原样保留。
- spill 的输出头 20k + 尾 9k。

**验收：** 35k 输出同时含第一行和最后一行的错误；29k 不变；`tool.end` 的 SSE payload 与模型消息用同一段 inline 文本。

## 3. Task 2：并行只读工具（P0）

**目标：** 一轮里所有工具都是只读时，最多 4 个并发；否则走现有串行路径。

**设计：**

给 `Tool` 加：

```java
default boolean concurrencySafe() { return false; }
```

只在 `ListFilesTool`、`ReadFileTool`、`SearchTextTool`、`LoadSkillTool` 覆盖为 true。

不要用 `mutatesWorkspace() == false` 当谓词：`compile_code`/`run_code`/`compile_java`/`run_java` 没有覆盖它，但会改构建产物或验证状态。

`ToolRegistry.concurrencySafe(String name)`：未知工具返回 false。

`AgentLoop.executeTools` 拆三段：

- **Phase A（主线程）**：应用 `maxToolCallsPerStep`、解析参数、发 `tool.start`、鉴权、构造 `PreparedCall`。
- **Phase B（并行）**：仅当 `prepared.size() > 1`、全部 `concurrencySafe`、且 `prepared.size() <= 4`，提交到固定 4 线程池；否则走现有串行代码。worker 只调 `tools.execute(name, args, context)`，返回 `ToolOutcome`。不要在 worker 里调 `context.setNetworkAllowed`、`markDirty`、`markTouched`、`markVerified`、`updatePlan`、`refreshFiles`。
- **Phase C（主线程）**：按原顺序加 tool 消息、发 `tool.end`、更新统计、最后调一次 `context.refreshFiles()`。

worker 抛异常时按 `ToolRegistry.execute` 的语义转成 `ToolResult.error`，不打断兄弟调用。取消时：提交前 `control.checkCancelled()`；worker 启动前检查 `control.isCancelled()`；已启动的读等待完成，不 interrupt。

**接口：**

```java
record PreparedCall(int index, ToolCall call, Map<String,Object> args) {}
record ToolOutcome(int index, ToolResult result, long durationMs) {}
```

`Tool`/`ToolSpec` 的 JSON schema 不变。

**验收：**

- 两个慢只读 fake tool 用 `CyclicBarrier` 证明在任一返回前都进入；耗时低于串行基线。
- 5 个只读调用最大并发不超过 4（计数器断言）。
- 批次里有一个写工具时整批串行（断言无重叠）。
- 完成顺序不同，tool 消息与 `tool.end` 仍按模型顺序。
- 被拒绝的权限在 worker 启动前就拒绝；不占 worker 槽。
- worker 异常变成 error tool result，兄弟仍返回。
- 提交前取消不启动任何 worker。
- 73 个既有测试通过。

**风险：** `ToolContext` 共享，白名单工具不能碰可变 helper；`RunEventLog.emit` 是 synchronized，事件安全，顺序由 Phase C 保证；`SearchTextTool` 用的 `Workspace` 除 root 外无状态，需确认没有缓存；MCP 工具保持串行，stdio 请求/响应关联未证明线程安全。

## 4. S1–S8 执行切片

每个切片一个 commit；超过估算 50% 就停下报告，不扩范围。每片都按「先写失败测试 → 最小实现 → 片内测试 → `mvn -q test` → commit」走。

| 切片 | 交付物 | 主要文件 | Gate | 工时 |
|------|--------|----------|------|------|
| S1 | Spill store，不接线 | `ToolOutputStore.java`、`ToolOutputStoreTest.java` | `ToolOutputStoreTest` | 3h |
| S2 | `read_tool_output` 工具 | `ReadToolOutputTool.java`、`BuiltInTools.java` | `ReadToolOutputToolTest,BuiltInToolsTest` | 3h |
| S3 | 接入循环，保留尾部 | `AgentLoop.java`、`AgentLoopSpillIntegrationTest` | `AgentLoopSpillIntegrationTest,AgentLoopTest` | 3h |
| S4 | 模型错误码 | `LlmException.java`、`DeepSeekClient.java` | `DeepSeekClientErrorCodeTest` | 2h |
| S5 | 激进压缩 | `ContextCompactor.java`、`AgentSettings.java`、`AppConfig.java` | `ContextCompactorTest` | 3h |
| S6 | 一次性溢出恢复 | `AgentLoop.java`、`AgentLoopOverflowRecoveryTest` | `AgentLoopOverflowRecoveryTest` | 3h |
| S7 | 并发元数据 | `Tool.java`、`ToolRegistry.java`、四个内置工具 | `ToolConcurrencyTest` | 2h |
| S8 | 并行批执行 | `AgentLoop.executeTools`、`ParallelToolExecutionTest` | `ParallelToolExecutionTest,AgentLoopTest` + 全量 | 5h |

合计 24h，约 3 个工程日。任务级估算仍是 Task 1 两天 + Task 2 一天。

### S1 — Spill store（3h）

`ToolOutputStore` 构造参数是 run workspace `Path`。`save` 建 `.agent/tool-results/<runId>/`，`CREATE_NEW` 写 UTF-8，POSIX 下设 owner-only，返回 `ToolOutputRef`。`inline` 对 >30,000 返回头 20k + 标记 + 尾 9k，否则原样。`read` 只读请求窗口，拒绝越界路径、`..`、绝对路径、NUL、符号链接、非普通文件。

测试：40k 触发；29,999 不触发；标记含字符数与路径；40k inline 首行/末行都在；工具名被清洗；spill 失败只报告不抛。

**回滚：** 删新类，没有生产引用。

### S2 — `read_tool_output`（3h）

注册为第 15 个内置工具。参数 `path` 必填、`startChar` 0..5,000,000、`maxChars` 1..65,536（默认 16,384）。通过 `ToolContext.workspace()` 解析，归一化后强制 `.agent/tool-results/` 前缀；返回文本和 `{path, startChar, returnedChars, totalChars}`。

测试：30,000–31,000 精确；默认窗口；start 超 EOF 返回空文本 + totalChars；`../`、绝对路径、符号链接、目录拒绝；未知路径拒绝。

**回滚：** 去掉注册，类可以留着不用。

### S3 — 接入循环（3h）

`executeTools` 在 `tools.execute` 之后、`truncate` 之前调 `spill.save(run.id(), eventSeq, call.name(), result.output())`；tool 消息与 `tool.end` 都用 `spill.inline(...)`。`truncate` 只留给请求体和事件字段。`save` 失败时保留头截断并追加失败原因。

测试：35k 首行/末行都在；29k 不变；`tool.end.data.output` 等于模型可见 inline；小输出不建文件；失败路径仍可用。

**回滚：** 去掉两个调用点，旧 `truncate` 路径完整。

### S4 — 模型错误码（2h）

`LlmException` 加 `code` 和 `isContextOverflow()`，旧构造器保留为重载。`DeepSeekClient` 最多读 8KB 非 2xx body，解析 `error.code`/`error.type`，不整包打日志。400/401 不重试；429/5xx 行为不变。

测试：JSON code、纯文本 body、429 仍重试、401 不重试、只有 message 的兜底。

**回滚：** `code` 默认 null，`isContextOverflow()` 返回 false。

### S5 — 激进压缩（3h）

`compactForOverflow` 按 2.2 的规则实现，返回现有 `Result` 类型。`AgentSettings` 加 `overflowRecovery`（默认 true，`AGENT_OVERFLOW_RECOVERY`），保留构造器重载，`--check` 打印。

测试：最新 user 与首个 user 任务保留；不强制 3 组下限；摘要失败回退确定性 note；200k 输入返回预算内或 best effort；普通 `compact()` 不变。

**回滚：** 方法留着不用；设置默认 true，但只有 S6 读。

### S6 — 一次性溢出恢复（3h）

抽出 `ChatResult callModel(...)`，Task 3 以后只改这一个调用点。捕获 `LlmException`；满足 `overflowRecovery && isContextOverflow() && !retried` 时发 `context.overflow`、压缩、发 `context.compacted`、重试一次。401/403/429 不重试；不递归。

测试：一次溢出后成功；事件各一次；第二次溢出失败；非溢出 400 立即失败；429 仍走 HTTP 重试；重试请求含最新 user。

**回滚：** `overflowRecovery=false`，回到今天的快速失败。

### S7 — 并发元数据（2h）

`Tool` 加 `default boolean concurrencySafe()`；四个只读工具覆盖 true；`ToolRegistry.concurrencySafe(name)`；未知 false。

测试：白名单精确；其他内置全 false；未知 false；`mutatesWorkspace` 不作为谓词。

**回滚：** 去掉覆盖，默认 false 即串行。

### S8 — 并行批执行（5h）

按 Phase A/B/C 重构 `executeTools`。worker 只调 `tools.execute`；不碰 `markDirty`/`markTouched`/`markVerified`/`updatePlan`/`refreshFiles`/`setNetworkAllowed`。提交前与 worker 启动前检查取消；已启动的读等待完成，不 interrupt。

测试：barrier 证明两个慢读重叠；5 个读最大并发 4；一个写工具整批串行；模型顺序保留；拒绝不占槽；worker 异常变 error；取消不启动 worker；73 个既有测试通过。

**回滚：** `parallelToolCalls=false` 保留串行分支；测试全绿后才默认 true。

## 5. S8 停点

跑完 S8 后只报数，不继续：

- 全量测试：`<n> tests, 0 failures`（预期 73 + 新增）。
- Spill smoke：120k 输出，全文可回读，inline 头尾正确。
- Overflow smoke：一次模拟 400，run 成功，事件各一次；第二次 400 给出可操作错误。
- Parallel smoke：4 × 200ms mock 读，串行约 800ms，并行 ≤300ms；5 个读最大并发 4。
- Diff：`pom.xml` 无依赖变化，无 SQL 迁移，无 SSE 破坏性改名。

## 6. 本轮不执行

MCP resources/prompts、MCP 重连状态机、OAuth；Skills catalog-only、chokidar 热更新；Linux/Windows 沙箱后端、默认开启；Cordis/profile/bundle/plugin market/桌面壳/WebSocket mux；多 provider 路由/failover/成本表；长期记忆/向量库；DSH 会话格式/zstd/FTS5/OTel；Schedule/Team/Workflow/Office/Browser；MySQL 重写；UI 框架更换。

## 7. 主要风险

- **spill 磁盘增长**：v1 只保留不删；先文档化清理命令，后续再加保留策略。
- **溢出识别是启发式**：provider 错误文本会变；code 优先，message 兜底；上线前用真实 key 手工验证一次。
- **激进压缩丢细节**：它是最后手段；事件日志必须写清 before/after，用户能看到为什么被裁。
- **并行共享状态**：白名单只允许无副作用的读；测试断言四个工具不碰可变 helper；MCP 永远串行。
- **顺序语义**：worker 完成顺序不保证；Phase C 统一按模型顺序回填，避免 transcript 与事件错位。

这份计划的目标不是「像 DSH」，是让 Jade 在长任务里不因为一个 400 或一个 40k 输出就前功尽弃，同时把多文件调研的墙钟时间压下来。做完这两件，再谈流式、fork 和子代理。
