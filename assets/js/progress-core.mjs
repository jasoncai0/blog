// 阅读进度的纯逻辑：滚动换算、本地存储的读写与修剪。
// 没有 DOM 依赖，浏览器里由 reading-progress.js 调用，Node 里由测试直接跑。

export const STORAGE_KEY = "jasoncai:read:v1";
export const DONE_THRESHOLD = 95;
export const RESUME_MIN = 5;
export const MAX_ENTRIES = 300;
export const MAX_AGE_MS = 180 * 24 * 60 * 60 * 1000;

// 视口底部落在正文的哪个百分比上。顶部留白与文末不足一屏的部分都不计入。
export function progressFromScroll(scrollY, viewportHeight, contentTop, contentHeight) {
  if (!(contentHeight > 0)) return 0;
  // 移动端回弹会报负的 scrollY，先压平，否则顶部留白会被负值抵消。
  const y = Math.max(0, scrollY);
  const raw = ((y + viewportHeight - contentTop) / contentHeight) * 100;
  return Math.min(100, Math.max(0, Math.round(raw)));
}

// progressFromScroll 的反函数：要让视口底部落在 percent 上，页面该滚动到哪。
// 不 clamp——顶部几篇的 5% 会算出负数，交给调用方处理。
export function scrollForProgress(percent, viewportHeight, contentTop, contentHeight) {
  return (percent / 100) * contentHeight + contentTop - viewportHeight;
}

export function isDone(percent) {
  return percent >= DONE_THRESHOLD;
}

export function formatMark(entry) {
  if (!entry || typeof entry.p !== "number") return "";
  if (entry.d) return "读完";
  if (entry.p <= 0) return "";
  return `${Math.round(entry.p)}%`;
}

export function readEntry(store, slug, now = Date.now()) {
  if (!store || typeof store !== "object") return null;
  const entry = store[slug];
  if (!entry || typeof entry !== "object") return null;
  if (typeof entry.p !== "number" || !Number.isFinite(entry.p)) return null;
  // 写入时的修剪只在下次落盘发生，读的时候也要按时效兜住，别把半年前的记录显示出来
  if (typeof entry.t === "number" && now - entry.t > MAX_AGE_MS) return null;
  return { p: entry.p, t: entry.t, d: entry.d === true };
}

// 同一篇只记最高进度；d 一旦为真就不再回落；t 每次刷新，用于修剪。
// 这里刻意不看 180 天时效：过期只是「不再展示」，不能让一条 100% 的记录
// 在下次重读 20% 时被当成不存在，否则进度和读完状态都会回退。
export function writeEntry(store, slug, percent, now) {
  const next = { ...(store || {}) };
  const raw = next[slug];
  const old =
    raw && typeof raw.p === "number" && Number.isFinite(raw.p)
      ? { p: raw.p, t: raw.t, d: raw.d === true }
      : null;
  const p = Math.min(100, Math.max(0, Math.round(Number(percent) || 0)));
  const best = old ? Math.max(old.p, p) : p;
  next[slug] = {
    p: best,
    t: now,
    d: (old && old.d) || best >= DONE_THRESHOLD,
  };
  return prune(next, now);
}

function prune(store, now) {
  const kept = {};
  for (const [slug, entry] of Object.entries(store)) {
    if (!entry || typeof entry.t !== "number" || !Number.isFinite(entry.t)) continue;
    if (now - entry.t > MAX_AGE_MS) continue;
    kept[slug] = entry;
  }
  const slugs = Object.keys(kept);
  if (slugs.length <= MAX_ENTRIES) return kept;
  slugs.sort((a, b) => kept[a].t - kept[b].t);
  const out = {};
  for (const slug of slugs.slice(slugs.length - MAX_ENTRIES)) out[slug] = kept[slug];
  return out;
}

export function parseStore(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    return {};
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return {};
  const out = {};
  for (const [slug, entry] of Object.entries(data)) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
    const { p, t } = entry;
    if (typeof p !== "number" || !Number.isFinite(p) || p < 0 || p > 100) continue;
    if (typeof t !== "number" || !Number.isFinite(t)) continue;
    out[slug] = { p: Math.round(p), t, d: entry.d === true };
  }
  return out;
}

export function serializeStore(store) {
  const out = {};
  for (const [slug, entry] of Object.entries(store || {})) {
    out[slug] = { p: entry.p, t: entry.t, d: entry.d === true };
  }
  return JSON.stringify(out);
}

export function shouldResume(entry) {
  if (!entry || typeof entry.p !== "number") return false;
  return entry.p >= RESUME_MIN && entry.p < DONE_THRESHOLD;
}
