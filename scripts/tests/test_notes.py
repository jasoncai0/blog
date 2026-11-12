#!/usr/bin/env python3
"""划词高亮与批注的浏览器回归。

先 `hugo --minify` 生成 public，再跑：`python3 scripts/tests/test_notes.py`。
"""

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
    page.fill(".nt-pop textarea", "这里要补一个反例")
    page.click(".nt-pop button[data-act='save']")
    page.wait_for_timeout(300)
    page.click("mark.note-hl")
    pop.wait_for(state="visible")
    assert "我" in pop.inner_text(), "气泡里应署名"
    assert "这里要补一个反例" in pop.locator("textarea").input_value()
    page.fill(".nt-pop textarea", "改成：补两个反例")
    page.click(".nt-pop button[data-act='save']")
    page.reload()
    page.wait_for_timeout(400)
    page.click("mark.note-hl")
    pop.wait_for(state="visible")
    assert "改成：补两个反例" in page.locator(".nt-pop textarea").input_value()
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
    listing = page.locator(".note-list")
    assert listing.is_visible(), "有笔记时应显示左栏列表"
    assert "本文笔记 ·1" in listing.locator(".note-head").inner_text()
    page.locator(".note-list .note-items li a").first.click()
    page.wait_for_timeout(400)
    focused = page.evaluate("() => document.activeElement.className")
    assert "note-hl" in focused, f"点列表应把焦点落到高亮上，实际 {focused!r}"


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
    assert page.locator(".note-list").count() == 0 or not page.locator(".note-list").is_visible()
    assert not errors, f"存储被禁时不应抛错：{errors}"


def test_no_js_has_no_toolbar_and_no_marks(ctx, base):
    page = ctx.new_page()
    page.goto(base + ARTICLE)
    assert page.locator(".nt-actions").count() == 0
    assert page.locator("mark.note-hl").count() == 0
    assert "收敛是必要条件" in page.locator(".post-body").inner_text()


CASES = [
    (test_highlight_persists_across_reload, False, DESKTOP),
    (test_highlight_paints_only_the_selected_occurrence, False, DESKTOP),
    (test_toolbar_appears_above_selection, False, DESKTOP),
    (test_toolbar_hides_when_selection_cleared, False, DESKTOP),
    (test_annotate_then_edit_and_delete, False, DESKTOP),
    (test_escape_closes_the_bubble, False, DESKTOP),
    (test_note_list_jumps_to_highlight, False, {**DESKTOP, "accept_downloads": True}),
    (test_export_produces_json, False, {**DESKTOP, "accept_downloads": True}),
    (test_home_marks_articles_with_notes, False, DESKTOP),
    (test_selection_spanning_inline_code_is_wrapped_in_parts, False, DESKTOP),
    (test_anchor_miss_is_silent_and_paints_nothing, False, DESKTOP),
    (test_storage_disabled_does_not_break_the_page, True, DESKTOP),
    (test_no_js_has_no_toolbar_and_no_marks, True, {**DESKTOP, "java_script_enabled": False}),
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
