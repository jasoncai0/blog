---
title: "BGE 与 ANN 检索：从 embedding 到十亿级索引"
date: "2026-11-10T12:15:00+08:00"
summary: "把 BGE 的 CLS、对比学习、M3 三模式，与 IVF/PQ/HNSW 的召回机制串起来：为什么 recall@k 在索引之前就被 embedding 决定，以及过滤、更新、调参和面试里最容易答错的点。"
description: "把 BGE 的 CLS、对比学习、M3 三模式，与 IVF/PQ/HNSW 的召回机制串起来：为什么 recall@k 在索引之前就被 embedding 决定，以及过滤、更新、调参和面试里最容易答错的点。"
tags: ["rag", "embedding", "ann"]
slug: "bge-ann-vector-retrieval"
---

**这篇文档在做什么：** 把 RAG 检索链路拆成两段——BGE 负责“把语义压进一个向量”，ANN 负责“在海量向量里把候选捞出来”——然后回答三个问题：BGE 的向量是怎么训练出来的，IVF/PQ/HNSW 到底在省什么、又在哪里丢召回，以及线上真正会踩的坑和面试里最容易被追问的点。它不是 API 手册，也不是 benchmark 复读；重点是把公式、参数和工程后果对上。

如果你只做过“调一个向量库、塞进去、搜 top-k”，这篇会补上中间那层：为什么同样的 embedding，换索引后 recall 会掉；为什么加了过滤条件，HNSW 会突然变慢；为什么 `recall@10` 看起来很高，答案还是错。

## 一、先看全局：两条流水线，一个召回上限

检索系统有两条完全不同的流水线。离线这条可以慢、可以批、可以全量重建；在线这条必须快、必须稳、必须能降级。很多故障的根因，是把两条线的约束混在了一起。

{{< fig "diagrams/01-pipelines.svg" "离线建库与在线查询。BGE 决定“语义上有没有”，ANN 决定“能不能被找到”，rerank 只能重排已经召回的候选。" >}}

离线侧的关键动作是：解析文档、按结构切块、用 BGE 编码、把向量和元数据一起写入索引。在线侧是：改写 query、加检索指令、编码 query、ANN 召回、rerank、拼上下文。两条线共享同一个向量空间，所以**模型、版本、归一化方式、max_length 必须严格一致**。

这里先立一个后面会反复用的不等式：

```text
最终命中上限 = P(文档在语义上可被召回) × P(索引把它的近邻返回)
```

第一项由 embedding 模型、切块方式、query 改写、指令模板决定；第二项由索引结构、参数、过滤策略决定。两项是乘法关系：任何一项接近 0，另一项再高也没用。rerank 只作用在已经召回的候选上，它能把第 37 名提到第 1 名，但没法把根本没进候选集的文档变出来。

所以排障时要先问：**是“没召回”，还是“召回了但排得不好”？** 这两个问题的修法完全不同。前者要动 embedding、切块、索引或过滤；后者才轮到 reranker、prompt 和上下文排序。

常用的四个指标也要分开看：

| 指标 | 回答的问题 | 典型采集方式 |
| --- | --- | --- |
| `recall@k` | 真相关文档有多少进了前 k | 人工标注或强模型标注一批 query，跑精确近邻作 ground truth |
| `nDCG@k` / `MRR` | 相关文档排得靠不靠前 | 有分级相关性标注时用 nDCG；只有一个正确答案时用 MRR |
| `p99` 延迟 | 最慢的那 1% 用户等多久 | 线上直方图，不要只看平均 |
| 内存 / 构建时间 / 新鲜度 | 这套索引养不养得起 | 单向量字节数 × 数量 + 图/倒排开销 |

`recall@k` 的定义值得单独写一遍，因为后面所有索引参数都在围绕它做交易：

```text
recall@k = |ANN 返回的 top-k ∩ 精确 top-k| / k
```

注意它衡量的是“集合重叠”，不是“顺序正确”。一个索引可以把 recall@10 做到 0.95，但把最相关的那条排在第 10 位；这时答案质量仍然可能很差，需要 rerank 或更大的 k 来补。反过来，recall@10 只有 0.6 时，再怎么调 rerank 也很难稳定。

## 二、BGE 的实现：一个 CLS 向量，三阶段训练

BGE 是 BAAI General Embedding 的缩写，不是单个模型，而是一族同架构、同训练范式的双塔 encoder。C-Pack 论文（arXiv 2309.07597）把它拆成三部分：C-MTEB 评测集、C-MTP 训练数据、C-TEM 模型。理解 BGE 的关键，不是记住参数量，而是理解“为什么一个 1024 维的 CLS 向量能承载语义”。

### 2.1 推理：双塔、CLS、L2

BGE 的底座是 BERT 类 encoder（中文 v1.5 是 BERT-base/large 结构，M3 是 XLM-RoBERTa-large）。输入一段文本，经过若干层 self-attention，取最后一层 `[CLS]` 位置的隐状态，再做 L2 归一化，得到单位向量：

```text
h = Encoder(tokens)
e = h[CLS] / ||h[CLS]||₂
score(q, p) = q · p = cos(q, p)
```

因为向量已经归一化，内积就等于余弦相似度。这一点看起来只是实现细节，实际上决定了后面索引的 metric 选择：用 Faiss 时应该建 `IndexFlatIP`（inner product），而不是默认的 L2；如果用 L2，排序在数学上仍然等价（`||q-p||² = 2 - 2q·p`），但阈值、分数解释和某些索引的默认行为会变。

{{< fig "diagrams/02-bge-encoder.svg" "BGE 的双塔推理与三阶段训练。文档侧离线编码，query 侧在线编码；对比学习用 batch 内其他样本作为负例。" >}}

双塔（bi-encoder）的核心取舍是：**query 和 passage 在编码阶段互不可见**。这样做的好处是文档向量可以全部预计算，在线只需要编码一次 query，然后做向量检索；代价是模型没有 token 级交互，细粒度匹配（数字、否定、实体关系）会丢，所以后面通常还要一个 cross-encoder reranker。

这也是为什么“embedding 模型”和“reranker”不能互相替代：

- bi-encoder 把整段文本压成一个点，适合**召回**，追求的是“别漏”；
- cross-encoder 把 query 和 passage 拼在一起过模型，适合**精排**，追求的是“排得准”，但每个候选都要一次前向，成本随候选数线性增长。

工程上的常见组合是：ANN 召回 50–200 条，cross-encoder 重排到 5–10 条，再交给 LLM。召回阶段用 1024 维 BGE，精排阶段用 bge-reranker，各自做擅长的事。

还有一个容易忽略的点：**池化方式必须和训练时一致**。BGE 用 CLS，不是因为 CLS 天然比 mean pooling 好，而是因为对比学习就是在这个向量上做的。如果你把 BGE 的 CLS 换成 mean pooling，再去比较余弦，等于换了一个没有训练过的表示，效果会掉，而且掉得没有规律。同理，`max_length`、是否加 instruction、是否归一化，都要和模型卡保持一致。

### 2.2 三阶段训练：先学会重建，再学会区分，最后学会听指令

BGE 的训练不是“拿一个 BERT，直接跑对比学习”这么简单。C-Pack 把它拆成三个阶段，每个阶段解决一个不同的问题。

**第一阶段：RetroMAE 风格的预训练。** 把原始文本做污染（mask、截断、替换），让 encoder 编码污染后的文本，再用一个轻量 decoder 还原干净文本。这个目标看起来像 MAE，目的不是直接做检索，而是让 `[CLS]` 承载“可重建的语义”：如果 CLS 能帮助 decoder 还原原文，它就必须保留足够的词汇、句法和篇章信息。对中文场景来说，这一步还承担了领域适配的作用。

**第二阶段：大规模对比学习。** 用 100M 无标注 pair 做 InfoNCE。每个 pair 是 `(query, passage)` 的正例，batch 内其他样本自动成为负例。损失可以写成：

```text
L_i = -log( exp(sim(q_i, p_i) / τ) / Σ_j exp(sim(q_i, p_j) / τ) )
```

其中 `τ` 是温度，控制分布的尖锐程度。BGE 的公开训练配置里 batch size 达到 19,200，这个数字不是噱头：batch 越大，每个正例能见到的负例越多，InfoNCE 的梯度估计越接近“在全量数据上做对比”。反过来，batch 太小，负例太少，模型很容易把“同一个 batch 里的所有样本”都推远，学不到细粒度区分。

但大 batch 也带来两个问题。第一是**假负例**：batch 里可能有一条文本和 query 其实相关，却被当成负例推远。第二是**温度敏感**：τ 太小会让模型只盯着最难的负例，训练不稳定；τ 太大则所有负例的权重接近均匀，区分度不够。公开实现里 τ 常在 0.01–0.05 量级，具体值要以训练配置为准，不要把它当成论文里的固定常数。

**第三阶段：多任务 / 指令微调。** 用 838K 标注 pair 做有监督训练，任务不只有 retrieval，还包括分类、STS、pair classification、reranking 等。多任务的作用是防止模型在单一检索目标上过拟合；指令微调的作用是让模型理解“这是一条搜索 query”，而不是一段普通文本。

指令只加在 query 上，这是 BGE 使用中最容易错的地方。中文 v1.5 的检索指令是：

```text
为这个句子生成表示以用于检索相关文章：
```

英文 v1.5 是：

```text
Represent this sentence for searching relevant passages: 
```

模型卡写得很明确：**instruction 只加给 query，永远不要加给 passage**。原因是 query 和 passage 的分布本来就不对称：query 短、口语、带指代，passage 长、陈述、信息密度高。指令的作用是把 query 往“检索意图”的分布上推；如果 passage 也加同样的指令，等于把两侧一起推，反而抹掉了这种不对称，训练时学到的边界就乱了。

v1.5 相比 v1 的一个改进是：即使不加指令，相似度分布也更合理，检索效果更好。但在 short query → long passage 的场景，官方仍然建议加指令。到了 BGE-M3，模型已经不需要指令；再加反而可能引入分布偏移。

### 2.3 模型谱系：不要只看参数量

BGE v1.5 的常见规格如下（维度、层数、参数量来自 HuggingFace `config.json`，不是估算）：

| 模型 | 维度 | 层数 | 参数量 | 最大长度 | 适用 |
| --- | ---: | ---: | ---: | ---: | --- |
| `bge-small-zh-v1.5` | 512 | 4 | 24M | 512 | 中文、成本敏感、低延迟 |
| `bge-base-zh-v1.5` | 768 | 12 | 102M | 512 | 中文通用 |
| `bge-large-zh-v1.5` | 1024 | 24 | 326M | 512 | 中文、效果优先 |
| `bge-small-en-v1.5` | 384 | 12 | 33M | 512 | 英文、成本敏感 |
| `bge-base-en-v1.5` | 768 | 12 | 109M | 512 | 英文通用 |
| `bge-large-en-v1.5` | 1024 | 24 | 335M | 512 | 英文、效果优先 |
| `BAAI/bge-m3` | 1024 | 24 | 约 5.6 亿 | 8192 | 多语言、长文、三模式 |

中文 small 只有 4 层，这是很多人第一次看表会愣一下的地方：它并不是“把 large 等比例缩小”，而是用了更浅的层数换吞吐。层数少不代表一定差，C-MTEB 上 small-zh 的平均分是 57.82，base 是 63.13，large 是 64.53；从 base 到 large 的边际收益已经明显变小。选型时不要默认“large 一定划算”，要先看你的 query 分布和延迟预算。

BGE-M3 则是另一条路线：底座换成 XLM-RoBERTa-large，用 RetroMAE 把最大长度从 512 扩到 8192，训练数据扩展到 1.2B pair、194 种语言。它不是“更大的 BGE”，而是“一个 encoder，三种检索头”。

### 2.4 BGE-M3：dense、sparse、multi-vector 三合一

BGE-M3 的核心设计是：同一个 Transformer 编码器，接三个不同的输出头，分别产生 dense 向量、sparse 词权重、multi-vector token 向量。三者在检索时可以用不同方式组合。

{{< fig "diagrams/03-bge-m3.svg" "BGE-M3 的三种检索头与自蒸馏。dense 管语义、sparse 管词面、multi-vector 管 token 级交互，最终分数相加。" >}}

**Dense 头**和 v1.5 一样：取 `[CLS]`，归一化，内积。

```text
e_q = normalize(H_q[CLS])
s_dense = <e_q, e_p>
```

**Sparse 头**是学习出来的词权重，不是 BM25。对每个 token 的隐状态过一个线性层再 ReLU，得到该 token 的权重；query 和 passage 在共享词项上相乘再求和：

```text
w_q(t) = ReLU(W_lexᵀ H_q[i])     # 同一 token 多次出现取 max
s_lex = Σ_{t ∈ q ∩ p} w_q(t) · w_p(t)
```

它保留了词面匹配的能力，对专有名词、型号、代码标识符特别有用；但权重是模型学出来的，不是 IDF，所以不要指望它和 BM25 的分数可比。

**Multi-vector 头**是 ColBERT 式的 late interaction：每个 token 出一个向量，query 的每个 token 和 passage 的所有 token 取最大相似度，再平均：

```text
E_q = normalize(W_mulᵀ H_q)
s_mul = (1/N) Σ_i max_j E_q[i] · E_p[j]
```

它比单个 CLS 向量保留了更多 token 级信息，但存储成本高：一条 passage 不再是一个 1024 维向量，而是 `token 数 × 维度`。所以 M3 的推荐用法是：dense 和 sparse 负责候选召回，multi-vector 只在候选集上做重排；不要一上来就给全库建 ColBERT 索引，除非你算过存储。

三种分数不在同一量纲。M3 论文里的做法是先把三者相加得到 teacher 分数，再用 self-knowledge distillation 让每个头去拟合这个“融合后的软标签”：

```text
s_inter = s_dense + s_lex + s_mul
L'_* = -p(s_inter) · log p(s_*)
L' = (L'_dense + L'_lex + L'_mul) / 3
L_final = L + L'
```

这样做的目的，是避免三个目标互相打架：dense 的梯度不必压过 sparse，sparse 也不必压过 multi-vector；三者共同提供一个更稳的教师信号。线上融合时，M3 的最终分数也是三者相加：

```text
s_rank = s_dense + s_lex + s_mul
```

但“相加”不等于“直接加原始分数”。不同模型、不同语言的分数尺度不一样，生产系统里通常要先归一化或学习权重；如果直接加，某一项可能主导排序。

M3 还有一个长文技巧叫 MCLS：如果模型没有经过长文微调，就在长文本里每 256 个 token 插一个 `[CLS]`，最后把所有 `[CLS]` 的隐状态取平均。这样比只用第一个 `[CLS]` 更能覆盖长文的不同片段。它是推理期的补丁，不是训练目标；能缓解长文信息被一个向量压扁的问题，但不能替代合理的切块。

### 2.5 一段最小可用代码

用 `sentence-transformers` 加载 BGE，手动加 query 指令，是最不容易出错的写法：

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("BAAI/bge-large-zh-v1.5")
model.max_seq_length = 512

INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

query = "BGE 的 batch size 为什么这么大？"
passages = [
    "BGE 使用大规模对比学习，batch size 达到 19200。",
    "HNSW 是一种多层可导航小世界图。",
]

q = model.encode([INSTRUCTION + query], normalize_embeddings=True)
p = model.encode(passages, normalize_embeddings=True)

scores = q @ p.T
print(scores)
```

三个细节：

1. `normalize_embeddings=True` 不能省；否则 `q @ p.T` 是内积，不是余弦。
2. 文档侧不要加 `INSTRUCTION`；只有 query 加。
3. `max_seq_length=512` 是 v1.5 的上限；超过会被截断，长文要先切块。M3 可以设到 8192，但显存和延迟会显著上升。

如果用 FlagEmbedding，`FlagModel.encode_queries()` 会自动给 query 加指令，`encode_corpus()` 不会；这正好对应“query only”的约定。生产代码里建议把这两个入口封装成两个函数，禁止业务方直接调 `encode()`，从 API 层面杜绝“文档也加指令”的错误。

## 三、ANN 索引：用一点召回换数量级的速度

### 3.1 为什么不能暴力扫

精确最近邻（flat / brute force）的成本很直白：每个 query 要和 N 个向量做内积，每个内积 d 次乘加，复杂度 `O(Nd)`。存一条 1024 维 fp32 向量要 4 KB，100 万条就是 4 GB；1 亿条就是 400 GB。查询时把 4 GB 全读一遍已经不现实，更不用说 400 GB。

ANN（approximate nearest neighbor）的交换条件是：**允许少量真近邻被漏掉，换取数量级的延迟和内存下降**。所有索引结构都在三个维度上做取舍：

- **召回**：返回的 top-k 和精确 top-k 有多接近；
- **延迟**：一次查询要扫多少候选、走多少跳；
- **内存 / 构建成本**：索引本身占多少空间，建一次要多久。

没有一种结构在三项上都赢。Flat 召回 100%，但延迟和内存都是线性；HNSW 延迟低、召回高，但图很占内存；IVF-PQ 内存极省，但量化误差会让召回掉；DiskANN 把图放到 SSD，省内存但依赖磁盘 IO。选型的第一步不是“哪个最新”，而是“哪一项我可以牺牲”。

| 结构 | 召回 | 查询延迟 | 内存 | 更新 | 典型场景 |
| --- | --- | --- | --- | --- | --- |
| Flat | 100% | 高，线性 | 4d B/vector | 直接增删 | 小库、离线评测基线 |
| IVF-Flat | 中高，随 nprobe | 中 | 4d + 8 B | 支持增删 | 中大库、内存够 |
| IVF-PQ | 中，随压缩率下降 | 低 | M B + 8 B | 支持增删 | 大库、内存紧 |
| HNSW | 高，随 efSearch | 很低 | 4d + 图开销 | 插入方便，删除麻烦 | 低延迟在线检索 |
| DiskANN | 高 | 中（SSD） | 压缩向量 + 磁盘图 | 复杂 | 十亿级、内存放不下 |

### 3.2 IVF：先找桶，再扫桶内

IVF（inverted file）的思路是“先粗排，再细扫”。它先用 k-means 把向量空间划分成 `nlist` 个簇，每个簇有一个质心（centroid），簇内的向量 ID 挂在一条倒排表上。查询时，先算 query 到所有质心的距离，选出最近的 `nprobe` 个簇，只扫这些簇里的向量。

{{< fig "diagrams/04-ivf-pq.svg" "IVF 用粗量化减少扫描范围，PQ 用残差编码减少单向量存储；两者组合成 IVF-PQ。" >}}

三个参数决定它的行为：

- `nlist`：簇的数量。太小，每个簇很大，扫得慢；太大，簇内样本少，边界效应明显，而且训练和内存开销上升。Faiss 的常见启发式是 `nlist ≈ 4–16 × √N`，先按这个范围试，再用真实数据调。
- `nprobe`：查询时扫几个簇。它是 query-time 参数，可以按延迟预算动态调。`nprobe=1` 最快，但真近邻只要落在没被选中的簇里，就永远召不回来。
- 训练样本：k-means 需要在代表性样本上跑。样本太少或分布偏移，质心会偏，簇的划分就会把真实近邻拆到错误的桶里。

IVF 的召回损失有一个很直观的来源：**query 落在簇边界附近时，真近邻可能在相邻簇里，而 `nprobe` 没覆盖到**。提高 `nprobe` 能缓解，但扫描量近似按 `nprobe/nlist` 增长；当 `nprobe` 接近 `nlist`，IVF 就退化成 Flat 了。

IVF 本身只省“扫描范围”，不省“单向量存储”。要省内存，还要叠加 PQ。

### 3.3 PQ：把残差切成 M 段，每段只留 1 字节

PQ（product quantization）把 d 维向量切成 M 段，每段单独做 k-means，得到 256 个质心（8 bit）。编码时，每段找最近的质心，记下它的编号；于是一条向量变成 M 个字节。

```text
d = 1024, M = 64
每段 16 维，每段 256 个质心
code = 64 字节 / 向量
原始 fp32 = 1024 × 4 = 4096 字节
压缩比 = 4096 / 64 = 64×
```

100 万条 1024 维向量，原始要 4 GB，PQ 后只要 64 MB。这就是为什么大库愿意承受一点量化误差。

PQ 的查询不是“解码后算距离”，而是 ADC（asymmetric distance computation）：查询向量不量化，先为每个子空间预计算 query 到 256 个质心的距离表，大小 `M × 256`；然后对每个候选，把 M 个字节对应的表项加起来，得到近似距离。这样每个候选只要 M 次查表加法，而不是 d 次乘加。

```text
ADC 距离表: M × 256
单候选查询成本: O(M)
总成本: O(M × 256 + M × 候选数)
```

PQ 的误差来自“每段只保留一个质心编号”。段内方差越大，误差越大。两个常用补丁：

1. **OPQ**：先学一个旋转矩阵，把方差均匀分配到各段，再做 PQ；它不改变维度，只是让切分更合理。
2. **残差 PQ**：在 IVF 里，先减去粗质心，得到残差 `r = x - c`，再对残差做 PQ。残差的动态范围更小，量化更准。这就是 IVF-PQ 的标准做法。

但无论怎么优化，PQ 都是有损的。**高 stakes 场景不要直接拿 PQ 近似距离当最终排序**：用 PQ 召回一个较大的候选集（比如 top-200），再用原始全精度向量或 reranker 重排到 top-10。这样内存省在索引上，精度补在 rerank 上。

### 3.4 HNSW：上层跳远，下层精搜

HNSW（hierarchical navigable small world）是目前低延迟场景最常用的图索引。它把向量组织成多层图：上层节点少、边长，负责“跳远”；下层节点多、边短，负责“精搜”。查询从最高层的入口点开始，贪心地向离 query 更近的邻居走，走不动了就下一层，直到第 0 层用 `efSearch` 的束宽做局部搜索。

{{< fig "diagrams/05-hnsw.svg" "HNSW 的多层结构、贪心下降与第 0 层束搜索。上层负责导航，第 0 层决定最终召回。" >}}

节点的层数是随机采样的，服从指数衰减：大部分节点只在第 0 层，少数节点出现在更高层。一个常见的采样公式是：

```text
level = floor(-ln(U) × mL),  mL = 1 / ln(M)
```

`M` 是每个节点在每层最多保留的邻居数，第 0 层通常是 `2M`。`M` 越大，图越密，召回越高，内存和构建时间也越大。hnswlib 的建议是：常规场景 12–48，高维或高召回要求 48–64。

三个参数要分清：

| 参数 | 作用 | 调大后的后果 |
| --- | --- | --- |
| `M` | 每个节点的邻居数，决定图的连通性 | 召回↑、内存↑、构建↑ |
| `efConstruction` | 建图时的候选束宽，决定图的质量 | 图更连通、构建更慢 |
| `efSearch` | 查询时的候选束宽，决定搜索广度 | 召回↑、延迟↑ |

`efSearch` 不能小于 `k`。如果 `k=10`、`efSearch=10`，束里只有 10 个候选，基本没有回旋余地；通常从 `efSearch=64` 或 `128` 开始，用真实 query 测 recall-latency 曲线，再选工作点。`efConstruction` 的判断方法是：建完图后，用 `ef=efConstruction` 跑一遍 recall；如果低于 0.9，说明图的质量还不够，应该加大 `efConstruction` 重建。

HNSW 的内存可以粗略估成：

```text
memory ≈ 4d + 8M + 4 字节/向量
```

其中 `4d` 是原始向量，`8M` 是图边（每条约 8 字节），`4` 是元数据。1024 维、M=16 时，一条向量约 `4096 + 128 + 4 ≈ 4.2 KB`；100 万条约 4.3 GB。这个数字很关键：HNSW 不是“免费的低延迟”，它是用内存换的。如果内存放不下，要么降 M、要么上 PQ、要么换 DiskANN。

HNSW 的删除是另一个坑。Faiss 的 HNSW 不支持 `remove_ids`；hnswlib 支持 `mark_deleted`，但只是打墓碑，节点和边还占着内存，搜索时跳过。频繁删改会让图越来越“空心”，召回和延迟都退化，最终还是要重建。生产上更稳的做法是**不可变段 + 定期合并**：新数据写新段，删除写墓碑，查询跨段合并，后台 compaction 重建。

### 3.5 怎么选：先算内存，再看更新

把上面的约束落成一张决策表：

| 条件 | 建议 |
| --- | --- |
| N < 10 万，内存充足 | Flat 或 HNSW；Flat 作为召回基线 |
| 10 万–1000 万，低延迟 | HNSW，M 从 16/32 起调 |
| 1000 万–1 亿，内存紧 | IVF-PQ + 全向量 rerank；OPQ 可选 |
| 1 亿以上，单机放不下 | 分片 + IVF-PQ / DiskANN；HNSW 每片一份 |
| 频繁删除 / 更新 | IVF 或“段 + 墓碑 + compaction”；别依赖 Faiss HNSW 删除 |
| 强过滤 / 多租户 | 先分区，再在分区内建索引 |

Faiss 的 index factory 字符串可以快速做原型：

```python
import faiss

# 精确基线：归一化后用内积
flat = faiss.IndexFlatIP(d)

# HNSW，M=32；显式指定内积 metric
hnsw = faiss.index_factory(d, "HNSW32", faiss.METRIC_INNER_PRODUCT)
hnsw.efConstruction = 200
hnsw.efSearch = 64

# IVF-PQ：1024 个簇，64 段，每段 8 bit；要求 d 能被 64 整除
ivfpq = faiss.index_factory(d, "IVF1024,PQ64", faiss.METRIC_INNER_PRODUCT)
ivfpq.nprobe = 16
```

原型阶段一定要先跑 Flat，拿到精确 top-k 作为 ground truth，再测 ANN 的 recall@k。没有基线的“感觉很快”没有意义。另外，IVF/PQ 需要先用训练样本 `train()`，HNSW 不需要；`efConstruction` 要在 `add()` 之前设好。

## 四、带过滤的检索：过滤条件会改变索引的数学

现实里的检索几乎从来不是“在全部向量里找最近邻”，而是“在满足条件的向量里找最近邻”：租户隔离、权限、时间范围、文档类型、状态、语言。过滤条件一加，前面所有索引的假设都变了。

{{< fig "diagrams/06-filtering.svg" "post-filter、pre-filter、filtered graph 三种策略。命中率一变，成本会反转；低命中率下最容易被忽略的是图被切断。" >}}

先定义清楚“命中率”（filter pass rate）：

```text
hit_rate = 满足过滤条件的向量数 / 全库向量数
```

它和“选择性”是倒数关系，不同数据库的文档里叫法不一致，所以本文统一用“命中率”，避免歧义。

### 4.1 Post-filter：先召回，再过滤

最简单：ANN 先召回 `k'` 条，再按条件过滤，凑够 k 条。问题是低命中率下 `k'` 要放得很大：

```text
期望候选数 k' ≈ k / hit_rate
```

`k=10`、`hit_rate=1%` 时，要召回约 1000 条才能指望剩下 10 条。这会把延迟和内存带宽放大两个数量级；如果 `k'` 不够大，系统可能只返回 3 条，调用方还以为“库里没有”。

Post-filter 适合命中率高、过滤宽松的场景，比如“排除已删除”这种 99% 都通过的过滤。它的实现最简单，但不要把“返回不足 k 条”当成正常行为。

### 4.2 Pre-filter：先过滤，再精确扫

先算出满足条件的 ID 集合，再在这个子集上做精确扫描。它的成本是：

```text
扫描量 ≈ N × hit_rate
```

命中率 1% 时，100 万库只扫 1 万条，很便宜；命中率 80% 时，等于扫 80 万条，退化成暴力搜索，QPS 直接崩。所以 pre-filter 适合**高选择性**（命中少）的谓词，比如“某个租户 + 某个时间段 + 某个状态”。

Pre-filter 的另一个问题是 ID 集合的构建成本。如果过滤条件本身要走二级索引、回表、去重，这部分延迟可能比向量扫描还高。过滤字段要建好索引，且尽量能走 bitmap / 倒排，不要把多个 `LIKE` 拼在一起。

### 4.3 Filtered graph：在图上带条件走

HNSW 的邻居关系是在全库上建的。如果查询时只允许访问满足条件的节点，图可能被切成很多不连通的小块：从入口点出发，能走的邻居都不满足条件，搜索就卡住了。表现是“过滤越严，recall 掉得越快”，而不是线性下降。

ACORN（arXiv 2403.04871）针对这个问题提出 predicate subgraph traversal：在建图和遍历时考虑谓词，让满足条件的子图仍然可导航；论文声称在固定 recall 下比之前的方法高 2–1000 倍吞吐。它的代价是实现复杂，而且不是所有向量库都支持。

工程上更常见的做法是组合拳：

- 过滤条件很严、命中率很低：pre-filter 精确扫，或者直接按租户/时间分区；
- 过滤条件宽松：post-filter + over-fetch；
- 过滤条件复杂且召回要求高：用支持 filtered graph 的引擎，或者把过滤条件下推成“分区键”；
- 不确定：先用真实过滤条件测 recall@k，再决定，不要拍脑袋。

### 4.4 多租户：不要用一个巨型索引 + tenant_id filter

这是生产里最常见的架构错误之一。把所有租户的向量塞进一个 HNSW，查询时加 `tenant_id = ?` 过滤，看起来省事，实际上有三个问题：

1. **召回不稳定**：小租户的向量在全局图里很稀疏，过滤后图更容易断；租户越小，recall 越差。
2. **故障和配额耦合**：一个大租户的批量写入、删除、重建会影响所有租户；一个租户的热点 query 会挤占公共缓存。
3. **权限风险**：过滤条件一旦写错、缓存 key 没带租户、rerank 阶段越权取数，都会造成跨租户泄露。向量检索的“相似”不是权限，必须显式隔离。

更稳的做法是**一个租户一个 partition / collection / 索引段**，至少也要做到按租户分片、查询只路由到对应分片。代价是索引数量变多、小租户有固定开销；可以用“共享大租户 + 小租户合并池”的混合策略，但隔离边界要清楚。

过滤还有一层常被忽略：**过滤必须发生在返回结果之前，而不是在 prompt 里**。如果先把文档召回、再让 LLM“忽略无权内容”，那些内容已经进入了上下文，注入风险和泄露风险都已经发生。权限过滤要下推到检索层，缓存 key 要包含租户和权限版本，删除要做墓碑并让缓存失效。

## 五、实践中会踩的坑

下面这些坑按“embedding → 索引 → 过滤 → 运维”排序。每一条都给出症状、根因和规避方式；它们大多不是“配置错了一个参数”，而是“把训练假设带进了生产”。

### 5.1 文档侧也加了 query instruction

**症状**：离线用 `encode()` 统一编码，query 和 passage 都拼上了“为这个句子生成表示以用于检索相关文章：”。离线评测看起来还行，上线后 short query 的召回明显变差。

**根因**：BGE 的指令是在不对称分布上训练的：query 侧有指令，passage 侧没有。两侧都加，等于把训练时的边界抹平；模型见到的 passage 分布和训练时不一致，CLS 被推向了一个没见过的区域。

**规避**：API 层面拆成 `encode_queries()` 和 `encode_corpus()` 两个入口，内部固定模板；禁止业务方直接调用底层 `encode()`。写一个单元测试，断言 corpus 编码后的文本不包含指令前缀。如果换模型，把指令模板写进模型配置，不要散落在业务代码里。

### 5.2 归一化和距离度量不一致

**症状**：离线用 `normalize_embeddings=True`，在线某次重构忘了归一化；或者索引建的是 L2，查询用内积。召回时好时坏，阈值完全对不上。

**根因**：BGE 的语义是“单位球面上的余弦相似度”。只归一化一侧，内积就不再是余弦；两侧都不归一化，向量模长会主导排序。L2 距离和余弦在两侧都归一化时排序等价，但分数尺度不同，不能混用阈值。

**规避**：把“归一化”作为 embedding 服务的契约，而不是调用方的选项；返回前用 `assert abs(norm - 1) < 1e-3` 做抽检。Faiss 用 `IndexFlatIP` / `METRIC_INNER_PRODUCT`；如果必须用 L2，明确它是“排序等价”，不要拿 L2 分数当余弦分数解释。索引 metadata 里记录 metric 类型，查询侧启动时校验。

### 5.3 模型、版本、维度三者不一致

**症状**：索引是 `bge-base-zh-v1.5`（768 维）建的，查询服务滚动升级到了 `bge-large-zh-v1.5`（1024 维），直接报维度错误；或者维度碰巧一样，但版本不同，召回 silently 变差。

**根因**：embedding 空间是模型训练出来的，不同模型/不同版本之间没有可比的几何关系。维度相同不代表空间相同；甚至同一模型换了 tokenizer、pooling、instruction，空间都会变。

**规避**：索引 metadata 记录 `model_name`、`revision`、`dim`、`normalized`、`instruction`、`max_length`；查询服务从同一份配置读取，启动时做兼容性检查。模型升级走蓝绿：新模型建新索引，双写/双查对比，确认后再切别名；不要原地覆盖旧索引。

### 5.4 把 512 当成“建议长度”

**症状**：一篇 3000 字的文档直接喂给 BGE，只编码了前 512 个 token；答案在文末，永远召不回。或者长文档被截断后，标题还在、正文没了。

**根因**：v1.5 的 `max_position_embeddings=512` 是硬上限，超过的部分被 tokenizer 截断，不会报错。很多人只看“模型能跑”，不看“截了多少”。

**规避**：上线前统计 token 长度分布和截断率；超过 512 的文档必须切块，或者换 M3（8192）。切块时把标题、章节路径、文档摘要作为每个 chunk 的前缀，避免块离开上下文后语义漂移。M3 的 8192 也不是无限：长文仍然需要合理切块，MCLS 只是缓解，不是替代。

### 5.5 切块把语义切断了

**症状**：固定 512 字符切块，表格被切成两半，代码块断了，问答对拆散；召回单个 chunk 看起来相关，但 LLM 拿到的上下文缺前因后果。

**根因**：切块是检索的“分辨率”。块太大，一个向量混了多个主题，近邻不精确；块太小，语义不完整，query 和 chunk 的匹配信号弱。固定长度没有考虑文档结构。

**规避**：按结构切（标题、段落、列表、表格、代码块），块之间保留 10%–20% overlap；对问答/工单保留“问题 + 答案”成对；对长文档用 parent-child：子块做检索，父块做上下文。切块参数不要拍脑袋，用一批真实 query 测 recall@k 和答案质量。

### 5.6 把相似度分数当概率

**症状**：设一个 `score > 0.8` 的阈值，结果同一条 query 在中文上 0.82，英文上 0.61；换个模型，整个分布又变了。或者把 0.78 展示成“78% 相关”。

**根因**：BGE 的余弦分数不是校准过的概率。它只保证“同一模型、同一分布下，相对排序有意义”；不同模型、不同语言、不同 query 长度，分数尺度都不同。reranker 的分数也是无界的 logits，不是概率。

**规避**：用业务验证集选阈值，不要用公开 demo 的默认值；按语言/租户/query 类型分别校准；需要展示置信度时，用 Platt scaling / isotonic regression 单独校准，或者只展示“高/中/低”三档。更重要的是：阈值只用于过滤，不要用它替代 rerank。

### 5.7 HNSW 参数照搬博客

**症状**：看到“M=16、efSearch=64 性能很好”，直接上线；在自己的数据上 recall 只有 0.7，或者 p99 延迟飙到几百毫秒。

**根因**：HNSW 的最优参数取决于维度、数据分布、k、过滤比例和硬件。M 太小，图不连通；efSearch 太小，束搜索不够；efSearch 小于 k，连候选都凑不齐。别人的 64 是在别人的数据上调的。

**规避**：从 `M=16/32`、`efConstruction=200`、`efSearch=64/128` 起步，画 recall-latency 曲线，选满足 recall 目标的最低延迟点；`efSearch >= k` 是硬约束。建图后用 `ef=efConstruction` 测一次 recall，低于 0.9 就加大 `efConstruction` 重建。上线后按 query 类型动态调 `efSearch`，别一个值打天下。

### 5.8 IVF 没训练好，或 nprobe 太小

**症状**：IVF 的 recall 比 Flat 低很多；加大 `nprobe` 后延迟线性上涨，但 recall 还是上不去。

**根因**：k-means 质心没有代表性（训练样本太少、分布偏移），或者 `nlist` 太大导致簇内样本太少、边界效应严重；也可能是 `nprobe` 只覆盖了 1% 的簇，真近邻在没扫的簇里。

**规避**：用覆盖真实分布的样本训练，样本量至少是 `nlist` 的几十倍；训练完检查簇大小分布，避免超大簇和空簇。`nprobe` 从 1–5% 开始扫，画 recall-延迟曲线；数据分布漂移后重新训练。IVF 的召回损失是“漏桶”，不是“排序误差”，rerank 救不回来。

### 5.9 PQ 压缩后直接拿近似距离当最终排序

**症状**：内存省了 64 倍，recall@10 看起来还行，但答案质量明显下降；换 OPQ 有一点改善，仍然不够。

**根因**：PQ 的每个候选距离都是“查表加出来的近似值”，误差在排序头部会被放大。 recall@10 高不代表 top-1 对；量化误差最容易把“差一点”的候选排到前面。

**规避**：PQ 只做召回，不做最终排序。召回 top-100/200，再用全精度向量或 cross-encoder 重排到 top-10；有条件加 OPQ，把方差均匀分配到子空间。监控“PQ 近似 top-k”和“全精度 top-k”的重叠率，别只看端到端答案。

### 5.10 用 Faiss HNSW 做频繁删除

**症状**：删除接口调用后，搜索结果里还能看到已删除文档；或者删除越频繁，延迟越高、recall 越低。

**根因**：Faiss 的 HNSW 不支持 `remove_ids`；很多封装会“假装删除”，要么重建整个索引，要么只删元数据、向量还留在图里。hnswlib 的 `mark_deleted` 也只是墓碑，节点和边还在，搜索时跳过；墓碑多了，图会被“空心化”。

**规避**：删除走“墓碑 + 查询过滤 + 定期 compaction”；频繁删改的场景用不可变段，新数据写新段，后台合并；或者换支持删除的索引/引擎。权限删除（用户撤权）必须保证向量和缓存都失效，不能只删业务表。上线前专门测“删除后是否还能被召回”。

### 5.11 低命中率还用 post-filter

**症状**：带 `tenant_id` 或时间过滤的查询经常只返回 3–5 条，调用方以为“没数据”；把 `k` 调大后延迟爆炸。

**根因**：post-filter 的候选数要按 `k / hit_rate` 放大。命中率 1% 时，想拿 10 条要召回 1000 条；如果 ANN 只召回 100 条，期望只剩 1 条，返回不足是数学必然。

**规避**：先算命中率。命中率低（比如 <5%）时优先 pre-filter 精确扫，或者按租户/时间分区；命中率高时再 post-filter + 适度 over-fetch。不要用“把 k 调到 1000”解决低命中率，那会把延迟和内存带宽一起放大。过滤条件要能走索引，别在向量扫描后再做昂贵的回表。

### 5.12 缓存 key 漏了租户或权限版本

**症状**：A 租户的 query 命中了 B 租户的缓存结果；用户被撤权后，旧结果还能被召回；删除文档后，缓存里还有。

**根因**：检索结果缓存只用了 `query` 做 key，没有把租户、ACL 版本、过滤条件哈希、模型版本算进去。向量检索的“相似”不是权限，缓存更不是。

**规避**：缓存 key 至少包含 `tenant_id + acl_version + filter_hash + model_version + query`；权限变更和删除要 bump 版本，让旧 key 自然失效。不要在缓存里存“未过滤的候选集”再在应用层过滤；过滤必须下推到检索层。跨租户的共享缓存只允许存“无权限语义”的公共数据。

### 5.13 只测无过滤 recall 和平均延迟

**症状**：离线 benchmark 的 recall@10 是 0.95，线上带过滤后只有 0.6；平均延迟 20 ms，p99 是 300 ms；压测时 QPS 很漂亮，真实流量一上来就雪崩。

**根因**：无过滤的 recall 不能代表带过滤的 recall；平均延迟会掩盖长尾；压测的 query 分布和真实流量不同（长度、过滤比例、热点）。

**规避**：用真实过滤条件测 recall@k；按 query 类型分层看 p50/p95/p99；压测用真实 query 日志采样，带上过滤和租户分布；监控召回率漂移（人工标注一小批，按周跑）。HNSW 的 `efSearch`、IVF 的 `nprobe` 要能在不改索引的情况下动态调，给线上留一个“降级/提速”的旋钮。

### 5.14 embedding 服务和索引不同步

**症状**：新文档写入业务库后，检索不到；或者模型升级后，新 query 编码到新空间，旧索引还是旧空间；双写失败后，两边数据不一致。

**根因**：业务库、embedding 服务、向量索引是三套系统，没有事务；同步失败、重试丢失、版本切换都会造成漂移。

**规避**：业务库是 source of truth，索引是可重建的派生数据；用 outbox / 消息队列做“写库后发事件”，消费者幂等写入索引；记录 `doc_version` 和 `indexed_at`，定期对账。模型升级走“新索引 + 别名切换”，不要原地改；保留全量重建能力，并定期演练。

### 5.15 用公开 benchmark 代替业务评测

**症状**：C-MTEB / MTEB 上分数很高，业务上却不如一个更小的模型；换模型后离线涨点，线上不涨。

**根因**：公开 benchmark 的任务分布、query 长度、语言、领域和你的业务不同；有些 query 是 LLM 生成的，和真实用户输入有差距；还有训练集污染的问题。平均分更会掩盖某个任务上的大幅下降。

**规避**：建自己的评测集：从真实日志采样 query，人工标注相关性，覆盖短 query、长 query、多语言、带过滤、否定/数字等难例；按任务看分，不只看平均；新模型先 shadow / 小流量 A/B，再全量。公开 benchmark 只用来筛候选，不用来做最终决策。

### 5.16 chunk 超过模型窗口，ES 里是全文，向量里只有前半段

**症状**：文档 559 字，关键代码在 531 字；ES 的 `_source` 里明明有全文，召回却只命中前半段。把 chunk 从 400 调到 600，Recall@1 反而掉了 4 个百分点。

**根因**：embedding 模型按 token 截断，不按字符截断；BGE v1.5 的 512 token 是硬上限，超出的部分不会报错。实测同一篇文档，全文 embedding 与“只取前 512 字符”的 embedding 余弦为 **1.0**，说明尾部对向量完全没有贡献。chunk 越大，这个“看不见的尾巴”越长。

**规避**：用模型的 tokenizer 统计长度，不要用字符数猜；给 token 上限留 10%–20% 余量（400 字符只是这套中文语料的经验值，不是通用常数）。把标题、编号、日期等关键字段放在 chunk 前部，或单独建 keyword 字段。对“答案在文末”的文档做专门回归，别只测开头就有答案的样本。

### 5.17 把资产编号交给 embedding

**症状**：query 就是 `asset-1015`，Top-5 里没有它；换更大的 embedding 模型仍然不稳定。

**根因**：dense 向量被训练来压语义，不保证逐字匹配；稀有 token、数字、主键在向量里很容易被“平滑”掉。用语义模型查主键，方向就错了。

**规避**：`assetId`、订单号、错误码走 keyword/`term` 字段，与向量做 hybrid；查询解析阶段识别出编号后，甚至可以走 exact-match 短路。不要用“换更大模型”解决主键检索，成本高且不可靠。

### 5.18 hybrid 把两路分数直接相加

**症状**：dense 余弦是 0–1，BM25 是 0–几十；直接 `score = dense + bm25` 后，BM25 永远主导。改成归一化相加，长文档又因为词频高而占优。

**根因**：两路分数不同量纲、不同校准，不能直接比大小。`min-max` 归一化依赖当次候选集，跨 query 不稳定。

**规避**：用 RRF 按名次融合，`score(d) = Σ 1/(k + rank_i(d))`，`k=60` 起步；或者用标注数据学习融合权重。过滤条件要同时下推到 dense 和 lexical 两路，不能先融合再在应用层过滤。

### 5.19 在合成集上刷到 100% 就收工

**症状**：26 条合成 query 全部命中，团队认为检索没问题；上线后“查编号、查文末、查否定、查同义”仍然失败。

**根因**：合成 query 和 chunk 的用词高度重叠，测的是“复述”而不是“检索”；平均分掩盖了长尾失败模式。

**规避**：按失败模式切片建评测集：语义、编号、数字、时间、否定、多跳、长文尾部、ACL 负例，每类至少 10 条；同时看 Recall@1、MRR、nDCG 和“证据是否真的在返回的 chunk 里”。合成集只做回归，不做上线结论。

## 六、面试向：从原理到系统设计

面试官通常不是要你背公式，而是看你能否把公式和工程后果连起来。下面按“BGE 基础 → 索引 → 系统设计”三组整理，答案尽量给到“为什么”，而不是只给结论。

### 6.1 BGE 基础

**Q1：BGE 的 embedding 是怎么来的？为什么用 CLS 而不是 mean pooling？**

取 BERT 类 encoder 最后一层 `[CLS]` 的隐状态，做 L2 归一化，得到单位向量；相似度用内积，等价于余弦。用 CLS 不是因为 CLS 天然更好，而是因为对比学习就是在这个位置上做的：正负例的梯度直接把 CLS 拉近/推远，它被训练成了“整段文本的语义摘要”。mean pooling 是另一个可用的池化，但 BGE 没在这个表示上训练过；直接换等于换了一个未训练的向量，效果会掉。要用 mean pooling，必须重新训练或微调，不能只改推理代码。

**Q2：为什么 BGE 的对比学习需要 19,200 这么大的 batch？**

InfoNCE 的负例来自 batch 内其他样本。batch 为 B 时，每个正例有 B-1 个负例；B 越大，负例越多，梯度越接近“在全量数据上做对比”，模型学到的区分边界越细。batch 小的时候，负例太少，模型容易把“同一个 batch 的所有样本”都推远，学不到细粒度语义。大 batch 的代价是显存和假负例：batch 里可能混进真实相关但被当负例的样本，需要用去重、过滤或更强的 teacher 缓解。

**Q3：为什么 instruction 只加 query，不加 passage？**

因为 query 和 passage 的分布本来就不对称：query 短、口语、带指代，passage 长、陈述、信息密度高。指令的作用是把 query 往“检索意图”的分布上推；如果 passage 也加，等于把两侧一起推，训练时学到的“query 有指令、passage 没有”的边界就被抹平了。模型卡明确写了：instruction 只加 query，永远不加 passage。v1.5 不加指令也能用，但 short query → long passage 仍然建议加；M3 不需要指令。

**Q4：BGE v1.5 和 BGE-M3 怎么选？**

v1.5 是单 dense、512 token、分语言（zh/en）的模型，适合短文本、单语言、成本敏感的场景；M3 是 1024 维、8192 token、多语言、dense + sparse + multi-vector 三模式，适合长文、跨语言、需要词面/细粒度匹配的场景。M3 更大、更慢、存储更复杂；如果只是中文短 query 检索，v1.5 large 往往更划算。选型不要只看 benchmark 平均分，要看你的长度分布、语言分布和延迟预算。

**Q5：归一化后内积为什么等于余弦？用 L2 距离有区别吗？**

余弦是 `q·p / (||q|| ||p||)`；当 `||q||=||p||=1` 时，分母是 1，余弦就是内积。L2 距离展开是 `||q-p||² = ||q||² + ||p||² - 2q·p`；两侧都归一化时，它等于 `2 - 2q·p`，和余弦是单调等价的，所以排序一样。但分数尺度不同，阈值不能混用；如果只归一化一侧，内积就不是余弦，排序会被模长带偏。索引的 metric 必须和归一化策略一起设计。

**Q6：BGE-M3 的三种分数分别是什么？怎么融合？**

dense 是 `[CLS]` 归一化后的内积；sparse 是每个 token 的 ReLU 词权重，在共享词项上相乘求和；multi-vector 是每个 token 出一个向量，query 的每个 token 对 passage 所有 token 取 max，再平均。训练时用三者相加的 teacher 分数做 self-distillation，让每个头拟合融合软标签；线上最终分数也是三者相加。但三者不在同一量纲，生产里通常要先归一化或学习权重，不能把原始分数直接相加。

### 6.2 索引

**Q7：IVF 的 nlist 和 nprobe 怎么定？召回为什么会丢？**

`nlist` 是粗聚类的簇数，常见启发式是 `4–16 × √N`；太小每簇太大、扫得慢，太大簇内样本少、边界效应明显。`nprobe` 是查询时扫几个簇，是 query-time 参数，可以按延迟预算调。召回丢是因为真近邻可能落在没被 probe 的簇里，尤其是 query 在簇边界附近时；`nprobe` 越小，漏得越多。提高 `nprobe` 能缓解，但扫描量近似按 `nprobe/nlist` 增长，接近 `nlist` 就退化成 Flat。

**Q8：PQ 的 ADC 是什么？为什么能省内存？误差从哪来？**

PQ 把 d 维切成 M 段，每段用 256 个质心（8 bit）编码，一条向量变成 M 字节。ADC 不解码向量，而是先算 query 到每个子空间 256 个质心的距离表（`M × 256`），再对候选的 M 个字节查表求和，得到近似距离。省内存是因为每个候选只存 M 字节，而不是 `4d` 字节；误差来自“每段只保留一个质心编号”，段内方差越大误差越大。OPQ 用旋转均衡方差，残差 PQ 先减粗质心再量化；最终排序要用全精度向量 rerank。

**Q9：HNSW 为什么是 O(log N)？M、efConstruction、efSearch 分别影响什么？**

HNSW 是多层图：上层节点少、边长，负责跳远；下层节点多、边短，负责精搜。查询从顶层入口贪心下降到第 0 层，每层只访问少量邻居，层数按指数衰减，所以期望是 O(log N)。`M` 是每层邻居数，决定图的连通性和内存；`efConstruction` 是建图束宽，决定图的质量；`efSearch` 是查询束宽，决定召回和延迟。`efSearch` 必须 ≥ k；建完图用 `ef=efConstruction` 测 recall，低于 0.9 就加大 `efConstruction` 重建。

**Q10：HNSW 怎么删除？Faiss 和 hnswlib 有什么区别？**

Faiss 的 HNSW 不支持 `remove_ids`，只能重建或在外层做过滤；hnswlib 支持 `mark_deleted`，但只是墓碑，节点和边还占内存，搜索时跳过。墓碑多了，图会空心化，召回和延迟都退化。生产上更稳的是不可变段 + 墓碑 + 定期 compaction：新数据写新段，删除写墓碑，查询跨段合并，后台重建。权限删除还要让缓存失效，不能只删向量。

**Q11：带过滤的检索有哪几种做法？低命中率怎么选？**

三种：post-filter（先 ANN 再过滤）、pre-filter（先过滤再精确扫）、filtered graph（在图上带条件走）。post-filter 的候选数要按 `k / hit_rate` 放大，低命中率会爆炸或返回不足；pre-filter 的扫描量是 `N × hit_rate`，高命中率会退化成全表扫；filtered graph 在低命中率下容易因图被切断而掉 recall，ACORN 用 predicate subgraph traversal 缓解。低命中率优先 pre-filter 或按租户/时间分区；高命中率用 post-filter + over-fetch；复杂谓词用支持 filtered graph 的引擎，并测真实过滤下的 recall。

**Q12：recall@k 怎么测？没有 ground truth 怎么办？**

最可靠的是用 Flat 精确检索跑一批 query，得到 ground truth，再算 `|ANN_k ∩ GT_k| / k`。没有标注时，可以用业务日志采样 + 人工标注，或用强模型标注，但要抽检、去偏；不要用另一个 ANN 的结果当 ground truth。除了 recall@k，还要看 nDCG/MRR（排序质量）和端到端答案质量；recall@k 高不代表答案对。线上可以做小流量 canary，用人工标注集监控召回漂移。

### 6.3 系统设计 / 排障

**Q13：线上召回率突然下降，怎么排查？**

按层排查：先看 query 分布有没有变（新业务、长 query、多语言），再看 embedding 服务（模型版本、归一化、instruction、tokenizer），再看索引（版本、维度、metric、`efSearch`/`nprobe`、是否重建中），再看过滤（命中率、ACL、租户路由），最后看数据新鲜度（写入延迟、双写失败、墓碑）。用离线 replay 对比“同一批 query 在旧/新链路上的召回”，定位是哪一层变了；不要一上来就调 prompt。

**Q14：设计一个多租户 RAG 检索系统，你会怎么切？**

摄入层：解析、切块、embedding、元数据；索引层：按租户 partition/collection，至少按租户分片，索引别名做蓝绿；查询层：query 改写、指令、编码、租户路由、过滤下推、ANN、rerank；缓存层：key 含 tenant、ACL 版本、filter hash、model version；权限层：过滤在检索层完成，删除写墓碑并失效缓存；运维层：全量重建、对账、p99/recall 监控、配额和故障隔离。核心原则是“隔离优先于共享”，不要让一个巨型索引 + `tenant_id` filter 承担所有租户。

**Q15：十亿向量怎么存？**

先算内存：1024 维 fp32 是 4 KB/条，十亿条就是 4 TB，单机放不下。常见路线是分片 + IVF-PQ（64 字节/条，十亿条约 64 GB 码本 + 倒排），或者 DiskANN（SSD 放图，内存放压缩向量），或者 HNSW 每片一份、按租户/时间路由。热数据用 HNSW/Flat 保延迟，冷数据用 PQ/磁盘；召回后统一用全精度向量或 reranker 重排。没有“一种索引通吃十亿”，都是分层和分片。

**Q16：为什么 rerank 不能解决召回问题？**

因为 rerank 只能重排“已经进入候选集”的文档。候选集是 ANN 返回的 top-k，真相关文档如果没被召回，cross-encoder 根本看不到它。召回是上限，rerank 是在上限内做精度优化；两者的成本结构也不同：ANN 可以 sublinear，cross-encoder 每个候选一次前向，成本随候选数线性增长。所以正确顺序是“先把召回做够，再谈 rerank”。

### 6.4 如果只记八句话

| 一句话 | 对应后果 |
| --- | --- |
| 归一化后内积才是余弦 | metric、阈值、索引选型全跟着变 |
| instruction 只加 query | 加错侧会破坏训练时的分布 |
| M3 不需要 instruction | 多加了反而可能偏移 |
| `efSearch >= k` | 否则 HNSW 连候选都凑不齐 |
| Faiss HNSW 不支持删除 | 频繁删改要段 + 墓碑 + 重建 |
| PQ 只做召回，不做最终排序 | 量化误差会在头部放大 |
| 低命中率优先 pre-filter / 分区 | post-filter 的 `k/hit_rate` 会爆炸 |
| recall@k 要对精确 top-k 测 | 用 ANN 测 ANN 是循环论证 |

## 七、实测调优：chunk、hybrid 与 query instruction

这一节来自一次真实的端测，不是 benchmark 复读。为了避免“调参表演”，先把评测口径固定下来：

- 语料：23 个合成资产、8,263 字、2 个租户；28 条 query（26 条有标注 + 2 条 ACL 负例），其中 6 个资产会切成 2 个 chunk。
- 模型：`BAAI/bge-small-zh-v1.5`，512 维，ONNX/CPU；ES 8.15.3 单节点，1 GB heap；Spring Boot demo；LLM 是确定性 mock，只验证检索与引用链路。
- 指标：资产级 Recall@1/5、MRR、nDCG@5；必要时看“证据是否在返回的 chunk 里”。
- 局限：合成语料、单机、串行请求。下面的数字用于说明方法和失败模式，不能外推成生产结论。

### 7.1 chunk 不是越大越省，窗口外的文字等于不存在

先固定 400 字 / 80 overlap，再分别跑 200/40、400/80、600/80 三组。为了只比较 chunk 质量，这一组排除 exact-ID 查询 q28（它由后面的 lexical 通道处理）：

| chunk / overlap | chunk 数 | 索引 store | Recall@1 | Recall@5 | MRR | nDCG@5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 200 / 40 | 64 | 680,903 B | 1.000 | 1.000 | 1.000 | 0.9968 |
| 400 / 80 | 31 | 451,523 B | 1.000 | 1.000 | 1.000 | 0.9951 |
| 600 / 80 | 24 | 366,482 B | 0.960 | 1.000 | 0.980 | 0.982 |

只看这张表，200 和 400 的召回打平，600 开始掉点。真正解释原因的是边界用例：一篇 559 字的资产，关键代码 `ZX-9917` 在约第 531 字，前半段是无关的餐厅运营内容。查询“`ZX-9917` 的上线截止日期是哪天”：

- 200/40 和 400/80 都把目标资产排在第 1；
- 600/80 排在第 2（单条 query 的 MRR 为 0.5）；
- 把 559 字全文和“只取前 512 字”分别做 embedding，余弦为 **1.0**——尾部文字对向量没有任何贡献。

600 的 chunk 看起来只多了 200 字，但 BGE v1.5 的 512 token 窗口已经把 `ZX-9917` 截掉了；ES 里能搜到全文，向量里却没有这段语义。这不是“模型效果差”，是输入在进入模型前就丢了。

最后选 **400/80** 作为默认：200 的召回没有更好，但 chunk 数和索引存储大约翻倍（+51%）；600 省了约 19% 存储，却丢了 4 个百分点的 Recall@1，而且尾部事实不可见。单机 p50 在 16–31 ms 之间波动，样本不足以拿延迟做决策。

还要注意：400 是**字符数**，不是 token 数。中文、代码、英文缩写混排时，400 字符仍可能超过 512 token；生产应该用模型 tokenizer 统计，并给关键字段（标题、编号、日期）留出前部位置。换模型、换语言、换文档结构，这组参数都要重测。

### 7.2 hybrid 的收益在 dense 的盲区，不在平均分

chunk 固定 400/80 后，比较三条检索路径：dense kNN、BM25、以及 RRF 融合的 hybrid。两路都套同一套 tenant/ACL filter，候选各取 top-50，RRF 的 `k=60`。

先看 28 条 base query：

| 检索方式 | Recall@1 | Recall@5 | MRR | nDCG@5 |
| --- | ---: | ---: | ---: | ---: |
| dense | 0.9615 | 0.9615 | 0.9615 | 0.9568 |
| BM25 | 0.2308 | 0.2308 | 0.2308 | 0.2308 |
| hybrid | **1.000** | **1.000** | **1.000** | **0.9953** |

BM25 在 base 上很差，不是“BM25 没用”，而是 ES 默认 standard analyzer 对中文按单字切分，再加上 `operator=AND`，长 query 几乎匹配不到完整词序列；换成 `ik_smart` 或词级 analyzer、放宽 operator，结果会好很多。它真正擅长的是下面这组 11 条 lexical probes：资产编号、`PIPL`、`500 万`、`23 分钟`、`Token Bucket`、`MFA/USB`、`90 天`、`30 天`、`4.1 分`、`asset-1018`、`asset-1003`。

| 检索方式 | Recall@1 | Recall@5 | MRR | nDCG@5 |
| --- | ---: | ---: | ---: | ---: |
| dense | 0.7273 | 0.9091 | 0.7909 | 0.8198 |
| BM25 | **1.000** | **1.000** | **1.000** | **1.000** |
| hybrid | **1.000** | **1.000** | **1.000** | **1.000** |

这就是 hybrid 的价值：dense 负责“意思相近”，BM25 负责“字面精确”，RRF 负责把两种排名合起来。融合公式不需要分数校准：

```text
score(d) = Σ_i 1 / (60 + rank_i(d))
```

只看名次，不看原始分数，避免“余弦 0.7 + BM25 12.3”这种量纲灾难。Java demo 里 `SEARCH_MODE=hybrid` 已是默认：`buildLexicalQuery` 用 `term assetId`、`match_phrase name/chunkText`、`multi_match chunkText^2/name^2/description operator=AND`，与 dense 共用 `buildFilter`；exact-ID 仍然在融合前短路。

切换后重跑完整 E2E：29 chunk，Recall@1/5 = 1.0/1.0，nDCG@5 = 0.9953，ACL 泄漏 0，RAG 引用 6/6，更新/删除生命周期通过；搜索 p50 30.95 ms，和上一轮 dense 的 30.13 ms 基本同一水平。**不要说 hybrid 更快**——延迟差异在单机噪声内；它的收益是 ID、数字、专有名词这些 dense 的盲区，代价是多一路查询和融合。如果业务里几乎没有这类 query，hybrid 可能只是多花开销。

### 7.3 query instruction 不是“加了就赚”

BGE v1.5 的检索指令是为“短 query → 长 passage”准备的。在这套域内 query 上做消融（27 条 base，dense 400）：

| query instruction | Recall@1 | nDCG@5 |
| --- | ---: | ---: |
| 加 | 1.000 | 0.9951 |
| 不加 | 1.000 | 0.9968 |

差异只有 0.0017，不能说明“指令有害”，也不能说明“指令有用”；在这个分布上它是噪声。结论应该窄一点：v1.5 不加指令也能工作，域内 query 不必盲加；短 query 找长 passage、跨分布检索仍建议加；M3 不需要指令。换数据集要重新 A/B，不要把某一次消融当成通用规律。

### 7.4 这次实际改了什么

- `AssetIngestionService` 把写死的 400/80 改成 `CHUNK_MAX_CHARS` / `CHUNK_OVERLAP` 配置，默认仍是 400/80。
- `AssetSearchService` 增加 `SEARCH_MODE`，默认 `hybrid`；RRF `rank_constant=60`；dense 和 lexical 共用同一套过滤条件。
- exact-ID 保留为独立短路，不参与 chunk 质量对比；它解决的是“主键查询”，不是“语义检索”。
- 端测报告同步更新为 hybrid + 400/80 的最终结果，并保留 chunk、hybrid、instruction 三组实验数据。

调优顺序也很重要：**先看输入有没有被截断，再看召回通道是否覆盖 query 类型，最后才调 instruction、rerank 和 prompt**。反过来做，很容易在一个已经丢字的输入上反复换模型。

## 八、上线前检查清单

把上面的内容压成一张可执行的清单。不是每条都要做到满分，但每一条都要有明确答案。

- [ ] 索引 metadata 记录了模型名、revision、维度、归一化、instruction、max_length、metric。
- [ ] 统计过 token 长度分布和截断率；超过 512 的文档有切块或换 M3 的策略。
- [ ] 用 tokenizer 抽检过 chunk 不越窗（字符数上限只是近似）；有“答案在文末”的回归用例。
- [ ] 切块按结构做，表格/代码/问答对没有被切断；有 overlap 或 parent-child。
- [ ] 资产编号/订单号/错误码有 keyword 或 lexical 通道，不指望 embedding 记住主键。
- [ ] 如果上 hybrid，融合用 RRF 或学习权重，不直接加不同量纲的分数；过滤同时下推到两路。
- [ ] query 和 passage 走不同的编码入口，文档侧没有 instruction。
- [ ] 用 Flat 精确检索建了 recall@k 基线，ANN 参数是在真实数据上调的。
- [ ] 带真实过滤条件测过 recall@k，知道命中率和 pre/post-filter 的切换点。
- [ ] 删除、撤权、缓存失效有端到端测试；删除后不会被召回。
- [ ] 多租户有隔离方案，缓存 key 含租户和权限版本。
- [ ] 监控了 p50/p95/p99、QPS、内存、构建时间、召回漂移。
- [ ] 模型/索引升级有蓝绿或双写方案，能回滚，能全量重建。
- [ ] 有业务评测集，不只依赖 C-MTEB / MTEB 平均分；评测按编号/数字/否定/长文尾部/ACL 切片。
- [ ] 知道当前系统的召回上限卡在哪：embedding、切块、索引，还是过滤。

最后用一句话收束：**BGE 决定“这段语义能不能被表示出来”，ANN 决定“这个表示能不能被快速找到”，rerank 决定“找到的候选怎么排”。** 三件事的优化顺序不能反：先保证语义覆盖和切块合理，再调索引召回，最后才轮到 rerank 和 prompt。反过来做，通常是在给一个已经漏水的桶刷漆。

## 参考资料

- C-Pack: Packed Resources For General Chinese Embeddings，https://arxiv.org/abs/2309.07597
- BGE M3-Embedding: Multi-Lingual, Multi-Functionality, Multi-Granularity Text Embeddings Through Self-Knowledge Distillation，https://arxiv.org/abs/2402.03216
- Efficient and robust approximate nearest neighbor search using Hierarchical Navigable Small World graphs，https://arxiv.org/abs/1603.09320
- ACORN: Performant and Predicate-Agnostic Search Over Vector Embeddings and Structured Data，https://arxiv.org/abs/2403.04871
- Faiss wiki（索引选型、内存、Metric），https://github.com/facebookresearch/faiss/wiki
- hnswlib（`ALGO_PARAMS.md`，M / efConstruction / efSearch 的实践建议），https://github.com/nmslib/hnswlib
- FlagEmbedding（BGE 推理与微调代码），https://github.com/FlagOpen/FlagEmbedding
- BGE v1.5 / BGE-M3 模型卡，https://huggingface.co/BAAI
- Anthropic, Contextual Retrieval，https://platform.claude.com/cookbook/capabilities-contextual-embeddings-guide
- Pinecone, Chunking Strategies，https://www.pinecone.io/learn/chunking-strategies/
- Elastic, RRF retriever，https://www.elastic.co/docs/reference/elasticsearch/rest-apis/retrievers/rrf-retriever
- BGE reranker v2-m3 模型卡，https://huggingface.co/BAAI/bge-reranker-v2-m3
