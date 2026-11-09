---
title: "给 Agent 装一张账单：OpenCode 的计费服务端设计"
date: "2026-11-09T13:55:00+08:00"
summary: "从 Task / Run / Step 三层任务模型出发，讲清预扣、结算、释放、长任务与对账；再把机制接到 OpenCode 的 session 头、step 事件、provider baseURL 和插件 hook 上，形成一套网关强制、插件增强、SSE 对账的扣费服务端方案。含 5 张机制图。"
description: "从 Task / Run / Step 三层任务模型出发，讲清预扣、结算、释放、长任务与对账；再把机制接到 OpenCode 的 session 头、step 事件、provider baseURL 和插件 hook 上，形成一套网关强制、插件增强、SSE 对账的扣费服务端方案。"
tags: ["opencode", "billing", "architecture"]
slug: "opencode-billing"
---

一个编码 Agent 跑一次任务，可能调用模型几十次。每次调用几百毫秒到几分钟，中间夹着读文件、跑命令、等权限。用户看到的是「一个会话」，账单上却是几十笔 token 消费。

这时候，「扣费」最难的地方不是单价，而是**扣减和实际执行状态对不上**：请求发出去了，上游扣了钱，本地还没记账；任务失败了，钱已经花了；进程崩了，额度冻结在那儿；用户重试一次，扣了两遍。

我给一套通用的任务与积分机制，再把它接到 OpenCode 上。结论先说：**要预扣，但预扣不是扣费，是冻结；冻结的对象是 Step，不是整个会话；长任务靠「每步结清 + Run 封顶」，不靠一个长 TTL。**

---

## 一、先看清问题：三种扣法，只有一种能兜住

最直觉的做法是「先查余额，执行完再扣」。两个请求同时进来，都查到余额 100，各自扣 80，最后余额 -60。这就是 check-then-act：检查和扣减不是一个原子动作。

第二种是「执行前直接扣一笔预估值，失败再退」。比第一种好，但预估值往往不是实际值：模型可能提前结束，可能触发工具循环，可能被上游限流。退多退少都是麻烦。

第三种是「先冻结上限，执行后按实际结算」。冻结只减少可用额度，不增加已用；结算时多退少补。这是支付系统里的 authorization / capture，也是 LiteLLM、Cycles 这些系统采用的模型。

| 做法 | 并发安全 | 长任务 | 主要问题 |
|---|---|---|---|
| 先查后扣 | 否 | 敞口无界 | 超卖 |
| 先扣预估，失败退 | 是 | 冻结久 | 退多退少、失败判定 |
| 冻结上限，按实结算 | 是 | 可控 | 需要状态机、幂等、对账 |

真正要设计的不是「扣多少」，而是**什么时候冻结、什么时候结算、什么时候释放、失败和未知怎么处理**。

---

## 二、任务分三层：Task、Run、Step

{{< fig "diagrams/01-task-model.svg" "Task / Run / Step 三层任务模型" >}}

把一次工作拆成三层，很多问题会突然变简单：

- **Task**：一个长期工作单元。比如一个 OpenCode Session、一个视频项目、一个批量作业。它承载预算上限、归属和最终对账，生命周期可以是几小时到几个月。
- **Run**：一次执行。比如一次 prompt 触发的 agent loop、一次视频渲染。它有自己的状态机：运行、等待、成功、失败、取消、未知。
- **Step**：一次可计量的操作。比如一次 LLM 调用、一次付费工具调用。它是预扣、结算和幂等的最小单位。

为什么不能只有 Task？

因为资金的最小单位是 Step。一次模型调用的用量在响应结束时就能确定，立即结算最准确。如果把整个 Task 当一个预扣单，一个跑三小时的会话就要冻结三小时的预算；失败时你也不知道该退多少。

为什么不能只有 Run？

因为 Run 可能包含几十个 Step，失败可能发生在任意一步。Run 的状态决定「这次执行是否成功」，但不决定「每个 Step 的钱是否结清」。两个状态机要分开。

这也是我在 OpenCode 上看到的结构：Session 是 Task，Execution 是 Run，Step 是 Step。它本来就这么分层，只是没有把「钱」接上去。

---

## 三、两套状态机：执行归执行，资金归资金

{{< fig "diagrams/02-state-machine.svg" "Run 与预扣单的状态机" >}}

Run 的状态是 `CREATED → RUNNING → SUCCEEDED / FAILED / CANCELED / UNKNOWN`。预扣单的状态是 `HELD → SETTLED / RELEASED / EXPIRED`。

它们不是一一对应：

- Run 成功，预扣单结算最后一步。
- Run 失败或取消，结算已经产生的用量，释放未使用部分。
- Run 未知，冻结保留，直到对账窗口结束。
- 预扣单过期，资金释放；之后到达的用量走「迟到补记」，状态不回退。

`UNKNOWN` 是最容易被忽略、也最值钱的状态。上游超时、worker 崩溃、回调丢失，都不等于失败。如果这时候立刻退款，而上游其实已经计费，平台就承担了损失；如果立刻扣款，而任务其实没执行，用户就会投诉。正确做法是把它标成未知，保留冻结，去查上游，再决定。

预扣单的 `EXPIRED` 也一样。过期释放之后，上游账单才到，这时不能把状态改回 `SETTLED`，否则终态可逆，账会乱。正确做法是在已释放的单上补记一条 `CONSUME`，状态仍是 `EXPIRED`，金额累加。

---

## 四、一次 Step：冻结、执行、结算

{{< fig "diagrams/03-step-sequence.svg" "一次 Step 的预扣与结算时序" >}}

一个 Step 的完整流程：

1. 估算上限：`输入 token × 输入价 + 最大输出 token × 输出价 + buffer`。
2. 原子冻结：`UPDATE account SET held = held + ? WHERE granted - consumed - held >= ?`。
3. 调用上游，保持流式。
4. 从 usage 取实际值，计算 `delta = actual - captured`。
5. 结算：`held -= 剩余`，`consumed += delta`，写流水和结算明细。
6. 状态 CAS 到 `SETTLED`。

这里有几个必须做对的细节。

**第一，冻结要原子。** 不要先 `SELECT` 再 `UPDATE`；一条带条件的 `UPDATE`，看 `affected_rows` 是否等于 1。账户行是热点，但这是必要的串行化。

**第二，加锁顺序要固定。** 先锁预扣单，再动账户；或者反过来。所有路径保持一致，否则 Reserve 和 Settle 并发时会死锁。

**第三，幂等靠唯一键，不靠先查。** 结算明细用 `(reservation_id, seq)` 唯一；重复结算会撞唯一键，返回已有结果。不要「先查有没有结算过，再插入」，查询和插入之间的竞态永远存在。

**第四，事务里不要调上游。** 冻结、流水、状态、outbox 在同一个本地事务里；上游调用在事务提交之后。上游慢会拖长事务，上游成功但本地回滚会「做了没扣」。

**第五，预扣是上限，不是预估。** 结算实际小于上限，退差额；实际大于上限，走 overage 策略：可用额度内补扣、标记 over-limit、或者欠款。不要在预扣时「估一个差不多」，那样最坏情况没有担保。

---

## 五、OpenCode 已经留好了接口

OpenCode 是一个本地服务 + 多个瘦客户端的编码 Agent。它的源码里，和计费相关的接入面比想象中多。

**请求头。** 每次模型请求都会带：

```text
x-opencode-session-id: ses_...
x-opencode-parent-session-id: ses_...   # 子会话
x-opencode-project: prj_...
x-opencode-client: <app>
x-session-affinity: ses_...
```

`x-opencode-session-id` 是当前会话，`x-opencode-parent-session-id` 把子 agent 的费用归到父任务。网关不需要插件，就能知道「这笔钱算在谁头上」。

**Step 事件。** `session.step.ended` 带 `cost` 和完整的 token 分类：

```text
input / output / reasoning
cache.read / cache.write
```

`session.step.failed` 也带可选的 `cost/tokens`。这些事件通过 `GET /api/event` 的 SSE 推出来，可以按类型订阅。

**Provider 配置。** `options.baseURL`、`options.apiKey` 可以指向自己的网关；`timeout`、`headerTimeout`、`chunkTimeout` 也能配。

**插件 hook。** `session.model.request` 可以改 `baseURL` 和 `headers`；`session.http.response` 可以读响应；`event` 可以订阅事件；`session.prompt` 可以在 prompt 进来时做预检。`model.request` 还带 `kind`，能区分 `primary / compaction / title / generate`。

**原生 cost。** OpenCode 的 `Usage` 会优先采用 endpoint 自报的 `usage.cost`；没有时才用模型目录价算。网关只要在响应 usage 里回填 `cost`，OpenCode 的 session/message cost 就会跟着更新。

这就是为什么我说不需要 fork：**OpenCode 已经把「谁在调用、调了什么、花了多少」暴露出来了，缺的是一个可信的收款方。**

---

## 六、四种接法，推荐「网关强制 + 插件增强」

{{< fig "diagrams/04-opencode-architecture.svg" "OpenCode + 计费网关 + 计费服务" >}}

| 方式 | 强制力 | 任务归属 | 改动 | 适用 |
|---|---|---|---|---|
| 纯网关 | 强 | session 头；run/step 近似 | 改 baseURL | 商业中转 |
| 纯插件 | 弱 | 精确 | 装插件 | 内部记账 |
| 混合 | 强 + 精确 | 精确 + 请求头 | 网关 + 插件 | 生产 |
| Fork/内嵌 | 最强 | 最精确 | 改源码 | 自有发行版 |

纯网关的好处是**唯一入口**：上游 key 只在网关，用户只拿网关 key；插件被禁用、客户端被改，请求仍然要经过网关。缺点是网关要实现协议适配、SSE 解析、usage 归一化，还要承担单点和延迟。

纯插件的好处是**知道得多**：session、run、step、kind 都能拿到；但插件跑在用户机器上，可以被禁用，也可以被篡改。它适合内部团队分摊，不适合对不受信用户硬扣费。

所以推荐混合：

- 网关做硬限制和计费事实；
- 插件只做三件事：补归属头（`kind`、`run id`、`agent`）、prompt 前预检余额、把余额/费用展示给用户。

即使插件被禁用，网关仍然能扣费；插件只是让体验更好。

---

## 七、一次 prompt：多个 Step，逐笔结清

{{< fig "diagrams/05-prompt-sequence.svg" "一次 OpenCode prompt 的扣费时序" >}}

OpenCode 的 agent loop 是这样的：

```text
prompt → LLM step 1 → tool call → LLM step 2 → tool call → ... → final
```

每一次 LLM 调用都是一个 Step：

1. 网关收请求，读 `x-opencode-session-id`，找到 Task。
2. 估算上限，Reserve。
3. 转发上游，流式返回。
4. 从 usage 取实际值，Settle。
5. 把 `usage.cost` 和 `x-billing-*` 回给 OpenCode。
6. 工具执行不计 LLM 费用；下一步重新预扣。
7. Run 结束，关 Run；Task 仍然可以继续。

这里不需要「为整个 Run 冻结一笔钱」。Run 级只需要一个闸门：

```text
max_credits  这次 Run 最多花多少
max_steps    最多多少步
max_duration 最长跑多久
```

每次 Step 前检查 `run.consumed + estimate <= max_credits`。超过就拒绝下一步，结束 Run，返回「预算用尽」。已产生的 Step 照常结算。

这比「冻结整个 Run」好在哪里？

- 冻结是短的：每个 Step 几秒到几分钟，不会占住几小时。
- 失败恢复简单：已完成的 Step 已结清，未执行的 Step 没有预扣，不需要退。
- 资金和状态对齐：Step 结束就知道花了多少，不用等整个 Run。
- 防死循环：Run 级上限拦住 agent 的无限循环，而不是靠用户盯着。

---

## 八、流式、重试、子 agent：三个容易漏的角落

**流式。** OpenAI Chat 的 usage 在最后一个 chunk；Anthropic 在 `message_start` 和 `message_delta`；Gemini 在 `usageMetadata`。网关要在客户端断开后继续读上游一段时间，拿到最终 usage；否则只能按已收到内容估算。上游已经产生的成本，不会因为客户端断开而消失。

**重试。** OpenCode 在可重试错误上会重发同一个 Step。每次 HTTP 请求都是一次计费尝试：上游没产生用量就释放；已经流出一部分就按实际/估算结算。不要用「同一个 step_id 只允许一次结算」吞掉重试成本；用 `(step_id, attempt)` 做幂等键。

**子 agent。** 子会话有 `x-opencode-parent-session-id`。网关把子会话的费用归到父任务，但保留子任务的明细。这样账单既能按根任务汇总，也能下钻到子 agent。

**Compaction / Title。** 这些是额外的模型调用，也会花钱。插件用 `model.request.kind` 加一个 `x-billing-kind` 头，网关就能区分主调用、压缩、标题、生成，做差异定价或免计费。没有插件也能跑，只是统一按主调用计费。

---

## 九、对账：网关是事实，OpenCode 是投影

网关记网关的账，OpenCode 记 OpenCode 的账，两边一定会有差异。差异不是问题，不能解释才是问题。

三方对账：

```text
网关流水（用户扣费）
   ↕ 按 session + 时间 + tokens
OpenCode SSE（step.ended 的 cost/tokens）
   ↕ 按 request id / 时间
上游账单（provider invoice）
```

常见差异：

- **价格差异**：自己的价目表 vs 上游成本，预期内，记毛利。
- **口径差异**：cache / reasoning 的计费口径，按 provider 规则修正。
- **估算差异**：没有 usage，按估算落账，T+1 修正。
- **重复扣**：幂等键拦截，已发生的退款。
- **漏扣**：上游有账单、本地无流水，补记 + 告警。
- **绕过**：OpenCode 有事件、网关无记录，检查是否直连上游。

网关在响应里回填 `usage.cost` 之后，OpenCode 的 session cost 会和网关的扣费接近；但不要把它当唯一账本。OpenCode 的 cost 是投影，网关的流水才是事实。

---

## 十、落地：先做窄，再做全

**P0（2–3 周）**

- OpenAI Chat 网关 + 流式 usage。
- 按 Step 的 Reserve / Settle / Release。
- 账户、预扣单、流水、Task。
- 读 `x-opencode-session-id` 建任务；Run 用时间窗聚合。
- `GET /v1/balance`。
- 并发、幂等、超时测试。

**P1（3–4 周）**

- Anthropic Messages；usage 归一化。
- Run 级 `max_credits / max_steps / max_duration`。
- `UNKNOWN` + `settle_deadline` + 迟到结算。
- SSE 对账；差异告警。
- 插件：kind 头、余额预检、余额展示。

**P2（4–6 周）**

- OpenAI Responses / Gemini。
- 子 agent 任务树、按项目/团队分摊。
- 付费工具计量（MCP）。
- 多租户、发票、退款、信用额度。
- 影子模式灰度 + 自动对账。

几个不要做：

- 不要在事务里调上游。
- 不要先查余额再扣。
- 不要把 Redis 当账本。
- 不要失败一律全退。
- 不要过期立即释放、迟到成功再扣。
- 不要用浮点算钱。
- 不要为整个 Session 冻结一笔大预算。

---

## 结语：账单是 Agent 的基础设施

Agent 越能干，跑得越久，账单越像基础设施：用户不该感知到它，但它必须在每一次模型调用前后，安静地把钱算对。

这套机制的核心其实只有三句话：

> **Task 管边界，Run 管状态，Step 管钱。**
>
> **预扣保底，结算按实，租约防僵，幂等防重。**
>
> **网关是事实，事件是投影，对账是兜底。**

OpenCode 已经把 Session、Step、usage、cost 和事件流都暴露出来了。剩下要做的，是在它和模型之间放一个可信的收款方，然后把上面这三句话，变成一条条可测试的约束。

---

*本文基于 [opencode](https://github.com/sst/opencode) 2.2.5（commit a687c04）的源码。关键证据：`packages/core/src/session/model-request.ts` 的请求头、`packages/schema/src/session-event.ts` 的 Step 事件、`packages/core/src/session/usage.ts` 的 cost 算法、`packages/plugin/src/effect/session.ts` 的 hook、`packages/server/src/event-feed.ts` 的 SSE。*
