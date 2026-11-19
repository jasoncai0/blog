---
title: "OpenDesign 与编码 Agent：四处分岔"
date: 2026-11-19T19:04:00+08:00
summary: "完成判据、知识形态、评审定位、模态供给线，四个维度对比设计 Agent 与编码 Agent；附六轴对照表与 Design Jury 的开关优先级。"
tags: ["architecture", "agent"]
slug: "open-design-vs-coding-agent"
series: "open-design"
series_order: 6
---


> 源码版本：`nexu-io/open-design` `0725880`（package.json 0.27.2）。本地 clone 后逐文件核对，行号都对这个 commit。仓库 14,308 个文件，`apps/daemon/src/server.ts` 一个文件 18,959 行。

本系列 8 篇 · [总览](/posts/open-design-architecture.html) · 上一篇 [端到端](/posts/open-design-walkthrough.html) · 下一篇 [生成面](/posts/open-design-media.html)

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

---

下一篇是[生成面](/posts/open-design-media.html)。回到[总览](/posts/open-design-architecture.html)，或者从[分册索引](/posts/open-design-architecture.html#分册索引)挑一个方向。
