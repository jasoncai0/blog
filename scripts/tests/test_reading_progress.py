#!/usr/bin/env python3
"""文章页阅读进度的浏览器测试。

先 `hugo --minify` 生成 public，再跑本脚本；脚本自己起 http.server，
用 Chromium 打开文章页，检查进度条、胶囊文字与 localStorage 落盘。
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


def read_store(page):
    raw = page.evaluate(f"localStorage.getItem({KEY!r})")
    assert raw, "localStorage 里没有进度记录"
    return json.loads(raw)


def test_article_scroll_records_progress(page, base):
    page.goto(base + "/posts/hello.html")
    page.wait_for_selector(".rp-bar")
    assert page.locator(".rp-bar").count() == 1, "应当只注入一个进度条"
    assert page.eval_on_selector(".rp-chip", "e => e.textContent").strip() in {"0%", "1%", "2%"}
    assert page.eval_on_selector(".rp-chip", "e => e.hasAttribute('hidden')"), "顶部胶囊应隐藏"
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


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", "public"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures = []
    try:
        wait_for_server(base + "/posts/hello.html")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page_tests = [test_article_scroll_records_progress, test_chip_appears_after_scrolling]
            for fn in page_tests:
                ctx = browser.new_context(viewport={"width": 900, "height": 800})
                page = ctx.new_page()
                try:
                    fn(page, base)
                    print(f"PASS {fn.__name__}")
                except Exception as exc:
                    failures.append(fn.__name__)
                    print(f"FAIL {fn.__name__}: {exc}")
                finally:
                    ctx.close()
            ctx = browser.new_context(viewport={"width": 900, "height": 800})
            try:
                test_two_pages_merge(ctx, base)
                print("PASS test_two_pages_merge")
            except Exception as exc:
                failures.append("test_two_pages_merge")
                print(f"FAIL test_two_pages_merge: {exc}")
            finally:
                ctx.close()
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=5)
    if failures:
        print(f"{len(failures)} 个用例失败")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
