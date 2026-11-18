---
title: "GitHub 周趋势 2026-W47：Agent 技能包主导"
date: "2026-11-18T18:58:00+08:00"
summary: "本周 16 个上榜项目里 7 个是给 agent 用的技能包或插件，合计吸走 23,291 星；meta/astryx 以单周 8,700 星领跑。"
description: "口径为 GitHub Trending since=weekly，抓取于 2026-11-18 18:57（Asia/Shanghai），共 16 个项目。本期是系列首篇，分析这周 38% 的新增星数为何流向 Agent Skills，以及四家大厂把内部工具搬上开源的分发逻辑。"
tags: ["github-trending"]
slug: "github-trending-2026-w47"
---

> 口径：GitHub Trending `since=weekly`，抓取于 2026-11-18 18:57（Asia/Shanghai），共 16 个项目；星数为榜单显示值，与仓库实时值可能差几十。分类词表见文末。

这周榜单的主线是 Agent Skills 变成了一种分发格式。16 个项目里 7 个归入 AI 编程代理类，其中 5 个本身就是装进 agent 的 skill 包或插件，7 个项目合计 23,291 星，占全榜单周新增 61,366 星的 38%。

支撑这条主线的是三个不同来源：GoogleChrome 牵头的 `modern-web-guidance-src`（+7,015）、Anthropic 官方的 `anthropics/skills`（+4,847）、以及个人作者写的 `claude-trading-skills`（+2,985）和 `last30days-skill`（+1,548）。同一个格式，从浏览器厂商、模型厂商到个人开发者都在往里填内容。

第二名线索是大厂内部工具的公开化。meta 把用了八年的设计系统 Astryx 开源，单周 +8,700 星；阿里巴巴把内部跑了两年的代码评审助手放出来，总星已经 54,069；OpenAI 开了 `codex-security`。这四个项目加起来 21,921 星，占全榜 36%。

16 个项目里 TypeScript 8 个、Python 5 个，其余 Ruby、Go、Rust 各 1 个。清一色是 CLI、SDK 和前端胶水，没有一个是纯后端框架或数据库。

## 1. 榜单全貌

| # | 项目 | 类别 | 语言 | 本周 +★ | 总 ★ |
|---|------|------|------|--------:|-----:|
| 1 | [GoogleChrome/modern-web-guidance-src](https://github.com/GoogleChrome/modern-web-guidance-src) | AI 编程代理 | TypeScript | 7,015 | 8,639 |
| 2 | [backnotprop/plannotator](https://github.com/backnotprop/plannotator) | AI 编程代理 | TypeScript | 2,926 | 13,246 |
| 3 | [elementalsouls/Claude-BugHunter](https://github.com/elementalsouls/Claude-BugHunter) | 安全 | Python | 2,508 | 10,265 |
| 4 | [meta/astryx](https://github.com/meta/astryx) | 前端与设计 | TypeScript | 8,700 | 30,384 |
| 5 | [we-promise/sure](https://github.com/we-promise/sure) | 其他 | Ruby | 4,786 | 16,370 |
| 6 | [alibaba/open-code-review](https://github.com/alibaba/open-code-review) | 开发者工具 | Go | 3,675 | 54,069 |
| 7 | [google-labs-code/stitch-skills](https://github.com/google-labs-code/stitch-skills) | AI 编程代理 | TypeScript | 1,197 | 10,261 |
| 8 | [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) | AI 编程代理 | Python | 2,985 | 6,963 |
| 9 | [ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) | 开发者工具 | TypeScript | 2,948 | 58,138 |
| 10 | [anthropics/skills](https://github.com/anthropics/skills) | AI 编程代理 | Python | 4,847 | 196,970 |
| 11 | [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) | 其他 | Python | 3,653 | 73,243 |
| 12 | [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) | AI 编程代理 | Python | 1,548 | 69,532 |
| 13 | [ag-ui-protocol/ag-ui](https://github.com/ag-ui-protocol/ag-ui) | AI 编程代理 | TypeScript | 2,773 | 20,255 |
| 14 | [vercel-labs/agent-browser](https://github.com/vercel-labs/agent-browser) | 开发者工具 | Rust | 3,429 | 52,507 |
| 15 | [openai/codex-security](https://github.com/openai/codex-security) | 安全 | TypeScript | 2,531 | 14,027 |
| 16 | [dream-num/univer](https://github.com/dream-num/univer) | 其他 | TypeScript | 5,845 | 29,757 |

本周新增星数前三（astryx 8,700、modern-web-guidance 7,015、univer 5,845）和总星数前三（anthropics/skills 196,970、daily_stock_analysis 73,243、last30days-skill 69,532）不是同一批。前者是新东西起量，后者是老仓库回热，两组人没有重叠。

## 2. 分类拆解

### 2.1 AI 编程代理（7 个，23,291 星）

这一类的共同点：都在解决 agent「知道得不够具体」的问题。技能包补领域知识，插件补人机回路，协议补渲染标准。

**modern-web-guidance-src** — 把 Web 平台的最佳实践、浏览器兼容数据和现代 API 用法打包成技能，注入编码 agent 的上下文，让它别再用 jQuery 时代的写法。这周起来的直接依据是它由 Chrome、Edge、Firefox、MDN 四方共同维护，且 README 明说指南按 token 效率剪过（用 eval 砍掉模型已经会的常识），这是技能包从「堆文档」转向「做评测」的信号。技术看点在 CLI 按需返回片段而不是整包塞入。适合做前端又觉得 agent 生成的代码老气的人；不适合只想让 agent 写后端脚本的场景，装了也用不上。

**anthropics/skills** — Anthropic 官方的 Agent Skills 仓库，每个技能一个目录一个 `SKILL.md`，从文档编辑、艺术创作到 MCP 生成都有。总星 196,970 让它成了事实上的格式参考实现。这周 +4,847 更像是生态效应：上面几个第三方技能包在 README 里都拿它当标准引用。适合想学 skill 怎么写、想看官方文档处理技能怎么实现的人；不适合找现成业务方案的人，里面多数是示范。

**claude-trading-skills** — 给个人投资者的 Claude 技能包，覆盖市场复盘、技术图表、经济日历、选股器和策略开发。作者写得很克制：定位是「结构化决策流程」，不是买卖信号服务，README 明说不承诺盈利。这周 +2,985 星，说明「AI 辅助但要保留人的判断」这个姿势在投资圈有市场。适合做长期 ETF、股息股、拿波段当卫星仓的散户；不适合想找自动交易机器人的人，它连券商执行接口都没有。

**plannotator** — 本地浏览器里的评审面板，接进 Claude Code、Codex、Copilot CLI、Gemini CLI、OpenCode 等 agent，把计划、规格、HTML 产物、diff 渲染出来让人批注，再把反馈一键送回 agent。最近的更新是 Inbox，用来在多 agent 并行时汇总待决策项。这周起来的依据是它把「人等 agent」变成了「agent 等评审」，钩子和插件覆盖了七八个宿主。适合同时跑多个 agent、需要人工卡点的人；不适合单人小脚本，开个浏览器面板比直接看终端重。

**ag-ui** — 把 agent 的事件流标准化成协议，让前端不用为每个框架写一套对接。1.0 版已经冻结规范，TypeScript、Python、.NET 三个 SDK 由 JSON Schema 生成，且 0.x 与 1.0 双向兼容。README 把三件套的分工写得很清楚：MCP 给工具，A2A 给 agent 之间，AG-UI 给 agent 到人。适合做 agent 产品前端、想一次接通多个框架的人；不适合纯 CLI 场景，用不上它的事件流。

**last30days-skill** — 按 Reddit 点赞、X 喜欢、YouTube 转录、Polymarket 赔率给话题打分，再由 agent 合成一份摘要，相当于「用真人互动排序的搜索引擎」。这周 +1,548 是本周 ai-coding 类里最小的增量，但它是日榜和周榜的交集之一，说明热度还在持续。适合做选题调研、判断某个话题是不是真有人讨论；不适合需要权威信源的合规场景，它的排序依据是参与度不是可信度。

**stitch-skills** — Google Stitch 的官方技能库，遵循 Agent Skills 开放标准，Codex、Antigravity、Gemini CLI、Claude Code、Cursor 各有安装路径，OpenCode 要手动拷目录。这周 +1,197 是全榜最小增量，也是日榜交集之一。适合用 Stitch 做设计稿再交给 agent 落地的人；不适合不用 Stitch 的团队，技能都围绕 MCP server 展开。

### 2.2 开发者工具（3 个，10,052 星）

三个都是「给 agent 装上手和眼睛」的工具，差别在于装的是代码评审、浏览器还是 DevTools。

**open-code-review** — 阿里内部跑了两年的 AI 代码评审 CLI，开源后总星 54,069。架构是确定性流水线加 LLM agent 的混合：内置多语言规则集（NPE、线程安全、XSS、SQL 注入），agent 可以读完整文件、搜代码库、看同批改动，输出精确到行的评论。README 给了自建基准：50 个开源仓库、200 个真实 PR、10 种语言、1,505 条标注，对比通用 agent 时精度和 F1 更高、token 只花约九分之一，代价是召回更低（官方说这是刻意换精度）。适合团队想控制评审成本和噪音的人；不适合指望它兜住所有漏洞的人，召回是主动降的。

**agent-browser** — Vercel Labs 的 Rust CLI，把网页读成带 `@e1` 这类引用的紧凑文本，agent 一条命令一个动作，浏览器进程在命令之间保持存活。第一次命令会拉起后台 daemon，空闲一小时自动退出并保存状态。支持接已有 Chrome、切 Lightpanda 或 Obscura 引擎、驱动模拟器里的 Mobile Safari、接 Browserbase 等云浏览器。这周 +3,429，说明「agent 用浏览器」这件事正在从 MCP server 往原生 CLI 迁移。适合要稳定做网页自动化的 agent；不适合只需要抓静态 HTML 的场景，起浏览器太重。

**chrome-devtools-mcp** — Chrome 官方把 DevTools 包成 MCP server 和 CLI，让 agent 录性能 trace、看网络请求、抓控制台报错（带 source map 栈）、用 puppeteer 做自动化并自动等待动作结果。总星 58,138，是本周第三老的仓库。这周 +2,948。适合调前端性能、排查线上页面问题的 agent 工作流；不适合处理别人的浏览器数据，README 明确警告它会把浏览器里的内容暴露给客户端。

### 2.3 安全（2 个，5,039 星）

**openai/codex-security** — Codex 的安全工作流加 CLI 和 TypeScript SDK：扫仓库或 diff、验证候选发现、生成补丁、核验修复、起草 `SECURITY.md`、存威胁模型，结果能导 SARIF、JSON、CSV，也能推到 Linear、Jira、GitHub 或 Codex Security Cloud。这周 +2,531。适合已经在用 Codex 且需要把安全扫描接进 CI 的团队；不适合只想跑一次开源扫描器的人，它的价值在流程和工单闭环。

**Claude-BugHunter** — 83 个技能、15 个斜杠命令、681 条公开漏洞报告模式（其中 433 条逐条标注可审计），覆盖 24 类漏洞，外加 M365、Entra、Okta、vCenter、SSL-VPN 这些企业设施的攻击链，还带 Burp MCP 集成和授权范围内的证据卫生检查。这周 +2,508，和 codex-security 只差 23 星挤在一起。适合有授权的红队和众测；不适合没有授权的人，工具本身管不了你的合法性。

### 2.4 前端与设计（1 个，8,700 星）

**meta/astryx** — Meta 内部用了八年、覆盖 13,000+ 应用的设计系统，这周开源后拿到全榜最高的单周 8,700 星。150+ 无障碍组件、品牌主题、暗色模式、CLI 一起放出，基于 React 19 和 StyleX。但它把 StyleX 藏在内部：消费方用 `className` 覆盖，Tailwind、CSS Modules、纯 CSS 都行；主题就是一组 CSS 自定义属性覆盖，不用包一层组件。`swizzle` 可以把组件源码弹进项目自己改。适合想换设计系统又不想被样式方案绑死的中大型前端；不适合还在 React 18 的项目，peer dependency 卡在 19。

### 2.5 其他（3 个，14,284 星）

**dream-num/univer** — Office SDK，用开源核心做表格和文档，再往上接协作、导入导出和 AI SDK。这周 +5,845 排全榜第三。适合想把表格编辑能力嵌进自己产品的人；不适合只想要一个在线 Excel 替代品的人，它是 SDK 不是成品。

**we-promise/sure** — 个人理财应用，Maybe Finance 停更后的社区 fork，Docker 自托管，浏览器、macOS、移动端、API、LLM agent 都能接。这周 +4,786。适合想自己管账又不想把财务数据交给 SaaS 的人；不适合要开箱即用的普通用户，自托管要自己维护。

**daily_stock_analysis** — LLM 驱动的多市场股票分析，覆盖 A 股、港股、美股、日股、韩股、台股和 ETF，每天自动生成决策仪表盘推送到企业微信、飞书、Telegram、Discord、Slack 或邮箱，支持 GitHub Actions 零成本定时跑。这周 +3,653。适合想搭自己的每日复盘流水线的人；不适合当交易依据，它给的是报告不是建议。

## 3. 趋势判断

1. 技能包已经是分发单位，不再只是提示词合集——依据：modern-web-guidance 用 eval 剪内容控制 token，astryx 顺手发 CLI 让每个编码 agent 认识它，stitch-skills 给五六个宿主各写一套安装路径。
2. 大厂内部工具开源是本周主要供给——meta、阿里、OpenAI、Google 四家合计 21,921 星，占全榜 36%，且都强调「内部跑了 N 年」。
3. 个人理财方向在榜上占了 11,424 星（18.6%）——sure、daily_stock_analysis、claude-trading-skills 三个，横跨自托管记账、每日分析、决策流程，说明 AI 落地最先跑通的是「和自己钱有关」的场景。
4. 日榜与周榜交集只有 3 个（Claude-BugHunter、last30days-skill、stitch-skills），交集小意味着本周热度多数是周内刚起量，而不是持续爬升一周。

## 4. 值得这周动手试的 3 个

- **vercel-labs/agent-browser**：`brew install agent-browser && agent-browser install`，或者不装直接 `npx agent-browser open example.com`。跑完一条 `snapshot` 就知道 agent 眼里的网页长什么样，10 分钟内能判断要不要接进自己的流程。预期卡点：首次要下 Chrome for Testing，网络慢的话十几分钟；DOM 一变就得重新 snapshot，旧引用会失效。
- **alibaba/open-code-review**：README 的说法是配好模型端点就能跑，`ocr scan` 可以扫整个文件而不只是 diff。先在 50 个提交以内的仓库上跑一遍，半小时能看出评论精度够不够。预期卡点：模型端点配置和 token 预算，另外它的召回是主动降过的，别指望它替代人工评审。
- **GoogleChrome/modern-web-guidance-src**：按 README 的 Quickstart 跑那个交互式向导，装完重启 agent 会话，让它写一个带原生 `<dialog>` 或 View Transitions 的组件，对比不开技能时的输出。20 分钟能看到差别。预期卡点：agent 不认新技能目录是最常见的失败，装完先确认 `SKILL.md` 落在它扫的路径里。

## 5. 与上周对比

- 新上榜 16 个：本期是系列首篇，`data/` 下只有 2026-W47 一个文件，没有上周基线可比。
- 连续在榜：无法统计。所有项目的 `streak` 为 0 是因为没有历史周文件可算，不代表 16 个全是新面孔；下期起这个字段才有意义。
- 掉榜：无法统计。

## 附：分类口径

固定八类，按项目实际用途归类，不按语言或公司：AI 编程代理（agent、编码助手、技能包、MCP 工具）、模型与推理、开发者工具（CLI、构建、调试、测试、代码审查）、前端与设计、数据与基础设施、安全、学习与资源、其他。本期「其他」三项分别是个人理财应用（sure）、多市场股票分析（daily_stock_analysis）和 Office SDK（univer），前两个属于垂直应用，第三个是嵌入式文档引擎，都不落在现有七类里。
