---
title: "GitHub 周趋势 2026-W48：技能包占榜，热度退潮"
date: "2026-11-23T14:15:00+08:00"
summary: "13 个项目里 7 个是 AI 编程技能包，周增总量降 31.5%；Univer 以 +6,436 领跑。"
description: "GitHub Trending 周榜 2026-W48 复盘：本周只返回 13 个项目，周内新增星合计 42,050，比上周少 31.5%；AI 编程类占比升到 56.6%，但主要因为其他类别退潮。"
tags: ["github-trending"]
slug: "github-trending-2026-w48"
---

> 口径：GitHub Trending `since=weekly`，抓取于 2026-11-23 14:11（Asia/Shanghai），共 13 个项目；星数为榜单显示值，与仓库实时值可能差几十。分类词表见文末。

本周榜单只剩 13 个项目，上周是 16 个。周内新增星合计 42,050，比上周的 61,366 少 31.5%。数量与总量都在退，但榜面结构没怎么换：13 个里 11 个上周也在，只有 HermesEnabler 和 ffmpeg-skill 是新面孔。

撑住场面的还是 AI 编程类。7 个项目合计周增 23,788，和上周 AI 类的 23,291 几乎持平；占比从 37.9% 升到 56.6%。这不是 AI 类变热了，是别的类别退得更多。

单点上看，Univer 以 +6,436 排本周新增第一，last30days 以 +4,731 第二；总星最高的仍是 last30days（72,929）和 chrome-devtools-mcp（58,897），但它们的新增占比只有 6.5% 和 3.9%，靠存量。

## 1. 本周全貌

{{< fig "diagrams/01-category.svg" "本周 13 个项目按类别分布；条长 = 项目数，右侧 = 该类周内新增 ★ 合计。AI 编程代理 7 个、23,788 ★，其他 3 个、8,668 ★。" >}}

| # | 项目 | 一句话 | 类别 | 本周 +★ | 总 ★ |
|---|------|--------|------|--------:|-----:|
| 1 | [backnotprop/plannotator](https://github.com/backnotprop/plannotator) | [给编码代理的计划批注与评审工作台 ↓](#p-plannotator) | AI 编程代理 | 2,815 | 14,503 |
| 2 | [google-labs-code/stitch-skills](https://github.com/google-labs-code/stitch-skills) | [让编码代理调用 Stitch 做 UI 的技能包 ↓](#p-stitch-skills) | AI 编程代理 | 2,683 | 12,013 |
| 3 | [GoogleChrome/modern-web-guidance-src](https://github.com/GoogleChrome/modern-web-guidance-src) | [把现代 Web 最佳实践喂给编码代理 ↓](#p-modern-web-guidance-src) | AI 编程代理 | 3,575 | 10,282 |
| 4 | [alibaba/open-code-review](https://github.com/alibaba/open-code-review) | [阿里开源的行级 AI 代码审查 CLI ↓](#p-open-code-review) | 开发者工具 | 3,700 | 55,488 |
| 5 | [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) | [跨 Reddit/X/HN 调研近期话题的技能 ↓](#p-last30days-skill) | AI 编程代理 | 4,731 | 72,929 |
| 6 | [ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) | [让编码代理操控 Chrome 调试与测速 ↓](#p-chrome-devtools-mcp) | 开发者工具 | 2,268 | 58,897 |
| 7 | [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) | [给个人投资者的复盘选股交易技能集 ↓](#p-claude-trading-skills) | AI 编程代理 | 2,616 | 7,833 |
| 8 | [ag-ui-protocol/ag-ui](https://github.com/ag-ui-protocol/ag-ui) | [把任意 AI 代理接进前端应用的事件协议 ↓](#p-ag-ui) | AI 编程代理 | 3,620 | 21,246 |
| 9 | [meta/astryx](https://github.com/meta/astryx) | [Meta 开源的 React 设计系统，150+ 组件 ↓](#p-astryx) | 前端与设计 | 3,626 | 32,316 |
| 10 | [pookjw/HermesEnabler](https://github.com/pookjw/HermesEnabler) | [在任意 Apple Watch 上启用爱马仕表盘 ↓](#p-hermesenabler) | 其他 | 461 | 2,413 |
| 11 | [we-promise/sure](https://github.com/we-promise/sure) | [可自托管的个人记账与财富管理应用 ↓](#p-sure) | 其他 | 1,771 | 17,396 |
| 12 | [dream-num/univer](https://github.com/dream-num/univer) | [可嵌入产品的表格文档协作 SDK ↓](#p-univer) | 其他 | 6,436 | 31,324 |
| 13 | [kajisho5/ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) | [让编码代理本地剪视频的 FFmpeg 技能 ↓](#p-ffmpeg-skill) | AI 编程代理 | 3,748 | 9,860 |

这张表按 GitHub 周榜原始顺序排，不是按新增星排。按新增星重排，前五是 Univer（+6,436）、last30days（+4,731）、ffmpeg-skill（+3,748）、open-code-review（+3,700）、astryx（+3,626）。原始榜第 1 的 plannotator 只排到第 8。

## 2. 分类拆解

### 2.1 AI 编程代理（7 个）

这一类本周的关键词是「给代理加能力」：调研、设计、剪视频、写前端、做交易复盘，都是挂在现成编码代理上的技能或配套工具。最值得先看的是 last30days，它是本周仅有的两个同时出现在周榜和日榜的项目之一，增量排第二。

<a id="p-last30days-skill" style="scroll-margin-top:96px"></a>**[mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill)**｜定位：给代理用的跨平台调研器，输入话题就抓 Reddit、X、YouTube、HN、Polymarket 近 30 天的讨论并按互动量排序。｜场景：写选题、见人前做功课、扫描某个产品近一个月的真实口碑，需要「大家现在怎么说」而不是通用搜索。｜选型：零配置能跑 Reddit/HN/Polymarket/GitHub，其余平台靠首次运行向导接凭据；和通用搜索比，它赢在按点赞、播放、真金白银排序且给出处，输在结果依赖各平台接口、窗口固定 30 天。｜本周上榜：连续第 2 周，周增从 1,548 抬到 4,731，是本周第二增量，总星 72,929 全场最高。

<a id="p-ffmpeg-skill" style="scroll-margin-top:96px"></a>**[kajisho5/ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill)**｜定位：让编码代理在本地剪视频的技能包，42 个 FFmpeg 工具加一个 MCP 服务。｜场景：手里有原始素材，要切段、加字幕、改 9:16、统一响度、按平台导出，但不想开剪辑软件。｜选型：全程本地、不接云、不要 API key，Python 标准库就能跑；和云端视频 API 比，它赢在素材不出机器、成本为零，输在没有时间线 GUI，复杂转场和调色别指望。｜本周上榜：新上榜，+3,748 直接排新增第 3；9,860 的总星里有 38% 是这一周加的，是本期弹性最大的项目。

<a id="p-ag-ui" style="scroll-margin-top:96px"></a>**[ag-ui-protocol/ag-ui](https://github.com/ag-ui-protocol/ag-ui)**｜定位：把任意代理框架接到前端应用的事件协议，流式 token、工具调用、共享状态、人工介入都走一条双向事件流。｜场景：已经用 LangChain、CrewAI、Mastra、Google ADK 之类搭了代理，现在要给它做一个 Web、移动端或 Slack 界面。｜选型：前端用 CopilotKit 时集成最省事，TypeScript 和 Python SDK 都有；只用一个框架且前后端同栈时，框架自带接口可能更简单，别为了「标准」多加一层。｜本周上榜：连续第 2 周，+3,620 与上周 +2,773 相比仍在加速，21,246 的总星里 17% 来自本周。

<a id="p-modern-web-guidance-src" style="scroll-margin-top:96px"></a>**[GoogleChrome/modern-web-guidance-src](https://github.com/GoogleChrome/modern-web-guidance-src)**｜定位：把现代 Web 平台能力和兼容性数据做成技能，喂给编码代理，纠正它爱用旧写法的毛病。｜场景：代理生成的页面总用过时 API、忽略新特性，或者团队要统一一套现代 Web 写法。｜选型：需要 Node.js 22 以上，装完由 CLI 按任务返回精选指南；和让代理直接读 MDN 比，它赢在按场景裁剪、带兼容性判断，输在覆盖面仍有限，冷门 API 还是得查文档。｜本周上榜：连续第 2 周，+3,575 是本周第 7，但 10,282 的总星里有 34.8% 是这周加的，属于小体量高占比。

<a id="p-plannotator" style="scroll-margin-top:96px"></a>**[backnotprop/plannotator](https://github.com/backnotprop/plannotator)**｜定位：给编码代理加一层「批注与评审」工作台，计划、规格、diff、HTML 产物都能圈选留言，再把反馈一键送回代理。｜场景：代理一次改十几个文件，你想在 diff 上逐行提意见，而不是在聊天框里描述「第 3 个文件那个函数」。｜选型：一个安装脚本能配好 Claude Code、Codex、Copilot、Gemini CLI 等；只跑简单任务、很少审计划的话，这层工作台是额外负担。｜本周上榜：连续第 2 周，周增 2,815 比上周的 2,926 略低，但原始榜排到第 1，说明同期对手退得更多。

<a id="p-stitch-skills" style="scroll-margin-top:96px"></a>**[google-labs-code/stitch-skills](https://github.com/google-labs-code/stitch-skills)**｜定位：Google Stitch 的设计技能包，把设计稿转成 React、React Native 组件，也能把现有前端代码反向上传成 Stitch 设计。｜场景：设计在 Stitch 里出，代码要让代理按设计系统落地，减少手抄样式。｜选型：需要 Stitch 账号和 MCP 服务，Codex、Claude Code、Cursor 有插件安装路径；设计源头不在 Stitch 的话，这套技能没有优势，去看对应设计工具的 MCP。｜本周上榜：连续第 2 周，+2,683 比上周 +1,197 翻倍，是本周日榜与周榜同时出现的两个项目之一。

<a id="p-claude-trading-skills" style="scroll-margin-top:96px"></a>**[tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)**｜定位：给个人投资者的交易流程技能集，覆盖市场状态、组合复盘、选股、仓位和交易日志。｜场景：想把每周复盘、风险检查、交易记录变成固定流程，而不是每天临时问 AI「今天买什么」。｜选型：不接付费数据也能用公开 CSV 和图表截图跑基础流程，Alpaca、FMP 是可选增强；它明确不做自动下单，想要信号或托管执行的话别选它。｜本周上榜：连续第 2 周，+2,616 与上周 +2,985 接近；7,833 的总星里 33.4% 来自本周，是小体量里热度最集中的项目之一。

### 2.2 开发者工具（2 个）

两个都是「让代理直接动手」的工具，一个审代码，一个调浏览器。共同点是替代理补上它看不见的那部分环境。

<a id="p-open-code-review" style="scroll-margin-top:96px"></a>**[alibaba/open-code-review](https://github.com/alibaba/open-code-review)**｜定位：阿里内部用了两年后开源的代码审查 CLI，规则引擎加 LLM 代理，按行给评论。｜场景：CI 里要一道稳定的自动审查，或者本地提交前想让代理先扫一遍 NPE、线程安全、XSS、SQL 注入这类硬问题。｜选型：`npm install -g @alibaba-group/open-code-review` 后直接用 `ocr`，要求 Git 2.41 以上；原生模式要配模型端点，委派模式则复用宿主代理的模型和额度。和通用编码代理比，它赢在有确定性规则兜底、输出稳定，输在规则集对非阿里技术栈的覆盖要自己验。｜本周上榜：连续第 2 周，+3,700 与上周 +3,675 基本持平；55,488 的总星是全场第三，但本周只加了 6.7%。

<a id="p-chrome-devtools-mcp" style="scroll-margin-top:96px"></a>**[ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp)**｜定位：把 Chrome DevTools 包成 MCP 服务和 CLI，让代理能开页面、看控制台、抓网络请求、录性能。｜场景：代理改完前端要自己验证，或者线上问题需要有人（或代理）复现并给出性能证据。｜选型：Node LTS 环境加一条 MCP 配置即可，代理用到浏览器时才启动 Chrome；和 Playwright 类方案比，它赢在真实 Chrome 的性能面板和调试协议，输在多浏览器覆盖和跨平台一致性。｜本周上榜：连续第 2 周，+2,268 比上周 +2,948 降了 23%，58,897 的总星里只有 3.9% 来自本周，热度在往存量靠。

### 2.3 前端与设计（1 个）

<a id="p-astryx" style="scroll-margin-top:96px"></a>**[meta/astryx](https://github.com/meta/astryx)**｜定位：Meta 内部用了八年的设计系统开源版，150+ 可访问组件、品牌主题、暗色模式和给代理用的 CLI。｜场景：React 19 新项目或内部工具，需要一套开箱即用、人和代理都能查的组件库。｜选型：要求 React 19 和 StyleX，目前是 Beta，API 还会动；和 shadcn/ui 比，它赢在组件密度、主题体系和 CLI 文档，输在生态成熟度和可改造的自由度。｜本周上榜：连续第 2 周，+3,626 比上周的 +8,700 回落约 58%，32,316 的总星里 11.2% 来自本周，属于首周爆发后的降温。

### 2.4 其他（3 个）

三个都归在「其他」，但都踩中了「把专业软件的能力搬到个人手里」：Office 文档、个人财务、手表表盘。

<a id="p-univer" style="scroll-margin-top:96px"></a>**[dream-num/univer](https://github.com/dream-num/univer)**｜定位：可嵌入产品的表格与文档协作 SDK，Apache-2.0 核心覆盖编辑、公式、渲染和 Headless 计算。｜场景：要在自己的产品里放一个 Excel/Word 式编辑器，或者让代理读写表格内容。｜选型：开源核心能商用，但协同后端、Server SDK、商业 AI 扩展另售，免费试用带水印；和直接嵌 SaaS 表格比，它赢在可控、可改、能进自有工作流，输在要自己接存储和协同。｜本周上榜：连续第 2 周，+6,436 是本周新增第一，比上周 +5,845 还高；31,324 的总星里 20.5% 是这一周加的，连续两周加速。

<a id="p-sure" style="scroll-margin-top:96px"></a>**[we-promise/sure](https://github.com/we-promise/sure)**｜定位：可自托管的个人记账与财富管理应用，接手了停更的 Maybe Finance 代码。｜场景：不想把账户数据交给托管记账服务，又愿意自己跑一个 Docker 容器。｜选型：Docker 自托管，AGPLv3，浏览器、macOS、移动端和 API 都能接；和托管记账 App 比，它赢在数据在自己手里、可接代理，输在部署、备份和银行连接都要自己维护。｜本周上榜：连续第 2 周，+1,771 比上周 +4,786 明显回落，17,396 的总星里 10.2% 来自本周。

<a id="p-hermesenabler" style="scroll-margin-top:96px"></a>**[pookjw/HermesEnabler](https://github.com/pookjw/HermesEnabler)**｜定位：让非爱马仕版 Apple Watch 用上爱马仕表盘的自签名应用。｜场景：手上是普通 Apple Watch，又喜欢那几款表盘，愿意折腾一次侧载。｜选型：只支持 watchOS 27.0 到 27.3 beta 1，要用 Sideloadly 或 WatchBridge 自签，还要开开发者模式；不想折腾、或系统版本不在窗口内的，别碰。｜本周上榜：新上榜，+461 是本期最小增量；2,413 的总星说明它是小圈子话题，样本只有一周，别当趋势信号。

## 3. 热度结构

{{< fig "diagrams/02-rank.svg" "按本周新增 ★ 降序排列；条形右侧为总 ★。Univer 以 +6,436 排第一，HermesEnabler 只有 +461。" >}}

{{< fig "diagrams/03-scatter.svg" "横轴为总 ★（对数），纵轴为本周 +★；圆点越大总 ★ 越高。悬停任意圆点可看项目名与两个数字。" >}}

存量最大的一批没有跟着放量。last30days 总星 72,929、本周 +4,731，新增只占总星 6.5%；chrome-devtools-mcp 58,897、+2,268，占 3.9%；open-code-review 55,488、+3,700，占 6.7%。三个 5 万星以上的项目合计贡献 10,699，占总增量的 25.4%，但新增占总星都压在 7% 以下。

增量弹性在 1 到 3 万星这一段。Univer 31,324、+6,436，单周加了两成；ag-ui 21,246、+3,620，占 17%；modern-web-guidance 10,282、+3,575，占 34.8%；ffmpeg-skill 9,860、+3,748，占 38%。本周没有「千星小项目冲进前十」的极端样本，最小的是 HermesEnabler 的 2,413 总星，但它周增只有 461，热度没起来。

## 4. 趋势判断

{{< fig "diagrams/04-trend.svg" "柱 = 每周上榜项目新增 ★ 总和，折线 = 新上榜项目占比（右轴）。本周总量 42,050，新上榜 2/13，换血速度 15.4%。" >}}

1. **AI 编程类的绝对量没退，占比是被动抬高的。** 本周 AI 类周增 23,788，上周 23,291，几乎持平；总榜从 61,366 掉到 42,050，它的占比才从 37.9% 升到 56.6%。判断「AI 更火了」会看错，实际是别的类别缩了。
2. **换血几乎停滞，热度集中在上半周。** 13 个里 11 个连续在榜，新面孔只有 2 个；周榜与日榜的交集只有 last30days 和 stitch-skills 两个。周五还能留在日榜的项目这么少，说明本周没有新的爆发点。
3. **开发者工具在往「代理接口」收敛。** 本周两个 devtools 项目（open-code-review、chrome-devtools-mcp）都不是给人直接用的 GUI，而是给代理调用的 CLI/MCP；再加上 ffmpeg-skill 和 plannotator，13 个项目里有 4 个明确是代理的执行层。选型时该问的问题从「这工具好不好用」变成「我的代理能不能调它」。
4. **大体量项目靠存量，新增看中小体量。** 5 万星以上的三个项目本周只加了 3.9% 到 6.7%，而 1 到 3 万星区间的项目单周能加 17% 到 38%。榜单的动能已经从「大项目继续涨」转到「中项目被看见」。

## 5. 值得这周动手试的 3 个

- **open-code-review**：装一个全局 npm 包，配好模型端点，在仓库里跑一次 `ocr review`，10 分钟能看到按行评论。最可能卡在模型额度和规则集对自家技术栈的覆盖。
- **ffmpeg-skill**：本机要有 FFmpeg，再装技能包，找一段素材让它切 9:16 加字幕，30 分钟内能出片。最可能卡在 FFmpeg 编译选项和中文字体路径。
- **plannotator**：跑一条安装脚本，让代理进入计划模式，在计划或 diff 上圈选留言，10 分钟能试完。最可能卡在代理版本与 hook 的兼容性。

## 6. 与上周对比

| 项 | 本周 | 上周 |
|---|---:|---:|
| 上榜项目数 | 13 | 16 |
| 新上榜 | 2 | 16（首期，无历史基线） |
| 周内新增 ★ 合计 | 42,050 | 61,366 |
| 最大单类 | AI 编程代理 7 个 | AI 编程代理 7 个 |

- 连续在榜：11 个，均为连续 2 周（plannotator、stitch-skills、modern-web-guidance-src、open-code-review、last30days、chrome-devtools-mcp、claude-trading-skills、ag-ui、astryx、sure、univer）。
- 掉榜：5 个（Claude-BugHunter、anthropics/skills、daily_stock_analysis、vercel-labs/agent-browser、openai/codex-security）。
- 周增总量减少 19,316（-31.5%）；新上榜占比 15.4%，上周因是首期收录，没有可比基线。

## 附：分类口径

八类固定为 AI 编程代理、模型与推理、开发者工具、前端与设计、数据与基础设施、安全、学习与资源、其他。按项目实际用途归类，不按语言或公司；放不进前七类的归「其他」，本周是 Univer、Sure、HermesEnabler 三个。
