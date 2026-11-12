#!/usr/bin/env python3
"""侧栏（写作日历、标签、目录）与「上次读到这」标记的浏览器测试。

先 `hugo --minify` 生成 public，再跑：`python3 scripts/tests/test_sidebar.py`。
沿用 test_reading_progress.py 的跑法（自己起 http.server、Chromium、CASES 列表）。
"""

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

DESKTOP = {"viewport": {"width": 1200, "height": 900}}


def wait_for_server(url, timeout=15.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except Exception as exc:
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"服务器没起来: {url}: {last}")


def test_home_tags_live_in_rail(page, base):
    page.goto(base + "/")
    aside = page.locator("aside.rail-home")
    assert aside.count() == 1, "首页应有 aside.rail-home"
    chips = aside.locator("a.tag-chip")
    assert chips.count() >= 15, "左栏应有标签云"
    assert aside.locator("a.tag-chip .count").count() == chips.count()
    assert page.locator("header nav.tag-cloud").count() == 0, "顶部不应再有 tag 云"


def test_home_rail_is_left_of_list_on_desktop(page, base):
    page.goto(base + "/")
    rail = page.locator("aside.rail-home").bounding_box()
    posts = page.locator("ul.posts").bounding_box()
    assert rail["x"] + rail["width"] <= posts["x"] + 1


def test_home_rail_stacks_below_900(page, base):
    page.set_viewport_size({"width": 880, "height": 800})
    page.goto(base + "/")
    rail = page.locator("aside.rail-home").bounding_box()
    posts = page.locator("ul.posts").bounding_box()
    assert rail["y"] < posts["y"]


CASES = [
    (test_home_tags_live_in_rail, False, DESKTOP),
    (test_home_rail_is_left_of_list_on_desktop, False, DESKTOP),
    (test_home_rail_stacks_below_900, False, DESKTOP),
]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_case(browser, fn, base, needs_context, context_kwargs):
    opts = {"viewport": {"width": 1200, "height": 900}, **context_kwargs}
    ctx = browser.new_context(**opts)
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
