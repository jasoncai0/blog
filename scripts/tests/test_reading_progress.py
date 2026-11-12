#!/usr/bin/env python3
"""文章页阅读进度的浏览器测试。

先 `hugo --minify` 生成 public，再跑本脚本；脚本自己起 http.server，
用 Chromium 打开文章页，检查进度条、胶囊文字、续读与 localStorage 落盘。
"""

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

KEY = "jasoncai:read:v1"


def wait_for_server(url, timeout=15.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return  # 服务器已经在答话，只是这个路径不存在
        except Exception as exc:  # 端口还没起来
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"服务器没起来: {url}: {last}")


def preset(context, entries):
    """预置存储；条目没写 t 的补成现在，否则会被 180 天时效挡掉。"""
    payload = json.dumps(entries)
    context.add_init_script(
        f"const data = JSON.parse({json.dumps(payload)});"
        "Object.values(data).forEach(e => { if (!e.t) e.t = Date.now(); });"
        f"localStorage.setItem({json.dumps(KEY)}, JSON.stringify(data));"
    )


def read_store(page):
    raw = page.evaluate(f"localStorage.getItem({KEY!r})")
    assert raw, "localStorage 里没有进度记录"
    return json.loads(raw)


def test_article_scroll_records_progress(page, base):
    page.goto(base + "/posts/hello.html")
    page.wait_for_selector(".rp-bar")
    assert page.locator(".rp-bar").count() == 1, "应当只注入一个进度条"
    assert page.eval_on_selector(".rp-chip", "e => e.hasAttribute('hidden')"), "顶部胶囊应隐藏"
    assert page.eval_on_selector(".rp-bar", "e => e.getAttribute('aria-valuenow')").isdigit()
    page.mouse.wheel(0, 100000)
    page.wait_for_timeout(1400)  # 等 1s 落盘节流
    store = page.evaluate(f"localStorage.getItem({KEY!r})")
    assert store is not None and '"p":100' in store.replace(" ", "")
    assert page.eval_on_selector(".rp-chip", "e => e.textContent").strip() == "读完"
    assert page.eval_on_selector(".rp-bar-fill", "e => e.style.width") in {"100%", "100.0%"}


def test_chip_appears_after_scrolling(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    page.wait_for_selector(".rp-chip", state="attached")
    assert page.eval_on_selector(".rp-chip", "e => e.hasAttribute('hidden')")
    page.mouse.wheel(0, 900)
    page.wait_for_timeout(300)
    assert not page.eval_on_selector(".rp-chip", "e => e.hasAttribute('hidden')"), "滚动后胶囊应出现"
    text = page.eval_on_selector(".rp-chip", "e => e.textContent").strip()
    assert text.endswith("%") and int(text.rstrip("%")) > 2


def test_two_pages_merge(context, base):
    first = context.new_page()
    first.goto(base + "/posts/hello.html")
    first.wait_for_selector(".rp-bar")
    first.mouse.wheel(0, 100000)
    first.wait_for_timeout(1400)

    second = context.new_page()
    second.goto(base + "/posts/ot-crdt-lww.html")
    second.wait_for_selector(".rp-bar")
    second.mouse.wheel(0, 100000)
    second.wait_for_timeout(1400)

    store = read_store(first)
    assert "hello" in store and "ot-crdt-lww" in store, store
    assert store["hello"]["p"] == 100 and store["ot-crdt-lww"]["p"] == 100


def test_resume_scrolls_and_hints(context, base):
    # hello 太短（40% 只落在 51px），换成长文
    preset(context, {"ot-crdt-lww": {"p": 40, "t": 0, "d": False}})
    page = context.new_page()
    page.goto(base + "/posts/ot-crdt-lww.html")
    page.wait_for_timeout(800)
    assert page.evaluate("window.scrollY") > 2000, "应当自动滚到 40% 的位置"
    hint = page.locator(".rp-hint")
    assert hint.count() == 1, "应当出现续读提示"
    assert "从 40% 继续" in hint.inner_text()


def test_anchor_wins_over_resume(context, base):
    preset(context, {"ot-crdt-lww": {"p": 40, "t": 0, "d": False}})
    page = context.new_page()
    page.goto(base + "/posts/ot-crdt-lww.html#d1t")
    page.wait_for_timeout(800)
    assert page.locator(".rp-hint").count() == 0, "带锚点时不该续读"


def test_bfcache_does_not_double_scroll(context, base):
    page = context.new_page()
    page.goto(base + "/posts/hello.html")
    page.wait_for_selector(".rp-bar")
    page.evaluate("window.scrollTo(0, 1200)")
    page.wait_for_timeout(300)
    before = page.evaluate("window.scrollY")
    page.goto(base + "/")
    page.wait_for_timeout(200)
    page.go_back()
    page.wait_for_timeout(800)
    assert page.locator(".rp-hint").count() == 0, "后退回来不该再续读"
    after = page.evaluate("window.scrollY")
    assert abs(after - before) <= 50, f"滚动位置被脚本动过: {before} -> {after}"


def test_partial_view_is_not_done(context, base):
    # 正文 1099px 高、从 ~411px 开始：900×1200 的视口只看到一部分，不能算读完
    page = context.new_page()
    page.goto(base + "/posts/hello.html")
    page.wait_for_selector(".rp-bar")
    page.wait_for_timeout(800)
    text = page.eval_on_selector(".rp-chip", "e => e.textContent").strip()
    assert text != "读完", f"正文还有一部分在视口外，不该显示读完: {text}"
    store = json.loads(page.evaluate(f"localStorage.getItem({KEY!r})") or "{}")
    assert store.get("hello", {}).get("p", 0) < 95, store


def test_short_post_fills_viewport(context, base):
    # 正文一屏放得下：不看滚动也该是 100%，不能停在 0% 占位
    page = context.new_page()
    page.goto(base + "/posts/hello.html")
    page.wait_for_selector(".rp-bar")
    page.wait_for_timeout(800)
    assert page.eval_on_selector(".rp-chip", "e => e.textContent").strip() == "读完"
    store = read_store(page)
    assert store["hello"]["p"] == 100, store


def test_home_mark_shares_row_with_tags(context, base):
    preset(context, {"hello": {"p": 62, "t": 0, "d": False}})
    page = context.new_page()
    page.goto(base + "/")
    info = page.eval_on_selector('li[data-progress-post="hello"]', """li => {
      const tags = li.querySelector('.post-tags');
      const chip = tags.querySelector('.tag-chip');
      const mark = li.querySelector('.read-mark');
      const style = getComputedStyle(tags);
      return {display: style.display, gap: style.columnGap,
              markTop: Math.round(mark.getBoundingClientRect().top),
              chipTop: Math.round(chip.getBoundingClientRect().top)};
    }""")
    assert info["display"] == "flex", info
    assert info["gap"] == "6px", f"tag 间距应恢复到 6px: {info}"
    assert abs(info["markTop"] - info["chipTop"]) <= 8, f"标记应与 tag 行同排: {info}"


def test_storage_full_is_silent(context, base):
    context.add_init_script(
        "Storage.prototype.setItem = function () { const e = new Error('QuotaExceededError'); e.name='QuotaExceededError'; throw e; };"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(base + "/posts/ot-crdt-lww.html")
    page.wait_for_selector(".rp-bar")
    page.mouse.wheel(0, 3000)
    page.wait_for_timeout(1400)
    assert page.eval_on_selector(".rp-chip", "e => e.textContent").strip().endswith("%")
    assert errors == [], f"配额满不该抛未捕获错误: {errors}"


def test_no_js_page_intact(context, base):
    page = context.new_page()
    page.goto(base + "/posts/hello.html")
    assert "hello" in page.inner_text("h1").strip().lower() or page.inner_text("h1").strip()
    assert page.locator(".post-body p").count() > 0, "正文应当在"
    assert page.locator(".rp-bar").count() == 0, "没有 JS 就不该有进度条"


def test_storage_failure_is_silent(context, base):
    context.add_init_script(
        "Object.defineProperty(window, 'localStorage', { get() { throw new Error('denied'); } });"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(base + "/posts/hello.html")
    page.wait_for_selector(".rp-bar")
    page.mouse.wheel(0, 3000)
    page.wait_for_timeout(1400)
    assert page.locator(".post-body p").count() > 0, "存储坏了正文也要在"
    text = page.eval_on_selector(".rp-chip", "e => e.textContent").strip()
    assert text == "读完" or text.endswith("%"), text
    assert errors == [], f"不该有未捕获错误: {errors}"


def test_js_url_is_fingerprinted(page, base):
    page.goto(base + "/posts/hello.html")
    src = page.eval_on_selector("script[src*='reading-progress']", "e => e.getAttribute('src')")
    assert "/js/reading-progress." in src and src.endswith(".js"), f"JS 应带 fingerprint: {src}"
    assert "reading-progress.js" != src.rsplit("/", 1)[-1], "不该再用未指纹化的固定 URL"


def test_home_marks_refresh_after_bfcache(context, base):
    preset(context, {"hello": {"p": 62, "t": 0, "d": False}})
    page = context.new_page()
    page.goto(base + "/")
    mark = page.locator('li[data-progress-post="hello"] .read-mark')
    assert mark.inner_text().strip() == "62%"
    # 模拟从文章页后退、页面由 bfcache 恢复：存储里已经是 100
    page.evaluate("localStorage.setItem('jasoncai:read:v1', JSON.stringify({hello:{p:100,t:Date.now(),d:true}}))")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    page.wait_for_timeout(100)
    assert mark.inner_text().strip() == "读完", "bfcache 恢复后标记应重算"


def test_home_marks(context, base):
    preset(context, {
        "hello": {"p": 62, "t": 0, "d": False},
        "ot-crdt-lww": {"p": 100, "t": 0, "d": True},
    })
    page = context.new_page()
    page.goto(base + "/")
    hello = page.locator('li[data-progress-post="hello"] .read-mark')
    lww = page.locator('li[data-progress-post="ot-crdt-lww"] .read-mark')
    assert hello.count() == 1, "hello 那行应有进度标记"
    assert hello.inner_text().strip() == "62%"
    assert hello.is_visible(), "进度标记应可见"
    assert lww.inner_text().strip() == "读完"
    # 没读过的篇目不留可见空壳（节点留着，等服务器上的进度晚到）
    empty = page.locator('li[data-progress-post="agent-vs-computer"] .read-mark')
    assert empty.inner_text().strip() == "" and not empty.is_visible()


CASES = [
    (test_article_scroll_records_progress, False, {}),
    (test_chip_appears_after_scrolling, False, {}),
    (test_two_pages_merge, True, {}),
    (test_resume_scrolls_and_hints, True, {}),
    (test_anchor_wins_over_resume, True, {}),
    (test_bfcache_does_not_double_scroll, True, {}),
    (test_home_marks, True, {}),
    (test_js_url_is_fingerprinted, False, {}),
    (test_home_mark_shares_row_with_tags, True, {}),
    (test_storage_full_is_silent, True, {}),
    (test_home_marks_refresh_after_bfcache, True, {}),
    (test_partial_view_is_not_done, True, {}),
    (test_short_post_fills_viewport, True, {"viewport": {"width": 900, "height": 2400}}),
    (test_no_js_page_intact, True, {"java_script_enabled": False}),
    (test_storage_failure_is_silent, True, {}),
]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_case(browser, fn, base, needs_context, context_kwargs):
    opts = {"viewport": {"width": 900, "height": 800}, **context_kwargs}
    ctx = browser.new_context(**opts)
    try:
        if needs_context:
            fn(ctx, base)
        else:
            fn(ctx.new_page(), base)
        print(f"PASS {fn.__name__}")
        return True
    except Exception as exc:
        print(f"FAIL {fn.__name__}: {exc}")
        return False
    finally:
        ctx.close()


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
        wait_for_server(base + "/posts/hello.html")
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
