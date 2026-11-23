# 用户账号与数据隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 站点支持用户名密码登录，已读/收藏/批注按账号隔离，存量数据归管理员 Root，废弃全局口令。

**Architecture:** 服务端在 `server/notesd.py` 内加 `users` / `auth_tokens` 两张表与 Bearer 鉴权，把 `notes`、`progress`、`favorites` 按 `user_id` 分区；客户端新增 `assets/js/auth.js` 与 `assets/js/user-store.js`，本地缓存按用户分键，未登录只写本机。

**Tech Stack:** Python 3.12 标准库（`ThreadingHTTPServer` + SQLite）、Hugo 静态站、原生 JS（`js.Build`）、Playwright 浏览器测试、`node --test`。

**Spec:** `blog/docs/superpowers/specs/2026-11-23-user-accounts-design.md`

## Global Constraints

- 服务端只用 Python 标准库，不引入第三方包；SQLite 用 WAL。
- 密码 PBKDF2-HMAC-SHA256、120000 轮、16 字节盐，存储格式 `pbkdf2$120000$<salt hex>$<hash hex>`。
- token 用 `secrets.token_urlsafe(32)`，服务端只存 SHA-256 十六进制；有效期 180 天（`expires=0` 表示不失效，仅 bridge token 使用）。
- 除 `/healthz` 外所有 `/api` 都要 Bearer token；`/api/jobs/*`、`/api/sessions/*` 只允许 `role=admin`。
- 用户名 3–24 字符，只允许 `[A-Za-z0-9_-]`；唯一性按小写比较；管理员显示名固定 `Root`。
- 非 admin 不建 job、看不到「让 AI 回答」；回复自己的笔记只追加楼层。
- 未登录访客：可读全文，已读/收藏/批注只存本机，不发任何状态请求。
- 登录失败返回 401 `{"error":"bad credentials"}`，不区分用户不存在与密码错；登录限速每 IP 每分钟 10 次，超出 429。
- 提交信息用 `feat(accounts): …` / `fix(accounts): …`，一个任务一个提交。

## Review Focus

1. 迁移顺序与失败恢复：旧库（`user_version=0`）上先跑脚本再装新服务；脚本中途失败时库必须还能回到旧版本可用的备份，不能留下半迁移状态。
2. 跨账号泄漏：`/api/summary`、笔记批量接口、SSE 频道、job 列表任意一处漏 `user_id` 过滤，就把 Root 的私有数据给了别人。
3. 未登录客户端的 401 噪声：访客打开首页不能出现任何鉴权报错、不能空列表、不能反复重试。
4. 楼层只增不减：旧的整条 PUT 在服务端不能抹掉已追加的 AI 楼层（今天靠 `put_note` 的尾巴保留逻辑，加 `user_id` 后必须继续成立）。
5. 退出与离线写入：退出时队列未 flush 成功不能删缓存；换账号后不能看到上一个账号的笔记、星标、已读。

---

### Task 1: 账号表、登录与 token 鉴权

**Files:**
- Modify: `server/notesd.py`（`SCHEMA_VERSION`、`hash_password`、`verify_password`、`Store` 的 users/tokens 方法、`Handler.current_user`、`route` 的 `login|me|logout|users` 分支、`limiter` 登录限速）
- Test: `server/test_notesd.py`

**Interfaces:**
- Produces:
  - `hash_password(password: str) -> str`；`verify_password(password: str, stored: str) -> bool`
  - `Store.create_user(username: str, password: str, role: str = "user") -> dict | None`（返回 `{id, username, role}`，重名返回 `None`）
  - `Store.user_by_name(username: str) -> dict | None`；`Store.user_by_id(user_id: int) -> dict | None`
  - `Store.create_token(user_id: int, name: str = "browser", expires: int = 0) -> str`（返回原始 token，库里存 hash；`expires=0` 时取 now+180 天）
  - `Store.user_for_token(token: str) -> dict | None`
  - `Store.drop_token(token: str) -> None`
  - `Handler.current_user() -> dict | None`（读 `Authorization: Bearer …`）
  - 路由：`POST /api/login` → `{token, user}`；`POST /api/logout` → 204；`GET /api/me` → `{user}`；`POST /api/users`（admin）→ `{user}`，重名 409、非 admin 403。

- [ ] **Step 1: 写失败测试**

在 `server/test_notesd.py` 顶部 `setUpClass` 里把 `notesd.Handler.edit_key = ""` 改成 `notesd.Handler.edit_key = ""`（保留），新增容器 `SCHEMA_VERSION` 断言与新用例：

```python
def test_login_and_me(self):
    self.admin = self.seed_user("Root", "root-pass-1", "admin")
    _, body = self.call("POST", "/api/login", {"username": "root", "password": "root-pass-1"})
    self.assertEqual(body["user"]["role"], "admin")
    token = body["token"]
    status, me = self.call("GET", "/api/me", headers={"Authorization": f"Bearer {token}"})
    self.assertEqual((status, me["user"]["username"]), (200, "Root"))

def test_login_rejects_bad_password(self):
    self.seed_user("Root", "root-pass-1", "admin")
    status, body = self.call("POST", "/api/login", {"username": "Root", "password": "nope"})
    self.assertEqual((status, body["error"]), (401, "bad credentials"))

def test_login_is_rate_limited(self):
    for _ in range(10):
        self.call("POST", "/api/login", {"username": "x", "password": "y"})
    self.assertEqual(self.call("POST", "/api/login", {"username": "x", "password": "y"})[0], 429)

def test_notes_require_token(self):
    self.assertEqual(self.call("GET", "/api/notes/ot-crdt-lww")[0], 401)

def test_admin_can_create_user_and_duplicate_is_409(self):
    token = self.admin_token()
    self.assertEqual(self.call("POST", "/api/users", {"username": "Alice", "password": "alice-pass"},
                              self.auth(token))[0], 200)
    self.assertEqual(self.call("POST", "/api/users", {"username": "alice", "password": "x2"},
                              self.auth(token))[0], 409)

def test_non_admin_cannot_create_user(self):
    self.seed_user("Alice", "alice-pass", "user")
    token = self.token_for("Alice", "alice-pass")
    self.assertEqual(self.call("POST", "/api/users", {"username": "Bob", "password": "bob-pass"},
                              self.auth(token))[0], 403)
```

辅助方法（放在 `NotesdTest` 里，后续任务复用）：

```python
def seed_user(self, username, password, role="user"):
    return notesd.Handler.store.create_user(username, password, role)

def token_for(self, username, password):
    _, body = self.call("POST", "/api/login", {"username": username, "password": password})
    return body["token"]

def admin_token(self):
    return self.token_for("Root", "root-pass-1")

def auth(self, token):
    return {"Authorization": f"Bearer {token}"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 server/test_notesd.py`
Expected: 新增用例失败，`Store` 无 `create_user`。

本任务**不收紧**任何既有接口：笔记、进度、收藏仍按老规则匿名可读写，`X-Edit-Key` 原样保留，所有既有用例保持绿色。鉴权在 T2 一次性切换，避免中间提交带着一片红。

- [ ] **Step 3: 实现**

`server/notesd.py` 顶部加：

```python
SCHEMA_VERSION = 2
PBKDF2_ROUNDS = 120_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,24}$")
TOKEN_TTL = 180 * 24 * 3600

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)
```

`Store.__init__` 建表加 `users`、`auth_tokens`（见 spec §6.2），`PRAGMA user_version = SCHEMA_VERSION`。

`Store` 方法按 Interfaces 实现；`create_token` 用 `secrets.token_urlsafe(32)`，存 `hashlib.sha256(token.encode()).hexdigest()`；`user_for_token` 校验 hash 存在且（`expires == 0 or expires > now()`）。

`Handler.current_user()`：

```python
def current_user(self):
    header = self.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    return self.store.user_for_token(token) if token else None
```

`route` 在 `healthz` 之后、其它分支之前插入登录路由（`login`` 不要求 token，且走单独的登录限速计数）：

```python
if parts[0] == "login" and self.command == "POST":
    if not self.login_limiter.allow(self.client_ip()):
        self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate limited"}); return
    body = self.read_json() or {}
    user = self.store.user_by_name(str(body.get("username", "")))
    if user is None or not verify_password(str(body.get("password", "")), user["pass_hash"]):
        self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "bad credentials"}); return
    token = self.store.create_token(user["id"], "browser")
    self.send_json(HTTPStatus.OK, {"token": token, "user": {"id": user["id"], "username": user["username"], "role": user["role"]}})
    return
```

`logout` 用当前 Bearer 删 token；`me` 返回当前用户；`users` 建号（`role` 只能落 `user`，Root 由迁移脚本创建）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 server/test_notesd.py`
Expected: 全部通过——新增的 6 个用例绿，既有的匿名用例不受影响。

- [ ] **Step 5: 提交**

```bash
git add server/notesd.py server/test_notesd.py
git commit -m "feat(accounts): users, tokens and bearer login"
```

---

### Task 2: 按用户隔离笔记、进度、收藏与 summary

**Files:**
- Modify: `server/notesd.py`（`Store.notes_for/note/put_note/delete_note/progress/put_progress/favorites/put_favorite/delete_favorite/counts` 加 `user_id` 参数；`Handler.note_bundle`、`route` 的 `summary|progress|favorites|notes` 分支）
- Test: `server/test_notesd.py`

**Interfaces:**
- Consumes: Task 1 的 `current_user()`、`auth()` helper。
- Produces: 所有数据方法签名带 `user_id: int` 作为第一个参数；`note_bundle(user_id, slug)`。

- [ ] **Step 1: 写失败测试**

```python
def test_two_users_have_separate_notes(self):
    self.seed_user("Alice", "alice-pass"); self.seed_user("Bob", "bob-pass")
    ta, tb = self.token_for("Alice", "alice-pass"), self.token_for("Bob", "bob-pass")
    self.call("PUT", "/api/notes/ot-crdt-lww/a1note", self.note("a1note"), self.auth(ta))
    self.assertEqual(self.call("GET", "/api/notes/ot-crdt-lww", headers=self.auth(tb))[1]["notes"], [])
    self.assertEqual(len(self.call("GET", "/api/notes/ot-crdt-lww", headers=self.auth(ta))[1]["notes"]), 1)

def test_put_cannot_overwrite_another_users_note(self):
    self.seed_user("Alice", "alice-pass"); self.seed_user("Bob", "bob-pass")
    ta, tb = self.token_for("Alice", "alice-pass"), self.token_for("Bob", "bob-pass")
    self.call("PUT", "/api/notes/ot-crdt-lww/a1note", self.note("a1note"), self.auth(ta))
    stolen = self.note("a1note", q="别人的笔记")
    self.assertEqual(self.call("PUT", "/api/notes/ot-crdt-lww/a1note", stolen, self.auth(tb))[0], 404)

def test_progress_and_favorites_are_per_user(self):
    self.seed_user("Alice", "alice-pass"); self.seed_user("Bob", "bob-pass")
    ta, tb = self.token_for("Alice", "alice-pass"), self.token_for("Bob", "bob-pass")
    self.call("PUT", "/api/progress/ot-crdt-lww", {"p": 100, "t": 1, "d": True}, self.auth(ta))
    self.call("PUT", "/api/favorites/ot-crdt-lww", {}, self.auth(ta))
    _, a = self.call("GET", "/api/summary", headers=self.auth(ta))
    _, b = self.call("GET", "/api/summary", headers=self.auth(tb))
    self.assertEqual((a["progress"]["ot-crdt-lww"]["p"], len(a["favorites"])), (100, 1))
    self.assertEqual((b["progress"], b["favorites"]), ({}, {}))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 server/test_notesd.py`
Expected: `test_two_users_have_separate_notes` 失败（Bob 能看到 Alice 的笔记）。

- [ ] **Step 3: 实现**

`Store` 各方法加 `user_id` 首参，SQL 一律带 `AND user_id = ?`；`put_note` 先 `SELECT ... WHERE id=? AND user_id=?`，不存在则视为新插入（但若该 id 属于别人，`INSERT` 会撞主键 → 捕获后返回 `False`，路由返回 404）。`delete_note` 同样带 `user_id`。`counts(user_id)`、`progress(user_id)`、`favorites(user_id)` 按 `WHERE user_id = ?`。

`note_bundle(user_id, slug)` 只返回该用户的笔记与删除墓碑。路由在每个分支先 `user = self.current_user()`，`None` 就 401。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 server/test_notesd.py`
Expected: 全部通过；把旧用例的 `self.call` 改成带 `self.auth(self.admin_token())`。注意 `test_put_cannot_drop_appended_floors` 必须继续绿：`put_note` 加 `user_id` 后，尾巴保留逻辑要在 `WHERE id=? AND user_id=?` 查到的旧行上做。

- [ ] **Step 5: 提交**

```bash
git add server/notesd.py server/test_notesd.py
git commit -m "feat(accounts): scope notes, progress and favorites per user"
```

---

### Task 3: jobs、sessions 与回复触发只对 Root 开放

**Files:**
- Modify: `server/notesd.py`（`route` 的 `jobs`、`sessions`、`ask`、`replies` 分支；删掉 `Handler.bridge_token` 与 `Handler.edit_key`、`write_allowed`）
- Modify: `server/notes_bridge.py`（去掉 `X-Edit-Key`，改用 `Authorization: Bearer <NOTES_API_TOKEN>`）
- Test: `server/test_notesd.py`、`server/test_notes_bridge.py`

**Interfaces:**
- Consumes: Task 1/2 的 `current_user()` 与 per-user Store 方法。
- Produces: `/api/jobs/*`、`/api/jobs/stream`、`/api/sessions/*` 都需要 admin token；`POST /api/notes/<slug>/<id>/ask` 非 admin 返回 403 `{"error":"admin only"}`；自动触发只在 admin 回复自己的笔记时发生。

- [ ] **Step 1: 写失败测试**

```python
def test_jobs_require_admin(self):
    self.seed_user("Alice", "alice-pass")
    ta = self.token_for("Alice", "alice-pass")
    self.assertEqual(self.call("GET", "/api/jobs?status=pending")[0], 401)
    self.assertEqual(self.call("GET", "/api/jobs?status=pending", headers=self.auth(ta))[0], 403)
    self.assertEqual(self.call("GET", "/api/jobs?status=pending", headers=self.auth(self.admin_token()))[0], 200)

def test_non_admin_reply_does_not_enqueue_ai(self):
    root = self.admin_token()
    self.call("PUT", "/api/sessions/ot-crdt-lww", {"session_id": "session-abcdefgh", "workspace": "ws"}, self.auth(root))
    self.seed_user("Alice", "alice-pass")
    ta = self.token_for("Alice", "alice-pass")
    self.call("PUT", "/api/notes/ot-crdt-lww/a1note", self.note("a1note"), self.auth(ta))
    _, body = self.call("POST", "/api/notes/ot-crdt-lww/a1note/replies",
                        {"author": "我", "text": "为什么不加锁？"}, self.auth(ta))
    self.assertIsNone(body.get("job"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 server/test_notesd.py`
Expected: `test_jobs_require_admin` 中无 token 请求返回 200。

- [ ] **Step 3: 实现**

路由里在 `jobs`/`sessions`/`ask`/`replies` 前取 `user = self.current_user()`，`user is None → 401`，`user["role"] != "admin" → 403 {"error":"admin only"}`。`replies` 允许普通用户写**自己的**笔记：先 `store.note(user["id"], slug, id)`，`None → 404`；自动建 job 的条件加 `user["role"] == "admin"`。删除 `Handler.bridge_token`、`Handler.edit_key`、`write_allowed()`，`BRIDGE_TOKEN` 环境变量不再读取。

`notes_bridge.py`：`api()` 里的 `X-Edit-Key` 换成 `Authorization: Bearer {NOTES_API_TOKEN}`；`_load_env_file` 增读 `NOTES_API_TOKEN`；SSE 订阅 `jobs/stream` 同样带 Bearer，不再用 `X-Bridge-Token`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 server/test_notesd.py && python3 server/test_notes_bridge.py`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add server/notesd.py server/notes_bridge.py server/test_notesd.py server/test_notes_bridge.py
git commit -m "feat(accounts): gate AI jobs and sessions to admin"
```

---

### Task 4: 迁移脚本（schema v2 + Root + bridge token）

**Files:**
- Create: `server/migrate_accounts.py`
- Test: `server/test_migrate_accounts.py`

**Interfaces:**
- Consumes: `notesd.hash_password`、`notesd.SCHEMA_VERSION`。
- Produces: `migrate(db_path: str, root_password: str | None = None, dry_run: bool = False) -> dict`，返回 `{"root_id", "root_password", "bridge_token", "counts": {"notes": n, "progress": n, "favorites": n}}`；CLI `python3 server/migrate_accounts.py <db> [--password …] [--dry-run]` 打印同样的 JSON。

- [ ] **Step 1: 写失败测试**

```python
class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "notes.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(V1_SCHEMA)   # 测试文件内嵌旧建表 SQL（notes/progress/favorites）
        conn.execute("INSERT INTO notes (id, slug, q, pre, post, block, t, thread, deleted, updated) VALUES ('n1abcde','ot-crdt-lww','收敛','','',4,1,'[]',0,1)")
        conn.execute("INSERT INTO progress (slug, p, t, d) VALUES ('ot-crdt-lww', 100, 1, 1)")
        conn.execute("INSERT INTO favorites (slug, t, updated) VALUES ('ot-crdt-lww', 1, 1)")
        conn.commit(); conn.close()

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
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id != ?", (out["root_id"],)).fetchone()[0], 0)
        self.assertTrue(out["bridge_token"])
        conn.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 server/test_migrate_accounts.py`
Expected: `ImportError` / 文件不存在。

- [ ] **Step 3: 实现**

`migrate()` 步骤：备份文件路径 `f"{db_path}.bak-<ts>"` 用 `shutil.copy2`（`dry_run` 时跳过）；连库后依次：

1. `ALTER TABLE notes ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0`（若列不存在，用 `PRAGMA table_info` 判断）。
2. 建 `progress_new(user_id, slug, p, t, d, PRIMARY KEY(user_id, slug))`、`favorites_new(...)`，`INSERT ... SELECT 0, ...`，`DROP TABLE progress`，`ALTER TABLE progress_new RENAME TO progress`；favorites 同理。
3. 建 `users`、`auth_tokens`（同 notesd 的定义），若 Root 不存在则插入，密码为参数或 `secrets.token_urlsafe(12)`。
4. `UPDATE notes SET user_id = ?`（Root id）、`UPDATE progress ...`、`UPDATE favorites ...`。
5. 生成 bridge token（`expires=0`，`name='bridge'`），插入 `auth_tokens`。
6. `PRAGMA user_version = 2`，提交。

`dry_run` 时只读、算出行数并返回生成的密码/token 占位（token 不落库）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 server/test_migrate_accounts.py && python3 server/test_notesd.py`
Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add server/migrate_accounts.py server/test_migrate_accounts.py
git commit -m "feat(accounts): migration script for schema v2 and Root"
```

---

### Task 5: 客户端登录、退出与顶部账号栏

**Files:**
- Create: `assets/js/auth.js`
- Create: `layouts/partials/account.html`
- Modify: `layouts/_default/baseof.html`（在 `<main class="page">` 后插入 `{{ partial "account.html" . }}`）
- Modify: `layouts/partials/head.html`（`.IsHome .IsPage` 时加载 `auth.js`）
- Modify: `assets/css/site.css`（`.account-bar`、`.account-btn`、`.auth-dialog`）
- Test: `scripts/tests/test_accounts.py`

**Interfaces:**
- Produces:
  - `assets/js/auth.js` 导出 `AUTH_KEY = "jasoncai:auth:v1"`、`token() -> string`、`current() -> {id, username, role} | null`、`isAdmin() -> boolean`、`login(username, password) -> Promise<void>`、`logout() -> Promise<void>`、`mountAccountBar()`。
  - `account.html` 渲染 `<div class="account-bar"><button class="account-btn"></button></div>`；JS 负责打开对话框。

- [ ] **Step 1: 写失败测试**

`scripts/tests/test_accounts.py` 沿用 `test_favorites.py` 的 `CASES` + `run_case` 结构；`login_as(page, base, username="Root", password="root-pass-1")` 走 UI：

```python
def login_as(page, base, username="Root", password="root-pass-1"):
    page.goto(base)
    page.click(".account-btn")
    page.fill(".auth-dialog input[name=username]", username)
    page.fill(".auth-dialog input[name=password]", password)
    page.click(".auth-dialog button[type=submit]")
    page.wait_for_function("() => Boolean(JSON.parse(localStorage.getItem('jasoncai:auth:v1') || 'null'))", timeout=10000)
    page.wait_for_load_state("load")

def test_login_and_logout(page, base):
    login_as(page, base)
    page.wait_for_selector(".account-bar .account-name")
    assert "Root" in page.locator(".account-bar").inner_text()
    uid = page.evaluate("() => JSON.parse(localStorage.getItem('jasoncai:auth:v1')).id")
    page.click(".account-bar .account-logout")
    page.wait_for_function("() => !localStorage.getItem('jasoncai:auth:v1')")
    page.wait_for_timeout(300)
    for base_key in ("jasoncai:read:v2", "jasoncai:fav:v2", "jasoncai:notes:v2"):
        assert page.evaluate("([k, u]) => !localStorage.getItem(k + ':u' + u)", [base_key, uid]), base_key

def test_bad_password_shows_message_without_crash(page, base):
    page.goto(base); page.click(".account-btn")
    page.fill(".auth-dialog input[name=username]", "Root")
    page.fill(".auth-dialog input[name=password]", "wrong")
    page.click(".auth-dialog button[type=submit]")
    page.wait_for_selector(".auth-dialog .auth-error")
    assert "用户名或密码" in page.locator(".auth-dialog .auth-error").inner_text()
```

测试用的 notesd 进程由 `run_case` 现有 fixture 启动，需在 `main()` 前 `POST /api/users` 建好 Root 与 Alice（用服务端 bearer token 或迁移脚本；在测试 fixture 初始化时调用 `migrate_accounts.migrate(db, root_password="root-pass-1")`）。

- [ ] **Step 2: 跑测试确认失败**

Run: `hugo --minify && python3 scripts/tests/test_accounts.py`
Expected: `.account-btn` 不存在。

- [ ] **Step 3: 实现**

`auth.js`：读 `localStorage[AUTH_KEY]`；`login` 打 `POST /api/login`，成功后写 `{token, ...user}` 并 `location.reload()`；`logout` 先 `await flush()`（从 `sync.js` 导入），删除 `jasoncai:read:v2:u<id>`、`jasoncai:fav:v2:u<id>`、`jasoncai:notes:v2:u<id>`、队列键，再 `location.reload()`；失败保留并 `alert("还有 N 条没同步，下次登录会补发")`。

`account.html` 与 JS：未登录按钮文案「登录」，点击打开 `.auth-dialog`（用户名、密码、提交、错误行）；已登录按钮显示 `.account-name` 与 `.account-logout`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 scripts/tests/test_accounts.py`
Expected: 两个用例通过。

- [ ] **Step 5: 提交**

```bash
git add assets/js/auth.js layouts/partials/account.html layouts/_default/baseof.html layouts/partials/head.html assets/css/site.css scripts/tests/test_accounts.py
git commit -m "feat(accounts): login dialog and account bar"
```

---

### Task 6: 本地缓存按用户分键 + 请求带 Bearer

**Files:**
- Create: `assets/js/user-store.js`
- Modify: `assets/js/favorites.js`、`assets/js/notes-core.mjs`（`readNotes/writeNotes` 接受 `key` 参数）、`assets/js/notes.js`、`assets/js/reading-progress.js`、`assets/js/sync.js`
- Test: `scripts/tests/test_accounts.py`、`scripts/tests/test_notes_sync.py`、`scripts/tests/test_favorites.py`

**Interfaces:**
- Consumes: `auth.js` 的 `current()`。
- Produces: `user-store.js` 导出 `nsKey(base) -> string`、`getItem(base)`、`setItem(base, value)`、`removeItem(base)`；`sync.js` 的 `API` 请求统一走 `authHeaders()`（有 token 时 `Authorization: Bearer …`，不再有 `X-Edit-Key`）；`sync.js` 导出 `flush()` 供 `auth.js` 使用。

- [ ] **Step 1: 写失败测试**

```python
def test_state_is_scoped_per_account(page, base):
    login_as(page, base, "Root", "root-pass-1")
    page.goto(base + "/posts/ot-crdt-lww.html")
    page.click("button.fav-btn")
    page.wait_for_function("() => document.querySelector('.fav-btn').getAttribute('aria-pressed') === 'true'")
    page.evaluate("() => localStorage.removeItem('jasoncai:auth:v1')")
    page.reload()
    assert page.locator("button.fav-btn[aria-pressed=true]").count() == 0

def test_non_admin_cannot_ask_ai(page, base):
    login_as(page, base, "Alice", "alice-pass")
    page.goto(base + "/posts/ot-crdt-lww.html")
    page.wait_for_timeout(1200)
    assert page.locator(".nt-ask").count() == 0

def test_guest_does_not_call_server_state(page, base):
    seen = []
    page.on("request", lambda r: seen.append(r.url) if "/api/" in r.url else None)
    page.goto(base); page.wait_for_timeout(1200)
    assert not [u for u in seen if "/api/notes" in u or "/api/progress" in u or "/api/favorites" in u]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `hugo --minify && python3 scripts/tests/test_accounts.py`
Expected: 两个用例失败（切账号后仍是已收藏；访客仍在打 `/api`）。

- [ ] **Step 3: 实现**

`user-store.js`：

```js
import { current, AUTH_KEY } from "./auth.js";
export function nsKey(base) {
  const user = current();
  return `${base}:${user ? `u${user.id}` : "guest"}`;
}
export const getItem = (base) => localStorage.getItem(nsKey(base));
export const setItem = (base, value) => localStorage.setItem(nsKey(base), value);
export const removeItem = (base) => localStorage.removeItem(nsKey(base));
```

各模块把 `jasoncai:read:v1` → `jasoncai:read:v2`、`jasoncai:fav:v1` → `:v2`、`jasoncai:notes:v1` → `:v2`，读写都经 `user-store`。`notes-core.readNotes(store, key = NOTES_KEY)` 与 `writeNotes(store, all, key = NOTES_KEY)` 加可选 key，`notes.js` 传 `nsKey(NOTES_KEY)`。

`sync.js`：`authHeaders()` 返回 `Authorization` 或空对象；`getJSON`、`send`、`postReply`、`openNoteStream` 都用它。无 token 时 `pullSummary`、`pullNotes`、`flush` 直接返回空/`null`，不发请求。

首次登录把 `:guest` 的 read/fav/notes 复制进 `:u<id>`（若该用户 v2 为空）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 scripts/tests/test_accounts.py && python3 scripts/tests/test_notes_sync.py && python3 scripts/tests/test_favorites.py`
Expected: 全绿；`test_notes_sync.py` 的每个用例前加 `login_as`，并新增 Alice 隔离用例。

- [ ] **Step 5: 提交**

```bash
git add assets/js/user-store.js assets/js/favorites.js assets/js/notes-core.mjs assets/js/notes.js assets/js/reading-progress.js assets/js/sync.js scripts/tests/test_accounts.py scripts/tests/test_notes_sync.py scripts/tests/test_favorites.py
git commit -m "feat(accounts): per-user local caches and bearer requests"
```

---

### Task 7: Live 更新改 fetch 流，频道按用户隔离

**Files:**
- Modify: `assets/js/sync.js`（`openNoteStream`）
- Modify: `server/notesd.py`（`publish` 的频道名、`/api/notes/<slug>/stream`）
- Test: `server/test_notesd.py`、`scripts/tests/test_notes_sync.py`

**Interfaces:**
- Consumes: Task 2 的 `current_user()`。
- Produces: `openNoteStream(slug, onEvent, { signal } = {}) -> Promise<void>`（内部 fetch + ReadableStream）；服务端频道 `note:<user_id>:<slug>`。

- [ ] **Step 1: 写失败测试**

```python
def test_note_stream_requires_token_and_is_scoped(self):
    self.seed_user("Alice", "alice-pass")
    ta = self.token_for("Alice", "alice-pass")
    self.assertEqual(self.call("GET", "/api/notes/ot-crdt-lww/stream")[0], 401)
    # Alice 的频道收到自己的事件，Bob 的不会
```

（SSE 用 `http.client` 读事件，参照现有 `test_jobs_stream` 的写法。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 server/test_notesd.py`
Expected: 无 token 的 stream 返回 200。

- [ ] **Step 3: 实现**

服务端：`publish(f"note:{user_id}:{slug}", …)`；`stream` 路由取 `current_user()`，`notes` 分支用它调 `note_bundle(user["id"], slug)` 后才订阅。

客户端 `openNoteStream`：

```js
export async function openNoteStream(slug, onEvent, { signal } = {}) {
  const res = await fetch(`${API}/notes/${slug}/stream`, { headers: authHeaders(), signal });
  if (!res.ok || !res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, sep); buf = buf.slice(sep + 2);
      const event = chunk.match(/^event:\s*(.*)$/m)?.[1];
      const data = chunk.match(/^data:\s*(.*)$/m)?.[1];
      if (event && data) onEvent(event, JSON.parse(data));
    }
  }
}
```

调用方（`notes.js`）在页面卸载时 `controller.abort()`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 server/test_notesd.py && python3 scripts/tests/test_notes_sync.py`
Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add server/notesd.py assets/js/sync.js assets/js/notes.js server/test_notesd.py scripts/tests/test_notes_sync.py
git commit -m "feat(accounts): authenticated fetch-based note stream"
```

---

### Task 8: 文档、迁移上线与线上验收

**Files:**
- Modify: `README.md`、`~/.dsh/hosts/blog-jasoncai.site.md`、`~/.dsh/hosts/blog-notes-reply.md`、`~/.dsh/AGENTS.md`
- Modify: `scripts/publish.sh`（无改动则跳过）
- Test: 线上手工核对

- [ ] **Step 1: 更新文档**

README 加「账号与登录」：登录入口、建号（`POST /api/users`）、Root 权限、数据隔离、访客本地模式、迁移命令。runbook §7/§11/§11.5/§13/§14/§15 改写；`blog-notes-reply.md` 的 curl 改成 `Authorization: Bearer $NOTES_API_TOKEN`；AGENTS 博客段补一句权限。

- [ ] **Step 2: 备份并迁移**

```bash
ssh … 'sudo systemctl stop notesd && sudo cp /var/lib/notesd/notes.db /var/lib/notesd/notes.db.bak-$(date +%Y%m%d-%H%M%S)'
scp server/notesd.py server/migrate_accounts.py …
ssh … 'sudo python3 /opt/notesd/migrate_accounts.py /var/lib/notesd/notes.db'
```

记录输出的 Root 密码与 bridge token；token 写进 Mac 的 `~/.dsh/notes-bridge.env`（`NOTES_API_TOKEN`），`launchctl kickstart -k gui/$(id -u)/site.jasoncai.notes-bridge`。

- [ ] **Step 3: 起服务并冒烟**

`sudo systemctl start notesd`；`curl /api/healthz` 200；`curl /api/notes/…` 无 token 401；登录拿 token 后 200。

- [ ] **Step 4: 发布并线上验收**

`bash scripts/publish.sh --final`；线上核对：登录框可用、Root 登录后看到 98 条进度与 1 条收藏、未登录访客首页无报错、换浏览器登录同账号数据一致、非 Root 看不到「让 AI 回答」。

- [ ] **Step 5: 提交**

```bash
git add README.md scripts/publish.sh
git commit -m "docs(accounts): login, migration and permissions"
git push origin main
```
