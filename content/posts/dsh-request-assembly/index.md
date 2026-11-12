---
title: "请求是怎么装出来的：信封、提示词与工具清单"
date: "2026-11-11T12:00:00+08:00"
summary: "DSH 把模型请求拆成信封、派生历史、工具 schema 与运行时上下文四块，每块都有自己的日志与失效规则。"
description: "拆解 request/header 的规范形式与四种 reason、system prompt 的协作式组装与两种历史路由、每步一次的工具准入，以及它们对 prompt cache 的影响。"
tags: ["architecture", "context-engineering", "agent"]
slug: "dsh-request-assembly"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。行号以该 commit 为准，引用一律指向 `src/`。
> 依据：`packages/core/agent-loop/src/agent.ts:613-744`（`prepareRequest` / `buildRequest`）、`packages/core/agent-loop/src/runtime-context.ts`、`packages/core/system-prompt/src/index.ts:296`（`renderPrompt`）、`docs/subsystems/system-prompt.zh.md`、`docs/subsystems/tools.zh.md`。

一个 LLM 请求看起来只有几个字段，放在 Agent 里，每个字段都是一条决策链的终点：

- `messages` 从哪来？不能是「上次的数组 + 这次的增量」。
- `tools` 谁决定？同一时刻不同 agent 看到的工具可以不同。
- `system` 放哪？它变了，是替换还是追加？
- `temperature`、`max_tokens` 谁说了算？重试时还是同一套吗？

DSH 把请求拆成四块，每块有自己的真源和失效规则。

## 1. 请求的七个输入

`buildRequest()` 产出的请求对象长这样：

| 字段 | 来源 | 变化频率 |
| --- | --- | --- |
| `header.config` | `canonicalHeader` + 适配器默认值 | 路由/采样变化时 |
| `header.tools` | 本步 `toolSet` 的 schema | 工具集变化时 |
| `header.adapterDefaults` | 适配器实际生效的默认值 | 路由变化时 |
| `messages` | `session.deriveMessages()` 冻结副本 | 每次请求 |
| `toolHistory` | 已发生的工具调用摘要 | 每次请求 |
| `sessionId` | 会话 id | 固定 |
| `signal` | 当前 turn 的 AbortSignal | 固定 |

{{< fig "diagrams/01-request-anatomy.svg" "一次请求的四个来源：信封来自 request/header 快照，消息来自日志投影，工具清单来自本步准入集合，运行时上下文来自组装结果。四者都先变成日志事件，再被组装进请求。" >}}

`system` 不在这张表里。这是刻意的，第 3 节解释。

## 2. 信封：把「请求本身」也记进日志

`request/header` 是 log-only 事件（没有 `surfaceOp`），data 是一份完整快照，不是增量：

```ts
interface EpochHeader {
  config: LlmCallConfig            // provider, model, reasoningEffort, 采样标量
  adapterDefaults?: LlmCallConfigAdapterDefaults
  tools?: ToolSchema[]
  system?: never                   // 已退役；系统提示词属于 system/message
}
```

四个 `reason` 覆盖了它的全部生命周期：

| reason | 何时写 |
| --- | --- |
| `initial` | 本 agent loop 实例的第一个请求 |
| `resume` | 恢复会话后的第一个请求 |
| `change` | 信封发生变化（路由、采样、工具集） |
| `series` | 信封没变，但显式开启新消息序列或跟随 surface 替换 |

重建时 `foldRequestHeader(events)` 只取最新一条快照。日志里因此会有多份重复的 header，这是有意的冗余：每次变化都留一个可回放的锚点，不必让人去重放差分。

规范化要求很硬：`header` 里不允许出现任何 `system` 字段，`tools: []` 与 `adapterDefaults: {}` 必须省略。seed、append 和持久化读取都会拒绝非规范 header，不会静默修正。静默规范化会让「日志里写的」和「实际发出去的」出现偏差，这是可重建性最怕的事。

`adapterDefaults` 容易忽略：它记录适配器实际生效的默认值，调用方提议的值不记。有些提供方会把自己的默认 `temperature` 填进来，不记下来的话，重放时同一份 header 可能算出不同的请求。

{{< fig "diagrams/02-header-reasons.svg" "header 的四种 reason 与重建规则：initial/resume 标记 loop 实例边界，change 是信封变化，series 只开启新序列而不改信封。重建只取最新快照。" >}}

## 3. 系统提示词：协作式组装

系统提示词由一组命名的段落拼成：

```ts
interface PromptSection {
  name: string
  order: number
  text: string | ((context: AssembleContext) => string)
  interpolate?: boolean
  complete?: boolean
}
```

组装顺序：先按 `order` 升序，同 order 再按 `name` 的代码单元顺序。「同 order 用 name 兜底」这条规则有用：两个插件碰巧用了同一个 order 时，输出顺序不会退化成注册顺序，而注册顺序在不同 profile 下可能不同。

`text` 可以是函数，每次组装时用当前 `AssembleContext` 求值。这给了动态段落能力，也带来了本节最后一个坑（第 9 节）。

`complete: true` 是一个「接管」机制：某个贡献者声明「我就是完整提示词」。但组装仍然会跑完整个协作式 waterfall，让工具、上下文、变量都能被解析，最后再把这一段恢复成唯一的段落。这样接管不会让其他插件失去被求值的机会（它们可能有副作用，比如解析工具清单）。多于一个生效的 `complete` 段会让组装失败，这属于配置错误，不该静默选一个。

{{< fig "diagrams/03-prompt-assembly.svg" "提示词组装管线：注册的 section 与 context 先按 order/name 排序，跑 system-prompt/assemble waterfall，求值动态文本，插值变量，过滤空段，最后用两个空行连接。complete 段在 waterfall 之后恢复为唯一段。" >}}

渲染函数本身很短：

```ts
export function renderPrompt(assembly: PromptAssembly): string {
  return assembly.sections
    .map(s => s.interpolate === false ? s.text : interpolate(s, assembly.variables, 'section'))
    .filter(text => text.length > 0)
    .join('\n\n')
}
```

三个细节：

- `interpolate: false` 保留字面量，包括看起来像 `{{variable}}` 的目录名。
- 畸形的引用（有 `{{` 没有 `}}`）当普通文本；未知或 undefined 的变量抛错，不静默替换成空。
- 替换进去的值不会再次扫描，避免「变量里套变量」的注入问题。

## 4. 提示词怎么进入历史：两条路由

这是整篇最反直觉的部分。系统提示词不在请求字段里，它是 surface 的一个节点。节点怎么更新，取决于路由能力：

```ts
project(rendered: string, input: SystemPromptDecisionInput): SystemPromptCommit[] {
  const nodes = this.systemNodes()
  const head = nodes[0]
  if (head === undefined) {
    return [{ message: createSystemMessage(rendered), intent: { surfaceOp: 'append' } }]
  }
  const latest = nodes.findLast(node => node.text !== '') ?? head
  if (!input.inHistory || rendered.length === 0 || (input.startsSeries && latest.text !== rendered)) {
    const updates = nodes.slice(1).filter(n => n.text !== '').map(n => this.replace(n.seq, ''))
    if (head.text !== rendered) updates.push(this.replace(head.seq, rendered))
    return updates
  }
  if (latest.text === rendered) return []
  return [{ message: createSystemMessage(rendered), intent: { surfaceOp: 'append' } }]
}
```

拆成三条规则：

路由不支持 in-history，或渲染为空，或新序列且文本不同：走归一化，把后续所有非空 system 节点替换成空（休眠），再把 head 替换成新文本。结果是模型只看到一段系统提示词，位置永远是节点 0。

支持 in-history 且是序列延续：追加一个新节点。前缀完全不动，缓存友好。

文本没变：不产生任何 commit。这一条挡掉了大量无意义的替换。

{{< fig "diagrams/04-system-node-routes.svg" "系统提示词的两种更新路由。左：归一化，清空后续节点并把 head 替换成新文本；右：in-history，追加新节点、保留前缀。空渲染会清空所有活跃节点。" >}}

in-history 这条路由是为 prompt cache 准备的：缓存按前缀命中。如果每次提示词变化都替换节点 0，前缀就变了，缓存全废。而追加一个节点，前面所有内容（包括旧的系统提示词）仍然是同一个前缀。

代价是模型可能同时看到多段系统提示词。所以这条路由只给「明确声明 `systemPromptUpdate: 'in-history'` 的提供方」用，并且只在同一序列内生效；新序列开始且文本不同时，仍然要归一化，否则旧指令会一直堆着。

还有一个边界：空渲染会清空所有活跃 system 节点。「移除提示词」因此是彻底的，旧的后续节点不会留着让模型继续看到过期指令。

## 5. 运行时上下文：一个会被取代的快照

除了系统提示词，还有一类动态信息：当前时间、工作目录、git 状态、可用技能列表。它们不适合放进 system（变化太频繁），也不适合当普通 user 消息（会被误认为是用户说的）。

DSH 用 `PromptContext` + 一条 user-role 消息：

```ts
interface PromptContext {
  name: string
  order: number
  text: string | ((context: AssembleContext) => string)
  interpolate?: boolean
  required?: boolean
}
```

渲染出来的文本带一句固定前缀：

```
Current runtime context. This snapshot supersedes earlier runtime-context snapshots.
```

这句话是给模型的语义指令，不是装饰：看到它就知道后面是最新快照，前面同类快照可以忽略。这样就不用每次删除旧快照，也不用改写历史。

`RuntimeContextProjection` 负责去重：

- `retained === undefined`：从未有过快照；当前渲染为空则不写。
- `retained === null`：曾经有过但已被替换遮蔽；当前为空则不写。
- 否则比较文本，相同就不写。

一条 surface 替换事件遮蔽 retained 的 seq 时，投影状态被置为 `null`。压缩把运行时快照压掉的情况就是这样处理的：下一轮必须重新写一份，否则模型缺上下文。

{{< fig "diagrams/05-message-order.svg" "一次请求里消息的排列顺序：system 节点（可能多段）→ 运行时上下文快照（user 角色）→ 本轮 claim 的用户消息 → 历史 assistant/tool 消息。运行时快照只插一次，且插在第一条用户消息之前。" >}}

插入位置也有讲究。首轮提交用户消息时，循环遍历 claim 到的批次，遇到第一条 `source.kind === 'runtime-context'` 的消息，就把运行时快照插在它前面（只插一次）。快照因此总在本轮用户输入之前。放到后面的话，模型会先读问题再读环境，容易把环境当成回答的一部分。

## 6. 工具清单：每个拟议步骤一次准入

工具清单不直接从全局注册表取。`AgentLoop` 为每个拟议步骤调用一次：

```ts
const toolSet = await ctx.tools.resolve({ agent, modelSelection, signal })
```

`ResolvedToolSet` 的生命周期被严格限定：

- 它决定这一步的工具声明、执行器分区，以及批次里的每次调用（包括 PTC 子调用）。
- 它有一个 `dispose()`，在该步工具批次结算后由循环调用。
- 同名替换或新的 scope restriction 会阻止尚未开始的调用使用已准入的注册。

{{< fig "diagrams/06-tool-visibility.svg" "工具可见性的过滤链：全局注册 → scope shadowing → restriction（allow/deny 交集）→ when 条件（每步求值一次）→ 本步 schema。被过滤掉的工具既不出现于提示词，也拒绝执行，与不存在无法区分。" >}}

过滤链的顺序值得记住：

1. scope shadowing：最具体的注册胜出。
2. restriction：对全局工具取 allow/deny 交集；scope-local 注册在过滤后合并。
3. `when` 条件：每步求值一次，可以异步，必须观察 `signal`（细节见 6.1）。
4. 结果就是这一步的 schema 集合。

被过滤掉的工具不出现在提示词里，也拒绝执行，与「根本不存在」无法区分。这是安全设计：模型不该从错误信息里推断出「有个工具被禁了」，否则它会尝试绕过。

工具集变化还会产生一条 `developer/message`：

```ts
content: [{ type: 'tool-addition' | 'tool-removal', toolName }]
```

它告诉模型「你的工具变了」，免得模型下一轮突然发现「我记得有个工具，怎么没了」。这条消息带 `headerSeq`（有新增时），指向定义这些工具的那份 header，模型看到工具变更时能对应到具体的 schema 快照。

### 6.1 `when` 的处理形式：一个每步求值的谓词

注册时可以带一个可选的激活条件。它的类型原文在 `packages/core/tools/src/contracts.ts`：

```ts
/** Inputs for a tool's step-time activation condition. */
interface ToolActivationContext {
  /** Agent preparing the step. */
  readonly agent: Agent
  /** Provider, model, and optional reasoning effort captured for the step. */
  readonly modelSelection: Readonly<ModelSelection>
  /** Cancellation of step preparation; asynchronous conditions must observe it. */
  readonly signal: AbortSignal
}

interface ToolRegistrationOptions {
  readonly when?: (context: ToolActivationContext) => boolean | Promise<boolean>
}
```

用法是注册的第二个参数，不是定义里的字段：

```ts
const dispose = ctx.tools.register(definition, {
  when: ({ agent, modelSelection, signal }) =>
    modelSelection.model.includes('pro') && !signal.aborted,
})
```

三个语义要分开记：

| 返回值 | 含义 | 后果 |
| --- | --- | --- |
| `true`（或省略 `when`） | 这一步开放 | 进入声明与派发 |
| `false` | 这一步不开放，等价于「没注册过」 | 不出现在 schema 里，调用报 `UNKNOWN_TOOL` |
| 抛出 / reject | 条件本身坏了 | 包成 `ToolActivationError`（code `TOOL_ACTIVATION_FAILED`），整步准备失败 |

`false` 与 `throw` 的区别是刻意的：前者是「这一步不需要你」，后者是「判断不了」。把故障伪装成不可用，会让模型在缺工具的情况下硬做；把不可用当成故障，则会让一次临时网络抖动毁掉整轮。DSH 选了后者，条件函数得自己决定哪些异常可以吞成 `false`。

求值位置固定在 `agent-loop/src/agent.ts` 的 `preStep` 里：

```ts
const claimed = this.inbox.claim(target, position.turn)   // 先把输入出队
const selection = this.model.current()
const toolSet = await this.loopCtx.tools.resolve({
  agent: this, modelSelection: selection, signal,
})
const assembly = await this.loopCtx.systemPrompt.assemble({
  ...assembleContextFor(this, signal), modelSelection: selection, toolSet,
})
```

也就是说 `when` 在 system prompt 组装之前跑完，因为提示词里「有哪些工具」必须先定，不能组装完再筛。

`resolve()` 的实现很短，每行都有约束：

```ts
for (const [name, definition] of view.visible) {
  const registration = view.registrations.get(name)
  let enabled: boolean
  try {
    enabled = registration?.when === undefined || await registration.when(context)
  } catch (cause: unknown) {
    signal.throwIfAborted()
    throw new ToolActivationError(name, cause)
  }
  signal.throwIfAborted()
  if (enabled) admitted.set(name, definition)
}
```

- 串行：一个 `await` 接一个，不用 `Promise.all`。N 个条件的延迟相加，条件必须便宜。
- 按可见集合的迭代序：已经过 shadowing 与 restriction 的 `view.visible`。
- 前后都查取消：`signal.throwIfAborted()` 在循环前、每个条件后各一次，取消时不发布部分集合。
- 失败即中止：任何一个条件抛错，整个 `resolve()` 失败，前面已经求得的结果全部丢弃。

{{< fig "diagrams/07-when-sequence.svg" "when 的求值时序：claim 之后、system prompt 组装之前，AgentLoop 调一次 resolve；ctx.tools 对可见集合逐个 await 条件，false 排除、throw 中止整步、abort 丢弃全部结果；集合冻结后随请求声明，执行时按同一性复核，批次结算后 dispose。" >}}

拿到集合之后，约束还在继续：

- 每步一次，不是每次调用：一步内多个 tool call、PTC 程序里的子调用，共享同一份准入集合。
- 冻结：返回的对象被 `Object.freeze`，批次结束由循环 `dispose()`；关闭后 `view()` 返回空。
- 同一性复核：`toolSet.view(scope)` 会把当前注册表与准入时的 `definition`、`registration` 逐项比对。同名替换或新增 restriction 会让尚未开始的调用失败，已开始的调用不受影响。
- 步骤之外不可用：`executable(name, scope)` 不传 `toolSet` 时，带 `when` 的注册一律返回 `undefined`。手动调用（不经过模型步骤）跑不了条件工具，这是有意的，条件语义绑定在「步骤」上。

四个拦截机制经常被混用，作用不同：

| 机制 | 作用域 | 求值时机 | 能看到什么 | 拒绝的可见性 |
| --- | --- | --- | --- | --- |
| restriction | scope（agent） | 可见性计算时 | 工具名 | 完全不可见 |
| when | 单条注册 | 每个拟议步骤 | agent、本步模型选择、signal | 这一步不可见 |
| guard | 单次调用 | 流水线 pre-execute | 参数、agent、signal | 模型看到拒绝原因 |
| approval | 单次调用 | guard 之后 | 参数 | 模型看到拒绝/未批准 |

「这个工具只对 pro 模型开放」用 `when`；「这个 agent 不许用 shell」用 `restriction`；「这条命令要不要问人」用 `approval`。拿 `when` 做权限判断是错的：它只决定这一步是否出现，不决定这次调用是否允许，而且求值发生在参数存在之前。

踩坑点：

1. 条件卡住就是这一步卡住。`when` 没有自己的超时，只有 step 的取消信号；条件里必须把 `signal` 透传，否则用户取消后这一步仍然挂着。
2. 别在条件里改注册表或启动工具工作。合同明确禁止；`resolve()` 正在遍历 `view`，改注册表属于未定义行为。需要预热就缓存到自己的状态，条件只读。
3. 一个条件抛错会让整轮失败，而输入已经被 claim 走了。`preStep` 先 `claim` 再 `resolve`，失败时那一批消息已经出队、不会回插，`turn/end` 以 `error`（code `TOOL_ACTIVATION_FAILED`）关闭，日志里能看到 claim 却没有 `step/start`。条件里的「临时故障」要自己决定吞不吞。
4. 串行求值，别写贵的条件。每个条件一次磁盘/网络访问，十步就是十次；把它做成纯内存判断，或自己在条件外缓存。
5. 不要在条件之间建立顺序依赖。迭代序是可见集合的顺序，不是声明顺序；今天能跑不代表改个注册顺序还能跑。
6. `when` 不是「每次调用重新判断」。模型在一步里连续调同一个工具十次，条件只跑一次；需要按参数判断就用 guard 或 approval。

## 7. 冻结、去重与缓存

`buildRequest()` 的最后几步都在做同一件事：让请求变成不可变值。

```ts
const messages = session.deriveMessages()
for (const message of messages) deepFreeze(message)
Object.freeze(messages)
```

同一份消息对象在多个请求间复用（`frozenMessages` 按 identity 去重），所以冻结只做一次。好处是请求一旦构造完成，就不会被后续插件悄悄改掉；坏处是任何想临时改一下消息的插件都必须走日志通道。

缓存方面，一次请求的失效点可以精确列出：

| 变化 | 是否破坏 prompt cache |
| --- | --- |
| 新增 user/assistant/tool 消息 | 否（前缀不变，只是变长） |
| header 变化（路由/采样/工具） | 是（信封变了） |
| system 节点原地替换 | 是（该节点之后的前缀全变） |
| system 节点追加（in-history） | 否（旧前缀保留） |
| 压缩 replace | 是（被遮蔽段之后全变） |
| 运行时上下文新快照 | 否（追加在尾部附近） |

这张表解释了 DSH 为什么做 in-history 路由、压缩为什么尽量替换靠后的范围、header 为什么尽量少变。缓存在这里属于 Agent 的成本结构，不能当优化项看待。

## 8. 设计取舍

提示词作为历史，还是当成请求字段。作为历史，压缩可以统一处理、fork 可以带走、审计可以看到当时的提示词。代价是每次提示词变化都要写事件，还要处理「多段系统提示词」的语义。

header 用全量快照，还是增量。全量快照让重建只需读最后一条，代价是日志冗余。增量（`header-delta`）在 v0 里存在过，后来被移除了，因为「读到最后一条就能重建」比「重放所有差分」可靠得多。

工具准入每步一次，还是每轮一次。每步一次允许工具随模型能力、上下文压力动态变化（比如压缩后隐藏大工具）。代价是每步都有一次 `resolve()` 调用，且 `when` 条件必须是纯的、可取消的。

工具变更写 developer 消息，还是静默处理。写消息让模型知道工具变了，减少「幻觉调用不存在的工具」。代价是它占上下文，且需要 `headerSeq` 关联。

运行时快照取代旧快照，而不是删除。靠一句「本快照取代之前所有快照」的文案，避免改写历史。代价是模型要理解这句话；如果模型不遵守，就会看到多份过期环境信息。

## 9. 踩坑点

- 在 `agent/request` 里改 model/provider。会直接抛错。正确做法是 `agent.model.select()`，且必须在 step 准备之前。
- 在动态 section 里做 IO 或随机。`text: (ctx) => ...` 每次组装都会跑；如果它读文件、读时钟、调随机数，同一份日志就无法重建出同一份提示词。
- 给两个 section 用同一个 order 且指望注册顺序。同 order 会按 name 排序；不同 profile 的注册顺序可能不同。
- 注册多个 `complete` 段。组装会失败，而不是选第一个。
- 以为 header 每次请求都写。只有 initial/resume/change/series 四种情况写；不变的请求沿用最新快照。
- 以为工具 `when` 条件在每次调用时求值。它在每个拟议步骤求值一次；同一步内的多次调用共享同一个准入集合。
- 在工具被 restriction 移除后继续持有旧 `ToolDefinition` 调用。执行时会拒绝；`dispose()` 之后更不能再使用。
- 让 `when` 抛错却不接受「整轮失败」。条件失败会把已 claim 的输入一起带走（不回队），turn 以 `TOOL_ACTIVATION_FAILED` 关闭。
- 把运行时上下文当普通 user 消息插在任意位置。必须在第一条用户消息之前，且只插一次。
- 手写非规范 header（带 `system`、带空 `tools`）。seed/append/读取都会拒绝，不会帮你修。

## 10. 源码索引

- `packages/core/agent-loop/src/agent.ts:613-653`：`prepareRequest`，selection 一致性、`prepareCall`、`NO_ADAPTER` 容忍。
- `:656-744`：`buildRequest`，header 规范、developer 工具通知、`request/context`、冻结与去重。
- `packages/core/agent-loop/src/runtime-context.ts:93`：`SystemPromptProjection.project` 的两条路由与空渲染清空。
- `:157`：`RuntimeContextProjection.project` 的去重与 `CLEARED` 语义。
- `packages/core/system-prompt/src/index.ts:296`：`renderPrompt` 的插值、空段过滤与空行连接。
- `:588`：`refreshContext`，准入时的上下文刷新。
- `docs/subsystems/system-prompt.zh.md`：`PromptSection` / `PromptContext` / `complete` / 排序规则。
- `packages/core/tools/src/contracts.ts:105-143`：`ToolActivationContext`、`ToolRegistrationOptions.when`、`ToolActivationError`。
- `packages/core/tools/src/index.ts:264-305`：`resolve()` 的串行求值、取消检查与冻结集合。
- `:251`：`executable()` 在没有 `toolSet` 时排除条件注册。
- `:150`：`register()` 把 `when` 与定义一起存进注册层。
- `packages/core/agent-loop/src/agent.ts:287-292`：`claim` 之后、`assemble` 之前调用 `resolve`。
- `docs/subsystems/tools.zh.md:182-220`：`resolve`、`ResolvedToolSet`、`when` 条件与批次生命周期。

请求组装是「模型看到什么」的最后一公里。看起来只是拼字符串，每个字段背后却有一条日志、一种失效规则和一个缓存后果。这层想清楚，后面讨论压缩、上下文注入、工具权限时，才不会把它们当成互不相干的补丁。

上一篇：[turn、step 与尝试的三层状态机](/posts/dsh-turn-step-state-machine.html) ｜ 下一篇：[失败、重试与恢复](/posts/dsh-error-retry-recovery.html)
