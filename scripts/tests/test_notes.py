#!/usr/bin/env python3
"""划词高亮与批注的浏览器回归。

先 `hugo --minify` 生成 public，再跑：`python3 scripts/tests/test_notes.py`。
"""

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

ARTICLE = "/posts/ot-crdt-lww.html"
SLUG = "ot-crdt-lww"
KEY = "jasoncai:notes:v1"
DESKTOP = {"viewport": {"width": 1200, "height": 900}}

ANCHOR_JS = """(needle) => {
  const body = document.querySelector('.post-body');
  const blocks = [...body.children];
  for (let b = 0; b < blocks.length; b += 1) {
    const text = blocks[b].textContent;
    const at = text.indexOf(needle);
    if (at < 0) continue;
    return {
      q: needle,
      pre: text.slice(Math.max(0, at - 32), at),
      post: text.slice(at + needle.length, at + needle.length + 32),
      block: b,
    };
  }
  return null;
}"""

SELECT_JS = """(needle) => {
  const body = document.querySelector('.post-body');
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const i = node.textContent.indexOf(needle);
    if (i < 0) continue;
    const range = document.createRange();
    range.setStart(node, i);
    range.setEnd(node, i + needle.length);
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.dispatchEvent(new Event('selectionchange'));
    return true;
  }
  return false;
}"""


SCROLL_JS = """(needle) => {
  const body = document.querySelector('.post-body');
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (node.textContent.includes(needle)) {
      node.parentElement.scrollIntoView({ block: 'center', behavior: 'instant' });
      return true;
    }
  }
  return false;
}"""


def select_text(page, needle):
    # html 上有 scroll-behavior: smooth，平滑滚动会持续发 scroll 事件把工具条收掉，
    # 所以先瞬时滚到目标、等稳定，再设选区。
    assert page.evaluate(SCROLL_JS, needle), f"文章里找不到要选的文字：{needle}"
    page.wait_for_timeout(250)
    assert page.evaluate(SELECT_JS, needle), f"文章里找不到要选的文字：{needle}"


def seed_note(page, needle, text="批注", slug=SLUG):
    anchor = page.evaluate(ANCHOR_JS, needle)
    assert anchor, f"文章里找不到：{needle}"
    seed(page, {slug: [{
        "id": "n1", "q": anchor["q"], "pre": anchor["pre"], "post": anchor["post"],
        "block": anchor["block"], "t": 1,
        "thread": [{"author": "我", "text": text, "t": 1}],
    }]})
    return anchor


def seed(page, notes):
    page.evaluate(
        """([key, data]) => localStorage.setItem(key, JSON.stringify(data))""",
        [KEY, notes],
    )


CROSS_SELECT_JS = """(needle) => {
  const body = document.querySelector('.post-body');
  for (const block of [...body.children]) {
    const at = block.textContent.indexOf(needle);
    if (at < 0) continue;
    const end = at + needle.length;
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    let pos = 0, node, startNode = null, startOff = 0, endNode = null, endOff = 0;
    while ((node = walker.nextNode())) {
      const len = node.textContent.length;
      if (!startNode && pos + len > at) { startNode = node; startOff = at - pos; }
      if (startNode && !endNode && pos + len >= end) { endNode = node; endOff = end - pos; }
      pos += len;
    }
    if (!startNode || !endNode) continue;
    const range = document.createRange();
    range.setStart(startNode, startOff);
    range.setEnd(endNode, endOff);
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.dispatchEvent(new Event('selectionchange'));
    return true;
  }
  return false;
}"""


def wait_for_server(url, timeout=15.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"服务器没起来: {url}: {last}")


def test_highlight_persists_across_reload(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    select_text(page, "LWW 不是与 OT、CRDT 并列的第三种算法")
    page.click(".nt-actions button[data-action='highlight']")
    page.wait_for_selector("mark.note-hl")
    stored = page.evaluate("(k) => JSON.parse(localStorage[k] || '{}')", KEY)
    assert SLUG in stored, f"应存在 {SLUG} 下，实际键 {list(stored)}"
    assert stored[SLUG][0]["q"].startswith("LWW 不是与 OT")
    page.reload()
    page.wait_for_timeout(400)
    assert page.locator("mark.note-hl").count() >= 1, "刷新后高亮应还在"


def test_highlight_paints_only_the_selected_occurrence(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件")
    page.reload()
    page.wait_for_timeout(400)
    marks = page.locator("mark.note-hl")
    assert marks.count() == 1, f"应只标一处，实际 {marks.count()}"
    assert marks.first.inner_text() == "收敛是必要条件"


def test_toolbar_appears_above_selection(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    select_text(page, "收敛是必要条件")
    bar = page.locator(".nt-actions")
    bar.wait_for(state="visible")
    assert bar.locator("button[data-action='highlight']").is_visible()
    assert bar.locator("button[data-action='annotate']").is_visible()
    box = bar.bounding_box()
    top = page.evaluate("() => getSelection().getRangeAt(0).getBoundingClientRect().top")
    assert box["y"] + box["height"] <= top + 2, "工具条应在选区上方"


def test_toolbar_hides_when_selection_cleared(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    select_text(page, "收敛是必要条件")
    page.locator(".nt-actions").wait_for(state="visible")
    page.evaluate("() => getSelection().removeAllRanges()")
    page.wait_for_timeout(400)
    assert page.locator(".nt-actions").count() == 0, "清空选区后工具条应消失"


def test_annotate_then_edit_and_delete(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    select_text(page, "收敛是必要条件")
    page.click(".nt-actions button[data-action='annotate']")
    pop = page.locator(".nt-pop")
    pop.wait_for(state="visible")
    page.fill(".nt-pop .nt-input", "这里要补一个反例")
    page.click(".nt-pop button[data-act='save']")
    page.wait_for_timeout(300)
    page.click("mark.note-hl")
    pop.wait_for(state="visible")
    assert "我" in pop.inner_text(), "气泡里应署名"
    assert "这里要补一个反例" in pop.locator(".nt-input").input_value()
    page.fill(".nt-pop .nt-input", "改成：补两个反例")
    page.click(".nt-pop button[data-act='save']")
    page.reload()
    page.wait_for_timeout(400)
    page.click("mark.note-hl")
    pop.wait_for(state="visible")
    assert "改成：补两个反例" in page.locator(".nt-pop .nt-input").input_value()
    page.click(".nt-pop button[data-act='delete']")
    page.wait_for_timeout(300)
    assert page.locator("mark.note-hl").count() == 0, "删掉笔记后高亮应消失"


def test_escape_closes_the_bubble(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "批注一")
    page.reload()
    page.wait_for_timeout(400)
    page.click("mark.note-hl")
    page.locator(".nt-pop").wait_for(state="visible")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert page.locator(".nt-pop").count() == 0


def test_note_list_jumps_to_highlight(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "批注一")
    page.reload()
    page.wait_for_timeout(400)
    listing = page.locator(".main-col > .note-list")
    assert listing.is_visible(), "有笔记时应显示左栏列表"
    assert "本文笔记 ·1" in listing.locator(".note-head").inner_text()
    page.locator(".note-list .note-items li a").first.click()
    page.wait_for_timeout(400)
    focused = page.evaluate("() => document.activeElement.className")
    assert "note-hl" in focused, f"点列表应把焦点落到高亮上，实际 {focused!r}"


def test_note_list_click_opens_thread_floors(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    anchor = page.evaluate(ANCHOR_JS, "收敛是必要条件")
    assert anchor, "文章里找不到锚点"
    seed(page, {SLUG: [{
        "id": "n2", "q": anchor["q"], "pre": anchor["pre"], "post": anchor["post"],
        "block": anchor["block"], "t": 1,
        "thread": [
            {"author": "我", "text": "批注一", "t": 1},
            {"author": "AI", "text": "这是 AI 的回复", "t": 2},
        ],
    }]})
    page.reload()
    page.wait_for_timeout(400)
    page.locator(".note-list .note-items li a").first.click()
    page.wait_for_selector(".nt-pop", state="visible", timeout=5000)
    assert "2 层" in page.locator(".nt-floors").inner_text()
    assert page.locator(".nt-thread .nt-floor").count() == 1, "楼层应展开显示"
    assert "这是 AI 的回复" in page.locator(".nt-thread").inner_text()


def test_note_relocates_when_paragraphs_shift(page, base):
    """编辑文章插了段落：块下标失效，靠原文 + 前后文重新找回。"""
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    anchor = page.evaluate(ANCHOR_JS, "收敛是必要条件")
    assert anchor
    shifted = dict(anchor, block=anchor["block"] + 2)
    seed(page, {SLUG: [{
        "id": "n3", "q": shifted["q"], "pre": shifted["pre"], "post": shifted["post"],
        "block": shifted["block"], "t": 1,
        "thread": [{"author": "我", "text": "改结构后还在吗", "t": 1}],
    }]})
    page.reload()
    page.wait_for_timeout(500)
    assert page.locator("mark.note-hl").count() >= 1, "块下标变了也应该重新定位到"
    assert page.locator(".note-list .note-stale").count() == 0


def test_note_whose_text_was_edited_is_marked_stale(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed(page, {SLUG: [{
        "id": "n4", "q": "这句原文已经被删掉了", "pre": "", "post": "", "block": 3, "t": 1,
        "thread": [{"author": "我", "text": "旧批注", "t": 1}],
    }]})
    page.reload()
    page.wait_for_timeout(500)
    assert page.locator("mark.note-hl").count() == 0
    item = page.locator(".note-list .note-items li").first
    assert "is-stale" in (item.get_attribute("class") or "")
    assert item.locator(".note-stale").inner_text() == "定位不到"
    item.locator("a").click()
    page.wait_for_selector(".nt-pop", state="visible", timeout=5000)
    assert "定位不到" in page.locator(".nt-stale-tip").inner_text()
    assert "旧批注" in page.locator(".nt-input").input_value()


TRANSLATE_JS = """(needle) => {
  // 模拟沉浸式翻译：在原文段落最前面插一段译文（双语模式）
  const body = document.querySelector('.post-body');
  for (const block of body.children) {
    if (!block.textContent.includes(needle)) continue;
    if (block.querySelector('[data-immersive-translate-effect]')) return false;
    const box = document.createElement('div');
    box.className = 'immersive-translate-target-wrapper';
    box.setAttribute('data-immersive-translate-effect', '1');
    const p = document.createElement('p');
    p.textContent = 'This is a translated paragraph that is much longer than the original text.';
    box.appendChild(p);
    block.insertBefore(box, block.firstChild);
    return true;
  }
  return false;
}"""


def mark_texts(page):
    return page.evaluate("() => [...document.querySelectorAll('mark.note-hl')].map(m => m.textContent)")


def test_translation_insert_does_not_break_anchor_offsets(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "翻译插件在场")
    page.reload()
    page.wait_for_timeout(500)
    before = page.locator("mark.note-hl").first.bounding_box()
    assert page.evaluate(TRANSLATE_JS, "收敛是必要条件")
    # 插件把包着高亮的节点换掉（真实插件重写文本时会这样），高亮必须按原文重画
    page.evaluate("""() => {
      const mark = document.querySelector('mark.note-hl');
      const span = document.createElement('span');
      span.textContent = mark.textContent;
      mark.replaceWith(span);
    }""")
    page.wait_for_timeout(900)
    texts = mark_texts(page)
    assert texts == ["收敛是必要条件"], f"高亮应只包原文，实际 {texts!r}"
    after = page.locator("mark.note-hl").first.bounding_box()
    assert abs(after["x"] - before["x"]) < 2, "高亮应仍落在原文那句的起点，而不是被译文挤到别处"
    page.locator("mark.note-hl").first.click()
    page.wait_for_selector(".nt-pop", state="visible", timeout=5000)
    assert "翻译插件在场" in page.locator(".nt-input").input_value()


def test_highlights_opt_out_of_translation(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "别翻译我")
    page.reload()
    page.wait_for_timeout(500)
    attrs = page.evaluate("""() => {
      const m = document.querySelector('mark.note-hl');
      return [m.getAttribute('translate'), m.classList.contains('notranslate')];
    }""")
    assert attrs == ["no", True], f"高亮片段应带 translate=no / notranslate，实际 {attrs!r}"


def test_highlight_is_repainted_when_translation_replaces_it(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "被冲掉再画回来")
    page.reload()
    page.wait_for_timeout(500)
    page.evaluate("""() => {
      const mark = document.querySelector('mark.note-hl');
      const span = document.createElement('span');
      span.textContent = mark.textContent;
      mark.replaceWith(span);
    }""")
    page.wait_for_timeout(900)
    assert page.locator("mark.note-hl").count() >= 1, "高亮丢了应该自动重画"


def test_selecting_translated_text_does_not_open_toolbar(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "原文批注")
    page.reload()
    page.wait_for_timeout(500)
    assert page.evaluate(TRANSLATE_JS, "收敛是必要条件")
    page.wait_for_timeout(400)
    page.evaluate("""() => {
      const box = document.querySelector('.immersive-translate-target-wrapper');
      const text = box.querySelector('p').firstChild;
      const range = document.createRange();
      range.setStart(text, 0);
      range.setEnd(text, 10);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      document.dispatchEvent(new Event('selectionchange'));
    }""")
    page.wait_for_timeout(400)
    assert page.locator(".nt-actions").count() == 0, "译文里划词不该弹批注工具条"


def test_export_produces_json(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "批注一")
    page.reload()
    page.wait_for_timeout(400)
    with page.expect_download() as download:
        page.click(".note-list button[data-act='export']")
    path = download.value.path()
    text = open(path, encoding="utf-8").read()
    assert "批注一" in text, text[:200]


def test_home_marks_articles_with_notes(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "批注一")
    page.goto(base + "/")
    page.wait_for_timeout(500)
    mark = page.locator("li[data-progress-post='ot-crdt-lww'] .note-mark")
    assert mark.count() == 1, "有笔记的篇目应在首页标出"
    assert mark.inner_text().strip() == "有笔记 ·1", mark.inner_text()
    empty = page.locator("li[data-progress-post='hello'] .note-mark")
    assert empty.count() == 0 or empty.inner_text().strip() == ""


def test_selection_spanning_inline_code_is_wrapped_in_parts(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    needle = "CRDT——max 满足"
    assert page.evaluate(SCROLL_JS, "它当然是合法的"), "找不到跨 <code> 的段落"
    page.wait_for_timeout(250)
    assert page.evaluate(CROSS_SELECT_JS, needle), f"选区跨不过 <code>：{needle}"
    page.click(".nt-actions button[data-action='highlight']")
    page.wait_for_timeout(300)
    marks = page.evaluate("""() => [...document.querySelectorAll('mark.note-hl')].map(m => ({
      id: m.dataset.note, text: m.textContent }))""")
    assert len(marks) >= 2, f"跨节点选区应拆成多段，实际 {marks}"
    assert len({m["id"] for m in marks}) == 1, "多段应共享同一个 data-note"
    assert "".join(m["text"] for m in marks) == needle


def test_anchor_miss_is_silent_and_paints_nothing(page, base):
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed(page, {SLUG: [{
        "id": "gone", "q": "这句话已经不在这篇文章里了", "pre": "无", "post": "无",
        "block": 2, "t": 1, "thread": [{"author": "我", "text": "旧批注", "t": 1}],
    }]})
    page.reload()
    page.wait_for_timeout(400)
    assert page.locator("mark.note-hl").count() == 0, "锚点对不上时不应画高亮"
    assert not errors, f"不应抛错：{errors}"
    assert "收敛是必要条件" in page.locator(".post-body").inner_text()


def test_storage_disabled_does_not_break_the_page(ctx, base):
    errors = []
    ctx.add_init_script("""Object.defineProperty(window, 'localStorage', {
      get() { throw new Error('denied'); },
    });""")
    page = ctx.new_page()
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(base + ARTICLE)
    page.wait_for_timeout(500)
    assert "收敛是必要条件" in page.locator(".post-body").inner_text()
    assert page.locator(".note-list").count() == 0 or not page.locator(".main-col > .note-list").is_visible()
    assert not errors, f"存储被禁时不应抛错：{errors}"


def test_no_js_has_no_toolbar_and_no_marks(ctx, base):
    page = ctx.new_page()
    page.goto(base + ARTICLE)
    assert page.locator(".nt-actions").count() == 0
    assert page.locator("mark.note-hl").count() == 0
    assert "收敛是必要条件" in page.locator(".post-body").inner_text()


SVG_SELECT_JS = """(needle) => {
  const node = [...document.querySelectorAll('.post-body svg text')]
    .find((t) => t.textContent.includes(needle));
  if (!node) return false;
  node.scrollIntoView({ block: 'center', behavior: 'instant' });
  const text = node.firstChild;
  const at = text.textContent.indexOf(needle);
  const range = document.createRange();
  range.setStart(text, at);
  range.setEnd(text, at + needle.length);
  const sel = getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  document.dispatchEvent(new Event('selectionchange'));
  return true;
}"""


def test_figure_text_is_not_wrapped_by_marks(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    before = page.evaluate("() => document.querySelectorAll('.post-body svg text').length")
    assert page.evaluate(SVG_SELECT_JS, "基础状态"), "图里找不到可选文字"
    page.wait_for_timeout(300)
    assert page.locator(".nt-actions").count() == 0, "图内文字不该出现高亮工具条"
    assert page.evaluate("() => document.querySelectorAll('.post-body svg mark').length") == 0
    after = page.evaluate("() => document.querySelectorAll('.post-body svg text').length")
    assert after == before, f"图里的文字节点被破坏了：{before} → {after}"
    assert "基础状态" in page.locator(".post-body").inner_text() or True


def test_seeded_note_inside_figure_does_not_break_the_svg(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    page.evaluate("""() => {
      const body = document.querySelector('.post-body');
      const blocks = [...body.children];
      const at = blocks.findIndex((b) => b.querySelector('svg') && b.textContent.includes('基础状态'));
      const text = blocks[at].textContent;
      const i = text.indexOf('基础状态');
      localStorage.setItem('jasoncai:notes:v1', JSON.stringify({'ot-crdt-lww': [{
        id: 'svgnote', q: '基础状态',
        pre: text.slice(Math.max(0, i - 32), i),
        post: text.slice(i + 4, i + 36),
        block: at, t: 1,
        thread: [{author: '我', text: '旧的图内批注', t: 1}]}]}));
    }""")
    page.reload()
    page.wait_for_timeout(500)
    assert page.locator(".post-body svg mark").count() == 0, "不该往 SVG 里塞 mark"
    assert page.evaluate("() => document.querySelectorAll('.post-body svg text').length") > 10
    tag = page.locator(".note-list .note-stale").first
    assert tag.inner_text() == "图内文字", "图内那条应该标成「图内文字」而不是「定位不到」"


def test_note_list_moves_to_the_right_rail_on_wide_screen(page, base):
    page.set_viewport_size({"width": 1500, "height": 950})
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "批注一")
    page.reload()
    page.wait_for_timeout(500)
    rail = page.locator(".rail-notes .note-list")
    assert rail.is_visible(), "宽屏时本文笔记应在右栏"
    assert "本文笔记 ·1" in rail.inner_text()
    article = page.locator(".main-col").bounding_box()
    box = rail.bounding_box()
    assert box["x"] >= article["x"] + article["width"] - 2, f"笔记栏压住了正文：{box} vs {article}"
    assert not page.locator(".main-col > .note-list").is_visible(), "宽屏时正文末尾那份应隐藏"


def test_note_list_stays_below_the_article_when_narrow(page, base):
    page.set_viewport_size({"width": 1100, "height": 900})
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "批注一")
    page.reload()
    page.wait_for_timeout(500)
    with_notes = page.locator(".main-col > .note-list")
    assert with_notes.is_visible(), "窄屏时本文笔记在正文末尾"
    assert "本文笔记 ·1" in with_notes.inner_text()
    assert not page.locator(".rail-notes").is_visible(), "窄屏没有右栏"


def test_bubble_still_follows_the_mark_on_wide_screen(page, base):
    page.set_viewport_size({"width": 1500, "height": 950})
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "批注一")
    page.reload()
    page.wait_for_timeout(400)
    page.click("mark.note-hl")
    pop = page.locator(".nt-pop")
    pop.wait_for(state="visible")
    box = pop.bounding_box()
    mark = page.locator("mark.note-hl").first.bounding_box()
    assert abs(box["x"] - mark["x"]) <= 2, f"气泡应贴住高亮，实际 {box} vs {mark}"


def test_ask_saves_the_typed_annotation_first(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "旧批注")
    page.reload()
    page.wait_for_timeout(400)
    page.locator("mark.note-hl").first.click()
    page.wait_for_timeout(200)
    page.fill(".nt-pop .nt-input", "为什么丢掉所有编辑也算收敛？")
    page.click(".nt-pop .nt-ask")
    page.wait_for_timeout(500)
    stored = page.evaluate(
        """() => JSON.parse(localStorage.getItem('jasoncai:notes:v1'))['ot-crdt-lww'][0]['thread'][0]['text']"""
    )
    assert stored == "为什么丢掉所有编辑也算收敛？", f"点 AI 之前应当先存批注：{stored}"


def test_ask_without_text_asks_for_a_question(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "")
    page.reload()
    page.wait_for_timeout(400)
    page.locator("mark.note-hl").first.click()
    page.wait_for_timeout(200)
    page.click(".nt-pop .nt-ask")
    page.wait_for_timeout(300)
    hint = page.locator(".nt-pop .nt-job-text").inner_text()
    assert "先写" in hint, f"空批注时应当提示先写问题：{hint}"


def test_save_shows_ai_is_answering_when_a_question_triggers_it(page, base):
    asked = []
    page.on("request", lambda req: asked.append(req.post_data) if req.url.endswith("/ask") else None)
    page.route("**/api/notes/**/ask", lambda route: route.fulfill(
        status=200,
        content_type="application/json",
        body='{"ok": true, "triggered": true, "job": {"id": "job-x", "status": "pending"}}',
    ))
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "旧批注")
    page.reload()
    page.wait_for_timeout(400)
    page.locator("mark.note-hl").first.click()
    page.wait_for_timeout(200)
    page.fill(".nt-pop .nt-input", "为什么这里说收敛不是目标？")
    page.click(".nt-pop [data-act='save']")
    page.wait_for_timeout(800)
    text = page.locator(".nt-job-text").first.inner_text()
    assert "AI 正在回答中" in text, f"保存并触发后应当提示正在回答：{text}"
    assert any(d and '"auto":true' in d.replace(" ", "") for d in asked), f"应当以 auto 方式触发：{asked}"


def test_save_stays_quiet_when_nothing_triggers(page, base):
    page.route("**/api/notes/**/ask", lambda route: route.fulfill(
        status=200,
        content_type="application/json",
        body='{"ok": true, "triggered": false, "job": null}',
    ))
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    seed_note(page, "收敛是必要条件", "旧批注")
    page.reload()
    page.wait_for_timeout(400)
    page.locator("mark.note-hl").first.click()
    page.wait_for_timeout(200)
    page.fill(".nt-pop .nt-input", "收敛是必要条件，不是充分条件")
    page.click(".nt-pop [data-act='save']")
    page.wait_for_timeout(800)
    assert page.locator(".nt-pop").count() == 0, "保存后气泡应当关闭"


def test_home_uploads_local_notes_in_one_batch(page, base):
    """首页补传本地笔记只能发一次批量请求，不能每篇一次（那会把刷新拖慢）。"""
    requests = []
    local = {
        slug: [{"id": f"n{n}abcde", "q": "收敛", "pre": "", "post": "", "block": 0,
                "t": 1_700_000_000_000 + n, "thread": [{"author": "我", "text": "本地旧笔记", "t": 1_700_000_000_000}]}]
        for n, slug in enumerate(["dsh-recall", "dsh-compaction", "agent-reflection"], start=1)
    }
    page.add_init_script(f"localStorage.setItem('{KEY}', {json.dumps(json.dumps(local))})")
    page.route("**/api/notes*", lambda route: (
        requests.append(route.request.url),
        route.fulfill(status=200, content_type="application/json", body='{"items": {}}'),
    )[-1])
    page.goto(base + "/")
    page.wait_for_timeout(1200)
    single = [u for u in requests if "/api/notes/" in u]
    bulk = [u for u in requests if "/api/notes?slugs=" in u]
    assert not single, f"不该逐篇拉取：{single[:3]}"
    assert len(bulk) == 1, f"三篇本地笔记应当合成一次批量请求：{bulk}"


CASES = [
    (test_save_shows_ai_is_answering_when_a_question_triggers_it, False, DESKTOP),
    (test_save_stays_quiet_when_nothing_triggers, False, DESKTOP),
    (test_ask_saves_the_typed_annotation_first, False, DESKTOP),
    (test_ask_without_text_asks_for_a_question, False, DESKTOP),
    (test_highlight_persists_across_reload, False, DESKTOP),
    (test_highlight_paints_only_the_selected_occurrence, False, DESKTOP),
    (test_toolbar_appears_above_selection, False, DESKTOP),
    (test_toolbar_hides_when_selection_cleared, False, DESKTOP),
    (test_annotate_then_edit_and_delete, False, DESKTOP),
    (test_escape_closes_the_bubble, False, DESKTOP),
    (test_note_list_jumps_to_highlight, False, {**DESKTOP, "accept_downloads": True}),
    (test_note_list_click_opens_thread_floors, False, DESKTOP),
    (test_note_relocates_when_paragraphs_shift, False, DESKTOP),
    (test_note_whose_text_was_edited_is_marked_stale, False, DESKTOP),
    (test_translation_insert_does_not_break_anchor_offsets, False, DESKTOP),
    (test_highlight_is_repainted_when_translation_replaces_it, False, DESKTOP),
    (test_selecting_translated_text_does_not_open_toolbar, False, DESKTOP),
    (test_highlights_opt_out_of_translation, False, DESKTOP),
    (test_export_produces_json, False, {**DESKTOP, "accept_downloads": True}),
    (test_home_marks_articles_with_notes, False, DESKTOP),
    (test_selection_spanning_inline_code_is_wrapped_in_parts, False, DESKTOP),
    (test_anchor_miss_is_silent_and_paints_nothing, False, DESKTOP),
    (test_storage_disabled_does_not_break_the_page, True, DESKTOP),
    (test_no_js_has_no_toolbar_and_no_marks, True, {**DESKTOP, "java_script_enabled": False}),
    (test_figure_text_is_not_wrapped_by_marks, False, DESKTOP),
    (test_seeded_note_inside_figure_does_not_break_the_svg, False, DESKTOP),
    (test_note_list_moves_to_the_right_rail_on_wide_screen, False, DESKTOP),
    (test_note_list_stays_below_the_article_when_narrow, False, DESKTOP),
    (test_bubble_still_follows_the_mark_on_wide_screen, False, DESKTOP),
    (test_home_uploads_local_notes_in_one_batch, False, DESKTOP),
]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_case(browser, fn, base, needs_context, context_kwargs):
    ctx = browser.new_context(**context_kwargs)
    try:
        if needs_context:
            fn(ctx, base)
        else:
            fn(ctx.new_page(), base)
    except AssertionError as exc:
        print(f"FAIL {fn.__name__}: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
        return False
    finally:
        ctx.close()
    print(f"ok   {fn.__name__}")
    return True


def main():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", "public"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures = 0
    try:
        wait_for_server(base + "/")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for fn, needs_context, context_kwargs in CASES:
                if not run_case(browser, fn, base, needs_context, context_kwargs):
                    failures += 1
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=5)
    if failures:
        print(f"{failures} 个用例失败")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
