# 批注唤醒会话 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 页面上的批注能触发一次真实的 DSH 会话续跑，回答以新楼层回到页面，全程不用手工刷新。

**Architecture:** 服务器加 `sessions` 与 `jobs` 两张表，暴露 `ask / claim / done` 三个接口与两条 SSE 流；Mac 上的 `notes-bridge` 用一条出站长连接订阅任务、串行 spawn `dsh --profile headless`，把回答 POST 回 `/replies`；页面订阅自己的 `note.updated` 流，收到就插楼层。

**Tech Stack:** Python 3.12 标准库（`http.server`、`sqlite3`、`subprocess`、`threading`）、原生 JS（ES module，`js.Build` 打包）、Playwright（测试）、launchd（Mac 常驻）。

**Spec:** `content/posts/blog-agent-bridge/index.md`（已发布的方案稿，线上 https://jasoncai.site/posts/blog-agent-bridge.html）

## Global Constraints

- 只用标准库，不引入任何 pip 依赖；服务器上没有 Node、没有 sqlite3 CLI。
- 读接口保持公开；写接口一律要 `X-Edit-Key`；bridge 专用接口要 `X-Bridge-Token`。
- 触发规则：回复文本含 `?`/`？` 或「为什么 / 怎么 / 是不是」才自动建任务，其余只存不回，页面上给「让 AI 回答」按钮。
- AI 回复只写结论，≤400 字，不带开场白与总结句；署名固定 `author: "AI"`。
- 任务状态只有 `pending / running / done / blocked`；重试上限 3；running 心跳 30s、超时 10 分钟回退 `pending`。
- 页面在无 JS、无 SSE、离线时都必须能正常阅读与手动回复。

## Review Focus

- 同一个 note 连点两次「让 AI 回答」：应只有一个 active 任务，第二次返回 409 而不是排两条。
- bridge 断线 30 秒后重连：期间建的任务不能丢，重连后按 `pending` 补拉。
- 同一会话被 GUI 占用（`session.lock`）：bridge 要退避重试，不能把 job 标成 done。
- 会话回答超时或返回空：job 要变 `blocked` 并在页面显示原因，而不是永远「正在处理」。
- 页面 SSE 断线重连：不能重复插入同一楼层（按 `thread` 长度或最后一条的 `t` 去重）。

---

### Task 1: 来源会话表与注册接口

**Files:**
- Modify: `server/notesd.py`（`SCHEMA`、`Store`、`Handler.route`）
- Modify: `server/test_notesd.py`
- Create: `layouts/partials/source-session.html`
- Modify: `layouts/_default/single.html`、`assets/css/site.css`

**Interfaces:**
- Consumes: 现有 `Store`/`Handler` 结构、`SLUG_RE`、`write_allowed()`
- Produces: `Store.upsert_session(slug, session_id, workspace, title) -> dict`、`Store.session_for(slug) -> dict | None`；`PUT /api/sessions/<slug>`、`GET /api/sessions/<slug>`；模板读 `.Params.source_session`

- [ ] **Step 1: 写失败测试**（`server/test_notesd.py`）

```python
def test_session_round_trip(server):
    s = post(server, "/api/sessions/ot-crdt-lww", {"session_id": "0f3a...", "workspace": "default-workspace", "title": "OT/CRDT 那篇"}, key=KEY)
    assert s.status == 200
    got = get(server, "/api/sessions/ot-crdt-lww")
    assert got["session"]["session_id"] == "0f3a..."
```

再写两条：`session_id` 不合 `^[0-9a-f-]{8,64}$` 返回 400；不存在的 slug 返回 `{"session": None}`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest server.test_notesd -k session`
Expected: FAIL（404 / AttributeError）

- [ ] **Step 3: 实现**
  - `SCHEMA` 加表：`sessions(slug TEXT PRIMARY KEY, session_id TEXT NOT NULL, workspace TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL DEFAULT 0)`
  - `Store.upsert_session` 用 `INSERT ... ON CONFLICT(slug) DO UPDATE`，`updated=now()`
  - `Handler.route` 在 `parts[0] == "sessions"` 分支处理 GET/PUT（PUT 走 `write=True`）
  - `layouts/partials/source-session.html`：`{{ with .Params.source_session }}<a class="src-session" data-session="{{ . }}" href="{{ $.RelPermalink }}#session">来自会话 {{ printf "%.8s" . }}…</a>{{ end }}`，插在日期与 tag 之间
  - CSS `.src-session{font-size:11px;font-family:var(--mono);color:var(--soft)}`

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest server.test_notesd && hugo --minify --quiet`
Expected: 全绿，构建无错

- [ ] **Step 5: 提交**

```bash
git add server/notesd.py server/test_notesd.py layouts/partials/source-session.html layouts/_default/single.html assets/css/site.css
git commit -m "feat(bridge): session mapping table, API and source-session link"
```

---

### Task 2: 任务表、提问判定与手动触发

**Files:**
- Modify: `server/notesd.py`
- Modify: `server/test_notesd.py`

**Interfaces:**
- Consumes: Task 1 的 `sessions`（`Store.session_for`）
- Produces: `QUESTION_RE`、`Store.create_job(slug, note_id, session_id, prompt, kind) -> dict`、`Store.active_job(note_id) -> dict | None`、`Store.claim_job(job_id, worker) -> dict | None`、`Store.finish_job(job_id, ok, error) -> dict`、`Store.pending_jobs() -> list[dict]`；接口 `POST /api/notes/<slug>/<id>/ask`、`POST /api/jobs/<id>/claim`、`POST /api/jobs/<id>/done`、`GET /api/jobs?status=pending`

- [ ] **Step 1: 写失败测试**

```python
def test_question_reply_creates_job(server):
    post_reply(server, "ot-crdt-lww", "n1", "这里为什么不用向量时钟？")
    jobs = get(server, "/api/jobs?status=pending")
    assert len(jobs["jobs"]) == 1
    assert jobs["jobs"][0]["kind"] == "auto"

def test_statement_reply_does_not_create_job(server):
    post_reply(server, "ot-crdt-lww", "n1", "这句是全文转折")
    assert get(server, "/api/jobs?status=pending")["jobs"] == []

def test_ask_twice_returns_409(server):
    assert post(server, "/api/notes/ot-crdt-lww/n1/ask", {}, key=KEY).status == 200
    assert post(server, "/api/notes/ot-crdt-lww/n1/ask", {}, key=KEY).status == 409
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest server.test_notesd -k job`
Expected: FAIL

- [ ] **Step 3: 实现**
  - 表：`jobs(id TEXT PK, slug, note_id, session_id, kind TEXT, prompt TEXT, status TEXT, attempts INT DEFAULT 0, worker TEXT DEFAULT '', claimed_at INT DEFAULT 0, heartbeat INT DEFAULT 0, error TEXT DEFAULT '', created INT, updated INT)`，索引 `jobs_status(status, created)`、`jobs_note(note_id, status)`
  - `QUESTION_RE = re.compile(r"[?？]|为什么|怎么|是不是|如何|能不能")`
  - `POST /replies` 成功追加后：`if QUESTION_RE.search(text) and store.session_for(slug) and not store.active_job(note_id)` → `create_job(..., kind="auto")`
  - `ask`：无 session → 409 `{"error": "no session"}`；已有 active → 409；否则 `create_job(kind="manual")`，`prompt` 取 `thread[0].text`
  - `claim`：`UPDATE jobs SET status='running', worker=?, attempts=attempts+1, claimed_at=?, heartbeat=? WHERE id=? AND status='pending'`，`rowcount==1` 才算成功，否则 409
  - `done`：`ok` → `status='done'`；否则 `attempts < 3` → 回 `pending`，`>= 3` → `blocked` 并存 `error`

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest server.test_notesd`
Expected: 全部通过（含既有 10 例）

- [ ] **Step 5: 提交**

```bash
git commit -am "feat(bridge): job queue with question heuristic, ask/claim/done"
```

---

### Task 3: 两条 SSE 流

**Files:**
- Modify: `server/notesd.py`
- Modify: `server/test_notesd.py`

**Interfaces:**
- Consumes: Task 2 的 `Store`
- Produces: `GET /api/jobs/stream`（bridge，要 `X-Bridge-Token`）、`GET /api/notes/<slug>/stream`（页面，公开）；事件 `job.created`、`job.updated`、`note.updated`，`data:` 为 JSON，15s 心跳 `: ping`

- [ ] **Step 1: 写失败测试**（用 `http.client` 读前两行）

```python
def test_bridge_stream_pushes_new_job(server):
    stream = open_stream(server, "/api/jobs/stream", {"X-Bridge-Token": TOKEN})
    post(server, "/api/notes/ot-crdt-lww/n1/ask", {}, key=KEY)
    event = read_event(stream, timeout=3)
    assert event["event"] == "job.created"
    assert json.loads(event["data"])["slug"] == "ot-crdt-lww"

def test_bridge_stream_requires_token(server):
    assert open_stream_status(server, "/api/jobs/stream") == 401
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest server.test_notesd -k stream`
Expected: FAIL

- [ ] **Step 3: 实现**
  - `Store` 里加 `self._subs: dict[str, list[queue.Queue]]` 与 `subscribe(channel) -> Queue` / `unsubscribe`；每次 `create_job`、`finish_job`、`add_reply` 后 `publish(channel, event)`
  - 三个写入点分别发 `job.created`（channel `jobs`）、`job.updated`（`jobs`）、`note.updated`（channel `note:<slug>`）
  - Handler：`send_response(200)` + `Content-Type: text/event-stream` + `Cache-Control: no-cache`，循环 `queue.get(timeout=15)`，超时写 `: ping\n\n`
  - `BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")`；空值时该流返回 503（bridge 未配置）

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest server.test_notesd`
Expected: 通过

- [ ] **Step 5: 提交**

```bash
git commit -am "feat(bridge): SSE streams for jobs and per-note updates"
```

---

### Task 4: bridge 进程

**Files:**
- Create: `server/notes_bridge.py`
- Create: `server/test_notes_bridge.py`
- Create: `deploy/notes-bridge.plist`

**Interfaces:**
- Consumes: Task 2/3 的接口
- Produces: `main()`、`claim_and_run(job, runner) -> None`、`build_prompt(note) -> str`、`parse_reply(stdout) -> str`

- [ ] **Step 1: 写失败测试**（`runner` 用假函数，不真跑 dsh）

```python
def test_build_prompt_includes_note_and_article():
    p = build_prompt({"slug": "ot-crdt-lww", "note": {"q": "为什么不用向量时钟", "thread": [{"author": "我", "text": "为什么不用向量时钟？"}]}})
    assert "ot-crdt-lww" in p and "只给结论" in p and "400" in p

def test_parse_reply_takes_last_assistant_text():
    stdout = '{"type":"assistant","text":"第一段"}\n{"type":"assistant","text":"第二段"}\n'
    assert parse_reply(stdout) == "第二段"

def test_claim_and_run_posts_reply(jobstore):
    claim_and_run(job, runner=lambda prompt: "结论：不需要，因为……")
    assert jobstore.replies[-1]["author"] == "AI"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest server.test_notes_bridge`
Expected: FAIL（module not found）

- [ ] **Step 3: 实现**
  - 环境变量：`NOTES_API`（默认 `http://127.0.0.1:8099/api`）、`BRIDGE_TOKEN`、`DSH_BIN`（默认 `dsh`）、`DSH_PROFILE`（默认 `headless`）、`SKILL_NOTE`（可选）
  - `build_prompt`：文章 slug、被划原句 `q`、整条 `thread`、硬约束（简体中文、只给结论、≤400 字、不写「好的」开头）
  - 主循环：`GET /api/jobs?status=pending` 拉一次 → 逐条 `claim` → `subprocess.run([dsh, "--profile", profile, "--session-id", job.session_id, "--approval-policy", "never", prompt], timeout=600)` → `parse_reply` → `POST /replies`（`X-Edit-Key`）→ `POST /jobs/<id>/done`
  - 锁冲突（stderr 含 `session.lock`）→ `done {ok:false, error:"locked"}`，按 5s/15s/45s 退避
  - SSE 订阅在独立线程，收 `job.created` 只做「踢一下主循环」；主循环每 30s 也轮询一次兜底
  - `deploy/notes-bridge.plist`：`KeepAlive true`、`RunAtLoad true`、`StandardOutPath ~/Library/Logs/notes-bridge.log`

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest server.test_notes_bridge`
Expected: 通过

- [ ] **Step 5: 提交**

```bash
git add server/notes_bridge.py server/test_notes_bridge.py deploy/notes-bridge.plist
git commit -m "feat(bridge): macOS bridge that claims jobs and resumes sessions"
```

---

### Task 5: 页面增量渲染与手动触发

**Files:**
- Modify: `assets/js/notes.js`、`assets/css/site.css`
- Modify: `scripts/tests/test_notes_sync.py`

**Interfaces:**
- Consumes: `sync.js` 的 `API`、`getEditKey`；Task 2 的 `ask`、Task 3 的 `note.updated`
- Produces: `renderJobState(block, job)`、`openNoteStream(slug)`、`askAI(noteId)`；DOM `.nt-job`（状态行）、`.nt-ask`（按钮）、`.nt-retry`（重试）

- [ ] **Step 1: 写失败测试**

```python
def test_manual_ask_button_enqueues_job(page, base):
    page.goto(f"{base}/posts/ot-crdt-lww.html"); page.wait_for_selector("mark.note-hl")
    page.click("mark.note-hl"); page.click(".nt-ask")
    assert wait_for(lambda: page.locator(".nt-job").inner_text().find("处理中") >= 0, 5)
```

再写：楼层 SSE 到达后不刷新页面就出现第 3 层；同一楼层不会插两次；`blocked` 时显示原因与 `.nt-retry`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 scripts/tests/test_notes_sync.py`
Expected: FAIL（找不到 `.nt-ask`）

- [ ] **Step 3: 实现**
  - 气泡底部加 `.nt-job`（空则隐藏）与 `.nt-ask`（`thread` 里没有 AI 楼层时显示）
  - `askAI`：`POST /ask`（带口令）→ 409 时提示「已有一条在排队」；成功后 `.nt-job` 显示「已收到，AI 正在处理」
  - `openNoteStream(slug)`：`EventSource(`${API}/notes/${slug}/stream`)`，收 `note.updated` 按 `updated` 合并后重画气泡与右栏列表；断线 3s 重连
  - `renderJobState`：`pending`→「排队中 第 N 位」、`running`→「AI 正在处理」、`done`→隐藏、`blocked`→原因 + `.nt-retry`
  - `retry`：`POST /ask` 重新入队

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 scripts/tests/test_notes_sync.py && python3 scripts/tests/test_notes.py`
Expected: 两条都全绿

- [ ] **Step 5: 提交**

```bash
git commit -am "feat(notes): job status, manual ask and live floor updates"
```

---

### Task 6: 端到端与上线

**Files:**
- Modify: `README.md`、`~/.dsh/hosts/blog-jasoncai.site.md`
- Modify: `deploy/notesd.service`（若不存在则新建）

**Interfaces:**
- Consumes: 前 5 个任务
- Produces: 一条可复现的验收脚本 `scripts/bridge-smoke.sh`

- [ ] **Step 1: 写冒烟脚本**

```bash
#!/usr/bin/env bash
# 1) 注册会话 2) 发一条问句批注 3) 等 bridge 处理 4) 断言出现 AI 楼层
```

- [ ] **Step 2: 本地跑通**（bridge 用 `DSH_BIN=/bin/echo` 的假实现先验管道）

Run: `bash scripts/bridge-smoke.sh http://127.0.0.1:8099`
Expected: 输出 `OK: AI floor 2`

- [ ] **Step 3: 部署**：服务器 `notesd.service` 加 `Environment=BRIDGE_TOKEN=...` 并重启；Mac 安装 plist（`launchctl bootstrap`），日志落 `~/Library/Logs/notes-bridge.log`

- [ ] **Step 4: 线上验收**：在 https://jasoncai.site/posts/ot-crdt-lww.html 划一句问句写批注 → 页面出现「AI 正在处理」→ 2 分钟内出现第 2 层 → 换一个浏览器打开同一页也能看到

- [ ] **Step 5: 文档与提交**：README 加「批注唤醒会话」一节；runbook 加 §15（token、plist、排障）；`python3 scripts/check-tags.py`、`scripts/verify-site.py`、`scripts/publish.sh --final`

```bash
git commit -am "docs(bridge): smoke script, deployment and runbook"
```
