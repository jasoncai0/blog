// 登录态：token 存 localStorage，请求带 Authorization: Bearer。
// 未登录时整站照常读，收藏/进度只留在本机。

import { flush } from "./sync.js";

export const AUTH_KEY = "jasoncai:auth:v1";

const read = () => {
  try {
    const raw = JSON.parse(localStorage.getItem(AUTH_KEY) || "null");
    return raw && raw.token ? raw : null;
  } catch {
    return null;
  }
};

export const token = () => read()?.token || "";

export const current = () => {
  const me = read();
  return me ? { id: me.id, username: me.username, role: me.role } : null;
};

export const isAdmin = () => current()?.role === "admin";

export async function login(username, password) {
  const res = await fetch(`${window.__NOTES_API__ || "/api"}/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(res.status === 401 ? "用户名或密码不对" : body.error || "登录失败");
  }
  const data = await res.json();
  localStorage.setItem(
    AUTH_KEY,
    JSON.stringify({ token: data.token, id: data.user.id, username: data.user.username, role: data.user.role }),
  );
  location.reload();
}

export async function logout() {
  const me = read();
  if (me) {
    try {
      await flush();
    } catch {
      alert("还有没同步完的内容，下次登录会补发。");
    }
    for (const key of ["jasoncai:read:v2", "jasoncai:fav:v2", "jasoncai:notes:v2"]) {
      localStorage.removeItem(`${key}:u${me.id}`);
    }
    localStorage.removeItem(`jasoncai:notes:pushed:u${me.id}`);
    try {
      await fetch(`${window.__NOTES_API__ || "/api"}/logout`, {
        method: "POST",
        headers: { Authorization: `Bearer ${me.token}` },
      });
    } catch {
      /* 断网也照样退出本地 */
    }
    localStorage.removeItem(AUTH_KEY);
  }
  location.reload();
}

function dialog() {
  let el = document.querySelector(".auth-dialog");
  if (el) return el;
  el = document.createElement("div");
  el.className = "auth-dialog";
  el.setAttribute("role", "dialog");
  el.innerHTML = `
    <form class="auth-form">
      <h2 class="auth-title">登录</h2>
      <label class="auth-field"><span>用户名</span><input name="username" autocomplete="username" required></label>
      <label class="auth-field"><span>密码</span><input name="password" type="password" autocomplete="current-password" required></label>
      <p class="auth-error" hidden></p>
      <div class="auth-actions">
        <button type="button" class="auth-cancel">取消</button>
        <button type="submit">登录</button>
      </div>
    </form>`;
  document.body.append(el);
  el.querySelector(".auth-cancel").addEventListener("click", () => el.remove());
  el.addEventListener("mousedown", (event) => {
    if (event.target === el) el.remove();
  });
  el.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.target);
    const err = el.querySelector(".auth-error");
    err.hidden = true;
    try {
      await login(String(data.get("username") || ""), String(data.get("password") || ""));
    } catch (exc) {
      err.textContent = exc.message || "登录失败";
      err.hidden = false;
    }
  });
  return el;
}

export function mountAccountBar() {
  const bar = document.querySelector(".account-bar");
  if (!bar) return;
  const button = bar.querySelector(".account-btn");
  const me = current();
  if (!me) {
    bar.innerHTML = '<button type="button" class="account-btn">登录</button>';
    bar.querySelector(".account-btn").addEventListener("click", () => {
      const el = dialog();
      el.querySelector("input[name=username]").focus();
    });
    return;
  }
  bar.innerHTML = `<span class="account-name">${me.username}</span><button type="button" class="account-logout">退出</button>`;
  bar.querySelector(".account-logout").addEventListener("click", () => logout());
  void button;
}

mountAccountBar();
