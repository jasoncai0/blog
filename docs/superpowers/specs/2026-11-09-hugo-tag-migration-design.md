# 博客 Hugo 化 + Tag 体系设计

- 日期：2026-11-09
- 状态：待批准（已通过 builder check，用户已定 3 项关键选择）
- 站点：https://www.jasoncai.site（62.234.122.22）

## 1. 意图与为什么

现在的博客是「12 份不同哈希的 `build.py` + 手工合并 index/rss + 一段 AGENTS.md 发布文字」。文章没有 tag 字段，站点没有 tag 浏览，发布靠人记忆。

要解决的三件事其实是同一件事：

1. 发布技能没有可执行的门禁，靠文字约定；
2. 站点没有 tag，读者无法按主题浏览；
3. 存量文章没有 tag，也没地方存。

统一方案：**用 Hugo 承担 tag 体系与页面生成**，把「写 tag」变成 front matter 里的必填字段，由构建过程强制校验。选 Hugo 而不是继续自研，是因为 taxonomy、tag 归档页、RSS category、sitemap 都是它的内置能力 —— 这恰好是自研脚本唯一真正吃亏的地方。

## 2. 目标

1. **单一事实源**：所有文章源、图、模板进一个 git 仓库（`blog/`，GitHub 私有 `jasoncai0/blog`），取代散落的 12 份 `build.py`。
2. **tag 硬门禁**：每篇 1–4 个 tag，必须来自固定词表；缺 tag、错 tag 直接构建失败，发布脚本拒绝上传。
3. **站点 tag 功能**：`/tags/` 索引、`/tags/<tag>.html` 归档、首页与文章页 tag chip、RSS 带 `<category>`。
4. **存量重写**：14 篇全部补 tag 并迁入新仓库（4 篇无 Markdown 源，从线上 HTML 反推）。
5. **一条命令发布**：`scripts/publish.sh` 完成 校验 → 构建 → 结构自检 → 备份 → 上传 → 线上复验 → 失败回滚。
6. **不破坏现有资产**：`/posts/<slug>.html` 全部 URL、canonical、RSS 地址保持不变。
7. **视觉统一**：全站统一到文章页的纸感主题（首页与 2 篇旧文随之变化，暗色模式不再保留）。

## 3. 非目标

- 不做全文搜索、tag 客户端筛选、评论、分页（v1 只做静态归档页）。
- 不再生成「自包含单文件 HTML 阅读版」；改为共享 CSS，本地用 `hugo server` 预览。
- 不保留暗色模式（`/style.css` 的 `prefers-color-scheme` 分支随之废弃）。
- 不引入 Node 工具链。
- 不改 nginx 配置。
- 不做多语言。

## 4. 反目标（什么算失败）

- **tag 沦为装饰**：词表自由生长，出现 `OpenCode`/`opencode`/`开放代码` 三个同义 tag。
- **URL 变了**：任何 `/posts/*.html` 失效或变成 `/posts/<slug>/`。
- **发布变复杂**：需要记住多步手工操作，或要 SSH 上去手改文件。
- **并发互相覆盖**：两个会话同时发布，后写者把前者文章冲掉。
- **迁移丢内容**：反推的 4 篇在转换中丢掉段落、图或表格。

## 5. 约束

| 约束 | 来源 | 影响 |
|---|---|---|
| 服务器 2 GB 内存，不安 Hugo | 现状 | 本地构建 + rsync 上传，服务器零构建依赖 |
| `/posts/<slug>.html` 必须不变 | 已发布 URL / RSS / canonical | `uglyURLs = true` |
| 内容中文，图以内联 SVG 为主 | 现有 14 篇 | 图继续内联，不退化为 `<img>` |
| 4 篇无 Markdown 源 | 全盘搜索确认 | 从 HTML 反推，验收基线改为 HTML |
| 现有 CSS 视觉是作者定过的 | `build.py` 内联样式 | 直接复用纸感主题，不改设计语言 |
| macOS 没有 `flock` | 本机环境 | 锁改用 `mkdir` 原子创建 |
| 多会话可能同时发布 | AGENTS.md 已记录 | 加锁 + 前置 `git pull --rebase` |

## 6. 方案对比

| 方案 | 做法 | tag 从哪来 | 代价 | 结论 |
|---|---|---|---|---|
| A 自研生成器 | 合并 12 份 build.py，自写 tag 页 | 自己实现 | 要维护 taxonomy/RSS/sitemap 等已有轮子 | 未选 |
| **B Hugo** | `content/posts/*.md` + front matter，taxonomy 内置 | 框架内置 | 一次性迁移 14 篇、写模板、装 hugo | **选中** |
| C Astro | content collections + 动态路由 | 自己写路由 | 引入 Node 工具链 | 未选：对 14 篇静态文档过重 |

## 7. 设计

### 7.1 仓库布局

```
blog/                                  # git 仓库，远端 github.com/jasoncai0/blog（私有）
├── hugo.toml
├── data/tags.yaml                     # 固定 tag 词表（唯一权威）
├── archetypes/post.md                 # 新建文章模板（自带 tags 字段）
├── content/posts/
│   ├── opencode-multiagent.md
│   ├── opencode-multiagent/diagrams/01-*.svg
│   ├── agent-vs-computer.md           # 反推生成
│   └── agent-vs-computer/diagrams/*.svg  # 从站根 /agent-vs-computer/ 收编
├── layouts/
│   ├── _default/{baseof,single,list,terms,taxonomy,rss}.html
│   ├── partials/{head,header,footer,tag-chips,figure}.html
│   └── index.html
├── assets/css/site.css                # 由 build.py 的 CSS 抽出 + tag chip 样式
├── scripts/
│   ├── reverse-convert.py             # HTML → Markdown（一次性，4 篇）
│   ├── check-tags.py                  # tag 门禁
│   ├── publish.sh                     # 一条命令发布
│   └── verify-site.py                 # 结构等价性 + 链接校验
└── docs/superpowers/specs/
```

### 7.2 Front matter 契约

```yaml
---
title: "OpenCode 的多智能体"
date: 2026-11-09
summary: "首页列表用的一句话摘要"
description: "meta description 用的框定版文案"   # 可选，缺省回落到 summary
tags: ["opencode", "multi-agent"]
series: opencode          # 可选，文章页系列导航
source_commit: a687c047   # 可选，保留现有「源码版本」信息
---
```

- `tags` 用 ASCII slug（URL 友好），显示名由 `data/tags.yaml` 映射中文。
- `summary` 必填；`description` 可选，缺省用 `summary`。**两版文案都保留**：列表读 `summary`，`<meta name="description">` 读 `description`，避免迁移丢掉现有文案。
- `title` 必填，源文件的 `# 一级标题` 迁移时删除，避免与模板标题重复。

### 7.3 Tag 模型与门禁

`data/tags.yaml`（12 个，含新增 `planning`）：

```yaml
opencode:            { name: OpenCode,         description: OpenCode 的架构、机制与源码拆解 }
architecture:        { name: 架构,             description: 系统结构与设计取舍 }
concurrency:         { name: 并发,             description: 并发、取消与执行模型 }
context-engineering: { name: 上下文工程,       description: 上下文构建、预算与压缩 }
multi-agent:         { name: 多智能体,         description: 委派、子会话与协作结构 }
codegen:             { name: 代码生成,         description: 写代码、改文件与工具调用 }
event-sourcing:      { name: 事件溯源,         description: 事件目录、投影与订阅 }
billing:             { name: 计费,             description: 用量、结算与对账 }
collaborative-editing: { name: 协同编辑,       description: OT、CRDT 与并发合并 }
planning:            { name: 任务规划,         description: 计划、任务分解与执行编排 }
agent:               { name: Agent,            description: Agent 与计算机系统的类比与演进 }
meta:                { name: 建站与随笔,       description: 站点本身、工具与写作 }
```

`scripts/check-tags.py` 校验（任一失败即非零退出）：

1. 每篇 `tags` 存在、非空、1–4 个；
2. 每个 tag 都在 `data/tags.yaml` 里（拼错即失败）；
3. 词表里没有被任何文章引用的孤儿 tag（仅警告）；
4. 同一篇内 tag 不重复。

**路径**：`uglyURLs=true` 下 taxonomy 输出 `/tags/<term>.html` 与 `/tags/index.html`。中文显示名只出现在页面，URL 保持 ASCII。

### 7.4 图（内联 SVG）

shortcode：`{{< fig "diagrams/01-delegation-timeline.svg" "委派时间线" >}}`

- 读取 page bundle 里的 SVG，去掉 `<?xml?>`，内联进 `<figure class="diagram">`；
- 用 `.Page.Scratch` 计数，自动编号「图 N」；
- 输出结构与现有 `build.py` 一致（`figure > div.diagram > svg` + `figcaption`），CSS 不用改。

**`agent-vs-computer` 的 7 张外链 SVG**（现位于站根 `/agent-vs-computer/*.svg`，共 ~50 KB）迁移时收进 page bundle 并内联，不再外链；同时删除站根的 `/agent-vs-computer/` 目录。

### 7.5 页面与样式（统一纸感主题）

- `assets/css/site.css`：搬 `build.py` 的 `CSS`（纸感配色、字体栈、表格、代码块、图样式），追加 `.tag-chip` / `.tag-cloud` / `.post-tags`。
- **统一**：首页、`hello`、`agent-vs-computer` 全部改为纸感版式；`/style.css` 及其暗色模式废弃，不再输出 `/style.css`。
- `layouts/index.html`：保留现有结构（header + `ul.posts` + footer），每条加 tag chips，顶部加 tag 云。
- `layouts/_default/single.html`：返回首页 → 标题 → 日期 + tag chips → TOC → 正文 → footer；底部加「同 tag 的其他文章」。
- `layouts/_default/terms.html`：`/tags/` 索引，按文章数降序。
- `layouts/_default/taxonomy.html`：`/tags/<tag>.html`。
- TOC 用 `.TableOfContents` 替代手工 `sec-N` 锚点。
- 新增 `404.html`、`sitemap.xml`、`robots.txt`。

### 7.6 URL 与 SEO

- `uglyURLs = true` → `/posts/opencode-multiagent.html` 不变。
- canonical 固定 `https://www.jasoncai.site/posts/<slug>.html`。
- RSS 仍输出 `/rss.xml`，每条带 `<category>`。
- `baseURL = "https://www.jasoncai.site/"`。

### 7.7 发布流程（`scripts/publish.sh`）

```
1. 前置：git 工作区干净；git pull --rebase；check-tags.py 通过；hugo 存在
2. 锁定：mkdir /tmp/blog-publish.lock（原子创建，失败即退出；macOS 无 flock）
3. 构建：hugo --minify --cleanDestinationDir
4. 本地自检：文章数 == content/posts/**/*.md 数；每个 tag 有归档页；rss 条目数 == 文章数
5. 备份：远端 /var/www/blog → /home/ubuntu/backups/blog-<ts>/
6. 上传（两段式）：
   a. 首次/切换：rsync -a（不带 --delete）
   b. 验收通过后：rsync -a --delete（收尾，清除旧文件）
7. 线上复验：curl 首页 / 最新文章 / /tags/ / 每个 tag 页 / rss.xml，断言 200 + 关键字符串
8. 失败：打印并执行回滚（从备份 rsync 回去）
```

并发防线：本地 `git pull --rebase` + `mkdir` 锁 + 上传前比对远端文章集合，发现远端有本地没有的 slug 就中止并提示先合并。

### 7.8 存量 14 篇的迁移

**有 Markdown 源的 10 篇**（9 个 `opencode-*/` + `billing-system/blog.md`）：脚本机械转换（加 front matter、删 `# H1`、`![...](...)` → `{{< fig >}}`、图搬进 page bundle）。

**无源的 4 篇**（`hello`、`agent-vs-computer`、`ot-crdt-lww`、`opencode-interrupt`）：`scripts/reverse-convert.py` 从**线上 HTML** 反推 Markdown —— 按 `build.py` 的渲染规则逆向拆块（h2/h3/p/blockquote/table/pre/figure/ul/ol/hr），内联 SVG 还原成 `diagrams/*.svg` + `{{< fig >}}`。转换后**以线上 HTML 为验收基线**逐项比对块数量，不等则人工修到相等。

**基线规则**：除 `opencode-plan` 外，以线上版本为准；`opencode-plan` 以本地最新源为准（本地比线上多一节「1.5 界面上怎么进入（以及一个坑）」，该节随本次迁移一起上线）。

**tag 分配**：

| 文章 | tags |
|---|---|
| opencode-architecture | opencode, architecture |
| opencode-deep-dive | opencode, architecture, context-engineering |
| opencode-deep-dive-2 | opencode, concurrency, architecture |
| opencode-deep-dive-3 | opencode, architecture |
| opencode-deep-dive-4 | opencode, event-sourcing |
| opencode-coding | opencode, codegen |
| opencode-context-management | opencode, context-engineering |
| opencode-interrupt | opencode, concurrency |
| opencode-billing | opencode, billing, architecture |
| opencode-multiagent | opencode, multi-agent |
| opencode-plan | opencode, planning |
| ot-crdt-lww | collaborative-editing, architecture |
| hello | meta |
| agent-vs-computer | agent, architecture |

`hello.md` 需**改写正文**：现文写着「Hugo、Hexo、Astro 都试过，它们解决的是我没有的问题」。迁移后不再成立，改为记录这次从自研脚本迁到 Hugo 的原因（tag 体系与 taxonomy 是自研唯一吃亏的地方）。

### 7.9 发布技能的改写

`~/.dsh/AGENTS.md` 的「发文章」段落替换为：

1. 新建：`hugo new posts/<slug>.md`（archetype 自带空 `tags`，会被门禁拦下）；
2. 写 `title/date/summary/tags`，tags 只能取 `data/tags.yaml` 的 key；新主题先加词表再写文章；
3. 图放 `content/posts/<slug>/diagrams/`，正文用 `{{< fig "diagrams/x.svg" "说明" >}}`；
4. `scripts/publish.sh` 一条命令发布；失败按输出的回滚命令处理；
5. 不再手工改 `index.html` 与 `rss.xml`。

同步更新 `~/.dsh/hosts/62.234.122.22.md` 的第 6、7 节。

## 8. 验收标准

1. `check-tags.py` 在 缺 tag / 错 tag / 空 tag / 超 4 个 tag 四种情况下均非零退出。
2. 10 篇有源文章：h2/h3/table/figure/pre/hr 数量与**迁移前本地构建产物**逐项相等。
3. 4 篇反推文章：上述块数量与**线上 HTML**逐项相等。
4. 全站遍历：sitemap 每个 URL 返回 200；`/posts/*.html` 集合与迁移前完全相同（14 个）。
5. `/tags/` 列出 **12** 个 tag（数字由 `data/tags.yaml` 实际条数推导，不硬编码）；每个 `/tags/<tag>.html` 的条目集合与 front matter 一致。
6. `rss.xml` 条目数 == 14，每条含 `<category>`。
7. 线上抽查 3 篇（最长 `opencode-deep-dive`、最短 `hello`、外链图转内联的 `agent-vs-computer`）视觉正确。
8. 一次完整发布 < 60 s，失败可自动回滚。

## 9. 决定记录

| 决定 | 谁定的 | 理由 |
|---|---|---|
| 用 Hugo，不用自研/Astro | 用户 | tag 体系交给框架 |
| 4 篇无源文章从 HTML 反推 | 用户 | 保住单一事实源 |
| 全站统一纸感主题 | 用户 | 12/14 已是该版式；放弃暗色模式 |
| 仓库放 GitHub 私有 `jasoncai0/blog` | 用户 | 草稿不外泄，多会话可对齐 |
| opencode-plan 纳入，加 `planning` | 我 | 已发布内容不能排除在 tag 体系外 |
| 基线取本地最新源 | 我 | 本地是源，线上是产物 |
| summary 与 description 双字段 | 我 | 两版文案都保留 |
| 两段式上传（先不带 --delete） | 我 | 避免新旧流程切换期误删 |
| `mkdir` 锁替代 `flock` | 我 | macOS 无 flock |

## 10. 留给构建者的决定

- baseof/single 模板的拆分粒度；
- tag chip 的视觉细节（沿用现有配色变量，不引入新色）；
- `verify-site.py` 的检查项组织；
- 反推脚本的容错策略（遇到无法解析的块时是报错还是降级为 raw HTML）。
