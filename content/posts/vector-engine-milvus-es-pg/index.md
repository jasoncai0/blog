---
title: "同一条 kNN 查询，走的是三条路：Milvus、Elasticsearch 与 pgvector 对比"
date: "2026-11-10T14:30:00+08:00"
summary: "把 Milvus、Elasticsearch、pgvector 放回各自的抽象层次：查询在哪里归并、过滤发生在哪一步、扩容加什么。再从索引维护、量化、混合检索、成本与 benchmark 前提，给出可执行的选型阶梯。"
description: "把 Milvus、Elasticsearch、pgvector 放回各自的抽象层次：查询在哪里归并、过滤发生在哪一步、扩容加什么。再从索引维护、量化、混合检索、成本与 benchmark 前提，给出可执行的选型阶梯。"
tags: ["ann", "architecture", "rag"]
slug: "vector-engine-milvus-es-pg"
---

**这篇在做什么：** 把 Milvus、Elasticsearch、pgvector 放回各自的抽象层次，比较向量索引的构建、查询、过滤与扩容。重点不在排名，而在同一条 kNN 查询经过哪些组件、在哪儿归并、过滤发生在哪一步——这些差异最终决定写入成本、召回表现和扩容方式。版本与数据截至 2026 年 11 月。

## 一、结论：先按约束排除

如果你带着一个具体系统来读，先看这张表。它不评“最强”，只回答“在已有约束下，最少新增什么”。

| 你的处境 | 更合适的选择 | 主要原因 |
| --- | --- | --- |
| 已有 PG，向量 ≤ 1000 万，QPS 中等，要事务 / Join | pgvector | 不新增系统，SQL、事务、备份直接复用 |
| 已有 PG，1000 万–1 亿，能自建扩展 | pgvector + pgvectorscale / VectorChord | 单机换 DiskANN / 量化，延迟和成本更稳 |
| 已有 ES，文本 + 向量混合，需要过滤 / 聚合 | Elasticsearch | BM25、kNN、聚合、权限在一套查询里 |
| 亿级以上，高并发，多租户，要独立扩索引 | Milvus | 存算分离，query / data / index 各自扩 |
| 向量已经在 Parquet / Iceberg 里，不想搬 | Milvus 3.0 External Collections | 零拷贝挂外部表，省一次摄入 |

这张表的排序逻辑是：能复用已有系统就复用，复用不了再看规模，规模再大才引入专用系统。向量检索很少是孤立负载——它通常长在业务库、搜索索引或日志平台上，选型的第一成本往往不是 QPS，而是多养一套系统。

## 二、三种抽象层次

把三个系统画在同一张图上，最先暴露的不是索引算法，而是索引的归属。

{{< fig "diagrams/01-query-path.svg" "三种查询路径。Milvus 在 query node 内归并 segment、在 proxy 归并 shard，Elasticsearch 在 shard 间归并，pgvector 在单机上扫描一棵 HNSW 图并回表过滤。" >}}

### Milvus：索引是一个独立服务

Milvus 的组件比另外两个多得多：proxy 接请求，query node 执行搜索，data node 管写入与 compaction，index node 异步建索引，etcd 存元数据，对象存储放 segment，WAL 保证可恢复。

一条搜索请求的路径是：proxy 解析请求，按 shard 把子请求发给持有对应 shard 的 query node；query node 在自己的 sealed segment 和 growing segment 上各查一遍，段内先做标量过滤，再做 ANN；结果回到 proxy 归并。

query coordinator 管的是 segment 在 query node 上的分布和负载均衡，不参与单次查询的归并。这个区分很重要：Milvus 的查询路径是 proxy → query node → proxy，coordinator 不在数据面上。

segment 是 Milvus 的基本单位。写入先进入 growing segment，达到阈值后封存为 sealed segment；sealed segment 由 index node 建索引，再由 query node 加载。删除是墓碑，compaction 时真正清理。2.6 用 Woodpecker 替掉了对外部 Kafka / Pulsar 的依赖；3.0 的 External Collections 允许把 Parquet、Lance、Iceberg、Vortex 表零拷贝挂进来，Loon 把存储层换成基于 manifest 的列式格式。

代价是组件多、部署重。好处是索引构建和查询执行彻底解耦：写入高峰不会直接拖垮查询，索引重建可以单独扩 index node。

### Elasticsearch：索引藏在 segment 里

ES 的层次是 index → shard → segment。每个 shard 是一个 Lucene 索引，每个 segment 是不可变的小索引；每个 segment 里，每个 `dense_vector` 字段有一棵 HNSW 图。

写入先进内存 buffer 和 translog，refresh（默认 1 秒）生成新 segment，后台 merge 把多个 segment 合成一个。merge 不是搬数据那么简单：它要重写倒排、doc values 和向量图。所以 ES 的写入成本里，merge 占很大一块。

kNN 查询默认要 fan-out 到所有 shard：每个 shard 在自己的 segment 图上取 `num_candidates` 个候选，协调节点把所有 shard 的结果归并成全局 top-k。分片数越多，单次查询的固定开销越大；这也是为什么 ES 的向量性能对分片设计很敏感。

另一个容易忽略的点：`dense_vector` 默认不写入 `_source`（由 `index.mapping.exclude_source_vectors` 控制），节省磁盘与响应体积；但索引里仍保留原始向量用于 rescore。

### PostgreSQL：索引是一个 index AM

pgvector 不是数据库，是 PostgreSQL 的一个索引访问方法。你建的是普通索引，查询也是普通 SQL；差别在于索引内部是 HNSW 图或 IVFFlat 的聚类列表。

一条查询进来，planner 按代价选择走不走向量索引；走索引时，backend 在 shared_buffers 里沿图搜索，拿到 TID 后回表，再应用 `WHERE`。索引页和堆页都受 WAL、MVCC、checkpoint、autovacuum 约束。

这带来两个直接后果。第一，索引和热数据要能放进内存，否则图搜索会退化成随机 I/O。第二，更新不是原地修改：HNSW 插入新节点，旧元组由 MVCC 标记删除，vacuum 回收堆空间，但图里的旧节点不会立刻消失，索引会膨胀，最终需要 `REINDEX`。

好处是事务、Join、PITR、权限、备份都和现有 PG 一套走；坏处是所有这些机制都会给向量索引增加额外成本。

这张图能解释三件事：

- **更新成本**：Milvus 把重建压力放在后台 compaction，ES 放在 merge，PG 放在 autovacuum 与 `REINDEX`。
- **过滤能力**：ES 有倒排和 bitset，Milvus 有段内标量索引和 partition key，PG 默认是 post-filter。
- **扩容方式**：Milvus 加 query / data / index node，ES 加节点与 shard，PG 垂直扩容 + 只读副本 + 分片。

## 三、索引内部：同样的 HNSW，不同的维护方式

三者都支持 HNSW，参数名也几乎一样：`M` / `m` 控制每个节点的邻居数，`efConstruction` 控制建图时的候选队列，`ef` / `num_candidates` / `ef_search` 控制查询时的束宽。但维护方式不同。

| 维度 | Milvus | Elasticsearch | pgvector |
| --- | --- | --- | --- |
| 常用索引 | HNSW、IVF、DiskANN、GPU CAGRA | HNSW（Lucene） | HNSW、IVFFlat |
| 图参数 | M、efConstruction、ef | m、ef_construction、num_candidates | m、ef_construction、ef_search |
| 量化 | PQ、SQ、二值索引 | int8、int4、BBQ、DiskBBQ | halfvec、binary、SBQ（pgvectorscale） |
| 维度上限 | 32768 | 4096 | 2000（vector 索引） |
| 距离算子 | L2、IP、COSINE、JACCARD、HAMMING | cosine、dot_product、l2_norm、max_inner_product | L2、IP、cosine、L1、Hamming、Jaccard |
| 删除 / 更新 | 墓碑 + compaction | 墓碑 + merge | MVCC + vacuum / REINDEX |
| 构建方式 | index node 异步构建 | 写入时增量构建，merge 重建 | `CREATE INDEX`，可并行 |

Milvus 的索引由 index node 异步构建，query node 只负责加载和查询；构建失败可以重试，重建可以单独扩容。

ES 的索引跟着 segment 走：新 segment 建新图，merge 时重建；没有独立的“重建索引”动作，但 merge 的写放大是常态。

PG 的索引跟着表走：`INSERT` 直接插图中，`UPDATE` 是删除 + 插入，`DELETE` 留墓碑；autovacuum 清堆，但图里的空洞要等 `REINDEX`。

更新密集的负载下，这个差异比查询延迟更致命。PG 的 HNSW 在大量更新后会出现索引膨胀和召回下降；ES 靠 merge 控制，但 merge 会抢 I/O；Milvus 把 compaction 放在后台，代价是磁盘和后台任务。

### 量化：默认值就是产品观点

量化是十亿级的入场券，但三家对“默认”的态度完全不同。

Elasticsearch 最激进。9.0 起 float 向量默认用 `int8_hnsw`；后续 9.x 把默认细化成：384 维及以上用 `bbq_hnsw`，低于 384 维用 `int8_hnsw`。有 Enterprise 许可时，新索引还会默认 `bbq_disk`。BBQ 把每维压到 1 bit，32× 压缩，查询时用原始向量 rescore；`bbq_disk` 进一步把图和向量放到磁盘，内存只留少量结构，并用 `auto_calibrate` 自动选编码和 oversampling。

Milvus 把选择权交给你。HNSW、`IVF_PQ`、`IVF_SQ`、DiskANN、GPU CAGRA；3.0 又补了新的索引类型。好处是可控，坏处是默认不替你决定；选错量化方式，召回会在上线后才掉。

pgvector 最保守。`halfvec` 把 float32 压到 float16，2×；`binary_quantize` 压到 1 bit，32×；两者通常都要二阶段重排。pgvectorscale 的 SBQ（统计二值量化）思路类似，把量化误差压到更小。

{{< fig "diagrams/02-memory.svg" "十亿条 768 维向量的内存账。float32 约 3.07 TB，1 bit 约 96 GB；HNSW 图与副本另算。" >}}

十亿条 768 维，float32 是 3.07 TB，int8 是 768 GB，1 bit 是 96 GB。这还没算 HNSW 图（m=16 大约 100–150 GB）和副本。看到“十亿向量”先算这笔账，很多方案在这一步就被排除了。

量化的代价是召回。1 bit 必须配 rescore，否则召回掉得比内存快；halfvec 的损失小，但也不是零。lore-weave 在 1 万行、1024 维的实测里，halfvec 平均 recall 只掉 0.5%，最差查询掉到 0.90，换来 49% 的存储下降——这个 trade-off 是否值得，取决于你的最差查询能不能接受。

另一个常被忽略的点：量化后的索引仍然需要原始向量做 rescore，所以省的是索引内存，不一定是总内存。ES 的 `dense_vector` 默认不进 `_source`，但原始向量仍在索引里；pgvector 的 binary 索引旁边还得留一份 full-precision 列。

## 四、过滤：真正的分水岭

在百万到千万级、单机放得下的范围内，纯向量 QPS 的差距没有数量级；加上 `WHERE` 之后，差距会突然拉开。

{{< fig "diagrams/03-filtering.svg" "过滤发生在 ANN 之前、之中、之后。位置不同，召回损失与延迟抖动也不同。" >}}

过滤可以发生在三个位置：ANN 之前（先算出候选集，再在子集上搜索）、ANN 之中（在图遍历时用 bitset 排除不合格节点）、ANN 之后（先取 top-k，再过滤，不够再扫）。位置不同，召回和延迟的尾部行为完全不同。

### Elasticsearch：过滤器是一等公民

ES 的 filter 在 Lucene 里是 bitset，和倒排、doc values 是同一套机制。查询时它可以先算 filter 再喂给 kNN，也可以在图搜索之后过滤；选择哪条路，取决于 filter 的命中率和查询结构。

这让 ES 在“文本条件 + 向量”的场景里很占便宜：同一个查询里可以既有 BM25 的 `must`，又有 kNN 的 `filter`，还有聚合和排序。代价是 kNN 仍然要 fan-out 到所有 shard，filter 只能减少每个 shard 内部的候选，不能减少 shard 数量（除非用 routing）。

### Milvus：段内过滤 + partition key

Milvus 在每个 segment 内部用标量索引生成 bitset，再做 ANN；partition key 可以把整个 segment 裁掉，减少需要搜索的段数。但如果过滤条件命中率很低、又分散在很多 segment 里，每个 segment 还是要跑一遍图搜索，尾部延迟会上去。

3.0 的服务端 `ORDER BY` 和聚合 / 分面，补上了“过滤后直接出统计”的能力，但它的主战场仍然是向量召回，不是复杂 SQL。

### PostgreSQL：post-filter 与 iterative scan

pgvector 的默认行为是 post-filter：HNSW 先按距离取 `ef_search` 个候选，回表应用 `WHERE`，够 k 条就返回，不够就结束——0.8 之前，这就是召回丢失的主要来源。

0.8 引入 iterative scan 之后，可以用 `hnsw.iterative_scan = relaxed_order` 或 `strict_order` 让索引继续扫，直到凑够 k 条或达到 `hnsw.max_scan_tuples`（默认 20000）。`relaxed_order` 不保证严格按距离排序，但延迟更稳；`strict_order` 保证顺序，代价是更慢。

```sql
-- 低命中率过滤：先缩小索引范围，再 ANN
CREATE INDEX ON docs USING hnsw (embedding vector_cosine_ops)
  WHERE tenant_id = 42;

SET hnsw.ef_search = 100;
SET hnsw.iterative_scan = relaxed_order;

SELECT id, embedding <=> :q AS dist
FROM docs
WHERE tenant_id = 42
ORDER BY embedding <=> :q
LIMIT 10;
```

更根本的解法是减少候选集：按租户 / 分类做 partial index 或分区，让每次查询只碰一个小索引。pgvector 的文档也明确建议：低命中率的过滤，不要指望单棵 HNSW 图。

| 过滤命中比例 | 风险 | 做法 |
| --- | --- | --- |
| > 10% | 低 | 三者都能扛；ES 最省心 |
| 1%–10% | 中 | ES pre-filter；Milvus partition key；PG partial index |
| < 1% | 高 | 别让单棵 HNSW 硬扛：分区、租户索引、先过滤再 ANN |

这张表是经验值，不是定律。真正要测的是你的数据分布：过滤条件是否相关、命中率是否随查询漂移、最差查询的延迟是多少。

## 五、混合检索：谁自带，谁要自己拼

混合检索的常见形态是 BM25 + 向量 + RRF。三家在这一层的成熟度差距，比纯向量更大。

Elasticsearch 是原生的。一个查询里可以同时有 `query`（BM25）和 `knn`，用 RRF 或线性组合融合；retriever 把多路召回串成 pipeline；ES|QL 还能继续做聚合。过滤、分页、高亮、权限都在同一套查询里。

Milvus 支持多向量字段和 `WeightedRanker` / `RRFRanker`，2.5 起又补了稀疏 BM25；3.0 的 SINDI 稀疏索引、StructArray（ColBERT / ColPali 的多向量）和 TEXT LOB 把多模态检索也纳入进来。它更擅长“向量为主、文本为辅”，而不是反过来。

pgvector 要自己拼。`tsvector` + GIN 做全文，HNSW 做向量，两个索引分别查，再在 SQL 里做 RRF。没有内置融合算子，planner 也不会跨索引估算代价；查询写起来不难，难的是调优和稳定的召回。

一个粗略的判断：如果 80% 的查询都带文本条件，ES 的工程优势比向量 QPS 更重要；如果查询主要是“以图搜图”或“以向量搜向量”，Milvus 的专用路径更短；如果只是给现有业务加一个“相似推荐”，pgvector 足够了。

## 六、性能：把数字放回前提

先给一张不带数字的场景表。

| 场景 | Milvus | Elasticsearch | pgvector |
| --- | --- | --- | --- |
| 百万级、单机、低并发 | 过剩 | 合适 | 很合适 |
| 千万级、高并发 | 合适 | 合适 | 吃力 |
| 亿级以上 | 原生设计 | 可以，但成本高 | 需分片 / 多实例 |
| 高频写入、更新 | 好 | 中 | 差 |
| 复杂过滤 | 好 | 最好 | 中 |
| 文本 + 向量混合 | 好 | 最好 | 自己拼 |
| 事务、Join | 无 | 无 | 最好 |

“好 / 中 / 差”是同一行内的相对判断，不是绝对分数。这张表故意不写 QPS，因为 QPS 离开数据集、召回率、并发和过滤条件就没有意义。

再看几组公开数字，全部带前提：

- **pgvectorscale**：50M Cohere 768 维，99% recall，对比 Pinecone s1，p95 低 28×、吞吐高 16×、成本低 75%。厂商 benchmark。
- **VectorChord**：EDB 2026 年 6 月的 benchmark 宣称，在特定 `shared_buffers` 设置下，单客户端 QPS 是 pgvector 的 13.4×（179 vs 约 13）。厂商数据，原始 PDF 已不可访问。
- **Milvus 3.0 Loon**：300 万行、128 维、S3、256 并发读，单点读 I/O 从 Parquet 的约 9.4 MB 降到 Vortex + Loon 的约 0.07 MB，约 135×。官方内部 benchmark。
- **Elasticsearch Serverless Vector Database**：宣称可扩展到数千亿向量。厂商新闻稿。

这些数字的共同点是：都能证明“在我的条件下，我的方案更好”，但不能证明你的条件。

还有一个反例：lore-weave 在 181 行、1024 维的真实语料上测 pgvectorscale 的 StreamingDiskANN，服务器默认参数下 recall@10 只有 0.715，最差查询 0.30；把 `search_list` 调到 300、`rescore` 调到 200 之后，recall 到 1.000，p50 4.66 ms。同机的 HNSW 是 1.000 / 3.78 ms。

181 行说明不了十亿级，但它说明默认参数有多危险：同一个索引，默认配置和调优后可以是两个产品。

看到任何倍数，先问六个问题：

1. 数据集多大、多少维、什么分布？
2. 召回目标是多少，怎么测的？
3. 带不带过滤，过滤命中率多少？
4. 量化了吗，rescore 算进去了吗？
5. 并发多少，缓存是冷的还是热的？
6. 索引构建时间算了吗？

六个问题里有一个没对齐，倍数就不能横比。

## 七、扩展性：加机器之后，查询路径变不变

扩展性不是“能不能加机器”，而是“加机器之后查询路径变不变、成本是不是线性”。

Milvus 查询加 query node，写入加 data node，构建加 index node；proxy 无状态，可以横向扩。对象存储和 etcd 是共享依赖，WAL 可以用 Woodpecker 内置，也可以外接 Kafka。代价是组件多，K8s 基本是默认选项；小团队养一套 Milvus，人力成本常常高于机器成本。

ES 加节点、加副本容易；改主分片数要 reindex。kNN 要 fan-out 到所有 shard，分片越多，协调节点越忙，尾部延迟越难控。ES 的扩展性是“搜索集群”的扩展性，不是“向量库”的扩展性：它能到很大，但每一步都要付搜索系统的税。

PG 垂直扩容最先撞墙的是内存；只读副本能扩读，但副本上的索引也要建、也要占内存；Citus / PgDog 把查询 scatter 到多个库，归并回到应用层。pgvector 没有原生的分布式向量索引，十亿级要靠分区 + 多实例 + 路由，复杂度不比上一套 Milvus 低。

所以“PG 简单”只在中小规模成立。规模上去之后，省下的组件会以另一种形式还回来。

## 八、生态、许可与成本

| 维度 | Milvus | Elasticsearch | pgvector |
| --- | --- | --- | --- |
| 许可 | Apache 2.0 | ELv2 / AGPLv3 / SSPL | PostgreSQL License |
| 客户端 | pymilvus、Java、Go、Node、REST | 全语言 + Kibana | 所有 PG 驱动 |
| 全文检索 | BM25（2.5+） | 最强 | tsvector，够用 |
| 聚合 / BI | 3.0 有聚合、分面 | 最强 | SQL，最强 |
| 事务 / Join | 无 | 无 | 完整 |
| 托管 | Zilliz Cloud | Elastic Cloud / serverless | RDS、Aurora、Cloud SQL、Supabase、Neon |
| 运维复杂度 | 高 | 中 | 低 |
| GPU 索引 | 支持 | 支持 | 无 |

许可这块，ES 的 `bbq_disk` 需要 Enterprise 订阅；Milvus 是 Apache 2.0；pgvector 是 PostgreSQL License。选型时要把“能用”和“许可允许用”分开算。

成本要算三笔：内存、许可、人力。

内存账上一节已经算过。许可是最容易被低估的一笔：ES 的 DiskBBQ、部分安全与机器学习功能在付费层；Milvus 和 pgvector 没有这个问题，但 Milvus 的托管服务（Zilliz Cloud）按容量计价。人力则取决于团队：Milvus 需要平台 / 运维，ES 通常已经有集群，PG 通常已经有 DBA。

还有一笔隐形成本：embedding 模型升级要全量重算、重建索引。三家都一样，但数据量越大，这笔账越贵；选型时要把“三年内换一次模型”算进去。

## 九、选型：一张决策阶梯

{{< fig "diagrams/04-decision.svg" "选型决策阶梯。先排除已有的系统，再按规模、检索形态与并发要求逐级判断。" >}}

图里的顺序是刻意设计的：先看已有系统，再看规模，最后看检索形态。因为大多数团队的问题不是“选哪个最强”，而是“能不能不加一套系统”。

**已经有 PG，且 ≤ 1000 万向量。** 先用 pgvector。把 `halfvec`、partial index、iterative scan 用上；如果写入 / 过滤更重，再考虑 pgvectorscale 或 VectorChord。

**已经有 PG，但规模更大。** 先确认托管环境让不让装扩展。RDS 这类实例通常装不了 pgvectorscale / VectorChord，那就只能走分区 + 多实例，或者换系统。

**已经有 ES，文本 + 向量混合。** 用 ES。它的边际成本最低，过滤、聚合、权限都是现成的。

**亿级以上、高并发、多租户。** Milvus。存算分离、索引独立扩、多副本，这些是它的设计目标。

**向量在湖仓里。** Milvus 3.0 的 External Collections 可以零拷贝挂 Parquet / Iceberg，省掉一次搬运。

几个反模式：

1. **用 ES 当主库。** 它没有事务，更新是 delete + index，不适合账务 / 订单类数据。
2. **用 PG 扛亿级高并发。** 垂直扩容有上限，副本和分片会把复杂度还回来。
3. **为几百万向量上 Milvus。** 组件、K8s、对象存储、etcd，一套下来人力成本远高于收益。
4. **忽略 embedding 版本。** 模型换了不重建索引，召回会无声地掉。
5. **只测纯向量 QPS。** 真实查询带过滤、带文本、带分页，纯向量数字会高估体验。

## 十、参考

- [Milvus 3.0: lake-native vector search and a more powerful retrieval engine](https://milvus.io/blog/announcing-milvus-3-lake-native-vector-search-and-a-more-powerful-retrieval-engine.md)
- [Elasticsearch dense_vector 文档](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/dense-vector)
- [Elastic introduces serverless vector database](https://ir.elastic.co/News--Events/news/news-details/2026/Elastic-Introduces-Serverless-Vector-Database-Ship-in-Minutes-Scale-Affordably-to-Hundreds-of-Billions-of-Vectors/)
- [pgvector README](https://github.com/pgvector/pgvector)
- [pgvectorscale README](https://github.com/timescale/pgvectorscale)
- [lore-weave 向量后端召回实测（2026-08-10）](https://raw.githubusercontent.com/letuhao/lore-weave/refs/heads/main/docs/measurements/2026-08-10-vector-backend-recall.md)
- [The Case Against pgvector](https://alex-jacobs.com/posts/the-case-against-pgvector/)
- [VectorChord](https://github.com/tensorchord/VectorChord)

厂商数字均标注来源，未独立复现；版本与数据截至 2026 年 11 月。
