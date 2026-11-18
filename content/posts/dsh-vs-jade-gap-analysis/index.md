---
title: "DSH 与 Jade：一个本地 coding agent 该补什么"
date: "2026-11-17T13:05:00+08:00"
summary: "把 DSH 的核心机制逐项对照 Jade：五个真正影响正确性与交互的差距，以及十个不该照搬的产品外壳。"
description: "DSH 0.2.2-rc.3 与 Jade 5772293 的能力矩阵：上下文安全网、并行只读工具、流式、子代理、fork，附源码行号与优先级。"
tags: ["harness", "architecture", "context-engineering"]
slug: "dsh-vs-jade-gap-analysis"
---

> 分析快照：Jade `5772293`，108 个 Java 文件、10,740 行、73 个 `@Test`。当前进度：Jade `1.4.0`（`1b5ce8a`），15 个内置工具、143 个 `@Test`，P0/P1 与 MCP resources、Skills 目录化、沙箱可见性、MCP 启动重试均已交付；逐项代码见第 8 节。DSH 版本：已安装的 `@deepseek-ai/dsh 0.2.2-rc.3`；另有一份 `0.2.2-rc.1` 解包源码只用于交叉核对，行号以 asar 安装版为准。分析日期：2026-11-18。

Jade 是一个单 jar 的本地 coding agent：十阶段循环、MySQL 事件持久化、权限审批、真实编译验证、Goal review。DSH 是一个 Cordis 插件树上的 harness：profile、bundle、子代理、workflow、桌面壳。

把两者放在一张表里，最容易得到的结论是「Jade 少了很多功能」。这个结论没有用。功能数量取决于产品边界，不取决于机制是否成立。真正值得问的是：**哪些缺失会让一次本来能成功的任务失败，或者让用户明显更累？**

这篇只回答这个问题。配套的 P0 执行计划在[《Jade P0 补齐计划》](/posts/jade-p0-context-parallel-plan.html)。

## 0. 结论

按用户价值排序，五个差距值得先补。**截至 2026-11-18，这五项都已落地**，代码在 Jade `1.4.0`：

1. **上下文安全网**（P0，已完成）：溢出后能压缩重试；工具输出能落盘并保留尾部。
2. **并行只读工具调用**（P0，已完成）：同一轮多个读操作不再排队。
3. **真流式输出**（P1，已完成）：模型 token 边生成边显示。
4. **子代理委派**（P1，已完成）：只读调研丢给子上下文，父上下文只收摘要。
5. **会话 fork**（P1，已完成）：从某个会话分叉出独立工作区，比较不同方案。

其余差距要么是产品层，要么工作量大而收益靠后：MCP resources、技能目录化、跨平台沙箱、插件市场、桌面壳、Schedule/Team/Workflow。**不建议把 DSH 的 Cordis 插件树、profile/bundle 或桌面壳搬进 Jade。**

## 1. 证据口径

- `J/` = `java-coding-agent/`
- `P/` = `/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/node_modules/@deepseek-ai/`
- `D/` = `P/dsh/docs/`

asar 内文件用 `read` 直接读；原生 `grep/find/glob` 不能进入 asar（报 `Not a directory`），批量检索走一个只读的 `asar-grep.js`。Jade 的代码、测试、`pom.xml`、git 状态都在本机仓库直接核对。

有五处结论明确标为未验证：DSH 部署层是否额外限制 `maxSteps`；fork 子代理继承父上下文前缀的精确源码行；DSH 测试门禁与覆盖率；DSH HTTP MCP 的 `tools/list_changed` 与会话续期；Jade 真实 DeepSeek 的溢出错误体与 SSE 分块格式。计划里用 mock server 验证，真实错误码上线前需要手工补验。

## 2. 能力矩阵

下表是 **2026-11-18 分析时（Jade `5772293`）的快照**，用来解释「为什么当时判定为缺口」。当前 `1.4.0` 的实现状态与代码见第 8 节，不要再用下表的「Jade 现状」判断今天的能力。

差距等级：**高** = 影响正确性或核心能力；**中** = 明显的能力/体验缺口；**低** = 实现差异，暂不影响使用；**无** = 对等，或双方都没有。

| # | 能力 | DSH 现状 | Jade 现状 | 差距 | 用户价值 | 优先级 |
|---|------|----------|-----------|------|----------|--------|
| 1 | Agent loop / turn-step | `turn()` 内循环 `step()`；step = 一次模型请求 + 工具调用（`P/dsh-agent-loop/src/agent.ts:276,333,441`；`D/architecture.md:83-106`） | 十阶段 `intake→context→skills→plan→model→tools→observe→verify→compact→checkpoint`（`J/src/main/java/io/github/jasoncai0/agent/agent/AgentLoop.java:136`） | 低 | 中 | — |
| 2 | 并行工具调用 | 仅 `isConcurrencySafe === true` 判并行，其余 exclusive；有界滚动池，默认上限 10（`P/dsh-tools/src/pipeline.ts:106`；`P/dsh-tools/src/batch.ts:187`） | 严格串行 `for (int i = 0; i < calls.size(); i++)`（`AgentLoop.java:462`）；无并发元数据（`J/.../tool/Tool.java:7-14`） | 高 | 高 | **P0** |
| 3 | 子代理委派 | 统一 activation；local child 有 durable Session；`maxDepth` 默认 1、`maxActiveSubagents` 默认 8；`spawn` 不继承父上下文（`D/subsystems/subagent.md:9-60`；`P/dsh-subagent/src/index.ts:196-197`） | 全库无 subagent/fork；README 明说没有 branch/fork/concurrent-chat（`J/README.md:626`） | 高 | 高 | **P1** |
| 4 | 上下文压缩 | `pressure` 与 `context-overflow` 两种触发；阈值 `min(window×0.8, budget−headroom 65,536)`；保留最近 16%；溢出重试 1 次（`D/subsystems/compaction.md:15-19,81`；`P/dsh-compaction-basic/src/config.ts:20-23,75,105-106`） | 只在每轮末尾 COMPACT；默认 token 预算 30k、字符 120k；丢组至少留 3 组；摘要上限 1200 token（`AgentLoop.java:312-334`；`ContextCompactor.java:23-26,54-65,84-96`） | 高 | 高 | **P0** |
| 5 | 工具结果落盘/裁剪 | 超 12,500 tokens 写 spill 文件，inline 留 head/tail + 回读路径；压缩前 pruner 默认 8192/4096/1024 字符（`P/dsh-spill-policy/src/index.ts:28`；`P/dsh-compaction-tool-result-pruner/src/config.ts:10-14`；`D/subsystems/spill.md:90-92`） | 超 30,000 字符只留前 30k，**丢尾部**（`AgentLoop.java:37,508,693-698`）；压缩时旧/新工具结果裁成头 3000 + 尾 1000（`ContextCompactor.java:23-26,76-79,168-186`） | 高 | 高 | **P0** |
| 6 | 真流式输出 | `StreamChunk` 闭集；adapter 一次调用 = 一次 provider attempt；空闲 5 分钟超时；失败统一 `LlmFailure`（`D/subsystems/llm-streaming.md:203-216,298-306`） | `LlmClient` 只有阻塞 `chat()`；`DeepSeekClient` 用 `BodyHandlers.ofString()` 且 `stream=false`（`J/.../llm/LlmClient.java:3-13`；`DeepSeekClient.java:78,134`） | 高 | 高 | **P1** |
| 7 | 会话 fork / 分支 | `SessionStore.fork(source, boundary?, childSessionId?)` 可在任意 turn 间分叉（`D/subsystems/session.md:711`；`D/architecture.md:167`） | 只有 resume/follow-up；`EventStore` 无 fork/copy；MySQL 无 parent 列（`J/.../history/EventStore.java:1-38`；`SchemaMapper.java:7-29`） | 中 | 中 | **P1** |
| 8 | 持久化 | append-only typed event log 是唯一真相源；JSONL + zstd；崩溃恢复补 interrupted turn（`D/subsystems/persistence.md:7,106-112,339-341`） | MySQL `jade_sessions`/`jade_events` + 本地 `transcript.json`/`events.jsonl`；启动恢复 200 个会话；RUNNING→FAILED（`SchemaMapper.java:7-29`；`RunArtifacts.java:31-32,59-113`；`RunState.java:59-78`） | 低 | 中 | — |
| 9 | 长期记忆 | **没有长期记忆系统**；会话日志 + `AGENTS.md` + spill 是持久状态；跨会话搜索默认关闭（`D/subsystems/session.md:5`） | 没有长期记忆；`AGENTS.md` + MySQL 会话 + `.agent` 文件（`J/README.md:616-629`） | 无 | — | — |
| 10 | 权限/审批 | 权限 = `(sandbox mode, approval policy)` preset；`ask/never`；只有 `allowed-once` 放行，其他 fail-closed（`D/subsystems/approval.md:21-28`；`D/subsystems/permission-presets.md:15-24`） | 四种模式 `balanced/strict/read-only/bypass`；ASK 阻塞 300 秒；once/session；`.agent/permissions.jsonl` 审计（`J/README.md:180-216`） | 低 | 中 | P2 |
| 11 | 沙箱 | 三平台后端：Linux bwrap/Landlock、macOS Seatbelt、Windows ACL；`read-only/workspace-write/danger-full-access`；`full/partial` 必须显式呈现（`D/subsystems/sandbox.md:5,11,20,30`） | 仅 macOS `sandbox-exec` 写监狱 + 可选网络；默认 off；读与进程共享宿主（`J/.../workspace/CommandSandbox.java:26-80`） | 高 | 高 | **P2** |
| 12 | MCP | tools + resources；stdio/HTTP；`mcp__server__tool`；凭据模板；指数退避重连（`P/dsh-mcp-resources/src/tools.ts:16-59`；`P/dsh-mcp-manager/src/index.ts:43-59`） | 只有 tools；命名 `mcp_<server>_<tool>`；无 resources/prompts/reconnect 状态机（`J/.../mcp/McpConnection.java:55,77`） | 中 | 中 | **P2** |
| 13 | Skills | 6 个根；catalog 只放 name+description；正文按需 inject；chokidar 热更新（`D/subsystems/skills.md:66-94,128`） | 3 个根、深度 3；top-2 全文预载进 system prompt；无 watcher（`J/.../skill/SkillRegistry.java:36-97`；`AgentLoop.java:98,120,642`） | 中 | 中 | **P2** |
| 14 | 模型路由/重试 | adapter 注册；`model.select()` 只作用下一个未准备 step；**无自动 failover**；normal 默认 5 次重试（`D/subsystems/llm-streaming.md:437-461`；`P/dsh-llm/src/retry-policy.ts:15-25`） | 单 `DEEPSEEK_MODEL`；429/5xx/超时重试 3 次；400/401 不重试（`DeepSeekClient.java:28,97-122,256-268`） | 低 | 低 | P2 |
| 15 | 可观测性 | session telemetry ledger/ops、OTel、token 投影、session inspector（`D/subsystems/session-telemetry.md:31-60`） | SSE 事件、`llm.request` 完整请求体、usage、MySQL 历史、Web trace 折叠（`J/.../web/WebServer.java:387-434`） | 低 | 中 | — |
| 16 | CLI / Web UI | WebSocket mux、Electron、headless、ACP、SDK（`D/subsystems/web-server.md:5,47`；`P/dsh-api-gateway/src/stream-protocol.ts:7`） | `check/ping/headless/serve` 四模式；本地 HTTP + SSE；1,879 行原生 JS；无鉴权、127.0.0.1（`WebServer.java:46,244,387-434`） | 低 | 中 | — |
| 17 | 测试/发布/扩展 | Cordis 插件树、profile/bundle、plugin_manager；测试源码不随 asar 发布（`D/architecture.md:15-43`；`D/tool-catalog.md`） | 26 个测试文件、73 个 `@Test`；ServiceLoader `ToolProvider`；shade 单 jar（`J/pom.xml`） | 低 | 低 | — |

## 3. 五个真正值得补的差距

### 3.1 上下文安全网（P0，已实现）

> 现状：`ToolOutputStore` + `read_tool_output` + 溢出压缩重试已上线，见 8.1。

Jade 在每轮 VERIFY 之后才压缩（`AgentLoop.java:312-334`），请求前不检查压力；`LlmException` 一律 `run.failed`（`:399-402`）。工具输出超过 30,000 字符时只保留前 30k（`:508,693-698`），而编译/测试错误通常在尾部。压缩器虽然对工具结果留了头 3000 + 尾 1000（`ContextCompactor.java:168-186`），但那是「已进入上下文之后」的二次裁剪，原始输出已经丢了。

长会话在第 N 轮突然 400，整个 run 失败，用户只能新开会话，丢失已验证的上下文。一次 `mvn test` 输出 40k 字符，尾部 10k 的失败堆栈被切掉，模型看到「前面都通过」，然后给出错误修复。想回看完整输出时，`.agent/events.jsonl` 只有被截断的版本。

补上之后，会话可以跨过模型窗口继续跑；溢出时自动「裁工具结果 → 摘要旧组 → 再试一次」，失败才报错。任何大输出都能通过 `read_tool_output` 按字符区间回读，尾部不丢。压缩事件里能看到 `before/after`、`spill` 路径、`overflowRetry`。

边界：不做 DSH 的图片 offload、不做 zstd 会话格式、不做跨模型容量重测；只做文本工具输出、单次溢出重试、本地文件落盘。

### 3.2 并行只读工具调用（P0，已实现）

> 现状：`Tool.concurrencySafe()` + 只读白名单 + 上限 4 的并行池已上线，见 8.2。

`AgentLoop.executeTools` 严格串行（`:462`）；`Tool` 没有 `concurrencySafe` 元数据（`Tool.java:7-14`）。一次「读 5 个文件 + 搜 3 个关键词」的调研要 8 个往返，墙钟时间随工具数线性增长。

补上之后，同一轮里的只读工具并发执行（上限 4），结果仍按模型给出的顺序回填；写文件、跑命令、问用户仍然串行。权限检查仍在主线程逐个做，拒绝/超时语义不变。

边界：只对白名单 `list_files/read_file/search_text/load_skill` 开启；MCP 工具本轮不并行（共享 stdio 连接，线程安全未验证）；只要批次里有一个非白名单工具，整批退回串行。

### 3.3 真流式输出（P1，已实现）

> 现状：`LlmClient.stream` + DeepSeek SSE + Web `assistant.delta` 已上线，见 8.3。

`LlmClient` 只有阻塞 `chat()`（`LlmClient.java:3-13`）；`DeepSeekClient` 用 `BodyHandlers.ofString()` 且 `stream=false`（`:78,134`）。Web 的 SSE 只转发 agent 事件，模型 token 不是逐字到达。

长答案/长 reasoning 期间界面只有「asking the model」，看起来像卡死；用户不能在中途看到方向错了就取消，只能等整包返回。补上之后回答逐字出现，reasoning 可选折叠，取消时立即关闭 HTTP 流。

边界：只做 DeepSeek/OpenAI-compatible SSE；不做 replay state、image chunk、adapter 热替换；Web 仍用现有 SSE 通道。

### 3.4 子代理委派（P1，已实现）

> 现状：`delegate_task` + 只读白名单子 run 已上线，见 8.4。

全库没有 subagent/fork（`README.md:626`）；所有调研都堆在父 transcript 里。`AgentService` 每个 run 一个虚拟线程，但没有父子 run 关系（`AgentService.java:87`；`SchemaMapper.java:7-29`）。

「读 20 个文件确认一个调用链」会把父上下文塞满，压缩后细节丢失；也无法并行调研两个方向。补上之后，父 agent 调 `delegate_task`，子 agent 在只读工具集里调研，返回 8k 以内的摘要 + 证据路径；完整 transcript 落盘可查。父上下文只增加一条工具结果。

边界：v1 子代理只读（不能写、不能跑命令、不能再委派）；同 workspace 读；最多 2 个并发、深度 1、10 分钟超时；不做 DSH 的 fork 子代理、agent team、workflow。

### 3.5 会话 fork（P1，已实现）

> 现状：`AgentService.fork` + `WorkspaceFork` 已上线，见 8.5。

只有 resume/follow-up 继续同一 run（`AgentService.java:397-472`）；`EventStore` 没有 fork/copy，MySQL 没有 parent 列（`EventStore.java:1-38`；`SchemaMapper.java:7-29`）。README 明确说没有 branch/fork（`:626`）。

想比较「方案 A / 方案 B」只能覆盖原会话，或者手工复制整个目录；失败的探索会污染主会话的 transcript 和 workspace。补上之后，从已完成会话 fork 出独立 run：复制 transcript 前缀 + 复制 workspace（排除 `.agent/.git/target/node_modules`），父会话不变。

边界：v1 只支持「终态会话 fork 到当前末尾」，不支持任意 turn 中间分叉；workspace 复制有 200MB/20k 文件上限；不做 git worktree。

## 4. 其余差距：有，但排在后面

**MCP resources（P2，已实现）。** 分析时 Jade 只有 `initialize → tools/list → tools/call`（`McpConnection.java:55,77`）；现已按 server 的 `capabilities.resources` 注册 `list_resources`、`list_resource_templates`、`read_resource` 三个工具，支持游标分页与 text/blob 内容，见 8.6。prompts / sampling / OAuth 仍未实现。

**Skills 目录化（P2，部分实现）。** Jade 仍扫 3 个根，但新增 `AGENT_SKILL_PRELOAD=false` 的 catalog-only 模式；单条正文超过 4,000 字符时只注入 name+description，并要求 `load_skill` 回读。DSH 的 chokidar 热更新仍未做，见 8.7。

**跨平台沙箱（P2，部分完成）。** 仍只有 macOS `sandbox-exec` 写监狱，默认 off，读与进程共享宿主（`CommandSandbox.java:26-80`）；DSH 有 Linux bwrap/Landlock、macOS Seatbelt、Windows ACL（`D/subsystems/sandbox.md:5,30`）。本轮先做了可见性：`/api/health` 暴露模式与网络状态，Web 顶部 banner 明示默认不隔离，见 8.8；Linux/Windows 后端（C 项）暂缓。

## 5. 非目标

以下能力**不是本轮要补的差距**：

1. **Cordis 插件树 / profile / bundle / plugin market**：Jade 的 ServiceLoader 已够，重写会破坏单 jar 定位。
2. **桌面 Electron 壳 / HMR / 多窗口 / WebSocket mux**：产品外壳；本地 HTTP + SSE 够用。
3. **多 provider 自动 failover / 企业路由**：DSH 也没有自动 failover；Jade 单 provider 不是缺陷。
4. **多租户 / 登录 / 鉴权**：Jade 明确本地单用户、127.0.0.1；加鉴权是另一个产品。
5. **图片/视觉输入**：Jade 的定位是代码/文本；本轮不做。
6. **DSH 会话格式 / 迁移链 / zstd / FTS5**：Jade 用 MySQL，迁移成本高、收益低。
7. **OTel / 遥测 / 审计平台**：本地 trace 已够；不做数据外发。
8. **Schedule / Agent Team / Workflow / Office / Browser 工具**：DSH 的产品层能力；Jade 先把 coding loop 做对。
9. **长期记忆 / 向量库**：DSH 也没有；不要为了「对齐」而引入。
10. **重写权限模型为 DSH preset**：Jade 的四模式 + 审计已覆盖；最多后续把 sandbox mode 与 approval 打包展示。

## 6. Jade 已有的优势

对齐的时候容易把这些丢掉：

- **验证门**：Java/Maven、Python、JS、Go 的真实编译/语法检查，shell 会使已验证失效（`AgentLoop.java:342`；`Verifier.java:47-222`）。DSH 未见等价的「每轮自动验证」内置门。
- **MySQL 持久化 + 启动恢复**：200 个会话、RUNNING→FAILED、SSE 重放（`AgentService.java:58,397-472`；`RunState.java:59-78`）。
- **Goal review**：无工具 JSON 复核，连续 3 次拒绝失败（`AgentLoop.java:346-376`；`LlmGoalReviewer.java:26-81`）。
- **单 jar、Jackson-only、无 Cordis**：安装/调试成本低，代码可读。
- **143 个测试**：覆盖循环、压缩、权限、流式、fork、子代理、MCP stdio/HTTP/资源、MySQL；比「功能多但测试不随产物发布」的 DSH 更容易本地回归。

## 7. 顺序与取舍

当时的执行顺序是：先做两项 P0，停下评估；再按「流式 → fork → 子代理」推进。**这条路线已经走完**：

1. **上下文安全网** —— 已交付（S1–S6）。
2. **并行只读工具** —— 已交付（S7–S8）。
3. **流式 → fork → 子代理** —— 已交付（Phase 2 / Phase 3）。

当前未做的是 C 项（Linux/Windows 沙箱后端）与 MCP prompts/sampling/OAuth。

四个已定的设计取舍：

- **fork 的 workspace**：复制，独立、可比较；带上限，不共享。
- **子代理权限**：v1 只读；写入型需要权限继承/隔离设计，风险高。
- **spill 保留**：先按 run 保留全部，文档写清清理命令；不做后台回收。
- **MCP 命名**：Jade 的 `mcp_<server>_<tool>` 不改，避免破坏已有配置。

具体到每个文件、每个测试的 P0 执行计划，见[《Jade P0 补齐计划》](/posts/jade-p0-context-parallel-plan.html)。完整评估文档在 [`docs/DSH-GAP-ANALYSIS.md`](https://github.com/jasoncai0/Jade/blob/main/docs/DSH-GAP-ANALYSIS.md)。

## 8. TODO 与实现状态（2026-11-18，Jade 1.4.0）

下表把原文的优先级清单当成 ToDo 表逐项对账。**已完成**的项附核心实现（每项 ≤20 行，省略异常、日志与参数校验）；**部分完成**与**未做**的项写明缺口。测试总数 143，`mvn test` 0 failures。

<table style="table-layout:fixed;width:100%">
<thead>
<tr><th style="width:15%;white-space:normal">ToDo 项（优先级）</th><th style="width:27%;white-space:normal">状态与证据</th><th style="width:58%;white-space:normal">核心实现（Java，≤20 行）</th></tr>
</thead>
<tbody>
<tr>
<td><strong>上下文安全网</strong>（P0）</td>
<td><strong>已完成</strong> · <code>5a50abc</code>、<code>edb5d9c</code> · 工具输出落盘 + 溢出压缩重试 + 同名工具分文件</td>
<td><pre style="white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;margin:0;font-size:0.78em"><code>// ToolOutputStore.java:63
public ToolOutputRef save(..., String callId, String output) {
    String safeCall = "-" + safeSegment(callId, "call", 48);
    Path target = directory.resolve(seq + "-" + tool + safeCall + ".txt");
    Files.writeString(target, output, CREATE_NEW, UTF_8);
}
// AgentLoop.java:767
if (!settings.overflowRecovery() || !e.isContextOverflow()) throw e;
var recovery = compactor.compactForOverflow(messages, summarizer);
messages.clear(); messages.addAll(recovery.messages());</code></pre></td>
</tr>
<tr>
<td><strong>并行只读工具</strong>（P0）</td>
<td><strong>已完成</strong> · <code>a0214d3</code> · 只读白名单并发，写/命令/审批仍串行</td>
<td><pre style="white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;margin:0;font-size:0.78em"><code>// AgentLoop.java:604
boolean canParallel = executable &gt;= 2
    &amp;&amp; prepared.stream().filter(PreparedCall::executable)
        .allMatch(i -&gt; tools.concurrencySafe(i.call.name())
                   &amp;&amp; !i.permission.networkAllowed());
if (canParallel) runParallel(prepared, context, control);
else for (PreparedCall item : prepared) execute(item);
// runParallel: 上限 4
int workers = Math.min(4, runnable.size());</code></pre></td>
</tr>
<tr>
<td><strong>真流式输出</strong>（P1）</td>
<td><strong>已完成</strong> · <code>1c49e2a</code>、<code>b4852ec</code>、<code>2d2644e</code> · SSE + 增量总线 + Web 逐字渲染</td>
<td><pre style="white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;margin:0;font-size:0.78em"><code>// DeepSeekClient.java:156
HttpResponse&lt;InputStream&gt; response = http.send(
    newRequest(endpoint, body, "text/event-stream"),
    BodyHandlers.ofInputStream());
if (status == 400 &amp;&amp; includeUsage &amp;&amp; mentionsStreamOptions(message)) {
    includeUsage = false; continue;   // 去掉 stream_options 重试
}
ChatResult result = parseStream(response.body(), listener);</code></pre></td>
</tr>
<tr>
<td><strong>子代理委派</strong>（P1）</td>
<td><strong>已完成</strong> · <code>18bebe4</code> · 只读子 run、每父 2 并发、深度 1、父上下文只收摘要</td>
<td><pre style="white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;margin:0;font-size:0.78em"><code>// SubagentService.java:40
private static final Set&lt;String&gt; READ_ONLY_TOOLS = Set.of(
    "list_files", "read_file", "search_text", "load_skill", "reflect");
// SubagentService.java:66
if (parent.state().isSubagent()) throw new IllegalArgumentException("max depth is 1");
Semaphore slot = slots.computeIfAbsent(parent.id(),
    ignored -&gt; new Semaphore(MAX_ACTIVE_PER_PARENT));
acquired = slot.tryAcquire(timeout, TimeUnit.SECONDS);</code></pre></td>
</tr>
<tr>
<td><strong>会话 fork</strong>（P1）</td>
<td><strong>已完成</strong> · <code>5d9977e</code> · 复制 workspace + transcript 前缀 + 事件历史，父会话不变</td>
<td><pre style="white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;margin:0;font-size:0.78em"><code>// AgentService.java:239
if (parent.running() || !parentSnapshot.terminal())
    throw new IllegalStateException("Only a finished session can be forked");
WorkspaceFork.copy(source, target.root());
List&lt;ChatMessage&gt; messages = RunArtifacts.loadMessages(source);
long forkSeq = parentEvents.stream().mapToLong(AgentEvent::seq).max().orElse(0L);</code></pre></td>
</tr>
<tr>
<td><strong>MCP resources</strong>（P2）</td>
<td><strong>已完成</strong> · <code>854ba42</code> · 按 capabilities.resources 注册三个工具，支持分页与 text/blob</td>
<td><pre style="white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;margin:0;font-size:0.78em"><code>// McpManager.java:62
if (connection.resourcesSupported()) {
    for (McpResourceTool.Kind kind : McpResourceTool.Kind.values()) {
        String name = McpTool.toolName(server, McpResourceTool.suffix(kind), 0);
        registry.register(new McpResourceTool(server, connection, kind, name));
        resourceTools++;
    }
    status.put("resourceTools", resourceTools);
}</code></pre></td>
</tr>
<tr>
<td><strong>Skills 目录化</strong>（P2）</td>
<td><strong>部分完成</strong> · <code>9d321cb</code> · catalog-only 开关 + 4,000 字符正文上限；chokidar 热更新未做</td>
<td><pre style="white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;margin:0;font-size:0.78em"><code>// AgentLoop.java:727
if (!settings.preloadSkills()) {
    return "(catalog-only mode; call load_skill with a name...)";
}
if (skill.body().length() &lt;= 4_000) return skill.render();
return "### Skill: " + skill.name() + "\n" + skill.description()
     + "\n\n(full instructions are " + skill.body().length()
     + " chars; call load_skill to read them)";</code></pre></td>
</tr>
<tr>
<td><strong>沙箱可见性</strong>（P2）</td>
<td><strong>已完成</strong> · <code>0247b8d</code> · health 字段 + Web banner；默认仍 off，不假装隔离</td>
<td><pre style="white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;margin:0;font-size:0.78em"><code>// WebServer.java:136
data.put("sandbox", sandbox.modeName());
data.put("sandboxActive", sandbox.active());
data.put("sandboxNetwork", sandbox.networkEnabled());
data.put("sandboxDescription", sandbox.description());</code></pre></td>
</tr>
<tr>
<td><strong>MCP HTTP 重连</strong>（P2）</td>
<td><strong>已完成</strong> · <code>50f0777</code> · 启动 5 次指数退避；4xx / 非法 URL 不重试</td>
<td><pre style="white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;margin:0;font-size:0.78em"><code>// McpManager.java:105
int attempts = server.stdio() ? 1 : HTTP_CONNECT_ATTEMPTS; // 5
long delay = HTTP_RETRY_INITIAL_MS;                        // 500ms
for (int attempt = 1; attempt &lt;= attempts; attempt++) {
    try { connection.initialize(); return connection; }
    catch (RuntimeException e) {
        if (!retryable(e)) throw e;
        sleep(delay); delay = Math.min(delay * 2, 30_000);
    }
}</code></pre></td>
</tr>
<tr>
<td><strong>跨平台沙箱</strong>（P2）</td>
<td><strong>未做</strong> · C 项暂缓，等真实 Linux 用户；当前只有 macOS Seatbelt 写监狱</td>
<td><code>—（未实现，不给代码）</code></td>
</tr>
</tbody>
</table>
