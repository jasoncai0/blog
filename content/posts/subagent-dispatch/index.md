---
title: "子 Agent 的调度：委派、工具调用与中控的规划"
date: "2026-11-24T13:50:00+08:00"
summary: "工具调用和子 Agent 委派是两条不同的边：前者在父循环里同步执行，结果原文留在上下文；后者另开会话，只回结论。中控是否存在是拓扑选择，它的「规划」大多不是调度器读的数据，而是每轮上下文加一次模型决策。配五张图和四段伪代码。"
description: "拆解多 Agent 系统里子 Agent 的调度：工具调用与委派的三个差别（上下文、控制权、失败边界）、三种协调拓扑（handoff、supervisor、代码编排）、计划的三层结构、一轮调度循环的伪代码、四种派发粒度，以及五个失败模式。"
tags: ["multi-agent", "architecture", "planning", "harness"]
slug: "subagent-dispatch"
---

给子 Agent 派活和调一次工具，在协议层长得几乎一样：模型输出一个 `name` 加 `args`，系统执行，把结果送回去。

差别在返回的东西。工具返回数据，父模型下一轮读到原文。委派返回一个句柄，真正回来的是另一个会话的结论。

这个差别往下推，会改三件事：父上下文怎么涨、控制权在谁手里、失败在哪一层被接住。再往上推，会改一件更大的事：所谓「中控协调器」，到底在协调什么。

## 1. 工具调用是同一循环里的同步调用

一次工具调用的完整路径是这样：

1. 工具的名字、描述、参数 schema 随每次请求一起发出去，占着上下文。
2. 模型输出一个 tool call，参数是 JSON。
3. 运行时执行 handler，拿到结果。
4. 结果作为一个 tool result 块追加进同一份 transcript。
5. 下一轮请求把这份 transcript 原样带上，模型读到的就是原文。

第 4 步是全部区别的来源。一次 `grep` 返回三百行命中，这三百行就永久留在上下文里，后面每一轮都要为它付 token。父模型看得见每一行的内容，也看得见异常堆栈，所以它能在下一步立刻改参数、换工具、或者停下来问人。

同步指的是父循环停在这一步等结果。一轮里可以并行发多个 tool call，但那仍然是同一轮里的多个动作，结果都落在同一份 transcript 里。

## 2. 委派是另起一个循环

委派的实现不是「把 handler 换个地方跑」，而是新建一个会话。这个会话有自己的系统提示、自己的工具白名单、自己的预算、自己的 transcript。

父 Agent 发出 spawn 之后拿到的是一个 id，不是结果。子会话在自己的循环里跑完，把一段结算消息送回父的消息队列，内容通常是结论、状态、用量。父上下文只多了两行：派活时写的 brief，和回来的一句结论。

{{< fig "diagrams/01-tool-vs-delegation.svg" "工具调用的结果原文回填父上下文；委派另开会话，父只收到 brief 与回执。同一份上下文因此有两种增长速度。" >}}

三个差别值得逐个说。

1. **上下文。** 工具结果原样进父上下文，细节全在，token 也全在。子会话只回结论，中间读过的文件、跑过的命令、失败过的尝试都留在子会话里，父看不到，也不用为它们付后续的 token。
2. **控制权。** 工具调用里，下一步永远由父模型决定；子 Agent 里，下一步由子模型决定，父只能发消息干预，不能替它选工具。这是「委派」和「调用」真正的分界：交出去的是决策权，不只是执行能力。
3. **失败边界。** 工具抛异常，异常文本直接出现在父的下一步里。子会话失败，父收到的是带状态的回执，看不到失败前那二十步。父可以重试、换一个子会话、或者自己接手。

还有一个变体是把父的对话历史灌进子会话。DSH 的 `subagent_fork` 就是这么做的：子会话能看到父已经完成的轮次，看不到正在进行的那一轮。好处是子会话知道前面聊过什么，代价是这段历史要重新付一遍 token，而且父之前的误判会被子会话继承。

## 3. 同一件事，两种写法

拿一个具体任务：在二十个文件里找出所有调用旧接口的地方，改掉，跑测试。

用工具调用，父模型得自己走完：`grep` 找位置，逐个 `read` 确认上下文，逐个 `edit`，再 `bash` 跑测试。每一步的结果都进上下文，二十个文件读完，上下文可能已经涨了几万 token，而且这些内容后面再也用不上。

用委派，父模型写一段 brief 就结束这一轮：

```python
handle = delegate(
    brief="在 src/ 下找所有调用 legacyClient.send() 的地方，"
          "改成 client.send()，跑 pytest tests/unit，"
          "返回 JSON：{changed:[...], failed:[...], note}",
    tools=["read", "grep", "edit", "bash"],
    budget=80_000,       # 单位 token，含子会话自己的工具返回
)
# 父上下文只多了上面这一条调用；回执稍后到
```

两次调用在日志里都是 tool call。但 handler 干的事完全不同：

```python
# 工具：在本进程里执行，结果进父上下文
def handle_tool_call(parent, call):
    result = registry[call.name](**call.args)     # 可能是 20 KB 输出
    parent.transcript.append(tool_result(call.id, result))
    return result                                  # 同步返回给循环

# 委派：建会话，立刻返回句柄
def handle_delegate(parent, call):
    child = Session.new(
        system=agent_type(call.args.type).prompt,
        seed=[],                                   # fork 则灌父的已完成轮次
        tools=call.args.tools,
        budget=call.args.budget,
    )
    parent.children[child.id] = child
    parent.transcript.append(call)                 # 记录调用本身（含 brief）
    return Handle(child.id)                        # 不返回结果
```

所以「子 Agent 是不是一种工具」这个问题，答案分两层。协议层是：它就是一个 tool call，schema 里写着 `prompt`、`type`、`tools`。控制流层不是：handler 返回的是句柄，真正的结果从另一条消息通道回来，父模型在这一轮结束时并没有拿到任何数据。父上下文里躺着的那条记录是这次调用，不是它的输出。

选哪一种，看两个问题。

- 结果需要逐字进入后续推理吗？需要就用工具，摘要一定会丢东西。
- 下一步必须由父逐步决定吗？必须就用工具，委派出去以后父只能等或者发消息。

两个都否定，才轮到子 Agent。

## 4. 中控是拓扑选择，不是多 Agent 的定义

{{< fig "diagrams/03-topologies.svg" "三种拓扑共用同一套子会话机制，区别在最终答复权留在谁手里：交给下游（handoff）、留在父（supervisor）、交给代码（workflow）。" >}}

第一种是对等交接。受理 Agent 判断这是退款问题，把整段对话连同工具和答复权交给退款 Agent，自己退出。OpenAI Agents SDK 的 handoff 就是这个语义：控制权转移，最终回答由接手的 Agent 给。这条路里没有汇总者，也没有中控。

第二种是中控。Lead 保留答复权，子 Agent 只提供有界能力。OpenAI 把这种叫 agents as tools，Anthropic 的 orchestrator-workers 是同一个形状。DSH 和 Claude Code 的子 Agent 都属于这一类：父发起、父汇总，子会话不会接管对话。

第三种是代码编排。谁先谁后、派几份、什么条件下重派，写在程序里。模型只在每个节点内部决策。DSH 的 `workflow` 工具是这种：脚本负责 fan-out 和阶段推进，脚本本身没有文件系统、没有网络，活都是子 Agent 干的。Anthropic 说的「预定义代码路径的工作流」也是这一类。

它的最终答复仍然由发起脚本的那一层给出，所以答复权没有转移；变的是派发决定权，从模型手里挪到了代码里。

还有第四种：共享任务板。没有派发者，任务写在板上，agent 自己认领。DSH 的 team 子系统就是在这个形状上加了一块持久任务板，每任务一个 revision，认领用 CAS 保证只有一个赢家。它解决的是「谁认领了哪块活」在重启后还能回答，但没有 lease、没有超时、不会自动回收死掉的认领者。

四种放在一起，判断有没有「中控协调区」只看两条：最终答复权在谁手里，派发由谁决定。handoff 把答复权交出去，没有中控；supervisor 和 workflow 都留在发起方，区别是派发由模型还是代码决定；共享任务板没有发起方，也就没有中控。架构图里画没画一个居中的框，不作数。

## 5. 中控怎么规划：三层，只有第三层驱动调度

{{< fig "diagrams/05-planning-layers.svg" "声明层与记录层都是进入上下文的文本；模型读完后输出本轮的 tool_call，这一层才真正改变系统。上下文里没有名为「计划」的字段。" >}}

这里是最容易想错的地方。很多人以为协调器里有一个 planner，输出一张任务 DAG，调度器按 DAG 派活。先约定一个词：本文说的「调度器」就是第 6 节那个循环本身，读上下文、问模型、执行动作，不是另一个独立组件。按这个定义，实际实现里通常没有 planner 模块，只有三层文本与动作。

**声明层**是系统提示词里的委派准则：什么情况下该派、派之前要问什么、有哪些子 Agent 类型。Claude Code 把每个 subagent 的 `description` 放进上下文，模型靠读这些描述决定派给谁；描述写得越模糊，派得越随机。这层是静态的，改它等于改模型的行为倾向。

**记录层**是 todo 清单、任务板、计划模式。它的作用是抗压缩、抗重启、给人看：上下文被压掉一半以后，清单还在；进程重启以后，板上还写着哪块活没做完。但它不触发任何调用。DSH 的 `todo_write` 只是往会话日志里追加一条事件，调度器不读它；team 的任务板有认领状态，可「谁去认领」仍然是模型在某一轮里的决定。

**决策层**是本轮模型输出的那个 tool call。派谁、派几份、要不要等、现在收尾吗，全部落在这一条动作上。这是唯一真正改变系统状态的输出。

所以「协调器按计划分配任务」这句话要拆开：计划记录进了上下文，模型读完重新决定下一步，派发是这次决定的结果。计划表换一种格式，只要模型还读得到，派发行为可能一点不变；反过来，同一张计划表在不同上下文下会派发出完全不同的任务。

这套做法的好处是能随观察改路线，坏处是可预测性差。DSH 的 workflow 走的是另一条路：把派发写进代码，模型退化成节点执行器，换来确定性和可重放。两条路的取舍在下一节展开。

## 6. 一轮调度循环长什么样

{{< fig "diagrams/04-coordinator-loop.svg" "协调器每轮重新组装上下文，模型在工具、委派、收尾三条出口里选一条；工具结果与子会话回执都回到组装步骤，守卫条件直接跳收尾。" >}}

把上面的东西收进一个循环，代码大致是这样：

```python
def run_coordinator(goal, budget, max_depth):
    ctx = [system(POLICY), user(goal)]          # POLICY 里是声明层的委派准则
    while True:
        if budget.exhausted() or depth() >= max_depth:
            return finish(ctx, reason="guard")
        if ctx.has_pending_settlements():
            ctx.extend(drain_settlements())     # 回执作为消息进入上下文
        action = model.next(ctx, tools=[call_tool, delegate, finish])

        if action.name == "finish":
            return action.text
        elif action.name == "delegate":
            handle = spawn_child(action.args)    # 立即返回，不阻塞
            ctx.append(event("spawned", handle.id, action.args.brief))
        else:
            ctx.append(action)                   # tool_call
            ctx.append(call_tool(action))        # tool_result 原文
```

三个细节决定这套东西跑不跑得稳。

1. **spawn 不阻塞。** 父派完一份可以立刻派下一份，或者自己先做点别的。阻塞式委派会把并行退化成串行，也会让父在等待期间无法响应新消息。
2. **回执必须排队唤醒。** 子会话结算时父可能正在跑一轮，也可能空闲。前者要等这一轮结束再注入，后者要立刻唤醒一个新轮次。塞错队列的表现是「模型看到了结果但没反应」，排查时先看消息落在哪个队列。
3. **守卫在循环外。** 预算、深度、用户取消都不该由模型判断。它们检查在每轮开头，触发就跳收尾，不经过决策。这时的收尾也不该问模型：循环把已经拿到的结论拼成答复，标上 `reason=guard` 交出去。

子会话那一侧也有一个循环，只是更简单：

```python
def child_loop(brief, tools, budget):
    ctx = [system(brief.role), user(brief.task)]
    while not done(ctx):
        action = model.next(ctx, tools=tools)
        ctx.append(action)
        if action.name == "finish":
            return Settlement(status="ok", text=action.text, usage=budget.used())
        ctx.append(call_tool(action))            # 结果只留在子上下文
    return Settlement(status="failed", text="budget", usage=budget.used())
```

## 7. 派发粒度：串行、并行、流水线、竞争

{{< fig "diagrams/02-fanout-sequence.svg" "一次并行委派：spawn 三次立即返回，三个子会话各自跑循环，两个结论和一个失败先后到达，协调器据回执重新决策。" >}}

同样是派活，粒度不同，成本和延迟差一个量级。

**串行**是后一份 brief 依赖前一份的结论。延迟叠加，但上下文最省，因为父每次只需要拿着一份回执做决定。

**并行 fan-out** 是一次 spawn N 份。前提是切片独立、写集不重叠、结果可合并。三个条件缺一个，就会出现重复劳动、互相覆盖，或者汇总时发现口径不一致。

**流水线**是每个切片独立走完多个阶段，阶段之间不设全局屏障。DSH 的 `workflow` 里 `pipeline` 和 `parallel` 的区别就在这：`pipeline` 让先完成的切片直接进下一阶段，`parallel` 要等所有切片都完成才继续。前者的吞吐更高，后者适合「必须先看全量再决定下一步」的收尾阶段。

```js
// DSH workflow：派发写在脚本里，脚本没有 IO，agent 干活
const files = await discover();                       // 代码决定范围
const out = await pipeline(
  files,
  (_, f) => agent(`审计 ${f}，返回 {file, issues[]}`, { schema }),
  (prev, f) => prev.issues.length
    ? agent(`修复 ${f}，只改这一个文件`, { schema })
    : prev
);
```

**竞争**是同一份 brief 派 N 份，再让裁判或测试选一个。质量可能更好，成本正好 N 倍，而且要求任务能被自动评分，否则裁判本身成了瓶颈。

## 8. 失败与成本

1. **摘要丢约束。** 子会话把五万 token 的探索压成两百字，父拿它做后续决策，关键限制可能在压缩时掉了。对策是 brief 里写死返回 schema，让子会话填字段，别让它自由总结。
2. **并行写冲突。** 两个子会话在同一个工作树上改同一个文件，后写的覆盖先写的。对策是按文件所有权切片，或者一个子会话一个 worktree。worktree 方案还欠一步合并：每个子会话产出一个分支或 patch，由父（或一个人）按顺序合回主树，冲突退回派活的那一层决定，不要指望子会话之间自己谈。
3. **成本被低估。** 每开一个子会话，系统提示、工具 schema、brief 都要重新付一遍；并行还会抬高瞬时消耗。子会话总数和单会话预算都要设上限。
4. **深度失控。** 子会话也能派子会话，一层套一层，最后没人知道总共开了多少。深度限制要落在 spawn 的入口上，别指望模型自己克制。
5. **可观测性断裂。** 父上下文里只有回执，过程在子会话的 transcript 里。出问题时得能按 id 找到子会话、读它的日志、必要时给它发消息。DSH 的 `list_agents` 加 `send_message` 就是为这个准备的。

什么时候干脆别开子 Agent，判断只有三条：

- 任务短到十几次工具调用能做完，委派的固定开销比干活还大。
- 结果需要逐字引用，摘要一定会失真。
- 每一步都要人看着、随时可能要改方向，异步回执只会让反馈变慢。

## 9. 一张选型表

| 任务特征 | 用什么 | 原因 |
| --- | --- | --- |
| 单步取数、改一处、需要看原文 | 工具调用 | 结果原样进入上下文，父逐步可控 |
| 独立子任务，结果能压成结论 | 子 Agent | 上下文隔离，父不被过程污染 |
| 路径固定、要重放、要能测 | workflow | 派发写在代码里，失败位置固定 |
| 跨轮持续协作，要保留子会话状态 | 常驻子会话 + 消息 | 一次委派不够，需要多轮追加 |

延伸阅读：子会话结算消息怎么投递，见[《Agent 之间怎么说话》](/posts/dsh-agent-messaging.html)；任务板与认领，见[《Agent Teams：共享任务板与委派策略》](/posts/dsh-agent-team.html)；工作流与自主循环的边界，见[《Anthropic 的 Agent 设计指南》](/posts/anthropic-effective-agents.html)。
