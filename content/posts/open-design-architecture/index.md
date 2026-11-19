---
title: "OpenDesign 架构解剖：不拥有 agent 的设计工作台"
date: 2026-11-18T22:22:00+08:00
summary: "OpenDesign 把 agent loop 外包给用户机器上的编码 CLI，自己只做装配。本文按总-分拆开它的三层进程、run 生命周期、adapter 数据规格、prompt 编译器、四类内容平面和三条交付物路径；后半部分专门对比设计 Agent 与编码 Agent 的分岔，拆解它怎么用一条命令行加一张 run 级令牌调生成模型，以及生成 prompt 的三道质量闸。"
tags: ["architecture", "agent"]
slug: "open-design-architecture"
---

> 源码版本：`nexu-io/open-design` `0725880`（package.json 0.27.2）。本地 clone 后逐文件核对，行号都对这个 commit。仓库 14,308 个文件，`apps/daemon/src/server.ts` 一个文件 18,959 行。

## 0. 先给结论（按影响排序）

**P0 · agent loop 外置决定了其他所有分层**（§1、§3.3）。判断错的代价：你会在这个仓库里找模型路由、工具循环、上下文压缩，一个都找不到，然后按"这是个 agent 框架"的方式去扩展，落到错误的层。决定下一步调什么工具的代码不在这里，在 `claude -p` 或 `codex app-server` 的子进程里。

**P0 · adapter 是一份数据，不是一组类**（§3.3）。`RuntimeAgentDef` 是纯对象，27 个 CLI 共用同一套 detect / launch / invoke / stream 引擎。判断错的代价：加第 28 个 CLI 时去写子类，探测路径会和 spawn 路径分叉，出现"能检测到、起不来"。

**P0 · prompt 由 daemon 拼装，且存在两套并行实现**（§3.4）。判断错的代价见 #7568：deck 协议只落到 legacy 分支，两套测试全绿，修补的第二个 PR 体量接近第一个的两倍。

**P0 · 生成能力是"一条命令行 + 按 run 发的令牌"，不是工具定义**（§6.1、§6.2）。27 个 CLI 只共享 shell，所以 `od media generate` 是唯一入口，daemon 只为这一轮发一张 15 分钟的工具令牌。判断错的代价：你会去找图片工具的 schema，或者想给 provider 加一个 SDK 适配层，而仓库里这两样都不存在。

**P1 · 交付物有两条物理路径，由 streamFormat 一处分流**（§3.6）。`plain` 走文本 artifact 协议，其余走文件系统。判断错的代价：给支持工具的 CLI 发"请输出 artifact 块"，产物会被当成普通消息丢掉。

**P1 · run 没有 `interrupted` 状态**（§3.2）。重启把在跑的 run 改写成 `canceled` 加 `errorCode: DAEMON_RESTARTED`。判断错的代价：前端按 `interrupted` 写的分支永远不触发。

**P1 · 协作层的同步单位是版本化快照**（§3.8）。冲突靠发布顺序与版本身份裁定。判断错的代价：按协同编辑器的思路去改，会破坏 content-first / pointer-last 的发布顺序。

**P2 · 单数据根、单写者、无跨进程锁**（§3.1、§8）。两个 daemon 指向同一个 `OD_DATA_DIR` 会同时写 SQLite。判断错的代价：想在本机开两个实例做对比测试，数据互相踩。

**P2 · 生成 prompt 的质量来自语料与重写，不来自编译器**（§7.1）。106 个模板只是"结构与风格参考"，daemon 从不解析占位符、不拼最终 prompt，最终那段文字由 agent 写。判断错的代价：去 `prompt-templates/` 找模板引擎，或者以为模板里的 aspect 会被严格遵守（Volcengine 上 2:3 会被改成 16:9）。

## 1. 定位：三个不变量

README 自称 "The 1st Collaborative AI Design Agent Workspace"。真正决定代码形状的是 `docs/spec.md` 里那三条下注（该文档标着 Archived Draft，下注本身仍然有效）：

1. 不拥有 agent loop，交给用户已装好的编码 CLI。
2. 不拥有模型，密钥和账号都在用户机器上。
3. 不拥有内容目录的所有权，`skills/` 是可以用文件夹替换的。

同一篇 spec 还有四条 non-goals。其中 "We do not ship a desktop app. No Electron" 在 v0.27 被推翻了：`apps/desktop` 和 `apps/packaged` 都在仓库里，PDF 导出用的就是 Electron 的 `webContents.printToPDF`。另外三条守住了，分别是"不做模型路由""不重写 agent loop""单机单用户"。

于是整个产品可以写成一条等式：

```
产品 = 内容注册表（skills / templates / design-systems / plugins）
     + prompt 编译器
     + 交付物物化器
     + 外壳（web / Electron / od CLI / MCP）
```

agent 的推理过程全部落在等式右边之外。这条等式解释了目录形状：`apps/web` 只有一个 App Router 页面、路由手写在 `apps/web/src/router.ts`；`apps/daemon` 里塞着 18,959 行的 `server.ts` 和 47 个路由文件。前端薄、后端厚，因为后端要替一个"看不见内部"的子进程承担所有确定性职责。

## 2. 总览：三层进程，一个数据根

{{< fig "diagrams/01-topology.svg" "三层进程与单一数据根。CLI 子进程绕过 daemon 直接写项目文件，daemon 只通过文件监听感知变化。" >}}

第一层是渲染层。Next.js 用 `output: 'export'` 打静态产物，`apps/web/app/[[...slug]]/page.tsx` 是唯一的业务页面（另一个 `page.tsx` 是桌面宠物），`generateStaticParams()` 返回 `[{ slug: [] }]`，所以导出只有一个 `index.html`。真正的路由是手写的 521 行 `parseRoute` / `buildPath`，架在 `useSyncExternalStore` 与 `pushState` 上。桌面端加载的是 `od://app/`，由 `protocol.handle` 转发到真实的 web 源。

第二层是 daemon。Express，默认 `127.0.0.1:7456`，拥有 `/api/*`、SSE、SQLite、项目目录、内容注册表，以及所有子进程的生死。

第三层是执行面。每个 run 一个 CLI 子进程。它不通过 daemon 写文件，直接往项目目录里落盘。daemon 对它的写入只有一种感知方式：chokidar 看到改动，发一个 `file-changed` 事件。

daemon 启动时只解析一次数据根（`apps/daemon/src/server.ts:1432`）：

```ts
RUNTIME_DATA_DIR = resolveDataDir(process.env.OD_DATA_DIR, PROJECT_ROOT, {
  requireExplicit: SANDBOX_MODE_ENABLED,
})
```

`projects/`、`artifacts/`、`runs/`、`app.sqlite`、日志、MCP 配置、插件状态、agent 的 home 全部从这里派生。唯一例外是"导入文件夹"项目：文件留在用户选的目录，数据库里只记 `metadata.baseDir`。

安全边界有三条，都在代码里。第一条，`DEFAULT_DAEMON_BIND_HOST = '127.0.0.1'`（`daemon-startup.ts:31`）；非 loopback 且没有 `OD_API_TOKEN` 时直接抛 `OD_BIND_HOST=${host} requires OD_API_TOKEN to be set.`（`server.ts:3255`）。第二条，带 `Origin` 头的请求要过白名单，`Origin: null` 只放行 GET，端口还没确定时 fail-closed 到 403 `Server initializing`。第三条，导入文件夹需要主进程用 `randomBytes(32)` 的密钥签一个 HMAC，覆盖 `baseDir`、nonce 和过期时间，放在 `X-OD-Desktop-Import-Token` 头里（`apps/desktop/src/main/runtime.ts:267-277`）。注释写了原因：旧流程让渲染进程回传路径，"a compromised renderer could substitute an arbitrary baseDir at the second step"。

还有一条组织约束值得抄：同一个能力必须同时出现在 web UI 和 `od` 子命令里，且两边打同一组 `/api/*`（`AGENTS.md`，UI/CLI dual-track）。`packages/contracts` 只放 HTTP 与 SSE 的 DTO，`apps/web` 不许 import `apps/daemon/src`。薄契约挡住了前后端各自长出自己的方言。

进程图回答谁在跑。仓库里 27 个 workspace 包回答另一个问题：谁能 import 谁。

{{< fig "diagrams/02-module-layers.svg" "27 个包分五层，import 只能自上而下。红色那条是被禁止的边：web 拿不到 daemon 的内部实现，只能对着 contracts 的 DTO 编程。" >}}

压在中间的是 `packages/contracts`。它和 `platform`、`sidecar`、`sidecar-proto` 一起被两端同时依赖，是这 4 个包里唯一只做类型与纯函数的那个：HTTP 与 SSE 的 DTO，加上两端共用的解析函数，不碰 Node fs、Express 和浏览器 API。`apps/web` 的 6 条内部依赖里没有 `apps/daemon`，这条缺失由 `AGENTS.md` 写死，没有构建期检查，只在 review 时守。`apps/packaged` 是唯一同时依赖 web 与 daemon 的包，它负责把两个 sidecar 拉起来并注册回 daemon（`sidecars.ts:905-920`）。`tools/*` 和 `shells/*` 在图的底部：不被任何 app 反向依赖，只由 `pnpm` 脚本调用；`shells/terminal` 里没有 TypeScript 运行时，只有 sh/ps1 启动器与 JSON Schema 契约。

## 3. 核心模块

### 3.1 daemon 的注册顺序就是它的契约

`server.ts` 里的顺序不能改：

1. 局部 body parser 先于全局（`:3290` 128MB 的 library 上传、`:3295` 32MB 的品牌包），再挂全局 JSON 4MB（`:3305`）。
2. `/api` token 校验（`:3330`，`/health`、`/ready`、`/version` 豁免，loopback 直接短路）。
3. origin guard（`:3500-3580`）。
4. 打开 SQLite（`:3587`）。
5. 三组路由：`registerDaemonRoutes`（`:8855`）、`registerRunRoutes`（`:18193`）、`registerChatRoutes`（`:18749`）。
6. 异步跑一次 `reconcileDurableRunTerminals`（`:8681`）。
7. `listen`（`:18835`），`keepAliveTimeout` 设 120 秒（`:18859`），比 25 秒的 SSE keepalive 长。

顺序错了不会报错，只会静默失去保护。比如把全局 JSON parser 挂到 token 校验前面，未鉴权的请求就能先让 daemon 解析 4MB 请求体。

### 3.2 一次生成的完整生命周期

入口是 `POST /api/chat`（`routes/runs.ts:3500`，`handleRunCreate` 的 `stream=true` 分支）。校验顺序本身是一份错误码清单：503 关闭中 → 400 请求体/媒体执行/工具包 → BYOK 完整性 → 404 会话归属 → 项目授权 → 409 `IDEMPOTENCY_CONFLICT` → 409 `DESIGN_SYSTEM_ENRICHMENT_IN_PROGRESS`。

真正容易做错的是这一条：**先建 SSE 响应，再 spawn 子进程**。`routes/runs.ts:2867` 建流，`:2900` 才启动。反过来的话，进程启动最初几十毫秒的输出没有接收者。

{{< fig "diagrams/03-run-sequence.svg" "一次生成的时序。红色的两步是这套设计的骨架：先写 SSE 头，再 spawn；文件改动由 chokidar 传递。" >}}

run 先以 `status: 'queued'` 落库（`:1044`），同时在 `<data>/runs/<id>/` 下写 `state.json` 和 `events.jsonl`。这里有一个容易误判的事实：**SQLite 里没有 runs 表**。run 是进程内的内存对象，状态挂在 `messages` 行上，诊断库里另有一张 `active_runs` 只给排障用。

事件词汇分两层。传输层是 `start | run_retry_attempted | agent | stdout | stderr | diagnostic | error | end`（`packages/contracts/src/sse/chat.ts:511`）；`agent` 里再按 `type` 分 `status`、`text_delta`、`thinking_*`、`tool_use`、`tool_input_delta`、`tool_result`、`usage` 等十几类。落库节奏是 250 毫秒一批写 `message_event_batches`（`chat-run-messages.ts:82`），终态时一次性折叠进 `messages.events_json`（`db.ts:3752`）。刷新页面时前端从 `messages` 重建整段对话，不回放流。

终态只有一个出口：`finish()`（`runs.ts:1774`），判定顺序固定（`chat-run-lifecycle.ts:67-105`）：用户取消 → 退出码 0 → ACP 的正常 SIGTERM 覆盖 → artifact 静默关闭 → 退出码非 0 但本轮有产物 → turn 干净结束 → 失败。

{{< fig "diagrams/04-run-states.svg" "run 的五个状态。重启不是第六个状态，它是 queued/running 到 canceled 的一条边，多带一个 errorCode。" >}}

取消路径在 `runs.ts:2497`：`terminateProcessTree` 先 SIGTERM，等 3 秒（`OD_CHAT_RUN_CANCEL_GRACE_MS`），再 SIGKILL，再等 500 毫秒。重启路径在 `run-restart-recovery.ts:53-88`：未完成的 run 一律改写成 `canceled`，`terminalAt` 取最后一次活动时间。启动之后 `reconcileDurableRunTerminals` 还会扫一遍 `state.json`，把写早了的 `failed` 修回 `succeeded`，剩下的才算真中断。

中途插话走 `POST /api/runs/:id/steer`（`runs.ts:3415`）。写进子进程 stdin 的是一帧 `{"type":"user","message":{...}}`（`run-steering.ts:95-103`），三种不可达情况（运行时不支持、run 已终态、stdin 已关）都回 409。SQLite 里的那条 user 消息只在投递成功后才补写（`:3470-3485`），所以"用户以为说了、其实没送到"不会污染上下文。

### 3.3 Adapter 层：一份数据规格加一个通用引擎

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

### 3.4 prompt 编译器

`composeSystemPrompt`（`prompts/system.ts:899`）的函数体第一行就是分叉：

```ts
if (odNextStrategyRecipe) return composeOdNextStrategyRequestPromptV2(...)
```

没有 recipe 就走 slim core charter（`core-slim.ts:425`，默认；`OD_PROMPT_CORE=classic` 切回）。正文有三十余个区块，按职责能合并成下面十三组。

{{< fig "diagrams/05-prompt-stack.svg" "prompt 的组装顺序与两道闸门。左列是顺序，右侧三块是分叉点、已发生的漂移和明确不做的三件事。" >}}

顺序里有三处判断值得单独拎出来。有设计系统时，"设计方向"整段被跳过（`:1150`），因为 `DESIGN.md` 已经回答了风格问题。craft 规则夹在品牌 token 块与 skill 正文之间（`:1331`），冲突时品牌 token 优先。角色边界守卫放在最后一段（`:1530`），用来压住前面所有指令，包括用户自己写的。

体积管理只有一个动作：组装完按字节卡上限。超限直接报 `AGENT_PROMPT_TOO_LARGE`，不做裁剪、不做分节丢弃。"按 DESIGN.md 分节裁剪"这个字段（`od.design_system.sections`）在协议里预留了，实现没有。

风险出在"两套实现"上。strategy 维度有 legacy 与 OD Next 两条路，core 维度有 slim 与 classic 两个头，加上 `apps/daemon/src/prompts/` 与 `packages/contracts/src/prompts/` 之间五对手抄镜像，漂移已经发生：`system.ts` 一边 2048 行、一边 1141 行，`core-slim.ts` 在 contracts 侧根本不存在。

#7568 是这套结构的必然产物：deck 协议只加在 legacy 那一侧，两个新增守护都调用 `composeSystemPrompt` 且不带 `odNextStrategyRecipe`，所以它们永远走 legacy 分支、永远绿。#7651 的修法是把协议收口到单源 `packages/contracts/src/prompts/deck-framework.ts:573`。`docs/prompt-composition.md` 留了一句话，我认为是这份代码库里最该贴在墙上的一句：绿色测试不证明两边都带上了你的改动。

### 3.5 内容平面：四类目录加一套通用规则

{{< fig "diagrams/06-content-planes.svg" "四类内容平面、遮蔽规则，以及同一条 skill 记录的三处消费：prompt、工作目录、UI 分组。" >}}

规模：`skills/` 171 个 `SKILL.md`，`design-templates/` 116 个，`design-systems/` 154 个包，`plugins/_official/` 6 个工作流，`craft/` 13 条品牌无关规则。

发现逻辑在 `skills.ts` 与 `server.ts:1494` 的 `SKILL_ROOTS`。用户目录先扫，仓库目录后扫，`frontmatter.name` 相同就停（`skills.ts:284` 那行 `if (seenIds.has(parentId)) continue`）。每次列表请求重新扫盘，没有 watcher、没有 SIGHUP、没有安装步骤：把文件夹丢进去，下一次刷新就能看见。

一个 skill 出现在三个地方，这三处必须一起看：

1. **prompt 里**：选中 skill 的 `SKILL.md` 正文，不带 `references/*.md`，正文里指路让 agent 自己去读。`@` 提及的 skill 与插件带的 skill 一起拼进来。
2. **工作目录里**：每轮把 skill 目录拷到 `<cwd>/.od-skills/<basename>-<sha256[:10]>/`（`cwd-aliases.ts:108`）。用真实拷贝。注释写了原因：早期用 symlink 指向 `skills/`，agent 往工作目录一写，就把仓库里的发布资源改了（`:9-29`）。
3. **UI 里**：`od.mode`、`od.surface`、`od.category` 决定新建面板的分组。缺 `od:` 块时按 `prototype` 兜底，所以 171 个 skill 里那些老文件不需要改就能被识别。

设计系统包的结构是 `manifest.json` + `DESIGN.md` + `tokens.css`，可选 `USAGE.md`、`components.manifest.json`、`components.html`、`design-tokens.json`、`preview/`、`source/`。校验很硬：`schemaVersion` 必须等于 `od-design-system-project/v1`，`id` 必须等于目录名，`files.design` 必须指向 `DESIGN.md`（`design-systems/index.ts:4114-4162`）。token 契约（`token-contract.ts:171-199`）把未声明的 `var()` 判成错误，把一屏内出现超过两次的 `var(--accent)` 判成警告。

插件是"带流水线的 skill 包"：目录 + `SKILL.md` + `open-design.json`，后者声明 `od.pipeline.stages[].atoms[]`。安装器（`plugins/installer.ts:1-20`）支持本地目录、`github:owner/repo`、`.tar.gz`，拒路径穿越、符号链接、超过 50MiB、覆盖已有 id。信任分 trusted 与 restricted 两级，能力声明决定它能拿到哪些工具。运行时把 atom 展开成有序阶段，`pipeline-runner.ts:55` 逐阶段发 `pipeline_stage_started` 和 `completed`，`until` 词表是封闭集合（`critique.score`、`iterations`、`user.confirmed`、`preview.ok`、`build.passing`、`tests.passing`），devloop 轮数上限默认 10。

### 3.6 交付物：两条物理路径，加一条刷新路径

{{< fig "diagrams/07-delivery.svg" "三条交付路径。A 与 B 由 streamFormat 分流，C 是一种带刷新状态的记录，与项目文件列表互不可见。" >}}

定义先说清：一个 Normal Artifact 等于一个入口文件加一个 manifest，恰好各一个。盘上是普通项目文件外加一个 `<name>.artifact.json` 边车。manifest 字段在 `manifest.ts:217-249`：`version` 恒为 1、`kind`（html / deck / react-component / markdown-document / svg / diagram / code-snippet / mini-app / design-system）、`entry`、`renderer`、`status`、`exports`、`primary`、`supportingFiles`。路径必须相对、不超过 260 字符、不含 `..`，`supportingFiles` 最多 128 个。

路径 A（filesystem，21 个 adapter）：CLI 写文件，chokidar 看到改动，发 `file-changed`（`routes/project/index.ts:5698`），前端 80/250 毫秒合并刷新，iframe 换 `?v=mtime` 重载。"这一轮交付了什么"由 `validateRunDeliverable` 判定（`run-deliverable-validation.ts:17-25`），失败原因是一个封闭枚举：`not_succeeded`、`no_artifact`、`project_missing`、`entry_missing`、`entry_not_touched`、`type_mismatch`。

路径 B（text_artifact，6 个 plain adapter）：分流判据只有一行，`packages/contracts/src/execution-profile.ts` 整个文件 7 行，`plain` 映射到 `text_artifact`。解析器在 `plain-stream.ts`：`<artifact` 后必须跟空白（`:230`），属性只认引号形式（`:203`），内容不 trim（`:105`），单 run 最多 50 个（`:44`），代码围栏里的示例跳过（`:252-306`）。物化时文件名截到 60 字符，冲突后缀 `-2` 到 `-9999`（`:388-419`）。截断的流按头尾拼接去重（`server.ts:17455-17491`）。

路径 C（Live Artifact）：一张可刷新的记录，落在 `<project>/.live-artifacts/<id>/`，对文件面板和 ZIP 隐藏。刷新是一个小状态机：加锁 → `running` → 校验 `sourceJson` → 校验权限 → 跑数据源（单源 30 秒、总 120 秒）→ 按单调序号提交（旧 `refreshId` 直接拒，`store.ts:822-850`）→ 渲染 `index.html` 并做原子快照。模板只允许单遍插值和一层 `data-od-repeat`，出现 `script`、`iframe`、`on*`、`javascript:` 直接拒（`render.ts:19-45`）。启动时会把超过 120 秒的锁标成 `failed`，错误码 `REFRESH_CRASH_RECOVERY_TIMEOUT`（`store.ts:1100`）。

预览沙箱分两档，这个细节容易搞错。普通产物预览是 `allow-scripts allow-forms allow-downloads`，**没有** `allow-same-origin`（`FileViewer.tsx:439`）；只有需要宿主桥的"增强预览"才额外放开 same-origin，而桥的身份校验只靠 `event.source === iframe.contentWindow`（`:8916-8933`），入站数据仍然当不可信处理。

导出四条路：单文件 HTML（内联本地依赖，限额 2MiB 入口、500 候选、2500 节点、50MiB 资产）、ZIP、PDF、PPTX。PDF 有个坑：桌面端走 Electron `printToPDF`，非桌面只有位图路，向量路默认关闭，因为打包运行时 `printToPDF` 会丢 CJK 字形（`import-export-routes.ts:1574`）。PPTX 是每页一张整幅图，页宽固定 13.333 英寸。

### 3.7 持久化与崩溃恢复

SQLite 用 better-sqlite3 的同步 API，单连接按文件缓存，`journal_mode = WAL`，外键打开（`db.ts:88-96`）。**没有 schema_version 表**。迁移靠一段幂等的 `CREATE TABLE/INDEX IF NOT EXISTS`（`:105-417`），加列靠 `PRAGMA table_info` 判断（`:418-419`），破坏性改动是事务里的显式重建（`:760`、`:834`、`:894`）。

主要表：`projects`、`workspace_projects`（一个项目一个 workspace）、`conversations`、`messages`（带 `run_id`、`run_status`、`events_json`、`produced_files_json`）、`message_event_batches`、`agent_sessions`（恢复护栏）、`preview_comments`、`routines` / `routine_runs` / `routine_schedule_claims`、`workspace_artifacts` / `chat_artifact_snapshots` / `chat_artifact_blobs`、`installed_plugins`、`applied_plugin_snapshots`、`run_devloop_iterations`。

项目目录里有两类保留段：`.file-versions/` 按文件名的 sha256 前 24 位分目录存历史 HTML，`.live-artifacts/` 存 live 记录。文件版本只对 `.html` 和 `.htm` 生成，没有保留期也没有 GC，删除文件时连版本根一起删。

崩溃恢复分三层：进程层，子进程用 detached 进程组启动，每个 run 写一份 `agent-process-<pid>.json`，下次启动按 OS 身份回收（`agent-process.ts:470-553`）；run 层，`state.json` 加启动期的 `reconcileDurableRunTerminals`；live 层，过期锁改写。

### 3.8 外壳、协作与 MCP

桌面端的启动链条值得抄：主进程先起 daemon sidecar，轮询到 URL，再起 web sidecar，且把 `OD_WEB_PORT` 设成 `"0"` 让它自己挑一个临时端口，最后用 `REGISTER_WEB_URL` 把 URL 回写给 daemon（`apps/packaged/src/sidecars.ts:1005/1085/905`）。渲染进程永远只加载 `od://app/`，由 `protocol.handle` 用 `net.fetch` 转发到当前的 web 源。这样 web sidecar 崩了、重启换了端口，Electron 侧不需要知道。sidecar 的身份只由五个 argv 字段决定：`channel`、`namespace`、`source`、`mode`、`app`。

协作层 113 个模块，编排在 `collab/runtime.ts`。同步单位是"每个项目一个版本化快照"：发布调度器把作者的连续编辑合并成一次发布（`publish-scheduler.ts:1-26`），先落内容、再推进指针，成员用 `POST /api/projects/:id/collab/pull` 拉取（`routes/collab-sync.ts:2678`）。冲突裁定靠发布顺序加版本身份，不做三方合并。presence 是 30 秒 TTL 的心跳集合（`presence-tracker.ts:31`），源码注释直说它不是实时光标引擎。评论存 `preview_comments`，带 `anchor_state`、`anchored_version`、`last_good_position_json`，用来在文件改动后重新锚定。

MCP 有两个角色。OD 自己是 stdio server（`mcp.ts`，3601 行，`od mcp` 启动），暴露 22 个工具：`collect_brief`、`confirm_brief`、`list_projects`、`get_active_context`、`get_artifact`、`list_files`、`search_files`、`create_artifact`、`write_file`、`create_project`、`start_run`、`get_run`、`cancel_run`、`list_agents` 等。这些工具全部是到 daemon HTTP 的薄 fetch，不碰文件系统，所以 MCP 侧不可能与 UI 契约分叉。省略 `project` 参数时读 `GET /api/active`，活跃上下文在最后一次网页交互后 5 分钟过期（`constants.ts:1`）。另一个角色是配置写入：`od mcp install <agent>` 覆盖 17 个目标，分三种装法（子命令 `mcp add`、深合并 JSON、只打印片段）。

## 4. 走一遍：一句话到一份可交付的 deck

拿一条具体链路把前八节串起来。Deck tab 里选 `deck-swiss-international`（16 列网格、22 个锁死版面、`design_system.requires: false`），挂一套品牌 design system，输入一句话。

1. 建项目：`POST /api/projects` 带 `id`、`name`、`skillId`、`designSystemId`、`pendingPrompt`（`routes/project/index.ts:3936`）。skill 与 design system 的 id 都要在服务端解析成功，否则 400；`metadata.baseDir` 在这个入口被直接拒，只有 `/api/import/folder` 能设。通过之后才建 `PROJECTS_DIR/<id>` 与 `projects` 行。
2. 发首轮：`pendingPrompt` 存在项目行上，前端在 ProjectView 挂载时用它发起这一轮，只发一次（`App.tsx:3862`）。daemon 侧先写 SSE 头再 spawn，顺序见 §3.2。
3. 拼 prompt：skill 正文、DESIGN.md 全文、`tokens.css`、组件清单、craft 规则、模板骨架按 §3.4 的顺序拼成一串；skill 目录同时拷到 `.od-skills/deck-swiss-international-<sha10>/`。
4. 子进程写盘：CLI 拿到 cwd 和一句 prompt，`index.html` 与附件由它自己写。daemon 从 chokidar 看到 `file-changed`，不解析写文件请求。
5. 写边车：入口文件落地后，daemon 写 `index.html.artifact.json`（`kind: deck`、`entry`、`exports`、`status`）。没有边车的文件不进产物列表。
6. 收尾：`finish()` 判定 succeeded，`validateRunDeliverable` 再确认 entry 在 `touchedPaths` 里、类型对得上。
7. 预览：iframe 走 URL 模式，换 `?v=mtime` 重载。deck 的翻页与 inspect 要宿主桥，这一步切 srcDoc。
8. 评审（可选）：Design Jury 最多 3 轮，评委是同一会话里的不同 turn，不额外起进程。
9. 导出：PPTX 每页一张整幅位图（页宽 13.333in）、PDF 走 Electron `printToPDF`、单文件 HTML 内联本地依赖、ZIP 走 `/api/projects/:id/archive`。

{{< fig "diagrams/08-swimlane.svg" "同一条链的十二步泳道。红色两步是骨架：SSE 头先于 spawn 写出，否则子进程启动阶段的输出无处可去。" >}}

图 8 把同一条链展开成十二步：建项目与首轮各拆两段，生成段多出一次文件变更回传；评审那一步是可选的，图里省了。01–05 都在准备上下文，06 之后才开始产生字节。

{{< fig "diagrams/09-deliverable.svg" "跑完之后盘上的样子：项目目录、边车字段，以及四个消费者各读哪些文件。" >}}

图 9 是收工后的盘面。项目目录里只有普通文件和一份 skill 副本，加一个 `artifact.json` 边车；run 的 `state.json` 与 `events.jsonl` 在数据根，不在这里。

这条链每一段都能单独换：换 CLI 加一个 def 文件，换 skill 换一个目录，换品牌换一个 DS 包。硬契约只有两处，SSE 帧格式（`packages/contracts/src/api/chat.ts`）与 artifact manifest（`version` 必须是 1）。

## 5. 与：设计 Agent 和编码 Agent 分岔在哪

OD 复用编码 CLI 的全部推理能力，却没有把验收标准一起外包。分岔落在四处：做完了怎么算、知识以什么形态进 prompt、谁给好坏打分、非文本产物走哪条供给线。

### 5.1 完成判据：入口文件有没有被这一轮碰过

编码 agent 的完成信号来自外部：测试绿、构建通过、diff 被人看过。OD 用不了这套，它的产物是一份可以被再次渲染、再次导出的具名文件，不是仓库状态。

判据写在 `run-deliverable-validation.ts`。一轮结束时，声明的入口文件必须出现在本轮 `touchedPaths` 里（`:318-331`，否则报 `entry_not_touched`），文件类型要匹配 `kind`（`:58-68`，deck 只认 html/presentation/pdf），manifest 要存在且 `version` 为 1。失败原因是封闭枚举：`not_succeeded`、`no_artifact`、`project_missing`、`entry_missing`、`entry_not_touched`、`type_mismatch`。

这套判据判不了"好不好"，但它不需要跑构建就能机械校验。代价与收益都在这一句里。

| 轴 | 编码 Agent | OpenDesign | 代码 |
| --- | --- | --- | --- |
| 交付 | 仓库里变了的文件 | manifest 声明的具名产物 | `manifest.ts:22-49` |
| 验收 | 测试、构建、人看 diff | 入口被本轮触碰且类型匹配 | `run-deliverable-validation.ts:318-331` |
| 预览 | 用户自己的 dev server | daemon 预览源加宿主桥 | `file-viewer-render-mode.ts:104-137` |
| 知识 | 仓库约定 | DESIGN.md 加 tokens.css 逐字 | `system.ts:1276-1331` |
| 评审 | 另一个人或另一个会话 | 同会话五个 panelist 打分 | `critique-theater.md:54-74` |
| 工具 | 文件、shell、测试、git | 只有 shell，其余走 `od` 包装 | `tools/` 仅 `connectors.ts` |

第三行那个"只有 shell"值得展开。`apps/daemon/src/tools/` 整个目录只有一个 `connectors.ts`，137 行。没有 test runner，没有 git 提交、分支、重构工具。它跟 git 的唯一接触是只读摘要 `git.summary`，作为 live artifact 的刷新数据源（`live-artifacts/refresh.ts:10-11`）。这是单元边界决定的：OD 的单元是一个产物，不是一堆代码。

### 5.2 知识：让违反变得可见

编码 agent 的固定前缀是约定：命名风格、目录结构、提交格式。这类知识天然是软的，模型违反它不会产生结构性错误，只会让人皱眉。

OD 的设计知识分三层，第二层是硬的。

1. `DESIGN.md` 用散文写清颜色、字体、间距、组件规则，要求"不要发明这套调色板以外的 token"（`system.ts:1276-1285`）。
2. `tokens.css` 是同一份契约的机器可读形式，指令是整段 `:root` 原样粘进第一个 `<style>`（`system.ts:1294-1305`）。
3. craft 那 13 份规则品牌无关，只管可用性、动效纪律、排版层级这类通用要求，插在 DESIGN.md 与 skill 正文之间；冲突时品牌 token 优先（`system.ts:1325-1331`）。

第二层的价值在于可校验：粘错一个变量名，产物里立刻看得见。把审美约定翻译成可以 diff 的字符串，是这套 prompt 架构里最实用的一招。

四类内容平面也是同一个思路的延伸。skills、design-templates、design-systems、plugins 都是磁盘目录，加一个文件夹就多一种能力，不需要建表、不需要 watcher、不需要重启（`skills.ts:284`、`cwd-aliases.ts:108`）。

### 5.3 反馈：评审判好坏，但拦不住导出

Design Jury 容易被误读成 CI，它更像一轮结构化的自我复看。

五个固定评委：designer 权重 0、critic 0.4、brand 0.2、accessibility 0.2、copy 0.2；阈值 8.0/10；最多三轮；兜底策略 `ship_best`（`docs/critique-theater.md:54-74`）。评委不是五个进程，是同一个 CLI 会话里被 `<PANELIST role="...">` 分隔的五个 turn，所以它不增加一次模型调用之外的成本。

开关有四层优先级：skill 的 `od.critique.policy` > 项目 `metadata.critiqueTheaterEnabled` > `OD_CRITIQUE_ENABLED` > rollout 档位。默认关。

分数不通过不会阻止导出。它产出的是"要不要再改一轮"的判断，附一份可回放的 ndjson 记录。想把它变成门禁，得自己接，比如把导出挂在评审之后。

### 5.4 多模态是另一条供给线

图片、视频、音频三种 surface 走的是完全不同的 prompt 栈，这一点在源码里是显式的。

设计系统的注入被 `if (!isMediaSurfaceEarly && !isAskMode)` 门控（`system.ts:1134`、`:1275`），也就是说图像与视频的 run 拿不到 DESIGN.md、拿不到 tokens.css、拿不到 craft。它们拿到的是：skill 正文、媒体契约（压在系统提示最后）、项目元数据里的模型与画幅。

原因不难推：图像模型不吃 token 名。它吃"镜头、材质、光线、时间"这类描述，`#bf4520` 这种值写进 prompt 只是噪声。品牌一致性在媒体面上靠参考图加文字描述，靠不住 token 契约。

这个门控是设计决定，也是能力边界：同一个项目里，HTML 产物受品牌 token 强约束，配图只受文字描述弱约束。

## 6. 怎么调用生成类 Agent

OD 调生成模型的方式，是把"调模型"降级成"跑一条命令"。

### 6.1 只给一条命令行

27 个 CLI 的共同能力只有 shell，`media-contract.ts` 开头就把这个理由写明了：

> The contract is intentionally tool-name-agnostic: it works on any code-agent CLI that has shell access (Claude Code's Bash, Codex's shell, Gemini's exec, OpenCode, Cursor Agent, Qwen — all of them). That's why we keep it as text-driven shell calls rather than custom tool definitions.

于是生成能力的接口是一行 shell，不是一个工具 schema：

```bash
"$OD_NODE_BIN" "$OD_BIN" media generate --project "$OD_PROJECT_ID" \
  --surface image --model vela/gpt-image-2 --aspect 1:1 \
  --output assets/stairs.png --prompt "<full prompt>"
```

OD 自己的 MCP server 有 22 个工具，里面没有生成工具，最接近的是 `start_run`（它只是再开一轮对话）。生成永远走 CLI 与 HTTP 这条车道。

{{< fig "diagrams/10-media-dispatch.svg" "媒体派发的五列结构。凭证不出 daemon，agent 拿到的是一张按 run 发的令牌；HyperFrames 的渲染也在 daemon 进程里，因为 agent 的 Bash 被 sandbox 包住，puppeteer 抓帧会卡住。" >}}

### 6.2 通行证：按 run 发的一次性令牌

agent 要访问 daemon，用的不是 `OD_API_TOKEN`，是一张为这一轮铸的令牌。

- 形态：`odtt_` 前缀加 32 字节随机数的 base64url，daemon 只存 sha256（`tool-tokens.ts:121-127`）。
- 有效期：`max(15 分钟, 本轮不活跃超时 + 15 分钟)`，agent 每有活动就续期（`server.ts:12015`、`tool-tokens.ts:237-262`）。
- 权限：endpoint 白名单 17 条、operation 白名单 16 条，逐条字符串匹配（`tool-tokens.ts:18-63`，校验在 `:226-232`）。
- 撤销：run 结束的 finalize 钩子里 `revokeToolToken('run_finalized')`（`server.ts:12068`），另有 `child_exit`、`sse_end`、`ttl_expired`、`manual` 四种原因。
- 隔离：`createAgentRuntimeEnv` 会把子进程环境里所有 `OD_API_TOKEN` 键删掉（`server.ts:1883-1885`），注释原话是 agent children 只拿 run 级能力，不拿那个大凭据。

为什么值得这么麻烦：agent 是外部进程，一段 prompt 注入就能让它执行任意 shell。给它大令牌等于把整个 `/api` 交出去；给它一张只覆盖本轮、只覆盖 17 条路径、15 分钟过期的令牌，最坏情况被限定在一次 run 的范围内。

### 6.3 派发：从 surface 到 provider

媒体目录 95 个模型条目，23 个 provider（18 个已接）：50 个图像、31 个视频、14 个音频（4 音乐 / 8 语音 / 2 音效），默认项标在 `vela/gpt-image-2`（`media/models.ts`）。`fal-ai/*` 这类带前缀的 id 直接直通，不进目录。

一次请求的校验顺序固定：项目不存在 404 → surface 不是 image/video/audio 就 400 → 没有 model 就 400 → 媒体策略拒绝 403（`media:generate` 被默认允许，但 `enabled: false` 或 `allowedSurfaces` 不含该面时拒绝）→ 建 `media_tasks` 行 → 派发。

路由永远回 202 加 taskId，代码里没有"同步返回字节"这条路。CLI 侧替 agent 扛掉前 25 秒：等到就当场把 `{"file":{...}}` 打给 stdout，等不到就把 taskId 交出去，agent 进 `media wait <taskId> --since <n>` 的 120 秒长轮询，退出码 0 完成、2 还在跑、5 失败、4 daemon 出错。`flux-pro-ultra` 通常 60–180 秒，`sora-2` 与 `veo-3-fal` 更久。

数值参数一律钳制，不报错：视频时长按 `[3,5,8,10,15,30]` 取最近档，音频按 `[5,10,15,30,60,120]`，画幅收敛到 `1:1 / 16:9 / 9:16 / 4:3 / 3:4`（`media/index.ts:270-300`）。未知模型 id 才直接抛错；`fal-ai/*` 与 `aihubmix-*` 这类带前缀的 id 在运行时现场合成定义。

provider 返回的资源 URL 还要过一遍 `assertAndFetchExternalAsset`：先 DNS 解析，拒掉回环、RFC1918、链路本地、CGNAT、组播与云元数据地址，`redirect` 设为 `error`，连接时把解析出的 IP 钉死（`connectionTest.ts:357-400`）。生成服务经常把结果放在第三方 CDN 上，这一步防的是"模型服务被诱导返回一个内网 URL"。

{{< fig "diagrams/11-media-sequence.svg" "一次图片生成的十一步时序，六条泳道。红色两步是骨架：令牌随 Authorization 头进 daemon，可见性却只在 daemon 的 queued 诊断行里。" >}}

### 6.4 两件"事后才落地"的事

媒体任务比 run 活得久，于是有两种反常时序必须显式处理。

第一种是迟到文件。run 早已写出终态，provider 那边才把字节送回来。`associateLateRunProducedFile` 负责把文件补挂到那个已经结束的 run 上，前提是 run 真的已经终态，还活着就不抢。

第二种是失败回写。这条链上曾经有一个真实的 bug：agent 在正文里道歉、`exit 0`，run 被判成 succeeded，界面上绿灯、产物栏空白，而 daemon 自己的日志 55 秒前就记了唯一交付物生成失败。修法是在派发侧留一条反向通道 `noteMediaTaskFailure(runId, {...})`，让任务失败能把 run 改判成失败。

启动期的对账也是同一类工作：停在 `queued` 与 `running` 的行一律改写成 `interrupted`，带 `code: "DAEMON_RESTART"` 与 `status: 5`（`media/tasks.ts:290-310`）；终态行保留一小时供前端补读，之后清掉（`task-store.ts:50`）。

失败到 `nextStep` 的映射是张固定表：429/503 与超时给 `retry-later`，内容安全拒绝给 `revise-request`，没配 key 给 `open-settings`，重启打断给 `retry-later`，策略拒绝给 `unsupported` 或 `switch-model`，磁盘写不进去这类归 `contact-support`。agent 不需要理解原因，只需要照表说话。

{{< fig "diagrams/12-media-task-states.svg" "媒体任务的五个状态。done 与 failed 都可能发生在 run 结案之后，所以成败是两条时间线对账出来的，不是 exit code 一个人说了算。" >}}

### 6.5 本地渲染路径：HyperFrames

有一条生成路径完全不联网：`hyperframes-html`。agent 写一份 HTML 加 GSAP 时间轴，daemon 用 Chromium 逐帧抓图，再用 FFmpeg 编码成 MP4。

这条路存在的直接原因是沙箱。Claude Code 的 Bash 在 macOS 上被 `sandbox-exec` 包住，puppeteer 起自己的 Chrome 会卡在抓帧，所以渲染必须在 daemon 进程里做。agent 只负责写 composition：`hyperframes.json`、`meta.json`、`index.html` 三个文件，用 `media scaffold` 生成骨架，目录必须落在 `.hyperframes-cache` 或 `motion-source` 下，不能是符号链接，不能预先存在。

可复现性靠四件事：固定的 `hyperframes` 0.8.1 与 `@ffmpeg-installer/ffmpeg` 版本；`-start_number 0 -framerate <fps>` 的帧序列；`libx264 / yuv420p / faststart` 的编码参数；渲染前注入的帧桥把 `<audio>` 换成隐藏 span，让音频时钟归 FFmpeg 管。输出先落在 `mkdtemp` 目录里的 `render.mp4`，读进内存后删掉，再走那条所有 provider 共用的写文件路径。

这条路没有 stub 回退，渲染失败就是任务失败，跟 `vela/*` 一样。

## 7. 生成 prompt 的质量靠什么

三道闸：给例子、把硬参数挪出 prompt 文本、失败只说一句。没有第四道。

### 7.1 第一道闸：语料加改写

`prompt-templates/` 里 106 个模板，48 张图、58 段视频，只覆盖四个模型：`gpt-image-2` 46 个、`seedream-5-pro` 2 个、`seedance-2.0` 40 个、`hyperframes-html` 18 个。每条都带来源与许可：awesome-seedance-2-prompts 36 条、awesome-gpt-image-2 36 条、HyperFrames 18 条、仓库自建 12 条；许可以 CC-BY-4.0 为主（72 条），Apache-2.0 31 条，MIT 1 条。

模板进 prompt 的方式不是填空。系统提示里的原话是：

> The user picked this template as inspiration. Treat it as a structural and stylistic reference: borrow composition, palette cues, lighting language, lens/motion direction, and the level of detail. Adapt the wording to the user's actual subject and brief — do NOT generate the template subject verbatim.

新建面板里模板正文是可编辑的文本框，用户改过的版本成为权威，最终那段 `--prompt` 由 agent 写。daemon 从不编译模板，也不替换占位符：`{argument name="..." default="..."}` 这种写法在仓库里没有任何解析器，它是上游语料库的约定，agent 只能从 `default=` 推断该怎么填。

加载校验只有六条：是对象、`id` 非空、`surface` 与所在目录一致、`title` 非空、`prompt` 至少 20 字符、`source` 里有 `repo` 与 `license`（`media/prompt-templates.ts:93-143`）。没有 schema 库，没有去重，没有 aspect 白名单，也没有许可白名单。

质量在这里靠的是语料的密度：36 条同类范例里能看出这个模型喜欢什么样的句式与信息密度，比任何"prompt 最佳实践"清单都具体。

### 7.2 第二道闸：硬参数走命令行

契约里最有用的一类规则，是把"不该写进 prompt 的东西"明确挪出去。

尺寸与档位属于用户。原话是：用户说的"2K"、"高质量"就是那个要求，要映射到 `--resolution` 或 `--quality`；把它复述进 prompt 文本传不到 provider。画幅有启发式：电影感与横屏 16:9、竖屏社媒 9:16、没有线索时 1:1；provider 侧还会再纠一次，火山引擎上非枚举值一律落到 16:9。

音色必须是 provider 的 voice_id，自然语言描述不被接受。音效 prompt 硬上限 450 字符，目标 180–320，因为超长会在 provider 校验阶段白白浪费一次生成。图生视频用 `--image` 传项目相对路径，越界路径直接拒。

还有两条防呆：不要 `Read` 生成的二进制回上下文（会把下一次请求撑爆），不要手工"编辑"生成出来的字节（重新生成，让用户挑版本）。

### 7.3 第三道闸：失败只说一句

每个失败都带 `error.nextStep`，九值封闭集合：`revise-request`、`switch-model`、`open-settings`、`sign-in`、`add-credit`、`retry-later`、`update-app`、`unsupported`、`contact-support`。

契约要求 agent 只按这个字段分支，不猜 code、status 或措辞。只有 `switch-model` 与 `retry-later` 允许自动重派一次，理由写在注释里：第二次调用改变不了结果，还可能再计一次费。

给用户的话是一句本地化文案，不出现模型名、provider 名、taskId、路径、stderr、退出码。重试只调时间不调 prompt：只对 429 与 503 重试，最多一次，延迟上限 60 秒。

### 7.4 缺口

四处明确的空缺，读代码能确认：

1. 没有 negative prompt 参数。任何 provider 调用里都不存在这个字段。
2. daemon 不做内容审核。所谓"提示词没通过审核"是 provider 返回后按状态码分类的结果。
3. 模板里的 aspect 只是建议。2:3 的模板在火山引擎上会被改成 16:9，代码里没有告警。
4. 产物没有自动 QA。`motion-compare` 会算 SSIM 并拼对比图，但它是量具不是门禁：注释原话是让 agent 自己看帧对，分数只说明先看哪个时间点。
5. 请求链路里没有 `seed` 参数。同一个 prompt 重跑会得到不同结果，所谓可复现只体现在"字节被 sha256 冻结、绑在 runId 与 taskId 上"，不体现在生成过程。

要补第四道闸，位置在这里：把 `motion-compare` 与人工抽查接成导出前的可选检查。

## 8. 代价

| 代价 | 位置 | 后果 |
| --- | --- | --- |
| 单文件体量 | `server.ts` 18,959 行 / 846KB，`db.ts` 6,007 行 | review 粒度失效，改动靠 grep 定位 |
| 两套 prompt 实现 | `prompts/` 与 `contracts/src/prompts/` 五对镜像 | #7568 已证明会漏，且漏得无声 |
| 没有产物回收 | 无 artifact GC，文件版本无保留期，live 快照只增不减 | 长期使用的磁盘只涨 |
| 没有跨进程数据根锁 | `OD_DATA_DIR` 只做可写校验 | 两个 daemon 指同一目录会同时写 SQLite |
| SSE 背压被丢弃 | `server.ts:3149` 忽略 `res.write` 返回值 | 慢客户端下内存增长，而另一条 `message-stream.ts:21` 会 `await drain` |
| prompt 不裁剪 | 只在组装后按字节卡 | 设计系统一多就直接 `AGENT_PROMPT_TOO_LARGE` |
| 检测无持久缓存 | 只有进程内 5 秒的版本缓存 | 每次 `/api/agents` 重跑五路探测 |

前两条是结构性的，后五条是局部可修的。如果要给这个仓库提第一个 PR，我会挑背压那一条：两处实现对同一件事有两种处理，这种不一致迟早会咬人。

## 9. 如果要抄，抄这五条

1. **把"多运行时"做成数据规格**。`RuntimeAgentDef` 加一个通用引擎，27 个 CLI 的成本是 27 个 def 文件，合计 3,327 行。
2. **先建流再启动**。`routes/runs.ts:2867` 与 `:2900` 的先后顺序，是这套 run 生命周期里最便宜也最容易做错的一处正确性。
3. **执行档只在一处映射**。`execution-profile.ts` 全文件 7 行，把"产物是文件还是文本块"这个判断收在一个函数里，所有调用方共享同一份答案。
4. **内容注册表用目录加 frontmatter**。没有数据库表、没有 watcher、没有安装步骤，用户目录遮蔽内置目录的规则只有一行。
5. **MCP 工具层只做 HTTP 转发**。22 个工具全是 `fetch`，MCP 侧没有状态，也就没有第二套真相。

## 10. 参考

- 仓库：`nexu-io/open-design`，commit `0725880`，Apache-2.0。
- 架构与规范：`docs/architecture.md`、`docs/agent-adapters.md`、`docs/skills-protocol.md`、`docs/prompt-composition.md`、`docs/critique-theater.md`、`docs/atoms.md`、`docs/spec.md`（Archived Draft）。
- 关键源码：`apps/daemon/src/server.ts`、`apps/daemon/src/runtimes/{registry,detection,types}.ts`、`apps/daemon/src/runtimes/defs/*.ts`、`apps/daemon/src/prompts/system.ts`、`apps/daemon/src/prompts/core-slim.ts`、`apps/daemon/src/skills.ts`、`apps/daemon/src/design-systems/index.ts`、`apps/daemon/src/artifacts/plain-stream.ts`、`apps/daemon/src/live-artifacts/store.ts`、`packages/contracts/src/execution-profile.ts`、`packages/contracts/src/sse/chat.ts`。
