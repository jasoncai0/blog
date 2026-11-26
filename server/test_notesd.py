#!/usr/bin/env python3
"""notesd 的接口测试：起一个真进程内的服务，打真实 HTTP。

    python3 server/test_notesd.py
"""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

import notesd


class NotesdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(cls.tmp.name, "notes.db")
        notesd.Handler.store = notesd.Store(db)
        notesd.Handler.limiter = notesd.Limiter()
        notesd.Handler.login_limiter = notesd.Limiter()
        notesd.Handler.edit_key = ""
        notesd.Handler.store.create_user("Root", "root-pass-1", "admin")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), notesd.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        notesd.Handler.store.close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, headers=None, noauth=False):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        # 登录之后 /api 都要 Bearer；用例默认带 Root，专门的未登录用例传 noauth=True。
        if not noauth and "Authorization" not in hdrs and path.startswith("/api") and not path.startswith("/api/login"):
            hdrs["Authorization"] = f"Bearer {self.admin_token()}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def setUp(self):
        # 限速按进程共享计数，每个用例从干净配额开始，避免顺序依赖。
        notesd.Handler.login_limiter = notesd.Limiter()
        notesd.Handler.limiter = notesd.Limiter()
        self._admin = None

    def seed_user(self, username, password, role="user"):
        return notesd.Handler.store.create_user(username, password, role)

    def token_for(self, username, password):
        _, body = self.call("POST", "/api/login", {"username": username, "password": password})
        return body["token"]

    def admin_token(self):
        # 每个用例只登录一次：登录接口每分钟限 10 次，逐次取 token 会把自己限掉。
        if not self._admin:
            self._admin = self.token_for("Root", "root-pass-1")
        return self._admin

    def auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_login_and_me(self):
        _, body = self.call("POST", "/api/login", {"username": "root", "password": "root-pass-1"})
        self.assertEqual(body["user"]["role"], "admin")
        status, me = self.call("GET", "/api/me", headers=self.auth(body["token"]))
        self.assertEqual((status, me["user"]["username"]), (200, "Root"))

    def test_login_rejects_bad_password(self):
        status, body = self.call("POST", "/api/login", {"username": "Root", "password": "nope"})
        self.assertEqual((status, body["error"]), (401, "bad credentials"))

    def test_login_is_rate_limited(self):
        for _ in range(10):
            self.call("POST", "/api/login", {"username": "x", "password": "y"})
        self.assertEqual(self.call("POST", "/api/login", {"username": "x", "password": "y"})[0], 429)

    def test_admin_can_create_user_and_duplicate_is_409(self):
        token = self.admin_token()
        self.assertEqual(self.call("POST", "/api/users", {"username": "Alice", "password": "alice-pass"},
                                   self.auth(token))[0], 200)
        self.assertEqual(self.call("POST", "/api/users", {"username": "alice", "password": "alice-pass-2"},
                                   self.auth(token))[0], 409)

    def test_non_admin_cannot_create_user(self):
        self.seed_user("Alice", "alice-pass", "user")
        token = self.token_for("Alice", "alice-pass")
        self.assertEqual(self.call("POST", "/api/users", {"username": "Bob", "password": "bob-pass"},
                                   self.auth(token))[0], 403)

    def test_change_password_updates_login(self):
        self.seed_user("BobPw", "bobpw-pass-1")
        token = self.token_for("BobPw", "bobpw-pass-1")
        status, _ = self.call("POST", "/api/password", {"old_password": "bobpw-pass-1", "new_password": "bobpw-pass-2"}, self.auth(token))
        self.assertEqual(status, 204)
        self.assertEqual(self.token_for("BobPw", "bobpw-pass-2") is not None, True)
        status, body = self.call("POST", "/api/login", {"username": "BobPw", "password": "bobpw-pass-1"})
        self.assertEqual(status, 401)

    def test_change_password_checks_old_and_length(self):
        self.seed_user("Cara", "cara-pass-1")
        token = self.token_for("Cara", "cara-pass-1")
        status, body = self.call("POST", "/api/password", {"old_password": "nope", "new_password": "cara-pass-2"}, self.auth(token))
        self.assertEqual((status, body["error"]), (400, "wrong password"))
        status, body = self.call("POST", "/api/password", {"old_password": "cara-pass-1", "new_password": "short"}, self.auth(token))
        self.assertEqual((status, body["error"]), (400, "password too short"))
        status, _ = self.call(
            "POST", "/api/password", {"old_password": "cara-pass-1", "new_password": "cara-pass-2"}, noauth=True
        )
        self.assertEqual(status, 401)

    def test_change_password_signs_out_other_devices(self):
        self.seed_user("Dana", "dana-pass-1")
        phone = self.token_for("Dana", "dana-pass-1")
        laptop = self.token_for("Dana", "dana-pass-1")
        status, _ = self.call("POST", "/api/password", {"old_password": "dana-pass-1", "new_password": "dana-pass-2"}, self.auth(laptop))
        self.assertEqual(status, 204)
        self.assertEqual(self.call("GET", "/api/me", headers=self.auth(laptop))[0], 200)
        self.assertEqual(self.call("GET", "/api/me", headers=self.auth(phone))[0], 401)

    def test_change_password_keeps_bridge_token(self):
        self.seed_user("Eve", "eve-pass-1")
        token = self.token_for("Eve", "eve-pass-1")
        bridge = notesd.Handler.store.create_token(notesd.Handler.store.user_by_name("Eve")["id"], "bridge", 0)
        status, _ = self.call("POST", "/api/password", {"old_password": "eve-pass-1", "new_password": "eve-pass-2"}, self.auth(token))
        self.assertEqual(status, 204)
        self.assertEqual(self.call("GET", "/api/me", headers=self.auth(bridge))[0], 200)

    def test_logout_revokes_token(self):
        token = self.admin_token()
        self.assertEqual(self.call("POST", "/api/logout", {}, self.auth(token))[0], 204)
        self.assertEqual(self.call("GET", "/api/me", headers=self.auth(token))[0], 401)

    def note(self, note_id, q="收敛是必要条件", block=4):
        return {
            "id": note_id,
            "q": q,
            "pre": "",
            "post": "",
            "block": block,
            "t": 1_700_000_000_000,
            "thread": [{"author": "我", "text": "这里要补一个反例", "t": 1_700_000_000_000}],
        }

    def test_healthz(self):
        status, body = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_put_then_get_returns_note(self):
        status, _ = self.call("PUT", "/api/notes/ot-crdt-lww/n1abcde", self.note("n1abcde"))
        self.assertEqual(status, 200)
        status, body = self.call("GET", "/api/notes/ot-crdt-lww")
        self.assertEqual(status, 200)
        self.assertEqual([n["id"] for n in body["notes"]], ["n1abcde"])
        self.assertEqual(body["notes"][0]["thread"][0]["author"], "我")

    def test_rejects_empty_quote_and_bad_id(self):
        bad = self.note("n2abcde", q="")
        self.assertEqual(self.call("PUT", "/api/notes/ot-crdt-lww/n2abcde", bad)[0], 400)
        self.assertEqual(self.call("PUT", "/api/notes/ot-crdt-lww/!!", self.note("n3abcde"))[0], 400)
        self.assertEqual(self.call("PUT", "/api/notes/ot-crdt-lww/n4abcde", {"id": "n4abcde"})[0], 400)

    def test_delete_leaves_tombstone(self):
        self.call("PUT", "/api/notes/ot-crdt-lww/n5abcde", self.note("n5abcde"))
        status, body = self.call("DELETE", "/api/notes/ot-crdt-lww/n5abcde")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        _, listing = self.call("GET", "/api/notes/ot-crdt-lww")
        self.assertNotIn("n5abcde", [n["id"] for n in listing["notes"]])
        self.assertIn("n5abcde", listing["deleted"])

    def test_bulk_notes_returns_one_bundle_per_slug(self):
        self.call("PUT", "/api/notes/bulk-a/nb1abcde", self.note("nb1abcde", q="甲"))
        self.call("PUT", "/api/notes/bulk-a/nb2abcde", self.note("nb2abcde", q="乙"))
        self.call("PUT", "/api/notes/bulk-b/nb3abcde", self.note("nb3abcde", q="丙"))
        self.call("DELETE", "/api/notes/bulk-a/nb2abcde")
        status, body = self.call("GET", "/api/notes?slugs=bulk-a,bulk-b,bulk-c")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]["bulk-a"]["notes"]), 1)
        self.assertEqual(body["items"]["bulk-a"]["deleted"], ["nb2abcde"])
        self.assertEqual(len(body["items"]["bulk-b"]["notes"]), 1)
        self.assertEqual(body["items"]["bulk-c"], {"notes": [], "deleted": []})

    def test_bulk_notes_rejects_bad_slug(self):
        status, body = self.call("GET", "/api/notes?slugs=ok-slug,Bad_Slug")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad slug")

    def test_summary_counts_live_notes_only(self):
        self.call("PUT", "/api/notes/tally-x/n6abcde", self.note("n6abcde"))
        self.call("PUT", "/api/notes/tally-x/n7abcde", self.note("n7abcde"))
        self.call("DELETE", "/api/notes/tally-x/n7abcde")
        _, body = self.call("GET", "/api/summary")
        self.assertEqual(body["notes"].get("tally-x"), 1)

    def test_progress_never_regresses(self):
        self.call("PUT", "/api/progress/regress", {"p": 80, "t": 1000, "d": False})
        _, body = self.call("PUT", "/api/progress/regress", {"p": 40, "t": 2000, "d": False})
        self.assertEqual(body["p"], 80, "后到的低进度不能覆盖已经上报过的 80%")
        self.assertEqual(body["t"], 2000)
        _, body = self.call("PUT", "/api/progress/regress", {"p": 30, "t": 3000, "d": False})
        self.assertEqual(body["p"], 80)
        self.call("PUT", "/api/progress/regress2", {"p": 100, "t": 1000, "d": True})
        _, body = self.call("PUT", "/api/progress/regress2", {"p": 10, "t": 1500, "d": False})
        self.assertEqual(body["d"], True, "读完标记不能因为之后的低进度被撤销")
        self.assertEqual(body["p"], 100)

    def test_progress_merges_max_and_done_sticks(self):
        self.assertEqual(self.call("PUT", "/api/progress/hello", {"p": 40, "t": 100, "d": False})[0], 200)
        _, body = self.call("PUT", "/api/progress/hello", {"p": 20, "t": 90, "d": False})
        self.assertEqual(body["p"], 40)
        self.assertEqual(body["t"], 100)
        _, body = self.call("PUT", "/api/progress/hello", {"p": 50, "t": 110, "d": True})
        self.assertTrue(body["d"])
        _, after = self.call("PUT", "/api/progress/hello", {"p": 60, "t": 120, "d": False})
        self.assertTrue(after["d"])
        self.assertEqual(after["p"], 60)

    def test_put_cannot_drop_appended_floors(self):
        self.call("PUT", "/api/notes/floor-keep/floorabc", self.note("floorabc"))
        self.call("POST", "/api/notes/floor-keep/floorabc/replies", {"author": "AI", "text": "AI 的回答"})
        stale = self.note("floorabc")
        self.assertEqual(self.call("PUT", "/api/notes/floor-keep/floorabc", stale)[0], 200)
        _, body = self.call("GET", "/api/notes/floor-keep")
        note = next(n for n in body["notes"] if n["id"] == "floorabc")
        self.assertEqual(len(note["thread"]), 2, "旧的整条 PUT 不能抹掉已经追加的楼层")
        self.assertEqual(note["thread"][1]["author"], "AI")

    def test_favorites_roundtrip(self):
        self.assertEqual(self.call("PUT", "/api/favorites/ot-crdt-lww", {"t": 111})[0], 200)
        self.assertEqual(self.call("PUT", "/api/favorites/hello", {})[0], 200)
        _, body = self.call("GET", "/api/favorites")
        self.assertEqual(set(body["favorites"]), {"ot-crdt-lww", "hello"})
        self.assertIsInstance(body["favorites"]["hello"], int)
        self.assertEqual(self.call("DELETE", "/api/favorites/hello")[0], 200)
        _, body = self.call("GET", "/api/favorites")
        self.assertEqual(set(body["favorites"]), {"ot-crdt-lww"})

    def test_two_users_have_separate_notes(self):
        self.seed_user("Alice", "alice-pass")
        self.seed_user("Bob", "bob-pass")
        ta = self.token_for("Alice", "alice-pass")
        tb = self.token_for("Bob", "bob-pass")
        self.call("PUT", "/api/notes/user-scope/a1note", self.note("a1note"), self.auth(ta))
        self.assertEqual(self.call("GET", "/api/notes/user-scope", headers=self.auth(tb))[1]["notes"], [])
        self.assertEqual(len(self.call("GET", "/api/notes/user-scope", headers=self.auth(ta))[1]["notes"]), 1)

    def test_put_cannot_overwrite_another_users_note(self):
        self.seed_user("Alice", "alice-pass")
        self.seed_user("Bob", "bob-pass")
        ta = self.token_for("Alice", "alice-pass")
        tb = self.token_for("Bob", "bob-pass")
        self.call("PUT", "/api/notes/user-scope/a1note", self.note("a1note"), self.auth(ta))
        stolen = self.note("a1note", q="别人的笔记")
        self.assertEqual(self.call("PUT", "/api/notes/user-scope/a1note", stolen, self.auth(tb))[0], 404)

    def test_progress_and_favorites_are_per_user(self):
        self.seed_user("Alice", "alice-pass")
        self.seed_user("Bob", "bob-pass")
        ta = self.token_for("Alice", "alice-pass")
        tb = self.token_for("Bob", "bob-pass")
        self.call("PUT", "/api/progress/user-scope", {"p": 100, "t": 1, "d": True}, self.auth(ta))
        self.call("PUT", "/api/favorites/user-scope", {}, self.auth(ta))
        _, a = self.call("GET", "/api/summary", headers=self.auth(ta))
        _, b = self.call("GET", "/api/summary", headers=self.auth(tb))
        self.assertEqual((a["progress"]["user-scope"]["p"], len(a["favorites"])), (100, 1))
        self.assertEqual((b["progress"], b["favorites"]), ({}, {}))

    def test_jobs_require_admin(self):
        self.seed_user("Alice", "alice-pass")
        ta = self.token_for("Alice", "alice-pass")
        self.assertEqual(self.call("GET", "/api/jobs?status=pending", noauth=True)[0], 401)
        self.assertEqual(self.call("GET", "/api/jobs?status=pending", headers=self.auth(ta))[0], 403)
        self.assertEqual(self.call("GET", "/api/jobs?status=pending")[0], 200)

    def test_non_admin_reply_does_not_enqueue_ai(self):
        root = self.admin_token()
        self.call("PUT", "/api/sessions/ot-crdt-lww", {"session_id": "session-abcdefgh", "workspace": "ws"}, self.auth(root))
        self.seed_user("Alice", "alice-pass")
        ta = self.token_for("Alice", "alice-pass")
        self.call("PUT", "/api/notes/ot-crdt-lww/a1note", self.note("a1note"), self.auth(ta))
        _, body = self.call("POST", "/api/notes/ot-crdt-lww/a1note/replies",
                            {"author": "我", "text": "为什么不加锁？"}, self.auth(ta))
        self.assertIsNone(body.get("job"))

    def test_summary_includes_favorites(self):
        self.call("PUT", "/api/favorites/summary-check", {"t": 222})
        _, body = self.call("GET", "/api/summary")
        self.assertEqual(body["favorites"].get("summary-check"), 222)

    def test_favorites_require_login(self):
        self.assertEqual(self.call("PUT", "/api/favorites/anonymous-ok", {"t": 333}, noauth=True)[0], 401)
        self.assertEqual(self.call("PUT", "/api/favorites/anonymous-ok", {"t": 333})[0], 200)
        # 用独立 slug 测完就删，避免污染 test_favorites_roundtrip 的全集断言。
        self.call("DELETE", "/api/favorites/anonymous-ok")

    def test_favorites_bad_slug(self):
        code, body = self.call("PUT", "/api/favorites/Bad_Slug", {"t": 1})
        self.assertEqual(code, 400)

    def test_session_accepts_absolute_workspace(self):
        code, body = self.call(
            "PUT",
            "/api/sessions/abs-ws",
            {"session_id": "session-abcdefgh", "workspace": "/Users/x/Documents/ws"},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["session"]["workspace"], "/Users/x/Documents/ws")

    def test_notes_and_progress_require_login(self):
        self.assertEqual(self.call("PUT", "/api/notes/hello/n8abcde", self.note("n8abcde"), noauth=True)[0], 401)
        self.assertEqual(self.call("PUT", "/api/notes/hello/n8abcde", self.note("n8abcde"))[0], 200)
        self.assertEqual(self.call("GET", "/api/notes/hello", noauth=True)[0], 401)
        self.assertEqual(
            self.call("PUT", "/api/progress/anon-probe", {"p": 40, "t": 1700000000000, "d": False}, noauth=True)[0],
            401,
        )

    def test_reply_appends_a_floor(self):
        slug = "thread-test"
        self.call("PUT", f"/api/notes/{slug}/thr-0001", self.note("thr-0001"))
        status, body = self.call("POST", f"/api/notes/{slug}/thr-0001/replies",
                                 {"author": "AI", "text": "这是 AI 的回复", "t": 2000})
        self.assertEqual(status, 200)
        self.assertEqual([m["author"] for m in body["note"]["thread"]], ["我", "AI"])
        _, fetched = self.call("GET", f"/api/notes/{slug}")
        got = next(n for n in fetched["notes"] if n["id"] == "thr-0001")
        self.assertEqual(len(got["thread"]), 2)
        self.assertEqual(got["thread"][1]["text"], "这是 AI 的回复")

    def test_reply_requires_an_existing_note(self):
        status, _ = self.call("POST", "/api/notes/thread-test/ghost-0001/replies",
                              {"author": "AI", "text": "hi"})
        self.assertEqual(status, 404)

    def stream(self, path, headers=None, timeout=3):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=timeout)
        hdrs = dict(headers or {})
        if "Authorization" not in hdrs:
            hdrs["Authorization"] = f"Bearer {self.admin_token()}"
        conn.request("GET", path, headers=hdrs)
        return conn, conn.getresponse()

    def first_event(self, resp, timeout=3):
        deadline = time.time() + timeout
        event, data = None, None
        while time.time() < deadline:
            line = resp.fp.readline().decode()
            if not line:
                break
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
            elif line.strip() == "" and event:
                return event, json.loads(data or "{}")
        return None, None

    def test_jobs_stream_requires_admin(self):
        conn, resp = self.stream("/api/jobs/stream", {"Authorization": ""})
        self.assertEqual(resp.status, 401)
        conn.close()
        self.seed_user("Alice", "alice-pass")
        conn, resp = self.stream("/api/jobs/stream", {"Authorization": f"Bearer {self.token_for('Alice', 'alice-pass')}"})
        self.assertEqual(resp.status, 403)
        conn.close()

    def test_jobs_stream_opens_for_admin(self):
        conn, resp = self.stream("/api/jobs/stream")
        self.assertEqual(resp.status, 200)
        conn.close()

    def test_bridge_stream_pushes_new_job(self):
        slug = "stream-jobs"
        self.register(slug)
        self.call("PUT", f"/api/notes/{slug}/job-0006", self.note("job-0006"))
        conn, resp = self.stream("/api/jobs/stream")
        self.assertEqual(resp.status, 200)
        self.call("POST", f"/api/notes/{slug}/job-0006/ask", {})
        event, data = self.first_event(resp)
        self.assertEqual(event, "job.created")
        self.assertEqual(data["slug"], slug)
        conn.close()

    def test_note_stream_pushes_reply(self):
        slug = "stream-note"
        self.call("PUT", f"/api/notes/{slug}/job-0007", self.note("job-0007"))
        conn, resp = self.stream(f"/api/notes/{slug}/stream")
        self.assertEqual(resp.status, 200)
        self.call("POST", f"/api/notes/{slug}/job-0007/replies", {"author": "AI", "text": "结论：不需要", "t": 3000})
        event, data = self.first_event(resp)
        self.assertEqual(event, "note.updated")
        self.assertEqual(data["note"]["id"], "job-0007")
        conn.close()

    def register(self, slug, sid="0f3a1c2d-1234-4abc-9def-001122334455"):
        self.call("PUT", f"/api/sessions/{slug}", {"session_id": sid, "workspace": "default-workspace"})

    def test_question_reply_creates_job(self):
        slug = "jobs-auto"
        self.register(slug)
        self.call("PUT", f"/api/notes/{slug}/job-0001", self.note("job-0001"))
        status, _ = self.call("POST", f"/api/notes/{slug}/job-0001/replies",
                              {"author": "我", "text": "这里为什么不用向量时钟？", "t": 2000})
        self.assertEqual(status, 200)
        status, body = self.call("GET", "/api/jobs?status=pending")
        self.assertEqual(status, 200)
        mine = [j for j in body["jobs"] if j["slug"] == slug]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["kind"], "auto")
        self.assertEqual(mine[0]["status"], "pending")

    def test_note_payload_carries_active_job(self):
        slug = "jobs-payload"
        self.register(slug)
        self.call("PUT", f"/api/notes/{slug}/job-0008", self.note("job-0008"))
        self.call("POST", f"/api/notes/{slug}/job-0008/ask", {})
        _, body = self.call("GET", f"/api/notes/{slug}")
        note = next(n for n in body["notes"] if n["id"] == "job-0008")
        self.assertEqual(note["job"]["status"], "pending")
        self.assertEqual(note["job"]["attempts"], 0)

    def test_note_stream_announces_job_updates(self):
        slug = "jobs-stream2"
        self.register(slug)
        self.call("PUT", f"/api/notes/{slug}/job-0009", self.note("job-0009"))
        conn, resp = self.stream(f"/api/notes/{slug}/stream")
        self.call("POST", f"/api/notes/{slug}/job-0009/ask", {})
        event, data = self.first_event(resp)
        self.assertEqual(event, "job.updated")
        self.assertEqual(data["note_id"], "job-0009")
        conn.close()

    def test_ask_after_ai_reply_queues_the_latest_question(self):
        """多轮：AI 答过一轮后，追问还能再让 AI 回答，且问的是最新那层。"""
        slug = "multi-round"
        self.call("PUT", f"/api/notes/{slug}/mr-0001", self.note("mr-0001"))
        _, first = self.call("POST", f"/api/notes/{slug}/mr-0001/ask", {})
        notesd.Handler.store.finish_job(first["job"]["id"], True, "")
        self.call("POST", f"/api/notes/{slug}/mr-0001/replies", {"author": "AI", "text": "结论一"})
        self.call("POST", f"/api/notes/{slug}/mr-0001/replies", {"author": "我", "text": "那第二点呢"})
        status, body = self.call("POST", f"/api/notes/{slug}/mr-0001/ask", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["triggered"])
        self.assertEqual(body["job"]["prompt"], "那第二点呢")

    def test_reply_with_ask_flag_forces_a_job(self):
        slug = "reply-ask"
        self.call("PUT", f"/api/notes/{slug}/ra-0001", self.note("ra-0001"))
        status, body = self.call("POST", f"/api/notes/{slug}/ra-0001/replies",
                                 {"author": "我", "text": "展开讲讲第二点", "ask": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["triggered"])
        self.assertEqual(body["job"]["prompt"], "展开讲讲第二点")
        self.assertEqual(body["job"]["kind"], "manual")

    def test_question_reply_triggers_without_a_bound_session(self):
        slug = "reply-nosess"
        self.call("PUT", f"/api/notes/{slug}/rn-0001", self.note("rn-0001"))
        _, body = self.call("POST", f"/api/notes/{slug}/rn-0001/replies", {"author": "我", "text": "为什么？"})
        self.assertTrue(body["triggered"])

    def test_ai_reply_never_triggers_another_job(self):
        """bridge 用 Root 身份回帖；AI 的回答里带问号也不能再排一轮，否则自我循环。"""
        slug = "ai-loop"
        self.call("PUT", f"/api/notes/{slug}/al-0001", self.note("al-0001"))
        _, body = self.call("POST", f"/api/notes/{slug}/al-0001/replies",
                            {"author": "AI", "text": "为什么这样？因为收敛。"})
        self.assertFalse(body["triggered"])

    def test_finished_job_is_pushed_to_the_owners_note_stream(self):
        slug = "jobs-finish-stream"
        self.call("PUT", f"/api/notes/{slug}/jf-0001", self.note("jf-0001"))
        _, body = self.call("POST", f"/api/notes/{slug}/jf-0001/ask", {})
        conn, resp = self.stream(f"/api/notes/{slug}/stream")
        notesd.Handler.store.finish_job(body["job"]["id"], True, "")
        event, data = self.first_event(resp)
        self.assertEqual(event, "job.updated")
        self.assertEqual(data["job"]["status"], "done")
        conn.close()

    def test_statement_reply_does_not_create_job(self):
        slug = "jobs-plain"
        self.register(slug)
        self.call("PUT", f"/api/notes/{slug}/job-0002", self.note("job-0002"))
        self.call("POST", f"/api/notes/{slug}/job-0002/replies", {"author": "我", "text": "这句是全文转折", "t": 2000})
        _, body = self.call("GET", "/api/jobs?status=pending")
        self.assertEqual([j for j in body["jobs"] if j["slug"] == slug], [])

    def test_ask_twice_returns_409(self):
        slug = "jobs-manual"
        self.register(slug)
        self.call("PUT", f"/api/notes/{slug}/job-0003", self.note("job-0003"))
        self.assertEqual(self.call("POST", f"/api/notes/{slug}/job-0003/ask", {})[0], 200)
        self.assertEqual(self.call("POST", f"/api/notes/{slug}/job-0003/ask", {})[0], 409)

    def test_auto_ask_skips_a_statement(self):
        slug = "jobs-auto-statement"
        self.register(slug)
        self.call("PUT", f"/api/notes/{slug}/job-0010", self.note("job-0010", q="收敛是必要条件", block=2))
        status, body = self.call("POST", f"/api/notes/{slug}/job-0010/ask", {"auto": True})
        self.assertEqual(status, 200)
        self.assertFalse(body["triggered"])
        self.assertIsNone(body["job"])

    def test_auto_ask_queues_a_question(self):
        slug = "jobs-auto-question"
        self.register(slug)
        note = self.note("job-0011", q="收敛是必要条件", block=2)
        note["thread"][0]["text"] = "为什么这里说收敛不是目标？"
        self.call("PUT", f"/api/notes/{slug}/job-0011", note)
        status, body = self.call("POST", f"/api/notes/{slug}/job-0011/ask", {"auto": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["triggered"])
        self.assertEqual(body["job"]["kind"], "auto")

    def test_reply_with_question_returns_the_created_job(self):
        slug = "jobs-reply-auto"
        self.register(slug)
        self.call("PUT", f"/api/notes/{slug}/job-0012", self.note("job-0012", q="收敛是必要条件", block=2))
        status, body = self.call("POST", f"/api/notes/{slug}/job-0012/replies", {"text": "这里为什么不算反例？"})
        self.assertEqual(status, 200)
        self.assertTrue(body["triggered"])
        self.assertEqual(body["job"]["note_id"], "job-0012")

    def test_ask_without_session_still_queues(self):
        self.call("PUT", "/api/notes/asked-nosess/n9abcde", self.note("n9abcde", q="为什么"))
        status, body = self.call("POST", "/api/notes/asked-nosess/n9abcde/ask", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["triggered"])
        self.assertEqual(body["job"]["session_id"] or "", "")

    def test_job_claim_done_and_retry_budget(self):
        slug = "jobs-life"
        self.register(slug)
        self.call("PUT", f"/api/notes/{slug}/job-0005", self.note("job-0005"))
        _, body = self.call("POST", f"/api/notes/{slug}/job-0005/ask", {})
        job_id = body["job"]["id"]
        status, claimed = self.call("POST", f"/api/jobs/{job_id}/claim", {"worker": "bridge-1"})
        self.assertEqual(status, 200)
        self.assertEqual(claimed["job"]["status"], "running")
        self.assertEqual(self.call("POST", f"/api/jobs/{job_id}/claim", {"worker": "bridge-2"})[0], 409)
        _, done = self.call("POST", f"/api/jobs/{job_id}/done", {"ok": False, "error": "locked"})
        self.assertEqual(done["job"]["status"], "pending")
        self.assertEqual(done["job"]["attempts"], 1)
        for _ in range(2):
            _, claimed = self.call("POST", f"/api/jobs/{job_id}/claim", {"worker": "bridge-1"})
            _, done = self.call("POST", f"/api/jobs/{job_id}/done", {"ok": False, "error": "locked"})
        self.assertEqual(done["job"]["status"], "blocked")
        self.assertEqual(done["job"]["attempts"], 3)
        self.assertEqual(done["job"]["error"], "locked")

    def test_busy_session_does_not_consume_retry_budget(self):
        slug = "jobs-busy"
        self.register(slug)
        self.call("PUT", f"/api/notes/{slug}/busy-0001", self.note("busy-0001"))
        _, body = self.call("POST", f"/api/notes/{slug}/busy-0001/ask", {})
        job_id = body["job"]["id"]
        busy = 'session "session-x" is already owned by an active write handle'
        done = None
        for _ in range(5):
            self.call("POST", f"/api/jobs/{job_id}/claim", {"worker": "bridge-1"})
            _, done = self.call("POST", f"/api/jobs/{job_id}/done", {"ok": False, "error": busy})
            self.assertEqual(done["job"]["status"], "pending", "占用中的会话不该把任务打成 blocked")
        self.assertLessEqual(done["job"]["attempts"], 1, "busy 不应消耗重试次数")
        self.assertIn("active write handle", done["job"]["error"])

    def test_session_round_trip(self):
        status, _ = self.call("PUT", "/api/sessions/ot-crdt-lww", {
            "session_id": "0f3a1c2d-1234-4abc-9def-001122334455",
            "workspace": "default-workspace",
            "title": "OT/CRDT 那篇",
        })
        self.assertEqual(status, 200)
        status, body = self.call("GET", "/api/sessions/ot-crdt-lww")
        self.assertEqual(status, 200)
        self.assertEqual(body["session"]["session_id"], "0f3a1c2d-1234-4abc-9def-001122334455")
        self.assertEqual(body["session"]["workspace"], "default-workspace")

    def test_session_rejects_bad_id(self):
        status, _ = self.call("PUT", "/api/sessions/ot-crdt-lww", {"session_id": "../../etc/passwd"})
        self.assertEqual(status, 400)

    def test_session_missing_returns_null(self):
        status, body = self.call("GET", "/api/sessions/nope-never")
        self.assertEqual(status, 200)
        self.assertIsNone(body["session"])

    def test_reply_rejects_empty_text(self):
        self.call("PUT", "/api/notes/thread-test/thr-0002", self.note("thr-0002"))
        status, _ = self.call("POST", "/api/notes/thread-test/thr-0002/replies", {"author": "AI", "text": "   "})
        self.assertEqual(status, 400)



if __name__ == "__main__":
    unittest.main(verbosity=2)