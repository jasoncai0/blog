#!/usr/bin/env python3
"""notesd 的接口测试：起一个真进程内的服务，打真实 HTTP。

    python3 server/test_notesd.py
"""

import json
import os
import tempfile
import threading
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

    def test_reply_rejects_empty_text(self):
        self.call("PUT", "/api/notes/thread-test/thr-0002", self.note("thr-0002"))
        status, _ = self.call("POST", "/api/notes/thread-test/thr-0002/replies", {"author": "AI", "text": "   "})
        self.assertEqual(status, 400)



if __name__ == "__main__":
    unittest.main(verbosity=2)