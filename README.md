# blog

Jason Cai 的个人站点源码：<https://www.jasoncai.site>

Hugo 构建，本地出 `public/` 再 rsync 到服务器。**服务器不装 Hugo，也不在服务器上构建。**

## 结构

```
content/posts/<slug>/index.md     # 文章（page bundle），slug 决定 URL /posts/<slug>.html
content/posts/<slug>/diagrams/    # 该篇的 SVG，正文里用 {{< fig "diagrams/x.svg" "图注" >}} 内联
data/tags.yaml                    # tag 词表，唯一权威
layouts/                          # baseof / single / list / terms / taxonomy / rss / 404
assets/css/site.css               # 纸感主题（全站统一，无暗色模式）
static/favicon.*                  # 站点图标（ico / svg / 32 / 512 / apple-touch），由 scripts/make-favicon.py 生成
scripts/                          # check-tags / verify-site / publish / blockstats / migrate / reverse-convert
docs/baseline/local.json          # 每篇的结构块数量基线，verify-site 用来防回归
docs/superpowers/                 # 设计与实施计划
```

## 写一篇新文章

```bash
hugo new content posts/<slug>/index.md
```

然后填 front matter：

| 字段 | 说明 |
|---|---|
| `title` | 文章标题，正文里不要再写 `# 一级标题` |
| `date` | 发布时间，**必须带时分**（如 `2026-11-09T21:19:00+08:00`），同一天多篇靠它排序 |
| `summary` | 首页与 RSS 用的一句话摘要，**必填** |
| `description` | `<meta name="description">` 用，可省略，缺省回落到 `summary` |
| `tags` | 1–4 个，必须来自 `data/tags.yaml`；写错或漏写，发布会被拦下 |

图：放在 `content/posts/<slug>/diagrams/`，正文里写 `{{< fig "diagrams/01.svg" "图注" >}}`，会自动内联并编号「图 N」。引用了不存在的图会让构建失败。

**代码块里要写短代码语法时**，必须转义成 `{{</* fig ... */>}}`。Hugo 会执行代码块里的 `{{< ... >}}`，不转义的话读者看到的是渲染结果而不是这段代码本身，而且会偷偷消耗图号。`check-tags.py` 会扫描并拦下未转义的写法。

需要新 tag 时，先往 `data/tags.yaml` 加一个 key（ASCII 小写连字符）+ `name`（中文显示名）+ `description`，再在文章里用。

## 本地预览

```bash
hugo server              # http://localhost:1313
hugo --minify --cleanDestinationDir   # 只出产物到 public/
```

## 发布

```bash
scripts/publish.sh                 # 校验 → 构建 → 自检 → 备份 → 上传 → 线上复验
scripts/publish.sh --dry-run       # 只跑到自检，不碰服务器
scripts/publish.sh --final         # 带 --delete 收尾（清理线上旧文件）
```

脚本会拒绝这些情况：tag 不合规、工作区不干净、另一个发布正在跑（`/tmp/blog-publish.lock`）、远端存在本地没有的文章（说明别人先发过，先合并）。

失败时按输出的命令回滚：

```bash
rsync -a --delete ~/backups/blog-<时间戳>/ ubuntu@62.234.122.22:/var/www/blog/
```

## 校验

```bash
python3 -m unittest discover -s tests      # 脚本单测
bash tests/test_publish.sh                 # 发布流程行为测试（全在临时目录）
python3 scripts/check-tags.py content/posts data/tags.yaml
python3 scripts/verify-site.py --public public
```

`verify-site.py` 检查：内容里的每篇都有产物、每篇的结构块数量相对基线没有漂移、每个用到的 tag 都有归档页、RSS 条目数与 `category`、sitemap 存在。

新文章不需要先登记基线（只会提示「还没进基线」）；**有意**给老文章加小节/图/表格后，块数量会变，这时刷新基线：

```bash
hugo --minify --cleanDestinationDir
python3 scripts/verify-site.py --public public --update-baseline
git add docs/baseline/local.json
```
