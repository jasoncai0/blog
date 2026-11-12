// 侧栏交互：写作日历的窗口/平移/日期跳转、点日期闪文章。
// 目录的折叠与滚动高亮在后续任务里加到这里。

import { windowStart, shiftWindow } from "./calendar-core.mjs";

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

function initCalendar() {
  const root = document.querySelector("nav.calendar");
  if (!root) return;
  const weeks = [...root.querySelectorAll(".cal-week")];
  if (!weeks.length) return;

  const total = weeks.length;
  const todayIso = root.dataset.today;
  const todayWeek = todayIso
    ? Number(root.querySelector(`[data-date="${todayIso}"]`)?.closest(".cal-week")?.dataset.week ?? total - 1)
    : total - 1;
  let size = 12;
  let start = windowStart(total, size, todayWeek);

  const paint = () => {
    weeks.forEach((week, i) => {
      week.classList.toggle("is-hidden", i < start || i >= start + size);
    });
  };

  const scroller = root.querySelector(".cal-scroll");
  const revealWeek = (index) => {
    const week = weeks[index];
    if (!scroller || !week) return;
    const left = week.offsetLeft;
    if (left < scroller.scrollLeft || left + week.offsetWidth > scroller.scrollLeft + scroller.clientWidth) {
      scroller.scrollLeft = Math.max(left - 24, 0);
    }
  };

  const setSize = (next) => {
    size = next;
    start = next >= total ? 0 : windowStart(total, size, todayWeek);
    paint();
    if (next >= total) revealWeek(todayWeek);
    root.querySelectorAll("[data-cal-range]").forEach((btn) => {
      const on = btn.dataset.calRange === String(next === total ? "year" : next);
      btn.classList.toggle("is-on", on);
      btn.setAttribute("aria-pressed", String(on));
    });
  };

  root.querySelectorAll("[data-cal-range]").forEach((btn) => {
    btn.addEventListener("click", () => {
      setSize(btn.dataset.calRange === "year" ? total : Number(btn.dataset.calRange));
    });
  });

  root.querySelector(".cal-prev")?.addEventListener("click", () => {
    if (size >= total) {
      scroller.scrollLeft = Math.max(scroller.scrollLeft - scroller.clientWidth, 0);
      return;
    }
    start = shiftWindow(start, -1, total, size);
    paint();
  });
  root.querySelector(".cal-next")?.addEventListener("click", () => {
    if (size >= total) {
      scroller.scrollLeft += scroller.clientWidth;
      return;
    }
    start = shiftWindow(start, 1, total, size);
    paint();
  });

  const jump = root.querySelector(".cal-jump");
  jump?.addEventListener("change", () => {
    if (!jump.value) return;
    const cell = root.querySelector(`[data-date="${jump.value}"]`);
    if (!cell) return;
    const week = cell.closest(".cal-week");
    start = windowStart(total, size, Number(week.dataset.week));
    paint();
    weeks.forEach((w) => w.classList.remove("is-target"));
    week.classList.add("is-target");
    revealWeek(Number(week.dataset.week));
    window.setTimeout(() => week.classList.remove("is-target"), 2000);
  });

  paint();
}

/** 点日历上有文章的日期：闪一下那天在首页的条目并滚过去。 */
function initDayClick() {
  document.addEventListener("click", (event) => {
    const cell = event.target.closest("a.heat-cell[data-date]");
    if (!cell) return;
    const date = cell.dataset.date;
    const items = document.querySelectorAll(`ul.posts li[data-date="${date}"]`);
    if (!items.length) return; // 交给默认跳转
    event.preventDefault();
    items.forEach((item) => item.classList.add("is-flash"));
    items[0].scrollIntoView({ block: "center", behavior: "smooth" });
    window.setTimeout(() => items.forEach((item) => item.classList.remove("is-flash")), 1600);
  });
}

const TOC_KEY = "jasoncai:toc:collapsed";

function initToc() {
  const rail = document.querySelector("aside.rail-article");
  if (!rail) return;
  const btn = rail.querySelector(".toc-toggle");
  const apply = (collapsed) => {
    rail.classList.toggle("is-collapsed", collapsed);
    if (!btn) return;
    btn.setAttribute("aria-expanded", String(!collapsed));
    btn.textContent = collapsed ? "»" : "收起";
    btn.title = collapsed ? "展开目录" : "收起目录";
  };
  let stored = null;
  try {
    stored = window.localStorage.getItem(TOC_KEY);
  } catch {
    // 存储被禁用：只在本次会话内折叠
  }
  // 窄屏首次进来默认收起：展开的目录会把标题挤到屏幕外
  const narrow = window.matchMedia("(max-width: 900px)").matches;
  let collapsed = stored === null ? narrow : stored === "1";
  apply(collapsed);
  btn?.addEventListener("click", () => {
    collapsed = !rail.classList.contains("is-collapsed");
    apply(collapsed);
    try {
      window.localStorage.setItem(TOC_KEY, collapsed ? "1" : "0");
    } catch {
      // 同上
    }
  });
}

export function boot() {
  initCalendar();
  initDayClick();
  initToc();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}

export { WEEK_MS };
