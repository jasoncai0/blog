import test from "node:test";
import assert from "node:assert/strict";

import {
  NOTES_KEY,
  ANCHOR_PAD,
  newId,
  makeNote,
  readNotes,
  writeNotes,
  notesFor,
  addNote,
  setThreadText,
  removeNote,
  countFor,
  parseStore,
  serializeStore,
} from "../../assets/js/notes-core.mjs";

test("常量固定：键名与锚点 padding", () => {
  assert.equal(NOTES_KEY, "jasoncai:notes:v1");
  assert.equal(ANCHOR_PAD, 32);
});

test("makeNote 组装出 id + thread[0]，newId 不重复", () => {
  const a = makeNote({ q: "甲", pre: "前", post: "后", block: 3, t: 1000, text: "批注", author: "我" });
  assert.equal(a.q, "甲");
  assert.equal(a.block, 3);
  assert.equal(a.thread.length, 1);
  assert.deepEqual(a.thread[0], { author: "我", text: "批注", t: 1000 });
  assert.ok(a.id);
  assert.notEqual(newId(1000), newId(1000));
});

test("addNote 追加并按 slug 分开存", () => {
  const n = makeNote({ q: "甲", pre: "", post: "", block: 3, t: 1000, text: "批注", author: "我" });
  const all = addNote({}, "a", n);
  assert.equal(notesFor(all, "a").length, 1);
  assert.equal(notesFor(all, "a")[0].thread[0].author, "我");
  assert.deepEqual(notesFor(all, "b"), []);
});

test("setThreadText 只改 thread[0].text，不动作者与时间", () => {
  const n = makeNote({ q: "甲", pre: "", post: "", block: 0, t: 1000, text: "旧", author: "我" });
  const all = addNote({}, "a", n);
  const next = setThreadText(all, "a", n.id, "新", 2000);
  const note = notesFor(next, "a")[0];
  assert.equal(note.thread[0].text, "新");
  assert.equal(note.thread[0].author, "我");
  assert.equal(note.thread[0].t, 1000);
  assert.equal(notesFor(all, "a")[0].thread[0].text, "旧", "不改原对象");
});

test("setThreadText 保留 AI 追加的后续回答", () => {
  const n = makeNote({ q: "甲", pre: "", post: "", block: 0, t: 1000, text: "问", author: "我" });
  n.thread.push({ author: "AI", text: "答", t: 1100 });
  const all = addNote({}, "a", n);
  const note = notesFor(setThreadText(all, "a", n.id, "问（改）", 2000), "a")[0];
  assert.deepEqual(note.thread.map((m) => m.author), ["我", "AI"]);
  assert.equal(note.thread[1].text, "答");
});

test("removeNote 移除指定 id，其他笔记不动", () => {
  const a = makeNote({ q: "甲", pre: "", post: "", block: 0, t: 1, text: "一", author: "我" });
  const b = makeNote({ q: "乙", pre: "", post: "", block: 1, t: 2, text: "二", author: "我" });
  const all = addNote(addNote({}, "s", a), "s", b);
  const next = removeNote(all, "s", a.id);
  assert.deepEqual(notesFor(next, "s").map((n) => n.q), ["乙"]);
  assert.equal(notesFor(all, "s").length, 2, "不改原对象");
});

test("parseStore 丢掉坏条目但保留好条目", () => {
  const all = parseStore('{"a":[{"id":"x","q":"甲","thread":[]},{"id":"y"}],"b":5}');
  assert.equal(notesFor(all, "a").length, 1);
  assert.deepEqual(notesFor(all, "b"), []);
});

test("parseStore 对非 JSON、非对象、坏 thread 都返回空结构", () => {
  assert.deepEqual(parseStore("不是 json"), {});
  assert.deepEqual(parseStore(null), {});
  const all = parseStore('{"a":[{"id":"x","q":"甲","thread":[{"author":"我"}]}]}');
  assert.deepEqual(notesFor(all, "a"), []);
});

test("writeNotes 在 store 抛 QuotaExceededError 时返回 false 且不抛", () => {
  const store = {
    setItem() {
      const err = new Error("full");
      err.name = "QuotaExceededError";
      throw err;
    },
  };
  assert.equal(writeNotes(store, { a: [] }), false);
});

test("writeNotes / readNotes 往返", () => {
  const data = new Map();
  const store = {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, v),
  };
  const n = makeNote({ q: "甲", pre: "前", post: "后", block: 2, t: 5, text: "批注", author: "我" });
  assert.equal(writeNotes(store, addNote({}, "slug", n)), true);
  const back = readNotes(store);
  assert.equal(notesFor(back, "slug").length, 1);
  assert.equal(notesFor(back, "slug")[0].q, "甲");
});

test("serializeStore 只写白名单字段", () => {
  const all = { a: [{ id: "x", q: "甲", pre: "", post: "", block: 0, t: 1, junk: "x", thread: [{ author: "我", text: "t", t: 1, junk: 2 }] }] };
  const raw = serializeStore(all);
  assert.ok(!raw.includes("junk"));
  assert.equal(JSON.parse(raw).a[0].thread[0].text, "t");
});

test("countFor 统计条数，空 slug 返回 0", () => {
  const n = makeNote({ q: "甲", pre: "", post: "", block: 0, t: 1, text: "一", author: "我" });
  const all = addNote({}, "a", n);
  assert.equal(countFor(all, "a"), 1);
  assert.equal(countFor(all, "b"), 0);
  assert.equal(countFor({}, ""), 0);
});
