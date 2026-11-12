---
title: "工具执行流水线：九段与三闸"
date: "2026-11-11T14:00:00+08:00"
summary: "一次工具调用要穿过九个阶段；三个 waterfall 加一道只能否决的 guards，决定了谁可以改写、谁只能旁观。顺序不只是实现细节，它本身就是对外契约。"
description: "逐段拆解 DSH 的工具执行流水线：pre-execute、guards、execute、body、project、post、normalize、finalize、notify，以及并发分批与 PTC 子调用如何复用同一条链。"
tags: ["harness", "architecture", "concurrency"]
slug: "dsh-tool-pipeline"
---

> 源码版本：`deepseek-harness` v0.2.2-rc.1（commit `fcc0ad46`）。本文逐句对照 `docs/tool-execution-pipeline.zh.md` 与 `packages/core/tools/src/pipeline.ts`（771 行）。
> 本系列第 10 篇。上一篇讲工具怎么被模型看见，这一篇讲被看见之后，一次调用到底经过了谁。

先看三个容易让人停一下的现象。第一个：你注册了一个 `tools/pre-execute` 监听器记审计日志，本意只是旁路观察，结果发现它跑在 guard 之前，而且只要你不调 `next()`，后面所有阶段都不会发生。第二个：一个被 guard 拒绝的 `bash` 调用，明明没有执行，结果里却出现了工具自己写的摘要，原因是 `projectContent` 在拒绝路径上照样运行。第三个：你在 `tools/post-execute` 里抛了个错，以为会看到原始结果，模型收到的却是 `Error: ...`，而且 finalize 仍然跑了。

三个现象都来自同一条顺序契约，只是切面不同。工具执行平面最容易被低估的地方：大家以为核心是 `execute()`，其实核心是 execute 前后各有谁能插手、插手的形状是什么。

本文按一次调用的时间顺序走完九个阶段，再把并发、PTC 两个旁支接回来。读完你应该能回答一个具体问题：我想加的这个行为，该挂在哪一段，以及它能不能否决。

## 0. 结论

DSH 把一次工具调用拆成九段。前四段管「做不做、准不准、怎么做」，中间两段管「结果长什么样」，后三段管「怎么定稿、通知谁」。

| # | 阶段 | 代码位置 | 输入 | 输出 | 谁能写 |
|---|---|---|---|---|---|
| 1 | `tools/pre-execute` waterfall | `pipeline.ts:196` | `exec` | allow / deny / cancel / ask | 任意插件 |
| 2 | guards | `pipeline.ts:222` | `exec` | 第一个拒绝理由或 undefined | 注册的 guard |
| 3 | `tools/execute` waterfall | `pipeline.ts:296` | `exec` | 任意 dispatch 结果 | 任意插件 |
| 4 | tool body | `pipeline.ts:279` | args、`exec` | 一个待校验的 value | 工具作者 |
| 5 | `projectContent` | `pipeline.ts:344` | `exec`、规范化结果 | `content[]` 或 undefined | 工具作者 |
| 6 | `tools/post-execute` waterfall | `pipeline.ts:487` | `exec`、结果 | accept / block | 任意插件 |
| 7 | normalize | `pipeline.ts:374` | 任意结果或异常 | 合法 `ToolExecutionResult` | 框架 |
| 8 | `finalizeContent` | `pipeline.ts:381` | `exec`、结果 | `content[]` 或 undefined | 工具作者 |
| 9 | `tools/result` | `pipeline.ts:400` | 冻结的 `exec`、结果 | 无返回值 | 任意观察者 |

「三闸」严格说指三个 waterfall：pre-execute、execute、post-execute。它们都能让调用停下或者改写结果。guards 是第四道检查，刻意没有做成 waterfall：它只能说「不」，返回值里没有 `allow`，因此后面的监听器无法把已经发生的拒绝翻回来。

把九段压成一句路线图：pre 决定做不做，guards 决定准不准，execute 决定怎么做，post 决定怎么看，finalize 决定最后长什么样，tools/result 只能看。

{{< fig "diagrams/01-nine-stages.svg" "图注：九个阶段沿时间轴排开，三条旁路分别标出 pre 拒绝、execute 抛错、post 阻断时哪些后续阶段被跳过；右侧是三个 waterfall 的返回形状。" >}}

三个 waterfall 的签名值得单独看一眼。参数个数不同，形状却是同构的：前几个参数是被处理的对象，最后一个是下一个处理器。

| 闸 | 事件 | 签名 | 默认行为 | 能做什么 | 不能做什么 |
|---|---|---|---|---|---|
| 第一闸 | `tools/pre-execute` | `(exec, next)` | 返回 `{kind:'allow'}` | 允许、拒绝、取消、发起审批 | 改参数、改工具定义 |
| 第二闸 | `tools/execute` | `(exec, next)` | 调用工具 body | 包裹、短路、追加取消信号 | 伪造未校验的成功值 |
| 第三闸 | `tools/post-execute` | `(exec, result, next)` | 返回 `{kind:'accept'}` | 换 content、换 value、阻断 | 改已经失败的 value、同时换两者 |

guards 不在这张表里，因为它没有 `next`，也没有「允许」这个返回值。它只有一个单向的否决权。

{{< fig "diagrams/02-three-gates.svg" "图注：三个 waterfall 各自的输入、输出与责任边界；guards 画在闸门之外，标出它只返回拒绝理由，因此不能参与覆盖链。" >}}

## 1. 九段不是九个函数，是三种形状

读 `pipeline.ts` 最容易迷路的地方，是它没有一个大 `run()` 顺序调用九个函数。它围绕一个每次调用新铸的 `execution` 对象推进，每一步只返回三种形状之一：

- `{ kind: 'dispatch', exec }`：还可以真的执行。
- `{ kind: 'post-result', result }`：已经有一个结果，但还要走 project 与 post。
- `{ kind: 'final-result', result }`：定稿，直接进 finish。

`prepare()` 产出前两种，`dispatch()` 产出后两种，`complete()` 只产出最终结果。这个设计把「哪些阶段该跑」编码进了返回值，没有靠布尔标志和 `if` 堆叠。你只要看某条路径返回的是 `post-result` 还是 `final-result`，就知道它会不会经过 `projectContent` 和 `tools/post-execute`。

这也解释了 deny 之后为什么还有 project：拒绝被建模成 `post-result`，没被当成异常。异常走另一条路（`final-result`），会跳过中间两段。

## 2. 第 0 段：批次先 commit，再分类

第一段其实在 `pipeline.ts` 外面。`executeToolBatch()` 拿到模型给出的整批 tool-call block，先做一次计划，再决定这次调用是独占还是并行。

```ts
while (next < planned.length) {
  const first = planned[next]!
  // Commit before classifying again so registry changes affect unstarted calls.
  const mode = pipeline.executionMode(first.exec).kind
  const group = mode === 'parallel' ? planned.slice(next) : [first]
  const outcome = await runGroup(group, mode, ...)
  next += outcome.consumed
}
```

看 `executionMode(first.exec)` 这一行：它在每次开始一组之前重算，而不是在批次开始时算一次。原因写在注释里，注册表可能在两次调用之间变化（一个插件被卸载、一个新 restriction 生效），尚未开始的调用必须按新现实分类。

分类本身是 fail-closed 的。`executionMode()` 只有拿到严格的 `true` 才返回 `parallel`；工具没声明、声明不是布尔、或者 `isConcurrencySafe(args)` 自己抛了异常，一律 `exclusive`。它宁可少并行，也不把两个可能写同一份状态的调用放进同一个池子。

`runGroup()` 里还有两个顺序约束。第一，`settlement.start(block)` 在 `pipeline.prepare()` 之前执行，也就是 `tool/call` 先落 session，再去问权限。第二，`commitReady()` 只提交「连续且已就绪」的槽位：第 3 个调用先完成，也要等第 1、2 个都完成后才能写 `tool/result`。模型看到的顺序因此永远是它自己给出的顺序。

> 自己搭 Agent 时：把「记录调用开始」放在任何可能 await 的检查之前，并且让它幂等。否则进程在审批中途崩掉，日志里既没有 call 也没有 result，恢复逻辑连「这里曾经有个调用」都不知道。DSH 的 `ToolCallRecovery` 能补齐，前提正是 `tool/call` 已经落账。

## 3. 第 1 段 pre-execute：唯一能"问"的闸

`tools/pre-execute` 是整条链上唯一可以发起人机交互的阶段。它的返回值是四个之一：`allow`、`deny`、`cancel`、`ask`。`ask` 会走 `ctx.approval.request()`，只有拿到 `allowed-once` 才继续；被拒、被取消、没有审批通道、没有 agent 可以路由，全部变成拒绝。

```ts
const gate = await ctx.waterfall(
  carrier, 'tools/pre-execute', exec,
  () => Promise.resolve({ kind: 'allow' }),
)
// ask 分支省略：ask → ctx.approval.request(...)
//   → allowed-once / rejected / cancelled / unavailable
const denialReason = gate.kind === 'allow' ? this.tools.checkGuards(exec) : gate.reason
```

看 `checkGuards` 这一行：它只在 `gate.kind === 'allow'` 的三元分支里被调用。这一行同时定义了两件事，guards 在审批之后，且 deny/cancel 的调用根本不会经过 guards。

参数在这一段是**不可改写**的。`createExecution()` 已经对 `exec.arguments` 做了 snapshot 和 `deepFreeze`，监听器拿到的 `exec` 也是同一个对象。原因不在防君子：模型看到的参数、UI 展示的参数、审计记录的参数、实际执行的参数必须是同一份；只要允许 pre 阶段改一个字段，四者就会分叉。要改变行为，只能拒绝、取消，或者去改 `exec.signal` 这种执行环境字段，而不要动调用内容。

waterfall 的 carrier 是 `scopeTarget(this.tools, exec.agent)`：注册在某个 agent scope 上的监听器只处理该 agent 及其祖先链上的调用；没有 scope 标签的监听器是全局的。这和第 9 篇讲的 scope 是同一套机制，只不过这次分发的是「关于某个 agent 的调用事件」。

pre 排在 guards 之前，原因在职责分工。pre 是策略与授权层：hooks、permission、sandbox 都注册在这里，它可以问人，也可以在执行前建立本次调用的策略状态（比如把升级后的模式记到 `exec` 上）。guards 是最后一道只读的不变量检查，必须看到已经被授权、确定要执行的调用，否则检查的是一个还可能被用户取消的幻影。

代价是显式的：一个 guard 最终会拒绝的调用，可能已经打扰过用户一次。DSH 接受这个代价，因为把 guards 提前意味着把「不变量」变成「预筛选」，而预筛选需要理解所有策略插件的语义，耦合会更糟。

## 4. 第 2 段 guards：只能说不

guards 不是 waterfall。`ToolGuard` 的签名只有 `(exec) => string | undefined`，没有 `next`，也没有 `allow`。

```ts
checkGuards(exec: ToolExecution): string | undefined {
  for (const layer of [this.guards.global, ...this.guards.chainLayers(exec.agent)]) {
    for (const guard of layer.guards.values()) {
      const reason = guard(exec)
      if (reason !== undefined) return reason
    }
  }
  return undefined
}
```

看 `if (reason !== undefined) return reason` 这一行：第一个非空理由立即终止，后面的 guard 都不再运行。全局 guard 先于 agent 链，agent 链内部按 nearest-first。

如果把 guards 做成 waterfall，会得到一个更「可组合」也更危险的系统：A 插件 deny，B 插件可以返回 allow 把它翻回来，最终结果取决于注册顺序。安全判断不适合这种「后者覆盖前者」的语义。guards 选择单调：一旦有人说不，就没有「但是」。

guards 抛异常也不会被吞掉。它从 `prepare()` 冒出去，被 `prepare` 的 catch 捕获，变成 `final-result` 的 error，跳过 project 和 post，但 finalize 仍会跑。

## 5. 第 3 段 execute：包裹，不是替换

`tools/execute` 是一个围绕 `dispatchToolBody()` 的 waterfall。默认行为就是调 body；wrapper 可以在调用 body 之前或之后做事，也可以选择不调 `next()` 直接返回一个结果。

```ts
const result = await ctx.waterfall(carrier, 'tools/execute', mutableExec, () => this.dispatchToolBody(mutableExec))
const normalized = this.normalizeDispatchResult(exec, result)
```

看 `normalizeDispatchResult` 这一行。wrapper 返回的结果不会因为「是插件写的」就被信任：如果它不是本流水线铸出来的 canonical 结果（用 `canonicalResults.get(result) === exec.token` 判断），成功值会被重新 snapshot、重新按 `output.schema` 校验、重新 `render`、重新取 `presentationMeta`；失败结果会被重建成规范形状。

所以 wrapper 不能靠伪造一个 `isError: false` 的结果绕过输出校验。它可以短路（比如超时策略直接返回错误），但短路出来的东西仍要过同一套规范化。

wrapper 能做什么、不能做什么，其实由 `ToolRunContext` 的形状决定。它拿到的是 `exec`，里面可写的主要是 `signal`；`arguments` 已冻结，`agent`、`toolSet`、`callId` 都是只读标识。超时类 wrapper 的正确写法是：起一个计时器，超时就 abort 注入的 controller，body 的融合 signal 会收到取消，结果变成 `ABORTED`。要在失败后重试，应该在上层重试整个调用，因为 body 可能已经产生副作用，而 execute waterfall 没有「回滚」这个概念。`tools/execute` 的文档只说 wrapper 可以改 `exec.signal`，这不是风格建议，是保持参数、日志、展示三者一致的最低条件。

body 内部还有一次重新解析：

```ts
const tool = this.resolveExecution(execution)
if (tool === undefined) throw new ToolNotFoundError(execution.name, ...)
```

已经解析过为什么还要再解析？中间隔着 `await`：审批可能等了几十秒，插件可能在这期间被卸载，restriction 可能刚刚生效。派发前再查一次，才能保证「批准的」和「执行的」是同一个注册。查不到就按未知工具报错，不能拿旧引用硬跑。

body 拿到的 `exec.signal` 是 caller signal 与 wrapper signal 融合后的产物。wrapper 只能通过自己的 signal 追加取消，不能撤销 caller 的取消；`fuseToolSignals` 在任一方 abort 时把融合 signal 一起 abort，并在 finally 里解绑监听器。

## 6. 第 4 段 body：唯一碰得到副作用的地方

工具 body 是整条链上唯一应该产生外部副作用的地方。文件变更、进程启动、网络请求都在这里。也正是在这里，工具会发出自己的 session 事件：`todo/write`、`fs/observed`、`hook/invoked`、`hook/result`、`tool/ptc-dispatch`。这些事件不算流水线的阶段，它们是 body 内部的账本。

成功返回之后，value 要过四道关：`snapshotToolValue` 要求它无损 JSON 化；`validateJsonSchemaValue` 按注册时声明的 `output.schema` 校验；`deepFreeze` 冻结；`tool.output.render(args, value)` 生成模型可见的 `content`，`presentationMeta` 生成 UI 可重放的 `meta`。任何一道失败都会变成 `ToolOutputError`，最终是 `isError: true`。

`timeoutMs` 不在这里实现。它是 `@deepseek-ai/dsh-tool-call-timeout-policy` 这个 `tools/execute` wrapper 的职责。工具声明 `timeoutMs` 等于承诺「我会把 `exec.signal` 转发给能停稳的实现」；框架不会替你 kill 一个不响应 signal 的 Promise。

`isConcurrencySafe` 也不在这里检查。它在批次分类时被读一次，进入并行池之后就不再复查。所以一个声明了并行的工具，必须自己保证不修改父级共享状态。这是一条契约，运行时验证不了。

body 里发出的 `todo/write`、`fs/observed`、`hook/invoked` 这些事件，和流水线的九个阶段不是一回事。它们是工具自己的账本，发生在这里是因为只有这里知道「刚刚做了什么」。这也解释了为什么 `fs/observed` 是即发即弃的普通 emit：工具不 await 它，也不捕获它的异常，监听方必须同步、只能做副作用。把它们塞进 post-execute 会让「观察」变成「可以否决」，语义就错了。

## 7. 第 5 段 projectContent：deny 也要走的那一段

`projectContent` 是工具作者提供的「值到模型可读文本」的投影。它在 `tools/post-execute` 之前运行，拿到的是已经规范化的结果。

```ts
const project = this.contentProjectors.get(exec)
this.contentProjectors.delete(exec)
const content = project?.(exec, result)
if (content !== undefined) markCanonical(exec, materializeFinalResult({ ...result, content }))
```

看 `project?.(exec, result)` 这一行：返回值是 `undefined` 就保留原 content，只有返回数组才替换。这让投影可以「不表态」。

它被设计成在 deny 路径上也运行，因为「拒绝」也需要一个模型可读的结果。prepare 的拒绝分支直接返回 `post-result`，于是 complete 会先跑 project，再跑 post。工具可以在这里把自己熟悉的错误对象渲染成更可诊断的文本，而不是让模型看到一句笼统的 `Error: ...`。

但有一条边界：如果调用在 `createExecution` 阶段就被拒绝了（工具不可见、参数无法无损序列化），拿不到可信的 definition，projector 会被跳过，走 `final-result`。这不是遗漏：没有 definition，就没有可调用的投影函数。

投影函数在参数 getter 执行之前就被捕获了。`createExecution` 先 `bind` 住 `finalizeContent` 和 `projectContent`，再读 `exec.arguments`。这样即使某个参数对象带 getter 并在读取时做了奇怪的事，也不会把投影函数换掉。这是很细的一层防御，但它保证「这一次调用用哪个投影」在调用开始时就已经确定。

project 抛异常会被 `complete` 的 catch 接住，转成 error 结果直接 `finish`；`tools/post-execute` 不会再跑，但 `finalizeContent` 会。这是很多人第一次写投影时踩的坑：以为抛错只是「不投影」，实际是把整个结果变成了错误。

> 自己搭 Agent 时：把「程序值」和「模型文本」从一开始就分开存。工具返回结构化 value，投影函数只负责把它变成文本，并且允许返回「无意见」。如果两者混在一个字段里，你迟早会在 post 里遇到「UI 想改展示、程序想读原值」的冲突，而冲突的解法只能是再拆一层。

## 8. 第 6 段 post-execute：策略改写与阻断

post 是三个 waterfall 里权力最大的一个。它可以接受结果、改写 content、替换 value，或者阻断。

| 决定 | 语义 | 约束 |
|---|---|---|
| `accept`（无字段） | 原样通过 | 默认 |
| `accept { content }` | 只换模型可见文本 | 不改变 value；不是保密手段 |
| `accept { value }` | 换掉程序值 | 会重新校验、重新 render、重新取 meta |
| `block { feedback }` | 变成 `isError`，移除 value | feedback 进入 error.message |
| 同时给 content 和 value | 抛 `TypeError` | 无法判断以谁为准 |
| 对失败结果给 value | 抛 `TypeError` | 失败没有可替换的值 |

```ts
if (decision.kind === 'block') markCanonical(exec, { ...result, content: decision.feedback, isError: true, ... })
if (decision.kind === 'accept' && decision.value !== undefined) {
  if (result.isError) throw new TypeError('tools/post-execute cannot replace the value of a failed result')
  return this.createSuccessResult(exec, tool, decision.value)
}
```

看这一行：替换 value 走的是 `createSuccessResult`，没有直接赋值。所以 post 也不能把未校验的值塞进结果；它会重新 snapshot、校验 schema、render、取 meta。`concludesTurn` 也只在成功结果上重新计算。

`additionalContexts` 在这里合并：`[...result.additionalContexts ?? [], ...decision.additionalContexts ?? []]`。这些上下文不会立刻进模型，而是先挂在结果上，等批次里所有 `tool/result` 都记录完，再由 loop 以 `user/message` 的形式补进去。顺序是 FIFO，先产生的结果先注入。

一个真实例子是 spill-policy：它用 `{ prepend: true }` 注册，先 `await next()` 让别的策略改完，再判断 token 预算，超了就落盘并把 content 换成本身保头保尾的版本。它刻意跳过 `exec.name === 'read'`，因为 read 有自己的分页和 locator，再套一层 spill 只会让模型拿到两个地址。

### 8.1 默认是 accept，不是 allow

注意 post 的默认返回值是 `{ kind: 'accept' }`，不是 `{ kind: 'allow' }`。这跟命名洁癖无关。`allow` 暗示「我审查并通过了」，post 的默认语义则是「我不表态，保留上游的决定」。一条没有监听器的 post 链不应该改变任何东西；一个只关心 `bash` 的监听器也不应该对 `read` 的结果负责。

这个区别在链式组合里很重要。如果默认是 `allow`，每个监听器都必须显式转发前一个决定，否则就会意外地把别人的改写丢掉。`accept` 的语义是「接受当前状态」，`next()` 返回什么就接受什么，组合起来更接近中间件的直觉。

### 8.2 阻断不是删除

`block` 会把结果变成 `isError: true`，把 value 移除，把 feedback 放进 `error.message`。它不会让这条 tool result 消失，模型仍然会收到一条结果，只是这条结果在说「这次调用被策略挡下了」。

这是刻意的。如果 block 直接删掉结果，模型看到的就是一个没有回应的 tool-call，下一轮要么重试、要么困惑。保留一条错误结果，既维持了 call/result 配对，也把「为什么被挡」传达给了模型。要彻底静默，只能在更早的阶段取消调用，而那会留下 `ABORTED_BEFORE_DISPATCH`，同样不是删除。

## 9. 第 7 段 normalize：异常到底在哪一步变成 isError

「异常被规范化」是这条流水线里最容易被含糊带过的说法。实际上它发生在多个点，而且不同点的后果不同。

| 抛出位置 | 被谁接住 | 结果形状 | 还走 post 吗 | 还走 finalize 吗 |
|---|---|---|---|---|
| 参数无法无损序列化 | `createExecution` | `final-result` error | 否 | 是 |
| pre-execute 监听器抛错 | `prepare` | `final-result` error | 否 | 是 |
| guard 抛错 | `prepare` | `final-result` error | 否 | 是 |
| 审批服务抛错 | `prepare` | `final-result` error | 否 | 是 |
| body 抛错 | `dispatchToolBody` | `post-result` error | 是 | 是 |
| 输出 schema / render 失败 | `createSuccessResult` | `post-result` error | 是 | 是 |
| execute wrapper 抛错 | `dispatch` | `final-result` error | 否 | 是 |
| projector 抛错 | `complete` | error → `finish` | 否 | 是 |
| post 监听器抛错 | `complete` | error → `finish` | 否（剩余监听器） | 是 |
| finalize 抛错 | `finish` | 第二次物化成 error | 不适用 | 不适用 |

这张表值得贴在显示器边上。它解释了为什么「我在 post 里加的日志」有时看不到 body 的错误：body 抛错确实会走 post，execute wrapper 抛错不会；两者在调用方看来都是「工具失败了」。

所有错误最终都收敛成同一个形状：`content` 是 `[{ type: 'text', text: 'Error: ...' }]`，`isError: true`，`error` 至少含 `message`，如果是 `HarnessError` 还带 `{ name, code }`。取消是两种特殊错误：执行前取消是 `ABORTED_BEFORE_DISPATCH`，执行中取消是 `ABORTED`，两者都保留之前收集的 `additionalContexts`。

`toolErrorMessage()` 本身是 total 的：`Error` 取 message，对象取 string message，其他 `String()`，连 `String()` 都抛就返回 `<unprintable thrown value>`。normalize 不能因为「错误对象长得怪」再抛一次。

> 自己搭 Agent 时：把「异常 → 结果」的转换收敛到一个函数，并且在每个 catch 点问自己两个问题：这个错误该不该让策略层看到？这个错误发生时，结果对象里还有哪些字段是可信的？DSH 的答案是用 `post-result` / `final-result` 两种形状显式表达，没有让每个 catch 自己决定。

## 10. 第 8 段 finalize：恰好一次的最后改写

`finalizeContent` 和 `projectContent` 长得很像，但语义完全不同。project 是「把值投影成文本」，finalize 是「在定稿前做最后一次边界处理」。

它有三个硬约束。第一，同步，不能返回 Promise。第二，对每个规范化结果**恰好一次**，包括那些绕过了 post 的失败路径。第三，必须 total：不能抛，返回 `undefined` 表示保留原 content。

`finish()` 会做两次物化：先 `materializeFinalResult(result)` 得到候选，再 `applyFinalContent`，再物化一次。第一次失败就把结果换成 error 再物化；第二次失败同样换成 error。两次物化之间，finalize 拿到的已经是一个合法、冻结、可 JSON 化的结果。

实际用法可以看两个例子。`tool-terminal` 在所有策略改完之后，把 content 重新截到 `maxResultBytes`；`tool-jobs` 在 pre-execute 里把本次调用的 `maxBytes` 存进 `WeakMap<ToolExecution, number>`，finalize 时按这个上限裁剪，连 `job_output` 的失败结果也处理。共同点是，边界必须施加在完整结果上，而且必须在所有改写之后。在 body 里截断会被 post 的替换绕过，在 post 里截断会被 finalize 之前的拼接撑破。

## 11. 第 9 段 tools/result：只读通知

最后一个阶段是 `tools/result`。它是 emit，不是 waterfall。

```ts
Object.freeze(exec)
const callbacks = ctx.events.dispatch('emit', [scopeTarget(this.tools, exec.agent), 'tools/result', exec, result])
for (const callback of callbacks) { /* 同步 throw 与 async reject 都只 warn */ }
```

看 `Object.freeze(exec)` 这一行，以及后面被丢弃的返回值。观察者拿到的 `result` 也是冻结的；它可以读、可以记日志、可以驱动 UI，但不能改。改了也不会影响已经写进 session 的那一份。

不做成 waterfall，是因为到这里结果已经定稿：物化完成、finalize 跑完、即将作为 `tool/result` 落账。如果再允许一个监听器改写，就会出现两个权威版本（session 里的和内存里的），重放时对不上。而且 `tools/result` 允许异步观察者，如果返回值能改结果，流水线就必须 await 所有观察者，一个慢日志后端会拖住整个批次。

观察者失败被隔离：同步 throw 和 async reject 都只记 `ctx.logger.warn`，不改变结果，也不中断批次。

还有一个时序细节：`notifyResult()` 在 `complete()` 内部执行，`tool/result` 的 session append 却在批次 `commitReady()` 里执行。也就是说，观察者收到通知时，结果还没有落账。这不是疏忽：批次要保证 model-order 提交，通知要保证「结果一确定就发出」。副作用是，任何依赖「收到 tools/result 就一定能在日志里查到」的观察者都会偶发失败。正确的假设是通知早于持久化，需要持久化保证的消费者应该监听 `session/event`，而不是 `tools/result`。

## 12. 五种"没跑"，模型看到的不一样

「工具没执行」在日志里其实有五种完全不同的形态。把它们混为一谈，是排障时最常见的误判。

| 形态 | 触发点 | 结果形状 | 模型能区分吗 |
|---|---|---|---|
| 工具不存在 | `createExecution` 找不到定义 | `UNKNOWN_TOOL`，`final-result` | 不能，和"被过滤掉"一样 |
| 被策略拒绝 | pre-execute 返回 deny | `isError`，文本是拒绝理由 | 能，理由可见 |
| 用户取消 | pre-execute 返回 cancel | `ABORTED_BEFORE_DISPATCH` | 能，code 不同 |
| 执行前中止 | batch signal 已 abort | `ABORTED_BEFORE_DISPATCH`，synthetic | 能，但没跑过 body |
| 执行中中止 | body 期间 signal abort | `ABORTED`，保留已有 contexts | 能，code 不同 |

前两种是权限问题，后三种是生命周期问题。它们都会让模型看到一条 `isError: true` 的 tool result，但 `error.info.code` 不同。如果你的 agent 在失败后自动重试，必须按 code 分支：`ABORTED_BEFORE_DISPATCH` 可以安全重试（body 没跑），`ABORTED` 不能（body 可能已经产生副作用），`UNKNOWN_TOOL` 重试多少次都一样。

`ToolNotFoundError` 里还有一个 `reachableFrom` 字段，用来表达「这个名字在别的 scope 可见，但当前展示层不允许直呼」。它让错误文本可以给出「你可能想用 X」的提示，同时不泄漏不该出现的工具名。

## 13. 并发：barrier 与 rolling pool

并发不是「把 batch 全丢进 Promise.all」。`runGroup()` 实现的是一个有界滚动池，独占调用是池子的屏障。

- 遇到 exclusive：这一组只有它自己，等它完全结束后，外层再重新分类。
- 遇到 parallel：从当前位置一直取到批次末尾，池子上限是 `maxParallelToolCalls`（原生 executor 默认 10）。
- 池子填充时，如果下一个调用的 `executionMode` 不再是 parallel，立即停止填充，等当前池排空，再由外层按 exclusive 重新开始。
- `commitReady()` 只提交连续槽位；完成顺序可以乱，写入顺序不能乱。

{{< fig "diagrams/03-concurrency.svg" "图注：一个批次里 exclusive 调用形成屏障，parallel 段用有界滚动池并发；右侧标出后一个调用被重分类为 exclusive 时，池子停止补充并等待排空的路径。" >}}

中止路径也考虑了重放合法性。signal abort 后，还没开始的调用不会被静默丢弃，`settlement.skip()` 会给每个调用补一条 `tool/call` + 一条 synthetic 的 `ABORTED_BEFORE_DISPATCH` 结果。模型下一轮看到的是「这些调用被取消了」，而不是「这些调用凭空消失」。

这里有一个容易写错的并发细节：`maxParallelToolCalls` 不是一个常量：有输入就覆盖，没输入读 executor 的 `() => number`。但它在每组开始时读一次（`batch.ts:83`），不是每次填充池子时重读。上限变化从下一组生效，当前已经组好的并行段不受影响。它是这一组的在飞上限，不是整批次的全局上限；遇到独占调用切组时，下一组会重新读值。

另一个细节是 `commitReady()` 不能并发。它必须由单线程的调度循环串行调用，否则两个相邻槽位可能同时判断「我前面的都好了」，然后一起写 `tool/result`，顺序就乱了。`runGroup()` 的主循环用 `await Promise.race(inFlight.values())` 每次只处理一个完成事件，正是为了避免这种竞争。

调度器自身失败（比如 `pipeline.dispatch` 抛了非预期异常）会先 `Promise.allSettled` 排空已启动的调用，再抛 `ToolBatchExecutionError`，并把已提交的结果放在 `partial` 里。它不会留下悬空的 in-flight Promise 去写一个已经失败的批次。

## 14. PTC：同一条流水线，不同的入口

PTC（programmatic tool calling）让模型在 `run_code` 里发起子调用。子调用不走一条简化路径，它进的是同一条流水线，只是入口参数不同。

{{< fig "diagrams/04-ptc.svg" "图注：普通调用与 PTC 子调用从不同入口进入同一组阶段；子调用多带 parent token 与 tool/ptc-dispatch 记账，并在结果注入上省略 additionalContexts。" >}}

差异只有四点。第一，子调用带 parent token，`rootCallId` 指向根调用，日志里能还原调用树。第二，每次派发记一条 `tool/ptc-dispatch`，durable log 里的 content 可以被 `tools/ptc-dispatch-log` 单独改写，但程序已经拿到的完整 value 不受影响。第三，子调用被拒绝时，呈现给程序的是一个有约束力的驳回，而不是「再试一次」的建议。第四，子调用省略 `additionalContexts`：它不能让一段上下文插到自己的 call 和 result 之间，否则父批次里 call/result 就不再相邻。

还有一条容易忽略的：纯文本子调用的结果溢出走的是异步 log-only spill，不阻塞程序；带图的子调用如果图片被全部裁掉，会补一条 `ptc-mode` 的 user message，让模型至少知道「这里原本有图」。

## 15. 逐帧回放：一次 `bash` 调用

把上面的规则压成一条时间线，看看一次普通调用实际发生了什么。假设模型一次给出三个 tool-call：`read`、`bash`、`grep`，其中 `bash` 是 exclusive。

1. `executeToolBatch()` 计划三个 execution，`callId` 取自 block id，arguments 先 `JSON.parse`；解析失败就原样保留字符串，留给工具自己报参数错。
2. 外层看第一个 `read` 是 parallel，于是把三个都放进候选组，`maxParallelToolCalls` 为 10。
3. `startCall(0)` 先 append `tool/call`，再 `prepare()`；此时 `bash` 还没开始，日志里已经有 `read` 的调用记录。
4. `read` 的 pre-execute 返回 allow，guards 全过，execute waterfall 调 body，读到文件，`createSuccessResult` 校验并 render。
5. `read` 进 `complete`：projectContent 换成带行号的窗口，post-execute 里 spill-policy 判断没超预算，finalize 按字节上限再裁一次。
6. 第 0 个槽位就绪，`commitReady()` 立即写 `tool/result`，`sourceEventSeqs` 指向第 3 步的 `tool/call`。
7. `bash` 开始，`executionMode` 返回 exclusive，`fillPool()` 在它之前就停止补充后面的 `grep`；池子里只剩 `bash`。
8. `bash` 的 pre-execute 发现策略是 `workspace-write`，返回 allow；guards 通过；execute wrapper 把 timeout signal 融进去。
9. body 里 `tool-bash` 先 `resolveSandboxPolicy(exec)`，再 `validateBashArgs`，需要升级就先走审批，最后 `ctx.shell.execute()` 拿到被 confine 的进程。
10. 进程退出码非零，但工具选择「返回结果而不是抛错」：`isError: false`，content 里带 exit marker。
11. post-execute 里另一个策略把长输出换成「尾部 + spillPath」，content 被改写，value 仍是原始 `{stdout, stderr, exitCode}`。
12. finalize 看到 content 已含 spill 提示，不再重复截断；物化后 `tool/result` 落账。
13. `grep` 这时才被重新分类、开始执行；它和 `bash` 之间隔着屏障，不会重叠。
14. 三个结果都记录完后，loop 把批次收集到的 `additionalContexts` 以一条 `user/message` 注入，排在所有 `tool/result` 之后。

这条时间线里，模型只看到第 6、12、13 步产生的三条 result，以及最后一条 user message。它看不到 pre/guards 的分歧，看不到 spill 的落盘，看不到屏障的存在。流水线做的就是把这些复杂性留在平面内，交给模型一个稳定的顺序。

## 设计取舍

1. 三种返回形状，而不是异常控制流。`dispatch`/`post-result`/`final-result` 让「跑不跑中间两段」变成类型问题。代价是每个阶段都要维护三个分支，读代码时要一直记着当前形状；收益是不会出现「某条路径忘了跑 post」的隐性不一致。

2. 审批先于 guards。授权层在不变量层之前，guards 看到的是已经确定要执行的调用。代价是可能为一个最终被 guard 拒绝的调用打扰用户；收益是 guards 不必理解审批语义，也不会把「用户还没决定」误判成「可以执行」。

3. guards 不做 waterfall。牺牲了插件间的可组合覆盖，换来单调性：拒绝一旦发生就不能被后续监听器翻案。对安全判断来说，这个交换值得；对「我想临时放行」的需求，正确做法是改 restriction 或改 guard，而不是加一个后置 allow。

4. deny 仍然走 project 与 post。拒绝没被当成异常，它就是结果。这样拒绝文本、UI 卡片、审计字段都走同一条呈现路径。代价是「拒绝即终止」的直觉不成立，投影函数必须能处理「工具没跑」的情况。

5. `tools/result` 只读。牺牲了「用观察者做最后修补」的灵活性，换来内存结果与 durable event 的一致。要改结果，必须在 finalize 之前改；过了那条线，就只能记录。

6. 并发按模型顺序提交。完成顺序可以乱，写入顺序不能乱。代价是队头慢调用会拖住后面已完成调用的落账；收益是 `tool/call` 与 `tool/result` 的配对永远简单，重放不需要额外的排序信息。

## 踩坑点

1. 以为 guard 会在审批之前跑。现象：用户先被弹窗，点了允许之后才看到 guard 的拒绝。原因：`checkGuards` 在 `gate.kind === 'allow'` 的分支里。对策：把「绝不允许」的判断放进 pre-execute 的早期监听器，或者接受这次白问。

2. 在 pre-execute 里改参数。现象：改了 `exec.arguments` 的字段，工具收到的还是旧值，甚至直接抛「not losslessly JSON-serializable」。原因：参数在 `createExecution` 已经 snapshot + deepFreeze。对策：参数不可改写，只能拒绝或取消；要改写就在模型侧改 tool-call。

3. 以为 post 换 content 能保密。现象：模型看不到原始值了，但程序侧、日志侧、`accept { value }` 之后的 render 仍能拿到。原因：content 只是呈现层，value 才是程序值。对策：要彻底藏住，用 `block` 或替换 value。

4. post 同时返回 content 和 value。现象：`TypeError: tools/post-execute accept decision cannot replace both value and content`。原因：无法判断以谁为准。对策：二选一；通常换 value 让 render 重新生成 content。

5. 给失败结果换 value。现象：`TypeError: tools/post-execute cannot replace the value of a failed result`。原因：失败结果没有可信 value。对策：用 `block` 改 feedback，或接受失败。

6. 在 projectContent 里抛错。现象：原本想「跳过投影」，结果整个调用变成 `isError`，post 也没跑。原因：projector 在 `complete` 里没有单独 catch，异常走 `finish(error)`。对策：返回 `undefined` 表示不投影，不要抛。

7. 在 finalize 里做可能失败的事。现象：结果被替换成 `Error: ...`，原始内容丢失。原因：finalize 抛错会被 `finish` 转成 error 再物化。对策：finalize 内部自己 try/catch，失败时返回原 content 或 undefined。

8. 观察 `tools/result` 并试图改写。现象：改了对象没生效，或者 freeze 下直接抛。原因：emit 的返回值被丢弃，`exec` 与 `result` 都已冻结。对策：把改写逻辑放到 post 或 finalize；观察者只做记录。

## 源码索引

| 位置 | 证明什么 |
|---|---|
| `packages/core/tools/src/pipeline.ts:105-114` | `executionMode` 只有严格 `true` 才并行，异常也归为 exclusive |
| `packages/core/tools/src/pipeline.ts:147-151` | 先捕获 project/finalize 回调，再读参数 getter |
| `packages/core/tools/src/pipeline.ts:190-233` | `prepare` 的 allow → guards 顺序，以及 deny 走 post-result |
| `packages/core/tools/src/pipeline.ts:258-286` | body 前的二次 `resolveExecution` 与 signal 融合 |
| `packages/core/tools/src/pipeline.ts:295-325` | execute waterfall 包裹 dispatch，以及 wrapper 结果的再规范化 |
| `packages/core/tools/src/pipeline.ts:336-364` | `complete` 的 project → post → finish 顺序 |
| `packages/core/tools/src/pipeline.ts:374-389` | 两次物化、finalize 恰好一次、失败转 error |
| `packages/core/tools/src/pipeline.ts:400-419` | `tools/result` 冻结 + 观察者失败隔离 |
| `packages/core/tools/src/pipeline.ts:432-473` | ask 的五种审批结果如何变成 allow/deny |
| `packages/core/tools/src/pipeline.ts:486-525` | post 的 accept/block 约束与 value 重渲染 |
| `packages/core/tools/src/pipeline.ts:570-588` | 非 canonical 结果必须重新校验 |
| `packages/core/tools/src/batch.ts:74-96` | commit-before-classify 与按组消费 |
| `packages/core/tools/src/batch.ts:154-201` | `tool/call` 先落账、滚动池、重分类屏障 |
| `packages/core/tools/src/batch.ts:237-284` | `tool/result` 的 `sourceEventSeqs` 与 abort 补齐 |
| `packages/core/tools/src/pipeline.ts:591-606` | 物化失败会转成 isError，finalize 之前结果一定合法 |
| `packages/core/tools/src/pipeline.ts:648-662` | `toolErrorMessage` 是 total 的，错误对象再怪也不会二次抛 |
| `packages/core/tools/src/batch.ts:99-106` | 参数解析失败时保留原始字符串，不提前判死 |
| `packages/core/scope/src/index.ts:170-185` | waterfall carrier 的向上过滤，决定 scoped 监听器能收到哪些调用 |
| `packages/core/tool-executor-native/src/index.ts` | 原生 executor 只做 describe + 调 batch，默认并行 10 |
| `packages/experimental/tool-terminal/src/index.ts:155` | finalize 在最后重新上界，证明它不是 project 的别名 |
| `packages/jobs/tool-jobs/src/index.ts:291` | finalize 对失败结果也运行，边界按完整结果施加 |
| `docs/tool-execution-pipeline.zh.md` | 九段链路的官方顺序图与旁路说明 |

## 最后

这条流水线最反直觉的地方，是它把「执行」降级成了九段中的一段。系统行为主要由两件事决定：每一段能返回什么形状，形状又如何决定下一段是否运行。

落到实践上，在 DSH 里问「这个逻辑该放哪」之前，先问它需要否决、改写，还是只需要看。否决去 pre 或 guards，改写去 execute 或 post，定稿去 finalize，只能看去 tools/result。位置选错，行为看起来也能工作，但会在某条旁路上悄悄失效。

还有一条更隐蔽的判断标准：看这段逻辑失败时，应不应该影响工具调用的成败。会影响的，放在能产出 `post-result`/`final-result` 的地方；不该影响的，放在 `tools/result` 或工具自己的事件里。DSH 把审计、指标、UI 回显都放在最后一段，正是因为它们失败不该让一次成功的 `read` 变成错误。

下一篇讲边界：模型 schema、注册表过滤、审批、OS 沙箱这四层各自能挡住什么，以及为什么「工具自己检查路径」永远不够。

读源码时，建议从 `prepare()` 与 `dispatch()` 的返回值入手，而不是从头读 `execute()`。这条链的骨架是返回值，不是调用栈。

上一篇：[09 工具注册表：全局、作用域与遮蔽](/posts/dsh-tool-registry-scope.html)｜下一篇：[11 沙箱、权限与审批：工具的边界](/posts/dsh-tool-boundaries.html)
