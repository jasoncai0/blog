import {
  STORAGE_KEY,
  DONE_THRESHOLD,
  progressFromScroll,
  isDone,
  parseStore,
  serializeStore,
  writeEntry,
} from "./progress-core.mjs";

// 浏览器自带的滚动恢复会和续读打架，先接管。
history.scrollRestoration = "manual";

const FLUSH_MS = 1000;

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
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}
