---
title: "实验面盘点：DSH 里哪些能力还在评估期"
date: "2026-11-16T21:30:00+08:00"
slug: "dsh-experimental-surface"
summary: "把 packages/experimental 的 61 个目录按域摊开，对照三词定义、命名与隔离规则，给插件作者一张依赖分层表。"
description: "DSH 实验包的全量盘点：computer-use、browser-use、action cards、workspace、deliverables、feedback、settings、identity，以及哪些接口可以依赖。"
tags: ["harness","architecture","agent"]
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）。行号取自该 commit 的 `packages/**/src`、`scripts/` 与 `docs/`；稳定性判断来自仓库内的策略脚本与 CI 检查，未做运行时观测；由 README、AGENTS.md 与类型定义合并得出的结论，在句子里标了来源。

翻到 `packages/experimental` 的人，通常带着两个相反的问题。一个是「这些能力能不能直接用」，另一个是「我写的插件能不能依赖它们」。两个问题的答案都不在目录名里。

这一组有 61 个目录（`ls -d */ | wc -l`），其中 9 个目录名以 `-bundle` 结尾，只负责把别的包组合成可安装的一层。剩下的是 provider 实现、工具包、客户端面板和共享运行时。规模已经超过很多项目的全部源码，所以值得单独盘一次。

本文按四步走：先解释仓库对「实验」的定义，再按功能域把 61 个目录摊开，然后挑四组最容易误判的能力（computer-use、browser-use、action cards、产品侧五个服务）看它们的注册与生命周期，最后给插件作者一张依赖分层表。状态与扩展点的整体关系可以对照[状态与扩展点地图](/posts/dsh-state-and-extension-atlas.html)，客户端侧的挂载方式在[客户端架构](/posts/dsh-client-architecture.html)里，这里不重复。

## 1. 三个词各有归属

仓库没有给「实验」下情绪化的定义，而是给了三个互相独立的标签，各自回答一个问题。

Experimental 回答的是支持责任：完整的公共能力仍在评估，不构成产品支持承诺；行为或 API 可变，能力也可以撤回；工程、安全、文档、测试和已发布数据的义务不因此放松（`packages/experimental/README.md:26`）。同一段还限定入组条件：只有整个公共契约都处于实验性、或者仅内部可用的包才进这一组，产品包内部的一个实验选项留在原产品角色里。

Optional 回答的是启用方式：用户显式选择才打开。Official 回答的是维护归属：项目维护，并且在 Plugins 页里列出。

README 紧接着的一句最容易被跳过：这两个词都不描述成熟度，发布与否、默认安装与否、GUI 里可见与否，也不描述成熟度（`:28`）。

`:28` 的后半句把「稳定」压到了很窄的区间：不属于实验组的产品包有维护中的产品角色，但它们的公共 API 仍遵循仓库的 pre-stable 政策。换个说法，产品组承诺的是「有人继续修」，不承诺「接口不再变」。

| 标签 | 回答的问题 | 谁决定 | 常见误读 |
|---|---|---|---|
| Experimental | 谁承担支持责任 | 维护者 | 不稳定，所以可以随便改 |
| Optional | 怎么启用 | 用户 | 装了就等于开了 |
| Official | 谁维护、在哪列出 | 项目 | 官方等于稳定版 |

三者都不描述成熟度，这句话的实用价值在于：你不能用任何一个标签推断 API 会不会变，只能推断出「出问题时谁负责」。

判断成熟度要看另外三件事。第一，包在哪个分组，产品组还是实验组。第二，CI 里有没有对应的隔离检查，实验组有 `verify-default-product-isolation`，产品组没有对等的反向检查。第三，公共契约是不是只剩注册接口，形状越小越耐用。

举个例子。`packages/computer-use/computer-use` 在产品组，提供 `ctx.computerUse` 注册表，它承诺的是这个注册表会继续存在。具体能点鼠标的实现 `cua` 在实验组，能力可能被撤换。读目录时把这两件事分开，能少掉一半困惑。
还有一个容易忽略的细节：Optional 与 GUI 可见性也不绑定。README 说插件管理页决定哪些 bundle 默认出现、隐藏的怎么启用（`packages/experimental/README.md:12`）。所以「在界面上找不到」和「没装」是两件事，先看 profile 里的启用记录，再去翻 `OPTIONAL_BUNDLES`。


## 2. 61 个目录按域摊开

按功能域分组之后，61 个目录的分布比名字看起来均匀：桌面与浏览器驱动 9 个，集成桥 14 个，会话与检索 13 个，语音与翻译 6 个，卡片与交互 5 个，客户端面板 7 个，工程配套 7 个。

| 域 | 数量 | 代表目录 | 形态 |
|---|---|---|---|
| 桌面执行 | 5 | `cua`、`cua-macos`、`cua-bundle`、`computer-use-cua-driver-mcp`、`computer-use-cua-driver-native` | provider 实现加一个开关 bundle |
| 浏览器 | 4 | `browser-use-playwright-mcp`、`browser-use-chrome-devtools-mcp`、`browser-use-stagehand-native`、`browser-use-runtime` | 三个 provider 加共享运行时 |
| 集成桥 | 14 | `hook-protocol`、`webhook`、`webhook-github`、`liveshare`、`inspector`、`claude-code-mods`、`plugin-market-bundle` | 协议包、桥接包、市场与推荐 |
| 会话与检索 | 13 | `session-search`、`session-inspector`、`tool-session-query`、`tool-terminal`、`terminal-bundle`、`tool-ralph`、`tool-worktree` | 工具包加各自的绑定 bundle |
| 语音与翻译 | 6 | `speech-to-text`、`speech-to-text-sensevoice`、`translator`、`cot-translation-bundle`、`voice-input-bundle` | 能力包加语音入口 |
| 卡片与交互 | 5 | `action-cards`、`client-ui-action-cards`、`skill-badge`、`badge-skill-bundle`、`auto-review` | 提案服务与它的界面 |
| 客户端面板 | 7 | `client-ui-stats`、`client-ui-cot-translation`、`client-ui-forge`、`client-ui-voice-input` | 只有 UI，没有 Host 服务 |
| 工程配套 | 7 | `forge`、`forge-github`、`forge-session`、`webworker-runtime`、`ptc-runtime-python` | 运行时与执行环境 |

这张表里有两类东西值得分开看。前六行的包大多提供 Host 侧服务或者工具，会注册 `ctx` 上的键；最后一行和后两行里，有相当一部分只做组合或只画界面，注册表里找不到它们。
{{< fig "diagrams/01-experiment-map.svg" "61 个实验目录按域分组：桌面与浏览器驱动 9 个、集成桥 14 个、会话与检索 13 个、语音与翻译 6 个、卡片与交互 5 个、客户端面板 7 个、工程配套 7 个，bundle 只做组合。" >}}


还有一组成对出现的名字：`action-cards` 配 `client-ui-action-cards`，`cot-translation-bundle` 配 `client-ui-cot-translation`，`voice-input-bundle` 配 `client-ui-voice-input`，`forge-bundle` 配 `client-ui-forge`。Host 能力与客户端界面拆成两个包，可以只装一半：装 Host 不装 UI，工具在但没有面板；装 UI 不装 Host，面板在但拿不到数据。

`-bundle` 目录不提供新能力。它们把若干包组合成一次安装，配置项很少，取消它最多让某个功能入口消失，不会留下半套状态。
61 这个数字也可以反过来读。会进默认安装路径的只有产品组包；这 61 个目录里，能直接出现在默认对话里的只有被启动器选中的少数。多数目录的命运是「装得上、默认关」，所以数量大不代表默认面宽。

集成桥那 14 个目录里还有一个小集群：`hook-protocol`、`hooks-claude-code`、`hooks-codex`、`claude-code-mods`。它们服务的是「把别的 harness 的钩子接进来」这一类需求，协议包定义形状，桥接包各自适配一种宿主。要接第四种宿主，照 `hook-protocol` 的形状写一个新包，别改协议。

命名与发布规则比功能分组更硬。新包要叫 `@deepseek-ai/dsh-<name>`，禁止 `dsh-experimental-` 前缀，并且要把目录加进 `scripts/experimental-package-policy.ts` 的 `EXPERIMENTAL_PACKAGE_NAME_EXCEPTIONS`；隔离检查与插件记录的所有权，只通过这张映射认出无前缀的实验包（`packages/experimental/AGENTS.md:6`）。老包可以保留前缀，但只能出现在 `LEGACY_PREFIXED_EXPERIMENTAL_PACKAGE_DIRECTORIES` 里，`pnpm run constraints` 拒绝清单外的前缀，而且这份清单不允许增长。

规则为什么盯名字：带前缀的包在晋升到产品组时必须改名，改名会打断所有 import、依赖条目和配置行（`AGENTS.md:6` 的原话是 "breaks every import, dependency entry, and configuration that names it"）。新包从一开始就不带前缀，晋升时只换目录和分组，不动 npm 名字，下游的 `package.json` 一行都不用改。

发布默认是公开的。实验包默认加入 dsh release family：省略 `private`，设 `publishConfig.access: public`；只有 `PRIVATE_EXPERIMENTAL_PACKAGE_DIRECTORIES` 里的目录保持私有（`AGENTS.md:7`）。这个私有清单目前是空数组（`scripts/experimental-package-policy.ts:111`），按默认规则，61 个目录都会作为公开包发布。

公开不等于放进默认安装。`scripts/verify-default-product-isolation.ts` 的头注释写明了它的职责：把实验包挡在默认安装、运行时 import 和随包发布的组合之外，唯一被点名的例外是启动器在 `OPTIONAL_BUNDLES` 里列出的那种 bundle（`:1-6`）。这类 bundle 随安装发出去，但不被任何随包模板选中，用户要自己在插件页里打开。

`OPTIONAL_BUNDLES` 一共 15 条，全部是实验组的名字（`packages/util/official-bundles/src/bundles.ts:56-72`）：

```ts
// packages/util/official-bundles/src/bundles.ts:56-72（摘录）
'@deepseek-ai/dsh-personalized-recommendations',
'@deepseek-ai/dsh-cua-bundle',
'@deepseek-ai/dsh-experimental-session-search',
'@deepseek-ai/dsh-experimental-ralph-bundle',
'@deepseek-ai/dsh-experimental-terminal-bundle',
...
'@deepseek-ai/dsh-experimental-forge-bundle',
```

名单里多数还带着 `dsh-experimental-` 前缀，这条 legacy 前缀因此仍然活在随包发布的包名里。新规则管的是新增目录，已经发出去的包名不能随便动。

同一份规则还禁止 release 组包在 `dependencies`、`optionalDependencies`、`peerDependencies` 里出现实验包；测试可以用 `devDependencies`，示例可以显式加载（`AGENTS.md` 第 8 条）。于是「产品包依赖实验包」这件事在 CI 层就断了，不靠自觉。

## 3. computer-use：一个槽位，三个 provider

产品包 `packages/computer-use/computer-use` 不在实验组。它只做一件事：持有 `ctx.computerUse` 注册表，并且只留一个 provider 槽位。

槽位是排他的，连同名重复注册也失败：

```ts
// packages/computer-use/computer-use/src/index.ts:37
throw new Error(`computer use provider "${this.registration}" is already registered`)
```

这个注册表自己不做桌面操作。它不注册任何模型可见的工具，没有公共的桌面操作方法，也没有模型可控的选择器。工具、截图、点击全部由 provider 提供。

实验组里的 provider 有四个实现和一个开关：

| 包 | 做什么 | 额外注册 |
|---|---|---|
| `cua` | 原生实现，自己管桌面会话与权限 | `ctx.cuaAccess` |
| `cua-macos` | Swift 侧的原生执行与平台准备 | 通过 `ctx.computerUse` |
| `computer-use-cua-driver-mcp` | 连已经装好的 cua-driver，走 MCP | `ctx.computerUse` |
| `computer-use-cua-driver-native` | 内嵌原生 npm runtime | `ctx.computerUse` |
| `cua-bundle` | 只做组合，给 PTC session 提供可选开关 | 无 |

`cua` 的入口很短：

```ts
// packages/experimental/cua/src/index.ts:217
yield ctx.computerUse.register(ComputerUseProviderName('dsh-cua'))
```

工具注册也走常规路径，`scope.tools.register(tool)` 挂在 `:348-352`，其中一段判断 `if (config.agentPreset === false) register(ctx)`，意思是不要预设 agent 的场景下自己把工具挂上。这里没有特殊的工具注入通道，Computer Use 的能力面和别的工具一样进 Session log、走同一条执行管线。

权限在 provider 一侧。`ctx.cuaAccess` 持有系统权限状态、显式系统请求和应用撤销；应用级同意通过 User Questions 走人的确认，窗口和应用操作在 dispatch 之前先执行 provider 的 consent policy（`docs/subsystems/computer-use.md`）。

权限模型分了几种类型，各自的持久性不同：

| 类型 | 描述什么 | 存在哪里 |
|---|---|---|
| 应用身份与已保存决定 | 平台应用 id，以及人对它的 grant | provider 持久化，可撤销 |
| 系统权限 | Accessibility 或 Screen Recording，由人选 | 操作系统 |
| 权限进程与状态 | 原生权限检查、观察到的 helper/parent 进程 | 进程内快照 |
| 应用状态 | 已保存授权、被拒应用、持久化是否可用 | provider 内部 |

session-only 的授权留在 provider 内部，不写到共享状态里。这个划分让「这次会话允许点这个窗口」和「这台机器永远允许控制这个应用」分开，撤销一个不会误伤另一个。
应用授权走 User Questions，不走 permission preset。原因是它需要人的显式确认，而且确认的对象是一个具体的应用和一次具体的操作范围，agent 不能代答。系统权限是另一条路：Accessibility 与 Screen Recording 由人去系统设置里授予，provider 只负责检查状态和发起请求。
{{< fig "diagrams/02-provider-consent.svg" "Computer Use 的注册与授权路径：产品包只留一个 provider 槽位，provider 自己注册工具，窗口操作在 dispatch 前过 consent policy，系统权限由人授予。" >}}


这条分界也解释了为什么 `cua` 要单独持有 `ctx.cuaAccess`。权限状态需要跨 provider 存在，换一个 provider 实现不应该让已保存的应用授权失效；而 session-only 的临时授权又必须跟着会话走，所以留在 provider 内部。

生命周期的几条约束值得抄进设计笔记。provider 在关闭工具与自有资源期间保留注册，启动失败才释放；MCP provider 重连期间也保留注册，避免上层看到注册表抖动。一个 provider 不预留桌面，跨 Session、跨 DSH 进程的 observe/act/verify 由调用方协调。取消一次调用不能撤销已经送达的输入。

几种失败的样子：

| 事件 | 注册表 | 工具 | 调用结果 |
|---|---|---|---|
| provider 启动失败 | 释放 | 消失 | 上层看不到 provider |
| MCP 断线 | 保留 | 仍在 | 调用报连接错误 |
| 用户拒绝授权 | 保留 | 仍在 | 动作被策略拒绝，带原因 |
| 取消在途调用 | 保留 | 仍在 | 已送达的输入不回收 |

自己搭的时候，把 Computer Use 当成「有人在场的驱动层」来用，不要当成无状态的函数调用。模型拿到的是截图和动作接口，窗口归属、应用授权、两次动作之间有没有别人动了鼠标，这些问题都得在调用方这一层解决。稳妥的写法是一个 Session 一个串行队列，observe、act、verify 三步之间不复用旧截图，输入送达后立刻校验目标窗口是否还是同一个。

## 4. browser-use：浏览器归哪个 Session

浏览器这一侧的结构相同。产品包 `packages/browser-use/browser-use` 持有 `ctx.browserUse`，同样是单槽注册，第二次注册（含同名）直接抛错（`packages/browser-use/browser-use/src/index.ts:37`，错误文案与 computer-use 那条一致，只换了 provider 名）。共享服务只登记名字，不提供公共的浏览器操作、浏览器资源或者模型可控选择器。

初始浏览器引擎是 Chromium。provider 在 profile 或 preset 里配置，可选 `playwright-mcp`、`chrome-devtools-mcp`、`stagehand-native`。共享的 Session 级浏览器资源在 `browser-use-runtime` 里，`SessionResources<T>` 类在 `packages/experimental/browser-use-runtime/src/index.ts:60`，注册动作在 `src/mcp.ts:129`：

```ts
// packages/experimental/browser-use-runtime/src/mcp.ts:129
yield ctx.browserUse.register(BrowserUseProviderName(options.name))
```

归属规则是这一节的重点，分 launch 与 attach 两种：

| 维度 | launched | attached |
|---|---|---|
| 所有者 | 使用它的那个 live Agent 加 Session | 外部进程或用户 |
| 跨 turn | 复用 | 保留现有状态 |
| 第二个 Session | 不能共享 | 拒绝并发 attach |
| teardown | 关闭自有资源 | 只断开 |
| reload / fork | 得到全新浏览器状态 | 不由 DSH 重置 |
| profile 与登录态 | 不从 Session log 恢复 | 在外部保持不变 |

跨 DSH 进程和其他客户端不在预留范围内，这条写在 `docs/subsystems/browser-use.md` 里。attach 的语义是「借用」，不是「托管」。

MCP provider 的初始化有两个容易踩的时序。第一，每个在 provider 加载之后创建的 live Agent 会初始化一个 client；既有的串行 `agent/created` 事件会等连接与 discovery 完成，才让 create 或 resume 返回。启动失败或取消会让 create/resume 失败并清理 client。第二，attachment 忙的时候，该次激活没有浏览器，后续 turn 也不会重试，要等下一次激活。

discovery 完成后，工具列表才确定。create 的耗时里因此包含了浏览器启动或连接的耗时，客户端在 Session 列表上看到的「创建很慢」有一部分来自这里。

provider 选择可以按任务分：

| 任务 | 建议 provider | 理由 |
|---|---|---|
| 需要确定性脚本、可调试 | `playwright-mcp` | 固定流程，MCP 层能看到每一步 |
| 需要看 console、网络、性能 | `chrome-devtools-mcp` | 调试面更全 |
| 自然语言驱动页面操作 | `stagehand-native` | 由 Stagehand 自己规划动作 |

Stagehand 是个例外。它的 AI 操作使用自己显式配置的原生模型，DSH 只保留任务循环；DSH 的模型路由、凭证复用、底层推理请求捕获和用量计入，对这条路径暂缓。用它的时候，账单和可观测性要单独接。
失败时的表现不一样，排查时要先分清：

| 情况 | create/resume | 浏览器 | 后续 turn |
|---|---|---|---|
| MCP 启动失败 | 失败，client 被清理 | 不存在 | 不存在 |
| 初始化被取消 | 失败 | 不存在 | 不存在 |
| attachment 忙 | 成功 | 本次没有 | 不重试，等下次激活 |
| 第二个 Session attach | 拒绝 | 仍归第一个 | 不受影响 |

第一行和第二行会让创建直接失败，客户端看到的是错误，不是「没有浏览器」。第三行最容易被忽略：Session 起来了，工具在，但一调就报没有浏览器，而系统不会自动补一个。

自己搭的时候，需要批量跑 headless 任务就把 provider 固定成 playwright 或 chrome-devtools，并且显式给 profile 目录。fork 出来的 Session 不会继承登录态，reload 也不会；依赖「先登录再 fork 一批任务」的流程会在第一步就断。需要复用登录态，走 attach 模式，把浏览器生命周期的责任留在 DSH 外面。

## 5. action cards：模型提议，人拍板

`action-cards` 是实验组里少数带完整产品语义的服务。它注册 `ctx.actionCards`，对外提供一个 `propose_action` 工具，行为是「模型提议一个带按钮的卡片，卡片在 turn 结束后出现在答案下面，用户点了才执行」。

工具不是常驻的。它按 agent 的条件注册：

```ts
// packages/experimental/action-cards/src/index.ts:215
}), { when: ({ agent }) => this.declares(agent) })
```

`declares` 在 `:413-414`，注释是「Whether `propose_action` is declared for this agent's next step」。激活条件是本 turn 内 root agent 自己加载过某个已注册 kind 的 skill。subagent 加载、代码里调用，都不激活（`:223` 的注释写明两者都拒绝）。

拒绝面比接受面大，这是有意的：

| 条件 | 结果 |
|---|---|
| 同一 turn 第二次调用 | 拒绝，一个 turn 只出一张卡 |
| 未注册的 action | 拒绝 |
| kind 的 skill 本 turn 没加载 | 报 `ACTION_CARD_SKILL`，提示先加载 |
| 从代码里派发 | 报 `ACTION_CARD_NESTED`，提示直接调工具 |
| subagent 调用 | 拒绝 |
| 空 title 或 brief | 拒绝 |
| details 不符合 kind 的 schema | 拒绝 |
| 待处理用户消息已有 2 条 | 拒绝，避免卡片和下一轮指令打架 |

最后一条的上限来自配置默认值：

```ts
// packages/experimental/action-cards/src/index.ts:19-20
/** Queued next-turn user messages at or above which `propose_action` refuses, so no card is offered. */
queuedMessageLimit: z.number().int().min(1).default(2),
```

「同一个 turn 只出一张卡」这条规则，把小步骤的确认冲动压住了。模型可以在一轮里提议多个动作，但只有第一次成功的那张会留在界面上，其余被替换。替换关系从日志推导，不单独记 `replaced` 事件，所以客户端重放日志能得到同一张卡，不需要额外的状态同步。

卡片的生命周期也有明确规则。一个 turn 里第一次成功的 `propose_action` 成为卡；同一 turn 再来一张，替换掉所有还没开始的旧卡。fork 继承来的事件不产卡。每次用户动作（包括 Not now）之前，先跑 kind 的 `reconcile`，从 Session 之外的状态更新卡片；dismissed 和 replaced 的卡不再接受动作；同一张卡上的并发动作共享一个 operation。执行动作需要卡所属 Session 的精确 live Agent，且来源不能是 subagent。

kind 是这一层粒度最细的扩展点。kind 包注入 `actionCards` 并调用 `ctx.actionCards.register(kind)`，注册时声明：动作标识、提案用的 skill、关闭时的邀请文案、details 的 zod schema、可接受的用户动作与每个动作的共享检查（global Remote access、只读 Session 拒绝写操作）、仍可 dismiss 的 phase、可选的状态文案与状态通知字段、可选的 reconcile，以及除 dismiss 外每个动作的 handler。

需要声明的东西不少，因为卡片是少见的「模型输出直接变成可点击控件」的地方。schema 收窄一点，用户看到的按钮就少一点歧义；共享检查写全一点，只读 Session 和远程连接就不会出现一个点了没反应的按钮。

handler 用 `ctx.actionCards.record()` 记录决定，返回四种结果之一：

| 结果 | 含义 |
|---|---|
| accepted | 用户点了，处理已开始 |
| settled | 处理完成，卡片可以显示最终状态 |
| refused | 明确拒绝，附带原因 |
| unconfirmed | 无法确认结果，卡片保留可重试的入口 |

卸载一个 kind 包之后，该 kind 的动作会被拒为 unavailable，但 Not now 仍然能关掉没开始的卡。这个降级路径设计得比较克制：能力消失时，用户至少还能把卡片清掉。

同一组里的 `client-ui-*` 是另一类扩展点，只加界面，不加工具。`client-ui-action-cards` 画答案下的卡框、kind 的 body 和提案过程行；`client-ui-stats` 在每 turn、每 step 上显示上下文、耗时、缓存与工具成本；`client-ui-cot-translation` 给推理加翻译和原文切换；`client-ui-voice-input` 做麦克风采集与受保护的草稿插入；`client-ui-forge` 是 Git checkout、worktree、PR 面板；`client-ui-claude-code-mods` 把 mod 树放在 prompt 上方。这些包没有 Host 侧 `ctx` 键，取消它们只影响观感。
卡片与 User Question 都能拦住人，分工不同。Question 在 turn 中间发出，回答之前 turn 不结束；卡片在 turn 结束后出现，点击是下一个 turn 的输入。前者适合「这一步需要参数」，后者适合「方案做完了，选一个」。


自己搭的时候，要加一种卡，先把三件东西准备好：一个能教会模型何时提议的 skill、一个足够窄的 details schema、一个能从外部状态把卡改对的 reconcile。缺 reconcile 的卡在状态变化后会给用户一个已经过期的按钮，这比不提供卡片更糟。另外别从工具实现里直接调 `propose_action`，`ACTION_CARD_NESTED` 就是为这个场景留的。

## 6. 五个产品侧服务

`workspace`、`deliverables`、`feedback`、`settings`、`identity` 都不在实验组，但经常被当成实验能力，因为它们各自只覆盖产品的一个角落。放在一起看，边界比较清楚。

| 服务 | 包 | 写进什么 | 进模型上下文 |
|---|---|---|---|
| workspace | `packages/workspace` | 项目与会话分组，持久层 | 否 |
| deliverables | `packages/deliverables` | log-only 的 `deliverables/presented` 事件 | 否，但 present 的工具结果会 |
| feedback | `packages/feedback` | `feedback/message-put`、`feedback/record` | 否 |
| settings | `packages/settings` | active profile patch | 否 |
| identity | `packages/identity` | `$DSH_HOME/.anonymous-user-id` | 否 |

workspace 的核心是 `WorkspaceRegistry`（`packages/workspace/workspace/src/index.ts:170`）。它管命名有序的项目，会话按目录分组；会话可以隐藏但不删除；删项目不删目录与历史；隐藏或删除的会话会掉进未分组历史，仍然可访问。它需要 session storage 和 persistence backend，但不向模型暴露工具、prompt 或 session 事件。

它的错误类型说明了防护边界：`WorkspaceUnknownSessionError`（`:47`）挡对未知会话的操作，`WorkspaceActiveSessionError`（`:63`）挡删除仍有活跃会话的项目，`WorkspaceArchivedSessionPinError`（`:75`）挡把归档会话钉在可见位置，`WorkspaceOrderInvalidError`（`:86`）挡非法的顺序更新。四种错误都发生在写操作上，读操作宽松。

deliverables 有两类来源。一类是模型通过 `present` 工具声明的文件，落在 `deliverables/presented` 这个 log-only 事件里（`packages/deliverables/tool-present/src/index.ts:39` 声明工具名，`:106` 追加事件）。另一类是本轮改动过的文件：turn 开始时和结束时各做一次 git working-tree 快照，加上每个文件工具编辑前后的整文件捕获，覆盖 git 管不到的路径。

两类都带每个文件在 turn 起止时的对比，由 Host 服务在 Session 存活期提供，只有客户端读，Web 端在 turn 末渲染。`PresentedFile` 的语义是「声明过的文件，当前内容仍在源路径」，所以它不是副本，也不是归档。

快照的边界规则也写清了：patch 调用复用同一套 summary 与 comparison API；git 覆盖之外的移动记为删源加增目标；被 revert 掉的 commit 只贡献净变更。这些规则让「这一轮改了什么」在复杂的 git 操作下仍然有一个稳定答案。
{{< fig "diagrams/03-deliverable-lifecycle.svg" "交付物的两条来源：present 工具写日志事件，workspace-changes 在 turn 边界取 git 快照并补文件编辑前后的整文件内容，客户端在 turn 末读取。" >}}


feedback 分成两套。消息级反馈跟着单条 assistant 消息走，可编辑，事件是 `feedback/message-put` 与 `feedback/message-delete`，版本号只用来做 CAS 相等比较；会话级 remark 不可变，事件是 `feedback/record`，配一套分类法。三类记录都是 log-only，永不进模型上下文。这个选择有明确理由：让模型看见自己被怎么评价，会改变它的回答策略。

settings 从插件的 Config schema 派生表单，编辑写入 active profile patch，由 Loader 应用，插件读自己的 volatile 引用。并发编辑用 revision 挡：`SettingsForms` 在 `packages/settings/settings/src/index.ts:223`，revision 映射在 `:225`，递增并广播 `settings/document-updated` 在 `:313-317`；冲突时抛 `SettingsConflictError`（`:45`），消息里带期望值与当前值（`:59`，`:395` 抛出）。冲突消息的形状是 `settings namespace "<ns>" changed since it was read (expected revision X, now Y)`，前端可以据此提示重新加载，不用整页刷新。

revision 放在服务端而不是表单里，是为了让两个窗口同时改同一段配置时有一个确定的赢家。先读到 revision 的请求成功，后到的那个拿到冲突，用户重新读一次再提交。

identity 给每个 harness home 一个随机 UUID v4，存成 `$DSH_HOME/.anonymous-user-id`（常量在 `packages/identity/anonymous-user-id/src/index.ts:29`，路径拼装在 `:69`，生成器默认 `crypto.randomUUID` 在 `:75`）。它不派生自 hostname、网络地址、git remote 或其他可识别来源；同一个 `$DSH_HOME` 的多进程共享它；删掉文件，下次启动换一个新 id；读写是同步的，按解析后的文件路径 memoize。telemetry、feedback 和 DeepSeek provider 请求共用这一个值。

这五个服务的共同点是「旁观者」：它们读 Session 的产物，或者给产品加组织方式，都不修改 agent 的推理输入。插件作者想让模型感知到它们，得自己加工具或注入上下文。

## 7. 生命周期对照

把四类注册面放进同一张表，能看出失效路径的差别。

| 对象 | 注册点 | 何时出现 | 何时消失 | 失效时的表现 |
|---|---|---|---|---|
| Computer Use provider | `ctx.computerUse.register` | profile 加载时 | 卸载或启动失败 | 工具消失，调用报无 provider |
| 浏览器 provider | `ctx.browserUse.register` | profile 加载时 | 卸载或启动失败 | 同上，附 Session 归属错误 |
| action card kind | `ctx.actionCards.register` | profile 加载时 | 卸载 | 动作被拒为 unavailable，Not now 仍可用 |
| deliverables 服务 | Host 服务注册 | Session 创建 | Session dispose | 客户端读不到这一轮的交付物 |

四个对象的寿命不一样。前三个跟着 profile 走，只在插件重载时变化；deliverables 跟着 Session 走，每个会话一份，进程重启后旧会话的交付物不再实时提供。

provider 的注册还有一个反直觉的细节：它在关闭自有资源期间保留注册。也就是说「provider 在」和「provider 能用」是两件事。上层如果只检查注册表，会在关闭窗口期拿到一个会失败的 provider。判断可用性要看调用结果，或者 provider 自己暴露的状态。

action card kind 的寿命最短。它随包加载出现，随包卸载消失，期间创建的卡还留在界面上。卸载后的降级是「动作不可用，但可以关掉」，这个策略保住了用户对界面的控制权。
四个对象的注册时机也有差别。前三个在 profile 加载时完成，失败会让整个 profile 的加载报告错误；deliverables 的服务按 Session 创建，一个会话出问题不影响下一个。排查时先看错误发生在加载期还是会话期，范围能缩小一半。


## 8. 稳定度分层

把上面的材料按「变动时会发生什么」重新排一次，可以得到一张比目录更实用的表。

| 层 | 例子 | 变动后果 | 依赖建议 |
|---|---|---|---|
| 产品服务契约 | `ctx.workspaceRegistry`、`ctx.settings`、`ctx.computerUse` | 随版本演进，pre-stable | 读类型，锁 minor |
| 注册 seam | `computerUse.register`、`browserUse.register`、`actionCards.register(kind)` | 形状简单，仍在实验组 | 只依赖形状，不依赖实现 |
| provider 实现 | `cua`、`browser-use-playwright-mcp` | 可整包撤换或改名 | 当可替换件 |
| GUI 与组合 | `client-ui-*`、`*-bundle` | 只影响观感与启用方式 | 不往里放业务状态 |
{{< fig "diagrams/04-stability-layers.svg" "四层稳定度：产品服务契约、注册 seam、provider 实现、GUI 与组合；越往下越像可替换件，插件状态应留在 Host 侧。" >}}


第一层看起来最稳，实际承诺最弱。README 明说产品包的公共 API 仍遵循 pre-stable 政策（`packages/experimental/README.md:28`），所以「稳定」只意味着有人维护，不意味着签名冻结。能写进第三方插件的，是这一层里形状最小的那些方法调用。

第二层是给外部挂的，反而比第一层更耐用。注册接口通常只吃一个名字、一个 provider 对象、或者一个 kind 描述，参数形状变了也很容易跟。它们的问题在别处：包名在实验组里，版本号不承诺语义化。

第三层和第四层要按可替换件对待。provider 实现可能整包撤掉，比如 `computer-use-cua-driver-native` 依赖内嵌的原生 runtime；GUI 包可能只改一版样式。把业务状态放进 `client-ui-*`，升级时会丢。

对自己的第三方插件，判断顺序可以简化成两条。要调用 DSH 的能力，依赖注册 seam，别 import provider 实现；要在 DSH 上叠界面，依赖客户端包，同时把状态留在 Host 侧。前者失败时是编译错，后者失败时是数据错，前一种好修。

对自己要发进这个仓库的包，规则是硬的：release 组不能依赖实验包，实验包默认公开且必须进例外清单，legacy 前缀不能新增。这些约束由 `pnpm run constraints` 和隔离 CI 执行，绕不过去。

自己搭的时候，把依赖面收在 seam 上，还有一个副作用：写测试容易。注册接口可以用假 provider 顶替，不需要真的开一个浏览器或者要一次桌面授权。

## 9. 设计取舍

几个决定值得单独说，因为它们的理由不在代码注释里。

用独立目录而不是 feature flag。目录边界能被 CI 的 import graph 检查，flag 不能。`verify-default-product-isolation.ts` 能挡住传递依赖、运行时 import 和随包组合，靠的是包边界；如果实验能力只是产品包里的一堆 `if (experimental)`，检查就退化成人工 review（`:1-6`）。

实验包默认公开。私有清单为空（`scripts/experimental-package-policy.ts:111`），外部可以直接装、直接报错。代价是 npm 上会出现一批不承诺稳定的包，收益是隔离规则能靠真实安装路径验证，不用维护一份内部镜像。

provider 用单槽注册，不做多路复用。模型可见工具由 provider 提供，两个 provider 同时在场会出现工具名冲突，桌面或浏览器归属也说不清。单槽把冲突提前到加载期，报错信息里带已注册的名字（`packages/computer-use/computer-use/src/index.ts:37`），排查成本低。

卡片工具绑定 skill 加载。`propose_action` 的 schema 不算大，但每种 kind 的 details 各不相同，常驻会让工具列表和 prompt 一起膨胀。用「本 turn 加载过该 kind 的 skill」作为激活条件，等于让模型先读到使用说明再获得工具，token 成本和误用率都降下来（`packages/experimental/action-cards/src/index.ts:215`、`:413`）。

卡片的 reconcile 放在动作之前跑，而不是在渲染之前跑。渲染时更新会让客户端的显示依赖它的写入顺序；放在动作之前，用户看到的状态至少是他点击那一刻的状态。

deliverables 用日志事件加 turn 快照，不扫文件系统。历史 turn 里的文件内容会变，甚至会被删，客户端要回答的是「那一轮交了什么」，只能在 turn 边界取快照。`present` 声明的文件另走事件，因为它是模型的意图，不是文件系统的状态（`packages/deliverables/tool-present/src/index.ts:106`）。

feedback 不进模型上下文。这条取舍的收益是反馈更真实，代价是模型无法从反馈里学习。仓库选择了前者的可观测性，把学习留给显式的记忆与 skill 机制。
kind 做成独立包，也是为了文案与 schema 跟着宿主领域走。`action-cards` 只定义协议与状态机，卡片里写什么、按钮叫什么、哪些动作允许，由 kind 包自己声明。宿主领域变化时只改一个 kind 包，不动内核（`packages/experimental/action-cards/src/index.ts` 的服务定义里只保留了 `propose_action`、队列上限与状态机）。


## 10. 新增能力时的选型

看完材料，实际要动手时只剩两个判断：这个能力属于产品组还是实验组，以及它挂在哪个注册面上。下面这张表是按仓库规则推出来的答案。

| 你要加的 | 分组 | 挂点 | 还要同步改的 |
|---|---|---|---|
| 另一种桌面驱动 | 实验组，独立 provider 包 | `ctx.computerUse.register` | 例外清单、可选 bundle 入口 |
| 另一种浏览器驱动 | 实验组，独立 provider 包 | `ctx.browserUse.register` | 同上，外加共享运行时复用 |
| 一种新的确认卡 | 实验组，kind 包 | `ctx.actionCards.register(kind)` | 提案 skill、details schema、reconcile |
| 一个新的客户端面板 | 实验组，`client-ui-*` | 客户端槽位 | 无 `ctx` 键，不加 Host 服务 |
| 一个新的产品侧服务 | 产品组 | `ctx` 上的服务键 | 产品角色评审、公开 API 记录 |
| 一个新的工具 | 看通用性 | 工具注册表 | 权限与结果类型 |

判断顺序可以固定成三问。第一问，这个能力有没有独立的公共契约；有，就单独成包，没有，就留在调用它的产品包里当选项。第二问，它要不要给模型可见的工具；要，就走 provider 或 kind 的注册面，不要，就只做服务。第三问，它有没有需要跨 turn 保留的状态；有，就把状态写进 Session 事件，没有，就留在进程内。

三问的顺序不能换。先定分组，命名与发布规则才落得下来；先定注册面，容易出现一个产品组包暴露实验 API 的组合。仓库的隔离 CI 检查的是第一问的答案，`ctx` 键的归属检查的是第二问。

还有一条经验：能用现有 seam 表达的，别新开一个。`computerUse` 和 `browserUse` 的单槽设计已经把「同一时刻只有一个实现」写死了，新能力硬塞进去会和现有 provider 抢槽位。真需要并存，应该新开一个服务键，并且想清楚用户的配置界面怎么表达这个二选一。

## 11. 踩坑点

1. 把实验组当成「可以乱改」的区域。README 写明工程、安全、文档、测试和已发布数据的义务照旧（`packages/experimental/README.md:26`），隔离 CI 与 constraints 都在这组包上跑。

2. 在产品包里 import 实验包。release 组的 `dependencies`、`optionalDependencies`、`peerDependencies` 都不允许出现实验包，传递依赖也算；测试放 `devDependencies` 才行。

3. 给新实验包起 `dsh-experimental-` 前缀。新包禁止这个前缀，老包只能留在 legacy 清单里；清单不允许增长，`pnpm run constraints` 会直接拒。

4. 以为装了 bundle 就等于启用。15 个 optional bundle 随安装发出去但默认关闭，要在插件页里显式打开；`OPTIONAL_BUNDLES` 只是可选项清单（`packages/util/official-bundles/src/bundles.ts:56-72`）。

5. 同时挂两个 provider。第二个 register 会失败，错误信息给的是已注册的 provider 名，初次遇到容易误判成自己的配置写错。

6. 把 `propose_action` 当普通工具调，或者把卡片当成「点了就一定执行」。从工具实现里直接调用会报 `ACTION_CARD_NESTED`（`packages/experimental/action-cards/src/index.ts:426`），在 subagent 里调用则不会激活卡片；handler 返回 refused 或 unconfirmed 时，客户端拿到的是决定，不是保证。

7. 假设 fork 或 reload 会继承浏览器登录态。launched 浏览器的 profile 与登录态不从 Session log 恢复，reload 和 fork 都得到全新状态（`docs/subsystems/browser-use.md`）。

8. 以为 `present` 会把文件复制到某个交付目录。`PresentedFile` 的当前内容仍在源路径，客户端读的是源文件，源文件被移走就失效（`packages/deliverables/tool-present/src/index.ts`）。

## 12. 源码索引

- `packages/experimental/README.md:26`：Experimental 的定义与入组条件。
- `packages/experimental/README.md:28`：Optional 与 Official，以及产品包仍遵循 pre-stable 政策。
- `packages/experimental/AGENTS.md:6`：命名规则、例外清单、legacy 前缀与 constraints。
- `packages/experimental/AGENTS.md:7`：默认加入 release family 与私有清单。
- `scripts/experimental-package-policy.ts:31`：legacy 前缀常量。
- `scripts/experimental-package-policy.ts:34`：允许保留前缀的目录清单。
- `scripts/experimental-package-policy.ts:111`：私有实验包清单，当前为空。
- `scripts/verify-default-product-isolation.ts:1`：隔离 CI 的职责与唯一例外。
- `packages/util/official-bundles/src/bundles.ts:56`：15 个可选 bundle。
- `packages/computer-use/computer-use/src/index.ts:37`：单槽注册与重复注册错误。
- `packages/experimental/cua/src/index.ts:217`：CUA provider 注册。
- `packages/experimental/browser-use-runtime/src/index.ts:60`：Session 级浏览器资源容器。
- `packages/experimental/browser-use-runtime/src/mcp.ts:129`：浏览器 provider 注册。
- `packages/experimental/action-cards/src/index.ts:215`：`propose_action` 的条件注册。
- `packages/experimental/action-cards/src/index.ts:20`：待处理消息上限默认值。
- `packages/deliverables/tool-present/src/index.ts:106`：`deliverables/presented` 事件写入。
- `packages/settings/settings/src/index.ts:223`：表单服务与 revision 跟踪。
- `packages/workspace/workspace/src/index.ts:170`：`WorkspaceRegistry`。
- `packages/identity/anonymous-user-id/src/index.ts:29`：匿名 id 文件名。

## 13. 最后

实验面最容易产生的误判，是把「在实验组」和「随时会被删」画等号。仓库的做法是分开两件事：支持责任用分组说清楚，能力边界用注册接口收窄。分组会变，注册接口的形状变得慢。

插件作者能拿到的可靠结论只有三条。要调 Host 能力，走注册 seam，别 import provider；要叠界面，把状态留在 Host，GUI 包只做展示；要发进这个仓库，先看 `AGENTS.md` 的命名与依赖规则，`pnpm run constraints` 会照着脚本拒绝。

剩下的判断只能跟着版本走：每次升级先看实验组的目录列表和 `OPTIONAL_BUNDLES`，这两处变动最能说明哪些能力正在被重新定位。
