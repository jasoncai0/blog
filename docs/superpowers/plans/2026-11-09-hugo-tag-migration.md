# 博客 Hugo 化 + Tag 体系 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 14 篇手工 HTML 博客迁到 Hugo，建立前台不可绕过的 tag 体系，并把发布收敛成一条可回滚的命令。

**Architecture:** 本地 Hugo 构建出纯静态 `public/`，rsync 到既有 nginx 目录；tag 的唯一权威是 `data/tags.yaml`，每篇文章的 front matter 必须引用词表内的 key，构建前的 Python 门禁负责拦截违规。URL 用 `uglyURLs=true` 保持 `/posts/<slug>.html` 不变。

**Tech Stack:** Hugo 0.169+（local only）、Python 3.11+ 标准库（无第三方依赖）、bash、rsync、无框架测试（`unittest`）。

**Spec:** `blog/docs/superpowers/specs/2026-11-09-hugo-tag-migration-design.md`

## Global Constraints

- 所有文章 URL 必须是 `https://www.jasoncai.site/posts/<slug>.html`；`uglyURLs = true`，不得改成目录式 URL。
- tag key 一律 ASCII 小写连字符；显示名只存在于 `data/tags.yaml`。
- 词表恰好 12 个 key：`opencode` `architecture` `concurrency` `context-engineering` `multi-agent` `codegen` `event-sourcing` `billing` `collaborative-editing` `planning` `agent` `meta`。
- 每篇 front matter 的 `tags` 数量 1–4，且必须全部命中词表。
- 全站统一纸感主题；**不输出** `/style.css`，**不保留**暗色模式。
- 图必须内联为 `<figure class="diagram"><svg>`；不得用 `<img>` 指向外链 SVG。
- 服务器**不安装** Hugo，也不在服务器上构建；只有 rsync 上传。
- macOS 无 `flock`；锁一律用 `mkdir` 原子创建。
- Python 脚本只用标准库；测试用 `python3 -m unittest`。
- 提交信息格式：`type: 一句话`，正文用 `-` 列改动。

## Review Focus

1. **标题含全角标点或斜杠**：`title` 里有 `：`、`/`、空格时，Hugo 生成的路径必须仍等于手写的 slug，不能把标题塞进 URL。→ Task 5 测试 `test_slug_from_filename_not_title`。
2. **正文含 Hugo 短代码语法**：代码块里出现 `{{<` 或 `{{%` 时必须原样输出，不能被当 shortcode 执行。→ Task 6 测试 `test_code_block_with_shortcode_syntax_is_escaped`。
3. **引用的图不存在**：`{{< fig "diagrams/missing.svg" >}}` 必须让构建失败并指出文件名，而不是产出空 figure。→ Task 3 测试 `test_fig_shortcode_fails_on_missing_svg`。
4. **tag 大小写与近义拼写**：`OpenCode`、`opencode `（尾随空格）必须被判为违规而不是静默规整。→ Task 2 测试 `test_tag_case_and_whitespace_rejected`。
5. **上传前远端多出文章**：远端有本地没有的 slug 时必须中止，不能 `--delete` 掉别人的新文章。→ Task 9 测试 `test_aborts_when_remote_has_unknown_post`。

---

### Task 1: 仓库脚手架与 Hugo 构建跑通

**Files:**
- Create: `blog/hugo.toml`
- Create: `blog/.gitignore`
- Create: `blog/README.md`
- Create: `blog/tests/__init__.py`

**Interfaces:**
- Consumes: 无
- Produces: 可用的 `hugo build` 环境；`hugo.toml` 中的 `baseURL`、`uglyURLs`、`[taxonomies] tag = "tags"`、`[outputFormats.RSS] baseName = "rss"` 供后续所有任务依赖。

- [ ] **Step 1: 安装 Hugo 并确认版本**

Run: `brew install hugo && hugo version`
Expected: 输出 `hugo v0.169.0` 或更高

- [ ] **Step 2: 写 `hugo.toml`**

```toml
baseURL = "https://www.jasoncai.site/"
languageCode = "zh-cn"
title = "Jason Cai"
uglyURLs = true
enableRobotsTXT = true
disableKinds = ["section"]

[taxonomies]
  tag = "tags"

[outputs]
  home = ["HTML", "RSS"]

[outputFormats.RSS]
  baseName = "rss"

[params]
  description = "笔记与记录"
  author = "Jason Cai"
  github = "https://github.com/jasoncai0"

[markup.goldmark.renderer]
  unsafe = true
```

- [ ] **Step 3: 写 `.gitignore`**

```
public/
resources/
.hugo_build.lock
__pycache__/
*.pyc
.tmp/
```

- [ ] **Step 4: 建最小内容与首页，确认能构建**

```bash
mkdir -p content/posts layouts
printf -- '---\ntitle: "构建自检"\ndate: 2026-11-09\ntags: ["meta"]\n---\n\n自检正文。\n' > content/posts/_smoke.md
printf '<html><body>{{ .Content }}</body></html>\n' > layouts/single.html
hugo --minify --cleanDestinationDir
```

Expected: 退出码 0，`public/posts/_smoke.html` 存在

- [ ] **Step 5: 清理自检产物并提交**

```bash
rm content/posts/_smoke.md layouts/single.html
git init -b main
git add -A && git commit -m "chore: 初始化 Hugo 仓库与站点配置

- hugo.toml 固定 baseURL/uglyURLs/taxonomy/rss 输出
- .gitignore 排除构建产物"
```

---

### Task 2: Tag 词表与门禁脚本

**Files:**
- Create: `blog/data/tags.yaml`
- Create: `blog/scripts/check-tags.py`
- Create: `blog/tests/test_check_tags.py`
- Create: `blog/tests/fixtures/tags.yaml`

**Interfaces:**
- Consumes: `hugo.toml` 的 taxonomy 名（tag → /tags/）
- Produces: `check_tags(posts_dir: Path, vocab_path: Path) -> list[str]`（返回人类可读的违规列表，空列表表示通过）；CLI `python3 scripts/check-tags.py <posts_dir> <vocab_path>`，违规时退出码 1。Task 9 的 publish.sh 依赖退出码。

- [ ] **Step 1: 写失败测试 `tests/test_check_tags.py`**

```python
import tempfile, textwrap, unittest
from pathlib import Path
import importlib.util

spec = importlib.util.spec_from_file_location(
    "check_tags", Path(__file__).parent.parent / "scripts" / "check-tags.py")
check_tags = importlib.util.module_from_spec(spec); spec.loader.exec_module(check_tags)

VOCAB = "meta: {name: 随笔}\nagent: {name: Agent}\n"

def write(tmp: Path, name: str, front: str) -> None:
    (tmp / name).write_text(f"---\n{front}\n---\n正文\n", encoding="utf-8")

class CheckTagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "vocab.yaml").write_text(VOCAB, encoding="utf-8")
        self.posts = self.tmp / "posts"; self.posts.mkdir()

    def test_missing_tags_rejected(self):
        write(self.posts, "a.md", 'title: "A"\ndate: 2026-11-09')
        self.assertTrue(check_tags.check_tags(self.posts, self.tmp / "vocab.yaml"))

    def test_unknown_tag_rejected(self):
        write(self.posts, "a.md", 'title: "A"\ntags: ["nope"]')
        self.assertTrue(check_tags.check_tags(self.posts, self.tmp / "vocab.yaml"))

    def test_more_than_four_rejected(self):
        write(self.posts, "a.md", 'title: "A"\ntags: ["meta","agent","meta","agent","meta"]')
        self.assertTrue(check_tags.check_tags(self.posts, self.tmp / "vocab.yaml"))

    def test_case_and_whitespace_rejected(self):
        write(self.posts, "a.md", 'title: "A"\ntags: ["Meta"]')
        self.assertTrue(check_tags.check_tags(self.posts, self.tmp / "vocab.yaml"))
        write(self.posts, "a.md", 'title: "A"\ntags: [" meta "]')
        self.assertTrue(check_tags.check_tags(self.posts, self.tmp / "vocab.yaml"))

    def test_valid_post_passes(self):
        write(self.posts, "a.md", 'title: "A"\ntags: ["meta", "agent"]')
        self.assertEqual(check_tags.check_tags(self.posts, self.tmp / "vocab.yaml"), [])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_check_tags -v`
Expected: FAIL，`FileNotFoundError` 或 `AttributeError: check_tags`

- [ ] **Step 3: 写 `data/tags.yaml`（12 个 key，逐字照抄 Global Constraints）**

```yaml
opencode:
  name: OpenCode
  description: OpenCode 的架构、机制与源码拆解
architecture:
  name: 架构
  description: 系统结构与设计取舍
concurrency:
  name: 并发
  description: 并发、取消与执行模型
context-engineering:
  name: 上下文工程
  description: 上下文构建、预算与压缩
multi-agent:
  name: 多智能体
  description: 委派、子会话与协作结构
codegen:
  name: 代码生成
  description: 写代码、改文件与工具调用
event-sourcing:
  name: 事件溯源
  description: 事件目录、投影与订阅
billing:
  name: 计费
  description: 用量、结算与对账
collaborative-editing:
  name: 协同编辑
  description: OT、CRDT 与并发合并
planning:
  name: 任务规划
  description: 计划、任务分解与执行编排
agent:
  name: Agent
  description: Agent 与计算机系统的类比与演进
meta:
  name: 建站与随笔
  description: 站点本身、工具与写作
```

- [ ] **Step 4: 写 `scripts/check-tags.py`**

实现要点（函数签名见 Interfaces）：
- 用 `yaml` 解析 front matter 不可用（标准库无 YAML）→ 只解析 `tags: [...]` 行与 `- ` 块列表两种形态；词表文件按「缩进两空格的 `key:` 起始行」正则提取 key，不引入 YAML 库。
- 逐条违规拼成 `f"{fname}: {reason}"`。
- 空目录或零篇文章视为违规（防止目录指错时静默通过）。
- `main()` 取 `sys.argv[1:3]`，违规时 `print` 到 stderr 并 `sys.exit(1)`。

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m unittest tests.test_check_tags -v`
Expected: 5 tests PASS

- [ ] **Step 6: 提交**

```bash
git add data/tags.yaml scripts/check-tags.py tests/
git commit -m "feat: 增加 tag 词表与构建前门禁

- data/tags.yaml 为唯一权威词表（12 个 key）
- check-tags.py 拦截缺失/未知/超量/大小写错误"
```

---

### Task 3: 文章页模板 + 内联图 shortcode + 纸感 CSS

**Files:**
- Create: `blog/layouts/_default/baseof.html`
- Create: `blog/layouts/_default/single.html`
- Create: `blog/layouts/partials/head.html`
- Create: `blog/layouts/partials/tag-chips.html`
- Create: `blog/layouts/shortcodes/fig.html`
- Create: `blog/assets/css/site.css`
- Create: `blog/tests/test_fig_shortcode.py`

**Interfaces:**
- Consumes: `hugo.toml`
- Produces: shortcode `fig`，签名 `{{< fig "diagrams/x.svg" "图注" >}}`；partial `tag-chips`，入参 `.Params.tags` 与 `.Site.Data.tags`；CSS 类名 `.tag-chip`、`.post-tags`、`.tag-cloud`（Task 4 复用）。

- [ ] **Step 1: 写失败测试 `tests/test_fig_shortcode.py`，覆盖缺失 SVG 必须构建失败**

```python
import subprocess, tempfile, unittest
from pathlib import Path

class FigShortcodeTest(unittest.TestCase):
    def test_fig_shortcode_fails_on_missing_svg(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "content" / "posts").mkdir(parents=True)
            (site / "layouts").mkdir()
            (site / "hugo.toml").write_text(
                'baseURL = "https://example.com/"\nuglyURLs = true\n')
            (site / "layouts" / "single.html").write_text("{{ .Content }}")
            (site / "content" / "posts" / "a.md").write_text(
                '---\ntitle: "A"\ntags: ["meta"]\n---\n\n{{< fig "diagrams/nope.svg" "缺图" >}}\n',
                encoding="utf-8")
            result = subprocess.run(["hugo", "--source", str(site)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("nope.svg", result.stderr)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_fig_shortcode -v`
Expected: FAIL（当前无 shortcode，构建成功或不报该文件）

- [ ] **Step 3: 写 `layouts/shortcodes/fig.html`**

要点：`.Get 0` 为相对 page bundle 的路径，`.Get 1` 为图注；用 `resources.Get` 读取，读不到就 `errorf "figure not found: %s" $src`；用 `.Page.Scratch.Add "fig" 1` 计数编号 `图 N`；输出 `<figure id="fig-N"><div class="diagram">SVG</div><figcaption><span class="fig-no">图 N</span>图注</figcaption></figure>`。SVG 文本用 `replaceRE` 去掉 `<?xml ...?>`。

- [ ] **Step 4: 写模板与 CSS**

`baseof.html`：`<html lang="zh-CN">` + `partial "head" .` + `<body><main class="page">{{ block "main" . }}{{ end }}</main></body>`。
`head.html`：抄现有 index.html 的 meta/og/canonical/rss link，`<link rel="stylesheet" href="{{ "css/site.css" | relURL }}">`，样式由 `resources.Get "css/site.css" | minify | fingerprint` 输出。
`tag-chips.html`：遍历 `.Params.tags`，`index .Site.Data.tags .` 取 `name`，输出 `<a class="tag-chip" href="{{ "/tags/" | relURL }}{{ . }}.html">{{ name }}</a>`。
`single.html`：`<p class="back">` → `<h1>{{ .Title }}</h1>` → `<p class="post-meta">{{ .Date.Format "2006-01-02" }}</p>` + tag-chips → TOC（`.TableOfContents`）→ `{{ .Content }}` → footer。
`site.css`：把 `opencode-multiagent/build.py` 的 `CSS` 常量整段搬入，追加 `.tag-chip`（小号、下划线、hover 变色）、`.post-tags`、`.tag-cloud`、`.post-meta`；删掉 `.toc a::after` 之类无用规则可忽略。

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m unittest tests.test_fig_shortcode -v`
Expected: PASS

- [ ] **Step 6: 用一篇真实文章手工冒烟**

```bash
mkdir -p content/posts/_smoke/diagrams
printf -- '---\ntitle: "冒烟"\ndate: 2026-11-09\ntags: ["meta"]\n---\n\n## 小节\n\n{{< fig "diagrams/a.svg" "示意图" >}}\n' > content/posts/_smoke/index.md
printf '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>' > content/posts/_smoke/diagrams/a.svg
hugo --minify --cleanDestinationDir
grep -c 'class="diagram"' public/posts/_smoke.html   # 期望 1
grep -c 'tag-chip' public/posts/_smoke.html          # 期望 ≥1
rm -rf content/posts/_smoke
```

- [ ] **Step 7: 提交**

```bash
git add layouts/ assets/ tests/test_fig_shortcode.py
git commit -m "feat: 文章页模板与内联图 shortcode

- fig shortcode 缺图即构建失败
- 复用纸感 CSS，新增 tag chip 样式"
```

---

### Task 4: 首页 / tag 索引 / 归档页 / RSS / 404

**Files:**
- Create: `blog/layouts/index.html`
- Create: `blog/layouts/_default/terms.html`
- Create: `blog/layouts/_default/taxonomy.html`
- Create: `blog/layouts/_default/rss.xml`
- Create: `blog/layouts/404.html`
- Modify: `blog/hugo.toml`（若 Task 1 的 `disableKinds` 误伤 taxonomy，此处修正为 `disableKinds = ["section"]` 并保留 taxonomy）

**Interfaces:**
- Consumes: Task 3 的 `tag-chips` partial 与 CSS 类
- Produces: `/tags/index.html`、`/tags/<term>.html`、`/rss.xml`；Task 8/9 的验收依赖这些路径

- [ ] **Step 1: 写 `terms.html`（tag 云）与 `taxonomy.html`（单 tag 归档）**

`terms.html`：遍历 `.Data.Terms.Alphabetical`（或 `.ByCount`），每条输出 tag 名（经 `Site.Data.tags` 映射中文）+ 文章数。`taxonomy.html`：标题显示中文名，列表复用首页的 `ul.posts` 结构，每条含日期、标题、summary。

- [ ] **Step 2: 写 `index.html`**

保留现有首页三段结构：`<header>`（h1 + tagline + GitHub 链接）、tag 云、`<ul class="posts">`（`range .Site.RegularPages.ByDate.Reverse`，每条 `time` + 标题链接 + summary + tag chips）、`<footer>`。

- [ ] **Step 3: 写 `rss.xml`**

`<rss version="2.0">` + `<channel>`（title/link/description/language），每条含 `<title><link><guid><pubDate><description>` 与 `<category>`（遍历 tags，输出中文名）。`pubDate` 用 `.Date.Format "Mon, 02 Jan 2006 15:04:05 -0700"`。

- [ ] **Step 4: 写 `404.html`**

沿用纸感样式，一句「页面不存在」+ 返回首页链接。

- [ ] **Step 5: 冒烟验证**

```bash
hugo --minify --cleanDestinationDir
test -f public/tags/index.html && echo "tags index ok"
test -f public/rss.xml && echo "rss ok"
test -f public/404.html && echo "404 ok"
grep -c "<category>" public/rss.xml   # 期望 == 文章数
```

- [ ] **Step 6: 提交**

```bash
git add layouts/ hugo.toml
git commit -m "feat: 首页、tag 索引与归档、RSS、404 模板

- RSS 每条带 category，与 front matter 同源"
```

---

### Task 5: 有源 10 篇的迁移脚本

**Files:**
- Create: `blog/scripts/migrate_sourced.py`
- Create: `blog/scripts/blockstats.py`
- Create: `blog/tests/test_migrate.py`
- Create: `blog/tests/test_blockstats.py`
- Create: `blog/content/posts/*/index.md`（10 篇产物，page bundle 形式）
- Create: `blog/content/posts/*/diagrams/*.svg`（图从原目录 copy）
- Create: `blog/docs/baseline/local.json`（迁移前的块数量基线）

**Interfaces:**
- Consumes: Task 2 的词表；原目录 `~/Documents/deepseek-harness/default-workspace/opencode-*/`
- Produces: `migrate_one(src_md: Path, out_dir: Path, meta: dict) -> Path`，`meta` 键为 `slug/title/date/summary/tags`；CLI `python3 scripts/migrate_sourced.py --posts-root <workspace> --out content/posts`；`blockstats(path: Path) -> dict[str,int]`（键 `h1/h2/h3/table/figure/pre/hr`），CLI `python3 scripts/blockstats.py <file>` 打印 JSON —— Task 6 与 Task 8 都复用它，所以在此任务先建。

- [ ] **Step 1: 写失败测试 `tests/test_migrate.py`**

```python
def test_slug_from_filename_not_title(self):
    """标题里的全角冒号与斜杠不得进入 URL。"""
    src = self.tmp / "opencode-plan.md"
    src.write_text("# 规划：A/B 与 C\n\n正文\n", encoding="utf-8")
    out = migrate_one(src, self.out, meta={"slug": "opencode-plan", "title": "规划：A/B 与 C",
                                           "date": "2026-11-09", "summary": "s", "tags": ["opencode"]})
    self.assertEqual(out.name, "opencode-plan.md")
    self.assertIn('slug: "opencode-plan"', out.read_text(encoding="utf-8"))

def test_h1_removed_and_fig_converted(self):
    src = self.tmp / "x.md"
    src.write_text('# 标题\n\n![委派](diagrams/01.svg)\n', encoding="utf-8")
    out = migrate_one(src, self.out, meta={...})
    text = out.read_text(encoding="utf-8")
    self.assertNotIn("\n# 标题", text)
    self.assertIn('{{< fig "diagrams/01.svg" "委派" >}}', text)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_migrate -v` → FAIL（无 `migrate_one`）

- [ ] **Step 3: 实现 `migrate_sourced.py`**

要点：front matter 用固定字段顺序手写 YAML（`title/date/summary/description/tags/series`），字符串统一双引号并转义内部双引号；`slug` 显式写入 front matter 保证 Hugo 用它而非标题；删除首个 `# ` 行；`![alt](path)` 整行替换为 `{{< fig "path" "alt" >}}`；把 `diagrams/` 目录整体 copy 到 `content/posts/<slug>/diagrams/` 并把文章改为 page bundle（`<slug>/index.md`）。**决定：统一用 page bundle 形式**（`content/posts/<slug>/index.md`），因为 `[[<slug>.md]]` 与图目录同名会冲突；同时 `slug` 字段保证 URL 仍是 `/posts/<slug>.html`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest tests.test_migrate -v` → PASS

- [ ] **Step 5: 先冻结基线（在改动任何内容之前）**

```bash
mkdir -p docs/baseline
python3 - <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path.home() / "Documents/deepseek-harness/default-workspace"
out = {}
for html in root.glob("opencode-*/*.html"):
    out[html.stem] = json.loads(subprocess.check_output(
        [sys.executable, "scripts/blockstats.py", str(html)]))
pathlib.Path("docs/baseline/local.json").write_text(json.dumps(out, indent=1))
print(f"baseline for {len(out)} posts")
PY
```

Expected: 打印 `baseline for 10 posts`（多出来的是未发布的实验目录，核对后剔除）

- [ ] **Step 6: 迁移 10 篇并核对块数量**

```bash
python3 scripts/migrate_sourced.py --posts-root ~/Documents/deepseek-harness/default-workspace --out content/posts
python3 scripts/check-tags.py content/posts data/tags.yaml && echo "tags ok"
hugo --minify --cleanDestinationDir
python3 - <<'PY'
import json, pathlib, subprocess, sys
base = json.loads(pathlib.Path("docs/baseline/local.json").read_text())
bad = 0
for slug, want in base.items():
    got = json.loads(subprocess.check_output(
        [sys.executable, "scripts/blockstats.py", f"public/posts/{slug}.html"]))
    for k in want:
        if want[k] != got.get(k):
            print(f"MISMATCH {slug}.{k}: want {want[k]} got {got.get(k)}"); bad += 1
print("OK" if not bad else f"{bad} mismatches")
PY
```

Expected: `OK`；出现 MISMATCH 就回到 Markdown 修，直到相等。

- [ ] **Step 7: 提交**

```bash
git add content/posts scripts/migrate_sourced.py scripts/blockstats.py tests/ docs/baseline/local.json
git commit -m "feat: 迁移 10 篇有源文章到 Hugo

- 统一 page bundle，slug 字段固定 URL
- 图引用转 fig shortcode
- 迁移前冻结块数量基线"
```

---

### Task 6: 无源 4 篇的反推

**Files:**
- Create: `blog/scripts/reverse-convert.py`
- Create: `blog/tests/test_reverse_convert.py`
- Create: `blog/tests/fixtures/legacy_sample.html`
- Create: `blog/content/posts/{hello,agent-vs-computer,ot-crdt-lww,opencode-interrupt}/index.md`

**Interfaces:**
- Consumes: 线上 `/var/www/blog/posts/*.html`
- Produces: `html_to_md(html: str) -> tuple[str, list[tuple[str, str]]]`（返回 Markdown 正文与 `(diagram 相对路径, SVG 文本)` 列表）；CLI `python3 scripts/reverse-convert.py --in a.html --out-dir content/posts/<slug> --slug <slug> --meta meta.json`

- [ ] **Step 1: 写失败测试（含短代码转义用例）**

```python
def test_code_block_with_shortcode_syntax_is_escaped(self):
    html = "<pre><code>{{&lt; fig &quot;a.svg&quot; &gt;}}</code></pre>"
    md, _ = html_to_md(html)
    self.assertIn("{{< fig", md)
    self.assertIn("```", md)   # 必须在 fenced code 内，Hugo 才不会执行

def test_figure_inlined_svg_becomes_shortcode(self):
    html = '<figure id="fig-1"><div class="diagram"><svg>X</svg></div><figcaption><span class="fig-no">图 1</span>说明</figcaption></figure>'
    md, diagrams = html_to_md(html)
    self.assertEqual(len(diagrams), 1)
    self.assertIn('{{< fig "diagrams/01.svg" "说明" >}}', md)
```

- [ ] **Step 2: 跑测试确认失败** → Run: `python3 -m unittest tests.test_reverse_convert -v` → FAIL

- [ ] **Step 3: 实现 `reverse-convert.py`**

要点：用 `html.parser.HTMLParser` 遍历，按 `h1/h2/h3/p/blockquote/table/pre/figure/ul/ol/hr` 映射回 Markdown；表格还原成 `| a | b |` + 对齐行；`pre>code` 还原为 fenced code 并保留 `data-lang`；`figure` 里的 `<svg>` 抽到 `diagrams/NN.svg` 并输出 `{{< fig ... >}}`；`<img src="/agent-vs-computer/x.svg">` 读本地同名文件（先用 `scp` 拉到 `.tmp/legacy-assets/`）内联成 figure。无法识别的块报错退出，不静默降级。

- [ ] **Step 4: 跑测试确认通过** → PASS

- [ ] **Step 5: 从线上拉取并转换 4 篇**

```bash
mkdir -p .tmp/legacy-assets
# 4 篇 HTML + agent-vs-computer 的 7 张外链 SVG（后者挂在站根，不在 posts/ 下）
/tmp/.sshtmp/rsh 'tar -C /var/www/blog/posts -cf - hello.html agent-vs-computer.html ot-crdt-lww.html opencode-interrupt.html' > .tmp/legacy.tar
tar -C .tmp/legacy -xf .tmp/legacy.tar
/tmp/.sshtmp/rsh 'tar -C /var/www -cf - agent-vs-computer' > .tmp/assets.tar
tar -C .tmp/legacy-assets -xf .tmp/assets.tar
ls .tmp/legacy-assets/agent-vs-computer/*.svg | wc -l   # 期望 7
for s in hello agent-vs-computer ot-crdt-lww opencode-interrupt; do
  python3 scripts/reverse-convert.py --in ".tmp/legacy/$s.html" --out-dir "content/posts/$s" \
    --slug "$s" --meta ".tmp/legacy/$s.json" --assets .tmp/legacy-assets
done
```

- [ ] **Step 6: 逐篇核对块数量与线上 HTML 相等**

```bash
for s in hello agent-vs-computer ot-crdt-lww opencode-interrupt; do
  echo "== $s"; python3 scripts/blockstats.py ".tmp/legacy/$s.html"
  python3 scripts/blockstats.py "public/posts/$s.html"
done
```

Expected: 每篇两组 JSON 逐项相等；不等则手工修 Markdown 直到相等，并在提交信息里记下修了哪几处。

- [ ] **Step 7: 提交**

```bash
git add content/posts scripts/reverse-convert.py tests/
git commit -m "feat: 从线上 HTML 反推 4 篇无源文章

- 内联 SVG 还原为 page bundle
- agent-vs-computer 外链图收编并内联"
```

---

### Task 7: hello 正文改写与全站 tag 复核

**Files:**
- Modify: `blog/content/posts/hello/index.md`
- Modify: `blog/docs/superpowers/specs/2026-11-09-hugo-tag-migration-design.md`（若 tag 分配有变）

**Interfaces:**
- Consumes: Task 5/6 的产物
- Produces: 14 篇全部带合法 tags

- [ ] **Step 1: 重写 hello 正文**

把「Hugo、Hexo、Astro 都试过……一个 Python 脚本就够了」一段替换为迁移后的真实说法：自研脚本解决了构建，但在 tag 索引、归档页、RSS category 上要自己造轮子，因此迁到 Hugo，tag 由 `data/tags.yaml` 统一约束。保留原文章的架构描述（nginx + 静态文件 + Let's Encrypt）与目录结构。

- [ ] **Step 2: 全站 tag 门禁复核**

Run: `python3 scripts/check-tags.py content/posts data/tags.yaml; echo "exit=$?"`
Expected: `exit=0`，且输出里无孤儿 tag 警告（12 个 key 都被用到）

- [ ] **Step 3: 核对每篇 tag 与设计表一致**

Run: `python3 scripts/dump-tags.py content/posts`（若需，随本任务新增 8 行脚本打印 `slug -> tags`）
Expected: 14 行，与设计文档 §7.8 的表格逐行一致

- [ ] **Step 4: 提交**

```bash
git add content/posts/hello/index.md
git commit -m "docs: 改写建站文章以反映 Hugo 迁移

- 移除与新架构矛盾的『不用框架』结论"
```

---

### Task 8: 结构等价性与链接校验脚本

**Files:**
- Create: `blog/scripts/verify-site.py`
- Create: `blog/tests/test_verify.py`

**Interfaces:**
- Consumes: `public/`；Task 5 的 `blockstats` 与 `docs/baseline/local.json`
- Produces: `verify_site(public: Path, baseline: dict, base_url: str | None) -> list[str]`（空列表表示通过）；CLI `python3 scripts/verify-site.py --public public [--base-url URL]`，有问题时退出码 1

- [ ] **Step 1: 写失败测试**

`baseline` 的形状与 `docs/baseline/local.json` 相同：`{slug: {h2: int, ...}}`。

```python
def test_detects_missing_tag_page(self):
    # public/ 里没有 tags/ 目录
    errs = verify_site(self.public, baseline={"a": {"h2": 1}}, base_url=None)
    self.assertTrue(any("tags" in e for e in errs))

def test_detects_post_count_mismatch(self):
    # public/posts 只有 a.html，基线多一篇 b
    errs = verify_site(self.public, baseline={"a": {"h2": 1}, "b": {"h2": 2}}, base_url=None)
    self.assertTrue(any("b" in e for e in errs))
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现 `verify-site.py`**

检查项：`public/posts/*.html` 数量 == `docs/baseline/local.json` 的条目数（即 14，不硬编码）；`public/tags/*.html` 数量 == `data/tags.yaml` 的 key 数 + 1（index）；`public/rss.xml` 的 `<item>` 数 == 文章数且每条含 `<category>`；`public/sitemap.xml` 存在；给定 `--base-url` 时按 sitemap 逐个发 HEAD 断言 200。基线由 `blockstats` 复用，本任务不重写它。

- [ ] **Step 4: 跑测试确认通过** → PASS

- [ ] **Step 5: 全站校验**

```bash
hugo --minify --cleanDestinationDir
python3 scripts/verify-site.py --public public --base-url https://www.jasoncai.site | tee /tmp/verify.txt
grep -c "FAIL" /tmp/verify.txt   # 期望 0
```

- [ ] **Step 6: 提交**

```bash
git add scripts/verify-site.py tests/test_verify.py
git commit -m "feat: 站点完整性校验

- 文章/tag/rss/sitemap 计数与线上可达性"
```

---

### Task 9: publish.sh（锁、备份、两段式上传、回滚）

**Files:**
- Create: `blog/scripts/publish.sh`
- Create: `blog/tests/test_publish.sh`

**Interfaces:**
- Consumes: `check-tags.py`、`verify-site.py`、`built public/`
- Produces: `scripts/publish.sh [--dry-run] [--final]`；退出码 0 成功，非 0 失败并保证远端未被破坏

- [ ] **Step 1: 写失败测试 `tests/test_publish.sh`**

用本地目录模拟远端（`REMOTE_ROOT=/tmp/fake-www`），断言：
- 缺 tag 时脚本非零退出，`REMOTE_ROOT` 未被修改；
- 正常发布后 `REMOTE_ROOT/posts/*.html` 存在；
- 并发第二次执行因锁存在而立即失败。

- [ ] **Step 2: 跑测试确认失败** → `bash tests/test_publish.sh` → FAIL

- [ ] **Step 3: 实现 `publish.sh`**

顺序严格照设计 §7.7：`git pull --rebase` → `check-tags.py` → `mkdir /tmp/blog-publish.lock`（`trap` 里 `rmdir`）→ `hugo --minify --cleanDestinationDir` → `verify-site.py` → 远端备份到 `~/backups/blog-<ts>/` → `rsync -a`（`--final` 时加 `--delete`）→ 线上 curl 断言 → 任一步失败执行回滚并 `exit 1`。上传前比对远端 `posts/` 的 slug 集合，发现本地缺失即中止（Review Focus 第 5 条）。

- [ ] **Step 4: 跑测试确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/publish.sh tests/test_publish.sh
git commit -m "feat: 一条命令发布，含锁、备份与回滚

- macOS 用 mkdir 锁
- 远端多出文章时中止，保护并发发布"
```

---

### Task 10: 发布技能与文档改写

**Files:**
- Modify: `~/.dsh/AGENTS.md`（「发文章」段）
- Modify: `~/.dsh/hosts/62.234.122.22.md`（§6、§7）
- Create: `blog/README.md`
- Create: `blog/archetypes/post.md`

**Interfaces:**
- Consumes: Task 9 的 CLI 行为
- Produces: 可被后续会话直接照做的发布说明

- [ ] **Step 1: 写 `archetypes/post.md`**

```markdown
---
title: "{{ replace .Name "-" " " | title }}"
date: {{ .Date }}
summary: ""
description: ""
tags: []
---

```

（空 `tags` 会被门禁拦下，这是设计意图。）

- [ ] **Step 2: 写 `README.md`**

一页说明：目录结构、`hugo server` 预览、`publish.sh` 用法、tag 规则、回滚命令。

- [ ] **Step 3: 改写 `~/.dsh/AGENTS.md` 的发布段**

替换为设计 §7.9 的 5 条，并保留原有「先重取线上再合并」的精神改成「发布前 `git pull --rebase`」。

- [ ] **Step 4: 更新 host 笔记 §6/§7**

`/var/www/blog` 说明改为「Hugo 产物，勿手改」；运维命令加 `hugo`、`publish.sh`、`verify-site.py`。

- [ ] **Step 5: 验证文档中的命令都能跑**

Run: `rg -n "publish.sh|check-tags|hugo new" ~/.dsh/AGENTS.md blog/README.md`
Expected: 每条命令在仓库里真实存在且可执行

- [ ] **Step 6: 提交**

```bash
git add README.md archetypes/
git commit -m "docs: 发布技能改写为 Hugo 流程

- 明确 tag 门禁与回滚步骤"
```

---

### Task 11: 上线切换（两段式）

**Files:**
- 无新增；运行 `scripts/publish.sh`

**Interfaces:**
- Consumes: 全部前置任务
- Produces: 线上站切换为 Hugo 产物

- [ ] **Step 1: 建 GitHub 私有仓库并推送**

```bash
export GH_CONFIG_DIR=$HOME/Documents/deepseek-harness/default-workspace/.tools/ghconfig
gh repo create jasoncai0/blog --private --source . --remote origin --push
```

Expected: 仓库私有，`origin` 指向 `github.com/jasoncai0/blog`

- [ ] **Step 2: 第一段上传（不带 --delete）**

Run: `./scripts/publish.sh --dry-run` 先演练；确认无误后 `./scripts/publish.sh`
Expected: 线上 14 篇文章、`/tags/`、`/rss.xml` 全部 200

- [ ] **Step 3: 线上逐项验收**

```bash
for u in / /posts/hello.html /posts/opencode-deep-dive.html /tags/index.html \
         /tags/opencode.html /rss.xml /sitemap.xml; do
  printf "%-34s %s\n" "$u" "$(curl -s -o /dev/null -w '%{http_code}' "https://www.jasoncai.site$u")"
done
```

Expected: 全 200；`/rss.xml` 含 14 个 `<item>` 与 `<category>`

- [ ] **Step 4: 第二段上传（带 --delete）收尾**

Run: `./scripts/publish.sh --final`
Expected: `/style.css` 与站根 `/agent-vs-computer/` 被清除，站点无 404 新增

- [ ] **Step 5: 回归抽查 3 篇视觉**

人工在浏览器打开 `opencode-deep-dive`（最长）、`hello`（最短、已改写）、`agent-vs-computer`（外链图转内联），确认段落、表格、图完整，tag chip 可点进归档页。

- [ ] **Step 6: 记录上线结果**

```bash
git commit --allow-empty -m "chore: 线上切换完成

- 14 篇 + 12 个 tag 页 + rss 全量验证通过"
```
