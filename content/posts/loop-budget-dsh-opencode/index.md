---
title: "Agent Loop 的预算机制：DSH 与 OpenCode 源码对照"
date: "2026-11-11T19:00:00+08:00"
summary: "DSH 与 OpenCode 的 Loop 都没有单一的“预算旋钮”：压缩阈值、输出上限、工具截断、重试与轮次各自记账。本文按源码拆开每一项，并给出可交互的失控 turn 模拟器。"
description: "对照 dsh-agent-loop / dsh-compaction-basic 与 OpenCode session runner，解释预算如何改变 turn、step、请求与工具执行；含 4 张机制图和交互模拟器。"
tags: ["harness", "opencode", "architecture", "context-engineering"]
slug: "loop-budget-dsh-opencode"
---

> DSH 侧代码来自桌面版安装包 `app.asar` 内的 `@deepseek-ai/dsh-*` 包，正文行号以解包后的 `lib/index.js` 为准。
> OpenCode 侧代码来自 `.opencode-src/`，版本 `2.2.5`，commit `a687c047`（2026-11-08）；`packages/core/src/session/` 前缀在正文中省略。

“Loop 的预算”很容易被理解成一个数字：还能跑多少 token、还能调几次工具、还能花多少钱。真正读代码会发现，**没有一个地方在做总预算**。DSH 的 agent loop 明确写着没有内建 turn budget；OpenCode 的 runner 也只管 step 计数，不把 token、美元、墙钟放进同一个账本。

预算被拆成了几张互不相通的账：

- **上下文预算**：窗口还剩多少，什么时候压缩，压缩后保留多少。
- **输出预算**：单次请求最多生成多少 token，截断之后是结束还是续写。
- **工具预算**：一次工具结果能塞回多少行/字节，超出的部分放哪。
- **重试预算**：失败后还能再试几次，重试算不算新 step。
- **轮次预算**：goal / Ralph 这类外层循环还能开几轮。

这篇文章按“一次 turn 从进入到结束”的顺序，把两套实现的账本拆开对照，并说明每一笔预算到底改变了哪一步。文末有一个可交互的模拟器，可以自己把窗口、输出上限和 step 上限拉来拉去。

## 1. 先把四个词分开

| 词 | DSH | OpenCode |
| --- | --- | --- |
| Turn | 一次用户输入触发的执行段，可被 steering 延长 | 当前代码把“一次逻辑 LLM 调用”叫 Step；Turn 被保留给未来“从 promote 到 idle”的单位 |
| Step | 一次模型请求 + 随后的工具执行 | 一次 `llm.stream`，包含流式响应、工具调用与持久化 |
| Request | 一次 provider 调用 | 一次 attempt；重试或输出续写会再发一次 |
| Round | goal / Ralph 的外层轮次 | goal 插件的外层轮次 |

两套实现里，“一个 step”都约等于“一次逻辑模型调用”，但**只有 OpenCode 把 step 数量做成了可配置的上限**。DSH 的 step 是循环里的位置，不是带上限的计数器。

## 2. DSH：一个没有 step 上限的 turn

DSH 的 `dsh-agent-loop` 把一次 turn 写成 `while (true)`：每个 step 重新派生 history 和可见工具 schema，调模型、流式输出、执行工具，把接受的事实写回 session；只要模型还返回工具调用，就进入下一个 step。

```js
// dsh-agent-loop/lib/index.js（简化）
while (true) {
  const decision = await this.preStep(...)
  if (decision.kind === "reject") break
  await this.dispatch.serial("step/start", { turn, step })
  const outcome = await this.step(decision)
  await this.dispatch.serial("step/end", { turn, step, outcome })
  if (turnEnds && inbox.nextStep.length === 0) {
    await this.dispatch.serial("agent/turn-stopping", ...)
    break
  }
}
```

它没有 `step >= maxSteps` 这样的分支。README 的 Known Limitations 写得很直白：**No built-in turn budget**；工具调用和 steering 可以让同一个 turn 一直继续，宿主若想限制失控，要在 `agent/turn-stopping` 这个扩展点上取消。

{{< fig "diagrams/01-dsh-loop.svg" "DSH 的 turn/step 结构与四类预算作用点。DSH 没有 step 或 turn 数量上限，唯一常驻的刹车是 pre-step 的上下文压缩。" >}}

因此 DSH 的预算不是“拦住循环”，而是“让循环跑得下去”：在请求组装前压缩历史，在请求上限制输出，在工具结果上截断，在失败时重试。循环本身不记账。

### 2.1 上下文压缩：DSH 唯一常驻的软刹车

压缩策略在 `dsh-compaction-basic`。核心只有三行：

```js
// dsh-compaction-basic/lib/index.js:124-146（简化）
const messageBudget = W - reservedCompletionTokens;
const pressureBudget = messageBudget - headroomTokens; // headroom 默认 65,536
const thresholdTokens = Math.floor(Math.min(W * 0.8, pressureBudget));
const retainTokens = Math.floor(messageBudget * 0.16);
```

其中 `W` 是模型上下文窗口，`O = reservedCompletionTokens` 是这次请求预留的输出 token（`header.config.maxTokens ?? adapter 默认 ?? 0`）。默认参数是 `thresholdRatio = 0.8`、`retainRatio = 0.16`、`headroomTokens = 65,536`。

代入 `W = 200k`、`O = 8k`：

```text
T = min(0.8 × 200,000, 200,000 − 8,000 − 65,536)
  = min(160,000, 126,464)
  = 126,464
R = 0.16 × (200,000 − 8,000) = 30,720
```

这里有两个容易被忽略的推论。

第一，**输出上限越高，压缩越早**。因为 `T` 的第二个上界是 `W − O − headroom`；把 `O` 从 8k 提到 32k，`T` 会从 126.5k 降到 102.5k。为输出预留的空间，是从消息预算里直接扣掉的。

第二，**保留量与阈值是两回事**。`R = 30.7k` 不是“压缩后窗口只剩 30.7k”，而是“压缩后只把最近 30.7k 的消息逐字保留，其余压成一条 summary”。窗口仍然是 200k，summary 和最近消息一起重新占用预算。

{{< fig "diagrams/02-thresholds.svg" "同一个 200k 窗口下，DSH 在 126.5k 触发压缩并保留 30.7k；OpenCode 在 170k 触发并保留 15k。两者都预留了输出与缓冲空间，但预留方式不同。" >}}

触发点有三处，都在 `_registerAutomaticCompaction` 里：

- `agent/pre-step`：每个 step 开始前按 `pressure` 检查，压不到阈值以下就继续（失败只 warn，不阻断 turn）。
- `agent/request`：provider/model 切换时，用新模型的 header 重新计算；如果压不下去，抛 `MODEL_SWITCH_COMPACTION_FAILED`，保留旧模型。
- `agent/request-error`：只有 `CONTEXT_WINDOW_EXCEEDED_CODE` 才做 overflow 压缩；压缩前先跑 `toolResultPruner.pruneSession`，压缩范围 `retain = 0`，默认只重试 1 次。

压缩本身不能压缩 system prompt、工具 schema、session 前缀，也不能把一次不可分割的超大工具调用切开。这些是“固定开销”；如果它们本身就接近窗口，换模型或减工具才是解法。

### 2.2 输出上限：截断就是 turn 的终点

DSH 的 `maxTokens` 是单请求输出上限。流结束时如果 `finish.kind === "max-tokens"`，`step()` 返回 `{ kind: "max-tokens" }`，turn 被标记为收尾；除非 inbox 里还有新的 steering 消息，否则下一步就 break。

它没有“输出被截断，发一条 synthetic 消息让模型接着写”的机制。好处是语义简单：一次回答没写完就是没写完，不会在一个 step 里偷偷续命；代价是长回答必须靠模型自己分段，或者靠用户再发一条消息。

### 2.3 工具输出：head-tail 截断 + spill

`dsh-output-retention` 提供两种保留器：`ItemRetainer({kind:"head", maxItems})` 和 `TextRetainer({kind:"headTail", headBytes, tailBytes})`，后者按 UTF-8 安全切分，并在尾部生成 “Omitted N items.” 的提示。

`dsh-spill` 再把“模型不必内联看到的大文本”写到文件，返回 `{ locator, bytes, retrievalHint }`；`maxInlineTokens` 决定多大的工具结果只给预览 + locator。spill 失败时保留原文，属于 best-effort。bash/pwsh 走字符预算，文件工具走字节上限，不是所有工具共用同一个 token 阈值。

### 2.4 重试：同一 step，不增加计数

`dsh-llm-retry` 在 `agent/request-error` 上工作。normal 模式对 `EMPTY_RESPONSE`、`RATE_LIMIT`、`SERVER`、`TIMEOUT`、`TRANSPORT` 重试 5 次，指数退避 500ms → 10s，10% jitter；always 模式没有次数上限，直到成功、取消或插件卸载。

重试发生在同一个 open step 内，step 号不变；但每次重试都是一次计费请求。它买的是“别因为一次 429 结束 turn”，不是“多给 agent 几步”。

### 2.5 外层轮次：goal 256，Ralph 256

DSH 的 `dsh-goal` 默认 `maxGoalRounds = 256`，只对“进入模型历史的 goal round”计数；token、货币、墙钟、provider 配额都不在它的账上。到上限记 `round-limit` blocker。

`dsh-tool-ralph` 则是另一种外层循环：每轮起一个全新的 child，只带 immutable objective、上一轮 bounded report 和共享 workspace；`maxRounds` 默认 256，部署配置既是默认也是上限；终态是 `complete | blocked | budget-limited`，到上限时返回最后一轮报告。

两者都只数轮次。**轮次预算不是资源预算**：一轮里可以烧掉很多 token，也可以几乎不烧。

## 3. OpenCode：step 是硬上限，输出是软续写

OpenCode 的 runner 也是 `while (true)`，但循环体里有一个显式的 `step++`：

```ts
// packages/core/src/session/runner/llm.ts（简化）
while (true) {
  const next = yield* advanceToStep()
  continuing = yield* runStep(next.context, step)
  step++
}
```

新用户输入被 promote 时 `step = 1`；continuation 从持久化的 step 继续。`runStep` 内部还有一层 `while (true)`，负责重试和压缩。

### 3.1 `agent.steps`：到点就关工具

`steps` 是 agent 配置里的可选正整数（`packages/schema/src/agent.ts:33`），未设置就没有上限。runner 在每次请求前算：

```ts
// llm.ts:253
const stepLimitReached =
  loaded.agent.info.steps !== undefined && step >= loaded.agent.info.steps
```

到点时做两件事：

```ts
messages: stepLimitReached
  ? [...transcript.messages, Message.assistant(MAX_STEPS_PROMPT)]
  : transcript.messages,
toolChoice: stepLimitReached ? "none" : undefined,
```

`MAX_STEPS_PROMPT` 是一段很硬的提示：达到最大 step 数，工具被禁用直到下一次用户输入，只能文本回复，必须说明已达到上限、完成了什么、还剩什么、建议怎么做；任何工具调用都是 critical violation。

注意它还保留了 tool definitions，只把 `toolChoice` 设为 `"none"`，注释说这是为了维持 provider 的缓存前缀。工具执行层还有一道保险：`executeTool` 看到 `toolChoice.type === "none"` 就直接返回 `Tool.Error("Tools are disabled after the maximum agent steps")`。

{{< fig "diagrams/03-opencode-steps.svg" "OpenCode 的三道刹车：step 硬上限在第 N 步关闭工具；输出截断允许两次续写；压缩发生在请求前且不增加 step。" >}}

### 3.2 输出上限：两次续写，第三次失败

OpenCode 不把 `finish: "length"` 当作 turn 的终点。它发一条 synthetic 的 `CONTINUE_AFTER_OUTPUT_LIMIT`，让模型接着写；但连续 3 条 assistant 消息的 `finish` 都是 `length` 时，抛 `StepFailedError`：

```text
Response still truncated after two output token limit continuations
```

判断依据是最近 3 条 user/assistant 消息；只要中间出现 user 或其他 finish，streak 就重置。每次续写仍然走外层循环，所以**会消耗一个 step**。这意味着“先撞 step 还是先撞输出”，取决于 `steps` 和单步输出量。

### 3.3 压缩：请求前，且不花 step

`runStep` 在每次请求前先尝试自动压缩：

```ts
const estimate = SessionCompaction.estimatePrompt(loaded)
const compacted = yield* compaction.compact({ reason: "auto", context: loaded, estimate })
if (compacted.status === "failed") return yield* new StepFailedError({ error: compacted.error })
if (compacted.status === "completed") {
  assistantMessageID = SessionMessage.ID.create()
  continue
}
```

`completed` 时换一个新的 assistant message ID 并 `continue`，**不增加 step**；压缩完成后重新装载上下文，再走 step 上限检查。压缩失败则直接把这一步判失败。

阈值由 `packages/core/src/session/compaction.ts` 计算：

```text
window = min(input, context)（两者都有时）
C = window − max(floor(window × 0.15), window >= 32,000 ? 16,000 : 0)
K = 15,000（默认 keep）
```

默认保留 15% 且至少 16k 的 buffer；`keep` 是压缩后留在 summary 旁边的最近对话 token。`W = 200k` 时 `C = 170k`，`K = 15k`。和 DSH 不同，OpenCode 的 ceiling **不显式减去输出上限**；输出与 prompt 的超限由 provider 报错后走 overflow 压缩。

### 3.4 工具输出：2,000 行 / 50 KiB，超出落盘

OpenCode 也有工具结果截断（`packages/core/src/tool-output.ts`）：默认 `MAX_LINES = 2_000`、`MAX_BYTES = 50 * 1024`；超出时只保留头部能放下的行，把完整文本写到 `tool-output/tool_<id>`，结果里留一条 `[showing lines 1-N of M; full output saved to …]` 的标记。文件保留 7 天；阈值可用 `tool_output.max_lines` / `tool_output.max_bytes` 覆盖。

它和 DSH 的差别在形状：OpenCode 是全局 head-only 上限 + 落盘；DSH 有 head-tail、按工具的字节/字符预算、以及 `maxInlineTokens` 预览阈值。两者都不是“按 token 精确记账”，而是用行/字节做近似。

### 3.5 重试与配额

OpenCode 的 `model-retry.ts` 用 2s→4s→8s→10s 的指数退避，最多 10 次，约 84 秒；`Retry-After` 上限 15 分钟；不可达网络有独立的 1 小时窗口、30 秒 gap；超时重试 3 次。重试在 `runStep` 内层循环，**不增加 step**。

`packages/core/src/usage-limits.ts` 会读取 OpenCode Console 的 `current-budgets`（美元预算、scope、重置时间）和 Go 的 rolling/weekly/monthly 百分比，15 秒超时，403 视为无预算。但在 runner 的请求路径上没有看到“预算不足就拒绝请求”的分支——它主要驱动 UI 的配额展示。也就是说，**美元预算是可观测的，不是 loop 内的硬刹车**。

### 3.6 外层轮次

OpenCode 的 goal 插件：`MAX_OBJECTIVE = 4,000`、`MAX_ROUNDS = 50`、`MAX_STALLS = 3`、`WAIT_LIMIT = 30 分钟`。同一个 reason 或没有用工具算 stall，连续 3 次 pause；等待后台 shell 超过 30 分钟 pause。每轮 reviewer 请求过大时最多压缩一次。

50 轮比 DSH 的 256 轮紧得多，但同样是轮次预算，不是 token 预算。

## 4. 同一场“停不下来”的实验

把两套规则放进同一个简化模型，会看到完全不同的失控形状：

- DSH 没有 step 上限。只要模型继续调工具，turn 就继续；每步输入是 `system + tools + history`，history 到阈值就压缩，压缩后从 `retain` 重新长。
- OpenCode 有 step 上限。到第 `steps` 步强制 `toolChoice: none`，模型只能文本收尾；输出截断则允许两次续写，第三次失败。

下面的模拟器把这两个循环并排跑。拖动窗口、输出上限、每步新增、step 上限和单步输出，点“运行”，可以看到压缩次数、累计输入和最终保留量的差别。数值是教学简化模型，不是 provider 账单。

<style>
.loop-lab { margin: 1.6rem 0 2rem; }
.loop-lab iframe {
  display: block;
  width: 100%;
  height: 1220px;
  border: 1px solid rgba(45, 49, 66, 0.16);
  background: #f5f5f5;
}
.loop-lab .lab-link {
  margin-top: 0.55rem;
  font-size: 0.82rem;
  text-align: right;
}
@media (max-width: 996px) {
  .loop-lab iframe { height: 2100px; }
}
@media (max-width: 720px) {
  .loop-lab iframe { height: 2340px; }
}
</style>

<div class="loop-lab">
<iframe src="/lab/loop-budget.html" title="DSH 与 OpenCode 的 Loop 预算模拟器" loading="lazy"></iframe>
<div class="lab-link"><a href="/lab/loop-budget.html" target="_blank" rel="noopener">在新窗口打开模拟器 ↗</a></div>
</div>

用默认参数（W=200k、O=8k、每步新增 6k、OpenCode steps=40、单步输出 1.5k）跑满，会得到：

| 指标 | DSH | OpenCode |
| --- | --- | --- |
| 停下来的原因 | 没有 step 上限，模拟器在 60 步停 | 第 40 步 `toolChoice: none` |
| 压缩次数 | 3 | 1 |
| 累计输入 | 4.58M token | 3.33M token |
| 最终保留 | 79k | 93k |

几个可以直接观察到的现象：

1. **DSH 的“停不下来”不是 bug，是设计。** 它的失控保护在 turn 之外：要么宿主在 `agent/turn-stopping` 取消，要么靠 goal/Ralph 的轮次上限，要么靠用户中断。
2. **DSH 压缩更早，单步更便宜，但跑得更久。** 60 步累计 4.58M，平均每步约 76k；OpenCode 40 步累计 3.33M，平均每步约 83k。早压缩把每次请求的 history 压低了，但没有 step 上限，总账还是可能更大。
3. **输出上限的语义完全不同。** 把“单步输出”拉到 `O` 以上：DSH 第一次 `finish: max-tokens` 就进入收尾；OpenCode 会续写两次，第三次才报 `output-limit`。
4. **压缩次数不等于压缩成本。** 每次压缩本身也是一次模型请求（summary），只是模拟器没有把 summary 请求计入“累计输入”。

## 5. 预算到底影响了哪些步骤

把两套实现按阶段摊开，预算的落点是这样的：

{{< fig "diagrams/04-budget-matrix.svg" "预算影响的九个阶段：turn 进入、pre-step、组装请求、模型调用、流结束、工具执行、step 收尾、turn 收尾与外层轮次。红条是会直接中止当前 turn/step 的硬预算。" >}}

逐条说：

**turn 进入。** DSH 没有入环预算，steering 还能继续延长；OpenCode 在新输入 promote 时把 step 复位为 1，continuation 则继承持久化的 step。两者的“还能不能继续”都不由 token 决定。

**pre-step。** DSH 在这里做压缩检查，阈值是 `min(0.8W, W−O−65,536)`；OpenCode 在 `runStep` 的请求前做 auto compaction，阈值是 `W − max(0.15W, 16k)`（窗口 ≥32k 时取 16k 下限）。这是最像“预算”的一步：它直接决定下一步的 history 大小。

**组装请求。** DSH 每步重发 system + tools + history，`token meter` 用 4 字符/token 估算；OpenCode 用 summary + 最近 keep 消息。两边都没有“只发增量”的选项，缓存靠稳定前缀，不靠少发。

**模型调用。** DSH 的 `maxTokens` 是单请求输出上限，未配置时用 adapter 默认；OpenCode 在这一步检查 `step >= agent.steps`，到点把 `toolChoice` 设为 `none`，并追加 `MAX_STEPS_PROMPT`。**这是 OpenCode 唯一会因为“步数”而拒绝工具的硬停点。**

**流结束。** DSH 把 `max-tokens` 记为 turn 的结束原因，不做自动续写（有 steering 才继续）；OpenCode 遇到 `length` 发 synthetic 续写，连续 3 次才失败。同一个“输出被截断”，一个是终点，一个是两次缓刑。

**工具执行。** DSH 用 head-tail / spill / `maxInlineTokens` 控制结果体量；OpenCode 用全局 2,000 行 / 50 KiB 上限 + `tool-output` 落盘。两者都不让工具结果无限膨胀，但都不是精确 token 预算。

**step 收尾。** DSH 的 `step++` 没有上限，有 tool call 就回到 pre-step，turn 不重置；OpenCode 的 `step++` 是计数器的递增，压缩完成时 `continue` 不增加 step，重试也不增加 step。

**turn 收尾。** DSH 的 `agent/turn-stopping` 是扩展点，不是内置预算；OpenCode 到 step 上限后强制文本收尾，工具禁用直到下一条用户消息。

**外层轮次。** DSH 的 goal 256 / Ralph 256，OpenCode 的 goal 50 轮 / stall 3 / wait 30 分钟。两者都只数轮次，不计量 token、货币或墙钟。

## 6. 设计取舍

**软预算 vs 硬预算。** DSH 把“停”交给上下文压缩和宿主扩展点，Loop 本身不判断“该不该继续”；OpenCode 把“停”内建为 `agent.steps`，到点一定关工具。前者灵活，但默认没有上限；后者可预测，但需要用户显式配置 `steps`，不配就是无限。

**输出截断的两种语义。** DSH 的 `max-tokens` 以收尾结束 turn，适合“一次回答必须完整”的场景；OpenCode 的续写适合“长文/长代码，允许分段”的场景，但续写会吃 step，且第三次失败会把这一步判死。没有哪种更好，取决于产品希望“截断”是错误还是正常路径。

**压缩频率与断层深度。** DSH 更早压缩、保留更多，压缩更频繁但每次丢得少；OpenCode 更晚压缩、保留更少，压缩次数少但单次断层更深。`thresholdRatio` / `retainRatio` / `headroom` 和 `buffer` / `keep` 就是调这个权衡的旋钮。

**预算的可观测性 ≠ 可执行性。** OpenCode 能读到美元预算，但 runner 不在请求前拦截；DSH 的 goal 只数轮次，不计量钱。如果产品需要“花到 5 美元就停”，得在 `agent/request` 或宿主层自己加一条硬规则，不能指望现有的 context/step 预算代劳。

**重试是最容易被忽略的预算。** 它不增加 step，但会增加请求数和账单；DSH normal 模式 5 次、OpenCode 10 次，always 模式还可能无上限。重试策略应该和“这一步是否可重放”一起看，而不是只配一个次数。

## 7. 源码索引

DSH（安装包内解包后的路径）：

- `dsh-agent-loop/lib/index.js`：`turn()` 的 while 循环、`step()`、`agent/request-error`、`agent/turn-stopping`。
- `dsh-agent-loop/README.md`：Known Limitations 明确 “No built-in turn budget”。
- `dsh-compaction-basic/lib/index.js:124-146`：`resolveCompactSpec`；`:757-759`：`reservedCompletionTokens`；`:834-905`：自动压缩的三个 hook。
- `dsh-token-meter`：`measure()` / `contextPressure()` / `contextBreakdown()`，4 字符/token 估算。
- `dsh-output-retention`、`dsh-spill`：head-tail 保留器与落盘 locator。
- `dsh-llm-retry/README.md`：normal 最多 5 次重试、always 无上限。
- `dsh-goal`、`dsh-tool-ralph`：256 轮默认值，只数轮次。

OpenCode（`.opencode-src/`）：

- `packages/core/src/session/runner/llm.ts`：外层 step 循环、`stepLimitReached`（:253）、compaction 调用、OutputLimit 分支。
- `packages/core/src/session/runner/max-steps.ts`：`MAX_STEPS_PROMPT`。
- `packages/core/src/session/runner/step.ts:100`：`toolChoice === "none"` 时拒绝工具。
- `packages/core/src/session/compaction.ts`：`calculateCeiling`、`inputWindow`、`summaryRoom`、`SHRINK_STEPS`。
- `packages/core/src/tool-output.ts`：2,000 行 / 50 KiB、落盘与 7 天保留。
- `packages/core/src/plugin/goal.ts:15-19`：50 轮 / 3 stall / 30 分钟。
- `packages/core/src/model-retry.ts`：10 次、2s→10s 退避。
- `packages/schema/src/config/tool-output.ts`：`max_lines` / `max_bytes`。

最后回到开头那句话：**Loop 的预算不是一个数字，而是一组作用在不同阶段的规则。** DSH 选择让循环自由跑、用上下文压缩和扩展点兜底；OpenCode 选择在 step 上装一道硬门，再用输出续写和压缩把门内的空间用满。理解这一点，比记住某个阈值更重要。
