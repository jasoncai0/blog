# 划词高亮与批注 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 读者能在文章里划词高亮、写批注，数据存在浏览器本地，有笔记的文章在首页标出条数。

**Architecture:** 纯静态站点上就地加三层——`notes-core.mjs` 管本地存储与笔记增删改（无 DOM，node 可测）；`notes.js` 管选区锚定、`<mark>` 包裹、工具条/气泡、左栏笔记列表；Hugo 模板只提供挂载点（`data-progress-post` 已有 slug）。首页由同一个 JS 读存储填「有笔记 ·N」。

**Tech Stack:** Hugo 0.169、原生 ES module（`js.Build`）、`localStorage`、Playwright（Python）+ `node --test`。

**Spec:** `docs/superpowers/specs/2026-11-12-notes-highlight-design.md`

## Global Constraints

- 存储键固定 `jasoncai:notes:v1`；每条笔记 `{id, q, pre, post, block, t, thread:[{author,text,t}]}`，`thread[0].author` 恒为 `"我"`。
- 锚点 `pre`/`post` 各 32 字（`ANCHOR_PAD = 32`）。
- 只处理 `.post-body` 内的连续选区；跨块拒绝；`pre/code/figcaption/svg` 内不可划。
- 无 JS、`localStorage` 抛异常时页面必须照常可读，不出现 pageerror。
- 样式只用现有 token（`--paper/--ink/--muted/--soft/--accent/--rule/--mono`），字号 11–13px。
- 与阅读进度、目录、图放大共存；不得改动 `jasoncai:read:v1`。

## Review Focus

- 同一句话在文章里出现两次：必须标中用户选的那次，不能标第一个匹配。
- 选区跨过 `<code>`/`<strong>`/`<a>`：每一段都要包上，点任意一段打开同一条笔记。
- 文章改字后锚点失配：静默跳过，不能把高亮画到别的句子上。
- `localStorage` 写入超限（QuotaExceededError）：不崩，提示一句。
- 移动端点选（无 hover）：工具条仍要能出现，且不能盖住选区。

---

### Task 1: 笔记存储核心

**Files:**
- Create: `assets/js/notes-core.mjs`
- Test: `scripts/tests/notes-core.test.mjs`

**Interfaces:**
- Consumes: 无
- Produces: `NOTES_KEY = "jasoncai:notes:v1"`、`ANCHOR_PAD = 32`、`newId(now) -> string`、`makeNote({q, pre, post, block, t, text, author}) -> Note`、`readNotes(store) -> object`、`writeNotes(store, all) -> boolean`、`notesFor(all, slug) -> Note[]`、`addNote(all, slug, note) -> object`、`setThreadText(all, slug, id, text, t) -> object`、`removeNote(all, slug, id) -> object`、`countFor(all, slug) -> number`、`parseStore(raw) -> object`、`serializeStore(all) -> string`

- [ ] **Step 1: 写失败测试**

```js
// scripts/tests/notes-core.test.mjs
test("addNote 追加并按 slug 分开存", () => {
  const n = makeNote({ q: "甲", pre: "", post: "", block: 3, t: 1000, text: "批注", author: "我" });
  const all = addNote({}, "a", n);
  assert.equal(notesFor(all, "a").length, 1);
  assert.equal(notesFor(all, "a")[0].thread[0].author, "我");
  assert.deepEqual(notesFor(all, "b"), []);
});
test("setThreadText 只改 thread[0].text，不动作者与时间", () => {});
test("removeNote 移除指定 id，其他笔记不动", () => {});
test("parseStore 丢掉坏条目但保留好条目", () => {
  const all = parseStore('{"a":[{"id":"x","q":"甲","thread":[]},{"id":"y"}],"b":5}');
  assert.equal(notesFor(all, "a").length, 1);
  assert.deepEqual(notesFor(all, "b"), []);
});
test("writeNotes 在 store 抛 QuotaExceededError 时返回 false 且不抛", () => {});
test("countFor 统计条数，空 slug 返回 0", () => {});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --test scripts/tests/notes-core.test.mjs`
Expected: FAIL — `Cannot find module '../assets/js/notes-core.mjs'`

- [ ] **Step 3: 实现 `assets/js/notes-core.mjs`**

校验规则：笔记必须有非空 `id`、字符串 `q`、数组 `thread`；`thread` 每项要有 `author`/`text`；否则整条丢弃。`writeNotes` 内部 `try/catch` 包 `setItem`，失败返回 `false`。`serializeStore` 重建对象避免写入外来字段。

- [ ] **Step 4: 跑测试确认通过**

Run: `node --test scripts/tests/notes-core.test.mjs`
Expected: PASS，6 个用例

- [ ] **Step 5: 提交**

```bash
git add assets/js/notes-core.mjs scripts/tests/notes-core.test.mjs
git commit -m "feat(notes): local storage core for highlights and annotations"
```

---

### Task 2: 选区锚定与高亮渲染

**Files:**
- Create: `assets/js/notes.js`
- Modify: `layouts/partials/head.html`（`.IsPage` 时加载 `notes.js`）
- Test: `scripts/tests/test_notes.py`

**Interfaces:**
- Consumes: Task 1 全部导出
- Produces: `anchorOf(range, body) -> {q, pre, post, block} | null`、`locate(anchor, body) -> Array<{node, start, end}>`、`paint(body, anchor, id) -> number`（返回包裹的文本节点数，0 表示失配）、`initNotes()`

- [ ] **Step 1: 写失败测试**

```python
def test_highlight_persists_across_reload(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    select_text(page, "LWW 不是与 OT、CRDT 并列的第三种算法")
    page.click(".nt-actions button[data-action='highlight']")
    page.wait_for_selector("mark.note-hl")
    page.reload(); page.wait_for_timeout(400)
    assert page.locator("mark.note-hl").count() >= 1
    assert page.evaluate("() => JSON.parse(localStorage['jasoncai:notes:v1'])")["posts/ot-crdt-lww"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 scripts/tests/test_notes.py`
Expected: FAIL — 找不到 `mark.note-hl`（`notes.js` 还不存在）

- [ ] **Step 3: 实现锚定与渲染**

`anchorOf`：从 `range.startContainer` 向上找 `.post-body` 的直接子元素，下标即 `block`；块内文本用 `Range` 取前后各 32 字；选区跨块返回 `null`。
`locate`：块内 `indexOf(q)` 全部候选，校验 `pre`/`post` 前缀后缀，多候选时取 `block` 命中的那个，否则取唯一一个；无匹配返回 `[]`。
`paint`：对每个候选建 `Range`，用 TreeWalker 收集文本节点，逐节点 `splitText` 后包 `mark.note-hl[data-note]`；`code/pre/figcaption` 内跳过。
初始化：读 `body.dataset.progressPost` 作 slug，`readNotes` 后逐条 `paint`，失配记 `console.debug` 不抛。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 scripts/tests/test_notes.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add assets/js/notes.js layouts/partials/head.html scripts/tests/test_notes.py
git commit -m "feat(notes): anchor selected text and paint highlights"
```

---

### Task 3: 选中工具条

**Files:**
- Modify: `assets/js/notes.js`
- Test: `scripts/tests/test_notes.py`

**Interfaces:**
- Consumes: Task 2 的 `anchorOf`
- Produces: `.nt-actions`（`button[data-action="highlight"]`、`button[data-action="annotate"]`）、`showToolbar(rect)`、`hideToolbar()`

- [ ] **Step 1: 写失败测试**

```python
def test_toolbar_appears_above_selection(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    select_text(page, "收敛是必要条件")
    bar = page.locator(".nt-actions")
    bar.wait_for(state="visible")
    assert bar.locator("button[data-action='highlight']").is_visible()
    assert bar.locator("button[data-action='annotate']").is_visible()
    box, sel = bar.bounding_box(), page.evaluate("() => { const r = getSelection().getRangeAt(0).getBoundingClientRect(); return {y: r.top}; }")
    assert box["y"] + box["height"] <= sel["y"] + 2

def test_toolbar_hides_when_selection_cleared(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    select_text(page, "收敛是必要条件")
    page.locator(".nt-actions").wait_for(state="visible")
    page.evaluate("() => getSelection().removeAllRanges()")
    page.wait_for_timeout(300)
    assert page.locator(".nt-actions").count() == 0 or page.locator(".nt-actions").is_hidden()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 scripts/tests/test_notes.py -k toolbar`（脚本无 -k，则直接跑全部并看这两条 FAIL）

- [ ] **Step 3: 实现工具条**

`selectionchange` 防抖 120ms + `mouseup`；选区非空、单块、不在禁用标签内才显示；`position: absolute` 挂 `document.body`，`left` 取选区中心，`top` 取 `rect.top - height - 8`，越界翻到下方；滚动即隐藏。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 scripts/tests/test_notes.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add assets/js/notes.js scripts/tests/test_notes.py
git commit -m "feat(notes): selection toolbar"
```

---

### Task 4: 批注气泡（增删改）

**Files:**
- Modify: `assets/js/notes.js`
- Test: `scripts/tests/test_notes.py`

**Interfaces:**
- Consumes: Task 1 `setThreadText`/`removeNote`、Task 2 `paint`
- Produces: `.nt-pop`（`textarea.nt-input`、`button[data-act="save"]`、`button[data-act="delete"]`）、`openPop(id, rect)`、`closePop()`

- [ ] **Step 1: 写失败测试**

```python
def test_annotate_then_edit_and_delete(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    select_text(page, "收敛是必要条件")
    page.click(".nt-actions button[data-action='annotate']")
    page.fill(".nt-pop textarea", "这里要补一个反例")
    page.click(".nt-pop button[data-act='save']")
    page.click("mark.note-hl")
    pop = page.locator(".nt-pop")
    pop.wait_for(state="visible")
    assert "我" in pop.inner_text()
    assert "这里要补一个反例" in pop.locator("textarea").input_value()
    page.fill(".nt-pop textarea", "改成：补两个反例")
    page.click(".nt-pop button[data-act='save']")
    page.reload(); page.wait_for_timeout(400)
    page.click("mark.note-hl")
    assert "改成：补两个反例" in page.locator(".nt-pop textarea").input_value()
    page.click(".nt-pop button[data-act='delete']")
    page.wait_for_timeout(300)
    assert page.locator("mark.note-hl").count() == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 scripts/tests/test_notes.py`

- [ ] **Step 3: 实现气泡**

气泡挂在 `document.body`，`top` 在高亮下方 8px；显示 `thread` 全部条目（作者 + 时间 + 文本，AI 条目只读）；人写的 `thread[0].text` 可编辑；删除整条笔记并移除 mark。Esc / 点空白关闭。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 scripts/tests/test_notes.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add assets/js/notes.js scripts/tests/test_notes.py
git commit -m "feat(notes): annotation bubble with edit and delete"
```

---

### Task 5: 左栏笔记列表与导出

**Files:**
- Modify: `layouts/partials/rail-article.html`（目录底部加 `<div class="note-list" hidden></div>`）
- Modify: `layouts/_default/single.html`（无目录文章在正文后放同一挂载点）
- Modify: `assets/js/notes.js`、`assets/css/site.css`
- Test: `scripts/tests/test_notes.py`

**Interfaces:**
- Consumes: Task 1 `notesFor`、Task 4 `openPop`
- Produces: `.note-list`（`ol.note-items`、`button[data-act="export"]`、`button[data-act="clear"]`）

- [ ] **Step 1: 写失败测试**

```python
def test_note_list_jumps_to_highlight(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    seed_note(page, "posts/ot-crdt-lww", "收敛是必要条件", "批注一")
    page.reload(); page.wait_for_timeout(400)
    assert page.locator(".note-list").is_visible()
    assert "本文笔记 ·1" in page.locator(".note-list .note-head").inner_text()
    page.locator(".note-list .note-items li a").first.click()
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.activeElement.className.includes('note-hl')")

def test_export_produces_json(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    seed_note(page, "posts/ot-crdt-lww", "收敛是必要条件", "批注一")
    page.reload(); page.wait_for_timeout(400)
    with page.expect_download() as dl:
        page.click(".note-list button[data-act='export']")
    body = open(dl.value.path(), encoding="utf-8").read()
    assert "批注一" in body
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现列表与导出**

列表按 `t` 升序；每行「片段（截 28 字）+ 批注首行」；点击滚动到首个 mark 并加 `.is-flash` 1.2s；导出用 `Blob` + `URL.createObjectURL` 触发下载 `notes-<slug>.json`；清空需 `confirm`。

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: 提交**

```bash
git add layouts/partials/rail-article.html layouts/_default/single.html assets/js/notes.js assets/css/site.css scripts/tests/test_notes.py
git commit -m "feat(notes): per-article note list with export"
```

---

### Task 6: 首页「有笔记 ·N」

**Files:**
- Modify: `layouts/index.html`（`.post-foot` 加 `<span class="note-mark"></span>`）
- Modify: `layouts/partials/head.html`（首页也加载 `notes.js`）
- Modify: `assets/js/notes.js`、`assets/css/site.css`
- Test: `scripts/tests/test_notes.py`

**Interfaces:**
- Consumes: Task 1 `countFor`
- Produces: `markHome()`（首页初始化时对每个 `li[data-progress-post]` 填 `.note-mark`）

- [ ] **Step 1: 写失败测试**

```python
def test_home_marks_articles_with_notes(page, base):
    page.goto(base + "/")
    seed_note(page, "posts/ot-crdt-lww", "收敛是必要条件", "批注一")
    page.reload(); page.wait_for_timeout(400)
    mark = page.locator("li[data-progress-post='ot-crdt-lww'] .note-mark")
    assert mark.inner_text().strip() == "有笔记 ·1"
    empty = page.locator("li[data-progress-post='hello'] .note-mark")
    assert empty.count() == 0 or empty.inner_text().strip() == ""
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现首页标记**

复用 `markHome()`；文本 `有笔记 ·N`；`N === 0` 时移除节点；`pageshow.persisted` 时重跑（bfcache 返回）。

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: 提交**

```bash
git add layouts/index.html layouts/partials/head.html assets/js/notes.js assets/css/site.css scripts/tests/test_notes.py
git commit -m "feat(notes): mark articles with notes on the home page"
```

---

### Task 7: 边界：跨节点、失败锚点、存储禁用、无 JS

**Files:**
- Modify: `assets/js/notes.js`
- Test: `scripts/tests/test_notes.py`

**Interfaces:**
- Consumes: 前六个任务的产出
- Produces: 无新导出

- [ ] **Step 1: 写失败测试**

```python
def test_selection_spanning_inline_code_is_wrapped_in_parts(page, base):
    # 选区跨过 <code>：期望多个 mark 共享同一 data-note
def test_anchor_miss_is_silent_and_paints_nothing(page, base):
    # 先写入一条 q 在文中不存在的笔记，加载后 mark 数为 0、无 pageerror
def test_storage_disabled_does_not_break_the_page(page, base):
    # 注入 localStorage getter 抛错：正文可见、无 pageerror
def test_no_js_has_no_toolbar_and_no_marks(ctx, base):
    # java_script_enabled=False：.nt-actions 不存在，正文完整
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 修实现**

跨节点：`paint` 已按文本节点逐段包，测试要确认 `data-note` 一致；失配：`locate` 返回 `[]` 时静默；存储禁用：所有读写走 `try/catch`；无 JS：脚本本就不执行。

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: 提交**

```bash
git add assets/js/notes.js scripts/tests/test_notes.py
git commit -m "test(notes): cover cross-node, stale anchors and disabled storage"
```

---

### Task 8: 文档、全量验证与发布

**Files:**
- Modify: `README.md`、`~/.dsh/hosts/blog-jasoncai.site.md`
- Test: 全量

**Interfaces:**
- Consumes: 全部
- Produces: 上线版本

- [ ] **Step 1: 写文档**

README「笔记」节：怎么用、存在哪、导出、换浏览器会丢；runbook §13：`localStorage` 键、锚定算法、失败表现、怎么手工清数据。

- [ ] **Step 2: 全量验证**

```bash
hugo --minify
node --test "scripts/tests/*.test.mjs"
python3 scripts/tests/test_reading_progress.py
python3 scripts/tests/test_sidebar.py
python3 scripts/tests/test_figure_zoom.py
python3 scripts/tests/test_notes.py
python3 scripts/check-tags.py content/posts data/tags.yaml
python3 scripts/verify-site.py --public public
```
Expected: 全绿

- [ ] **Step 3: 发布并线上复验**

```bash
bash scripts/publish.sh --final
```
线上用 Playwright 划词、高亮、刷新、看首页标记。

- [ ] **Step 4: 提交**

```bash
git add README.md
git commit -m "docs: notes and highlights"
git push origin main
```
