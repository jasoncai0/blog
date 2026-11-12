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


CASES = [
    (test_highlight_persists_across_reload, False, DESKTOP),
    (test_highlight_paints_only_the_selected_occurrence, False, DESKTOP),
    (test_toolbar_appears_above_selection, False, DESKTOP),
    (test_toolbar_hides_when_selection_cleared, False, DESKTOP),
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
