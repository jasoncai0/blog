---
title: "循环为什么比一次回答可靠——T-A-O 与压缩的边界"
date: "2026-11-11T17:54:27+08:00"
summary: "把 opencode、dsh、Claude Code 的循环还原成同一骨架：Thought、Action、Observation 是同一次生成的三份产物，观察回灌才是闭环成立的关键；压缩不在三要素里，它是 transcript 上的 GC。附一个可运行的 compact() 实现与三条轨迹对照。"
description: "拆解 Agent 循环的 T-A-O 结构、三家 harness 的实现差异、可靠性的因果链与成本边界，并用一个 Python demo 展示压缩如何切边界、pin 事实、改写窗口。"
tags: ["harness", "context-engineering", "architecture"]
slug: "agent-loop-tao-compaction"
---

> 源码基准：`opencode 2.2.5`（仓库根目录 `.opencode-src/`）、`dsh` 的 `packages/core/agent-loop/`。
> 文中的 demo 是一个 475 行的纯标准库 Python 脚本，模型与工具都是脚本化的，用来把机制跑给人看，不是 benchmark。

## 0. 先给结论

三家 harness 的循环是同一个骨架：`context → LLM → tool_use → execute → observation → 写回 context`。差异在宿主、持久化与权限，不在循环形状。

- **三要素不是三个步骤**。Thought 与 Action 是同一次生成的两类输出（thinking/text 与 tool_use），Observation 是 harness 执行之后写入的另一条消息。
- **循环成立的必要条件，是 observation 进入下一次调用的 messages**。不写回，循环就退化成"带工具的一次性回答"。
- **可靠性来自测量点，不来自轮数**。外部事实校验 → 假设空间裁剪 → 改计划，这条因果链只在观察能证伪时才成立。
- **压缩不是第四要素**。它不改世界，只改"模型看到的世界"；是 append-only 日志上唯一的一次 rewrite。
- **代价是真实的**：串行延迟、token 近二次增长、observation 本身会错、压缩有损且破坏前缀缓存。
- 一句话：**Observation 是对世界的测量，压缩是对自己记忆的转述；测量可以错，转述会失真。**

## 1. 一个骨架，三种实现

把循环写成最小形式，只有四件事：

```python
context = [system_prompt, user_message]

while True:
    assistant = llm.generate(context, tools=tool_schemas)
    context.append(assistant)                    # 别漏了这条
    if not assistant.tool_calls:
        return assistant.text
    for call in assistant.tool_calls:
        result = execute(call)                   # 权限 / 超时 / 幂等
        context.append(tool_result(call.id, result))
```

{{< fig "diagrams/01-loop.svg" "统一骨架：上下文送入模型，生成 tool_use 后由 harness 执行，结果作为 observation 回灌；没有工具调用时返回文本。" >}}

`context.append(assistant)` 这一行经常被忽略：工具结果必须跟在"发起调用的那条 assistant 消息"后面，协议才成立。少了它，下一次请求里会出现一个没有前因的 `tool_result`。

三家的实现差异，落在这张表里：

| 维度 | opencode | dsh | Claude Code |
| --- | --- | --- | --- |
| 循环宿主 | session runner 的 step | `ReactLoopAgent.turn()` 内的 `while` | Messages API 客户端 |
| 一次迭代 | 一次 `llm.stream` | 一次 step | 一次 `messages.create` |
| 上下文来源 | 从 SQLite 重新投影 | session 事件日志 + inbox | 进程内 `messages` 数组 |
| 继续条件 | `needsContinuation` | 响应里还有 tool-call block | `stop_reason == "tool_use"` |
| 终止 | 没有待执行工具 / 预算耗尽 | `completed` / `max-tokens` | `end_turn` |
| 观察回灌 | 工具结果持久化，下一 step 重载 | `session.append` + `inbox.next-step` | `tool_result` 追加进 `messages` |
| 副作用边界 | 权限回调 | tool gate | permission callback |

**opencode** 的选择最"事件溯源"：每个 step 只调用一次模型，输入不是内存里不断增长的数组，而是从持久化历史重新投影出来的窗口。runner 只判断这次响应是否还需要继续。仓库的 `AGENTS.md` 把这条写成了硬约束：

> Each step is one explicit `llm.stream` call that continues from reloaded history. Do not add an in-memory tool loop.

理由不是风格。崩溃恢复、interrupt/rewind、多进程同时观察一个 session，都要求"循环的状态在日志里，不在进程里"。代价是每个 step 都要重新装载与投影；收益是任何一个 step 都可以被重放、打断、从中间接上。

**dsh** 把循环放在 agent 内部，分 turn 与 step 两层：turn 是一次用户输入的完整处理，step 是一次模型调用。每个 step 的 assistant message 先 append 到 session；然后过滤出 tool-call block，为空就返回 `completed`，否则交给 `tools.execute`，把 `additionalContexts` 塞进 `next-step` 的 inbox，下一步开始时随上下文注入（`packages/core/agent-loop/src/agent.ts:587`、`:590`）。

**Claude Code** 直接贴着协议走：`stop_reason` 是 `tool_use` 就继续，把 `tool_result` 作为 user 消息追加；`end_turn` 就结束。权限回调卡在执行之前，子 agent 是另一层嵌套循环。

三种实现的共同点比差异更重要：**模型无状态，循环的全部记忆就是那组 messages；所谓 agent，就是反复把工具返回值写回 messages 再调用。**

## 2. 三要素：同一次生成的三份产物

{{< fig "diagrams/02-message.svg" "Thought 与 Action 共享同一条 assistant 消息；Observation 由 harness 写入 tool 消息，成为下一次生成的输入。" >}}

严格说，API 里没有 `Thought` 这个字段。它要么是 thinking block，要么就是普通 text。它和 `tool_use` 出现在同一条 assistant message 里，来自同一次采样。

| 要素 | 载体 | 在循环里的角色 | 能否省略 |
| --- | --- | --- | --- |
| Thought | thinking / text block | 解释器与策略：读观察、选下一步 | 可以，模型可以直接发工具 |
| Action | `tool_use` block | 唯一能改变外部状态的通道 | 终局轮可以没有 |
| Observation | `tool_result` 消息 | 唯一的外部事实来源 | 不可以，缺了就退化为开环 |

把 Thought 单独拆成一次模型调用，不会让它变成"第四要素"，只会多一个 step。反过来，一条 assistant 消息里同时有 text 和 tool_use 也很常见：text 是给人看的解释，tool_use 是给执行器的指令。harness 两个都要处理——只读 text 会丢动作，只读 tool_use 会丢理由。

## 3. Observation：唯一的测量

{{< fig "diagrams/03-open-closed.svg" "一次性回答没有测量点；循环把执行结果写回上下文，错误才有机会在下一轮被外部事实截断。" >}}

模型的参数没有变，prompt 里新增的只有这条结果。如果它没被写回，下一次调用的输入与上一次在信息上等价——采样会变，但不会有新信息。这就是"开环"：动作发生了，世界变了，模型不知道。

observation 要成为信息，至少得满足三件事：

1. **存在**。每个 `tool_use` 都必须有一个终态结果，包括异常、超时、被取消。dsh 在 step 抛错时会先补上 pending 的 tool result 再抛（`agent.ts:363-388`），否则下一次请求里会留下一个悬空的 `tool_use`。
2. **可判定**。`exit code 1` 不是观察，`stderr` 的最后十行才是。`0 tests passed` 可能是成功，也可能是"一个测试都没收集到"。
3. **没被截断到失真**。工具输出有长度上限，截断必须显式标记，并保留头尾；只留开头，往往正好丢掉报错。

一个常见的坏观察：命令超时被杀，harness 回了一句 `exit code 1`，没有 stderr。模型下一轮只能猜，通常会重跑同一条命令。这不是模型笨，是观察没有携带可行动的信息。

## 4. 为什么比一次性回答可靠

一次性回答没有测量点：模型输出即终点。它可以在内部自检，但自检与生成同分布，拿不到外部信息。循环插入了一个执行器，因果链有四段：

1. **外部事实校验**。动作在世界里发生，结果由执行器产生，可信度独立于模型。
2. **假设空间裁剪**。观察把"可能的世界"缩小；下一轮不再是重新猜，而是在剩余假设里选。
3. **观察后改计划**。这与 retry 不同：retry 是同分布重采样，改计划是拿到了新约束之后换路径。
4. **分解降难度**。一步只错一点，错误可以在下一步被局部修正，而不是整段重来。

把单步错误率记作 `p_step`，观察捕获错误的条件概率记作 `c`，观察本身出错或误判的概率记作 `p_oracle`，一次任务的残余失败率近似为：

```text
P_fail ≈ p_step × (1 − c) + p_oracle
```

这个式子解释了两件事。观察越强，`c` 越接近 1，循环越值；观察很弱时，`c` 接近 0，多轮只是把 `p_step` 重复 n 次，还额外付了延迟和 token。

所以"循环比一次性可靠"是有条件的：**可靠性来自校验点，不来自轮数**。纯回忆、纯改写、没有可执行 oracle 的任务，循环没有收益；`c` 很低的工具（比如只能返回"成功/失败"的接口），循环也接近掷硬币。

## 5. 压缩不在三要素里

{{< fig "diagrams/04-window.svg" "日志仍然 append-only；压缩把 keepID 之前的旧前缀替换成摘要，下一次请求看到的窗口由摘要与保留段拼成。" >}}

压缩不是第四个要素，因为它不产生关于世界的新事实。它处理的是记忆的表示：把一段已经发生过的 transcript，替换成一段更短的描述。

| 维度 | Observation | 压缩 |
| --- | --- | --- |
| 数据方向 | 世界 → 上下文，新增 | 上下文 → 上下文，重写 |
| 生产者 | 执行器 / 工具 | 模型自述，或模板 |
| 可信度来源 | 独立于模型 | 依赖模型，没有外部校验 |
| 对日志 | append | 日志仍 append，窗口被替换 |
| 错误后果 | 假事实，可被下一次观察纠正 | 记忆失真，可能永久丢事实 |
| 触发时机 | 每个 action 之后 | 预算、overflow、idle |

opencode 把压缩实现成一次显式的 compact：写入一条 compaction 消息，用 `metadata.keepID` 指向被原样保留的最近消息，历史由这个 id 切分。三个触发点：

- **事前**：发送前按 `window − reserve` 估算剩余空间，接近 ceiling 时触发；opencode 的 `reserve` 默认取 15%，且至少留 16k。
- **事中**：provider 返回上下文溢出，压缩后重试（`isContextOverflowFailure` → `Outcome.Compacted()`，`step.ts:160-171`）。
- **事后**：session 即将 idle 时后台预计算摘要，下次需要时直接可用（`llm.ts:321-324`）。

压缩有三个结构性代价，和实现无关：

1. **破坏前缀缓存**。压缩改写的是 prompt 前缀，provider 的缓存只能从头重建；压得越频繁，缓存收益越薄。
2. **记忆变二手**。摘要由模型自己写，没有外部校验；它可能漏掉数字，也可能把推测写成事实。
3. **不可逆**。一旦窗口被替换，原始观察只剩归档里那一份；归档丢了，就真的丢了。

## 6. 一个可运行的 compact()

demo 的结构是：`Session` 持有 append-only 的 `events`，`compact()` 在超预算时切出 keep 边界，把旧前缀归档、抽 pin、写摘要，然后替换窗口。模型与工具都是脚本化的，三条轨迹从同一初始状态出发，只改压缩策略。

{{< fig "diagrams/05-compact.svg" "compact() 的四个动作：切边界、归档、pin 加摘要、换窗口；虚线是摘要丢失事实时回读归档的退路。" >}}

核心函数：

```python
def compact(session, mode, reason):
    if not session.over_budget():
        return None
    before = session.tokens

    # 1) 从尾部回溯凑够 keep，再对齐到 assistant/user，不切开 tool_call/result
    acc, cut = 0, len(session.events)
    for i in range(len(session.events) - 1, 0, -1):
        acc += session.events[i].tokens
        cut = i
        if acc >= session.keep:
            break
    while cut > 1 and session.events[cut].role == "tool":
        cut -= 1

    dropped, tail = session.events[1:cut], session.events[cut:]
    if not dropped:
        return None

    # 2) 先归档：压缩可以丢表示，不能丢事实
    with open(f"runs/archive-{mode}.jsonl", "a", encoding="utf-8") as f:
        for ev in dropped:
            f.write(json.dumps(ev.__dict__, ensure_ascii=False) + "\n")

    # 3) 抽 pin + 写摘要
    pins = extract_pins(dropped) if mode == "pinned" else []
    summary = summarize(dropped, pins, mode)

    # 4) 换窗口：system 永远保留，摘要替换 keepID 之前的历史
    summary_ev = Event(session._n, "summary", "summary", summary,
                       meta={"keep_id": tail[0].id})
    session.events = [session.events[0], summary_ev] + tail
    return {"before": before, "after": session.tokens, "pins": len(pins)}
```

四个设计决定，对应四个容易踩的坑：

- **边界对齐**。keep 从尾部回溯凑 token，但切点落在 tool 消息上时向前退到 assistant，否则会把一对 `tool_call/tool_result` 切开，下一次请求直接违反协议。
- **归档先行**。先写 `runs/archive-<mode>.jsonl`，再改窗口。顺序反过来，一旦摘要丢了事实，原始观察就没有第二份。
- **pin 确定性抽取**。正则抓报错原文、`path:line`、执行过的命令，不交给模型转述。opencode 在摘要 prompt 里要求"保留精确路径、符号、命令、错误串"，这里再加一层确定性兜底。
- **system 不压缩**。角色、约束、验收标准永远不进入摘要，避免"压掉约束"。

demo 的场景是一个金额算错的接口：CI 日志里有 `E AssertionError: expected 402, got 500`、`api/handlers.py:88`，以及 110 行 `PASSED` 噪声；根因是 `total = subtotal + 100` 多写了固定费用，正确修复是 `+ 2`。三条轨迹：

| 策略 | 步数 | 压缩后 | pin 条数 | 累计输入 token | 结果 |
| --- | --- | --- | --- | --- | --- |
| pinned | 6 | 342 | 6 | 1947 | 成功 |
| naive | 9 | 271 | 0 | 2320 | 成功，但多花了 3 步 |
| naive-lost | 9 | 271 | 0 | 1989 | 失败 |

naive 轨迹里，摘要没有带上 `402`，模型先按"空值保护"改成了 `+ 0`，测试仍然失败；它只好重新读日志尾部，找回 `expected 402`，第二次才改对。naive-lost 把第二次读取变成 404——同样的压缩，在观察可重取时只是多一步，在观察不可重取时就是死路。

这正是压缩的真实代价：**它把"已经存在上下文里的事实"变成了"需要重新观察才能得到的事实"**。省下的是 token，付出的是对可重取性的依赖。

## 7. 压缩的坑与调优

| 坑 | 症状 | 调优 |
| --- | --- | --- |
| 摘要丢精确事实 | 下一轮重复读同一个文件、试同一条命令 | pin 报错原文、路径:行号、命令、URL；标识符不改写 |
| 摘要幻觉 | 出现对话里没发生过的结论或文件 | 摘要只引用事件 id；关键结论回读归档 |
| 压掉约束 | 忘了"不要动 main"、验收标准、用户偏好 | system 与验收标准永不压缩；用户原话保留 |
| 压后仍溢出 | 压缩本身超窗，或 keep 太大 | reserve 留够；先截断工具输出，再分层压缩 |
| 多轮坍缩 | 每压一次丢一点，几轮后目标漂移 | 保留原始目标与待办；增量合并，不重写 |
| 缓存抖动 | 压缩后每一步都重建前缀 | 在 idle 或批量时压；压完立刻发一次稳定前缀 |

对应的指标：压后任务成功率、每任务重复动作率、摘要事实保真率（抽样比对归档）、缓存命中率、压缩耗时 p95。只看 token 下降幅度会得出错误结论——naive 的窗口更小，累计 token 反而更高。

## 8. 成本与边界

**延迟**。step 是串行的：一次工具调用要等执行器返回，模型才能继续。工具 p95 是 3 秒，十步就是 30 秒的尾部延迟。并行工具调用能压缩一部分，但"观察之后才能决定下一步"的依赖无法并行。

**token**。第 i 步的输入约等于前 i−1 步的全部输出。n 步之后总输入近似 `nP + d·n²/2`（P 是初始 prompt，d 是每步新增）。前缀缓存只折扣重复的前缀，而压缩恰好会重置前缀。所以"循环更可靠"和"循环更便宜"从来不是同一句话。

**observation 也会错**。过期缓存、被截断的日志、有歧义的输出、flaky 测试、被注入的网页内容、模型误读、oracle 自身的 bug——每一种都会让错误的观察进入下一轮。错误的观察比没有观察更糟：它给出假确定性，模型会在此基础上继续推理。

**不可逆动作**。删除、发布、转账、发消息，一旦执行，观察只能确认后果，不能撤销。权限门应该卡在 action 之前，而不是靠 observation 事后补救。

**Goodhart**。如果奖励只看"测试通过"，模型会学会跳过测试、改断言、写 `assert True`。观察指标必须和真实目标对齐，否则循环会高效地优化错的东西。

**什么时候不该循环**：纯生成、纯回忆、没有可执行 oracle、延迟预算极紧、动作不可逆且无法沙箱。这些场景里，一次性回答加人工复核更划算。

## 9. 一句话

循环不是让模型变聪明，是给它一个被事实反驳的机会。压缩不是让模型记得更多，是让它忘得更有选择。

设计这套东西时，先问三个问题：观察能不能证伪？每个 `tool_use` 有没有终态？压缩丢掉的事实还能不能拿回来？三个都答"是"，循环才成立。
