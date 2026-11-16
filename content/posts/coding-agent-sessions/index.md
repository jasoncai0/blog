---
title: "Coding Agent 的会话与历史：连续追问、权限、MySQL 分页与压缩"
date: "2026-11-16T18:30:00+08:00"
summary: "会话恢复不是把 JSON 读回来。run.finished 的生命周期、审批卡的位置、96 MB 事件日志的分页、MySQL 快照回放和 token 预算压缩，任何一项都会让长会话失真。"
description: "复盘 Jade 的连续追问、权限审批卡、MySQL 事件分页、FAILED 状态回放、上下文压缩、Trace 敏感性和一次性 CLI 退出条件。"
tags: ["harness", "event-sourcing", "context-engineering"]
slug: "coding-agent-sessions"
---

> 系列：[总览](/posts/coding-agent-pitfalls.html) · [循环护栏](/posts/coding-agent-loop.html) · [执行环境](/posts/coding-agent-sandbox.html) · [会话与历史](/posts/coding-agent-sessions.html)

这篇拆自[总览](/posts/coding-agent-pitfalls.html)，讲一轮结束后，什么该继续、什么该关闭、什么必须从磁盘恢复。

## 1. 会话恢复的顺序问题

会话保留最初只存在内存里。重启后 `/api/runs` 为空，但每个工作区其实已经有：

```text
.agent/transcript.json
.agent/events.jsonl
.agent/final.md
```

恢复时踩了两个顺序问题。

第一，`AgentLoop` 在 `finally` 里先持久化、再发 `run.finished`、再关闭日志。这样写出的 `events.jsonl` 里没有终止事件，恢复时无法判断 run 是正常结束还是中断。修法是先持久化快照，再写终止事件，最后再持久化一次事件日志。

第二，恢复 `RUNNING` 状态不能原样信。服务重启后旧 run 不可能还在跑，必须改成 `FAILED`，并写清楚“Run was interrupted by a server restart”。

恢复出来的是只读历史，还是可以继续追问？最初是只读，后来改成了连续会话。这个取舍放在下一节讲。

### 1.1 连续追问：`run.finished` 不是会话终点

把“恢复出来能看”改成“恢复出来能继续追问”时，真正的问题不是界面加一个输入框，而是事件流的生命周期。

旧实现里，`AgentLoop` 每一轮 `finally` 都关闭 `RunEventLog`，UI 收到 `run.finished` 就关闭 EventSource。于是：

- 第二轮没有地方写事件，浏览器也收不到。
- `run.finished` 被当成整个会话的终点，但它其实只代表“这一轮用户请求结束”。
- 恢复出来的日志是关闭状态，直接追加会丢事件，重新打开又可能和 UI 的 `Last-Event-ID` 对不上。

修法是把“轮”和“会话”分开：

- `run.finished` 每轮都发，但日志不关；关闭只发生在服务停机。
- 恢复的日志先保持关闭；收到 follow-up 时 `open()`，从旧序号继续追加。
- UI 收到 `run.finished` 不再断开 SSE；收到 `turn.started` 才重置本轮 step/phase/plan，并在时间线插入用户消息。
- 跨轮累积 usage，单轮重置验证状态；transcript 追加而不是覆盖。
- 浏览器重连时带 `?after=<lastSeq>`，避免历史事件重复插入。

验证用的是一段真实的 Java 会话：同一个 `run-c66e8593`，先让程序打印 `jade-v1`，追问改成 `jade-v2`，服务重启后再追问改成 `jade-v3`。三轮共用 `run-20261115-171012-47495a` 工作区，最终文件、transcript 和 73,139 tokens 的累计 usage 都对得上。后端集成测试也覆盖了“关闭服务 -> 恢复 -> 追问”的路径。

这里还有一个反直觉的坑：为了支持连续会话，`run.finished` 不再关闭事件日志。服务端没问题，但一次性 CLI 仍然只等 `RunEventLog.closed()`，于是产物和最终回答都完成了，进程又挂了 9 分钟。`jstack` 停在 `RunEventLog.awaitAfter`。修法是 CLI 在“事件已消费完且当前快照已终止”时退出，同时保留服务端的日志生命周期。教训是：同一套事件流，长驻 Web 和一次性 CLI 需要两个不同的退出条件。

### 1.2 审批卡不能藏在滚动区里

权限流程第一次联调时，后端状态已经是 `WAITING_APPROVAL`，`GET /api/runs/<id>/permissions` 也返回了 pending 请求，但浏览器里看不到任何批准按钮。现场很容易误判成 SSE 丢了事件。

实际原因是卡片被 `append` 到 Live Trace 时间线的末尾，而时间线本身有最大高度和独立滚动条。窗口没有滚到底时，卡片就在可视区下面；`scrollTimeline(true)` 也没有可靠地把它带出来。后来把卡片 `prepend` 到时间线顶部，仍会遇到下一个问题：当页面顶部被 run header 和 phase card 占满时，卡片标题能看见，操作按钮却落在 950px 视口之外。

最终修法不是在滚动区里继续调 `scrollTop`，而是把审批卡移出时间线：

- `showPermissionCard` 把卡片直接挂到 `document.body`，不再作为 timeline 的子节点。
- CSS 用 `position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%)`，配 `max-height: calc(100vh - 48px); overflow: auto`。
- 卡片带 `role="dialog"` 和 `aria-live="assertive"`；按钮提交时禁用，避免重复批准。
- 刷新或恢复会话时仍用 `GET /permissions` 补拉 pending 请求，不能只依赖 SSE。

验证场景是真实 DeepSeek 生成 `src/Fixed.java`：第一次写文件弹 `FILE_WRITE`，选 `Allow for session`；第二次运行弹 `EXECUTE`，选 `Allow once`。两次审批都有 `.agent/permissions.jsonl` 审计记录，最终 stdout 为 `fixed-card-ok`，run 状态 `SUCCEEDED`。


### 1.3 长会话历史：分页不是把旧事件重新播一遍

资产系统那个 run 留下了 96 MB、12,786 条事件。最初服务启动时会读取每个 run 的全部 `events.jsonl`，浏览器选中后又从 `seq=0` 把事件全部重放，结果一个失败会话就能把页面拖死。改用 MySQL 存事件后，历史接口只返回最后 150 条 compact 事件：LLM body 只保留 2,000 字预览，工具参数和输出也设上限；完整请求体单独按 `seq` 取。

分页本身又踩了一个状态回放的坑：点“加载更早事件”把旧的 `run.started`、`usage` 重放了一遍，页面上的 `FAILED` 会变回 `RUNNING`，token 也会跳回旧值。修法是在分页结束后重新应用当前 snapshot，只让历史事件生成 trace，不让它们改写运行状态。进度条也不该用 DOM 节点数，而要用已加载事件数；没有更早页时直接隐藏“加载更早事件”。

验证：`run-2952644c` 首屏 `150 of 12,786 events loaded`，加载一页后变 `300 of 12,786`，状态保持 `FAILED`；MySQL 里 150 条 tail 约 64 ms，单条 130 KB 的完整 LLM body 约 54 ms。

### 1.4 压缩不是裁剪：token 预算、钉住记忆、模型摘要

早期 Compact 只看字符数，做法是删最旧的 assistant/tool 组，再截断长内容。它有一个不显眼的 bug：`compacted` 只在“确实删了消息”时为真；如果已经只剩三组、只能靠截断降预算，截断结果会被丢弃，上下文仍然是超的，也没有 `compaction` 事件。

更麻烦的是语义。直接删掉旧组会丢掉：

- 用户中途追加的约束；
- 已经确认过的设计决定；
- 失败过的命令和原因；
- 下一步该做什么。

系统提示还写着“原始 transcript 在 `.agent/transcript.json`”，但持久化发生在压缩之后，该文件实际是压缩后的对话，不是原文。

新管线按四层处理：

1. **token 预算**：同时看估算 token 和字符预算，任一超限触发；token 估算把 ASCII 粗算为 4 字符/token，非 ASCII 按 1 字符/token。
2. **pinned memory**：system、第一条用户任务、包含最新用户消息的组不参与删除。
3. **工具输出微压缩**：旧的超大 tool result 只保留头 3,000、尾 1,000 字符，并标注省略量；assistant tool call 和 tool result 仍然成组。
4. **模型摘要**：被删的旧组交给一个无工具、`response_format=text`、1,200 token 上限的模型调用，生成“目标/决定/文件/命令/失败/剩余工作”摘要，作为新的 system memory 插回；摘要失败则退化为确定性说明，再截断剩余长内容。

压缩事件现在报告 removed、microCompacted、truncated、before/after chars、before/after tokens、summarized 和 summaryTokens。摘要调用计入 run 的 usage，但不发工具、不改工作区。

这件事的教训是：压缩的目标不是“把数组变短”，而是“在不超预算的前提下，保住继续工作所需的记忆”。只做裁剪，模型会重复劳动；只做摘要，不控制工具输出，又把预算烧在无意义的日志上。


## 2. Trace 很有用，但别把 key 写进去

模型请求体 trace 是排障利器。UI 里每次 `llm.request` 默认折叠，点开才显示完整 JSON：`model`、`temperature`、`messages`、`tools`、`tool_choice`。

实现上有一个必须守住的边界：trace 只记请求体，不记 `Authorization` header。请求体里也可能有 API key 以外的敏感内容：完整任务、用户上下文、工具输出。因此：

- trace 默认折叠，不主动展开。
- 不把请求体写进普通日志。
- `.agent/events.jsonl` 和 run 目录按敏感数据管理。
- 结构化输出用 `response_format` / strict tool schema 约束格式，但不要把它当成业务正确性的证明。



---

系列其他文章：[总览](/posts/coding-agent-pitfalls.html) · [循环护栏](/posts/coding-agent-loop.html) · [执行环境](/posts/coding-agent-sandbox.html)
