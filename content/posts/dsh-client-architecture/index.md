---
title: "客户端：60 个 ui 包、一张 slot 表与一条投影管线"
date: "2026-11-13T10:30:00+08:00"
summary: "浏览器侧不是一个前端，是三层可替换的投影：对象层拿事件窗口，Conversation 折叠成视图节点，slot 把节点分发给插件注册的组件。60 个 ui-* 包按渲染位置分层，跨包只走 import type、注入服务与 slot；工具卡片从原始事件与持久 meta 派生，presentCall 从不进客户端。"
description: "DSH Web 客户端的分层、slot 冲突解法、transcript 投影、工具卡生命周期与截断、object layer 与 store 的边界、断线重连与 HMR。"
tags: ["harness", "architecture", "collaborative-editing"]
slug: "dsh-client-architecture"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准。
> 第二季第 3 篇。上一季：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)；本季导览：[《第二季：把主干两侧补齐》](/posts/dsh-season-two.html)。

打开开发者工具，翻到一次 `read` 调用。`tool/result` 的正文只是一段带 `<content>` 标签的文本，屏幕上却是一张有行号、有总行数、能展开的代码卡。行号从哪来？客户端没有解析那段文本，它读的是同一条结果上持久化的 `meta`。

再看包目录。`packages/client/` 下有 60 个 `ui-*` 包，互相之间不允许运行时 import。工具卡片由 `ui-tool` 渲染，`ui-skill` 却能把自己的卡片插进去；右侧栏有文件、浏览器、文档预览三种面板，各自在独立包里。这些东西没有统一的注册中心，靠的是一张编译期 slot 表和一组注入点。

最后一个现象跟前端直觉相反。断网三十秒再恢复，滚动位置回到原处，流式气泡继续往下长，但浏览器内存里没有一份「消息数组」。客户端持有的是一段带 seq 的事件窗口，每次重连都整段替换。它不知道自己看到的第 137 行是怎么拼出来的，也不需要知道。

## 0. 结论

浏览器侧的所有权分成六层，依赖单向向下：

| 层 | 拥有的东西 | 典型包 | 越界会怎样 |
|---|---|---|---|
| Host 应用 | 业务 service 与 `*-controller` 的 Host entry | `packages/api/*-controller` | 把 Host 状态塞进浏览器，重放就断 |
| 传输与 API 装配 | WebSocket 代际、RPC、packed stream | `connection`、`api/gateway`、`api/remotes` | 组件直连 socket，重连语义散落 |
| Client model | `ClientSessions` → `SessionManager` → `Session`，workspace 行 | `api/session-controller/client`、`api/workspace-controller/client` | 把 session 塞进 store，刷新即丢 |
| UI adapter | 对象层状态到标准 slot source 与 hook | `ui-session`、`ui-workspace` | 组件自己订阅对象层，多一份镜像 |
| Conversation 数据 | 事件窗口到视图节点、target 快照 | `ui-conversation`、`ui-chat`、`ui-trajectory` | 组件自己拼消息，与日志分叉 |
| 组合与渲染 | slot 表、React 绑定、布局 | `ui-slots`、`ui-renderer`、`ui-layout` | 功能包互相 import，装不回去 |

三句话总结这篇的结论：

1. 60 个包不是 60 个功能，是 6 组所有权。分组判据是谁声明渲染位置、谁拥有状态，与目录顺序无关。
2. slot 的 `children` 同时是声明、授权和分发规格。跨包扩展只有两条路：注入服务做行为，slot 做 UI；运行时值只能落在 `store`、`ui-primitives` 这类窄静态owner里。
3. transcript 渲染的输入是投影后的视图节点，事件窗口归 Conversation assembler。客户端自己拼消息，会在 prepend、gap repair、重试与 PTC 子调用四处与日志分叉。

## 1. 60 个 ui-* 包按什么分组

### 1.1 六个组与它们的计数

`packages/client/README.zh.md` 的包表按职责分行，我按「渲染位置归谁」重排成六组，数量刚好 60：

| 组 | 数量 | 代表 | 判据 |
|---|---|---|---|
| 渲染地基 | 6 | `ui-slots`、`ui-renderer`、`ui-session`、`ui-theme`、`ui-primitives`、`ui-dockkit` | 提供机制，不提供业务界面 |
| 外壳 | 8 | `ui-layout`、`ui-workspace`、`ui-sidebar`、`ui-sidebar-right`、`ui-sidebar-files`、`ui-sidebar-browser`、`ui-sidebar-documentpreview`、`ui-brand-official` | 拥有页面骨架与命名 slot |
| 会话视图 | 8 | `ui-conversation`、`ui-chat`、`ui-tool`、`ui-trajectory`、`ui-workflow-run`、`ui-attachment`、`ui-deliverables`、`ui-message-feedback` | 消费事件窗口，产出视图节点 |
| 交互面 | 16 | `ui-approval`、`ui-user-questions`、`ui-plan`、`ui-goal`、`ui-commands`、`ui-input-trigger`、`ui-skill`、`ui-reference`、`ui-secret-mask`、`ui-model-selection`、`ui-agent-preset`、`ui-agent-team`、`ui-subagent`、`ui-jobs`、`ui-schedule`、`ui-permission-presets` | 把一次用户动作送回 Host |
| 设置 | 14 | `ui-settings` 与 11 个分区包，加 `ui-plugin-manager`、`ui-plugin-market`、`ui-entry-customization` | 只读写配置，不进 transcript |
| 旁路 | 8 | `ui-context-menu`、`ui-file-actions`、`ui-file-drag`、两个 `ui-directory-picker-*`、`ui-open-in-app`、`ui-shortcuts`、`ui-sidebar-terminal` | 操作系统级能力与输入辅助 |

另有 9 个非 `ui-*` 内核包：`web`、`modules`、`connection`、`store`、`resources`、`hmr`、`locale`、`shortcuts`、`file-upload`。它们不出界面，但决定了界面能拿到什么。

这六组之间的依赖是单向的。地基不 import 业务包，外壳只通过子 slot 让业务包填内容，会话视图消费事件，交互面把结果送回 Host，设置与旁路只挂在指定位置。`packages/client/AGENTS.md` 把这条写成红线：功能插件之间只能 `import type`，跨包行为走注入的 Cordis service，跨包 UI 走 slot。

### 1.2 为什么只能 import type

一个插件包想在另一个插件的卡片上加按钮，常规做法是 import 那个组件，或者 import 它的 store。DSH 两条都禁。原因是装载模型：每个包编译成独立的 `lib/client.js`，由模块表按需 materialize，插件可以在运行时被替换、禁用、重载。两个包互相持有运行时引用，HMR 换掉其中一个就会留下悬空引用。

所以跨包只留三种缝：

- 行为：`inject` 一个 service，比如 `ctx.remote`、`ctx.slots`、`ctx.resources`。服务可以替换，未满足时 fiber 等在那里。
- UI：往对方的 slot 注册 cell。对方只知道 key，不知道注册方是谁。
- 共享值：只能放 `client/store`、`ui-primitives` 或浏览器安全的 util 包。这三处是静态 owner，不参与功能插件的生命周期。

`ui-primitives` 之所以只有一个，同一条规则推出来的：插件不能 import 另一个插件的组件，那么所有控件就只剩一个能放的地方。它的 README 里有一张组件目录，写明什么时候复用、什么时候提升、什么时候该留在自己包里。

### 1.3 分组图看的是依赖，不是目录

{{< fig "diagrams/01-ui-layers.svg" "图 1：60 个 ui 包与 9 个内核包的分层。箭头只有向下的一条，横向的虚线是 slot 注册（UI）与 service 注入（行为）。" >}}

图里最上面的 `connection` 与 `store` 并不渲染任何东西，但下面每一层都依赖它们。分界线在第三层：`ui-session` 是对象层状态进入 React 的唯一适配点，越过它往下，组件拿到的就只剩数据与回调。

自己搭的时候，可以先不抄这个规模。60 个包的成本主要在边界，不在代码量。一个二十来人的项目，把所有界面塞进一个包，再用目录分区，能跑很久。什么时候该拆：当某个功能需要被单独禁用、单独重载，或者它的状态生命周期与其他功能不同步时。拆之前先确定它属于哪一组；分错组的包，后面一定会绕过边界去拿不该拿的东西。

### 1.4 九个内核包各管一段

不渲染界面的九个包，职责比 ui 包更硬，因为它们一旦被绕过，整层的恢复语义就没了：

| 包 | 负责的事 | 绕过它的后果 |
|---|---|---|
| `web` | 启动外壳，把 `WebBootGraph` 挂到 `window.__DSH_BOOT__` | 页面自己找插件，装载顺序失控 |
| `modules` | 客户端模块表，注册 factory 并按需 materialize | 动态包被静态 import，HMR 失效 |
| `connection` | RPC、事件投递、代际发布 | 每个功能自己开 socket |
| `store` | 不依赖 React 的 observable 与快照原语 | 每个包带一套订阅实现 |
| `resources` | `dsh-resource://` 协议与 `useResource` | 文件读取各写一份缓存 |
| `hmr` | 开发期轮询与 entry 替换 | 改一行刷整页 |
| `locale` | 词典与格式化 | 文案散落在组件里 |
| `shortcuts` | 键盘命令注册 | 快捷键冲突无人仲裁 |
| `file-upload` | 页面线程之外的 Blob 与字节流上传 | 大文件阻塞主线程 |

这九个里只有 `web` 与 `modules` 参与启动时序，其余都是被注入的服务。它们不出现在 slot 表里，但决定了 slot 里的组件能拿到什么工具。

## 2. slot：声明即授权

### 2.1 五种基数，三种作用域

`docs/subsystems/slots.zh.md` 把 slot 拆成两个正交维度。基数决定一个位置能放几个 cell，作用域决定它能拿到什么上下文。

| 基数 | 谁决定内容 | 冲突时 |
|---|---|---|
| `single` | 当前 priority 最小的存活 cell | 新 cell 遮蔽旧的，旧的不销毁 |
| `list` | 每个 cell 有必填 `id`，按 `order` 再按注册顺序 | 同 id 视为同一格，覆盖 |
| `keyed` | owner 传 `entryKey`，用该 key 的 props 渲染对应 cell | 复用已有 key 表示有意替换其展示 |
| `chain` | 每个 entry 提供纯 `select(owner)`，按 priority 取第一个非 null | 全拒时渲染 owner fallback |
| `hoc` | entry 层层包裹 owner 内容，最小者最外层 | 增减一层会让内部重新挂载 |

作用域有三种：`root` 只有一个组件和 store 实例；`session-maybe` 在没有 binding 时也能渲染，Session 值可选；`session` 要求能解析出确定的 Session。严格 `session` 的 entry 在 binding 代际变化时重挂，这正好对应切换会话时该丢的局部状态。

### 2.2 children 是声明、授权与分发规格

一个渲染位置要能被填，必须由它的父组件在注册时声明。声明写法就是注册参数的 `children` 字段：

```ts
ctx.slots.register({
  name: 'conversation.chat.node',
  key: 'tool-call',
  locale: NS,
  children: {
    'tool.call.toolview': { kind: 'keyed', scope: 'session' },
  },
  inject: toolInject,
}, ToolCallTree)
```

这段来自 `packages/client/ui-tool/src/client/apply.ts:36-51`。`children` 里的条目同时做三件事：让 `tool.call.toolview` 这个 key 生效，授权 `ToolCallTree` 调用 `renderSlot`，并在运行时登记分发表。三者缺一，位置就是死的。

如果某个包往一个没人声明的 slot 注册，或者在 `children` 里重复声明别人已经声明的 child，失败发生在插件激活时，不会等到渲染。`root` 是唯一内建声明，也是唯一由框架自己渲染的 key。

这条约束把「扩展点」变成了显式契约。加一个可扩展位置，是在父组件的注册里加一行；用别人的位置，是在自己的 apply 里写 `ctx.slots.inject(key, () => ctx.slots.register(...))`。`inject` 跟着声明方的生命周期走：声明方折叠，贡献被移除；声明方重挂，贡献重跑。

### 2.3 冲突只有四种解法

slot 没有仲裁者，冲突在声明阶段就被规则消掉了：

- `single` 用 priority 遮蔽。数值越小越先，先到者渲染。想并列内容，应该声明子 slot，而不是把 `single` 当列表用。
- `keyed` 复用 key 等于声明「我来替换这个 key 的展示」。普通增量贡献必须用新 key，否则就是覆盖别人的卡片。
- `list` 按 `id` 定址，再用 `order` 排序。两个包用同一个 `id`，语义就是同一格。
- `chain` 把选择权交给 entry：每个 entry 给一个纯函数，按 priority 依次问，第一个非 null 获选；全拒时回到 owner 的 fallback。
- `hoc` 不争夺内容，只包裹。工具卡片的图片画廊就用这个思路：图片卡声明一个子 slot，别的包往里加层。

### 2.4 一张卡片从注册到出现

以 `grep` 的搜索结果卡为例，完整路径是五步：

1. `ui-tool` 注册 `conversation.chat.node` 的 `tool-call` 渲染器，并在 `children` 里声明 `tool.call.toolview`（`apply.ts:41`）。
2. 同一个包里，`search-row` 用 `ctx.slots.inject('tool.call.toolview', ...)` 注册 `key: 'grep'` 的 cell（`toolviews/search-row.tsx:48`）。
3. 会话里出现一个 `tool/call` 事件，Conversation 折叠出 `tool-call` 节点，带着 phase 与 block。
4. `ToolCallTree` 从 block 里取 wire tool name，调 `renderSlot('tool.call.toolview', owner, { entryKey: toolName, fallback })`。
5. keyed 分发命中 `grep` 的 cell，渲染搜索卡；没命中的工具落到 `GenericToolCard`。

`fallback` 是关键。未认领的工具永远有地方显示，扩展失败不会让 transcript 出现空洞。`ui-skill` 注册 `key: 'skill'`，`ui-deliverables` 注册 `key: 'present'`，`ui-schedule` 注册自己的几个工具名，谁都不需要知道别人存在。

{{< fig "diagrams/02-slot-assembly.svg" "图 2：一张工具卡从注册到出现。上半是声明与注册两个时刻，下半是事件到达后的 keyed 分发与 fallback。" >}}

自己搭的时候，可以先把 slot 的类型表写死在一个文件里，别急着做动态注册协议。类型表带来的收益是编译期就能发现「渲染了没声明的 slot」，这比运行时报错便宜一个数量级。等出现第二个注册方，再补 `inject` 的生命周期语义。

### 2.5 组件拿到的五份 share

注册一个组件时，参数里能写的东西被固定成五份派生 share，组件不能自己扩展：

| share | 内容 | 来源 |
|---|---|---|
| `PropsRuntime<K>` | owner props 加 scope 座位 | 父组件 `renderSlot` 的第二个参数 |
| `PropsRenderSlots<S>` | `children` 每个 key 的渲染函数 | 注册项的 `children` |
| `PropsRenderFactories` | `renderFactorySlot` | 框架提供 |
| `PropsStore<H>` | `useStore` 与 `actions` | 注册项的 `store` |
| inject face | 纯数据与回调 | 注册项的 `inject` |

组件不接收 `ctx`，不 import service 类，也不读 React context。需要一个新东西时，答案是从 share 的来源加一个 prop：owner 站点、store 声明或 inject factory。加一个自定义 hook 不在这份清单里，因为 hook 只能由框架在绑定点组合。

站立席位是固定的几个：`useSession`、`useSessions`、`useWorkspaces`、`useStore`、`renderSlot`、`renderFactorySlot`。注册项可以带一个 `hooks` 舱室，里面放裸的 `getSnapshot` / `subscribe` source，渲染器把它绑成 `use<Name>(selector)`，组件看不到 source 本身。

## 3. transcript 渲染的输入是投影，不是消息

### 3.1 客户端持有的是事件窗口

`packages/api/session-controller/client` 里的 `Session` 持有一段连续的逻辑事件窗口，元素是 `SessionEventLikeEntry`，两种形态：

```ts
{ type: 'event', event: SessionEvent }
{ type: 'transient', event: AssistantLiveChunkEvent }
```

持久事件来自日志；`assistant/live-chunk` 只存在于浏览器，是流式增量。两者都公开 `type`、`seq`、`time`、`data`，所以下游可以一视同仁地匹配。

窗口之外还有游标与分页。`follow()` 第一帧给 header、尾部一页、游标和一份完整投影 baseline；往前翻历史用 `page()`。客户端从不假设自己拥有全部历史。

### 3.2 从 entry 到视图节点

`ui-conversation` 的 `ConversationNodeAssembler` 做折叠。每个 Session 一个 assembler，每个 view target 注册自己的 Definition。Definition 的契约很短：

```ts
match(event): { id, role: 'start' | 'update' } | null
start(context, match): State
update(context, match): State
buildViewNode(context): { key, kind, id, target, anchorSeq, location, visibility, data }
```

`match` 只看当前这一条 entry，命中后返回稳定 id 与角色。第一次命中走 `start`，后续走 `update`。折叠结果按 `(kind, id)` 存成 Context，Context 再物化成带位置的视图节点。

两条容易踩的规则：节点一旦发布，之后必须继续返回同一个 `key`，想隐藏只能给 `visibility: 'hidden'`，不能返回 null 撤回；一个事件被同一 target 认领两次会抛错。

### 3.3 为什么客户端不能自己拼消息

手写一个聊天前端，最自然的模型是「消息数组 + 追加」。DSH 的组装层要处理五种窗口变化，每一种都会让追加模型错位：

- 打开或重同步：整窗替换。要先 `start` 再按 seq 升序 `update`，顺序错了，依赖前序状态的折叠就拿不到正确的 State。
- 往前翻页：只匹配新增的更早 input，合并进已有 Context，再重放受影响的 Context。追加模型在这里会把历史插到错误的位置。
- 实时追加：每条新事件对每个 Definition 做一次 match。注册了 D 个 Definition，就是 D 次匹配，不遍历窗口。
- 增量 flush 失败：完整窗口回放到一个新 Context，重建成功就替换 active view 并抛 `rebuilt: true` 的 `ConversationUpdateError`；重建也失败则下次重试。
- 断线重连：按新代际的 opening snapshot 整窗替换，再用 `page()` 补 range gap。

assember 的实现把这些收在一个 `flush()` 里（`conversation/assembler.ts:399`）。有替换待办时走 `flushReplacement()`（:411），否则走 `flushIncremental()`（:448）；对 target builder 只有两种调用，先 `replace` 否则 `apply`（:1002-1003）。

还有三件事是追加模型看不见的。重试会让同一轮出现多个 `assistant/attempt`，最终消息要选哪一份、中间失败要不要显示，都是折叠决策。PTC 的子调用挂在父调用的 `subCalls` 上，不是顶层消息。压缩会用位置替换改写历史，消息数组会保留被替换掉的旧文本。

{{< fig "diagrams/03-render-pipeline.svg" "图 3：从事件窗口到 DOM。左侧是两条输入（持久日志、瞬态 live chunk），中间是 Context 与 target builder，右侧是 slot 分发与 React。" >}}

自己搭的时候，可以先把「事件 → 视图节点」这一层单独写出来，哪怕它只有一个 Definition。好处不是抽象，是可测：给一段事件数组，断言输出的节点数组，不需要浏览器。等这一层稳了，再把 slot 与 React 接上去。

### 3.4 流式与重连共用一条入口

实时的助手增量以 `assistant/live-chunk` 到达，这是一个只在浏览器存在的瞬态事件，不写日志。持久化的 `assistant/message` 与 `assistant/attempt` 里嵌着一份紧凑 stream，供历史回放使用。

重连时，baseline 把活跃的紧凑 stream 展开成同样的瞬态事件。assembler 收到的两类输入形状一致，所以它不需要区分「正在实时流」与「重放一段已经流完的内容」。这也是为什么客户端可以在断线恢复后继续往同一个气泡里追加，而不用把气泡拆成两段。

工具参数的 delta 与随后的 `tool/call` 靠同一个 callId 匹配。`preparing` 阶段能先显示工具名，等 `tool/call` 到了再补参数。分页不会展开已经结束的消息，所以往前翻历史时不会突然多出几百个节点。

## 4. 工具卡片：生命周期与截断

### 4.1 三个阶段

工具调用的展示状态有三个阶段，来自 `ui-tool/src/client/contract/slots.ts:76`：

| phase | block 里有什么 | 能确定的事 |
|---|---|---|
| `preparing` | 参数块还在流，只有名称 | 只能显示名称，参数可能不全 |
| `start` | `tool/call` 已持久化，参数完整 | 标题、参数摘要、cwd、文件路径都能算 |
| `result` | `tool/result` 到达 | 成功/失败、输出、meta、行号、diff |

行模型在此基础上再分出五种视觉状态：`preparing`、`running`、`ok`、`error`、`stopped`。`stopped` 的判定是错误码为 `interrupted`，与普通失败分开显示（`tool-call-model.ts:394-396`）。

命名完成但参数块还没完成时，模型调用只显示一行不可展开的行；直到 `tool/call` 到达，才启用调用呈现。仅参数块完成不会触发执行，这条边界经常被误画成「参数流完就开始跑」。

### 4.2 卡片数据来自原始事件与持久 meta

`ui-tool/README.zh.md:58` 有一句很硬的话：结构化卡片直接由第一方原始事件字段派生，Host 的 `presentCall` 与 `presentResult` 值从不进入客户端。

这句话解释了两个容易混的东西。`presentCall` / `presentResult` 是工具定义上的展示意图 API，返回 `ToolCallView` / `ToolResultView`，词汇是 provider 中立的：`card` 判别 generic、terminal、diff、search、read、web。定义在 `packages/core/tools/src/contracts.ts:93` 与 `:101`。

在这个 commit 里，除了工具自己定义与 `defineTool` 的装载逻辑，我没有找到调用这两个方法的地方。Web 客户端一条也不读。卡片依赖的是事件字段与 `tool/result` 上持久化的 `meta`：`read` 的行号、`apply_patch` 的 hunks、shell 的退出码与截断尾注，都在 meta 里。

`meta` 的定义在 `contracts.ts:152-164`，注释写明它持久在 `tool/result` 或 `tool/ptc-dispatch` 上，供 Host presenter 与 Client renderer 各自窄化。这句话决定了回放能力：卡片不需要重跑工具，也不需要解析模型-facing 文本。

`presentCall` 仍然有用，只是用途不在 Web。`defineTool` 给它加了软校验，注释解释得很清楚：展示可能在回放任意历史参数时运行，参数可能来自旧 schema，所以不能抛，校验不过就返回 `undefined` 让 UI 退回通用卡片（`schema.ts:610-620`）。

### 4.3 大结果在三处被截

模型看到的结果、日志留下的结果、屏幕显示的结果，三份各有各的上限：

- 模型侧：`projectContent` 与 `tools/post-execute` 决定留什么，超长内容落盘换 locator。这条链在第一季[《工具结果的一生》](/posts/dsh-tool-results.html)里拆过。
- 日志侧：完整结果进 `tool/result`，`meta` 与内容分开存，压缩与回放读的是日志。
- 显示侧：聊天行是摘要面，details 面板才是阅读面。所以卡片的上限比通用控件小：`CHAT_READ_MAX_LINES = 8`（`read-card-model.ts:17`）、`CHAT_SEARCH_MAX_LINES = 8`（`search-card-model.ts:12`）、`CHAT_DIFF_MAX_LINES = 9`（`diff-card-model.ts:8`），而 `ui-primitives` 的输出卡默认 16 行。

通用输出卡的几何是 `white-space: pre` 加横向滚动，超过 `maxLines` 时留头加尾的切片，展开按钮标出隐藏行数（`ui-primitives/README.md:193`）。shell 的尾注用正则还原成「第几到第几行、共几行、是否截断」的说明文字，`[output truncated]` 单独识别。

read 卡还有一道严格校验：meta 的 `path`、`offset >= 1`、行号递增、`totalLines` 都要成立，结果 envelope 也要匹配 `<path>` / `<type>file</type>` / `<content>` 的格式，任何一项不成立就退回 generic 行（`read-card-model.ts:100-118`）。这道校验是为了让旧会话里的老结果也能安全降级。

### 4.4 失败、重开与降级

工具卡的失败分三类，显示不同：`error` 是工具返回了错误；`stopped` 是错误码为 `interrupted`，表示进程或轮次被中断；未执行是 Auto review 拒绝，卡片显示拒绝理由而不是执行输出。

`ask_user_question` 的问题行比普通卡片多一个状态。前台等待超时后，工具返回 pending，问题行变成可重开的入口；用户点开时走 `ctx.userQuestionPanels.reveal`，迟到回复进入 inbox 后，行内直接显示已提交的答案。关闭面板不会发送任何内容。

未知或畸形的工具数据一律回退到 generic 行，卡片组件不抛错。这条在回放旧会话时很重要：半年前的参数可能不满足今天的 schema。

## 5. 状态归属：object layer 与 store 的边界

### 5.1 三条通道

客户端里能变的数据只有三个来源，规则写死在 `packages/client/AGENTS.md`：

- 父组件渲染时已经知道的，走 owner props。
- 只有组件自己知道的，走局部 state。
- 跨 entry 共享或在重挂后要活下来的视图状态，走注册时声明的 store。

派生数据一律用 `useMemo` 从框架 hook 的数据算，不允许自己再订阅一份。

store 只装「怎么画」：选中项、草稿、面板宽度、折叠状态。业务数据在 object layer：`ClientSessions`、`SessionManager`、`Session`、`ClientWorkspaceModel`、`ClientJobs`。把 session 数组放进 store，刷新页面会丢，重连会与日志对不上，多标签页会各说各话。

写 store 必须是 `createXXXStore()` 工厂，模块级句柄被明确禁止，因为它事实上是单例。注册时把句柄传给组件，读用 `props.useStore`，写只用声明的 `props.actions.*`。

通知也要分档：`notifyNow` 只用于用户手势的直接回显，结构更新用微任务批处理的 `markDirty`，可见的流式块用累计的 `markFrameDirty`。用错档位的症状是列表每来一行就整块重排，或者反过来，输入框回显慢半拍。

### 5.2 六层所有权

`docs/subsystems/web-client.zh.md` 给了一张六层表，与第 0 节那张对应。这里补三个边界细节：

- `SessionManager` 拥有 list baseline、实时 control 更新、惰性 Session 实例、projection store、subagent 目录，以及「拉取与后到更新」的冲突顺序。顺序错了，界面会短暂回退到旧列表。
- `Session` 拥有一段连续窗口、分页、follow、prompt 与 control state。它不拥有视图，视图由 Conversation 派生。
- workspace 行保留排序位置，归档后置灰但仍在原位；要打开归档行必须先取消归档，走 `workspace.unarchiveSession`。

### 5.3 断线重连的时序

{{< fig "diagrams/04-reconnect-timeline.svg" "图 4：一次断线重连。物理链路恢复后，每个逻辑流各自重开；Session 窗口整段替换，control 与 workspace 用新 baseline 原子替换。" >}}

重连分两段，两段的负责人不同：

1. 物理段。Gateway mux 重新连上 WebSocket，Connection 发布一个新 generation。默认在 3 秒未就绪时告警、15 秒超时（`connection/src/recovery-config.ts:29-30`）。
2. 逻辑段。每个 `RemoteStream` 拿新 generation 重开自己的 source。载体失败可以重试；业务错误、非法 opening item、协议违规会终止这个逻辑流，不会无限重连。

数据恢复按域各自处理：

- Session journal 校验逻辑 seq range，用每代 opening snapshot 替换窗口；`page()` 补更早历史并修复 range gap。
- control 与 workspace 在断开期间保留最后值，重连后先用新 baseline 整段替换，再应用增量。
- 普通 forwarded notification 不 replay。需要可靠恢复的域必须自带 baseline、cursor 或显式查询。

架构里没有统一的 Client `Runtime`、`HostFrame`、`events.mux`，也没有一个通用 `resync()`。每个流自己负责恢复，代价是代码分散，收益是一个域挂掉不会拖着别的域一起停。

自己搭的时候，可以先把「一个流一个 generation」写进类型里：`RemoteStream` 的每个结果都带世代号，跨代的增量直接丢弃。这一步能省掉后面大量的「幽灵更新」，也就是旧连接的回包在新连接上生效。

### 5.4 资源地址：文件读取不经过 session

右侧栏的文件、文档预览、浏览器面板都不把内容塞进事件。它们用地址：`dsh-resource://file/session/<sessionId>/<path>`，协议头决定谁来提供。

一个协议只能有一个提供方，第二次注册直接抛错。提供方实现 `open(address, { signal })`，返回 `RemoteResult` 帧流。消费方用 `useResource` 读，状态有 `none`、`loading`、`live`、`failed` 四种；`failed` 会保留上一个成功的值，所以面板不会闪成空白。

```ts
ctx.resources.register({
  protocol: 'file',
  open: async function* (address, { signal }) {
    yield await statWorkspaceFile(address, signal)
  },
})
```

不订阅但需要保活时用 `pin(address, signal)`：第一个持有者打开流，最后一个释放时中止。右侧栏每个 tab 在存续期钉住自己的地址，切走再切回来不会重新拉一遍。这条设计把「文件内容」与「会话事件」彻底分开，文件再大也不进日志。

代价是地址要能被序列化，且必须在重连后仍然有效。`fileAddressFor` 与 `parseFileAddress` 成对出现，就是为了让地址在后端重启后还能解析回同一个文件。

## 6. HMR 在客户端插件里的位置

客户端插件的 HMR 不刷新页面，也不重挂 React 树。它替换的是 entry 级别的模块。

Host 半侧轮询每个包已经盖章的入口产物，默认 500ms；revision 用入口文件的 mtime、ctime、size 判定，不读内容、不算哈希。所以 Host 重启但产物没变时，revision 不变，重连也不会换插件。

浏览器半侧把 `graph` 与 `rebuilt` 两类帧交给 Client Modules，由它串行化 entry 变更。一次 `rebuilt` 的顺序是：

1. invalidate 旧模块；旧 fiber 还在服务时先预取新脚本。
2. 删掉 registry runtime，排空旧 fiber，清 entry 引用，移除它拥有的样式。
3. 模块系统 materialize 新 exports，然后 `entry.refresh()`。
4. 旧 effect 结束后再注入 CSS。

service provider 的替换由 Cordis 自己级联：fiber 的 activation epoch 把 provider 的 uid 串起来，依赖它的 fiber 会跟着重挂。HMR 不记这笔账。

失败策略是保守的：下载失败就保留旧插件继续跑；旧 fiber 已经拆掉之后 import 或激活失败，不回滚，错误进页面本地状态，设置里的插件列表可以重试。

代价写得很直白：重载丢 React 状态，react-refresh 级别的状态保留被明确排除。connection、runtime fiber、Session 这些数据层不动。所以一次工具卡片的 HMR 会保留 transcript 与滚动位置，只重置那张卡片内部的局部 state。

对 slot 来说，HMR 等于一次完整的「贡献方消失再由新版本注册」。dispose 时贡献必须被移除，这条在测试里是硬要求：拆掉 fiber 之后，`ctx.slots.entries(key)` 必须为空。

这条要求把「注册」变成了可验证的生命周期。一个插件的最小 HMR 测试长这样：

```ts
const fiber = ctx.plugin(SomeUiPlugin)
expect(ctx.slots.entries('tool.call.toolview')).toHaveLength(1)
await fiber.dispose()
expect(ctx.slots.entries('tool.call.toolview')).toHaveLength(0)
```

两个断言都必要。只测注册，会漏掉忘写 disposer 的插件；只测卸载，会漏掉注册路径根本没跑起来的插件。更难写的是 `slots.inject` 那一层：贡献挂在声明方的生命周期上，声明方自己也可能被 HMR 换掉。

自己搭的时候，可以先只做「整包替换」，不做模块级热替换。整包替换的语义简单：拆掉 fiber，重跑 apply，贡献重新注册。等确认所有注册都能正确 dispose，再考虑粒度更细的方案。

## 7. 设计取舍

选显式声明，放弃隐式发现。 slot 的 key 必须先在 `SlotMap` 里声明，父组件必须把 child 写进 `children`。代价是加一个扩展点要改两处，收益是漏写会在装载期报错，不会在运行时静默失效。

选投影层，放弃「组件直接读事件」。 多一层 assembler，等于多一份要维护的状态机与性能契约。换来的能力是 prepend、gap repair、重试折叠、PTC 子调用和替换式压缩。如果产品只是一个单轮对话窗口，不需要这些，投影层是纯成本。

选窄 store，放弃「前端一个 store 装所有」。 边界感很强：业务数据永远在对象层，store 只装视图状态。代价是组件要区分「这是业务还是视图」，新人容易把草稿放进 session。收益是刷新、重连、多标签页三种场景不用各写一套恢复逻辑。

选分发式卡片，放弃统一渲染器。 每个业务包注册自己的 toolview，核心包只留通用行。代价是视觉一致性要靠 `ui-primitives` 与评审维持；收益是新工具不需要改核心渲染器，装不上就自动降级。

选 entry 级 HMR，放弃状态保留。 替换粒度大，React 状态会丢。如果后续要保留局部状态，需要引入更细的热替换边界，成本会扩散到每个组件。

什么时候该反悔：如果客户端只有一块固定界面，没有第三方插件，这五条里最该砍的是 slot 与分发式卡片。固定界面用直接 import 更省事，代价是加功能要改核心包。反过来，如果产品要开放插件，slot 的声明成本会在第三个插件出现时回本。

## 8. 踩坑点

`presentCall` 不是客户端卡片的数据源。它定义在工具上，返回展示意图，但浏览器读的是原始事件字段与持久 `meta`。把卡片写成「调用 presentCall」会在回放与旧 schema 上直接崩。

`keyed` 复用 key 等于覆盖。加新卡片要用新 wire name；两个包抢同一个 key，后注册的会替换先注册的展示，而且不报错。

`children` 漏写会在激活时失败。往 `tool.call.toolview` 注册之前，确认父组件已经在 `children` 里声明了它，并且自己用了 `slots.inject` 而不是裸 `register`。

视图节点不能撤回。折叠逻辑想隐藏一行，只能返回 `visibility: 'hidden'` 并保持 key 不变。返回 null 会把下游的锚点与滚动位置一起打乱。

store 里放业务数据，第一次刷新就会暴露。`SessionManager` 与 `Session` 的重建依赖日志窗口，store 里的副本没有恢复路径。

截断上限是两套数字。聊天行 8 行、9 行，details 面板 16 行，模型侧还有一套字节与行数上限。调其中一个之前，先确认自己改的是哪一层。

## 9. 源码索引

| 位置 | 内容 |
|---|---|
| `packages/client/README.zh.md` | 60 个包的职责表与 ctx 键 |
| `packages/client/AGENTS.md` | slot、props、store、分层红线 |
| `packages/client/web/src/platform.ts:8` | `PLATFORM_MODULES`，shell 预置的静态模块表 |
| `packages/client/ui-slots/src/renderer.ts` | slot 运行时与 `root` 渲染入口 |
| `packages/client/ui-renderer/src/client/*` | ctx 到 React 的绑定与 hydrate |
| `packages/client/ui-tool/src/client/contract/slots.ts` | `tool.call.toolview` 的 owner props 与 phase |
| `packages/client/ui-tool/src/client/apply.ts:36-51` | 声明子 slot 并注册 `tool-call` 节点 |
| `packages/client/ui-tool/src/client/tool/ToolCallTree.tsx` | keyed 分发、fallback、PTC 子调用递归 |
| `packages/client/ui-tool/src/client/tool/models/tool-call-model.ts:372-396` | 行模型与状态推导 |
| `packages/client/ui-conversation/src/client/conversation/assembler.ts:198-448` | Context 折叠与两条 flush 路径 |
| `packages/client/ui-chat/src/client/apply.ts:227` | `conversation.chat.node` 的 keyed 声明 |
| `packages/client/connection/src/recovery-config.ts:29-30` | 代际就绪告警与超时 |
| `packages/client/hmr/README.md` | entry 替换顺序与失败策略 |
| `packages/core/tools/src/contracts.ts:93-164` | `presentCall` / `presentResult` 与 `meta` |
| `docs/subsystems/{web-client,slots,conversation,client-resources}.zh.md` | 四份权威参考 |

## 最后

这套客户端最不直观的地方，是它几乎不保存「结果」。它保存事件窗口、视图节点和一小撮视图状态，其余每次都能从日志重新算出来。代价是每次重连都要重算一遍，收益是重算的结果与断线前一致。
