---
title: "OpenDesign 产物面：三条交付路径、持久化与外壳"
date: 2026-11-19T19:02:00+08:00
summary: "文件系统与文本 artifact 怎么分流、manifest 与预览沙箱的边界、SQLite 没有 schema_version 的迁移方式，以及桌面 sidecar、协作快照、MCP 三个外立面。"
tags: ["architecture", "agent"]
slug: "open-design-deliverables"
series: "open-design"
series_order: 3
---


> 源码版本：`nexu-io/open-design` `0725880`（package.json 0.27.2）。本地 clone 后逐文件核对，行号都对这个 commit。仓库 14,308 个文件，`apps/daemon/src/server.ts` 一个文件 18,959 行。

本系列 8 篇 · [总览](/posts/open-design-architecture.html) · 上一篇 [上下文面](/posts/open-design-context.html) · 下一篇 [画布与落点](/posts/open-design-canvas.html)

## 3.6 交付物：两条物理路径，加一条刷新路径

{{< fig "diagrams/07-delivery.svg" "三条交付路径。A 与 B 由 streamFormat 分流，C 是一种带刷新状态的记录，与项目文件列表互不可见。" >}}

定义先说清：一个 Normal Artifact 等于一个入口文件加一个 manifest，恰好各一个。盘上是普通项目文件外加一个 `<name>.artifact.json` 边车。manifest 字段在 `manifest.ts:217-249`：`version` 恒为 1、`kind`（html / deck / react-component / markdown-document / svg / diagram / code-snippet / mini-app / design-system）、`entry`、`renderer`、`status`、`exports`、`primary`、`supportingFiles`。路径必须相对、不超过 260 字符、不含 `..`，`supportingFiles` 最多 128 个。

路径 A（filesystem，21 个 adapter）：CLI 写文件，chokidar 看到改动，发 `file-changed`（`routes/project/index.ts:5698`），前端 80/250 毫秒合并刷新，iframe 换 `?v=mtime` 重载。"这一轮交付了什么"由 `validateRunDeliverable` 判定（`run-deliverable-validation.ts:17-25`），失败原因是一个封闭枚举：`not_succeeded`、`no_artifact`、`project_missing`、`entry_missing`、`entry_not_touched`、`type_mismatch`。

路径 B（text_artifact，6 个 plain adapter）：分流判据只有一行，`packages/contracts/src/execution-profile.ts` 整个文件 7 行，`plain` 映射到 `text_artifact`。解析器在 `plain-stream.ts`：`<artifact` 后必须跟空白（`:230`），属性只认引号形式（`:203`），内容不 trim（`:105`），单 run 最多 50 个（`:44`），代码围栏里的示例跳过（`:252-306`）。物化时文件名截到 60 字符，冲突后缀 `-2` 到 `-9999`（`:388-419`）。截断的流按头尾拼接去重（`server.ts:17455-17491`）。

路径 C（Live Artifact）：一张可刷新的记录，落在 `<project>/.live-artifacts/<id>/`，对文件面板和 ZIP 隐藏。刷新是一个小状态机：加锁 → `running` → 校验 `sourceJson` → 校验权限 → 跑数据源（单源 30 秒、总 120 秒）→ 按单调序号提交（旧 `refreshId` 直接拒，`store.ts:822-850`）→ 渲染 `index.html` 并做原子快照。模板只允许单遍插值和一层 `data-od-repeat`，出现 `script`、`iframe`、`on*`、`javascript:` 直接拒（`render.ts:19-45`）。启动时会把超过 120 秒的锁标成 `failed`，错误码 `REFRESH_CRASH_RECOVERY_TIMEOUT`（`store.ts:1100`）。

预览沙箱分两档，这个细节容易搞错。普通产物预览是 `allow-scripts allow-forms allow-downloads`，**没有** `allow-same-origin`（`FileViewer.tsx:439`）；只有需要宿主桥的"增强预览"才额外放开 same-origin，而桥的身份校验只靠 `event.source === iframe.contentWindow`（`:8916-8933`），入站数据仍然当不可信处理。

导出四条路：单文件 HTML（内联本地依赖，限额 2MiB 入口、500 候选、2500 节点、50MiB 资产）、ZIP、PDF、PPTX。PDF 有个坑：桌面端走 Electron `printToPDF`，非桌面只有位图路，向量路默认关闭，因为打包运行时 `printToPDF` 会丢 CJK 字形（`import-export-routes.ts:1574`）。PPTX 是每页一张整幅图，页宽固定 13.333 英寸。

## 3.7 持久化与崩溃恢复

SQLite 用 better-sqlite3 的同步 API，单连接按文件缓存，`journal_mode = WAL`，外键打开（`db.ts:88-96`）。**没有 schema_version 表**。迁移靠一段幂等的 `CREATE TABLE/INDEX IF NOT EXISTS`（`:105-417`），加列靠 `PRAGMA table_info` 判断（`:418-419`），破坏性改动是事务里的显式重建（`:760`、`:834`、`:894`）。

主要表：`projects`、`workspace_projects`（一个项目一个 workspace）、`conversations`、`messages`（带 `run_id`、`run_status`、`events_json`、`produced_files_json`）、`message_event_batches`、`agent_sessions`（恢复护栏）、`preview_comments`、`routines` / `routine_runs` / `routine_schedule_claims`、`workspace_artifacts` / `chat_artifact_snapshots` / `chat_artifact_blobs`、`installed_plugins`、`applied_plugin_snapshots`、`run_devloop_iterations`。

项目目录里有两类保留段：`.file-versions/` 按文件名的 sha256 前 24 位分目录存历史 HTML，`.live-artifacts/` 存 live 记录。文件版本只对 `.html` 和 `.htm` 生成，没有保留期也没有 GC，删除文件时连版本根一起删。

崩溃恢复分三层：进程层，子进程用 detached 进程组启动，每个 run 写一份 `agent-process-<pid>.json`，下次启动按 OS 身份回收（`agent-process.ts:470-553`）；run 层，`state.json` 加启动期的 `reconcileDurableRunTerminals`；live 层，过期锁改写。

## 3.8 外壳、协作与 MCP

桌面端的启动链条值得抄：主进程先起 daemon sidecar，轮询到 URL，再起 web sidecar，且把 `OD_WEB_PORT` 设成 `"0"` 让它自己挑一个临时端口，最后用 `REGISTER_WEB_URL` 把 URL 回写给 daemon（`apps/packaged/src/sidecars.ts:1005/1085/905`）。渲染进程永远只加载 `od://app/`，由 `protocol.handle` 用 `net.fetch` 转发到当前的 web 源。这样 web sidecar 崩了、重启换了端口，Electron 侧不需要知道。sidecar 的身份只由五个 argv 字段决定：`channel`、`namespace`、`source`、`mode`、`app`。

协作层 113 个模块，编排在 `collab/runtime.ts`。同步单位是"每个项目一个版本化快照"：发布调度器把作者的连续编辑合并成一次发布（`publish-scheduler.ts:1-26`），先落内容、再推进指针，成员用 `POST /api/projects/:id/collab/pull` 拉取（`routes/collab-sync.ts:2678`）。冲突裁定靠发布顺序加版本身份，不做三方合并。presence 是 30 秒 TTL 的心跳集合（`presence-tracker.ts:31`），源码注释直说它不是实时光标引擎。评论存 `preview_comments`，带 `anchor_state`、`anchored_version`、`last_good_position_json`，用来在文件改动后重新锚定。

MCP 有两个角色。OD 自己是 stdio server（`mcp.ts`，3601 行，`od mcp` 启动），暴露 22 个工具：`collect_brief`、`confirm_brief`、`list_projects`、`get_active_context`、`get_artifact`、`list_files`、`search_files`、`create_artifact`、`write_file`、`create_project`、`start_run`、`get_run`、`cancel_run`、`list_agents` 等。这些工具全部是到 daemon HTTP 的薄 fetch，不碰文件系统，所以 MCP 侧不可能与 UI 契约分叉。省略 `project` 参数时读 `GET /api/active`，活跃上下文在最后一次网页交互后 5 分钟过期（`constants.ts:1`）。另一个角色是配置写入：`od mcp install <agent>` 覆盖 17 个目标，分三种装法（子命令 `mcp add`、深合并 JSON、只打印片段）。

---

下一篇是[画布与落点](/posts/open-design-canvas.html)。回到[总览](/posts/open-design-architecture.html)，或者从[分册索引](/posts/open-design-architecture.html#分册索引)挑一个方向。
