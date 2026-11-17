---
title: "现场诊断：从一份会话日志定位失败"
date: "2026-11-17T20:50:00+08:00"
summary: "DSH 的故障排查不靠猜。日志里有七种 turn 结局、两种 attempt 结算、一对必须配平的工具事件；四张图给出一条从现象到复现的固定路径。"
description: "会话日志取证：turn/step/attempt 三段定位、工具配对、错误码分诊、复现矩阵。"
tags: ["harness","architecture","event-sourcing"]
slug: "dsh-diagnostics"
---


> 依据：DSH 0.2.2-rc.1（commit fcc0ad46）。事件类型来自 `packages/core/session/src/types.ts`，修复逻辑来自 `packages/core/session/src/repair.ts`，错误码来自 `packages/llm/llm/src/error.ts` 与各工具包。诊断流程是读码归纳，未做真实崩溃注入实验。

线上出问题时，能拿到的通常只有三样：用户的一句描述、一份会话文件、一段时间的日志。DSH 的设计让这三样够用，因为模型看到的一切都必须先落成事件，失败也逃不掉。

这篇讲怎么读。不是「看日志的技巧」，而是一条固定的取证路径：先定 turn，再定 step，再定 attempt，然后配平工具，最后看错误码决定复现方式。

{{< fig "diagrams/01-evidence-chain.svg" "五层证据：日志是唯一的原始事实，投影、指标、追踪、复现都是它的派生视图。" >}}

## 1. 先确认你看的是哪份日志

会话存储的形态（`docs/subsystems/session.zh.md`）：

| 版本 | 文件名 | 说明 |
| --- | --- | --- |
| v0 | `session.jsonl` / `session.jsonl.zstd` | 早期格式 |
| v1+ | `session.vN.jsonl` / `.zstd` | 带代际号，已提交的 generation 不被改写 |

两个容易踩的点：

- `.zstd` 不是可选的压缩开关，是持久化后端的选择。用 `zstd -dc` 解开再读，不要试图用文本工具硬看。
- 一个会话可能有多个 generation。恢复、迁移、压缩都会发布后继文件；最早的文件只覆盖到某个 seq。判断「日志是否完整」的标准是 `session/end-seed` 与最后的 `turn/end`，不是文件行数。

读完先做一件事：把事件按 `seq` 排序，确认没有空洞。`seq = log.length`，任何跳号都说明文件被手工改过或拼接错了。

## 2. turn 是第一层边界

`turn/end` 有七种 `reason`（`packages/core/session/src/types.ts:206-236`）：

| reason | 谁写的 | 含义 | 诊断价值 |
| --- | --- | --- | --- |
| `completed` | loop | 模型自然停、收件箱空 | 正常结束 |
| `aborted` | loop | 被取消，带 `TurnEndCancelCause` | 看 cause 区分 user/parent/hook/disposed |
| `blocked` | loop | pre-step 全部拒绝 | 拒因在 `agent/pre-step` 监听器里 |
| `error` | loop | 某步抛错，带 `LlmFailure` | 最常见，看 code |
| `max-tokens` | loop | 输出被截断 | 不是错误，是预算 |
| `interrupted` | repair | 崩溃留下的孤儿轮次 | 只由恢复写入，loop 从不实时发 |
| `forked` | buildForkSeed | fork 种子专用 | 不是故障 |

看到 `interrupted` 就说明进程死过。 它不由 loop 写，而是下一次打开会话时由 `openTurnClosers()` 合成的（`packages/core/session/src/repair.ts`）。同一批合成事件里还有 `step/end` 与补写的 `tool/result`。

`aborted` 的 cause 是对象，不是字符串：

```ts
type AgentCancelCause =
  | { kind: 'user' }
  | { kind: 'parent' }
  | { kind: 'hook'; reason: string }
  | { kind: 'disposed' }
```

`hook` 带 reason，这一条在排查「为什么它自己停了」时最有用，往往是某个 guard 或 hook 主动取消。

## 3. step 与 attempt：第二、第三层

一个 turn 里可以有很多 step，一个 step 里可以有很多 attempt（重试、压缩后重发、工具后继续）。

判断「这一步到底请求了几次模型」看 `assistant/*`：

| 事件 | 含义 | 是否进模型历史 |
| --- | --- | --- |
| `assistant/message` | 一次成功的提供方调用 | 是（空内容除外） |
| `assistant/message` + `interrupted: true` | 流到一半被取消，但有内容 | 是（前缀仍有效） |
| `assistant/attempt` | 失败、重试、取消、流错误 | 否 |

所以「模型回复了但用户没看到」有两种完全不同的原因：`attempt` 说明这次调用根本没成功；`message` 带 `interrupted` 说明成功了一半，客户端应当把已收到的前缀显示出来。

`assistant/attempt` 里没有内嵌 stream，只有 settlement。不要试图从 attempt 里找 token，它记录的是「这次尝试怎么结束的」，不是「模型说了什么」。

{{< fig "diagrams/02-turn-forensics.svg" "一个坏 turn 的事件时间线：step/start 后没有 assistant/message，只有 attempt 与 turn/end{error}；工具调用停在 tool/call 没有配对结果。" >}}

## 4. 工具必须配平

不变量只有一条：每个 `tool/call` 都必须有且只有一条 `tool/result`，顺序按模型给出的调用顺序，不按完成顺序。

诊断时按 `callId` 配对，三种异常各有名字：

| 现象 | 含义 | 修复怎么写 |
| --- | --- | --- |
| 有 call、无 result，且 call 之后进程死了 | 不知道工具有没有执行 | `TOOL_OUTCOME_UNKNOWN`，文案要求先核实外部状态或问用户 |
| 有 call、无 result，且 call 没到执行器 | 确定没开始 | `TOOL_NOT_STARTED`，文案允许直接重试 |
| 有 result、无 call | 日志被改过 | 拒绝重建 |

前两种由 `openTurnClosers()` 合成，`isError: true`，带 `sourceEventSeqs: [callSeq]`（只有 `started` 时带）。合成顺序固定：先补结果，再补 `step/end`，最后 `turn/end{kind:'interrupted'}`；`seq` 从最后一条 +1，`time` 复用最后一条的时间，因此合成事件的时间戳不可信，不要用它算耗时。

这条区分很实用：`TOOL_OUTCOME_UNKNOWN` 意味着可能已经写了文件、发了请求，盲目重试会重复副作用；`TOOL_NOT_STARTED` 才是安全的重试。

## 5. 错误码分诊

拿到 `LlmFailure.code` 之后，处置基本是确定的：

| code | 出现位置 | 处置 |
| --- | --- | --- |
| `CONTEXT_WINDOW_EXCEEDED` | 提供方返回 | 触发压缩后同 step 重试；压不下去就是配置问题 |
| `QUOTA` / `ACCOUNT_QUOTA_EXCEEDED` | 提供方返回 | 不重试，换账号或换路由 |
| `RATE_LIMIT` | 提供方返回 | 可重试，按 `providerRetryAfterMs` 退避 |
| `TIMEOUT` / `TRANSPORT` / `SERVER` | 传输层 | 可重试 |
| `EMPTY_RESPONSE` | 适配器归一化 | 可重试（模型确实可能空回） |
| `INVALID_CREDENTIAL` | 适配器 | 不重试 |
| `NO_ADAPTER` | `ctx.llm` 路由 | 路由没注册；中间件模式下可容忍 |
| `TOOL_TIMEOUT` | 超时策略替换结果 | 只影响该调用，兄弟调用继续 |
| `TOOL_ABORTED_BEFORE_DISPATCH` | abort 时未启动的调用 | 批次被取消，不是工具失败 |
| `TOOL_EXECUTOR_UNAVAILABLE` | 执行器缺失 | 环境问题，整批中止 |
| `TOOL_ACTIVATION_FAILED` | `when` 抛错 | 条件函数自己坏了，整步准备失败 |
| `INVALID_PREPARED_CALL` | 重复使用 prepared call | 编程错误 |

`error` 类 turn 的 `turn/end` 里直接带 `LlmFailure`，不用去别处找。若 `agent/request-error` 的监听器返回了 `{kind:'retry'}`，这次失败不会成为 turn 的结局，所以「日志里有 error」不等于「这一轮失败了」，要看最终 `turn/end`。

{{< fig "diagrams/03-error-triage.svg" "从错误码到处置的分诊树：先分「谁的错」，再分「能不能重试」，最后分「要不要人介入」。" >}}

## 6. 指标与追踪：确认不是「只有你这里慢」

日志给的是因果，指标给的是量级。DSH 的遥测走 OTLP log record，不是 span：

| 字段 | 作用 |
| --- | --- |
| `(turn, step)` | 把一条记录钉到具体步骤 |
| `callId` | 关联工具调用与结果 |
| `retryId` | 关联一次重试的前后尝试 |

没有 span 父子关系，因为 step 是串行推进的，坐标本身就表达了层级。想在追踪系统里重建调用树，按 `(turn, step)` 分组即可。

token 侧的入口是 `ctx.tokenMeter.measure(session)`，返回 `totalTokens` 与基线三态（`usage`/`estimated`/`none`）。看到 `estimated` 就不要用它做计费判断，那是启发式估算，规则是「提供方 usage 不小于全量估算时才采信」。

本地还有 `packages/experimental/inspector`：默认监听 `127.0.0.1:9230`，提供 `ctx.inspector.publish(topic, payload)` 与只读的 Cordis 拓扑查询，并捕获 fetch。它是开发期工具，不是生产探针，`maxJournalBytes`、`maxRetainedRequests` 这些上限就是为了防止它把内存吃光。

## 7. 复现：三条路，成本递增

| 方式 | 做法 | 适用 |
| --- | --- | --- |
| 回放 | 从日志尾部重建 surface，对比 `deriveMessages()` | 怀疑投影或压缩出错 |
| fork | `ctx.agents.create({ sessionId, seed, inheritedEventCount })` 在切点分叉 | 怀疑某一步之后走偏 |
| 最小脚本 | 只保留失败那一步的输入，写单测 | 怀疑代码路径 |

fork 的切点是最后一个 `turn/end`，不是任意 seq。原因很简单：在 turn 中间分叉会得到一个没有结算的轮次，恢复时会补一串合成事件，反而污染现场。

回放要注意：只读打开不会发布后继日志。想保留现场，先复制文件再打开，不要在生产会话上做实验。

{{< fig "diagrams/04-repro-harness.svg" "复现的三条路与各自的成本：回放只读、fork 分叉、最小脚本重写。" >}}

## 8. 设计取舍

| 取舍 | 选择 | 代价 |
| --- | --- | --- |
| 故障现场 | 只追加日志，不改写 | 文件会变大，需要压缩与归档 |
| 崩溃修复 | 下次打开时补合成事件 | 时间戳失真，需靠 `sourceEventSeqs` 追溯 |
| 工具配对 | 结果按模型顺序落库 | 慢调用会阻塞后面结果的提交 |
| 重试记录 | 失败也写 attempt | 日志条目变多，但能解释「为什么重试」 |
| 遥测 | 用 log record 而非 span | 没有原生父子链，要按坐标重建 |
| 诊断工具 | 放在 experimental | 生产环境默认不可用 |

## 9. 踩坑点

1. 把 `turn/end{error}` 当成唯一失败信号。`request-error` 重试成功后，中间的失败只在 attempt 与 `llm/retry` 里。
2. 用合成事件的时间戳算耗时。`openTurnClosers()` 复用最后一条的时间。
3. 以为 `tool/result` 按完成顺序写。按模型给出的调用顺序，乱序完成要等前面的 commit。
4. 看到 `TOOL_OUTCOME_UNKNOWN` 就直接重试。可能已经产生了副作用，先核实外部状态。
5. 从 attempt 里找模型输出。attempt 只记 settlement，没有 stream。
6. 把 `interrupted: true` 的 assistant/message 当成失败。它有完整前缀，客户端应当显示。
7. 在 turn 中间 fork。切点必须是最后一个 `turn/end`。
8. 用 `estimated` 的 token 数对账。它只是估算。

## 10. 自己搭的时候

- 把「失败」也做成事件。只记成功的话，线上永远只能看到「没回复」，看不到「为什么」。
- 给配对关系一个显式 id。`callId` 让工具调用与结果可配平；没有它，崩溃后无法判断哪些调用悬空。
- 区分「确定没开始」与「不知道有没有开始」。这两种失败的重试策略完全相反，混成一个错误码会逼出错误的自动重试。
- 让修复是幂等的。合成事件只在打开时写一次，重复打开不会叠加。
- 诊断入口要能在只读副本上跑。生产现场的价值在于「原样」，任何写操作都会破坏它。

## 11. 源码索引

| 位置 | 内容 |
| --- | --- |
| `packages/core/session/src/types.ts:206-236` | `TurnEndReasonMap` 七种结局 |
| `packages/core/session/src/types.ts:238` | `TurnEndCancelCause` |
| `packages/core/session/src/repair.ts` | `openTurnClosers()`、两种工具码、合成顺序 |
| `packages/core/session/src/index.ts:806` | `append()` 六道关 |
| `packages/core/session/src/index.ts:999` | `deriveMessages()` 增量派生 |
| `packages/core/agent-loop/src/agent.ts:505-520` | 中断时写 `assistant/message{interrupted}` |
| `packages/core/agent-loop/src/agent.ts:560-575` | 失败时写 `assistant/attempt` |
| `packages/llm/llm/src/error.ts` | 错误码常量与上下文溢出正则 |
| `packages/llm/llm/src/types.ts:48` | `LlmFailure` 形状 |
| `packages/llm/llm-retry/src/index.ts` | `llm/retry` 与 `llm/retry-started` |
| `packages/core/tools/src/batch.ts` | 结果按调用顺序提交 |
| `packages/core/tools/src/executor.ts:89` | `ToolBatchExecutionError(partial)` |
| `packages/experimental/inspector/src/index.ts` | `ctx.inspector` 与 9230 默认端口 |
| `packages/llm/token-meter/src/index.ts:83` | `measure()` 与基线三态 |

## 12. 最后

诊断路径可以压缩成六步：turn 定边界，step 定请求，attempt 定结局，callId 配工具，code 定处置，fork 定复现。

六步里前五步都只读日志，只有最后一步会创建新会话。这也是这套设计最实际的好处：出事时不需要「再跑一次看看」，先把日志读完。

系列导航：[Cordis 内部](/posts/dsh-cordis-internals.html) · [第五季导览](/posts/dsh-season-five.html) · [错误与重试](/posts/dsh-error-retry-recovery.html)
