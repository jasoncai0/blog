// 图放大：正文里的图一律按栏宽缩放，看不见细节时点右上角「放大」，
// 在灰屏遮罩里按视口尺寸重画一份（SVG 是矢量，放大不糊）。
const OPEN_CLASS = "is-open";

let box = null;
let restoreFocus = null;
let prevOverflow = null;

function build() {
  const el = document.createElement("div");
  el.className = "lb";
  el.hidden = true;
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-modal", "true");
  el.innerHTML =
    '<button class="lb-close" type="button" aria-label="关闭">×</button>' +
    '<div class="lb-stage"></div><p class="lb-cap"></p>';
  document.body.appendChild(el);
  return el;
}

// 克隆出来的图会和原图撞 id（箭头 marker、clipPath 都靠 id 引用），统一加前缀。
function isolateIds(root) {
  const map = new Map();
  root.querySelectorAll("[id]").forEach((node) => {
    const next = "lb-" + node.id;
    map.set(node.id, next);
    node.id = next;
  });
  if (!map.size) return;
  const walk = (el) => {
    for (const attr of [...el.attributes]) {
      const value = attr.value;
      if (!value.includes("#") && !value.includes("url(")) continue;
      let out = value;
      for (const [from, to] of map) {
        out = out.replaceAll(`url(#${from})`, `url(#${to})`).replaceAll(`#${from}`, `#${to}`);
      }
      if (out !== value) el.setAttribute(attr.name, out);
    }
    [...el.children].forEach(walk);
  };
  walk(root);
  if (root.tagName.toLowerCase() === "svg") {
    for (const attr of ["href", "xlink:href"]) {
      const value = root.getAttribute(attr);
      if (!value) continue;
      const hit = map.get(value.slice(1));
      if (hit) root.setAttribute(attr, "#" + hit);
    }
  }
}

function fit(copy) {
  const viewBox = copy.viewBox?.baseVal;
  const ratio = viewBox && viewBox.width && viewBox.height ? viewBox.width / viewBox.height : 1.6;
  const maxW = window.innerWidth - 80;
  const maxH = window.innerHeight - 130;
  let width = maxW;
  let height = width / ratio;
  if (height > maxH) {
    height = maxH;
    width = height * ratio;
  }
  copy.setAttribute("width", String(Math.round(width)));
  copy.setAttribute("height", String(Math.round(height)));
  copy.style.width = `${Math.round(width)}px`;
  copy.style.height = `${Math.round(height)}px`;
}

function close() {
  if (!box || box.hidden) return;
  box.classList.remove(OPEN_CLASS);
  const done = () => {
    box.hidden = true;
    box.querySelector(".lb-stage").replaceChildren();
  };
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) done();
  else window.setTimeout(done, 160);
  document.documentElement.style.overflow = prevOverflow ?? "";
  prevOverflow = null;
  restoreFocus?.focus();
  restoreFocus = null;
}

function open(svg, caption, trigger) {
  box = box || build();
  const stage = box.querySelector(".lb-stage");
  const copy = svg.cloneNode(true);
  isolateIds(copy);
  copy.removeAttribute("role");
  copy.setAttribute("aria-hidden", "true");
  stage.replaceChildren(copy);
  fit(copy);
  box.querySelector(".lb-cap").textContent = caption;
  box.hidden = false;
  requestAnimationFrame(() => box.classList.add(OPEN_CLASS));
  prevOverflow = document.documentElement.style.overflow;
  document.documentElement.style.overflow = "hidden";
  restoreFocus = trigger;
  box.querySelector(".lb-close").focus({ preventScroll: true });
}

function init() {
  const figures = document.querySelectorAll("figure");
  figures.forEach((figure) => {
    const svg = figure.querySelector(".diagram svg");
    const button = figure.querySelector(".fig-zoom");
    if (!svg || !button) return;
    button.hidden = false;
    button.addEventListener("click", () => {
      const caption = figure.querySelector("figcaption")?.textContent.trim() ?? "";
      open(svg, caption, button);
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });
  document.addEventListener("click", (event) => {
    if (!box || box.hidden) return;
    const target = event.target;
    if (target === box || target.closest(".lb-close")) close();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, { once: true });
} else {
  init();
}
