---
title: "护栏：超时、重复提醒与钩子"
date: "2026-11-14T19:15:00+08:00"
summary: "guard 只能说不，hook 能在调用前后观察与改写；两类机制如何分工，误报与漏报各自长什么样。"
description: "拆 timeout-policy、repeat-tool-reminder、permission presets 与 hook/invoked、hook/result 事件：写一个护栏需要守哪些约束。"
tags: ["harness", "architecture", "agent"]
slug: "dsh-guards-and-hooks"
---

> 依据：`deepseek-harness` v0.2.2-rc.1（commit `fcc0ad46`）。证据等级 A：逐符号核对 `packages/guard/*`、`packages/experimental/hooks-claude-code`、`packages/experimental/hook-protocol`、`packages/interaction/permission-presets`、`packages/interaction/user-approval` 与 `docs/subsystems/approval.zh.md`、`docs/subsystems/permission-presets.zh.md`。文末列出未核实点。

先看三个现象。

第一个：一个 `bash` 调用卡住，超时之后模型没有立刻收到结果。它要等到下游工具自己放弃，才看到一条 `TOOL_TIMEOUT`。第二个：模型第三次跑同一条 `pytest -q`，上下文里多出一条提醒，此后每次请求都带着它。第三个：团队挂了一个 `PreToolUse` 钩子拦截 `rm -rf`，某天钩子脚本崩了，`rm -rf` 照常执行。

三个现象分别对应 guard 与 hook 的三种性质：guard 是同步的单调否决，超时是协作式的信号约定，钩子是进程外的可选策略。它们都能让一次调用「停下来」，但停法的代价完全不同。

本文按「形状、位置、两个内置 guard、三层的分工、钩子契约、自己写」走一遍，最后给三条禁令。

## 0. 结论

| 机制 | 挂点 | 返回值语义 | 能放行吗 | 失败时模型看到什么 |
| --- | --- | --- | --- | --- |
| guard | 审批之后、body 之前 | `string` 拒绝，`undefined` 不表态 | 不能 | `Error: <理由>`，`isError: true` |
| `timeout-policy` | `tools/execute` 包裹 | 结算后替换工具结果 | 不涉及 | `Error: tool call timed out after <N>ms` |
| `repeat-tool-reminder` | `tools/post-execute` 观察 | 往决策里塞 `additionalContexts` | 不涉及 | 一条 `user/message`（`form: notice`） |
| permission preset | 配置层，两个 knob | 只写 knob，不做强制 | 通过 knob 生效 | 无 |
| hook | 6 个扩展点，外部进程 | deny / ask / block / context / steer | 只能加严 | 拒绝理由，或什么都没有 |

三句话概括：

1. guard 的价值在单调。它没有 `allow` 这个返回值，所以「谁先说不」是唯一需要排序的事，后面的监听器翻不了案。
2. 超时策略的价值在归属。预算写在工具定义上，包装层只负责把 deadline 接到 `exec.signal`，到期后替换结果，不硬杀进程。
3. 钩子的价值在进程外。它能力最宽，也最容易静默失败，因为失败默认只进日志。

{{< fig "diagrams/01-guard-position.svg" "图注：准入链上的四个位置。pre-execute 与审批决定「做不做」，guards 只决定「准不准」，且只在 allow 分支运行；右侧是两层注册顺序，先拒绝即返回。" >}}

## 1. guard 的签名：只能返回一个字符串

类型只有一行，注释比代码长：

```ts
// packages/core/tools/src/contracts.ts:384-391
export type ToolGuard = (execution: Readonly<ToolExecution>) => string | undefined
```

注释原文值得逐句读：

> A monotonic execution guard evaluated after every `tools/pre-execute` listener and before the tool body. Returning a reason denies the call; returning `undefined` leaves it unchanged. Because guards have no allow result, listener ordering cannot turn a denial back into permission.

拆成四条约束：

1. 同步。返回类型里没有 `Promise`，所以 guard 里做不了 I/O，也不该做重活。
2. 只有两种输出。字符串是拒绝理由，`undefined` 是不表态。没有「赞成」这一档。
3. 输入只读。`Readonly<ToolExecution>`，参数在更早的阶段已经 deepFreeze，guard 改不了。
4. 单调。任一 guard 返回理由，调用即被拒；所有 guard 都不表态，才继续。

第 2 条是关键设计。一个只能否决的接口，组合律是逻辑与：`deny ∨ deny = deny`，`undefined ∨ undefined = 放行`。顺序只影响「哪条理由先被看到」，不影响「是否拒绝」。如果 guard 也接受 `allow`，最后注册的监听器就能覆盖前面的拒绝，单调性随即消失。

注册与求值在同一个文件里：

```ts
// packages/core/tools/src/index.ts:381-400
guard(guard: ToolGuard): () => void {
  return this.guards.effect(this.ctx, layer => layer.guards.append(guard), { label: 'tools.guard()' })
}

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

「注册顺序只在全局与链内两层」指的是这张表：

| 层 | 谁往里写 | 作用范围 | 求值位置 |
| --- | --- | --- | --- |
| `global` | 根 context 里的插件 | 所有 agent、所有工具 | 最先 |
| scoped | 子 agent 的 context | 该 agent 及其后代 | 全局之后，由远到近 |

`chainLayers` 的顺序有明确注释：

```ts
// packages/core/scope/src/store.ts:192-200
chainLayers(scope: ScopeKey | undefined): L[] {
  const layers: L[] = []
  for (const key of scopeChainOf(scope).reverse()) {
    const layer = this.scoped.get(key)
    if (layer !== undefined) layers.push(layer)
  }
  return layers
}
```

`reverse()` 之后是「最远祖先在前，最近作用域在后」。全局层先跑，祖先层其次，当前 agent 最后。因为遇到第一个拒绝就 `return`，一个全局 guard 的拒绝发生在所有 scoped guard 之前，scoped 层无法翻案。反过来，scoped guard 的拒绝也不会被全局层「重新允许」，因为全局层早就跑完了。

这条顺序只在一个地方有实际影响：拒绝理由。两个 guard 同时想拒绝时，模型看到的是先跑的那个的理由。写护栏时值得把最可执行的理由放在全局层。

工具注册时，`guard` 的返回 disposer 绑定在调用方 context 的 effect 上，所以插件卸载时自动摘除，不需要手写清理。

## 2. 它站在哪：审批之后，执行之前

`prepare` 是准入段的入口，逻辑可以压成四行：

```ts
// packages/core/tools/src/pipeline.ts:196-236（摘录）
const decision = await this.ctx.waterfall('tools/pre-execute', carrier, exec)
// ask → 审批求解 → allow / deny / cancel
const denialReason = decision.kind === 'allow' ? this.tools.checkGuards(exec) : decision.reason
```

展开是四步：

1. `tools/pre-execute` 先跑。监听器可以返回 allow、deny、cancel 或 ask。
2. 返回 ask 时，走审批求解器，只有 `allowed-once` 折算成 allow。
3. 只有 allow 才调用 `checkGuards`。被 pre 拒绝或取消的调用，根本不进 guards。
4. 拒绝理由被物化成工具结果：`content` 是 `Error: <reason>` 的文本块，`isError: true`，`error.info` 带上来源。

这个位置带来三个后果。

第一，guard 看不到「已经被拒绝」的调用。想审计所有尝试的插件要挂在 post-execute 或 `tools/result`，不能挂在 guard 里。

第二，guard 的拒绝可能浪费一次已经发生的审批。用户点了允许，guard 才说不。`dsh-tool-pipeline` 的设计取舍第 2 条讨论过这个交换：授权层在不变量层之前，guards 不必理解审批语义，代价是偶尔白问一次。

第三，拒绝不是异常。它是一条合法的工具结果，会继续走 project、post、finalize。投影函数要能处理「工具没跑」的情况，否则拒绝路径上会抛二次错误。

还有一个容易忽略的细节：guard 每个放行的调用都跑一次。同步签名限制了它能做的事，也限制了它的成本。把「查数据库确认这个路径在不在白名单」写进 guard，会把每次工具调用的延迟抬高一个数量级。正确的位置是注册时查一次，或者放进 pre-execute 的异步监听器里。

自己搭 Agent 时，先把「拒绝」当一条正常结果来设计，接上投影与审计，再想具体规则。反过来做的人，通常会在第一次真实拒绝时发现 UI 把 `isError` 渲染成了崩溃。

## 3. timeout-policy：把 deadline 接到 `exec.signal` 上

整个插件只有一个监听器：

```ts
// packages/guard/timeout-policy/src/index.ts:56-80
ctx.on('tools/execute', async (exec, next): Promise<ToolExecutionResult> => {
  const timeoutMs = ctx.tools.get(exec.name, exec.agent)?.timeoutMs
  if (timeoutMs === undefined) return next()
  using d = deadline(exec.signal, timeoutMs, TOOL_TIMEOUT)
  const upstream = exec.signal
  exec.signal = d.signal
  try {
    const result = await next()
    if (timeoutOf(d.signal, TOOL_TIMEOUT) !== undefined) {
      return toolTimeoutResult(timeoutMs)
    }
    return result
  } finally {
    exec.signal = upstream
  }
})
```

七个决定，每个都有代价：

预算写在工具定义上。 从注册表读 `ctx.tools.get(exec.name, exec.agent)?.timeoutMs`，插件自身没有配置项。好处是拼错工具名不可能发生，未声明预算的工具完全不被包裹。代价是没有全局默认值，新工具忘记声明就没有上限。

分类码复用。 `TOOL_TIMEOUT` 同时是 deadline 的 code 和结构化错误的 code。`timeoutOf(d.signal, TOOL_TIMEOUT)` 把判断限定到这个码，嵌套的外层 deadline 先触发时读作普通的上游取消，不会被误报成工具超时。

交换信号只覆盖 dispatch。 `exec` 是每次调用新建的对象，包装层原地换 `signal`，`finally` 里换回调用方的。post-execute 监听器看到的永远不是本插件中止过的信号。

结算后才替换。 代码先 `await next()` 到静止，再看 `timeoutOf`。没有 `Promise.race`，没有放弃下游。这保证了结果与副作用一致，代价是忽略取消的工具会让调用方一直等。

`using` 管释放。 `deadline()` 返回一个 disposable，作用域结束即释放定时器。异常路径也走 `finally`。

只影响单次调用。 deadline 在每次 `tools/execute` 里新建，没有全局定时器，没有共享状态。PTC 的子调用同样经过 `tools/execute`，因此各自拿到一层。一个调用超时不会让下一次调用继承剩余预算。

结果是结构化错误。 消息是 `tool call timed out after ${timeoutMs}ms`，`isError: true`，`error.info` 里是 `{ name: 'ToolTimeoutError', code: TOOL_TIMEOUT }`。模型能按 code 分支，UI 也能按 code 渲染。

{{< fig "diagrams/02-timeout-sequence.svg" "图注：两条泳道。上边是工具遵守 signal 的路径：派生 deadline、交换 signal、下游中止、结算后替换成 TOOL_TIMEOUT、finally 还原。下边是忽略 signal 的路径：包装层仍停在 await 里，超时结果要等工具自己结束才出现。" >}}

哪些工具有预算，是一张具体清单：

| 工具 | 预算来源 | 默认值 |
| --- | --- | --- |
| `web_fetch` / `web_search` | `dsh-tool-web` 的 `fetchTimeoutMs` / `searchTimeoutMs` | 30 000 ms |
| `lsp` | `tool-lsp` 的 `timeoutMs` | 60 000 ms |
| `bash` / `read` / `write` / `edit` | 未声明 | 无上限 |

`bash` 没有超时是刻意的。一个 `make` 可能跑二十分钟，硬性上限会把正常构建切成失败。要限制它，用工具自己的参数或外层作业，不要指望这个插件。

自己搭 Agent 时，超时最容易踩的坑是把「超时」当成「终止」。这里的能力边界写在 README 里：插件只能请求工具停止，忽略取消的工具会继续运行，调用方继续等待。如果你的工具是同步阻塞的，先把它改成可取消，再谈预算。

## 4. repeat-tool-reminder：重复是一个键

检测的核心是一个字符串键：

```ts
// packages/guard/repeat-tool-reminder/src/index.ts:110-129（摘录）
function canonicalize(argumentsValue: unknown): string {
  return JSON.stringify(sortJsonValue(argumentsValue))
}
// key = JSON.stringify([exec.name, canonicalize(exec.arguments)])
```

参数先做深度键排序，再 stringify。`{"a":1,"b":2}` 与 `{"b":2,"a":1}` 是同一个键，属性顺序不会把循环切成两段。注释解释了为什么这样就够：参数来自 JSON 解析（畸形时回退原始字符串），值域里没有 bigint、循环引用或 `undefined`，所以「键排序 + stringify」是完整且确定性的同一性判定。

链按 agent 分开存：

```ts
// packages/guard/repeat-tool-reminder/src/index.ts:180
const chains = new WeakMap<Agent, Chain>()
// Chain = { key: string; count: number }
```

计数发生在 post-execute：

```ts
// packages/guard/repeat-tool-reminder/src/index.ts:220-232（摘录）
ctx.on('tools/post-execute', async (exec, _result, next): Promise<PostToolDecision> => {
  const reminder = observe(exec)
  const downstream = await next()
  // 把 reminder 折叠进 additionalContexts，accept 与 block 两个分支都带
})
```

四个判断规则：

1. 连续才算。同键的下一次调用 `count + 1`；换成一个受跟踪但键不同的调用，`count` 重置为 1。
2. 未跟踪调用透明。被 `include` / `exclude` 排除的工具既不计数也不重置，`grep → todo_write → grep` 在 `todo_write` 被排除时仍算连续两次。
3. 被拒绝的调用也计数。post-execute 是被拒绝调用也会经过的地方，模型反复撞同一个拒绝，正好是值得打断的循环。
4. 没有 agent 的调用跳过。直接调 `ctx.tools.execute()` 的调用方没有可提醒的模型。

阈值默认 `[3, 5, 8]`。第一次用短文案，只要求模型重新读上一次结果、换方法或结束。后续用长文案，带工具名、连续次数和参数预览，预览上限 500 字符，超出部分标注 `… (+N more chars)`。

提醒进入上下文的方式很讲究：

- 它不替换 `content`。`tool/result` 事件里保留工具自己的输出，审计与回放不受影响。
- 它进入 post-execute 决策的 `additionalContexts`，来源是 `{ kind: 'repeat-tool-reminder', form: 'notice', summary: '<tool> × <count>' }`。
- 循环把这段上下文缓冲起来，在该 step 的工具结果之后作为注入的 `user/message` 追加。模型看到一条普通的合成用户消息，带来源归属。
- 它能从会话日志重建，不需要额外的事件类型。

重置发生在 step 开始前：

```ts
// packages/guard/repeat-tool-reminder/src/index.ts:236-245（摘录）
ctx.on('agent/pre-step', ({ agent, messages }, next) => {
  if (messages.some(m => m.source.kind === 'user')) chains.delete(agent)
  return next()
})
```

只有真实用户消息会清零。提醒自己的 `source.kind` 是 `repeat-tool-reminder`，不会自我重置。从持久化恢复的会话从空链开始，这是启发式提醒可以接受的代价。

{{< fig "diagrams/03-repeat-chain.svg" "图注：同一工具、同一参数连续八次调用。第 3、5、8 次越过阈值，post-execute 把提醒折进 additionalContexts，step 结束后作为 user message 出现；真实用户插话把计数清零。" >}}

误报的代价要单独算：

- 每条提醒是一条持久消息，跟着后续每个请求直到压缩。三次阈值最多三条，文本量不大，但它占的是注意力。
- 合法轮询会被判重复。等构建、等端口、等 CI，参数往往一字不差。模型收到「你在重复」之后，可能放弃一个正确的重试。
- 只认完全相同的参数。改一个 flag 就不触发，所以它对「换着法儿试」无效。
- 它是建议，不阻断。阈值调高只能少提醒，不能消除误报。

减轻误报的正确位置在工具输出，不在阈值。一条带时间戳或状态的输出，会让模型自己看出「这次和上次不一样」，也让提醒显得多余。

自己搭 Agent 时，这类「只提醒不阻断」的护栏值得先于硬性 guard 上线。它不会把正常调用变成错误，最坏情况是多几条消息；硬性 guard 一旦误判，代价是一次失败的工具调用，模型还要花一轮去理解为什么。

## 5. 预设、审批、钩子：三层各自管什么

这三层经常被混着讲，实际职责分得很开。

预设是配置层的命名组合。一个预设捆两个独立的 knob：`sandbox`（沙箱模式）与 `approval`（审批策略）。默认两个：

```ts
// packages/interaction/permission-presets/src/index.ts:183-190
'workspace-write': {
  sandbox: 'workspace-write', approval: 'ask',
  name: 'workspace-write', description: 'Write inside the workspace and permitted temporary directories; wider retries require approval.',
},
'danger-full-access': {
  sandbox: 'danger-full-access', approval: 'never',
  name: 'danger-full-access', description: 'Full file access without approval prompts.',
},
```

`custom` 是派生值：当前两个 knob 不匹配任何预设时，`current()` 返回它，但它不能作为切换目标。`auto` 由集成在 effect 生命周期内注册，固定为 `danger-full-access + ask`，不进配置 schema，集成卸载即消失。

切换的实现只有两步：

```ts
// packages/interaction/permission-presets/src/index.ts:406-415（摘录）
if (current !== name) session.append('permission/preset', { preset: name })
updateKnobs()
```

`permission/preset` 是只记录意图的日志事件，不改变行为；实际改变是逐个 knob 调 `setSandboxMode` / `setApprovalPolicy`。重复选当前预设不追加事件。这一层不做任何强制，文档写得很直接：presets 拥有零强制力，额外的强制归 Auto review 所有。

审批层管「问不问、问到了算不算」。策略只有两个值：

| 策略 | 行为 | 模型看到的声明 |
| --- | --- | --- |
| `ask` | 走 waterfall 找 answerer；没有 answerer 则 fail closed | 审批已启用，必要时会问 |
| `never` | 不问，每个 ask 直接 `rejected` | 审批已禁用，需要审批的动作会被自动拒绝 |

`never` 的判断在 dispatch 之前：

```ts
// packages/interaction/user-approval/src/index.ts:296-301
// The 'never' policy is decided HERE, before any dispatch: a listener
// registered with `prepend: true` after this service mounts would sit
// ahead of any gate LISTENER, so a listener-shaped gate cannot keep the
// documented promise that 'never' rejects deterministically regardless
// of registration order — only the service's own request path can.
if (this.effectivePolicy(session) === 'never') return 'rejected'
```

四种结局里只有 `allowed-once` 是授权：`rejected`、`cancelled`、`unavailable` 都按拒绝执行。审计事件 `approval/asked` 与 `approval/decided` 只进日志，不进模型 transcript。

钩子与这两层的关系可以这样概括：钩子挂在 pre-execute 上可以 deny 或 ask，等于在审批之前加一道外部策略，但它只能加严。

- 钩子说 deny，调用不会进审批。
- 钩子说 ask，走同一个审批求解器；`never` 策略下不会被弹窗，直接按拒绝结算。
- 钩子说 allow，压不过 policy 的 `never`，也压不过同点位更严格的 deny。

三层合起来是一张单向的收紧链：预设决定默认宽严，审批决定要不要问人，钩子在同一条链上再加一道。没有任何一层能把已经发生的拒绝改回允许。

## 6. hooks：点位、记录与能改的东西

DSH 自己不做「钩子运行时」，它提供一个桥，读未修改的 Claude Code 钩子配置。包名 `hooks-claude-code`，依赖注入 `['shell', 'agents', 'workingDirectory']`，配置只有路径和几个上限。

六个点位，映射到 DSH 的事件：

| Claude Code 事件 | DSH 挂点 | 能做什么 | 是否等待 |
| --- | --- | --- | --- |
| `SessionStart` | `agent/created` | 注入上下文 | 是 |
| `UserPromptSubmit` | `agent/pre-step` | deny → `{kind:'reject'}`；上下文只给下游 `enter` | 是 |
| `PreToolUse` | `tools/pre-execute` | deny → `{kind:'deny', reason}`；ask → `{kind:'ask'}` | 是 |
| `PostToolUse` | `tools/post-execute` | deny → `{kind:'block', feedback}`，可附上下文 | 是 |
| `Stop` | `agent/turn-stopping` | deny → `agent.steer(...)`，强制再走一步 | 是 |
| `SubagentStart` / `SubagentStop` | 子 agent 生命周期 | 注入上下文 / 只观察 | 否，detached |

事件的记录由桥自己写，不在钩子脚本里：

```ts
// packages/experimental/hooks-claude-code/src/index.ts:163-187（摘录）
appendHookInvoked(session, { turn, point, dialect: 'claude-code', handlerId, matcher })
// ...运行钩子命令...
appendHookResult(session, { turn, point, handlerId, output, stderrSummaryMaxChars, durationMs })
```

`handlerId` 形如 `claude-code:PreToolUse:1`，把 invoked 与 result 配成一对。两个事件都是 log-only，定义在 `packages/experimental/hook-protocol/src/types.ts:19-41`，注释写明它们不是 surface 事件、不携带 `surfaceOp`，因此不进模型 transcript。只有拿到 `session` 且 `turn` 有定义时才写；`SubagentStart` / `SubagentStop` 这类 detached 点位不写这对记录。

负载很小：

| 事件 | 字段 |
| --- | --- |
| `hook/invoked` | `turn`、`point`、`dialect`、`matcher?`、`handlerId` |
| `hook/result` | `turn`、`point`、`handlerId`、`decision`、`exitCode?`、`stderrSummary?`、`durationMs` |

`decision` 是解析后的权限结论，`continue:false` 记成 `stop`，其余记成 `pass`。stderr 摘要默认上限 500 字符，超出加省略号。这些字段是排查「钩子到底说了什么」的唯一 durable 证据。

能改什么，清单比想象中窄：

- 能拒绝：`PreToolUse` 的 `permissionDecision: "deny"`，理由变成模型的错误文本。
- 能请求审批：`permissionDecision: "ask"`，但受 policy 约束。
- 能阻断结果：`PostToolUse` 的 deny 把工具结果换成 feedback 文本。
- 能注入上下文：`additionalContext` 变成模型可见的 user message，来源是 `{kind:'hooks-claude-code'}`。
- 能强制继续：`Stop` 钩子 deny 之后，桥调用 `agent.steer(...)` 再走一步。

不能改什么，同样明确：

| 想做的事 | 实际行为 |
| --- | --- |
| 用 `updatedInput` 改工具参数 | 解析出来，warn，忽略 |
| 用 `systemMessage` 给用户提示 | 解析出来，warn，忽略 |
| 用 `continue:false` 终止 turn | 记录成 `stop`，不执行终止（代码里有 TODO） |
| 用 `allow` 放行被 policy 拒绝的调用 | 无效，`never` 在更早的位置生效 |
| 订阅 Claude Code 的全部 30 个事件 | 只映射 6 个点位，其余解析后跳过 |

钩子配置的解析在装载时做一次，失败就只 warn，然后什么都不注册。这条行为值得强调：配置写错不会让 DSH 启动失败，也不会让模型看到任何提示，护栏静默消失。

运行期失败的处理同样是「记录并继续」：

- 钩子命令非零退出，桥记日志，agent 继续。例外是退出码 2：它被当成 block，stderr 变成理由。
- 钩子超时默认 600 000 ms，单个钩子可以用 `timeout`（秒）覆盖。
- stderr 只保留尾部 500 字符，写进 `hook/result` 的 `stderrSummary`。
- 同一事件有多个钩子时按配置顺序串行执行。串行保证 invoked/result 相邻，也让「最严结论」的折叠与顺序无关：deny > ask > allow。

{{< fig "diagrams/04-hook-points.svg" "图注：六个点位与它们对应的 DSH 事件。右侧标出 invoked/result 这对 log-only 记录的写入位置，以及钩子能改与不能改的边界。" >}}

`docs/subsystems/extensions.zh.md` 讲的是动态 Cordis 插件包（`ctx.cordisInspect`、`ctx.dynamicCordisRunner` 那一套），与本文的钩子不是同一层。钩子的契约在 `packages/experimental/hook-protocol`，桥在 `packages/experimental/hooks-claude-code`。把这两个混为一谈，会以为钩子能热加载，实际它只在装载时读一次配置。

## 7. 自己写一个 guard 与一个 hook

写 guard 是五步，代码量通常不到二十行。

第一步，选拒绝理由的读者。 理由是给模型的，要可执行。「不允许」是坏理由，「这个路径在 workspace 外，改用 workspace 内的临时目录」是好理由。理由会带 `Error: ` 前缀进入工具结果。

第二步，确认判断依据都在 `exec` 里。 可用字段是工具名、已冻结的参数、agent、session、callId。需要外部状态时，在注册时取一次快照，闭包捕获；不要在 guard 里发请求。

第三步，注册。 插件里直接调 `ctx.tools.guard`，disposer 挂在当前 context 的 effect 上：

```ts
ctx.tools.guard((exec) => {
  if (exec.name !== 'bash') return undefined
  const command = String((exec.arguments as { command?: unknown }).command ?? '')
  return /\brm\s+-rf\s+\//.test(command)
    ? 'refusing recursive delete outside the workspace; use the sandbox delete path'
    : undefined
})
```

第四步，考虑作用域。 只有某个子 agent 该受限制时，在子 agent 的 context 上注册，别注册到全局。`chainLayers` 会让它继承给后代，全局层仍先求值。

第五步，验证拒绝路径。 构造一次必然被拒的调用，检查结果 `isError` 为真、`content` 是 `Error: ` 开头、post-execute 仍被调用。第三步漏了，拒绝会变成异常。

写 hook 是另一套步骤。

第一步，选点位与匹配器。 匹配对象是工具名、会话来源或常量 `general-purpose`。`PreToolUse` 的匹配器写 `bash`，`Stop` 与 `UserPromptSubmit` 的匹配器被强制为空（匹配全部）。

第二步，写配置。 桥接受 `{ hooks: { ... } }` 形式的 settings，也接受裸的事件表：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [{ "type": "command", "command": "python3 .dsh/hooks/guard_rm.py", "timeout": 10 }]
      }
    ]
  }
}
```

第三步，脚本从 stdin 读事件，往 stdout 写决策。 权限结论必须放在 `hookSpecificOutput` 里，顶层 `decision` 只认 `approve` / `block` 两个旧值：

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "recursive delete outside the workspace"
  }
}
```

第四步，处理失败。 钩子崩了不会阻断调用。要拦住的场景，脚本自己 try/catch，出错时输出 deny 而不是退出非零。

第五步，看 `hook/result`。 每次运行都会留下 `decision`、`exitCode`、`stderrSummary`、`durationMs`。没有这对记录，钩子就是黑盒。

三条禁令：

1. 禁在 guard 里做异步或重活。签名是同步的，每个放行的调用都会跑一遍；一次磁盘检查乘以调用次数，就是整轮对话的固定开销。
2. 禁把 hook 当参数改写器。`updatedInput` 会被解析、警告、丢弃；参数在更早的阶段已冻结，任何「钩子改一下参数」的设计都走不通。
3. 禁把「放行」写成逻辑。guard 没有 allow；hook 的 allow 在折叠里被 deny 压过；审批策略 `never` 在 waterfall 之前生效。三层都只能加严，想放宽只能改配置或改工具。

## 8. 设计取舍

1. guard 做成同步的。牺牲了「查外部状态再判断」的便利，换来两点：每个调用都能承受的固定成本，以及没有 await 之后仍能保持的求值顺序。要异步判断，用 pre-execute 监听器，接受它可能被后续监听器覆盖的事实。

2. guard 没有 allow。牺牲了插件间的覆盖组合，换来单调性。对安全判断，这个交换划算；对「临时放行」的需求，正确做法是改 restriction 或改 guard，不是加一个后置 allow。

3. 超时是协作式的。换来的是结果与副作用一致：不会出现「调用被放弃但进程还在写文件」。代价是忽略取消的工具不受约束，预算形同虚设。

4. 超时预算放在工具定义上。换来的是不可拼错的工具名与「未声明即不限制」的清晰默认。代价是没有统一上限，得逐个工具检查。

5. 重复提醒只建议不阻断。换来的是零误杀：最坏情况是多一条消息。代价是它对卡死的循环只有提醒作用，模型可以选择继续。

6. 钩子用外部进程。换来的是语言无关与配置兼容：任何能读 stdin 写 stdout 的程序都能当钩子。代价是每次调用一次进程启动、一个 600 秒的默认超时，以及失败时的静默。

7. 钩子记录做成 log-only。换来的是模型上下文不被审计噪声污染。代价是排查只能看会话日志，模型自己不知道钩子跑过。

## 9. 踩坑点

1. 以为 guard 在审批之前跑。现象：用户先被弹窗，点了允许之后才看到 guard 的拒绝。原因：`checkGuards` 在 `decision.kind === 'allow'` 的分支里。对策：把「绝不允许」放进 pre-execute 的早期监听器，或接受这次白问。

2. 在 guard 里 throw。现象：整次调用变成 `Error: ...`，理由文本丢失，post-execute 可能没跑。原因：guard 的契约是返回字符串，异常走的是另一条物化路径。对策：只返回字符串。

3. 以为超时会杀进程。现象：`bash` 卡了十分钟，超时结果一直不出现。原因：包装层 `await next()` 到静止才替换结果。对策：给工具实现取消，或改用可终止的作业。

4. 给所有工具配同一个超时。现象：大仓库的 `grep` 被切成失败。原因：预算在工具定义上，统一值只能靠改注册。对策：逐个工具评估，宁可不设。

5. 把重复提醒当 guard 用。现象：期望第三次重复被阻断，实际只是多一条消息。原因：`repeat-tool-reminder` 只折 `additionalContexts`，从不否决。对策：要阻断就写 guard，但要先算误报。

6. 用 `include` 排除了关键工具。现象：循环发生在被排除的工具上，一条提醒都没有。原因：被排除的调用既不计数也不重置。对策：排除清单只放确实不需要提醒的工具。

7. 钩子脚本 exit 1 以为能拦住。现象：调用照常执行。原因：只有 exit 2 被当成 block，其余非零退出只记日志。对策：用 exit 2，或输出结构化 deny。

8. 钩子配置写错，以为会报错。现象：护栏静默失效。原因：装载时解析失败只 warn，不注册任何钩子。对策：把 `hook/invoked` 是否出现纳入启动自检。

9. 以为 `PostToolUse` 能改工具结果。现象：模型看到的还是原始输出。原因：deny 只能换成 feedback 文本，改内容要在 `tools/post-execute` 的监听器里做。对策：区分「钩子」与「原生插件」。

10. 在 `Stop` 钩子里做长任务。现象：一次 turn 结束被拖住，或者 steer 之后又触发一次钩子。原因：Stop 钩子是同步等待的，deny 会强制再走一步。对策：Stop 钩子只做判断，长任务放后台。

## 10. 源码索引

| 位置 | 证明什么 |
| --- | --- |
| `packages/core/tools/src/contracts.ts:384-391` | guard 的签名与「无 allow」注释 |
| `packages/core/tools/src/index.ts:381-400` | `guard()` 注册与 `checkGuards` 的两层循环 |
| `packages/core/scope/src/store.ts:192-200` | `chainLayers` 由远到近的顺序 |
| `packages/core/tools/src/pipeline.ts:196-236` | guard 只在 allow 分支运行 |
| `packages/core/tools/src/pipeline.ts:222-236` | 拒绝理由如何物化成 isError 结果 |
| `packages/guard/timeout-policy/src/index.ts:56-80` | deadline 派生、signal 交换与 finally 还原 |
| `packages/guard/timeout-policy/src/index.ts:41-54` | `TOOL_TIMEOUT` 结果的消息与 code |
| `packages/guard/repeat-tool-reminder/src/index.ts:110-129` | 参数规范化与键的构造 |
| `packages/guard/repeat-tool-reminder/src/index.ts:180-232` | WeakMap 链、post-execute 计数与折叠 |
| `packages/guard/repeat-tool-reminder/src/index.ts:236-245` | 真实用户消息清零 |
| `packages/interaction/permission-presets/src/index.ts:183-190` | 两个默认预设的 knob 组合 |
| `packages/interaction/permission-presets/src/index.ts:406-415` | 先记 `permission/preset` 再写 knob |
| `packages/interaction/user-approval/src/index.ts:56-68` | 四种结局与两种策略的语义 |
| `packages/interaction/user-approval/src/index.ts:296-301` | `never` 在 dispatch 之前拒绝 |
| `packages/experimental/hooks-claude-code/src/index.ts:163-187` | invoked/result 的写入与 `updatedInput` 忽略 |
| `packages/experimental/hooks-claude-code/src/index.ts:242-257` | PreToolUse / PostToolUse 的决策映射 |
| `packages/experimental/hook-protocol/src/types.ts:19-41` | 两个 log-only 事件的负载 |
| `packages/experimental/hook-protocol/src/events.ts:75-105` | `appendHookInvoked` / `appendHookResult` |
| `packages/experimental/hook-protocol/src/codec.ts:90-134` | 顶层 decision 与 hookSpecificOutput 的解析差异 |

未核实点两处：

- 「30 个事件里只映射 6 个」取自 `hooks-claude-code` 的模块注释与 README，没有逐条比对 Claude Code 当前版本的事件表。
- 钩子进程的实际启动开销没有做基准测试，文中只说「每次调用一次进程启动」，不给毫秒数。

## 最后

护栏这一层最容易被写错的地方，是把「能拦住」与「拦得住」混为一谈。guard 的拦截是确定的，代价是它只能看见参数；超时的拦截是协作的，代价是工具不配合就没有上限；钩子的拦截是可选的，代价是配置错、脚本崩、超时，任何一种都让护栏静默消失。

选型可以按一个问题走：这个约束失效时，你希望发生什么。希望调用失败，用 guard 或 pre-execute；希望模型知道但继续，用提醒；希望完全不影响调用，用日志与 `hook/result`。三者的失败模式不一样，别用同一个机制覆盖三种需求。

下一篇讲跨进程的两种接法：LSP 把编辑器能力借给 agent，SSH 把执行搬到另一台机器。两者都绕不开今天这三层：LSP 工具声明了 60 秒预算，被 `timeout-policy` 接管；SSH 的失败要如实报成「结果未知」，而不是伪装成一次干净的拒绝。

上一篇：[08 凭据与身份：谁能代表用户](/posts/dsh-credentials-and-identity.html)｜下一篇：[10 LSP 与远程执行：跨进程的两种接法](/posts/dsh-lsp-and-ssh.html)

延伸阅读：[工具执行流水线：九段与三闸](/posts/dsh-tool-pipeline.html)、[人在回路：审批、提问与权限预设](/posts/dsh-human-in-the-loop.html)、[错误、重试与恢复：一次失败的三种归宿](/posts/dsh-error-retry-recovery.html)。
