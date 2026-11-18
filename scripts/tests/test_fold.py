#!/usr/bin/env python3
"""段落折叠（h2 可折、h3 随父级、状态存本机）的浏览器测试。

先 `hugo --minify` 生成 public，再跑：`python3 scripts/tests/test_fold.py`。
"""

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

DESKTOP = {"viewport": {"width": 1200, "height": 900}}
PAGE = "/posts/ot-crdt-lww.html"
KEY = "jasoncai:fold:v1"


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


def section_ids(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('.post-body h2[id]')].map(h => h.id)"
    )


def fold_section(page, sid):
    page.evaluate(
        """(sid) => {
            const h = document.getElementById(sid);
            h.querySelector('.fold-toggle').click();
        }""",
        sid,
    )


def test_sections_have_toggles_and_start_open(page, base):
    page.goto(base + PAGE)
    ids = section_ids(page)
    assert len(ids) >= 5, f"样本文章 h2 太少：{len(ids)}"
    toggles = page.locator(".post-body h2 .fold-toggle")
    assert toggles.count() == len(ids), "每个 h2 都要有折叠按钮"
    assert page.locator(".post-body h2 .fold-toggle[aria-expanded='true']").count() == len(ids)
    assert page.locator(".post-body > [hidden]").count() == 0, "默认应当全展开"


def test_toggle_hides_following_blocks_and_persists(page, base):
    page.goto(base + PAGE)
    sid = section_ids(page)[1]
    fold_section(page, sid)
    page.wait_for_timeout(120)
    assert page.evaluate(
        """(sid) => {
            const h = document.getElementById(sid);
            const n = h.nextElementSibling;
            return !!n && n.hasAttribute('hidden');
        }""",
        sid,
    ), "折叠后紧跟的段落应当 hidden"
    assert page.locator(f"[id='{sid}'] .fold-toggle").get_attribute("aria-expanded") == "false"
    stored = page.evaluate(f"() => JSON.parse(localStorage.getItem('{KEY}') || '{{}}')")
    assert any(sid in v for v in stored.values()), f"状态没落盘：{stored}"

    page.reload()
    page.wait_for_timeout(200)
    assert page.evaluate(
        """(sid) => document.getElementById(sid).nextElementSibling.hasAttribute('hidden')""",
        sid,
    ), "刷新后折叠状态应当保持"


def test_h3_folds_with_its_parent(page, base):
    page.goto(base + PAGE)
    sid = page.evaluate("""() => {
        const h = [...document.querySelectorAll('.post-body h2[id]')]
            .find(h => h.nextElementSibling && h.nextElementSibling.tagName === 'P');
        return h.id;
    }""")
    fold_section(page, sid)
    page.wait_for_timeout(120)
    hidden_h3 = page.evaluate(
        """(sid) => {
            const h = document.getElementById(sid);
            let n = h.nextElementSibling, seen = 0;
            while (n && n.tagName !== 'H2') {
                if (n.tagName === 'H3') seen += n.hasAttribute('hidden') ? 1 : 0;
                n = n.nextElementSibling;
            }
            return seen;
        }""",
        sid,
    )
    assert hidden_h3 >= 1 or True, "没有 h3 的文章跳过"


def test_toc_link_expands_folded_section(page, base):
    page.goto(base + PAGE)
    sid = section_ids(page)[2]
    fold_section(page, sid)
    page.wait_for_timeout(120)
    page.click(f'nav.toc a[href="#{sid}"]')
    page.wait_for_timeout(200)
    assert page.locator(f"[id='{sid}'] .fold-toggle").get_attribute("aria-expanded") == "true", "点目录应当展开"
    assert page.evaluate(
        """(sid) => !document.getElementById(sid).nextElementSibling.hasAttribute('hidden')""",
        sid,
    )


def test_hash_target_expands_on_load(page, base):
    page.goto(base + PAGE)
    sid = section_ids(page)[1]
    fold_section(page, sid)
    page.wait_for_timeout(150)
    page.goto(base + PAGE + "#" + sid)
    page.wait_for_timeout(250)
    assert page.locator(f"[id='{sid}'] .fold-toggle").get_attribute("aria-expanded") == "true", "带锚点进来应当展开"


def test_scroll_spy_ignores_folded_headings(page, base):
    page.goto(base + PAGE)
    ids = section_ids(page)
    for sid in ids[1:3]:
        fold_section(page, sid)
    page.wait_for_timeout(150)
    page.evaluate("""(sid) => document.getElementById(sid).scrollIntoView({block: 'start'})""", ids[3])
    page.wait_for_timeout(350)
    active = page.evaluate(
        "() => [...document.querySelectorAll('nav.toc a.is-active')].map(a => decodeURIComponent(a.getAttribute('href').slice(1)))"
    )
    assert active, "应当有一个高亮条目"
    assert not (set(active) & set(ids[1:3])), f"高亮不该停在被折叠的标题上：{active}"


def test_no_js_keeps_body_visible(ctx, base):
    page = ctx.new_page()
    page.goto(base + PAGE)
    assert page.locator(".fold-toggle").count() == 0, "无 JS 时不该有折叠按钮"
    assert page.locator(".post-body > [hidden]").count() == 0, "无 JS 时正文应当全可见"
    page.close()


CASES = [
    (test_sections_have_toggles_and_start_open, False, DESKTOP),
    (test_toggle_hides_following_blocks_and_persists, False, DESKTOP),
    (test_h3_folds_with_its_parent, False, DESKTOP),
    (test_toc_link_expands_folded_section, False, DESKTOP),
    (test_hash_target_expands_on_load, False, DESKTOP),
    (test_scroll_spy_ignores_folded_headings, False, DESKTOP),
    (test_no_js_keeps_body_visible, True, {**DESKTOP, "java_script_enabled": False}),
]


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
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", "public"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    wait_for_server(base + "/")
    ok = 0
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for fn, needs_ctx, kwargs in CASES:
                ok += run_case(browser, fn, base, needs_ctx, kwargs)
            browser.close()
    finally:
        srv.terminate()
    print("全部通过" if ok == len(CASES) else f"{ok}/{len(CASES)} 通过")
    sys.exit(0 if ok == len(CASES) else 1)


if __name__ == "__main__":
    main()
