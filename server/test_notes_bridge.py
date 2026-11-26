#!/usr/bin/env python3
"""notes_bridge 的单元测试：不真跑 dsh，用假 runner 与假 post。

    python3 server/test_notes_bridge.py
"""

import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="bridge-test-")
os.environ["BRIDGE_STATE"] = os.path.join(_tmp, "state.json")

import notes_bridge  # noqa: E402  （必须在设置 BRIDGE_STATE 之后导入）


class BridgeTest(unittest.TestCase):
    def test_job_without_session_uses_a_fresh_article_session(self):
        calls = []
        fresh = []

        def runner(prompt, sid, cwd=None):
            calls.append((prompt, sid))
            return "回答"

        def runner_new(first, cwd=None):
            fresh.append(first)
            return "明白了", "session-new-1234"

        job = {
            "slug": "personal-agent-anatomy",
            "session_id": "",
            "note": {"q": "收敛是必要条件", "thread": [{"author": "我", "text": "为什么"}]},
            "prompt": "为什么",
        }
        self.assertEqual(notes_bridge.answer(job, cwd="/tmp", runner=runner, runner_new=runner_new), "回答")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "session-new-1234")
        self.assertIn("明白了", fresh[0])
        self.assertNotEqual(notes_bridge._load_state().get("personal-agent-anatomy"), "")

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

    def test_build_prompt_follow_up_carries_history_and_latest_question(self):
        job = {
            "slug": "ot-crdt-lww",
            "note": {
                "q": "收敛是必要条件",
                "thread": [
                    {"author": "我", "text": "这里为什么不用向量时钟？"},
                    {"author": "AI", "text": "因为 LWW 只需要全序时间戳。"},
                    {"author": "我", "text": "那时钟漂移怎么办"},
                ],
            },
            "prompt": "fallback",
        }
        prompt = notes_bridge.build_prompt(job)
        self.assertIn("之前的楼层", prompt)
        self.assertIn("因为 LWW 只需要全序时间戳", prompt)
        question = prompt.split("读者这次的追问")[1]
        self.assertIn("那时钟漂移怎么办", question)
        self.assertNotIn("向量时钟", question)

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
            if method == "GET" and path.startswith("/sessions/"):
                return 200, {"session": {"workspace": "demo-ws"}}
            return 200, {"ok": True}

        ok = notes_bridge.claim_and_run(
            job, runner=lambda prompt, sid, cwd=None: "结论：不需要", post=post
        )
        self.assertTrue(ok)
        paths = [c[1] for c in calls]
        self.assertEqual(paths[0], "/jobs/job-1/claim")
        self.assertIn("/notes/s/n1/replies", paths)
        reply = next(c[2] for c in calls if c[1].endswith("/replies"))
        self.assertEqual(reply["author"], "AI")
        self.assertEqual(reply["text"], "结论：不需要")
        self.assertEqual(calls[-1][1], "/jobs/job-1/done")
        self.assertTrue(calls[-1][2]["ok"])

    def test_falls_back_to_bridge_session_when_preset_is_not_composable(self):
        import tempfile, os
        import notes_bridge as nb

        tmp = tempfile.mkdtemp()
        old = nb.BRIDGE_STATE
        nb.BRIDGE_STATE = os.path.join(tmp, "sessions.json")
        calls = []

        def post(method, path, body=None, timeout=20):
            calls.append((method, path, body))
            if path.startswith("/sessions/"):
                return 200, {"session": {"workspace": "/tmp/ws"}}
            return 200, {}

        def runner(prompt, sid, cwd=None):
            if sid == "session-orig-0001":
                raise RuntimeError(
                'session "session-orig-0001" runs under agent preset "standard", '
                "which the one-shot runner does not compose"
            )
            return "结论：走自建会话"

        created = []

        def runner_new(prompt, cwd=None):
            created.append(prompt[:20])
            return "明白了", "session-bridge-0001"

        job = {
            "id": "job-1", "slug": "demo-post", "note_id": "n1", "session_id": "session-orig-0001",
            "prompt": "为什么？", "note": {"q": "原文", "thread": [{"author": "我", "text": "为什么？"}]},
        }
        try:
            ok = nb.claim_and_run(job, runner=runner, post=post, runner_new=runner_new)
            assert ok, "应当成功"
            assert created, "应当新建会话"
            replies = [c for c in calls if c[1].endswith("/replies")]
            assert replies and "自建会话" in replies[0][2]["text"]
            import json
            state = json.load(open(nb.BRIDGE_STATE))
            assert state["demo-post"] == "session-bridge-0001"
        finally:
            nb.BRIDGE_STATE = old

    def test_busy_bridge_session_is_replaced_by_a_fresh_one(self):
        import json, os, tempfile
        import notes_bridge as nb

        tmp = tempfile.mkdtemp()
        old = nb.BRIDGE_STATE
        nb.BRIDGE_STATE = os.path.join(tmp, "sessions.json")
        with open(nb.BRIDGE_STATE, "w", encoding="utf-8") as fh:
            json.dump({"demo-busy": "session-busy-0002"}, fh)
        calls, created = [], []

        def post(method, path, body=None, timeout=20):
            calls.append((method, path, body))
            if path.startswith("/sessions/"):
                return 200, {"session": {"workspace": "/tmp/ws"}}
            return 200, {}

        def runner(prompt, sid, cwd=None):
            if sid in ("session-busy-0002", "session-orig-0009"):
                raise RuntimeError(
                    f'session "{sid}" is already owned by an active write handle'
                )
            return "结论：换会话后答出来"

        def runner_new(prompt, cwd=None):
            created.append(prompt[:10])
            return "明白了", "session-fresh-0003"

        job = {
            "id": "job-2", "slug": "demo-busy", "note_id": "n2", "session_id": "session-orig-0009",
            "prompt": "为什么？", "note": {"q": "原文", "thread": [{"author": "我", "text": "为什么？"}]},
        }
        try:
            ok = nb.claim_and_run(job, runner=runner, post=post, runner_new=runner_new)
            self.assertTrue(ok, "会话被占用时应换新会话继续，而不是失败")
            replies = [c for c in calls if c[1].endswith("/replies")]
            self.assertTrue(replies and "换会话" in replies[0][2]["text"])
            state = json.load(open(nb.BRIDGE_STATE))
            self.assertEqual(state["demo-busy"], "session-fresh-0003")
        finally:
            nb.BRIDGE_STATE = old

    def test_claim_and_run_reports_failure(self):
        calls = []

        def post(method, path, body=None, timeout=20):
            calls.append((method, path, body))
            return 200, {"ok": True}

        def boom(prompt, sid, cwd=None):
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
            runner=lambda p, s, cwd=None: "never", post=post,
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
