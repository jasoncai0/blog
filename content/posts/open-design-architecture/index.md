---
title: "OpenDesign 架构解剖（总览）：不拥有 agent 的设计工作台"
date: 2026-11-18T22:22:00+08:00
summary: "OpenDesign 把 agent loop 外包给用户机器上的编码 CLI，自己只做装配。总览页给定位、三层进程、27 个包的分层，再按执行面、上下文面、产物面、画布与落点、画布设计稿、端到端、与编码 Agent 的分岔、生成面拆成八篇分册。"
tags: ["architecture", "agent"]
slug: "open-design-architecture"
series: "open-design"
series_order: 0
---

> 源码版本：`nexu-io/open-design` `0725880`（package.json 0.27.2）。本地 clone 后逐文件核对，行号都对这个 commit。仓库 14,308 个文件，`apps/daemon/src/server.ts` 一个文件 18,959 行。

## 0. 先给结论（按影响排序）

**P0 · agent loop 外置决定了其他所有分层**（§1、[§3.3](/posts/open-design-runtime.html)）。判断错的代价：你会在这个仓库里找模型路由、工具循环、上下文压缩，一个都找不到，然后按"这是个 agent 框架"的方式去扩展，落到错误的层。决定下一步调什么工具的代码不在这里，在 `claude -p` 或 `codex app-server` 的子进程里。

**P0 · adapter 是一份数据，不是一组类**（[§3.3](/posts/open-design-runtime.html)）。`RuntimeAgentDef` 是纯对象，27 个 CLI 共用同一套 detect / launch / invoke / stream 引擎。判断错的代价：加第 28 个 CLI 时去写子类，探测路径会和 spawn 路径分叉，出现"能检测到、起不来"。

**P0 · prompt 由 daemon 拼装，且存在两套并行实现**（[§3.4](/posts/open-design-context.html)）。判断错的代价见 #7568：deck 协议只落到 legacy 分支，两套测试全绿，修补的第二个 PR 体量接近第一个的两倍。

**P0 · 生成能力是"一条命令行 + 按 run 发的令牌"，不是工具定义**（[§6.1、§6.2](/posts/open-design-media.html)）。27 个 CLI 只共享 shell，所以 `od media generate` 是唯一入口，daemon 只为这一轮发一张 15 分钟的工具令牌。判断错的代价：你会去找图片工具的 schema，或者想给 provider 加一个 SDK 适配层，而仓库里这两样都不存在。

**P1 · 交付物有两条物理路径，由 streamFormat 一处分流**（[§3.6](/posts/open-design-deliverables.html)）。`plain` 走文本 artifact 协议，其余走文件系统。判断错的代价：给支持工具的 CLI 发"请输出 artifact 块"，产物会被当成普通消息丢掉。

**P1 · run 没有 `interrupted` 状态**（[§3.2](/posts/open-design-runtime.html)）。重启把在跑的 run 改写成 `canceled` 加 `errorCode: DAEMON_RESTARTED`。判断错的代价：前端按 `interrupted` 写的分支永远不触发。

**P1 · 协作层的同步单位是版本化快照**（[§3.8](/posts/open-design-deliverables.html)）。冲突靠发布顺序与版本身份裁定。判断错的代价：按协同编辑器的思路去改，会破坏 content-first / pointer-last 的发布顺序。

**P2 · 单数据根、单写者、无跨进程锁**（§2、§8）。两个 daemon 指向同一个 `OD_DATA_DIR` 会同时写 SQLite。判断错的代价：想在本机开两个实例做对比测试，数据互相踩。

**P2 · 生成 prompt 的质量来自语料与重写，不来自编译器**（[§7.1](/posts/open-design-media.html)）。106 个模板只是"结构与风格参考"，daemon 从不解析占位符、不拼最终 prompt，最终那段文字由 agent 写。判断错的代价：去 `prompt-templates/` 找模板引擎，或者以为模板里的 aspect 会被严格遵守（Volcengine 上 2:3 会被改成 16:9）。

## 1. 定位：三个不变量

README 自称 "The 1st Collaborative AI Design Agent Workspace"。真正决定代码形状的是 `docs/spec.md` 里那三条下注（该文档标着 Archived Draft，下注本身仍然有效）：

1. 不拥有 agent loop，交给用户已装好的编码 CLI。
2. 不拥有模型，密钥和账号都在用户机器上。
3. 不拥有内容目录的所有权，`skills/` 是可以用文件夹替换的。

同一篇 spec 还有四条 non-goals。其中 "We do not ship a desktop app. No Electron" 在 v0.27 被推翻了：`apps/desktop` 和 `apps/packaged` 都在仓库里，PDF 导出用的就是 Electron 的 `webContents.printToPDF`。另外三条守住了，分别是"不做模型路由""不重写 agent loop""单机单用户"。

于是整个产品可以写成一条等式：

```
产品 = 内容注册表（skills / templates / design-systems / plugins）
     + prompt 编译器
     + 交付物物化器
     + 外壳（web / Electron / od CLI / MCP）
```

agent 的推理过程全部落在等式右边之外。这条等式解释了目录形状：`apps/web` 只有一个 App Router 页面、路由手写在 `apps/web/src/router.ts`；`apps/daemon` 里塞着 18,959 行的 `server.ts` 和 47 个路由文件。前端薄、后端厚，因为后端要替一个"看不见内部"的子进程承担所有确定性职责。

## 2. 总览：三层进程，一个数据根

{{< fig "diagrams/01-topology.svg" "三层进程与单一数据根。CLI 子进程绕过 daemon 直接写项目文件，daemon 只通过文件监听感知变化。" >}}

第一层是渲染层。Next.js 用 `output: 'export'` 打静态产物，`apps/web/app/[[...slug]]/page.tsx` 是唯一的业务页面（另一个 `page.tsx` 是桌面宠物），`generateStaticParams()` 返回 `[{ slug: [] }]`，所以导出只有一个 `index.html`。真正的路由是手写的 521 行 `parseRoute` / `buildPath`，架在 `useSyncExternalStore` 与 `pushState` 上。桌面端加载的是 `od://app/`，由 `protocol.handle` 转发到真实的 web 源。

第二层是 daemon。Express，默认 `127.0.0.1:7456`，拥有 `/api/*`、SSE、SQLite、项目目录、内容注册表，以及所有子进程的生死。

第三层是执行面。每个 run 一个 CLI 子进程。它不通过 daemon 写文件，直接往项目目录里落盘。daemon 对它的写入只有一种感知方式：chokidar 看到改动，发一个 `file-changed` 事件。

daemon 启动时只解析一次数据根（`apps/daemon/src/server.ts:1432`）：

```ts
RUNTIME_DATA_DIR = resolveDataDir(process.env.OD_DATA_DIR, PROJECT_ROOT, {
  requireExplicit: SANDBOX_MODE_ENABLED,
})
```

`projects/`、`artifacts/`、`runs/`、`app.sqlite`、日志、MCP 配置、插件状态、agent 的 home 全部从这里派生。唯一例外是"导入文件夹"项目：文件留在用户选的目录，数据库里只记 `metadata.baseDir`。

安全边界有三条，都在代码里。第一条，`DEFAULT_DAEMON_BIND_HOST = '127.0.0.1'`（`daemon-startup.ts:31`）；非 loopback 且没有 `OD_API_TOKEN` 时直接抛 `OD_BIND_HOST=${host} requires OD_API_TOKEN to be set.`（`server.ts:3255`）。第二条，带 `Origin` 头的请求要过白名单，`Origin: null` 只放行 GET，端口还没确定时 fail-closed 到 403 `Server initializing`。第三条，导入文件夹需要主进程用 `randomBytes(32)` 的密钥签一个 HMAC，覆盖 `baseDir`、nonce 和过期时间，放在 `X-OD-Desktop-Import-Token` 头里（`apps/desktop/src/main/runtime.ts:267-277`）。注释写了原因：旧流程让渲染进程回传路径，"a compromised renderer could substitute an arbitrary baseDir at the second step"。

还有一条组织约束值得抄：同一个能力必须同时出现在 web UI 和 `od` 子命令里，且两边打同一组 `/api/*`（`AGENTS.md`，UI/CLI dual-track）。`packages/contracts` 只放 HTTP 与 SSE 的 DTO，`apps/web` 不许 import `apps/daemon/src`。薄契约挡住了前后端各自长出自己的方言。

进程图回答谁在跑。仓库里 27 个 workspace 包回答另一个问题：谁能 import 谁。

{{< fig "diagrams/02-module-layers.svg" "27 个包分五层，import 只能自上而下。红色那条是被禁止的边：web 拿不到 daemon 的内部实现，只能对着 contracts 的 DTO 编程。" >}}

压在中间的是 `packages/contracts`。它和 `platform`、`sidecar`、`sidecar-proto` 一起被两端同时依赖，是这 4 个包里唯一只做类型与纯函数的那个：HTTP 与 SSE 的 DTO，加上两端共用的解析函数，不碰 Node fs、Express 和浏览器 API。`apps/web` 的 6 条内部依赖里没有 `apps/daemon`，这条缺失由 `AGENTS.md` 写死，没有构建期检查，只在 review 时守。`apps/packaged` 是唯一同时依赖 web 与 daemon 的包，它负责把两个 sidecar 拉起来并注册回 daemon（`sidecars.ts:905-920`）。`tools/*` 和 `shells/*` 在图的底部：不被任何 app 反向依赖，只由 `pnpm` 脚本调用；`shells/terminal` 里没有 TypeScript 运行时，只有 sh/ps1 启动器与 JSON Schema 契约。

## 分册索引

全书按"面"拆成八篇。编号沿用同一套，所以总览停在第 2 节、正文从第 3 节开始，代价与结论留在这一页。

| 篇 | 内容 | 章节 | 图 |
| --- | --- | --- | --- |
| [执行面](/posts/open-design-runtime.html) | daemon 注册顺序、run 生命周期、adapter 数据规格 | §3.1–3.3 | 时序图、五状态图 |
| [上下文面](/posts/open-design-context.html) | prompt 编译器、四类内容平面 | §3.4–3.5 | prompt 栈、内容平面 |
| [产物面](/posts/open-design-deliverables.html) | 三条交付路径、持久化、外壳与协作 | §3.6–3.8 | 交付路径 |
| [画布与落点](/posts/open-design-canvas.html) | 没有画布引擎的证据、产物落点链路、导入的画布 | §3.9–3.12 | 落点时序、三层归属 |
| [画布设计稿](/posts/open-design-canvas-design.html) | 空间模型、落点投影、视口状态三处改造，附验收 | §3.13–3.18 | 功能架构、落点时序、节点状态 |
| [端到端](/posts/open-design-walkthrough.html) | 一句话到一份 deck 的九步链路 | §4 | 十二步泳道、盘面结构 |
| [与编码 Agent 的分岔](/posts/open-design-vs-coding-agent.html) | 完成判据、知识、评审、模态 | §5 | 对照表 |
| [生成面](/posts/open-design-media.html) | 一条命令行加 run 级令牌、prompt 质量三道闸 | §6–7 | 派发结构、十一步时序、任务状态 |

读法：想知道 OD 为什么这样分层，读总览和执行面；想给自家 agent 接生成模型，直接跳到生成面；想判断这套架构值不值得抄，看端到端那一篇和这一页末尾的代价表。

## 8. 代价

| 代价 | 位置 | 后果 |
| --- | --- | --- |
| 单文件体量 | `server.ts` 18,959 行 / 846KB，`db.ts` 6,007 行 | review 粒度失效，改动靠 grep 定位 |
| 两套 prompt 实现 | `prompts/` 与 `contracts/src/prompts/` 五对镜像 | #7568 已证明会漏，且漏得无声 |
| 没有产物回收 | 无 artifact GC，文件版本无保留期，live 快照只增不减 | 长期使用的磁盘只涨 |
| 没有跨进程数据根锁 | `OD_DATA_DIR` 只做可写校验 | 两个 daemon 指同一目录会同时写 SQLite |
| SSE 背压被丢弃 | `server.ts:3149` 忽略 `res.write` 返回值 | 慢客户端下内存增长，而另一条 `message-stream.ts:21` 会 `await drain` |
| prompt 不裁剪 | 只在组装后按字节卡 | 设计系统一多就直接 `AGENT_PROMPT_TOO_LARGE` |
| 检测无持久缓存 | 只有进程内 5 秒的版本缓存 | 每次 `/api/agents` 重跑五路探测 |

前两条是结构性的，后五条是局部可修的。如果要给这个仓库提第一个 PR，我会挑背压那一条：两处实现对同一件事有两种处理，这种不一致迟早会咬人。

## 9. 如果要抄，抄这五条

1. **把"多运行时"做成数据规格**。`RuntimeAgentDef` 加一个通用引擎，27 个 CLI 的成本是 27 个 def 文件，合计 3,327 行。
2. **先建流再启动**。`routes/runs.ts:2867` 与 `:2900` 的先后顺序，是这套 run 生命周期里最便宜也最容易做错的一处正确性。
3. **执行档只在一处映射**。`execution-profile.ts` 全文件 7 行，把"产物是文件还是文本块"这个判断收在一个函数里，所有调用方共享同一份答案。
4. **内容注册表用目录加 frontmatter**。没有数据库表、没有 watcher、没有安装步骤，用户目录遮蔽内置目录的规则只有一行。
5. **MCP 工具层只做 HTTP 转发**。22 个工具全是 `fetch`，MCP 侧没有状态，也就没有第二套真相。

## 10. 参考

- 仓库：`nexu-io/open-design`，commit `0725880`，Apache-2.0。
- 架构与规范：`docs/architecture.md`、`docs/agent-adapters.md`、`docs/skills-protocol.md`、`docs/prompt-composition.md`、`docs/critique-theater.md`、`docs/atoms.md`、`docs/spec.md`（Archived Draft）。
- 关键源码：`apps/daemon/src/server.ts`、`apps/daemon/src/runtimes/{registry,detection,types}.ts`、`apps/daemon/src/runtimes/defs/*.ts`、`apps/daemon/src/prompts/system.ts`、`apps/daemon/src/prompts/core-slim.ts`、`apps/daemon/src/skills.ts`、`apps/daemon/src/design-systems/index.ts`、`apps/daemon/src/artifacts/plain-stream.ts`、`apps/daemon/src/live-artifacts/store.ts`、`packages/contracts/src/execution-profile.ts`、`packages/contracts/src/sse/chat.ts`。
