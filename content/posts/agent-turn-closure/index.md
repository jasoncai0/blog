---
title: "每轮对话都闭合吗：Agent 的 turn 终点与崩溃修复"
date: "2026-11-24T22:30:00+08:00"
summary: "运行中的 turn 由 finally 保证有 end；进程被 SIGKILL 时，磁盘上一定可能留下只有 start 没有 end 的 turn。DSH 在恢复时补写 tool/result、step/end、turn/end(interrupted)，Claude Code 只在 resume 时补一条 synthetic assistant。两种做法对应两种「闭合」的定义。"
description: "从 DSH 的 turn() finally、200ms 写盘窗口、interruptedTurnClosers，到 Claude Code 的 turn_duration 与 resume 补丁，拆解 turn 闭合的三层保证和六个切断点。"
tags: ["agent", "harness", "event-sourcing", "architecture"]
slug: "agent-turn-closure"
---

> 源码版本：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`；Claude Code `2.1.321`。DSH 行号以该 commit 为准；Claude Code 的证据来自本机会话文件、二进制字符串和三次 `-p` 实验。

结论：进程还在运行时，每个 `turn/start` 都能有配对的 `turn/end`；进程被强杀时，做不到。任何 Agent 都不例外。工程上能保证的是另外两件事：磁盘上留下一个可判定的未闭合尾巴，下次打开时用幂等规则补上。

## 1. 先把「闭合」拆成三个断言

「每一轮都闭合」这句话混了三个不同层面的断言：

| 断言 | 谁保证 | 硬崩溃时 |
|---|---|---|
| 内存日志里 start/end 成对 | `turn()` 的 `finally` | 不成立，finally 不执行 |
| 磁盘上是一个完整前缀 | JSONL writer 的整批写 + fsync | 成立，但可能缺尾巴 |
| 重开后历史成对 | `interruptedTurnClosers` | 成立，补写后生效 |

前三个断言分别对应进程内控制流、持久化协议、恢复算法。它们解决的问题不同，失败模式也不同。

所以「崩溃后 turn 会不会不闭合」的答案是会，而且这是正常状态；「有没有机制保证闭合」的答案是有，但在下一次打开时生效。

{{< fig "diagrams/01-closure-stack.svg" "四层闭合保证：进程内 finally、磁盘前缀、恢复补写、消费方折叠。SIGKILL 切在第一层和第二层之间，finally 不会执行。" >}}

## 2. 日志里的 turn 是一对括号

真实事件长这样：

```json
{"type":"turn/start","seq":5,"time":1795530915512,"data":{"turn":1}}
{"type":"turn/end","seq":26,"time":1795530939779,"data":{"turn":1,"reason":{"kind":"completed"}}}
```

事件流是扁平的，没有嵌套结构。turn 的边界靠 `turn/start` 与 `turn/end` 配对扫出来。`seq` 连续，`time` 是毫秒时间戳，`data.turn` 在这一对里相同。

`turn/end` 的 reason 有七种：`completed`、`blocked`、`aborted`、`error`、`max-tokens`、`interrupted`、`forked`。前五种是运行期出口，`max-tokens` 一旦出现会粘住；后两种只由恢复或 fork 产生。七种出口的细节在[上一篇文章](/posts/dsh-turn-step-state-machine.html)里，这里只关心一件事：只要这一轮开始过，就必须有一条 end。

如果只写 start 不写 end，消费方会遇到三个具体问题：

- UI 无法判断这一轮是还在跑、已经结束，还是进程死了；
- 下一次请求的历史里可能出现没有结果的 tool_use，provider 直接拒绝；
- fork、统计、重放都无法确定切点。

`turn/end` 是日志协议的一部分，不只是展示。

## 3. 进程内：所有出口收在 finally

`packages/core/agent-loop/src/agent.ts` 的 `turn()` 把整轮包在 try/finally 里。所有出口先给 `turnEnds` 赋值，finally 只做一次 append：

```ts
} finally {
  try {
    this.session.append('turn/end', { turn, reason: turnEnds! })
  } catch (error: unknown) {
    this.throwError(error)
  }
}
```

`turnEnds` 初值是 null，每条路径都会在离开 try 之前写它：

- 正常跑完：`stepEnd`；
- pre-step 拒绝：`blocked`；
- 空轮次：`completed`；
- 取消：catch 里读到 abort cause，写 `aborted`；
- 异常：catch 里归一化成 `error`。

finally 里再包一层 try/catch，是因为 append 自己也可能失败。这时把错误交给 driver，不能让 finally 吞掉。

外层 `kick()` 再兜一层：`while (await this.turn()) {}` 外面是 try/catch/finally，任何 turn 抛出的错误都在这里收口，phase 回到 idle。`step()` 也有自己的 finally，保证 `step/end` 与 `step/start` 配对。三层 finally 叠起来，运行期的闭合是结构性的，不靠调用方记得收尾。

取消和退出走同一条路。`cancel(cause)` 只是 abort 当前 turn 的 AbortController，`throwIfAborted` 在下一个检查点抛出，catch 认出 cause，finally 照常写 end。正常退出应用时，`dispose` 先 `cancel({kind:'disposed'})`，再 `await whenIdle()`，最后关闭 session handle 把缓冲的事件排干。正常点停止、正常关应用、模型报错、工具抛异常，都走同一条 finally，区别只在 reason。

## 4. 磁盘：只保证前缀

`Session.append()` 是同步的，但它同步的只是内存：校验、分配 seq、发布事件。持久化是另一条异步链路。JSONL 后端把 live 事件放进缓冲，最多等 200ms 再批量写：

```ts
export const LIVE_WRITE_BATCH_MAX_DELAY_MS = 200
```

也就是说，`turn/end` 在内存里已经成立时，磁盘上可能还没有。写入本身是「整批 encode → 一次 write → fsync」，失败回滚到写入前的长度；进程在写一半时死掉，下次打开会截断 torn tail。修复之后，磁盘上的不变量是：已经写下的字节是一个完整、连续的前缀。它不保证最后一条是 end。

真正把「意图」提前落盘的是 checkpoint 策略（`session-checkpoint-policy`）：

- 每次 `llm/stream` 发出第一个 chunk 之前 flush；
- 每次顶层 `tools/execute` 进入工具体之前 flush；
- 每个 `agent/pre-step` 之前 flush。

这三个屏障覆盖了最贵的两个副作用：模型请求和工具执行。崩溃后你至少知道「请求发过」或「工具调用开始了」。轮次结束不在屏障列表里，因为下一轮的 pre-step 会顺手把上一轮的尾巴刷掉，正常退出也会在 close 时排干。省一次 fsync，换一个最多 200ms 的窗口。

{{< fig "diagrams/02-normal-sequence.svg" "正常一轮的写入时序：turn/end 在内存日志里立即成立，落盘要等 200ms 批窗口或下一次 checkpoint。" >}}

## 5. 重开：补写而不是重放

恢复入口在 `packages/core/agent-loop/src/index.ts` 的 resume 路径：

```ts
const persisted = coldRead.events
const closers = interruptedTurnClosers(persisted)
if (closers.length > 0) await handle.append(closers)
```

顺序很重要：先拿写句柄，排除并发 resume；再冷读物理日志；再算 closers；再把 closers 当成一个普通 batch 追加；最后用 `[...persisted, ...closers]` 建 seed。补写会真正写盘，不只改内存里的视图。

`openTurnClosers` 从头扫一遍事件，只跟踪三个游标：openTurn、openStep、未答复的 tool call。扫完如果 turn 还开着，就按下面的顺序生成合成事件：

1. 每个未答复的 tool call 补一条 `tool/result`，`isError: true`；
2. 如果 step 还开着，补 `step/end`；
3. 补 `turn/end { reason: { kind: 'interrupted' } }`。

tool result 必须排在 step/end 和 turn/end 前面。Provider 要求每个 tool_use 后面跟一个 tool_result；只补 turn/end 的话，下一轮请求会带着悬空的 tool_use 被拒绝。合成结果有两种错误码：

- `TOOL_OUTCOME_UNKNOWN`：`tool/call` 已经记录，工具可能已经产生副作用；
- `TOOL_NOT_STARTED`：只有 assistant 的 tool_use，没有 `tool/call`，工具肯定没开始。

文案会进入下一轮上下文，直接影响模型行为。`OUTCOME_UNKNOWN` 明确写着「结果未知，只读或幂等才重试；有副作用先核实外部状态或问用户，不要盲目重试」。`NOT_STARTED` 则允许在仍然需要时重试。

`interrupted` 与 `forked` 共用这套算法，只是 cause 不同。前者是崩溃恢复，后者是从一个还开着的 turn 里 fork 出子会话。两者的文案和 `turn/end` reason 不同，因为模型需要知道「是进程死了」还是「父会话可能已经做完了」。

修复是幂等的。再崩一次也没关系：下次扫描看到 tool result 已存在、step 已关闭，就只补还缺的那几条。`handle.append(closers)` 是一批写；即使只写进去一部分，下一次也会从实际状态继续。

只读路径也补。`session-query` 的 cold read 在内存里拼上同样的 closers，不写盘。查看器、导出、统计看到的历史都是平衡的，而磁盘上的文件保持原样，等真正 resume 时才修。

{{< fig "diagrams/03-repair-flow.svg" "openTurnClosers 的恢复流程：先补 tool/result，再补 step/end，最后补 turn/end(interrupted)；扫描实际日志，幂等。" >}}

## 6. 六个切断点

把一轮拆成事件，逐个切断：

| 切在哪 | 磁盘上可能最后一条 | 恢复补什么 |
|---|---|---|
| turn/start 之前 | 上一条无关事件 | 无，这一轮没开始 |
| turn/start 之后、step/start 之前 | turn/start | turn/end(interrupted) |
| 模型请求已发、assistant/message 未落盘 | request/context（或 step/start） | turn/end(interrupted) |
| tool/call 已落盘、tool/result 未落盘 | tool/call | tool/result(OUTCOME_UNKNOWN) + step/end + turn/end |
| tool/result 已落盘、step/end 未落盘 | tool/result | step/end + turn/end(interrupted) |
| step/end 已落盘、turn/end 未落盘 | step/end | turn/end(interrupted) |

最后一行最容易混淆。`turn/end` 可能已经在内存里，但还在 200ms 缓冲里；磁盘上看到的和「turn/end 还没发生」完全一样。恢复只能写 `interrupted`，因为日志无法证明这一轮是正常跑完的还是被杀的。这是「闭合一定发生、原因可能不精确」的代价。

还有一个窗口在工具内部：`tool/call` 落盘、工具体执行到一半。恢复会补一条「结果未知」的 tool result，但不会重放工具。外部副作用是否发生，日志不知道，只能让模型或用户去核实。

{{< fig "diagrams/04-crash-windows.svg" "三个代表性切断点：工具执行中补 3 条，tool/result 之后补 2 条，step/end 之后补 1 条。切得越晚，结束原因越难证明。" >}}

## 7. Claude Code：消息链，没有括号

Claude Code 的会话文件在 `~/.claude/projects/<project>/<uuid>.jsonl`。记录类型包括 `user`、`assistant`、`attachment`、`system`，消息之间用 `uuid` / `parentUuid` 串成链。没有 `turn_start` 或 `turn_end` 这种生命周期事件。

它有一个可选的 `system/turn_duration`，带 `durationMs` 和 `messageCount`：

```json
{"parentUuid":"01ccfa6b-...","type":"system","subtype":"turn_duration","durationMs":875,"messageCount":6,"timestamp":"2026-11-08T11:31:52.811Z","uuid":"f52d2572-..."}
```

这个记录在样本里只出现在 `entrypoint: "cli"` 的交互式会话；`-p` / SDK 模式的三次实验都没有写，本地命令轮不写，崩溃轮也不写。它的用途是 resume 时做一致性遥测：拿 `messageCount` 和当前链长算差值，上报 `tengu_resume_consistency_delta`。它不是闭合标记。

流式输出里的 `result` 才是调用方看到的结束信号。正常结束时 `subtype: "success"`；SIGINT 中断时仍然有 `result`，`subtype: "error_during_execution"`、`terminal_reason: "aborted_streaming"`。但 `result` 只走 stdout，不写进 jsonl。

三条路径的具体行为：

**正常结束**：jsonl 末尾是 assistant 加可选的 `turn_duration`；stdout 收到 `result/success`。

**用户中断（SIGINT）**：CLI 当场往 jsonl 追加一条 user 消息，内容是 `[Request interrupted by user]`；stdout 收到 `result/error_during_execution`。闭合靠这条标记消息，不靠 turn 事件。

**硬崩溃（SIGKILL）**：jsonl 末尾停在一条裸的 user 消息上，没有 assistant，没有 `turn_duration`，没有中断标记；stdout 只有 `system/init` 和 `system/status`，没有 `result`。在 `-p --resume` 的实测里，Claude Code 会在悬空的 user 消息后补一条：

```json
{"type":"assistant","parentUuid":"2ecd5f6a-...","message":{"model":"<synthetic>","content":[{"type":"text","text":"No response requested."}],"stop_reason":"stop_sequence"}}
```

然后才追加新的 prompt。第二次 resume 不会再补第二条。工具调用崩在中间时，二进制字符串里有对应的 `[Tool call interrupted: ... its outcome is unknown ...]` 文案；恢复时补的是消息，不是重放工具。这一点只有二进制字符串证据，没有构造出真实的落盘样本。

Claude Code 的闭合是消息层的：一条 synthetic assistant 把「没有回复」补成「有回复」，让链继续往下走。它没有 turn 级的开闭记录，也没有中断原因字段。想知道某一轮是正常结束还是被杀，只能看链的形状、有没有 `turn_duration`、以及 stdout 有没有 `result`。

它的控制面在流上，日志只负责消息历史。这个分工对 REPL 和 SDK 够用：调用方从流里知道这一轮什么时候结束。代价是日志本身不携带 turn 边界，后处理只能推断。

## 8. 两种设计的取舍

| | DSH | Claude Code |
|---|---|---|
| 结束标记 | `turn/end` 事件，带 reason | 可选的 `turn_duration`；流式 `result` 只在 stdout |
| 崩溃后的尾巴 | 可能只有 `turn/start` 或 `tool/call` | 可能只有一条 user 消息 |
| 恢复动作 | 补 tool/result、step/end、turn/end，持久化 | 补一条 synthetic assistant，消息层 |
| 可审计性 | 能数出每一轮的边界和结束原因 | 只能从消息形状推断 |
| 复杂度 | 事件协议、seq、修复算法 | 消息链、加载期结构修复 |

DSH 把闭合做成协议，代价是每条事件都要维护 seq，恢复要理解 turn/step/tool 三层边界。Claude Code 把闭合做成消息，代价是「结束」没有单一真相：`turn_duration` 可选、`result` 不落盘、崩溃恢复靠一条语义上并不准确的填充消息。

两种设计服务于不同的消费方。Claude Code 的消费方主要是 REPL 和 SDK，流式输出本身就能告诉调用方什么时候结束；DSH 的消费方包括 UI、fork、统计、重放、子会话，必须从日志里独立重建状态。日志承担的责任越多，闭合就越需要显式。

## 9. 给自己的 Agent 的检查清单

1. 单一出口。turn 的所有路径都经过同一个 finally，reason 用穷尽类型，别让某个 return 漏掉。
2. 副作用前落盘。模型请求和工具执行之前 flush，日志里先有「我要做什么」。
3. 恢复先补 tool result，再补 step/end 和 turn/end。顺序反了，provider 会拒绝下一轮请求。
4. 区分 crash、fork、user abort。别把补写的结束标成 completed；模型和统计都需要知道这是事后推断。
5. 修复幂等，只读路径也补。扫描实际日志决定补什么，不要依赖「上次修到哪」的额外状态。

## 10. 仍然不闭合的情况

- 会话再也不打开：文件里会一直留着未闭合的 turn。读取方在内存里补，写回要等真正的 resume。
- 日志后端不可用：连 `turn/start` 都没写进去，就没有可修复的尾巴。没有日志，就没有真相。
- 修复过程中再次崩溃：torn tail 被截断，下次重新扫描；幂等保证了不会重复补。
- 插件自己的括号：DSH 的崩溃修复只关 turn/step/tool，不碰 `compaction/start` 这类插件事件。插件要自己定义恢复语义。
- 结束原因不精确：`turn/end` 可能已经发生但没落盘，恢复会写 `interrupted`。闭合成立，语义是推断的。

---

相关阅读：[一轮到底怎么结束：turn、step 与尝试的三层状态机](/posts/dsh-turn-step-state-machine.html) ｜ [会话日志与事件溯源](/posts/dsh-session-event-sourcing.html)
