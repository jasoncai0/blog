// 收藏：本机先落盘，再和服务器对账。收藏是匿名的（和阅读进度一样），
// 访客不用口令就能点；所以换浏览器、清缓存之后还能从服务端取回来。
import { getItem, setItem } from "./user-store.js";
import { authToken } from "./sync.js";

const authHeaders = () => (authToken() ? { Authorization: `Bearer ${authToken()}` } : {});

const API = window.__NOTES_API__ || "/api";
const KEY = "jasoncai:fav:v2";
const QUEUE_KEY = "jasoncai:fav-queue:v2";

function readJSON(key, fallback) {
  try {
    const raw = getItem(key);
    const value = raw ? JSON.parse(raw) : null;
    return value && typeof value === "object" ? value : fallback;
  } catch {
    return fallback;
  }
}

function writeJSON(key, value) {
  try {
    setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

function normalize(input) {
  const out = {};
  for (const [slug, t] of Object.entries(input || {})) {
    if (typeof slug !== "string" || !slug) continue;
    out[slug] = Number.isFinite(t) ? t : Date.now();
  }
  return out;
}

let favorites = normalize(readJSON(KEY, {}));

function isFav(slug) {
  return Object.prototype.hasOwnProperty.call(favorites, slug);
}

function count() {
  return Object.keys(favorites).length;
}

function save() {
  writeJSON(KEY, favorites);
}

function pending() {
  const list = readJSON(QUEUE_KEY, []);
  return Array.isArray(list) ? list : [];
}

function enqueue(slug, on) {
  const list = pending().filter((op) => op.slug !== slug);
  list.push({ slug, on });
  writeJSON(QUEUE_KEY, list);
}

function send(slug, on) {
  const init = {
    method: on ? "PUT" : "DELETE",
    headers: { "Content-Type": "application/json", ...authHeaders() },
  };
  if (on) init.body = JSON.stringify({ t: favorites[slug] || Date.now() });
  return fetch(`${API}/favorites/${encodeURIComponent(slug)}`, init).then((res) => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  });
}

function flush() {
  const list = pending();
  if (!list.length) return Promise.resolve();
  return list
    .reduce(
      (chain, op) =>
        chain.then(() =>
          send(op.slug, op.on)
            .then(() => {
              const rest = pending().filter((x) => x.slug !== op.slug);
              writeJSON(QUEUE_KEY, rest);
            })
            .catch(() => {}),
        ),
      Promise.resolve(),
    )
    .then(() => {});
}

function push(slug, on) {
  send(slug, on).catch(() => enqueue(slug, on));
}

// 服务端是收藏这一份清单的准绳：拉下来直接覆盖本地，取消收藏才能同步到别的设备。
// 唯一例外是还没发出去的改动（离线时点的星），它们排在队列里、拉取后再盖回去。
function pull() {
  if (!authToken()) return Promise.resolve(); // 访客只存本机
  return fetch(`${API}/favorites`, { headers: { Accept: "application/json", ...authHeaders() } })
    .then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    })
    .then((data) => {
      favorites = normalize(data.favorites);
      for (const op of pending()) {
        if (op.on) favorites[op.slug] = favorites[op.slug] || Date.now();
        else delete favorites[op.slug];
      }
      save();
      render();
    })
    .catch(() => {});
}

function label(on) {
  return on ? "★ 已收藏" : "☆ 收藏";
}

function paint(button, slug) {
  const on = isFav(slug);
  button.setAttribute("aria-pressed", on ? "true" : "false");
  button.setAttribute("aria-label", on ? "取消收藏" : "收藏这篇");
  button.textContent = label(on);
}

function toggle(slug) {
  const on = !isFav(slug);
  if (on) favorites[slug] = Date.now();
  else delete favorites[slug];
  save();
  push(slug, on);
  render();
  return on;
}

function render() {
  for (const button of document.querySelectorAll(".fav-btn")) {
    paint(button, button.dataset.slug);
  }
  document.dispatchEvent(new CustomEvent("fav:changed"));
  if (typeof window.__refreshHomeFilter === "function") window.__refreshHomeFilter();
}

function makeButton(slug) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "fav-btn";
  button.dataset.slug = slug;
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    toggle(slug);
  });
  paint(button, slug);
  return button;
}

function mount() {
  for (const item of document.querySelectorAll("ul.posts li[data-progress-post]")) {
    if (item.querySelector(".fav-btn")) continue;
    const foot = item.querySelector(".post-foot") || item;
    const button = makeButton(item.dataset.progressPost);
    const mark = foot.querySelector(".read-mark");
    if (mark) foot.insertBefore(button, mark);
    else foot.append(button);
  }
  const meta = document.querySelector(".post-meta");
  const body = document.querySelector(".post-body[data-progress-post]");
  if (meta && body && !meta.querySelector(".fav-btn")) meta.append(makeButton(body.dataset.progressPost));
}

function boot() {
  mount();
  render();
  flush().then(pull);
}

// 首页过滤要用：提示 reading-progress.js 哪些篇被收藏了。
window.__favIs = isFav;
window.__favCount = count;

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}
