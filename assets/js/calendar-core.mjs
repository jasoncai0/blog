// 日历网格的纯逻辑：不碰 DOM，不进 localStorage。

const DAY = 24 * 60 * 60 * 1000;

/** "2026-01-01" → 本地零点的 Date（new Date("2026-01-01") 会按 UTC 解析）。 */
export function parseIso(s) {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
}

/** Date → "YYYY-MM-DD"（本地时区）。 */
export function isoOf(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** 该周周一（本地零点）。周日归上一周。 */
export function alignWeek(date) {
  const back = (date.getDay() + 6) % 7;
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() - back);
}

/** 从 firstIso 所在周排到 lastIso 所在周，每列 7 天（周一→周日）。 */
export function buildGrid(firstIso, lastIso) {
  const end = alignWeek(parseIso(lastIso));
  const weeks = [];
  let cursor = alignWeek(parseIso(firstIso));
  while (cursor <= end) {
    const week = [];
    for (let i = 0; i < 7; i++) {
      week.push(isoOf(new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() + i)));
    }
    weeks.push(week);
    cursor = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() + 7);
  }
  return weeks;
}

/** 当天文章数 → 色阶 0..4。 */
export function level(count) {
  if (count <= 0) return 0;
  if (count === 1) return 1;
  if (count === 2) return 2;
  if (count <= 4) return 3;
  return 4;
}

/** 让窗口贴住 anchorIndex 的起始列，夹在 0..total-size。 */
export function windowStart(totalWeeks, size, anchorIndex) {
  const max = Math.max(totalWeeks - size, 0);
  return Math.min(Math.max(anchorIndex - size + 1, 0), max);
}

/** 平移窗口并夹住。 */
export function shiftWindow(start, delta, total, size) {
  const max = Math.max(total - size, 0);
  return Math.min(Math.max(start + delta, 0), max);
}

export { DAY };
