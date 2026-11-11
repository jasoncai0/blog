---
title: "请求是怎么装出来的：信封、提示词与工具清单"
date: "2026-11-11T12:00:00+08:00"
summary: "模型请求不是「messages 数组」。DSH 把它拆成信封、派生历史、工具 schema 与运行时上下文四块，每块都有自己的日志与失效规则。"
description: "拆解 request/header 的规范形式与四种 reason、system prompt 的协作式组装与两种历史路由、每步一次的工具准入，以及它们对 prompt cache 的影响。"
tags: ["architecture", "context-engineering", "agent"]
slug: "dsh-request-assembly"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。行号以该 commit 为准，引用一律指向 `src/`。
> 依据：`packages/core/agent-loop/src/agent.ts:613-744`（`prepareRequest` / `buildRequest`）、`packages/core/agent-loop/src/runtime-context.ts`、`packages/core/system-prompt/src/index.ts:296`（`renderPrompt`）、`docs/subsystems/system-prompt.zh.md`、`docs/subsystems/tools.zh.md`。

一个 LLM 请求看起来只有几个字段，但在 Agent 里，每个字段都是一条决策链的终点：

- `messages` 从哪来？不能是「上次的数组 + 这次的增量」。
- `tools` 谁决定？同一时刻不同 agent 看到的工具可以不同。
- `system` 放哪？它变了，是替换还是追加？
- `temperature`、`max_tokens` 谁说了算？重试时还是同一套吗？

DSH 的答案是把请求拆成四块，**每块都有自己的真源和失效规则**。

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

注意 `system` 不在这个列表里。这是一个刻意的省略，第 3 节会解释。

## 2. 信封：把「请求本身」也记进日志

`request/header` 是 log-only 事件（没有 `surfaceOp`），它的 data 是一个**完整快照**，不是增量：

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

重建时 `foldRequestHeader(events)` 只取**最新一条快照**。这意味着日志里会有多份重复的 header——这是有意的冗余：每次变化都留一个可回放的锚点，而不是让人去重放差分。

规范化要求很硬：`header` 里不允许出现任何 `system` 字段，`tools: []` 与 `adapterDefaults: {}` 必须省略。**seed、append 和持久化读取都会拒绝非规范 header，而不是静默修正。** 理由是「静默规范化」会让「日志里写的」和「实际发出去的」出现偏差，而这正是可重建性最怕的事。

`adapterDefaults` 是另一个容易忽略的字段：它记录**适配器实际生效的默认值**，而不是调用方提议的值。有些提供方会把自己的默认 `temperature` 填进来，如果不记下来，重放时同一份 header 可能算出不同的请求。

{{< fig "diagrams/02-header-reasons.svg" "header 的四种 reason 与重建规则：initial/resume 标记 loop 实例边界，change 是信封变化，series 只开启新序列而不改信封。重建只取最新快照。" >}}

## 3. 系统提示词：协作式组装

系统提示词不是一个大字符串常量，而是一组**命名的段落**：

```ts
interface PromptSection {
  name: string
  order: number
  text: string | ((context: AssembleContext) => string)
  interpolate?: boolean
  complete?: boolean
}
```

组装顺序：先按 `order` 升序，同 order 再按 `name` 的代码单元顺序。这个「同 order 用 name 兜底」的规则很重要——否则两个插件碰巧用了同一个 order，输出顺序就取决于注册顺序，而注册顺序在不同 profile 下可能不同。

`text` 可以是函数，每次组装时用当前 `AssembleContext` 求值。这给了动态段落能力，也带来了本节最后一个坑（第 9 节）。

`complete: true` 是一个「接管」机制：某个贡献者声明「我就是完整提示词」。但组装**仍然会跑完整个协作式 waterfall**，让工具、上下文、变量都能被解析，最后再把这一段恢复成唯一的段落。这样「接管」不会让其他插件失去被求值的机会（它们可能有副作用，比如解析工具清单）。多于一个生效的 `complete` 段会让组装失败——这是配置错误，不该静默选一个。

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
- 畸形的引用（有 `{{` 没有 `}}`）当普通文本；未知或 undefined 的变量**抛错**，不静默替换成空。
- 替换进去的值**不会再次扫描**，避免「变量里套变量」的注入问题。

## 4. 提示词怎么进入历史：两条路由

这是整篇最反直觉的部分。系统提示词**不是请求字段**，它是 surface 的一个节点。而节点怎么更新，取决于路由能力：

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

**路由不支持 in-history，或渲染为空，或新序列且文本不同** → 走「归一化」：把后续所有非空 system 节点替换成空（休眠），再把 head 替换成新文本。结果是模型只看到一段系统提示词，位置永远是节点 0。

**支持 in-history 且是序列延续** → 追加一个新节点。前缀完全不动，缓存友好。

**文本没变** → 不产生任何 commit。这一条挡掉了大量无意义的替换。

{{< fig "diagrams/04-system-node-routes.svg" "系统提示词的两种更新路由。左：归一化，清空后续节点并把 head 替换成新文本；右：in-history，追加新节点、保留前缀。空渲染会清空所有活跃节点。" >}}

为什么要有 in-history 这条路由？因为 **prompt cache 按前缀命中**。如果每次提示词变化都替换节点 0，前缀就变了，缓存全废。而追加一个节点，前面所有内容（包括旧的系统提示词）仍然是同一个前缀。

代价是模型可能同时看到多段系统提示词。所以这条路由只给「明确声明 `systemPromptUpdate: 'in-history'` 的提供方」用，并且只在同一序列内生效；新序列开始且文本不同时，仍然要归一化，否则旧指令会一直堆着。

还有一个边界：**空渲染总是清空所有活跃 system 节点**。这保证「移除提示词」是彻底的，不会因为旧的后续节点还在而让模型继续看到过期指令。

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

这句话不是装饰，它是**给模型的语义指令**：看到这句，就知道后面是最新快照，前面那些同类快照可以忽略。这样就不需要每次删除旧快照，也就不需要改写历史。

`RuntimeContextProjection` 负责去重：

- `retained === undefined`：从未有过快照；当前渲染为空则不写。
- `retained === null`：曾经有过但已被替换遮蔽；当前为空则不写。
- 否则比较文本，相同就不写。

当一条 surface 替换事件遮蔽了 retained 的 seq 时，投影状态被置为 `null`。这处理了「压缩把运行时快照也压掉了」的情况——下一轮必须重新写一份，否则模型会缺上下文。

{{< fig "diagrams/05-message-order.svg" "一次请求里消息的排列顺序：system 节点（可能多段）→ 运行时上下文快照（user 角色）→ 本轮 claim 的用户消息 → 历史 assistant/tool 消息。运行时快照只插一次，且插在第一条用户消息之前。" >}}

插入位置也有讲究。首轮提交用户消息时，循环遍历 claim 到的批次，遇到**第一条** `source.kind === 'runtime-context'` 的消息，就把运行时快照插在它前面（只插一次）。这保证快照总是出现在本轮用户输入之前，而不是之后——否则模型会先读问题再读环境，容易把环境当成回答的一部分。

## 6. 工具清单：每个拟议步骤一次准入

工具不是「全局注册表里有什么就给什么」。`AgentLoop` 为**每个拟议步骤**调用一次：

```ts
const toolSet = await ctx.tools.resolve({ agent, modelSelection, signal })
```

`ResolvedToolSet` 的生命周期被严格限定：

- 它决定这一步的工具声明、执行器分区，以及批次里的每次调用（包括 PTC 子调用）。
- 它有一个 `dispose()`，在**该步工具批次结算后**由循环调用。
- 同名替换或新的 scope restriction 会阻止**尚未开始**的调用使用已准入的注册。

{{< fig "diagrams/06-tool-visibility.svg" "工具可见性的过滤链：全局注册 → scope shadowing → restriction（allow/deny 交集）→ when 条件（每步求值一次）→ 本步 schema。被过滤掉的工具既不出现于提示词，也拒绝执行，与不存在无法区分。" >}}

过滤链的顺序值得记住：

1. **scope shadowing**：最具体的注册胜出。
2. **restriction**：对全局工具取 allow/deny 交集；scope-local 注册在过滤后合并。
3. **`when` 条件**：每步求值一次，可以异步，必须观察 `signal`。
4. 结果就是这一步的 schema 集合。

被过滤掉的工具**既不出现于提示词，也拒绝执行**，而且与「根本不存在」无法区分。这是一个安全设计：不要让模型通过错误信息推断出「有个工具被禁了」，那会诱导它去尝试绕过。

工具集变化还会产生一条 `developer/message`：

```ts
content: [{ type: 'tool-addition' | 'tool-removal', toolName }]
```

它告诉模型「你的工具变了」，而不是让模型在下一轮突然发现「我记得有个工具，怎么没了」。这条消息带 `headerSeq`（当有新增时），指向定义这些工具的那份 header——这样模型看到工具变更时，能对应到具体的 schema 快照。

## 7. 冻结、去重与缓存

`buildRequest()` 的最后几步都在做同一件事：**让请求变成不可变值**。

```ts
const messages = session.deriveMessages()
for (const message of messages) deepFreeze(message)
Object.freeze(messages)
```

同一份消息对象在多个请求间复用（`frozenMessages` 按 identity 去重），所以「冻结」只做一次。这样做的好处是：请求一旦构造完成，就不会被后续的插件悄悄改掉；坏处是任何想「临时改一下消息」的插件都必须走日志通道。

缓存方面，一次请求的失效点可以精确列出：

| 变化 | 是否破坏 prompt cache |
| --- | --- |
| 新增 user/assistant/tool 消息 | 否（前缀不变，只是变长） |
| header 变化（路由/采样/工具） | 是（信封变了） |
| system 节点原地替换 | 是（该节点之后的前缀全变） |
| system 节点追加（in-history） | 否（旧前缀保留） |
| 压缩 replace | 是（被遮蔽段之后全变） |
| 运行时上下文新快照 | 否（追加在尾部附近） |

这张表解释了为什么 DSH 要做 in-history 路由、为什么压缩要尽量替换「靠后」的范围、为什么 header 要尽量少变。**缓存不是优化，它是 Agent 成本结构的一部分。**

## 8. 设计取舍

**提示词作为历史 vs 请求字段。** 作为历史，压缩可以统一处理、fork 可以带走、审计可以看到「当时的提示词」。代价是每次提示词变化都要写事件，且要处理「多段系统提示词」的语义。

**header 全量快照 vs 增量。** 全量快照让重建只需读最后一条，代价是日志冗余。增量（`header-delta`）在 v0 里存在过，后来被移除了——因为「读到最后一条就能重建」比「重放所有差分」可靠得多。

**每步一次工具准入 vs 每轮一次。** 每步一次允许工具随模型能力、上下文压力动态变化（比如压缩后隐藏大工具）。代价是每步都有一次 `resolve()` 调用，且 `when` 条件必须是纯的、可取消的。

**工具变更写 developer 消息 vs 静默。** 写消息让模型知道「工具变了」，减少「幻觉调用不存在的工具」。代价是它占上下文，且需要 `headerSeq` 关联。

**运行时快照取代而非删除。** 靠一句「本快照取代之前所有快照」的文案，避免改写历史。代价是模型要理解这句话；如果模型不遵守，就会看到多份过期环境信息。

## 9. 踩坑点

- **在 `agent/request` 里改 model/provider。** 会直接抛错。正确做法是 `agent.model.select()`，且必须在 step 准备**之前**。
- **在动态 section 里做 IO 或随机。** `text: (ctx) => ...` 每次组装都会跑；如果它读文件、读时钟、调随机数，同一份日志就无法重建出同一份提示词。
- **给两个 section 用同一个 order 且指望注册顺序。** 同 order 会按 name 排序；不同 profile 的注册顺序可能不同。
- **注册多个 `complete` 段。** 组装会失败，而不是选第一个。
- **以为 header 每次请求都写。** 只有 initial/resume/change/series 四种情况写；不变的请求沿用最新快照。
- **以为工具 `when` 条件在每次调用时求值。** 它在**每个拟议步骤**求值一次；同一步内的多次调用共享同一个准入集合。
- **在工具被 restriction 移除后继续持有旧 `ToolDefinition` 调用。** 执行时会拒绝；`dispose()` 之后更不能再使用。
- **把运行时上下文当普通 user 消息插在任意位置。** 必须在第一条用户消息之前，且只插一次。
- **手写非规范 header（带 `system`、带空 `tools`）。** seed/append/读取都会拒绝，不会帮你修。

## 10. 源码索引

- `packages/core/agent-loop/src/agent.ts:613-653`：`prepareRequest`，selection 一致性、`prepareCall`、`NO_ADAPTER` 容忍。
- `:656-744`：`buildRequest`，header 规范、developer 工具通知、`request/context`、冻结与去重。
- `packages/core/agent-loop/src/runtime-context.ts:93`：`SystemPromptProjection.project` 的两条路由与空渲染清空。
- `:157`：`RuntimeContextProjection.project` 的去重与 `CLEARED` 语义。
- `packages/core/system-prompt/src/index.ts:296`：`renderPrompt` 的插值、空段过滤与空行连接。
- `:588`：`refreshContext`，准入时的上下文刷新。
- `docs/subsystems/system-prompt.zh.md`：`PromptSection` / `PromptContext` / `complete` / 排序规则。
- `docs/subsystems/tools.zh.md:182-220`：`resolve`、`ResolvedToolSet`、`when` 条件与批次生命周期。

请求组装是「模型看到什么」的最后一公里。它看起来只是拼字符串，实际上每个字段背后都有一条日志、一种失效规则和一个缓存后果。把这层想清楚，后面讨论压缩、上下文注入、工具权限时，才不会把它们当成互不相干的补丁。

上一篇：[turn、step 与尝试的三层状态机](/posts/dsh-turn-step-state-machine.html) ｜ 下一篇：[失败、重试与恢复](/posts/dsh-error-retry-recovery.html)
