---
title: "从零写一个 DSH 插件"
date: "2026-11-14T20:00:00+08:00"
summary: "一个插件从 package.json 声明到 ctx.effect 注册、再到 bundle 分发：两种插件形态、四个扩展点、三步本地装载与两条测试线。"
description: "以 dsh-time-context 为样板，拆 function 插件的 name/inject/Config/apply、配置校验、effect 回收与装载顺序。"
tags: ["harness", "codegen", "architecture"]
slug: "dsh-write-a-plugin"
---

> 依据：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`。样板代码取自 `packages/context/time-context/src/index.ts`，装载顺序取自 `docs/architecture.zh.md` 与 `docs/subsystems/extensions.zh.md`。

前两季反复提到「一切皆插件」，但没有给过一份可照抄的骨架。这一篇补上：写一个能装、能测、能分发的插件，需要哪几个文件、哪些导出、哪些约束。

## 0. 结论

- 插件只有两种形态：service 包（默认导出 class）和 function 插件（命名导出 `name`、`inject`、`Config`、`apply`）。混用会被 Loader 直接丢弃。
- 一个插件的最小契约是四个导出：`name` 用于诊断，`inject` 声明依赖，`Config` 做加载期校验，`apply` 里注册的一切都必须可回收。
- 注册即副作用。`ctx.effect()`、`ctx.on()` 返回的 disposer 决定卸载是否干净；没有 disposer 的注册会在热重载后留下幽灵监听器。
- 装载顺序是 profile → bundle → patch，patch 按 id 替换整条配置或插入新条目。插件作者只需要保证「被替换后能重建」。

{{< fig "diagrams/01-plugin-anatomy.svg" "一个 function 插件的四个导出与三条运行期注册：配置在加载期校验，注册在 apply 里发生，回收由 effect 负责。" >}}

## 1. 先选形态

| 形态 | 导出 | 适用 | 典型包 |
| --- | --- | --- | --- |
| service | `export default class X extends Service` | 提供新能力 seam，别的插件 `inject` 它 | `dsh-session`、`dsh-tools` |
| function | `name` / `inject` / `Config` / `apply` | 消费已有能力，挂扩展点 | `dsh-time-context`、`dsh-compaction-basic` |

判断方法：你要给别人 `ctx.xxx` 用，就写 service；你只是要在某个时机做事，就写 function。两者都能做工具注册，区别在于是否引入一个新的服务键。

`packages/AGENTS.md` 把这条写成硬规则：service 包默认导出 class；function 插件命名导出且不能有 default。原因是 Loader 按导出形状决定挂载方式，混用不会报错，插件会被静默跳过。

## 2. 四个导出

以 `time-context` 为例，去掉业务逻辑后的骨架是：

```ts
export const name = 'time-context'
export const inject = ['agents', 'sessionProjections']

export interface Config {
  timeZone?: string
  refreshIntervalMs?: number
}

export const Config: z<Config> = z.object({
  timeZone: z.string(),
  refreshIntervalMs: z.number(),
})

export function apply(ctx: Context, config: Config): void {
  ctx.effect(() => ctx.on('agent/pre-step', async function* (payload, next) {
    // ...
    return next()
  }))
}
```

四个导出各管一件事：

- `name`：Loader 诊断与 patch 定位用。重复会冲突。
- `inject`：声明必须存在的服务。缺一个，插件不加载，报错里带服务名。
- `Config`：schemastery schema，在加载期校验。非法值让插件加载失败，而不是运行到一半才炸。
- `apply`：唯一有副作用的地方。所有注册必须返回 disposer，或包在 `ctx.effect()` 里。

## 3. 挂在哪个扩展点

DSH 有三类扩展点，选错会写进死胡同：

{{< fig "diagrams/02-extension-points.svg" "三类扩展点：事件（观察/改写）、服务注册（提供能力）、会话事件（持久化）。选错类型的典型症状是「重启后状态没了」。" >}}

| 你想做 | 用 | 不要用 |
| --- | --- | --- |
| 每步注入上下文 | `agent/pre-step` waterfall | 改 system prompt 文本 |
| 工具调用前拦截 | `tools/pre-execute` / guard | 在工具里判断全局状态 |
| 新增一个工具 | `ctx.tools.register()` | 直接改工具注册表 |
| 记录可回放的状态 | 扩展 `SessionEventMap` | 内存 Map |
| 提供新能力 | service + `ctx.set()` | 全局单例 |
| UI 展示 | 会话事件 + 客户端投影 | 插件直接推 UI 消息 |

一个判断标准：模型可见的东西必须能从日志重建。如果你的状态会影响模型看到的内容，它就必须是会话事件；否则压缩、恢复、审计三件事会同时失真。

## 4. 一个真实插件的逐段拆解

`time-context` 做了四件事，每件都值得抄：

1. 类型合并：`declare module '@deepseek-ai/dsh-llm' { interface MessageSourceMap { 'time-context': ... } }`。给消息来源加一个可辨识的 `kind`，下游按 tag 分支，不用字符串比较。
2. 状态投影：用 zod 定义 `timeContextStateSchema`，把「上次注入时间」这类状态挂到会话投影上。投影可重建，进程重启不丢。
3. 节流：`refreshIntervalMs` 默认 600000，避免每步都写一条时间消息。配置项在 `Config` 里声明，加载期校验。
4. 决策不改写：监听器只追加消息，最终 `return next()`。waterfall 的规则是必须调用 `next()`，否则后续监听器与默认实现都被短路。

第 3 点是插件设计里最容易被忽略的：**可配置的阈值必须来自 Config，不能硬编码**。硬编码的阈值在别人机器上就是 bug。

## 5. package.json 与分发

function 插件的 `package.json` 需要三样东西：

```json
{
  "name": "@your-scope/dsh-my-plugin",
  "type": "module",
  "peerDependencies": {
    "@deepseek-ai/cordis": "workspace:~",
    "@deepseek-ai/dsh-agent": "workspace:*"
  },
  "dsh": { "bundle": "./dsh.bundle.yml" }
}
```

- `type: module`：全仓 ESM，没有例外。
- `peerDependencies`：Cordis 与宿主服务由宿主提供，不要打进产物。
- `dsh.bundle`：声明这是一个可被 profile 装载的组合包；没有它，`dsh plugin add` 找不到入口。

`dsh.bundle.yml` 里写的是 Cordis 配置项：插件 id、入口、默认 Config。profile 装载时按 id 合并，patch 可以按 id 替换整条。

{{< fig "diagrams/03-load-order.svg" "装载顺序：profile 选 bundle，bundle 给默认配置，patch 按 id 覆盖，Loader 最后实例化。任何一层都能把插件换掉。" >}}

## 6. 本地装载与调试

三步：

1. 在插件目录 `pnpm build`，确认 `lib/index.js` 存在。
2. 在 profile 的 `cordis.patch.yml` 里加一条：`id`、`entry`（包名或路径）、`config`。
3. 重启宿主，或让 host 的 HMR 重新加载配置。

调试优先看三处：Loader 的启动日志（插件是否被跳过）、`ctx.get('服务名')` 是否为空、以及 `agent/pre-step` 是否真的被调用（加一行临时日志，确认后删掉）。

常见失败：`inject` 写了但服务没启（插件静默不加载）、`Config` 校验过严导致默认配置非法（加载失败）、`apply` 里 `await` 了不该 await 的初始化（阻塞装载）。

## 7. 两条测试线

- **REAL-composition**：用真实 Loader 装载你的插件与它依赖的最小组合，断言注册生效、卸载后无残留。产品可见的插件必须有这条。
- **Loader smoke**：只验证 `name`/`inject`/`Config`/`apply` 的形状与加载顺序，防止「导出写错被静默丢弃」。

`packages/AGENTS.md` 还要求：注册表贡献必须证明 disposal。测试里显式 dispose 一次，再断言监听器数量归零。

## 8. 设计取舍

| 取舍 | 选择 | 代价 |
| --- | --- | --- |
| 配置校验时机 | 加载期 | 一个坏配置让整个 profile 起不来 |
| 扩展点粒度 | 事件 + 服务两类 | 想要「改一行行为」时缺少细粒度钩子 |
| 状态归属 | 会话事件优先 | 高频状态写日志会放大存储 |
| 分发单位 | bundle | 单插件无法独立安装，必须进组合 |

## 9. 踩坑点

1. 同时写 `export default` 和 `export const name`。Loader 会按 class 处理，你的 `apply` 永远不执行。
2. `inject` 漏写。插件不报错，只是不加载。
3. `ctx.on()` 不包 `ctx.effect()`。热重载后监听器翻倍。
4. 在 `apply` 里读全局单例。多 profile 并存时状态串台。
5. 把阈值写成常量。换一台机器就失效。
6. 用内存 Map 存「已注入过什么」。重启后重复注入。
7. 在 waterfall 监听器里忘记 `next()`。后续监听器全部被短路。
8. 直接改 `SessionEventMap` 之外的结构。压缩与恢复不认识你的字段。

## 10. 源码索引

| 主题 | 位置 |
| --- | --- |
| function 插件样板 | `packages/context/time-context/src/index.ts` |
| 插件规范 | `packages/AGENTS.md` |
| 装载与 patch | `docs/architecture.zh.md`、`docs/subsystems/extensions.zh.md` |
| service 样板 | `packages/core/tools/src/index.ts`、`packages/core/session/src/index.ts` |
| 工具注册 | `packages/core/tools/src/contracts.ts`、`docs/subsystems/tools.zh.md` |
| 测试工具 | `packages/test-support/`、`packages/host/loader-smoke` |

## 最后

写插件的门槛不在代码量，在四个约定：形态选对、依赖声明、注册可回收、状态可重建。四条都满足，插件就能在别人的 profile 里活下去。

系列导航：上一篇 [成本与安全](/posts/dsh-cost-and-safety.html)，下一篇：无（第三季完）。
