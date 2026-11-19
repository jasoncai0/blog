#!/usr/bin/env python3
"""从 DSH 的会话日志里摘出「跟这篇文章有关」的上下文。

用途：文章绑定的原会话跑在桌面端的 `standard` 预设下，headless 一次性
runner 接不了（见 notes_bridge.answer）。退而求其次，把原会话里提到这篇
文章的对话节选搬进新会话，让回答能接上当时的设计讨论。

只依赖标准库；日志是 zstd 压缩的 JSONL，用系统自带的 `zstd -dc` 解。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

SESSIONS_ROOT = os.path.expanduser("~/.dsh/sessions")
MAX_ITEM_CHARS = 400
SKIP_PREFIXES = (
    "<system-reminder>",
    "Instructions from:",
    "Current runtime context",
    "Time sampled",
    "Updated instructions from",
    "<acp",
    "Compressed conversation section",
    "以下是与这篇文章有关的对话节选",
    "[Compressed",
)


def _log_path(session_id: str) -> str | None:
    """`~/.dsh/sessions/<workspace>/<session-id>/session.v4.jsonl.zstd`。"""
    if not os.path.isdir(SESSIONS_ROOT):
        return None
    names = {session_id, f"session-{session_id}"} if not session_id.startswith("session-") else {session_id}
    for workspace in sorted(os.listdir(SESSIONS_ROOT)):
        for name in names:
            path = os.path.join(SESSIONS_ROOT, workspace, name, "session.v4.jsonl.zstd")
            if os.path.exists(path):
                return path
    return None


def _events(path: str):
    try:
        proc = subprocess.run(["zstd", "-dc", path], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _text_of(event: dict) -> str:
    data = event.get("data") or {}
    parts = data.get("content")
    if isinstance(parts, list):
        out = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append(part.get("text") or "")
        return "\n".join(out).strip()
    if isinstance(parts, str):
        return parts.strip()
    return ""


def _keywords(slug: str) -> list[str]:
    words = [slug] + [w for w in re.split(r"[-_]", slug) if len(w) >= 4]
    return [w.lower() for w in words]


def _clip(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def digest_for(slug: str, session_id: str, budget_chars: int = 6000) -> str:
    """返回可直接塞进 prompt 的节选；找不到日志或没有相关内容时返回空串。"""
    path = _log_path(session_id)
    if not path:
        return ""
    keys = _keywords(slug)
    users: list[str] = []
    relevant: list[str] = []
    seen: set[str] = set()
    for event in _events(path):
        kind = event.get("type")
        if kind not in ("user/message", "assistant/message"):
            continue
        text = _text_of(event)
        if not text or text.startswith(SKIP_PREFIXES):
            continue
        if text in seen:
            continue
        seen.add(text)
        low = text.lower()
        if kind == "user/message":
            users.append(_clip(text))
        elif any(k in low for k in keys):
            relevant.append(_clip(text))

    lines: list[str] = []
    budget = budget_chars
    for text in users[:20]:
        if budget <= 0:
            break
        lines.append("[作者] " + text)
        budget -= len(text)
    for text in relevant[:12]:
        if budget <= 0:
            break
        lines.append("[助手] " + text)
        budget -= len(text)
    if not lines:
        return ""
    body = "\n".join(lines)
    return "以下是与这篇文章有关的对话节选（按时间顺序，已截断）：\n\n" + body


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: session_digest.py <slug> <session_id> [budget_chars]", file=sys.stderr)
        return 2
    budget = int(argv[3]) if len(argv) > 3 else 6000
    digest = digest_for(argv[1], argv[2], budget)
    if not digest:
        print("（没有可用节选）")
        return 1
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
