import {
  STORAGE_KEY,
  progressFromScroll,
  scrollForProgress,
  isDone,
  parseStore,
  serializeStore,
  writeEntry,
  readEntry,
  shouldResume,
} from "./progress-core.mjs";

const FLUSH_MS = 1000;
const HINT_MS = 2500;

function readRaw() {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null; // 隐私模式 / 存储被禁用
  }
}

function saveEntry(slug, percent) {
  if (!slug) return;
  try {
    // 每次落盘都从 localStorage 重新读，合并其他标签页写进来的条目。
    const store = parseStore(readRaw() || "");
    localStorage.setItem(STORAGE_KEY, serializeStore(writeEntry(store, slug, percent, Date.now())));
  } catch {
    // 存不进去就算了，不影响阅读
  }
}

function boot() {
  const host = document.querySelector(".post-body[data-progress-post]");
  if (!host) return;
  const slug = host.dataset.progressPost;

  const bar = document.createElement("div");
  bar.className = "rp-bar";
  bar.setAttribute("aria-hidden", "true");
  const fill = document.createElement("div");
  fill.className = "rp-bar-fill";
  bar.append(fill);

  const chip = document.createElement("div");
  chip.className = "rp-chip";
  chip.setAttribute("hidden", "");
  chip.textContent = "0%";

  document.body.append(bar, chip);

  let percent = 0;
  let metrics = measure();
  let flushTimer = 0;
  let frame = 0;

  function measure() {
    const rect = host.getBoundingClientRect();
    return { top: rect.top + window.scrollY, height: host.offsetHeight };
  }

  function paint() {
    fill.style.width = `${percent}%`;
    chip.textContent = isDone(percent) ? "读完" : `${percent}%`;
    chip.classList.toggle("is-done", isDone(percent));
    chip.toggleAttribute("hidden", percent <= 2);
  }

  function flush() {
    if (flushTimer) {
      clearTimeout(flushTimer);
      flushTimer = 0;
    }
    saveEntry(slug, percent);
  }

  function scheduleFlush() {
    if (flushTimer) return;
    flushTimer = setTimeout(() => {
      flushTimer = 0;
      saveEntry(slug, percent);
    }, FLUSH_MS);
  }

  function update() {
    frame = 0;
    percent = progressFromScroll(window.scrollY, window.innerHeight, metrics.top, metrics.height);
    paint();
    scheduleFlush();
  }

  function onScroll() {
    if (frame) return;
    frame = requestAnimationFrame(update);
  }

  function navType() {
    const [nav] = performance.getEntriesByType("navigation");
    return nav ? nav.type : "navigate";
  }

  function showHint(percent) {
    const hint = document.createElement("div");
    hint.className = "rp-hint";
    hint.setAttribute("role", "status");
    hint.textContent = `从 ${percent}% 继续`;
    document.body.append(hint);
    setTimeout(() => {
      hint.classList.add("is-out");
      setTimeout(() => hint.remove(), 400);
    }, HINT_MS);
  }

  // 续读：只在「干净地打开这一页」时接手。锚点、后退/前进、浏览器已经恢复过
  // 滚动位置（scrollY !== 0）这三种情况都不抢——浏览器比我们知道得更多。
  function maybeResume() {
    if (location.hash) return;
    if (navType() === "back_forward") return;
    if (window.scrollY !== 0) return;
    const entry = readEntry(parseStore(readRaw() || ""), slug);
    if (!shouldResume(entry)) return;

    metrics = measure();
    const target = Math.max(
      0,
      Math.round(scrollForProgress(entry.p, window.innerHeight, metrics.top, metrics.height)),
    );
    const root = document.documentElement;
    const previous = root.style.scrollBehavior;
    root.style.scrollBehavior = "auto"; // 站点的 smooth 会让续读变成慢慢滑下去
    window.scrollTo(0, target);
    root.style.scrollBehavior = previous;
    showHint(entry.p);
    onScroll();
  }

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", () => {
    metrics = measure();
    onScroll();
  });
  window.addEventListener("pagehide", flush);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });

  paint();

  const resume = () => requestAnimationFrame(() => requestAnimationFrame(maybeResume));
  if (document.readyState === "complete") {
    resume();
  } else {
    window.addEventListener("load", resume, { once: true });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}
