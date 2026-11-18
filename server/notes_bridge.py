#!/usr/bin/env python3
"""notes-bridge — 在 Mac 上把「批注唤醒会话」的任务跑完。

服务器不发起连接（没有公网入口、也不该有）。bridge 主动连云端的
`/api/jobs/stream` 长连接，收到任务就 claim，串行 spawn 一次
`dsh --profile headless --session-id <id>`，把模型最后一段回答 POST 回
`/api/notes/<slug>/<id>/replies`，再回报 done / failed。

用法：

    NOTES_API=https://jasoncai.site/api \
    BRIDGE_TOKEN=… NOTES_EDIT_KEY=… python3 notes_bridge.py

只依赖标准库；DSH 的凭据由 `--profile headless` 自己读，bridge 不碰。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

def _load_env_file(path: str = "~/.dsh/notes-bridge.env") -> None:
    """launchd 里没有 shell，密钥放 600 的文件里，进程自己读。"""
    full = os.path.expanduser(path)
    if not os.path.exists(full):
        return
    with open(full, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()

API = os.environ.get("NOTES_API", "http://127.0.0.1:8099/api").rstrip("/")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()
EDIT_KEY = os.environ.get("NOTES_EDIT_KEY", "").strip()
DSH_BIN = os.environ.get("DSH_BIN", "dsh")
DSH_PROFILE = os.environ.get("DSH_PROFILE", "headless")
WORKER = os.environ.get("BRIDGE_WORKER", "bridge-" + os.uname().nodename)
LOCK_BACKOFF = (5, 15, 45)

PROMPT_TEMPLATE = """你是这篇文章的作者。读者在文章《{slug}》的这句话下面留了批注：

> {quote}

读者的原话：
{question}

请直接给结论：{points}

硬约束：
- 简体中文，只给结论和必要理由，不要开场白、不要「好的」「希望对你有帮助」这类收尾。
- 不超过 400 字。要展开就分成 2–3 个短段落，不要列长清单。
- 只引用你能从这场会话里确认的事实；不确定就说不确定，不要编造引用。
"""


def build_prompt(job: dict) -> str:
    note = job.get("note") or {}
    thread = note.get("thread") or []
    question = ""
    for floor in thread:
        if floor.get("author") != "AI" and floor.get("text"):
            question = floor["text"]
    quote = note.get("q") or job.get("prompt") or ""
    return PROMPT_TEMPLATE.format(
        slug=job.get("slug", ""),
        quote=quote,
        question=question or job.get("prompt", ""),
        points="一到三点，每点一句话",
    )


def parse_reply(stdout: str) -> str:
    """dsh --print 输出一行一个 JSON 事件；取最后一条助手文本。"""
    last = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") in ("assistant", "assistant_message", "text") and event.get("text"):
            last = str(event["text"]).strip()
    if not last:
        last = stdout.strip()[-4000:]
    return last


def run_session(prompt: str, session_id: str, timeout: int = 600) -> str:
    cmd = [
        DSH_BIN,
        "--profile",
        DSH_PROFILE,
        "--session-id",
        session_id,
        "--approval-policy",
        "never",
        prompt,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    err = (proc.stderr or "").strip()
    if "session.lock" in err or "locked" in err.lower():
        raise RuntimeError("locked")
    if proc.returncode != 0:
        # stderr 前半是 MCP 之类的噪声，真正的失败原因在 `dsh: ...` 那行。
        lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
        reason = next((ln for ln in reversed(lines) if ln.startswith("dsh:")), "")
        raise RuntimeError(reason.removeprefix("dsh: ").strip() or (lines[-1] if lines else f"exit {proc.returncode}"))
    return parse_reply(proc.stdout)


def _request(method: str, path: str, body: dict | None = None, timeout: int = 20) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if BRIDGE_TOKEN:
        headers["X-Bridge-Token"] = BRIDGE_TOKEN
    if EDIT_KEY:
        headers["X-Edit-Key"] = EDIT_KEY
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}")
        except json.JSONDecodeError:
            return err.code, {}


def claim_and_run(job: dict, runner=run_session, post=_request) -> bool:
    job_id = job["id"]
    status, _ = post("POST", f"/jobs/{job_id}/claim", {"worker": WORKER})
    if status != 200:
        return False
    try:
        text = runner(build_prompt(job), job["session_id"])
        if not text.strip():
            raise RuntimeError("empty reply")
        status, _ = post(
            "POST",
            f"/notes/{job['slug']}/{job['note_id']}/replies",
            {"author": "AI", "text": text[:4000], "t": int(time.time() * 1000)},
        )
        if status != 200:
            raise RuntimeError(f"reply rejected: {status}")
        post("POST", f"/jobs/{job_id}/done", {"ok": True})
        print(f"[bridge] done {job_id} {job['slug']}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001 — 任何失败都要落成 job 状态
        print(f"[bridge] fail {job_id}: {exc}", flush=True)
        post("POST", f"/jobs/{job_id}/done", {"ok": False, "error": str(exc)[:200]})
        return False


def drain() -> None:
    status, body = _request("GET", "/jobs?status=pending")
    if status != 200:
        return
    for job in body.get("jobs", []):
        note_status, note_body = _request("GET", f"/notes/{job['slug']}")
        if note_status == 200:
            for note in note_body.get("notes", []):
                if note["id"] == job["note_id"]:
                    job["note"] = note
        claim_and_run(job)


def subscribe() -> None:
    """SSE 只是「有新任务」的门铃，真正的任务列表仍走 HTTP 拉取。"""
    req = urllib.request.Request(
        API + "/jobs/stream",
        headers={"X-Bridge-Token": BRIDGE_TOKEN, "Accept": "text/event-stream"},
    )
    while True:
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("event:"):
                        drain()
        except Exception as exc:  # noqa: BLE001 — 断线就退避重连
            print(f"[bridge] stream down: {exc}", flush=True)
            time.sleep(3)


def main() -> None:
    if not BRIDGE_TOKEN:
        print("BRIDGE_TOKEN is required", file=sys.stderr)
        return
    threading.Thread(target=subscribe, daemon=True).start()
    print(f"[bridge] {WORKER} watching {API}", flush=True)
    while True:
        try:
            drain()
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] drain failed: {exc}", flush=True)
        time.sleep(10)


if __name__ == "__main__":
    main()
