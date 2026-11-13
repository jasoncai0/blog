---
title: "组装一个宿主：空根配置、四层 patch 与第一个 turn"
date: "2026-11-13T13:00:00+08:00"
summary: "dsh 启动时没有一份写死的插件清单。它从空根配置开始，按顺序叠四层 patch，再让 Cordis 按服务依赖逐条激活；agent preset 是运行时挂上去的子树，切换时只重建那一块。"
description: "拆解 app-boot 的启动时序、profile/bundle/patch/preset 四层覆盖、preset 与 persona 的区别，以及插件从安装到卸载的可逆点。"
tags: ["harness", "architecture", "meta"]
slug: "dsh-host-composition"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准。
> 第二季第 8 篇。上一季：[《一个 Agent Harness 的解剖》](/posts/dsh-architecture-anatomy.html)；本季导览：[《第二季：把主干两侧补齐》](/posts/dsh-season-two.html)。

第一次翻 `packages/bundle/base/cordis.patch.yml` 会有点懵。一个 YAML 文件里塞了一百多行 `- id: tool-bash`、`- id: session`、`- id: agent-loop`，看起来像全系统的插件总表。可它并不被 import，也不在启动时被直接读取。启动器只做一件事：把这份文件当成补丁，叠到一个空数组上。

我当时的疑问是：如果根配置是空的，第一行业务插件从哪来；如果一百多行没有先后，谁决定谁先可用；既然文件里看不到 `agent` 与 `session` 的挂载顺序，`agent.followup()` 怎么保证 session 已经就绪。

还有一个更具体的困惑来自改配置。我在 profile 的 `cordis.patch.yml` 里写 `- id: tool-web` 加一段 `config`，本意是只改一个超时字段，结果整段 config 被替换，原本的 `fetch` 开关丢了。文档说「patch 替换匹配条目」，但没说替换的粒度是 key 还是整行。

这篇按进程的真实顺序走一遍：从 `dsh` 解析参数，到 `agent.followup()` 把第一条消息推进 inbox。中间会拆开四层组装的覆盖关系、preset 的运行时挂载，以及插件卸载时哪些东西真的能退回去。

## 0. 结论

- profile 是一份空的 `cordis.yml` 加一叠 overlay，并不保存完整的插件清单。所有条目都来自 bundle 的 patch 层，后面几层可以按 id 改任何一行。
- 覆盖粒度按 key 计算。patch 写了 `config` 就整体替换 `config`，没写的 key 保持原样；`insert` 进 group 的条目必须自带 id，否则运行时的编辑找不到它。
- 插件的激活顺序由 `inject` 声明的服务依赖图决定，行序不参与。条目在依赖满足时激活，`loader.await()` 等到图稳定。
- agent preset 是运行时的子树，并非启动期的 profile 行。切换 preset 会建新一代 scope，旧代等 users 归零再 dispose；宿主机平面上的 registry 不动。
- 插件的可逆性来自两处：patch 文本可以反写，注册都是 `ctx.effect()`。包管理器副作用与模块缓存是不可逆的部分。
- 审计只保证「该在的行激活了」，不保证「这个 profile 挂全了」。required 列表里不存在的 id 会被静默忽略。

## 1. 从 argv 到 Loader 稳定

`dsh` 的 bin 解析完 `--profile`、`--patch`、`--dump-config` 之后，进入 `runProfile`（`apps/cli/src/profile-boot.ts:271`）。顺序是固定的：

1. `installProxyFromEnvironment`（`:276`）在任何插件挂载前执行。代理按 launcher 的环境快照解析，不读之后可能被插件改写的 `process.env`。
2. `composeProfile`（`:224`）确定 profile 目录。默认在 `$DSH_HOME/profiles/<name>`；不存在时从随附模板复制一份。随后往目录里写一个空的根 `cordis.yml`。
3. `createRuntimeResolution`（`:233`）在任何插件被 import 之前算出不可变的模块解析表。它同时看安装依赖图和有序的 bundle 依赖图，结果装进 Node 的 ESM/CJS resolver，不建 fallback 软链。
4. `readProfilePatches` 组装出这一代的 patch 列表，交给 `boot()`。
5. `boot()`（`packages/boot/app-boot/src/index.ts:1200`）新建 `Context`，挂一个独立的 diagnostics context 收集 warn/error，设置 `baseUrl`，`provide('dshHomePath', ...)`，然后 `ctx.plugin(Loader)`。
6. `prepare` 回调依次注入 `profileContext`、launch environment、`PluginPackages`（带 resolution）与 `cmdlineArgs`。这些值在任何 app 插件读之前就已经是不可变快照。
7. `mountRootInclude`（`:738`）把 patch 列表应用到根 include 的空条目数组上。
8. `await ctx.get('loader')?.await()`：等所有能激活的条目完成激活，或者确定地失败。
9. `auditStartupEntries(ctx, binName)`（`:1153`）检查 required 列表。
10. 回到 `runProfile`，`appReady.commit()`（`:344`）。

第 2 步写空根配置这件事，第一次看到会觉得多此一举：既然内容全是 patch 叠出来的，为什么还要落一个文件。原因是 Loader 需要一个可解析的入口。`cordis.yml` 里只有注释和 `[]`，`mountRootInclude` 才能把 patch 结果当成「这个根 include 的条目」应用上去。空文件也让「profile 里到底有什么」变成可 diff 的事实：bundle 升级、用户改动、`--patch` 临时覆盖，都能在文件层面分开看。

第 3 步的顺序同样有讲究。模块解析表必须在第一个插件被 import 之前算完，否则某个插件在自己的模块体里 `import` 一个不在安装树里的包，会先撞上 Node 的解析失败，而不是 boot 给出的诊断。解析表算完之后装进 resolver，之后所有 `import` 都按它走，进程运行期间不再变化。

第 8 步是这个系统最容易被误读的地方。`loader.await()` 等的是「没有 pending 的 fiber」，并不表示所有插件按顺序执行完毕。一个条目声明了 `inject: [agentPresets]`，在 `agentPresets` 出现之前它一直停在 PENDING，不报错也不执行。base 补丁文件的头注释把这件事写得很直白：行序不承载加载语义，激活由服务可用性驱动。

```ts
// 简化后的骨架，省略错误处理
const ctx = new Context()
const logs = new Context()          // 独立 context 收集诊断
await ctx.plugin(Loader)
await prepare?.(ctx)                // profileContext / cmdlineArgs / resolution
mountRootInclude(ctx, configPath, patches)
await ctx.get('loader')?.await()
auditStartupEntries(ctx, binName)
appReady.commit()
```

{{< fig "diagrams/01-boot-sequence.svg" "从进程启动到第一个 turn：launcher 先算解析表与 patch 层，Loader 按服务依赖激活，审计通过后才发布 appReady，最后由 surface 插件提交第一条消息。" >}}

启动失败分两档。optional 条目 import 失败、schema 校验失败、`!!js` 求值抛错、`apply()` 抛错，都只产生一条带标签的警告，进程继续；required 条目失败则拆卸整棵树并退出非零。required 列表是十个 id：`agent-loop`、`tool-executor-native`、`tool-executor-ptc`、`webserver`、`web-runtime`、`modules`、`connection`、`headless-runner`、`acp`、`sdk-jsonrpc-server`。

这里有个反直觉的细节：profile 里根本没有的 required id 不算失败，被显式 `disabled` 的 required 条目也不算。理由是同一个 required 列表服务五种 surface，web profile 不该因为没挂 `headless-runner` 而起不来。代价是：一个 profile 忘了挂自己的运行时，审计不会替你发现。

`installFailLoud`（`:879`）补的是另一类失败。它监听未处理的 rejection 与未捕获异常，往 stderr 写带标签的 `util.inspect` 诊断，固定超时等界面 release 钩子跑完，然后 `exit(1)`。这类错误不经过 Loader，也就没有 fiber 状态可读，只能靠进程级兜底。

### profile 从哪来

`dsh --profile web` 第一次跑时，profile 目录并不存在。`composeProfile` 会从随附模板复制一份：复制的是模板的 bundle 列表与空的 patch 文件，不复制运行数据。随附的 profile 名称（大小写不敏感）被保留，用户不能用一个内置名字建自己的 profile。

复制过程用 staging 目录加 rename，失败时恢复旧目录。这样中途断电不会留下半个 profile：要么旧目录在，要么新目录完整。这个原子性只覆盖初始化，不覆盖之后的 patch 编辑；后者由插件管理器的写锁与 HMR 监听兜底。

`.env` 的读取只发生一次，在启动期。调用目录的 `.env` 优先于 harness home 的 `.env`，两者都低于继承的进程环境。之后插件再怎么改环境变量，也不会重新读文件。想换一组凭据，得重启。

### snapshot 模式

`resolveConfigPath` 在 `DSH_SNAPSHOT=replay` 时把配置文件名从 `cordis.yml` 换成 `cordis.snapshot.yml`，其余情况原样解析。替换只认 basename，不看目录，所以一个 profile 可以同时保留运行配置与回放配置。

回放用的是录制好的 session，不是实时模型。这让「同一份配置、同一段输入、不同 commit」的比较成为可能，也让启动路径的回归能在没有 API key 的机器上跑。代价是快照配置必须跟着运行配置一起维护，两份文件漂移时，回放通过不代表线上能起。

移动 profile 包需要先卸载。profile 的 `package.json` 里记着依赖，路径变了之后 pnpm 的链接还指向旧位置，启动会在解析阶段失败。这不是可以靠 `mv` 加改配置绕过的事情。

## 2. 四层，与覆盖的方向

`readProfilePatches`（`packages/boot/app-boot/src/profile-context.ts:74`）把 patch 按下面的顺序拼成一个数组：

| 顺序 | 来源 | 谁能改 |
| --- | --- | --- |
| 1 | `dsh.profile.bundles` 里每个 bundle 的 `dsh.bundle.patch` | 组合包作者 |
| 2 | profile 自己的 `cordis.patch.yml` | 用户 / 插件管理器 |
| 3 | `$DSH_HOME/cordis.patch.yml` | 用户（跨 profile） |
| 4 | `--patch <file>`，按 argv 顺序 | 调用方 |
| 5 | `DSH_TELEMETRY_DISABLED` 生成的硬关闭行 | launcher |

后一层覆盖前一层，粒度是「同一个 id 的同一个 key」。第 4 层会替换配置文件这种说法不准确：它只是又一份 patch，叠在第 3 层之后。第 5 层永远最后，因为隐私开关按「宁可多关」设计：任何非空值都关闭，包括 `0` 和 `false`。组合里没有 telemetry 行时这一层不生成，自定义 profile 不必为了这个开关去挂遥测。

`web-app` 的 bundle 层自己又是一个有序列表：`cordis.patch.yml`、`presets/standard.patch.yml`、`presets/ptc.patch.yml`、`presets/minimal.patch.yml`、`presets/cordis.patch.yml`。前一个是宿主平面，后四个各插入一行 agent preset。preset 的定义文件在第 1 层内就已经排好队，运行时要挂哪个由 registry 的 `default` 决定。

{{< fig "diagrams/02-four-layers.svg" "四层组装与覆盖方向：bundle 层按声明顺序摊平，profile、home、--patch 依次叠加，最后是遥测开关；右侧标出同 id 同 key 的覆盖方向。" >}}

patch 的语义在 `vendor/include/src/patch.ts` 里，一共两个分支：

- 有 `insert`：把条目克隆后追加到根数组，或追加到 `id` 指定的 group 的 `config` 数组。指定了 `id` 但目标不是 group、或根本不存在，只 warn 并跳过。
- 没有 `insert`：必须有 `id`。找到目标条目后，把 patch 里出现的每个 key 逐个写上去。可选 `name` 是守卫，和目标的 `name` 不一致就 warn 并跳过，防止 id 复用后改错行。

`config` 只是众多 key 里的一个。所以写 `- id: sandbox-policy` 加一段 `config`，是把这一行的 `config` 整体换掉，不与原 config 深合并。headless 的补丁把 `mode` 和 `workspaceRoot` 又写了一遍，注释里说明的是它自己补全了字段，patch 引擎没有在帮忙合并。

```yaml
# 覆盖：只改 config 这个 key，整体替换
- id: sandbox-policy
  config:
    mode: workspace-write
    workspaceRoot: !!js "ctx.get('profileContext').cwd"

# 插入：追加到根数组或指定 group
- insert:
    - id: my-tool
      name: '@acme/dsh-my-tool'
      config:
        timeoutMs: 15000
```

`EntryOwner`（`:118`）把这个模型说全了：一个条目、或条目的一个 key，归属可能是原始文件、某一份 patch（最后一个写的赢）、或某一次 insert。插件管理器判断「这个开关是不是被更高优先级的层覆盖了」，读的就是这个归属，而不是再猜一遍顺序。

### 谁在写这些层

写 profile patch 的有三个入口：手写、`dsh plugin` CLI、Web 侧栏。三者最终都落到同一份 `cordis.patch.yml`，由同一把写锁串行化。手写的问题是容易写出重复 id 的覆盖项，最后一条赢，前面几条静默失效；管理器的问题是它会把你手写的项当成自己的状态去比较。

HMR 打开时，profile 目录被监听。外部编辑触发一次重组：`recomposeProfile(ctx, patches, binName, { requiredIds, running })` 移除新一代不再列出的行，等它们释放，发布新的 resolution，应用新一代，再启动更新后没有运行 fiber 的条目。失败时它可以把移除的行放回原位置重启，所以「改坏一处就整站起不来」不是必然的。

重组成功后发 `app-boot/config-reload` 事件。事件不带 diff，只表示「树已经稳定，且没有新增未激活条目」。监听方要自己重读 Loader 条目，不能假设只变了自己关心的那一行。

关掉 HMR 时，同样的编辑不会触发重组。插件管理器返回 `restart-required`，把原因写进结果。这时候磁盘上的配置已经变了，运行中的树还是旧的，两边不一致会一直持续到下一次启动。

`insert` 还有个强制约定：每个插入条目都要有 id。`ensureInsertIds`（`:37`）会给缺 id 的条目补随机 id，因为运行时的修改要按 id 路由回插入项。手写 YAML 时最好自己给稳定 id，否则 HMR 重载后同一个逻辑条目会换一个身份。

`!!js` 让这份 YAML 不只是数据。base 里 `plugin-manager` 的 `disabled` 写成 `!!js "!ctx.get('profileContext')"`，含义是「不在 profile 上下文中就关闭」；headless 的 `workspaceRoot` 直接读 `profileContext.cwd`。表达式只在启动时求值一次，求值失败按条目的 required 级别处理：optional 警告，required 终止。求值发生在 `prepare` 注入完上下文之后，所以表达式里能拿到 `profileContext`、`dshHomePath`、launch environment。

自己搭的时候，先给每一层写一句「这一层只负责什么」。bundle 层负责默认值与平台差异，profile 层负责用户选择，home 层负责跨 profile 偏好，`--patch` 只负责本次调用。把临时开关写进 profile，过一周就会忘记它为什么在那儿；把用户偏好写进 `--patch`，换个入口就丢。

## 3. 激活顺序与失败边界

根配置是空的，所以第一层 `insert` 决定全部内容。base 的一百多行彼此靠 `inject` 形成偏序。几个典型依赖：

- `agent-loop` 注入 `agents`、`sessions`、`tools`、`llm`，它必须等到这些服务出现。
- `tool-executor-native` 注入 `tools`，工具执行器在注册表可用后才挂上。
- `webserver` 注入 `webStartup`，端口绑定发生在 web 的启动值算出来之后。
- `agent-preset-registry` 注入 `loader`、`sessionProjections`、`agents`。它需要 loader 才能在运行时挂子树。

| 失败位置 | optional 行 | required 行 |
| --- | --- | --- |
| 模块 import | 警告，继续 | 拆卸整树，非零退出 |
| schema 校验 | 警告，继续 | 拆卸整树，非零退出 |
| `!!js` 求值 | 警告，继续 | 拆卸整树，非零退出 |
| `apply()` 抛错 | 警告，继续 | 拆卸整树，非零退出 |
| 注入服务缺失 | 停在 PENDING | 停在 PENDING，审计报错 |
| 端口绑定失败 | 端点不可用 | 拆卸整树，非零退出 |

Cordis 的 fiber 状态是 `PENDING → LOADING → ACTIVE`，失败进 `FAILED`，拆卸走 `UNLOADING → DISPOSED`（`vendor/cordis/src/fiber.ts`）。`inject` 未满足时停在 PENDING，不占线程也不报错。`loader.await()` 的返回值不告诉你「全都成功」，只告诉你「没有还在加载的了」；成功与否要另读每个条目的 fiber 状态，审计做的就是这件事。

不这样会出什么事：如果按行序同步执行，`agent-loop` 会在 `tools` 还没注册时构造，工具列表为空；而它在构造时抓的是引用还是快照，决定了后面补注册能不能生效。这类顺序 bug 在启动日志里通常只表现为「模型看不到某个工具」，很难定位。

另一个边界是端口。`webserver` 激活时立即 `listen`，`EADDRINUSE` 会让它初始化失败。它是 required，于是整个应用拆卸退出。这比「起来一半、页面 404」要好查，但也意味着同机跑第二个实例需要显式换端口，不能靠 fallback。

拆卸本身也有顺序。`ctx.fiber.dispose()` 按激活的逆序释放 fiber，每个 `ctx.effect()` 的清理函数在这一步跑。`webserver` 的 disposer 同时 `close()` 与 `closeAllConnections()`，因为只关 listener 会留下 keep-alive 连接，进程退不掉。`process-shutdown.ts` 里 SIGTERM 映射到 0、SIGINT 映射到 130，两个信号都先走 `dispose`。

### 冷启动的四个可观测点

排查启动问题时，按这四个点看，比读整份日志快：

1. diagnostics context 收到的 warn/error。optional 条目失败只在这里出现，主日志可能很干净。
2. Loader 的条目状态。pending 说明依赖没满足，failed 说明 `apply()` 或 import 出错。
3. `auditStartupEntries` 的结果。它把 required 的 inactive 条目变成一条 StartupError，附带启动组合快照。
4. `appReady` 是否 commit。没有 commit 就没有 surface，端口、stdio、升级路由都不存在。

这四点里，第 2 点最容易被跳过。日志里看到「所有插件已加载」并不等于所有条目 ACTIVE，只等于没有 pending。

自己搭的时候，把「启动成功」定义成三个条件：树里没有 pending、required 全 ACTIVE、`appReady` 已 commit。只看 `loader.await()` 返回，会把「某个 optional 插件失败」当成启动成功，后面在请求路径上才发现少了一个服务。

## 4. 第一个 turn 从哪里来

树稳定、`appReady` 发布之后，surface 插件才开始接活。headless 是最短的一条路径，适合拿来看清调用顺序（`packages/bundle/headless/src/index.ts`）：

```ts
const agent = config.sessionId === undefined
  ? (await agents.create({
    sessionId,
    meta: { cwd },
    agentOptions,
    setup,
  })).agent
  : await resolveAgent(ctx, agents, sessionId, agentOptions, setup, cwd)

await agent.whenIdle()
// ...
agent.followup(createUserMessage({
  content: [{ type: 'text', text: task }],
  source: { kind: 'user' },
}))
```

`agents.create` 是 registry 的工厂调用（`packages/core/agent/src/index.ts:410`）：它拿注册的 factory，`Reflect.apply(target.createAgent, ...)`，返回的 handle 让调用方精确拆掉这一个 agent。创建过程同时建 session，`setup` 回调在 agent 的 context 上执行，preset 就是在这里挂上去的。

`createAgent` 的选项里有两样东西决定了这个 agent 的行为：`agentOptions` 是 model、reasoning effort、max tokens 这类模型侧配置；`setup` 是在 agent context 上跑的安装函数。`session-controller` 的 `composeAgent` 就是典型的 setup：先 `installModelSelection(agentCtx)`，再 `presets.mount(agentCtx, resolvedId)`。

`followup` 的语义容易误解。`send(message, 'next-turn', true)` 把消息插到 inbox 的 `next-turn` 位置并请求唤醒（`packages/core/agent-loop/src/agent.ts`）。同一族的还有 `steer`（进 `next-step`，会唤醒）与 `inject`（进 `next-step`，不唤醒）。三个动词的差别是排队位置和是否叫醒 driver，并非同步异步。随后 driver 领走消息，写 `turn/start`，进入 step 循环。

```ts
// 三个入口共用 send，只差 target 与 wakeup
followup(input) { return this.send(input, 'next-turn', true) }
steer(input)    { return this.send(input, 'next-step', true) }
inject(input)   { return this.send(input, 'next-step', false) }
```

唤醒时还有一个细节：如果当前 phase 正在 abort，`send` 会把 target 从 `next-step` 改成 `next-turn`，避免把消息塞进一个已经被取消的 step。这个判断在 `send` 开头，依据是 `phase.kind !== 'idle' && phase.abort.signal.aborted`。

`whenIdle()` 在 headless 里被 await，意思是「上一个 turn 已经结束」。首次运行时它立即返回；`--session-id` 续跑时它等恢复的 turn 收尾。之后 headless 进入 `whenIdle` 循环，等 `subagents.waitForTreeWork` 清空，最后 `sessions.flush` 落盘。

### turn 开始之后

消息进 inbox 只是入队。driver 领走它时先写 `turn/start`，然后进入 step 循环：组装 prompt、请求模型、执行工具、把结果写回日志、判断是否还要下一步。每一步都以 session 事件的形式落盘，`turn/end` 是这一轮的收尾。

`followup` 与 `steer` 的差别在这里变得具体。`followup` 进的是下一轮的队首，当前 turn 跑完才轮到它；`steer` 进的是当前 step 之后的位置，模型下一次请求就能看到。两者都唤醒 driver，所以从调用方看都是「立刻返回、稍后生效」。

`inject` 不唤醒。它适合把上下文塞进一个已经在跑的 turn，或者等下一次自然唤醒时一起处理。如果 agent 正处于 idle，`inject` 的消息会一直躺在 inbox 里，直到有人调用 `followup` 或 `steer`。

工具执行期间也有边界。`cancel` 默认清空 inbox 并 abort 当前 phase；`runMaintenance` 要求 idle，期间不接受新消息。这些约束让「一个 agent 同时只有一个活动 phase」成立，session 日志的 seq 才能保持单线。

这也是为什么 `appReady.commit()` 的时机重要。Web 的 Gateway 在 `appReady` 存在时，只有启动成功后才注册 WebSocket 升级路由；SDK 的 server 在启动后才开始读 stdio；ACP 在启动后才应答 `initialize`。在 commit 之前到达的请求没有可投递的树，只能失败或排队。

{{< fig "diagrams/03-preset-rebuild.svg" "一次 preset 切换的重建范围：会话与日志不动，宿主机平面的 registry 不动，只有 agent 子树按代重建，旧代在 users 归零后释放。" >}}

## 5. preset 不是 persona

这两个词在文档里经常挨着出现，作用层级完全不同。

persona 是 system prompt 的一个 section。`dsh-system-prompt` 自己拥有全局 persona，作为它的 config 无条件注册。`@deepseek-ai/dsh-persona` 这一行之所以存在，是因为它只能 shadow 一个 scope：挂在 agent preset 里，它改这个 session 的 prefix/suffix；挂在全局，它会和 registry 自己的注册撞车并 fail loud。源码注释把动机写死了：一个 preset 不能挂 prompt registry 本身，没有这一行，preset 能换工具却换不了身份。

persona 的 config 只有四个字段：`prefix`、`suffix`、`complete`、`includeRuntimeContext`。`complete` 为 true 时压掉其余 section，`includeRuntimeContext` 为 false 时调 `ctx.systemPrompt.suppressRuntimeContext()`。它注册两个 section：`PERSONA_PREFIX_SECTION` 与 `PERSONA_SUFFIX_SECTION`。

| 维度 | agent preset | persona |
| --- | --- | --- |
| 挂载点 | `agentPresets.register()` 后的子树 | `systemPrompt` 的 section |
| 作用范围 | 一个 agent scope | 一个 prompt scope（可被 preset 收窄） |
| 能改什么 | 工具、指令、planning/compaction/delegation、身份 | 只改 prefix/suffix 与 runtime context |
| 可否全局挂 | 是（registry 在宿主平面） | 否，会与 system-prompt 的注册冲突 |
| 切换代价 | 重建 agent 子树 | 下次组装 prompt 时生效 |

agent preset 是一整棵子树。`@deepseek-ai/dsh-agent-preset` 只有三十行：它把 `{ id, name, description, order, plugins }` 注册进 `agentPresets`，并把 `plugins` 数组留给 registry 去挂。standard preset 的 `plugins` 从 persona 开始，接着是 agent-instructions、time-context、平台化的 shell 工具、fs 工具、skill、goal、planning group、compaction group、delegation group，最后是 ask-user、todo、web、present。

所以「切 preset」切的是这串 plugins，并不更换一段 prompt。宿主机平面与 agent 平面的分界，在 `web-app/cordis.patch.yml` 里用一排 `disabled: true` 画出来：`tool-bash`、`tool-fs`、`tool-jobs`、`skill-filesystem`、`tool-subagent`、`workflow-ptc`、`tool-todo`、`tool-web` 这些行在宿主层被关掉，改由 preset 自己挂。注释解释了为什么不直接删：base 是共享的，删掉的行会在某次重排后悄悄回来。

这条分界的判据可以概括成一句：服务是全进程的，就留在宿主平面；状态是 per-agent 的，才进 preset。job registry、skill registry、token meter、goal driver 都是前面的；工具行、指令注入、persona 是后面的。判错的症状通常是「服务在，但看不到」或者「每个 agent 一份，互相不通」。

### preset 的选择存在哪

选择不是一个内存字段。`AgentPresetRegistry` 的 `defaultId` 读 `config.selectedDefault.get() ?? config.default`，`selectedDefault` 是 volatile 的，重启就回到默认。落盘的「这个 session 用哪个 preset」写在 session 里，切换时追加一条 `agent-preset/selected` 事件。

读取走 `presetForSession(session)`，从 session projection 取已提交的值。所以恢复一个旧会话时，用的是它记录里的 preset，不是客户端此刻选中的那个。客户端想改，只能显式发一次切换，让它落一条新事件。

这也解释了 `ApiSessionPresetConflict` 为什么存在。复用一个空白 session 时，如果请求指定了 preset，而这个 session 还没有任何记录，冲突无法用「以请求为准」或「以记录为准」单方面解决。报错比猜一个更安全。

volatile 的 `selectedDefault` 服务于另一种场景：同一个 profile 的默认 preset 可以在运行期被设置页改掉，新会话用新默认，旧会话不受影响。它不落盘，所以下次启动又回到 `config.default`。

## 6. 切一次 preset，重建了什么

registry 的实现（`packages/preset/agent-preset-registry/src/index.ts`）值得逐段读：

- `register`（`:97`）拒绝空 id 与重复 id，把声明存进 definitions。
- `activate` 为每一代建一个 scope：`createScope(this.owner, key)`，再 `mountPreset(context, id, plugins)` 把子树挂进去。记录里有 `{ scope, key, mount, users, retired }`。
- `mount` 失败不清场，写 `record.broken` 并 warn。`diagnostic()`（`:151`）读时重审；如果 loader 还有 pending，先 `await loader.await()` 再看。挂载失败是终局，不重试。
- `collect`（`:164`）只在 `retired && users === 0` 时 dispose 旧 scope。
- `presetForSession`（`:89`）从 session projection 读已提交的 preset，不读内存里的当前选择。

一次切换的动作因此是：新代挂载 → 会话记录 `agent-preset/selected` → 旧代标记 retired → 等它上面的 agent 全部释放 → dispose。会话日志、projection store、宿主机服务都不重建；重建的是 agent 作用域里的工具注册、指令注入、persona section、planning/compaction/delegation 子树。

`users` 这个计数是整个回收机制的关键。一个 generation 被挂上去时 `users` 是使用它的 agent 数；agent 释放时减一。retired 之后只要还有人用，scope 就不能 dispose，否则会拆掉正在跑的会话。反过来，如果 `collect` 漏了，旧代的 effect 会一直挂着，`inspectCompositions` 就能看到泄漏的 module refs 与 services。

诊断分两种。`diagnostic()` 在读取时重审 rows；如果定义里某一行还在 pending，它先等 loader 稳定再看。`list()` 返回所有声明，包括失败的。`inspectCompositions()` 返回当前活跃的 module refs 与 leaked services，给开发者看「这一代到底挂了多少东西」。

### 切换失败怎么收场

新一代替换旧一代时，如果 mount 抛错，registry 记录 `record.broken` 并 warn，不会自动退回旧代。旧代此时已经 retired，等 users 归零就 dispose。也就是说，一次失败的选择会把会话留在一个没有 preset 的状态，后续请求要么报 `agent-preset/not-found`，要么按默认 preset 重新挂载。

更糟的情况是 mount 部分成功：一半工具挂上、一半失败。这种状态没有事务语义，`inspectCompositions` 能看到多出来的 refs，但不会有自动清理。运维上的做法是让会话重新选一次 preset，触发新一代；旧代在 users 归零后释放。

所以切换入口应该先做一次「能不能挂」的检查。`diagnostic()` 提供了这个能力：它在挂载前就能给出 rows 的状态，包括失败原因。把它放在 UI 的确认按钮后面，比让用户切完再看到报错要好。

切换时机也有约束。`session-controller` 的 `composeAgent`（`packages/api/session-controller/src/agent.ts:261`）在创建 agent 前解析 preset：没有 `agentPresets` 服务时只装 model selection；有则先 `installModelSelection` 再 `presets.mount`。复用已有 session 时，如果请求指定了 preset 但这个 session 没有 roster，会抛 `ApiSessionPresetConflict`；已经开始 turn 的 session 也不复用。恢复会话时走记录里的 preset，不由客户端当前选择覆盖。

这里最容易踩的坑是 realm 可见性。preset 的 scope 是 entry-local realm，它对 realm 外的兄弟行不可见。曾经出现过 `run_in_background` 回答「background jobs unavailable」：`tool-jobs` 在 preset 里，但 background-job registry 在宿主行上，两者不在同一个 realm。修法是让 registry 留在宿主平面，preset 只拥有 per-agent 的那一行。skill registry、token meter、goal 服务、subagent registry 与 backends 都按同一原则处理。

不这样会出什么事：如果把 registry 也塞进 preset，每个 session 会得到一份独立的 job 表，后台任务在切换 preset 时凭空消失；如果把 preset 做成启动期的一行，切换就得重启进程，Web 上「换一个 agent 角色」会变成一次断线。

自己搭的时候，先画出两张表：哪些服务必须全进程唯一，哪些状态必须跟 agent 走。把第一张表钉在宿主平面，第二张表放进 preset。然后给每个 preset 写一条「挂载失败会怎样」的说明，因为挂载失败是终局，不会自动回滚到上一个能用的 preset。

## 7. typert、extensions、plugin-manager、plugin-market 各在哪一层

这四个名字经常被并列提起，位置差别很大。

typert 是 Host 到 Client 的类型化 RPC 层。构建期 generator 从源码类型生成反射、schema 与 Remote 描述符；运行期 registry 保存产物；loader 集成把产物自动注册；protocol 包提供装饰器与 wire 描述符。`SessionController extends TypertRemoteService`，Gateway 消费同一份 `InvocationDescriptor`。它不提供 agent 能力，只保证「客户端调用的方法与参数，在编译期和运行期是同一份定义」。

这套东西解决的是手写桥接的漂移问题。以前加一个客户端方法，要改三处：服务实现、wire 类型、客户端封装。漏一处，编译能过，运行时报参数不匹配。typert 把这三处收敛到服务类的签名上，generator 从签名产出其余两处。代价是构建期多一步生成，以及描述符必须在两侧版本一致。

extensions 是运行时的自省与动态执行。`tool-cordis` 给 agent 两个只读的 API 发现工具；`cordis-host-runner` 提供 `ctx.dynamicCordisRunner` 与 `ctx.cordisInspect`，把 host 半源码在沙箱化的生命周期里求值；`cordis-client-runner` 是浏览器半；`ui-cordis` 是面板与卡片。它让 agent 能写一个插件并当场跑起来，但不负责持久化。

plugin-manager 是 profile 的唯一写者。`PluginManager`（`packages/boot/plugin-manager/src/index.ts:312`）同时服务 Web 侧栏、设置页与 `dsh plugin` CLI，三者共用 `operations.ts`。它改的是 profile 的 `cordis.patch.yml` 与 `package.json` 的 `dsh.profile.bundles`，然后触发重组。

plugin-market 只读。它拉取配置的插件索引，校验后给每个条目附上安装状态、兼容性与 install 参数；索引源默认为 `[]`，不配置就不发请求。安装动作仍然回到 `pluginManager.installBundle`。

按职责分开看：typert 负责调用过线，extensions 负责插件怎么写与怎么跑，plugin-manager 负责装什么与开什么，plugin-market 负责去哪找。四者不在同一层，也不共享状态；把它们画在一张图上时，唯一共用的东西是 `profileContext` 与 Cordis 的 `Context`。

| 组件 | 输入 | 输出 | 是否持久化 |
| --- | --- | --- | --- |
| typert | 服务类签名 | 反射、schema、Remote 描述符 | 构建产物 |
| extensions | 源码片段 | 沙箱里的 Cordis 生命周期 | 否 |
| plugin-manager | 包 spec、开关意图 | profile patch、bundle 列表 | 是 |
| plugin-market | 索引 URL | 带状态的条目列表 | 否 |

这张表也解释了为什么它们经常被同时提到却很少互相调用：输入输出根本没有交集。plugin-manager 装完一个包，typert 的描述符是那个包自己构建时生成的；extensions 跑起来的插件，持久化与否由它自己决定。

## 8. 从安装到卸载，哪些能退回去

安装的完整链路是：`inspect(spec)` 读目标（registry 包走 `pnpm view`，绝对路径读 `package.json`，git/tarball 只报形式与 host）→ `pnpm add` → 写 `package.json` 的有序 `dsh.profile.bundles` → 重组 → 激活。

磁盘写入失败时恢复 `package.json` 与 `pnpm-lock.yaml`；`pnpm-workspace.yaml` 的 `allowBuilds` 有意不恢复，因为构建脚本的批准是用户级决定。安装成功但激活失败，依赖与选择都可能保留：包已经装上了，下一轮还能再试。

恢复的边界值得写清楚。`package.json` 决定装了什么，`pnpm-lock.yaml` 决定装的是哪个版本，两者不一致会让下一次 install 得到不同结果，所以一起回滚。`allowBuilds` 记的是「允许这个包跑安装脚本」的批准，属于用户对供应链的决定；把它回滚等于替用户撤回一次授权。

安装失败还有一类是「装上了但不是 bundle」。管理器会在 `inspect` 阶段先判断目标有没有 `dsh.bundle`，没有就拒绝，避免把一个普通依赖写进 profile 的 bundle 列表。这类拒绝发生在 pnpm 之前，不留任何磁盘痕迹。

开关插件只改一行里的 `disabled`。管理器找 profile patch 里最后一条匹配覆盖项；没有就追加。匹配依据是条目 id 加覆盖项声明的模块名。如果 bundle 层已经给出目标状态，只含 `id` 与 `disabled` 的覆盖项会被移除，profile patch 只记录与组合包不同的设置。home 层与 `--patch` 层不参与这个比较，因为管理器不改它们。

卸载的顺序是反的：先从 bundle 列表取消选择，停掉并释放这些行，再 `pnpm remove`。有 HMR 时，模块会被逐出、映射会被移除；没有 HMR 时，已经跑起来的组合保留到重启，操作返回 `restart-required` 与原因（`no-hmr`、`package-moved`、HMR pending、`overridden`）。

| 阶段 | 可逆 | 逆操作 | 残留 |
| --- | --- | --- | --- |
| patch 覆盖项 | 是 | 删掉该项或改回原值 | 无 |
| 条目 `disabled` | 是 | 改回，或删掉覆盖项 | 无 |
| `dsh.profile.bundles` 选择 | 是 | 从数组移除 | 依赖仍在磁盘 |
| `pnpm add` | 是 | `pnpm remove` | store 缓存、构建批准 |
| 激活中的 fiber | 是 | `ctx.effect()` disposer | 模块缓存可能不释放 |
| 已写入会话的 preset 选择 | 否 | 只能切回 | 日志里留下 `agent-preset/selected` |

可逆的部分：patch 文本、profile manifest、Cordis 的 effect 注册。每个 `register()` 都返回 disposer，`ctx.effect()` 在 fiber 释放时跑清理，所以停一行不会留下半个服务。

不可逆或半可逆的部分：pnpm 的 store 与构建批准、已经跑起来的模块缓存、安装成功但激活失败的半状态。装新版必须先卸旧版，也是因为模块缓存无法原地替换。

### 版本不兼容时

包可以在自己的 `package.json` 里声明 `engines.dsh`，或者把 DSH 包列进 peer 依赖。解析时如果运行时不满足，管理器返回 `incompatible-version`，带上每个包的 name、version、runtimeVersion 与 requirements。

绕过的方式是版本豁免：在 profile 的 `compatibility.json` 里写一个精确的 `package-name@version` 到运行时版本列表的映射。豁免只影响选择与激活判断，不改依赖、不改 bundle 列表、不改 patch。写完之后，在线 profile 当场重组，启动型 profile 返回 `restart-required`。

豁免的风险是实打实的：包声明的约束被忽略，运行时不保证 API 还在。管理器把「要求豁免」和「批准安装」分成两步，就是为了让这个决定显式一次，而不是被一次 `pnpm add` 顺手带过。

{{< fig "diagrams/04-plugin-lifecycle.svg" "插件生命周期与卸载顺序：磁盘事实、profile 选择、运行时激活三层各自有独立状态；卸载按「取消选择 → 释放 → 删包」逆序执行。" >}}

自己搭的时候，把安装拆成「磁盘事实」与「profile 选择」两个字段分别存，不要用「装了没有」一个布尔概括。装上了但没启用、启用了但激活失败、选进了列表但平台不支持，是四种不同的状态，合并成一个布尔就再也分不开。

## 9. 一次组装的自检清单

改完 profile 或 bundle 之后，按这个顺序自查，比直接启动看结果可靠：

1. `cordis.patch.yml` 能被 YAML 解析，且不是空文件。空文件会让启动失败，禁用一层要写 `[]`。
2. 每个非 insert 的覆盖项都有 `id`；每个 insert 项都有稳定 `id`，不依赖 `ensureInsertIds` 的随机值。
3. 覆盖 `config` 的项把必须保留的字段写全了，尤其是 `mode`、`workspaceRoot`、`root` 这类有默认值的。
4. 新增的 group 用 `cordis:group`，子项放在 `config` 里；直接平铺会被当成根条目。
5. 新增的包在 `dependencies` 里，且能被 runtime resolution 解析到；树外路径用绝对路径或 `./` 相对路径。
6. 声明的 `inject` 在这个 profile 里真的有行提供；否则它会永远 pending，审计不会报错。
7. 如果是 required 行，确认它在每个要启动的 surface 里都存在，或者接受「缺失被忽略」。
8. 改过 bundle 列表后，确认顺序符合预期：后加载的 bundle 会覆盖先加载的同 id 行。
9. Web profile 里新增的 `dsh.client` 行会被 modules 扫进启动清单；漏了 browser 半，面板不会出现。
10. 跑一次 `dsh --profile <name> --dump-config` 之类能离线看组合的入口，确认最终条目数与预期一致。

这张清单里第 6 条最值得强调。`inject` 未满足是静默的，日志里没有错误，只有审计阶段才会以「inactive」形式出现；而审计只检查 required 列表，不检查你新加的行。

## 10. 设计取舍

空根配置加 overlay，对比「生成一份完整配置」。生成式的好处是打开就能读；overlay 的好处是升级时不用 diff 整棵树，随附 bundle 改了默认值，用户只保留自己改过的那几个 key。代价是启动必须有一个会报错的 reconciler，以及严格的 id 纪律。

按 key 覆盖，对比深合并。深合并对嵌套 config 友好，但「删除一个字段」没有表达方式，而且数组语义含糊。按 key 覆盖可预测，代价是替换 `config` 时要把继承字段重写一遍，写漏了就丢默认值。

surface overlay 选择 disable 而不是 delete。删除更干净，但 base 是共享的，某次重排后消失的行会悄悄回来。disable 留下可审计的痕迹，代价是宿主层有一排永远不激活的行，读配置时要习惯。

preset 在运行时挂载，对比启动期固定。运行时挂载换来按会话隔离与热切换，代价是 realm 可见性、代际回收、失败诊断这些复杂度。这套复杂度是必要的：Web 上多个会话可以跑不同 preset，而进程只有一个。

插件管理器改 profile 文本，对比维护一个安装数据库。文本可读、可 diff、可被用户手改；代价是外部编辑与内存状态可能不一致，所以 HMR 监听外部改动时不发 `plugin-manager/changed`，让调用方自己决定要不要重读。

required 列表只管「激活失败」，不管「缺失」。这个取舍让同一份列表服务五种 surface，代价是 profile 少挂运行时不会报错。如果重做一次，我倾向于给每个 profile 加一条自己的 required 断言，而不是扩大全局列表。

把组合逻辑放进 loader，而不放进启动脚本。启动脚本只做「读文件、算解析表、调 boot、提交 appReady」，剩下全交给 Cordis 的依赖图。这让 surface 之间的差异收敛到 patch 文本，代价是启动流程的阅读门槛变高：只看 `profile-boot.ts` 看不出会加载什么，必须把 patch 叠起来读。

profile patch 允许手写，而不强制走管理器。手写适合临时调试与版本控制里的团队共享，管理器适合终端用户。两者共用同一份文件，冲突只能靠写锁与「最后一条赢」规则处理。如果重做，我会给每个覆盖项加一个来源标记，让「这行是谁写的」不再需要猜。

## 11. 踩坑点

1. patch 的 id 打错只 warn。`applyPatches` 找不到目标时打一行日志继续，启动照常成功，行为却没变。
2. 替换 `config` 会丢继承字段。写 `sandbox-policy` 这类行时，原 config 里的 `mode`、`workspaceRoot` 不会自动保留。
3. 空的 `cordis.patch.yml` 会让启动失败。想禁用一层就写 `[]`，不要留空文件或纯注释。
4. `insert` 的条目没有稳定 id，运行时编辑就找不到它。`ensureInsertIds` 会补随机 id，但那个 id 不跨重载。
5. `--patch` 是可重复的单值参数，不能写成可变参数。写成 variadic 会把后面的内层参数一起吞掉。
6. 把 registry 挂进 preset，会遇到 realm 隔离。`tool-jobs` 在 preset 里、job registry 在宿主行上时，`run_in_background` 会报不可用。
7. 把 persona 挂到全局会 fail loud。`dsh-system-prompt` 已经注册了全局 persona，第二份注册是冲突，不是覆盖。
8. 关掉 HMR 后，安装与开关都可能只落到磁盘。看到 `restart-required` 就别假设已经生效。
9. required 列表不检查「缺失」。profile 少挂自己的运行时不会报错，只会表现为没有 surface 响应。
10. 审计发生在 `appReady` 之前。任何在 `prepare` 阶段就假设 Web 端口可用的插件，都会拿到未绑定的 server。
11. `loader.await()` 返回不代表全部成功。还要读 fiber 状态，或让 `auditStartupEntries` 替你读。
12. 卸载时先取消选择再停行。反过来会留下「装了但没选」的半状态，下一次安装会以 `already-installed` 拒绝。
13. 版本豁免写在 `compatibility.json`，不写进 `package.json`。写错位置不会生效，也不会有提示。
14. `selectedDefault` 是 volatile 的。改完默认 preset 后重启，选择会回到 `config.default`。

## 12. 源码索引

- 启动入口：`apps/cli/src/profile-boot.ts:224`（`composeProfile`）、`:271`（`runProfile`）、`:276`（代理）、`:295`（appReady）、`:309`（fail loud）、`:344`（commit）、`:352`（die 顺序）
- boot 主体：`packages/boot/app-boot/src/index.ts:1200`（`boot`）、`:738`（`mountRootInclude`）、`:1153`（`auditStartupEntries`）、`:879`（`installFailLoud`）、`:344`（`appReady`）
- patch 顺序：`packages/boot/app-boot/src/profile-context.ts:52`（遥测开关）、`:63`（bundle 层）、`:74`（`readProfilePatches`）
- patch 语义：`vendor/include/src/patch.ts:37`（`ensureInsertIds`）、`:47`（`applyPatches`）、`:85`（按 key 覆盖）、`:118`（`EntryOwner`）
- 组合包：`packages/bundle/base/cordis.patch.yml`、`packages/bundle/web-app/cordis.patch.yml:598`（`agent-preset-registry`）、`packages/bundle/web-app/presets/standard.patch.yml:5`（`preset-standard`）
- preset：`packages/preset/agent-preset/src/index.ts`、`packages/preset/agent-preset-registry/src/index.ts:54`、`:82`、`:89`、`:97`、`:126`、`:151`、`:164`、`:180`
- persona：`packages/preset/persona/src/index.ts:50`、`:64`、`:74`
- 会话侧组合：`packages/api/session-controller/src/agent.ts:261`（`composeAgent`）、`:270`（安装 model selection）、`:271`（mount preset）
- 第一个 turn：`packages/bundle/headless/src/index.ts:389`（`agents.create`）、`:420`（`followup`）、`packages/core/agent/src/index.ts:410`
- agent 入口：`packages/core/agent-loop/src/agent.ts`（`send`/`followup`/`steer`/`inject`/`cancel`/`runMaintenance`）
- 插件生命周期：`packages/boot/plugin-manager/src/index.ts:312`、`:713`、`:832`、`:850`、`:872`、`:1029`、`:1046`
- 相关篇目：[01 解剖](/posts/dsh-architecture-anatomy.html)、[04 工具注册表作用域](/posts/dsh-tool-registry-scope.html)、[06 turn/step 状态机](/posts/dsh-turn-step-state-machine.html)

## 最后

启动路径上没有一处「总装车间」。空根配置、五层 patch、按服务依赖激活、运行时挂 preset，每一步都只做一种转换，每一步的失败都停在局部。代价是读启动要同时看 YAML、resolver、fiber 状态和审计日志；收益是任何一层都能单独替换，而不用动另外三层。

系列导航：上一篇 [S2-7 交互面](/posts/dsh-interaction-surfaces.html)；下一篇 [S2-9 嵌入与协议](/posts/dsh-embedding-protocols.html)。
