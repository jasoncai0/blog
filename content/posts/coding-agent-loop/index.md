---
title: "Coding Agent 的循环护栏：从 MAX_STEPS 到反思机制"
date: "2026-11-16T18:28:00+08:00"
summary: "MAX_STEPS 只说明循环结束，不说明工件是否成立。Jade 最终去掉默认轮次上限，用 8/16 轮无进展反思、同参数 4 连和命令形状 8/10 三层护栏兜底，并保留真实的编译验证门。"
description: "从 Go 快排和 Spring Boot 两个 MAX_STEPS 案例出发，解释 Jade 的反思机制、命令指纹归一化、工具/技能发现和编译验证门的边界。"
tags: ["harness", "codegen", "architecture"]
slug: "coding-agent-loop"
---

> 系列：[总览](/posts/coding-agent-pitfalls.html) · [循环护栏](/posts/coding-agent-loop.html) · [执行环境](/posts/coding-agent-sandbox.html) · [会话与历史](/posts/coding-agent-sessions.html)

这篇拆自[总览](/posts/coding-agent-pitfalls.html)，专门讲“循环什么时候停、为什么停”。核心不是把轮数调大，而是把失败拆成可执行的判据。

## 1. 循环为什么停：`MAX_STEPS` 会掩盖两种完全不同的失败

这是最容易被误判的一个坑。

第一次跑 Go 快排，任务结束在 `MAX_STEPS`，10/10 轮，48,272 tokens。看起来像“代码没写出来”。打开工作区才发现，`go build`、`go test`、`go run` 全部通过，模型甚至补了随机数组测试；它只是没来得及写最终回答。

把上限调到 12 再跑一次，9/12 轮，`SUCCEEDED`，200 个随机数组与 `sort.Ints` 结果一致。

同一种终态，换到 Spring Boot 就完全是另一回事。`run-d28944ce` 也是 10/10，但验证结果是 `Maven compile failed`，真实错误是：

```text
java.nio.file.FileSystemException:
/Users/caizhensheng/.m2/repository/org/springframework/boot/spring-boot-maven-plugin/3.5.7:
Operation not permitted
```

这不是“再多给几轮就能写好”的问题。Maven 缓存不可写、网络被拒、JDK 临时目录越界，任何一项都会让构建失败。

{{< fig "diagrams/02-max-steps-triage.svg" "同样是 MAX_STEPS：Go 快排的工件已经成立，缺的是最终回答；Spring Boot 的工件未成立，根因在沙箱边界。先读 verification、events.jsonl 和工作区，再决定补轮数还是修环境。" >}}

现在我的检查顺序固定为：

1. 看 `RunSnapshot.verification`，它是最后一个真实命令的结论。
2. 看 `.agent/events.jsonl` 的 `tool.end`，确认最后执行了什么。
3. 进工作区手动跑构建/测试，确认工件是否存在。
4. 最后才看 `MAX_STEPS` 和 token 数。

`MAX_STEPS` 是“循环结束”，不是“代码失败”。

**2026-11-16 更新：** 项目已移除默认轮次上限，`AGENT_MAX_STEPS=0` 表示不限制轮数；正数仍可作为显式上限。默认不设 token 预算（`AGENT_TOKEN_BUDGET=0`），需要成本上限的无人值守任务可显式设置。终态增加 `BUDGET_EXCEEDED` 和 `LOOP_DETECTED`；`MAX_STEPS` 只在使用正数上限时出现。

这个取舍和主流实现一致：Claude Agent SDK 的 `max_turns`、`max_budget_usd` 默认都是 `None`，agent loop 以“模型不再调用工具”为正常终点；DSH 默认 profile 里是 token-meter 与上下文压缩，硬预算由可选的 per-turn 插件提供，不是 base 的默认行为。总 token 预算适合无人值守/CI 的成本上限，不适合作为交互式长任务的默认刹车。

取消预算后很快遇到反例：旧会话 `run-2952644c` 在一条追问里连续 15 分钟用 `javap ... | sed -n '60,80p'` 翻字节码，每次参数只改行号，四连重复检测抓不到；事件日志涨到 96 MB、641 步，最后 Jade 进程被系统 `SIGKILL`。补救不是把 token 预算加回来，而是把“循环”拆成三种不同的失败，分别加护栏：

1. **完全相同**：同名工具、完全相同参数连续 4 次，执行前拒绝。
2. **形状相同**：`run_command` 去掉数字差异后的命令形状，在最近 10 次里出现 8 次，执行前拒绝。`sed -n '60,80p'` 和 `sed -n '80,100p'` 属于同一种形状。
3. **长时间无进展**：有工具调用，但既没有新增文件，也没有新的验证通过。连续 8 轮触发反思检查点，16 轮确定性停止。

前两层是硬护栏；第三层先给模型一次结构化自证的机会，再决定是否止损。

### 1.1 反思机制：无进展时先让模型自证

反思不是另起一个“裁判模型”，也不是再跑一次任务摘要。它只做两件事：在同一个会话里注入一条结构化的 `REFLECTION CHECKPOINT`，以及提供一个不修改工作区的 `reflect` 工具，让模型把“为什么卡住”写成可检查的字段。

触发判据很窄：

```java
boolean madeProgress = !state.files().equals(filesBefore)
        || (!verifiedBefore && context.verified());
```

也就是说，只有**文件列表新增/删除**，或**上一次没通过、这一次通过了验证**，才算进展。读文件、跑失败的诊断命令、重复 `list_files` 都不算。这个判据故意保守：它不评价模型“有没有思考”，只评价工作区有没有变化。

{{< fig "diagrams/04-reflection-loop.svg" "反思机制不是让模型自由发挥，而是三层确定性护栏：8 轮无进展注入检查点，16 轮停止；执行前还有同参数 4 连和同命令形状 8/10 两次拦截。reflect 工具只记录诊断、证据、尝试和下一步，不修改工作区。" >}}

### 检查点：8 轮把“别再重复”变成一条必须回答的提示

连续 8 个工具轮次无进展时，`AgentLoop` 往对话里追加一条 user message，而不是调用另一个模型：

```text
REFLECTION CHECKPOINT
You have made no workspace change and produced no new verification
for 8 consecutive tool turns. ...
Call the `reflect` tool now with:
- diagnosis
- evidence
- attempts
- next_action
- should_stop
```

同一段连续无进展里只注入一次，避免每轮刷屏。模型下一轮会看到这条消息；如果它还有别的策略，应该在这里给出一个具体的 `next_action`，而不是继续换行号切同一个文件。

`reflect` 工具是第 13 个内置工具，schema 要求五个字段：

| 字段 | 用途 |
|---|---|
| `diagnosis` | 当前阻塞是什么 |
| `evidence[]` | 命令/文件里的具体证据 |
| `attempts[]` | 已经试过什么 |
| `next_action` | 一个能改变结果的下一步 |
| `should_stop` | 是否必须等用户输入 |

它不修改工作区，也不需要权限审批；每次调用会追加 `.agent/reflections.jsonl`，并发 `reflection.recorded` 事件。UI 把 `reflection.requested` 和 `reflection.recorded` 都渲染成系统卡片，长会话里能直接看到模型在哪一轮开始绕圈、它自己怎么解释。

但 `reflect` 本身**不算进展**。它只把下一步具体化；只有真正写文件或通过验证，`noProgressTurns` 才会清零。`should_stop=true` 也只是模型的自述，不会直接触发框架停止——真正的止损仍然是 16 轮的确定性条件。

### 止损：16 轮反思无效就停

如果 16 个连续工具轮次仍然没有文件变化和新验证，`AgentLoop` 设置 `LOOP_DETECTED`，错误信息明确写出“连续 16 轮无进展”，保存工作区和 transcript 后结束。它不是“模型觉得该停”，而是框架自己停。

这层的价值不在于让模型变聪明，而在于把失败变成可解释的终态。以前是 641 步、96 MB events 后被系统 `SIGKILL`；现在是 16 轮、一个有 `reason=no_progress` 的 `loop.detected` 事件，工作区还能继续检查或追问。

### 这套机制的边界

- 它检测的是“工具轮次无进展”，不是“回答质量差”。模型可能连续读 15 轮才形成正确判断；8 轮检查点会打断它，但不阻止它继续。
- `madeProgress` 只看文件列表和验证状态，不比较文件内容。修改已有文件但尚未验证时，可能被算作无进展；这是有意的保守。
- `reflect` 的质量取决于模型。框架只能保证“要求它自证”和“无效时停止”，不能保证诊断一定正确。
- 命令形状归一化只处理 `run_command` 的数字差异；真正的语义循环仍靠无进展计数和 16 轮上限兜底。


## 2. 工具/技能“发现”不是有文件就行

工具注册用了双通道：

- 内置工具在 `BuiltInTools` 里显式注册。
- 外部工具用 `ServiceLoader<ToolProvider>` 发现。

测试里踩了一个很典型的坑。ServiceLoader 的服务文件写的是 `EchoToolProvider`，实际嵌套类是 `EchoProvider`，编译直接报：

```text
ToolRegistryTest.java:[41,17] 找不到符号
  类 EchoToolProvider
```

生产代码不会因为名字写错而报错，只会少一个 provider。因此注册表必须有：

- 重名拒绝。
- 外部 provider 与内置工具冲突拒绝。
- 发现数量断言，而不是只断言“能跑”。
- 一个真实 `META-INF/services` 测试文件，覆盖发现路径。

技能是另一套：`SKILL.md` 在启动时扫描，解析 frontmatter；目录清单进系统提示，按任务关键词选 top 2 全量注入；模型还可以用 `load_skill` 按需加载。技能不是“有 markdown 就会生效”，它同时依赖发现、排序和注入。


## 3. 验证器只证明编译，不证明运行

最早 `run_java` 在编译成功后就把 `verified` 置为 true，然后才启动 Java 进程。结果是：如果程序运行时报错，工具会返回错误，但模型可以立刻结束，验证器看到 `verified && !dirty`，说“已经验证过”，最终状态仍可能是 `SUCCEEDED`。

这暴露了一个边界：**编译、运行、测试是三件事**。

要真正收口，至少要做到：

- `run_java` / `run_code` 的 exit code 非 0 不标 verified。
- 任务要求执行时，验证门必须包含真实运行。
- 任务要求测试时，测试命令必须显式执行；没跑测试不能声称测试通过。

本项目目前仍停留在“编译成功就标 verified”，运行失败可以被模型忽略。这一条还没有完全修完，先把它写进坑里，避免下一次又把 `SUCCEEDED` 当成行为正确。

这也是 `MAX_STEPS` 之外的第二类误判：`SUCCEEDED` 也可能只代表“编译过”，不代表“行为对”。



---

系列其他文章：[总览](/posts/coding-agent-pitfalls.html) · [执行环境](/posts/coding-agent-sandbox.html) · [会话与历史](/posts/coding-agent-sessions.html)
