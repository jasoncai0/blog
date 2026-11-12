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
WIDE = {"viewport": {"width": 1440, "height": 900}}


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


def seed_progress(page, base, percent=62):
    page.goto(base + "/")
    page.evaluate(
        """p => localStorage.setItem('jasoncai:read:v1',
             JSON.stringify({'ot-crdt-lww': {p, t: Date.now(), d: false}}))""",
        percent,
    )


def test_marker_shows_where_reading_stopped(page, base):
    seed_progress(page, base)
    page.goto(base + "/posts/ot-crdt-lww.html#keep")  # 带锚点=不触发续读
    page.wait_for_selector(".rp-marker")
    assert "62%" in page.locator(".rp-marker").inner_text()


def test_marker_disappears_after_scrolling_past(page, base):
    seed_progress(page, base)
    page.goto(base + "/posts/ot-crdt-lww.html#keep")
    page.wait_for_selector(".rp-marker")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(900)
    assert page.locator(".rp-marker").count() == 0


def test_no_marker_when_resume_jumped(page, base):
    seed_progress(page, base)
    page.goto(base + "/posts/ot-crdt-lww.html")
    page.wait_for_timeout(600)
    assert page.locator(".rp-marker").count() == 0


def test_no_js_home_keeps_rail_content(ctx, base):
    page = ctx.new_page()
    page.goto(base + "/")
    assert page.locator("aside.rail-home nav.calendar").count() == 1
    assert page.locator("aside.rail-home a.tag-chip").count() >= 15
    assert page.locator("ul.posts li").count() > 50
    assert not page.locator(".cal-controls").is_visible(), "无 JS 时不显示日历控件"


def test_no_js_article_keeps_toc_but_hides_toggle(ctx, base):
    page = ctx.new_page()
    page.goto(base + "/posts/ot-crdt-lww.html")
    assert page.locator("nav.toc a").count() > 3
    assert not page.locator(".toc-toggle").is_visible(), "无 JS 时不显示折叠按钮"


def test_collapsed_rail_centers_the_article(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    page.wait_for_timeout(250)
    before = page.locator(".main-col").bounding_box()
    page.click(".toc-toggle")
    page.wait_for_timeout(450)
    after = page.locator(".main-col").bounding_box()
    rail = page.locator("aside.rail-article").bounding_box()
    assert abs(after["width"] - before["width"]) < 8, "收起目录后正文不该被拉宽"
    page_center = page.evaluate("document.documentElement.clientWidth / 2")
    assert abs(after["x"] + after["width"] / 2 - page_center) < 10, "正文列应水平居中"
    assert rail["x"] + rail["width"] <= after["x"] + 1, "窄轨不该压到正文"


def test_expanded_rail_keeps_article_centered(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    page.wait_for_timeout(250)
    main = page.locator(".main-col").bounding_box()
    rail = page.locator("aside.rail-article").bounding_box()
    center = page.evaluate("document.documentElement.clientWidth / 2")
    assert abs(main["x"] + main["width"] / 2 - center) < 10, "展开时正文也应居中"
    assert rail["x"] + rail["width"] <= main["x"] + 1, "目录不该压到正文"
    assert abs(main["width"] - 848) <= 2, "宽屏正文行宽 848（沿用原来的文档尺寸）"


def test_collapse_keeps_article_in_place_on_wide(page, base):
    page.goto(base + "/posts/ot-crdt-lww.html")
    page.wait_for_timeout(250)
    before = page.locator(".main-col").bounding_box()
    page.click(".toc-toggle")
    page.wait_for_timeout(450)
    after = page.locator(".main-col").bounding_box()
    assert abs(after["x"] - before["x"]) < 2 and abs(after["width"] - before["width"]) < 2


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
    (test_marker_shows_where_reading_stopped, False, DESKTOP),
    (test_marker_disappears_after_scrolling_past, False, DESKTOP),
    (test_no_marker_when_resume_jumped, False, DESKTOP),
    (test_no_js_home_keeps_rail_content, True, {**DESKTOP, "java_script_enabled": False}),
    (test_no_js_article_keeps_toc_but_hides_toggle, True, {**DESKTOP, "java_script_enabled": False}),
    (test_collapsed_rail_centers_the_article, False, DESKTOP),
    (test_expanded_rail_keeps_article_centered, False, WIDE),
    (test_collapse_keeps_article_in_place_on_wide, False, WIDE),
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
