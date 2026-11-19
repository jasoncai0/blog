---
title: "画布设计稿：把标签换成坐标"
date: 2026-11-19T22:46:00+08:00
summary: "给 OpenDesign 加画布的设计稿：节点只引用产物、布局是纯函数、视口归用户、事件只做信号。含功能架构图、落点时序图、节点状态图，以及上线与回滚路径。"
tags: ["architecture", "agent"]
slug: "open-design-canvas-design"
series: "open-design"
series_order: 5
---

> 源码版本：`nexu-io/open-design` `0725880`（package.json 0.27.2）。本地 clone 后逐文件核对，行号都对这个 commit。仓库 14,308 个文件，`apps/daemon/src/server.ts` 一个文件 18,959 行。本篇是设计稿，代码尚未存在；所有"现有"标注都能在仓库里找到。

本系列 9 篇 · [总览](/posts/open-design-architecture.html) · 上一篇 [画布与落点](/posts/open-design-canvas.html) · 下一篇 [端到端](/posts/open-design-walkthrough.html)

## 3.13 目标、非目标、四条原理

目标是让一次生成的全部产物同屏可见，并且能回答三个问题：产物在哪、彼此什么关系、当前看到的是哪一块。落地面仍然是文件系统，画布只是它的一层投影。

非目标先划掉四件：

1. 不做自由绘图工具。没有画笔、没有连线编辑器，节点只从产物来。
2. 不做多画布。一个项目一张画布，和现在一个项目一套标签对齐。
3. 不引入图数据库或空间索引服务。SQLite 一张关系表加 x/y 索引够用。
4. 不做实时协作光标。协作沿用已有的版本化快照与 publish-order 权威，不新造一套 CRDT。

四条原理，后面每个设计决定都能追到其中一条：

1. **节点引用产物，不复制产物。** 删节点不删文件；改文件不产生新节点。因为文件的真相已经在盘上，复制一份就多一个会漂移的副本。
2. **布局是纯函数。** 同样的产物集合与同样的排序输入，永远算出同样的坐标。这样重放、导出、多人对齐都不需要额外状态。
3. **视口主权在用户。** agent 可以放节点，不能滚动视口。用户手动平移或点过别的标签之后，自动聚焦让位。
4. **实时事件只做信号。** 事件说"哪块变了"，不说"变成什么"；断线重连靠重新拉取补齐。这条直接沿用仓库里的既有约定。

## 3.14 功能架构：三层改造，两处复用

{{< fig "diagrams/01-canvas-architecture.svg" "功能架构。三个新增点（空间模型、落点投影、视口状态）分别落在数据层、服务层与视图层，其余全部复用现有链路。" >}}

改动按 ① ② ③ 分三处，落在数据层、服务层与视图层。表里列每处新增什么、复用仓库里的哪一块：

| 改造点 | 新增 | 复用 |
| --- | --- | --- |
| ① 空间模型 | `canvas_nodes` 表；`tabs_state.state_json` 里加 `canvas` 字段 | `tabs` / `tabs_state` 已有的 JSON 状态位（`db.ts:333`、`:342`） |
| ② 落点投影 | `apps/daemon/src/canvas/{store.ts,project.ts,routes.ts}` | `project-watchers.ts` 的 `file-changed`、`reconcileHtmlArtifactManifest`、`emitProjectEvent` |
| ② 的契约 | `packages/contracts/src/api/canvas.ts`：DTO 与纯函数（`layoutNodes`、`clampViewport`、`nodeAtPoint`） | `contracts` 只放类型与纯函数的老规矩 |
| ③ 视口状态 | `CanvasSurface`、`NodeFrame`、`ViewportController` | `FileWorkspace` 的标签集合、`FileViewer` 的 iframe 与沙箱配置、`preview-url` 的 scope |
| 工具面 | `canvas.place` 操作、MCP 一个工具 | `tool-tokens.ts` 的 17 条 endpoint / 16 条 operation 白名单机制 |

三条边界值得先写死，否则实现时会互相打架：

- **画布不持有文件内容。** 节点上只有 `ref`（项目相对路径）与几何信息。渲染时向 `preview-url` 要地址，和标签页走同一条预览通道。
- **标签与画布是两套视图，不是两套数据。** 画布上选中节点等价于选中标签；标签区切回画布时，选中的还是那个节点。
- **daemon 不做布局计算。** 布局纯函数在 `contracts` 里，daemon 只在写入时落一次结果；重新计算由客户端或脚本触发，保证可重放。

## 3.15 数据模型：一张表加一个 JSON 字段

节点表：

```sql
CREATE TABLE IF NOT EXISTS canvas_nodes (
  project_id TEXT NOT NULL,
  id         TEXT NOT NULL,
  kind       TEXT NOT NULL,          -- artifact | frame | group | note
  ref        TEXT,                   -- 项目相对路径；frame/group 为空
  x          REAL NOT NULL,
  y          REAL NOT NULL,
  w          REAL NOT NULL,
  h          REAL NOT NULL,
  z          INTEGER NOT NULL DEFAULT 0,
  parent_id  TEXT,                   -- 只指向 frame / group
  rev        INTEGER NOT NULL DEFAULT 1,
  origin     TEXT NOT NULL,          -- projection | user | agent
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (project_id, id),
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_canvas_nodes_ref
  ON canvas_nodes(project_id, ref) WHERE ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_canvas_nodes_bounds
  ON canvas_nodes(project_id, x, y);
```

四个约束各有理由：

1. `UNIQUE(project_id, ref)` 让落点幂等。同一个入口文件无论被投影几次，只有一个节点。
2. `rev` 每次写加一，冲突时用它做比较，不做合并。协作没有 CRDT，最后写的赢。
3. `parent_id` 只允许指向 `frame` 或 `group`，在 store 层校验。层级只有两层，不做任意树。
4. 删除节点只删行，`ref` 指向的文件不动。反过来，文件消失时节点转 `missing` 而不是删除，因为产物可能只是暂时移动。

视口不建表，挂在已有的 `tabs_state.state_json` 上：

```ts
// packages/contracts/src/api/projects.ts 的 ProjectTabsState 增补
canvas?: {
  mode: 'tabs' | 'canvas';          // 右侧面板当前视图
  view: { x: number; y: number; zoom: number };
  selected: string[];               // 节点 id
  updatedAt: number;
};
```

理由是这个表本来就是"每个项目一段 JSON"的状态位（`setTabs` 整段覆盖写，`db.ts:5977`），视口正好是同类状态。省一次迁移，也省一张只有一行的表。

**布局纯函数**的输入只有产物清单，输出坐标：

```ts
// 按 kind 分道，道内按 mtime 升序，道宽 = 最大节点宽 + 40
export function layoutNodes(items: CanvasItem[], opts: LayoutOptions): PlacedNode[] {
  const lanes = groupBy(opts.laneOrder, (kind) => items.filter((i) => i.kind === kind));
  let cursorX = opts.originX;
  const out: PlacedNode[] = [];
  for (const [kind, list] of lanes) {
    let cursorY = opts.originY;
    for (const item of [...list].sort((a, b) => a.mtime - b.mtime)) {
      out.push({ id: item.id, kind, ref: item.ref, x: cursorX, y: cursorY,
                 w: opts.size[kind].w, h: opts.size[kind].h });
      cursorY += opts.size[kind].h + opts.gapY;
    }
    cursorX += Math.max(...list.map(() => opts.size[kind].w), 0) + opts.gapX;
  }
  return out;
}
```

它不读数据库、不读时间、不用随机数。给定同一批输入，重放得到同一张画布，导出 PNG 也不会每次都不同。

## 3.16 落点时序：从写盘到上画布

{{< fig "diagrams/02-canvas-sequence.svg" "落点时序。前三步是现有链路，第四步起是新增：投影、信号、增量渲染。视口只在用户空闲时跟随，且用户操作过就不再跟随。" >}}

七步，前四步与 3.10 那条链路同源：

1. CLI 子进程写文件，落在项目目录。daemon 不代理写。
2. chokidar 发出 `file-changed`，daemon 收尾时补边车（`reconcileHtmlArtifactManifest`）。
3. 投影器读产物清单（入口加边车），算出 `layoutNodes`，按 `UNIQUE(project_id, ref)` 幂等 upsert。新节点带 `origin: 'projection'`。
4. daemon 发一条 `canvas-changed { projectId, nodeIds }` 到 `/api/projects/:id/events`。只有 id，没有几何与内容，符合"事件是信号"的约定。仓库原文是 "A missed event during a disconnect is harmless (the reconnect snapshot re-fetch closes the gap)"，这句话写在 `packages/contracts/src/sse/collab.ts` 的注释里。
5. 前端收到信号后重新拉 `GET /api/projects/:id/canvas`，用返回的 `rev` 做差量渲染。断开重连时同样走这条全量拉取，不需要 Last-Event-ID 回放。
6. 视口按规则决定要不要动：用户空闲超过 800ms、没有手动平移过、且新节点在视口外时才平移一次。用户点过画布或拖动过，`userTookOverPreviewRef` 同款的让位标记置真，本轮不再跟随。
7. 节点进入渲染队列：只渲染视口外扩一屏内的节点；`zoom < 0.35` 时用静态快照替代 iframe；同屏 iframe 上限 24 个，超出按距离视口中心的远近排队。

第 7 步是这套设计里唯一有硬预算的地方。一个节点一个 iframe，二十个就是二十份 JS 堆，所以必须有降级档位：

| 缩放 | 渲染 | 触发条件 |
| --- | --- | --- |
| ≥ 0.35 | 真 iframe，`preview-url` 的 scope 复用 | 正常编辑 |
| < 0.35 | 最近一次截图或封面图 | 快速浏览全图 |
| 完全不可见 | 不渲染，只占位 | 视口剔除 |

截图从哪来：复用导出链路里已有的 Electron `capturePage`，在节点首次 `ready` 后抓一张，存在数据根的 `canvas/posters/<nodeId>.png`。没有桌面进程时退回占位色块加标题，不阻塞。

## 3.17 节点状态与失败面

{{< fig "diagrams/03-node-states.svg" "节点四态。文件是真相，节点只是它的投影：文件在而边车未到是 pending，文件改动是 stale，文件消失是 missing。" >}}

四个状态，迁移条件都能从现有信号推出来，不需要新轮询：

- `pending`：磁盘上有入口文件，边车还没写。来源是 `file-changed` 先到、`reconcileHtmlArtifactManifest` 还没跑完的那段窗口。
- `ready`：边车存在且 `version` 为 1、`entry` 指回自己。渲染走预览通道。
- `stale`：文件 mtime 变了。此时保留上一次渲染，角标提示"有新版本"，重载后回到 `ready`。
- `missing`：`file-changed` 的 `kind` 是 `unlink`，或重新扫盘时文件不在。节点留在原位，渲染成虚线框；用户删除节点才是真删行。

三个已知风险，按发生概率排：

1. **iframe 内存。** 上限 24 个只是拍脑袋的初值，要按节点平均 DOM 大小实测后调。降级档位是唯一的安全阀。
2. **协作冲突。** 两个人同时拖同一个节点，`rev` 小的那次写被拒，前端回滚到服务端值。画布不做意图合并，只做拒绝与重放。
3. **布局漂移。** 一旦布局函数混入当前时间或随机数，重放就对不上。约束是纯函数，验收时用同一批输入跑两遍比对坐标。

## 3.18 上线路径与回滚

开关四层，从窄到宽，和 Design Jury 的 rollout 同一套形状：

1. 项目级：`metadata.canvas === true`，只对指定的项目开。
2. 环境级：`OD_CANVAS_ENABLED=1`，本机全开。
3. 默认：关。右侧面板仍是标签。

数据迁移是纯增量：`CREATE TABLE IF NOT EXISTS canvas_nodes` 加一个索引，不动任何老表；`tabs_state.state_json` 多一个可选字段，老客户端读到未知字段会忽略。仓库里已经有 63 处 `ALTER TABLE` 守卫，这套写法是既有惯例。

回滚只关视图：开关关掉，画布不渲染，表与数据留在库里。重新打开时投影器幂等重算，不会重复建节点。

验收五条，每条都能机械判定：

1. 一轮生成三个产物，画布上出现三个节点，坐标与 `layoutNodes` 的纯函数输出一致。
2. 同一批产物重放两次，节点 `id` 与坐标完全相同。
3. 删除一个节点后，对应文件仍然存在。
4. 断开 SSE 十秒再恢复，画布内容与恢复前一致，无重复节点。
5. 用户拖动视口后，新产物到达时视口不跳。

---

下一篇 [端到端](/posts/open-design-walkthrough.html) 用一个真实项目走完从建项目到导出的九步。回到 [分册索引](/posts/open-design-architecture.html#分册索引)。
