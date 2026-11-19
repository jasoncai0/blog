#!/usr/bin/env python3
"""session_digest 的离线测试：不需要网络，也不碰真实会话目录。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import session_digest


def _write_log(dirpath: str, name: str, events: list[dict]) -> str:
    os.makedirs(dirpath, exist_ok=True)
    raw = "\n".join(json.dumps(e, ensure_ascii=False) for e in events).encode("utf-8")
    plain = os.path.join(dirpath, "plain.jsonl")
    with open(plain, "wb") as fh:
        fh.write(raw)
    zst = os.path.join(dirpath, name)
    subprocess.run(["zstd", "-q", "-f", plain, "-o", zst], check=True)
    os.unlink(plain)
    return zst


def _user(text: str) -> dict:
    return {"type": "user/message", "data": {"content": [{"type": "text", "text": text}]}}


def _assistant(text: str) -> dict:
    return {"type": "assistant/message", "data": {"content": [{"type": "text", "text": text}]}}


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "sessions")
        self.sid = "session-aaaa1111-2222-3333-4444-555566667777"
        events = [
            _user("对比协同编辑算法 OT / CRDT，写一篇博客"),
            _user("Current runtime context. This snapshot supersedes earlier ones."),
            _user("Time sampled while preparing turn 1, step 1: 2026-11-09"),
            _assistant("好的，我先把 OT 的变换讲清楚，再讲 CRDT 的合并。"),
            _assistant("这篇文章会重点讨论 CRDT 与 CAS 的区别。"),
            _user("再帮我补充一下，A 的写入被丢弃了吗？"),
            _assistant("这篇博客里没有提到 A。"),
        ]
        _write_log(os.path.join(self.root, "--workspace--", self.sid), "session.v4.jsonl.zstd", events)
        self.patch = mock.patch.object(session_digest, "SESSIONS_ROOT", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_keeps_user_intent_and_drops_injected_noise(self):
        digest = session_digest.digest_for("ot-crdt-lww", self.sid)
        self.assertIn("对比协同编辑算法", digest)
        self.assertIn("A 的写入被丢弃了吗", digest)
        self.assertNotIn("Current runtime context", digest)
        self.assertNotIn("Time sampled", digest)

    def test_keeps_only_relevant_assistant_text(self):
        digest = session_digest.digest_for("ot-crdt-lww", self.sid)
        self.assertIn("这篇文章会重点讨论 CRDT 与 CAS 的区别", digest)
        self.assertNotIn("这篇博客里没有提到 A", digest)

    def test_respects_budget(self):
        digest = session_digest.digest_for("ot-crdt-lww", self.sid, budget_chars=60)
        self.assertLess(len(digest), 400)
        self.assertIn("[作者]", digest)

    def test_missing_session_returns_empty(self):
        self.assertEqual(session_digest.digest_for("ot-crdt-lww", "session-deadbeef"), "")

    def test_slug_keywords_drive_selection(self):
        digest = session_digest.digest_for("unrelated-post", self.sid)
        self.assertNotIn("CRDT 与 CAS 的区别", digest)
        self.assertIn("对比协同编辑算法", digest)


if __name__ == "__main__":
    unittest.main()
