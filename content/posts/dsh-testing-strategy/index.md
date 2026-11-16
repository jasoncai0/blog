---
title: "怎么测一个 Agent Harness：真组合、假模型与按事件断言"
date: "2026-11-16T21:00:00+08:00"
summary: "从 Loader 真组合、可脚本化的假模型到按 seq 断言事件：测试面怎么分层，取消竞态用什么当同步点，以及交付前仍然测不到的那几块。"
description: "DeepSeek Harness 的测试分层笔记：REAL composition 的 fixtures、MockAdapter 与 llm-replay 的分工、确定性时钟的两种钉法、以 workspace.expected 为准的世界断言。"
tags: ["codegen","harness","architecture"]
slug: "dsh-testing-strategy"
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit `fcc0ad46`，工作区 `harness-research/deepseek-harness/`）。行号按该 commit 的工作区读取，没有凭记忆补。测试政策引文来自 `docs/testing.md`、`packages/AGENTS.md`、`snapshots/AGENTS.md` 三份原文；取消场景的证据来自已提交的 `snapshots/acp/cancel-tool-calls/`（`session.v4.jsonl` 与 `workspace.expected/`）。本文没有执行 `pnpm run test`，`1838` 个 `*.spec.ts`、`89` 个 `*.e2e.ts`、`361` 个 `tests/` 目录由 `find` 统计；凡称「实跑」的结论，都有 fixture 里的字节做依据。

harness 的测试难点不在代码量。三条链路（模型流、工具执行、会话落盘）各自异步，UI 只是它们的一个投影。这三条链路的切分见[《工具执行流水线：九段与三闸》](/posts/dsh-tool-pipeline.html)与[《一轮到底怎么结束》](/posts/dsh-turn-step-state-machine.html)。你写一个断言「点了按钮之后出现红色错误条」，这条断言同时依赖模型输出、工具返回值、投影渲染三层；任何一层改动都会让它红，而它红的理由通常跟你要测的东西无关。

所以这套仓库把断言往两个极端推：要么贴着内部事件，精确到 `seq`；要么贴着外部世界，精确到文件字节。中间那层（UI 文案、日志字符串、内部字段）能不断言就不断言。

本文按这个顺序走一遍：一个插件的行为为什么只能在组合里验、真组合的最小闭环长什么样、假模型分几档、时钟能钉到什么程度、事件怎么断言、取消竞态用什么当同步点，最后列出这套办法仍然覆盖不到的东西。

{{< fig "diagrams/01-assertion-ladder.svg" "四个断言层次：内部字段最敏感也最易误报，磁盘世界最迟钝但最难骗过" >}}

## 1. 一个插件的行为只存在于组合里

`packages/AGENTS.md:7` 的第一条就是硬要求：

> **Product-visible plugins require a non-unit REAL-composition test.** Hand-built `ctx.plugin(...)` suites are insufficient.

理由在包的元数据里。一个 Cordis 插件的对外行为有两半：一半写在 `apply()` 里，另一半写在模块的导出形状里（默认导出还是命名导出、`inject` 列表、`Config` schema）。`ctx.plugin(SomePlugin)` 直接拿构造函数，绕过了 Loader 对模块命名空间的解析。仓库有一份 postmortem 专门记这个坑：`docs/postmortem/0001-acp-default-export-drops-inject.md`，默认导出与函数插件的命名导出混用，Loader 丢掉整个命名空间，`inject` 没生效，服务要到运行时才炸。

手搭的 ctx 看不到这一层，因为手搭的代码 `import` 到的就是正确的那个东西。同理还有可选服务的读取方式，`packages/AGENTS.md` 要求 `ctx.get(name)` 而不是 `ctx.<name>`，理由是属性代理对拓扑敏感，只有真 Loader 才复现那个拓扑。

数量上，这套仓库是 1838 个 `*.spec.ts` 对 89 个 `*.e2e.ts`。比例说明绝大多数行为还是靠进程内 spec 覆盖，组合测试只留给装载路径、真实入口、以及跨进程的协议。

## 2. 真组合的最小闭环

最小的组合测试由三个文件加一个 spec 构成，`packages/session/session-projection/tests/` 下就有现成的。配置只有三行：

```yaml
- name: '@deepseek-ai/dsh-session'
- name: '@deepseek-ai/dsh-session-projection'
- name: './legacy-plugin.ts'
```

前两行是包名，由 Loader 的解析清单落到 workspace；第三行是相对路径，测一个还没进包的插件。这份 yml 本身就是断言的一部分：它能让 Loader 报错，就说明解析链有问题。

驱动是一个独立进程，`fixtures/driver.ts`：

```ts
const configPath = process.argv[2]
if (configPath === undefined) throw new Error('projection compatibility fixture requires a config path')

const ctx = await boot('projection-compatibility', configPath)
const warnings: string[] = []
ctx.logger.exporter({
  levels: { default: 2 },
  export: ({ type, args }) => { if (type === 'warn') warnings.push(String(args[0])) },
})
try {
  const session = ctx.sessions.create()
  const legacy = ctx.get('legacyProjectionFixture')
  if (legacy === undefined) throw new Error('legacy projection fixture did not activate')
  session.append('turn/start', { turn: 1 })
  const first = legacy.read(session)
  session.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
  const second = legacy.read(session)
  const values = ctx.sessionProjections.snapshot(session).values
  await legacy.release()
  await legacy.release()
  const released = legacy.read(session) ?? null
  console.log(JSON.stringify({ first, second, values, released, warnings }))
} finally {
  await ctx.fiber.dispose()
}
```

这段 fixture 里塞了四个测试意图。`boot()` 走真 Loader，所以 yml 里 `inject` 缺失会在这里失败。`ctx.get('legacyProjectionFixture')` 是可选服务读取，拿不到就抛，不静默降级。`release()` 连调两次，验幂等释放。最后把结果打成一行 JSON，交给父进程断言。

父进程这一侧只有十几行（`tests/loader-compatibility.e2e.ts:6-22`）：

```ts
const { stdout } = await runLoaderSmoke({
  label: 'legacy projection registration',
  tempDirPrefix: 'dsh-projection-compatibility-',
  binScript: driver,
  libBinScript: driver,
  configPath: fileURLToPath(new URL('./fixtures/cordis.yml', import.meta.url)),
  tsconfigPath: fileURLToPath(new URL('../../../../tsconfig.json', import.meta.url)),
})
expect(stdout.trim()).toBe(JSON.stringify({
  first: 1,
  second: 2,
  values: { 'fixture/legacy-projection': 2 },
  released: null,
  warnings: ['sessionProjections.stateOf(session, "fixture/legacy-projection") is deprecated; read handle.stateOf(session) from an awaited registration, or use the owning domain service'],
}))
```

断言用 `toBe` 而不是 `toContain`，比较的是完整对象。多一个字段、少一个字段、warnings 数组顺序变了，都算失败。其中那条弃用警告的原文被钉在断言里，所以改文案会红。这是有意的：这条警告是给插件作者看的对外承诺，值得钉住。

`runLoaderSmoke` 做的隔离值得单独看（`packages/test-support/loader-smoke/src/index.ts:213-255`）：`mkdtemp` 造一个临时 cwd，`DSH_HOME` 指到 `<cwd>/.dsh`，`DSH_AGENTS_HOME` 指到 `<cwd>/.agents`，`input: ''` 立刻关 stdin，`timeout: 30_000` 配 `killSignal: 'SIGKILL'`，`reject: false` 让超时、非零退出、spawn 失败各自出现在结果字段里。失败时把两个流都塞进错误消息。`inspect(cwd)` 是给「验世界」留的钩子。

这里还有一个 src/lib 双模式（`resolveExampleLaunch`）。`DSH_EXAMPLE_MODE=lib` 时跑构建产物、用真实 `exports` 解析裸包名；不设或设成空串时跑源码、用 tsconfig 的 `paths` 映射、经 tsx 加载；填别的值直接抛，拼错不会被静默回退成源码。`docs/testing.md:41` 把后者叫 "real entry path"，因为它能抓到 tsx 掩盖的问题：模块解析差异、兜不住的装载失败、settle 竞态。

| 装载方式 | 抓到什么 | 抓不到什么 | 成本 |
|---|---|---|---|
| `ctx.plugin(X)` 手搭 | 服务内部逻辑、纯函数 | 模块导出形状、`inject` 拓扑、Loader 报错 | 毫秒 |
| Loader + 源码（tsx） | 导出形状、`inject`、yml 解析 | `exports` 映射、构建期裁剪 | 起进程，秒级 |
| Loader + 构建产物 | 上面全部，加上发布形态 | 无（最接近用户） | 需要先构建 |

自己搭的时候，最容易省掉的是第三行。省掉之后所有组合测试都在跑源码，发布产物里 `exports` 写错、`bin` 指向不存在的文件，CI 全绿也照样出事。

{{< fig "diagrams/02-real-composition.svg" "组合测试的三段式：test-only yml 经 Loader 进入子进程，stdout 与工作区是两个独立出口" >}}

## 3. 假模型分四档

假模型的位置决定了测试的保真度。仓库里同时存在四种，各管一段。

```text
进程内 adapter        llm-replay            llm-mock-server        真 API
MockAdapter     →     录制会话回放     →     HTTP/SSE 终点     →    test:e2e
loop/session 真        loop/session 真       adapter 解析真          全真
```

第一档是进程内的 `MockAdapter`（`packages/acp/acp/tests/harness.ts:32`），构造签名就三个参数：

```ts
class MockAdapter extends LlmAdapter {
  constructor(
    private readonly script: (StreamChunk[] | 'hang')[],
    private readonly imageCapable: boolean,
    private readonly provider = 'mock',
  ) { ... }
```

`stream()` 每次调用把入参推进 `this.requests`，再从脚本里 `shift()` 一条。脚本用完抛 `'MockAdapter: script exhausted'`。这行错误消息本身就是一条断言辅助：测试跑挂了，它告诉你模型被调用的次数比预期多。

第二档是 `@deepseek-ai/dsh-llm-replay`，它挂在同一个 adapter 边界，脚本来源换成录制好的 `session.vN.jsonl`。对 loop 来说没有区别，对维护者区别很大：fixture 是资产，会跟着会话格式版本走，格式升级时要么迁移要么重录。

第三档是 `@deepseek-ai/dsh-llm-mock-server`，一个讲 Messages 协议的 HTTP/SSE 端点，可以按脚本返回 reset、stall、截断的 chunk、429、500。这一档假的是网络终点，adapter 的解析和重试逻辑是真的。想测「流读到一半连接断了」这种情形，只有这一档能造。

第四档是 `test:e2e`，真 provider，要 key，没有 key 时自行跳过。

选哪档的判断标准写在 `docs/testing.md:27`：

> Mock only the expensive or non-deterministic boundary (LLM adapter, network, clock); keep everything downstream real.

`makeBridgeHarness()`（`packages/acp/acp/tests/harness.ts:221` 起）是这条规则最好的样本。它组装的东西包括 `mountAgentLoopTestDependencies`、带 `compression: 'none'` 的 `JsonlSessionPersistence`、`TokenMeter`、`MemoryAttachmentStore`、`AgentLoop`，然后用一对 `TransformStream` 加 `ndJsonStream` 接到真的 ACP SDK 客户端上。整条链里唯一被替换的是 `LlmAdapter`。

`compression: 'none'` 这个参数值得学。它让落盘内容就是可读 JSONL，spec 失败时可以直接 `cat` 那个文件看事件序列，不用先解压。

{{< fig "diagrams/03-fake-model-layers.svg" "四种假模型的位置：越靠右保真度越高，越靠左越便宜；选档标准是「只假昂贵或非确定的边界」" >}}

## 4. 一个 step 的假模型脚本长什么样

同一个文件里的三个辅助函数是脚本语法的全部（`harness.ts:153-183`）：

```ts
function textResponse(text: string): StreamChunk[] {
  const chunks: StreamChunk[] = [{ type: 'block-start', index: 0, blockType: 'text' }]
  for (const char of text) chunks.push({ type: 'text-delta', index: 0, text: char })
  chunks.push({ type: 'block-end', index: 0, block: { type: 'text', text } })
  chunks.push({ type: 'usage', usage: { inputTokens: 10, outputTokens: 10 } })
  chunks.push({ type: 'finish', reason: { kind: 'stop' } })
  return chunks
}

function errorResponse(text: string): StreamChunk[] {
  const chunks = textResponse(text)
  chunks[chunks.length - 1] = {
    type: 'finish',
    reason: { kind: 'error', error: { code: 'PROVIDER_ERROR', message: 'mock provider failure' } },
  }
  return chunks
}
```

`textResponse` 按字符切片，不一次性给整段。这个细节有实际效果：`step()` 里是 `for await (const chunk of stream) { signal.throwIfAborted(); live.push(chunk) }`（`packages/core/agent-loop/src/agent.ts:470-478`），一次给整块文本就绕过了逐 chunk 累积和 revision 递增那条路径，也测不到「中途取消但已有文本要 finalize」。

`'hang'` 是取消测试的主力。它先吐 `block-start` 和一段 `'partial'` 文本，然后一直等到 signal 被 abort：

```ts
if (entry === 'hang') {
  controller.enqueue({ type: 'block-start', index: 0, blockType: 'text' })
  controller.enqueue({ type: 'text-delta', index: 0, text: 'partial' })
  await new Promise<void>((resolve) => {
    options.signal.addEventListener('abort', () => resolve(), { once: true })
  })
  return
}
```

这样取消之后能断言的具体事实是「已经交付的文本被结算成了一条 assistant 消息」，而不是「什么都没写」。后者在真实产品里是错的：用户已经看见那几个字了。

回放侧的脚本语法比 `MockAdapter` 宽，`ReplayEntry` 是三态联合（`packages/test-support/llm-replay/src/index.ts:64-75`）：

```ts
export type ReplayEntry =
  | { kind: 'chunks'; chunks: StreamChunk[] }
  | { kind: 'throw'; chunks: StreamChunk[]; message: string; code: string; accepted?: boolean }
  | {
    kind: 'hang'
    /** Optional marker written after the prefix chunks are consumed and before the stream waits for cancellation. */
    readyFile?: string
  }
```

`throw` 带一个 `accepted?: boolean`，区分「请求还没被接受就失败」与「2xx 之后流中间失败」。这两种情况在 loop 里的归宿不同：前者进 attempt 失败、可以重试，后者会留下半截消息。

`hang` 的 `readyFile` 是个小的同步原语：前缀 chunk 吐完之后写一个标记文件，测试进程等这个文件出现再发取消。没有它，取消请求可能在流开始之前就到，测的就不是你想测的那段。

失败场景必须写 sidecar，原因在 `deriveReplayScript` 的报错里：

```text
llm-replay: model call ${key} ended without a finish chunk (a thrown stream);
this scenario needs a replay.override.json sidecar
```

durable settlement 只记录成功的 assistant 结算，抛出的流没有终止 chunk，从日志反推不出来。所以 `snapshot.yml` 里要写 `replay.override: true`，再放一份 `replay.override.json`。sidecar 两种形态：整份 `ReplayEntry[]` 替换录制推导出的脚本，或 `{ patches: [{ at, entry }] }` 只换掉某一次调用。取消场景 `snapshots/acp/cancel-tool-calls/replay.override.json` 用的是第一种，一次给全两个 bash 调用。

## 5. 时钟：能钉的钉住，钉不住的断言接口

`time-context` 插件直接读墙钟，`const now = Date.now()`（`packages/context/time-context/src/index.ts:207`），没有注入点。所以时钟只能从测试这一侧钉。

进程内 spec 用 `vi.setSystemTime`，可以钉到秒：

```ts
vi.setSystemTime(BASE + 90_061_000)
await fire(ctx, sessionAgent(session), 1, 1)
expect(contextTexts(session)).toEqual([
  'Time sampled while preparing turn 1, step 1: 2026-07-15T09:01:01+08:00[Asia/Shanghai]\n'
  + 'Browser time zone for this request: Asia/Shanghai. Interpret otherwise-unqualified dates and times in this zone.\n'
  + 'Elapsed since the preceding model-visible message: 1d 1h 1m 1s.',
])
```

同一段文本在子进程 e2e 里钉不住。`vi` 管不到另一个进程，于是断言退成正则，外加一条负断言（`packages/context/time-context/tests/time-context.e2e.ts:72-78`）：

```ts
expect(contextText[0]).toMatch(
  /Time sampled while preparing turn 1, step 1: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00\[Asia\/Shanghai\]/,
)
expect(contextText[0]).toContain('Elapsed since the preceding model-visible message: unavailable.')

const headers = events.filter(event => event.type === 'request/header')
expect(JSON.stringify(headers)).not.toContain('Time sampled while preparing')
```

负断言在这里比正断言有用。它排除的是归属错误：这句话应该出现在消息体里，混进 `request/header` 就会被模型当成协议元数据。

第三层是录制归一化。已提交的 fixture 里这一行长这样：

```text
Time sampled while preparing turn 1, step 1: 2026-10-16T00:00:00+00:00[UTC]
```

Web 场景里换成具名 token `{{timeContextTimestamp}}`。墙钟读数在录制时被替换掉了，回放不依赖真实时间。`snapshots/AGENTS.md` 把这件事说得更硬：已提交的会话是「normalization fixed points」，波动身份换成保留关系的类型化 token。

还有一个结构细节会被断言用到。这条上下文的 `source` 是：

```ts
{ kind: name, form: 'snapshot', sections: [{ name, text }] }
```

`form: 'snapshot'` 加一个具名 section，意思是一整段文本归一个名字，消费方不用再切字符串。e2e 里就是用 `toMatchObject` 断言这个结构，而不是断言文本前缀。

## 6. 按事件断言：seq 是唯一稳定的坐标

`session.snapshotEvents()` 是这个仓库里最常用的断言入口（`packages/core/session/src/index.ts:728`）。它返回冻结的事件数组，每条带 `seq`、`time`、`type`、`data`。围绕它有三类断言。

第一类是比较顺序。`time-context` 的 e2e 里，每条上下文事件都必须排在对应的 `step/start` 之后：

```ts
for (let index = 0; index < contexts.length; index += 1) {
  expect(contexts[index]!.seq).toBeGreaterThan(starts[index]!.seq)
  expect(contexts[index]!.surfaceOp).toBe('append')
}
```

`seq` 是会话日志里的单调位置，`surfaceOp` 说明事件对模型可见视图做了什么操作。这两个字段一起断言，等于同时钉住「出现了」和「怎么出现」。

第二类是关联。每条 `tool/result` 带 `sourceEventSeqs: [callSeq]`，指回它对应的 `tool/call`。取消场景里那条合成结果就是这么来的：`skip()` 先 `start(block)` 补写一条 `tool/call`，再拿它的 `seq` 当 `sourceEventSeqs` 提交结果。所以「模型调用了一条从未派发的工具」这种事在日志里对不上，replay 也不会因为缺结果而断链。

第三类是原子性，用注入失败的方式测。`packages/core/agent-loop/tests/interception.spec.ts:478` 有一个例子：

```ts
it('leaves inbox state unchanged when its durable append fails', async () => {
  const adapter = new MockAdapter([])
  const ctx = await harness(adapter)
  const agent = await ctx.agentLoop.create(SessionId('rejected-pre-step-append-failure'), {
    provider: 'mock',
    model: 'mock',
  })
  vi.spyOn(agent.session, 'append').mockImplementationOnce(() => {
    throw new Error('append unavailable')
  })

  expect(() => {
    send(agent, 'blocked prompt')
  }).toThrow('append unavailable')
  expect(events(agent)).toEqual([])
  expect(agent.inbox.nextTurn).toEqual([])
  expect(agent.inbox.nextStep).toEqual([])
  expect(agent.status).toBe('idle')
})
```

`mockImplementationOnce` 让第一次 append 抛。之后的四条断言覆盖四个维度：日志是空的、两个队列是空的、状态回到 idle。这是「失败不留痕」的直接编码。同一个文件里还有 `'a blocked prompt preserves adjacent queued prompts'`，断言 `turn/end` 的 reason 只有一个 `{ kind: 'blocked' }`，而且 `turn/start` 也只出现一次，说明被拒绝的 prompt 开了轮、关了自己、没有吞掉后面的队列。

四类断言对象的稳定性差别很大，值得排成一张表：

| 层 | 断言对象 | 实现换一种写法会不会误报 | 能抓到的缺陷 |
|---|---|---|---|
| 内部字段 | `agent.phase.kind`、`inbox.nextStep` | 会，改字段名就红 | 状态机走错分支 |
| 事件流 | `snapshotEvents()` 的 `type` / `seq` / `data` | 偶尔，加事件时要更新 | 顺序错、漏事件、多事件、归属错 |
| 模型可见请求 | `adapter.requests[n].messages` | 少，这是对外契约 | prompt 里混进不该有的内容、工具 schema 漏项 |
| 磁盘世界 | `workspace.expected/` 全树 | 不会 | 工具没生效、多写了文件、写错内容 |

第三种在这套仓库里用得比想象中多。`interception.spec.ts` 里有一条断言直接检查模型收到了什么：

```ts
expect(JSON.stringify(adapter.requests[0]?.messages)).toContain('safe')
expect(JSON.stringify(adapter.requests[0]?.messages)).not.toContain('secret')
```

两行合起来是一个完整的语义：被拒绝的 prompt 没有进请求，排队的那条进了。这种断言比检查内部变量稳定，因为 `messages` 是模型的输入，属于对外契约。

## 7. 取消竞态：拿文件当同步点

取消类的测试最容易写成 flaky。取消本身的三条投递路径见[《插话、注入与急停》](/posts/dsh-steering-and-cancel.html)，这里只看它怎么被验证。`await sleep(50)` 之后再取消，机器慢一点就取消了还没开始的东西，机器快一点就取消了个寂寞。

`snapshots/acp/cancel-tool-calls/` 用文件当同步点。模型脚本是两个 bash 调用：`call_wait` 先写 `started.tmp` 再 `rename` 成 `started.txt`，然后 `setInterval` 挂住不退；`call_skipped` 是 `printf skipped > skipped.txt`。测试客户端在 `promptAndCancel` 里等 `started.txt` 出现再发取消：

```json
{
  "steps": [
    { "op": "initialize" },
    { "op": "newSession" },
    {
      "op": "promptAndCancel",
      "text": "Run two shell commands: wait for cancellation, then write skipped.txt.",
      "waitForFile": { "path": "started.txt" }
    },
    { "op": "waitForTurnEnd" }
  ]
}
```

`rename` 这一步是刻意的。直接写 `started.txt` 的话，测试进程可能在文件只有半个字节的时候就看到它。原子重命名给出一条 happens-before 边：文件存在就意味着第一个调用已经在跑。

取消之后，`session.v4.jsonl` 里的顺序是：

| 顺序 | 事件 | 关键字段 |
|---|---|---|
| 1 | `tool/call` | `callId: call_wait` |
| 2 | `job/registered` | bash 起了后台 job |
| 3 | `job/settled` | `status: "killed"`、`cause: "kill"` |
| 4 | `tool/result` | `Error: tool call aborted`、`code: "ABORTED"` |
| 5 | `tool/call` | `callId: call_skipped`，由 `skip()` 的 `start()` 补写 |
| 6 | `tool/result` | `Error: tool call aborted before dispatch`、`code: "ABORTED_BEFORE_DISPATCH"` |
| 7 | `step/end` | |
| 8 | `turn/end` | `reason: {"kind":"aborted","reason":{"kind":"user"}}` |

第 4 和第 6 是两条不同的结算路径，码值也不同。`ABORTED` 来自已派发调用的 abort 传播，`ABORTED_BEFORE_DISPATCH` 来自 `BatchSettlement.skip()`（`packages/core/tools/src/batch.ts:237` 起），常量定义在 `packages/core/tools/src/contracts.ts:267`。第 5 行那次 `tool/call` 是代价：为了让日志自洽，必须给一个从未派发的调用补一条调用事件，否则第 6 行指向一个不存在的 seq。

自己搭的时候，这段顺序最容易被写错。直觉上「没派发就没有调用事件」，但日志的读者是 replay 和投影，它们按结果反查调用，缺调用会当成数据损坏。宁可多一条假调用，也不要一条悬空结果。

{{< fig "diagrams/04-cancel-race.svg" "取消场景的两条结算路径：已派发的调用被 drain 成 ABORTED，未派发的拿到 ABORTED_BEFORE_DISPATCH，磁盘上没有 skipped.txt" >}}

## 8. 验证世界，而不是自述

`docs/testing.md:33` 那节叫 "Verify the world, not the self-report"：

> an e2e assertion re-runs the command or re-reads the file externally; a keyword probe on the agent's own output lets a cheating agent pass.

同一个场景就是这么断言的。`snapshot.yml` 里有 `workspace.final: true`，同目录下的 `workspace.expected/` 里只有一个文件 `started.txt`。`snapshots/AGENTS.md` 说明这是全树比较：

> A scenario that mutates the workspace sets `workspace.final: true` and commits the complete result under `workspace.expected/`.

全树比较的意思是 `skipped.txt` 不存在也算断言的一部分。如果工具偷偷跑了第二个调用，多出来的文件会让比较失败。这条断言跟模型说了什么、工具返回了什么都无关，它只看磁盘。

同一条规则还给了反例：「Model prose and tool-result text do not prove the external effect」。断言 `tool/result` 里写着 `printf skipped > skipped.txt` 是没用的，那句话是工具自己说的。要证明没写，得看目录。

顺带一句工程细节。`workspace.expected/` 不是录制工具生成的，录制和刷新都不会重写它（`snapshots/AGENTS.md` 原话：「Record and refresh do not rewrite this independent oracle」）。它是一份独立的口径，由人维护。这也是为什么它敢被当成 oracle。

## 9. 测不到的四件事

这套体系有明显的边界，讲清楚比藏着有用。

真实 provider 的流式行为。`test:e2e` 的套件在缺 key 时自跳过（`docs/testing.md:11`），CI 上跑的是跳过之后的绿。`docs/testing.md:25` 的原话是：

> A no-key test proves plumbing; only a with-key run proves the agent works against a real model.

补偿手段是分层：无 key 的套件证明管线通，有 key 的套件证明模型能干活，`test:snapshot:record` 在有 key 的机器上重录 fixture。三件事都不做，就只有第一层。

宿主时钟与并发抖动。`time-context` 直接读 `Date.now()`，没有注入点，子进程里钉不住。并发方面，`docs/testing.md` 的 `## How specs execute` 一节写得很明确：

> Only the process is isolated: ports, predictable paths, external namespaces, and inherited children are not.

所以 spec 必须自己管好端口、路径、子进程。同一节还有一条判据：一个 spec 单独跑才过，那是 spec 的缺陷，不是 runner 不稳。

覆盖率。`test:coverage` 是 CI 的门，逐文件 100%，但 `docs/testing.md:10` 自己声明它不充分：

> Line coverage is necessary, never sufficient: it proves lines ran, not that the feature works as shipped.

同一节还有一句更实用的：没被覆盖的行往往是死代码，门禁是在提醒你删，不是在提醒你补测试。把覆盖率当功能证明，会得到一堆跑到但没用上的行。

浏览器与 GUI。这一层走 `pnpm run test:web`，主体在 Chromium 里跑，模型与推理档位的选择器另跑一遍 WebKit，CI 用只读的 `DSH_SNAPSHOT=replay`。Web 场景留下的是浏览器与 ARIA 证据，和同一份 session 放在一起，没有像素比对，像素对字体和 DPR 太敏感。

## 10. 设计取舍

把上面这些做法摊开，能看到五个明确的取舍，每个都有代价。自己搭的时候，这五条没有一条是必须照抄的，但每条都要先知道代价在哪。

用录制会话当期望输出，不用手写断言。一份 `session.v4.jsonl` 同时是回放输入和期望输出，写一次测试拿到两样东西。代价是 fixture 变成需要维护的资产：会话格式升版时要迁移或重录，而仓库规则不允许移动、覆盖或删除已提交的代，录制与刷新也不会去改 `workspace.expected/` 这份独立口径。收益是回放跑的是真 loop，而不是一段手写的期望。

假模型下沉到 adapter 边界。loop、session、tool pipeline、持久化全是真的，只有 `LlmAdapter.stream()` 被替换。代价是造不出「adapter 内部解析失败」这种场景，那要第三档的协议级假服务器。收益是所有下游行为都是真的，包括你没预料到的那些。

组合测试放子进程。能抓到 Loader、模块解析、退出码这些进程内的东西抓不到的问题。代价是每个 case 一个 30 秒预算、状态不能跨 case 共享、调试要看两个流。所以组合测试只覆盖装载路径和少数端到端场景，不铺开。

失败场景靠 sidecar 手写。durable log 表达不了「流在 chunk 之间炸了」，于是失败场景要么手写 `replay.override.json`，要么用 `MockAdapter` 的 `'hang'` / `errorResponse`。代价是这些场景的脚本不能从录制里再生，格式一改就要人工跟。

断言贴着两端。事件级断言比内部字段稳定，比 UI 文案精确；磁盘级断言最难骗过。代价是中间那层（用户实际看到的界面）覆盖不足，要靠 `test:web` 的 ARIA 快照补。

## 11. 踩坑点

1. `'hang'` 脚本必须有人 abort。没有 `cancel()` 或超时兜底，spec 会一直挂到 vitest 的 deadline，报错信息还指不到你写的那行。

2. 脚本条目数等于模型调用次数，不等于 step 数。一个 step 里模型被重试两次就消耗两条脚本，报 `MockAdapter: script exhausted` 时先数调用，别数 step。

3. 断言 stdout 用 `toBe` 加完整 JSON。`toContain` 会放过「字段缺失」这类问题，而字段缺失正是组合测试最想抓的东西。

4. `workspace.expected/` 是全树比较，多一个临时文件就失败。写标记文件要用「写 tmp 再 rename」，不然测试进程可能看到半个文件，比较也可能抓到残留的 `.tmp`。

5. 快照里 `seq` 和 `time` 可以省略。省略的行按 `projected` 处理，回放时补 `seq` 与 `time: 0`；但 `projected` 行和 `complete` 行不能混在同一个 body 里，混了会直接报错（`packages/test-support/llm-replay/src/index.ts:265-280`）。

6. `vi.setSystemTime` 只影响当前进程。子进程 e2e 里时间照样是真的，断言要么退成正则，要么靠录制归一化。

7. 函数插件的守卫要真的能挂掉。`docs/testing.md:40` 要求在测试里加 `expect('default' in mod).toBe(false)` 和 `unwrapExports` 往返，理由是「守卫只有在回归能让它失败时才算守卫」。只写 `inject` 断言、不写导出形状断言，等于没测 postmortem 里那个坑。

8. 组合测试的 spec 不要依赖固定端口和固定路径。forked worker 并行跑，端口冲突的表现是随机的超时，不是明确的报错。所有资源用 `mkdtemp` 和自己的 teardown 管住。

## 12. 源码索引

- `docs/testing.md:8` 单元层：`pnpm run test`，vitest，registry 的 HMR 安全测试
- `docs/testing.md:10` 覆盖率门：逐文件 100%，附「跑过不等于功能对」的说明
- `docs/testing.md:23-25` with-key 政策：不要省真实 API 测试
- `docs/testing.md:27` 只 mock 昂贵或非确定的边界
- `docs/testing.md:33` 验世界，不验自述
- `docs/testing.md:39-41` 产品可见插件要真组合测试；released 形态跑 `lib/bin.js`
- `packages/AGENTS.md:7` 同一条真组合要求，面向包作者
- `packages/AGENTS.md:20` spec 并行执行，单独跑才过是 spec 缺陷
- `packages/acp/acp/tests/harness.ts:32` `MockAdapter`，脚本数组与 `'hang'`
- `packages/acp/acp/tests/harness.ts:221` `makeBridgeHarness`，唯一的 mock 是 adapter
- `packages/session/session-projection/tests/fixtures/cordis.yml:1-3` 三行组合配置
- `packages/session/session-projection/tests/loader-compatibility.e2e.ts:6-22` 子进程断言 stdout 全文
- `packages/test-support/loader-smoke/src/index.ts:213-255` 子进程隔离与超时诊断
- `packages/test-support/llm-replay/src/index.ts:64-75` `ReplayEntry` 三态
- `packages/test-support/llm-replay/src/index.ts:529` `ReplayOverrideDoc` 两种形态
- `packages/core/agent-loop/tests/interception.spec.ts:478` 追加失败时的原子性断言
- `packages/core/session/src/index.ts:728` `snapshotEvents()`
- `packages/context/time-context/src/index.ts:207` 直接读墙钟，无注入点
- `packages/context/time-context/tests/time-context.e2e.ts:72-78` 正则加负断言
- `snapshots/acp/cancel-tool-calls/input.json` `waitForFile` 同步点

## 13. 最后

这套测试跑起来是三层：进程内 spec 覆盖服务行为，子进程组合测试覆盖装载与入口，录制会话覆盖端到端行为并把期望输出一并存下来。三层之间靠 `seq` 和文件字节对齐。

如果你要照着搭，先做两件最便宜的事：把假模型放在 adapter 边界而不是 tool 边界，让下游全真；把断言从 UI 文案挪到 `snapshotEvents()`。这两件都不需要改产品代码，收益立刻能看见。

第三件事贵一些，但要等它出过事才做就晚了：给每个产品可见插件配一份 test-only 的 `cordis.yml`，在子进程里跑一次。导出的形状错了、`inject` 丢了，这类问题在进程内永远抓不到。

