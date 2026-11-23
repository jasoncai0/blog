# 用户账号与个人数据隔离 · 设计

日期：2026-11-23
状态：待批准

## 1. 意图

站点现在只有一个隐含用户：阅读进度、收藏、批注都是全站的同一份数据，写操作靠一个全局口令 `X-Edit-Key` 保护。这套东西对单人博客勉强能用，但没法区分访客，也没法让数据跟着人走。

要改成：访客点「登录」，输用户名密码；每个账号的状态各自独立；存量数据归管理员 Root；非 Root 不开放 AI 回答。

## 2. 目标

1. 用户名全局唯一，管理员账号为 `Root`。
2. 登录用用户名 + 密码，不开放自助注册；只有 Root 能建号。
3. 已读状态、收藏状态、批注按账号隔离：A 读过的文章，B 打开时仍是未读。
4. 存量 `progress`、`favorites`、`notes` 全部转到 Root 名下，数量和内容不变。
5. 废弃 `NOTES_EDIT_KEY`：写操作改成校验登录 token。
6. 未登录访客仍可读全文；收藏与已读只存本机，不显示别人的状态。
7. 非 Root 用户看不到「让 AI 回答」入口，服务端也拒绝其建任务。

## 3. 非目标

- 不做第三方登录、邮箱验证、找回密码。
- 不做注册页；建号走命令行脚本或 Root 的接口。
- 不做角色细分（只有 `admin` 与 `user` 两种）。
- 不改文章内容、不改静态页的公开可读性。
- 不做多设备同时登录互踢（同一账号多 token 并存，退出只删当前 token）。

## 4. 反目标

- 任何一条数据泄漏到别的账号（包括首页计数、`/api/summary`、批注楼层）。
- 未登录访客因为鉴权失败看到报错弹窗或空白列表。
- 迁移后 Root 的进度从 100% 掉回 0、批注丢失。
- 为了鉴权引入第三方库或 NPM 依赖。

## 5. 约束

- 服务端只能 Python 3.12 标准库 + SQLite（`server/notesd.py`，现 891 行）。
- 站点是静态 Hugo 产物，运行在 nginx 后，API 同源反代到 `127.0.0.1:8099`。
- 客户端无构建依赖，JS 走 Hugo `js.Build` + `fingerprint`。
- bridge（`server/notes_bridge.py`）跑在本机 launchd，用同一套 API 写 AI 回复，必须能凭一个长期凭据继续工作。
- 密码与 token 不得出现在日志、URL、仓库里。

## 6. 方案

### 6.1 鉴权方式（用户已选 A）

- **A. Bearer token 存 localStorage（选中）**：`POST /api/login` 返回随机 token，服务端只存它的 SHA-256；客户端所有请求带 `Authorization: Bearer <token>`。与现有 `sync.js` 的 fetch 封装贴合，bridge 与脚本都能用。
- B. HttpOnly cookie：抗 XSS 更好，但需要 CSRF token，代理与脚本调试都更麻烦。
- C. 每用户一条长期 API key：没有登录动作，改密码不失效，不符合需求。

### 6.2 数据模型

新增：

```sql
users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,          -- 原样保存，用于显示
  username_lower TEXT NOT NULL UNIQUE,  -- 唯一性按小写算
  pass_hash TEXT NOT NULL,         -- pbkdf2$120000$<salt hex>$<hash hex>
  role TEXT NOT NULL DEFAULT 'user',    -- 'admin' | 'user'
  created INTEGER NOT NULL
)
auth_tokens(
  token_hash TEXT PRIMARY KEY,     -- sha256(token) 十六进制
  user_id INTEGER NOT NULL,
  created INTEGER NOT NULL,
  expires INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT ''    -- 'browser' / 'bridge'
)
```

改动现有表（`user_id INTEGER NOT NULL DEFAULT 0`）：

- `notes`：加 `user_id`；索引改 `(user_id, slug, deleted)`。
- `progress`：主键由 `slug` 改为 `(user_id, slug)`。
- `favorites`：主键由 `slug` 改为 `(user_id, slug)`。
- `jobs`、`sessions`、`note_rev`：不改结构；`jobs` 查询随 Root 走，`sessions` 只对 Root 生效。

SQLite 改主键需要重建表：`SCHEMA_VERSION` 用 `PRAGMA user_version` 记录，启动时若为旧版本，执行「建新表 → `INSERT ... SELECT` 带 `user_id = Root`（迁移脚本负责）→ 删旧表 → 改名」。

### 6.2.1 Live 更新的鉴权（EventSource 带不了头）

`GET /api/notes/<slug>/stream` 今天不带鉴权，而且把整条笔记广播给所有订阅者。改成 **fetch + ReadableStream 手解析 SSE**：`sync.js` 的 `openNoteStream` 从 `EventSource` 换成 `fetch(path, {headers: {Authorization: Bearer ...}, signal})`，逐行解析 `data:` / 空行分发，其余行为不变（断线重连、`AbortController` 收尾）。

服务端频道按用户隔离：频道名 `note:<user_id>:<slug>`；`publish` 只在当前用户的频道。未登录不订阅。

### 6.3 接口

新增：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | `{username, password}` → `{token, user:{id, username, role}}`；失败 401，限速 10 次/分/IP |
| POST | `/api/logout` | 删除当前 token，204 |
| GET | `/api/me` | `{user:{id, username, role}}`；无 token 401 |
| POST | `/api/users` | 仅 admin：`{username, password}` → 新用户；用户名重复 409 |

改造：

- `GET /api/summary`：只统计当前用户（笔记数、进度、收藏）。
- `GET /api/notes/<slug>`、`PUT|DELETE /api/notes/...`：只读写当前用户的笔记。
- `GET /api/progress`、`PUT /api/progress/<slug>`：按 `user_id` 读写。
- `GET /api/favorites`、`PUT|DELETE /api/favorites/<slug>`：按 `user_id`。
- `POST /api/notes/<slug>/<id>/ask`：非 `admin` → 403 `{"error":"admin only"}`。
- `POST /api/notes/<slug>/<id>/replies`：只允许回复自己的笔记；AI 回复由 bridge 代表 Root 写入。
- `/api/jobs/*`（`stream`、`pending`、`claim`、`done`）与 `/api/sessions/*`：**只允许 admin token**；非 admin 403，无 token 401。bridge 用 Root 的 token（`name='bridge'`），不再有独立的 `BRIDGE_TOKEN`，也不再需要 `X-Edit-Key`。
- `POST /api/notes/<slug>/<id>/replies` 自动触发 AI 只在「当前用户是 admin 且该笔记属于自己」时生效；普通用户回复自己的笔记只追加楼层。
- `PUT|DELETE /api/notes/<slug>/<id>` 必须先校验笔记归属当前用户，别的 id 一律 404（不泄漏存在性）。
- 公开接口只剩 `/healthz`。

鉴权规则：除 `/healthz` 与 bridge 的 `/api/jobs/*`（用 `BRIDGE_TOKEN`）外，所有 `/api` 读写都要 Bearer token；缺 token 401。

### 6.4 客户端

- 新文件 `assets/js/auth.js`：
  - 键 `jasoncai:auth:v1` 存 `{token, id, username, role}`。
  - 顶栏组件：未登录显示「登录」；登录后显示用户名与「退出」。
  - 登录框：用户名、密码、错误提示、回车提交；成功后 `location.reload()`，退出后同样重载（清掉按用户缓存）。
- `sync.js`：把 `X-Edit-Key` 换成 `Authorization: Bearer`；无 token 时不发任何状态请求（本地模式），`AuthError` → 401 时提示「登录已过期，请重新登录」。
- 本地缓存按用户分键：`jasoncai:read:v2:u<id>`、`jasoncai:fav:v2:u<id>`、`jasoncai:notes:v2:u<id>`、队列同理；未登录用 `...:guest`。旧键（v1）在 Root 第一次登录时若服务端为空，复制进 v2 后由既有补传逻辑推上去。
- `notes.js`：非 admin 不渲染 `.nt-ask`，保存与回复照常（写自己的笔记）。
- 首页过滤、星标、进度沿用现有组件，只改数据来源键。

### 6.5 迁移与落地

`server/migrate_accounts.py`：

1. 若无 Root，创建 Root（密码取 `--password`，否则随机生成 16 位并打印一次）。
2. 给 `notes/progress/favorites` 的存量行填 `user_id = Root.id`。
3. 生成一条长期 token（`name='bridge'`，`expires=0` 表示不失效），打印到标准输出。
4. `--dry-run` 打印每张表将被改写的行数。

顺序（脚本自己完成结构迁移，服务端启动只校验版本）：

1. 停 `notesd`，备份 `/var/lib/notesd/notes.db`。
2. 在服务器上跑 `sudo python3 server/migrate_accounts.py`：建新表、`INSERT ... SELECT` 把存量行写进 `user_id = Root`、建 Root、写 `PRAGMA user_version = 2`、生成 bridge token 并打印。
3. 装新 `notesd.py`，启动；若 `user_version` 与代码期望不一致，服务拒绝启动并打日志（不静默重建）。
4. 把脚本打印的 token 写进 Mac 的 `~/.dsh/notes-bridge.env`（`NOTES_API_TOKEN`），`launchctl kickstart -k`。

### 6.6 退出与本地数据

- 退出：先 `await flush()` 把队列发完；成功则删掉该用户的 `:v2:u<id>` 缓存与队列，再清 `jasoncai:auth:v1`、重载。flush 失败则保留缓存并提示「还有 N 条没同步，下次登录会补发」。
- 未登录访客继续用现有 v1 键（`jasoncai:read:v1` 等），登录时若该账号的 v2 缓存为空，把 v1 数据复制进 v2 再由既有补传逻辑推到服务端；`jasoncai:notes:pushed` 标记改为按用户存（`jasoncai:notes:pushed:u<id>`）。

## 7. 安全

- 密码：PBKDF2-HMAC-SHA256，120000 轮，16 字节盐；比较用 `hmac.compare_digest`。
- token：32 字节 `secrets.token_urlsafe`，服务端存 SHA-256；有效期 180 天；退出即删。
- 登录限速：每 IP 每分钟 10 次，失败返回 401 且不区分「用户名不存在 / 密码错」。
- 日志不打密码、token；`note_rev` 继续记笔记变更，不记鉴权信息。
- `Authorization` 头在 CORS 白名单里，只允许本站来源。

## 8. 测试

服务端（`server/test_notesd.py`，现有 37 例）：

1. 登录成功返回 token，`/api/me` 可用；错误密码 401；限速第 11 次 429。
2. 无 token 访问 `/api/notes/...` 401；带 token 200。
3. 两个用户各自建笔记，`GET /api/notes/<slug>` 只返回自己的。
4. 两个用户各自收藏同一篇，`/api/summary` 计数各为 1；取消一方不影响另一方。
5. 进度：A 写 100，B 读到 0；`/api/progress` 各自独立。
6. 非 admin 调 `/ask` → 403；admin 正常入队。
7. 建号接口：非 admin 403、重名 409、成功后能登录。
8. 迁移脚本：`--dry-run` 不写库；迁移后 Root 拥有全部存量行，行数不变。

浏览器（新增 `scripts/tests/test_accounts.py`，自建 notesd + 临时 db）：

1. 未登录首页正常、星标只在本机；刷新仍在。
2. 登录后星标与已读写进服务端；退出后回到 guest 视图。
3. 另一个浏览器登录同一账号，看到同样的收藏与已读。
4. 另一个账号登录，看不到 Root 的收藏、读过、批注。
5. 非 admin 看不到「让 AI 回答」。
6. 错误密码显示「用户名或密码不对」，不弹未捕获错误。

现有 13 个 `test_notes_sync.py`、9 个 `test_favorites.py`、`test_notes.py`、`test_reading_progress.py` 需改为带登录态（各加一个 `login()` 辅助），或保留 guest 用例并新增登录用例。

## 9. 文档与记忆

- `README.md`：新增「账号与登录」节（登录、建号、权限、数据隔离、迁移），改写「同步服务」「收藏」「阅读进度」「笔记与高亮」里关于匿名与口令的句子。
- `~/.dsh/hosts/blog-jasoncai.site.md`：§7 访问与密钥、§11/11.5、§13、§14、§15 更新；新增「账号」小节与迁移命令。
- `~/.dsh/hosts/blog-notes-reply.md`：AI 回复改为用 Root 的 bridge token。
- `~/.dsh/AGENTS.md`：博客段补一句「批注与状态按账号隔离，AI 回答仅 Root」。

## 10. 工作量

约 2 天：服务端账号与鉴权 1 天；客户端登录、按用户缓存与模板 0.5 天；迁移、部署、测试与文档 0.5 天。

## 11. 待定

无。用户已确认：方案 A、批注私有、存量归 Root、非 Root 无 AI 回答、只有 Root 建号、访客本地模式。
