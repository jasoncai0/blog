# 阅读进度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 文章页显示阅读百分比进度条与常驻胶囊，把每篇的最大进度存进 localStorage，首页显示「62%／已读完」，再次打开时回到上次位置。

**Architecture:** 纯逻辑放进 `assets/js/progress-core.mjs`（无 DOM、可被 Node 直接单测），DOM 装配放进 `assets/js/reading-progress.js`，由 Hugo `js.Build` 打包。文章页与首页在模板里加 `data-progress-post="<slug>"` 钩子，进度 DOM 全部由 JS 注入，无 JS 时页面不受影响。

**Tech Stack:** Hugo 0.169（js.Build/esbuild）、原生 ES modules、Node 24 内置 `node:test`、Python 3.13 + Playwright 做浏览器级验证。

**Spec:** `docs/superpowers/specs/2026-11-10-reading-progress-design.md`

## Global Constraints

- 不引入任何第三方运行时依赖；前端只有两个自托管文件，经 `resources.Get ... | js.Build` 打包。
- 存储键固定为 `jasoncai:read:v1`；单条结构 `{ "p": 0-100 整数, "t": 毫秒时间戳, "d": 布尔 }`。
- 常量：读完阈值 `DONE_THRESHOLD = 95`、续读下限 `RESUME_MIN = 5`、条目上限 `MAX_ENTRIES = 300`、过期 `MAX_AGE_MS = 180 天`。
- 颜色只用已有 token：进度条 `--accent`（#bf4520），文字 `--muted`，等宽数字 `--mono`。
- 所有读写 localStorage 必须包 try/catch，失败时静默降级为「无进度」，不得抛出到控制台以外的行为。
- 现有行为不变：`verify-site.py`、`check-tags.py`、发布流程照旧通过。

## Review Focus

- **localStorage 被禁用或写满**（Safari 隐私模式、配额满）：页面必须照常阅读，进度条显示实时百分比但不落盘，控制台无未捕获异常。
- **极短文章/一屏放得下**：进度应为 100%，不能停在 0% 或算出负数。
- **浏览器前进后退**（bfcache `pageshow.persisted`）：不得与浏览器自身的滚动恢复打架，出现二次跳动。
- **带 `#` 锚点的链接**：锚点定位优先，不得被续读覆盖。
- **多标签页并发写同一键**：写之前重新读取并合并，后写的标签页不能抹掉另一篇的记录。

---

### Task 1: 纯逻辑核心模块

**Files:**
- Create: `assets/js/progress-core.mjs`
- Test: `scripts/tests/progress-core.test.mjs`

**Interfaces:**
- Produces（后续任务只依赖这些名字与签名）：
  - `STORAGE_KEY: string`、`DONE_THRESHOLD: 95`、`RESUME_MIN: 5`、`MAX_ENTRIES: 300`、`MAX_AGE_MS: number`
  - `progressFromScroll(scrollY, viewportHeight, contentTop, contentHeight) -> number`（0–100 整数）
  - `scrollForProgress(percent, viewportHeight, contentTop, contentHeight) -> number`（≥0 的像素值）
  - `isDone(percent) -> boolean`
  - `formatMark(entry) -> string`（`"读完"` / `"62%"` / 无记录时 `""`）
  - `readEntry(store, slug) -> {p,t,d} | null`
  - `writeEntry(store, slug, percent, now) -> store`（取最大值、`d` 只增不减、修剪）
  - `parseStore(text) -> store`、`serializeStore(store) -> string`
  - `shouldResume(entry) -> boolean`

- [ ] **Step 1: 写失败的测试**

```js
// scripts/tests/progress-core.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import * as c from "../../assets/js/progress-core.mjs";

test("progressFromScroll 把视口底部映射到 0–100", () => {
  assert.equal(c.progressFromScroll(0, 800, 300, 2000), 25);
  assert.equal(c.progressFromScroll(1500, 800, 300, 2000), 100); // 越界截断
  assert.equal(c.progressFromScroll(0, 800, 500, 2000), 15);     // 顶部留白不计
  assert.equal(c.progressFromScroll(-200, 800, 300, 2000), 0);   // 负数截断
});

test("一屏放得下时直接算 100", () => {
  assert.equal(c.progressFromScroll(0, 800, 300, 400), 100);
});

test("scrollForProgress 是 progressFromScroll 的反函数", () => {
  assert.equal(c.scrollForProgress(25, 800, 300, 2000), 0);
  assert.equal(c.scrollForProgress(50, 800, 300, 2000), 500);
  assert.equal(c.scrollForProgress(5, 800, 300, 2000), -300); // 交由调用方 clamp
});

test("isDone 与 formatMark", () => {
  assert.equal(c.isDone(94), false);
  assert.equal(c.isDone(95), true);
  assert.equal(c.formatMark({ p: 62, d: false }), "62%");
  assert.equal(c.formatMark({ p: 96, d: true }), "读完");
  assert.equal(c.formatMark(null), "");
});

test("writeEntry 取最大进度且 d 只增不减", () => {
  let s = c.writeEntry({}, "a", 70, 1000);
  s = c.writeEntry(s, "a", 40, 2000);
  assert.deepEqual(s.a, { p: 70, t: 2000, d: false });
  s = c.writeEntry(s, "a", 96, 3000);
  assert.deepEqual(s.a, { p: 96, t: 3000, d: true });
  s = c.writeEntry(s, "a", 10, 4000);
  assert.deepEqual(s.a, { p: 96, t: 4000, d: true });
});

test("writeEntry 修剪过期与超量条目，且保留其他篇", () => {
  const old = 1000;
  const now = old + c.MAX_AGE_MS + 1;
  let s = { stale: { p: 50, t: old, d: false }, keep: { p: 10, t: now - 1000, d: false } };
  s = c.writeEntry(s, "new", 30, now);
  assert.equal(s.stale, undefined);
  assert.ok(s.keep && s.new);
});

test("条目超过 300 时丢最旧的", () => {
  let s = {};
  for (let i = 0; i < 301; i++) s = c.writeEntry(s, `p${i}`, 10, 1000 + i);
  assert.equal(Object.keys(s).length, 300);
  assert.equal(s.p0, undefined);
  assert.ok(s.p300);
});

test("parseStore 容忍坏数据", () => {
  assert.deepEqual(c.parseStore("not json"), {});
  assert.deepEqual(c.parseStore("[]"), {});
  assert.deepEqual(c.parseStore('{"a":{"p":-5,"t":"x"}}'), {});
  assert.deepEqual(c.parseStore('{"a":{"p":62,"t":1,"d":false}}').a.p, 62);
  assert.equal(c.serializeStore({ a: { p: 1, t: 1, d: false } }), '{"a":{"p":1,"t":1,"d":false}}');
});

test("shouldResume 只在 5–94 之间为真", () => {
  assert.equal(c.shouldResume({ p: 4, d: false }), false);
  assert.equal(c.shouldResume({ p: 5, d: false }), true);
  assert.equal(c.shouldResume({ p: 94, d: false }), true);
  assert.equal(c.shouldResume({ p: 95, d: true }), false);
  assert.equal(c.shouldResume(null), false);
});
```

- [ ] **Step 2: 运行并确认失败**

Run: `node --test scripts/tests/progress-core.test.mjs`
Expected: FAIL，`Cannot find module .../progress-core.mjs`

- [ ] **Step 3: 实现 `assets/js/progress-core.mjs`**

按 Interfaces 导出。要点：`progressFromScroll` 用 `(scrollY + viewportHeight - contentTop) / contentHeight * 100` 后 `Math.round` 并 `Math.min(100, Math.max(0, ...))`，`contentHeight <= viewportHeight` 时直接返回 100；`writeEntry` 先读旧值取 `Math.max`，`d = 旧 d || percent >= DONE_THRESHOLD`，再按 `t` 升序修剪（先丢超过 `MAX_AGE_MS` 的，再丢到 `MAX_ENTRIES` 以内）。

- [ ] **Step 4: 运行并确认通过**

Run: `node --test scripts/tests/progress-core.test.mjs`
Expected: PASS，9 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add assets/js/progress-core.mjs scripts/tests/progress-core.test.mjs
git commit -m "feat(reading): pure progress math and store pruning"
```

---

### Task 2: 文章页进度条 + 胶囊 + 落盘

**Files:**
- Create: `assets/js/reading-progress.js`
- Create: `scripts/tests/test_reading_progress.py`
- Modify: `layouts/_default/single.html`
- Modify: `layouts/partials/head.html`

**Interfaces:**
- Consumes: Task 1 的全部导出。
- Produces: 页面上的 DOM 契约 —— `.rp-bar`（含 `.rp-bar-fill`）、`.rp-chip`、`[data-progress-post]` 容器、`.post-body` 正文块（`single.html` 加，用于量高度）。

- [ ] **Step 1: 写失败的浏览器测试**

`scripts/tests/test_reading_progress.py`：一个 `main()`，用 `subprocess` 起 `python3 -m http.server 8765 --directory public`，`playwright.sync_api.sync_playwright()` 起 chromium，先写 `test_article_scroll_records_progress(page, base)`：

```python
page.goto(base + "/posts/hello.html")
page.wait_for_selector(".rp-bar")
assert page.eval_on_selector(".rp-chip", "e => e.textContent").strip() in {"0%", "1%", "2%"}
page.mouse.wheel(0, 100000)
page.wait_for_timeout(1200)   # 等 1s 落盘节流
store = page.evaluate("localStorage.getItem('jasoncai:read:v1')")
assert store is not None and '"p":100' in store.replace(" ", "")
assert page.eval_on_selector(".rp-chip", "e => e.textContent").strip() == "读完"
```

- [ ] **Step 2: 运行并确认失败**

Run: `hugo --minify && python3 scripts/tests/test_reading_progress.py`
Expected: FAIL，等待 `.rp-bar` 超时

- [ ] **Step 3: 实现模板钩子与运行时**

- `single.html`：把 `{{ .Content }}` 包进 `<div class="post-body" data-progress-post="{{ .File.ContentBaseName }}">`；`data-progress-post` **只加在这一个元素上**，运行时用 `.post-body[data-progress-post]` 同时拿到 slug 和量高元素。
- `head.html`：文章页与首页才输出 `{{ $js := resources.Get "js/reading-progress.js" | js.Build (dict "minify" true) }}<script type="module" src="{{ $js.RelPermalink }}" defer></script>`；判断用 `.IsPage` 或 `.IsHome`。
- `reading-progress.js`：顶部立刻 `history.scrollRestoration = "manual"`；`boot()` 找 `[data-progress-post]` 与 `.post-body`，注入 `.rp-bar`（内含 `.rp-bar-fill`）与 `.rp-chip`；`scroll` 监听走 `requestAnimationFrame`，更新填充宽度、胶囊文字（`isDone(p) ? "读完" : p + "%"`，`p <= 2` 时胶囊加 `hidden`）；落盘节流 1s，并在 `pagehide`/`visibilitychange` 时 flush；写盘前重新 `parseStore(localStorage.getItem(...))` 合并，别用内存里的旧快照。

- [ ] **Step 4: 写多标签页合并测试并确认通过**

在同一文件加 `test_two_pages_merge(context, base)`：同一 context 开两个 page，分别打开 `/posts/hello.html` 与 `/posts/ot-crdt-lww.html` 各滚到底，等各自落盘；再读第一个 page 的 `localStorage`，断言键里同时有 `hello` 和 `ot-crdt-lww` 两个条目。

Run: `hugo --minify && python3 scripts/tests/test_reading_progress.py`
Expected: PASS（5 个用例）

- [ ] **Step 5: 提交**

```bash
git add assets/js/reading-progress.js scripts/tests/test_reading_progress.py layouts/_default/single.html layouts/partials/head.html
git commit -m "feat(reading): progress bar and percent chip on posts"
```

---

### Task 3: 断点续读与护栏

**Files:**
- Modify: `assets/js/reading-progress.js`
- Modify: `scripts/tests/test_reading_progress.py`

**Interfaces:**
- Consumes: `shouldResume`、`scrollForProgress`、`readEntry`、`formatMark`。

- [ ] **Step 1: 写三个失败测试**

同一测试文件追加：
1. `test_resume_scrolls_and_hints`：`context.add_init_script` 预置 `localStorage["jasoncai:read:v1"] = '{"hello":{"p":40,"t":0,"d":false}}'`，打开 `/posts/hello.html`，等待 800ms，断言 `window.scrollY > 200` 且 `.rp-hint` 文本含 `从 40% 继续`。
2. `test_anchor_wins_over_resume`：同一预置，打开 `/posts/hello.html` 加文中真实存在的标题锚点（先 `grep -o 'id="[^"]*"' public/posts/hello.html` 取一个），断言 `.rp-hint` 不存在。
3. `test_bfcache_does_not_double_scroll`：打开 `/posts/hello.html` 滚到中部，跳首页，`page.go_back()`；断言回来后 `.rp-hint` 不存在且 `window.scrollY` 与离开前相差不超过 50px（浏览器自己恢复，脚本不插手）。

- [ ] **Step 2: 运行并确认失败**

Run: `hugo --minify && python3 scripts/tests/test_reading_progress.py`
Expected: FAIL（`scrollY` 为 0，或 `.rp-hint` 不存在）

- [ ] **Step 3: 实现续读**

`load` 后执行：若 `location.hash` 非空、或 `scrollY !== 0`、或 `event.persisted`，则不续读；否则 `shouldResume(entry)` 为真时，用 `scrollForProgress` 算出目标，临时 `document.documentElement.style.scrollBehavior = "auto"`，`window.scrollTo(0, Math.max(0, y))`，恢复样式，插入 `.rp-hint`（`从 {p}% 继续`），2500ms 后加 `is-out` 类并在过渡结束后移除。

- [ ] **Step 4: 运行并确认通过**

Run: `hugo --minify && python3 scripts/tests/test_reading_progress.py`
Expected: PASS（全部用例）

- [ ] **Step 5: 提交**

```bash
git add assets/js/reading-progress.js scripts/tests/test_reading_progress.py
git commit -m "feat(reading): resume last position with hint"
```

---

### Task 4: 首页进度标记

**Files:**
- Modify: `layouts/index.html`
- Modify: `assets/js/reading-progress.js`
- Modify: `scripts/tests/test_reading_progress.py`

**Interfaces:**
- Consumes: `readEntry`、`formatMark`。

- [ ] **Step 1: 写失败测试**

`test_home_marks`：预置 `{"hello":{"p":62,"t":0,"d":false},"ot-crdt-lww":{"p":100,"t":0,"d":true}}`，打开 `/`，断言 `li[data-progress-post="hello"] .read-mark` 文本为 `62%`，`li[data-progress-post="ot-crdt-lww"] .read-mark` 为 `读完`，且 `li[data-progress-post="hello"] .read-mark` 在页面可见。

- [ ] **Step 2: 运行并确认失败**

Run: `hugo --minify && python3 scripts/tests/test_reading_progress.py`
Expected: FAIL（`.read-mark` 不存在）

- [ ] **Step 3: 实现**

`index.html` 的 `<li>` 加 `data-progress-post="{{ .File.ContentBaseName }}"`，在 tag chips 之后加 `<span class="read-mark"></span>`；`reading-progress.js` 在非文章页分支里遍历 `[data-progress-post]`，用 `formatMark` 填文本，空串时移除该 span。

- [ ] **Step 4: 运行并确认通过**

Run: `hugo --minify && python3 scripts/tests/test_reading_progress.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add layouts/index.html assets/js/reading-progress.js scripts/tests/test_reading_progress.py
git commit -m "feat(reading): show per-post progress on homepage"
```

---

### Task 5: 样式、无 JS 降级与文档

**Files:**
- Modify: `assets/css/site.css`
- Modify: `README.md`
- Modify: `~/.dsh/hosts/blog-jasoncai.site.md`

- [ ] **Step 1: 写两个回归测试**

同一测试文件追加：
1. `test_no_js_page_intact`：`new_context(java_script_enabled=False)` 打开 `/posts/hello.html`，断言正文首段文本存在、且页面里没有 `.rp-bar`。
2. `test_storage_failure_is_silent`：`add_init_script` 里用 `Object.defineProperty(window, "localStorage", { get() { throw new Error("denied"); } })` 覆盖存储，打开文章页并滚到底，断言页面无未捕获错误（收集 `page.on("pageerror")`）、正文仍在、胶囊文本形如百分比但不报错。

- [ ] **Step 2: 运行并确认现状**

Run: `hugo --minify && python3 scripts/tests/test_reading_progress.py`
Expected: `test_no_js_page_intact` PASS（回归护栏）；`test_storage_failure_is_silent` 此时 FAIL —— 运行时还没接住抛错。

- [ ] **Step 3: 写样式并补文档**

`.rp-bar`：`position: fixed; inset: 0 0 auto; height: 2px; z-index: 50; background: transparent;`，`.rp-bar-fill` 用 `background: var(--accent); width: 0;`。`.rp-chip`：左下或右下固定，`font-family: var(--mono); font-size: 11px; color: var(--muted); background: rgba(245,245,245,.9); border: 1px solid var(--rule); border-radius: 999px; padding: 2px 8px;`，`[hidden]{display:none}`。`.rp-hint`：与胶囊同侧、在上方 40px，`opacity` 过渡 300ms，`.is-out { opacity: 0 }`。`.read-mark`：`margin-left: 8px; font-family: var(--mono); font-size: 11px; color: var(--soft);`。同时把运行时所有 `localStorage` 读写收进 `safeGet()` / `safeSet()`（内部 try/catch，失败返回 `null`/静默），这是 `test_storage_failure_is_silent` 的修复步骤。。README 与 runbook 各加一节「阅读进度」：存哪个键、怎么清空（`localStorage.removeItem('jasoncai:read:v1')`）、只在本机。

- [ ] **Step 4: 全量验证**

```bash
node --test scripts/tests/progress-core.test.mjs
python3 scripts/check-tags.py content/posts data/tags.yaml
hugo --minify && python3 scripts/verify-site.py --public public
python3 scripts/tests/test_reading_progress.py
```
Expected: 全部 PASS；`verify-site` 基线仍是当前篇数。

- [ ] **Step 5: 提交并发布**

```bash
git add assets/css/site.css README.md
git commit -m "feat(reading): progress styles and docs"
bash scripts/publish.sh --final
```

线上复验：`curl -s https://www.jasoncai.site/posts/hello.html | grep -c 'reading-progress'` 为 1；浏览器打开一篇文章，滚到底后回首页应看到该篇标记。
