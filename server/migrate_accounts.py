"""把 v1 的 notesd 数据库迁到 v2（按用户隔离）。

用法：
    python3 server/migrate_accounts.py /var/lib/notesd/notes.db --dry-run
    sudo systemctl stop notesd
    sudo python3 server/migrate_accounts.py /var/lib/notesd/notes.db
    sudo systemctl start notesd

迁移会先做一份 `notes.db.bak-<时间戳>`，然后把存量笔记、进度、收藏全部归给
Root，并给 bridge 生成一条不过期的 admin token。输出 JSON 里有 root_password
与 bridge_token，两者都只打印这一次。
"""

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notesd  # noqa: E402

USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  username       TEXT NOT NULL,
  username_lower TEXT NOT NULL UNIQUE,
  pass_hash      TEXT NOT NULL,
  role           TEXT NOT NULL DEFAULT 'user',
  created        INTEGER NOT NULL DEFAULT 0
)
"""

TOKENS_DDL = """
CREATE TABLE IF NOT EXISTS auth_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  created    INTEGER NOT NULL,
  expires    INTEGER NOT NULL DEFAULT 0,
  name       TEXT NOT NULL DEFAULT 'browser'
)
"""

BACKUP_SUFFIX = ".bak-"


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _count(conn, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _now() -> int:
    return int(time.time() * 1000)


def migrate(db_path: str, root_password: str | None = None, dry_run: bool = False) -> dict:
    root_password = root_password or secrets.token_urlsafe(12)
    counts = {
        "notes": 0,
        "progress": 0,
        "favorites": 0,
    }
    bridge_token = secrets.token_urlsafe(32)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        counts = {name: _count(conn, name) for name in counts}
        if dry_run:
            return {
                "root_id": None,
                "root_password": root_password,
                "bridge_token": bridge_token,
                "counts": counts,
                "dry_run": True,
            }

        backup = f"{db_path}{BACKUP_SUFFIX}{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(db_path, backup)

        conn.execute("BEGIN")
        conn.execute(USERS_DDL)
        conn.execute(TOKENS_DDL)

        # 1) notes 加 user_id
        if "user_id" not in _columns(conn, "notes"):
            conn.execute("ALTER TABLE notes ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='notes_user_slug'"
        ).fetchone():
            conn.execute("CREATE INDEX IF NOT EXISTS notes_user_slug ON notes(user_id, slug, deleted)")

        # 2) progress / favorites 换成复合主键
        if "user_id" not in _columns(conn, "progress"):
            conn.execute(
                "CREATE TABLE progress_v2 (user_id INTEGER NOT NULL DEFAULT 0, slug TEXT NOT NULL,"
                " p INTEGER NOT NULL, t INTEGER NOT NULL, d INTEGER NOT NULL, PRIMARY KEY (user_id, slug))"
            )
            conn.execute(
                "INSERT INTO progress_v2 (user_id, slug, p, t, d) SELECT 0, slug, p, t, d FROM progress"
            )
            conn.execute("DROP TABLE progress")
            conn.execute("ALTER TABLE progress_v2 RENAME TO progress")
        if "user_id" not in _columns(conn, "favorites"):
            conn.execute(
                "CREATE TABLE favorites_v2 (user_id INTEGER NOT NULL DEFAULT 0, slug TEXT NOT NULL,"
                " t INTEGER NOT NULL, updated INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, slug))"
            )
            conn.execute(
                "INSERT INTO favorites_v2 (user_id, slug, t, updated) SELECT 0, slug, t, updated FROM favorites"
            )
            conn.execute("DROP TABLE favorites")
            conn.execute("ALTER TABLE favorites_v2 RENAME TO favorites")

        # 3) Root
        row = conn.execute("SELECT id FROM users WHERE username_lower = 'root'").fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO users (username, username_lower, pass_hash, role, created)"
                " VALUES ('Root', 'root', ?, 'admin', ?)",
                (notesd.hash_password(root_password), _now()),
            )
            root_id = cur.lastrowid
        else:
            root_id = row[0]

        # 4) 存量归 Root
        conn.execute("UPDATE notes SET user_id = ? WHERE user_id = 0", (root_id,))
        conn.execute("UPDATE progress SET user_id = ? WHERE user_id = 0", (root_id,))
        conn.execute("UPDATE favorites SET user_id = ? WHERE user_id = 0", (root_id,))

        # 5) bridge 的长期 admin token
        existing = conn.execute(
            "SELECT token_hash FROM auth_tokens WHERE name = 'bridge'"
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO auth_tokens (token_hash, user_id, created, expires, name)"
                " VALUES (?, ?, ?, 0, 'bridge')",
                (notesd.token_hash(bridge_token), root_id, _now()),
            )
        else:
            # 已经有 bridge token 时不再生成新的，调用方用旧的。这里直接把新 token
            # 覆盖进去，避免两处不一致（bridge 会从环境变量读）。
            conn.execute(
                "UPDATE auth_tokens SET token_hash = ?, user_id = ? WHERE name = 'bridge'",
                (notesd.token_hash(bridge_token), root_id),
            )

        conn.execute(f"PRAGMA user_version = {notesd.SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()

    return {
        "root_id": root_id,
        "root_password": root_password,
        "bridge_token": bridge_token,
        "counts": counts,
        "backup": backup,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移 notesd 数据库到 v2")
    parser.add_argument("db")
    parser.add_argument("--password", default=None, help="Root 的初始密码；不给就随机生成")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    out = migrate(args.db, root_password=args.password, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
