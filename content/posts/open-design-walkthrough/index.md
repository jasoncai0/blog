---
title: "OpenDesign 端到端：一句话到一份可交付的 deck"
date: 2026-11-19T19:03:00+08:00
summary: "把前面几篇串成一条链：建项目、拼 prompt、子进程写盘、边车落地、预览与导出，共九步；配十二步泳道图与盘面结构图。"
tags: ["architecture", "agent"]
slug: "open-design-walkthrough"
series: "open-design"
series_order: 4
---


> 源码版本：`nexu-io/open-design` `0725880`（package.json 0.27.2）。本地 clone 后逐文件核对，行号都对这个 commit。仓库 14,308 个文件，`apps/daemon/src/server.ts` 一个文件 18,959 行。

本系列 7 篇 · [总览](/posts/open-design-architecture.html) · 上一篇 [产物面](/posts/open-design-deliverables.html) · 下一篇 [与编码 Agent 的分岔](/posts/open-design-vs-coding-agent.html)

## 4. 走一遍：一句话到一份可交付的 deck

拿一条具体链路把前八节串起来。Deck tab 里选 `deck-swiss-international`（16 列网格、22 个锁死版面、`design_system.requires: false`），挂一套品牌 design system，输入一句话。

1. 建项目：`POST /api/projects` 带 `id`、`name`、`skillId`、`designSystemId`、`pendingPrompt`（`routes/project/index.ts:3936`）。skill 与 design system 的 id 都要在服务端解析成功，否则 400；`metadata.baseDir` 在这个入口被直接拒，只有 `/api/import/folder` 能设。通过之后才建 `PROJECTS_DIR/<id>` 与 `projects` 行。
2. 发首轮：`pendingPrompt` 存在项目行上，前端在 ProjectView 挂载时用它发起这一轮，只发一次（`App.tsx:3862`）。daemon 侧先写 SSE 头再 spawn，顺序见 [§3.2](/posts/open-design-runtime.html)。
3. 拼 prompt：skill 正文、DESIGN.md 全文、`tokens.css`、组件清单、craft 规则、模板骨架按 [§3.4](/posts/open-design-context.html) 的顺序拼成一串；skill 目录同时拷到 `.od-skills/deck-swiss-international-<sha10>/`。
4. 子进程写盘：CLI 拿到 cwd 和一句 prompt，`index.html` 与附件由它自己写。daemon 从 chokidar 看到 `file-changed`，不解析写文件请求。
5. 写边车：入口文件落地后，daemon 写 `index.html.artifact.json`（`kind: deck`、`entry`、`exports`、`status`）。没有边车的文件不进产物列表。
6. 收尾：`finish()` 判定 succeeded，`validateRunDeliverable` 再确认 entry 在 `touchedPaths` 里、类型对得上。
7. 预览：iframe 走 URL 模式，换 `?v=mtime` 重载。deck 的翻页与 inspect 要宿主桥，这一步切 srcDoc。
8. 评审（可选）：Design Jury 最多 3 轮，评委是同一会话里的不同 turn，不额外起进程。
9. 导出：PPTX 每页一张整幅位图（页宽 13.333in）、PDF 走 Electron `printToPDF`、单文件 HTML 内联本地依赖、ZIP 走 `/api/projects/:id/archive`。

{{< fig "diagrams/08-swimlane.svg" "同一条链的十二步泳道。红色两步是骨架：SSE 头先于 spawn 写出，否则子进程启动阶段的输出无处可去。" >}}

第一张图把同一条链展开成十二步：建项目与首轮各拆两段，生成段多出一次文件变更回传；评审那一步是可选的，图里省了。01–05 都在准备上下文，06 之后才开始产生字节。

{{< fig "diagrams/09-deliverable.svg" "跑完之后盘上的样子：项目目录、边车字段，以及四个消费者各读哪些文件。" >}}

第二张图是收工后的盘面。项目目录里只有普通文件和一份 skill 副本，加一个 `artifact.json` 边车；run 的 `state.json` 与 `events.jsonl` 在数据根，不在这里。

这条链每一段都能单独换：换 CLI 加一个 def 文件，换 skill 换一个目录，换品牌换一个 DS 包。硬契约只有两处，SSE 帧格式（`packages/contracts/src/api/chat.ts`）与 artifact manifest（`version` 必须是 1）。

---

下一篇是[与编码 Agent 的分岔](/posts/open-design-vs-coding-agent.html)。回到[总览](/posts/open-design-architecture.html)，或者从[分册索引](/posts/open-design-architecture.html#分册索引)挑一个方向。
