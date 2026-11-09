---
title: "循环之外：opencode、Claude Code、hermes、DSH 的任务编排对照"
date: "2026-11-09T22:10:00+08:00"
summary: "四个 harness 的主循环几乎是同一个形状。真正的差异在循环之外的四个位置：输入怎么排队、工具怎么分批、任务怎么交出去、状态存在哪。逐条对照源码后写下的机制笔记。"
description: "四个 harness 的主循环几乎是同一个形状。真正的差异在循环之外的四个位置：输入怎么排队、工具怎么分批、任务怎么交出去、状态存在哪。"
tags: ["harness", "architecture", "multi-agent", "concurrency"]
slug: "harness-orchestration-compare"
---

> 源码版本：opencode `2.2.5`，commit `a687c047`（2026-11-08）；Claude Code 类型面 `2.1.277`、本机二进制 `2.1.319`；hermes-agent commit `8a5a7ac`（2026-11-09）；DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`。
> 四份源码都在工作区：`.opencode-src/`、`harness-research/claude-code/`、`harness-research/hermes-agent/`、`harness-research/deepseek-harness/`，每个仓库都建了 CodeGraph 索引。

## 0. 先说结论

四个 harness 的主循环都能写成一个 for：把消息发出去，拿到工具调用，执行，把结果塞回去，再发一次。这一段代码谁都写得出，四家的实现也确实高度相似。

差异全在循环之外。具体是四个位置：

1. **L1 输入**：人在它跑的时候说话，这句话去哪，是排队、插队，还是打断。
2. **L3 工具批**：一次生成里的五个工具调用，并发几个、谁先提交、失败一个怎么办。
3. **L4 委派**：把任务交给另一个 agent 时，交的是什么上下文，结果从哪条路回来。
4. **L4 编排**：多步、多 agent 的流程由谁持有控制流——模型逐次调用，还是一段代码。

一句话概括四家的选择：

| | 编排住在哪 | 一句话 |
| --- | --- | --- |
| opencode | 一次 step 的 fiber 树里 | 生成到哪，工具就 fork 到哪，没有中央调度器 |
| Claude Code | 引擎中间件 + 可执行脚本 | turn 是一等对象，fan-out 交给 Workflow 脚本 |
| hermes | 显式的 phase 流水线 | 每个阶段可替换、可观测，先落盘再执行 |
| DSH | 插件 + 数据（任务板、目标、事件日志） | 循环本身是插件，协作状态是持久数据 |

{{< fig "diagrams/01-four-layers.svg" "同一个 agent harness 的四层。越靠近模型（L2），四家的实现越像；越往上，越是各自的设计选择。四家的分歧主要落在 L1 与 L4。" >}}

## 1. 先对齐术语：turn 和 step 不是同一件事

读这四份代码最容易踩的坑，是它们都用 turn，但指的不是一个东西。

| 框架 | 一次模型请求叫什么 | 「一轮」的边界 | 谁在记账 |
| --- | --- | --- | --- |
| opencode | Step | 一次 prompt 引发的若干 Step，直到不再需要续跑 | `session/step.ts` 的 `Outcome` |
| Claude Code | 一次 API 调用（loop 内） | turn：从收到 prompt 到 `stop_reason` 落定 | `turn.start` / `turn.step` / `turn.complete` 事件 |
| hermes | iteration，数的是 API 调用 | turn：一次 `run_conversation` 调用 | `_LoopState` + `IterationBudget` |
| DSH | step | turn：prompt 被提升、到 inbox 排空回到 idle | 会话事件 `step/start`、`step/end`、`turn/end` |

同一个词，opencode 的 Step 是 DSH 的 step，Claude Code 的 turn 是 DSH 的 turn，hermes 的 iteration 又是 DSH 的 step。后面我一律用 **step = 一次模型请求**、**turn = 一次任务回合**，需要时再标各家原名。

这个区别不是命名洁癖。它决定了三件事：步数上限按什么计数、压缩在哪一级触发、恢复时从哪里续。比如 hermes 的 `max_iterations` 数的是 iteration，而它的委派预算按的是另一个数（父 agent 500、子 agent 250），两者不是一回事。

## 2. 四层模型

回到图 1，从上往下读：

- **L4 委派与持久**：子会话、后台任务、任务板、脚本化 fan-out、持久目标。
- **L3 工具调度**：注册表、权限、沙箱、并发与取消、结果截断与落盘。
- **L2 主循环**：调用模型 → 执行工具 → 回灌结果，循环几次、谁来继续。
- **L1 输入队列**：人（或另一个 agent）在循环跑着的时候插话。

四家的 L2 几乎可以逐行对应，L1 与 L4 则各有各的写法。所以这篇的重点是 L1、L3、L4，L2 只讲到「怎么判定继续」为止。

## 3. L2：一圈里到底发生什么

{{< fig "diagrams/02-loops.svg" "同一个 step 内部，四条循环把「请求—工具—写回」摆成不同形状。灰色是模型请求，暖色是工具执行；四家的差别是两者重叠多少、结果按什么顺序承认。" >}}

### 3.1 opencode：工具在流里就 fork 出去

opencode 的 step 是「一次 `llm.stream` + 这次生成产生的工具」。关键在时序：它不等模型把话说完再执行工具，而是在 provider 的事件流里遇到第一个 `tool-call` 就开跑。

```ts
// packages/core/src/session/runner/step.ts:110-135（节选）
const providerStream = llm.stream(input.prepared.request, input.prepared.options).pipe(
  Stream.runForEach((event) =>
    Effect.gen(function* () {
      yield* publisher.publish(event)
      if (event.type !== "tool-call" || event.providerExecuted) return
      toolRuns.push({
        call: event,
        fiber: yield* restore(executeTool(event)).pipe(
          Effect.flatMap(toolOutput.truncate),
          Effect.flatMap((outcome) => publisher.toolExecution(event.id, event.name, outcome)),
          Effect.forkScoped,
        ),
      })
    }),
  ),
)
```

所以「一次生成里的多个工具并行」在 opencode 里不是调度决策，而是副作用：模型吐得越快，工具起得越早。step 结束时 `Fiber.awaitAll` 收拢，再把结果写回会话。

是否继续也由数据决定，不看计数器：只要有任一非 provider 执行的工具被 called 或 settled，这一步就欠模型一次请求（`publish-llm-event.ts:597-600`）。仓库的 `AGENTS.md` 把这条写成了硬规则：**一个 step 一次显式 `llm.stream`，禁止在内存里写工具循环**。

### 3.2 Claude Code：turn 是一等对象

Claude Code 把 turn 建模成事件：`turn.start`、`turn.step`、`turn.complete`，循环的出口是一个 8 值联合：

```ts
// mods/types/claude-code.d.ts:10931（节选）
stop_reason:
  | "end_turn" | "max_tokens" | "stop_sequence" | "tool_use"
  | "pause_turn" | "compaction" | "refusal"
  | "model_context_window_exceeded" | null
```

`tool_use` 表示「还要再来一次」，`pause_turn` 是服务端主动挂起，`compaction` 是压缩把上下文换了，`refusal` 是模型拒答。把「为什么停」显式化成枚举，好处是所有下游（UI、钩子、子代理回收）都不用猜；代价是每加一种停法都要改类型与分支。

一个 assistant message 里的多个 `tool_use` 天然构成一个 batch：批内并行、批间串行，`PostToolBatch` 钩子一批触发一次。`tool_result` 必须按 `tool_use_id` 紧随 `tool_use` 回灌——这个 id 是整个 loop 里唯一稳定的关联键。

### 3.3 hermes：把循环写成一条 phase 流水线

hermes 的循环体不在一个函数里堆着，而是拆成命名的 phase，每个 phase 返回一个动作，由循环统一裁决：

```python
# agent/conversation_loop.py:1750-1756（节选）
while _prelude_action != "break" and (
    (s.api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0)
    or agent._budget_grace_call
):
    if _run_phase(begin_iteration, agent, s).action == "break":
        break
    _run_phase(prepare_iteration, agent, s)
    _run_phase(assemble_api_request, agent, s)
    _run_phase(run_preflight_gate, agent, s)
    ...
```

`_LoopState` 是一个显式的状态对象，phase 按名取参数、回写 verdict。这种写法的收益是可观测：每一步都能挂钩子、能中断、能重试；代价是循环的控制流被打散在十几个函数里，读的时候必须顺着 `action` 的取值走。

两条硬约束值得单独拎出来：

- **先落盘，再执行**。`turn_tool_round.py` 的 docstring 写得很直白：工具调用轮次必须在任何副作用之前持久化，规范追加失败就直接结束 turn，而不是拿进程内状态去跑工具。
- **预算是共享的**。`IterationBudget` 每个 agent 一份（父 500、子 250），但 `execute_code`（程序化工具调用）的 iteration 会被 `refund()` 退回去，不占额度。

### 3.4 DSH：循环是插件，step 只有三种结局

DSH 的 `agent.ts` 把循环写成两段：`while (await this.turn()) {}` 负责拉 turn，`turn()` 里跑若干 step。step 的返回值只有三种：

```ts
// packages/core/agent-loop/src/agent.ts:584-597（节选）
if (finish.kind === "max-tokens") return { kind: "max-tokens" }

const toolCalls = message.content.filter((block) => block.type === "tool-call")
if (toolCalls.length === 0) return { kind: "completed" }

const result = await this.loopCtx.tools.execute({ calls: toolCalls, signal, agent: this, ... })
return result.concluded ? { kind: "completed" } : null
```

`null` 的意思是「还欠模型一次请求」，`completed` 是「这一轮可以收了」，`max-tokens` 是粘性的——后续 step 即使正常完成也不能覆盖它。turn 结束时还有一个 `agent/turn-stopping` 事件，监听器可以在这里 `steer()` 塞一条消息，循环发完事件会重读 inbox 再决定是否 break。

### 3.5 四家的停止条件

| | 正常停 | 预算 | 压缩在哪触发 |
| --- | --- | --- | --- |
| opencode | 没有工具调用需要续跑 | 可选 `agent.steps`，到顶后把 `toolChoice` 设为 `none`，逼模型只说话 | step 前后按压力检查 |
| Claude Code | `stop_reason` 落定 | 无步数上限，靠压缩 | `stop_reason: "compaction"` |
| hermes | 无 tool_calls | `max_iterations`（默认 500）+ `IterationBudget` | 工具结果之后、下一次请求之前 |
| DSH | `completed` 且 next-step 为空 | 无步数上限，靠外部插件（goal 的轮次、workflow 的 agent 上限） | `agent/pre-step` 与 `agent/request` 两处 |

opencode 那个 `steps` 的处理方式值得单独说：它不抛错、不截断，而是把最后一步的 `toolChoice` 设成 `none`，让模型「用一次没有工具的请求把话说完」。这比硬中断优雅，但也意味着步数上限不是安全边界——模型仍然会跑满这一步。

## 4. L3：并发不是「开个线程池」

四家都有并行工具调用，但没有一家是「把一批调用丢进池子」。真正要解决的问题是顺序与部分失败。

{{< fig "diagrams/03-batches.svg" "同一次生成里的工具调用如何分批。Claude Code 用连续安全段合并，DSH 用独占屏障切断，hermes 按读写冲突切段，opencode 则在流式事件里逐个 fork。" >}}

### 4.1 Claude Code：并发安全是 (工具, 输入) 的属性

从二进制里能直接读到这三行：

```js
// strings 2.1.319，工具基类默认值
isConcurrencySafe: (e) => !1
// 可读工具覆写
isConcurrencySafe(e) { return this.isReadOnly?.(e) ?? !1 }
// 分批器
if (b && y.at(-1)?.isConcurrencySafe) y.at(-1).blocks.push(r); else y.push(...)
```

翻译过来：默认不安全；只读工具安全；分批器只把**连续的**安全调用合进同一批，遇到一个写调用就切断。执行队列再把非安全调用当成屏障。上限由 `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` 控制。

注意这里的粒度：安全性不是工具的静态属性，而是 `(tool, input)` 的函数。同一个 `Bash`，`ls` 是只读、`rm` 不是。这也是为什么默认值是 `false`——判错方向的代价不对称。

### 4.2 DSH：独占屏障 + 连续槽提交

DSH 的规则写在工具接口上：只有 `isConcurrencySafe(args) === true` 才判并行，未声明、参数非法、分类器抛异常一律按独占处理（fail-closed，`pipeline.ts:100-112`）。批次调度里，`runGroup` 处理「一个独占调用」或「一个并行池」，结果的提交只跨**连续的模型序槽**推进：

```ts
// packages/core/tools/src/batch.ts:138-149（节选）
// `committed` advances only across contiguous model-order slots.
while (committed < group.length) {
  const slot = slots[committed]
  if (slot === undefined) break
  const result = await pipeline.complete(slot.exec, slot)
  settlement.commit(call!.block, result, callSeqs[committed])
  committed++
}
```

第 2 个调用先完成，也要等第 1 个提交完才能写回。这保证模型看到的结果顺序与它发出的顺序一致——对缓存、对推理连贯性都重要。并发上限是 `maxParallelToolCalls`，默认 10。

### 4.3 hermes：按读写冲突切段

hermes 的调度更接近数据库：把一轮调用切成 `("parallel" | "sequential", calls)` 的段，切段依据是 `_NEVER_PARALLEL_TOOLS`、参数能否解析、以及路径读写冲突。两个 reader 可以并行，一写一读就退回顺序。并发度 8。

### 4.4 opencode：没有上限，也没有批次

opencode 里根本没有「批次」这个对象。工具在流里各自 fork（见 3.1），顺序由「按来源排序 + 单写者发布」保证，而不是由提交屏障保证。全仓搜不到并发上限——真正的约束来自 provider 允许多少个并行 tool call。

这四种做法其实在回答同一个问题：**模型看到的结果，要不要严格保持它发出的顺序？** Claude Code 说「批内不用，批间必须」；DSH 说「必须，哪怕要等」；hermes 说「用读写冲突决定」；opencode 说「顺序由事件流本身保证」。

### 4.5 权限：四种不同的把关点

| | 决策 | 默认 | 谁能否决 |
| --- | --- | --- | --- |
| opencode | allow / ask / deny，规则 findLast | 未命中 → ask | 规则；无人值守时按 unattended 结算 |
| Claude Code | deny → ask → allow，先命中先赢 | 有规则才拦 | 规则；**钩子压不过规则** |
| hermes | 分类器 + 硬底线 + 白名单 | manual | 用户 deny 最高，其次硬底线 |
| DSH | allow / deny / ask | 无 approval 服务 → deny | 审批服务，一次一放行 |

两个细节值得记住。Claude Code 的 `PreToolUse` 钩子即使返回 allow，也不能绕过 deny/ask 规则——钩子是观察者与增强器，不是权限源。DSH 则在没有审批服务时直接 deny，把「忘装配」的后果定成拒绝而不是放行。

## 5. L4：子 agent 的四种物理形态

「委派」在四个框架里长得完全不一样，因为**子 agent 是什么**不一样。

{{< fig "diagrams/04-delegation.svg" "四种委派形态。opencode 是同一进程里的子会话 + job；Claude Code 默认后台进程，结果落在文件里；hermes 新建一个 AIAgent 线程，结果进共享队列；DSH 有进程内、进程外两种后端，并区分结果交付给谁。" >}}

### 5.1 opencode：子会话就是一个新 session

`subagent` 工具的参数里有 `sessionID`、`background`、`model`、`agent`。同步模式下父会话 `block()` 等结果，用户一按后台就原地升格成 job；异步模式立刻返回一句「完成时会通知你」。

完成回传不是函数返回值，而是一条合成消息：

```xml
<subagent state="completed">...</subagent>
```

以 `resume: false` 注入父会话。这条消息会唤醒父会话，但不会让它重放历史。子会话本身是独立 Session，有自己的 step 循环与压缩，父只拿最后一段文本。

深度默认 1（`subagent.ts:123-139`），也就是子会话不能再开子会话。job 的 id 就是子会话 id（`subagent-job.ts:44-63`），这让「任务」与「会话」在运维上是同一个东西。

### 5.2 Claude Code：默认后台，结果在文件里

`Agent` 工具的注释写得很清楚：*Agents run in the background by default; you will be notified when one completes.* 后台态立刻返回 `{status: "async_launched", agentId, outputFile}`——父拿到的是一个文件路径，不是一个结果。

要继续对话，用 `SendMessage` 唤醒子 agent；`Explore`、`Plan` 这类内置类型不可 resume。嵌套默认 3 层，并发上限 20。

这里有个有意思的取舍：opencode 把结果做成一条注入消息，Claude Code 把结果做成一个文件。前者省一次读，后者让父可以「先干别的，回头再读」，也让结果不必挤进上下文——`outputFile` 可以被 `Read` 截断读、按需读。

### 5.3 hermes：新建一个 AIAgent

`delegate_task` 支持 `spawn / list / steer / stop` 四个动作。子 agent 是一个真正的 `AIAgent`：新建线程、独立的 `task_id`（终端会话、文件缓存都跟着走）、继承父的工具集但禁用一批（`delegate`、`clarify`、`memory`、`send_message`、`cronjob`），系统提示按 goal + context 重建。

默认深度 1、子 agent 迭代上限 250、并发 10、异步 3。可选的 `delegation.worktree_isolation` 会给每个子 agent 开一个 git worktree，结束时按「零提交且干净 / 有遗留」决定删除还是归档到 `refs/hermes/archive/<tree>`。

异步结果**只以新 turn 落地**：子 agent 把结果推进共享的 `completion_queue`，父在下一次 turn 开始时取走。hermes 没有去改父正在跑的那一轮。

### 5.4 DSH：委派是一个 seam，不是一种实现

`ctx.subagents.startActivation(spec)` 背后可以是三种后端：进程内新子级（spawn）、带父历史的进程内子级（fork）、进程外的 ACP / Codex / Claude Code。工具只负责启动，立刻返回 `started subagent <name>`。

两个设计细节把「委派」和「通知」拆开了：

- `delivery: "parent" | "caller"`：结果是以消息进父的 inbox（模型看得见），还是只作为工具返回值给调用者（模型只在需要时读）。
- `sendMessage` 对运行中的目标是 steer，对已结束的目标是 resume，对外部后端则直接不支持。

子 agent 有持久 Session 与最多一个在线 activation；`waitForTreeWork()` 可以等到整棵子树排空——goal 的轮次驱动就靠这个判断「是不是真的静默了」。

### 5.5 五种「结果怎么回来」

| | 父子通道 | 结果载体 | 深度 | 能否继续对话 |
| --- | --- | --- | --- | --- |
| opencode | 合成消息 | 最后一段文本 | 1 | 能（同一 sessionID 再调） |
| Claude Code | 通知 + 文件 | `outputFile` | 3 | 能（SendMessage，部分类型不行） |
| hermes | 共享队列 | 新 turn 的消息 | 1 | 能（steer） |
| DSH | inbox 消息 / 工具返回 | 文本或结构化 | 由后端定 | 能（本地）/ 不能（外部） |

四家都在压窄回传通道。Claude Code 甚至给回传加了一道审查：子 agent 的报告要经过一个受审投递流程（专用工具、harness 打戳、分类器先审、文本消毒），父侧只看到一个 `send / flagged / withheld` 的结果。这是把「子 agent 的输出」当成不可信输入来对待——在 prompt injection 变成默认风险的今天，这个选择会越来越多见。

## 6. L4：编排原语的光谱

{{< fig "diagrams/05-spectrum.svg" "编排原语的横轴：控制流在模型手里，还是在代码手里。越往右，越可重放、越省上下文，也越不灵活。" >}}

### 6.1 两个都做了脚本：Claude Code 的 Workflow 与 DSH 的 workflow

2026 年最明显的一次收敛，是两家各自把 fan-out 从「模型连续调用工具」改成了「模型写一段脚本」。

Claude Code 的 `Workflow` 工具接受一个自包含脚本，开头必须是纯字面量的 meta：

```js
export const meta = { name, description, phases }
// 脚本体
const found = await parallel(urls.map((u) => () => agent(`读 ${u}`, { label: u })))
```

脚本里只能用 `agent / parallel / pipeline / phase`，`args` 原样注入。每次调用会把脚本持久化到 session 目录并在结果里返回路径，下次用同一个 `scriptPath` 重跑；`resumeFromRunId` 会让「prompt 与 opts 都没变的 `agent()` 调用」直接命中缓存。官方描述的上限是：16 个并发 agent、单次 `parallel`/`pipeline` 最多 4096 项、单次 run 最多 1000 个 agent。

DSH 的 workflow 几乎是同一个设计：

```ts
// packages/workflow/workflow-ptc/src/index.ts:105-111
maxConcurrentAgents: z.natural().default(0),
maxTotalAgents: z.natural().min(1).default(1000),
maxItemsPerCall: z.natural().min(1).default(4096),
```

1000 与 4096 这两个数字一模一样，`parallel`/`pipeline` 的语义也一样（逐项 catch → null，fatal 错误直接重抛）。差别在执行环境：DSH 把脚本放进 Node 的 `vm` 里跑，注入的全局函数被 `Object.freeze` 冻结，参数在进 VM 前做一次无损 JSON 物化；Claude Code 则是隔离 runtime，中间结果不进上下文。

为什么两家会同时走到这里？因为「模型连续调用 20 次工具做扇出」有三个绕不过去的问题：上下文被中间结果塞满、失败重试没有幂等、进度不可观测。脚本把这三点一次性解决：中间结果留在脚本变量里，`resumeFromRunId` 提供幂等，`phase()` 提供进度。代价是模型必须会写对这段脚本——所以两家都把「只有用户显式要求时才用」写进了工具描述。

### 6.2 DSH 的另外两块：任务板与目标

DSH 还有两个别家没有的原语。

**team 任务板**：同一棵 subagent 树的根 Session 上挂一块共享任务表，任务有 `revision`（CAS 更新）、`blockedBy`（无环 DAG）、`writeScopes`（提示性路径前缀，不是锁）。关键设计是**任务变更不唤醒 agent**——加一条任务、改一个状态，不会给任何 agent 发消息。协作靠 agent 自己去看板子，而不是靠消息风暴。

**goal 持久目标**：每个会话最多一个目标，状态落在 `goal/change` 事件里。驱动器只在 agent 完全静默（且后代子树排空）时，才把下一轮以 `<goal_round>` 用户消息排进 next-turn；额度用完就记一条 `round-limit` blocker。它解决的是「长任务怎么自动续」——不是 while 循环，而是「排队一条新的用户消息」。

### 6.3 没有 workflow 的两家

opencode 明确没有编排器、没有 DAG。它的 multi-agent 就是「一层委派 + 并行工具调用 + 后台任务」，我在[上一篇](https://jasoncai.site/posts/opencode-multiagent/)里已经拆过。

hermes 也没有 workflow 工具或 DSL。它有的是 `delegate_task` 的批量模式，以及一个 kanban 工具；`plugins/teams_pipeline` 名字像多 agent，实际是 Microsoft Teams 会议纪要的流水线。这个坑值得单独提一句：**按名字猜架构，在这四个仓库里都会猜错**。

## 7. 插话、中断、恢复：三个时间点

### 7.1 插话

| | 跑着的时候说话 | 语义 |
| --- | --- | --- |
| opencode | 入队 `session_inbox` | 当前 step 跑完再取；可以 abort 当前 step |
| Claude Code | 消息队列 | 排队；`Esc` 是 interrupt，不回灌当前 turn |
| hermes | gateway 输入队列 | 按会话串行；turn 之间插 |
| DSH | `steer()` 进 next-step，`followup()` 进 next-turn | next-step 立刻在下一个 step 边界生效，next-turn 等这一轮结束 |

DSH 的三条队列语义分得最细：`followup()` 排到下一轮，`steer()` 插到下一个 step，`inject()` 只塞进上下文、不唤醒。opencode 则相反——它只有一条 inbox，插队的粒度是「当前 step 结束」。

### 7.2 中断时什么被保存

| | 中断点 | 保存策略 |
| --- | --- | --- |
| opencode | 工具执行前先写 claim | 写前 claim 与 `Started` 同事务；用户中断释放，关机保留待恢复 |
| Claude Code | turn 被 abort | 正在跑的工具不重跑；JSONL 按 `tool_use_id` 落盘 |
| hermes | 每个工具轮次 | 先落盘再执行；规范追加失败即结束 turn |
| DSH | step 边界 | 仅追加事件日志 + 投影；`step/start` 先于执行写入 |

四家都把「执行前先留痕」当成不变量，差别在留痕的粒度：opencode 留 claim，CC 留 tool_use 块，hermes 留整轮，DSH 留 step 事件。

### 7.3 能不能回到过去

- opencode：能 rewind（软删消息，回到某个 step 之前）。
- Claude Code：checkpoint 保留最近 100 个，且只跟踪编辑类工具；`--resume` 命中还在跑的 background session 时会 attach，而不是重放。
- hermes：rewind 是软删（`active=0`），fork 是 `_branched_from` + clone；压缩会新建 continuation session（`parent_session_id` + `end_reason='compression'`）。
- DSH：**没有 rewind**，只能 fork 一个新会话；旧日志不可变。

这是四种不同的持久化哲学：opencode 把会话当可变文档，CC 当 append-only 的 JSONL 加检查点，hermes 当带版本的关系库，DSH 当纯事件流。

## 8. 代价

每种选择都有账单。

**opencode** 的循环最薄，薄到几乎没有「编排」这一层。好处是读起来快、行为可预测；代价是所有协作都得靠模型自己组织，子会话一多，父的上下文就会被各自的总结撑爆。

**Claude Code** 把能力做成了引擎的中间件与脚本，功能最全；代价是「模型看到什么」随版本与模型代际变化——`Grep`/`Glob` 在新版默认不给，`TodoWrite` 按模型门控。读它的行为必须同时读类型面、二进制与文档，三者还会不一致（`TaskOutput` 在类型里，但已经从模型可见工具里移除）。

**hermes** 的持久化最硬，代价是复杂度：单个 `state.db` 上有约 80 个 mixin，rewind、fork、压缩 lineage 三套语义交织在一起。它的 phase 流水线也一样，可观测性换来了「读一遍循环要跳十几个函数」。

**DSH** 的可替换性最强，代价是「不读配置就不知道实际跑的是什么」：循环本身是插件，工具执行器是插件，连「结果怎么回传」都是 spec 里的一个字段。同样的源码，换个 profile 就是另一种行为。

一个共同的账单是上下文。四家都要压缩，但触发点、保留策略、压缩后重建什么完全不同：Claude Code 是「按白名单重建工作集」，hermes 是「新建 continuation session」，DSH 是「只替换安全区，保持 tool-call/result 配对」，opencode 是「ceiling = window - max(15%, 16k)」。相同的是都在跟 prefix cache 搏斗——压缩一旦动了前缀，缓存就废了。

## 9. 怎么读的

四份源码分别用 CodeGraph 建了索引（opencode 118k 节点、DSH 144k、hermes 90k），先让子代理按同一套 9 节提纲出报告，再自己逐条核对关键判断。Claude Code 没有公开实现源码，只能三源并用：`mods/types/claude-code.d.ts`（13521 行，由 `/plugin-types` 从引擎自生成）、本机二进制 `2.1.319` 的 strings（Bun 保留了可读 JS，能直接引用执行器源码）、官方文档镜像。这也是为什么它那一节里有些结论标了「推断」——比如并发分批的细节，文档从未明说，只存在于二进制里。

四份报告与索引都在工作区：

| 材料 | 位置 |
| --- | --- |
| opencode 报告 | `opencode-orchestration-report.md` |
| Claude Code 报告 | `claude-code-task-orchestration.md` |
| hermes 报告 | `harness-research/hermes-agent-orchestration-report.md` |
| DSH 报告 | `dsh-orchestration-report.md` |

如果要只看一段代码，我的建议是：opencode 看 `packages/core/src/session/runner/step.ts`，Claude Code 看 `mods/types/claude-code.d.ts` 里 `Workflow` 与 `Agent` 两段，hermes 看 `agent/turn_tool_round.py` 的 docstring，DSH 看 `packages/core/agent-loop/src/inbox.ts`。这四处分别代表了四种把控制权交出去的方式。
