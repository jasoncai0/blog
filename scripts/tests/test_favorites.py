#!/usr/bin/env python3
"""收藏功能的浏览器测试（静态站点，没有 /api，走纯本机路径）。

先 `hugo --minify`，再跑：`python3 scripts/tests/test_favorites.py`。
"""

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

KEY = "jasoncai:fav:v1"


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


def preset(context, slugs):
    payload = {slug: 1_700_000_000_000 for slug in slugs}
    context.add_init_script(
        f"localStorage.setItem({json.dumps(KEY)}, {json.dumps(json.dumps(payload))});"
    )


def visible_posts(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('ul.posts > li')].filter(li => !li.hidden).length"
    )


def test_home_renders_star_buttons(page, base):
    page.goto(base + "/")
    page.wait_for_selector(".fav-btn", state="attached")
    total = page.locator("ul.posts > li").count()
    assert page.locator("ul.posts .fav-btn").count() == total, "每篇都要有一颗星"
    assert page.eval_on_selector(".fav-btn", "e => e.getAttribute('aria-pressed')") == "false"
    assert page.eval_on_selector(".fav-btn", "e => e.textContent.trim()") == "☆ 收藏"


def test_toggle_persists_across_reload(page, base):
    page.goto(base + "/")
    page.wait_for_selector(".fav-btn", state="attached")
    slug = page.eval_on_selector("ul.posts > li", "e => e.dataset.progressPost")
    page.locator(f'.fav-btn[data-slug="{slug}"]').click()
    page.wait_for_timeout(150)
    assert page.eval_on_selector(f'.fav-btn[data-slug="{slug}"]', "e => e.getAttribute('aria-pressed')") == "true"
    store = json.loads(page.evaluate(f"localStorage.getItem({KEY!r})") or "{}")
    assert slug in store, "收藏要落盘"
    page.reload()
    page.wait_for_selector(".fav-btn", state="attached")
    assert page.eval_on_selector(f'.fav-btn[data-slug="{slug}"]', "e => e.getAttribute('aria-pressed')") == "true"


def test_filter_shows_only_favorites(context, base):
    preset(context, ["hello", "ot-crdt-lww"])
    page = context.new_page()
    page.goto(base + "/")
    page.wait_for_selector(".fav-btn", state="attached")
    chip = page.locator('.read-filter button[data-read="fav"]')
    assert chip.count() == 1, "要有收藏筛选按钮"
    assert "收藏 2" == chip.text_content().strip()
    chip.click()
    page.wait_for_timeout(150)
    assert visible_posts(page) == 2, "只剩两篇收藏"
    slugs = page.evaluate(
        "() => [...document.querySelectorAll('ul.posts > li')].filter(l => !l.hidden)"
        ".map(l => l.dataset.progressPost)"
    )
    assert set(slugs) == {"hello", "ot-crdt-lww"}
    assert page.evaluate("() => location.search") == "?read=fav"


def test_filter_deep_link(context, base):
    preset(context, ["hello"])
    page = context.new_page()
    page.goto(base + "/?read=fav")
    page.wait_for_selector(".fav-btn", state="attached")
    page.wait_for_timeout(200)
    assert visible_posts(page) == 1
    assert page.eval_on_selector('.read-filter button[data-read="fav"]', "e => e.getAttribute('aria-pressed')") == "true"


def test_unfavorite_leaves_the_filter(context, base):
    preset(context, ["hello"])
    page = context.new_page()
    page.goto(base + "/?read=fav")
    page.wait_for_selector(".fav-btn", state="attached")
    page.locator('.fav-btn[data-slug="hello"]').click()
    page.wait_for_timeout(150)
    assert visible_posts(page) == 0, "取消后立即从收藏视图消失"
    assert "收藏 0" == page.locator('.read-filter button[data-read="fav"]').text_content().strip()


def test_article_star_matches_home(context, base):
    page = context.new_page()
    page.goto(base + "/posts/hello.html")
    page.wait_for_selector(".post-meta .fav-btn", state="attached")
    page.locator(".post-meta .fav-btn").click()
    page.wait_for_timeout(150)
    assert page.eval_on_selector(".post-meta .fav-btn", "e => e.getAttribute('aria-pressed')") == "true"
    page.goto(base + "/")
    page.wait_for_selector('.fav-btn[data-slug="hello"]')
    assert page.eval_on_selector('.fav-btn[data-slug="hello"]', "e => e.getAttribute('aria-pressed')") == "true"


def test_no_js_keeps_page_clean(context, base):
    page = context.new_page()
    page.goto(base + "/")
    assert page.locator(".fav-btn").count() == 0, "无 JS 时不注入收藏按钮"
    assert page.eval_on_selector(".read-filter", "e => e.hasAttribute('hidden')")
    assert page.locator("ul.posts > li").count() > 10, "列表仍完整"


def test_storage_denied_is_silent(context, base):
    context.add_init_script(
        "Object.defineProperty(window, 'localStorage', {get(){ throw new Error('denied') }});"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/")
    page.wait_for_selector(".fav-btn", state="attached")
    page.locator(".fav-btn").first.click()
    page.wait_for_timeout(200)
    assert not errors, f"存储不可用时不该报错：{errors}"


def test_empty_favorite_filter_explains_itself(page, base):
    page.goto(base + "/?read=fav")
    page.wait_for_timeout(1200)
    empty = page.locator(".read-empty")
    if empty.count():
        assert "收藏" in empty.inner_text(), empty.inner_text()


CASES = [
    (test_home_renders_star_buttons, False, {}),
    (test_toggle_persists_across_reload, False, {}),
    (test_filter_shows_only_favorites, True, {}),
    (test_filter_deep_link, True, {}),
    (test_unfavorite_leaves_the_filter, True, {}),
    (test_article_star_matches_home, True, {}),
    (test_no_js_keeps_page_clean, True, {"java_script_enabled": False}),
    (test_storage_denied_is_silent, True, {}),
]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_case(browser, fn, base, needs_context, context_kwargs):
    opts = {"viewport": {"width": 1100, "height": 900}, **context_kwargs}
    ctx = browser.new_context(**opts)
    try:
        if needs_context:
            fn(ctx, base)
        else:
            fn(ctx.new_page(), base)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {fn.__name__}: {exc}")
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
