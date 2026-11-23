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

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        time.sleep(self.delay)
        try:
            req = urllib.request.Request(self.target + self.path)
            for key, value in self.headers.items():
                if key.lower() in ("host", "connection"):
                    continue
                req.add_header(key, value)
            with urllib.request.urlopen(req, timeout=5) as resp:
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


def api(path, method="GET", body=None, base=None, headers=None, noauth=False):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if not noauth and AUTH["token"] and "Authorization" not in hdrs:
        hdrs["Authorization"] = f"Bearer {AUTH['token']}"
    req = urllib.request.Request((base or API) + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return {"status": err.code, "body": err.read().decode()}


AUTH = {"token": "", "id": 0}


def apply_auth(ctx):
    ctx.add_init_script(f"window.__NOTES_API__ = {json.dumps(API)};")
    ctx.add_init_script(
        f"localStorage.setItem('jasoncai:auth:v1', JSON.stringify({login_payload()}));"
    )
    return ctx


def login_payload():
    return json.dumps({"token": AUTH["token"], "id": AUTH["id"], "username": "Root", "role": "admin"})


def reset_db(db):
    """每个用例从空库开始，避免上一条用例的笔记干扰断言。"""
    conn = sqlite3.connect(db, timeout=10)
    conn.executescript("DELETE FROM notes; DELETE FROM note_rev; DELETE FROM progress; DELETE FROM favorites;")
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
    page.wait_for_selector(".nt-pop .nt-input", state="visible")
    page.fill(".nt-pop .nt-input", text)
    page.click(".nt-pop button[data-act='save']")
    page.wait_for_selector("mark.note-hl")


def test_note_written_in_one_browser_shows_up_in_another(page, base):
    add_highlight(page, base)
    wait_until(lambda: api(f"/notes/{SLUG}")["notes"])

    other = apply_auth(page.context.browser.new_context(**DESKTOP))
    try:
        fresh = other.new_page()
        fresh.goto(base + ARTICLE)
        fresh.wait_for_selector("mark.note-hl", timeout=8000)
        assert fresh.locator("mark.note-hl").count() == 1, "新上下文里应出现同一条高亮"
        fresh.click("mark.note-hl")
        fresh.wait_for_selector(".nt-pop .nt-input", state="visible")
        assert "服务器上的一条批注" in fresh.locator(".nt-pop .nt-input").input_value()
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

    other = apply_auth(page.context.browser.new_context(**DESKTOP))
    try:
        fresh = other.new_page()
        fresh.goto(base + ARTICLE)
        fresh.wait_for_timeout(1500)
        assert fresh.locator("mark.note-hl").count() == 0, "服务端已删的笔记不该再出现"
    finally:
        other.close()


def test_progress_from_server_resumes_on_a_new_device(page, base):
    api(f"/progress/{SLUG}", "PUT", {"p": 62, "t": int(time.time() * 1000), "d": False})
    other = apply_auth(page.context.browser.new_context(**DESKTOP))
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
    other = apply_auth(page.context.browser.new_context(**DESKTOP))
    try:
        fresh = other.new_page()
        fresh.goto(base + "/")
        fresh.wait_for_selector(f'li[data-progress-post="{SLUG}"] .note-mark', timeout=8000)
        item = fresh.locator(f'li[data-progress-post="{SLUG}"]')
        assert item.locator(".note-mark").inner_text().startswith("有笔记"), "首页应标出有笔记的篇目"
        assert item.locator(".read-mark").inner_text().endswith("%"), "首页应标出读到哪"
    finally:
        other.close()


def test_local_only_progress_is_uploaded_from_home(context, base):
    """老数据只在浏览器里（服务端没有）时，打开首页就应该补传上去。"""
    slug = "agent-vs-computer"
    before = api("/progress")["progress"]
    assert slug not in before, "这个用例要求服务端一开始没有这篇的进度"
    context.add_init_script(
        f"window.__NOTES_API__ = {json.dumps(API)};"
        f"localStorage.setItem('jasoncai:read:v2:u{AUTH['id']}', JSON.stringify({{"
        f'"{slug}": {{"p": 77, "t": Date.now(), "d": false}}'
        "}));"
    )
    page = context.new_page()
    page.goto(base + "/")
    wait_until(lambda: slug in api("/progress")["progress"], timeout=8.0)
    stored = api("/progress")["progress"][slug]
    assert stored["p"] == 77, f"服务端应补上本地那 77%，实际 {stored}"



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


def test_reply_creates_a_floor(page, base):
    add_highlight(page, base, text="第一层：这条是提问")
    page.click("mark.note-hl")
    page.wait_for_selector(".nt-reply-input", state="visible")
    page.fill(".nt-reply-input", "第二层：这是回复")
    page.click(".nt-reply-send")
    page.wait_for_selector(".nt-thread .nt-floor", timeout=8000)
    page.wait_for_timeout(600)
    assert page.locator(".nt-thread .nt-floor").count() == 1
    assert "2 楼" in page.locator(".nt-thread .nt-floor").first.inner_text()
    assert "第二层：这是回复" in page.locator(".nt-thread").inner_text()
    assert "2 层" in page.locator(".nt-floors").inner_text()
    # 换一个浏览器看得到（服务端真的存了）
    other = apply_auth(page.context.browser.new_context(**DESKTOP))
    try:
        fresh = other.new_page()
        fresh.goto(base + ARTICLE)
        fresh.wait_for_selector("mark.note-hl", timeout=10000)
        fresh.click("mark.note-hl")
        fresh.wait_for_selector(".nt-thread .nt-floor", timeout=8000)
        assert "第二层：这是回复" in fresh.locator(".nt-thread").inner_text()
    finally:
        other.close()


def test_ai_reply_is_marked_as_ai(page, base):
    add_highlight(page, base, text="我想问一个问题")
    nid = api(f"/notes/{SLUG}")["notes"][0]["id"]
    result = api(f"/notes/{SLUG}/{nid}/replies", "POST", {"author": "AI", "text": "AI 的回答", "t": 2000})
    assert result.get("ok") or result.get("note"), result
    page.reload()
    page.wait_for_selector("mark.note-hl", timeout=10000)
    page.click("mark.note-hl")
    page.wait_for_selector(".nt-thread .is-ai", timeout=8000)
    assert "AI 的回答" in page.locator(".nt-thread .is-ai").inner_text()


class keyed_server:
    """起一个带口令的 notesd，返回 (api_base, stop)。"""

    def __init__(self):
        self.port = free_port()
        self.tmp = tempfile.TemporaryDirectory()

    def __enter__(self):
        env = dict(os.environ, NOTES_EDIT_KEY="test-secret")
        self.proc = subprocess.Popen(
            [sys.executable, "server/notesd.py", "--port", str(self.port), "--db",
             os.path.join(self.tmp.name, "k.db")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        wait_for_server(f"http://127.0.0.1:{self.port}/healthz")
        return f"http://127.0.0.1:{self.port}/api"

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.tmp.cleanup()


def test_stale_key_does_not_pop_on_page_load(context, base):
    with keyed_server() as keyed:
        context.add_init_script(
            f"window.__NOTES_API__ = {json.dumps(keyed)};"
            "localStorage.setItem('jasoncai:notes:key', 'wrong-key');"
        )
        page = context.new_page()
        page.goto(base + ARTICLE)
        page.wait_for_timeout(1500)
        assert page.locator(".nt-key").count() == 0, "加载页面时不该主动弹口令框"
        page.evaluate(SELECT_JS, "收敛是必要条件")
        page.click(".nt-actions button[data-action='highlight']")
        page.wait_for_selector(".nt-key", state="visible", timeout=8000)
        assert "口令不对" in page.locator(".nt-key-title").inner_text()


def test_write_without_key_prompts_and_then_succeeds(context, base):
    # 另起一个带口令的 notesd，客户端指过去；输入口令后队列应自己补发。
    port = free_port()
    tmp = tempfile.TemporaryDirectory()
    env = dict(os.environ, NOTES_EDIT_KEY="test-secret")
    proc = subprocess.Popen(
        [sys.executable, "server/notesd.py", "--port", str(port), "--db", os.path.join(tmp.name, "k.db")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_server(f"http://127.0.0.1:{port}/healthz")
        keyed = f"http://127.0.0.1:{port}/api"
        context.add_init_script(f"window.__NOTES_API__ = {json.dumps(keyed)};")
        page = context.new_page()
        page.goto(base + ARTICLE)
        page.wait_for_timeout(400)
        page.evaluate(SELECT_JS, "收敛是必要条件")
        page.click(".nt-actions button[data-action='highlight']")
        page.wait_for_selector(".nt-key", state="visible", timeout=8000)
        page.fill(".nt-key-input", "test-secret")
        page.click(".nt-key-ok")
        page.wait_for_timeout(200)
        deadline = time.time() + 15
        got = []
        while time.time() < deadline and not got:
            got = api(f"/notes/{SLUG}", base=keyed)["notes"]
            time.sleep(0.2)
        assert got, "输入口令后队列应补发到带口令的服务端"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        tmp.cleanup()


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
        f"localStorage.setItem('jasoncai:notes:v2:u{AUTH['id']}', {json.dumps(json.dumps(local))});"
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


def test_favorite_from_one_browser_shows_in_another(page, base):
    page.goto(base + "/")
    page.wait_for_selector('.fav-btn[data-slug="hello"]', state="attached")
    page.locator('.fav-btn[data-slug="hello"]').click()
    wait_until(lambda: "hello" in api("/favorites").get("favorites", {}), timeout=8.0, interval=0.2)
    other = apply_auth(page.context.browser.new_context(**DESKTOP))
    try:
        fresh = other.new_page()
        fresh.goto(base + "/")
        wait_until(
            lambda: fresh.eval_on_selector(
                '.fav-btn[data-slug="hello"]', "e => e.getAttribute('aria-pressed')"
            ) == "true",
            timeout=8.0,
            interval=0.2,
        )
    finally:
        other.close()


def test_unfavorite_propagates_to_other_browser(page, base):
    api("/favorites/hello", "PUT", {"t": int(time.time() * 1000)})
    page.goto(base + "/")
    page.wait_for_selector('.fav-btn[data-slug="hello"]', state="attached")
    wait_until(
        lambda: page.eval_on_selector('.fav-btn[data-slug="hello"]', "e => e.getAttribute('aria-pressed')") == "true",
        timeout=8.0,
        interval=0.2,
    )
    page.locator('.fav-btn[data-slug="hello"]').click()
    wait_until(lambda: "hello" not in api("/favorites").get("favorites", {}), timeout=8.0, interval=0.2)
    page.reload()
    page.wait_for_selector('.fav-btn[data-slug="hello"]', state="attached")
    wait_until(
        lambda: page.eval_on_selector('.fav-btn[data-slug="hello"]', "e => e.getAttribute('aria-pressed')") == "false",
        timeout=8.0,
        interval=0.2,
    )


CASES = [
    (test_note_written_in_one_browser_shows_up_in_another, False, DESKTOP),
    (test_deleted_note_does_not_come_back, False, DESKTOP),
    (test_progress_from_server_resumes_on_a_new_device, False, DESKTOP),
    (test_home_shows_server_side_marks, False, DESKTOP),
    (test_local_only_progress_is_uploaded_from_home, True, DESKTOP),
    (test_offline_write_is_queued_and_flushed_later, False, DESKTOP),
    (test_progress_arriving_after_load_still_resumes, False, DESKTOP),
    (test_all_local_notes_upload_from_any_page, True, DESKTOP),
    (test_favorite_from_one_browser_shows_in_another, False, DESKTOP),
    (test_unfavorite_propagates_to_other_browser, False, DESKTOP),
    (test_reply_creates_a_floor, False, DESKTOP),
    (test_ai_reply_is_marked_as_ai, False, DESKTOP),
]


def run_case(browser, fn, base, needs_context, context_kwargs, db=None):
    if db:
        reset_db(db)
    ctx = browser.new_context(**context_kwargs)
    ctx.add_init_script(f"window.__NOTES_API__ = {json.dumps(API)};")
    ctx.add_init_script(
        f"localStorage.setItem('jasoncai:auth:v1', JSON.stringify({login_payload()}));"
    )
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
        # 起一个 Root：新库没有账号，直接用 Store 建，再走登录拿 Bearer。
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "server"))
        import notesd as notesd_mod

        notesd_mod.Store(db).create_user("Root", "root-pass-1", "admin")
        body = api("/login", "POST", {"username": "Root", "password": "root-pass-1"}, noauth=True)
        AUTH["token"] = body["token"]
        AUTH["id"] = body["user"]["id"]
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
