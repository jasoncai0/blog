# blog

Jason Cai 的个人站点源码：<https://www.jasoncai.site>

Hugo 构建，本地出 `public/` 再 rsync 到服务器。**服务器不装 Hugo，也不在服务器上构建。**

## 结构

```
content/posts/<slug>/index.md     # 文章（page bundle），slug 决定 URL /posts/<slug>.html
content/posts/<slug>/diagrams/    # 该篇的 SVG，正文里用 {{< fig "diagrams/x.svg" "图注" >}} 内联
data/tags.yaml                    # tag 词表，唯一权威
layouts/                          # baseof / single / list / terms / taxonomy / rss / 404
layouts/partials/                 # calendar（写作日历）/ rail-home（首页左栏）/ rail-article（文章页目录）
assets/css/site.css               # 纸感主题（全站统一，无暗色模式）
assets/js/                        # progress-core.mjs / reading-progress.js（阅读进度）、calendar-core.mjs / site-nav.js（侧栏）
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

图一律按栏宽缩放（矢量图放大不糊），**不横向滚动**；鼠标悬停时右上角出现「放大」，点开是灰屏遮罩里的全屏图，Esc / 点遮罩 / 右上角 × 关闭，打开期间页面锁定不滚动。没有 JS 时按钮不出现，图照常完整显示。

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

## 同步服务（笔记 + 阅读进度）

笔记和阅读进度都存在浏览器 `localStorage`，只在本机有效。为了让换浏览器、换设备也还在，站点旁边跑了一个极小的同步服务 `server/notesd.py`（Python 标准库 + SQLite，无第三方依赖）：

- 接口挂在 `https://jasoncai.site/api/`（nginx 反代到 `127.0.0.1:8099`），客户端 `assets/js/sync.js` 用它做「本地优先 + 后台同步」。
- 页面先读本地（离线照常用），连上服务端后合并：笔记按更新时间取新的、删掉的用墓碑同步；进度取最大值，读完状态只增不减。
- 写操作先落本地再排队上行；断网时队列留在 `jasoncai:sync-queue:v1`，恢复后自动补发。
- 读公开；写默认也公开，设了 `NOTES_EDIT_KEY` 才要求 `X-Edit-Key`。

本地跑服务与测试：

```bash
python3 server/notesd.py --port 8099 --db /tmp/notes.db   # 起服务
python3 server/test_notesd.py                             # 接口测试（7 例）
python3 scripts/tests/test_notes_sync.py                  # 跨浏览器同步（5 例，自带 notesd）
```

部署见 `~/.dsh/hosts/blog-jasoncai.site.md` §14（systemd + nginx 两段）。

## 阅读进度

文章页顶部有一条 2px 的进度条，右下角在滚动后出现百分比胶囊（≥95% 显示「读完」）；再次打开同一篇会回到上次位置，并提示「从 N% 继续」。首页每篇的 tag 行后会标出读到哪了。

- 只存在**读者自己的浏览器**里，键名 `jasoncai:read:v1`，不上传服务器。
- 想清空：控制台执行 `localStorage.removeItem('jasoncai:read:v1')`。
- 同一篇只记最高进度；超过 300 篇或 180 天未访问的条目会被自动丢弃。
- 没有 JS、或浏览器禁用了存储时，页面照常可读，只是没有进度功能。
- 内部实现见 `docs/superpowers/specs/2026-11-10-reading-progress-design.md`。

## 侧栏

首页左栏是写作日历和标签云，文章页左栏是目录；窄屏（<900px）时它们堆到正文上方。

正文行宽自适应：≥1440px 时 938px 起，随窗口长到 1040px 封顶，**相对页面居中**，左栏（`clamp(180px, 13vw, 210px)`）落进左边距（`.page` 放宽到 1680px、左右留白收到 24px 才有这个余量）；900–1439px 时左栏占一列（240px），正文占剩下的（608–848px）。收起目录时正文位置不动，只是窄轨缩到 44px。

- 日历：服务端直接渲染整年的格子，默认只显示最近 12 周，可切「全年」；‹ › 平移，日期框跳到某天并高亮那一周；点有文章的格子会滚到并闪一下当天在首页的条目。
- 目录：点标题旁的「收起」缩到 44px，状态存在 `jasoncai:toc:collapsed`；滚动时当前小节以左侧竖线标出；点链接不会自动收起。
- 「上次读到这」：文章页在你离开的百分比位置画一条虚线，滚过去后淡出；自动续读时不画（人已经在那个位置了）。
- 没有 JS 时：日历停在整年视图、目录常开、不出现折叠按钮和日历控件，正文照常读。

## 笔记与高亮

选中正文里的任意一段（同一段落内），工具条会浮出来：点「高亮」就把它标成浅橙色；点「批注」再加一句自己的话。点高亮或左栏「本文笔记 ·N」列表里的条目，可以重新打开批注改字或删除。

- 全部存在**读者自己的浏览器**里，键名 `jasoncai:notes:v1`，不上传服务器；换浏览器、清缓存就没了，所以左栏给了「导出」，导出内容是这篇的 JSON。
- 一条笔记记的是「原文 + 前后各 32 字 + 段落下标」，文章改字后如果对不上就安静跳过（不会标错句子）；同一句话出现多次时标你选的那一处。
- 「本文笔记」列表在宽屏（≥1440px）时位于**页面右侧留白**；窄屏放到正文末尾。批注气泡始终贴着你点的那处高亮。
- 首页有笔记的文章会在 tag 行后标「有笔记 ·N」，bfcache 返回时也会刷新。
- 没有 JS、或浏览器禁用了存储时，页面照常可读，只是没有笔记功能。
- 内部实现见 `docs/superpowers/specs/2026-11-12-notes-highlight-design.md`。

## 校验

```bash
python3 -m unittest discover -s tests      # 脚本单测
node --test "scripts/tests/*.test.mjs"     # 阅读进度纯逻辑
hugo --minify && python3 scripts/tests/test_reading_progress.py   # 阅读进度浏览器回归
hugo --minify && python3 scripts/tests/test_sidebar.py            # 侧栏 / 目录 / 标记浏览器回归
hugo --minify && python3 scripts/tests/test_figure_zoom.py        # 图放大（lightbox）浏览器回归
hugo --minify && python3 scripts/tests/test_notes.py              # 划词高亮 / 批注浏览器回归
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
