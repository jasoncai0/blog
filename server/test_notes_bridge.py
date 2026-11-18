#!/usr/bin/env python3
"""notes_bridge 的单元测试：不真跑 dsh，用假 runner 与假 post。

    python3 server/test_notes_bridge.py
"""

import unittest

import notes_bridge


class BridgeTest(unittest.TestCase):
    def test_build_prompt_carries_quote_question_and_limits(self):
        job = {
            "slug": "ot-crdt-lww",
            "note": {
                "q": "收敛是必要条件",
                "thread": [{"author": "我", "text": "这里为什么不用向量时钟？"}],
            },
            "prompt": "fallback",
        }
        prompt = notes_bridge.build_prompt(job)
        self.assertIn("ot-crdt-lww", prompt)
        self.assertIn("收敛是必要条件", prompt)
        self.assertIn("为什么不用向量时钟", prompt)
        self.assertIn("只给结论", prompt)
        self.assertIn("400", prompt)

    def test_parse_reply_takes_last_assistant_text(self):
        stdout = "\n".join([
            '{"type":"assistant","text":"第一段"}',
            '{"type":"tool_call","name":"read"}',
            '{"type":"assistant","text":"第二段结论"}',
        ])
        self.assertEqual(notes_bridge.parse_reply(stdout), "第二段结论")

    def test_parse_reply_falls_back_to_raw_output(self):
        self.assertEqual(notes_bridge.parse_reply("纯文本回答"), "纯文本回答")

    def test_claim_and_run_posts_ai_floor_then_done(self):
        calls = []
        job = {"id": "job-1", "slug": "s", "note_id": "n1", "session_id": "sid", "note": {"q": "q", "thread": []}}

        def post(method, path, body=None, timeout=20):
            calls.append((method, path, body))
            return 200, {"ok": True}

        ok = notes_bridge.claim_and_run(job, runner=lambda prompt, sid: "结论：不需要", post=post)
        self.assertTrue(ok)
        paths = [c[1] for c in calls]
        self.assertEqual(paths[0], "/jobs/job-1/claim")
        self.assertIn("/notes/s/n1/replies", paths)
        reply = next(c[2] for c in calls if c[1].endswith("/replies"))
        self.assertEqual(reply["author"], "AI")
        self.assertEqual(reply["text"], "结论：不需要")
        self.assertEqual(calls[-1][1], "/jobs/job-1/done")
        self.assertTrue(calls[-1][2]["ok"])

    def test_claim_and_run_reports_failure(self):
        calls = []

        def post(method, path, body=None, timeout=20):
            calls.append((method, path, body))
            return 200, {"ok": True}

        def boom(prompt, sid):
            raise RuntimeError("session.lock held")

        ok = notes_bridge.claim_and_run(
            {"id": "job-2", "slug": "s", "note_id": "n1", "session_id": "sid", "note": {}},
            runner=boom, post=post,
        )
        self.assertFalse(ok)
        done = next(c[2] for c in calls if c[1].endswith("/done"))
        self.assertFalse(done["ok"])
        self.assertIn("session.lock", done["error"])

    def test_claim_refused_does_nothing(self):
        def post(method, path, body=None, timeout=20):
            return 409, {"error": "not claimable"}

        ok = notes_bridge.claim_and_run(
            {"id": "j", "slug": "s", "note_id": "n", "session_id": "x", "note": {}},
            runner=lambda p, s: "never", post=post,
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
