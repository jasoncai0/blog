# 侧栏目录、写作日历与上次读到位置 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给站点加上首页左栏（写作日历 + 标签云）、文章页左栏（可折叠、滚动高亮的目录）和正文里的「上次读到这」标记。

**Architecture:** 首页与文章页共用一套两栏栅格（240px 左栏 + 正文），左栏在桌面 sticky、900px 以下回到文档流。日历由 Hugo 模板服务端渲染整年格子，JS 只负责窗口平移与交互；目录复用 Hugo 的 `.TableOfContents`，JS 只加折叠与滚动高亮；标记复用阅读进度已有的 localStorage 记录。

**Tech Stack:** Hugo 0.169 extended（`js.Build` 打包 ES module）、原生 JS（无第三方库）、CSS Grid、Playwright（Python）+ `node --test`。

**Spec:** `docs/superpowers/specs/2026-11-12-sidebar-toc-calendar-design.md`

## Global Constraints

- 不引入任何第三方前端依赖；JS 只经由 `js.Build` 打包，产物必须 `fingerprint`。
- localStorage 键一律以 `jasoncai:` 开头；本次新增 `jasoncai:toc:collapsed`。
- 界面文案用中文；栏目名分别为「写作日历」「标签」「目录」。
- 颜色只用现有 token：`--paper #f5f5f5`、`--ink #2d3142`、`--muted #4f5d75`、`--soft #5a6580`、`--accent #bf4520`、`--rule rgba(45,49,66,.12)`。
- 页面最大宽度 1240px，左栏 240px，栏间距 56px；栅格在 900px 处切换为单栏堆叠。
- 无 JS 时：标签、日历、目录、正文必须照常可见可点；只允许丢失折叠、高亮、窗口平移。
- 现有阅读进度行为不得回退：顶部进度条、右下角胶囊、自动续读、首页 `.read-mark`。

## Review Focus

- **日期解析的时区**：`new Date("2026-01-01")` 按 UTC 解析，东八区会显示成前一天。所有日期换算必须走本地零点（Task 1 测试直接钉住）。
- **跨年边界**：2026-01-01 是周四，它所在周从 2025-12-29 开始；网格第一列不能把 2025 的日期算成本年（Task 1）。
- **同一天多篇文章**：11-11 有 28 篇，点击该日必须让 28 条一起闪、滚到第一条，而不是只处理一条（Task 4）。
- **没有目录的文章**：现有模板在 `.TableOfContents` 短于 30 字符时不渲染目录；左栏不能留一个空的「目录」框（Task 5）。
- **900px 边界与无 JS**：正好 900/899px 要切换布局；关掉 JS 后左栏内容仍完整（Task 2、Task 8）。

---

### Task 1: 日历的纯逻辑（周对齐、网格、窗口、色阶）

**Files:**
- Create: `assets/js/calendar-core.mjs`
- Test: `scripts/tests/calendar-core.test.mjs`

**Interfaces:**
- Produces（后续任务只依赖这些名字）：
  - `parseIso(s: string) -> Date`（本地零点）
  - `isoOf(d: Date) -> string`（`YYYY-MM-DD`）
  - `alignWeek(date: Date) -> Date`（该周周一，本地零点）
  - `buildGrid(firstIso: string, lastIso: string) -> string[][]`（每列 7 个 ISO 日期，周一→周日）
  - `level(count: number) -> 0|1|2|3|4`（0 篇→0、1→1、2→2、3–4→3、≥5→4）
  - `windowStart(totalWeeks: number, size: number, anchorIndex: number) -> number`
  - `shiftWindow(start: number, delta: number, total: number, size: number) -> number`

- [ ] **Step 1: 写失败的测试**

```js
import test from "node:test";
import assert from "node:assert/strict";
import { parseIso, isoOf, alignWeek, buildGrid, level, windowStart, shiftWindow } from "../../assets/js/calendar-core.mjs";

test("parseIso 取本地零点，不受 UTC 影响", () => {
  const d = parseIso("2026-01-01");
  assert.equal(d.getFullYear(), 2026);
  assert.equal(d.getMonth(), 0);
  assert.equal(d.getDate(), 1);
  assert.equal(d.getHours(), 0);
});

test("alignWeek 把周日算进上一周", () => {
  assert.equal(isoOf(alignWeek(parseIso("2026-11-08"))), "2026-11-02");
  assert.equal(isoOf(alignWeek(parseIso("2026-11-09"))), "2026-11-09");
});

test("buildGrid 跨年时第一周从 2025-12-29 开始", () => {
  const grid = buildGrid("2026-01-01", "2026-01-10");
  assert.equal(grid[0][0], "2025-12-29");
  assert.equal(grid[0].length, 7);
  assert.equal(grid.at(-1).at(-1), "2026-01-11");
});

test("level 分级", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 28].map(level), [0, 1, 2, 3, 3, 4, 4]);
});

test("windowStart 默认贴住最后一列，并在边界夹住", () => {
  assert.equal(windowStart(53, 12, 52), 41);
  assert.equal(windowStart(5, 12, 4), 0);
  assert.equal(windowStart(53, 12, 3), 0);
});

test("shiftWindow 夹在 0..total-size", () => {
  assert.equal(shiftWindow(41, 1, 53, 12), 41);
  assert.equal(shiftWindow(41, -1, 53, 12), 40);
  assert.equal(shiftWindow(0, -1, 53, 12), 0);
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test scripts/tests/calendar-core.test.mjs`
Expected: FAIL — `Cannot find module .../calendar-core.mjs`

- [ ] **Step 3: 实现 `assets/js/calendar-core.mjs`**

`parseIso` 用 `s.split("-").map(Number)` 构造 `new Date(y, m-1, d)`；`alignWeek` 用 `(getDay()+6)%7` 回退天数；`buildGrid` 从 `alignWeek(parseIso(firstIso))` 起逐日推到 `alignWeek(parseIso(lastIso))`；`windowStart` 返回 `Math.min(Math.max(anchorIndex - size + 1, 0), Math.max(total - size, 0))`；`shiftWindow` 用同样区间夹 `start + delta`。

- [ ] **Step 4: 运行测试确认通过**

Run: `node --test scripts/tests/calendar-core.test.mjs`
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add assets/js/calendar-core.mjs scripts/tests/calendar-core.test.mjs
git commit -m "feat(calendar): week alignment, grid building and level bucket helpers"
```

---

### Task 2: 两栏骨架与标签云搬进左栏

**Files:**
- Create: `layouts/partials/rail-home.html`
- Modify: `layouts/index.html`
- Modify: `assets/css/site.css`（`.page` 宽度、`.layout` 栅格、`.rail` 粘住、标签云样式迁移）
- Test: `scripts/tests/test_sidebar.py`（新建，先写「标签在左栏、顶部没有 tag-cloud」两条）

**Interfaces:**
- Produces: 首页 DOM 结构 `<div class="layout"><aside class="rail rail-home">…</aside><div class="main-col">…</div></div>`；`aside.rail-home` 内依次为 `.rail-block[data-block="calendar"]`、`.rail-block[data-block="tags"]`。
- 文章页在 Task 5 复用 `.layout` / `.rail` 同样的类名。

- [ ] **Step 1: 写失败的浏览器测试**

`scripts/tests/test_sidebar.py` 用与 `test_reading_progress.py` 相同的启动方式（随机端口 `http.server --directory public`），新增：

```python
def test_home_tags_live_in_rail(page):
    page.goto(f"{BASE}/")
    aside = page.locator("aside.rail-home")
    assert aside.locator("a.tag-chip").count() == 17
    assert page.locator("header nav.tag-cloud").count() == 0

def test_home_rail_is_left_of_list_on_desktop(page):
    page.goto(f"{BASE}/")
    rail = page.locator("aside.rail-home").bounding_box()
    posts = page.locator("ul.posts").bounding_box()
    assert rail["x"] + rail["width"] <= posts["x"]

def test_home_rail_stacks_below_900(page):
    page.set_viewport_size({"width": 880, "height": 800})
    page.goto(f"{BASE}/")
    rail = page.locator("aside.rail-home").bounding_box()
    posts = page.locator("ul.posts").bounding_box()
    assert rail["y"] < posts["y"]
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: FAIL — `aside.rail-home` 不存在

- [ ] **Step 3: 改模板与样式**

`layouts/partials/rail-home.html`：把 `index.html` 里现有的 `<nav class="tag-cloud">` 整段搬进来，包在 `<div class="rail-block" data-block="tags">` 里，`<nav aria-label="标签">`；上面预留 `<div class="rail-block" data-block="calendar"></div>`（Task 3 填内容）。
`layouts/index.html`：`<main>` 内先 `<div class="layout">`，左 `<aside class="rail rail-home">{{ partial "rail-home.html" . }}</aside>`，右 `<div class="main-col">` 包住 `header` 与 `ul.posts`。
`assets/css/site.css`：`.page { max-width: 1240px; }`；`.layout { display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 56px; align-items: start; }`；`.rail { position: sticky; top: 96px; max-height: calc(100vh - 160px); overflow-y: auto; }`；`@media (max-width: 900px) { .layout { grid-template-columns: 1fr; gap: 32px; } .rail { position: static; max-height: none; } }`；`.tag-cloud` 改为纵向 flex `gap: 8px`。

- [ ] **Step 4: 构建并运行测试**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add layouts/index.html layouts/partials/rail-home.html assets/css/site.css scripts/tests/test_sidebar.py
git commit -m "feat(layout): two-column skeleton and move tag cloud into home rail"
```

---

### Task 3: 服务端渲染写作日历

**Files:**
- Create: `layouts/partials/calendar.html`
- Modify: `layouts/partials/rail-home.html`（把 `data-block="calendar"` 的内容换成 partial 调用）
- Modify: `layouts/index.html`（`li` 加 `data-date`）
- Modify: `assets/css/site.css`（`.heat-*` 色阶与网格）
- Test: `scripts/tests/test_sidebar.py`

**Interfaces:**
- Produces: DOM `nav.calendar[aria-label="写作日历"]` > `.cal-grid` > 每列 `.cal-week[data-week="N"]` > 格子 `.heat-cell[data-date="YYYY-MM-DD"][data-count]`，有文章的是 `<a>`、没文章的是 `<span class="is-empty">`；色阶类 `lv1 lv2 lv3 lv4`；月份标签 `.cal-month`。
- 列表项新增 `li[data-date="YYYY-MM-DD"]`（Task 4 的点击跳转依赖它）。

- [ ] **Step 1: 写失败的测试**

```python
def test_calendar_renders_year_grid(page):
    page.goto(f"{BASE}/")
    weeks = page.locator("nav.calendar .cal-week")
    assert weeks.count() >= 52
    cell = page.locator('a.heat-cell[data-date="2026-11-11"]')
    assert cell.get_attribute("data-count") == "28"
    assert "lv4" in cell.get_attribute("class")

def test_calendar_empty_days_are_not_links(page):
    page.goto(f"{BASE}/")
    assert page.locator('span.heat-cell.is-empty[data-date="2026-03-03"]').count() == 1
    assert page.locator('a.heat-cell[data-date="2026-03-03"]').count() == 0

def test_list_items_carry_dates(page):
    page.goto(f"{BASE}/")
    assert page.locator('ul.posts li[data-date="2026-11-11"]').count() == 28
```

- [ ] **Step 2: 运行确认失败**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: FAIL — `nav.calendar` 不存在

- [ ] **Step 3: 写 `layouts/partials/calendar.html` 并接线**

`calendar.html` 用 Hugo 函数算网格：`$year := now.Year`、`$first := time (printf "%d-01-01" $year)`、`$last := time (printf "%d-12-31" $year)`（不写死年份）；从 1 月 1 日往回退到周一（`$first.Weekday` 的数字，周日 0 → 退 6 天）；逐周迭代：外层 `range seq 0 $weeks`，内层 `seq 0 6`，日期 = 起点 + 天数；每天用 `where $pages "Date.Year" eq $y` 太慢——先在 `index.html` 顶部建一张 `dict`：`{{ $byDay := dict }}`，`range $pages` 时 `{{ $byDay = merge $byDay (dict (dateFormat "2006-01-02" .Date) (add (index $byDay (dateFormat "2006-01-02" .Date) | default 0) 1)) }}`，再 `{{ partial "calendar.html" (dict "byDay" $byDay) }}`。
每格：`$n := index $byDay $iso`；`$n` 为 0 输出 `<span class="heat-cell is-empty" data-date="{{ $iso }}" data-count="0">`，否则 `<a class="heat-cell lv{{ level }}" data-date="{{ $iso }}" data-count="{{ $n }}" href="/#d-{{ $iso }}" aria-label="{{ $iso }}，{{ $n }} 篇">`（级别：1→lv1、2→lv2、3–4→lv3、≥5→lv4，用 if/else 写死，不引入 JS 计算）。列 `data-week` 从 0 递增。每月第一天所在列前输出 `.cal-month`（`dateFormat "1月"`）。
`index.html` 的 `li` 加 `data-date="{{ .Date.Format "2006-01-02" }}"`。
CSS：`.cal-grid { display: grid; grid-auto-flow: column; gap: 3px; }`、`.cal-week { display: grid; grid-template-rows: repeat(7, 10px); gap: 3px; }`、`.heat-cell { width: 10px; height: 10px; border-radius: 2px; background: var(--paper-2); }`、`.lv1 { background: rgba(191,69,32,.25); }` … `.lv4 { background: var(--accent); }`、`.cal-month { font-size: 9px; color: var(--soft); }`。

- [ ] **Step 4: 构建并运行测试**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: 6 passed

- [ ] **Step 5: 截图自查**

Run: 本地起服务后 `python3 - <<` 用 Playwright 截 `/#` 的 900×900 图，确认不是一片空框或格子溢出左栏。
Expected: 左栏里能看到一行行的浅色格子，11 月处有深色格。

- [ ] **Step 6: Commit**

```bash
git add layouts/partials/calendar.html layouts/partials/rail-home.html layouts/index.html assets/css/site.css scripts/tests/test_sidebar.py
git commit -m "feat(calendar): server-render year heat grid in home rail"
```

---

### Task 4: 日历交互（12 周窗口、全年切换、平移、日期跳转、点日跳文章）

**Files:**
- Create: `assets/js/site-nav.js`
- Modify: `assets/js/calendar-core.mjs`（若 Task 1 的导出不足以支撑窗口，只允许补函数，不改已有语义）
- Modify: `layouts/partials/calendar.html`（加控件：`.cal-range` 按钮组、`.cal-prev`/`.cal-next`、`input.cal-jump[type=date]`）
- Modify: `layouts/partials/head.html`（在阅读进度 script 旁按同样方式加载 `site-nav.js`，`js.Build` + `fingerprint`）
- Test: `scripts/tests/test_sidebar.py`

**Interfaces:**
- Consumes: `calendar-core.mjs` 的 `windowStart`、`shiftWindow`、`parseIso`、`isoOf`。
- Produces: 状态类 `.cal-week.is-hidden`（不在窗口内）、`.cal-week.is-target`（日期跳转命中）、`li.is-flash`（点击日期后闪烁）；控件按钮 `[data-cal-range="12"]` / `[data-cal-range="year"]`。

- [ ] **Step 1: 写失败的测试**

```python
def test_only_recent_12_weeks_visible_by_default(page):
    page.goto(f"{BASE}/")
    assert page.locator("nav.calendar .cal-week:not(.is-hidden)").count() == 12

def test_year_toggle_shows_all_weeks(page):
    page.goto(f"{BASE}/")
    page.click('[data-cal-range="year"]')
    assert page.locator("nav.calendar .cal-week:not(.is-hidden)").count() > 50

def test_clicking_a_day_flashes_that_day(page):
    page.goto(f"{BASE}/")
    page.click('[data-cal-range="year"]')
    page.locator('a.heat-cell[data-date="2026-11-11"]').click()
    page.wait_for_selector('ul.posts li[data-date="2026-11-11"].is-flash')
    assert page.locator("ul.posts li.is-flash").count() == 28
    assert page.evaluate("window.scrollY") > 0

def test_date_input_moves_window_and_marks_target(page):
    page.goto(f"{BASE}/")
    page.fill("input.cal-jump", "2026-11-09")
    assert page.locator('a.heat-cell[data-date="2026-11-09"]').is_visible()
    assert page.locator(".cal-week.is-target").count() == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: FAIL — `[data-cal-range]` 不存在

- [ ] **Step 3: 实现控件与 `assets/js/site-nav.js`**

`site-nav.js` 里 `initCalendar()`：读 `.cal-week` 列表、默认 `size=12`、`anchor = total-1`；`render()` 给每列加/去 `.is-hidden`；`‹ ›` 调 `shiftWindow` 并记住 `start`（切换全年时按 3 个月约 13 周平移）；`[data-cal-range]` 切换 `size` 与按钮 `.is-on`；`input.cal-jump` 的 `change`：`parseIso(value)` → 找到 `[data-date]` 所在 `.cal-week` 的 `data-week` → `start = windowStart(total, size, weekIndex)` → 渲染 + 给该列 `.is-target`（2 秒后移除）。整页点击委托：`a.heat-cell` 的 `click` 阻止默认，取 `data-date`，`document.querySelectorAll('ul.posts li[data-date="…"]')` 加 `.is-flash`（1.6 秒后移除），`first.scrollIntoView({block:"center", behavior:"smooth"})`；无匹配则放行默认跳转。
`head.html` 追加与阅读进度并列的 `<script type="module" src="{{ $nav.RelPermalink }}" defer></script>`（`resources.Get "js/site-nav.js" | js.Build (dict "minify" true) | fingerprint`），只在 `.IsHome` 加载。

- [ ] **Step 4: 构建并运行测试**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add assets/js/site-nav.js assets/js/calendar-core.mjs layouts/partials/calendar.html layouts/partials/head.html scripts/tests/test_sidebar.py
git commit -m "feat(calendar): 12-week window, year toggle, date jump and day click"
```

---

### Task 5: 文章页目录左栏与折叠

**Files:**
- Create: `layouts/partials/rail-article.html`
- Modify: `layouts/_default/single.html`
- Modify: `assets/css/site.css`
- Test: `scripts/tests/test_sidebar.py`

**Interfaces:**
- Produces: `<aside class="rail rail-article">` > `nav.toc[data-toc]` > `.toc-head`（标题「目录」+ `button.toc-toggle[aria-expanded]`）> `.toc-body` > `{{ .TableOfContents }}`；折叠态类 `.rail-article.is-collapsed`，持久化键 `jasoncai:toc:collapsed`。
- Consumes: Task 2 的 `.layout` / `.rail` 类。

- [ ] **Step 1: 写失败的测试**

```python
def test_article_toc_in_left_rail(page):
    page.goto(f"{BASE}/posts/ot-crdt-lww.html")
    rail = page.locator("aside.rail-article nav.toc")
    assert rail.count() == 1
    rail_box = page.locator("aside.rail-article").bounding_box()
    body_box = page.locator(".post-body").bounding_box()
    assert rail_box["x"] + rail_box["width"] <= body_box["x"]

def test_toc_collapse_persists(page):
    page.goto(f"{BASE}/posts/ot-crdt-lww.html")
    page.click(".toc-toggle")
    assert page.locator("aside.rail-article.is-collapsed").count() == 1
    page.reload()
    assert page.locator("aside.rail-article.is-collapsed").count() == 1

def test_article_without_toc_has_no_empty_rail(page):
    html = page.goto(f"{BASE}/posts/hello.html")
    assert page.locator("aside.rail-article").count() == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: FAIL — `aside.rail-article` 不存在

- [ ] **Step 3: 改模板与样式**

`rail-article.html`：`{{ with .TableOfContents }}{{ if gt (len .) 30 }}` 才输出整块 aside（没有目录就不渲染，避免空框）；`button.toc-toggle` 文案「收起/展开」。
`single.html`：用 `.layout` 两栏骨架包住原有的返回链接、标题、meta、tags、正文；`nav.toc` 从正文流里删掉。
CSS：`.rail-article { width: 240px; transition: width .18s ease; }`；`.is-collapsed { width: 44px; }`；`.is-collapsed .toc-body, .is-collapsed .toc-head .label { display: none; }`；`.is-collapsed .toc-head` 竖排「目录」两字；`#TableOfContents ul { columns: 1; }`（左栏窄，单列）；900px 以下 `aside.rail-article { width: auto; }`、`.toc-body` 默认折叠。

- [ ] **Step 4: 构建并运行测试**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add layouts/partials/rail-article.html layouts/_default/single.html assets/css/site.css scripts/tests/test_sidebar.py
git commit -m "feat(toc): move article table of contents into collapsible left rail"
```

---

### Task 6: 滚动高亮当前章节

**Files:**
- Modify: `assets/js/site-nav.js`
- Modify: `assets/css/site.css`
- Modify: `layouts/partials/head.html`（文章页也加载 `site-nav.js`）
- Test: `scripts/tests/test_sidebar.py`

**Interfaces:**
- Produces: 当前章节链接 `.is-active`；`initTocSpy()` 观察 `.post-body h2, .post-body h3`。

- [ ] **Step 1: 写失败的测试**

```python
def test_active_section_follows_scroll(page):
    page.goto(f"{BASE}/posts/ot-crdt-lww.html")
    page.locator(".post-body h2").nth(2).scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    active = page.locator("nav.toc a.is-active")
    assert active.count() == 1
    assert active.get_attribute("href").endswith("#" + page.locator(".post-body h2").nth(2).get_attribute("id"))
```

- [ ] **Step 2: 运行确认失败**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: FAIL — `a.is-active` count 0

- [ ] **Step 3: 实现 `initTocSpy()`**

`IntersectionObserver`，`rootMargin: "-88px 0px -70% 0px"`，记录最近进入的 id；`scroll` 兜底：若 `observer` 无命中则取视口上方最后一个标题。给对应 `a[href="#id"]` 加 `.is-active`（先清全部），并在其滚出栏外时 `scrollIntoView({block:"nearest"})`。CSS：`.toc a.is-active { color: var(--accent); border-left: 2px solid var(--accent); padding-left: 6px; }`。文章页加载同一个 `site-nav.js`（`head.html` 条件改为 `.IsHome | or .IsPage`）。

- [ ] **Step 4: 构建并运行测试**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add assets/js/site-nav.js assets/css/site.css layouts/partials/head.html scripts/tests/test_sidebar.py
git commit -m "feat(toc): highlight current section while scrolling"
```

---

### Task 7: 正文里的「上次读到这」标记

**Files:**
- Modify: `assets/js/reading-progress.js`
- Modify: `assets/css/site.css`
- Test: `scripts/tests/test_sidebar.py`

**Interfaces:**
- Consumes: `progress-core.mjs` 的 `readEntry`、`scrollForProgress`、`shouldResume`。
- Produces: `.rp-marker`（`<div aria-hidden="true"><span>上次读到这 · 62%</span></div>`）。

- [ ] **Step 1: 写失败的测试**

```python
def test_marker_shows_where_reading_stopped(page):
    page.goto(f"{BASE}/")  # 写入 62% 记录
    page.evaluate("localStorage.setItem('jasoncai:read:v1', JSON.stringify({'ot-crdt-lww':{p:62,t:Date.now(),d:false}}))")
    page.goto(f"{BASE}/posts/ot-crdt-lww.html")
    page.wait_for_selector(".rp-marker")
    assert "62%" in page.locator(".rp-marker").inner_text()

def test_marker_disappears_after_scrolling_past(page):
    page.evaluate("localStorage.setItem('jasoncai:read:v1', JSON.stringify({'ot-crdt-lww':{p:62,t:Date.now(),d:false}}))")
    page.goto(f"{BASE}/posts/ot-crdt-lww.html")
    page.wait_for_selector(".rp-marker")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(600)
    assert page.locator(".rp-marker").count() == 0

def test_no_marker_when_resume_jumped(page):
    page.evaluate("localStorage.setItem('jasoncai:read:v1', JSON.stringify({'ot-crdt-lww':{p:62,t:Date.now(),d:false}}))")
    page.goto(f"{BASE}/posts/ot-crdt-lww.html")
    page.wait_for_timeout(400)
    assert page.locator(".rp-marker").count() == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: FAIL — `.rp-marker` 不存在

- [ ] **Step 3: 实现标记**

`reading-progress.js` 的 `boot()` 里，在决定续读之后调用 `placeMarker(entry)`：仅当 `entry && entry.p >= 5 && entry.p < 95 && !resumed`；用 `scrollForProgress(entry.p, innerHeight, metrics.top, metrics.height)` 算 y，`document.createElement("div").className = "rp-marker"`，`style.top = y + "px"`（父级 `.post-body` 需 `position: relative`），插入 `.post-body`；`scroll` 回调里若 `marker.getBoundingClientRect().bottom < 0` 则加 `.is-passed`，300ms 后 `remove()`。
CSS：`.post-body { position: relative; }`、`.rp-marker { position: absolute; left: -12px; right: -12px; border-top: 1px dashed var(--rule); transition: opacity .3s; }`、`.rp-marker span { position: absolute; top: -1.4em; left: 0; font-family: var(--mono); font-size: 10px; letter-spacing: .08em; color: var(--soft); background: var(--paper); padding-right: 6px; }`、`.rp-marker.is-passed { opacity: 0; }`。

- [ ] **Step 4: 构建并运行测试**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add assets/js/reading-progress.js assets/css/site.css scripts/tests/test_sidebar.py
git commit -m "feat(reading): show a visible marker where reading stopped"
```

---

### Task 8: 无 JS、移动端核对、文档与发布

**Files:**
- Modify: `README.md`
- Modify: `~/.dsh/hosts/blog-jasoncai.site.md`
- Modify: `scripts/tests/test_sidebar.py`

**Interfaces:** 无新增导出。

- [ ] **Step 1: 补降级测试**

```python
def test_no_js_keeps_rail_content(browser):
    ctx = browser.new_context(java_script_enabled=False)
    page = ctx.new_page(); page.goto(f"{BASE}/")
    assert page.locator("aside.rail-home nav.calendar").count() == 1
    assert page.locator("aside.rail-home a.tag-chip").count() == 17
    assert page.locator("ul.posts li").count() > 50
    ctx.close()

def test_toggle_button_hidden_without_js(browser):
    ctx = browser.new_context(java_script_enabled=False)
    page = ctx.new_page(); page.goto(f"{BASE}/posts/ot-crdt-lww.html")
    assert page.locator(".toc-toggle").count() == 0
    assert page.locator("nav.toc a").count() > 3
    ctx.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `hugo --minify && python3 -m pytest scripts/tests/test_sidebar.py -v`
Expected: FAIL — 折叠按钮在无 JS 时仍渲染

- [ ] **Step 3: 让折叠按钮依赖 JS 出现**

`rail-article.html` 里 `button.toc-toggle` 加 `hidden`，`site-nav.js` 启动时 `toggle.hidden = false`；日历控件 `.cal-range`、`.cal-jump`、`.cal-prev`、`.cal-next` 同样默认 `hidden`，由 JS 显示。

- [ ] **Step 4: 全量验证**

Run: `hugo --minify && node --test "scripts/tests/*.test.mjs" && python3 -m pytest scripts/tests -v && python3 scripts/check-tags.py content/posts data/tags.yaml && python3 scripts/verify-site.py --public public`
Expected: 单测 16、浏览器 19、tag 17、基线 20 全部 OK

- [ ] **Step 5: 更新文档**

README 结构节补 `layouts/partials/{calendar,rail-home,rail-article}.html`、`assets/js/{calendar-core.mjs,site-nav.js}`；「阅读进度」节后加「侧栏」节（日历窗口、目录折叠键、标记行为）；runbook §12 记录同内容与 `jasoncai:toc:collapsed` 键。

- [ ] **Step 6: 发布并线上复验**

Run: `bash scripts/publish.sh --final`
Expected: 上传成功、备份路径打印；随后 `curl -s https://jasoncai.site/ | grep -c 'heat-cell'` > 300、`curl -sI https://jasoncai.site/js/site-nav.<hash>.js` 返回 200。

- [ ] **Step 7: Commit**

```bash
git add README.md scripts/tests/test_sidebar.py layouts/partials/rail-article.html assets/js/site-nav.js
git commit -m "docs(sidebar): document rail behavior and guard controls without JS"
```
