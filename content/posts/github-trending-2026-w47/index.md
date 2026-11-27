---
title: "GitHub 周趋势 2026-W47：Agent 技能包主导"
date: "2026-11-18T19:10:05+08:00"
summary: "本周 16 个上榜项目里 6 个是给 agent 的技能包，但第一名是 Meta 的设计系统 astryx，一周 8,700 星。"
description: "2026-W47 GitHub Trending 周榜：16 个项目按八类汇总，含分类占比、新增星排行与体量热度图，每个项目给定位、适用场景和选型建议。"
tags: ["github-trending"]
slug: "github-trending-2026-w47"
---

> 口径：GitHub Trending `since=weekly`，抓取于 2026-11-18 19:12（Asia/Shanghai），共 16 个项目；星数为榜单显示值，与仓库实时值可能差几十。分类词表见文末。

<div class="gt-kpis">
  <div class="gt-kpi" data-cat="ai-coding"><div class="gt-kpi-head"><span>本周上榜席位</span><i class="gt-dot"></i></div><div class="gt-kpi-val">16 席</div><div class="gt-kpi-sub">首期基线记录 · 新上榜 16 席</div></div>
  <div class="gt-kpi" data-cat="other"><div class="gt-kpi-head"><span>周内新增 ★ 合计</span><i class="gt-dot"></i></div><div class="gt-kpi-val">+61,366</div><div class="gt-kpi-sub">GitHub Trending 周榜累计周增</div></div>
  <div class="gt-kpi" data-cat="ai-coding"><div class="gt-kpi-head"><span>头号主导赛道</span><i class="gt-dot"></i></div><div class="gt-kpi-val">AI 编程代理 · 38.0%</div><div class="gt-kpi-sub">7 个项目合计 +23,291 ★</div></div>
  <div class="gt-kpi" data-cat="frontend"><div class="gt-kpi-head"><span>单周增量榜首</span><i class="gt-dot"></i></div><div class="gt-kpi-val">astryx</div><div class="gt-kpi-sub">+8,700 ★ 领跑 · 新面孔 16 席 (100.0%)</div></div>
</div>

这周最集中的形态不是产品，是「给 agent 的料」：16 个项目里 6 个是技能包，横跨 AI 编程与安全两类，合计 20,100 星。它们大多不是让人打开用的，而是让 Claude Code、Codex 这类工具多会一件事。

但榜首不在这条线上。Meta 的 astryx 一周 8,700 星，是第二名 1,300 星之外的单点爆发，而且它面向人：一套 React 组件库。榜单同时在往两个方向走，一个是 agent 的供给侧，一个是人的工作台。

## 1. 本周全貌

{{< fig "diagrams/01-category.svg" "本周 16 个项目按类别分布；条长 = 项目数，右侧 = 该类周内新增 ★ 合计。" >}}

<div class="gt-table-wrap">
  <table class="gt-table">
    <thead>
      <tr><th>#</th><th>项目 / 语言</th><th>一句话定位</th><th>类别</th><th class="gt-num">本周 +★</th><th class="gt-num">总 ★</th></tr>
    </thead>
    <tbody>
      <tr><td><span class="gt-rk is-top" data-cat="ai-coding">1</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/GoogleChrome/modern-web-guidance-src"><span class="gt-owner">GoogleChrome/</span><strong>modern-web-guidance-src</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">TypeScript · 首次上榜</div></td><td><a class="gt-jump" href="#p-modern-web-guidance-src">Chrome 的 web 实践技能包，给 agent 用 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+7,015</span><span class="gt-gain-pct">81.2%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:81%"></span></div></td><td class="gt-num gt-total">8,642</td></tr>
      <tr><td><span class="gt-rk is-top" data-cat="ai-coding">2</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/backnotprop/plannotator"><span class="gt-owner">backnotprop/</span><strong>plannotator</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">TypeScript · 首次上榜</div></td><td><a class="gt-jump" href="#p-plannotator">agent 计划的批注台，动手前先圈问题 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+2,926</span><span class="gt-gain-pct">22.1%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:34%"></span></div></td><td class="gt-num gt-total">13,246</td></tr>
      <tr><td><span class="gt-rk is-top" data-cat="security">3</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/elementalsouls/Claude-BugHunter"><span class="gt-owner">elementalsouls/</span><strong>Claude-BugHunter</strong></a><span class="gt-chip is-new">NEW</span><span class="gt-chip is-dual">周/日双榜</span></div><div class="gt-repo-meta">Python · 首次上榜</div></td><td><a class="gt-jump" href="#p-claude-bughunter">漏洞挖掘技能包，按 24 类组织报告模式 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="security"><i class="gt-dot"></i>安全</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+2,508</span><span class="gt-gain-pct">24.4%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="security" style="width:29%"></span></div></td><td class="gt-num gt-total">10,280</td></tr>
      <tr><td><span class="gt-rk">04</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/meta/astryx"><span class="gt-owner">meta/</span><strong>astryx</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">TypeScript · 首次上榜</div></td><td><a class="gt-jump" href="#p-astryx">Meta 开源设计系统，组件与 token 成套 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="frontend"><i class="gt-dot"></i>前端与设计</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+8,700</span><span class="gt-gain-pct">28.6%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="frontend" style="width:100%"></span></div></td><td class="gt-num gt-total">30,389</td></tr>
      <tr><td><span class="gt-rk">05</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/we-promise/sure"><span class="gt-owner">we-promise/</span><strong>sure</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">Ruby · 首次上榜</div></td><td><a class="gt-jump" href="#p-sure">自托管个人财务面板，社区接盘版 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="other"><i class="gt-dot"></i>其他</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+4,786</span><span class="gt-gain-pct">29.2%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="other" style="width:55%"></span></div></td><td class="gt-num gt-total">16,371</td></tr>
      <tr><td><span class="gt-rk">06</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/alibaba/open-code-review"><span class="gt-owner">alibaba/</span><strong>open-code-review</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">Go · 首次上榜</div></td><td><a class="gt-jump" href="#p-open-code-review">阿里评审助手开源版，CI 里的 AI 评审 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="devtools"><i class="gt-dot"></i>开发者工具</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+3,675</span><span class="gt-gain-pct">6.8%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="devtools" style="width:42%"></span></div></td><td class="gt-num gt-total">54,073</td></tr>
      <tr><td><span class="gt-rk">07</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/google-labs-code/stitch-skills"><span class="gt-owner">google-labs-code/</span><strong>stitch-skills</strong></a><span class="gt-chip is-new">NEW</span><span class="gt-chip is-dual">周/日双榜</span></div><div class="gt-repo-meta">TypeScript · 首次上榜</div></td><td><a class="gt-jump" href="#p-stitch-skills">Stitch 配套技能集，设计稿接到代码 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+1,197</span><span class="gt-gain-pct">11.7%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:14%"></span></div></td><td class="gt-num gt-total">10,262</td></tr>
      <tr><td><span class="gt-rk">08</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/tradermonty/claude-trading-skills"><span class="gt-owner">tradermonty/</span><strong>claude-trading-skills</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">Python · 首次上榜</div></td><td><a class="gt-jump" href="#p-claude-trading-skills">散户交易技能包，行情到筛股一条流程 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+2,985</span><span class="gt-gain-pct">42.8%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:34%"></span></div></td><td class="gt-num gt-total">6,969</td></tr>
      <tr><td><span class="gt-rk">09</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/ChromeDevTools/chrome-devtools-mcp"><span class="gt-owner">ChromeDevTools/</span><strong>chrome-devtools-mcp</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">TypeScript · 首次上榜</div></td><td><a class="gt-jump" href="#p-chrome-devtools-mcp">把 DevTools 交给 agent 自查页面 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="devtools"><i class="gt-dot"></i>开发者工具</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+2,948</span><span class="gt-gain-pct">5.1%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="devtools" style="width:34%"></span></div></td><td class="gt-num gt-total">58,139</td></tr>
      <tr><td><span class="gt-rk">10</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/anthropics/skills"><span class="gt-owner">anthropics/</span><strong>skills</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">Python · 首次上榜</div></td><td><a class="gt-jump" href="#p-skills">Claude 技能包参考实现，写 skill 照抄 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+4,847</span><span class="gt-gain-pct">2.5%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:56%"></span></div></td><td class="gt-num gt-total">196,973</td></tr>
      <tr><td><span class="gt-rk">11</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/ZhuLinsen/daily_stock_analysis"><span class="gt-owner">ZhuLinsen/</span><strong>daily_stock_analysis</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">Python · 首次上榜</div></td><td><a class="gt-jump" href="#p-daily-stock-analysis">多市场选股分析，每天推看板到 IM <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="other"><i class="gt-dot"></i>其他</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+3,653</span><span class="gt-gain-pct">5.0%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="other" style="width:42%"></span></div></td><td class="gt-num gt-total">73,243</td></tr>
      <tr><td><span class="gt-rk">12</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/mvanhorn/last30days-skill"><span class="gt-owner">mvanhorn/</span><strong>last30days-skill</strong></a><span class="gt-chip is-new">NEW</span><span class="gt-chip is-dual">周/日双榜</span></div><div class="gt-repo-meta">Python · 首次上榜</div></td><td><a class="gt-jump" href="#p-last30days-skill">跨社媒研究技能，按互动量排序 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+1,548</span><span class="gt-gain-pct">2.2%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:18%"></span></div></td><td class="gt-num gt-total">69,540</td></tr>
      <tr><td><span class="gt-rk">13</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/ag-ui-protocol/ag-ui"><span class="gt-owner">ag-ui-protocol/</span><strong>ag-ui</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">TypeScript · 首次上榜</div></td><td><a class="gt-jump" href="#p-ag-ui">agent 与前端的事件协议，接流式输出 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+2,773</span><span class="gt-gain-pct">13.7%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:32%"></span></div></td><td class="gt-num gt-total">20,259</td></tr>
      <tr><td><span class="gt-rk">14</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/vercel-labs/agent-browser"><span class="gt-owner">vercel-labs/</span><strong>agent-browser</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">Rust · 首次上榜</div></td><td><a class="gt-jump" href="#p-agent-browser">给 agent 的浏览器 CLI，读无障碍树 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="devtools"><i class="gt-dot"></i>开发者工具</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+3,429</span><span class="gt-gain-pct">6.5%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="devtools" style="width:39%"></span></div></td><td class="gt-num gt-total">52,510</td></tr>
      <tr><td><span class="gt-rk">15</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/openai/codex-security"><span class="gt-owner">openai/</span><strong>codex-security</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">TypeScript · 首次上榜</div></td><td><a class="gt-jump" href="#p-codex-security">OpenAI 安全 CLI，找漏洞并给修复 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="security"><i class="gt-dot"></i>安全</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+2,531</span><span class="gt-gain-pct">18.0%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="security" style="width:29%"></span></div></td><td class="gt-num gt-total">14,029</td></tr>
      <tr><td><span class="gt-rk">16</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/dream-num/univer"><span class="gt-owner">dream-num/</span><strong>univer</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">TypeScript · 首次上榜</div></td><td><a class="gt-jump" href="#p-univer">可嵌入产品的 office SDK，表格起步 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="other"><i class="gt-dot"></i>其他</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+5,845</span><span class="gt-gain-pct">19.6%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="other" style="width:67%"></span></div></td><td class="gt-num gt-total">29,758</td></tr>
    </tbody>
  </table>
</div>


表里最该注意的是：AI 编程代理这一类 7 个项目就吃掉 23,291 星，占全榜 38%，但其中没有一个在做「更好的模型」或「更快的推理」，全在做 agent 周边。

## 2. 分类拆解

### 2.1 AI 编程代理（7 个）

这一类的共同取向是「把知识打包成可加载的模块」。7 个项目里 5 个是技能包，1 个是前端协议，只有 1 个是给人用的评审工作台。最值得先看的是 modern-web-guidance-src，因为它解决的是每个前端团队都会碰到的问题。

<div id="p-modern-web-guidance-src" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/GoogleChrome/modern-web-guidance-src">GoogleChrome/modern-web-guidance-src ↗</a><span class="gt-lang">TypeScript</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+7,015 ★</span><span class="gt-card-total">总 8,642 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>Google Chrome 团队把 web 平台最佳实践整理成 agent 技能，让编码 agent 少写过期 API 和兼容性坑。</div>
    <div><strong>场景：</strong>agent 生成前端代码，你要它别再写 <code>document.write</code> 那类老写法，也不想每次手动贴 MDN 链接。</div>
    <div><strong>选型建议：</strong>和「在 prompt 里塞一堆规范文档」比，它体积小、可加载、随 Chrome 更新；代价是视角只有 Chrome，Safari 和 Firefox 的边角差异仍要人看。</div>
    <div><strong>本周上榜：</strong>一周 7,015 星，主仓发布后配套源仓库被一起带起来。</div>
  </div>
</div>

<div id="p-skills" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/anthropics/skills">anthropics/skills ↗</a><span class="gt-lang">Python</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+4,847 ★</span><span class="gt-card-total">总 196,973 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>Claude 官方放出的技能包参考实现，回答「一个 skill 该长什么样」。</div>
    <div><strong>场景：</strong>你准备给 agent 加一套领域能力，先找一个结构抄。</div>
    <div><strong>选型建议：</strong>当模板和范式看，别当框架依赖；真正的业务逻辑还是自己写。</div>
    <div><strong>本周上榜：</strong>一周 4,847 星，总星 19.7 万，属于存量项目被新一波 skill 热重新带起。</div>
  </div>
</div>

<div id="p-claude-trading-skills" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/tradermonty/claude-trading-skills">tradermonty/claude-trading-skills ↗</a><span class="gt-lang">Python</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+2,985 ★</span><span class="gt-card-total">总 6,969 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>面向个人投资者的 Claude 技能包，覆盖行情、技术图、经济日历、筛股和策略复盘。</div>
    <div><strong>场景：</strong>你有自选股清单，想让 agent 每天按固定流程过一遍。</div>
    <div><strong>选型建议：</strong>当流程模板用，数据源要自己接；把它当投资建议会出事。</div>
    <div><strong>本周上榜：</strong>一周 2,985 星，README 里明说作者是拿自己的交易流程做的。</div>
  </div>
</div>

<div id="p-plannotator" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/backnotprop/plannotator">backnotprop/plannotator ↗</a><span class="gt-lang">TypeScript</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+2,926 ★</span><span class="gt-card-total">总 13,246 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>把 agent 的计划、规格、diff 变成可批注的界面，反馈再一次回给 agent。</div>
    <div><strong>场景：</strong>让 agent 动手改代码之前，你想先看它打算怎么做，并且能圈出第三段。</div>
    <div><strong>选型建议：</strong>和「直接让 agent 干活再回滚」比，它把评审提前，适合改动面大的任务；一两行的小修会显得多余。</div>
    <div><strong>本周上榜：</strong>一周 2,926 星，纳入 inbox、artifact、代码评审三块工作台。</div>
  </div>
</div>

<div id="p-ag-ui" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/ag-ui-protocol/ag-ui">ag-ui-protocol/ag-ui ↗</a><span class="gt-lang">TypeScript</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+2,773 ★</span><span class="gt-card-total">总 20,259 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>agent 和前端之间的交互协议，把流式输出、工具调用、状态同步定成事件格式。</div>
    <div><strong>场景：</strong>你要把 agent 嵌进自己的 web 应用，不想每次自研一套 SSE 协议。</div>
    <div><strong>选型建议：</strong>和自研比，规范统一、生态在起；早期项目接入要接受规范还在动。</div>
    <div><strong>本周上榜：</strong>一周 2,773 星，属于协议类项目里少见的持续在榜。</div>
  </div>
</div>

<div id="p-last30days-skill" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/mvanhorn/last30days-skill">mvanhorn/last30days-skill ↗</a><span class="gt-lang">Python</span><span class="gt-chip is-new">本周新上榜</span><span class="gt-chip is-dual">周/日双榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+1,548 ★</span><span class="gt-card-total">总 69,540 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>跨 Reddit、X、YouTube、HN、Polymarket 的研究技能，按互动量和真金白银的赔率排序，再合成一份摘要。</div>
    <div><strong>场景：</strong>做市场调研或热点复盘，需要「最近 30 天大家真正在讨论什么」。</div>
    <div><strong>选型建议：</strong>比通用搜索多了排序依据；结果仍要人工核，尤其是社媒数据。</div>
    <div><strong>本周上榜：</strong>一周 1,548 星，总星 6.9 万。</div>
  </div>
</div>

<div id="p-stitch-skills" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/google-labs-code/stitch-skills">google-labs-code/stitch-skills ↗</a><span class="gt-lang">TypeScript</span><span class="gt-chip is-new">本周新上榜</span><span class="gt-chip is-dual">周/日双榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+1,197 ★</span><span class="gt-card-total">总 10,262 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>Google Stitch 的配套技能与插件集，按 Agent Skills 开放标准写。</div>
    <div><strong>场景：</strong>团队用 Stitch 出 UI 稿，想让 Codex、Claude Code、Cursor 接着把稿子变成代码。</div>
    <div><strong>选型建议：</strong>绑定 Stitch 生态，不用 Stitch 就没有价值。</div>
    <div><strong>本周上榜：</strong>一周 1,197 星，本期榜尾。</div>
  </div>
</div>

### 2.2 开发者工具（3 个）

三个都在做同一件事：让 agent 能看见真实运行环境。区别是一个看代码、一个看浏览器、一个操作浏览器。

<div id="p-open-code-review" class="gt-card" data-cat="devtools">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/alibaba/open-code-review">alibaba/open-code-review ↗</a><span class="gt-lang">Go</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="devtools">+3,675 ★</span><span class="gt-card-total">总 54,073 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>阿里内部用了两年的 AI 代码评审助手，开源成 CLI，确定性流水线加 LLM 做行级评论。</div>
    <div><strong>场景：</strong>仓库多、评审排队，想先过一遍机器再让人看。</div>
    <div><strong>选型建议：</strong>适合已有 CI、能接模型网关的团队；单仓库小团队收益有限，配置成本可能超过省下的时间。</div>
    <div><strong>本周上榜：</strong>一周 3,675 星，Go 写的 CLI，也是本榜唯一 Go 项目。</div>
  </div>
</div>

<div id="p-agent-browser" class="gt-card" data-cat="devtools">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/vercel-labs/agent-browser">vercel-labs/agent-browser ↗</a><span class="gt-lang">Rust</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="devtools">+3,429 ★</span><span class="gt-card-total">总 52,510 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>给 agent 用的浏览器命令行，页面读成无障碍树，元素带 <code>@e1</code> 这样的引用。</div>
    <div><strong>场景：</strong>要让 agent 登录后台、点表单、抓数据，又不想为每个网站写 Playwright 脚本。</div>
    <div><strong>选型建议：</strong>和 Playwright 脚本比，它省 token、对页面改版更耐受；代价是依赖页面的语义标注，老站点会打折。</div>
    <div><strong>本周上榜：</strong>一周 3,429 星，Rust 单文件 CLI。</div>
  </div>
</div>

<div id="p-chrome-devtools-mcp" class="gt-card" data-cat="devtools">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/ChromeDevTools/chrome-devtools-mcp">ChromeDevTools/chrome-devtools-mcp ↗</a><span class="gt-lang">TypeScript</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="devtools">+2,948 ★</span><span class="gt-card-total">总 58,139 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>把 Chrome DevTools 暴露成 MCP server 和 CLI，让 agent 自己看 console、网络请求和性能指标。</div>
    <div><strong>场景：</strong>agent 改完页面，需要自己确认有没有报错、LCP 有没有变差。</div>
    <div><strong>选型建议：</strong>和 agent-browser 互补，一个负责查问题，一个负责操作页面；同时开两个要注意权限边界。</div>
    <div><strong>本周上榜：</strong>一周 2,948 星，总星 5.8 万。</div>
  </div>
</div>

### 2.3 其他（3 个）

三个项目互不相关，但都不属于开发者工具的日常形态：一个 office SDK、一个自托管财务、一个选股脚本。

<div id="p-univer" class="gt-card" data-cat="other">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/dream-num/univer">dream-num/univer ↗</a><span class="gt-lang">TypeScript</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="other">+5,845 ★</span><span class="gt-card-total">总 29,758 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>可嵌入产品的 office SDK，从表格和文档起步，带协作和文件处理。</div>
    <div><strong>场景：</strong>你的产品要内置类 Excel 的表格编辑，不想引一个庞大客户端。</div>
    <div><strong>选型建议：</strong>和 Handsontable、Luckysheet 比，它更接近「整套 office」；许可和体量要先算清楚，再决定是不是只取表格模块。</div>
    <div><strong>本周上榜：</strong>一周 5,845 星，官方叙事已经转向 AI office。</div>
  </div>
</div>

<div id="p-sure" class="gt-card" data-cat="other">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/we-promise/sure">we-promise/sure ↗</a><span class="gt-lang">Ruby</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="other">+4,786 ★</span><span class="gt-card-total">总 16,371 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>自托管的个人财务应用，是已停维护的 Maybe Finance 的社区 fork。</div>
    <div><strong>场景：</strong>你想把账户、预算、净值放在自己服务器上，不再订阅。</div>
    <div><strong>选型建议：</strong>愿意维护 Ruby 应用和数据库就选它；只想在手机上记账，用现成 App 更省事。</div>
    <div><strong>本周上榜：</strong>一周 4,786 星，替代关停服务的需求一直真实存在。</div>
  </div>
</div>

<div id="p-daily-stock-analysis" class="gt-card" data-cat="other">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/ZhuLinsen/daily_stock_analysis">ZhuLinsen/daily_stock_analysis ↗</a><span class="gt-lang">Python</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="other">+3,653 ★</span><span class="gt-card-total">总 73,243 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>LLM 驱动的多市场自选股分析，每天生成决策看板并推到企业微信、飞书、Telegram。</div>
    <div><strong>场景：</strong>你有固定自选池，想每天早上收一份结构化复盘。</div>
    <div><strong>选型建议：</strong>要自己配行情源和模型 key；输出是线索不是结论，别直接照着下单。</div>
    <div><strong>本周上榜：</strong>一周 3,653 星，中文项目里少见的持续在榜。</div>
  </div>
</div>

### 2.4 安全（2 个）

一个做技能包，一个做 CLI 和 SDK。前者教 agent 怎么找漏洞，后者让 agent 在 CI 里找。

<div id="p-codex-security" class="gt-card" data-cat="security">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/openai/codex-security">openai/codex-security ↗</a><span class="gt-lang">TypeScript</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="security">+2,531 ★</span><span class="gt-card-total">总 14,029 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>OpenAI 的安全 CLI 和 TypeScript SDK，覆盖发现、验证、修复漏洞三段。</div>
    <div><strong>场景：</strong>想在 CI 里加一道带修复建议的扫描，而不只是报一串告警。</div>
    <div><strong>选型建议：</strong>和 semgrep 这类规则引擎比，它更偏「给出可执行修复」；代价是绑定 Codex 生态。</div>
    <div><strong>本周上榜：</strong>一周 2,531 星。</div>
  </div>
</div>

<div id="p-claude-bughunter" class="gt-card" data-cat="security">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/elementalsouls/Claude-BugHunter">elementalsouls/Claude-BugHunter ↗</a><span class="gt-lang">Python</span><span class="gt-chip is-new">本周新上榜</span><span class="gt-chip is-dual">周/日双榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="security">+2,508 ★</span><span class="gt-card-total">总 10,280 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>面向漏洞挖掘和外部红队的 Claude 技能包，含 800 多条已披露报告的模式，按 24 类漏洞组织。</div>
    <div><strong>场景：</strong>做渗透测试或打 CTF，想让 agent 按成熟套路走一遍。</div>
    <div><strong>选型建议：</strong>当方法论和检查清单用；真实业务扫描仍要人工确认，别拿它直接打生产。</div>
    <div><strong>本周上榜：</strong>一周 2,508 星。</div>
  </div>
</div>

### 2.5 前端与设计（1 个）

<div id="p-astryx" class="gt-card" data-cat="frontend">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/meta/astryx">meta/astryx ↗</a><span class="gt-lang">TypeScript</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="frontend">+8,700 ★</span><span class="gt-card-total">总 30,389 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>Meta 开源的设计系统，React 19 加 StyleX，组件和 token 成套给出。</div>
    <div><strong>场景：</strong>后台或内容站需要「像大厂产品」的观感，团队又没有专门的设计资源。</div>
    <div><strong>选型建议：</strong>和 shadcn/ui 比，它更像成品、开箱即用，但定制要顺着它的方式走；要长期维护自己的主题体系，选把源码交给你的方案更稳。</div>
    <div><strong>本周上榜：</strong>一周 8,700 星，目前仍是 Beta，属于发布即被围观。</div>
  </div>
</div>

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

<div class="gt-table-wrap">
  <table class="gt-table gt-table-cmp">
    <thead>
      <tr><th>对比项</th><th class="gt-num">本周（W47）</th><th class="gt-num">上周</th><th class="gt-num">环比变化</th></tr>
    </thead>
    <tbody>
      <tr><td>上榜项目数</td><td class="gt-num"><strong>16</strong></td><td class="gt-num">—</td><td class="gt-num gt-delta">首期基线</td></tr>
      <tr><td>新上榜席位</td><td class="gt-num"><strong>16（100%）</strong></td><td class="gt-num">—</td><td class="gt-num gt-delta">首期基线</td></tr>
      <tr><td>周内新增 ★ 合计</td><td class="gt-num"><strong>+61,366</strong></td><td class="gt-num">—</td><td class="gt-num gt-delta">首期基线</td></tr>
      <tr><td>最大单类赛道</td><td class="gt-num"><strong>AI 编程代理（7 个 / 38.0%）</strong></td><td class="gt-num">—</td><td class="gt-num gt-delta">首期基线</td></tr>
    </tbody>
  </table>
</div>


这是本系列第一期，上周列留空；从下周开始这张表才有对照价值，连续在榜与掉榜也一并补上。

## 附：分类口径

八类按项目用途划分：AI 编程代理指服务编码流程的 agent、技能包、MCP 工具与交互协议；开发者工具指 CLI、调试、评审；前端与设计指组件库与设计系统；安全指漏洞扫描与红队技能包；其余按字面意思。一个项目只归一类，放不进任何一类才归「其他」。
