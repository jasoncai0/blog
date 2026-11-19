---
title: "OpenDesign 生成面：怎么调生成模型，怎么保 prompt 质量"
date: 2026-11-19T19:05:00+08:00
summary: "一条命令行加一张 run 级令牌的调用链、95 个模型的派发与钳制、HyperFrames 的本地渲染，以及生成 prompt 的三道闸与五处缺口。"
tags: ["architecture", "agent"]
slug: "open-design-media"
series: "open-design"
series_order: 8
---


> 源码版本：`nexu-io/open-design` `0725880`（package.json 0.27.2）。本地 clone 后逐文件核对，行号都对这个 commit。仓库 14,308 个文件，`apps/daemon/src/server.ts` 一个文件 18,959 行。

本系列 9 篇 · [总览](/posts/open-design-architecture.html) · 上一篇 [与编码 Agent 的分岔](/posts/open-design-vs-coding-agent.html)

## 6. 怎么调用生成类 Agent

OD 调生成模型的方式，是把"调模型"降级成"跑一条命令"。

### 6.1 只给一条命令行

27 个 CLI 的共同能力只有 shell，`media-contract.ts` 开头就把这个理由写明了：

> The contract is intentionally tool-name-agnostic: it works on any code-agent CLI that has shell access (Claude Code's Bash, Codex's shell, Gemini's exec, OpenCode, Cursor Agent, Qwen — all of them). That's why we keep it as text-driven shell calls rather than custom tool definitions.

于是生成能力的接口是一行 shell，不是一个工具 schema：

```bash
"$OD_NODE_BIN" "$OD_BIN" media generate --project "$OD_PROJECT_ID" \
  --surface image --model vela/gpt-image-2 --aspect 1:1 \
  --output assets/stairs.png --prompt "<full prompt>"
```

OD 自己的 MCP server 有 22 个工具，里面没有生成工具，最接近的是 `start_run`（它只是再开一轮对话）。生成永远走 CLI 与 HTTP 这条车道。

{{< fig "diagrams/10-media-dispatch.svg" "媒体派发的五列结构。凭证不出 daemon，agent 拿到的是一张按 run 发的令牌；HyperFrames 的渲染也在 daemon 进程里，因为 agent 的 Bash 被 sandbox 包住，puppeteer 抓帧会卡住。" >}}

### 6.2 通行证：按 run 发的一次性令牌

agent 要访问 daemon，用的不是 `OD_API_TOKEN`，是一张为这一轮铸的令牌。

- 形态：`odtt_` 前缀加 32 字节随机数的 base64url，daemon 只存 sha256（`tool-tokens.ts:121-127`）。
- 有效期：`max(15 分钟, 本轮不活跃超时 + 15 分钟)`，agent 每有活动就续期（`server.ts:12015`、`tool-tokens.ts:237-262`）。
- 权限：endpoint 白名单 17 条、operation 白名单 16 条，逐条字符串匹配（`tool-tokens.ts:18-63`，校验在 `:226-232`）。
- 撤销：run 结束的 finalize 钩子里 `revokeToolToken('run_finalized')`（`server.ts:12068`），另有 `child_exit`、`sse_end`、`ttl_expired`、`manual` 四种原因。
- 隔离：`createAgentRuntimeEnv` 会把子进程环境里所有 `OD_API_TOKEN` 键删掉（`server.ts:1883-1885`），注释原话是 agent children 只拿 run 级能力，不拿那个大凭据。

为什么值得这么麻烦：agent 是外部进程，一段 prompt 注入就能让它执行任意 shell。给它大令牌等于把整个 `/api` 交出去；给它一张只覆盖本轮、只覆盖 17 条路径、15 分钟过期的令牌，最坏情况被限定在一次 run 的范围内。

### 6.3 派发：从 surface 到 provider

媒体目录 95 个模型条目，23 个 provider（18 个已接）：50 个图像、31 个视频、14 个音频（4 音乐 / 8 语音 / 2 音效），默认项标在 `vela/gpt-image-2`（`media/models.ts`）。`fal-ai/*` 这类带前缀的 id 直接直通，不进目录。

一次请求的校验顺序固定：项目不存在 404 → surface 不是 image/video/audio 就 400 → 没有 model 就 400 → 媒体策略拒绝 403（`media:generate` 被默认允许，但 `enabled: false` 或 `allowedSurfaces` 不含该面时拒绝）→ 建 `media_tasks` 行 → 派发。

路由永远回 202 加 taskId，代码里没有"同步返回字节"这条路。CLI 侧替 agent 扛掉前 25 秒：等到就当场把 `{"file":{...}}` 打给 stdout，等不到就把 taskId 交出去，agent 进 `media wait <taskId> --since <n>` 的 120 秒长轮询，退出码 0 完成、2 还在跑、5 失败、4 daemon 出错。`flux-pro-ultra` 通常 60–180 秒，`sora-2` 与 `veo-3-fal` 更久。

数值参数一律钳制，不报错：视频时长按 `[3,5,8,10,15,30]` 取最近档，音频按 `[5,10,15,30,60,120]`，画幅收敛到 `1:1 / 16:9 / 9:16 / 4:3 / 3:4`（`media/index.ts:270-300`）。未知模型 id 才直接抛错；`fal-ai/*` 与 `aihubmix-*` 这类带前缀的 id 在运行时现场合成定义。

provider 返回的资源 URL 还要过一遍 `assertAndFetchExternalAsset`：先 DNS 解析，拒掉回环、RFC1918、链路本地、CGNAT、组播与云元数据地址，`redirect` 设为 `error`，连接时把解析出的 IP 钉死（`connectionTest.ts:357-400`）。生成服务经常把结果放在第三方 CDN 上，这一步防的是"模型服务被诱导返回一个内网 URL"。

{{< fig "diagrams/11-media-sequence.svg" "一次图片生成的十一步时序，六条泳道。红色两步是骨架：令牌随 Authorization 头进 daemon，可见性却只在 daemon 的 queued 诊断行里。" >}}

### 6.4 两件"事后才落地"的事

媒体任务比 run 活得久，于是有两种反常时序必须显式处理。

第一种是迟到文件。run 早已写出终态，provider 那边才把字节送回来。`associateLateRunProducedFile` 负责把文件补挂到那个已经结束的 run 上，前提是 run 真的已经终态，还活着就不抢。

第二种是失败回写。这条链上曾经有一个真实的 bug：agent 在正文里道歉、`exit 0`，run 被判成 succeeded，界面上绿灯、产物栏空白，而 daemon 自己的日志 55 秒前就记了唯一交付物生成失败。修法是在派发侧留一条反向通道 `noteMediaTaskFailure(runId, {...})`，让任务失败能把 run 改判成失败。

启动期的对账也是同一类工作：停在 `queued` 与 `running` 的行一律改写成 `interrupted`，带 `code: "DAEMON_RESTART"` 与 `status: 5`（`media/tasks.ts:290-310`）；终态行保留一小时供前端补读，之后清掉（`task-store.ts:50`）。

失败到 `nextStep` 的映射是张固定表：429/503 与超时给 `retry-later`，内容安全拒绝给 `revise-request`，没配 key 给 `open-settings`，重启打断给 `retry-later`，策略拒绝给 `unsupported` 或 `switch-model`，磁盘写不进去这类归 `contact-support`。agent 不需要理解原因，只需要照表说话。

{{< fig "diagrams/12-media-task-states.svg" "媒体任务的五个状态。done 与 failed 都可能发生在 run 结案之后，所以成败是两条时间线对账出来的，不是 exit code 一个人说了算。" >}}

### 6.5 本地渲染路径：HyperFrames

有一条生成路径完全不联网：`hyperframes-html`。agent 写一份 HTML 加 GSAP 时间轴，daemon 用 Chromium 逐帧抓图，再用 FFmpeg 编码成 MP4。

这条路存在的直接原因是沙箱。Claude Code 的 Bash 在 macOS 上被 `sandbox-exec` 包住，puppeteer 起自己的 Chrome 会卡在抓帧，所以渲染必须在 daemon 进程里做。agent 只负责写 composition：`hyperframes.json`、`meta.json`、`index.html` 三个文件，用 `media scaffold` 生成骨架，目录必须落在 `.hyperframes-cache` 或 `motion-source` 下，不能是符号链接，不能预先存在。

可复现性靠四件事：固定的 `hyperframes` 0.8.1 与 `@ffmpeg-installer/ffmpeg` 版本；`-start_number 0 -framerate <fps>` 的帧序列；`libx264 / yuv420p / faststart` 的编码参数；渲染前注入的帧桥把 `<audio>` 换成隐藏 span，让音频时钟归 FFmpeg 管。输出先落在 `mkdtemp` 目录里的 `render.mp4`，读进内存后删掉，再走那条所有 provider 共用的写文件路径。

这条路没有 stub 回退，渲染失败就是任务失败，跟 `vela/*` 一样。

## 7. 生成 prompt 的质量靠什么

三道闸：给例子、把硬参数挪出 prompt 文本、失败只说一句。没有第四道。

### 7.1 第一道闸：语料加改写

`prompt-templates/` 里 106 个模板，48 张图、58 段视频，只覆盖四个模型：`gpt-image-2` 46 个、`seedream-5-pro` 2 个、`seedance-2.0` 40 个、`hyperframes-html` 18 个。每条都带来源与许可：awesome-seedance-2-prompts 36 条、awesome-gpt-image-2 36 条、HyperFrames 18 条、仓库自建 12 条；许可以 CC-BY-4.0 为主（72 条），Apache-2.0 31 条，MIT 1 条。

模板进 prompt 的方式不是填空。系统提示里的原话是：

> The user picked this template as inspiration. Treat it as a structural and stylistic reference: borrow composition, palette cues, lighting language, lens/motion direction, and the level of detail. Adapt the wording to the user's actual subject and brief — do NOT generate the template subject verbatim.

新建面板里模板正文是可编辑的文本框，用户改过的版本成为权威，最终那段 `--prompt` 由 agent 写。daemon 从不编译模板，也不替换占位符：`{argument name="..." default="..."}` 这种写法在仓库里没有任何解析器，它是上游语料库的约定，agent 只能从 `default=` 推断该怎么填。

加载校验只有六条：是对象、`id` 非空、`surface` 与所在目录一致、`title` 非空、`prompt` 至少 20 字符、`source` 里有 `repo` 与 `license`（`media/prompt-templates.ts:93-143`）。没有 schema 库，没有去重，没有 aspect 白名单，也没有许可白名单。

质量在这里靠的是语料的密度：36 条同类范例里能看出这个模型喜欢什么样的句式与信息密度，比任何"prompt 最佳实践"清单都具体。

### 7.2 第二道闸：硬参数走命令行

契约里最有用的一类规则，是把"不该写进 prompt 的东西"明确挪出去。

尺寸与档位属于用户。原话是：用户说的"2K"、"高质量"就是那个要求，要映射到 `--resolution` 或 `--quality`；把它复述进 prompt 文本传不到 provider。画幅有启发式：电影感与横屏 16:9、竖屏社媒 9:16、没有线索时 1:1；provider 侧还会再纠一次，火山引擎上非枚举值一律落到 16:9。

音色必须是 provider 的 voice_id，自然语言描述不被接受。音效 prompt 硬上限 450 字符，目标 180–320，因为超长会在 provider 校验阶段白白浪费一次生成。图生视频用 `--image` 传项目相对路径，越界路径直接拒。

还有两条防呆：不要 `Read` 生成的二进制回上下文（会把下一次请求撑爆），不要手工"编辑"生成出来的字节（重新生成，让用户挑版本）。

### 7.3 第三道闸：失败只说一句

每个失败都带 `error.nextStep`，九值封闭集合：`revise-request`、`switch-model`、`open-settings`、`sign-in`、`add-credit`、`retry-later`、`update-app`、`unsupported`、`contact-support`。

契约要求 agent 只按这个字段分支，不猜 code、status 或措辞。只有 `switch-model` 与 `retry-later` 允许自动重派一次，理由写在注释里：第二次调用改变不了结果，还可能再计一次费。

给用户的话是一句本地化文案，不出现模型名、provider 名、taskId、路径、stderr、退出码。重试只调时间不调 prompt：只对 429 与 503 重试，最多一次，延迟上限 60 秒。

### 7.4 缺口

四处明确的空缺，读代码能确认：

1. 没有 negative prompt 参数。任何 provider 调用里都不存在这个字段。
2. daemon 不做内容审核。所谓"提示词没通过审核"是 provider 返回后按状态码分类的结果。
3. 模板里的 aspect 只是建议。2:3 的模板在火山引擎上会被改成 16:9，代码里没有告警。
4. 产物没有自动 QA。`motion-compare` 会算 SSIM 并拼对比图，但它是量具不是门禁：注释原话是让 agent 自己看帧对，分数只说明先看哪个时间点。
5. 请求链路里没有 `seed` 参数。同一个 prompt 重跑会得到不同结果，所谓可复现只体现在"字节被 sha256 冻结、绑在 runId 与 taskId 上"，不体现在生成过程。

要补第四道闸，位置在这里：把 `motion-compare` 与人工抽查接成导出前的可选检查。

---

全书到这里结束。回到[总览](/posts/open-design-architecture.html)看代价表，或者从[执行面](/posts/open-design-runtime.html)重新读一遍判定链。
