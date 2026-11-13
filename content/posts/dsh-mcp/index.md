---
title: "MCP：把外部服务器接进工具表"
date: "2026-11-13T11:30:00+08:00"
summary: "一个 MCP 服务器掉线之后，模型手里的工具表先保持原样，十次重连都失败才消失；资源则从来不是工具，而是三个固定工具加一个 server 参数。这篇拆 mcp-manager、mcp-client、mcp-resources 的所有权、命名与失败路径。"
description: "连接状态机、工具换代的原子性、listChanged 与请求头的关系、resources 的共享工具设计，以及审批为什么落在通用流水线上。"
tags: ["harness", "architecture", "agent"]
slug: "dsh-mcp"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准。
> 第二季第 5 篇。上一季：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)；本季导览：[《第二季：把主干两侧补齐》](/posts/dsh-season-two.html)。

在设置里加一个 MCP 服务器，保存，下一轮请求的 `tools[]` 里多出十几项，名字长这样：`mcp__github__create_issue`。模型开始调用它们，参数按服务器声明的 inputSchema 填，结果变成普通的工具结果。

然后你把服务器进程 `kill -9` 掉。刷新页面，工具表还在。模型再调一次，这次拿回一段红字：`MCP transport closed`。再调几次，还是能调，每次都失败。直到重连预算耗尽，下一轮请求的 `tools[]` 突然少掉那十几项，历史里多出一条 developer 消息，逐条写着 `tool-removal`。

同一套系统里还有第二种外部能力：resources。你以为每个资源会变成一个工具，结果只有三个，长这样：`list_mcp_resources`、`list_mcp_resource_templates`、`read_mcp_resource`，每个都要求你显式传 `server` 参数。服务器有几百个文件，工具表还是只多三项。

这两个现象指向同一组所有权问题：连接归谁、工具表归谁、能力目录归谁。DSH 把答案拆成三个包，`mcp-manager` 管一群服务器的生命周期，`mcp-client` 每个服务器一个实例、管连接与工具换代，`mcp-resources` 管共享的资源工具和提示词章节。三者的边界不是按代码量切的，是按「谁在失败时负责什么」切的。

这篇按一次连接的完整路径走：装配、握手、注册、调用、换代、失败。第一季讲过的工具注册表与执行流水线不重复，需要的先读[《工具注册表与作用域》](/posts/dsh-tool-registry-scope.html)和[《工具调用的四段流水线》](/posts/dsh-tool-pipeline.html)。

## 0. 结论

- `mcp-manager` 是一个 fleet 控制器：它把 Settings 里的条目和插件运行时注册的条目合成一份清单，负责校验、挂载、卸载、重启、探测、聚合状态。它不碰 JSON-RPC。
- `mcp-client` 是每个服务器一个的 Cordis 插件：它拥有一代连接、重连预算、工具注册与注销、服务器指令、资源代理。它的生命周期挂在插件 fiber 上，热替换后同名工具原地复现。
- `mcp-resources` 是一个全局服务：它注册三个固定工具，维护「哪个作用域能看到哪些服务器」的目录，并在系统提示词里列出这些名字。它的注册活得比任何一个客户端都久。
- 工具名是 `mcp__<serverName>__<rawName>`，超过 64 字符或含非法字符时用 sha256 前 12 位做后缀。公开名只用于展示与调用，反解析永远不做。
- 工具换代是两阶段的：先拉全量清单，再一次性换掉旧注册。注册冲突会回滚整批，模型看到全量或零，不存在半套工具。
- 掉线不等于下线。重连期间工具继续留在表里，调用失败以工具结果的形式回到模型；只有预算耗尽才注销，并补一条 `tool-removal` 消息。
- MCP 没有自己的审批通道。工具进了注册表就是普通工具，审批走 `tools/pre-execute` 与 `ctx.approval`，模型看到的拒绝原因是一行 `Error: ...`。

## 1. 三个包，三种所有权

先看装配。每个随附 profile 都挂一次 `mcp-resources`，它是全局服务，不依赖任何服务器存在。Web 与 Desktop profile 再挂一次 `mcp-manager`，因为只有这两个宿主需要设置页、状态流和重启按钮。`mcp-client` 不直接出现在 `cordis.yml` 里，它由 manager 在运行时逐个挂载，一个条目一个实例。

这样切的第一层理由是失败归属。服务器崩了，坏掉的是一个 client 实例；设置页崩了，坏掉的是 manager；而资源工具是全局的，不能因为某个服务器掉线就消失，否则模型会以为「这个部署没有 MCP」。

`mcp-manager` 的数据结构是 `ManagedRecord`：条目快照、客户端 handle、进行中的探测、失败原因、是否已退役。它维护两个来源。第一个是 `Config.servers`，标了 Volatile，设置页按 Loader entry id 写入，profile 继承时还要过 revision 检查。第二个是插件在生命周期内调 `register(name, entry)` 注册的条目，比如某个插件自带一个内部服务器。任何一边变化都走 `reconcileOnce`：先卸载变化或消失的，再按顺序挂载新条目或标记 `disabled`。

`mcp-client` 的数据结构是一代连接加一个同步链。它导出的 Config 是 stdio 与 streamable-http 的联合类型：stdio 有 `command/args/env/cwd`，http 有 `url/headers`，两边共享 `serverName`、超时、指令字节上限、重连配置。`serverName` 有格式约束 `/^[A-Za-z0-9_-]{1,32}$/`，因为最终要拼进工具名。

`mcp-resources` 的数据结构是分层目录。`ScopedLayers` 让每个 agent 作用域看到自己那份服务器名集合，继承来的 provider 仍然可见。首个 provider 进入某个作用域时注册三个工具，最后一个离开时注销。服务自己持有一个 `selfCtx`，所以工具注册不随首个客户端的卸载而消失。

manager 对外只有五个入口，全是给宿主用的：

| 入口 | 作用 | 谁调 |
| --- | --- | --- |
| `list()` | 返回配置里、插件注册的、以及仍在保留中的全部条目与状态 | 设置页 |
| `register(name, entry)` | 插件在生命周期内加一个服务器，自动生效 | 插件 |
| `watch(signal)` | 流式推送全量列表，订阅时先给当前快照 | 设置页 |
| `restart(name)` | 串行队列里卸载再挂载，未知名字抛错 | 设置页按钮 |
| `probe(name, options)` | 新开一条连接跑 `listTools` 再关闭，不碰活实例 | 设置页与 CLI |

`restart` 和 `probe` 都标了 `@Remote`，因为它们要跨进程被调用；`list` 与 `register` 留在进程内，避免把内部对象图序列化出去。这五个入口之外，manager 没有别的公开方法，客户端也没有暴露给宿主的控制面接口。

{{< fig "diagrams/01-three-packages.svg" "图注：三个包的所有权与调用方向。manager 只挂载与聚合，client 拥有连接和工具注册，resources 拥有共享工具与提示词章节；箭头上的动作就是它们之间全部的接口。" >}}

有一条容易看错的边界：manager 调 `probe(name)` 时不复用活着的客户端。它解析模板、新开一条连接、`listTools`、关闭，全程不碰正在服务的实例。所以探测一个已经挂掉的服务器不会影响它现有的重连节奏，探测一个正常服务器也不会扰动它的工具表。代价是探测本身会消耗一次握手预算，对冷启动很慢的服务器，探测超时可能比真实连接更早发生。

不这样切会出什么事：如果 manager 直接持有连接，那么一次设置页刷新就会重建所有客户端，重连预算、工具注册、进行中的调用全部重置；如果 resources 的工具由每个客户端各注册一份，十个服务器就有十份同名工具，注册表直接抛冲突。

## 2. 连接的一生：从 spawn 到 ready

一个 client 实例的状态是一个封闭联合：

| 状态 | 含义 | 携带什么 |
| --- | --- | --- |
| `connecting` | 正在建传输、握手、首次拉工具 | `attempt` |
| `connected` | 首次同步完成 | `toolCount`、`connectedAt` |
| `reconnecting` | 掉线后等待下一次尝试 | `attempt`、`maxAttempts`、`nextRetryAtMs`、`lastError` |
| `waiting-credentials` | 模板变量缺失，等人补凭据 | `lastError` |
| `failed` | 不会再自动重试 | `reason`：`gave-up`、`reconnect-disabled`、`unclosed-generation` |
| `closed` | 已主动关闭 | 关闭结果 |

启动预算是一个整体，不是每步各算各的。`startupTimeoutMs` 从进入 `connectGeneration` 开始计时，`remaining()` 同时包住三件事：凭据模板解析、`client.connect(transport)`、首次 `syncTools`。任何一步超时都走同一条 catch：关掉这代连接、把 readiness 解析成错误、按策略决定是重连还是停在 `waiting-credentials`。默认 30 秒，manager 挂载时把它写进 Config，所以设置页里改的是这个总数。

手写 patch 时，一个 stdio 条目的形状是这样：

```yaml
- id: mcp-client
  config:
    transport: stdio
    serverName: pg
    command: npx
    args: ["-y", "@modelcontextprotocol/server-postgres"]
    env:
      DATABASE_URL: "!!js process.env.DATABASE_URL"
    startupTimeoutMs: 60000
    toolCallTimeoutMs: 15000
    reconnect:
      initialDelayMs: 500
      maxDelayMs: 30000
      maxAttempts: 10
```

`env` 里的 `!!js` 只在 `cordis.yml` 的 plugin config 下可用，其他位置写一个字符串就被当成字面量。凭据不要直接写值，用模板引用，客户端会在 `credentials/reference-updated` 时重建连接。

握手阶段的关闭事件有一个坑。`client.onclose` 只有在 `connectedAt` 已经设过时才当作「掉线」处理；握手期进程退出走的是 `connect` 抛错那条路。两条路最终都汇到 `scheduleReconnect`，但前者知道这次连接活过多久，后者不知道。这个区别后面会用到。

重连预算按「一次 outage」计算，不按进程生命周期算。`failedAttempts` 在每次失败后加一，延迟从 `initialDelayMs` 翻倍到 `maxDelayMs`，默认 500ms 起、30s 封顶、10 次。关键在稳定窗口：一条连接活过 `maxDelayMs`，下一次掉线时 `failedAttempts` 归零。所以「每天早上断一次、每次立刻连上」的服务器永远用不完预算，而「连上两秒就崩」的崩溃循环会很快耗尽。

耗尽之后做两件事：把状态置为 `failed/gave-up`，然后 `syncChain = syncChain.then(clearTools)`。注销排在同步链尾部，是为了不和正在进行的工具换代交叉。日志原文是 `giving up after N consecutive failed reconnect attempts — tools unregistered; reload the plugin or restart the Host to reconnect`。这句日志是运维能找到的唯一线索，因为状态流只到 UI，模型看不到。

关闭也有一套协议。`dispose()` 先置 disposed、abort lifetime、停掉重连 timer，再关当前 generation。关闭过程本身有 5 秒预算：等 pending 操作、等同步链、等凭据检查，超时就把 `closeConfirmed` 置 false。manager 的 `unmount` 读到不完整的关闭结果，会把这条记录标成 `failed/cleanup`，并在下一次 `restart` 时拒绝执行，提示 `cleanup for "<name>" is incomplete; restart the Host`。宁可让用户重启宿主，也不让两个进程同时持有一个 serverName。

HMR 是这套结构里容易忽略的一条。状态单元存在 `WeakMap<Fiber, McpStatusCell>` 里，热替换时新实例拿到同一个 fiber 的 cell，只换 sink；工具名与 `serverName` 预留也按注册作用域复用。改一行代码触发 reload，设置页看到的状态不会闪回 `connecting`，工具表也不会先消失再出现。代价是旧实例的清理必须完成，否则新实例直接拒绝启动。

{{< fig "diagrams/02-connection-state.svg" "图注：连接状态机。启动预算同时覆盖握手与首次工具同步；稳定窗口决定一次 outage 的预算是否归零；预算耗尽后工具才注销。" >}}

自己搭的时候，把 `startupTimeoutMs` 调到服务器冷启动的 2 倍以上，并且确认服务器在 `initialize` 返回之前不要做重活。很多 Python 服务器把 `listTools` 写成懒加载，第一次调用要 import 半个科学计算栈，30 秒根本不够。反过来，如果你的服务器会在启动时扫整个仓库，把扫描放到后台，让 `listTools` 先返回一份静态清单。

不这样会出什么事：没有稳定窗口，一个每 40 秒被 OOM 杀一次的服务器会在几分钟内耗尽预算，而它其实一直在正常工作；没有关闭确认，热替换会同时留下旧进程和新进程，两个都叫同一个 `serverName`，工具注册随机失败。

## 3. 服务器指令：第三条进入提示词的通道

MCP 的 `initialize` 结果里可以带一个 `instructions` 字段，服务器用它说明「我该怎么被使用」。DSH 把这段文本原样放进系统提示词，位置在工具说明那一组里，每个服务器一节。

装配发生在 `registerServerContext`：客户端在自己的注册作用域里 `ctx.inject(['systemPrompt'])`，注册一个名为 `mcp:<serverName>` 的 section，order 取 `MCP_SERVERS`，`interpolate: false`，`text` 是一个闭包，读当前这代连接的 `instructions()`。所以系统提示词里看到的是最新一次成功握手的文本，不是启动时的快照。

文本格式固定：

```
### MCP server: pg

Run SELECT-only queries. Prefer the read_replica role.
```

标题由客户端加，正文是服务器的原文，只做 `trimEnd()`。`interpolate: false` 是必须的：服务器文本里可能出现 `{{ ... }}` 或 `${...}`，如果走模板插值，要么被当成变量替换成空串，要么在组装期抛错。服务器指令是数据，不是模板。

字节上限由 `maxInstructionBytes` 控制，默认 32768。超限时连接直接失败，抛 `server instructions exceed maxInstructionBytes (32768)`，然后按普通连接失败走重连，最后耗尽预算。这里没有截断选项，理由是一段被腰斩的指令比没有指令更危险：模型会读到半条规则，然后按它行动。代价是一台指令超长的服务器永远连不上，运维只能改服务器或调大上限。

指令的发布时机和工具绑定。`serverInstructions` 只在「连接成功、工具同步成功、连接仍然有效」三个条件都满足之后才写入；重连期间读到的还是上一代文本，因为旧文本描述的工具可能还在表里。`clearTools()` 会同时把 `serverInstructions` 置空，所以给弃之后指令和工具一起消失，提示词里不会留下一个指向不存在工具的说明书。

这段文本每次请求都付 token。一个 8KB 的指令块在 100 轮的会话里就是 800KB 的输入，而且它在系统提示词前缀里，缓存命中时只算一次写入。服务器作者常把整个 API 文档塞进 instructions，这在 MCP 层面合法，在宿主层面很贵。DSH 给的上限是硬闸，不是建议值。

指令文本的变化只发生在一次成功握手之后，所以它总和工具表变化同批出现。如果服务器每次启动生成带时间戳或随机 ID 的 instructions，每次重连都会重写系统提示词前缀，缓存全废。给服务器作者的规则和工具描述一样：指令要稳定，把易变的东西放进工具结果。

不这样会出什么事：如果把指令拼进某个工具的描述，改一次指令就改一次 `tools[]`，整段前缀缓存作废；如果把指令作为 user 消息插进历史，它会随着每一轮重复出现，而且模型会把它当成用户说的话。

## 4. 工具怎么变成 ctx.tools 注册

`syncTools` 分两阶段。第一阶段只读：如果服务器没有声明 tools capability，直接得到空数组；否则调 `client.listTools(undefined, { cacheMode: 'refresh', signal })`。拿到清单后先在内存里建 `Map<publicName, ToolDefinition>`，同时检查两件事：同一个公开名出现两次，抛 `server listed tool "<name>" more than once — invalid tool list`；公开名超过 64 字符或含非法字符，走改名逻辑。

第二阶段才写。写之前重新问一次 `eligible()`：拉清单是慢 await，期间这代连接可能已经被放弃，或者插件已经被卸载。答案是 false 就返回上一代注册，什么都不动。答案是 true 就先 dispose 旧的一批，再逐个 `ctx.tools.register(definition)`。任何一个注册抛错，回滚已经注册的那部分，抛 `McpToolRegistrationError`，日志写 `tool registration failed, no tools registered`。

这个顺序保证模型看到的是全量或零。不会出现「前五个工具注册成功、第六个冲突」的中间态，因为中间态会让模型基于残缺的工具表做计划，而下一轮又变回去。

命名是另一层。每个工具的身份是 `(serverName, rawName)` 二元组，公开名是 `mcp__<serverName>__<rawName>`。raw name 只在上线调用时用，公开名从不反解析。原因是服务器可以用任何字符命名工具：点、斜杠、空格、emoji 都合法，但模型工具名只接受 `[A-Za-z0-9_-]`。发生替换或截断时，公开名变成 `前 51 字符 + _ + sha256(serverName + NUL + rawName) 的前 12 位十六进制`。51 + 1 + 12 = 64，正好卡在上限。

用哈希而不是序号，是为了让名字稳定。同一个工具在不同进程、不同会话、不同机器上都得到同一个后缀；两个不同身份也不会塌缩成同一个名字。代价是名字不可读，运维在日志里看到 `mcp__fs__read_file_a1b2c3d4e5f6` 时只能查表。

函数的全部逻辑就这几行：

```ts
export function publicToolName(serverName: string, rawName: string): string {
  const joined = `mcp__${serverName}__${rawName}`
  const normalized = joined.replace(INVALID_NAME_CHARS, '_')
  if (normalized === joined && normalized.length <= MAX_PUBLIC_NAME_LENGTH) return normalized
  const hash = createHash('sha256').update(`${serverName}\0${rawName}`).digest('hex').slice(0, HASH_LENGTH)
  return `${normalized.slice(0, MAX_PUBLIC_NAME_LENGTH - HASH_LENGTH - 1)}_${hash}`
}
```

注意 `\0` 分隔符。没有它，`(a, b_c)` 与 `(a_b, c)` 会拼出同一个前缀，哈希也就失去区分能力。这类细节在单元测试里看不出来，只有构造对抗性的名字才暴露。

作用域这一层要分清两件事。`serverName` 的预留是按注册作用域做的，模块级 `WeakMap<object, Set<string>>` 以 `scopeOf(ctx) ?? ctx.root` 为键；同一个作用域里两个 client 用同一个 `serverName`，第二个在加载期就抛 `serverName "<x>" is already in use by another mcp-client instance`。不同 agent 作用域可以复用同一个名字，因为它们的工具表本来就分开派生。

工具注册本身走的是 `ctx.tools.register` 的常规路径：在当前作用域插一层，近的遮蔽远的，`view()` 在单次层遍历里算完可见集合。MCP 客户端由 manager 挂在 manager 的 fiber 下，也就是全局作用域；如果某个 agent preset 自己挂一个 MCP 客户端，它的工具就只在这个 agent 的视图里出现。

多 agent 复用同一个 `serverName` 时，视图是这样分叉的：

| 挂载点 | 注册作用域 | 谁能看见 | `mcp__fs__read_file` 指向 |
| --- | --- | --- | --- |
| preset `reviewer` 挂客户端，根目录 A | reviewer 的 agent ctx | 只有 reviewer | 服务器 A |
| preset `coder` 挂客户端，根目录 B | coder 的 agent ctx | 只有 coder | 服务器 B |
| 全局再挂一个同名客户端 | root | 所有未被遮蔽的 agent | 全局实例 |

三个实例的公开名完全相同，注册表不冲突，因为它们在三个不同的层里。一个 agent 同时继承全局与自己的层时，自己的那一层赢，全局实例对它不可见。这就是 `serverName` 只在「注册作用域内唯一」的实际含义。

工具在两步之间加入时，下一步的请求头就带上它，同时历史里多一条 `tool-addition`，不需要等新的 turn。这也是为什么工具增删消息按请求头差异生成，而不是按 turn 边界生成。

{{< fig "diagrams/04-tool-list-change.svg" "图注：工具列表变化如何进入模型可见的 schema。刷新失败保留上一代；注册失败整批回滚；给弃后清空并在下一条请求补 tool-removal 消息。" >}}

自己搭的时候，别把 `serverName` 设成会变的东西，比如带进程号或时间戳。名字进了工具名，改名等于删掉旧工具再加一批新工具，请求头必变，缓存全丢，历史里的旧调用还会留下悬空引用。用固定的短名，比如 `gh`、`fs`、`pg`。

不这样会出什么事：如果先注册再校验重复，重复工具会让后一个覆盖前一个，模型调到的是另一个服务器的实现；如果公开名直接截断不加哈希，两个长名字会撞在一起，调用打到错误的工具上，而日志里看不出区别。

## 5. 一次 MCP 工具调用走到哪

模型发出 `tool_call` 之后，MCP 这条路上没有任何特殊分支。agent loop 把调用交给工具执行器，执行器按名字在当前作用域查定义，查不到就是 `UNKNOWN_TOOL`。查到之后进共享流水线：

1. 参数快照与冻结。不可无损 JSON 序列化的参数在准备阶段就抛错。
2. `tools/pre-execute` 瀑布。每个监听器可以返回 allow、deny、cancel、ask。MCP 不注册自己的监听器。
3. 如果返回 ask，走审批服务；不是 `allowed-once` 就变成拒绝。
4. guard 检查。guard 只能拒绝，不能把拒绝翻回允许。
5. 超时与取消合流。工具默认超时 60 秒，调用方取消走同一条 signal。
6. 进入 MCP executor。`tool.execution.taskSupport === 'required'` 的工具直接拒绝，因为这个桥不支持 task-based execution。
7. `client.callTool` 在 `generation.run` 里执行。调用方超时只让等待方 reject，服务器那边的请求不会被主动取消；连接 dispose 时 lifetime abort 才会连带取消。
8. 回来的 `CallToolResult` 先过 schema 校验。不合法抛 `Tool "<raw>" returned an invalid MCP result: ...`。
9. `isError === true` 时抛异常，把文本作为错误消息。工具运行时把它记成失败结果，不是成功结果。
10. 内容投影。文本合并换行，图片走附件系统，resource_link 变成一行 `Resource link: name (uri)`，audio 与 embedded resource 变成明确的 unsupported 文本，空结果补一句 `(<tool> returned no model-visible content)`。

图片这条支路值得单独说。服务器返回 image 内容块时，桥先检查四件事：MIME 是 png/jpeg/webp/gif 之一，base64 符合规范，`attachments` 服务存在，当前模型路由的 `inputModalities` 包含 image。四条都满足才把图片挂到附件上、在结果里留一个引用；任何一条不满足就给文本 `[image unavailable: <mime>; <reason>; raw image data remains available to programmatic callers]`。程序化调用方拿到的仍是原始 MCP JSON，模型拿到的是一句解释。

各内容块的投影结果：

| 服务器返回 | 程序化调用方 | 模型 |
| --- | --- | --- |
| `text` | 原样 | 文本，多个块之间合并换行 |
| `image` | base64 原样 | 附件引用；不支持时一行诊断 |
| `resource_link` | 原样 | `Resource link: name (uri)` |
| `audio` | 原样 | 一句 unsupported 诊断 |
| embedded `resource` | 原样 | 一句 unsupported 诊断 |
| 未知 type | 原样 | `[unsupported MCP content type: <type>]` |
| 空 content | `[]` | `(<tool> returned no model-visible content)` |

这张表决定了「MCP 结果能不能被模型用」的上限。协议允许的富内容比模型能读的多，桥的工作是把不能读的变成一句可读的话，同时不丢掉原始数据。

`structuredContent` 有一个反直觉的地方。结果同时带 `content` 与 `structuredContent` 时，模型只看 `content`，程序化调用方两者都能拿；如果服务器把数据全放进 `structuredContent`、`content` 留空，模型收到的是 `(<tool> returned no model-visible content)`。schema 校验通过，模型什么也没读到。

{{< fig "diagrams/03-call-sequence.svg" "图注：一次 MCP 工具调用的时序。审批、guard、超时都在共享流水线里，MCP 桥只负责协议调用与结果投影。" >}}

超时语义容易误解。`withDeadline` 只是让等待方不再等，底层 promise 还在跑；`generation.run` 把任务放进 pending 集合，只有连接关闭时才 abort。所以一个跑了 90 秒的服务器调用在 60 秒时给模型返回超时，但服务器进程还在算，算完之后结果被丢掉。对写操作来说这很危险：模型以为失败了，实际服务器已经提交。

不这样会出什么事：如果 `isError` 不转成异常，模型会看到一段「成功」的工具结果，里面写着错误文本，然后基于它继续推理；如果超时真的取消服务器请求，那些不支持取消的服务器会留下半完成状态，而客户端没有任何记录。

## 6. 工具列表变了，模型看到什么

服务器可以在运行中改工具表。新旧两种协议路径都支持：旧版发 `notifications/tools/list_changed`，新版走订阅。客户端建 `Client` 时把 `listChanged.tools.autoRefresh` 设成 false，自己接 `onChanged`，因为刷新要串行化，不能让 SDK 并发触发。

`debounceMs` 设成 0 表示合并交给串行链做，不在 SDK 层做。连续三条通知会排三次同步，前两次的结果被第三次取代；好处是每次同步都从服务器拿最新清单，坏处是突发通知会白跑几次 `listTools`。

收到通知后 `refreshTools()` 把一次 `syncTools` 推进 `enqueueSync` 的串行链。刷新失败分两种。普通网络错误只写日志 `tool re-sync failed`，保留上一代工具，等下一次通知或下一次重连。注册冲突抛 `McpToolRegistrationError`，客户端把它当成连接失效，直接走 `lost`，因为工具表已经无法安全替换，继续留着旧一代会让模型调用到已经不存在的实现。

模型可见的部分分两层。第一层是请求头：`tools[]` 是 `request/header` 的一部分，任何增删改都会让 `headerEquals` 为假，下一轮写一条 `reason: 'change'` 的新头。第二层是消息：agent loop 比较前后两版的工具名集合，把新增和删除写成 `tool-addition` / `tool-removal` 内容块，放进一条 developer 消息。

这里有一个按模型路由分叉的细节。有的模型支持 `toolUpdate: 'in-history'`，它会把历史里的 addition/removal 块当作对声明列表的增量，于是删除一个工具不需要重写 `tools[]`；不支持的路由每一轮都声明完整清单，删除就是真的从数组里拿掉。同一个 harness，同一个 MCP 服务器，在不同模型上缓存代价不同。

工具顺序也会影响请求。清单按服务器返回顺序进 Map，注册顺序就是 schema 顺序。服务器每次 `listTools` 返回的顺序不稳定时，即使集合没变，`tools[]` 的字节也不同，请求头还是会变。这一点在协议里没有约束，只能靠服务器自己保证稳定。

自己搭的时候，工具描述里不要放版本号、时间戳、当前分支名这类每次都变的东西。描述进了 `tools[]`，变一个字就重写整个前缀。想表达「这个工具有新版本」，放到工具结果的文本里，别放到 schema 里。

不这样会出什么事：如果刷新不串行，两次通知可能交错，后到的旧清单覆盖先到的新清单；如果刷新失败就清空工具，模型会在一次网络抖动后失去整台服务器的能力，而服务器其实还活着。

## 7. resources 与 tool 的区别

resources 是 MCP 的另一半协议：服务器暴露可读的 URI，客户端按需读取。DSH 没有把每个资源变成一个工具，理由很直接：资源数量没有上限，一个文件服务器可能有几万个；工具表是请求前缀，每加一项都要付缓存与上下文成本；而且资源的权限粒度是 URI，工具的权限粒度是名字，混在一起没法表达。

所以它注册三个固定工具：

| 工具 | 参数 | 打到哪个方法 |
| --- | --- | --- |
| `list_mcp_resources` | `server` 必填，`cursor` 可选 | `resources/list` |
| `list_mcp_resource_templates` | `server` 必填，`cursor` 可选 | `resources/templates/list` |
| `read_mcp_resource` | `server` 与 `uri` 必填 | `resources/read` |

三个工具的 schema 都是固定文本，不随服务器变化。服务器增减资源不会改请求头，这点和 tools 相反。模型要先知道有哪些服务器，提示词里有一节 `## MCP resource servers`，列出当前作用域可见的名字，包括那些没有工具、没有指令的服务器。这一节由 `mcp-resources` 注册，`interpolate: false`，文本每次组装时重算；没有可见服务器时返回空串，整节消失。

提示词里实际出现的样子：

```
## MCP resource servers

Use list_mcp_resources, list_mcp_resource_templates, or read_mcp_resource with one of these names as the server argument: ["fs","pg"]
```

名字排序后输出。集合不变时这一节字节不变，前缀缓存不受影响；加减服务器才改这一节，而那时工具表通常也在改，两个变化落在同一次请求里。

作用域继承是另一条容易踩的线。一个 agent 自己挂的客户端在它自己的层，继承自父级的 provider 在父层。`merge(exec.agent)` 从近到远找，同名时近的赢。子 agent 因此可以用自己的同名服务器覆盖父级的，而工具名不变。这条规则与 tools 的层叠规则同源，不是资源服务特有的。

调用路径是：工具拿到 `server` 与请求，去 `layers.merge(exec.agent)` 里找 provider，找不到直接抛 `MCP resource server "<server>" is unavailable in this agent's scope`，这一步发生在任何网络请求之前。找到之后转给对应客户端的连接，客户端再把 JSON 原样返回。渲染时把 `blob` 字段换成一句描述，二进制内容留在程序化结果里，模型只看到长度。

共享工具有一个生命周期细节。工具注册挂在 `selfCtx` 上，不挂在首个注册的客户端上。服务器 A 先注册，工具出现；服务器 A 掉线，工具还在；服务器 B 后注册，工具不重复注册；服务器 B 也卸载，最后一个 provider 走了，工具才注销。这条规则让模型看到的资源能力是「部署级」的，不随单个服务器的健康状态抖动。

不这样会出什么事：如果按资源生成工具，一个仓库几千个文件就会产生几千个 schema，请求直接超限；如果资源工具跟着客户端注册，两个服务器会产生两份同名工具，注册第二个时抛冲突，而这个冲突和服务器本身无关。

## 8. 权限与审批怎么套

MCP 桥里没有一行审批代码。工具注册进 `ctx.tools` 之后就是普通工具，审批发生在共享流水线的 `tools/pre-execute` 阶段。部署方可以挂一个监听器，按工具名、参数、agent 身份返回 allow、deny、cancel 或 ask。返回 ask 时，流水线调 `ctx.get('approval')`，把请求交给审批服务；服务返回 `allowed-once` 才继续，其余三种结果（rejected、cancelled、unavailable）都变成拒绝。

拒绝的形状是固定的：内容块 `Error: <reason>`，`isError: true`，错误对象里带原因。模型看到的就是这一行，它不知道背后是用户点了拒绝、策略拒绝，还是没有审批通道。审批取消还会额外走取消路径，结果是 `ABORTED` 而不是拒绝，因为用户取消的是这次调用，不是这个工具。

会话级策略只有两档：`ask` 与 `never`。`never` 下所有 ask 直接变 rejected，模型收到的系统提示会写明「本会话审批被禁用，需要审批的操作会被自动拒绝」。无人值守的 CI 用这一档，好处是结果可预测，坏处是所有需要审批的 MCP 工具都不可用。

MCP 协议本身有 `annotations`，比如 `readOnlyHint`、`destructiveHint`。DSH 的桥不读这些字段，整个 `packages/mcp/*/src` 里没有一处引用。这是一个明确的选择：服务器可以自称只读，但它同时也能在自己声明的只读工具里删库，提示不能当策略。要按服务器区分权限，只能在部署侧按 `mcp__<server>__` 前缀写规则。

自己搭的时候，如果你要给 MCP 工具加审批，别改桥，挂一个 `tools/pre-execute` 监听器，按公开名前缀匹配。这样规则和工具来源解耦，同一个监听器也能管内置工具；桥升级时你的规则不用动。

```ts
ctx.on('tools/pre-execute', async (exec, next) => {
  if (!exec.name.startsWith('mcp__pg__')) return next()
  if (READ_ONLY.has(exec.name)) return next()
  return { kind: 'ask', reason: 'writes to the production database' }
})
```

规则说「什么值得问」，策略说「这个会话能不能问」。监听器返回 ask 之后，`never` 策略会把它直接变成拒绝，模型收到的是一条 `approval ... was rejected`，不会弹出任何东西。两层分开的好处是同一套规则可以部署到交互式会话与 CI，只是策略不同。

| 审批结果 | 流水线动作 | 模型看到 |
| --- | --- | --- |
| `allowed-once` | 继续执行 | 工具结果 |
| `rejected` | deny | `Error: approval for tool "<name>" was rejected` |
| `cancelled` | deny 并标记取消 | 调用以 `ABORTED` 结束 |
| `unavailable` | deny | `Error: ... but no approval channel is available` |

`allowed-once` 的 once 是字面意思：授权只对这一次调用有效，下一次同样的调用还会再问。没有「本会话永远允许」的通道，这是审批服务刻意的限制。

guard 与审批是两件事。guard 在 pre-execute 之后、工具体之前跑，只能返回拒绝原因，不能放行也不能弹窗。它管的是「即使审批通过也不许做」的硬约束，比如路径必须落在工作区内。顺序固定：先 pre-execute（可能问人），再 guard，所以 guard 拒绝时用户可能已经被问过一次。

不这样会出什么事：如果桥自己弹审批，headless 部署每次调用都会卡在一个不存在的通道上；如果信任 annotations，一个恶意服务器只要把所有工具标成 readOnly 就绕过了全部审批。

## 9. 失败模式：模型看到什么

| 失败 | 客户端状态 | 模型看到 |
| --- | --- | --- |
| 服务器进程退出 | `reconnecting` | 工具还在表里；调用返回 `MCP transport closed` 或连接错误文本 |
| 握手超时 | `reconnecting` | 同上；首次失败时如果 `failOnStartupError` 为 true，插件加载失败，工具从未出现 |
| 重连预算耗尽 | `failed/gave-up` | 下一轮 `tools[]` 少掉这批工具，developer 消息列出 `tool-removal`；旧调用变 `UNKNOWN_TOOL` |
| 凭据模板缺值 | `waiting-credentials` | 不重试；工具未注册；设置页显示缺哪个变量 |
| 工具刷新失败 | 保持 `connected` | 无变化；旧工具继续可用；日志一条 `tool re-sync failed` |
| 刷新时注册冲突 | 走 `lost` | 整批回滚，随后按掉线处理 |
| 服务器返回 `isError` | 不变 | 工具结果 `isError: true`，文本是服务器给的错误消息 |
| 调用超时 | 不变 | 工具结果超时；服务器可能仍在执行 |
| 结果不合 schema | 不变 | `Tool "<raw>" returned an invalid MCP result: ...` |
| 图片但模型不支持 | 不变 | `[image unavailable: ...]` 文本，原始数据仍在程序化结果里 |
| 服务器不支持 tools | `connected`，0 工具 | 资源工具仍可用，提示词里仍有服务器名 |
| 插件卸载 | `closed` | 工具注销，`tool-removal` |

这张表里最容易被误读的是第一行。掉线期间工具还在，模型会继续规划、继续调用，每次拿回一段错误。这是有意的：短暂抖动时保留工具，比让模型看见工具表反复横跳要好。代价是模型可能连续浪费几个 step 在同一台死服务器上。

表里没写的一种情况是工具被移除之后模型还去调它。运行时报未知工具，模型通常会重新读一遍工具表，换一条路；如果移除发生在多步任务中间，它会丢掉后半段计划。这也是保留工具的另一个理由：模型不会因为工具表突然缩短而中断任务，只会在调用时拿到错误。

## 10. 一次掉线的完整时间线

把参数取默认值，看一台 stdio 服务器被 kill 之后发生什么。默认预算：初始 500ms，翻倍，上限 30s，最多 10 次。

| 时刻 | 发生什么 | 状态 | 模型可见 |
| --- | --- | --- | --- |
| t+0 | 传输层观察到关闭，触发 `lost` | `reconnecting`，attempt 1 | 工具仍在表里 |
| t+0.5s | 重连尝试 1，spawn 后握手失败 | `reconnecting`，attempt 2 | 同上 |
| t+1.5s | 尝试 2 失败 | attempt 3 | 同上 |
| t+3.5s | 尝试 3 失败 | attempt 4 | 同上 |
| t+7.5s | 尝试 4 失败 | attempt 5 | 同上 |
| t+15.5s | 尝试 5 失败 | attempt 6 | 同上 |
| t+31.5s | 尝试 6 失败 | attempt 7 | 同上 |
| t+61.5s | 尝试 7 失败 | attempt 8 | 同上 |
| t+91.5s | 尝试 8 失败 | attempt 9 | 同上 |
| t+121.5s | 尝试 9 失败 | attempt 10 | 同上 |
| t+151.5s | 尝试 10 失败，预算耗尽 | `failed/gave-up` | 下一轮 `tools[]` 少掉这批工具，developer 消息列出移除 |

窗口总长 151.5 秒，等于 0.5+1+2+4+8+16+30×4。这段时间里模型看到的是一个照常列出工具、每次调用都失败的服务器。失败来得很快：传输已关，SDK 直接拒绝请求，不会等满 `toolCallTimeoutMs`。

预算的归零发生在下一次掉线时，不在连上的那一刻。连接活过 30 秒（等于 `maxDelayMs`）之后再断，`failedAttempts` 归零，新的一次 outage 重新拥有 10 次机会。连接活不过 30 秒就断，计数继续累加。这条规则把「偶发抖动」和「崩溃循环」分开：前者每次都从头开始，后者在 10 次内耗尽，然后停止重连。

给弃之后没有自动恢复。状态停在 `failed`，工具已注销，只能重载插件或重启 Host。这是有意的：一个在 150 秒内连续失败 10 次的服务器，继续重试只会消耗资源，把决定权交回运维更合理。

## 11. 设计取舍

每服务器一个插件，代价是 N 个 Cordis 实例、N 份状态，收益是失败隔离与热替换。一个服务器崩了不影响另一个；设置页改一个条目只重建那一个实例。如果做成单例客户端管理所有连接，重连预算、状态、工具注销都要在一个对象里按服务器分桶，代码不会更短，失败面反而更大。

拆成三个包，代价是跨包接口都要显式定义：manager 给 client 的 Config、client 给 resources 的 provider、client 给 tools 的注册项。收益是 resources 能独立于连接状态存在，客户端全掉线时资源工具和提示词章节仍然在。合成一个包，资源工具就会跟着连接生命周期走，掉线即消失，模型会在同一轮里先看到工具、再看不到。

重连期间保留工具，代价是模型可能调用到死工具，收益是工具表稳定。反过来的设计是掉线立刻注销，模型马上知道能力没了，但一次 2 秒的网络抖动就会让请求头变化、缓存失效、模型重新规划。稳定窗口与预算上限就是为这个取舍服务的。

资源用三个固定工具，代价是模型要多一步「先列服务器再读」，收益是 schema 稳定。如果按资源生成工具，工具表会随数据量膨胀，这在多租户部署里不可控。

工具名带服务器前缀，代价是名字长、难读，收益是不同服务器的同名工具可以共存。MCP 协议里工具名只在服务器内唯一，跨服务器没有命名空间，这一层必须由宿主补。

manager 不碰协议，代价是探测要新开连接，收益是探测不影响活实例。把 probe 做成「复用现有连接发一个 ping」看起来更省，但它会把探测的超时和真实调用的超时混在一起，排障时说不清是谁超时。

manager 统一挂载客户端，代价是所有连接都在 manager 的 fiber 下，manager 重启会重建全部服务器连接，工具表整批换代；收益是设置页有一个统一入口做状态、重启、探测。profile 里直接挂一个客户端也支持，生命周期跟着 profile 走，改配置要重启进程。选哪一种，取决于你要统一运维还是最小依赖。

指令做成独立的提示词 section，代价是每次请求都占一段前缀 token，收益是它不污染任何工具的 schema。把指令并进工具描述能省一节标题，但改一次指令就改一次工具表，缓存与审批规则都跟着动。

`failOnStartupError` 默认 false（manager 挂载时显式写死），代价是配置错了不会在加载期炸，收益是一个坏服务器不会拖垮整个宿主。设置页能看到 `failed/load`，但要在 UI 里主动看。手写 `cordis.yml` 时默认是 true，适合「这个服务器必须有」的部署。

## 12. 踩坑点

1. `serverName` 的作用域是注册作用域，不是全局。两个 agent 各挂一个同名服务器是合法的，工具名会一样，但各自只在自己的视图里可见。以为全局唯一会写出多余的改名逻辑。
2. 公开名 64 字符上限包含前缀。一个 32 字符的 `serverName` 只剩 30 个字符给工具名，很容易触发哈希。名字长到带哈希时，日志检索要用原始名，不是公开名。
3. 工具顺序不稳会改请求头。服务器每次 `listTools` 用哈希表遍历返回，顺序随机，集合没变但 `tools[]` 字节变了，缓存每一步都失效。
4. 启动预算包含首次工具同步。`startupTimeoutMs` 不是「连接超时」，是「连接加首次 listTools」。服务器工具多、序列化慢时，卡在这一步的人很多。
5. 凭据缺失停在 `waiting-credentials`，不会自动重试。补完凭据后要触发 `credentials/reference-updated`，客户端才会重建连接。只改环境变量不会生效。
6. `restart` 可能被拒绝。上一次卸载没在 5 秒内确认关闭时，记录变成 `failed/cleanup`，之后所有 restart 都报 `cleanup for "<name>" is incomplete; restart the Host`。这是保护，不是 bug。
7. `isError` 的结果文本会进模型上下文，但不进工具的结构化 value。想给程序化调用方留数据，放在 `structuredContent` 里，别指望错误文本。
8. 探测会消耗服务器资源。`probe` 新开连接、跑一次 `listTools`、关闭；对按连接计费的远程服务器，频繁探测就是真金白银。
9. 二进制资源在模型侧只有长度描述。需要模型读内容时，让服务器提供文本资源，或者写一个把资源转成文本的工具。
10. 指令文本要稳定。带启动时间戳或随机 ID 的 instructions 每次重连都重写系统提示词前缀，缓存命中率会掉到与重连次数同阶。
11. 探测返回的工具名列表可能被截断。结果里有 `truncated` 标志，把它当完整清单做对账，会误判服务器少了工具。
12. guard 在审批之后跑。被 guard 拒绝的调用可能已经问过用户一次，交互式会话里表现为「点了允许还是失败」，这是顺序决定的，不是 bug。
13. 资源工具的第一个参数是服务器名，不是 URI。模型经常把 `file:///...` 填进 `server`，报 `unavailable in this agent's scope`，看起来像权限问题，实际是参数错位。

## 13. 源码索引

按排障顺序排：先看状态与预算，再看命名与换代，最后看宿主侧的注册与消息。

| 关注点 | 文件 | 关键符号 |
| --- | --- | --- |
| 每服务器插件、配置联合、名字预留 | `packages/mcp/mcp-client/src/index.ts` | `Config`、`apply`、`activeServerNames`、`connectionStatusOf` |
| 重连预算、启动预算、指令、关闭 | `packages/mcp/mcp-client/src/connection.ts:47` | `RECONNECT_DEFAULTS`、`resolveReconnectPolicy`、`connectGeneration` |
| 工具命名与换代 | `packages/mcp/mcp-client/src/tools.ts:91` | `publicToolName`、`syncTools:126`、`createMcpToolDefinition:251`、`createExecutor:298` |
| 状态联合与探针结果 | `packages/mcp/mcp-client/src/types.ts` | `McpConnectionStatus`、`McpProbeResult` |
| 跨 reload 的状态单元 | `packages/mcp/mcp-client/src/status.ts` | `McpStatusCell`、`registerStatusCell` |
| 一代连接的关闭协议 | `packages/mcp/mcp-client/src/generation.ts` | `McpGeneration`、`run`、`close` |
| fleet、校验、重启、探测 | `packages/mcp/mcp-manager/src/index.ts:127` | `validateEntries`、`reconcileOnce:458`、`probe:379` |
| 共享资源工具与作用域目录 | `packages/mcp/mcp-resources/src/index.ts:47` | `McpResourceRuntime`、`register:80`、`registerTools:108` |
| 三个资源工具的 schema | `packages/mcp/mcp-resources/src/tools.ts:61` | `registerResourceTools` |
| 注册表与作用域派生 | `packages/core/tools/src/index.ts:128` | `register`、`view:199`、`resolve:264` |
| 审批与 guard 的接入点 | `packages/core/tools/src/pipeline.ts:200` | `prepare`、`serviceAsk` |
| 审批决定的四种形状 | `packages/core/tools/src/contracts.ts:340` | `PreToolDecision` |
| 审批结果与会话策略 | `packages/interaction/user-approval/src/index.ts` | `ApprovalOutcome`、`ApprovalPolicy` |
| 工具增删的模型可见消息 | `packages/core/agent-loop/src/agent.ts:702` | `createDeveloperMessage` |
| 指令与资源注册进作用域 | `packages/mcp/mcp-client/src/server-context.ts` | `registerServerContext` |
| 凭据模板与缺失值 | `packages/mcp/mcp-client/src/credentials.ts` | `McpTemplateBinding` |
| 截止时间与剩余预算 | `packages/mcp/mcp-client/src/deadline.ts` | `withDeadline` |
| 资源结果的模型侧渲染 | `packages/mcp/mcp-resources/src/render.ts` | `renderResourceResult` |

## 附：本地验证清单

五步，按顺序做，前四步不需要真服务器之外的依赖：

1. 用 `npx @modelcontextprotocol/server-everything` 起一台 stdio 服务器，确认 `tools[]` 里出现 `mcp__everything__*`。
2. 把 `serverName` 改成 32 个字符，再加一个 40 字符的工具名，看公开名是否带哈希后缀。
3. `kill` 服务器进程，在第 5 秒调一次工具，确认错误立刻返回，而不是等满 60 秒。
4. 把 `reconnect.maxAttempts` 改成 1，重复第 3 步，确认下一轮请求头少掉这批工具，历史里出现 `tool-removal`。
5. 把服务器 instructions 设成 40KB，确认连接失败并最终停在 `failed/gave-up`，模型那边从未出现过这批工具。

第 2 步最容易卡住，因为现成服务器的工具名都很短，触发不了 64 字符上限。要构造一个长名字的假服务器，或者临时把常量改小跑一次再改回来。

要验证这些行为，不用读代码：起一个在收到 `tools/list` 后立刻退出的假服务器，连着调三次工具，看 `maxAttempts` 耗尽后请求头里还剩几个工具；再把 `maxAttempts` 改成 1，看给弃那一刻写进历史的 developer 消息。两分钟能看完的状态转移，比读 400 行源码直观。

