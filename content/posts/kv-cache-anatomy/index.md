---
title: "KV Cache 解剖：从重复计算到分页调度的显存账本"
date: "2026-11-13T16:30:00+08:00"
summary: "从 attention 公式一路讲到 PagedAttention 的块表、状态机和 cache domain 路由：KV Cache 存什么、吃多少显存、为什么 decode 被带宽卡住，以及压缩/量化/驱逐/PD 分离各自在省什么、代价是什么。"
description: "一篇把 KV Cache 的数学、显存账本、分页调度、前缀命中与集群路由串起来的深度笔记。"
tags: ["inference", "architecture"]
slug: "kv-cache-anatomy"
---

这篇文档把 KV Cache 从一条 attention 公式讲到一整条推理流水线：先证明它为什么成立、存的到底是什么，再算清它吃多少显存、为什么 decode 会被显存带宽卡住；然后进入工程侧——PagedAttention 的块表、连续批处理、前缀缓存、上下文压缩对命中的影响、块的生命周期状态机、cache domain 与路由，最后是 GQA/MLA、量化、驱逐、PD 分离这些手段各自在省什么、代价是什么。

如果你已经知道“KV Cache 就是缓存 Key 和 Value”，这篇补的是中间那层：为什么只缓存 K/V 不缓存 Q，为什么 70B 模型 8K 上下文的 cache 能到 2.5 GiB，为什么长上下文 decode 会越来越慢，为什么压缩一段历史会让后面的前缀缓存全部失效，为什么 vLLM 要把 cache 切成 16 个 token 一块，以及为什么“前缀缓存命中”和“语义缓存”完全不是一回事。

先给结论：KV Cache 用显存换计算，把每步 O(S²) 的重算降成 O(S) 的读取；它不改变模型输出，但改变了整个推理系统的瓶颈——从算力转到显存容量与带宽。后面所有的工程问题，都是这笔交易的利息。

## 一、先看一笔重复计算

自回归生成的第 t 步，输入是 x₁…x_t，输出 x_{t+1}。注意力是：

```text
Q = X W_Q,   K = X W_K,   V = X W_V
A = softmax(Q Kᵀ / √d + mask)
O = A V
```

mask 是下三角：位置 i 只能看到 ≤ i 的位置。这里有一个关键事实：**第 t 步算出来的 K_i、V_i（i < t），在第 t+1 步完全不变**。它们只由 x_i 和权重决定，后面的 token 不会回头改写它们。会变的只有新来的 Q_t，以及 softmax 的分母。

所以每步都重算整段前缀，是在重复计算同一批 K/V。没有 cache 时，生成第 t 个 token 要做 O(t²d) 的注意力；生成 N 个 token 合计 O(N³d)。有 cache 后，每步只算新 token 的 Q 和它跟 S 个缓存位置的乘积，合计 O(N²d)。差的是一个 N 倍。FFN 部分是 O(N²d²) 对 O(Nd²)，长生成下注意力项主导。

{{< fig "diagrams/01-recompute-vs-cache.svg" "左边每步重算整段前缀，计算量是下三角；右边只算新 token，前缀的 K/V 从 cache 读。两者输出相同，工作量差一个 N 倍。" >}}

三个常见追问：

1. **为什么不缓存 Q？** Q_t 只用于第 t 步，用完就丢；缓存它没有下一次读取。K/V 会被后续所有步读取，Q 不会。
2. **为什么不缓存 attention 输出？** 第 i 个位置的输出只由 Q_i 和 ≤i 的 K/V 决定，算完就不会变；但下一步要算的是新位置的输出，它需要新 Q 和全部 K/V，旧输出帮不上忙。要复用旧输出，就得同时保留每层的 hidden state，显存更大，而且仍然要存 K/V 才能算新位置；K/V 是既能复用、又足够算新 token 的最小状态。
3. **缓存会不会改变结果？** 不会。数学上等价，只有浮点累加顺序、量化、驱逐会引入误差。

还有一个 RoPE 的细节：缓存的是**旋转之后**的 K。第 t 步的 Q_t 按绝对位置 t 旋转，和缓存的 K_i 做内积，等价于相对位置 t−i。前缀缓存复用时，位置必须从 0 开始对齐；把同一段前缀挪到别的位置，K 里的旋转角就不对了。

## 二、存的是什么，存多大

一层里，K 和 V 的形状是 `[B, H_kv, S, d_head]`。L 层各自独立，所以总元素数：

```text
elements = 2 × L × B × H_kv × S × d_head
bytes    = elements × sizeof(dtype)
```

2 是 K 和 V；H_kv 是 **KV 头数**，不是 Q 头数。MHA 时 H_kv = H；GQA 时 H_kv = 组数；MQA 时 H_kv = 1；MLA 不按头存，换成每层一个 512 维 latent 加 64 维 RoPE key。

把 Llama-3-8B 代进去：L=32，H_kv=8，d_head=128，BF16 每元素 2 字节：

```text
2 × 32 × 8 × 128 × 2 = 131,072 B = 128 KiB / token
```

8K 上下文 = 1 GiB，32K = 4 GiB，128K = 16 GiB。**这还只是一个请求**；batch 8 就是 128 GiB。模型权重本身才 16 GB。

表 1：四个参照点（BF16，单请求）

| 模型 | L | H_kv | d_head | 每 token | 8K | 32K | 128K |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Llama-3-8B（GQA） | 32 | 8 | 128 | 128 KiB | 1 GiB | 4 GiB | 16 GiB |
| Llama-3-70B（GQA） | 80 | 8 | 128 | 320 KiB | 2.5 GiB | 10 GiB | 40 GiB |
| 70B 若用 MHA（64 头） | 80 | 64 | 128 | 2.5 MiB | 20 GiB | 80 GiB | 320 GiB |
| DeepSeek-V2（MLA） | 60 | latent 576 | — | 67.5 KiB | 0.53 GiB | 2.1 GiB | 8.4 GiB |

表里的 MHA 行是假设：同样 70B，只把 GQA 换成 MHA，cache 涨 8 倍。这就是 GQA 在 2023 年之后成为默认配置的原因——它不是“效果更好”，是“同样效果下 cache 只有 1/8”。

MLA 的数字要单独解释：每层每 token 只存 c_KV（512 维）和 k_R（64 维），60 层共 34,560 个元素，BF16 下 67.5 KiB。它不随头数增长，所以 128K 也只有 8.4 GiB。代价是 attention 前要做上投影，计算图比 GQA 复杂；DeepSeek-V2 的论文报告相比自家 67B，KV cache 降了 93.3%，生成吞吐最高 5.76 倍。

{{< fig "diagrams/02-memory-ledger.svg" "每个 token 的 cache 字节数，以及 128K 上下文的总量。右侧把 8B 模型在 128K 时的权重和 cache 放在同一根柱子上：两者几乎相等。" >}}

### 2.1 为什么 decode 会被带宽卡住

Decode 每步只算一个 token，但要把**全部权重**和**这个请求的全部 KV cache** 从 HBM 读进 SM。算术强度大约是每字节 1 次浮点运算，远低于 GPU 的算力/带宽比，所以 decode 是 memory-bound。单请求的每步时间有个下界：

```text
t_step ≥ (W + M_kv(S)) / BW
```

W 是权重字节数，M_kv 是单序列 cache 字节数，BW 是 HBM 带宽。A100 80GB 的 BW 约 2.0 TB/s；8B BF16 权重 16.1 GB，8K cache 1 GiB（约 1.07 GB）：

```text
t_step ≥ (16.1 + 1.07) / 2000 ≈ 8.6 ms  →  约 116 token/s 的上界
```

128K 时 cache 16 GiB，光读 cache 就要 8.6 ms，和读一遍权重一样贵；上界掉到 60 token/s。这就是“长上下文 decode 慢”的物理原因，不是 kernel 写得不好。8B 模型的交叉点大约在 12.3 万 token：从那里开始，读 cache 比读权重更费时间。

Batch 能摊薄权重，但摊不薄 cache：

```text
t_step(B) ≥ (W + B × M_kv) / BW
```

32K 上下文、B=8 时，cache 总共 34.4 GB，加权重 16.1 GB，单步 25.2 ms；总吞吐 8 / 0.0252 ≈ 317 token/s，而单请求只有约 98 token/s。代价是单 token 延迟从 10.2 ms 涨到 25.2 ms。到了 128K，B=8 需要 153 GB，单卡 80GB 根本放不下——**容量先于带宽成为瓶颈**。容量规划里最容易算错的就是这一步：只按“权重 + 单条 cache”估并发，忽略 batch 会把 cache 成倍放大。

## 三、cache 在模型里的位置

{{< fig "diagrams/03-decoder-layer.svg" "一层 decoder 里 KV cache 的读写位置：K 过 RoPE 后写入，V 原样写入；attention 每步读取 1..t 的全部 K/V；Q 只算一次，不落盘。" >}}

流程（每层、每个 token）：

1. hidden state h_t 过 QKV 投影，得到 q_t、k_t、v_t。
2. q_t、k_t 做 RoPE（旋转位置编码），v_t 不做。
3. k_t、v_t 写入 cache 的第 t 个槽位；q_t 不写。
4. 用 q_t 和 cache 里 1..t 的 K 做点积，除以 √d_head，过 softmax，得到 t+1 个权重。
5. 权重乘 cache 里的 V，得到 attention 输出。
6. 过输出投影、残差、FFN，进入下一层。

写成伪代码更直观：

```python
# 一层、一个 decode step；prefill 时 t 换成 0..S-1 的并行版本
q = rope(x @ Wq, pos=t)
k = rope(x @ Wk, pos=t)
v = x @ Wv

k_cache[l, :, t] = k          # 写一个槽位
v_cache[l, :, t] = v

scores = q @ k_cache[l, :, :t+1].T / sqrt(d_head)
out = softmax(scores) @ v_cache[l, :, :t+1]   # 读 1..t+1
```

几个工程含义：

- **cache 是每层独立的**。32 层就有 32 份 K/V；不能只存最后一层。
- **写入发生在 prefill 和每一步 decode**。prefill 一次写 S 个槽位；decode 每次写 1 个。
- **读取发生在每一步 decode**。读的是整段 1..t，不是只读新槽位；这就是 O(S) 的来源。
- **布局影响带宽**。常见布局是 `[L, B, H_kv, S, d_head]`，`d_head` 在最内层；分页实现会把它切成 `[L, 块, H_kv, 16, d_head]`，让同一块内、同一 KV 头的 16 个 token 连续。连续访问才能 coalesce，否则有效带宽掉一半。

精度上，cache 可以和权重不同：权重 BF16，cache FP8/INT8。K 的离群值集中在少数固定 channel，所以 K 适合 per-channel 量化；V 没有明显离群模式，适合 per-token。KIVI 用 2-bit 做到 2.6× 峰值显存下降、2.35–3.47× 吞吐提升，但这是有损的，长上下文和检索类任务要先评测。

## 四、prefill 与 decode：两种完全不同的负载

{{< fig "diagrams/04-prefill-decode.svg" "一条请求的时序：prefill 一次写 S 个槽位，决定 TTFT；decode 每步读全量 cache、追加一个槽位，决定 TPOT。两者优化的是不同资源。" >}}

**Prefill**：把 S 个 prompt token 一次送进模型。矩阵乘是 `[S, d] × [d, d]`，GPU 吃满，compute-bound；同时把每层的 K/V 写进 cache。它决定 TTFT（time to first token）。

**Decode**：每次只送 1 个 token。矩阵乘退化成 `[1, d] × [d, d]` 的 GEMV，GPU 的算力大量闲置，memory-bound；每步读全量 cache，写 1 个槽位。它决定 TPOT（time per output token，也叫 ITL，inter-token latency）。

一条请求的延迟可以拆成：

```text
TTFT = 排队 + prefill + 第一次 decode
总延迟 = TTFT + (输出 token 数 - 1) × TPOT
```

两个指标要分开优化：TTFT 看 prefill 的算力和排队；TPOT 看 decode 的带宽和 batch。把长 prompt 的 prefill 直接塞进 decode 循环，会让所有正在 decode 的请求卡住，TPOT 出现尖刺。解决办法是 **chunked prefill**：把 prefill 切成固定大小的 chunk（比如 512 或 2048 token），每个 iteration 只处理一个 chunk，和 decode 请求拼在同一批里。代价是长 prompt 的 TTFT 略微变长，收益是 TPOT 稳定。

### 4.1 连续批处理

静态批处理按“批”调度：一个 batch 里的请求必须一起开始、一起结束。短请求结束后，它的槽位空着，GPU 等最长的那个。

连续批处理（continuous batching，也叫 iteration-level scheduling）按“每一步”调度：每个 iteration 重新组 batch，做完的请求立刻退出，排队的请求立刻补进来。Orca 在 GPT-3 175B 上报告了同延迟下 36.9× 的吞吐提升；vLLM 在 PagedAttention 之上报告 2–4×。

{{< fig "diagrams/05-continuous-batching.svg" "上：静态批处理要等最长的请求，短请求结束后 GPU 空转。下：连续批处理每个 iteration 重排，完成的请求退出、新请求补位。" >}}

连续批处理的前提是 KV cache 能按请求独立分配、独立释放；这就引出了下一节的分页。

## 五、PagedAttention：把 cache 当虚拟内存管

### 5.1 连续分配的浪费

最直接的做法：每个请求按 `max_seq_len` 预留一段连续显存。问题是请求长度事先不知道：

- 内部碎片：预留 8K，实际只用 300 token，浪费 97%。
- 外部碎片：不同长度的请求反复分配释放，留下大小不一的空洞。
- 无法共享：两个请求有相同的 system prompt，也要各存一份。

vLLM 的论文测到，现有系统里 60%–80% 的 cache 显存被浪费在碎片和重复上。

### 5.2 块表

PagedAttention 借用操作系统的分页：

- 把 cache 切成固定大小的 **block**，默认 16 个 token。
- 每个请求维护一张 **block table**，记录“逻辑块 i → 物理块 j”。
- 物理块不需要连续；attention kernel 按块表 gather。
- 最后一块允许只填一部分，内部碎片最多 15 个 token。

{{< fig "diagrams/06-paged-attention.svg" "两个请求共享前缀块：逻辑块连续，物理块离散；块表负责映射，引用计数负责共享，写入共享块时触发写时复制。" >}}

这样做带来三个能力：

1. **按需增长**：请求多长就分配多少块，不再按 max_len 预留。
2. **前缀共享**：相同前缀的请求指向同一批物理块，引用计数 +1；写时复制（COW）。
3. **并行采样/beam search**：同一 prompt 的多个候选共享前缀块，分叉时才复制。

论文报告浪费降到 4% 以下，端到端吞吐提升 2–4×。这个数字不是算法快了，是同样显存能塞下更多请求。

### 5.3 前缀缓存

前缀缓存是块表的自然延伸：把“已填满的块”按 token 内容做哈希，存进一张全局表。新请求的 prompt 如果和某个缓存前缀逐 token 相同，就直接复用那些块，跳过对应的 prefill。

要点：

- **只缓存填满的块**。半块的内容还会变，不能作为稳定前缀。
- **哈希要包含模型、adapter、tokenizer、位置**。换模型或换 LoRA 必须 miss，否则输出会错。
- **命中率取决于流量结构**：固定 system prompt、few-shot 示例、多轮对话的历史，命中率很高；一次性随机 prompt 基本为 0。
- **前缀缓存不是语义缓存**。它要求 token 级完全相同；“意思差不多”不算命中。

命中后的收益主要在 TTFT：prefill 的计算量从 O(S²) 降到 O(S_new × S)，因为旧 token 的 K/V 直接读现成的，只需要新 token 去 attend。对 8K prompt、90% 命中，在 prefill 计算主导的场景，TTFT 可能降一个数量级。

### 5.4 压缩之后，前缀缓存还剩多少

上下文压缩和 KV Cache 压缩是两件事。前者改的是送进模型的 token 序列，后者改的是已经存下来的 K/V 的精度、数量或位置。改 token 序列会让前缀缓存失效；改精度或数量通常不改变 token 的身份，命中逻辑可以保留。

前缀缓存匹配的是“从第一个 token 开始、连续完整的块”。压缩如果改写了 system、开头示例或历史摘要，命中点就停在第一个被改写的块之前；后面的 token 即使没变，也因为父块哈希对不上而无法命中。

表 2：不同压缩策略对前缀命中的影响

| 做法 | 前缀是否稳定 | 命中情况 | 主要代价 |
| --- | --- | --- | --- |
| 只压缩新输入，历史不动 | 稳定 | 历史全命中，只 prefill 新输入 | 压缩收益有限 |
| 摘要追加，不重写旧摘要 | 单调增长 | 上一轮摘要仍命中 | 摘要越堆越长，最终要 compaction |
| 每轮重写历史摘要 | 每轮都变 | 只命中稳定 system | 每轮多 prefill 摘要 |
| 压缩 system / 开头 | 第一块就变 | 几乎全 miss | 最伤命中率 |
| 每 N 轮 compaction | 周期稳定 | 两次 compaction 之间命中高 | 每次 compaction 一次大 miss |
| KV 量化 | 不变 | 命中逻辑不变 | 复用有损 K/V，精度下降 |
| 驱逐 / 丢 token | 变了 | 被丢块之后无法命中 | 信息不可恢复 |

算一笔账。system 2K、history 30K、每轮新增 1K：

- 不压缩：命中 32K，prefill 1K。
- 每轮把 history 压成 3K 摘要：system 2K 命中，摘要 3K + 新输入 1K 要 prefill，共 4K；命中率从 97% 掉到 33%。
- 摘要追加而不是重写：上一轮摘要已在 cache，命中 5K，prefill 1K；但摘要会线性增长，迟早要 compaction。

命中率只说明“有多少 token 不用算”，不说明“还剩多少 token 要算”。33% 的命中率听起来差，但 4K prefill 仍然远小于不压缩时的 33K。反过来，热 cache 下每轮重写摘要，会把本来 1K 的 prefill 变成 4K，压缩反而更慢；冷 cache 下，压缩后的 4K 比 33K 便宜。要不要压缩，得看 cache 温度和 prefill 省下的时间，不能只看命中率。

如果要在压缩的前提下保住前缀缓存，按这五条做：

1. 稳定前缀放最前，且 token 级不变：system、工具定义、few-shot 示例不要每轮重排。
2. 只压新输入，或把摘要追加到末尾；不要每轮重写历史摘要。
3. 把 compaction 做成周期任务，而不是每轮都做；两次 compaction 之间让前缀保持稳定。
4. cache key 要包含模型、adapter、dtype/量化、位置编码、压缩模板；换任何一个都当作新前缀。
5. 监控 `cache_hit_tokens`、`prefill_tokens`、TTFT 三个数；命中率单独看会骗人。

最后区分两种“压缩”：KV 量化把每个元素从 BF16 压到 INT8/INT4，token 序列没变，在同一 engine、同一 cache 格式下前缀缓存照样命中，但复用的是有损的 K/V；驱逐把整块 token 丢掉，缓存命中链在丢掉的位置断开，信息也回不来。前者是精度问题，后者是信息问题。

## 六、块的生命周期：一个状态机

{{< fig "diagrams/07-block-lifecycle.svg" "KV 块的状态机：Free → Writing → Sealed → Shared/Evictable，以及写时复制和请求侧抢占（preempt）的位置。三条不变量是排查泄漏和前缀污染的依据。" >}}

把实现里的状态归并一下，可以得到五个：

- **Free**：在空闲链表里，可以被分配。
- **Writing**：已分配给某个请求，正在被 prefill 或 decode 写入；此时内容还不完整，不能被共享。
- **Sealed**：写满了，内容不可变；此时才可以计算哈希、放进前缀缓存。
- **Shared**：引用计数 > 1，被多个请求的 block table 指向；只读。
- **Evictable**：引用计数 = 0，但还留在前缀缓存里，可以被命中，也可以被回收。

迁移：

- 分配：Free → Writing。
- 写满：Writing → Sealed（ref=1）。
- 共享：Sealed → Shared（ref++）。
- 释放：Shared → Sealed（ref-- 到 1）；Sealed → Evictable（ref=0）。
- 命中：Evictable → Sealed（ref=1），跳过 prefill。
- 回收：Evictable → Free（LRU 或显存压力）。
- 写时复制：Shared 且未填满时要写，分配一个新块、拷贝旧内容，旧块 ref--，新块进入 Writing；如果旧块已经填满，新 token 直接写新块，不需要拷贝。

请求侧还有一条独立的状态线：Waiting → Prefill → Decode → Finished。显存不够时，调度器会 **preempt**：把某个请求的块换出到 CPU（swap）或直接丢掉（recompute），请求回到 Waiting；恢复时重新 prefill 或换入。短请求用 recompute 更划算，长请求用 swap 更划算——换出再换入的代价是 PCIe 带宽，重算的代价是 GPU 算力。

三条不变量，实现和排障都靠它们：

1. 一个 Sealed 块的 content hash 不变；变了就是有人绕过 COW 写了共享块。
2. 一个块的 ref_count 等于所有 block table 里指向它的条目数；对不上就是泄漏或重复释放。
3. 只有 ref_count=0 的块能进 Free 链表；提前回收会让仍在 decode 的请求读到别人的数据。

## 七、放不下时：四条路

### 7.1 少存：改结构

{{< fig "diagrams/08-structure-spectrum.svg" "从 MHA 到 GQA、MQA、MLA、滑动窗口：每 token cache 的下降幅度，以及各自付出的表达力或实现复杂度代价。" >}}

- **MHA**：每个 Q 头一对 K/V，cache 最大，表达力最强。
- **MQA**：所有 Q 头共享一对 K/V，cache 降到 1/H；质量会掉，训练也可能不稳。
- **GQA**：分成 G 组，每组共享一对 K/V；cache 降到 G/H。Llama-3-8B 是 8/32 = 1/4，70B 是 8/64 = 1/8。GQA 论文的说法是：质量接近 MHA，速度接近 MQA。
- **MLA**：把 K/V 压成一个 latent，attention 前再上投影；cache 只和 latent 维度、层数有关，和头数无关。DeepSeek-V2 用 576 维/层，128K 只有 8.4 GiB。
- **滑动窗口**：只保留最近 W 个 token 的 K/V，cache 上界 O(W)；代价是窗口外的信息直接看不到。多层堆叠能扩大有效感受野，但不是无损的全局注意力。
- **稀疏注意力**：每个 query 只选 top-k 个 token 计算；它省的是**计算**，不是**存储**——除非同时驱逐 cache。DeepSeek 的 DSA、各种 block-sparse 方案都属于这一类。
- **线性/混合注意力**：用 recurrent state 替代不断增长的 KV，cache 变成 O(1)；代价是精确回忆能力下降，通常要混搭少量全注意力层。

### 7.2 存紧：量化

FP8 是现在最稳的一档：8-bit，误差小，Hopper 之后有原生支持。INT8/INT4 更激进；KIVI 的结论是 K 按 channel、V 按 token，2-bit 也能保住大部分质量，但这是“大部分”，不是“全部”。量化后的 cache 仍然要读，所以它同时省容量和带宽；如果 kernel 不支持低精度 attention，反量化会吃掉收益。

### 7.3 丢掉：驱逐

- **StreamingLLM**：保留最前面 4 个 attention sink token，加最近 W 个 token，中间全部丢掉；论文跑到 400 万 token，比滑动窗口重算快 22.2×。它解决的是“流式对话不崩”，不是“长文精确检索”。
- **H2O**：按累计 attention 分数保留 heavy hitters，加最近 token，20% 预算就能保住大部分质量。
- **SnapKV / 类似方法**：在 prefill 末尾根据 attention 选择要保留的 token。

驱逐的共同风险是**不可逆**：被丢掉的 token 不会回来，后面如果需要它，只能重新 prefill 或接受错误。做 RAG、代码、长文档问答时，先测“丢 50% 会不会掉点”，再上线。

### 7.4 换出与分布式

{{< fig "diagrams/09-pd-separation.svg" "PD 分离：prefill 池和 decode 池各自扩缩容，KV 通过 NVLink 或 RDMA 传输。传输时间是否小于 prefill 省下的时间，决定这套架构是否划算。" >}}

- **CPU/NVMe offload**：把冷前缀的块换到主机内存或 SSD，需要时再换入。PCIe 4.0 x16 约 64 GB/s，是 HBM 的 1/30；换入 1 GiB 要 16 ms，只适合“很久才用一次”的前缀。LMCache 这类系统做的就是分层缓存。
- **张量并行（TP）**：KV 头按 rank 切分；H_kv=8、TP=8 时每卡存 1 个头。TP 大于 H_kv 时只能复制，cache 不再随卡数下降。
- **流水并行（PP）**：每层在不同 stage，cache 也跟着分层；stage 之间只传 activation，不传 cache。
- **PD 分离**：prefill 池和 decode 池分开，prefill 算完把 KV 传给 decode。好处是两类负载独立扩缩容，长 prompt 不会卡住 decode；代价是 KV 传输。70B、8K 上下文的 cache 是 2.5 GiB，NVLink 900 GB/s 约 3 ms，100 Gb RDMA 约 0.2 s——拓扑决定这笔账是否划算。Mooncake、DistServe 是这条路的代表。

表 3：手段与代价

| 手段 | 省什么 | 代价 | 适用 |
| --- | --- | --- | --- |
| MQA/GQA | 容量、带宽 | 表达力、训练难度 | 新模型默认 |
| MLA | 容量（数倍到数十倍） | 实现复杂、上投影算力 | 自研模型 |
| 滑动窗口 | 容量（有上界） | 窗口外信息丢失 | 流式、局部任务 |
| 量化 | 容量、带宽 | 精度、kernel 支持 | 显存紧张 |
| 驱逐 | 容量 | 不可逆的信息丢失 | 流式、长对话 |
| offload | 显存容量 | PCIe 延迟 | 冷前缀 |
| PD 分离 | TTFT/TPOT 解耦 | 传输、调度复杂 | 大规模服务 |

### 7.5 cache 落在哪：路由、实例与 HBM

{{< fig "diagrams/10-cache-to-hardware.svg" "KV Cache 的三层落点：逻辑块表只存编号，Engine Replica 持有 cache domain，真正的 K/V 按 TP 切分落在 Node/GPU 的 HBM；跨 domain 走 KV transfer，冷前缀可下沉到远端池。P3/P7/P1 与 TP=4 是示意。" >}}

把视角从单机拉到集群，KV Cache 的落点可以分三层：逻辑块表、推理实例（cache domain）、GPU/HBM。块表只存“L0→P3”这样的编号，P3 真正落在哪张卡、哪块 HBM，由实例的分配器和并行策略决定。

先定义 cache domain：能共享同一份前缀索引、并以足够低的延迟读到同一批 KV 块的引擎集合。一个 vLLM engine 通常就是一个 domain；TP/PP 组是一个逻辑实例，组内切分同一段 cache，不要把它拆成单卡看。两个 engine 即使跑在同一张 GPU 上，只要前缀索引不共享，就是两个 domain；反过来，一个 TP 组跨 4 张卡，仍然是一个 domain。

这解释了随机路由的命中率：N 个副本、每个副本各自维护前缀缓存，用户历史随机落到某一个副本，命中概率约 1/N。会话粘滞能把命中率拉回接近 100%，代价是热点和故障丢 cache；prefix-aware routing 按前缀哈希选副本，利用率更高，但需要一张全局索引；KV 传输/共享存储把 cache 搬到 decode 所在 domain，代价是 2.5 GiB/8K 的搬运时间；PD 分离把 prefill 和 decode 拆池，decode 池必须能低延迟读到 prefill 产出的 cache。

表 4：路由与共享方案

| 方案 | 命中范围 | 代价 | 适用 |
| --- | --- | --- | --- |
| session sticky | 同一会话固定副本 | 热点、故障即冷 | 会话型产品 |
| prefix-aware routing | 按前缀找副本 | 全局索引、冷启动 | 多副本、共享 system |
| KV transfer | 跨 domain 搬运 | 2.5 GiB/8K；NVLink 3ms / RDMA 0.2s | 跨池、跨节点 |
| 共享远端池 | 多 domain 读同一池 | 池带宽、协议、TTL | 大规模集群 |
| PD 分离 | prefill 产物给 decode | 传输、调度复杂 | 长 prompt、分离扩缩容 |

三个容易踩的坑：

- **“同一张 GPU”不等于“同一个 cache domain”**。多个 engine 可以共享 HBM，但前缀索引各自独立；路由到同一张卡也可能 miss。
- **“同一会话”不等于“同一个 domain”**。跨轮可以迁移，decode 循环里不要每一步迁移；迁移成本要摊到后续 token 上。
- **cache key 不一致，路由再准也没用**。模型、adapter、dtype/量化、位置编码、block size、压缩模板，任意一项不同都应当视为不同前缀。

一句话：路由选的是能读到这段 cache 的 domain；选不中，就只能重新 prefill，或者付一次 KV 传输。

## 八、容量规划：把公式落到卡上

给一个 8×A100 80GB（80 GiB/卡）、70B BF16、TP=8 的例子：

```text
总显存            = 8 × 80 GiB = 640 GiB
权重              = 140 GB ≈ 130 GiB（每卡 16.3 GiB）
可用（90%）       = 576 GiB
cache 预算        = 576 - 130 ≈ 446 GiB
单序列 8K cache   = 2.5 GiB
可容纳序列        ≈ 446 / 2.5 ≈ 178
单序列 32K        ≈ 44
单序列 128K       ≈ 11
```

这是**容量上限**，不是吞吐上限。真正能跑多少并发，还要看 TTFT/TPOT 的 SLO：batch 越大，每步读的 cache 越多，TPOT 越长。通常会在“容量”和“延迟”之间取一个工作点，比如 70% 显存利用率、TPOT p95 小于 50 ms。

监控四个数：

- **TTFT p50/p95**：prefill 和排队的健康度。
- **TPOT/ITL p95**：decode 的带宽和 batch 是否过载。
- **KV cache 利用率 + 前缀命中率**：容量是否被浪费。
- **preemption 次数**：显存是否长期贴着上限跑。

排障表：

| 现象 | 先查 | 常见修法 |
| --- | --- | --- |
| 长上下文 OOM | 单序列 cache × 并发 | 降并发、GQA/MLA、量化、滑动窗口 |
| TTFT 尖刺 | 是否有长 prefill 插队 | chunked prefill、prefill 池隔离 |
| TPOT 随 batch 线性上涨 | cache 带宽打满 | 降 batch、FP8 cache、PD 分离 |
| 前缀命中率低 | 模板/顺序是否稳定 | 固定 system prompt、把可变内容后置 |
| preemption 频繁 | 显存利用率是否 >95% | 留 10–20% 余量、限流 |
| decode 远低于理论上界 | kernel、布局、批大小 | 用 paged kernel、检查连续访问 |

## 九、几个容易混的点

1. **KV cache 不改变模型输出**。它只是把已经算过的 K/V 留下来；除非量化、驱逐、或者位置处理错了。
2. **FlashAttention 不省 KV cache**。它省的是 attention 中间矩阵 S×S 的 HBM 读写，把 attention 做成流式；KV 该存还得存。
3. **KV cache 不是 context window**。context window 是模型能接受的最大 token 数（位置编码和训练决定）；KV cache 是这些 token 在显存里的状态。前者是“能看多长”，后者是“看这么长要多少显存”。
4. **前缀缓存不是语义缓存**。它要求 token 级完全相同；改一个空格就可能全 miss。
5. **量化不是免费的**。2-bit 能跑，不等于在你的任务上不掉点；长上下文、检索、代码任务对 K 的误差更敏感。
6. **驱逐不是“只丢不重要的 token”**。attention 权重低不等于信息不重要；被丢的信息不可恢复。
7. **batch 不是越大越好**。长上下文下，KV 带宽会让吞吐提前饱和，延迟还会线性变差。
8. **PD 分离不是万能**。KV 传输超过 prefill 省下的时间，就不划算；小模型、短上下文通常没必要。
9. **压缩上下文和压缩 KV cache 是两件事**。前者改 token 前缀，会让改动点之后的完整块全部 miss；后者改精度或数量，命中逻辑通常不变，但复用值可能已经有损。
10. **会话粘滞不是绑卡**。要的是 cache 亲和：请求落到能读到这段 cache 的 domain；同一张卡上的两个 engine 也可能各自为政。

## 十、收束

KV Cache 的本质是一笔交易：用 O(N) 的显存和 O(N) 的读取，换掉 O(N²) 的重算。它把推理的瓶颈从算力搬到显存容量和带宽，于是后面所有的工程问题都围绕五个词展开：

- **少存**：MQA/GQA/MLA、滑动窗口、稀疏/线性注意力。
- **存紧**：FP8/INT8/INT4、K per-channel、V per-token。
- **放得下**：PagedAttention、前缀共享、COW、驱逐、offload。
- **找得到**：前缀缓存、cache-aware routing、KV 传输、共享池。
- **传得快**：连续批处理、chunked prefill、PD 分离、NVLink/RDMA。

选型顺序也别反：先看模型结构能省多少，再算容量，再选调度和前缀缓存，最后才动量化和驱逐。反过来做，通常是在给一个已经装不下的系统调 kernel。

## 参考资料

- Kwon et al., [Efficient Memory Management for Large Language Model Serving with PagedAttention](https://arxiv.org/abs/2309.06180), SOSP 2023.
- Yu et al., [Orca: A Distributed Serving System for Transformer-Based Generative Models](https://www.usenix.org/conference/osdi22/presentation/yu), OSDI 2022.
- Ainslie et al., [GQA: Training Generalized Multi-Query Transformer Models from Multi-Head Checkpoints](https://arxiv.org/abs/2305.13245), EMNLP 2023.
- Shazeer, [Fast Transformer Decoding: One Write-Head is All You Need](https://arxiv.org/abs/1911.02150), 2019.
- DeepSeek-AI, [DeepSeek-V2: A Strong, Economical, and Efficient Mixture-of-Experts Language Model](https://arxiv.org/abs/2405.04434), 2024.
- Liu et al., [KIVI: A Tuning-Free Asymmetric 2bit Quantization for KV Cache](https://arxiv.org/abs/2402.02750), ICML 2024.
- Xiao et al., [Efficient Streaming Language Models with Attention Sinks](https://arxiv.org/abs/2309.17453), ICLR 2024.
- Zhang et al., [H2O: Heavy-Hitter Oracle for Efficient Generative Inference of Large Language Models](https://arxiv.org/abs/2306.14048), NeurIPS 2023.
- Dao et al., [FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness](https://arxiv.org/abs/2205.14135), NeurIPS 2022.
- Zhong et al., [DistServe: Disaggregating Prefill and Decoding for Goodput-optimized Large Language Model Serving](https://arxiv.org/abs/2401.09670), OSDI 2024.
- Qin et al., [Mooncake: A KVCache-centric Disaggregated Architecture for LLM Serving](https://arxiv.org/abs/2407.00079), 2024.
- Zheng et al., [SGLang: Efficient Execution of Structured Language Model Programs](https://arxiv.org/abs/2312.07104), 2024.
- vLLM, [Prefix Caching](https://docs.vllm.ai/en/latest/features/prefix_caching.html), 官方文档.
- Grattafiori et al., [The Llama 3 Herd of Models](https://arxiv.org/abs/2407.21783), 2024.
