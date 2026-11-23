"""迁移脚本的测试：v1 库（slug 主键、没有 user）迁到 v2（按 user 隔离）。"""

import os
import sqlite3
import tempfile
import unittest

import migrate_accounts

V1_SCHEMA = """
CREATE TABLE notes (
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
CREATE TABLE progress (
  slug    TEXT PRIMARY KEY,
  p       INTEGER NOT NULL,
  t       INTEGER NOT NULL,
  d       INTEGER NOT NULL
);
CREATE TABLE favorites (
  slug    TEXT PRIMARY KEY,
  t       INTEGER NOT NULL,
  updated INTEGER NOT NULL DEFAULT 0
);
"""


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "notes.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(V1_SCHEMA)
        conn.execute(
            "INSERT INTO notes (id, slug, q, pre, post, block, t, thread, deleted, updated)"
            " VALUES ('n1abcde','ot-crdt-lww','收敛','','',4,1,'[]',0,1)"
        )
        conn.execute("INSERT INTO progress (slug, p, t, d) VALUES ('ot-crdt-lww', 100, 1, 1)")
        conn.execute("INSERT INTO favorites (slug, t, updated) VALUES ('ot-crdt-lww', 1, 1)")
        conn.commit()
        conn.close()

    def test_dry_run_changes_nothing(self):
        out = migrate_accounts.migrate(self.db, dry_run=True)
        self.assertEqual(out["counts"], {"notes": 1, "progress": 1, "favorites": 1})
        self.assertIsNotNone(out["root_password"])
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        conn.close()

    def test_migrate_makes_a_backup_first(self):
        migrate_accounts.migrate(self.db, root_password="root-pass-1")
        backups = [p for p in os.listdir(self.tmp.name) if p.startswith("notes.db.bak-")]
        self.assertEqual(len(backups), 1)

    def test_migrate_assigns_rows_to_root(self):
        out = migrate_accounts.migrate(self.db, root_password="root-pass-1")
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        for table in ("notes", "progress", "favorites"):
            stray = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id != ?", (out["root_id"],)
            ).fetchone()[0]
            self.assertEqual(stray, 0)
        self.assertTrue(out["bridge_token"])
        conn.close()

    def test_bridge_token_can_read_jobs(self):
        """迁移产出的 token 必须是 admin 的长期 token，bridge 才能拉任务。"""
        out = migrate_accounts.migrate(self.db, root_password="root-pass-1")
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT u.role, t.expires, t.name FROM auth_tokens t JOIN users u ON u.id = t.user_id"
            " WHERE t.name = 'bridge'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "admin")
        self.assertEqual(row[1], 0)
        self.assertEqual(row[2], "bridge")


if __name__ == "__main__":
    unittest.main()
