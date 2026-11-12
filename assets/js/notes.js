// 划词高亮 + 批注。锚点用「原文 + 前后各 32 字 + 段落下标」，文章改字后失配就静默跳过。
import {
  NOTES_KEY,
  ANCHOR_PAD,
  makeNote,
  readNotes,
  writeNotes,
  notesFor,
  countFor,
  setThreadText,
  removeNote,
  serializeStore,
} from "./notes-core.mjs";
import {
  pullNotes,
  pullSummary,
  queueNote,
  queueDelete,
  mergeNotes,
  uploadLocalOnly,
} from "./sync.js";

let openPop = null;

const BLOCKED = new Set(["PRE", "SCRIPT", "STYLE", "SVG", "FIGCAPTION"]);

// SVG 里的 tagName 是小写的（svg/text/g），统一转大写再查，否则 SVG 守卫永远不命中。
const tagOf = (node) => String(node.tagName || "").toUpperCase();

function blockedInside(element) {
  for (let node = element; node && node !== document.body; node = node.parentElement) {
    if (BLOCKED.has(tagOf(node))) return true;
  }
  return false;
}

const SKIP = new Set(["CODE", "PRE", "FIGCAPTION", "SCRIPT", "STYLE", "SVG"]);

function skipped(el) {
  for (let node = el; node; node = node.parentElement) {
    if (SKIP.has(tagOf(node))) return true;
  }
  return false;
}

function blockOf(node, body) {
  let el = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
  while (el && el.parentElement !== body) el = el.parentElement;
  return el && el.parentElement === body ? el : null;
}

function textBefore(block, node, offset) {
  const range = document.createRange();
  range.setStart(block, 0);
  range.setEnd(node, offset);
  return range.toString();
}

function textAfter(block, node, offset) {
  const range = document.createRange();
  range.setStart(node, offset);
  range.setEnd(block, block.childNodes.length);
  return range.toString();
}

export function anchorOf(range, body) {
  if (!range || range.collapsed) return null;
  const startBlock = blockOf(range.startContainer, body);
  const endBlock = blockOf(range.endContainer, body);
  if (!startBlock || startBlock !== endBlock) return null;
  if (skipped(range.startContainer)) return null;
  const q = range.toString();
  if (!q.trim()) return null;
  const before = textBefore(startBlock, range.startContainer, range.startOffset);
  const after = textAfter(startBlock, range.endContainer, range.endOffset);
  return {
    q,
    pre: before.slice(-ANCHOR_PAD),
    post: after.slice(0, ANCHOR_PAD),
    block: Array.prototype.indexOf.call(body.children, startBlock),
  };
}

export function locate(anchor, body) {
  const block = body.children[anchor.block];
  if (!block) return [];
  const text = block.textContent;
  const hits = [];
  let at = text.indexOf(anchor.q);
  while (at >= 0) {
    const preOk = !anchor.pre || text.slice(Math.max(0, at - anchor.pre.length), at).endsWith(anchor.pre);
    const after = at + anchor.q.length;
    const postOk = !anchor.post || text.slice(after, after + anchor.post.length).startsWith(anchor.post);
    if (preOk && postOk) hits.push({ node: block, start: at, end: after });
    at = text.indexOf(anchor.q, at + 1);
  }
  if (hits.length) return hits;
  // 文章被改过：前后文对不上时，只要这句在本段里只出现一次，仍然认它。
  const only = text.indexOf(anchor.q);
  if (only >= 0 && text.indexOf(anchor.q, only + 1) < 0) {
    return [{ node: block, start: only, end: only + anchor.q.length }];
  }
  return [];
}

function textNodesOf(block) {
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
  const out = [];
  let pos = 0;
  let node;
  while ((node = walker.nextNode())) {
    out.push({ node, start: pos, end: pos + node.length });
    pos += node.length;
  }
  return out;
}

function wrapRange(block, start, end, id) {
  const nodes = textNodesOf(block).filter((n) => n.end > start && n.start < end);
  let wrapped = 0;
  for (let i = nodes.length - 1; i >= 0; i -= 1) {
    const { node } = nodes[i];
    const from = Math.max(0, start - nodes[i].start);
    const to = Math.min(node.length, end - nodes[i].start);
    if (to <= from) continue;
    let target = node;
    if (to < target.length) target.splitText(to);
    if (from > 0) target = target.splitText(from);
    if (blockedInside(target.parentElement)) continue;
    const mark = document.createElement("mark");
    mark.className = "note-hl";
    mark.dataset.note = id;
    target.parentNode.insertBefore(mark, target);
    mark.appendChild(target);
    wrapped += 1;
  }
  return wrapped;
}

export function paint(body, anchor, id) {
  const hits = locate(anchor, body);
  let wrapped = 0;
  for (const hit of hits) wrapped += wrapRange(hit.node, hit.start, hit.end, id);
  return wrapped;
}

function safeStore() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function save(all) {
  const store = safeStore();
  if (!store) return all;
  if (!writeNotes(store, all)) console.warn("笔记没存进去：本地存储已满或被禁用");
  return all;
}

function currentSelection() {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  return sel.getRangeAt(0);
}

let serverCounts = {};

export function markHome() {
  const all = readNotes(safeStore());
  for (const li of document.querySelectorAll("li[data-progress-post]")) {
    const slug = li.dataset.progressPost;
    const count = countFor(all, slug) || serverCounts[slug] || 0;
    let mark = li.querySelector(".note-mark");
    if (!count) {
      mark?.remove();
      continue;
    }
    if (!mark) {
      const foot = li.querySelector(".post-foot");
      if (!foot) continue;
      mark = document.createElement("span");
      mark.className = "note-mark";
      foot.append(mark);
    }
    mark.textContent = `有笔记 ·${count}`;
  }
}

function initNotes() {
  const body = document.querySelector(".post-body[data-progress-post]");
  if (!body) {
    markHome();
    pullSummary()
      .then((data) => {
        serverCounts = data.notes || {};
        markHome();
      })
      .catch(() => {});
    window.addEventListener("pageshow", (event) => {
      if (event.persisted) markHome();
    });
    return;
  }
  if (!body) return;
  const slug = body.dataset.progressPost;

  let all = readNotes(safeStore());
  const paintAll = () => {
    for (const mark of body.querySelectorAll("mark.note-hl")) {
      const parent = mark.parentNode;
      mark.replaceWith(...mark.childNodes);
      parent.normalize();
    }
    for (const note of notesFor(all, slug)) {
      const wrapped = paint(body, note, note.id);
      if (!wrapped) console.debug("笔记锚点失配，跳过", note.id);
    }
  };
  paintAll();

  // 本地先行，连上服务端后再合并（服务端更新的取服务端，本地独有的推上去）。
  pullNotes(slug)
    .then(({ notes, deleted }) => {
      const local = notesFor(all, slug);
      const { merged, adopted } = mergeNotes(local, notes, deleted);
      all = save({ ...all, [slug]: merged });
      uploadLocalOnly(slug, local, adopted);
      paintAll();
      renderList();
    })
    .catch(() => {});

  let bar = null;
  const hideBar = () => {
    bar?.remove();
    bar = null;
  };

  let pop = null;
  const closePop = () => {
    pop?.remove();
    pop = null;
  };

  const fmtTime = (t) =>
    new Date(t).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });

  function openPopById(id) {
    const mark = body.querySelector(`mark.note-hl[data-note="${id}"]`);
    const note = notesFor(all, slug).find((n) => n.id === id);
    if (!mark || !note) return;
    closePop();

    pop = document.createElement("div");
    pop.className = "nt-pop";

    const head = document.createElement("div");
    head.className = "nt-head";
    const author = document.createElement("span");
    author.className = "nt-author";
    author.textContent = note.thread[0]?.author || "我";
    const when = document.createElement("span");
    when.className = "nt-time";
    when.textContent = fmtTime(note.t);
    head.append(author, when);

    const input = document.createElement("textarea");
    input.className = "nt-input";
    input.rows = 3;
    input.placeholder = "写点批注…";
    input.value = note.thread[0]?.text || "";

    const foot = document.createElement("div");
    foot.className = "nt-acts";
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.dataset.act = "save";
    saveBtn.textContent = "保存";
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.dataset.act = "delete";
    delBtn.textContent = "删除";
    foot.append(saveBtn, delBtn);

    pop.append(head, input);

    for (const msg of note.thread.slice(1)) {
      const row = document.createElement("div");
      row.className = "nt-msg";
      const who = document.createElement("span");
      who.className = "nt-author";
      who.textContent = msg.author;
      const bodyText = document.createElement("p");
      bodyText.textContent = msg.text;
      row.append(who, bodyText);
      pop.append(row);
    }

    pop.append(foot);
    document.body.append(pop);

    const rect = mark.getBoundingClientRect();
    pop.style.top = `${Math.min(window.innerHeight - pop.offsetHeight - 12, rect.bottom + 8)}px`;
    pop.style.left = `${Math.min(window.innerWidth - pop.offsetWidth - 12, Math.max(12, rect.left))}px`;
    input.focus();

    saveBtn.addEventListener("click", () => {
      all = save(setThreadText(all, slug, id, input.value, Date.now()));
      queueNote(slug, notesFor(all, slug).find((n) => n.id === id));
      renderList();
      closePop();
    });
    delBtn.addEventListener("click", () => {
      all = save(removeNote(all, slug, id));
      queueDelete(slug, id);
      body.querySelectorAll(`mark.note-hl[data-note="${id}"]`).forEach((el) => {
        const parent = el.parentNode;
        el.replaceWith(...el.childNodes);
        parent.normalize();
      });
      renderList();
      closePop();
    });
  }
  openPop = openPopById;

  const addNote = (anchor, text, openBubble) => {
    const note = makeNote({ ...anchor, t: Date.now(), text, author: "我" });
    all = save({ ...all, [slug]: [...notesFor(all, slug), note] });
    queueNote(slug, note);
    paint(body, note, note.id);
    renderList();
    if (openBubble) openBubble(note);
    return note;
  };

  const showBar = (range) => {
    hideBar();
    const rect = range.getBoundingClientRect();
    const anchor = anchorOf(range, body);
    if (!anchor) return;

    bar = document.createElement("div");
    bar.className = "nt-actions";
    const make = (action, label) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.action = action;
      button.textContent = label;
      button.addEventListener("mousedown", (event) => event.preventDefault());
      return button;
    };

    const highlight = make("highlight", "高亮");
    highlight.addEventListener("click", () => {
      addNote(anchor, "");
      hideBar();
      window.getSelection()?.removeAllRanges();
    });

    const annotate = make("annotate", "批注");
    annotate.addEventListener("click", () => {
      const note = addNote(anchor, "");
      hideBar();
      window.getSelection()?.removeAllRanges();
      if (typeof openPop === "function") openPop(note.id);
    });

    bar.append(highlight, annotate);
    document.body.append(bar);
    const height = bar.offsetHeight;
    const above = rect.top - height - 8;
    bar.style.top = `${above >= 8 ? above : rect.bottom + 8}px`;
    bar.style.left = `${Math.min(window.innerWidth - 12, Math.max(12, rect.left + rect.width / 2))}px`;
  };

  let timer = null;
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const range = currentSelection();
      if (!range || !anchorOf(range, body)) {
        hideBar();
        return;
      }
      showBar(range);
    }, 120);
  };

  document.addEventListener("selectionchange", schedule);
  document.addEventListener("mouseup", schedule);
  // 两个挂载点：正文末尾（窄屏）和右栏（≥1440px）；两边都渲染，由 CSS 决定露哪个。
  const listRoots = [...document.querySelectorAll(".note-list")];

  const buildList = (root, notes) => {

    const head = document.createElement("div");
    head.className = "note-head";
    head.textContent = `本文笔记 ·${notes.length}`;

    const items = document.createElement("ol");
    items.className = "note-items";
    for (const note of notes) {
      const li = document.createElement("li");
      const link = document.createElement("a");
      link.href = "#";
      const snippet = note.q.length > 28 ? `${note.q.slice(0, 28)}…` : note.q;
      const text = (note.thread[0]?.text || "").split("\n")[0].slice(0, 24);
      link.textContent = text ? `${snippet} — ${text}` : snippet;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        const mark = body.querySelector(`mark.note-hl[data-note="${note.id}"]`);
        if (!mark) return;
        mark.scrollIntoView({ block: "center", behavior: "instant" });
        mark.setAttribute("tabindex", "-1");
        mark.focus({ preventScroll: true });
        mark.classList.add("is-flash");
        setTimeout(() => mark.classList.remove("is-flash"), 1200);
      });
      li.append(link);
      items.append(li);
    }

    const acts = document.createElement("div");
    acts.className = "note-acts";
    const exportBtn = document.createElement("button");
    exportBtn.type = "button";
    exportBtn.dataset.act = "export";
    exportBtn.textContent = "导出";
    exportBtn.addEventListener("click", () => {
      const blob = new Blob([serializeStore({ [slug]: notes })], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `notes-${slug}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.dataset.act = "clear";
    clearBtn.textContent = "清空";
    clearBtn.addEventListener("click", () => {
      if (!window.confirm(`清空本文的 ${notes.length} 条笔记？`)) return;
      for (const note of notes) {
        body.querySelectorAll(`mark.note-hl[data-note="${note.id}"]`).forEach((el) => {
          const parent = el.parentNode;
          el.replaceWith(...el.childNodes);
          parent.normalize();
        });
      }
      all = save({ ...all, [slug]: [] });
      for (const note of notes) queueDelete(slug, note.id);
      renderList();
    });
    acts.append(exportBtn, clearBtn);

    root.append(head, items, acts);
  };

  const renderList = () => {
    const notes = [...notesFor(all, slug)].sort((a, b) => a.t - b.t);
    for (const root of listRoots) {
      root.replaceChildren();
      root.dataset.empty = notes.length ? "0" : "1";
      if (notes.length) buildList(root, notes);
    }
  };

  body.addEventListener("click", (event) => {
    const mark = event.target.closest?.("mark.note-hl");
    if (mark) openPopById(mark.dataset.note);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closePop();
  });

  document.addEventListener("mousedown", (event) => {
    if (!pop || pop.contains(event.target)) return;
    if (event.target.closest?.("mark.note-hl")) return;
    closePop();
  });

  renderList();

  window.addEventListener("scroll", hideBar, { passive: true });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initNotes);
} else {
  initNotes();
}
