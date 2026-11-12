// 笔记的本地存储核心：不碰 DOM，node 可直接测。
// 一条笔记 = 一段划线的锚点（q + 前后各 ANCHOR_PAD 字 + 段落下标）+ 一串批注。
// thread[0] 永远是读者本人写的；将来 AI 的回答以 {author:"AI"} 往后追加。

export const NOTES_KEY = "jasoncai:notes:v1";
export const ANCHOR_PAD = 32;

let seq = 0;

export function newId(now = Date.now()) {
  seq = (seq + 1) % 1e6;
  const rnd = Math.random().toString(36).slice(2, 8);
  return `n${now.toString(36)}-${seq.toString(36)}${rnd}`;
}

export function makeNote({ q, pre = "", post = "", block = 0, t = Date.now(), text, author = "我" }) {
  return {
    id: newId(t),
    q,
    pre,
    post,
    block,
    t,
    thread: [{ author, text, t }],
  };
}

const isText = (v) => typeof v === "string";

function validThreadItem(item) {
  return item && typeof item === "object" && isText(item.author) && isText(item.text);
}

function validNote(note) {
  if (!note || typeof note !== "object") return false;
  if (!isText(note.id) || note.id === "") return false;
  if (!isText(note.q) || note.q === "") return false;
  if (!Array.isArray(note.thread)) return false;
  return note.thread.every(validThreadItem);
}

function cleanNote(note) {
  return {
    id: note.id,
    q: note.q,
    pre: isText(note.pre) ? note.pre : "",
    post: isText(note.post) ? note.post : "",
    block: Number.isFinite(note.block) ? note.block : 0,
    t: Number.isFinite(note.t) ? note.t : 0,
    thread: note.thread.map((m) => ({
      author: m.author,
      text: m.text,
      t: Number.isFinite(m.t) ? m.t : 0,
    })),
  };
}

export function parseStore(raw) {
  if (typeof raw !== "string" || raw === "") return {};
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
  const out = {};
  for (const [slug, list] of Object.entries(parsed)) {
    if (!Array.isArray(list)) continue;
    const kept = list.filter(validNote).map(cleanNote);
    if (kept.length) out[slug] = kept;
  }
  return out;
}

export function serializeStore(all) {
  const out = {};
  for (const [slug, list] of Object.entries(all || {})) {
    if (!Array.isArray(list)) continue;
    const kept = list.filter(validNote).map(cleanNote);
    if (kept.length) out[slug] = kept;
  }
  return JSON.stringify(out);
}

export function readNotes(store) {
  try {
    return parseStore(store.getItem(NOTES_KEY));
  } catch {
    return {};
  }
}

export function writeNotes(store, all) {
  try {
    store.setItem(NOTES_KEY, serializeStore(all));
    return true;
  } catch {
    return false;
  }
}

export function notesFor(all, slug) {
  if (!slug || !all || !Array.isArray(all[slug])) return [];
  return all[slug];
}

export function countFor(all, slug) {
  return notesFor(all, slug).length;
}

export function addNote(all, slug, note) {
  if (!slug) return all;
  const list = notesFor(all, slug);
  return { ...all, [slug]: [...list, note] };
}

export function setThreadText(all, slug, id, text, t) {
  const list = notesFor(all, slug);
  const next = list.map((note) => {
    if (note.id !== id) return note;
    const thread = note.thread.map((m, i) => (i === 0 ? { ...m, text } : m));
    return { ...note, thread };
  });
  return { ...all, [slug]: next };
}

export function removeNote(all, slug, id) {
  const list = notesFor(all, slug).filter((note) => note.id !== id);
  return { ...all, [slug]: list };
}
