#!/usr/bin/env python3
"""登录/退出的浏览器测试：起一个临时 notesd，用 UI 登录 Root。

先 `hugo --minify`，再跑：`python3 scripts/tests/test_accounts.py`
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "server"))
import notesd  # noqa: E402

AUTH_KEY = "jasoncai:auth:v1"
DESKTOP = {"viewport": {"width": 1280, "height": 900}}
API = ""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url, timeout=15.0):
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    raise RuntimeError(f"没起来: {url}")


def login_as(page, base, username="Root", password="root-pass-1"):
    page.goto(base)
    page.click(".account-btn")
    page.fill(".auth-dialog input[name=username]", username)
    page.fill(".auth-dialog input[name=password]", password)
    page.click(".auth-dialog button[type=submit]")
    page.wait_for_function(
        f"() => Boolean(JSON.parse(localStorage.getItem({AUTH_KEY!r}) || 'null'))", timeout=10000
    )
    page.wait_for_load_state("load")


def test_login_and_logout(page, base):
    login_as(page, base)
    page.wait_for_selector(".account-bar .account-name")
    assert "Root" in page.locator(".account-bar").inner_text()
    uid = page.evaluate(f"() => JSON.parse(localStorage.getItem({AUTH_KEY!r})).id")
    page.click(".account-bar .account-logout")
    page.wait_for_function(f"() => !localStorage.getItem({AUTH_KEY!r})")
    page.wait_for_timeout(300)
    for base_key in ("jasoncai:read:v2", "jasoncai:fav:v2", "jasoncai:notes:v2"):
        assert page.evaluate("([k, u]) => !localStorage.getItem(k + ':u' + u)", [base_key, uid]), base_key


def test_bad_password_shows_message_without_crash(page, base):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base)
    page.click(".account-btn")
    page.fill(".auth-dialog input[name=username]", "Root")
    page.fill(".auth-dialog input[name=password]", "wrong")
    page.click(".auth-dialog button[type=submit]")
    page.wait_for_selector(".auth-dialog .auth-error")
    assert "用户名或密码" in page.locator(".auth-dialog .auth-error").inner_text()
    assert not errors, errors


CASES = [
    (test_login_and_logout, False),
    (test_bad_password_shows_message_without_crash, False),
]


def run_case(browser, fn, base, needs_context):
    ctx = browser.new_context(**DESKTOP)
    ctx.add_init_script(f"window.__NOTES_API__ = {json.dumps(API)};")
    try:
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
    web_port, api_port = free_port(), free_port()
    base = f"http://127.0.0.1:{web_port}"
    API = f"http://127.0.0.1:{api_port}/api"

    tmp = tempfile.TemporaryDirectory()
    db = os.path.join(tmp.name, "accounts.db")
    srv = subprocess.Popen(
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
        wait_for(base + "/")
        wait_for(API + "/healthz")
        notesd.Store(db).create_user("Root", "root-pass-1", "admin")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for fn, needs_context in CASES:
                if not run_case(browser, fn, base, needs_context):
                    failures += 1
            browser.close()
    finally:
        srv.terminate()
        web.terminate()
        tmp.cleanup()

    print(f"{len(CASES) - failures}/{len(CASES)} 通过" if failures else "全部通过")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
