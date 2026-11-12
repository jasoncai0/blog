---
title: "视频资产血缘：区间、版本与召回"
date: "2026-11-12T13:50:00+08:00"
summary: "视频血缘不能按文件记：一条成片是若干时间区间的组合，换 2 秒素材只该重渲受影响的片段；召回也不是删文件，而是从模型、授权或安全事件出发，沿 job、timeline、export、publication 找到所有在投版本并逐条处置。"
description: "视频资产血缘的区间模型、数据表、命中查询与召回状态机：半开区间、时间基、变速映射、转场 handle、嵌套时间线，以及从触发到关闭的召回流程。"
tags: ["architecture", "agent", "lineage"]
slug: "video-asset-lineage"
---

11 月 10 日，一条 30 秒品牌片要换掉第 12–14 秒的背景音乐。团队把整条片子重渲了一遍：剪辑工程里只有最终成片和音乐文件，没人说得清哪几个镜头用了这段音乐。

同一周，角色 LoRA v3 因授权问题撤稿。运营在六个渠道手动找了三天，漏了两个信息流版本。发布单上写的是“用了 LoRA v3”，但没人能回答：哪些镜头、哪些导出、哪些还在投。

两个事故，同一个原因：**视频血缘被记成了文件树，而不是时间轴。** 文件树能回答“这条片子和这个音乐有没有关系”，回答不了“哪一秒用了它、影响了哪些版本、现在该下架哪几条”。

这篇文章只展开两件事：**区间**和**召回**。前者解决“定位到秒”，后者解决“找到并处置到每一条投放”。

## 1. 文件级血缘丢掉了什么

一条 30 秒的成片，在文件级血缘里通常长这样：

```text
final-v3.mp4  --derived_from-->  music-M12.mp3
final-v3.mp4  --derived_from-->  shot-03.mov
final-v3.mp4  --rendered_by-->   resolve-19
```

这三条边都是真的，但都没法直接用来做影响分析：

- 换掉 `music-M12.mp3` 的第 3–4 秒，是重渲整条，还是只重渲某几个镜头？
- `shot-03.mov` 同时被横版、竖版、teaser 用过，改一个区间会影响哪几个导出的哪几秒？
- 已经投放到六个渠道的版本，哪些必须下架，哪些只需替换？

文件级血缘丢了三样东西：

| 丢失 | 文件级的回答 | 区间级的回答 |
|---|---|---|
| 定位 | 成片依赖这个素材 | `m-01` 的 `[13.0, 14.0)` 用了素材的 `[3.0, 4.0)` |
| 传播 | 所有用过这个文件的成片 | 哪些 `timeline_version`、`export`、`publication` |
| 处置 | 重渲整条 | 只重渲受影响的镜头或区间 |

还有一个更隐蔽的问题：文件级边不知道“怎么用的”。同一段音乐可能是背景底乐、画面内声源、被静音的参考轨，也可能是变速后的采样。用途不同，替换策略完全不同。区间模型至少要把 track、源区间、目标区间和速度记下来。

## 2. 区间模型：把镜头拆成可计算的段

### 2.1 五个对象

视频血缘里，真正需要落表的不是“文件”，而是下面五个对象：

| 对象 | 回答什么 | 关键字段 |
|---|---|---|
| `timeline_version` | 这是哪一版剪辑 | `timeline_id`、`version_id`、`timebase`、`duration` |
| `clip` | 哪个素材的哪一段，放在时间线的哪一段 | `dst` 区间、`src_artifact`、`src` 区间、速度、handle |
| `artifact` | 实际的字节是什么 | `content_hash`、`kind`、`duration`、`proxy_ref` |
| `export` | 从哪一版时间线渲染出了什么 | `timeline_version`、渲染区间、`artifact_id` |
| `publication` | 投到了哪个渠道、什么时间在投 | `export_id`、channel、market、live 窗口 |

召回还需要两张表：`recall` 记录一次召回的触发、级别、截止时间和 owner；`recall_item` 把处置落到每一条 `publication` 上。图 2 是这些对象的主要关系。

这里有两个容易做错的点。

第一，`clip` 是“放置”，不是资产。同一个 `shot-03.mov` 可以在三条时间线里出现很多次，每次的入点、出点、速度都可能不同。把 `clip` 当成资产版本，会把“素材复用”和“素材替换”混在一起。

第二，`export` 必须钉住 `timeline_version` 的快照，不能钉“最新版”。否则三个月后回查，时间线已经被改过，导出的血缘就对不上了。`publication` 则钉住 `export`，因为召回的实际对象是渠道上的那一条投放，不是 master。

### 2.2 半开区间与时间基

所有区间用半开区间 `[start, end)`，不用闭区间。原因很实际：两个相邻镜头 `[0, 12)` 和 `[12, 24)` 在 12 秒处不应该重复计入。闭区间会让每一次切点都变成两次命中。

时间统一用整数 tick，不用浮点秒。推荐时间基 90000，能整除 24、25、30、60 等常见帧率：

```text
1 秒 = 90000 ticks
24fps 的 1 帧 = 3750 ticks
25fps 的 1 帧 = 3600 ticks
```

对 29.97fps，不要存“帧号”，存 PTS tick，再用有理数换算：

```text
frame = ticks × fps_num / (timebase × fps_den)
```

其中 29.97fps 写成 `fps_num=30000, fps_den=1001`。可变帧率（VFR）视频尤其不能依赖帧号：同一秒里的帧数可能不同，只有 PTS 是稳定的。

### 2.3 源到目标的映射

`clip` 存四个时间点：源入点 `src_start`、源出点 `src_end`、目标入点 `dst_start`、目标出点 `dst_end`。再做一次变速，映射关系是：

```text
dst = dst_start + (src - src_start) / speed
src = src_start + (dst - dst_start) × speed
```

`speed` 的定义是“源秒 / 目标秒”。2 倍速播放时，1 秒目标时间消耗 2 秒源素材，所以 `speed = 2`。

图 1 是一个具体例子：源素材 `shot-03.mov` 的 `[3.0, 4.0)` 被三条时间线复用。

{{< fig "diagrams/01-intervals.svg" "一个源区间，三个落点。源素材只改 1 秒，受影响的是三条成片里的三个区间；按文件记只能得到「都依赖 shot-03」，按区间记才能定位到具体秒。" >}}

三条 clip 的参数不同：

| clip | src in | dst in | speed | 命中 src | 命中 dst |
|---|---|---|---|---|---|
| `m-01` | 2.0 | 12.0 | 1× | `[3.0, 4.0)` | `[13.0, 14.0)` |
| `v-07` | 2.0 | 4.0 | 1× | `[3.0, 4.0)` | `[5.0, 6.0)` |
| `t-03` | 2.0 | 5.0 | 2× | `[3.0, 4.0)` | `[5.5, 6.0)` |

同一个源区间，落点可以是三个完全不同的目标区间。反向也一样：一个目标区间可能由多个源素材合成，比如前景、背景、字幕、音效各一条轨。所以区间血缘天然是 N:M，不是文件树那种一对多。

### 2.4 转场、嵌套与音频

**转场要算 handle。** 一个 12 帧的交叉溶解，实际会读取切点前后各一段素材。只在 `clip` 上记 `[src_start, src_end)` 不够，还要记 `handle_in`、`handle_out`。否则你换掉切点前 6 帧的素材，影响分析会漏掉这个转场。

**嵌套时间线要递归。** `clip.src_artifact` 不一定是原始素材，也可能是另一条时间线的 `export`。比如一个片头模板被三条成片复用，改模板就要沿嵌套关系往下追。版本不可变，遍历天然是有向无环图，但仍要设深度上限，防止数据错误导致无限递归。

**音频不要混时间基。** 音频常用 48000Hz 采样点，视频用 tick。入库时统一换算到时间线 tick，或者单独存采样区间并在查询时换算；最忌讳的是把音频采样点直接塞进视频 tick 字段，差三个数量级。

## 3. 数据模型与查询

### 3.1 表结构

核心表可以很朴素，不需要一开始就上图数据库：

{{< fig "diagrams/02-model.svg" "区间血缘的数据模型。timeline_version 是不可变快照，clip 存两段区间与速度，export 钉住快照，publication 钉住导出，recall_item 钉到每一条投放。" >}}

```sql
create table timeline_version (
  timeline_id    text        not null,
  version_id     text        not null,
  timebase       integer     not null default 90000,
  duration_ticks bigint      not null,
  created_at     timestamptz not null,
  primary key (timeline_id, version_id)
);

create table artifact (
  artifact_id    text        not null primary key,
  content_hash   text        not null,
  kind           text        not null,
  duration_ticks bigint,
  proxy_ref      text,
  job_id         text
);

create table clip (
  clip_id          text    not null primary key,
  timeline_id      text    not null,
  timeline_version text    not null,
  track            text    not null,
  dst_start        bigint  not null,
  dst_end          bigint  not null,
  src_artifact     text    not null references artifact(artifact_id),
  src_start        bigint,
  src_end          bigint,
  speed_num        integer not null default 1,
  speed_den        integer not null default 1,
  handle_in        bigint  not null default 0,
  handle_out       bigint  not null default 0,
  check (dst_end > dst_start),
  check (src_end is null or src_end > src_start),
  check (speed_num > 0 and speed_den > 0),
  foreign key (timeline_id, timeline_version)
    references timeline_version (timeline_id, version_id)
);

create index clip_src_range on clip (src_artifact, src_start, src_end);
create index clip_timeline on clip (timeline_id, timeline_version);

create table export (
  export_id        text        not null primary key,
  timeline_id      text        not null,
  timeline_version text        not null,
  range_start      bigint      not null,
  range_end        bigint      not null,
  artifact_id      text        not null references artifact(artifact_id),
  created_at       timestamptz not null,
  check (range_end > range_start),
  foreign key (timeline_id, timeline_version)
    references timeline_version (timeline_id, version_id)
);

create table publication (
  pub_id    text        not null primary key,
  export_id text        not null references export(export_id),
  channel   text        not null,
  market    text        not null,
  live_from timestamptz not null,
  live_to   timestamptz,
  status    text        not null
);

create table recall (
  recall_id   text        not null primary key,
  trigger_ref text        not null,
  severity    text        not null,
  status      text        not null,
  cutoff_at   timestamptz not null,
  owner       text        not null
);

create table recall_item (
  recall_id      text not null references recall(recall_id),
  publication_id text not null references publication(pub_id),
  action         text not null,
  status         text not null,
  evidence_ref   text,
  primary key (recall_id, publication_id, action)
);
```

图 2 省略了 `generation_job`：真实系统里 `artifact` 还会带 `job_id`，用来连模型、LoRA、seed、成本和重试。区间表只负责时间关系，不负责生成配方。

### 3.2 命中查询

给定“源素材 + 源区间”，找所有受影响的 clip，是一次标准的半开区间相交：

```sql
with hit as (
  select c.clip_id,
         c.timeline_id,
         c.timeline_version,
         c.src_start,
         c.src_end,
         c.speed_num,
         c.speed_den,
         c.dst_start,
         greatest(c.src_start, $2) as hit_start,
         least(c.src_end, $3)    as hit_end
  from clip c
  where c.src_artifact = $1
    and c.src_start < $3
    and c.src_end   > $2
)
select clip_id,
       dst_start + ((hit_start - src_start) * speed_den::numeric
                    / speed_num)::bigint as hit_dst_start,
       dst_start + ((hit_end - src_start) * speed_den::numeric
                    / speed_num)::bigint as hit_dst_end
from hit;
```

`src_start < $3 and src_end > $2` 就是半开区间的重叠判断；不需要 `between`，也不会在边界上重复。

如果 clip 带 `handle_in` / `handle_out`，命中判断要先把源区间扩到 `[src_start - handle_in, src_end + handle_out)`；否则转场读取的帧不会出现在影响面里。

得到目标区间后，再沿 `export` 和 `publication` 反查：

```sql
select p.pub_id, p.channel, p.market, e.export_id
from clip c
join export e
  on e.timeline_id = c.timeline_id
 and e.timeline_version = c.timeline_version
 and e.range_start < c.dst_end
 and e.range_end   > c.dst_start
join publication p on p.export_id = e.export_id
where c.clip_id = $1
  and p.status = 'live'
  and (p.live_to is null or p.live_to > now());
```

嵌套时间线要在这个查询外面加一层递归：如果 `src_artifact` 本身是某个 `export` 的产物，就先找到它的 `timeline_version`，再继续往下查。生产环境要给递归设深度和节点数上限，并缓存“素材 → 时间线”的反向索引。

## 4. 召回：从触发到关闭

召回不是“执行一条删除语句”，而是“从触发条件出发，定界、止血、修复、复核”的流程。它和影响分析的区别在于：影响分析回答“可能影响谁”，召回还要回答“谁负责、做到哪一步、怎么证明做完了”。

### 4.1 触发条件

| 触发 | 典型例子 | 遍历起点 |
|---|---|---|
| 模型 / LoRA 撤稿 | 授权问题、风格污染、供应商下线 | `model_version` / `lora_version` |
| 训练数据问题 | 图片未授权、被投毒 | `dataset_item` |
| 素材授权到期 | 音乐、字体、肖像、商标 | `license` |
| 内容安全 | 违规画面、仇恨符号、未成年人 | `moderation_event` |
| 品牌 / 法务 | 声明不实、竞品元素、区域禁用 | `approval` / `legal_hold` |
| 安全漏洞 | 工具或依赖 CVE | `asset_version` |

不同触发，扇出的路径不同。模型撤稿走“模型 → job → artifact → clip → timeline → export → publication”；训练数据问题还要先经过“dataset → fine-tune job → model”这一段；授权到期则从 `license` 直接挂到素材和投放。

### 4.2 扇出：从 1 到 6 条投放

图 3 是一次 LoRA v3 撤稿的扇出：

- 1 个触发；
- 12 个生成 job；
- 48 个 artifact（12 个镜头 × 4 个变体）；
- 3 条时间线（16:9、9:16、1:1）；
- 18 个导出（3 条时间线 × 6 个渠道版本）；
- 6 条投放（4 条 live，2 条 scheduled）。

{{< fig "diagrams/03-recall.svg" "召回扇出与状态机。从 LoRA 撤稿到 6 条投放，扇出深度 5；定界后先止血，再修复、复核、关闭。" >}}

这张图里有两个关键决定。

第一，**召回对象是 `publication`，不是 `artifact`。** 下架一条成片，不等于六个渠道都停了；同一个 master 可能已经转码成多个渠道版本，每个版本有自己的投放状态和下线接口。

第二，**在线查询只做前 3 跳。** “模型 → job → artifact”可以在线查；“artifact → clip → timeline → export → publication”通常要预计算或离线批处理，尤其是训练数据召回这种可能几十万条边的场景。

### 4.3 状态机

一次召回至少有六个状态：

```text
opened → scoped → contained → remediating → verified → closed
```

- **opened**：登记触发、级别、owner、截止时间。没有 owner 的召回，最后一定烂尾。
- **scoped**：把受影响集合冻结成一个 snapshot，记录 `cutoff_at`；去重、估算影响面、确认误报。
- **contained**：先止血。live 的先下架或暂停，scheduled 的先停发；法务要求时加 legal hold。
- **remediating**：重渲、替换、重剪、补授权。新版本照常入库，不改旧版本。
- **verified**：重新跑一遍定界查询，抽样播放，核对 C2PA/水印，法务签字。
- **closed**：复盘、更新规则、记录指标。

`cutoff_at` 之后新发的投放不会被第一次 snapshot 覆盖，所以要有一个 **delta sweep**：在召回关闭前，重新查一次“cutoff 之后新增的 publication”，补进同一张 `recall`。图 3 底部的虚线就是这个回路。

### 4.4 定界与止血

定界阶段最重要的是“可解释的影响面”，不是“尽量大”。三个维度要一起用：

- **时间**：只召回触发时间之后生产的 job，还是所有历史版本？模型/LoRA 通常要全量；单次事故可以按时间窗收窄。
- **环境**：prod / staging / 沙箱；staging 可以只告警，不阻断。
- **渠道**：live / scheduled / archived；live 优先，archived 只做记录。

止血阶段要避免两个极端：只发一条“已下架”的通知，或者直接删文件。正确做法是给每条 `publication` 建一条 `recall_item`，记录动作、状态、证据和重试次数。动作至少包括：

```text
unpublish   下架
pause       暂停投放
replace     替换为新版本
re_render   重渲后替换
notify      仅通知，等待人工处理
legal_hold  法务冻结
```

`recall_item` 的主键是 `(recall_id, publication_id, action)`，天然幂等；渠道接口失败可以重试，不会重复下架。

### 4.5 误报与漏报

召回最怕两种错误。

**误报**通常来自“边太粗”。比如把 `model_version` 直接连到所有用过该模型的产物，一次撤稿会召回整个模型历史。更细的做法是带上 job 的时间、租户、campaign，先按影响面排序，再逐批确认。

**漏报**通常来自“边缺失”。渠道投放可能走人工，`publication` 没入库；素材可能被二次上传，`content_hash` 对不上；剪辑师可能直接在剪辑软件（NLE）里替换素材，没有回写 `clip`。所以召回不能只查图，还要做对账：

- 渠道后台的在投清单 vs `publication` 表；
- 存储里的 master 清单 vs `artifact` 表；
- 近 30 天有播放量但没有血缘记录的素材。

对账出来的差异，应该进“待补边”队列，而不是等下一次事故。

## 5. 工程细节与坑

1. **用帧号当坐标。** VFR、丢帧时间码、不同帧率混剪都会让帧号失真；tick/PTS 才是稳定坐标。
2. **忽略转场 handle。** 只记 cut 点，不记切点前后读取的帧，影响分析会漏掉转场。
3. **变速反向映射只取一个区间。** 源区间到目标区间可能是 1:N；反向也可能 N:1。不要假设一一对应。
4. **嵌套时间线不设深度。** 版本不可变理论上无环，但数据错误、手工导入仍可能造环；递归必须有深度和节点数上限。
5. **音频/视频时间基混用。** 48000 采样点和 90000 tick 不能直接比较；入库统一换算。
6. **proxy 当 master。** 代理文件用于预览，不能作为发布的源；`content_hash` 和 `proxy_ref` 要分开。
7. **召回只到 artifact。** 没有 `publication` 边，就不知道渠道上还有没有在投版本。
8. **没有 cutoff 和 delta sweep。** 定界之后新发的投放会漏掉；召回关闭前必须再扫一次。
9. **删除旧版本。** 审计、复现、法务都依赖旧版本；用 tombstone，不要物理删除。
10. **只靠 C2PA。** 平台转码可能剥离 manifest；C2PA 是证据之一，不是唯一来源。

## 6. 落地顺序

不要一开始就做“全量视频资产图”。按下面三步走：

**第一阶段：区间与投放边。** 选一条真实 pipeline（比如 15 秒信息流视频），接 NLE 导出（OTIO/EDL/XML）或渲染农场回调，把 `timeline_version`、`clip`、`export`、`publication` 四张表跑通。先支持两个查询：“这个素材影响了哪些镜头”和“这个镜头出现在哪些投放”。

**第二阶段：召回状态机。** 先接模型/LoRA 撤稿和授权到期两类触发，把 `recall`、`recall_item`、`cutoff_at`、`delta sweep` 做出来。渠道先接两个，手工渠道用对账清单兜底。

**第三阶段：训练数据与安全。** 把 dataset → fine-tune → model 这条链接上，接内容安全事件；帧级索引只在需要精确重渲时下钻，不要默认展开。

指标不用多，六个够用：

| 指标 | 定义 | 目标 |
|---|---|---|
| 区间覆盖率 | 有 `src` 区间的 clip / 总 clip | > 95% |
| 投放边覆盖 | 有 `export_id` 的在投 publication / 总在投 | > 98% |
| 定界耗时 | 触发到 snapshot 完成 | < 15 分钟 |
| 止血耗时 | 触发到 live 投放全下架/暂停 | < 1 小时 |
| 召回完整率 | verified item / scoped item | 100% |
| 误召回率 | 误报 publication / scoped publication | < 5% |

## 7. 结语

视频血缘的难点，不是把区间存下来，而是让区间在版本、转码、投放之后仍然可追。召回的难点，也不是把受影响的东西查出来，而是把“查出来”变成有人负责、有状态、可验证的处置。

一句话：**区间是视频血缘的最小单位，publication 是召回的最小单位，版本是两者之间的桥。** 没有状态的召回，只是一次搜索。
