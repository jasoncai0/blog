---
title: "PTC：把程序当工具执行"
date: "2026-11-14T18:15:00+08:00"
summary: "run_code 为什么能省 token：一段程序里的多次工具调用如何共享上下文、如何回到同一条流水线、拒绝与超时又如何呈现。"
description: "拆 PTC 运行时：沙箱、序列化边界、tool/ptc-dispatch 事件、预算与取消，以及它与普通 tool call 的取舍。"
tags: ["harness", "codegen", "architecture"]
slug: "dsh-ptc-runtime"
---

> 源码版本：`deepseek-harness` v0.2.2-rc.1（commit `fcc0ad46`）。本文对照 `docs/subsystems/ptc-runtime.zh.md`（257 行）、`packages/core/tool-executor-ptc/src/ptc.ts`（776 行）、`packages/ptc-runtime/ptc-runtime-node/src/execution.ts`、同目录 `worker.ts`（380 行）与 `bootstrap.ts`（286 行）。
> 本系列第三季第 5 篇。前两季拆过[工具执行流水线](/posts/dsh-tool-pipeline.html)和[工具结果](/posts/dsh-tool-results.html)的写法，这一篇看它们的另一种用法：模型不逐个调用工具，而是交一段程序，工具调用缩进成程序里的函数。

先看三个现象。

一次 `run_code` 读了二十个文件，事后翻消息历史，只有一段两百字的摘要和一句 `return` 的结果。二十份文件内容一份都不在，连读过的文件名都没留下。

程序里 `catch` 到的错误对象带 `toolName` 字段，模型下一轮却不知道那次调用失败过。失败的调用确实记了账，账本在日志里，不在对话里。

外层调用被取消时，程序里正在跑的子调用跟着断了。没有「先让它跑完再收尾」的中间态，也没有「已经发出去、还没回来」的悬空调用。

这三个现象指向同一套安排：程序在另一个进程里执行，工具调用被 RPC 回主进程，中间结果只写日志不进消息。把这套安排拆开，能回答一个具体问题：什么时候让模型写程序，什么时候让它老老实实调用工具。

## 0. 结论

PTC 是 programmatic tool calling 的缩写。模型唯一的直接工具是 `run_code`，其他能力都变成程序里的 `tools.*` 函数。

| # | 问题 | 答案 | 代码位置 |
| --- | --- | --- | --- |
| 1 | 程序在哪跑 | 受管 Node 子进程，512MB 老生代上限，`process.env` 清空，cwd 固定 | `worker.ts:282-296` |
| 2 | 能引用什么 | 单个 `tools` 命名空间、5 个 console 方法、`await import()` | `bootstrap.ts` `makeNamespaces` |
| 3 | 返回值怎么过界 | 只收无损 JSON，两端各快照一次 | `bootstrap.ts` `prepareCompletion` |
| 4 | 子调用怎么标识 | `${callId}:ptc:${n}`，带 rootCallId、父 token、run signal | `ptc.ts:519-529` |
| 5 | 怎么记账 | `tool/ptc-dispatch-start` 与 `tool/ptc-dispatch`，只进日志 | `packages/core/tool-executor-ptc/src/types.ts:25-60` |
| 6 | 拒绝长什么样 | 程序里 throw，包成带 `toolName` 的 `ToolCallError` | `ptc.ts:648` |
| 7 | 额度怎么算 | 超时分钟数乘 1024，默认两分钟 2048 次 | `ptc.ts:698-699` |
| 8 | 取消怎么传 | 外层 signal 到 runController，再到每个子调用 signal | `ptc.ts:379-381` |

路线图：第 1 节看进程与管道，第 2 节看程序能摸到什么，第 3 节看数据怎么过界，第 4 到 5 节看子调用怎么穿流水线，第 6 节算 token 账，第 7 节看预算与取消，第 8 节给取舍表。

## 1. 三个角色，两次跨进程

一次 PTC 调用里有三个角色。agent 主进程跑 loop、跑工具、持有 session；程序子进程跑模型写的代码；中间一条换行分隔 JSON 的控制管道。工具永远在主进程执行，子进程里只有等在那里的绑定函数。

```ts
const heapFlag = `--max-old-space-size=${this.config.maxOldGenerationSizeMb}`
const argv = [executable, ...packaged ? [] : [heapFlag], ...bootstrapArgs(...)]
this.confined = policy.mode === 'danger-full-access'
  ? undefined
  : await this.ctx.sandbox.confine(argv, { ...policy, mode: policy.mode }, this.controller.signal)
const env: NodeJS.ProcessEnv = Object.fromEntries(Object.keys(process.env)
  .filter(key => !STARTUP_ENVIRONMENT_NAMES.has(key.toUpperCase()) && key.toUpperCase() !== 'ELECTRON_RUN_AS_NODE')
  .map(key => [key, undefined]))
```

看 `map(key => [key, undefined])` 这一行：留下的键被设成 undefined，等于清空。模型侧说明写的是 `process.env starts empty`，代码用留键去值实现，没有删键。两者不等价，`'KEY' in process.env` 仍然为真，`process.env.KEY` 是 undefined。

`danger-full-access` 跳过 `confine`，程序仍然是子进程。`workspace-write` 走沙箱。`maxOldGenerationSizeMb` 默认 512，`--max-old-space-size` 只管 V8 老生代，原生内存与子进程树不在这个数字里。

同一个 `start()` 还定了三件事。cwd 固定为 `spec.cwd`，相对路径都按它解析。stdin 直接丢弃，程序没法交互。控制通道走 `stdio.control`，和 stdout 分开，日志不会污染协议。

控制通道用 birpc 跑换行分隔的 JSON，协议版本号是 2。版本不匹配直接判 `protocol` 失败，不会尝试降级。这条通道上的每条消息都带字节上限，程序往宿主发的调用失败也不会污染日志流。

> 自己搭 Agent 时：把「程序能碰到什么」和「工具能碰到什么」分开配置。这里前者由 argv、env、cwd、沙箱策略决定，后者由每个工具自己的审批决定。合成一个开关，改一个会顺带改另一个。

还有一个词要看清楚。`PtcRunSandbox.denied` 的定义是「程序失败文本匹配到后端诊断」，文档专门写了这句。它说明程序撞到过拒绝签名，不说明沙箱完整生效。

{{< fig "diagrams/01-processes.svg" "一次 PTC 调用里的三个角色：主进程持有循环、会话与工具，子进程只跑程序，控制管道是两者之间唯一的通道" >}}

## 2. 程序能引用什么

程序侧的环境由 host 注入，不由 Node 默认值决定。三样东西。

第一样是 `tools` 命名空间。它的成员来自当前 agent 可见的工具集，一次运行一份，每个 schema 深冻结：

```ts
for (const schema of tools.schemas(exec.agent, exec.toolSet)) {
  const frozen = deepFreeze(schema)
  functions[frozen.name] = binding(frozen)
}
const bindings = [{
  global: 'tools',
  functions,
  errorClass: { name: 'ToolCallError', memberNameProperty: 'toolName' },
}]
```

名字带横杠或点的工具，程序里写成 `tools["my-tool"](args)`，下标访问天然支持。这份投影在每次运行开始时重算，schema 深冻结，程序改 schema 也影响不到宿主侧的校验。全局名只能有一个，`validateBindings` 会拒绝重复、保留字、非法标识符和 `RESERVED_BINDING_GLOBALS`（`console`、`__dsh_main__` 这类名字属于宿主）。

第二样是 console，只有五个方法：

```ts
export function makeConsoleShim(logs: Pick<LogBuffer, 'push'>) {
  const render = (args: unknown[]): string =>
    args.map(arg => typeof arg === 'string' ? arg : inspect(arg, INSPECT_OPTIONS)).join(' ')
  const shim = Object.create(null) as Record<...>
  for (const level of CONSOLE_LEVELS) {
    shim[level] = (...args: unknown[]) => { logs.push(render(args)) }
  }
  return shim
}
```

`CONSOLE_LEVELS` 是 log、info、warn、error、debug。没有 `console.table`、`console.time`、`console.group`。`process.stdout.write` 也被接管，写进同一个 `LogBuffer`，顺序按发生顺序，不会和 console 抢管道。

写程序时声明的是 TypeScript，跑起来之前会被剥掉类型。`stripTypes` 用 `node:module` 的 `stripTypeScriptTypes`，前后各垫一段固定代码把程序包成函数体，再切回来。所以类型注解只是建议，语法必须是可擦除的：`enum`、`namespace`、参数属性都不行。

第三样是 `await import()`。模块加载走 Node 原生机制，所以 `fs/promises`、`node:child_process` 这类都在射程内，受沙箱策略约束。`process.env` 是空的，要凭据就得从工具层拿，或者由宿主显式注入。

命名空间是 null 原型，`__proto__` 在程序里只是普通自有属性。绑定函数先冻结参数，再编码过界：

```ts
try { detached = snapshotPtcJsonValue(args) } catch { detached = undefined }
if (detached === undefined) throw new CapturedError('binding arguments must be lossless JSON')
const result = decodePtcJsonWire(await host.invoke(global, name, encodePtcJsonWire(detached)))
```

看第一行：模型写的 getter 会在 detach 时抛错，`catch` 把它折成 `undefined`，随后统一报同一条消息。程序拿到的是一个普通 `Error`，带 `message`，不带宿主堆栈。

## 3. 序列化边界：只有无损 JSON 过界

PTC 的值域是一个递归类型，没有 `Date`、`Map`、`BigInt`、`undefined`、`NaN`、循环引用。过界失败是硬失败，不降级。

```ts
export function prepareCompletion(value: unknown, remainingOutputBytes: number, maxOutputBytes = remainingOutputBytes): ProgramResult {
  if (value === undefined) return {}
  let snapshot: ReturnType<typeof snapshotPtcJsonValue>
  try { snapshot = snapshotPtcJsonValue(value) } catch { snapshot = undefined }
  if (snapshot === undefined) {
    return prepareFailure('invalid-output', 'program completion must be lossless JSON', remainingOutputBytes, maxOutputBytes)
  }
  if (jsonValueBytesUpTo(snapshot, remainingOutputBytes) === undefined) return outputLimit(maxOutputBytes)
  return { value: encodePtcJsonWire(snapshot) }
}
```

三个分支各对应一种失败。`return` 不带值走 `{}`，参数里允许没有结果。值不可序列化给 `invalid-output`，消息固定，不回显原值。放不下给 `output-limit`，也只带字节数。

预算是共享的：日志和返回值共用一个 `maxOutputBytes`（默认 64MiB），`LogBuffer` 初始占 2 字节，那是空数组 `[]` 的 JSON 长度。程序先把预算花在 `console.log` 上，返回值就只剩零头。

宿主侧再校验一次。`prepareCompletion` 的注释给了理由：host 要能挡住敌意流量，而且程序 shim 看不见的原生管道写入由宿主负责。两端都查，一侧被绕过还有另一侧。

```ts
export function readProgramResult(reply: unknown): ProgramResult {
  ...
  const parsed = decodePtcJsonWire(...)
  if (parsed === undefined) return { error: { kind: 'invalid-output', message: 'invalid program result' } }
}
```

这条边界带来的直接后果，是用 `Date` 记时间、用 `Map` 攒计数、用 `undefined` 表示缺失的写法全部要改。程序里用 ISO 字符串、普通对象、`null`。

## 4. 子调用怎么回到流水线

绑定函数不直接调用工具。它构造一个 `ToolCallInput`，塞进执行器的队列，等结果。

```ts
const { name } = schema
if (runOver()) throw new Error(`run_code run is over (${String(runController.signal.reason)}); ${name} not dispatched`)
// Count before validation and queuing so repeated failed calls also exhaust the allowance.
const n = ++toolCallAttempts
if (n > maxSubCalls) {
  const error = new ToolCallLimitExceededError(maxSubCalls, n, maxSubCallsPerMinute)
  runController.abort(error)
  throw error
}
const normalized = jsonNormalizeArgs(rawArgs)
const subCallId = brandString<ToolCallId>(`${String(exec.callId)}:ptc:${n}`)
const input = {
  callId: subCallId, rootCallId: exec.rootCallId, name, schema,
  arguments: normalized.dispatched,
  ...exec.agent ? { agent: exec.agent } : {},
  ...exec.toolSet !== undefined ? { toolSet: exec.toolSet } : {},
  parent: exec.token, signal: runController.signal,
}
```

这段是整个机制的接缝。`parent: exec.token` 是父调用的 transport token，子调用拿它当自己的上游，所以流式事件的归属、`agent`、`toolSet` 全都继承。`rootCallId` 让一组子调用能挂回同一次 `run_code`。`signal: runController.signal` 接的是运行级取消，不是程序里的局部对象。

`jsonNormalizeArgs` 生成两份参数：`dispatched` 给工具，`logged` 给日志。注释说的是一份 sibling parse，字节完全相同但对象不同，工具改自己的参数不会污染日志记录。

进入队列之后由一条单驾驶通道推进，只有「启动后的函数体」并发：

```ts
async start(): Promise<void> {
  exec.agent?.session.append('tool/ptc-dispatch-start', {
    rootCallId: exec.rootCallId, parentCallId: exec.callId, subCallId, name,
    arguments: normalized.logged,
  })
  // Ordered prepare runs INSIDE the driver lane: the next entry's
  // pre-execute waits for this resolution, as under the native scheduler.
  const prepared = await executor.prepare(input)
  ...
}
```

看 `prepare` 这一行：它在有序车道上跑，所以下一个子调用的 `pre-execute` 会等它解析完。这与原生调度的语义一致。能重叠的只有 `executor.dispatch` 之后的函数体。

分工是明确的。append 开始事件、prepare、post-execute、context、settle 全在同一条车道；只有工具函数体并发。每个子调用启动前重新读一次 `executor.executionMode(input).kind`，分类变了就按新分类调度，注释写的是 fail-closed：没声明为并行的按独占处理。

独占调用会持屏障到提交完成。结果按提交顺序返回，用一个队首游标推进。程序里 `await` 的顺序可能与提交顺序不同，但日志顺序和「已经生效的副作用」顺序一致。

## 5. 拒绝怎么变成程序里的异常

工具失败时，`settle` 立刻给程序一个结果对象，没有等日志写完：

```ts
resolve(result.isError
  ? { isError: true, message: result.error.message }
  : { isError: false, value: result.value })
```

程序侧拿到 `isError` 后走这条线：

```ts
const outcome = await this.call(...)
if (outcome.isError) throw new Error(outcome.message)
```

抛出的是普通 `Error`。外层再包一次，把命名空间声明的错误类按上去：

```ts
export function makeBindingErrorClass(descriptor: { name: string; memberNameProperty: string }) {
  return class BindingCallError extends CapturedError {
    constructor(memberName: string, message: string) {
      super(message)
      defineBindingErrorField(this, 'name', descriptor.name)
      defineBindingErrorField(this, descriptor.memberNameProperty, memberName)
    }
  }
}
```

`defineBindingErrorField` 用 `defineProperty` 写成不可枚举、不可改写的字段。这样 `err.name === 'ToolCallError'` 与 `err.toolName === 'read_file'` 都能用，模型写的 `try/catch` 能按工具名分支。

日志侧的活另起一条 Promise：

```ts
const logged = await shapeDispatchLog({ exec, agent, subCallId, name, isError: result.isError, content: result.content })
agent.session.append('tool/ptc-dispatch', {
  rootCallId: exec.rootCallId, parentCallId: exec.callId, subCallId, name,
  arguments: normalized.logged, isError: result.isError,
  ...result.error?.info === undefined ? {} : { error: result.error.info },
  content: logged,
})
```

`settle` 的注释交代了顺序：程序立刻拿到值，日志监听器（比如落盘后端）不能拖住绑定，也不能占住调度槽位。事件追加是受追踪的旁路工作，运行收尾时 `drainDispatches` 会等它写完。

所以失败有两条记录路径，走的是两份内容：程序拿到 `{ isError: true, message }`，日志拿到完整的 error info 与 content。`tools/ptc-dispatch-log` 瀑布可以替换日志副本，注释写明程序的值和模型可见的结果都不受影响。

{{< fig "diagrams/02-dispatch-sequence.svg" "一次子调用的时序：程序侧绑定发起，有序车道上先写开始事件再 prepare，函数体并发，结果先回程序再补日志" >}}

## 6. 为什么省 token

省的是中间结果。`run_code` 自己是一次普通工具调用，结果照常进 `tool/result`；程序里的每次子调用只写日志，`deriveMessages()` 忽略这两类事件。

```ts
/**
 * Log-only: `deriveMessages()` ignores this event, so a sub-call never
 * re-enters the model's context. ...
 */
'tool/ptc-dispatch': (data: PtcDispatchEventData) => void
```

看「never re-enters」这一段：读十个文件、跑五次 grep、查三次数据库，对话里只留 `run_code` 的入参（程序文本）和出参（`logs` 与 `result`）。二十份文件内容从来没进过上下文，也就没进过下一次请求的 prompt。

省下的量随工具调用次数线性增长。单次调用的收益可以按「中间结果字节数减程序文本字节数」估。

代价有三块。

可观测性从「每次工具调用一条大记录」变成「每次一条小记录」。要看细节得去 `tool/ptc-dispatch`，那里有完整 content。

调试从「模型读错误原文」变成「程序自己解释错误」。模型只看到程序最终打印的东西，没打印就是没看见。

审批粒度从「每个工具一次」变成「程序一次加每个工具各一次」。程序级提权用 `subject: 'program'` 走审批，嵌套工具保留自己的策略与审批，不能靠程序级授权绕过。

README 对 token 的说法很克制：PTC 用生成的 SDK 文本加一个传输 schema 换掉了所有终端工具的 schema，不承诺普遍减少。工具少的时候，SDK 文本可能比 schema 更长。

KV cache 也跟着这个形状走。`maxSubCallsPerMinute` 改了不影响 prompt 与 schema token；SDK 文本、传输 schema、可见工具集不变，前缀就稳定。

> 自己搭 Agent 时：先量 SDK 文本和工具 schema 哪个更贵，再决定开不开 PTC。工具集小的时候，省下的中间结果不够付 SDK 的固定成本。

{{< fig "diagrams/03-token-accounting.svg" "哪些数据进模型历史：外层 run_code 的结果是一等公民，子调用的入参与结果只写日志，因此不占下一轮请求的 token" >}}

## 7. 预算与取消

超时先被夹一次。`NodeConfig.timeoutMs` 默认 120000，`maxTimeoutMs` 默认 600000，工具 schema 的 `timeoutMs` 只能在这个区间里调整；`0` 不解除截止。运行时的 `resolve()` 负责补 cwd 与截止，`run()` 不再兜底。

schema 里写了非正数、非有限值，或者当前运行时根本不支持超时，`execute` 会在调运行时之前就抛错，消息直接回给模型。这条校验放在工具层，是为了让错误早一点出现，不必等程序真的跑起来。

调用额度不是一个固定数字，按超时折算：

```ts
const maxSubCalls = spec.timeoutMs === null
  ? Infinity
  : Math.floor(spec.timeoutMs / 60_000 * maxSubCallsPerMinute)
```

`maxSubCallsPerMinute` 默认 1024。默认两分钟超时给 2048 次额度。它是总配额，不是滑动窗口，所有绑定共享同一个计数，排队中的、参数非法的、工具失败的都算。

额度耗尽不是异常，是中止：

```ts
if (n > maxSubCalls) {
  const error = new ToolCallLimitExceededError(maxSubCalls, n, maxSubCallsPerMinute)
  runController.abort(error)
  throw error
}
```

注释解释了为什么在参数校验和入队之前自增：反复失败的调用也要耗尽额度，否则一个写错的循环可以无限重试。错误消息里带一句「`run_code` 程序里可能有死循环」，给模型一个改写的线索。

`abort` 之后程序还能不能继续？不能。下一次绑定调用在门口就被 `runOver()` 拦下，消息是 `run_code run is over (...); <name> not dispatched`。已经发出的子调用被 `runController` 一起取消，排队中的被 `abandon`，消息是 `...; <name> tool call abandoned`。

取消的传播链是三跳。外层 `callSignal` 的 reason 通过 `onOuterAbort` 转进 `runController`，每个子调用的 `input.signal` 就是 `runController.signal`。运行结束的 `finally` 里还有一次 `runController.abort('run_code settled')`，接着 `await drainDispatches()`，保证收尾前不留悬空调用。

还有一条容易被忽略的取消源：沙箱模式变化。

```ts
// PtcCalls 监听 session/event 的 sandbox/mode，模式变了就取消该会话所有在飞调用
this.onSandboxMode = (sessionId: string) => {
  ...controller.abort(new Error('sandbox mode changed'))
}
```

程序跑到一半用户把沙箱放宽或收紧，正在执行的程序按旧策略继续跑就不对了，所以整个取消。新程序会等旧清理完成再启动。

并行上限是 10（`maxParallelSubCalls`），只对声明为并行安全的工具生效，独占工具按提交顺序串行。并发的东西一多，背压就落在日志侧：日志任务积压超过并行上限时，有序车道会等。

保留态环境另有一套规则。`stateOwner` 只在没有提权时传，`stateOwners` 用 `WeakMap` 沿 provider 链找原始实例，首次使用时给 agent 注册 `runtime.release(agent)` 的清理。提权的那次不复用环境，理由是提权只对一次执行有效。

{{< fig "diagrams/04-cancel-budget.svg" "预算与取消的传播：外层 signal、运行级 controller、每个子调用的 signal 三跳，额度耗尽与沙箱切换都走中止而不是异常" >}}

## 8. 与普通 tool call 的取舍

| 维度 | 普通 tool call | PTC 子调用 | 外层 run_code |
| --- | --- | --- | --- |
| 审批 | 每个工具一次，按工具策略 | 继承工具策略，提权走程序级审批 | 一次程序级审批（`subject: 'program'`） |
| 审计 | `tool/call` 加 `tool/result`，进对话 | `tool/ptc-dispatch-start` 加 `tool/ptc-dispatch`，只进日志 | 两条事件都在，进对话 |
| 错误语义 | 结果里 `isError`，模型可读 | 程序里 throw，可 `try/catch` | 整程序失败，模型读错误文本 |
| 并发 | 框架按工具声明分组并行 | 程序内 `Promise.all`，上限 10 | 单次调用，内部并行 |
| 上下文 | 每次结果进历史 | 不进历史 | 只有最终结果进历史 |
| 可调试性 | 高，每次输入输出都在对话里 | 低，要去日志里查 | 中，程序文本可读 |
| 重放 | 从 `tool/call` 重放 | 不能重建，中间值 execution-local | 能，程序文本在日志里 |

选择规则可以用三句话概括。

中间结果比程序文本大的活，交给程序。批量读文件、扫目录、查多次数据库，都是这一类。

需要模型逐步判断的活，保持普通调用。模型看见第一步结果才能决定第二步，中间结果本身就是决策输入。

需要人逐次审批的活，保持普通调用。程序级审批一次放行整段程序，嵌套调用不会逐个弹窗。

反过来也有判据。程序只用一次工具，或者调用之间没有数据依赖，写程序就是纯开销：多一次进程启动、多一层序列化、多一个模型要写的控制流。判断标准是「中间结果有没有必要被模型看见」，不是「调用次数多不多」。

## 9. 设计取舍

1. 程序放子进程，工具留主进程。代价是每次绑定一次 IPC，收益是工具、审批、策略、session 全都不用搬进沙箱；换成程序内直接调工具，所有工具都要在沙箱里重建一遍。

2. 绑定只给一个 `tools` 命名空间。代价是名字里有横杠要用下标，收益是「哪些能力对模型可见」只有一个投影点，与原生 function calling 共用一个 store。

3. 只让无损 JSON 过界。代价是 `Date`、`Map`、`BigInt` 不能用，收益是跨进程序列化不会出现静默丢字段；换成宽松序列化，模型会拿到被悄悄改过的值。

4. 记账用两个新事件，不复用 `tool/call`。代价是查询侧多认识两种事件，收益是 `deriveMessages()` 不用特判就能保证子调用不进上下文。

5. 额度按「每分钟乘超时」折算。代价是超时改了就改额度，收益是长程序不会因为固定额度被误杀；换成固定 2048，十分钟的程序和两分钟的程序一个待遇。

6. 环境保留要显式 `stateOwner`。代价是多一个参数，收益是无状态后端可以完全不复用进程；换成默认复用，每次运行都要赌上一次没留副作用。

## 10. 踩坑点

1. 程序里读不到 `process.env`。现象是 `process.env.API_KEY` 是 undefined。原因是子进程 env 被留键去值清空。对策是把凭据需求做成工具，让工具层去拿。

2. `denied: true` 被当成「沙箱生效了」。现象是以为越界被挡住了，其实程序只是碰到了拒绝签名。这个字段的定义就是失败文本匹配，要看 `enforcement` 与策略本身。

3. 返回值里塞了 `Date`。现象是 `invalid-output`，消息只有一句 `program completion must be lossless JSON`。原因是这条边界只收无损 JSON。对策是 `toISOString()`。

4. 大日志把返回值预算吃光。现象是返回值变成 `output-limit`，日志还在。原因是两者共享 64MiB。对策是程序里先聚合，只打印摘要。

5. 以为超时可以 `catch`。现象是 `try/catch` 里什么都没抓到，整个程序失败。原因是超时由 worker 的定时器触发，直接终止执行，不经过程序的异常路径。

6. 以为额度耗尽可以续跑。现象是 `catch` 住了 `ToolCallLimitExceededError`，下一次调用报 `run is over`。原因是这条路径先 `abort` 运行，再抛错。

7. 保留态环境配上提权。现象是第二次调用看不到上一次的变量。原因是提权那次不传 `stateOwner`，环境不复用。

8. 沙箱切换把程序打断了。现象是程序跑着跑着失败，消息里带 `sandbox mode changed`。原因是模式变化会取消该会话所有在飞调用。

9. 一次 `Promise.all` 发太多调用。现象是部分调用被拒，消息带上下文里的 pending 上限。原因是宿主通道有 `maxPendingCalls`（默认 128）与字节上限，排队不是无限的。对策是分批 `await`。

10. 用日志调试宿主问题。现象是日志里看不到工具的原始返回。原因是 `tools/ptc-dispatch-log` 瀑布已经改写过内容，宿主看到的和程序拿到的不一定是同一份。

## 11. 源码索引

| 位置 | 证明什么 |
| --- | --- |
| `docs/subsystems/ptc-runtime.zh.md:30-80` | `PtcRunRequest` / `PtcRunResult` 契约，错误是结果字段不是 reject |
| `packages/core/tool-executor-ptc/src/index.ts:24-40` | `PTC_ONLY_INSTRUCTION` 与 `tools:sdk` 两段 prompt |
| `packages/core/tool-executor-ptc/src/index.ts:70-100` | 执行器只暴露 `run_code`，其它名字直接拒 |
| `packages/core/tool-executor-ptc/src/ptc.ts:299` | `stateOwners` 的 WeakMap 与 provider 链 |
| `packages/core/tool-executor-ptc/src/ptc.ts:361` | 程序级审批 `subject: 'program'` |
| `packages/core/tool-executor-ptc/src/ptc.ts:379-383` | `runController`、外层 abort 转发、调用计数起点 |
| `packages/core/tool-executor-ptc/src/ptc.ts:504-529` | `runOver`、绑定闭包、`subCallId` 与父 token |
| `packages/core/tool-executor-ptc/src/ptc.ts:545-575` | `settle` 先后顺序与两份 content |
| `packages/core/tool-executor-ptc/src/ptc.ts:587-600` | 开始事件与有序车道里的 `prepare` |
| `packages/core/tool-executor-ptc/src/ptc.ts:648` | 失败折成程序里的 `throw` |
| `packages/core/tool-executor-ptc/src/ptc.ts:698-699` | 额度折算公式 |
| `packages/core/tool-executor-ptc/src/ptc.ts:705-706` | 收尾 abort 与 `drainDispatches` |
| `packages/core/tool-executor-ptc/src/types.ts:25-60` | 两个 log-only 事件的字段与语义 |
| `packages/core/tool-executor-ptc/src/errors.ts` | `ToolCallLimitExceededError` 的完整消息 |
| `packages/ptc-runtime/ptc-runtime-node/src/worker.ts:282-296` | argv、confine、env 清空、spawn |
| `packages/ptc-runtime/ptc-runtime-node/src/bootstrap.ts:105-126` | console 只有五个方法 |
| `packages/ptc-runtime/ptc-runtime-node/src/bootstrap.ts:159-180` | `prepareCompletion` 的三个失败分支 |
| `packages/ptc-runtime/ptc-runtime-node/src/bootstrap.ts:232-256` | 错误类的 `name` 与 `toolName` 字段 |
| `packages/ptc-runtime/ptc-runtime-node/src/execution.ts:120-183` | `resolve` 补 cwd 与截止、保留态复用与释放 |
| `packages/ptc-runtime/ptc-runtime-node/src/config.ts` | 默认超时、堆上限、输出预算 |

## 最后

PTC 做的事可以概括成一句：给模型一个进程，把工具调用变成进程里的函数调用，然后把中间结果关在日志里。省 token 是结果，机制是「只有一次工具调用的输入输出进对话」。

这套安排对模型的要求更高。它得会写程序、会处理异常、会自己决定打印什么。写不好就是零信息：程序跑完，日志里一堆东西，模型什么都没看见。

对框架的要求也更高。子调用要穿完整流水线，取消要传到底，额度要防死循环，日志要保证不丢。任何一环松一点，省下的 token 会以调试成本还回来。

如果你只记一件事：`run_code` 不是「更强的 bash」，它是一个受管进程加一条 log-only 的旁路记账。判据始终是同一个，中间结果该不该被模型看见。

下一篇拆遥测：一次请求里的 span 记录了什么、token 怎么记账、以及为什么「遥测不能改变行为」是一条硬约束。

上一篇：[协作工具：todo、提问、呈现与交付物](/posts/dsh-collaboration-tools.html)｜下一篇：[遥测：trace、token 与产品事件](/posts/dsh-telemetry.html)
