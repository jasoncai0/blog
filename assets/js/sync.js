// 把「笔记 + 阅读进度」从 localStorage 搬到服务端：
// 页面先读本地（离线可用），再与 /api 对一遍；写操作先本地、再排队上行。
// 服务端地址默认同源 /api；测试里用 window.__NOTES_API__ 指到别的端口。

export const API = window.__NOTES_API__ || "/api";

const QUEUE_KEY = "jasoncai:sync-queue:v1";
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

async function send(op) {
  const url = `${API}/${op.path}`;
  const init = { method: op.method, headers: { "Content-Type": "application/json" } };
  if (op.body !== undefined) init.body = JSON.stringify(op.body);
  const res = await fetch(url, init);
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
      } catch {
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

export const pullSummary = () => getJSON("summary");
export const pullNotes = (slug) => getJSON(`notes/${slug}`);

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
