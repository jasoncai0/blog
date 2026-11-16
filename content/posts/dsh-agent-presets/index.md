---
title: "Agent preset：一个进程里的多种代理"
date: "2026-11-16T22:00:00+08:00"
summary: "同一 profile 下各会话可挂不同人格、工具与提示词。Agent preset 把组合写成 YAML，按修订挂进会话 scope，委派、fork、resume 时据此继承。"
description: "拆开 DSH 的 agent preset：与 harness profile 的分层、definition 到 generation 的挂载、model/tools/prompt/scope 各由谁决定、委派继承与显式覆盖、持久化位置，以及 fork 与 resume 的差异。"
tags: ["harness", "multi-agent", "architecture"]
slug: "dsh-agent-presets"
---
> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）。preset 的注册、挂载、选择与继承结论来自 `packages/preset/*`、`packages/api/session-controller`、`packages/subagent/*` 的源码和包内 README；装配顺序来自 `docs/architecture.md`；会话格式与迁移来自 `packages/session/*`。行号按该 commit 核对。本文没有启动产品做端到端复现，凡涉及运行时先后顺序的判断都标了对应的 await 点。

`dsh web` 起来之后，同一个进程里可以同时开几个会话：一个按标准模式写代码，一个只读文件做审查，一个切到 plan 模式先出方案。它们共享同一棵 Cordis 插件树、同一个数据库、同一组模型适配器，但各自看到的工具目录、人格段落和系统提示词并不相同。

这层差异由 agent preset 提供。它容易被当成 profile 的别名，也容易被当成「换一段 system prompt」的开关。两种理解都会在委派和恢复时出错。下面从声明、挂载、选择、继承、持久化五个角度拆开。

## 1. profile 与 preset：进程级和会话级

`docs/architecture.md:15` 把运行中的 `dsh` 描述为一棵启动时按序组装的插件树。`profile` 是 Harness home 里的命名组合，列出要叠的 bundle、树外插件和自己的 `cordis.patch.yml`；`web`、`headless`、`sdk`、`sdk-minimal`、`acp` 作为模板发布。

preset 不在这层。它是某条 patch 插入的一个普通插件行，模块名是 `@deepseek-ai/dsh-agent-preset`，配置里带 `id`、`name`、`order` 和 `plugins`。`plugins` 本身又是一份 Cordis entry list，由 registry 在一个会话 scope 下挂载。

| 维度 | harness profile | agent preset |
|---|---|---|
| 存放 | Harness home 的组合定义与 `cordis.patch.yml` | profile patch 里一条 `@deepseek-ai/dsh-agent-preset` 行 |
| 生效时机 | 启动，组装整棵插件树 | 会话创建、空白会话切换、cold resume 时 |
| 作用范围 | 进程内所有会话 | 绑定到该修订的会话及其继承子代理 |
| 数量 | 一个进程一份 | 每个会话一份，多份共存 |
| 定义改动的落点 | profile patch | profile patch 或 Web 编辑器写回的 patch |

web profile 默认插入了四个 preset：`standard`、`ptc`、`minimal`、`cordis`，registry 的配置是 `default: standard`（`packages/bundle/web-app/cordis.patch.yml:598`）。这四个 id 在 `agent-preset-registry/src/display.ts` 里有内置显示名；用户自建的 preset 不带 `name` 时按 id 原样显示。

| id | order | 组合特征 | 适合的场景 |
|---|---|---|---|
| `standard` | 1 | persona、完整文件与 shell 工具、planning、compaction、delegation 三个 group | 默认编码会话 |
| `ptc` | 2 | 与 standard 同构，多一行 `tool-executor-ptc` | 希望工具调用走 PTC 执行器的会话 |
| `minimal` | 3 | persona `complete: true`、不注入运行时上下文、文件系统只留 `readImage`、另挂压缩与持久终端 | 上下文预算紧、主要靠终端的任务 |
| `cordis` | 4 | 与 standard 相同的 persona，另加改 harness 自身的插件管理工具 | 调试或扩展 DSH 本身 |

这四个定义都来自 `packages/bundle/web-app/presets/` 下的独立 patch 文件，`cordis.patch.yml:598` 只负责插入 registry 和 `default: standard`。bundle 的 `package.json` 用 `dsh.bundle.patch` 列出这些文件，启动时按顺序叠上去。新增一个 shipped preset 就是加一个 patch 文件并登记，不改 registry 代码。

{{< fig "diagrams/01-profile-vs-preset.svg" "profile 在启动时铺开进程的插件树，preset 在会话开始时挂一棵带 scope 的子树。两个会话可以绑定同一个修订，也可以在定义改动后绑定到新修订。" >}}

有一条边界值得先记住：preset 换掉的是这个会话可见的注册集合，进程里的服务实例、数据库连接、模型适配器没有复制。它靠 scope 做路由，不靠起新进程做隔离。

## 2. 一个 preset 就是一份 config.plugins

`standard.patch.yml` 的开头就是完整形状。这里截掉中间的工具行，保留结构：

```yaml
- insert:
    - id: preset-standard
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: standard
        order: 1
        plugins:
          - id: persona
            name: '@deepseek-ai/dsh-persona'
            config:
              prefix: You are a coding agent powered by the {{model}} model.
          - id: tool-fs
            name: '@deepseek-ai/dsh-tool-fs'
            config:
              applyPatch: false
          - id: planning
            name: cordis:group
            group: true
            isolate:
              planMode: true
            config:
              - id: plan-mode
                name: '@deepseek-ai/dsh-plan-mode'
```

三个细节决定了它的行为。

| 行 | 在会话里留下什么 |
|---|---|
| `persona` | 系统提示词的前缀与后缀 section，按挂载 scope 过滤 |
| `agent-instructions` | 读仓库说明文件并注入，超过 `maxBytes` 截断 |
| `time-context` | 当前时间段落 |
| `tool-fs`、`tool-fs-search` | 文件读写与搜索工具，配置决定具体暴露哪几个 |
| `planning` group | plan 模式服务与退出工具，状态写进会话投影 |
| `compaction` group | 压缩命令、压缩服务与工具结果裁剪器 |
| `delegation` group | `subagent`、`subagent_fork`、workflow 工具与 workflow 引擎 |

`tool-bash` 与 `tool-pwsh` 用 `!!js` 表达式按平台禁用，`tool-fs` 用 `applyPatch: false` 省掉一个工具，`minimal` 则把 `tool-fs` 的 `read`、`write`、`edit` 全部关掉，只留 `readImage`。preset 的能力差异大多落在这些行级配置上，不需要为每个变体写新插件。

`plugins` 的 schema 是 `z.array(z.any()).required()`（`agent-preset/src/index.ts:22`）。registry 只校验这是一个数组；每一行的模块名、配置、group 结构由 Loader 的 entry 方言接手。拼错模块名的行不会在启动时报错，它会在 `activate()` 里失败，被记进 `record.broken`。

`isolate` 不是展示字段。带服务状态的行放进 group 并声明 `isolate`，挂载时才会为这个 group 建一个私有 realm。`mount.ts:213` 在挂载末尾检查泄漏：如果一个服务实现写进了根 realm 的 slot，整次挂载被拒绝，错误是 `Preset services require isolate realms: <names>`。

`preset` 类型的 patch 不是另一套 YAML 方言。`docs/architecture.md:25` 写明，这种 patch 的 insert、replace、disable 作用在目标声明的 `config.plugins` 内部，优先级与普通 patch 相同。profile 编辑器保存的修改也落在这里。

## 3. 从 definition 到 generation

registry 的 `register()` 收到 definition 后做三件事：查重、建 scope、挂树（`agent-preset-registry/src/index.ts:97`）。挂载成功的产物是一个 `Generation`，不是一条只带 id 的记录：

```ts
interface Generation {
  scope: Scope
  key: ScopeKey
  mount: PresetMount
  users: number
  retired: boolean
}
```

`key` 是这次挂载的 scope key，`mount.tree` 是那棵实际运行的 EntryTree。同一个 preset 被重新注册时，旧 generation 标 `retired`，新 generation 立即生效；旧的那棵不会马上销毁，`collect()` 只在 `retired && users === 0` 时 dispose。

`users` 由两条路径增加。`mount()` 先 `retain()` 拿修订，再 `bind()` 把 agent scope 的父指针接到 generation 的 key 上，最后把自己那一份计数还回去：

```ts
async mount(ctx: Context, id?: string): Promise<AgentPreset> {
  const generation = await this.retain(id)
  try {
    await this.bind(ctx, generation)
    return { id: generation.mount.presetId }
  } finally {
    generation.users--
    await this.collect(generation)
  }
}
```

`bind()` 遇到已绑定的 scope 时走 `rebind`，否则 `join()`。`join()` 调用 `bindScopeParent(key, generation.key)`，再用 `ctx.effect` 注册清理。父指针一接上，`scopeChainOf` 就能从这个 agent 一路走到 generation 的 key，工具和提示词的注册过滤都靠这条链。

失败路径也留了诊断位。`activate()` 先 `createScope`，再校验 entry list、挂树；任何一步抛错都把消息写进 `record.broken`，然后 dispose 这个 scope。definition 仍留在列表里，`list()` 能读到失败原因。挂载时等待 Host 服务的行不算失败：`auditRows()` 把它们列进 pending，`diagnostic()` 在下一次读取时 `await this.owner.loader.await()`，再审计一次。

```ts
private join(ctx: Context, key: ScopeKey, generation: Generation): void {
  const binding = { parent: bindScopeParent(key, generation.key), generation }
  generation.users++
  this.bindings.set(key, binding)
  ctx.effect(() => async () => {
    this.bindings.delete(key)
    binding.generation.users--
    await this.collect(binding.generation)
  }, 'agent-preset.binding')
}
```

持有修订的是这个 binding，不是 `mount()` 的临时计数。`bindScopeParent` 只允许绑一次，重绑必须走它返回的 `rebind`；这样 scope 链不会在运行时被悄悄改指到另一棵树。

`PresetTree` 继承 `EntryTree`，只重写了一个方法：

```ts
override commit(_change: EntryChange): void {}
```

运行时树不落盘。定义的正本在 profile patch 里，registry 每次启动按当前 YAML 重新挂载。理解这一点之后，「改 preset 为什么没热更」和「重启后为什么变了」都能解释。

{{< fig "diagrams/02-preset-resolution.svg" "一次创建或恢复里，preset 从请求 id 走到已挂载 generation 的路径。resolve 只读当前定义，mount 才建立 scope 绑定；两步都可能因为定义损坏而失败。" >}}

## 4. model、tools、prompt 分别由谁决定

这三个东西经常被混成一句「preset 配了 agent」。拆开看，归属并不一样。

模型不在 preset 里。`AgentOptions` 只有 provider、model、reasoningEffort、maxTokens（`packages/core/agent/src/runtime-types.ts:32`）。session-controller 的 `agentOptions()` 返回一组 getter，读的是 `agentDefaultModel.currentSelection()`；`installModelSelection()` 只在每个 step 前比较本步选定路由与上次请求头，不同就往消息流补一条 `[model changed: ...]` 通知（`packages/core/agent/src/model-selection.ts:157`）。

preset 影响的是模型选择的外围能力。standard 的 delegation group 里，`tool-subagent` 行打开了 `modelSelection: true` 和 `modelDiscovery: true`，父代理才能给子代理指定模型。persona 的 `{{model}}` 是请求时变量，`agent-loop/src/index.ts:337` 从 `context.modelSelection?.model ?? context.agent?.options.model` 取值，所以同一段 persona 在不同路由下渲染出不同文本。

工具由 preset 决定。每个工具行在挂载 scope 里调用 `ctx.tools.register()`，注册记录带着 `scopeOf(this.ctx)`；执行时工具目录按 agent 的 scope 链过滤。两个会话可以各自有一份 `tool-fs`，行 id 相同，注册表里的条目不同。

提示词同理。persona 行注册 `PERSONA_PREFIX_SECTION` 与 `PERSONA_SUFFIX_SECTION`，minimal preset 把 `complete` 设为 true，前缀成为完整系统提示词并压掉其他 section；`includeRuntimeContext` 控制是否附带运行时上下文。`agent-instructions` 的 `maxBytes: 65536`、`time-context`、plan-mode 的大段说明，都是随行挂载的 section。

scope 是这四者的共同底座。`createScope()` 返回一个带 `[kScope]` 标记的 ctx（`packages/core/scope/src/index.ts:137`），`scopeTarget()` 构造一个只转发本 scope 及其祖先事件的路由器（`:170`）。工具、提示词、命令都挂在这个路由器后面。系统提示词的 section 可见性也按同一条链过滤：

| section | 来源 | 生效条件 |
|---|---|---|
| `deployment:persona-prefix` | persona 的 `prefix` | 挂载了 persona 行 |
| `deployment:persona-suffix` | persona 的 `suffix` | `complete` 为 false |
| `agent-instructions` | 仓库说明文件 | 挂载了该行 |
| 时间段落 | `time-context` | 挂载了该行 |
| plan 模式说明 | `plan-mode` 的 `section` | 挂载了 planning group |
| `subagent:delegation` | 子代理覆盖 | 当前会话是子代理 |

`complete: true` 会把前缀提升为完整系统提示词，压掉后缀和其他所有 section；`includeRuntimeContext: false` 则关掉动态运行时上下文。minimal preset 两个都用了，所以它的提示词只有一条前缀加工具定义。这段行为写在 persona 的 `Config` 注释里，不是靠 section 名字推断的。

工具那边还有一层目录稳定性约束。plan 模式要求工具目录在模式切换前后保持一致，`plan-mode` 的提示词明确说「The tool catalog stays the same across modes for request-cache stability」，靠提示词禁止写操作，而不是把写工具摘掉。preset 换工具会换掉请求的 schema 前缀，只能在会话开始前做；会话中途改工具目录会打断请求缓存。

这也解释了为什么同一修订可以被多个会话共享：插件实例是共享的，agent 级的可变状态放在会话日志和投影里。plan-mode 的 active 就是一个投影，服务本身只负责读写当前会话的状态。

## 5. 选 preset 的三条路径

请求进入 session-controller 后，preset 的选择分三种情况，行为不同。

| 路径 | 入口 | preset 来源 | 会话日志 |
|---|---|---|---|
| 新建 | `createSession` | 请求里的 `agentPreset`，缺省用 registry 的 `defaultId` | 写 header 的 `agentPreset` |
| 复用空白会话 | `reuseLive` / `reuseSetup` | 请求值，缺省沿用记录值 | 值变化时追加 `agent-preset/selected` |
| 恢复已存会话 | `resumeObserved` | 投影里的当前值，缺失回退 header | 不追加事件 |

新建路径在 `composeAgent(presetId)` 里完成解析和装配（`agent.ts:261`）。它先 `presets.resolve(presetId)` 拿到当前定义的 id，再返回一个 setup 回调：`installModelSelection(agentCtx)`，然后 `presets.mount(agentCtx, resolvedId)`。setup 在 Agent 发布前执行，挂载失败就不会有半成品会话。

空白的判定是 `boundary.openTurnStartSeq === null && boundary.lastTurn === 0`（`:417`）。只有这种会话允许 `select()` 换 preset。`select()` 按 `agent.id` 串行化，检查空白，比较当前修订，需要时 `recompose()` 并追加事件（`agent-preset-registry/src/index.ts:365`）。已经跑过 turn 的会话会收到 `agent-preset/locked`，消息是 `This session has already started`。

API 层对这个错误的处理值得注意：`reuseLive` 只捕获 `agent-preset/locked`，然后返回 undefined，调用方转去 `createMinted()`（`:383`）。客户端请求的是「用这个会话 id 和这个 preset」，最后拿到一个新 id。这不是静默失败，但调用方必须读返回值里的 `sessionId`。

复用路径还有第二次空白检查。`reuseSetup()` 在拿到写者之后重新判一次，然后装配置、补事件：

```ts
private reuseSetup(composition: ApiSessionComposition): AgentSetup {
  return async (agentCtx, agent) => {
    if (!this.isBlank(agent.session)) throw new ApiSessionStartedSinceObserved()
    const commit = await composition.setup(agentCtx, agent)
    const target = composition.agentPreset
    if (target !== undefined && this.presetForSession(agent.session) !== target) {
      agent.session.append('agent-preset/selected', { agentPreset: target })
    }
    return commit
  }
}
```

先检查再等写者会留下竞态：观察时空白，拿到写者时已经有人开了 turn。所以检查放在 setup 内部，失败抛 `ApiSessionStartedSinceObserved`，调用方退回新建。`assertPresetApplicable()` 处理另一种情况：宿主没有 registry，请求却点名了 preset，直接抛 `ApiSessionPresetConflict`，不会悄悄用默认组合。

| 错误 | 触发点 | 调用方要做什么 |
|---|---|---|
| `agent-preset/not-found` | `resolve()` 或 `retain()` 找不到 id | 重新列 roster 选一个 |
| `agent-preset/invalid` | 挂载失败或审计不过 | 修 YAML 或插件，再重试 |
| `agent-preset/locked` | 已开始的会话调 `select()` | 新建会话 |
| `ApiSessionPresetConflict` | 宿主没有 registry，请求却点名 preset | 去掉参数或安装 registry |
| `PRESET_UNAVAILABLE` | 子代理指定 preset，宿主没有服务 | 改用继承或安装服务 |

`select()` 的提前返回也有条件：当前 generation 一致，且投影里的 id 已经是目标值，才算无操作。否则即使 id 相同，定义改过也会触发重新挂载和 `tools/change`。

## 6. 委派时的继承与覆盖

子代理的 preset 有三个来源，优先级从高到低。

| 维度 | 默认继承父修订 | 显式指定 `agentPreset` |
|---|---|---|
| 工具注册 | 父会话同一份 | 按 id 取当前修订，通常仍是同一棵 |
| 提示词 section | 父的注册，再叠加子覆盖 | 新定义的 section，再叠加子覆盖 |
| 隔离服务实例 | 与父共用 | 同一修订时共用，换 id 或修订才分开 |
| 定义修订 | 固定为父当前修订 | 当前最新修订 |
| 模型 | 不继承 | 不继承 |

权限不在 preset 里。子会话的 delegated policy 从自己的日志重放，预设只决定工具目录和提示词；`tools.restrict()` 只能收窄，不能借 preset 放宽。

显式指定。`tool-subagent` 的 `agentPreset` 参数只对 spawn provider 有效。子代理创建时，`applyChildComposition()` 看到 `composition.agentPreset` 就调用 `presets.mount(childCtx, id)`，然后应用覆盖（`child-agent.ts:187`）。这条路径要求宿主注册了 `agent-presets` 服务，否则抛 `SubagentError('selecting a child agent preset requires the agent-presets service', 'PRESET_UNAVAILABLE')`。`allowedAgentPresets` 不配就是不限制；配了之后，工具 schema 只列白名单里的 id，配置里的 `agentPreset` 也必须在白名单内。

继承父修订。没有显式值时，`composeFrom(childCtx, parent.ctx)` 找到父绑定的 generation，把子 scope 的父指针直接接到同一个 key（`agent-preset-registry/src/index.ts:313`）。这里不重新 mount，子代理看到的是父会话此刻运行的那一份工具和 section，连修订号都相同。父没有绑定而 `composedPreset(parent)` 非空时，抛 `Parent preset revision is unavailable`。

没有 preset 的宿主。两条路径都落空时，子代理沿用宿主组合，不再挂 preset 树。

覆盖发生在挂载之后。`applyChildOverrides()` 注册 `'subagent:delegation'` 提示词段落，内容是权限范围在启动时固定、不能从会话内放宽；旧会话还带 persona 段时补一个历史 section；有工具过滤时调用 `tools.restrict(toolFilter)`（`child-agent.ts:205`）。这些覆盖只影响子 scope，不回写父的注册。

```ts
function applyChildOverrides(childCtx: Context, composition: ChildComposition): void {
  childCtx.systemPrompt.context({
    name: 'subagent:delegation',
    order: childCtx.systemPrompt.getContextOrder('SUBAGENT_DELEGATION'),
    text: SUBAGENT_DELEGATION_CONTEXT,
  })
  if (composition.persona !== undefined) {
    childCtx.systemPrompt.section({ name: 'deployment:persona-prefix', text: composition.persona /* 省略 order */ })
  }
  if (composition.toolFilter !== undefined) childCtx.tools.restrict(composition.toolFilter)
}
```

三处覆盖都写在子 scope 的注册表上：一个 prompt context、一个历史 persona section、一次工具收窄。父会话的注册不受影响。`toolFilter` 来自旧版参数，新版工具已经不再接受它，改为在 preset 里裁剪工具行。

fork 和 spawn 在这一点上不同。fork 是 session-controller 的命令（`commands.ts:276`），它读源会话的投影拿 preset，然后 `agents.create()` 建一个 seeded 子会话。spawn 是子代理工具的 provider，子会话带 `origin: 'subagent'` 和 `delegationDepth`。两者都会在 header 里写 `agentPreset`。fork 的 header 与继承相关的字段只有 `isSeeded` 和 `agentPreset`；spawn 的子会话由 `childSessionMeta` 写入 `parentSession`、`origin: 'subagent'` 和 `delegationDepth`。

{{< fig "diagrams/03-delegation.svg" "子代理拿 preset 的三条路：默认继承接到父的同一修订，显式指定按该 id 的当前修订解析，没有服务时回退宿主组合。覆盖只发生在子 scope。" >}}

自己搭的时候，如果希望子代理看到父会话完全一样的工具实例，默认继承比显式指定更稳；显式指定会按该 id 的当前修订重新解析，定义已经更新时子代理会落到新的那棵树上，挂载期初始化的状态不会跟着父走。

## 7. 持久化：四个位置，各存各的

preset 相关的事实分散在四个位置，任何一个缺失都会在恢复时变成错误。

第一处是 profile patch。`preset-standard` 这一行和它的 `config.plugins` 是定义的正本。Web 编辑器保存修改时也写这里，写的是同 id 行的整段 `config.plugins`。`plugin-manager` 在被更高优先级层覆盖时会返回一句 `Saved, but a higher-priority layer such as the agent preset overrides it`（`packages/boot/plugin-manager/src/tools.ts:63`），说明同一个行 id 可以在 profile 和 preset 里各有一份，后者生效。

第二处是会话 header 的 `agentPreset`（`packages/session/session-format/src/types.ts:25`）。它是创建事实，只在建会话时写一次。当前会话格式版本是 4（`packages/core/session/src/types.ts:90`），header 的形状大致是：

```json
{
  "version": 4,
  "id": "session-8a1f…",
  "createdAt": 1763300000000,
  "cwd": "/Users/me/repo",
  "agentPreset": "standard"
}
```

第三处是 `agent-preset/selected` 事件。它被标为 log-only，注释写得很直白：记录后续 turn 实际运行的组合，让恢复或 fork 重建同一个，而不是回到 header 的创建值。事件本体只有 id：

```json
{
  "type": "agent-preset/selected",
  "seq": 12,
  "time": 1763300060000,
  "data": { "agentPreset": "ptc" }
}
```

投影的定义在 `agent-preset-registry/src/session.ts:37`，`init` 读 header，`apply` 只认这个事件，`stateVersion: 1`。`wire.view` 把状态原样交给客户端，所以 Web 端读 `projectionValues.agentPreset` 就能显示当前组合，不需要自己重放日志。

第四处是内存里的 generation。它不落盘，重启后按当前定义重建。`inspectCompositions()` 能看到当前保留的修订和隔离诊断，看不到历史修订。

格式迁移碰过这个字段。v2 到 v3 的迁移把 preset id 字面量 `code` 改成 `ptc`，header 和每一条 `agent-preset/selected` 都改（`session-format-v2-to-v3/src/migration.ts:18`、`:142`）。升级前如果有自定义 preset 正好叫 `code`，会被一起改名。

{{< fig "diagrams/04-persistence.svg" "定义、创建事实、切换记录、运行时修订各在一个位置。恢复读投影，fork 读源会话投影，运行时树重启后按当前 YAML 重建。" >}}

## 8. fork 与 resume：同一个 id，不同的修订

fork 的子会话继承源会话的 preset id，但不继承模型选择。`commands.ts:276` 先 `composeAgent(presetForObservation(source))`，再 `agents.create()`；`agentOptions` 传的是 `{ provider, model }`，值来自当前 `agentDefaultModel.currentSelection()`，不是源会话请求头里的路由。想让 fork 出来的会话继续用源模型，调用方要自己选一次。

| 行为 | fork | resume |
|---|---|---|
| preset id 来源 | 源会话投影 | 自己的投影 |
| 装配方式 | `composeAgent` 后 `agents.create` | `composeAgent` 后 `agents.resume` |
| header 写入 | `isSeeded`、`agentPreset` | 沿用原 header |
| 模型 | 当前默认路由 | 当前默认路由 |
| 定义修订 | 当前定义 | 当前定义 |

resume 读的是投影。`resumeObserved()` 用 `presetForObservation(observation)` 得到当前 id，再走同一条 `composeAgent` 装配路径（`agent.ts:298`、`:308`）。header 里的创建值只在投影缺失时作为回退。

### headless 与没有 roster 的宿主

headless bundle 不组合 preset roster，它碰到的已存会话可能记录过 preset。收养前先重建当前 id：

```ts
function currentPreset(header, events, sessionId) {
  let preset = header.agentPreset
  for (const event of events) {
    if (event.type !== 'agent-preset/selected') continue
    const selected = event.data?.agentPreset
    if (typeof selected !== 'string' || selected === '') {
      throw new Error(`session "${sessionId}" records a malformed agent-preset/selected event and cannot be adopted`)
    }
    preset = selected
  }
  return preset
}
```

payload 坏掉时选择报错，不按「没有 preset」继续。接着 `assertAdoptable()` 拒绝收养任何带 preset 的会话：

```text
session "<id>" runs under agent preset "standard", which the one-shot runner does not compose
```

这条拒绝是有意设计的。headless 用自己的工具和提示词跑，如果静默接管一个有 preset 的会话，模型会在错误的工具目录下继续对话，日志里却看不出来。想跑这类会话，要用组合了相同 preset 的宿主。

子代理的冷恢复更严格。`restoreTarget()` 先看投影；投影为空但日志里出现过 `agent-preset/selected`，直接拒绝恢复：

```ts
throw new SubagentError(
  `subagent "${childId}" has no agent preset projection for restoration`,
  'NOT_RESUMABLE',
)
```

投影和 header 都没有时，子代理用宿主组合加历史 descriptor 里的 persona 与工具限制。包 README 的「Restoring child composition」还写了一句关键约束：恢复使用当前注册的定义，不保留重启前的旧修订。

所以「同一个 preset」在 fork 和 resume 里指 id，不指修订。定义改过之后恢复，挂载的是新定义；正在运行的旧会话仍留在旧 generation 上，直到最后一个绑定释放。这个差异在排查「恢复后工具少了/多了」时最先要确认。

## 9. 设计取舍

preset 用「一棵可挂载的子树」而不是「一组布尔开关」。代价是定义必须写成 Cordis entry list，收益是新增能力不用改 preset 代码，插一行就能进组合。工具、提示词、命令、服务走同一套挂载路径，不需要为 preset 维护第二套注册协议。

修订和 id 分离。`generation` 让运行中的会话对定义改动免疫，也让子代理能精确继承父的那一份。代价是内存里可能同时存在多棵相似的树，诊断时要按 generation 看，不能只看 id。

服务行要求 `isolate`。这把「一个会话一份服务状态」变成显式声明，挂载期检查泄漏，错了直接失败。代价是写 preset 的人必须知道哪些行有状态，不能无脑平铺。

日志里记切换，不记重挂。`agent-preset/selected` 只有 id，定义内容在 YAML 里。好处是日志小、可迁移；代价是历史会话无法还原当时的精确工具集，只能按当前定义重建。包 README 明确接受这个代价。

### 自己搭的时候

自己搭的时候，先把 preset 当成「会话的能力清单」，不要当成「模型的配置」。模型选择留在 `AgentOptions` 和默认路由层，preset 只声明这个 agent 有没有选模型的工具。

自己搭的时候，服务行一律先想清楚状态属于会话还是属于进程。属于会话的，放进 `isolate` group 并从投影读状态；属于进程的，留在 profile 层，别塞进 preset。

自己搭的时候，给 `allowedAgentPresets` 一个白名单。默认不限制意味着模型可以点名任何已注册 preset，包括你没打算暴露给委派的那个。

## 10. 踩坑点

1. preset 行只校验数组形状。模块名拼错时启动不报错，`activate()` 失败后定义进入 `broken`，创建会话才收到 `agent-preset/invalid`。排查先看 registry 的 `list()`，不要只看 YAML。

2. 有状态的服务行不加 `isolate`，挂载直接失败，错误是 `Preset services require isolate realms: <names>`。这个检查在 `mountPreset` 末尾，前面的行已经挂上了，失败会整棵回滚。

3. 已开始的会话切 preset 会得到 `agent-preset/locked`。API 层把它转成「另开一个会话」，客户端拿到的新 `sessionId` 与请求值不同。调用方不读返回值就会把消息发到旧会话。

4. 投影和 header 可能不一致。header 是创建事实，事件是后续切换；只读 header 的代码在切换过的会话上会拿到旧 id。恢复路径统一读投影。

5. 子代理恢复要求投影存在。日志里有 `agent-preset/selected` 但投影为空时，恢复抛 `NOT_RESUMABLE`。升级或手写日志后出现这个错误，先查投影折叠是否跑过。

6. 定义改动不热更已绑定会话。旧 generation 标 `retired`，`users` 不清零就不 dispose。看到「改了没生效」，先确认这个会话是不是还绑在旧修订上。

7. v2 到 v3 迁移会把 id `code` 改成 `ptc`，header 和事件都改。自定义 preset 叫 `code` 会在升级后被改名。

8. fork 不复制模型。子会话用当前默认路由；要复现源会话的模型，调用方得在 fork 请求里显式选。

## 11. 源码索引

| 位置 | 内容 |
|---|---|
| `packages/preset/agent-preset-registry/src/index.ts:54` | `AgentPresetRegistry`，注入 loader、sessionProjections、agents |
| `packages/preset/agent-preset-registry/src/index.ts:97` | `register()`：查重、建 scope、activate、返回反注册函数 |
| `packages/preset/agent-preset-registry/src/index.ts:119` | `activate()`：校验 entry list、挂树、失败记 `broken` |
| `packages/preset/agent-preset-registry/src/index.ts:248` | `retain()`：解析当前修订，抛 not-found 与 invalid |
| `packages/preset/agent-preset-registry/src/index.ts:297` | `mount()`：retain、bind、释放临时计数 |
| `packages/preset/agent-preset-registry/src/index.ts:313` | `composeFrom()`：子代理接入父的精确修订 |
| `packages/preset/agent-preset-registry/src/index.ts:365` | `select()`：空白检查、按 agent 串行、追加事件 |
| `packages/preset/agent-preset-registry/src/mount.ts:204` | `mountPreset()`：挂载、审计、泄漏检查 |
| `packages/preset/agent-preset-registry/src/session.ts:37` | `agentPreset` 投影定义 |
| `packages/preset/agent-preset/src/index.ts:15` | preset 行类型与 `EntryGroup.key` |
| `packages/preset/persona/src/index.ts:63` | persona 在挂载 scope 注册前后缀 section |
| `packages/core/scope/src/index.ts:137` | `createScope()` |
| `packages/core/scope/src/index.ts:170` | `scopeTarget()`：事件只向祖先流动 |
| `packages/api/session-controller/src/agent.ts:261` | `composeAgent()`：resolve 加 setup |
| `packages/api/session-controller/src/agent.ts:383` | `reuseLive()`：locked 时退回新建 |
| `packages/api/session-controller/src/commands.ts:276` | fork 从源投影组合 preset |
| `packages/subagent/subagent/src/child-agent.ts:187` | 子代理组合：显式挂载或继承父修订 |
| `packages/subagent/subagent/src/manager.ts:1218` | 冷恢复读投影，缺失则拒绝 |
| `packages/session/session-format/src/types.ts:25` | header 的 `agentPreset` 字段 |
| `docs/architecture.md:25` | 分层顺序与 preset patch 的作用域 |

## 12. 排障顺序

preset 的问题大多能压成四步：定义有没有挂上、会话绑的是哪个修订、日志里记了什么、客户端有没有收到变化。顺序反过来查会浪费很多时间。

先看定义。`presets.list()` 的每一行带 `id`、`order`，挂载失败时多一个 `broken` 字段，内容是 `auditRows()` 拼出来的行级原因。`resolve(id)` 也会带上同一个字段。看到 `broken` 就不要继续查会话，先去修 YAML。

再看修订。`inspectCompositions(agent.ctx)` 返回这个 agent 绑定的 generation：`id`、`modules`（实际加载的模块）、`leakedServices`（泄漏进根 realm 的服务）。不传 ctx 时返回全部保留修订，可以对照是不是绑在了旧的那棵上。

然后对日志。投影 `agentPreset` 是当前值，header 是创建值，两者不同说明中间发生过 `agent-preset/selected`。日志里事件存在但投影为空，是恢复报 `NOT_RESUMABLE` 的常见原因。

最后看客户端。`ui-commands` 收到 `agent-preset/selected` 会调用 `directory.resetSession(sessionId)`，`ui-skill` 会让技能目录失效。切换后界面没更新，先确认这两个消费者有没有收到事件，再查 UI。

## 13. 最后

如果只记一件事：preset 是「挂在会话 scope 下的一棵插件子树」，id 只是索引，实际生效的是 generation。理解这一点，选择、继承、持久化、恢复都能顺着 scope 链推出来。想继续看子代理的完整生命周期，可以读《[子代理：从一次工具调用到独立会话](/posts/dsh-subagent-lifecycle.html)》；想先看 profile 与 bundle 的分层，读《[宿主组合](/posts/dsh-host-composition.html)》。

自己搭的时候，建议先写一个最小 preset：一个 persona 行、一个工具行，跑通创建和恢复，再加入 group 和 isolate。把持久化路径先固定下来，比一次写全四个 preset 更省时间。
