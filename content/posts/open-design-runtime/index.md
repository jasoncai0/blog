---
title: "OpenDesign 执行面：daemon 契约、run 生命周期与 adapter"
date: 2026-11-19T19:00:00+08:00
summary: "daemon 的启动顺序为什么不能改，一次生成从建流到终态的完整判定链，以及 27 个 CLI 怎么共用一份数据规格。含 run 时序图与五状态图。"
tags: ["architecture", "agent"]
slug: "open-design-runtime"
series: "open-design"
series_order: 1
---


> 源码版本：`nexu-io/open-design` `0725880`（package.json 0.27.2）。本地 clone 后逐文件核对，行号都对这个 commit。仓库 14,308 个文件，`apps/daemon/src/server.ts` 一个文件 18,959 行。

本系列 9 篇 · [总览](/posts/open-design-architecture.html) · 下一篇 [上下文面](/posts/open-design-context.html)

## 3.1 daemon 的注册顺序就是它的契约

`server.ts` 里的顺序不能改：

1. 局部 body parser 先于全局（`:3290` 128MB 的 library 上传、`:3295` 32MB 的品牌包），再挂全局 JSON 4MB（`:3305`）。
2. `/api` token 校验（`:3330`，`/health`、`/ready`、`/version` 豁免，loopback 直接短路）。
3. origin guard（`:3500-3580`）。
4. 打开 SQLite（`:3587`）。
5. 三组路由：`registerDaemonRoutes`（`:8855`）、`registerRunRoutes`（`:18193`）、`registerChatRoutes`（`:18749`）。
6. 异步跑一次 `reconcileDurableRunTerminals`（`:8681`）。
7. `listen`（`:18835`），`keepAliveTimeout` 设 120 秒（`:18859`），比 25 秒的 SSE keepalive 长。

顺序错了不会报错，只会静默失去保护。比如把全局 JSON parser 挂到 token 校验前面，未鉴权的请求就能先让 daemon 解析 4MB 请求体。

## 3.2 一次生成的完整生命周期

入口是 `POST /api/chat`（`routes/runs.ts:3500`，`handleRunCreate` 的 `stream=true` 分支）。校验顺序本身是一份错误码清单：503 关闭中 → 400 请求体/媒体执行/工具包 → BYOK 完整性 → 404 会话归属 → 项目授权 → 409 `IDEMPOTENCY_CONFLICT` → 409 `DESIGN_SYSTEM_ENRICHMENT_IN_PROGRESS`。

真正容易做错的是这一条：**先建 SSE 响应，再 spawn 子进程**。`routes/runs.ts:2867` 建流，`:2900` 才启动。反过来的话，进程启动最初几十毫秒的输出没有接收者。

{{< fig "diagrams/03-run-sequence.svg" "一次生成的时序。红色的两步是这套设计的骨架：先写 SSE 头，再 spawn；文件改动由 chokidar 传递。" >}}

run 先以 `status: 'queued'` 落库（`:1044`），同时在 `<data>/runs/<id>/` 下写 `state.json` 和 `events.jsonl`。这里有一个容易误判的事实：**SQLite 里没有 runs 表**。run 是进程内的内存对象，状态挂在 `messages` 行上，诊断库里另有一张 `active_runs` 只给排障用。

事件词汇分两层。传输层是 `start | run_retry_attempted | agent | stdout | stderr | diagnostic | error | end`（`packages/contracts/src/sse/chat.ts:511`）；`agent` 里再按 `type` 分 `status`、`text_delta`、`thinking_*`、`tool_use`、`tool_input_delta`、`tool_result`、`usage` 等十几类。落库节奏是 250 毫秒一批写 `message_event_batches`（`chat-run-messages.ts:82`），终态时一次性折叠进 `messages.events_json`（`db.ts:3752`）。刷新页面时前端从 `messages` 重建整段对话，不回放流。

终态只有一个出口：`finish()`（`runs.ts:1774`），判定顺序固定（`chat-run-lifecycle.ts:67-105`）：用户取消 → 退出码 0 → ACP 的正常 SIGTERM 覆盖 → artifact 静默关闭 → 退出码非 0 但本轮有产物 → turn 干净结束 → 失败。

{{< fig "diagrams/04-run-states.svg" "run 的五个状态。重启不是第六个状态，它是 queued/running 到 canceled 的一条边，多带一个 errorCode。" >}}

取消路径在 `runs.ts:2497`：`terminateProcessTree` 先 SIGTERM，等 3 秒（`OD_CHAT_RUN_CANCEL_GRACE_MS`），再 SIGKILL，再等 500 毫秒。重启路径在 `run-restart-recovery.ts:53-88`：未完成的 run 一律改写成 `canceled`，`terminalAt` 取最后一次活动时间。启动之后 `reconcileDurableRunTerminals` 还会扫一遍 `state.json`，把写早了的 `failed` 修回 `succeeded`，剩下的才算真中断。

中途插话走 `POST /api/runs/:id/steer`（`runs.ts:3415`）。写进子进程 stdin 的是一帧 `{"type":"user","message":{...}}`（`run-steering.ts:95-103`），三种不可达情况（运行时不支持、run 已终态、stdin 已关）都回 409。SQLite 里的那条 user 消息只在投递成功后才补写（`:3470-3485`），所以"用户以为说了、其实没送到"不会污染上下文。

## 3.3 Adapter 层：一份数据规格加一个通用引擎

这是全仓最值得读的一层。`RuntimeAgentDef`（`runtimes/types.ts:145`）的核心是一个纯函数：

```ts
buildArgs(
  prompt: string,
  imagePaths: string[] | undefined,
  extraAllowedDirs: string[] | undefined,
  options: RuntimeBuildOptions,
  runtimeContext: RuntimeContext,
): string[]
```

除此之外全是数据：`bin`、`fallbackBins`、`versionArgs`、`streamFormat`、`promptViaStdin`、`promptViaFile`、`maxPromptArgBytes`、`externalMcpInjection`、看门狗超时、恢复能力谓词。没有 `run()`，没有 `cancel()`，没有子类。27 个 def 文件加起来 3,327 行，注册表里加一行就上架；重复 id 在启动时抛 `Duplicate agent definition id: ${def.id}`（`registry.ts:81-87`）。

检测（`detection.ts:564-789`）每次都是实跑，没有 24 小时持久缓存：

1. `resolveAgentLaunch()` 先算出真正要 spawn 的路径，保证探测和启动走同一条路。
2. 跑 `--version`。跑不起来就按 `MAX_EXECUTABLE_ATTEMPTS = 8` 重新解析候选路径。
3. 过版本门与兼容门（比如 Codex 的 app-server 要求 0.95.0 以上）。
4. 五路并发：能力探测、隐藏 flag、模型列表、鉴权状态、版本号。注释里给的估算是 5 秒，串行要 15 秒。
5. 每个 probe 单独 try/catch。这一条是被 issue #2297 教出来的：一个 adapter 抛异常，`/api/agents` 整个返回空数组。

prompt 到进程的路是一条流水线，每一站都可能拒绝：

`checkPromptArgvBudget`（`server.ts:14211`）→ `buildArgs`（`:14561`）→ Windows 两级守卫（`:14597` 的 cmd shim、`:14625` 的直接 exe）→ `spawnAgentProcess`（`agent-process.ts:276`）。POSIX 的 argv 上限取 120,000 字节；Windows 取 32,767 减 256。DeepSeek adapter 因为把 prompt 放位置参数，自己在 def 里声明 `maxPromptArgBytes = 30_000`（`defs/deepseek.ts:39-52`）。整段 prompt 用 file-backed stdin 交付，因为 64KiB 的 pipe 会截断（`agent-process.ts:94-121`）。

流的分派就是一张表（`server.ts:16307-16909`）：`claude-stream-json`、`qoder-stream-json`、`copilot-stream-json`、`pi-rpc`、`acp-json-rpc`、`dsh-profile-jsonl`、`codex-app-server`、`json-event-stream`、`plain`。`json-event-stream` 内部再按 adapter 细分成 opencode、cursor、codex 几套（`json-event-stream.ts:1522`）。归一化之后的产物是同一个 `DaemonAgentPayload`。

"加一个 CLI 只要一个文件"这句话的边界值得说清。复用已有 `streamFormat` 时，确实只要 def 加注册表一行。需要动引擎的只有四类：新 `streamFormat`（要同时改 `server.ts:16307-16909` 和 `connectionTest.ts:2143-2220` 两处）、新 `eventParser`、adapter 特有的环境修补、检测特例。

---

下一篇是[上下文面](/posts/open-design-context.html)。回到[总览](/posts/open-design-architecture.html)，或者从[分册索引](/posts/open-design-architecture.html#分册索引)挑一个方向。
