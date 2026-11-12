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
  formatMark,
  RESUME_MIN,
  DONE_THRESHOLD,
} from "./progress-core.mjs";

const FLUSH_MS = 1000;
const HINT_MS = 2500;

function safeGet() {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null; // 隐私模式 / 存储被禁用
  }
}

function safeSet(text) {
  try {
    localStorage.setItem(STORAGE_KEY, text);
    return true;
  } catch {
    return false;
  }
}

function saveEntry(slug, percent) {
  if (!slug) return;
  // 每次落盘都从 localStorage 重新读，合并其他标签页写进来的条目。
  const store = parseStore(safeGet() || "");
  safeSet(serializeStore(writeEntry(store, slug, percent, Date.now())));
}

// 首页：把每篇的最大进度标在标题下方，没读过的篇目不留空壳。
function markHome() {
  const store = parseStore(safeGet() || "");
  for (const item of document.querySelectorAll("[data-progress-post]")) {
    const mark = item.querySelector(".read-mark");
    if (!mark) continue;
    const text = formatMark(readEntry(store, item.dataset.progressPost));
    if (text) mark.textContent = text;
    else mark.remove();
  }
}

function boot() {
  const host = document.querySelector(".post-body[data-progress-post]");
  if (!host) {
    markHome();
    // 从文章页后退回来时页面可能直接来自 bfcache，标记要在恢复后重算一次
    window.addEventListener("pageshow", (event) => {
      if (event.persisted) markHome();
    });
    return;
  }
  const slug = host.dataset.progressPost;

  const bar = document.createElement("div");
  bar.className = "rp-bar";
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", "100");
  bar.setAttribute("aria-valuenow", "0");
  const fill = document.createElement("div");
  fill.className = "rp-bar-fill";
  bar.append(fill);

  const chip = document.createElement("div");
  chip.className = "rp-chip";
  chip.setAttribute("aria-hidden", "true"); // 与进度条同值的视觉副本，别让读屏念两遍
  chip.setAttribute("hidden", "");
  chip.textContent = "0%";

  document.body.append(bar, chip);

  let percent = 0;
  let revealed = false; // 用户真的滚动过（或续读跳转过）才露出胶囊
  let metrics = measure();
  let flushTimer = 0;
  let frame = 0;

  function measure() {
    const rect = host.getBoundingClientRect();
    return { top: rect.top + window.scrollY, height: host.offsetHeight };
  }

  function paint() {
    fill.style.width = `${percent}%`;
    bar.setAttribute("aria-valuenow", String(percent));
    chip.textContent = isDone(percent) ? "读完" : `${percent}%`;
    chip.classList.toggle("is-done", isDone(percent));
    const show = percent > 2 && (revealed || isDone(percent));
    chip.toggleAttribute("hidden", !show);
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
    if (isDone(percent)) flush(); // 都读完了，别等节流器（短文连滚动都不会有）
  }

  function onScroll() {
    revealed = true;
    watchMarker();
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
    if (location.hash) return false;
    if (navType() === "back_forward") return false;
    if (window.scrollY !== 0) return false;
    const entry = readEntry(parseStore(safeGet() || ""), slug);
    if (!shouldResume(entry)) return false;

    const target = Math.max(
      0,
      Math.round(scrollForProgress(entry.p, window.innerHeight, metrics.top, metrics.height)),
    );
    const root = document.documentElement;
    const previous = root.style.scrollBehavior;
    root.style.scrollBehavior = "auto"; // 站点的 smooth 会让续读变成慢慢滑下去
    window.scrollTo(0, target);
    root.style.scrollBehavior = previous;
    revealed = true;
    if (target > 0) showHint(entry.p); // 没真跳（目标被 clamp 到 0）就别提示
    onScroll();
    return true;
  }

  // 「上次读到这」：一条虚线 + 小标签，标在离开时那个百分比的位置。
  // 续读自动跳过去了就不画——用户已经在那个位置了。
  let marker = null;
  function placeMarker(entry) {
    if (!entry || entry.p < RESUME_MIN || entry.p >= DONE_THRESHOLD) return;
    if (document.querySelector(".rp-marker")) return;
    const y = Math.min(
      Math.max(scrollForProgress(entry.p, window.innerHeight, metrics.top, metrics.height) - metrics.top, 0),
      metrics.height,
    );
    // 线画在段落边界上，而不是穿字而过：取第一块「底边在 y 之下」的正文块。
    let snapped = metrics.height;
    for (const block of host.children) {
      if (!block.offsetHeight) continue;
      if (block.offsetTop + block.offsetHeight > y) {
        snapped = block.offsetTop;
        break;
      }
    }
    marker = document.createElement("div");
    marker.className = "rp-marker";
    marker.setAttribute("aria-hidden", "true"); // 纯视觉的辅助线，读屏不必念
    const label = document.createElement("span");
    label.textContent = `上次读到这 · ${Math.round(entry.p)}%`;
    marker.append(label);
    marker.style.top = `${Math.min(snapped, metrics.height)}px`;
    host.append(marker);
  }

  function watchMarker() {
    if (!marker || marker.classList.contains("is-passed")) return;
    if (marker.getBoundingClientRect().bottom < 0) {
      marker.classList.add("is-passed");
      const dying = marker;
      marker = null;
      setTimeout(() => dying.remove(), 320);
    }
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

  update(); // 首屏也算一次：长文顶部是真百分比，短文直接是 100%

  const resume = () =>
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        metrics = measure(); // 图片加载完可能改变正文高度
        const jumped = maybeResume();
        if (!jumped) placeMarker(readEntry(parseStore(safeGet() || ""), slug));
        update();
      }),
    );
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
