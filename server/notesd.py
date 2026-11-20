#!/usr/bin/env python3
"""notesd — 博客的批注 / 阅读进度同步服务。

只用标准库（http.server + sqlite3），单进程、单文件、单库：

    GET    /api/summary                 首页要的全部计数与进度
    GET    /api/notes/<slug>            某篇文章的全部批注
    PUT    /api/notes/<slug>/<id>       新增或更新一条批注
    DELETE /api/notes/<slug>/<id>       删除一条批注（留墓碑，便于多端同步）
    GET    /api/progress                全部阅读进度
    PUT    /api/progress/<slug>         更新一篇的进度
    GET    /healthz                     探活

读公开；写默认也公开，但设置环境变量 NOTES_EDIT_KEY 后所有写操作要求
`X-Edit-Key` 头与它一致（客户端会问一次并记住）。

数据落在 --db 指定的 SQLite 文件里；每次写入都额外记一条 note_rev，
误改或被人乱写时可以按时间回滚。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import queue
import sqlite3
import threading
import time
from collections import defaultdict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

MAX_BODY = 64 * 1024
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,63}$")
SESSION_RE = re.compile(r"^[0-9a-zA-Z][0-9a-zA-Z-]{7,63}$")
WS_RE = re.compile(r"^/?[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")  # 允许绝对路径，bridge 直接当 cwd 用
QUESTION_RE = re.compile(r"[?？]|为什么|怎么|是不是|如何|能不能")
MAX_ATTEMPTS = 3
BUSY_MARK = "active write handle"  # dsh 的会话被别的进程占着时的错误片断
MAX_THREAD = 50
MAX_THREAD_TEXT = 4000
MAX_Q = 400
MAX_CONTEXT = 80

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
  id      TEXT PRIMARY KEY,
  slug    TEXT NOT NULL,
  q       TEXT NOT NULL,
  pre     TEXT NOT NULL DEFAULT '',
  post    TEXT NOT NULL DEFAULT '',
  block   INTEGER NOT NULL DEFAULT 0,
  t       INTEGER NOT NULL DEFAULT 0,
  thread  TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  updated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS notes_slug ON notes(slug, deleted);
CREATE TABLE IF NOT EXISTS note_rev (
  seq     INTEGER PRIMARY KEY AUTOINCREMENT,
  note_id TEXT NOT NULL,
  slug    TEXT NOT NULL,
  action  TEXT NOT NULL,
  payload TEXT NOT NULL,
  at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS progress (
  slug TEXT PRIMARY KEY,
  p    INTEGER NOT NULL,
  t    INTEGER NOT NULL,
  d    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites (
  slug TEXT PRIMARY KEY,
  t    INTEGER NOT NULL,
  updated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
  id         TEXT PRIMARY KEY,
  slug       TEXT NOT NULL,
  note_id    TEXT NOT NULL,
  session_id TEXT NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'auto',
  prompt     TEXT NOT NULL DEFAULT '',
  status     TEXT NOT NULL DEFAULT 'pending',
  attempts   INTEGER NOT NULL DEFAULT 0,
  worker     TEXT NOT NULL DEFAULT '',
  claimed_at INTEGER NOT NULL DEFAULT 0,
  heartbeat  INTEGER NOT NULL DEFAULT 0,
  error      TEXT NOT NULL DEFAULT '',
  created    INTEGER NOT NULL DEFAULT 0,
  updated    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created);
CREATE INDEX IF NOT EXISTS jobs_note ON jobs(note_id, status);
CREATE TABLE IF NOT EXISTS sessions (
  slug       TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  workspace  TEXT NOT NULL DEFAULT '',
  title      TEXT NOT NULL DEFAULT '',
  updated    INTEGER NOT NULL DEFAULT 0
);
"""


def job_summary(job: dict) -> dict:
    return {
        "id": job.get("id", ""),
        "status": job.get("status", ""),
        "attempts": job.get("attempts", 0),
        "error": job.get("error", ""),
    }


def now() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self._subs: dict[str, list[queue.Queue]] = defaultdict(list)
        self._subs_lock = threading.Lock()
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- notes ----
    def note(self, slug: str, note_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM notes WHERE slug = ? AND id = ?", (slug, note_id)
        ).fetchone()

    def notes_for(self, slug: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM notes WHERE slug = ? AND deleted = 0 ORDER BY t ASC", (slug,)
        ).fetchall()

    def deleted_ids(self, slug: str) -> list[str]:
        return [
            row["id"]
            for row in self.conn.execute(
                "SELECT id FROM notes WHERE slug = ? AND deleted = 1", (slug,)
            )
        ]

    def counts(self) -> dict[str, int]:
        return {
            row["slug"]: row["n"]
            for row in self.conn.execute(
                "SELECT slug, COUNT(*) AS n FROM notes WHERE deleted = 0 GROUP BY slug"
            )
        }

    def put_note(self, slug: str, payload: dict) -> None:
        with self._lock:
            # 楼层只增不减：别人（或 AI）追加的 2 楼以后，旧的整条 PUT 不能把它们抹掉。
            # 客户端只会改 1 楼，所以比现有短的 thread 一律补回尾巴。
            row = self.conn.execute(
                "SELECT thread FROM notes WHERE id = ?", (payload["id"],)
            ).fetchone()
            thread = list(payload["thread"])
            if row is not None:
                old = json.loads(row["thread"])
                if len(old) > len(thread):
                    thread = thread + old[len(thread):]
                    payload = {**payload, "thread": thread}
            self.conn.execute(
                """INSERT INTO notes (id, slug, q, pre, post, block, t, thread, deleted, updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     slug = excluded.slug, q = excluded.q, pre = excluded.pre, post = excluded.post,
                     block = excluded.block, t = excluded.t, thread = excluded.thread,
                     deleted = 0, updated = excluded.updated""",
                (
                    payload["id"],
                    slug,
                    payload["q"],
                    payload["pre"],
                    payload["post"],
                    payload["block"],
                    payload["t"],
                    json.dumps(thread, ensure_ascii=False),
                    now(),
                ),
            )
            self.rev(payload["id"], slug, "put", payload)
            self.conn.commit()

    def add_reply(self, slug: str, note_id: str, reply: dict) -> tuple[str, dict | None]:
        """把一条回复追加到楼层末尾。返回 (状态, 笔记)。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM notes WHERE slug = ? AND id = ? AND deleted = 0", (slug, note_id)
            ).fetchone()
            if row is None:
                return "missing", None
            thread = json.loads(row["thread"])
            if len(thread) >= MAX_THREAD:
                return "full", None
            thread.append(reply)
            updated = now()
            self.conn.execute(
                "UPDATE notes SET thread = ?, updated = ? WHERE slug = ? AND id = ?",
                (json.dumps(thread, ensure_ascii=False), updated, slug, note_id),
            )
            self.rev(note_id, slug, "reply", reply)
            self.conn.commit()
            note = {
                "id": row["id"],
                "q": row["q"],
                "pre": row["pre"],
                "post": row["post"],
                "block": row["block"],
                "t": row["t"],
                "thread": thread,
                "updated": updated,
            }
        self.publish(f"note:{slug}", "note.updated", {"note": note})
        return "ok", note

    def delete_note(self, slug: str, note_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE notes SET deleted = 1, updated = ? WHERE slug = ? AND id = ? AND deleted = 0",
                (now(), slug, note_id),
            )
            if cur.rowcount:
                self.rev(note_id, slug, "delete", {"id": note_id})
            self.conn.commit()
            return bool(cur.rowcount)

    def rev(self, note_id: str, slug: str, action: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO note_rev (note_id, slug, action, payload, at) VALUES (?, ?, ?, ?, ?)",
            (note_id, slug, action, json.dumps(payload, ensure_ascii=False), now()),
        )

    # ---- progress ----
    def progress(self) -> dict[str, dict]:
        return {
            row["slug"]: {"p": row["p"], "t": row["t"], "d": bool(row["d"])}
            for row in self.conn.execute("SELECT * FROM progress")
        }

    def put_progress(self, slug: str, p: int, t: int, d: bool) -> dict:
        with self._lock:
            row = self.conn.execute(
                "SELECT p, t, d FROM progress WHERE slug = ?", (slug,)
            ).fetchone()
            best_p = max(p, row["p"]) if row else p
            best_t = max(t, row["t"]) if row else t
            best_d = int(bool(d) or bool(row["d"]) if row else bool(d))
            self.conn.execute(
                """INSERT INTO progress (slug, p, t, d) VALUES (?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET p = excluded.p, t = excluded.t, d = excluded.d""",
                (slug, best_p, best_t, best_d),
            )
            self.conn.commit()
            return {"p": best_p, "t": best_t, "d": bool(best_d)}


    # ---- favorites ----
    def favorites(self) -> dict[str, int]:
        return {row["slug"]: row["t"] for row in self.conn.execute("SELECT slug, t FROM favorites")}

    def put_favorite(self, slug: str, t: int) -> dict:
        with self._lock:
            self.conn.execute(
                """INSERT INTO favorites (slug, t, updated) VALUES (?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET t = excluded.t, updated = excluded.updated""",
                (slug, t, now()),
            )
            self.conn.commit()
            return {"slug": slug, "t": t}

    def delete_favorite(self, slug: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM favorites WHERE slug = ?", (slug,))
            self.conn.commit()
            return bool(cur.rowcount)

    # ---- events ----
    def subscribe(self, channel: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._subs_lock:
            self._subs[channel].append(q)
        return q

    def unsubscribe(self, channel: str, q: queue.Queue) -> None:
        with self._subs_lock:
            if q in self._subs.get(channel, []):
                self._subs[channel].remove(q)

    def publish(self, channel: str, event: str, data: dict) -> None:
        with self._subs_lock:
            queues = list(self._subs.get(channel, []))
        for q in queues:
            try:
                q.put_nowait({"event": event, "data": data})
            except queue.Full:
                pass

    # ---- jobs ----
    def create_job(self, slug: str, note_id: str, session_id: str, prompt: str, kind: str) -> dict:
        job_id = "job-" + os.urandom(6).hex()
        ts = now()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO jobs(id, slug, note_id, session_id, kind, prompt, status, created, updated) "
                "VALUES(?,?,?,?,?,?,'pending',?,?)",
                (job_id, slug, note_id, session_id, kind, prompt, ts, ts),
            )
        job = self.job(job_id) or {}
        self.publish("jobs", "job.created", job)
        self.publish(f"note:{slug}", "job.updated", {"note_id": note_id, "job": job_summary(job)})
        return job

    def job(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def active_job(self, note_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE note_id=? AND status IN ('pending','running') ORDER BY created DESC LIMIT 1",
            (note_id,),
        ).fetchone()
        return dict(row) if row else None

    def latest_job(self, note_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE note_id=? ORDER BY created DESC LIMIT 1", (note_id,)
        ).fetchone()
        return dict(row) if row else None

    def pending_jobs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY created LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]

    def claim_job(self, job_id: str, worker: str) -> dict | None:
        ts = now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE jobs SET status='running', worker=?, attempts=attempts+1, claimed_at=?, "
                "heartbeat=?, updated=? WHERE id=? AND status='pending'",
                (worker, ts, ts, ts, job_id),
            )
            if cur.rowcount != 1:
                return None
        return self.job(job_id)

    def finish_job(self, job_id: str, ok: bool, error: str) -> dict | None:
        row = self.job(job_id)
        if row is None:
            return None
        attempts = row["attempts"]
        if ok:
            status, error = "done", ""
        elif BUSY_MARK in (error or ""):
            # 会话被桌面端占用是暂态，不是任务失败：退回队列，不消耗重试次数。
            status = "pending"
            attempts = max(0, attempts - 1)
        elif attempts >= MAX_ATTEMPTS:
            status = "blocked"
        else:
            status = "pending"
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE jobs SET status=?, error=?, attempts=?, updated=? WHERE id=?",
                (status, error, attempts, now(), job_id),
            )
        job = self.job(job_id)
        if job:
            self.publish("jobs", "job.updated", job)
            self.publish(f"note:{job['slug']}", "job.updated", {"note_id": job["note_id"], "job": job_summary(job)})
        return job

    def upsert_session(self, slug: str, session_id: str, workspace: str, title: str) -> dict:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO sessions(slug, session_id, workspace, title, updated) VALUES(?,?,?,?,?) "
                "ON CONFLICT(slug) DO UPDATE SET session_id=excluded.session_id, "
                "workspace=excluded.workspace, title=excluded.title, updated=excluded.updated",
                (slug, session_id, workspace, title, now()),
            )
        return self.session_for(slug) or {}

    def session_for(self, slug: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE slug=?", (slug,)).fetchone()
        return dict(row) if row else None


class Limiter:
    """每个 IP 每分钟最多 120 次写、600 次读。"""

    def __init__(self) -> None:
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, ip: str, write: bool) -> bool:
        limit = 120 if write else 600
        cutoff = time.time() - 60
        with self.lock:
            q = self.hits[ip]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(time.time())
            return True


def clean_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def validate_note(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    note_id = raw.get("id")
    if not isinstance(note_id, str) or not ID_RE.match(note_id):
        return None
    q = clean_text(raw.get("q"), MAX_Q)
    if not q:
        return None
    block = raw.get("block")
    if not isinstance(block, int) or isinstance(block, bool) or block < 0:
        return None
    t = raw.get("t")
    if not isinstance(t, (int, float)) or isinstance(t, bool) or t <= 0:
        return None
    thread_raw = raw.get("thread")
    if not isinstance(thread_raw, list) or not 1 <= len(thread_raw) <= MAX_THREAD:
        return None
    thread = []
    for item in thread_raw:
        if not isinstance(item, dict):
            return None
        # 只划了高亮、还没写批注时 thread[0].text 是空串，这算合法数据。
        text = clean_text(item.get("text"), MAX_THREAD_TEXT)
        author = clean_text(item.get("author"), 20) or "我"
        thread.append({"author": author, "text": text, "t": int(item.get("t") or t)})
    return {
        "id": note_id,
        "q": q,
        "pre": clean_text(raw.get("pre"), MAX_CONTEXT),
        "post": clean_text(raw.get("post"), MAX_CONTEXT),
        "block": block,
        "t": int(t),
        "thread": thread,
    }


class Handler(BaseHTTPRequestHandler):
    store: Store
    limiter: Limiter
    edit_key: str = ""

    server_version = "notesd/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 交给 journald，别刷 stderr
        pass

    # ---- helpers ----
    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Edit-Key")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def write_allowed(self) -> bool:
        if not self.edit_key:
            return True
        return self.headers.get("X-Edit-Key", "") == self.edit_key

    def client_ip(self) -> str:
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def split_path(self) -> list[str]:
        return [unquote(p) for p in urlparse(self.path).path.split("/") if p]

    # ---- verbs ----
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Edit-Key")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self.route(write=False)

    def do_PUT(self) -> None:  # noqa: N802
        self.route(write=True)

    def do_DELETE(self) -> None:  # noqa: N802
        self.route(write=True)

    def do_POST(self) -> None:  # noqa: N802
        self.route(write=True)

    def stream(self, channel: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        q = self.store.subscribe(channel)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                body = json.dumps(ev["data"], ensure_ascii=False)
                self.wfile.write(f"event: {ev['event']}\ndata: {body}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.store.unsubscribe(channel, q)

    def note_bundle(self, slug: str) -> dict:
        notes = [
            {
                "id": row["id"],
                "q": row["q"],
                "pre": row["pre"],
                "post": row["post"],
                "block": row["block"],
                "t": row["t"],
                "thread": json.loads(row["thread"]),
                "updated": row["updated"],
                "job": job_summary(latest) if (latest := self.store.latest_job(row["id"])) else None,
            }
            for row in self.store.notes_for(slug)
        ]
        return {"notes": notes, "deleted": self.store.deleted_ids(slug)}

    def route(self, write: bool) -> None:
        if not self.limiter.allow(self.client_ip(), write):
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate limited"})
            return
        parts = self.split_path()
        if parts and parts[0] == "api":
            parts = parts[1:]
        if not parts:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        if parts[0] == "healthz":
            self.send_json(HTTPStatus.OK, {"ok": True, "time": now()})
            return

        # 阅读进度与收藏是匿名的（每个访客都会写），不收口令；笔记与任务要口令。
        anonymous = parts[0] in ("progress", "favorites")
        if write and not anonymous and not self.write_allowed():
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "edit key required"})
            return

        if parts[0] == "summary" and self.command == "GET":
            self.send_json(
                HTTPStatus.OK,
                {
                    "notes": self.store.counts(),
                    "progress": self.store.progress(),
                    "favorites": self.store.favorites(),
                    "updated": now(),
                },
            )
            return

        if parts[0] == "progress":
            if self.command == "GET" and len(parts) == 1:
                self.send_json(HTTPStatus.OK, {"progress": self.store.progress()})
                return
            if self.command == "PUT" and len(parts) == 2:
                slug = parts[1]
                if not SLUG_RE.match(slug):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad slug"})
                    return
                body = self.read_json()
                if body is None:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                    return
                p = body.get("p")
                t = body.get("t")
                if not isinstance(p, (int, float)) or not isinstance(t, (int, float)):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad values"})
                    return
                p = max(0, min(100, int(p)))
                result = self.store.put_progress(slug, p, int(t), bool(body.get("d")))
                self.send_json(HTTPStatus.OK, {"ok": True, **result})
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        if parts[0] == "favorites":
            if self.command == "GET" and len(parts) == 1:
                self.send_json(HTTPStatus.OK, {"favorites": self.store.favorites()})
                return
            if len(parts) == 2:
                slug = parts[1]
                if not SLUG_RE.match(slug):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad slug"})
                    return
                if self.command == "PUT":
                    body = self.read_json() or {}
                    t = body.get("t")
                    if not isinstance(t, (int, float)):
                        t = now()
                    self.send_json(HTTPStatus.OK, {"ok": True, **self.store.put_favorite(slug, int(t))})
                    return
                if self.command == "DELETE":
                    self.send_json(HTTPStatus.OK, {"ok": True, "deleted": self.store.delete_favorite(slug)})
                    return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        if parts[0] == "jobs" and len(parts) == 2 and parts[1] == "stream" and self.command == "GET":
            expected = type(self).bridge_token
            if not expected:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "bridge token not configured"})
                return
            if self.headers.get("X-Bridge-Token", "") != expected:
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "bridge token required"})
                return
            self.stream("jobs")
            return

        if parts[0] == "jobs":
            if len(parts) == 1 and self.command == "GET":
                query = parse_qs(urlparse(self.path).query)
                status = (query.get("status") or ["pending"])[0]
                if status != "pending":
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "only status=pending"})
                    return
                self.send_json(HTTPStatus.OK, {"jobs": self.store.pending_jobs()})
                return
            if len(parts) == 3 and self.command == "POST" and parts[2] == "claim":
                body = self.read_json()
                if body is None:
                    body = {}
                worker = clean_text(body.get("worker"), 40) or "bridge"
                job = self.store.claim_job(parts[1], worker)
                if job is None:
                    self.send_json(HTTPStatus.CONFLICT, {"error": "not claimable"})
                    return
                self.send_json(HTTPStatus.OK, {"ok": True, "job": job})
                return
            if len(parts) == 3 and self.command == "POST" and parts[2] == "done":
                body = self.read_json()
                if body is None:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                    return
                job = self.store.finish_job(parts[1], bool(body.get("ok")), clean_text(body.get("error"), 200))
                if job is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "no such job"})
                    return
                self.send_json(HTTPStatus.OK, {"ok": True, "job": job})
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        if parts[0] == "sessions" and len(parts) == 2:
            slug = parts[1]
            if not SLUG_RE.match(slug):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad slug"})
                return
            if self.command == "GET":
                self.send_json(HTTPStatus.OK, {"session": self.store.session_for(slug)})
                return
            if self.command == "PUT":
                body = self.read_json()
                if body is None:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                    return
                sid = body.get("session_id") or ""
                if not SESSION_RE.match(str(sid)):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad session id"})
                    return
                ws = str(body.get("workspace") or "")
                if ws and not WS_RE.match(ws):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad workspace"})
                    return
                title = clean_text(body.get("title"), 120)
                session = self.store.upsert_session(slug, str(sid), ws, title)
                self.send_json(HTTPStatus.OK, {"ok": True, "session": session})
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        if parts[0] == "notes":
            if len(parts) == 1 and self.command == "GET":
                query = parse_qs(urlparse(self.path).query)
                raw = (query.get("slugs") or [""])[0]
                if raw:
                    # 批量取：首页要确认「本地有、服务端没有」的笔记，一篇文章一次请求太吵。
                    slugs = [item for item in raw.split(",") if item][:50]
                    if any(not SLUG_RE.match(slug) for slug in slugs):
                        self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad slug"})
                        return
                    self.send_json(
                        HTTPStatus.OK,
                        {"items": {slug: self.note_bundle(slug) for slug in slugs}},
                    )
                    return
                self.send_json(HTTPStatus.OK, {"notes": self.store.counts()})
                return
            if len(parts) == 3 and parts[2] == "stream" and self.command == "GET":
                slug = parts[1]
                if not SLUG_RE.match(slug):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad slug"})
                    return
                self.stream(f"note:{slug}")
                return

            if len(parts) == 2 and self.command == "GET":
                slug = parts[1]
                if not SLUG_RE.match(slug):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad slug"})
                    return
                self.send_json(HTTPStatus.OK, self.note_bundle(slug))
                return
            if len(parts) == 4 and parts[3] == "ask" and self.command == "POST":
                slug, note_id = parts[1], parts[2]
                if not SLUG_RE.match(slug) or not ID_RE.match(note_id):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
                    return
                # 没绑会话也照答：bridge 会用文章正文自建一条会话，只是少了历史上下文。
                session = self.store.session_for(slug)
                if self.store.active_job(note_id):
                    self.send_json(HTTPStatus.CONFLICT, {"error": "already queued"})
                    return
                row = self.store.note(slug, note_id)
                if row is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "no such note"})
                    return
                thread = json.loads(row["thread"])
                prompt = (thread[0].get("text") if thread else "") or row["q"]
                body = self.read_json() or {}
                if body.get("auto") and not QUESTION_RE.search(prompt):
                    self.send_json(HTTPStatus.OK, {"ok": True, "triggered": False, "job": None})
                    return
                job = self.store.create_job(
                    slug,
                    note_id,
                    (session["session_id"] if session else ""),
                    prompt,
                    "auto" if body.get("auto") else "manual",
                )
                self.send_json(HTTPStatus.OK, {"ok": True, "triggered": True, "job": job})
                return

            if len(parts) == 4 and parts[3] == "replies" and self.command == "POST":
                slug, note_id = parts[1], parts[2]
                if not SLUG_RE.match(slug) or not ID_RE.match(note_id):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
                    return
                body = self.read_json()
                if body is None:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                    return
                text = clean_text(body.get("text"), MAX_THREAD_TEXT).strip()
                if not text:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "empty reply"})
                    return
                reply = {
                    "author": clean_text(body.get("author"), 20) or "我",
                    "text": text,
                    "t": int(body.get("t") or now()),
                }
                status, note = self.store.add_reply(slug, note_id, reply)
                if status == "missing":
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "no such note"})
                    return
                if status == "full":
                    self.send_json(HTTPStatus.CONFLICT, {"error": "thread full"})
                    return
                session = self.store.session_for(slug)
                job = None
                if (
                    session
                    and QUESTION_RE.search(reply["text"])
                    and not self.store.active_job(note_id)
                ):
                    thread = (note or {}).get("thread") or []
                    prompt = (thread[0].get("text") if thread else "") or reply["text"]
                    job = self.store.create_job(
                        slug, note_id, (session["session_id"] if session else ""), prompt, "auto"
                    )
                self.send_json(HTTPStatus.OK, {"ok": True, "note": note, "triggered": job is not None, "job": job})
                return

            if len(parts) == 3 and self.command == "PUT":
                slug, note_id = parts[1], parts[2]
                if not SLUG_RE.match(slug) or not ID_RE.match(note_id):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
                    return
                body = self.read_json()
                if body is None:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                    return
                body["id"] = note_id
                note = validate_note(body)
                if note is None:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid note"})
                    return
                self.store.put_note(slug, note)
                self.send_json(HTTPStatus.OK, {"ok": True, "note": note})
                return
            if len(parts) == 3 and self.command == "DELETE":
                slug, note_id = parts[1], parts[2]
                if not SLUG_RE.match(slug) or not ID_RE.match(note_id):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
                    return
                self.send_json(HTTPStatus.OK, {"ok": self.store.delete_note(slug, note_id)})
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="notesd")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--db", default=os.environ.get("NOTES_DB", "notes.db"))
    args = parser.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    Handler.store = Store(args.db)
    Handler.limiter = Limiter()
    Handler.edit_key = os.environ.get("NOTES_EDIT_KEY", "").strip()
    Handler.bridge_token = os.environ.get("BRIDGE_TOKEN", "").strip()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"notesd listening on http://{args.host}:{args.port} db={args.db}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        Handler.store.close()


if __name__ == "__main__":
    main()
