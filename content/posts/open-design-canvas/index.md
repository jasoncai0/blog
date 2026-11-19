---
title: "OpenDesign 的画布与落点：产物怎么进工作面"
date: 2026-11-19T19:23:00+08:00
summary: "这个仓库里没有无限画布：唯一的 pan/zoom 在导入的产物 HTML 内部。agent 产物靠 chokidar 与 file-changed 感知、靠 run 收尾补边车、靠一套排序规则在右侧标签区开合。"
tags: ["architecture", "agent"]
slug: "open-design-canvas"
series: "open-design"
series_order: 4
---

> 源码版本：`nexu-io/open-design` `0725880`（package.json 0.27.2）。本地 clone 后逐文件核对，行号都对这个 commit。仓库 14,308 个文件，`apps/daemon/src/server.ts` 一个文件 18,959 行。

本系列 8 篇 · [总览](/posts/open-design-architecture.html) · 上一篇 [产物面](/posts/open-design-deliverables.html) · 下一篇 [端到端](/posts/open-design-walkthrough.html)

## 3.9 结论：仓库里没有画布引擎

先回答被问到的东西：无限画布。这个仓库里没有实现它。

三条证据，按从入口到依赖的顺序：

1. `/board` 路由返回 `{ kind: 'home', view: 'board' }`（`apps/web/src/router.ts:158`），`EntryShell` 把它渲染成 `<TeamSlotPlaceholder icon="kanban" title={t('entry.navBoard')} />`（`apps/web/src/components/EntryShell.tsx:2551-2553`），中文文案是「看板」（`apps/web/src/i18n/locales/zh-CN.ts:1006`）。`router.ts:23-26` 的注释把这类槽位归给 "team-workspace slots (project spaces + members + board + workspace settings) whose views are provided by other lanes"，也就是占位，等别的分支填。
2. 依赖里没有任何画布库。`@xyflow/*`、`reactflow`、`tldraw`、`konva`、`fabric`、`excalidraw`、`react-zoom-pan-pinch` 在 `apps/web` 与 `packages/*` 的 `package.json` 里一个都搜不到。
3. "infinite canvas" 只出现在文案里：`design-systems/miro/DESIGN.md:4` 和 `plugins/_official/design-systems/miro/open-design.json:28` 写着 "Visual collaboration. Bright yellow accent, infinite canvas aesthetic." 这是给 Miro 设计系统写的视觉描述，不是功能清单。

所以真实的落点在右侧那排标签页，不在画布坐标上。下面这条链回答两件事：产物怎么被系统看见，怎么决定开哪个标签。

## 3.10 落点链路：写盘、感知、补齐、开签

链路分四段，每段的责任方不同，也没有一段需要 agent 主动"发布"点什么。

| 阶段 | 谁做 | 关键实现 | 断掉的症状 |
| --- | --- | --- | --- |
| 写盘 | CLI 子进程 | 直接写项目目录，不经 daemon | 文件落在项目外，标签区看不到 |
| 感知 | daemon + 前端 | chokidar 事件 → `file-changed` SSE → mtime 重载 | 预览停在旧版本，得手动刷新 |
| 补齐 | daemon | run 收尾时 `reconcileHtmlArtifactManifest` | 有 HTML 没有边车，产物列表与导出都认不出 |
| 开签 | 前端 | `auto-open-file.ts` 的排序与去重 | 生成了但没打开，用户以为没产物 |

### 写盘：文件系统是唯一真相

带工具能力的 runtime 在项目目录里直接写文件，daemon 不代理写。所以"一次生成产出什么"不看 agent 说了什么，看盘上多了什么。一个 Normal Artifact 由入口文件加 `<name>.artifact.json` 边车构成，两条路径的差别只在边车由谁写。

### 感知：200ms 稳定阈值与空 path 的含义

`apps/daemon/src/project-watchers.ts:113` 发出的事件形状是 `{ type: 'file-changed', path, kind }`，`kind` 是 add / change / unlink。chokidar 的配置里有两项值得记：`ignoreInitial: true` 让订阅者不会在挂载时收到历史文件，`awaitWriteFinish: { stabilityThreshold: 200, pollInterval: 50 }` 让 agent 边写边存的过程只产生一次稳定事件。跨文件系统的场景会退到轮询（`isPollingFallbackError` 认 `EMFILE` / `ENOSPC`，间隔 100ms，二进制 300ms）。

前端 `useProjectFileEvents`（`apps/web/src/providers/project-events.ts:311`）订阅同源 SSE，断线按 1s 到 30s 退避重连。它给的约定是"收到就重新拉文件列表"，不做增量。已经打开的预览靠 `?v=${Math.round(file.mtime)}&r=${reloadKey}` 重载 iframe（`FileViewer.tsx:10961`，同一模式还有 :11187、:19522、:19601、:19631、:19679），这条路是 PR #384 定的。

这里有一个容易写错的点：事件里的 `path` 可以是空串，表示"整目录都脏了"。按单文件处理会漏刷。

### 补齐：agent 用 write_file 写的 HTML 也要有边车

`apps/daemon/src/server.ts:17414-17437` 在 run 收尾时扫一遍项目文件，只对 mtime 晚于本轮开始时间的 `.html` / `.htm` 调 `reconcileHtmlArtifactManifest`。注释写得很直白：agent 可能用了 `write_file` 而不是 `create_artifact`，也可能在写完 HTML 与写完边车之间就结束了。这段是 best-effort，不阻塞 finalize（issue #2893）。

### 开签：OPEND-2588 的排序规则

三条规则，都在 `apps/web/src/components/auto-open-file.ts`：

1. 流式过程中，`tool_result` 一到就试着开。前提是 `provenProjectRelativeToolPath` 能证明这个绝对路径在项目目录内（`ProjectView.tsx:10099`）。证明不了就不开，避免给上游仓库的改动建一个永久空标签。
2. 一轮结束时，按"主要产物"排序来决定开哪些：HTML 高于 markdown，markdown 高于媒体；rank 0 的（deck、纯文本、草稿）永远不开。排序相同时按 mtime，最新的拿焦点。全部主要产物各开一个标签，不设上限。这是产品裁定 OPEND-2588，原话是「就让 agent 生成完，把那些产物在右侧全打开呗」，当天补了一句「是全部的**主要**产物」。
3. 模糊就放弃。路径后缀能对上多个文件时不开，宁可少开一个也不开错一个。`.jsx` / `.tsx` 若是某个 HTML 入口引用的模块，也不单独开（issue #2744），那里没有可预览的独立页面。

用户一旦自己点过别的标签，`userTookOverPreviewRef` 置真（`ProjectView.tsx:5189`，置真在 :4800），后续的自动开签全部让位。`web-clone` 意图的项目走例外：焦点落在 `index.html`，不落在最后写的子页（`ProjectView.tsx:2606-2609`）。

{{< fig "diagrams/01-landing-sequence.svg" "落点的完整时序。红色两步是视图层主动做的：先证明路径在项目内，再决定开哪个标签；其余事件都是被动感知。" >}}

预览本身是隔离的：`GET /api/projects/:id/preview-url` 现场铸一个短时 scope，返回 `/api/projects/:id/preview/<scope>/<文件>`，并带上 `csp`、`iframeSandbox`、`opaqueOrigin: true`、`expiresAt`。iframe 因此拿不到宿主的同源身份，产物里的脚本再怎么写也够不到 UI 的存储。

## 3.11 唯一的真画布在产物内部

仓库里确实有 pan/zoom 代码，但它不在 OD 里，在它导入的产物里。

`POST /api/import/claude-design`（`apps/daemon/src/import-export-routes.ts:118`）收一个 zip，解到 `projectDir(PROJECTS_DIR, id)`，用 `detectEntryFile` 找入口（`index.html` 优先，其次任意 `*.html`，都没有就返回 null，见 `apps/daemon/src/projects.ts:270`），然后建项目、建会话、`setTabs(db, id, [entryFile], entryFile)`，`pendingPrompt` 写成 "Imported from Claude Design ZIP: … Continue editing …"。限制是 `MAX_FILES = 5000`、总解压 100MB、单文件 25MB，路径经 `sanitizeZipPath`。

关键一步在 `apps/daemon/src/design/claude-design-import.ts`：`normalizeDesignCanvasWheelHandling` 改写导入模板里的滚轮处理块。原文注释写得很清楚："Plain wheel input should pan the infinite canvas. Claude Design exports previously guessed that large integer vertical deltas were mouse-wheel zoom clicks, but macOS trackpads can emit the same shape during ordinary two-finger scrolling." 改法是滚轮一律平移（带 72 / 160 像素的限幅），缩放只留 Cmd+滚轮和宿主工具栏。模板改了而正则没匹配上时，它打一行 `[claude-design-import] design-canvas.jsx found but … rewrite regex(es) did not match`，让人知道导入的画布可能会在滚动时缩放。

也就是说：画布是产物自带的 JS，跑在沙箱 iframe 里。OD 提供的是容器、入口识别、和一次针对"滚轮语义"的兼容改写。

{{< fig "diagrams/02-surface-ownership.svg" "三层归属：画布在产物内，标签在工作区，看板是占位。OD 核心不含画布引擎。" >}}

## 3.12 如果要给 OD 加画布，缺什么

这一节是推断，不是仓库现状。

缺三样东西：

1. 空间模型。库里没有 nodes / edges 表，`db.ts` 里跟"位置"有关的只有标签顺序与当前标签（`setTabs`）。画布要先回答节点是什么：一个产物、一个版本、还是一次生成。
2. 落点映射。现在的映射终点是"标签名"，画布需要的是"节点 id + 坐标 + 尺寸"。3.10 那条链路的最后一步要换掉，前面的感知与补齐可以原样复用。
3. 视口状态。缩放、平移、被折叠的分组，都要有地方存。现在没有任何一处持久化这类状态。

反过来，有两块现成的可以借：`live-artifacts` 已经是"可刷新的记录"（源数据加预览状态），比普通产物更接近画布节点；deck 预览的多页结构说明系统里已经有一层"一产物多画面"的模型，只是它没被抽象成空间。

---

下一篇 [端到端](/posts/open-design-walkthrough.html) 把这条链路放进一次真实的生成里走一遍。回到 [分册索引](/posts/open-design-architecture.html#分册索引)。
