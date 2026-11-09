---
title: "OpenCode 机制深潜"
date: "2026-11-09"
summary: "逐个拆开 agent loop、工具八道关、权限判定、MCP 生命周期、skill 与九层记忆、执行面、SQLite 存储与插件 hook：数据结构、调用链、设计取舍与失败模式。"
description: "基于 opencode 2.2.5 源码，逐个拆开 agent loop、工具管线、权限、MCP、skill 与记忆、执行面、存储与插件机制：数据结构、调用链、设计取舍与失败模式。"
tags: ["opencode", "architecture", "context-engineering"]
slug: "opencode-deep-dive"
---

> 源码版本：`opencode 2.2.5`，commit `a687c047`（2026-11-08）。代码引用以工作区 `.opencode-src/` 为基准，正文省略该前缀。
> 这是「OpenCode 解剖」系列的第四篇。前三篇分别讲整体架构、上下文管理、打断；本篇往下钻一层，逐个拆开机制本身。

## 0. 这篇在讲什么

前三篇回答的是"它长什么样"：

- [架构总览](https://www.jasoncai.site/posts/opencode-architecture.html)：分层、事件溯源、执行模型、多端。
- [上下文管理](https://www.jasoncai.site/posts/opencode-context-management.html)：每个 Step 如何重建上下文、压缩与缓存。
- [打断](https://www.jasoncai.site/posts/opencode-interrupt.html)：一次 Esc 如何穿过 HTTP、fiber、SQLite 与 claim。

这一篇回答"它是怎么咬合的"：agent loop、工具管线、权限、MCP、skill 与记忆、执行面、存储、插件。每个机制都按同一个模板写：**数据结构 → 调用链 → why → 失败模式**。上下文管理和打断不重复，只在需要时回指。

一句话先给结论：**这套系统的所有机制都挂在同一个安全点上——Step 边界；所有状态都先变成 durable 事件；所有副作用发生前都已经有记录。** 后面的细节都是这句话的展开。

## 1. Agent loop：三层循环，一个安全点

{{< fig "diagrams/01-step.svg" "一个 Step 的状态机：装载 → 请求 → 工具 → 再装载" >}}

打开 `session/runner/llm.ts` 之前，先记住它其实有三层循环，每层的退出条件不同：

```text
coordinator.loop      ← 一次忙期；doorbell 被按响就 trampoline 再 drain
  └ execution.drain   ← Moved / Reloaded 时换实例重入
      └ runner while  ← advanceToStep 与 runStep 交替，Step 之间不共享内存
```

**第一层是"忙期"。** `SessionRunCoordinator` 用一个 key 为 Session ID 的 Map 管执行（`session/run-coordinator.ts:66`）。空闲时 `run` 起一条 fiber；忙时 `wake` 只按门铃（`pendingWake`），当前 fiber 排空后会再 drain 一轮。门铃如果在执行结束之后才响，`settle` 会为剩余工作起一个后继执行，而不是把工作丢掉（`run-coordinator.ts:109-115`）。

**第二层是"换场地"。** `execution.drain` 只按 Session ID 路由，真正执行前才 `store.get` + `instances.provide` 解析 Location 服务（`session/execution.ts:91-96`）。所以 `drain` 的返回值有三种：

| DrainResult | 何时发生 | 谁处理 |
| --- | --- | --- |
| `Complete` | 没有待交付输入，或达到 idle 边界 | 结束这次 drain |
| `Moved` | 边界上下一个可提升项是 move 控制项 | 以 `force=false` 重新 drain，新 Location |
| `Reloaded` | Location 生命周期已关闭（shutdown/evict） | 以原 `force` 重新 drain |

`Moved` 与 `Reloaded` 都带一个 `stepped` 标志，目的是让新一次 drain 走到 idle 时仍然触发 idle hook——上一个 drain 已经跑过 Step，不能因为交接把钩子吞掉（`session/runner/index.ts:39-40`）。

**第三层是 Step 本身。** `runner` 里是 `while (true)` 套 `advanceToStep()` 与 `runStep()`（`runner/llm.ts:212-220`）。每个 Step 结束后，决定"要不要再来一个"的**唯一判据**是：

```ts
needsContinuation = 该 Step 中是否有"本地执行的工具调用"
```

不是"模型还想说话"，也不是"消息里还有 tool_calls 字段"。判据的实现在 `publish-llm-event.ts:592-601`：遍历这一步的 tool 记录，只要有一个 `!providerExecuted && (called || settled)`，就需要续步。

### 1.1 什么时候收束

`Complete` 有三条路径，顺序很重要：

1. **入口早退**：drain 一开始没有可提升的输入，且不是被迫执行，就直接 Complete（`llm.ts:72-77`）。
2. **idle 边界**：Step 跑完、不续步、又没有待交付输入时，先跑 `experimental.idle` hook（把未完成的后台任务概要交给插件），再查一次 `SessionInbox.has`；仍然为空才 Complete（`llm.ts:179-185`）。
3. **从未跑过 Step**：如果这次 drain 一个 Step 都没跑（`stepped` 为假），即使有 pending 也直接 Complete，交给下一次唤醒处理。

第 2 条的"再查一次"不是多余：门铃和 idle 之间有一个无法原子化的窗口，插件也可能在 idle hook 里塞入新工作。

### 1.2 步数上限：最后一步为什么还带着工具

`agent.steps` 是可选的正整数（`schema/agent.ts:33`），`step` 从 1 开始，每步重新从数据库和 agent 配置里取。达到上限时，runner 做三件事（`llm.ts:268-272`）：

- 把 `toolChoice` 设成 `"none"`；
- 在消息尾部追加一条 **assistant 角色**的 `MAX_STEPS_PROMPT`（相当于 prefill），内容是"工具已禁用，只回文本，必须给出总结/未完成清单/后续建议"；
- **保留**工具定义。

保留工具定义是刻意的：删掉它会改变请求前缀，provider 的 prompt cache 立刻失效。注释写得很直白——`Keep tool definitions on the final Step to preserve the provider's cached prefix`。

`toolChoice: "none"` 只是对 provider 的软约束。真正的兜底在 `step.ts:98-100`：如果模型无视它仍发出 tool-call，`executeTool` 直接返回 `Tool.Error("Tools are disabled after the maximum agent steps")`，**不执行任何副作用**。同时 `needsContinuation` 被强制为 false（`step.ts:297`），不会出现"禁了工具还无限续步"。

一个容易忽略的推论：`steps = N` 实际只有前 `N-1` 步能调工具，第 N 步是纯总结；`steps = 1` 时第一步就没有工具。这是设计还是 off-by-one，仓库里没有对应单测，可以保留疑问。

### 1.3 一个 Step 内部的六种结局

`runStep` 返回一个 `Outcome`，六种取值各有不同的善后动作（`runner/step.ts:40-52`）：

| Outcome | 触发条件 | 处置 |
| --- | --- | --- |
| `Completed` | 正常结束 | 不续步时顺手做后台压缩预计算，利用 idle 时间 |
| `Retry` | 可重试错误 | 同一 assistant 消息、同一步号，退避后重发 |
| `Continue` | 已产出内容但流不完整 | 把失败的 assistant 标失败，发 synthetic 续写提示，换新消息 ID，同一步继续 |
| `RecoverFull` | 传输层要求整包重建 | 关闭 continuation 恢复，同一 Step 整包重发 |
| `Compacted` | 未产出内容就命中上下文溢出 | 关掉本步的溢出恢复（每步只一次），压缩后换新消息 ID 重发 |
| `OutputLimit` | finish 为 `length` | 连续 3 条都是 length 就终态失败，否则注入续写提示继续 |

关键区别：`Retry` 与 `Compacted` 不消耗 agent 步数配额，`Continue` 与 `OutputLimit` 也不消耗——它们都在同一个逻辑 Step 里换一次"物理尝试"。这就是为什么"一个 Step"和"一次 HTTP 请求"不是一回事。

### 1.4 工具结果是消息的一部分，不是消息

存储层的 `SessionMessage.Info` 里**没有** `tool` 类型（`schema/session-message.ts:304-316`）。工具调用与结果都是 assistant 消息内部的 `type: "tool"` part，状态从 `streaming` 到 `running` 再到 `completed/error`，由 projector 原地更新。

直到 lowering（把持久消息翻译成模型请求）时，才把本地执行的工具结果展开成紧跟 assistant 的 `role: "tool"` 消息（`runner/to-llm-message.ts:197-205`）。供应商侧执行的 hosted 工具不展开，结果内联在 assistant content 里。

这么设计的原因：**存储结构服务 UI 与投影，请求结构服务模型**。UI 需要"调用和结果是一体的"，模型需要"tool 消息跟在 assistant 后面"，两个需求不必用同一种结构满足。

### 1.5 代价

- 每个 Step 都要一次 DB 读 + 解码 + 合并 part + lowering。历史越长，每步的固定开销越大。
- Step 之间不能携带任何未落库的临时状态，所以所有异常都必须在本步内用六种 Outcome 兜住，`runStep` 因此有三层嵌套和六个分支。
- `Moved` / `Reloaded` 的重入要一路传 `step / stepped / continuing / force` 四个状态，组合多、易错；而且 `execution.ts:106` 对 `Reloaded` 的递归没有尝试上限，终止性依赖"新实例的 lifecycle 不再关闭"。

## 2. 工具管线：八道关，先记账再动手

{{< fig "diagrams/02-tool-pipeline.svg" "一次工具调用要过八道关" >}}

工具不是一张硬编码的函数表，而是"注册进作用域的值"。整条管线可以拆成注册、快照、执行、落库四段。

### 2.1 注册：一次 transform，读时重放

`Tool.Service` 只暴露 `transform` 与 `reload`（`tool.ts:249`）。所有生产者——内置插件、MCP、第三方插件——都通过一个 `transform(editor => ...)` 改注册表；`State` 在每次读取时按注册顺序重放这些 transform（`state.ts:191-209`）。这意味着注册是**声明式**的：会话或请求读工具时，看到的永远是"当前所有 transform 的结果"。

`add` 时会立刻算出 effective name：

```ts
normalizedName = name.replace(/[^a-zA-Z0-9_-]/g, "_")
effectiveName  = namespace ? `${namespace.replaceAll(".", "_")}_${normalizedName}` : normalizedName
```

（`tool/runtime.ts:288-293`）

注册失败**不抛错**：名字不合法、`codemode:false` 却叫 `execute`、schema 编译失败，都只往 `errors` 里塞一条，然后 `logError("Skipping invalid tool registration")`。一个坏插件不能拖垮整个工具表。

### 2.2 快照：按权限分桶

每个请求前生成一次 snapshot（`tool.ts:253`）。它先做一次"整只剔除"：

```ts
whollyDisabled(action, rules) =
  rules.findLast(match(action, rule.action))?.resource === "*" &&
  effect === "deny"
```

只有"最后一条匹配该 action 的规则、resource 恰好是 `*`、effect 是 deny"时才整只移除。**这是目录可见性，不是执行鉴权**——真正的执行鉴权在工具内部调用 `permission.assert` 时发生。两者分开，是因为"看不见"和"不能做"是两种不同的策略。

剩下的工具分成两桶：

- `direct`：`codemode === false` 的工具，进 provider 的原生工具表；
- `codeModeTools`：其余工具，渲染成目录，模型只看到一个合成的 `execute` 工具。

如果 `execute` 本身被整只禁用，CodeMode 关闭，桶内工具全部降级成 direct。

### 2.3 执行：八个阶段

| 阶段 | 位置 | 做什么 |
| --- | --- | --- |
| ① snapshot | `tool.ts:253` | 权限过滤、分桶、生成 definitions |
| ② execute.before | `tool.ts:184` | 插件可改输入，也可直接拒绝 |
| ③ decode | `tool/runtime.ts:32` | 按 input schema 解码；不合法就不执行 |
| ④ execute | `tool/runtime.ts:38-46` | 运行工具本体，非 `Tool.Error` 的抛出统一包成 `Tool.Error` |
| ⑤ encode | `tool/runtime.ts:56` | 有 output schema 就必须返回并编码 |
| ⑥ normalize | `tool.ts:175` | 合并文本、压缩图片，`decode/size` 失败折叠成一行提示 |
| ⑦ truncate | `tool-output.ts:69` | 2000 行 / 50 KiB，超出写文件 |
| ⑧ persist | `step.ts:128` | 发 `Tool.Success`，由 projector 落到 assistant 的 part 上 |

两个反直觉的点：

**before hook 在 decode 之前。** 插件可以把模型给的输入改写成合法值，所以校验必须放在 hook 之后。**after hook 在截断之前。** 插件看到的是完整结果，模型看到的是截断后的结果。

**错误文案是写给模型的自修复提示。** 输入不合法时，错误信息会列出最多 5 条 issue（带点路径或下标路径）、附上完整 JSON 参数，最后写一句 `Update the arguments and call the tool again.`（`tool/runtime.ts:89-101`）。这不是给人看的，是给下一次工具调用看的。

### 2.4 截断：省上下文，不丢信息

`ToolOutput.truncate` 只统计 `text` 内容，用 `\n` 拼接后计行数与字节数（`tool-output.ts:72-77`）。超限时把完整文本写到 `tool-output/` 下的文件，文件名是 `tool_` + 12 位十六进制时间前缀 + 随机后缀，metadata 里写 `truncated: true` 与 `outputPath`，正文里插一行 `[showing lines 1-N of M; full output saved to <path>]`。

`file` 类型的内容（图片、PDF）原样保留，不参与截断。

一个细节：只要生产者自己设过 `metadata.truncated`（true 或 false 都算），通用截断就整只跳过（`tool-output.ts:70`）。`shell` 工具正是利用这一点——它有自己的行/字节窗口，截断状态由自己维护。目录按小时清理，保留 7 天，只清匹配 `^tool_[0-9a-f]{12}` 的文件（`tool-output.ts:42-49`）。

### 2.5 CodeMode：内层调用也走同一条管线

默认开启时，大部分工具不进 provider 的工具表，而是被渲染成一份"工具目录"放进 instructions，模型只看到一个 `execute` 工具，用它写一小段受限的 JS 来组合调用。

内层调用不会绕过任何检查：`execute` 的 runtime 通过 `Run.executeTool` 回调回 `run`，每个内层调用仍然走 before hook → decode → execute → encode → after hook（`codemode/tool.ts:322-327`）。区别只在于**不各自截断、不各自持久化**——截断与落库由外层 `execute` 统一处理。

目录本身有预算：2000 字符（约 500 token），先放每个 namespace 的汇总行与 pinned 工具，再每轮每个 namespace 各放一条完整签名，超预算就停（`codemode/catalog.ts:50-52`）。所以模型看到的目录可能只是部分，其余靠运行时的 search 找。

### 2.6 代价

- 双表示：`output` 是给 CodeMode 的机器值，`content` 是给模型的文本；只有 `content` 与 `metadata` 落库，`output` 不进库。
- 规范化跑两遍：执行后一次，after hook 后一次。
- hook 可以改名，所以调用时要按新名字回查本次请求的 definitions，再映射回真工具；被 session context hook 摘掉的工具要报 `Tool is not available for this request`，而不是 `No tool named ...`。

## 3. 权限：最后一条匹配的规则胜出

{{< fig "diagrams/03-permission.svg" "权限判定：最后一条匹配的规则胜出" >}}

权限模型只有三个字段：`{action, resource, effect}`，`effect` 是 `allow | deny | ask`（`schema/permission.ts:56-66`）。

### 3.1 求值

`evaluate(...rulesets)` 把多个规则集按顺序拼起来，然后 `findLast` 找最后一条匹配的规则（`permission.ts:94-104`）——**后置规则覆盖前置**。没有匹配时默认 `ask`，不是默认允许。

通配符只支持 `*` 和 `?`，全串锚定；匹配前会归一化反斜杠，并把尾部的 ` .*` 变成可选（`util/wildcard.ts:3-16`）。

### 3.2 一次 assert 的决策顺序

1. **先判 deny**：命中就抛 `BlockedError`，在工具层被转成 `ToolFailure` 回到模型。
2. **合并已保存的 allow**：`always` 留下的规则参与求值。
3. **逐 resource 求值**：任一 resource 得到 `ask`，整体就是 `ask`。
4. **跑 `permission.evaluate` hook**：插件可以改 effect、附一条说明。
5. **无人值守降级**：auto-accept 模式下 `ask` 直接按 `once` 处理。
6. 得到最终结果：`allow` 放行、`deny` 抛错、`ask` 挂起。

### 3.3 ask 的往返

`ask` 会创建一个挂起请求、发 `permission.asked` 事件，然后 `Deferred.await`（`permission.ts:256-288`）。客户端弹窗，用户选 `once | always | reject`，回 `POST .../permission/:requestID/reply`。

- `once`：放行这一次。
- `always`：写 saved 规则，并唤醒同会话其它可放行的挂起请求。
- `reject + message`：以 `CorrectedError` 结束当前 Step，把用户的话带回给模型。
- `reject`（无 message）：走 defect 隧道穿透到请求层，才转成 `DeclinedError`。

最后两条的差别很微妙：带 message 的拒绝是"换个方式做"，不带 message 的拒绝是"这件事到此为止"。

### 3.4 默认策略与合并

agent 默认是 `*:* allow`，但读 `.env` 要问（`schema/agent.ts:46-50`）；内置 agent 反过来，deny-all + 逐项 allow（`plugin/agent.ts:60-115`）。config 里的 `permissions` 追加到每个 agent 的规则末尾，所以天然可以覆盖默认；路径类 action 还会展开 `~` 与 `$HOME`。

### 3.5 为什么"看不见"和"不能做"要分开

`whollyDisabled` 只处理 `resource === "*"` 的 deny，目的是让工具从目录里消失、省 token；它不负责执行鉴权。反过来，执行鉴权发生在工具内部，由工具自己决定查哪些 action 和 resources。这样设计的好处是：MCP 工具、插件工具、内置工具可以各自声明不同的检查粒度，而目录过滤只需要一个粗粒度的开关。

## 4. MCP：发现即注册，变化即重载

{{< fig "diagrams/04-mcp.svg" "MCP 工具是怎么长进工具表的" >}}

### 4.1 三种传输

| 传输 | 建立方式 | 生命周期 |
| --- | --- | --- |
| `stdio` | 自研 `McpStdio`，走 Environment/沙箱 | 随进程；退出即失效 |
| Streamable HTTP | 远端 URL，带 OAuth 的 loggedFetch | 可重连；token 可刷新 |
| `attached` | 客户端自己开好连接挂上来 | 由客户端管理 |

协议层还带一个 `?codemode=false` 探测，用来判断服务端是否支持 CodeMode 的调用约定（`mcp/client.ts:349-356`）。

### 4.2 工具发现与重载

首次发现时，一次性把工具注册进当前 Location 的工具表，命名空间就是 server 名（归一化后），工具名是 `<server>_<tool>`（`tool/mcp.ts:23-25`）。

之后如果工具列表变化，MCP 会发 `McpEvent.ToolsChanged`；订阅方做 100ms 防抖，然后调用 `tools.reload()`（`mcp/index.ts:122-141`）。注意是 **reload 而不是重新注册**——重新注册会改变注册顺序，破坏插件对工具的覆盖关系。

### 4.3 调用与失败

每次 MCP 工具调用前都要先 `permission.assert(action: "<server>_<tool>", resources: ["*"])`（`tool/mcp.ts:63-76`），然后才 `callTool`。结果里的文本、内联媒体、二进制分别处理，二进制落盘。

连接失败按 server 隔离：一台挂了只重连它自己，不影响其它 server，也不影响内置工具。这是"多 server 并存"能成立的前提。

## 5. 记忆不是一层，而是九层

{{< fig "diagrams/05-epoch.svg" "指令不是每轮重读：基线 + 增量 + epoch" >}}

把"记忆"当成一个模块会看不懂这套系统。它其实是九层材料，作用域与寿命各不相同：

| 层 | 内容 | 作用域 | 寿命 |
| --- | --- | --- | --- |
| 指令基线 | 渲染成第二个 system 段 | Session | 到下一 epoch / revert / fork |
| 指令增量 | 历史里的一条 system 消息 | Session | 与消息同寿，随压缩消失 |
| skill 目录 | 只有 id / name / description | Location | 随技能文件或配置变化重建 |
| skill 正文 | `<skill_content>` 或 skill 消息 | Session | 进历史后与消息同寿 |
| 会话历史 | `session_message` / `session_part` | Session | 直到 revert 或删会话 |
| 压缩摘要 | compaction 消息 + 新基线 | Session | 取代旧历史，可再压缩 |
| 路径指令 | synthetic 消息 + metadata 账本 | Session | 压缩后丢失，下次读文件重注 |
| KV | 目录/配置/插件/artifact 缓存 | 全局，跨会话 | 无 TTL，靠业务字段失效 |
| 文件 | AGENTS.md、SKILL.md、工具输出、blob | 项目或全局 | 由用户或保留策略决定 |

一句话：**真正的对话记忆只有一层——durable 的会话历史；压缩只是换了 epoch 与摘要。** 其余都是"会变化的系统提示材料"或"可复用的全局存储"。

### 5.1 skill：目录进提示，正文按需取

来源有三类：`directory`、`url`、`embedded`。配置里每个目录来源会自动追加 `<dir>/skill/` 与 `<dir>/skills/` 两个目录，兼容目录 `.claude/skills`、`.agents/skills` 也会被扫到；内置两个 embedded 技能：`opencode` 与 `report`。

发现规则很克制：

- 目录源用 BFS，深度 ≤ 6，忽略点目录；某目录只要有 `SKILL.md` 就算一个技能，并且**不再往下钻**；根层散落的 `*.md` 也算技能（`config/plugin/skill-scan.ts:7-45`）。
- URL 源先取 `index.json`，逐项校验路径不逃逸、不跨源，下载到 `global.cache/skills/<hash>`，用 `.opencode-version` 加 staging/rename 做原子换版。

**注入方式才是重点**：系统提示里只有目录——每个有 description 且未禁用 autoinvoke 的技能的 id/name/description，作为指令源 `core/skill-guidance`；正文只在模型调用 `skill` 工具时才取，返回 `<skill_content>`，最多附 10 个参考文件（`skill.ts:40-57`）。

所以 skill 与 instruction 的分工是：一个常驻、要短、要稳定；一个按需、可以长。

顺带记录一处**文档与实现不一致**：`skill/instructions.ts` 的措辞暗示用户直接调用技能时也以 `<skill_content>` 呈现，但 `Session.skill()` 走的是普通消息投影，到模型侧是裸文本；只有 prompt 的 `skills` 附件才会被包成 `<skill_content>`。不影响行为，但会让读代码的人多绕一圈。

### 5.2 指令：串接，不覆盖

只认 `AGENTS.md` 这个文件名，三路来源：

1. 全局 `global.config/AGENTS.md`；
2. 项目祖先链：从 location 目录向上到项目根，**近→远**；
3. config 的 `instructions`：URL（加载时抓一次，失败保留上次）或路径/glob。

合并方式是**串接而非覆盖**：每份渲染成 `Instructions from: <path>` + 正文，空行连接，整体作为同一个 system 段。这意味着**没有显式优先级裁决**——两份 AGENTS.md 冲突时，靠的是模型自己读懂顺序，而不是后一份覆盖前一份。

还有一路容易被忽略：`read` 或列目录成功后，会向上找目标目录到 Location 根之间的 AGENTS.md，去重后作为 durable synthetic 注入；metadata 里的 `{instruction:{paths}}` 就是去重账本。这些 synthetic 在压缩后消失，于是下次读同一路径会**重新注入**。

### 5.3 基线、增量与 epoch

`instruction_state` 一行五列：`session_id / epoch_start / through_seq / initial_values / current_values`。

- **基线**：首次 prepare 时无行，`initial=true`、`previous={}`；commit 发一条 delta 并带 `metadata.instructions.initial=true`（无文本），投影写入 `epoch_start = through_seq = seq`，两套 values 相同。此后基线冻结，每次请求由它渲染成第二个 system 段。
- **增量**：每个 Step 前（含重试）都跑一次 prepare，按 key 比对 sha256，只有变化的 key 进入 delta；没变化就直接返回、不发事件。增量文本在发布时**冻结进事件**，因为事后无法重建 Location 作用域的源。
- **epoch**：`compaction.ended` 时 `advanceEpoch`，令 `epoch_start = through_seq = 本次 seq`，并把 `initial_values := current_values`。压缩提示词明令摘要不写指令类内容，所以压缩后历史被替换、基线带着最新值重冻，不需要重发 delta。
- **revert**：删掉 `through_seq >= 边界` 的整行；如果边界前的折叠仍与存活历史一致就保留（为了保住 prompt cache）。
- **fork**：不复制状态行，而是携带父会话的 `current` 与条目快照，投影成子会话的 `initialize`——**以父的最新值为基线，而不是 fork 边界处的值**。
- **move**：完全不碰指令状态；下一个 Step 的 prepare 把新 Location 的差异当普通 delta 发出去，基线不重冻。

### 5.4 KV、blob、工具输出是三样东西

- **KV**：全局 `key → JSON`，只有 get/set/remove/scan。它**不是 config**——config 是多层文档、有文件与监听，KV 只是小缓存，不参与指令或权限解析，也不暴露给模型。写它的都是基础设施：模型目录、wellknown、websearch 选中项、repository 缓存、artifact 索引、插件 storage，以及调度器的 leader 租约。没有 TTL，过期靠业务字段。
- **blob**：内容寻址的媒体（SHA-256），带引用计数，没人引用就删。
- **instruction_blob**：存指令源的**规范化 JSON**，哈希打的是 canonical JSON（键排序）而不是字节；跨会话共享，无引用计数、无回收。
- **tool-output 文件**：超限的工具输出写到 `global.data/tool-output/`，按 7 天清理，不寻址、不去重、不入库。

四者长得像，用途完全不同——把它们混为一谈，就会在"为什么删了消息 blob 还在""为什么指令改了摘要要作废"这类问题上卡住。

## 6. 执行面：四种寿命完全不同的"活物"

工具调用之外，系统里还有四类长期运行的东西：shell 命令、PTY 终端、子会话、后台任务。它们的寿命与归属完全不同，混在一起会互相踩。

### 6.1 shell 与 PTY 不是一回事

| | shell 工具 | PTY 会话 |
| --- | --- | --- |
| 定位 | 给模型执行命令 | 给人用的终端 |
| 输出 | 落文件 `<data>/shell/<projectID>/<id>.out` | 内存环形缓冲，上限 2MB |
| 窗口 | 无 | 有（可 resize） |
| 持久 | 退出后仍可读最近 25 个，文件留 7 天 | 进程重启全丢 |
| 进上下文 | 是（有界尾部） | **否** |

一句话概括：**只有 shell 有工具包装，PTY 输出永远不进上下文**——它只通过 `pty.*` 的 HTTP/WebSocket 提供给 TUI 与桌面端。

超时策略也不一样：前台命令默认 2 分钟，`background: true` 时缺省无超时；超时与进程退出赛跑，先到者定状态，而超时只是 kill，不会取消已排队的后续逻辑（`shell.ts:394-421`）。

回到上下文的是有界尾部（2000 行 / 50 KiB），截断时附完整输出的路径；后台任务完成时另发一条 synthetic 通知，把输出带回来（`tool/plugin/shell.ts:160-172`）。

### 6.2 子会话是"另一条忙期"

subagent 不是"在同一个循环里再调一次模型"，而是真的开了一个会话：

- `sessions.create({ parentID })` 建子会话，深度沿 `parentID` 链向上数，上限取 `experimental.subagent_depth`，**默认 1**——也就是默认不许嵌套（`tool/plugin/subagent.ts:123-139`）。
- 子会话有自己的 claim 与 busy period（key 是子会话 ID），所以父子并发是同一进程里两条互不阻塞的 fiber，不是线程。
- **前台模式**：父会话阻塞在 `jobs.block({ id: child.id })`，父的 drain 覆盖子的运行时长；中断时联动 `sessions.interrupt(child)` 与 `jobs.cancel(child)`（`subagent.ts:234-239`）。
- **后台模式**：父立即结束本轮，子跑在 Job 的全局 scope；完成由 `SubagentCompletion.deliver` 向父注入一条 synthetic 消息并唤醒父会话（`subagent-completion.ts:37-44`）。父被中断不会波及后台子会话。

这个设计解释了为什么"让子代理干活"不会把父的上下文撑爆：父只看到子返回的最终文本，看不到子的 transcript。

### 6.3 background job：内存是真相，DB 只是恢复点

`Job` 是进程级服务，内存 Map 是唯一执行真相，每个 job 一个 fork scope，按 id 去重（`job.ts:294-324`）。只有"可恢复、且被转到后台"的 job 才落 `job` 表（主键是通知 id，含 `session_id`、`child_session_id`、`kind`、`recovery` JSON）。

会话即将空闲时，`experimental.idle` hook 会把后代仍在跑的后台任务概要交给插件（`runner/llm.ts:180-181`），位置回收也把这些任务算作占用。消费后删行，内存只保留 25 条已消费历史。

### 6.4 schedule：用 KV 选主

`schedule` 表存 `action / check / when / state` 四个 JSON 列，外加 `next_due_at`、`retry_at`、`run_count`（`schedule/sql.ts:10-45`）。全局 run 循环按 tick 选出 `active` 且到期的行，**用 KV 表写 leader 租约**，防止多个进程重复触发（`schedule.ts:539-553`）。

`when` 支持 `in / at / every / cron`；cron 用 Effect 的 `Cron` 解析后缓存成下一个 `next_due_at`。几个细节值得记：

- 秒位只能有一个值，最短间隔 1 分钟（带 check 时 15 秒）；
- DST 回拨会跳过错位槽，避免同一时刻触发两次；错过的槽只跳不补；
- 触发动作是向会话 `prompt`，`delivery: "steer"`，消息 id 由 `run_count` 派生，保证重试幂等；
- 连续失败 3 次自动暂停（`schedule.ts:303-342`）。

### 6.5 worktree 只是目录

创建默认落在 `<data>/worktree/<projectID 前 6 位>/<slug>`，目前只有 git 策略（`worktree/strategies.ts:51-52`）。它与会话的关系是**间接**的：不会自动绑定，需要模型显式调 `session_move`；move 以 steer 控制项进 inbox，在安全步边界生效，切换前先关掉 model transport（`runner/llm.ts:108-125`）。

### 6.6 移动、重启、打断时会发生什么

- **会话移动**：不取消长任务。Job 是全局的、Shell 是 location 的；旧 location 若被 TTL 回收，其 finalizer 会中断仍在跑的命令。移动后的后台 shell 会不会被误杀，取决于占用判定，这一点我没能从静态代码完全确认。
- **进程重启**：内存 job 全丢。`job` 表里的未完成行由 `SessionRestart.resumeSuspendedSessions` 收尾——shell/image 直接补一条"服务重启已取消"，subagent 则尝试 resume 子会话，单轮预算 10 次（`restart.ts:26-34, 147-215`）。
- **用户打断**：只打断本进程该会话的 drain，后台 job 继续跑，完成后仍会 synthetic 通知；删除会话才会连带清掉该会话与子会话的 job。

## 7. 外部存储：一个 SQLite 文件扛下所有

{{< fig "diagrams/06-storage.svg" "23 张表里，只有 8 张在热路径上" >}}

全部持久状态在一个文件里：`<data>/opencode.db`，上层只有一层 Drizzle-over-Effect 的 SQL 客户端（`database/database.ts:14`）。23 张表可以分成六类：

| 类 | 表 | 说明 |
| --- | --- | --- |
| 会话（4） | `session_v2`、`session_message`、`session_part`、`session_inbox` | 都以 `session_v2.id` 为级联外键根 |
| 项目（4） | `project`、`project_directory`、`worktree`、`workspace` | 工程根、git worktree、远端 workspace |
| 凭据（4） | `account`、`account_state`、`control_account`、`credential` | OAuth 账号与集成凭据 |
| 事件（2） | `event`、`event_sequence` | 按 aggregate 追加的事件日志 |
| 调度（2） | `schedule`、`job` | 定时任务与后台任务通知 |
| 杂项（7） | `kv`、`permission`、`blob`、`blob_ref`、`instruction_*` | 插件状态、权限白名单、媒体与指令正文 |

### 7.1 为什么 message 和 part 是两张表

`session_message` 一行是消息的"信封"：`id / session_id / type / seq / data(json)`；`session_part` 以 `(message_id, ordinal)` 为主键，一行一个 assistant 分段（`session/sql.ts:86-128`）。

拆开的动因是**写放大**。assistant 流式输出时几乎每个事件都要更新消息；如果整条消息（含不断增长的工具输出）每次重新序列化写回一行，WAL 与页锁竞争会很重。拆成 part 后只 upsert 变化的 ordinal。

不过这里有个灰度中的细节：`experimental.part_rows` 默认是 **false**，也就是默认仍然把正文放在 `session_message.data.content` 里，开启后才写 `session_part` 并在信封里留空数组（`projector.ts:539-560`）。所以 2.2.5 的同一个库里，两种形态是并存的。

### 7.2 JSON 列：用得多，也越界了

用 JSON 的列不少：消息 `data`、part `data`、inbox `payload`、`schedule` 的四个字段、`event.data`、`kv.value`、`project.sandboxes/commands` 等。

整体边界是"事件/报文/可演进 schema 用 JSON，查询维度必须提成列"——`type`、`seq`、`status`、`next_due_at`、`time_created` 都是列并有索引。但代码里也有明显的越界：

- 有 70 多处 `json_extract` 用来过滤或排序，比如按 `$.time.completed` 筛未完成消息（`projector.ts:212-214`）；
- 统计把正文当表扫：`json_each(message.data, '$.content')` 与 `session_part` 三表 union（`stats.ts:211`）；
- `session_v2.model`、`permission`、`project.sandboxes` 这类业务字段直接存 JSON；
- 最扎眼的是调度器把 **leader 租约**的 owner/until 放进 KV 的 JSON，再用 `json_extract` 写进 upsert 的 WHERE（`schedule.ts:541-561`）——既没索引也没类型约束。

这些不是"不知道"，更像是演进过程中被性能与开发速度拉扯的结果。写在这里，是因为它正好是"业务字段不进 JSON"这条规范的反面教材。

### 7.3 索引：8 个真正重要的

| 索引 | 服务什么 |
| --- | --- |
| `session_message(session_id, seq)` UNIQUE | 会话内全序，重放/分页的基础 |
| `session_part(message_id, ordinal)` PK | 按序取分片 |
| `session_message(session_id, type, seq)` | 取最后一条未完成 assistant |
| `session_inbox(session_id, enqueued_seq)` UNIQUE | 入队幂等 + FIFO |
| `event(aggregate_id, seq)` UNIQUE | 事件严格递增 |
| `schedule(next_due_at) WHERE status='active'` | 调度只扫到期行 |
| `session_v2(time_suspended)` | 启动恢复只扫持有 claim 的会话 |
| `permission(project_id, action, resource)` UNIQUE | 权限幂等 upsert |

### 7.4 并发假设：单进程、单写者

打开库后依次设置 `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`cache_size=-64000`、`foreign_keys=ON`（Durable Object 环境跳过），文件强制 0600。

并发假设很明确：**单进程**。durable 事件按 aggregate 用 KeyedMutex 串行，并用 `begin immediate` 抢写锁；同一路径的 bootstrap 用 per-path 信号量串行。WAL 与 5 秒 busy_timeout 只是兜底，多进程争锁超过 5 秒直接视为致命失败——没有应用级重试。写者唯一性靠"最终只有一个 server 进程"这个部署事实保证。

### 7.5 迁移：只有 up，没有 down

迁移命名是 `<UTC 时间戳>_<名字>`，当前 59 个。用 `drizzle-kit generate` 从 `packages/core/schema.json` 生成增量 SQL，写进 `database/migration/`，同时重生成全量 `schema.gen.ts` 与 registry；CI 用 `--check` 校验"没有未生成的迁移"。

`Migration` 类型只有 `up`，**没有 `down`**——回退只能整库恢复备份。空库一次性跑全量建表并把所有迁移标记完成；已有库按序逐条执行，每条一个事务，写入 `migration(id, time_completed)`。

### 7.6 snapshot：借 git 的对象库

只在 git 项目、且没被 `snapshots:false` 关掉时拍。时机是**每个 Step**：发请求时 fork 一个 start 快照，Step 结束拍 end 快照并算变更文件。

它不复制文件：快照目录是一个独立 git 仓库，通过 `objects/info/alternates` 借用真实仓库的对象库，只新增 tree/commit 对象；快照 ID 就是 tree 的 SHA（`snapshot.ts:94-144`）。revert 时把每个文件映射到"第一次被该 Step 触达前"的 tree，用 `git checkout <tree> -- <path>` 恢复，tree 里不存在的路径会被删除。

### 7.7 blob：内容寻址 + 引用计数

`blob.hash` 是原始字节的 SHA-256，以 hash 为主键，同批同 hash 只入一次、`onConflictDoNothing`。`blob_ref(session_id, message_id, hash)` 记录引用，写入时登记、替换时补差、删除时 `release`——先删 ref，再删没有任何 ref 的 blob。

**没有后台孤儿清扫**：崩溃或异常路径留下的无引用 blob 不会被自动回收，只能等下次同 hash 复用时"复活"。这个功能由 `media_blob` flag 控制，默认关闭；关闭时媒体仍以 base64 data URI 内联在消息 JSON 里。

## 8. 插件：19 个 hook 与三种作用域

插件是一个 `{ id, effect(ctx) }` 的值，`ctx` 暴露二十多个 domain（`packages/plugin/src/effect/plugin.ts:64-69`）。它的能力边界由 hook 表和 domain 共同定义。

### 8.1 19 个 hook

| 域 | hook | 失败语义 |
| --- | --- | --- |
| tool | `execute.before` | **可失败**，直接拒绝调用 |
| tool | `execute.after` | 不可失败，错误被吞 |
| permission | `evaluate` | 不可失败，可改 effect |
| shell | `create.before` | 不可失败 |
| session | `prompt` / `context` / `compaction` / `generate` / `title` / `model.request` / `http.request` / `http.response` / `retry` | 不可失败 |
| session | `experimental.ws.handshake` / `send` / `receive` / `experimental.idle` | 不可失败 |
| aisdk | `sdk` / `language` | 不可失败 |

只有 `tool.execute.before` 允许让调用失败，其余 hook 抛错都会被吞掉。理由是"插件坏了不应该让整个会话挂掉"，代价是插件作者不能用异常做控制流。

### 8.2 加载顺序

加载顺序决定了谁覆盖谁：

```text
内置插件 → SDK 宿主 → 实例绑定 → config 里的 plugins
```

来源可以是 npm 包（会触发安装）、本地目录（watch 热重载）、或 `-target` 形式的删除指令。本地目录的插件改动后会重新 `import` 并重建，不需要重启进程。

### 8.3 分面

同一个插件包可以同时提供 `tui` 与 `gui` 两个入口（`packages/plugin` 的 exports 里有 `./tui`、`./gui`、`./effect`、`./runtime`），分别跑在终端与浏览器里。`peerDependencies` 把 electron、solid、opentui 全标成可选——插件不装它们也能工作，只是没有对应分面。

## 9. 这些机制怎么咬合

把上面几节放在一起看，会发现四个反复出现的设计动作：

1. **Step 边界是唯一安全点。** steer、compaction、move、interrupt、工具结果回填，全部只在这个边界生效。代价是响应粒度变粗，好处是不需要为"工具执行到一半改历史"设计锁。
2. **先记账，再动手。** 工具调用在副作用之前落库，执行声明在开始前写入，指令增量在进入请求前落库。所有"不可逆"的动作前面都有一行记录。
3. **投影是唯一的读模型。** UI 读消息表，模型读 lowering 后的消息，两者都来自同一批事件；不存在"内存里一份、库里一份"。
4. **失败就地隔离。** 一个 MCP server 挂了、一个插件坏了、一个工具 schema 非法，都只影响自己。系统的默认行为是"跳过并记日志"，不是"整体失败"。

代价也很集中：这套系统对"每步重读"和"事件投影"的依赖很深，长会话的每步固定开销随历史线性增长；Effect 的 Layer/Scope 心智门槛高；单写者 SQLite 决定了它暂时只能是单机架构。

## 10. 证据索引

| 机制 | 主要文件 |
| --- | --- |
| 忙期与门铃 | `session/run-coordinator.ts`、`session/execution.ts` |
| Step 循环与六种结局 | `session/runner/llm.ts`、`runner/step.ts` |
| 步数上限 | `runner/max-steps.ts`、`runner/llm.ts:253` |
| 工具注册与快照 | `tool.ts`、`tool/runtime.ts` |
| 截断与落盘 | `tool-output.ts` |
| CodeMode | `codemode/tool.ts`、`codemode/catalog.ts` |
| 权限 | `permission.ts`、`schema/permission.ts` |
| MCP | `mcp/*`、`tool/mcp.ts` |
| 指令与 epoch | `session/instruction-state.ts`、`session/instructions.ts` |
| skill | `skill.ts`、`tool/plugin/skill.ts` |
| 插件 | `plugin/hooks.ts`、`plugin/supervisor.ts` |

---

*本文的结论按模块分工调研、再由主代理回源抽查；涉及具体行为的段落均可在上表文件中找到对应实现。行号会随版本漂移，定位时以符号名为准。*
