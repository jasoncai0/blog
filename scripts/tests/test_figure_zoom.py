#!/usr/bin/env python3
"""图的显示方式：一律收进栏宽（不横向滚动）+ 点「放大」开灰屏遮罩。

先 `hugo --minify` 生成 public，再跑：`python3 scripts/tests/test_figure_zoom.py`。
沿用 test_sidebar.py 的跑法。
"""

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

ARTICLE = "/posts/ot-crdt-lww.html"
DESKTOP = {"viewport": {"width": 1440, "height": 900}}
NARROW = {"viewport": {"width": 820, "height": 900}}

FIT_JS = """() => {
  const boxes = [...document.querySelectorAll('.diagram')];
  return {
    count: boxes.length,
    overflowing: boxes.filter(b => b.scrollWidth > b.clientWidth + 1).length,
    pageOverflow: document.documentElement.scrollWidth - window.innerWidth,
    widest: Math.max(...boxes.map(b => {
      const svg = b.querySelector('svg');
      return svg ? Math.round(svg.getBoundingClientRect().width) : 0;
    })),
    boxWidth: Math.round(boxes[0].clientWidth),
  };
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


def test_figures_fit_the_column_without_scrolling(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    info = page.evaluate(FIT_JS)
    assert info["count"] >= 5, f"这篇文章应有若干张图，实际 {info['count']}"
    assert info["overflowing"] == 0, f"{info['overflowing']} 张图仍在横向滚动"
    assert info["pageOverflow"] <= 0, "整页出现横向滚动条"
    assert abs(info["widest"] - info["boxWidth"]) <= 2, "图应与栏宽同宽"


def test_figures_fit_on_narrow_screen(page, base):
    page.set_viewport_size({"width": 820, "height": 900})
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    info = page.evaluate(FIT_JS)
    assert info["overflowing"] == 0, "窄屏下图仍在横向滚动"
    assert info["pageOverflow"] <= 0, "窄屏整页出现横向滚动条"
    assert info["widest"] <= info["boxWidth"] + 1


def test_zoom_button_opens_fullscreen_overlay(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    button = page.locator(".fig-zoom").first
    assert not button.is_hidden(), "放大按钮应在有图时显示"
    button.click()
    overlay = page.locator(".lb")
    overlay.wait_for(state="visible")
    box = overlay.bounding_box()
    size = page.evaluate("() => [window.innerWidth, window.innerHeight]")
    assert box["width"] >= size[0] - 1 and box["height"] >= size[1] - 1, "遮罩应铺满视口"
    svg = overlay.locator("svg").first
    rect = svg.bounding_box()
    assert rect["width"] > 0 and rect["height"] > 0
    assert rect["x"] >= 0 and rect["y"] >= 0
    assert rect["x"] + rect["width"] <= size[0] + 1, "放大后的图不该超出屏幕"
    assert rect["y"] + rect["height"] <= size[1] + 1
    assert page.evaluate("() => getComputedStyle(document.documentElement).overflow") == "hidden"


def test_escape_and_backdrop_close_the_overlay(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    page.locator(".fig-zoom").first.click()
    page.locator(".lb").wait_for(state="visible")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.querySelector('.lb').hidden"), "Esc 应关闭遮罩"
    assert page.evaluate("() => getComputedStyle(document.documentElement).overflow") != "hidden"

    page.locator(".fig-zoom").first.click()
    page.locator(".lb").wait_for(state="visible")
    page.mouse.click(6, 6)
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.querySelector('.lb').hidden"), "点遮罩应关闭"


def test_overlay_shows_the_figure_caption(page, base):
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    page.locator(".fig-zoom").first.click()
    page.locator(".lb").wait_for(state="visible")
    caption = page.locator(".lb-cap").inner_text()
    assert "图 1" in caption, f"遮罩里应带图注，实际 {caption!r}"
    close = page.locator(".lb-close")
    assert close.is_visible()
    close.click()
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.querySelector('.lb').hidden")


def test_no_js_keeps_figure_visible_and_button_hidden(ctx, base):
    page = ctx.new_page()
    page.goto(base + ARTICLE)
    page.wait_for_timeout(300)
    info = page.evaluate(FIT_JS)
    assert info["overflowing"] == 0
    assert page.locator(".fig-zoom").first.is_hidden(), "没有 JS 时不该出现点了没反应的按钮"


CASES = [
    (test_figures_fit_the_column_without_scrolling, False, DESKTOP),
    (test_figures_fit_on_narrow_screen, False, NARROW),
    (test_zoom_button_opens_fullscreen_overlay, False, DESKTOP),
    (test_escape_and_backdrop_close_the_overlay, False, DESKTOP),
    (test_overlay_shows_the_figure_caption, False, DESKTOP),
    (test_no_js_keeps_figure_visible_and_button_hidden, True, {"java_script_enabled": False, **DESKTOP}),
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
