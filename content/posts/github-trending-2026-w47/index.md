---
title: "GitHub 周趋势 2026-W47：Agent 技能包主导"
date: "2026-11-18T19:10:05+08:00"
summary: "本周 16 个上榜项目里 6 个是给 agent 的技能包，但第一名是 Meta 的设计系统 astryx，一周 8,700 星。"
description: "2026-W47 GitHub Trending 周榜：16 个项目按八类汇总，含分类占比、新增星排行与体量热度图，每个项目给定位、适用场景和选型建议。"
tags: ["github-trending"]
slug: "github-trending-2026-w47"
---

> 口径：GitHub Trending `since=weekly`，抓取于 2026-11-18 19:12（Asia/Shanghai），共 16 个项目；星数为榜单显示值，与仓库实时值可能差几十。分类词表见文末。

这周最集中的形态不是产品，是「给 agent 的料」：16 个项目里 6 个是技能包，横跨 AI 编程与安全两类，合计 20,100 星。它们大多不是让人打开用的，而是让 Claude Code、Codex 这类工具多会一件事。

但榜首不在这条线上。Meta 的 astryx 一周 8,700 星，是第二名 1,300 星之外的单点爆发，而且它面向人：一套 React 组件库。榜单同时在往两个方向走，一个是 agent 的供给侧，一个是人的工作台。

## 1. 本周全貌

{{< fig "diagrams/01-category.svg" "本周 16 个项目按类别分布；条长 = 项目数，右侧 = 该类周内新增 ★ 合计。" >}}

| # | 项目 | 一句话 | 类别 | 本周 +★ | 总 ★ |
|---|------|--------|------|--------:|-----:|
| 1 | [meta/astryx](https://github.com/meta/astryx) | [Meta 开源设计系统，组件与 token 成套 ↓](#p-astryx) | 前端与设计 | 8,700 | 30,389 |
| 2 | [GoogleChrome/modern-web-guidance-src](https://github.com/GoogleChrome/modern-web-guidance-src) | [Chrome 的 web 实践技能包，给 agent 用 ↓](#p-modern-web-guidance-src) | AI 编程代理 | 7,015 | 8,642 |
| 3 | [dream-num/univer](https://github.com/dream-num/univer) | [可嵌入产品的 office SDK，表格起步 ↓](#p-univer) | 其他 | 5,845 | 29,758 |
| 4 | [anthropics/skills](https://github.com/anthropics/skills) | [Claude 技能包参考实现，写 skill 照抄 ↓](#p-skills) | AI 编程代理 | 4,847 | 196,973 |
| 5 | [we-promise/sure](https://github.com/we-promise/sure) | [自托管个人财务面板，社区接盘版 ↓](#p-sure) | 其他 | 4,786 | 16,371 |
| 6 | [alibaba/open-code-review](https://github.com/alibaba/open-code-review) | [阿里评审助手开源版，CI 里的 AI 评审 ↓](#p-open-code-review) | 开发者工具 | 3,675 | 54,073 |
| 7 | [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) | [多市场选股分析，每天推看板到 IM ↓](#p-daily-stock-analysis) | 其他 | 3,653 | 73,243 |
| 8 | [vercel-labs/agent-browser](https://github.com/vercel-labs/agent-browser) | [给 agent 的浏览器 CLI，读无障碍树 ↓](#p-agent-browser) | 开发者工具 | 3,429 | 52,510 |
| 9 | [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) | [散户交易技能包，行情到筛股一条流程 ↓](#p-claude-trading-skills) | AI 编程代理 | 2,985 | 6,969 |
| 10 | [ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) | [把 DevTools 交给 agent 自查页面 ↓](#p-chrome-devtools-mcp) | 开发者工具 | 2,948 | 58,139 |
| 11 | [backnotprop/plannotator](https://github.com/backnotprop/plannotator) | [agent 计划的批注台，动手前先圈问题 ↓](#p-plannotator) | AI 编程代理 | 2,926 | 13,246 |
| 12 | [ag-ui-protocol/ag-ui](https://github.com/ag-ui-protocol/ag-ui) | [agent 与前端的事件协议，接流式输出 ↓](#p-ag-ui) | AI 编程代理 | 2,773 | 20,259 |
| 13 | [openai/codex-security](https://github.com/openai/codex-security) | [OpenAI 安全 CLI，找漏洞并给修复 ↓](#p-codex-security) | 安全 | 2,531 | 14,029 |
| 14 | [elementalsouls/Claude-BugHunter](https://github.com/elementalsouls/Claude-BugHunter) | [漏洞挖掘技能包，按 24 类组织报告模式 ↓](#p-claude-bughunter) | 安全 | 2,508 | 10,280 |
| 15 | [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) | [跨社媒研究技能，按互动量排序 ↓](#p-last30days-skill) | AI 编程代理 | 1,548 | 69,540 |
| 16 | [google-labs-code/stitch-skills](https://github.com/google-labs-code/stitch-skills) | [Stitch 配套技能集，设计稿接到代码 ↓](#p-stitch-skills) | AI 编程代理 | 1,197 | 10,262 |

表里最该注意的是：AI 编程代理这一类 7 个项目就吃掉 23,291 星，占全榜 38%，但其中没有一个在做「更好的模型」或「更快的推理」，全在做 agent 周边。

## 2. 分类拆解

### 2.1 AI 编程代理（7 个）

这一类的共同取向是「把知识打包成可加载的模块」。7 个项目里 5 个是技能包，1 个是前端协议，只有 1 个是给人用的评审工作台。最值得先看的是 modern-web-guidance-src，因为它解决的是每个前端团队都会碰到的问题。

<a id="p-modern-web-guidance-src" style="scroll-margin-top:96px"></a>**[modern-web-guidance-src](https://github.com/GoogleChrome/modern-web-guidance-src)**｜定位：Google Chrome 团队把 web 平台最佳实践整理成 agent 技能，让编码 agent 少写过期 API 和兼容性坑。场景：agent 生成前端代码，你要它别再写 `document.write` 那类老写法，也不想每次手动贴 MDN 链接。选型：和「在 prompt 里塞一堆规范文档」比，它体积小、可加载、随 Chrome 更新；代价是视角只有 Chrome，Safari 和 Firefox 的边角差异仍要人看。本周上榜：一周 7,015 星，主仓发布后配套源仓库被一起带起来。

<a id="p-skills" style="scroll-margin-top:96px"></a>**[skills](https://github.com/anthropics/skills)**｜定位：Claude 官方放出的技能包参考实现，回答「一个 skill 该长什么样」。场景：你准备给 agent 加一套领域能力，先找一个结构抄。选型：当模板和范式看，别当框架依赖；真正的业务逻辑还是自己写。本周上榜：一周 4,847 星，总星 19.7 万，属于存量项目被新一波 skill 热重新带起。

<a id="p-claude-trading-skills" style="scroll-margin-top:96px"></a>**[claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)**｜定位：面向个人投资者的 Claude 技能包，覆盖行情、技术图、经济日历、筛股和策略复盘。场景：你有自选股清单，想让 agent 每天按固定流程过一遍。选型：当流程模板用，数据源要自己接；把它当投资建议会出事。本周上榜：一周 2,985 星，README 里明说作者是拿自己的交易流程做的。

<a id="p-plannotator" style="scroll-margin-top:96px"></a>**[plannotator](https://github.com/backnotprop/plannotator)**｜定位：把 agent 的计划、规格、diff 变成可批注的界面，反馈再一次回给 agent。场景：让 agent 动手改代码之前，你想先看它打算怎么做，并且能圈出第三段。选型：和「直接让 agent 干活再回滚」比，它把评审提前，适合改动面大的任务；一两行的小修会显得多余。本周上榜：一周 2,926 星，纳入 inbox、artifact、代码评审三块工作台。

<a id="p-ag-ui" style="scroll-margin-top:96px"></a>**[ag-ui](https://github.com/ag-ui-protocol/ag-ui)**｜定位：agent 和前端之间的交互协议，把流式输出、工具调用、状态同步定成事件格式。场景：你要把 agent 嵌进自己的 web 应用，不想每次自研一套 SSE 协议。选型：和自研比，规范统一、生态在起；早期项目接入要接受规范还在动。本周上榜：一周 2,773 星，属于协议类项目里少见的持续在榜。

<a id="p-last30days-skill" style="scroll-margin-top:96px"></a>**[last30days-skill](https://github.com/mvanhorn/last30days-skill)**｜定位：跨 Reddit、X、YouTube、HN、Polymarket 的研究技能，按互动量和真金白银的赔率排序，再合成一份摘要。场景：做市场调研或热点复盘，需要「最近 30 天大家真正在讨论什么」。选型：比通用搜索多了排序依据；结果仍要人工核，尤其是社媒数据。本周上榜：一周 1,548 星，总星 6.9 万。

<a id="p-stitch-skills" style="scroll-margin-top:96px"></a>**[stitch-skills](https://github.com/google-labs-code/stitch-skills)**｜定位：Google Stitch 的配套技能与插件集，按 Agent Skills 开放标准写。场景：团队用 Stitch 出 UI 稿，想让 Codex、Claude Code、Cursor 接着把稿子变成代码。选型：绑定 Stitch 生态，不用 Stitch 就没有价值。本周上榜：一周 1,197 星，本期榜尾。

### 2.2 开发者工具（3 个）

三个都在做同一件事：让 agent 能看见真实运行环境。区别是一个看代码、一个看浏览器、一个操作浏览器。

<a id="p-open-code-review" style="scroll-margin-top:96px"></a>**[open-code-review](https://github.com/alibaba/open-code-review)**｜定位：阿里内部用了两年的 AI 代码评审助手，开源成 CLI，确定性流水线加 LLM 做行级评论。场景：仓库多、评审排队，想先过一遍机器再让人看。选型：适合已有 CI、能接模型网关的团队；单仓库小团队收益有限，配置成本可能超过省下的时间。本周上榜：一周 3,675 星，Go 写的 CLI，也是本榜唯一 Go 项目。

<a id="p-agent-browser" style="scroll-margin-top:96px"></a>**[agent-browser](https://github.com/vercel-labs/agent-browser)**｜定位：给 agent 用的浏览器命令行，页面读成无障碍树，元素带 `@e1` 这样的引用。场景：要让 agent 登录后台、点表单、抓数据，又不想为每个网站写 Playwright 脚本。选型：和 Playwright 脚本比，它省 token、对页面改版更耐受；代价是依赖页面的语义标注，老站点会打折。本周上榜：一周 3,429 星，Rust 单文件 CLI。

<a id="p-chrome-devtools-mcp" style="scroll-margin-top:96px"></a>**[chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp)**｜定位：把 Chrome DevTools 暴露成 MCP server 和 CLI，让 agent 自己看 console、网络请求和性能指标。场景：agent 改完页面，需要自己确认有没有报错、LCP 有没有变差。选型：和 agent-browser 互补，一个负责查问题，一个负责操作页面；同时开两个要注意权限边界。本周上榜：一周 2,948 星，总星 5.8 万。

### 2.3 其他（3 个）

三个项目互不相关，但都不属于开发者工具的日常形态：一个 office SDK、一个自托管财务、一个选股脚本。

<a id="p-univer" style="scroll-margin-top:96px"></a>**[univer](https://github.com/dream-num/univer)**｜定位：可嵌入产品的 office SDK，从表格和文档起步，带协作和文件处理。场景：你的产品要内置类 Excel 的表格编辑，不想引一个庞大客户端。选型：和 Handsontable、Luckysheet 比，它更接近「整套 office」；许可和体量要先算清楚，再决定是不是只取表格模块。本周上榜：一周 5,845 星，官方叙事已经转向 AI office。

<a id="p-sure" style="scroll-margin-top:96px"></a>**[sure](https://github.com/we-promise/sure)**｜定位：自托管的个人财务应用，是已停维护的 Maybe Finance 的社区 fork。场景：你想把账户、预算、净值放在自己服务器上，不再订阅。选型：愿意维护 Ruby 应用和数据库就选它；只想在手机上记账，用现成 App 更省事。本周上榜：一周 4,786 星，替代关停服务的需求一直真实存在。

<a id="p-daily-stock-analysis" style="scroll-margin-top:96px"></a>**[daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)**｜定位：LLM 驱动的多市场自选股分析，每天生成决策看板并推到企业微信、飞书、Telegram。场景：你有固定自选池，想每天早上收一份结构化复盘。选型：要自己配行情源和模型 key；输出是线索不是结论，别直接照着下单。本周上榜：一周 3,653 星，中文项目里少见的持续在榜。

### 2.4 安全（2 个）

一个做技能包，一个做 CLI 和 SDK。前者教 agent 怎么找漏洞，后者让 agent 在 CI 里找。

<a id="p-codex-security" style="scroll-margin-top:96px"></a>**[codex-security](https://github.com/openai/codex-security)**｜定位：OpenAI 的安全 CLI 和 TypeScript SDK，覆盖发现、验证、修复漏洞三段。场景：想在 CI 里加一道带修复建议的扫描，而不只是报一串告警。选型：和 semgrep 这类规则引擎比，它更偏「给出可执行修复」；代价是绑定 Codex 生态。本周上榜：一周 2,531 星。

<a id="p-claude-bughunter" style="scroll-margin-top:96px"></a>**[Claude-BugHunter](https://github.com/elementalsouls/Claude-BugHunter)**｜定位：面向漏洞挖掘和外部红队的 Claude 技能包，含 800 多条已披露报告的模式，按 24 类漏洞组织。场景：做渗透测试或打 CTF，想让 agent 按成熟套路走一遍。选型：当方法论和检查清单用；真实业务扫描仍要人工确认，别拿它直接打生产。本周上榜：一周 2,508 星。

### 2.5 前端与设计（1 个）

<a id="p-astryx" style="scroll-margin-top:96px"></a>**[astryx](https://github.com/meta/astryx)**｜定位：Meta 开源的设计系统，React 19 加 StyleX，组件和 token 成套给出。场景：后台或内容站需要「像大厂产品」的观感，团队又没有专门的设计资源。选型：和 shadcn/ui 比，它更像成品、开箱即用，但定制要顺着它的方式走；要长期维护自己的主题体系，选把源码交给你的方案更稳。本周上榜：一周 8,700 星，目前仍是 Beta，属于发布即被围观。

## 3. 热度结构

{{< fig "diagrams/02-rank.svg" "本周新增星排行，前三名用强调色；数字为榜单显示的一周增量。" >}}

{{< fig "diagrams/03-scatter.svg" "总 ★ 与本周 +★ 的关系：越靠右上，体量和本周热度同时越高；横轴为对数刻度。悬停任意圆点可看项目名与两个数字。" >}}

榜单上半段是两种完全不同的增长。astryx 总星 3 万、一周加 8,700，是典型的新仓库发布脉冲；modern-web-guidance-src 更极端，总星只有 8,642，一周就加了 7,015，八成星是这周来的。

下半段则是存量项目被重新发现：anthropics/skills 总星 19.7 万，一周 4,847；last30days-skill 总星 6.9 万，一周只有 1,548。这类项目上榜靠的是事件，不是新鲜度。

中间还有一条很窄的带：open-code-review 和 daily_stock_analysis 一周增量几乎相同（3,675 和 3,653），一个是企业评审流程，一个是个人选股脚本。需求的两端在榜单上贴在一起，说明本周没有单一热点。

## 4. 趋势判断

1. **技能包正在变成分发格式**：6 个项目是技能包，合计 20,100 星，占全榜 33%，覆盖 AI 编程、安全、学习三类。它们的共同点是「不是应用，是给应用的料」。
2. **大厂内部工具外溢成了固定供给**：astryx、modern-web-guidance-src、open-code-review、codex-security、stitch-skills 五个来自 Meta、Google、阿里、OpenAI，合计 23,118 星、占 38%（与上一组有重叠）。
3. **单周脉冲比持续热度更常见**：前五名里三个是新仓库或刚开源，靠一次发布拿走大部分增量；真正的老项目（anthropics/skills、last30days-skill）增长平缓，靠存量带动。

## 5. 值得这周动手试的 3 个

- **astryx**：装 npm 包、照文档搭一个页面，30 分钟能出效果。最可能卡在文档还在补，组件覆盖面不如成熟库，遇到缺口要自己写。
- **plannotator**：装 CLI、接一个模型 key，10 分钟跑通一次方案评审。卡点在模型返回格式不稳时批注锚点会错位。
- **chrome-devtools-mcp**：接到现有 agent 上，让它自己看 console 和性能。卡点在权限，agent 拿到浏览器基本等于拿到登录态，先想清楚给它开哪个 profile。

## 6. 与上周对比

| 项 | 本周 | 上周 |
|---|---:|---:|
| 上榜项目数 | 16 | — |
| 新上榜 | 16 | — |
| 周内新增 ★ 合计 | 61,366 | — |
| 最大单类 | AI 编程代理 7 个 | — |

这是本系列第一期，上周列留空；从下周开始这张表才有对照价值，连续在榜与掉榜也一并补上。

## 附：分类口径

八类按项目用途划分：AI 编程代理指服务编码流程的 agent、技能包、MCP 工具与交互协议；开发者工具指 CLI、调试、评审；前端与设计指组件库与设计系统；安全指漏洞扫描与红队技能包；其余按字面意思。一个项目只归一类，放不进任何一类才归「其他」。
