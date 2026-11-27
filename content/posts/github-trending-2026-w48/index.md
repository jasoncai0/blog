---
title: "GitHub 周趋势 2026-W48：AI 代理占榜，增量腰斩"
date: "2026-11-26T20:21:00+08:00"
summary: "14 个项目里 8 个归入 AI 编程代理，周增总量降 52.5%；last30days 以 +5,302 领跑，n8n-io/nod 新上榜。"
description: "GitHub Trending 周榜 2026-W48 复盘：本周返回 14 个项目，周内新增星合计 29,133，比上周少 52.5%；AI 编程代理增至 8 个并占去 64.8% 新增星，但主要由其他类别退潮所致。"
tags: ["github-trending"]
slug: "github-trending-2026-w48"
---

> 口径：GitHub Trending `since=weekly`，抓取于 2026-11-26 20:20（Asia/Shanghai），共 14 个项目；星数为榜单显示值，与仓库实时值可能差几十。分类词表见文末。

<div class="gt-kpis">
  <div class="gt-kpi" data-cat="ai-coding"><div class="gt-kpi-head"><span>本周上榜席位</span><i class="gt-dot"></i></div><div class="gt-kpi-val">14 席</div><div class="gt-kpi-sub">较上周 16 席变化 -2 席 · 11 席连榜</div></div>
  <div class="gt-kpi" data-cat="other"><div class="gt-kpi-head"><span>周内新增 ★ 合计</span><i class="gt-dot"></i></div><div class="gt-kpi-val">+29,133</div><div class="gt-kpi-sub">较上周 61,366 环比 -52.5%</div></div>
  <div class="gt-kpi" data-cat="ai-coding"><div class="gt-kpi-head"><span>头号主导赛道</span><i class="gt-dot"></i></div><div class="gt-kpi-val">AI 编程代理 · 64.8%</div><div class="gt-kpi-sub">8 个项目合计 +18,890 ★</div></div>
  <div class="gt-kpi" data-cat="frontend"><div class="gt-kpi-head"><span>单周增量榜首</span><i class="gt-dot"></i></div><div class="gt-kpi-val">last30days-skill</div><div class="gt-kpi-sub">+5,302 ★ 领跑 · 新面孔 3 席 (21.4%)</div></div>
</div>

本周榜单只有 14 个项目，上周是 16 个。周内新增星合计 29,133，比上周的 61,366 少 32,233（降幅 52.5%）。留榜率仍然很高：14 个里 11 个上周也在，只有 `ffmpeg-skill`、`n8n-io/nod` 和 `HermesEnabler` 是新上榜。

支撑盘面的还是 AI 编程代理。8 个项目合计周增 18,890，占全榜增量的 64.8%，比上周的 37.9% 高出一截。但它的绝对增量其实从上周的 23,291 降到了 18,890（-18.9%），真正塌方的是其他类别，合计从 38,075 跌到 10,243（-73.1%）。

单点上看，`last30days-skill` 以 +5,302 排本周新增第一，同时以 75,450 排总星第一；排在第二增量的是新上榜的 `ffmpeg-skill`（+3,048），第三是 `astryx`（+2,635）。大存量老项目里，`chrome-devtools-mcp`（总星 59,437）本周只加 1,203（2.0%），`open-code-review`（总星 56,507）只加 2,259（4.0%），基本靠存量留在榜上。

## 1. 本周全貌

{{< fig "diagrams/01-category.svg" "本周 14 个项目按类别分布；条长 = 项目数，右侧 = 该类周内新增 ★ 合计。AI 编程代理占 8 个、+18,890，其余三类共 6 个、+10,243。" >}}

<div class="gt-table-wrap">
  <table class="gt-table">
    <thead>
      <tr><th>#</th><th>项目 / 语言</th><th>一句话定位</th><th>类别</th><th class="gt-num">本周 +★</th><th class="gt-num">总 ★</th></tr>
    </thead>
    <tbody>
      <tr><td><span class="gt-rk is-top" data-cat="ai-coding">1</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/backnotprop/plannotator"><span class="gt-owner">backnotprop/</span><strong>plannotator</strong></a></div><div class="gt-repo-meta">TypeScript · 连榜 2 周</div></td><td><a class="gt-jump" href="#p-plannotator">给编码代理的计划批注与评审工作台 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+1,764</span><span class="gt-gain-pct">11.6%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:33%"></span></div></td><td class="gt-num gt-total">15,174</td></tr>
      <tr><td><span class="gt-rk is-top" data-cat="ai-coding">2</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/google-labs-code/stitch-skills"><span class="gt-owner">google-labs-code/</span><strong>stitch-skills</strong></a><span class="gt-chip is-dual">周/日双榜</span></div><div class="gt-repo-meta">TypeScript · 连榜 2 周</div></td><td><a class="gt-jump" href="#p-stitch-skills">让编码代理调用 Stitch 做 UI 的技能包 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+2,616</span><span class="gt-gain-pct">19.9%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:49%"></span></div></td><td class="gt-num gt-total">13,143</td></tr>
      <tr><td><span class="gt-rk is-top" data-cat="ai-coding">3</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/GoogleChrome/modern-web-guidance-src"><span class="gt-owner">GoogleChrome/</span><strong>modern-web-guidance-src</strong></a></div><div class="gt-repo-meta">TypeScript · 连榜 2 周</div></td><td><a class="gt-jump" href="#p-modern-web-guidance-src">把现代 Web 最佳实践喂给编码代理 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+2,309</span><span class="gt-gain-pct">20.7%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:44%"></span></div></td><td class="gt-num gt-total">11,166</td></tr>
      <tr><td><span class="gt-rk">04</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/n8n-io/nod"><span class="gt-owner">n8n-io/</span><strong>nod</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">TypeScript · 首次上榜</div></td><td><a class="gt-jump" href="#p-nod">自带电脑与浏览器的本地个人代理 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+1,008</span><span class="gt-gain-pct">26.9%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:19%"></span></div></td><td class="gt-num gt-total">3,751</td></tr>
      <tr><td><span class="gt-rk">05</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/alibaba/open-code-review"><span class="gt-owner">alibaba/</span><strong>open-code-review</strong></a></div><div class="gt-repo-meta">Go · 连榜 2 周</div></td><td><a class="gt-jump" href="#p-open-code-review">阿里开源的行级 AI 代码审查 CLI <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="devtools"><i class="gt-dot"></i>开发者工具</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+2,259</span><span class="gt-gain-pct">4.0%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="devtools" style="width:43%"></span></div></td><td class="gt-num gt-total">56,507</td></tr>
      <tr><td><span class="gt-rk">06</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/mvanhorn/last30days-skill"><span class="gt-owner">mvanhorn/</span><strong>last30days-skill</strong></a><span class="gt-chip is-dual">周/日双榜</span></div><div class="gt-repo-meta">Python · 连榜 2 周</div></td><td><a class="gt-jump" href="#p-last30days-skill">跨 Reddit/X/HN 调研近期话题的技能 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+5,302</span><span class="gt-gain-pct">7.0%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:100%"></span></div></td><td class="gt-num gt-total">75,450</td></tr>
      <tr><td><span class="gt-rk">07</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/ChromeDevTools/chrome-devtools-mcp"><span class="gt-owner">ChromeDevTools/</span><strong>chrome-devtools-mcp</strong></a></div><div class="gt-repo-meta">TypeScript · 连榜 2 周</div></td><td><a class="gt-jump" href="#p-chrome-devtools-mcp">让编码代理操控 Chrome 调试与测速 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="devtools"><i class="gt-dot"></i>开发者工具</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+1,203</span><span class="gt-gain-pct">2.0%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="devtools" style="width:23%"></span></div></td><td class="gt-num gt-total">59,437</td></tr>
      <tr><td><span class="gt-rk">08</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/tradermonty/claude-trading-skills"><span class="gt-owner">tradermonty/</span><strong>claude-trading-skills</strong></a></div><div class="gt-repo-meta">Python · 连榜 2 周</div></td><td><a class="gt-jump" href="#p-claude-trading-skills">给个人投资者的复盘选股交易技能集 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+1,422</span><span class="gt-gain-pct">16.7%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:27%"></span></div></td><td class="gt-num gt-total">8,524</td></tr>
      <tr><td><span class="gt-rk">09</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/ag-ui-protocol/ag-ui"><span class="gt-owner">ag-ui-protocol/</span><strong>ag-ui</strong></a></div><div class="gt-repo-meta">TypeScript · 连榜 2 周</div></td><td><a class="gt-jump" href="#p-ag-ui">把任意 AI 代理接进前端应用的事件协议 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+1,421</span><span class="gt-gain-pct">6.5%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:27%"></span></div></td><td class="gt-num gt-total">21,796</td></tr>
      <tr><td><span class="gt-rk">10</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/meta/astryx"><span class="gt-owner">meta/</span><strong>astryx</strong></a></div><div class="gt-repo-meta">TypeScript · 连榜 2 周</div></td><td><a class="gt-jump" href="#p-astryx">Meta 开源的 React 设计系统，150+ 组件 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="frontend"><i class="gt-dot"></i>前端与设计</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+2,635</span><span class="gt-gain-pct">7.9%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="frontend" style="width:50%"></span></div></td><td class="gt-num gt-total">33,252</td></tr>
      <tr><td><span class="gt-rk">11</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/pookjw/HermesEnabler"><span class="gt-owner">pookjw/</span><strong>HermesEnabler</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">Objective-C · 首次上榜</div></td><td><a class="gt-jump" href="#p-hermesenabler">在任意 Apple Watch 上启用爱马仕表盘 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="other"><i class="gt-dot"></i>其他</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+318</span><span class="gt-gain-pct">12.6%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="other" style="width:6%"></span></div></td><td class="gt-num gt-total">2,533</td></tr>
      <tr><td><span class="gt-rk">12</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/we-promise/sure"><span class="gt-owner">we-promise/</span><strong>sure</strong></a></div><div class="gt-repo-meta">Ruby · 连榜 2 周</div></td><td><a class="gt-jump" href="#p-sure">可自托管的个人记账与财富管理应用 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="other"><i class="gt-dot"></i>其他</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+1,509</span><span class="gt-gain-pct">8.4%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="other" style="width:28%"></span></div></td><td class="gt-num gt-total">18,012</td></tr>
      <tr><td><span class="gt-rk">13</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/dream-num/univer"><span class="gt-owner">dream-num/</span><strong>univer</strong></a></div><div class="gt-repo-meta">TypeScript · 连榜 2 周</div></td><td><a class="gt-jump" href="#p-univer">可嵌入产品的表格文档协作 SDK <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="other"><i class="gt-dot"></i>其他</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+2,319</span><span class="gt-gain-pct">7.3%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="other" style="width:44%"></span></div></td><td class="gt-num gt-total">31,920</td></tr>
      <tr><td><span class="gt-rk">14</span></td><td><div class="gt-repo-line"><a class="gt-repo-link" href="https://github.com/kajisho5/ffmpeg-skill"><span class="gt-owner">kajisho5/</span><strong>ffmpeg-skill</strong></a><span class="gt-chip is-new">NEW</span></div><div class="gt-repo-meta">Python · 首次上榜</div></td><td><a class="gt-jump" href="#p-ffmpeg-skill">让编码代理本地剪视频的 FFmpeg 技能 <span class="gt-arr">↓</span></a></td><td><span class="gt-cat" data-cat="ai-coding"><i class="gt-dot"></i>AI 编程代理</span></td><td class="gt-num"><div class="gt-gain-row"><span class="gt-gain-val">+3,048</span><span class="gt-gain-pct">26.9%</span></div><div class="gt-bar-track"><span class="gt-bar-fill" data-cat="ai-coding" style="width:57%"></span></div></td><td class="gt-num gt-total">11,336</td></tr>
    </tbody>
  </table>
</div>


这张表按 GitHub 周榜原始顺序排列，与新增星排序差异明显。按本周 +★ 重排，前五是 `last30days-skill`（+5,302）、`ffmpeg-skill`（+3,048）、`astryx`（+2,635）、`stitch-skills`（+2,616）和 `univer`（+2,319）；原始榜第 1 的 `plannotator` 仅排在增量第 8（+1,764），原始榜第 14 的 `ffmpeg-skill` 反而是增量第 2。

## 2. 分类拆解

### 2.1 AI 编程代理（8 个）

这 8 个项目围绕两条主线展开：给现有编码代理外挂垂直技能（调研、剪视频、UI 生成、现代 Web 规范、交易复盘），以及补齐人与代理协作的交互和桌面执行层（批注工作台、`ag-ui` 协议、本地个人代理 `nod`）。最值得先看的是 `last30days-skill` 和 `stitch-skills`，它们是本周唯二同时出现在周榜与日榜上的项目。

<div id="p-last30days-skill" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/mvanhorn/last30days-skill">mvanhorn/last30days-skill ↗</a><span class="gt-lang">Python</span><span class="gt-card-streak">连榜 2 周</span><span class="gt-chip is-dual">周/日双榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+5,302 ★</span><span class="gt-card-total">总 75,450 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>给代理用的跨平台话题调研技能，抓取 Reddit、X、YouTube、HN、Polymarket 近 30 天的真实讨论并按互动量排序汇总。</div>
    <div><strong>场景：</strong>写选题、见客户前做背景功课、跟踪某个产品或技术近一个月的真实口碑，需要近期真实反馈而不是通用百科。</div>
    <div><strong>选型建议：</strong>Reddit、HN、Polymarket 和 GitHub 零配置可跑，X 和 YouTube 等需通过向导接入凭据或浏览器会话；和通用网页搜索比，它赢在跨社区按点赞与真金白银排序并附来源，输在受限于各平台接口与 30 天时间窗口。</div>
    <div><strong>本周上榜：</strong>连续第 2 周在榜，周增从上周 1,548 暴涨到 5,302 拿下本周第一，总星 75,450 全场最高，且仍在日榜第 10。</div>
  </div>
</div>

<div id="p-ffmpeg-skill" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/kajisho5/ffmpeg-skill">kajisho5/ffmpeg-skill ↗</a><span class="gt-lang">Python</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+3,048 ★</span><span class="gt-card-total">总 11,336 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>让编码代理调用本地 FFmpeg 剪视频的技能包与 MCP 服务，内置 42 个媒体工具并附带本地审片页面。</div>
    <div><strong>场景：</strong>手里有录屏、播客或拍摄素材，需要切片段、去静音、加卡拉 OK 字幕、转 9:16 竖屏和做响度标准化，但不想打开剪辑软件。</div>
    <div><strong>选型建议：</strong>依赖本机 FFmpeg 和 Python 标准库，全程本地跑、不接云、不要 API key；和云端视频剪辑 API 比，它赢在素材不出机器且零调用费，输在没有可视化时间线，复杂调色与转场仍需专业非编软件。</div>
    <div><strong>本周上榜：</strong>新上榜，周增 +3,048 排全场第二，总星 11,336 里有 26.9% 来自这一周。</div>
  </div>
</div>

<div id="p-stitch-skills" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/google-labs-code/stitch-skills">google-labs-code/stitch-skills ↗</a><span class="gt-lang">TypeScript</span><span class="gt-card-streak">连榜 2 周</span><span class="gt-chip is-dual">周/日双榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+2,616 ★</span><span class="gt-card-total">总 13,143 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>面向 Google Stitch 的设计技能包，把 Stitch 设计稿转成 React 或 React Native 组件，也能把前端代码反向同步成设计稿。</div>
    <div><strong>场景：</strong>团队用 Google Stitch 出页面原型，希望编码代理按设计系统规范直接还原组件，减少手工抄样式。</div>
    <div><strong>选型建议：</strong>需搭配 Stitch 账号和 Stitch MCP 服务，官方支持 Codex、Claude Code、Cursor 和 OpenCode 手动配置；如果设计源头在 Figma 而非 Stitch，直接用 Figma MCP，别绕道这套技能。</div>
    <div><strong>本周上榜：</strong>连续第 2 周在榜，周增 +2,616 比上周 1,197 翻倍有余，是本周仅有的两个周日双榜项目之一。</div>
  </div>
</div>

<div id="p-modern-web-guidance-src" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/GoogleChrome/modern-web-guidance-src">GoogleChrome/modern-web-guidance-src ↗</a><span class="gt-lang">TypeScript</span><span class="gt-card-streak">连榜 2 周</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+2,309 ★</span><span class="gt-card-total">总 11,166 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>由 Chrome、Edge、Firefox 和 MDN 团队联合维护的 Web 平台技能，按任务把现代浏览器 API 与兼容性指南塞进代理上下文。</div>
    <div><strong>场景：</strong>编码代理写前端总喜欢引入笨重的兼容库或过时写法，或者团队想强制新代码优先用原生现代 Web 能力。</div>
    <div><strong>选型建议：</strong>要求 Node.js 22 以上环境，通过 CLI 按需输出精简规则；和直接让代理查 MDN 比，它赢在裁掉了模型已知的废话、省 token 且带浏览器兼容性判断，输在覆盖范围偏现代特性，冷门老 API 仍需自行查文档。</div>
    <div><strong>本周上榜：</strong>连续第 2 周在榜，周增 +2,309（占总星 11,166 的 20.7%），虽然较上周 7,015 回落，仍稳居原榜第 3。</div>
  </div>
</div>

<div id="p-plannotator" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/backnotprop/plannotator">backnotprop/plannotator ↗</a><span class="gt-lang">TypeScript</span><span class="gt-card-streak">连榜 2 周</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+1,764 ★</span><span class="gt-card-total">总 15,174 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>独立在浏览器中运行的代理审阅与批注工作台，支持对计划、文档、HTML 产物和代码 diff 划线留言并一键回传代理。</div>
    <div><strong>场景：</strong>编码代理一次改十几个文件或写长计划时，你想在可视化界面上逐段批注修改意见，而不是在终端里打长段文字描述位置。</div>
    <div><strong>选型建议：</strong>通过插件或 MCP 接入 Claude Code、Codex、OpenCode 等主流代理，支持 Git、jj 和 Perforce；如果日常只让代理改几十行小文件，这层浏览器工作台会多一步切换成本。</div>
    <div><strong>本周上榜：</strong>连续第 2 周在榜且列原始周榜第 1，本周新增 1,764，相比上周 2,926 放缓 39.7%。</div>
  </div>
</div>

<div id="p-claude-trading-skills" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/tradermonty/claude-trading-skills">tradermonty/claude-trading-skills ↗</a><span class="gt-lang">Python</span><span class="gt-card-streak">连榜 2 周</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+1,422 ★</span><span class="gt-card-total">总 8,524 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>面向个人投资者的交易流程技能集，涵盖市场状态判断、持仓复盘、选股筛选、仓位管理与交易日志。</div>
    <div><strong>场景：</strong>希望用固定清单辅助每周股市复盘、风险检查和记录交易习惯，把分析过程结构化。</div>
    <div><strong>选型建议：</strong>公开 CSV 和图表截图即可跑基础流程，接 Alpaca 或 FMP 属于可选增强；它明确不做自动下单与买卖信号，追求量化自动交易的人别选它。</div>
    <div><strong>本周上榜：</strong>连续第 2 周在榜，周增 +1,422，总星 8,524 里有 16.7% 来自本周。</div>
  </div>
</div>

<div id="p-ag-ui" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/ag-ui-protocol/ag-ui">ag-ui-protocol/ag-ui ↗</a><span class="gt-lang">TypeScript</span><span class="gt-card-streak">连榜 2 周</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+1,421 ★</span><span class="gt-card-total">总 21,796 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>连接后端 AI 代理与前端应用的双向事件交互协议，1.0 版已冻结规范并提供 TypeScript、Python 和 .NET SDK。</div>
    <div><strong>场景：</strong>后端已经用了 LangChain、CrewAI 等异构代理框架，前端需要统一承接流式输出、工具调用、共享状态和人工介入。</div>
    <div><strong>选型建议：</strong>与 CopilotKit 等客户端配合集成最快；如果前后端同栈且只服务单一框架，直接用框架自带流式接口更轻，不必强上协议层。</div>
    <div><strong>本周上榜：</strong>连续第 2 周在榜，本周 +1,421，较上周 2,773 回落近半，总星来到 21,796。</div>
  </div>
</div>

<div id="p-nod" class="gt-card" data-cat="ai-coding">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/n8n-io/nod">n8n-io/nod ↗</a><span class="gt-lang">TypeScript</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="ai-coding">+1,008 ★</span><span class="gt-card-total">总 3,751 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>n8n 推出的开源本地个人代理，自带桌面与浏览器控制能力，能跨 Gmail、日历、Slack、Notion 和真实网页自动催发票、准备会议简报或取消订阅。</div>
    <div><strong>场景：</strong>需要一个跑在自己电脑上的个人助理去处理繁琐的多网页表单与日常跟进，同时要求动作前确认授权。</div>
    <div><strong>选型建议：</strong>基于 Electron 和 TypeScript，支持 macOS 14+（Windows 11 与 Linux 为预览版），可自接 20 多家模型或 ChatGPT Plus/Pro；和只跑云端工作流的 n8n 主站比，它赢在本地保存登录态与密码且能直接操作网页，输在桌面端跨平台稳定度还在早期预览期。</div>
    <div><strong>本周上榜：</strong>本周新上榜即排原始榜第 4，总星 3,751 中有 1,008 颗（26.9%）来自本周。</div>
  </div>
</div>

### 2.2 开发者工具（2 个）

两个项目都面向研发工程链路，且都采用了 CLI 或 MCP 形态，把代码缺陷扫描和浏览器调试能力包装成编码代理与 CI 可直接调用的接口。

<div id="p-open-code-review" class="gt-card" data-cat="devtools">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/alibaba/open-code-review">alibaba/open-code-review ↗</a><span class="gt-lang">Go</span><span class="gt-card-streak">连榜 2 周</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="devtools">+2,259 ★</span><span class="gt-card-total">总 56,507 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>阿里开源的混合架构代码审查 CLI，结合确定性规则流水线与工具调用 LLM 代理，按行输出缺陷评论与全库扫描。</div>
    <div><strong>场景：</strong>CI 流水线里卡一道低误报自动评审，或提交前扫描 NPE、线程安全、XSS 与 SQL 注入等硬伤。</div>
    <div><strong>选型建议：</strong>Go 编写的 CLI，支持本地模型端点或复用宿主代理额度；和直接让通用编码代理审 diff 比，它赢在精准率更高且仅消耗约 1/9 的 token，代价是故意压低了召回率以减少噪音。</div>
    <div><strong>本周上榜：</strong>连续第 2 周在榜，本周 +2,259（总星 56,507 的 4.0%），较上周 +3,675 有所降温。</div>
  </div>
</div>

<div id="p-chrome-devtools-mcp" class="gt-card" data-cat="devtools">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/ChromeDevTools/chrome-devtools-mcp">ChromeDevTools/chrome-devtools-mcp ↗</a><span class="gt-lang">TypeScript</span><span class="gt-card-streak">连榜 2 周</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="devtools">+1,203 ★</span><span class="gt-card-total">总 59,437 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>Google 官方的 Chrome DevTools MCP 服务与 CLI，让编码代理直接操控真实 Chrome 调试网络请求、控制台报错和性能轨迹。</div>
    <div><strong>场景：</strong>代理改完前端页面后需要自己打开浏览器验证效果、抓报错栈，或录制真实性能 trace 定位卡顿。</div>
    <div><strong>选型建议：</strong>TypeScript 实现，仅支持 Chrome 及 Chrome for Testing，且默认收集使用统计和 CrUX 数据（可加参数关闭）；和 Playwright 方案比，它赢在原生 DevTools 性能面板与调试深度，输在不支持 Firefox 和 Safari 跨浏览器验证。</div>
    <div><strong>本周上榜：</strong>连续第 2 周在榜，本周 +1,203 仅占 59,437 总星的 2.0%，是本周存量占比最高、新增速率最低的头部工具。</div>
  </div>
</div>

### 2.3 前端与设计（1 个）

本周前端设计类仅剩 Meta 一家开源的组件体系在榜，重点在于把组件 API、设计令牌和给代理看的文档打包进同一套 CLI 工作流。

<div id="p-astryx" class="gt-card" data-cat="frontend">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/meta/astryx">meta/astryx ↗</a><span class="gt-lang">TypeScript</span><span class="gt-card-streak">连榜 2 周</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="frontend">+2,635 ★</span><span class="gt-card-total">总 33,252 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>Meta 内部沉淀八年的开源 React 设计系统，提供 150 多个可访问组件、CSS 变量主题与配套 CLI 文档。</div>
    <div><strong>场景：</strong>从零搭建 React 19 新项目或内部后台，需要人和 AI 代理共用同一份组件规范，减少代理瞎编样式。</div>
    <div><strong>选型建议：</strong>硬性要求 React 19 以上，底层用 StyleX 编写但对外暴露预构建 CSS 与 Tailwind 兼容类名，目前仍为 Beta；和 shadcn/ui 比，它赢在企业级组件密度与主题一致性，输在社区生态和版本稳定性还在早期。</div>
    <div><strong>本周上榜：</strong>连续第 2 周在榜，本周 +2,635 排全场增量第 3，总星达 33,252，热度虽低于上周首波爆发的 +8,700，仍是前端唯一代表。</div>
  </div>
</div>

### 2.4 其他（3 个）

这三个项目分属办公协作 SDK、个人财务自托管和 Apple Watch 工具，彼此没有技术交集，共性在于面向具体的垂直软件场景提供可控的本地或可嵌入替代方案。

<div id="p-univer" class="gt-card" data-cat="other">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/dream-num/univer">dream-num/univer ↗</a><span class="gt-lang">TypeScript</span><span class="gt-card-streak">连榜 2 周</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="other">+2,319 ★</span><span class="gt-card-total">总 31,920 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>可嵌入自有产品的开源办公文档与表格 SDK，Apache-2.0 核心覆盖电子表格、文档、幻灯片编辑和 Headless 公式计算。</div>
    <div><strong>场景：</strong>SaaS 或企业系统里需要内嵌一个类似 Excel/Word 的协同编辑器，或让 AI 代理通过统一 API 读写表格结构。</div>
    <div><strong>选型建议：</strong>Web SDK 核心开源可商用，但实时协同后端、服务端导入导出和商业 AI 扩展需另购授权；和直接嵌入在线表格 SaaS 比，它赢在数据留在自有业务内且可深度扩展，输在服务端协同需要自建或付费。</div>
    <div><strong>本周上榜：</strong>连续第 2 周在榜，本周 +2,319（总星 31,920），比上周的 +6,436 和上上周的 +5,845 明显回落，进入平台期。</div>
  </div>
</div>

<div id="p-sure" class="gt-card" data-cat="other">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/we-promise/sure">we-promise/sure ↗</a><span class="gt-lang">Ruby</span><span class="gt-card-streak">连榜 2 周</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="other">+1,509 ★</span><span class="gt-card-total">总 18,012 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>社区接手原 Maybe Finance 开源代码库并持续维护的自托管个人记账与财富管理应用。</div>
    <div><strong>场景：</strong>不想把银行账户、资产负债和消费流水交给商业记账平台，愿意自己跑一套容器管理个人财务。</div>
    <div><strong>选型建议：</strong>基于 Ruby 和 Docker 自托管，采用 AGPL-3.0 协议，支持浏览器、桌面、移动端与 LLM 代理接入；和商用记账软件比，它赢在隐私完全自主，输在部署维护和第三方银行同步需自己折腾。</div>
    <div><strong>本周上榜：</strong>连续第 2 周在榜，本周 +1,509（总星 18,012），从上周的 +4,786 回落后保持稳定长尾。</div>
  </div>
</div>

<div id="p-hermesenabler" class="gt-card" data-cat="other">
  <div class="gt-card-head">
    <div class="gt-card-title"><a href="https://github.com/pookjw/HermesEnabler">pookjw/HermesEnabler ↗</a><span class="gt-lang">Objective-C</span><span class="gt-chip is-new">本周新上榜</span></div>
    <div class="gt-card-stats"><span class="gt-card-gain" data-cat="other">+318 ★</span><span class="gt-card-total">总 2,533 ★</span></div>
  </div>
  <div class="gt-card-grid">
    <div><strong>定位：</strong>通过侧载自签名在普通 Apple Watch 上开启爱马仕表盘的工具。</div>
    <div><strong>场景：</strong>拥有非爱马仕版 Apple Watch 且系统正好落在特定版本窗口内，愿意开开发者模式折腾表盘。</div>
    <div><strong>选型建议：</strong>Objective-C 原生应用，仅支持 watchOS 27.0 至 27.3 beta 1，需用 Xcode、Sideloadly 或 WatchBridge 自签名安装；系统不匹配或不想每 7 天续签证书的用户直接跳过。</div>
    <div><strong>本周上榜：</strong>新上榜，本周仅增 +318（总星 2,533），无论体量还是周增量都是全榜最小。</div>
  </div>
</div>

## 3. 热度结构

{{< fig "diagrams/02-rank.svg" "按本周新增 ★ 降序排列；前 3 名用高亮色，last30days-skill 以 +5,302 遥遥领先，末尾 HermesEnabler 仅 +318。" >}}

{{< fig "diagrams/03-scatter.svg" "横轴为总 ★（对数刻度），纵轴为本周 +★；越靠右上体量与本周增量越高。悬停任意圆点可看项目名与两个数字。" >}}

5 万星以上的大存量项目正在熄火。`last30days-skill` 是唯一特例，以 75,450 总星拿到 +5,302（周增占比 7.0%）；另两个 5 万星项目 `chrome-devtools-mcp`（59,437，+1,203）和 `open-code-review`（56,507，+2,259）周增占比分别只有 2.0% 和 4.0%，主要靠存量规模挂在榜上。

本周真正有增长弹性的是 3,700 到 15,000 星的中小项目。新上榜的 `n8n-io/nod`（总星 3,751，+1,008）和 `ffmpeg-skill`（总星 11,336，+3,048）单周新增均占总星的 26.9%；Chrome 的 `modern-web-guidance-src`（20.7%）和 `stitch-skills`（19.9%）也有接近两成的单周贡献。`HermesEnabler` 虽然体量最小（2,533），但周增只有 +318，并未形成破圈扩散。

## 4. 趋势判断

{{< fig "diagrams/04-trend.svg" "柱 = 每周上榜项目新增 ★ 总和，折线 = 新上榜项目占比（右轴）。2026-W48 总增降至 29,133，新上榜占比为 21.4%（3/14）。" >}}

1. **AI 编程代理占比冲高至 64.8%，源于其他赛道大幅退潮。** 本周 AI 编程代理增至 8 个，合计新增 18,890 ★，反而低于上周 7 个项目的 23,291 ★；但因为安全类清零、前端与开发工具缩水，AI 类在全榜的增量占比从 37.9% 跳升到 64.8%。
2. **周榜与日榜交集跌到 2/14，周尾新热点已经切走。** 14 个周榜项目里只有 `last30days-skill` 和 `stitch-skills` 同时留在今日的 16 个日榜中，而日榜前四已经换成 `Claude-BugHunter`、`superpowers`、`CLI-Anything` 和 `minimind`，说明周榜存量老面孔的动能多发生在前半周。
3. **代理技能的边界正从写代码外溢到桌面日常与媒体处理。** 上周占榜的多是编码、审查、测试工具，本周新冲进前列的 `ffmpeg-skill`（+3,048）面向本地音视频剪辑，`n8n-io/nod`（+1,008）面向收发邮件与网页办杂事，Skill/MCP 封装正在向非开发工作流扩散。
4. **留榜惯性极强，但整体吸星能力腰斩。** 11 个项目连续两周在榜，换血率仅 21.4%，而全榜周增总和从 61,366 掉到 29,133（-52.5%），连原本连续两周保持 +5,800 以上的 `univer` 本周也回落到 +2,319。

## 5. 值得这周动手试的 3 个

- **mvanhorn/last30days-skill**：在 Claude Code 或 Codex 中运行全局安装命令，Reddit、HN 和 GitHub 数据源无需配置即可跑通，5 分钟能生成一份带引用的近 30 天讨论简报。最可能卡在 X 或 YouTube 等闭源平台的接口凭据与浏览器会话授权。
- **kajisho5/ffmpeg-skill**：确认本机装有 `ffmpeg` 和 Python 3 后安装技能包或挂载 MCP，选一段本地录屏让代理切 9:16 并生成字幕，20 分钟能看到本地 HTML 审片页结果。最可能卡在字体路径缺失导致烧录中文字幕报错。
- **n8n-io/nod**：在 macOS 14+ 设备下载安装桌面端并绑定模型 API key 或 ChatGPT 订阅，给它一个只读网页检查任务，15 分钟能走通授权审批流程。最可能卡在本地浏览器登录态隔离与企业账号的二次验证拦截。

## 6. 与上周对比

<div class="gt-table-wrap">
  <table class="gt-table gt-table-cmp">
    <thead>
      <tr><th>对比项</th><th class="gt-num">本周（W48）</th><th class="gt-num">上周（W47）</th><th class="gt-num">环比变化</th></tr>
    </thead>
    <tbody>
      <tr><td>上榜项目数</td><td class="gt-num"><strong>14</strong></td><td class="gt-num">16</td><td class="gt-num gt-delta">-2 席</td></tr>
      <tr><td>新上榜席位</td><td class="gt-num"><strong>3（21.4%）</strong></td><td class="gt-num">16（100.0%）</td><td class="gt-num gt-delta">11 席留榜</td></tr>
      <tr><td>周内新增 ★ 合计</td><td class="gt-num"><strong>+29,133</strong></td><td class="gt-num">+61,366</td><td class="gt-num gt-delta">-32,233 (-52.5%)</td></tr>
      <tr><td>最大单类赛道</td><td class="gt-num"><strong>AI 编程代理（8 个 / 64.8%）</strong></td><td class="gt-num">AI 编程代理（7 个 / 38.0%）</td><td class="gt-num gt-delta">增量占比 +26.9pp</td></tr>
    </tbody>
  </table>
</div>


- 连续在榜：11 个，均为连续 2 周（`plannotator`、`stitch-skills`、`modern-web-guidance-src`、`open-code-review`、`last30days-skill`、`chrome-devtools-mcp`、`claude-trading-skills`、`ag-ui`、`astryx`、`sure`、`univer`）。
- 掉榜：5 个（`elementalsouls/Claude-BugHunter`、`anthropics/skills`、`ZhuLinsen/daily_stock_analysis`、`vercel-labs/agent-browser`、`openai/codex-security`）。
- 周增总量比上周减少 32,233（-52.5%）；本周新增的 3 个面孔为 `ffmpeg-skill`、`n8n-io/nod` 和 `HermesEnabler`。

## 附：分类口径

八类固定词表为 `ai-coding`（AI 编程代理）、`model-infra`（模型与推理）、`devtools`（开发者工具）、`frontend`（前端与设计）、`data-infra`（数据与基础设施）、`security`（安全）、`learning`（学习与资源）、`other`（其他）。按项目实际核心用途归类，不按语言或发布公司划分；本周无法归入前七类的项目均归入「其他」，共 `dream-num/univer`、`we-promise/sure` 与 `pookjw/HermesEnabler` 三个。
