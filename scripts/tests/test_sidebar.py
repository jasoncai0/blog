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


def test_calendar_renders_year_grid(page, base):
    page.goto(base + "/")
    weeks = page.locator("nav.calendar .cal-week")
    assert weeks.count() >= 52, f"整年应有 52+ 列，实际 {weeks.count()}"
    day = "2026-11-11"
    n = page.locator(f'ul.posts li[data-date="{day}"]').count()
    cell = page.locator(f'a.heat-cell[data-date="{day}"]')
    assert cell.count() == 1
    assert cell.get_attribute("data-count") == str(n)
    assert "lv4" in cell.get_attribute("class")


def test_calendar_empty_days_are_not_links(page, base):
    page.goto(base + "/")
    assert page.locator('span.heat-cell.is-empty[data-date="2026-03-03"]').count() == 1
    assert page.locator('a.heat-cell[data-date="2026-03-03"]').count() == 0


def test_list_items_carry_dates(page, base):
    page.goto(base + "/")
    assert page.locator("ul.posts li").count() == page.locator("ul.posts li[data-date]").count()
    assert page.locator('ul.posts li[data-date="2026-11-11"]').count() >= 5


def test_only_recent_12_weeks_visible_by_default(page, base):
    page.goto(base + "/")
    assert page.locator("nav.calendar .cal-week:not(.is-hidden)").count() == 12


def test_year_toggle_shows_all_weeks(page, base):
    page.goto(base + "/")
    page.click('[data-cal-range="year"]')
    assert page.locator("nav.calendar .cal-week:not(.is-hidden)").count() > 50


def test_clicking_a_day_flashes_that_day(page, base):
    page.goto(base + "/")
    day = "2026-11-11"
    n = page.locator(f'ul.posts li[data-date="{day}"]').count()
    page.locator(f'a.heat-cell[data-date="{day}"]').click()
    page.wait_for_selector(f'ul.posts li[data-date="{day}"].is-flash')
    page.wait_for_timeout(350)  # 等平滑滚动起步
    assert page.locator("ul.posts li.is-flash").count() == n
    assert page.evaluate("window.scrollY") > 0


def test_date_input_moves_window_and_marks_target(page, base):
    page.goto(base + "/")
    page.fill("input.cal-jump", "2026-11-09")
    page.wait_for_timeout(200)
    assert page.locator(".cal-week.is-target").count() == 1
    assert page.locator('a.heat-cell[data-date="2026-11-09"]').is_visible()


def test_article_toc_in_left_rail(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    assert page.locator("aside.rail-article nav.toc").count() == 1
    rail_box = page.locator("aside.rail-article").bounding_box()
    body_box = page.locator(".post-body").bounding_box()
    assert rail_box["x"] + rail_box["width"] <= body_box["x"] + 1


def test_toc_collapse_persists(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    page.click(".toc-toggle")
    assert page.locator("aside.rail-article.is-collapsed").count() == 1
    page.reload()
    assert page.locator("aside.rail-article.is-collapsed").count() == 1


def test_rail_absent_where_there_is_no_toc(page, base):
    page.goto(base + "/tags/agent.html")
    assert page.locator("aside.rail-article").count() == 0
    assert page.locator("ul.posts li").count() > 0


def test_active_section_follows_scroll(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    target = page.locator(".post-body h2").nth(2)
    # scroll_into_view_if_needed 只保证「可见」，标题停在视口 430px 处时上一节仍是当前节；
    # 要断言高亮就得把标题顶到视口顶部附近。
    target.evaluate("el => el.scrollIntoView({block: 'start'})")
    # html{scroll-behavior:smooth} 让这段滚动要 ~1s，等到标题真的贴近顶部再看高亮
    page.wait_for_function(
        "el => el.getBoundingClientRect().top < 130", arg=target.element_handle()
    )
    page.wait_for_timeout(120)
    active = page.locator("nav.toc a.is-active")
    assert active.count() == 1
    assert active.get_attribute("href").endswith("#" + target.get_attribute("id"))


CASES = [
    (test_home_tags_live_in_rail, False, DESKTOP),
    (test_home_rail_is_left_of_list_on_desktop, False, DESKTOP),
    (test_home_rail_stacks_below_900, False, DESKTOP),
    (test_calendar_renders_year_grid, False, DESKTOP),
    (test_calendar_empty_days_are_not_links, False, DESKTOP),
    (test_list_items_carry_dates, False, DESKTOP),
    (test_only_recent_12_weeks_visible_by_default, False, DESKTOP),
    (test_year_toggle_shows_all_weeks, False, DESKTOP),
    (test_clicking_a_day_flashes_that_day, False, DESKTOP),
    (test_date_input_moves_window_and_marks_target, False, DESKTOP),
    (test_article_toc_in_left_rail, False, DESKTOP),
    (test_toc_collapse_persists, False, DESKTOP),
    (test_rail_absent_where_there_is_no_toc, False, DESKTOP),
    (test_active_section_follows_scroll, False, DESKTOP),
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
