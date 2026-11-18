import test from "node:test";
import assert from "node:assert/strict";
import * as c from "../../assets/js/progress-core.mjs";

test("progressFromScroll 把视口底部映射到 0–100", () => {
  assert.equal(c.progressFromScroll(0, 800, 300, 2000), 25);
  assert.equal(c.progressFromScroll(1500, 800, 300, 2000), 100); // 越界截断
  assert.equal(c.progressFromScroll(0, 800, 500, 2000), 15);     // 顶部留白不计
  assert.equal(c.progressFromScroll(-200, 800, 300, 2000), 25);  // 回弹负值按 0 处理
});

test("一屏放得下时算 100，正文起点在视口下方时不算", () => {
  assert.equal(c.progressFromScroll(0, 800, 300, 400), 100);
  // 正文 1099px 高、从 411px 开始：视口 1200 只看到 789px，不是 100
  assert.equal(c.progressFromScroll(0, 1200, 411, 1099), 72);
  assert.equal(c.progressFromScroll(0, 800, 411, 1099), 35);
});

test("scrollForProgress 是 progressFromScroll 的反函数", () => {
  assert.equal(c.scrollForProgress(25, 800, 300, 2000), 0);
  assert.equal(c.scrollForProgress(50, 800, 300, 2000), 500);
  assert.equal(c.scrollForProgress(5, 800, 300, 2000), -400); // 交由调用方 clamp
});

test("isDone 与 formatMark", () => {
  assert.equal(c.isDone(94), false);
  assert.equal(c.isDone(95), true);
  assert.equal(c.formatMark({ p: 62, d: false }), "62%");
  assert.equal(c.formatMark({ p: 96, d: true }), "读完");
  assert.equal(c.formatMark(null), "");
});

test("writeEntry 取最大进度且 d 只增不减", () => {
  let s = c.writeEntry({}, "a", 70, 1000);
  s = c.writeEntry(s, "a", 40, 2000);
  assert.deepEqual(s.a, { p: 70, t: 2000, d: false });
  s = c.writeEntry(s, "a", 96, 3000);
  assert.deepEqual(s.a, { p: 96, t: 3000, d: true });
  s = c.writeEntry(s, "a", 10, 4000);
  assert.deepEqual(s.a, { p: 96, t: 4000, d: true });
});

test("readEntry 对超过 180 天的记录返回 null", () => {
  const now = 10 * c.MAX_AGE_MS;
  const store = { old: { p: 62, t: now - c.MAX_AGE_MS - 1, d: false }, fresh: { p: 62, t: now - 1000, d: false } };
  assert.equal(c.readEntry(store, "old", now), null);
  assert.equal(c.readEntry(store, "fresh", now).p, 62);
});

test("writeEntry 修剪过期与超量条目，且保留其他篇", () => {
  const old = 1000;
  const now = old + c.MAX_AGE_MS + 1;
  let s = { stale: { p: 50, t: old, d: false }, keep: { p: 10, t: now - 1000, d: false } };
  s = c.writeEntry(s, "new", 30, now);
  assert.equal(s.stale, undefined);
  assert.ok(s.keep && s.new);
});

test("条目超过 300 时丢最旧的", () => {
  let s = {};
  for (let i = 0; i < 301; i++) s = c.writeEntry(s, `p${i}`, 10, 1000 + i);
  assert.equal(Object.keys(s).length, 300);
  assert.equal(s.p0, undefined);
  assert.ok(s.p300);
});

test("parseStore 容忍坏数据", () => {
  assert.deepEqual(c.parseStore("not json"), {});
  assert.deepEqual(c.parseStore("[]"), {});
  assert.deepEqual(c.parseStore('{"a":{"p":-5,"t":"x"}}'), {});
  assert.deepEqual(c.parseStore('{"a":{"p":62,"t":1,"d":false}}').a.p, 62);
  assert.equal(c.serializeStore({ a: { p: 1, t: 1, d: false } }), '{"a":{"p":1,"t":1,"d":false}}');
});

test("shouldResume 只在 5–94 之间为真", () => {
  assert.equal(c.shouldResume({ p: 4, d: false }), false);
  assert.equal(c.shouldResume({ p: 5, d: false }), true);
  assert.equal(c.shouldResume({ p: 94, d: false }), true);
  assert.equal(c.shouldResume({ p: 95, d: true }), false);
  assert.equal(c.shouldResume(null), false);
});

test("过期条目不会让进度回退", () => {
  const now = Date.now();
  const stale = now - (c.MAX_AGE_MS + 24 * 60 * 60 * 1000);
  const store = { hello: { p: 100, t: stale, d: true } };
  const next = c.writeEntry(store, "hello", 20, now);
  assert.equal(next.hello.p, 100, "旧记录虽然过期，重读时也不能把 100% 拉回 20%");
  assert.equal(next.hello.d, true, "读完状态不能回退");
});

test("两次写入取较大者，读完状态只增不减", () => {
  const now = Date.now();
  let s = c.writeEntry({}, "a", 80, now);
  s = c.writeEntry(s, "a", 40, now + 1000);
  assert.equal(s.a.p, 80);
  s = c.writeEntry(s, "a", 99, now + 2000);
  assert.equal(s.a.d, true);
  s = c.writeEntry(s, "a", 30, now + 3000);
  assert.equal(s.a.p, 99);
  assert.equal(s.a.d, true);
});
