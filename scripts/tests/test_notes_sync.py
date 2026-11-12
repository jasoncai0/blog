#!/usr/bin/env python3
"""跨「浏览器」的同步回归：笔记与阅读进度存在服务端，换个上下文也还在。

先 `hugo --minify`，再跑：`python3 scripts/tests/test_notes_sync.py`。
测试会自己起一个 notesd（临时库）和一个静态服务器。
"""

import json
import os
import socket
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

ARTICLE = "/posts/ot-crdt-lww.html"
SLUG = "ot-crdt-lww"
DESKTOP = {"viewport": {"width": 1200, "height": 900}}

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


class SlowProxy(BaseHTTPRequestHandler):
    """把 /api/* 延迟 1 秒再转发给真的 notesd，用来复现「进度比页面晚到」。"""

    target = ""
    delay = 1.0

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        time.sleep(self.delay)
        try:
            with urllib.request.urlopen(self.target + self.path, timeout=5) as resp:
                body = resp.read()
                status = resp.status
        except Exception:  # noqa: BLE001
            body, status = b"{}", 502
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


API = ""


def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return {"status": err.code, "body": err.read().decode()}


def reset_db(db):
    """每个用例从空库开始，避免上一条用例的笔记干扰断言。"""
    conn = sqlite3.connect(db, timeout=10)
    conn.executescript("DELETE FROM notes; DELETE FROM note_rev; DELETE FROM progress;")
    conn.commit()
    conn.close()


def wait_until(fn, timeout=8.0, interval=0.15):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            value = fn()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(interval)
    raise AssertionError(f"等待超时：{last}")


def wait_for_server(url, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    raise RuntimeError(f"服务器没起来: {url}")


def add_highlight(page, base, needle="收敛是必要条件", text="服务器上的一条批注"):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    assert page.evaluate(SELECT_JS, needle), "没选中文字"
    page.click(".nt-actions button[data-action='annotate']")
    page.wait_for_selector(".nt-pop textarea", state="visible")
    page.fill(".nt-pop textarea", text)
    page.click(".nt-pop button[data-act='save']")
    page.wait_for_selector("mark.note-hl")


def test_note_written_in_one_browser_shows_up_in_another(page, base):
    add_highlight(page, base)
    wait_until(lambda: api(f"/notes/{SLUG}")["notes"])

    other = page.context.browser.new_context(**DESKTOP)
    other.add_init_script(f"window.__NOTES_API__ = {json.dumps(API)};")
    try:
        fresh = other.new_page()
        fresh.goto(base + ARTICLE)
        fresh.wait_for_selector("mark.note-hl", timeout=8000)
        assert fresh.locator("mark.note-hl").count() == 1, "新上下文里应出现同一条高亮"
        fresh.click("mark.note-hl")
        fresh.wait_for_selector(".nt-pop textarea", state="visible")
        assert "服务器上的一条批注" in fresh.locator(".nt-pop textarea").input_value()
    finally:
        other.close()


def test_deleted_note_does_not_come_back(page, base):
    add_highlight(page, base, text="这条马上删")
    wait_until(lambda: api(f"/notes/{SLUG}")["notes"])
    page.click("mark.note-hl")
    page.wait_for_selector(".nt-pop button[data-act='delete']", state="visible")
    page.click(".nt-pop button[data-act='delete']")
    page.wait_for_timeout(400)
    wait_until(lambda: not api(f"/notes/{SLUG}")["notes"])

    other = page.context.browser.new_context(**DESKTOP)
    other.add_init_script(f"window.__NOTES_API__ = {json.dumps(API)};")
    try:
        fresh = other.new_page()
        fresh.goto(base + ARTICLE)
        fresh.wait_for_timeout(1500)
        assert fresh.locator("mark.note-hl").count() == 0, "服务端已删的笔记不该再出现"
    finally:
        other.close()


def test_progress_from_server_resumes_on_a_new_device(page, base):
    api(f"/progress/{SLUG}", "PUT", {"p": 62, "t": int(time.time() * 1000), "d": False})
    other = page.context.browser.new_context(**DESKTOP)
    other.add_init_script(f"window.__NOTES_API__ = {json.dumps(API)};")
    try:
        fresh = other.new_page()
        fresh.goto(base + ARTICLE)
        fresh.wait_for_function("() => window.scrollY > 200", timeout=8000)
        top = fresh.evaluate("() => Math.round(window.scrollY)")
        assert top > 200, f"新设备应自动续读到 62%，实际 scrollY={top}"
    finally:
        other.close()


def test_home_shows_server_side_marks(page, base):
    api(f"/progress/{SLUG}", "PUT", {"p": 40, "t": int(time.time() * 1000), "d": False})
    stamp = int(time.time() * 1000)
    api(
        f"/notes/{SLUG}/seed-home-ab",
        "PUT",
        {
            "id": "seed-home-ab",
            "q": "收敛是必要条件",
            "pre": "",
            "post": "",
            "block": 4,
            "t": stamp,
            "thread": [{"author": "我", "text": "首页标记用", "t": stamp}],
        },
    )
    wait_until(lambda: api(f"/notes/{SLUG}")["notes"])
    other = page.context.browser.new_context(**DESKTOP)
    other.add_init_script(f"window.__NOTES_API__ = {json.dumps(API)};")
    try:
        fresh = other.new_page()
        fresh.goto(base + "/")
        fresh.wait_for_selector(f'li[data-progress-post="{SLUG}"] .note-mark', timeout=8000)
        item = fresh.locator(f'li[data-progress-post="{SLUG}"]')
        assert item.locator(".note-mark").inner_text().startswith("有笔记"), "首页应标出有笔记的篇目"
        assert item.locator(".read-mark").inner_text().endswith("%"), "首页应标出读到哪"
    finally:
        other.close()


def test_offline_write_is_queued_and_flushed_later(page, base):
    page.route("**/api/**", lambda route: route.abort())
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    assert page.evaluate(SELECT_JS, "收敛是必要条件")
    page.click(".nt-actions button[data-action='highlight']")
    page.wait_for_selector("mark.note-hl")
    assert not api(f"/notes/{SLUG}")["notes"], "断网时不该写到服务器"

    page.unroute("**/api/**")
    page.evaluate("() => window.dispatchEvent(new Event('online'))")
    wait_until(lambda: api(f"/notes/{SLUG}")["notes"], timeout=10)


def test_all_local_notes_upload_from_any_page(context, base):
    """换服务器前写在浏览器里的笔记，打开首页就该全部补传（不用逐篇打开）。"""
    stamp = int(time.time() * 1000)
    local = {
        SLUG: [{"id": "legacy-ot-0001", "q": "收敛是必要条件", "pre": "", "post": "", "block": 4,
                "t": stamp, "thread": [{"author": "我", "text": "旧批注 A", "t": stamp}]}],
        "hello": [{"id": "legacy-hi-0001", "q": "博客", "pre": "", "post": "", "block": 1,
                   "t": stamp, "thread": [{"author": "我", "text": "旧批注 B", "t": stamp}]}],
        "opencode-rewind": [{"id": "legacy-rw-0001", "q": "回滚", "pre": "", "post": "", "block": 2,
                             "t": stamp, "thread": [{"author": "我", "text": "旧批注 C", "t": stamp}]}],
    }
    context.add_init_script(
        f"localStorage.setItem('jasoncai:notes:v1', {json.dumps(json.dumps(local))});"
    )
    page = context.new_page()
    page.goto(base + "/")
    wait_until(lambda: all(api(f"/notes/{slug}")["notes"] for slug in local), timeout=15)
    got = {slug: [n["thread"][0]["text"] for n in api(f"/notes/{slug}")["notes"]] for slug in local}
    assert got[SLUG] == ["旧批注 A"], got
    assert got["hello"] == ["旧批注 B"], got
    assert got["opencode-rewind"] == ["旧批注 C"], got


def test_progress_arriving_after_load_still_resumes(page, base):
    """服务端响应晚于 load 事件时，也要接手续读（首屏读完才拿到进度的情况）。"""
    api(f"/progress/{SLUG}", "PUT", {"p": 62, "t": int(time.time() * 1000), "d": False})
    port = free_port()
    SlowProxy.target = API.rsplit("/api", 1)[0]  # 只保留 http://host:port

    proxy = ThreadingHTTPServer(("127.0.0.1", port), SlowProxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        page.add_init_script(f"window.__NOTES_API__ = 'http://127.0.0.1:{port}/api';")
        page.goto(base + ARTICLE)
        page.wait_for_function("() => window.scrollY > 200", timeout=10000)
        top = page.evaluate("() => Math.round(window.scrollY)")
        assert top > 200, f"晚到的进度也应续读，实际 scrollY={top}"
    finally:
        proxy.shutdown()
        proxy.server_close()


CASES = [
    (test_note_written_in_one_browser_shows_up_in_another, False, DESKTOP),
    (test_deleted_note_does_not_come_back, False, DESKTOP),
    (test_progress_from_server_resumes_on_a_new_device, False, DESKTOP),
    (test_home_shows_server_side_marks, False, DESKTOP),
    (test_offline_write_is_queued_and_flushed_later, False, DESKTOP),
    (test_progress_arriving_after_load_still_resumes, False, DESKTOP),
    (test_all_local_notes_upload_from_any_page, True, DESKTOP),
]


def run_case(browser, fn, base, needs_context, context_kwargs, db=None):
    if db:
        reset_db(db)
    ctx = browser.new_context(**context_kwargs)
    ctx.add_init_script(f"window.__NOTES_API__ = {json.dumps(API)};")
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
    global API
    web_port = free_port()
    api_port = free_port()
    base = f"http://127.0.0.1:{web_port}"
    API = f"http://127.0.0.1:{api_port}/api"

    tmp = tempfile.TemporaryDirectory()
    db = os.path.join(tmp.name, "notes.db")
    notesd = subprocess.Popen(
        [sys.executable, "server/notesd.py", "--port", str(api_port), "--db", db],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    web = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(web_port), "--bind", "127.0.0.1", "--directory", "public"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures = 0
    try:
        wait_for_server(base + "/")
        wait_for_server(API + "/healthz")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for fn, needs_context, context_kwargs in CASES:
                if not run_case(browser, fn, base, needs_context, context_kwargs, db):
                    failures += 1
            browser.close()
    finally:
        for proc in (web, notesd):
            proc.terminate()
        for proc in (web, notesd):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        tmp.cleanup()
    if failures:
        print(f"{failures} 个用例失败")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
