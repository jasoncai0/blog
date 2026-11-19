// 把「笔记 + 阅读进度」从 localStorage 搬到服务端：
// 页面先读本地（离线可用），再与 /api 对一遍；写操作先本地、再排队上行。
// 服务端地址默认同源 /api；测试里用 window.__NOTES_API__ 指到别的端口。

export const API = window.__NOTES_API__ || "/api";

const QUEUE_KEY = "jasoncai:sync-queue:v1";
const KEY_STORE = "jasoncai:notes:key";
const MAX_QUEUE = 200;

function readQueue() {
  try {
    const raw = JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

function writeQueue(ops) {
  try {
    localStorage.setItem(QUEUE_KEY, JSON.stringify(ops.slice(-MAX_QUEUE)));
  } catch {
    /* 存储满或被禁用时，队列退化成内存态 */
  }
}

let flushing = null;
let retryTimer = 0;
let backoff = 4000;

export function getEditKey() {
  try {
    return localStorage.getItem(KEY_STORE) || "";
  } catch {
    return "";
  }
}

export function setEditKey(key) {
  try {
    if (key) localStorage.setItem(KEY_STORE, key);
    else localStorage.removeItem(KEY_STORE);
  } catch {
    /* ignore */
  }
  if (key) {
    clearTimeout(retryTimer);
    backoff = 4000;
    flush();
  }
}

export class AuthError extends Error {}

const authListeners = [];
let authPending = false;
export function onAuthNeeded(fn) {
  authListeners.push(fn);
}
function notifyAuthNeeded() {
  authPending = true;
  for (const fn of authListeners) {
    try {
      fn();
    } catch {
      /* ignore */
    }
  }
}

// 队列里有没有因为缺口令停下来的操作；读一次就清掉。
export function takeAuthPending() {
  const value = authPending;
  authPending = false;
  return value;
}

// 用户主动写完之后调它：把队列推一遍，并告诉调用方要不要弹口令框。
export async function syncNow() {
  try {
    await flush();
  } catch {
    /* flush 自己处理重试 */
  }
  return { authNeeded: takeAuthPending() };
}

async function send(op) {
  const url = `${API}/${op.path}`;
  const headers = { "Content-Type": "application/json" };
  const key = getEditKey();
  if (key) headers["X-Edit-Key"] = key;
  const init = { method: op.method, headers };
  if (op.body !== undefined) init.body = JSON.stringify(op.body);
  const res = await fetch(url, init);
  if (res.status === 401) throw new AuthError("edit key required");
  if (res.status >= 500) throw new Error(`server ${res.status}`);
  if (!res.ok) {
    // 4xx 说明这条数据本身不被接受，再重试也没用，丢掉并留一行日志。
    console.warn("同步被拒", op.path, res.status);
  }
}

let queueSeq = 0;

export async function flush() {
  if (flushing) return flushing;
  if (!readQueue().length) return null;
  flushing = (async () => {
    while (true) {
      // 每次都重新读队列：发送期间可能有新操作入队，不能拿旧快照覆盖回去
      const pending = readQueue();
      if (!pending.length) break;
      const op = pending[0];
      try {
        await send(op);
      } catch (err) {
        if (err instanceof AuthError) {
          // 没口令就停在这儿，等用户输入后 setEditKey() 再继续，别无限重试。
          notifyAuthNeeded();
          return;
        }
        clearTimeout(retryTimer);
        retryTimer = setTimeout(() => {
          backoff = Math.min(backoff * 2, 60000);
          flush();
        }, backoff);
        return;
      }
      const rest = readQueue();
      if (op.qid) writeQueue(rest.filter((item) => item.qid !== op.qid));
      else {
        rest.shift();
        writeQueue(rest);
      }
    }
    backoff = 4000;
    return null;
  })();
  try {
    return await flushing;
  } finally {
    flushing = null;
  }
}

export function enqueue(op) {
  const ops = readQueue();
  ops.push({ ...op, at: Date.now(), qid: `${Date.now()}-${++queueSeq}` });
  writeQueue(ops);
  if (navigator.onLine !== false) flush();
}

export const queueNote = (slug, note) =>
  enqueue({ method: "PUT", path: `notes/${slug}/${note.id}`, body: note });

export const queueDelete = (slug, id) =>
  enqueue({ method: "DELETE", path: `notes/${slug}/${id}` });

export const queueProgress = (slug, entry) =>
  enqueue({
    method: "PUT",
    path: `progress/${slug}`,
    body: { p: entry.p, t: entry.t, d: !!entry.d },
  });

async function getJSON(path) {
  const res = await fetch(`${API}/${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET ${path} ${res.status}`);
  return res.json();
}

export async function postReply(slug, noteId, text, author = "我") {
  const headers = { "Content-Type": "application/json" };
  const key = getEditKey();
  if (key) headers["X-Edit-Key"] = key;
  const res = await fetch(`${API}/notes/${slug}/${noteId}/replies`, {
    method: "POST",
    headers,
    body: JSON.stringify({ author, text, t: Date.now() }),
  });
  if (res.status === 401) throw new AuthError("edit key required");
  if (!res.ok) throw new Error(`reply ${res.status}`);
  return res.json();
}

export async function askAI(slug, noteId, { auto = false } = {}) {
  const headers = { "Content-Type": "application/json" };
  const key = getEditKey();
  if (key) headers["X-Edit-Key"] = key;
  const res = await fetch(`${API}/notes/${slug}/${noteId}/ask`, {
    method: "POST",
    headers,
    body: JSON.stringify({ auto }),
  });
  if (res.status === 401) throw new AuthError("edit key required");
  if (res.status === 409) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error === "no session" ? "这篇文章没有关联的会话" : "已经有一条在排队了");
  }
  if (!res.ok) throw new Error(`ask ${res.status}`);
  return res.json();
}

export function openNoteStream(slug, onEvent) {
  const source = new EventSource(`${API}/notes/${slug}/stream`);
  source.addEventListener("note.updated", (evt) => {
    try {
      onEvent("note.updated", JSON.parse(evt.data));
    } catch {
      /* 坏事件直接丢 */
    }
  });
  source.addEventListener("job.updated", (evt) => {
    try {
      onEvent("job.updated", JSON.parse(evt.data));
    } catch {
      /* 同上 */
    }
  });
  return source;
}

// summary 只拉一次：阅读进度和笔记都会用到它，没必要各发一遍。
// 注意：hugo 把每个入口单独打包，sync.js 会在两个 bundle 里各有一份实例，
// 所以缓存挂在 window 上，跨实例共享。
const summaryCache = (globalThis.__notesSummary ||= { promise: null, at: 0 });
export const pullSummary = (maxAgeMs = 30000) => {
  if (summaryCache.promise && Date.now() - summaryCache.at < maxAgeMs) {
    return summaryCache.promise;
  }
  summaryCache.at = Date.now();
  summaryCache.promise = getJSON("summary").catch((err) => {
    summaryCache.promise = null;
    throw err;
  });
  return summaryCache.promise;
};
export const pullNotes = (slug) => getJSON(`notes/${slug}`);
export const pullNotesBulk = (slugs) =>
  getJSON(`notes?slugs=${encodeURIComponent(slugs.join(","))}`).then((data) => data.items || {});

// 本地与服务端的笔记合并：服务端更新时间不早于本地就采用服务端；墓碑直接删。
export function mergeNotes(localList, serverNotes, deletedIds) {
  const byId = new Map((localList || []).map((n) => [n.id, n]));
  const adopted = new Map();
  for (const note of serverNotes || []) {
    const cur = byId.get(note.id);
    const clean = {
      id: note.id,
      q: note.q,
      pre: note.pre,
      post: note.post,
      block: note.block,
      t: note.t,
      thread: note.thread,
    };
    if (!cur || (note.updated || 0) >= (cur.t || 0)) {
      byId.set(note.id, clean);
      adopted.set(note.id, clean);
    }
  }
  for (const id of deletedIds || []) byId.delete(id);
  return { merged: [...byId.values()].sort((a, b) => a.t - b.t), adopted };
}

// 进度合并：取最大值，读完状态只增不减。
export function mergeProgress(local, server) {
  const out = { ...(local || {}) };
  for (const [slug, entry] of Object.entries(server || {})) {
    const cur = out[slug];
    if (!cur) {
      out[slug] = { p: entry.p, t: entry.t, d: !!entry.d };
      continue;
    }
    out[slug] = {
      p: Math.max(cur.p || 0, entry.p || 0),
      t: Math.max(cur.t || 0, entry.t || 0),
      d: !!(cur.d || entry.d),
    };
  }
  return out;
}

// 首次连上服务端时，把本地独有的笔记推上去（本地是这次合并的来源之一）。
export function uploadLocalOnly(slug, localList, adopted) {
  for (const note of localList || []) {
    if (!adopted.has(note.id)) queueNote(slug, note);
  }
}

window.addEventListener("online", () => {
  backoff = 4000;
  flush();
});
