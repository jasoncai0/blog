// 本地状态按账号分键：未登录用 :guest，登录后用 :u<id>。
// 三个模块（进度、收藏、笔记）都经这里读写，换账号自然隔离。

import { current } from "./auth.js";

export function nsKey(base) {
  const user = current();
  return `${base}:${user ? `u${user.id}` : "guest"}`;
}

export const getItem = (base) => localStorage.getItem(nsKey(base));
export const setItem = (base, value) => localStorage.setItem(nsKey(base), value);
export const removeItem = (base) => localStorage.removeItem(nsKey(base));
