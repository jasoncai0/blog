---
title: "工具注册表：全局、作用域与遮蔽"
date: "2026-11-11T13:20:00+08:00"
summary: "DSH 的工具表不是一张全局表，而是全局注册、每 agent 作用域、restriction 过滤、同名遮蔽四层解析的结果。被过滤的工具在提示词里消失，在执行时也像不存在。"
description: "拆开 ToolsService.view()：注册即 effect，作用域不向下继承，restriction 取交集，遮蔽按最近者胜；以及工具集合变化如何变成 developer/message。"
tags: ["harness", "architecture", "agent"]
slug: "dsh-tool-registry-scope"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准；包内 `lib/` 是构建产物，引用一律指向 `src/`。
> 这是系列的第 9 篇，进入「工具执行平面」。上一篇 [08 dsh-error-retry-recovery](/posts/dsh-error-retry-recovery.html) 讲失败怎么收敛，这一篇讲工具怎么被看见。

第一次给 DSH 写工具的人，常会撞上三个互相矛盾的现象。

给某个 agent 单独注册一个工具，父 agent 立刻能看见它；反过来，父 agent 在 `setup` 里注册的工具，子 agent 却像从来没听说过。给 agent 加一条 `restrict({ deny: ['bash'] })`，模型既不报错，也不抱怨工具被拿走，它只是换了个办法把事做完。再给两个 scope 注册同名工具，不报错的那个版本生效了，另一个连日志都没有。

这三个现象不是 bug，而是同一套解析规则在不同侧面的投影。DSH 的工具表从来不是「一张全局 Map」，而是四层叠加：**全局注册、每 agent 作用域、restriction 过滤、同名遮蔽**。模型看到的永远是这四层算完之后的一个快照；快照之外的工具，对模型来说等于不存在。

{{< fig "diagrams/01-four-layers.svg" "四层解析把一份全局注册表收敛成某个 agent 在某一步可见的工具集合；knownNames 保留了「曾经存在」的痕迹，visible 才是模型能调用的部分" >}}

## 0. 先给结论

| 层 | 数据在哪 | 谁写 | 作用 | 冲突规则 |
| --- | --- | --- | --- | --- |
| ① global | `ScopedLayers.global` | 部署、bundle、preset | 所有 agent 的默认工具箱 | 同名重复直接抛错 |
| ② scoped | 每个 scope 一个 `ToolLayer` | `agent.ctx` 里的插件、setup | 只对该 agent 生效的增删 | 沿 scope 链「最近者胜」 |
| ③ restriction | 每个 layer 的 `restrictions` | `agent.ctx.tools.restrict()` | 过滤**继承来的**名字 | 多层 `every`，等价交集 |
| ④ shadowing | `view()` 的合并顺序 | 不需要显式 API | 同名覆盖 | own > 近祖先 > 远祖先 > global |

三句话概括：

1. **注册即 effect**。`register()` 返回的是 Cordis 的 disposer，agent 卸载时注册自己消失，没有 `unregister()` 这种补丁式 API。
2. **作用域只向下过滤事件，不向下继承注册**。子 agent 是兄弟，不是子节点；它要的工具得在自己的 `setup` 里注册。
3. **被过滤 = 不存在**。工具既不进 schema，也不进执行分支；模型拿到的是 `UNKNOWN_TOOL`，与拼错名字无法区分。

这篇按这四层从外往里拆，最后落到 schema 如何进提示词、工具集合变化如何通知模型。

## 1. 注册不是写表，是挂一个 effect

`packages/core/tools/src/index.ts` 里没有 `Map<string, ToolDefinition>` 这样的字段。服务持有的是三层 `ScopedLayers`：

```ts
// packages/core/tools/src/index.ts:106
private readonly layers = new ScopedLayers(
  scope => new ToolLayer(scope),
  () => { this.ctx.emit('tools/change') },
)
```

`ScopedLayers` 立即创建一个 global layer，其余 scope 的 layer 惰性创建；`register()` 不直接改表，而是把「插入」这个动作登记成调用方 `ctx` 上的一个 effect：

```ts
// packages/core/tools/src/index.ts:128-153（节选）
register(definition: ToolDefinition, options: ToolRegistrationOptions = {}): () => void {
  // ... 校验 output / timeoutMs / 保留名 run_code ...
  return this.layers.effect(
    this.ctx,
    layer => layer.tools.insert(definition.name, { definition, when: options.when }),
    { label: 'tools.register()' },
  )
}
```

哪一行是关键：`this.layers.effect(this.ctx, ...)`。第一个参数决定生命周期——在哪个 `ctx` 上注册，就跟着哪个 fiber 卸载。工具插件被热替换、agent 被 dispose、preset 被弹出，Cordis 会回收这个 effect，`ToolLayer` 里的条目随之消失，同时触发 `tools/change`。注册函数返回的 disposer 就是同一个东西，调用方可以提前撤销，但**没有第二条撤销路径**。

这带来两个直接后果。

第一，重复注册的判定发生在**同一个 layer 内**。`ToolLayer` 构造函数对同名条目直接抛错，错误信息还特意区分了两种场景：

```text
tool "deploy" is already registered (for a per-agent variant, register through that agent's `agent.ctx` instead)
```

第二，layer 的回收是「空了才收」。一个 scope 上注册了三个工具，撤掉两个，layer 还在；撤掉最后一个，`ScopedLayers` 才把整个 `ScopeLayer` 丢掉。这不是性能优化，而是为了保证「读不创建 layer」：`view()` 可以安全地遍历任意 scope，不会因为一次查询就留下空壳。

> **自己搭 Agent 时该怎么做**：如果你的注册表提供 `register()` 却没有把返回值接到宿主的生命周期上，你迟早要写一个 `unregisterAll()`，然后在热重载时漏掉一半。把「注册」实现成「在调用方作用域上挂一个可撤销的 effect」，让撤销成为唯一的清理语义；不要同时暴露 `remove(name)`，否则同一个条目会有两条死亡路径，顺序问题会追着你跑。

## 2. scope 是扁平的：deployment 与 agent 两层

`packages/core/scope/src/index.ts` 只做四件事：铸 key、把 key 打到 `Context` 上、维护 parent 链、按链过滤事件。

```ts
// packages/core/scope/src/index.ts:137-147（节选）
export function createScope(ctx: Context, key: ScopeKey, options?: CreateScopeOptions): Scope {
  if (options?.parent !== undefined) bindScopeParent(key, options.parent)
  const fiber = ctx.plugin(scope)
  const scoped: Context = fiber.ctx.extend({ [kScope]: key })
  return { ctx: scoped, rawDispose: fiber.dispose, dispose: () => (disposing ??= quiesceFiber(fiber)) }
}
```

`scopeOf(ctx)` 读的就是 `ctx[kScope]` 这个 symbol 属性；没有标签的 `ctx` 是「全局视角」，它的监听器能收到所有 scope 的事件。`scopeChainOf(key)` 返回 `[key, parent, grandparent, …]`，`scopeTarget(base, key)` 再把它变成 Cordis 的过滤器：**事件只向上流，永不向下**。一个注册在 deployment scope 的监听器能看见 agent 的事件，反过来不行。

关键在「谁是谁的 parent」。核心 agent 的创建只有一行：

```ts
// packages/core/agent-loop/src/agent.ts:147
this.scope = createScope(loopCtx, this)
```

没有传 `parent`。也就是说，在工具解析这件事上，所有 agent 的 scope 是**兄弟**，它们共同的祖先只有部署层。`bindScopeParent()` 是给需要真正分层的作用域准备的（例如某些实验性的浏览器运行时把子 scope 挂到宿主 scope 上），不是 agent 委派的默认行为。

这解释了两个看似矛盾的现象：

- deployment 层用 scoped 方式注册的工具，所有 agent 都能看见——因为 deployment 是它们的祖先；
- 父 agent 用 `agent.ctx` 注册的工具，子 agent 看不见——因为两者没有父子 scope 关系。

{{< fig "diagrams/02-scope-lifecycle.svg" "一个 agent scope 从 createScope 到 quiesceFiber 的生命周期；setup window 里只允许注册，agent/created 之后才发布，dispose 时所有 effect 逆序撤销" >}}

生命周期可以切成四段：

| 阶段 | 触发点 | 能做什么 | 模型能看到吗 |
| --- | --- | --- | --- |
| setup window | `CreateAgentOptions.setup` | 注册工具、restriction、prompt section | 不能，首次提示词还没组装 |
| created | `agent/created` | 读取最终 scope、挂观察者 | 能，下一步请求开始带工具 |
| running | 每个 model step | `resolve()` 冻结本步工具集合 | 能，按 step 快照 |
| disposed | `dispose()` → `quiesceFiber` | effect 逆序撤销，`tools/change` | 不能，scope 关闭 |

`setup` 的语义值得单独说：此时 scope 和 agent 对象都已存在，但 agent 还没发布，`agent/created` 没触发，第一次提示词也没组装。这个窗口是**唯一**可以「先注册、后发布」的地方。很多工具需要拿到 agent 的 session 或 cwd 才能工作，不能在 `apply()` 里就注册；`setup` 给了它们一个不会惊动模型的准备期。

> **自己搭 Agent 时该怎么做**：不要用「父 agent 的注册表」给子 agent 发工具。给每个 agent 一个独立的注册作用域，父 agent 在创建子 agent 时显式把工具注册进**子**作用域；父作用域只放真正共享的东西。否则你会同时得到两个 bug：子 agent 意外获得父的私有能力，以及父 agent 被回收时子 agent 正在跑的工具凭空消失。

## 3. 第一层 global：默认工具箱

global layer 在 `ScopedLayers` 构造时就存在。部署 bundle 里的 `ctx.tools.register()` 都落在这里，因为那些插件挂载在根 `ctx` 上，`scopeOf(ctx)` 是 `undefined`。

解析时它是起点：

```ts
// packages/core/tools/src/index.ts:199-213（节选）
const layers = this.layers.chainLayers(scope)
const own = this.layers.peek(scope)
const inherited = new Map(this.layers.global.tools.entries())
for (const layer of layers) {
  if (layer === own) continue
  for (const [name, registration] of layer.tools.entries()) inherited.set(name, registration)
}
```

`inherited` 先用 global 填充，再按 scope 链**由远及近**覆盖。因为是 `Map.set`，后写的赢；`chainLayers` 的顺序是 nearest-first，所以循环里越晚出现的其实是越远的层——但每一层都会覆盖前一层，最终效果仍然是「近的覆盖远的」。这段代码短，但它是整个遮蔽语义的地基。

global 层还有一个特殊之处：它没有 restriction。`restrict()` 要求调用方有 scope，根 `ctx` 上调用会直接报错：

```text
tools.restrict() requires a scoped context (agent.ctx): restricting the deployment-wide registry is not supported
```

这不是遗漏，而是权限模型的一部分。部署层能注册什么，是部署者的事；某个 agent 能少用什么，是那个 agent 的事。把「全局禁用」做成 restriction，会让所有 agent 的可见集合依赖一个全局可变状态，排查问题时无法区分「部署没装」和「有人禁了」。

## 4. 第二层 scoped：最近者胜，兄弟不继承

`agent.ctx.tools.register()` 落在该 agent 的 `ToolLayer`。解析时它有两个身份：

1. 作为 `own`，**无条件**进入 `visible`（下一节讲为什么）；
2. 作为祖先层，对它的后代（如果有）表现为「更近的注册」，覆盖更远的同名项。

核心 agent 之间没有父子 scope，所以第 2 条在默认组合里只对 deployment → agent 生效。真正需要 agent → agent 继承的场景，必须显式 `bindScopeParent(child, parent)`；这时子 agent 会看见父 agent 的 scoped 注册，但仍然看不见父的 restriction 之外的私有状态——restriction 是跟着 layer 走的，父层的 restriction 会在子层解析时一起参与 `every`。

为什么默认不继承？三个理由，按重要性排序：

**能力边界**。子 agent 是父 agent 为了完成子任务创建的，它应该拿到「完成这个子任务所需的最小工具集」，而不是父的全部能力。如果 scoped 注册默认向下继承，父 agent 每加一个危险工具，所有后代都自动获得，授权模型就反了。

**生命周期**。父 agent 可能比子 agent 先结束。如果子 agent 的可见集合依赖父的 layer，父 dispose 时子 agent 正在跑的一批调用会中途失去工具。DSH 的选择是让每个 agent 的工具集合只依赖自己和部署层，父的死活不影响子。

**可解释性**。工具集合是 prompt header 的一部分，header 进请求、进缓存、进日志。如果它依赖一条可变的继承链，同一个子 agent 在不同时刻的 header 可能不同，缓存命中率和问题复现都会变差。

那父子之间怎么协作？靠 **lineage 数据**，不靠 scope 结构。`parentSession`、`delegationDepth`、`subagentDepth` 这些字段告诉运行时「谁是谁的父亲」，但它们不参与工具解析。父 agent 要给子 agent 工具，就在创建子 agent 的 `setup` 里往子 scope 注册——这是一次显式的、可审计的授权动作。

> **自己搭 Agent 时该怎么做**：把「继承」换成「显式传递」。父 agent 创建子 agent 时，传入一个 `tools: [...]` 或 `setup: (ctx) => {...}`，由创建点决定子 agent 能看见什么。不要提供 `inheritTools: true` 这种开关：它省下的一行配置，会在你排查「子 agent 为什么能删库」时变成一周。

## 5. 第三层 restriction：交集，而且只过滤继承

`restrict()` 是这四层里唯一一个「减法」API：

```ts
// packages/core/tools/src/index.ts:162-189（节选）
restrict(filter: ToolRestriction): () => void {
  const scope = scopeOf(this.ctx)
  if (scope === undefined) throw new Error('tools.restrict() requires a scoped context (agent.ctx): ...')
  if (filter.allow === undefined && filter.deny === undefined) throw new Error('tools.restrict({}) is a no-op')
  // ... 编译 allow/deny 为 Set，拒绝 run_code，按 restrictableNames 校验未知名字 ...
  return this.layers.effect(this.ctx, layer => layer.restrictions.append(compiled), { label: 'tools.restrict()' })
}
```

三个细节决定了它的行为。

**只作用于继承来的名字。** `view()` 先过滤 `inherited`，再把 `own` 无条件合并进来。所以 restriction 不能隐藏同一个 scope 里注册的工具；它只能挡住从 global 或祖先 scope 继承来的同名项。

**多层取交集。** 判定不是「任一 restriction 拒绝」，而是 `layers.every(layer => layer.admits(name))`。每个 layer 的 `admits()` 要求本层所有 compiled restriction 都通过：如果某个 restriction 有 `allow` 集合，名字必须在里面；如果有 `deny`，名字不能在里面。两个 restriction 同时存在，等价于对继承名字做交集。

**allow 与 deny 的默认方向相反。** 只写 `deny` 的过滤器，允许所有未列出的继承工具通过；只写 `allow` 的过滤器，排除所有未列出的继承工具。这个不对称是刻意的：deny 表达「去掉几个」，allow 表达「只留这些」。

{{< fig "diagrams/04-restriction.svg" "两条 restriction 在同一条 scope 链上取交集；own 注册绕过滤，因此可以在被禁的名字上重新提供一个受控变体" >}}

`restrict()` 还拒绝把 `run_code` 写进 allow 或 deny。原因是 `run_code` 不是普通工具，它是 code mode 的 transport，是否可用由 agent 的 mode 决定，不由工具表决定。把它塞进 restriction 会让两个独立开关打架。

> **自己搭 Agent 时该怎么做**：restriction 要按「作用域 + 继承」建模，不要做成全局黑名单。一个全局 `disabledTools` 集合无法表达「这个子 agent 不能用 bash，但另一个可以」；而一旦你为它加上 `agentId` 参数，它实际上已经是一个 scoped restriction 了，只是没有生命周期。

## 6. 第四层 shadowing：同名覆盖的判定顺序

遮蔽不需要 API。它就是 `view()` 的合并顺序：

```ts
// packages/core/tools/src/index.ts:214-226（节选）
for (const [name, registration] of inherited) {
  knownNames.add(name)
  restrictableNames.add(name)
  if (layers.every(layer => layer.admits(name))) visible.set(name, registration.definition)
}
for (const [name, registration] of own.tools.entries()) {
  knownNames.add(name)
  registrations.set(name, registration)
  visible.set(name, registration.definition)   // 无条件
}
```

判定顺序可以写成一张表：

| 顺位 | 来源 | 是否受 restriction | 覆盖谁 |
| --- | --- | --- | --- |
| 1 | 当前 scope 的 own 注册 | 否 | 所有同名继承项 |
| 2 | 最近祖先的注册 | 是（该层及更远层的 restriction） | 更远祖先与 global |
| 3 | …沿链上溯… | 是 | 更远层 |
| 4 | global | 是（链上所有 restriction） | 无 |

两个容易踩的点。

**restriction 在遮蔽之前。** 如果一个全局工具被 restriction 挡住，而当前 scope 又注册了同名工具，最终可见的是当前 scope 的版本。这不是漏洞，而是「受控变体」的正规写法：禁掉通用的 `bash`，换成一个只允许跑 `npm test` 的 `bash`，注册在同一个 scope 里。

**同一 layer 内不允许重名。** 遮蔽只发生在层与层之间。想在同一个 agent 上提供两个同名工具，必须在不同的 layer（例如再包一个子 scope），否则 `ToolLayer` 在插入时就抛错。这条约束把「遮蔽」和「重复注册」分开了：前者是解析规则，后者是编程错误。

还有一个隐蔽的交互：`knownNames` 包含被 restriction 挡掉的名字，`visible` 不包含。这两个集合的差集，就是「模型不知道、但系统知道它存在」的工具。下一节会看到，这个差集是安全边界的一部分。

## 7. 「不存在」比「被拒绝」更安全

`resolve()` 每个 model step 调一次，返回一个冻结的 `ResolvedToolSet`。它的 `view(scope)` 不是简单返回当前表，而是带三重重验：

```ts
// packages/core/tools/src/index.ts:264-297（节选）
return Object.freeze({
  dispose,
  view(scope) {
    if (closed || scope !== agent) return undefined
    const current = this.view(scope)
    if (current.visible.get(name) !== definition) return undefined
    if (current.registrations.get(name) !== view.registrations.get(name)) return undefined
    return item
  },
})
```

也就是：批次开始后，如果注册被移除、被同名替换，或者新加了一条 restriction，**尚未开始**的调用会被挡住；已经进入 body 的调用不受影响。这是「快照 + 执行时复核」的组合：快照保证同一批调用的决策一致，复核保证撤销能及时生效。

被挡住时返回什么？`get()` 找不到定义，`createExecution()` 走 rejected 分支，最终是一个 `ToolNotFoundError`，code 是 `UNKNOWN_TOOL`。它不是 `DENIED`，不是 `FORBIDDEN`，文案和拼错工具名完全一样。

这是一个明确的安全取舍：

- **好处**：模型无法通过错误信息探测「这个环境里有没有 sudo 工具」。如果有，它会尝试绕过；如果没有，它会换方法。让它始终得到「未知工具」，能力面就不泄露。
- **代价**：调试变难。开发者看到模型反复拼错名字，无法从模型侧知道是「没注册」还是「被 restriction 挡了」。补偿手段在服务端：`knownNames` 保留全量名字，`ToolNotFoundError.reachableFrom` 能说明某个名字是「可见但不可直呼」，日志里也拿得到。

同样的原则适用于 `executable()`：

```ts
// packages/core/tools/src/index.ts:251-255
executable(name: string, scope: ScopeKey, toolSet?: ResolvedToolSet): ToolDefinition | undefined {
  if (toolSet !== undefined) return toolSet.view(scope)?.get(name)?.definition
  const registration = this.view(scope).registrations.get(name)
  return registration?.when === undefined ? registration.definition : undefined
}
```

没有 `toolSet` 时，带 `when` 条件的注册一律不可执行——因为条件求值需要一次 `resolve()`，而调用方没提供集合，就不能假装条件成立。这是 fail-closed 的写法：默认不可用，除非有明确的准入快照。

> **自己搭 Agent 时该怎么做**：把「不可见」和「不可执行」做成同一个判断，不要一个在提示词层过滤、一个在执行层放行。常见事故是：schema 里删掉了工具，但执行器仍然接受同名调用；或者反过来，执行器拒绝了，错误信息却告诉模型「你没有权限」。前者是越权，后者是能力泄露。正确做法是让执行器用同一份可见集合判定，并返回与「不存在」相同的错误。

## 8. schema 怎么变成提示词

工具定义里有一堆字段：`execute`、`output.schema`、`output.render`、`projectContent`、`finalizeContent`、`timeoutMs`、`isConcurrencySafe`、`presentCall`、`presentResult`。模型一个都看不到。

`schemas()` 的投影是显式 allowlist：

```ts
// packages/core/tools/src/index.ts:316-318
schemas(scope?: ScopeKey, toolSet?: ResolvedToolSet): ToolSchema[] {
  return this.definitions(scope, toolSet).map(d => projectToolSchema(d, true))
}
```

`projectToolSchema` 只取四个字段：

```ts
// packages/core/tools/src/schema.ts:641-646
export function projectToolSchema(definition: ToolDefinition, detachParameters: boolean): ToolSchema {
  const { name, description, parameters, deferLoading } = definition
  const detached = detachParameters ? snapshotJsonValue(parameters) : parameters
  if (detached === undefined) throw new Error(`tool "${name}" parameters must be lossless JSON before schema projection`)
  return { name, description, parameters: detached, ...deferLoading === true ? { deferLoading } : {} }
}
```

`detachParameters` 为 `true` 时做一次无损 JSON 快照。为什么注册表要 detach，执行器却传 `false`？因为注册表的返回值会被调用方长期持有，必须与定义对象解耦；执行器在请求组装的热路径上，definition 刚刚被解析过，直接引用即可。`snapshotJsonValue` 返回 `undefined` 就抛错——参数里只要有一个函数、`undefined`、`bigint` 或循环引用，注册就不算成功。

投影之后，schema 走 `systemPrompt.tools()` 注册的 provider：

```ts
// packages/core/tools/src/index.ts:113-117
ctx.systemPrompt.tools(({ scope, toolSet }) => {
  const executor = this.selectExecutor(scope)
  const description = executor.describe(this.definitions(scope, toolSet))
  return { ...description, knownNames: description.knownNames ?? [...this.view(scope).knownNames] }
})
```

`describe()` 是执行器的方法，不是工具的。原生执行器只返回 `{ schemas }`；一个把工具说明渲染成 prompt 文本的执行器可以返回 `sections`，一个支持 deferred loading 的执行器可以只返回名字。这个 seam 让「工具怎么呈现给模型」和「工具怎么执行」解耦，但也意味着 **schema 的最终形状由执行器决定，注册表只保证输入是合法的 ToolDefinition**。

{{< fig "diagrams/03-schema-to-prompt.svg" "ToolDefinition 的九个字段里只有 name/description/parameters 进入模型请求；其余字段在注册表内部被 allowlist 截断，执行器再决定是否追加 prompt section" >}}

`knownNames` 是 provider 返回值里的一个旁路字段。如果执行器没有自己提供，注册表用当前 view 的全量名字兜底。它不直接进请求；它给执行器或上层做「名字是否存在」的诊断。真正进请求的只有 `schemas` 和 `sections`。

## 9. 工具集合变了，模型怎么知道

工具集合是 prompt header 的一部分。header 变了，模型需要一条解释，否则它会继续按上一轮的假设调工具，然后收到一串 `UNKNOWN_TOOL`。

DSH 的做法是在下一次 `buildRequest` 时做**名字集合 diff**：

```ts
// packages/core/agent-loop/src/agent.ts:692-705（节选）
if (baseline !== undefined && headerSeq !== undefined) {
  const previous = new Set((baseline.tools ?? []).map(t => t.name))
  const current = new Set(tools.map(t => t.name))
  const additions = tools.filter(t => !previous.has(t.name))
    .map(t => ({ type: 'tool-addition' as const, toolName: t.name }))
  const removals = (baseline.tools ?? []).filter(t => !current.has(t.name))
    .map(t => ({ type: 'tool-removal' as const, toolName: t.name }))
  if (additions.length > 0 || removals.length > 0) {
    this.session.append('developer/message', {
      position,
      message: createDeveloperMessage({ source: { kind: 'tool-registry' }, content: [...additions, ...removals] }),
      ...additions.length > 0 ? { headerSeq } : {},
    }, { surfaceOp: 'append' })
  }
}
```

四个细节：

1. **只在 header 变化时 diff。** `headerSeq` 只在 `request/header` 首次写入或内容变化时存在；没有新 header，就没有新消息。
2. **additions 引用新 header。** `headerSeq` 让模型知道「新工具的定义在刚才那条 header 里」。removals 不需要，因为新 header 里本来就没有它。
3. **先增后删。** 同一条消息按 `[...additions, ...removals]` 排序。工具集合同时换血时，模型先看到「多了什么」，再看到「少了什么」。
4. **只 diff 名字。** 描述或参数 schema 变了、名字没变，不会产生 developer/message。这是一个真实的可观测性缺口：模型看到了新 header，却不会收到「某个工具的参数变了」的提示。

`tools/change` 事件本身是 emit，不是 scoped dispatch。任何一个 scope 的注册变化都会广播，因为 global contribution 会影响每个 agent 的下一次组装。scoped listener 也会收到所有 scope 的变化——这里刻意没有过滤：宁可多重算一次提示词，也不要让某个 agent 的 header 落后于注册表。

## 10. 执行器也按 scope 选

工具表解决了「有哪些工具」，执行器解决「这批调用交给谁跑」。这两件事都按 scope 解析：

```ts
// packages/core/tools/src/index.ts:357-367（节选）
selectExecutor(scope?: ScopeKey): ToolExecutor {
  for (const key of scopeChainOf(scope)) {
    const record = this.scoped.get(key)
    if (record?.executor !== undefined) return record.executor
    if (record === undefined) break
  }
  return this.root
}
```

`setExecutor()` 每个 scope 只允许一个，重复注册直接抛 `Only one tool executor may be registered in each scope`。卸载时不是删记录，而是把 `record.executor` 置为 `undefined`，防止选择逻辑 fall through 到另一个执行器——「这个 scope 曾经选了 A，A 没了」应该报错，而不是悄悄换成 B。

`ctx.tools.execute(input)` 在**派发时**选执行器，然后把整批交给它。已经接受的批次即使执行器随后卸载，也仍然按当初选中的 provider 结算。这和工具注册的「执行时复核」是两种不同的时间语义：工具集合可以撤销未开始的调用，执行器不能中途换人。

## 设计取舍

**取舍一：注册即 effect，没有显式 unregister。** 好处是生命周期只有一套真相，热重载、agent dispose、preset 弹出都走同一条路径；代价是无法「暂时移除、稍后恢复」——恢复必须重新注册，而重新注册可能拿到不同的 layer 顺序。DSH 接受这个代价，因为工具表的变化本来就应该伴随一次提示词重组。

**取舍二：scope 扁平，子 agent 不继承。** 好处是能力边界清晰、父 agent 的生死不影响子 agent；代价是父 agent 要给子 agent 工具时必须显式传递，写起来更啰嗦。DSH 把这个成本放在创建点，而不是藏在继承链里。

**取舍三：被过滤 = 不存在。** 好处是不泄露能力面；代价是模型和开发者都少了「为什么不行」的信息。补偿手段放在服务端日志和 `knownNames`，不放在模型可见的错误里。

**取舍四：restriction 只管继承，不管 own。** 好处是「受控变体」可以覆盖被禁的名字；代价是 restriction 不是绝对黑名单，审计时必须同时看 layer 的 own 注册。如果安全要求是「这个名字绝对不能出现」，restriction 不够，还要在 executor 层拒绝。

**取舍五：遮蔽按层，不按注册时间。** 好处是解析结果与插件加载顺序无关（同一层内重复直接报错）；代价是无法用「后注册的赢」做动态覆盖，必须显式建一个更近的 scope。

## 踩坑点

**坑 1：在 `apply()` 里给 agent 注册工具，结果所有 agent 都拿到了。**
现象：工具出现在每个 agent 的提示词里。原因：`apply()` 的 `ctx` 是插件挂载的 `ctx`，通常没有 scope 标签，注册落进 global layer。对策：需要 per-agent 的工具，放进 `CreateAgentOptions.setup`，用传进来的 `agent.ctx` 注册。

**坑 2：给子 agent 注册了工具，父 agent 却看得见。**
现象：父 agent 的 schema 里多了一个它不该有的工具。原因：`setup` 里误用了外层的 `ctx`，而不是 `agent.ctx`。注册位置决定可见性，不决定「给谁用」。对策：在 `setup` 里只使用参数提供的 scoped ctx；需要断言时用 `scopeOf(ctx) === agent` 检查。

**坑 3：restriction 写了，工具还在。**
现象：`restrict({ deny: ['bash'] })` 之后模型仍然能调 `bash`。原因：`bash` 是在同一个 agent scope 里 own 注册的，restriction 只过滤继承项。对策：要么把注册移到 deployment 层再 restrict，要么在 executor 层加硬拒绝。restriction 不是权限系统。

**坑 4：同名工具不报错，行为却变了。**
现象：两个插件都注册 `read`，加载顺序不同，行为不同。原因：它们落在不同的 layer（一个 global、一个 agent scoped），遮蔽规则让近的赢，看起来像「随机生效」。对策：同一层内重名必须报错（DSH 已经这么做）；跨层重名要在启动日志里打一条 shadow 记录，否则排查时只能靠猜。

**坑 5：以为 `tools/change` 会刷新模型。**
现象：注册了新工具，模型下一步还是不知道。原因：`tools/change` 只是让提示词重组，真正通知模型的是 `buildRequest` 里的 `developer/message`；如果 header 没变（例如只是改了 `when` 条件），不会有消息。对策：把「工具集合变化」和「工具定义变化」分开观测；后者目前没有模型可见的通知。

**坑 6：restriction 在批次中途生效，部分调用失败。**
现象：同一批 20 个调用，前 10 个成功，后 10 个 `UNKNOWN_TOOL`。原因：`resolve()` 的快照在执行时复核，新 restriction 会挡住尚未开始的调用。对策：这是预期行为，不要在批次中途改 restriction；如果必须改，等批次结束。

**坑 7：把 `run_code` 写进 restriction，启动直接失败。**
现象：`restrict({ allow: ['read', 'run_code'] })` 抛错。原因：`run_code` 是保留名，code mode 的开关不在工具表里。对策：用 agent 的 mode 配置控制 code mode，不要动工具表。

## 源码索引

| 位置 | 证明了什么 |
| --- | --- |
| `packages/core/tools/src/index.ts:106` | `ScopedLayers` 是三层解析的载体，变化时 emit `tools/change` |
| `packages/core/tools/src/index.ts:128-153` | `register()` 把插入登记为调用方 ctx 的 effect，返回同一 disposer |
| `packages/core/tools/src/index.ts:162-189` | `restrict()` 要求 scoped ctx、拒绝空过滤器与 `run_code`、编译成 Set |
| `packages/core/tools/src/index.ts:199-227` | `view()` 的合并顺序：global → 祖先 → restriction → own 无条件合并 |
| `packages/core/tools/src/index.ts:251-255` | 无 `toolSet` 时带 `when` 的注册不可执行（fail-closed） |
| `packages/core/tools/src/index.ts:264-297` | `resolve()` 冻结快照，并在执行时复核注册身份 |
| `packages/core/tools/src/index.ts:316-318` | `schemas()` 只投影模型可见字段 |
| `packages/core/tools/src/index.ts:357-367` | 执行器也按 scope 链最近者胜，卸载不 fall through |
| `packages/core/tools/src/schema.ts:641-646` | `projectToolSchema` 的 allowlist 与参数快照 |
| `packages/core/scope/src/index.ts:98-102` | `scopeChainOf` 的 nearest-first 顺序 |
| `packages/core/scope/src/index.ts:137-147` | `createScope` 铸 key、建 fiber、quiesce 语义 |
| `packages/core/scope/src/index.ts:170-185` | 事件只向上流，未打标签的监听器全局可收 |
| `packages/core/agent-loop/src/agent.ts:147` | agent scope 创建时不绑定父 agent，兄弟关系的来源 |
| `packages/core/agent-loop/src/agent.ts:692-705` | 工具集合 diff 变成 `developer/message` 的四条规则 |
| `packages/core/tools/src/contracts.ts:376-391` | `ToolRestriction` 与 `ToolGuard` 的形状（guard 没有 allow） |
| `docs/glossary.zh.md:11-21` | agent-scope 的正式定义：扁平、不向下继承、过滤即不存在 |

## 结尾

工具注册表最难的部分不是「存哪些工具」，而是「在哪个时刻、以谁的名义、用哪份快照回答『有哪些工具』」。DSH 把这个问题拆成四层：注册绑定生命周期，scope 绑定能力边界，restriction 绑定授权，遮蔽绑定覆盖关系。四层各自只有一个规则，组合起来却足以解释开头那三个矛盾现象。

下一篇 [10 dsh-tool-pipeline](/posts/dsh-tool-pipeline.html) 从「有哪些」转到「怎么跑」：一次调用如何穿过九段流水线，以及为什么任何一段都能改写结果、却只有一处能把异常变成 `isError`。

上一篇：[08 dsh-error-retry-recovery](/posts/dsh-error-retry-recovery.html)｜下一篇：[10 dsh-tool-pipeline](/posts/dsh-tool-pipeline.html)
