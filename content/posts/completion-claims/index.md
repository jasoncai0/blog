---
title: "谁说的「做完了」：agent 的完成判定"
date: "2026-11-11T20:34:00+08:00"
summary: "agent 说「完成了」的时候，谁在签字？拆开两套 harness 会发现：完成是个事件，不是事实。OpenCode 由模型触发，没有检查站；Claude Code 在模型之外还留了四个可以驳回的位置——但四个位置加起来，仍然只回答「有没有检查过」，不回答「对不对」。"
description: "对照 OpenCode 与 Claude Code 的完成判定：L0 模型自述、L1 harness 结构、L2 外部证据，以及为什么最后一层最难补。"
tags: ["harness", "agent", "architecture"]
slug: "completion-claims"
---

> 依据：OpenCode `2.2.5`（commit `a687c047`）源码，Claude Code `2.1.321` 二进制中抽出的提示词与工具描述。Claude Code 没有可读源码，涉及其内部判断的地方都标了推断与依据。

上一篇讲计划住在哪里，结尾留了一个问题：**存下来的计划，什么时候会被重新检查一遍？**

这一篇换个角度问同一个问题：**"做完了"这句话，是谁说的？**

## 0. 先说结论

完成不是一个事实，是一个**事件**。事件由谁触发，决定了它值多少。

{{< fig "diagrams/01-three-layers.svg" "三层判定：模型说、harness 说、世界说。所有 agent 都有前两层，差别在有没有第三层，以及第三层能不能否定第一层。" >}}

| 层 | 谁签字 | 判据 | 成本 |
|---|---|---|---|
| L0 | 模型 | 这一轮不再要求调用工具 | 0 |
| L1 | harness | 步数、预算、输出长度、中断 | 0 |
| L2 | 外部世界 | 测试、日志、评审、hook | 一次真实执行 |

三句话：

1. **L0 和 L1 说的是"停"，不是"对"。** 停下来的那一轮可以什么都没做完。
2. **两者的 L0 完全一样**：模型不再要工具，循环就认为结束了。
3. **差别在 L2**。OpenCode 把这件事留给了用户和插件；Claude Code 在四个位置埋了钩子，但没有一个能回答"这次改动是否正确"。

## 1. 完成是一个事件

先把"完成"这个词拆开。它至少指三件不同的事：

- **停下来了**——这一轮不再产生 token 或工具调用。
- **做完了**——需求里的每一项都落地了。
- **做对了**——落地的东西符合预期，且没有破坏别的。

agent 系统里，第一件是**结构可以判定的**，第二件需要**规格**，第三件需要**证据**。麻烦在于：日常对话里这三个意思共用一个词，于是"停下来了"经常被当成"做对了"。

## 2. OpenCode：模型停手就算完

### 2.1 判据只有一条

一个 Step 结束时，`step.ts:297` 这么算：

```ts
const needsContinuation =
  input.prepared.request.toolChoice?.type !== "none" && record.needsContinuation
if (record.finish?.finish === "length") return Outcome.OutputLimit({ needsContinuation })
return Outcome.Completed({ needsContinuation })
```

而 `record.needsContinuation` 的定义在 `publish-llm-event.ts:597`：

```ts
needsContinuation: Iterable.some(
  tools.values(),
  (tool) => !tool.providerExecuted && (tool.called || tool.settled),
)
```

翻译过来：**这一轮只要有过一个非 provider 执行的工具调用，就还要继续；一个都没有，就结束。**

`Completed` 且 `needsContinuation === false`，就是"做完了"的全部含义。

{{< fig "diagrams/02-opencode-done.svg" "从流结束到会话空闲，四步里没有一步去看改动对不对。" >}}

### 2.2 这条路上没有检查站

drain 收工的条件（`llm.ts:175-186`）是：非 force、非 continuation、没有待投递项。它检查的是**还有没有活要干**，不是**干得对不对**。

所以整条路径上：

- 没有编译，除非模型自己想起要编译；
- 没有测试，除非模型自己决定跑；
- 没有 diff 复核，除非用户自己看。

我核过一遍：`packages/core/src` 里（排除测试）所有 `verif` 命中都在 OAuth / JWT 的 `verifyIDToken`、`code_verifier`，**没有任何一条与"验证工作成果"有关**。这不是疏漏，是定位——OpenCode 假设用户在场，验证是人的事。

### 2.3 唯一被强制的"完成报告"，是"没完成"的那份

步数用尽时（`llm.ts:269`），harness 追加一条系统提示并把 `toolChoice` 置为 `none`：

```text
CRITICAL - MAXIMUM STEPS REACHED
...
2. MUST provide a text response summarizing work done so far
...
- Statement that maximum steps for this agent have been reached
- Summary of what has been accomplished so far
- List of any remaining tasks that were not completed
- Recommendations for what should be done next
```

之后任何工具调用都会被 `step.ts:99` 直接拒掉：`"Tools are disabled after the maximum agent steps"`。

这段提示词很好笑也很诚实：**整套系统里唯一强制要求的完成陈述，是"哪些还没做完"**。正常的收工没有任何报告要求。

### 2.4 谁在检查：`/goal` 只查进展，不查对错

`/goal` 是唯一会主动问"还该不该继续"的机制。它的评审模型返回五个判词（`goal.ts:31`）：`done / continue / blocked / paused / wait`。

但看它的停滞判定就明白了（`goal.ts` 的 `MAX_STALLS = 3`）：**这一轮的理由和上一轮相同，或者这一轮一个工具都没调，就算停滞**。

这是"有没有动"，不是"动得对不对"。一个把同一处代码改坏三次的 agent，在它眼里是"一直在动"。

## 3. Claude Code：把验证写进计划里

### 3.1 动手之前，先定"怎么算成了"

`ExitPlanMode` 对计划内容有一条明确要求（二进制字符串）：

```text
Include a verification section describing how to test the changes end-to-end
(run the code, use MCP tools, run tests)
```

这条比它看起来重要：它在**执行之前**就把"怎么验证"固定下来。计划获批时，验证方法也一起获批了。

代价是它仍然只是计划里的一段文字。模型可以在实现时把这段忘掉，或者写一段"运行测试"了事。

### 3.2 主动性档位会改写收尾方式

同一份二进制里，主动性（proactivity）的三档给出了三种收尾标准：

```text
proactive: When a task finishes, ... run the tests and fix what fails,
           fix lint and type errors, update affected docs, tie off loose ends
default:   Make the edits and run the checks the task needs without stopping to ask.
ask:       When a task is done, report back and stop; suggest next steps rather than starting them.
```

注意第三档：**做完就停下报告，建议下一步而不是直接做**。同一件事——"要不要顺手把测试跑了"——在三档里是三个默认值。

同一段里还有一句被低估的话：

```text
Never run `git commit`, push to a remote, or create or update a pull request
unless the user explicitly confirms it in this conversation.
Finishing a task is not confirmation.
```

它把**完成**和**授权**拆成了两个独立判断。这是整个完成判定里最容易被忽略的一处设计：做完不等于可以做下一步不可逆的事。

{{< fig "diagrams/03-veto-points.svg" "四个可以驳回「做完」的位置：计划期、执行期、收尾期、事后。" >}}

### 3.3 收尾期：hook 能把结束挡回去

这是唯一一层能**强制模型继续**的机制。Stop hook 在模型准备结束这一轮时运行，可以拒绝结束；界面上会出现：

```text
A hook blocked the turn from ending
```

配套还有防失控的两样东西：`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` 限制连续阻止的次数，`stop_hook_active` 让 hook 在被重入时直接放行。

这层的语义很关键：**它能要求"继续"，不能判断"对不对"**。一个只会说"再检查一遍"的 hook，会把模型推进无限自审，所以必须有上限。

### 3.4 事后：对抗验证只活在一个子系统里

研究/深研子系统里有一组验证代理，字符串里能看到它的工作方式：

```text
a few finders, single-vote verify
claims refuted by adversarial verification
verifier agents failed; neither confirmed nor refuted
```

注意最后一句——**验证失败时明确说"既没确认也没否认"**，而不是默认通过。这种"无法判定"的状态被显式建模，是很少见的严谨。

但它只服务于研究流程。写代码的主循环里没有对应的机制。

### 3.5 目标判定：有，但读不到细节

二进制里存在 `tengu_goal_achieved`、`tengu_goal_failed`、`evaluator_timeout` 这组遥测名，说明 Claude Code 也有一个目标评估器——和 OpenCode 的 `/goal` judge 是同一类东西。

它的判定提示词我没能从字符串里还原出来，**标记为未确认**。能确定的只有：它存在，有超时，且结果分"达成/未达成"两种。

## 4. 为什么第三层这么难补

### 4.1 规格常常不在场

"做完了"要成立，得先有"要做什么"。多数请求只有一句话，规格藏在用户的脑子里。agent 只能按自己的理解补全，于是它判定的"完成"是**对自己理解的完成**。

### 4.2 测试是模型自己写的

这是最尴尬的一处。让模型写测试再跑测试，等于让它自己出题自己判卷。测试能挡住"手滑"，挡不住"理解错了"——理解错了的时候，测试也会跟着错。

真正的外部证据得来自**不是这次会话产生的东西**：已有的测试套件、线上日志、别人的评审。

### 4.3 环境经常不完整

跑不起来的环境里，"验证"会退化成"看起来对"。这时模型说"测试通过"可能只是因为它根本没跑成——这也是为什么"无法判定"应该是一个显式状态，而不是默认成功。

## 5. 三个设计问题

如果你在做一个 agent，完成判定至少要回答这三个：

1. **完成的默认值是什么？** 没有证据时算完成还是算未完成？OpenCode 默认算完成（模型停手即结束），Claude Code 的验证器默认算"未确认"。
2. **谁能驳回？** 只有模型自己，还是允许外部（hook、评审、CI）说"不行，继续"？驳回的成本有多高？
3. **完成之后呢？** 完成是不是就等于可以提交、可以发布？Claude Code 用一句 `Finishing a task is not confirmation` 把两者拆开了，这个拆法值得抄。

## 6. 和上一篇的关系

上一篇说，计划存下来不等于还对不对；这一篇说，完成被宣布不等于真做对了。

两者是同一个缺口的两面：**agent 能把"做事"变成事件，却很难把"做对"变成事实**。事件可以零成本产生，事实必须有外部世界参与。

所以这一系列里反复出现的那个判断依然成立：harness 的价值不在循环写得多巧妙，而在它愿不愿意、以及有没有能力，**在关键的地方引入一个自己无法伪造的检查**。

## 7. 证据索引

| 结论 | 证据 | 等级 |
|---|---|---|
| `Completed` 的判据是没有待续工具调用 | `.opencode-src/packages/core/src/session/runner/step.ts:297-299` | 源码 |
| `needsContinuation` 的精确含义 | `publish-llm-event.ts:597-600` | 源码 |
| 收工条件不看正确性 | `llm.ts:175-186` | 源码 |
| 步数用尽时的强制报告 | `max-steps.ts:1-24`、`llm.ts:269`、`step.ts:99-101` | 源码 |
| core 里没有"验证成果"的代码 | `grep -ri verif packages/core/src`（排除 test）全部命中 OAuth/JWT | 源码 |
| 计划需含验证方法 | 二进制 `2.1.321`，ExitPlanMode 计划要求 | 二进制 |
| 三个主动性档位的收尾标准 | 二进制 `2.1.321`，proactivity 提示词 | 二进制 |
| 「完成不等于授权」 | 同上，`Finishing a task is not confirmation` | 二进制 |
| Stop hook 可阻止结束 | 二进制，`A hook blocked the turn from ending` / `STOP_HOOK_BLOCK_CAP` | 二进制 |
| 对抗验证与「无法判定」状态 | 二进制，研究子系统的验证代理文案 | 二进制 |
| 目标评估器存在 | 二进制遥测名 `tengu_goal_achieved` / `tengu_goal_failed` / `evaluator_timeout` | 二进制（细节未确认） |

系列前两篇：[《计划住在哪里》](/posts/plan-carriers.html)、[《OpenCode 的执行机制》](/posts/opencode-execution.html)。
