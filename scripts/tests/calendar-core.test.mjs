import test from "node:test";
import assert from "node:assert/strict";
import { parseIso, isoOf, alignWeek, buildGrid, level, windowStart, shiftWindow } from "../../assets/js/calendar-core.mjs";

test("parseIso 取本地零点，不受 UTC 影响", () => {
  const d = parseIso("2026-01-01");
  assert.equal(d.getFullYear(), 2026);
  assert.equal(d.getMonth(), 0);
  assert.equal(d.getDate(), 1);
  assert.equal(d.getHours(), 0);
});

test("alignWeek 把周日算进上一周", () => {
  assert.equal(isoOf(alignWeek(parseIso("2026-11-08"))), "2026-11-02");
  assert.equal(isoOf(alignWeek(parseIso("2026-11-09"))), "2026-11-09");
});

test("buildGrid 跨年时第一周从 2025-12-29 开始", () => {
  const grid = buildGrid("2026-01-01", "2026-01-10");
  assert.equal(grid[0][0], "2025-12-29");
  assert.equal(grid[0].length, 7);
  assert.equal(grid.at(-1).at(-1), "2026-01-11");
});

test("level 分级", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 28].map(level), [0, 1, 2, 3, 3, 4, 4]);
});

test("windowStart 默认贴住最后一列，并在边界夹住", () => {
  assert.equal(windowStart(53, 12, 52), 41);
  assert.equal(windowStart(5, 12, 4), 0);
  assert.equal(windowStart(53, 12, 3), 0);
});

test("shiftWindow 夹在 0..total-size", () => {
  assert.equal(shiftWindow(41, 1, 53, 12), 41);
  assert.equal(shiftWindow(41, -1, 53, 12), 40);
  assert.equal(shiftWindow(0, -1, 53, 12), 0);
});
