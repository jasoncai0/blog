---
title: "反思不是再想一遍：Agent 反思机制的证据、归因与沉淀"
date: "2026-11-11T16:51:00+08:00"
summary: "把 agent 的 reflection 拆成三问：看到了什么新证据、错在哪一层、留下了什么。讨论 oracle 谱系、归因诊断、批判者的信息食谱、结构化裁决、回路预算与经验污染，附十张图、一段可落地伪代码、一张上线检查清单，以及 OpenCode 源码里的五个接缝。"
description: "Agent 反思机制的工程拆解：证据、归因、沉淀三问，对照 OpenCode 源码看真实 harness 的接缝与缺口。"
tags: ["agent", "architecture", "planning", "harness"]
slug: "agent-reflection"
---

> 这不是一篇论文综述，它讨论一个具体的工程问题：agent 在一轮里失败了，怎么让下一轮真的不一样。文中引用 Reflexion、Self-Refine、CRITIC 这些工作，只取它们的机制与作者报告的数字，不做横向排名；所有阈值都是默认值，不是实测最优。后半段拿 OpenCode 的源码做对照，看这些机制在真实 harness 里长什么样。

## 0. 先说结论

- **反思的收益是四个概率的乘积**：有缺陷、证据能暴露它、归因正确、结论被下一次消费。任何一项接近零，整条回路就是剧场。
- **没有新证据的反思是重采样。** 同一上下文再问一遍，得到的不是批评，是第二次表态——通常更自信，不是更正确。
- **先造 oracle，再谈反思。** 测试、类型检查、编译器、schema、环境返回码，比任何「请你仔细检查」都值钱。
- **反思真正的工作是归因。** 错在实现、计划、工具还是环境，决定了下一步是改一行、换方案、修工具，还是问人。
- **修补与经验是两件产物。** 只修补是带解说的重试，只写经验是日记。前者不改变未来，后者不解决当下。
- **反思不是免费的。** 多一轮就多一次改坏的机会；接受修补前必须跑回归。
- **大多数团队测错了。** 「有反思 vs 无反思」里赢的，常常是「重试 + 新证据」，不是那段反思文字。

## 1. 三个现场

测试红了。agent 说「我再检查一遍」，然后把同一个函数从头到尾重写了一遍：变量名换了，注释多了，分支还是原来的分支。第二轮，同一个断言失败。

第二个场景里，它先读了失败输出——`AssertionError: b'PNG' != b'CSV'`，回去改了导出分支。绿了。

第三个场景，测试全绿。agent 写了一段反思：「实现覆盖了导出、格式选择与错误处理，符合需求。」但用户说的「导出」是 CSV，它做的是图片。测试是它自己写的，断言只有一句：文件存在。

三段都可以被叫做 reflection。第一段是重采样，第二段是修复，第三段是自我确认。差别不在模型愿不愿意想，而在三个量：它看到了什么原本没看到的东西；它把错误定位到了哪一层；它的结论有没有活到下一次。

后面三节按这三问展开，再往后是让它们成立的两件工程：批判者看什么，回路什么时候停。

## 2. 反思是四件事

「反思」这个词被用得太宽。这里给它一个窄的定义：

给定一条轨迹 τ 和一份证据 e，反思是一个映射 f: (τ, e) → (Δa, Δm)。Δa 是当前任务的修补，Δm 是跨任务的经验。

三个部件缺一不可：

- **证据**是唯一的信息入口。没有 e，f 只是从模型先验里再采样一次；它可能更长、更有条理，但不会更接近事实。
- **归因**是中间那一步，决定 Δa 改哪里。没有归因，修补就是试错；试错不是不行，但它不该叫反思。
- **沉淀**是出口。没有 Δm，每个任务都从零开始，系统不会因为做过一百次同类任务而变好。

{{< fig "diagrams/01-loop.svg" "反思的完整回路：尝试产出轨迹，oracle 产出证据，裁决与归因合成修补，经验从归因分叉出去，进入下一次任务的上下文。少了证据这一格，剩下的部分叫自我确认。" >}}

修补与经验可以只要一个，但不能两个都没有。只要修补，是一次带解说的重试；只要经验，是一篇日记。两种都常见，两种都不改变系统的斜率。

和相邻机制的区别，最好一次性划清：

| 机制 | 输入 | 输出 | 改变什么 |
| --- | --- | --- | --- |
| retry | 错误 | 同一动作再来一次 | 采样运气 |
| replan | 失败 | 新计划 | 当前任务的路径 |
| self-consistency | 同一个问题 | 多条路径投票 | 单次答案的分布 |
| **reflection** | 轨迹 + 证据 | 修补 + 经验 | 下一步 + 未来 |
| memory | 交互 | 可检索的事实 | 上下文 |
| finetune / RL | 大量轨迹 | 权重 | 模型本身 |

self-consistency 值得单说：它提高的是单次答案的稳定性；下一次遇到同类问题，它还是从零开始。reflection 的定义性特征是**改变下一次的行为**——不管改变的是这一轮的下一步，还是下一轮任务的提示。

## 3. 收益是四个概率的乘积

把一次反思的期望价值写成乘法：

```text
E[V] = P(有缺陷) · P(证据暴露它) · P(归因正确) · P(结论被消费) · V(修复) − C(反思)
```

逐项看它们怎么坏掉：

**P(有缺陷)**。不是每一轮都需要反思。流程要求「每轮结束必须写反思」，等于在没有缺陷的地方找缺陷，模型会开始编。编出来的缺陷比漏掉的缺陷更贵，因为它会触发一次真实的修改。

**P(证据暴露它)**。测试没覆盖的错误，critic 也看不见。它只能看见你给它的东西——这句话值得贴在每个反思 prompt 的开头。

**P(归因正确)**。看到了错误，但归错层。改了三行，问题在计划；换了方案，问题在一个边界条件。这一项最常被忽略，因为它无法从「反思文本写得好不好」里看出来。

**P(结论被消费)**。写进了日志，但下一次没检索到；检索到了，但没被采纳；采纳了，但用错了域。任何一环断掉，前面三项都白算。

乘积的意思是：任何一项接近零，其他三项再高也没用。这也解释了一个常见困惑——换个更强的模型来反思，为什么没变好。它提高的是措辞质量，不是证据的暴露率。

论文里的分歧也在这张表里。Self-Refine 用同一个模型做生成、反馈、修订，在 7 个任务上报告平均约 20 个百分点的绝对提升；它成立的前提是这些任务的输出「可读、可比较」，模型能看出粗糙的地方。Huang 等人在推理任务上发现，没有外部反馈的自我纠正常常掉点——错误答案和正确答案在模型眼里同样自洽，P(证据暴露它) 接近零。Kamoi 等人的综述把话说得更直：在提示模型自我纠正的研究里，除了极少数特别适合的任务，没有可靠证据表明它稳定奏效；可靠的外部反馈可以，大规模微调也可以。

这不是说 Self-Refine 错了，而是说它的适用面比标题窄。**反思的上限由证据决定，不由 prompt 决定。**

## 4. 证据：先造 oracle

{{< fig "diagrams/02-evidence-ladder.svg" "证据的六级谱系。越往上，信息增量越大、越难被模型自己编出来；越往下，反思越接近重采样。" >}}

六级，从强到弱：

1. **确定性验证器**：测试、类型检查、编译、schema 校验、diff 断言。它不解释，只判决。成本低、可重复，而且不可贿赂——除非你允许它改测试。
2. **环境真值**：退出码、HTTP 响应、浏览器 DOM、文件系统、游戏状态。比测试粗，但编不出来。
3. **工具检索**：文档、搜索、计算器、REPL。CRITIC 这一类工作做的就是「先查再批」：把可验证的事实拿进来，再让模型判断。
4. **独立模型 + 不同信息**：另一个上下文、另一个角色，甚至另一个模型。它带来的不是真值，是视角；只有它接触到 actor 没接触的信息时，才有增量。
5. **同模型 + 新上下文**：有点用，主要用来去锚定。信息增量有限。
6. **同模型 + 同上下文**：这不是反思，是第二次表态。

四条工程要点：

- **验收标准要可执行。** 如果需求只能写成「界面要好看」，就没有 oracle。这时候正确的动作是人审或采样对比，不是让模型自评。
- **错误信息要保真。** 给 critic 的应该是原始报错的尾部几十行、失败的断言、diff，不是 agent 自己总结的「刚才有个小问题」。总结会丢掉定位所需的那一行。
- **证据要能被反驳。** 「测试变绿了」可验证；「逻辑更清晰了」不可验证。后者不是证据，是评价。
- **让 oracle 便宜。** 跑一次测试 3 秒，反思就值；跑一次 30 分钟，预算会被它吃掉，人就会开始跳过它。

还有一个常被忽略的点：**证据的粒度要和归因的粒度匹配**。只给「测试失败」四个字，critic 只能猜；给 `test_export.py::test_csv_header:42, b'PNG' != b'CSV'`，它才能定位到分支。

反模式：把 agent 自己的总结当证据。「我检查了，之前的实现有问题」——这句话没有新信息，它只是把不确定性转移了一次。

## 5. 对象：四层粒度

{{< fig "diagrams/03-four-levels.svg" "四层对象按「反馈延迟 × 影响半径」排开。L0 用代码就能修；L3 要样本量、回放和灰度。最常见的错位，是从左下角的手法去修右上角的问题。" >}}

| 层 | 判据 | 典型信号 | 动作 | 频率 | 主要风险 |
| --- | --- | --- | --- | --- | --- |
| L0 动作 | 这次调用成功吗 | 退出码、异常、schema | 重试、改参数、换工具 | 每次调用 | 低 |
| L1 步骤 | 这一步推进了吗 | 世界状态变化、新信息 | 换下一步 | 每步 | 中 |
| L2 轨迹 | 这个方案解决任务了吗 | 端到端 oracle | 修补或换方案 | 每次尝试 | 高 |
| L3 策略 | 这类任务为什么总失败 | 多次轨迹的聚合 | 改提示、改工具、改流程 | 每 N 次 / 离线 | 很高 |

逐层说：

**L0 动作。** 一次工具调用。判据是返回值：异常、参数不合法、schema 不符。动作是重试、改参数、换工具。不需要模型，代码就能做。频率最高，单次价值最低。

**L1 步骤。** 一步推理加一组动作。判据是「世界变了吗」：文件改了没、新信息进来了没、失败集合变了吗。连续两步没有任何状态变化，就是原地打转。

**L2 轨迹。** 一次完整尝试。判据是端到端 oracle。只有这一层能回答「这个方案行不行」，也只有这一层能产出可复用的经验。

**L3 策略。** 一类任务的反复失败。判据是聚合：同一类失败在 N 次里出现 k 次。动作是改提示、改工具、改流程。频率最低，杠杆最大，也最容易过拟合。

两种错位最常见：

**用 L0 的手法处理 L2 的问题。** 测试红了，改一个参数；还红，再改一个参数。每一轮都在 L0，真正错的是方案。这是最常见的原地打转。判据很简单：同一层连续三次失败，强制升一层。

**用 L3 的手法处理 L0 的问题。** 一次工具报错，就去改系统提示词。三条轨迹里两条是环境抖动，改完提示词，下一批任务全被带偏。L3 需要样本量，也需要回放。

## 6. 归因：错在哪一层

{{< fig "diagrams/04-diagnosis.svg" "归因是一棵决策树，不是一段分析。红色那条是硬规则：同一层第三次失败，强制升一层。" >}}

归因不是「分析原因」这种笼统动作，它是一棵决策树。先问：有失败证据吗？没有，就不要反思，去拿证据。

有，再看证据指向哪：

- **工具边界报错、参数不合法、权限拒绝** → L0。修参数、换工具、补权限。
- **断言落在具体行为、可复现、影响局部** → L2 的实现。改代码。但先确认测试本身没写错。
- **测试全绿而验收标准没被覆盖，或三个不同实现都死在同一处** → 计划 / 规格。先补测试，再改实现；如果是规格歧义，问人。
- **本地过、CI 不过，随机失败，依赖版本漂移** → 环境。修环境，别让 agent 反思。

三种误判，各有各的现场：

**局部修补。** 同一层反复失败，diff 越来越大，红的测试从一个变成三个。它的机制是「改一个地方总能找到一点可改的」，所以永远有下一轮。

**推倒重来。** 一个边界错误，重写整个模块，然后重新踩一遍已经踩过的坑。它的机制是把「失败」当成了「方案错误」，而实际上只是「实现错误」。

**过度修正。** 环境抖一下，agent 把本来对的代码改错。「修复」之后，原本通过的测试红了。它的机制是归因到了最近的一个变化，而不是真正的原因。

一条硬规则，能把上面三种都压住：**每个修补都要附带一个可证伪的预测。**

写法是这样：「如果我把 `export.py:41` 的分支从写死 PNG 改成按扩展名选择，`test_csv_header` 会从红变绿，`test_png_export` 保持绿。」

预测不成立，不要在同一层换第二个补丁，直接升一层。理由是：同层第二个补丁的成功率明显低于第一个，而且它会掩盖「归因错了」这个事实。更实际的好处是，预测是可执行的——你不需要相信 critic 的置信度，跑一下就知道。

## 7. 批判者：信息食谱与结构化裁决

批判者的输出质量，一半由它的输入决定。

{{< fig "diagrams/05-critic-diet.svg" "批判者的信息食谱。作者的自我辩护和置信度被挡在门外，只有产物和原始证据能过闸；输出是裁决，不是重写。" >}}

| 给 | 不给 | 为什么 |
| --- | --- | --- |
| 任务与验收标准原文 | actor 的转述 | 转述会丢约束，也会预先解释 |
| 产物：diff、文件、输出 | actor 的自我辩护 | 它锚定 critic，让 critic 接受 actor 的框架 |
| 原始证据：失败测试、报错尾部 | actor 的置信度 | 谄媚的入口：模型看到「我很确定」，更容易同意 |
| 动作序列与结果 | 上一轮的批评全文 | 除非这一轮就是确认是否修复，否则会沿旧框架找问题 |
| 约束：不许改测试、预算 | 参考答案 | 它会把「像不像答案」当成「对不对」 |

Panickssery 等人的实验值得单独提：模型能识别自己的生成，并系统性地偏好它；把作者身份藏起来，偏好会下降。工程上的对应做法不只是「换个模型」——同家族的两个模型共享训练分布，偏好还在。最便宜的办法是**把作者的自述从输入里拿掉，只留证据**。

输出必须是裁决，不是重写：

```json
{
  "verdict": "fail",
  "layer": "implementation",
  "evidence": [
    {
      "kind": "test",
      "ref": "tests/test_export.py::test_csv_header",
      "quote": "AssertionError: b'PNG' != b'CSV'"
    }
  ],
  "defect": "导出分支写死成 PNG 编码，未按扩展名分派",
  "prediction": "改后该测试转绿，test_png_export 仍绿",
  "scope": "src/export.py:41-58",
  "confidence": 0.86
}
```

四条硬约束：

1. 没有 evidence，verdict 只能是 `uncertain`。
2. scope 必须比上一轮更窄；否则说明没定位，直接升级。
3. confidence 低于阈值（比如 0.6）不进自动修补，进人审或换 critic。
4. critic 不许改代码。诊断与修复分开，避免它先想出一个补丁再倒推理由。

prompt 的差别，比模型的差别大：

```text
请检查你的答案是否正确，如果有问题请修正。
```

```text
只根据下面的证据判断，不要推测没有出现在证据里的错误。
列出最多三个最可能的缺陷，每个必须指向 evidence 里的条目。
如果找不到缺陷，说明你检查了哪些边界、为什么它们不构成缺陷。
```

第二段的关键不是礼貌，是**允许它说「没找到」，但必须交代检查范围**。强迫「必须找出问题」会制造缺陷；允许「没问题」但不给检查范围，会制造剧场。两者中间的那一格，才是可用的。

还有一句：critic 的输出长度不是质量指标。一段 800 字的批评如果指不出一个文件行号，它的信息量低于一行 `test_export.py:42 AssertionError`。

## 8. 回路：预算、停止与升级

{{< fig "diagrams/06-budget.svg" "回路的刹车。收益在第一轮之后迅速变平，振荡则会让曲线来回摆；预算、无进展和边际收益三个条件，谁先到谁停。" >}}

三个停止条件：

**预算。** L0/L1 三轮，L2 两轮。超过就升级。轮数是成本，不是决心。写死轮数比让模型自己判断「还需不需要再来一次」可靠，因为后者几乎永远说需要。

**无进展。** 把 `(hash(diff), sorted(failing_tests))` 当状态。状态不变就停；状态在两个值之间循环就是振荡，也停。周期检测比最大轮数更早生效，也更省；实现上只需要一个长度 4 的环形缓冲。

**边际收益。** 第 k 轮的通过率增量除以这一轮的成本，低于阈值就停。经验值：第二轮之后增量通常小于第一轮的一半。这不是定理，是一个足够好的默认值。

| 触发 | 动作 |
| --- | --- |
| 工具报错 | 重试或换工具，不反思 |
| 有证据、归因清楚 | 修补 + 预测 + 回归 |
| 同层三次失败 | 升一层，重新规划 |
| 归因不确定 | 补证据（加日志、加测试），不是加反思 |
| 仍然不确定 | 问人，带上已排除的假设 |

**回归闸。** 每接受一个修补，跑一遍原本通过的集合。很多 agent 的净效果是负的，因为它修一个坏两个。闸门要放在接受修补之前：先预测哪些会受影响，再跑；跑不过就回滚，不进入下一轮。

一句总结这一节：反思不是免费的。它消耗 token、延迟，还有一次改坏的机会。把这三项都记在账上，很多「反思模块」会被关掉——这是好事。

## 9. 沉淀：经验的生老病死

修补是消耗品，经验是资产。经验的最小结构不是一段话：

```json
{
  "when": "pytest 报 ImportError: cannot import name 'X' from partially initialized module",
  "do": "先查循环导入，不要在 __init__ 里提前 import 子模块",
  "evidence": ["run-3f2a", "run-9c1b"],
  "scope": { "repo": "svc-a", "lang": "python", "tool": "pytest>=8" },
  "support": 2,
  "confidence": 0.7,
  "expires": "2027-03-01"
}
```

{{< fig "diagrams/07-lesson-lifecycle.svg" "经验的六个站与一条污染路径。写、去重、定域、回放、消费、退役；绕过前三站直接写入的自我叙述，会变成教条。" >}}

六个站：

1. **候选。** 只从已验证的结果里写。测试绿了、用户接受了，才算证据；agent 的自述不算。
2. **去重。** 按 `(归一化的 when, do)` 合并，累计支持数。支持数 1 的是假设，≥2 才升级为规则。
3. **定域。** 仓库、语言、工具版本、任务类型。没有域的经验会到处乱用，而且用错的时候看起来也挺合理。
4. **回放。** 在历史失败集上跑一遍，看它能不能让那些 case 变绿，且不弄坏别的。没有回放的经验是赌。
5. **消费。** 注入时记录 id；任务结束记录是否被采纳、是否有效。没有消费日志，就无法区分「有用」和「被读过」。
6. **退役。** 模型升级、工具换版本、连续两次应用失败 → 隔离或删除。

污染路径是：**未验证的自我叙述 → 直接写入 → 被反复检索 → 变成教条**。它可怕的地方是自我强化——经验让 agent 按某个方式做事，结果看起来「符合预期」，于是经验的支持数又加一。对策只有三个：只认外部证据、设支持数门槛、留失效期。

三种沉淀粒度，对应三类系统：

| 粒度 | 代表 | 写入时机 | 复用单位 | 主要风险 |
| --- | --- | --- | --- | --- |
| 情境经验 | Reflexion | 一次尝试后 | 下一次尝试的提示 | 过拟合单条轨迹 |
| 技能 / 工作流 | Voyager、AWM、ExpeL | 多次成功后 | 可执行代码或流程 | 版本漂移、组合爆炸 |
| 策略补丁 | Constitutional AI 式规则、提示词与工具改动 | 离线聚合 | 整个系统 | 一次改坏所有人 |

三种都成立，但顺序不能反。没有情境经验就直接改策略，等于用三条样本改系统提示词；没有回放就直接上线，等于让全体用户做灰度。

## 10. 失效清单

| 失效 | 机制 | 检测信号 | 对策 |
| --- | --- | --- | --- |
| 反思剧场 | critic 只会说「看起来不错」 | verdict 通过率接近 100%，零 evidence 指针 | 强制 evidence；换 critic；盲评 |
| 自我偏好 | 同一模型偏好自己的输出 | 交换作者后结论翻转 | 换模型、盲评、去掉作者自述 |
| 锚定 | 读了 actor 的解释，接受它的框架 | 批评复述 actor 的措辞 | 信息食谱隔离 |
| 振荡 | 两个补丁互相拆台 | diff 哈希呈周期 2 | 状态检测，强制升级 |
| 验证器游戏 | 改测试、特判、吞异常 | 测试文件 diff、断言数下降 | 测试冻结、覆盖率门槛、人工审 |
| 层级误判 | 用 L0 修 L2 | 同层三次失败，diff 变大 | 强制升层规则 |
| 记忆污染 | 坏经验被反复消费 | 经验胜率下降、支持数虚高 | 证据门槛、回放、隔离 |
| 成本失控 | 每轮都反思，收益趋零 | 单任务成本翻倍，通过率不变 | 预算、边际收益停止 |

补一条不在表里的：**把反思当验收。** agent 说「我已经检查过了」，这不是证据；只有 oracle 跑过才是。这个失效很隐蔽，因为它在日志里看起来像完成了工作。

## 11. 接线：四个钩子

| 钩子 | 时机 | 做什么 | 谁执行 |
| --- | --- | --- | --- |
| H0 | 每次工具返回后 | 异常分类、参数与 schema 修复 | 代码，不调模型 |
| H1 | 每个 step 后 | 进展检测：世界变了吗 | 代码 + 小模型 |
| H2 | 任务宣称完成前 | 跑 oracle；失败则裁决、归因、修补 | 代码 + critic |
| H3 | 每 N 个任务或离线 | 聚合失败，提策略补丁，回放、影子、灰度 | 离线流水线 |

H2 是投入产出比最高的一个钩子。它不需要改主循环，只需要在「完成」这个动作前面加一道门。很多系统的「反思」其实就是缺了这道门——模型说完就结束了。

一段伪代码：

```python
def run_task(task, max_rounds=3):
    lessons = retrieve(task)                 # 只取匹配当前域的
    trace, states = [], set()

    for round_no in range(max_rounds):
        result = actor(task, lessons, trace)
        evidence = oracle(task, result)      # 测试 / 类型 / 环境
        if evidence.ok:
            commit(result)
            learn(task, trace, evidence, outcome="pass")
            return result

        verdict = critic(task, result, evidence)   # 只看证据，不看自述
        if verdict.layer == "spec":
            return escalate(result, verdict)       # 规格问题，问人

        state = (hash(result.diff), frozenset(evidence.failing))
        if state in states:                        # 无进展或振荡
            return escalate(result, verdict)
        states.add(state)

        patch = repair(result, verdict)
        if regressed(patch):                       # 原本绿的变红
            rollback(patch)
            return escalate(result, verdict)

        trace.append((result, evidence, verdict, patch))
        lessons += learn(task, trace, evidence, outcome="fail")

    return escalate(result, trace)
```

注意三件事：

- `oracle` 在 `critic` 之前。没有证据就不叫 critic。
- `learn` 只在有外部结果时调用，写入前过去重和定域。
- `regressed` 是硬闸，不是建议。

存储上，轨迹、裁决、经验分三张表。轨迹是事件日志，用来回放；裁决是结构化记录，用来统计归因分布；经验是带域和失效期的对象。三者混在一个向量库里，检索会看起来很好，消费会一塌糊涂——这是[记忆那篇](/posts/design-agent-memory.html)的主题，这里不展开。

## 12. 怎么证明它有用

{{< fig "diagrams/08-ablation.svg" "验证反思价值的 2×2。只测 A 和 D，会把「重试 + 证据」的功劳记在反思头上；B 和 D 的差距，才是反思文本真正的增量。" >}}

四个臂：

- **A：不重试、不反思。** 基线。
- **B：重试 + 证据，不反思。** 把失败输出和 diff 直接回灌给 actor。
- **C：反思，不给新证据。** 常见的「请你复盘一下」。
- **D：重试 + 证据 + 反思。** 完整回路。

大多数人只测 A 和 D，然后归功于反思。正确的对照是 B 和 D。如果 B ≈ D，反思文本是装饰——保留 oracle 和重试，删掉那段散文，省下的 token 可以多跑几个任务。C 通常最差：它增加了成本，还可能把本来对的判断说服成错的。

指标：

| 指标 | 定义 | 为什么重要 |
| --- | --- | --- |
| pass@budget | 固定预算下的通过率 | 公平比较的前提 |
| iterations-to-green | 到通过的中位轮数 | 延迟与成本 |
| regression rate | 修好一个弄坏另一个的比例 | 净收益 |
| false-fix rate | 声称修好、oracle 不认 | 安全 |
| oracle-game rate | 测试被改或跳过的比例 | 奖励黑客 |
| cost per solved | 每个解决任务的花费 | 经济性 |
| lesson precision | 被应用且有效的经验比例 | 记忆质量 |

实验设计上有两个细节：固定预算（token、轮数或时间），固定任务集和 oracle；每个臂至少跑三次取中位数，因为采样方差比大多数人以为的大。pass 率上升不等于变好——如果测试被改弱了，pass 率会上升，能力会下降。所以 regression 和 oracle-game 必须和 pass 一起看。

## 13. 什么时候不要反思

| 场景 | 判断 | 做法 |
| --- | --- | --- |
| 一次性、可验证、重试便宜 | P(证据) 高，P(缺陷) 低 | 直接重试 + 错误信息 |
| 延迟敏感（在线交互） | 每轮几百毫秒 | 离线反思，不阻塞 |
| 无 oracle、验收主观 | P(证据) 低 | 人审，或多样性采样 |
| 环境抖动 | 归因不在 agent | 修环境，重试 |
| 高风险不可回滚 | 错误代价远大于反思成本 | 人工确认，禁止自动修补 |
| 任务本身一次就过 | 净成本为正 | 不接反思 |

一句话：反思是用延迟换正确率的手段。当延迟比正确率贵，或者正确率没有 oracle 可测，它就不划算。

## 14. 检查清单

上线前逐条过：

1. 有没有一个不依赖模型的 oracle？
2. 失败时，critic 拿到的是原始证据，还是 agent 的转述？
3. 裁决里有没有 evidence 指针和可证伪的 prediction？
4. 有没有「同层三次失败就升级」的规则？
5. 有没有无进展与振荡检测？
6. 修补前有没有回归闸？
7. 经验是不是只从已验证的结果里写？
8. 经验有没有域、支持数、失效期？
9. 有没有记录经验的消费与胜率？
10. 有没有测过「只重试、不反思」这一臂？

反思的价值不在那一刻的自我批评，而在它是否改变了下一个动作、下一次尝试、下一类任务。三者一个都没变，那段文字就只是让日志更长。

## 15. 代码里的反思：OpenCode 的五个接缝

> 这一节是文章发出来之后补的。前面的框架是归纳；这一节找一套真实源码做对照。样本是 [OpenCode](https://github.com/anomalyco/opencode/tree/a687c047afc78771fbe96bb4dca99a60fea42a2c)（commit `a687c047`），只读机制，不评价产品；路径都相对于 `packages/core/src/`。

### 15.1 先修正一个分类：反思不是 TAO 的第四个格子

一个常见的误解，是把反思画成 ReAct 循环里的第四个格子：

`Thought → Action → Observation → Reflection`

这样画很顺，但会把实现带偏：好像只要在 observation 后面加一段「请你复盘」，反思就接上了。

更准确的画法是两层。一阶是 TAO，负责把事做完；二阶消费一阶的轨迹和证据，产出对下一轮 T/A/O 的修改。反思不是第四个格子，是套在循环外面的那层回路。

{{< fig "diagrams/09-tao-meta.svg" "反思与 TAO 的关系。上面是一阶任务循环，下面是二阶反思循环；证据从 Observation 分叉，裁决改写控制流，再回到 Thought 或 Action。OpenCode 的代码实现的是下面这条回路，而且「沉淀」那一格是虚线：它有压缩摘要，没有经验库。" >}}

落到实现，它可能出现在五个地方：

| 落点 | 形态 | 例子 |
| --- | --- | --- |
| Thought | 模型在同一个上下文里自我批评 | 「等等，这个假设不成立」 |
| Action | 调一个 critic / verifier 工具 | 跑测试、读文档、开子会话 |
| Observation | 环境返回的失败信号 | 报错、diff、退出码 |
| Harness | 不改模型，直接改控制流 | 重试、压缩、回滚、停止 |
| Offline | 跨任务改策略 | 改 prompt、改工具、改流程 |

所以「反思是 action 的一种实践」只覆盖了第二行。OpenCode 的代码里，这五个落点都能找到影子；但没有一个函数叫 `reflect`。它把反思拆成了接缝。

### 15.2 一次 attempt 的出口是一个代数类型

先看 `runner/step.ts:30`。一次 attempt 结束，返回的不是一段文本，而是一个六选一的类型：

```ts
export type Outcome = Data.TaggedEnum<{
  Completed: { readonly needsContinuation: boolean }
  Retry: { readonly error: SessionError.Error; readonly decision: SessionRunnerRetry.Decision }
  Continue: {
    readonly error: SessionError.Error
    readonly decision: SessionRunnerRetry.Decision
  }
  RecoverFull: {}
  Compacted: {}
  OutputLimit: { readonly needsContinuation: boolean }
}>
```

`attempt` 覆盖一次模型流，以及它派生出来的工具调用。六个出口的含义：

| Outcome | 触发条件 | 执行 |
| --- | --- | --- |
| `Completed` | 没有 failure | 需要就继续下一 step，否则回 idle |
| `Retry` | 可重试失败，且尚未产出 | 退避后重跑同一个 step |
| `Continue` | 可重试失败，且已经产出 | 保留半截，发「继续」，新建 assistant message |
| `RecoverFull` | Transport 失败，`recovery` 为 `retry-full` 或 `rotate-and-retry-full`，且尚未产出 | 下一圈整请求重发 |
| `Compacted` | provider 报 context overflow，尚未产出，压缩成功 | 带压缩后的上下文重跑 |
| `OutputLimit` | `finish = length` | 续写两次仍截断就失败，否则续写 |

处理在 `runner/llm.ts:318` 的 `$match` 里。每个分支改的东西不一样：`Retry` 只等退避，不新建 message；`Continue` 会发一条 synthetic 的「继续」，并新建 message；`Compacted` 关掉 overflow 恢复，新建 message；`RecoverFull` 关掉 continuation 恢复，让下一圈整请求重发；`OutputLimit` 续写两次仍截断就抛 `output-limit`；`Completed` 在回 idle 前还会预计算下一次压缩摘要。

{{< fig "diagrams/10-outcomes.svg" "一次 attempt 的六个出口。分类发生在写任何修补之前：先判断有没有失败、失败属于哪一类、失败前有没有产出，再决定重跑、续跑、压缩还是收尾。图里的条件都来自 runner/step.ts；不满足任何分支的失败直接上抛。" >}}

分支不是拍脑袋的。源码里有三个守卫：

```ts
if (
  !publisher.record().outputStarted &&
  isContextOverflowFailure(streamFailure) &&
  (yield* restore(input.recoverOverflow))
) {
  // provider rejected the request as too long; compact and retry
  return Outcome.Compacted()
}

if (
  input.recoverContinuation &&
  llmFailure?.reason._tag === "Transport" &&
  (llmFailure.reason.recovery === "retry-full" ||
    llmFailure.reason.recovery === "rotate-and-retry-full") &&
  !recorded.outputStarted
)
  return Outcome.RecoverFull()

const retry =
  llmFailure && llmError && !isContextOverflowFailure(llmFailure)
    ? yield* restore(
        input.retry(
          llmFailure,
          llmError,
          SessionRunnerRetry.isRetryable(llmFailure) ||
            (recorded.outputStarted && isInterruptedStream(llmFailure)),
        ),
      )
    : undefined
if (llmFailure && llmError && retry?.retry && !recorded.outputStarted) {
  yield* publisher.startAssistant()
  return Outcome.Retry({ error: llmError, decision: retry })
}
```

三个判断决定了分支：有没有失败、失败属于哪一类、失败前有没有产出。最值得抄的是 `outputStarted`：它把「重跑」分成两种——没产出就重跑整个 step，已经产出就保留半截续跑。没有这个布尔值，重试要么丢掉有用的半截，要么把半截和重跑叠在一起，上下文里出现两份互相矛盾的回答。

这就是「反思改变控制流」的最小实现：一个枚举、一个 `match`、三个守卫。它不判断「实现错还是计划错」，但它保证不同的失败不会走同一条路。

### 15.3 上下文失败：compaction 是另一种修复

`Compacted` 修的不是任务，是上下文。它的触发条件很具体：provider 拒绝请求，且这次 attempt 还没产出任何 token。没产出才敢压，产出了就得保留。

压缩的参数都是硬编码的：

```ts
const RESERVE_MIN = 16_000
const RESERVE_SHARE = 0.15
const PRECOMPUTE_LEAD = 60_000
const PRECOMPUTED_TAIL_SHARE = 0.4
const PRECOMPUTE_MAX_FAILURES = 2
```

翻成中文：给模型的窗口至少留 15%，且不少于 16K；离上限还有 60K 时就在后台写摘要；摘要换入时最多保留 40% 的尾巴；后台摘要连续失败两次就不再启动。被 provider 拒绝后，下一次尝试按 `0.85 / 0.7 / 0.5 / 0.35` 逐级缩小请求。它甚至看 prompt cache 的剩余寿命：只有最近一次响应的缓存还剩 30 秒以上，才启动后台摘要。

摘要提示词里有一条很关键：

```text
- Do not mention the summary process or that context was compacted.
```

这是一个信息屏障。如果让 actor 知道「你刚被压缩过」，它会开始表演记忆：复述摘要、猜自己忘了什么、要求重读文件。压缩本来是为了让它继续干活，不是让它意识到自己失忆。所以摘要 prompt 还要求「不要继续任务、不要调用工具、只返回结构化摘要」。

这一段不是任务反思，但它和反思共用一套接线：触发条件、预算、保留区、失败上限、信息屏障。把这套接线搬到任务失败上，就是 §8 的预算与停止；搬到经验上，就是 §9 的写入门槛。

它缺的也很明显：压缩只知道「太长」，不知道「为什么长」。是工具输出太大、读文件太多，还是计划本身在绕路？它修症状，不修原因。这是后面「归因缺口」的一半。

证据侧还有一个很小的机制。`tool-output.ts` 把超过 2000 行或 50 KiB 的工具输出截断，原文落盘，模型看到的是：

```ts
const marker = `[showing ${shown} of ${lines.length}; full output saved to ${file}]`
```

这是「原文 + 指针」，不是「摘要」。摘要会丢证据；指针让模型知道去哪拿全文。反思要的是后者。

### 15.4 独立视角：subagent 是 critic 的原语，不是 critic

OpenCode 的核心代码里没有内建的 critic agent。它提供的是 `subagent`：在子会话里跑另一个 agent。工具描述里有一句关键的话：

```ts
"New child sessions start with fresh context, so include all relevant context and instructions when you don't pass a sessionID.",
```

「fresh context」是 critic 的第一价值：不是更聪明，是没看过 actor 的自我辩护。同一个上下文里的第二次表态，通常只是第一次表态的加强版；换一个干净上下文，至少有机会问出不同的问题。

但它也把责任推给了调用方：证据、问题、裁决格式都要自己给。原语有了，信息食谱没做，多 agent 评审就会退化成「另一个模型也说不错」。这正好对应 §7：critic 的输入，一半决定了它的输出。

### 15.5 状态：revert 把回滚做成两阶段

`session/revert.ts` 的 `stage` 做三件事：算边界、恢复文件、记下 revert。

```ts
const original = input.session.revert?.snapshot ?? (yield* snapshot.capture())
const next = yield* plan(database.db, { sessionID: input.session.id, messageID: input.messageID })
const restore = new Map<RelativePath, Snapshot.ID>()
if (original) {
  for (const file of input.session.revert?.files ?? []) restore.set(RelativePath.make(file.file), original)
}
if (input.files !== false) for (const [file, tree] of next) restore.set(file, tree)
if (restore.size) yield* snapshot.restore({ files: restore })
```

这时消息还在，文件已经回到边界，用户可以反悔。`commit` 才真正丢分支：`projector.ts:1131` 会删掉边界及之后的 message、inbox，释放 blob，并回退 instruction 与文件引用。`clear` 则撤销文件恢复，把 revert 置空。

两阶段做得干净：`stage` 可逆，`commit` 不可逆，中间有一次人工确认的机会。但它没有为被删的轨迹留证据——`commit` 之后，失败过程只存在于日志和快照里。要落实「丢分支、留证据」，得在 commit 前把证据写到外部。机制只做了前半句。

### 15.6 刹车：max steps 与 restart attempts

两个很小的机制，决定了反思会不会跑到改坏为止。

`runner/max-steps.ts` 在步数用完时注入一段提示：工具禁用，要求总结已完成、未完成和建议，最后一句是「任何工具调用都是 critical violation」。它不是建议，是硬闸。

`session/execution/restart.ts` 给每个 turn 一个持久化的重启预算：`DEFAULT_MAX_ATTEMPTS = 10`。一个 turn 被恢复超过 10 次，就直接终止，返回「Execution was interrupted repeatedly and will not be resumed automatically.」。计数器只有在收到 terminal event 时才重置，所以一个不断崩溃的 turn 不能靠重启刷预算。

再加上前面工具输出的 2000 行 / 50 KiB 上限，三件事凑齐了反思的边界：步数有上限、重启有上限、证据有上限。没有这三条，反思的默认结局是跑到改坏为止。

### 15.7 五个接缝，四件事，两个缺口

把 OpenCode 的机制对回 §2 的四件事：

| 接缝 | 对象 | 证据 | 归因 | 沉淀 |
| --- | --- | --- | --- | --- |
| Step Outcome | 失败类型 | 强：`finish`、`failure`、`outputStarted` | 弱：传输 / 上下文 / 可重试 | 无 |
| Compaction | 上下文 | 中：token 估算 + overflow | 弱：只知道「太长」 | 中：摘要留在历史里 |
| Subagent | 委派任务 | 由调用方给 | 无内建 | 弱：子会话历史 |
| Snapshot / Revert | 文件与分支 | 强：快照 diff | 无 | 无 |
| Budget | 步数与重启 | 强：持久化计数 | 无 | 无 |

两个缺口：

1. **归因**：OpenCode 分的是「传输 / 上下文 / 可重试」，不是「实现 / 计划 / 环境」。后者仍由模型在 loop 里做。harness 提供了证据和控制流，但没有把归因变成一个可校验的对象。
2. **沉淀**：没有经验库、没有回放、没有回归闸。压缩摘要会留在历史里，但那是「这次怎么继续」，不是「下次怎么做得更好」。

所以，判断一个 harness 有没有反思，不看它有没有 critic 工具，看三件事：

- 失败后控制流有没有分叉；
- 分叉前有没有拿到原始证据；
- 分叉后有没有状态可回、沉淀可写。

OpenCode 前两件做得扎实，第三件只做了一半：状态有 revert，沉淀没有 lesson store。

### 15.8 如果你要补一个 critic，最小改动在哪

不需要重写 loop。四个接口就够：

1. **入口**：在任务收尾时（`Completed && !needsContinuation`，模型已给出完成回答、即将回 idle）开一个子会话（`subagent` 已经是现成的）；step 失败后也可以提前跑。输入原始证据：失败的 tool output、diff、测试输出。不要输入 actor 的自述。
2. **出口**：要求结构化 verdict：`layer`（实现 / 计划 / 环境）、`evidence`（指针）、`prediction`（可证伪）、`scope`（域）、`confidence`。没有 evidence 指针的 verdict 直接丢。
3. **映射**：把 verdict 映射到已有的 Outcome。实现错走 `Retry` / `Continue`；计划错走重新规划（OpenCode 需要在 Outcome 外再加一条分支）；环境错直接停；不确定就问人。
4. **沉淀**：只有 oracle 验证通过的修补才写经验；先做回归闸，再做经验库。

这也是 §11 里 H2 的位置：`Completed` 且 `needsContinuation = false`，就是任务即将收尾、oracle 已经能给出结论的时刻。在这里插一个 verdict，控制流已经在了，不需要另造一套反思框架。

一句话：harness 的反思不是一段更严厉的 prompt，是把失败变成类型、状态和下一次尝试。类型让分支存在，状态让分支可回，证据让分支不瞎。

## 附：文中提到的论文

- Reflexion: Language Agents with Verbal Reinforcement Learning — [arXiv:2303.11366](https://arxiv.org/abs/2303.11366)
- Self-Refine: Iterative Refinement with Self-Feedback — [arXiv:2303.17651](https://arxiv.org/abs/2303.17651)
- CRITIC: Large Language Models Can Self-Correct with Tool-Interactive Critiquing — [arXiv:2305.11738](https://arxiv.org/abs/2305.11738)
- Large Language Models Cannot Self-Correct Reasoning Yet — [arXiv:2310.01798](https://arxiv.org/abs/2310.01798)
- When Can LLMs Actually Correct Their Own Mistakes? — [arXiv:2406.01297](https://arxiv.org/abs/2406.01297)
- Generative Agents: Interactive Simulacra of Human Behavior — [arXiv:2304.03442](https://arxiv.org/abs/2304.03442)
- Voyager: An Open-Ended Embodied Agent with Large Language Models — [arXiv:2305.16291](https://arxiv.org/abs/2305.16291)
- LLM Evaluators Recognize and Favor Their Own Generations — [arXiv:2404.13076](https://arxiv.org/abs/2404.13076)
