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
        notesd.Handler.edit_key = ""
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

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

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

    def test_edit_key_guards_writes(self):
        notesd.Handler.edit_key = "s3cret"
        try:
            self.assertEqual(
                self.call("PUT", "/api/notes/hello/n8abcde", self.note("n8abcde"))[0], 401
            )
            status, _ = self.call(
                "PUT", "/api/notes/hello/n8abcde", self.note("n8abcde"), {"X-Edit-Key": "s3cret"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(self.call("GET", "/api/notes/hello")[0], 200)
            # 阅读进度是匿名的：访客都能写，不受口令影响。
            self.assertEqual(
                self.call("PUT", "/api/progress/anon-probe", {"p": 40, "t": 1700000000000, "d": False})[0],
                200,
            )
        finally:
            notesd.Handler.edit_key = ""


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
        conn.request("GET", path, headers=headers or {})
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

    def test_bridge_stream_requires_token(self):
        notesd.Handler.bridge_token = "btok"
        try:
            conn, resp = self.stream("/api/jobs/stream")
            self.assertEqual(resp.status, 401)
            conn.close()
            conn, resp = self.stream("/api/jobs/stream", {"X-Bridge-Token": "wrong"})
            self.assertEqual(resp.status, 401)
            conn.close()
        finally:
            notesd.Handler.bridge_token = ""

    def test_bridge_stream_disabled_without_token(self):
        notesd.Handler.bridge_token = ""
        conn, resp = self.stream("/api/jobs/stream")
        self.assertEqual(resp.status, 503)
        conn.close()

    def test_bridge_stream_pushes_new_job(self):
        notesd.Handler.bridge_token = "btok"
        try:
            slug = "stream-jobs"
            self.register(slug)
            self.call("PUT", f"/api/notes/{slug}/job-0006", self.note("job-0006"))
            conn, resp = self.stream("/api/jobs/stream", {"X-Bridge-Token": "btok"})
            self.assertEqual(resp.status, 200)
            self.call("POST", f"/api/notes/{slug}/job-0006/ask", {})
            event, data = self.first_event(resp)
            self.assertEqual(event, "job.created")
            self.assertEqual(data["slug"], slug)
            conn.close()
        finally:
            notesd.Handler.bridge_token = ""

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

    def test_ask_without_session_returns_409(self):
        self.call("PUT", "/api/notes/jobs-none/job-0004", self.note("job-0004"))
        self.assertEqual(self.call("POST", "/api/notes/jobs-none/job-0004/ask", {})[0], 409)

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