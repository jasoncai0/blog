---
title: "Cordis 内部：fiber、effect 与依赖注入"
date: "2026-11-17T18:20:00+08:00"
summary: "DSH 的插件语义就是 Cordis 的语义。fiber 六态、effect 逆序释放、服务只在提供者 ACTIVE 时可见、事件五种派发方式，四张图讲清一个插件从装载到拆除的全部路径。"
description: "Cordis 容器内部：fiber 状态机、effect 生命周期与逆序释放、reflect 服务可见性、waterfall 与 veto 语义。"
tags: ["architecture","concurrency","harness"]
slug: "dsh-cordis-internals"
---


> 依据：DSH 0.2.2-rc.1（commit fcc0ad46）。Cordis 以 workspace 包形式 vendored 在 `vendor/cordis/src/`，共 9 个文件 2,811 行；本文行号取自该目录。凡涉及运行时行为的结论，均已对照实现；未做并发压力实验。

第一季讲过一句话：DSH 里没有需要打补丁的内核，模型适配器、工具注册表、会话日志、agent loop 自己都是插件。这句话的下一半在 `vendor/cordis/`：插件凭什么能被装进去、凭什么能被干净地拆掉、拆的瞬间谁先谁后。

不读这一层，很多设计看起来像约定。读了之后会发现它们是容器的语义：

- 为什么所有注册都要写成 `ctx.effect()` 或 `ctx.on()`，因为它们不是「登记」，是「副作用」。
- 为什么服务在提供者加载完成前读不到，因为 `strict` 只返回 ACTIVE 的实现。
- 为什么卸载顺序是依赖方先走、提供方后走，因为 `_unload()` 会先等所有 dependents 清空。
- 为什么 `waterfall` 监听器不调 `next()` 就等于否决，因为整条链连同内建行为一起被短路。

{{< fig "diagrams/01-fiber-states.svg" "fiber 的六个状态与迁移条件。菱形是判定入口，方框是稳定状态，虚线是内部迁移。" >}}

## 1. 一个插件就是一根 fiber

`ctx.plugin(callback, config)` 的产物是 `Fiber`（`fiber.ts:184`）。它记的不是插件实例，是一个插件应用的生命周期：配置、状态、effect、依赖、清理全在它身上。

| 字段 | 含义 | 关键细节 |
| --- | --- | --- |
| `uid` | 注册表内唯一 id | 根 fiber 是 `0`，dispose 后变 `null` |
| `ctx` | 这个插件运行的上下文 | `parent.extend({fiber: this})`，不是父 ctx 本身 |
| `config` | 原始输入，留给下次激活 | 与「生效配置」分开存 |
| `state` | 六态之一 | 每次变化发 `internal/status` |
| `dispose` | 拆除函数 | 可重复调用，幂等 |
| `committed` | 被依赖方持有的实现快照 | 卸载期间仍要供给 |
| `provisions` | 本 fiber 提供的服务实现 | 用于等待 dependents |
| `inertia` | 正在进行的装载/卸载 | `await fiber.await()` 等它 |
| `_disposables` | effect 清理器栈 | 逆序串行执行 |

构造器里有一处顺序值得记住（`fiber.ts:280-330`）：先建子上下文，再把 `dispose` 注册为父 fiber 的 effect，然后发出 `internal/plugin` 通知，最后才 `_checkImpl()` 与 `_refresh()`。

```ts
// fiber.ts:280-330（节选）
this.dispose = parent.fiber.effect(() => {
  // 登记到 runtime.fibers，返回清理函数
  return async () => {
    this.uid = null
    ...
    this._setEpoch(INACTIVE)
  }
}, 'ctx.plugin()')
context.emit('internal/plugin', this)
// 注释原文：Keep the initial notification's historical PENDING view
this._checkImpl(name)
this._refresh()
```

那条注释解释了一个容易被当成 bug 的现象：`internal/plugin` 的监听器看到的是 `PENDING`，而不是 `ACTIVE`。依赖必须在通知之后才解析，否则监听器里读到的服务状态与真实装载顺序不一致。

父子关系是 effect 关系，不是对象关系。 父 fiber 卸载时，子 fiber 的 `dispose` 作为父的一个 effect 被动执行。这就是为什么「配置创建的 agent 归 loop fiber 所有」，不是约定，是 `ctx.plugin` 的返回值挂在谁的 effect 栈上。

## 2. 六态与两条优先级规则

`FiberState`（`fiber.ts:147-154`）是 `const enum`，六个成员按声明顺序取 0–5：`PENDING / LOADING / ACTIVE / FAILED / DISPOSED / UNLOADING`。

状态不是随手赋的，`_getState()` 有四条优先级（`fiber.ts:580-584`）：

```ts
private _getState() {
  if (this.uid === null) return FiberState.DISPOSED
  if (this._error) return FiberState.FAILED
  if (this._runner.epoch !== INACTIVE) return FiberState.ACTIVE
  return FiberState.PENDING
}
```

读法是从上往下：已销毁 > 有错误 > 有 epoch > 待定。`_error` 存在时，即使 epoch 齐全也不会回到 ACTIVE，只能通过 `update()` 清错。这条规则解释了「插件启动失败后为什么不会自己恢复」。

`LOADING` 与 `UNLOADING` 不在这条优先级里，它们由 `_setEpoch()` 显式设置（`fiber.ts:637-643`）：

```ts
this._updateState(() => {
  if (epoch === INACTIVE) {
    this.inertia = this._unload()
    return FiberState.UNLOADING
  }
  this.inertia = this._reload()
  return FiberState.LOADING
})
```

因此六态实际上分成两组：三个「判定得出」的稳定态（PENDING/ACTIVE/FAILED，加 DISPOSED），两个「迁移中」的过渡态（LOADING/UNLOADING）。过渡态存在的唯一目的是让 `await()` 有东西可等。

### epoch 是什么

`epoch` 是依赖指纹，`_refresh()` 用 `revision + ':' + 每个 inject 的 impl.uid` 拼成。任一依赖缺失，epoch 就是 `__INACTIVE__`。

于是「插件什么时候加载」变成一道纯函数题：依赖齐了 → epoch 非 INACTIVE → 装载；依赖缺了 → epoch 变 INACTIVE → 卸载。没有「启动顺序」这个概念，只有「依赖是否满足」。boot 里那些 `inject` 声明的意义就在这里：它们不是排版用的依赖列表，是 epoch 的输入。

## 3. effect：注册即副作用

`ctx.effect()` 是 Cordis 的中心原语。`ctx.on()`、`ctx.provide()`、`ctx.plugin()` 全都走它（`events.ts:257`、`reflect.ts:284`、`fiber.ts:280`）。

`effect(execute, label)`（`fiber.ts:421` 起）做四件事：

1. `assertActive()`：非 ACTIVE 直接拒绝；`UNLOADING` 时抛 `CordisError('INACTIVE_EFFECT')`，文案是 `cannot create effect on inactive context`。
2. 执行 `execute`，把返回值规整成清理器。
3. 把清理器连同 `EffectMeta { label, children }` 压入 `_disposables`。
4. 返回一个幂等的 `dispose`。

第 2 步接受五种形状（`_execute` 经 `safeCollect`）：函数、`null`/`undefined`、thenable、同步 iterator、async iterator。其他一律 `TypeError('Invalid effect')`。这个宽容度是为了让 `ctx.effect(() => { ... return () => {...} })`、`ctx.effect(async function* () {...})` 这类写法都能用。

第 4 步的幂等性有实现依据：重复调用时 `if (disposing) return disposalTask`，不会把同一个清理器跑两遍。这条不显眼，但它是 `dispose` 能被多处持有的前提，`AgentHandle.dispose` 是 capability，谁持有谁都能调，重复调用不出错。

{{< fig "diagrams/02-effect-tree.svg" "effect 树与逆序释放：外层先注册、内层后注册；拆除时从最内层开始，前一个返回 Promise 则链式等待。" >}}

### 逆序串行

```ts
// 清理器执行（fiber.ts 内，语义等价）
disposables.splice(0).reverse()
for (const dispose of reversed) await runDisposable(dispose)
```

三个词都要记住：逆序（后注册的先拆）、串行（前一个完成才下一个）、清空后再跑（`splice(0)` 先取走，避免清理过程中再注册的被卷进本轮）。

逆序的理由是依赖方向：内层 effect 通常依赖外层建立的东西（监听器依赖服务、子插件依赖父配置），先拆内层才不会拿到半死的依赖。

串行的理由是确定性。并行拆会快，但两个清理器若争同一份资源，错误顺序会产生「有时成功有时失败」的偶发。Cordis 选了可预测，代价是卸载变慢，深层插件树卸载是 O(深度 × 清理耗时)。

### 卸载时的顺序：依赖方先走

`_unload()`（`fiber.ts:712`）开头有一段等待：

```ts
// Dependents are notified when this fiber leaves ACTIVE, but may still
// access its services during their own teardown, so the effects of this
// fiber are only disposed after every dependent has unloaded.
while ([...this.provisions].some(impl => impl.dependents.size)) {
  await new Promise<void>((resolve) => this._release = resolve)
}
```

注释把理由写清楚了：依赖方在自己的 teardown 里还要访问这些服务，所以提供方的 effect 必须等所有依赖方卸载完才拆。

这形成一条与 effect 逆序不同的顺序规则：fiber 之间按依赖方向（消费者先、提供者后），fiber 内部按注册逆序。两条规则叠起来，才是完整的拆除顺序。

再看 `_unload()` 的后半段：清理器用 `Promise.all` 并发跑，每个都包在 `composeError` 里，失败只 `logger.error` 不抛出。也就是说卸载错误不会让卸载停下来。

然后对 `committed` 里每个 impl 做 `dependents.delete(this)`，最后一个依赖移除时调 `impl.release?.()` 与 `impl.ctx.fiber._release?.()`，这就是上面那个 `while` 等待的唤醒点。

### `await()` 等什么

```ts
async await() {
  while (this.inertia) await this.inertia
  if (this._error) throw this._error
  return this
}
```

等的是当前迁移（`inertia`），不是「等它变 ACTIVE」。装载失败后 `await()` 会抛出启动错误，这就是 boot 里能一眼看出「哪个插件起不来」的原因。

## 4. 服务：只在 ACTIVE 时可见

`Service` 基类（`service.ts:11`）做的事很少：构造器里存 ctx、算名字、处理 callable、然后 `ctx.reflect.provide(name, self, check)`。

```ts
constructor(protected ctx: Context, name: string) {
  ...
  self.ctx.reflect.provide(name, self, this[symbols.check])
  return self
}
```

`provide()` 走 `fiber.effect()`（`reflect.ts:284`），所以在 fiber 卸载时自动注销。没有任何手写注销代码，这是 DSH 里「注册即 effect」的具体兑现。

可见性在 `_getImpl()`（`reflect.ts:247-256`）：

```ts
_getImpl(name: string, strict = true) {
  const key = this.ctx[symbols.isolate][name]
  const impl = key && this.store[key]
  if (!impl) return
  if (strict && impl.ctx.fiber.state !== FiberState.ACTIVE) return
  return impl
}
```

三行判定，三个含义：

- 按 isolate 符号取：服务名先经 `ctx[symbols.isolate][name]` 映射，同名服务在不同作用域里是不同实现。DSH 的 agent scope 就建立在这个机制上，`agent.ctx` 是一个 isolate 过的子上下文。
- 缺就返回 undefined：不抛错。所以可选依赖可以写成 `ctx.get('userQuestions')` 而不必 `inject`。
- strict 时要求提供者 ACTIVE：这就是「服务只在提供者加载完成后可见」。它同时解释了「依赖缺失导致 epoch 变 INACTIVE 再触发卸载」的闭环：状态与可见性是同一件事的两种读法。

`set()` 有两个拒绝（`reflect.ts:265-280`）：未 provide 时抛 `cannot set property "X" without provide`；非提供者 fiber 调用抛 `cannot set property "X" in multiple fibers`。一个服务名只能被一根 fiber 写，其他 fiber 想改只能换实现或换 isolate。这条约束排除了「多个插件都以为自己拥有同一份状态」的经典事故。

{{< fig "diagrams/03-service-visibility.svg" "一个服务从 provide 到被读到：注册进 isolate 表、随 fiber 卸载注销；读取时按 isolate 取实现，再按提供者状态决定是否可见。" >}}

### 两种 get

| 写法 | 语义 | 用在哪 |
| --- | --- | --- |
| `inject: ['foo']` + `ctx.foo` | 强依赖：foo 缺失则 epoch INACTIVE，插件不加载 | 插件必须有这个服务才工作 |
| `ctx.get('foo')` | 可选：缺失返回 undefined，不阻塞加载 | 有则增强、无则降级 |

Cordis 没有「弱依赖自动重试」这种机制，可选依赖的语义就是「读到 undefined 自己处理」。DSH 里 `ctx.get('userQuestions')`、`ctx.get('spillStore')` 这类写法对应的都是「宿主没装这个能力时走别的路」。

## 5. 事件：五种派发，一种否决

`DispatchMode = 'emit' | 'parallel' | 'serial' | 'bail' | 'waterfall'`（`events.ts:31`）。它们共用一个 `_resolve()`，差别只在怎么跑回调。

| 模式 | 是否等 Promise | 停止条件 | 错误处理 | DSH 用在哪 |
| --- | --- | --- | --- | --- |
| `emit` | 不等 | 跑完 | 抛出即中断调用方 | `session/event`、状态广播 |
| `parallel` | 等全部 | 跑完 | 聚合成 `AggregateError` | 并行通知类扩展点 |
| `serial` | 逐个等 | 第一个 bail | 抛出即中断 | `agent/turn-stopping` |
| `bail` | 不等 | 第一个同步 bail | 抛出即中断 | 同步短路查询 |
| `waterfall` | 看实现 | 不调 `next()` 即否决 | 抛出即中断 | `tools/pre-execute`、`agent/pre-step` |

bail 的判据是一个小函数（`events.ts:12`）：

```ts
export function isBailed(value: any) {
  return value !== null && value !== false && value !== undefined
}
```

所以「返回 false」不算否决，「返回 undefined」也不算。只有给出非空值才算，这就是为什么 `PreToolDecision` 里弃权要写成返回 `allow` 而不是什么都不返回。

### waterfall 的真实语义

```ts
waterfall(...args) {
  const [thisArg, callbacks] = this._resolve('waterfall', args)
  const inner = args.pop()
  const dispatch = () => {
    const callback = callbacks.shift()
    if (!callback) return inner()
    let called = false
    const next = () => {
      if (called) throw new Error('next() called multiple times')
      called = true
      return dispatch()
    }
    return Reflect.apply(callback, thisArg, [...args, next])
    }
  return dispatch()
}
```

三个细节决定了它的行为：

1. 最后入参是 `inner`，即内建行为。链条跑完才落到它。
2. 监听器不调 `next()` 就直接返回：`dispatch()` 不会被继续调用，`inner()` 也不会被执行。这否决的不只是后面的监听器，连内建行为一起否决。
3. `next()` 只能调一次：第二次抛 `next() called multiple times`。这条挡住「先试一下再决定」的写法，否决必须在调用前决定。

DSH 的 AGENTS.md 里有一条「waterfall 监听器必须 `next()`」，这条是第 2 点的直接推论：忘记调 `next()` 会静默关掉内建行为，症状是「工具不执行了但没人报错」。

### 注册即 effect，顺序可指定

```ts
private register(label, name, callback, options) {
  const method = options.prepend ? 'unshift' : 'push'
  return this.ctx.fiber.effect(() => {
    const hooks = this._hooks[name] ??= []
    hooks[method]({ ctx: this.ctx, callback, ...options })
    return () => this.unregister(name, callback)
  }, label)
}
```

监听器注册本身是一个 effect：fiber 卸载时自动移除。`prepend: true` 用 `unshift` 插到队首，也就是在 waterfall 链里更外层。

这解释了 DSH 里 `{ prepend: true }` 的含义：表示「包在更外面，能决定别人是否被调用」，而不是「优先级高」。`packages/compaction/compaction-basic/src/index.ts:184` 给 `agent/request` 挂压缩时用 `prepend`，目的就是在其他人看到请求之前先判断要不要压。

### 过滤器与逐监听器隔离

`_resolve()` 还有两件事（`events.ts:162-175`）：

```ts
const filter = thisArg?.[Context.filter]
return [thisArg, (this._hooks[name] || [])
  .filter(hook => hook.global || !filter || filter.call(thisArg, hook.ctx))
  .map(hook => hook.callback)] as const
```

- 有 `Context.filter` 时，只有通过过滤的监听器被选中，这就是 scope 限定的底层：不同 agent 的监听器看到不同的事件。
- 非 `internal/` 事件先发 `internal/dispatch`，给观测层一个统一钩子。

另外 DSH 保留了 `dispatch()`（`events.ts:181-186`），返回逐个绑定的回调数组而不是一次性派发。理由在注释里：`per-listener error containment`，调用方要能单独包错误、单独计时。`tools` 的流水线就是用它来实现「一个监听器抛错不影响整批」的。

{{< fig "diagrams/04-dispatch-modes.svg" "五种派发方式与 waterfall 的两条硬规则：不调 next 即否决，next 只能调一次。" >}}

## 6. DSH 在这层之上加了什么

Cordis 提供容器语义，DSH 把它用成五条产品规则：

| DSH 规则 | 依赖的 Cordis 机制 |
| --- | --- |
| 一切皆插件，没有特权内核 | `ctx.plugin()` 是唯一装载入口 |
| 注册即可逆副作用 | 所有注册走 `fiber.effect` |
| 作用域只有两层，scoped 不继承给子 agent | `isolate` 符号表按对象同一性切分 |
| 服务只在提供者 ACTIVE 时可见 | `_getImpl` 的 strict 判定 |
| 卸载顺序可预测 | dependents-first + effect 逆序 |

反过来，Cordis 有几件事 DSH 刻意没用：`intercept` 的完整配置合成、`runtime` 的跨进程 fiber、`bail` 派发在关键路径上。取舍理由是 DSH 要的是可重放，拦截越多，同一个日志在不同组合下重建出的行为差异越大。

## 7. 设计取舍

| 取舍 | 选择 | 代价 |
| --- | --- | --- |
| 启动顺序 vs 依赖满足 | 用 epoch 指纹推导，不排启动序 | 无法表达「A 必须在 B 之后」这种非依赖时序 |
| 逆序串行 vs 并发卸载 | 逆序串行，保证确定性 | 深树卸载变慢，且顺序错误不会报错 |
| 卸载失败 | 只 `logger.error`，继续卸 | 泄漏是静默的，要靠 `getEffects()` 诊断 |
| strict 可见性 | 提供者非 ACTIVE 时读不到 | 装载窗口内读到 undefined，要写成可选依赖 |
| waterfall 否决 | 不调 `next()` 连内建行为一起否决 | 忘调 `next()` 是静默事故 |
| `next()` 一次 | 第二次抛错 | 不能「试探性调用」 |
| 单写者 | 一个服务名只能被一根 fiber 写 | 想共享可变状态必须走事件或存储 |

## 8. 踩坑点

1. 在 effect 之外注册。直接 `hooks.push(...)` 或把监听器塞进模块级数组，fiber 卸载时不会移除，热重载会叠加两份。
2. 在 `agent/created` 里 `await agent.whenIdle()`。创建还没完成就等自己的空闲，等于等一个永远不会开始的循环。仓库契约明写禁止。
3. 以为 `await fiber.await()` 会等成 ACTIVE。它等的是当前迁移；装载失败会抛错，卸载完成会正常返回。
4. 忘了 `_error` 不会被自动清除。装载失败后改配置要用 `update()`，光等没用。
5. 把 `prepend` 当优先级。它是「包在更外层」，能决定后面的人是否被调用。
6. 以为卸载错误会冒泡。`_unload()` 里清理器失败只记日志，症状是资源泄漏而不是异常。
7. 用 `ctx.get()` 判断服务在不在，然后长期持有返回值。提供者可能随后卸载，持有的是死引用；需要用的时候再读。
8. 在 waterfall 里先 `await next()` 再抛错。内建行为已经跑了，否决无效，要否决必须在调用前决定。
9. 假设 fiber 之间并行卸载。实际是依赖方先走，提供方最后；想在 teardown 里读下游状态，读不到是正常的。

## 9. 自己搭的时候

- 如果自己写容器，先做 effect，再做服务。effect 是唯一能保证「注册与撤销成对」的原语；没有它，任何注册都会变成泄漏源。
- 用依赖指纹代替启动顺序。`epoch = 依赖集合的哈希`，装载条件变成纯函数判定，能自然处理「依赖中途被换掉」的情况，也不需要维护一张启动序表。
- 把可见性绑到状态。服务只在提供者 ACTIVE 时可见，这一条能同时解决「半初始化对象被读到」和「依赖缺失触发重载」两个问题。
- 卸载顺序分两层定：fiber 之间按依赖反序，fiber 内部按注册反序。两条规则都要写进文档，否则实现者会各自发明。
- 让否决只需一次决定。`next()` 只能调一次是廉价的约束，但它把「试探后否决」这种难以推理的写法挡在门外。

## 10. 源码索引

| 位置 | 内容 |
| --- | --- |
| `vendor/cordis/src/fiber.ts:147-154` | `FiberState` 六态 |
| `vendor/cordis/src/fiber.ts:184-200` | `Fiber` 字段与 `dispose` 契约 |
| `vendor/cordis/src/fiber.ts:280-330` | 构造器顺序与 `internal/plugin` 通知 |
| `vendor/cordis/src/fiber.ts:421` | `effect()` 入口与 `INACTIVE_EFFECT` |
| `vendor/cordis/src/fiber.ts:580-584` | `_getState()` 四条优先级 |
| `vendor/cordis/src/fiber.ts:630-643` | `_setEpoch()` 与 LOADING/UNLOADING |
| `vendor/cordis/src/fiber.ts:681-710` | `_reload()` 与 dependents 登记 |
| `vendor/cordis/src/fiber.ts:712-745` | `_unload()` 等待依赖方、逆序清理 |
| `vendor/cordis/src/fiber.ts:760` | `await()` 等 inertia 并抛出启动错误 |
| `vendor/cordis/src/events.ts:12` | `isBailed()` 判据 |
| `vendor/cordis/src/events.ts:31` | `DispatchMode` 五值 |
| `vendor/cordis/src/events.ts:162-175` | `_resolve()` 过滤与 `internal/dispatch` |
| `vendor/cordis/src/events.ts:181` | `dispatch()` 逐监听器绑定 |
| `vendor/cordis/src/events.ts:191-217` | `parallel` / `emit` / `serial` |
| `vendor/cordis/src/events.ts:238-262` | `waterfall` 与 `next()` 一次 |
| `vendor/cordis/src/events.ts:257` | `register()` 走 `fiber.effect` |
| `vendor/cordis/src/service.ts:11-60` | `Service` 基类构造与符号表 |
| `vendor/cordis/src/reflect.ts:247-256` | `_getImpl()` strict ACTIVE |
| `vendor/cordis/src/reflect.ts:265-280` | `set()` 单写者约束 |
| `vendor/cordis/src/reflect.ts:284` | `provide()` 走 `fiber.effect` |
| `vendor/cordis/src/registry.ts:71` | inject 声明归一化 |
| `packages/core/agent-loop/src/index.ts` | DSH 侧如何用 `ctx.plugin` 装载 agent |
| `packages/core/scope/src/index.ts` | isolate 之上建立的两层作用域 |

## 11. 最后

Cordis 只有四个原语：fiber、effect、服务表、事件表。DSH 的五条产品规则全部由这四个原语的组合推出来，而不是另外发明的约束。

读这一篇的收益不在记住 API，而在获得一种判断力：看到 `ctx.on(...)` 时知道它有生命周期、看到 `ctx.get(...)` 时知道它可能返回 undefined、看到 `waterfall` 时知道不调 `next()` 会关掉内建行为。

系列导航：[第五季导览](/posts/dsh-season-five.html) · [三层组装](/posts/dsh-bundle-assembly.html) · [整体解剖](/posts/dsh-architecture-anatomy.html)
