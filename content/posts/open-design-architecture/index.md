---
title: "OpenDesign 架构解剖：不拥有 agent 的设计工作台"
date: 2026-11-18T22:22:00+08:00
summary: "OpenDesign 把 agent loop 外包给用户机器上的编码 CLI，自己只做装配。本文按总-分拆开它的三层进程、run 生命周期、adapter 数据规格、prompt 编译器、四类内容平面和三条交付物路径，并列出七处结构性代价。"
tags: ["architecture", "agent"]
slug: "open-design-architecture"
---

> 源码版本：`nexu-io/open-design` `0725880`（package.json 0.27.2）。本地 clone 后逐文件核对，行号都对这个 commit。仓库 14,308 个文件，`apps/daemon/src/server.ts` 一个文件 18,959 行。

## 0. 先给结论（按影响排序）

**P0 · agent loop 外置决定了其他所有分层**（§1、§3.3）。判断错的代价：你会在这个仓库里找模型路由、工具循环、上下文压缩，一个都找不到，然后按"这是个 agent 框架"的方式去扩展，落到错误的层。决定下一步调什么工具的代码不在这里，在 `claude -p` 或 `codex app-server` 的子进程里。

**P0 · adapter 是一份数据，不是一组类**（§3.3）。`RuntimeAgentDef` 是纯对象，27 个 CLI 共用同一套 detect / launch / invoke / stream 引擎。判断错的代价：加第 28 个 CLI 时去写子类，探测路径会和 spawn 路径分叉，出现"能检测到、起不来"。

**P0 · prompt 由 daemon 拼装，且存在两套并行实现**（§3.4）。判断错的代价见 #7568：deck 协议只落到 legacy 分支，两套测试全绿，修补的第二个 PR 体量接近第一个的两倍。

**P1 · 交付物有两条物理路径，由 streamFormat 一处分流**（§3.6）。`plain` 走文本 artifact 协议，其余走文件系统。判断错的代价：给支持工具的 CLI 发"请输出 artifact 块"，产物会被当成普通消息丢掉。

**P1 · run 没有 `interrupted` 状态**（§3.2）。重启把在跑的 run 改写成 `canceled` 加 `errorCode: DAEMON_RESTARTED`。判断错的代价：前端按 `interrupted` 写的分支永远不触发。

**P1 · 协作层的同步单位是版本化快照**（§3.8）。冲突靠发布顺序与版本身份裁定。判断错的代价：按协同编辑器的思路去改，会破坏 content-first / pointer-last 的发布顺序。

**P2 · 单数据根、单写者、无跨进程锁**（§3.1、§4）。两个 daemon 指向同一个 `OD_DATA_DIR` 会同时写 SQLite。判断错的代价：想在本机开两个实例做对比测试，数据互相踩。

**P2 · 扩展性的落地形式是"往目录里放文件夹"**（§3.5）。skills、design-systems、templates 都是磁盘目录，每次列表请求重扫。判断错的代价：加数据库表或文件 watcher，和 shadow 规则打架。

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

## 4. 代价

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

## 5. 如果要抄，抄这五条

1. **把"多运行时"做成数据规格**。`RuntimeAgentDef` 加一个通用引擎，27 个 CLI 的成本是 27 个 def 文件，合计 3,327 行。
2. **先建流再启动**。`routes/runs.ts:2867` 与 `:2900` 的先后顺序，是这套 run 生命周期里最便宜也最容易做错的一处正确性。
3. **执行档只在一处映射**。`execution-profile.ts` 全文件 7 行，把"产物是文件还是文本块"这个判断收在一个函数里，所有调用方共享同一份答案。
4. **内容注册表用目录加 frontmatter**。没有数据库表、没有 watcher、没有安装步骤，用户目录遮蔽内置目录的规则只有一行。
5. **MCP 工具层只做 HTTP 转发**。22 个工具全是 `fetch`，MCP 侧没有状态，也就没有第二套真相。

## 6. 参考

- 仓库：`nexu-io/open-design`，commit `0725880`，Apache-2.0。
- 架构与规范：`docs/architecture.md`、`docs/agent-adapters.md`、`docs/skills-protocol.md`、`docs/prompt-composition.md`、`docs/critique-theater.md`、`docs/atoms.md`、`docs/spec.md`（Archived Draft）。
- 关键源码：`apps/daemon/src/server.ts`、`apps/daemon/src/runtimes/{registry,detection,types}.ts`、`apps/daemon/src/runtimes/defs/*.ts`、`apps/daemon/src/prompts/system.ts`、`apps/daemon/src/prompts/core-slim.ts`、`apps/daemon/src/skills.ts`、`apps/daemon/src/design-systems/index.ts`、`apps/daemon/src/artifacts/plain-stream.ts`、`apps/daemon/src/live-artifacts/store.ts`、`packages/contracts/src/execution-profile.ts`、`packages/contracts/src/sse/chat.ts`。
