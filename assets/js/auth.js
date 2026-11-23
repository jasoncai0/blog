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

// 第一次登录这个账号时，把访客期（:guest）攒下的进度/笔记/收藏搬过来，
// 免得登录一瞬间本地数据看起来「没了」。服务端有的会随后覆盖。
const GUEST_KEYS = ["jasoncai:read:v2", "jasoncai:notes:v2", "jasoncai:fav:v2"];

function adoptGuestData(id) {
  for (const base of GUEST_KEYS) {
    try {
      const target = `${base}:u${id}`;
      if (localStorage.getItem(target)) continue;
      const guest = localStorage.getItem(`${base}:guest`);
      if (guest) localStorage.setItem(target, guest);
    } catch {
      /* 存不了就算了，不影响登录 */
    }
  }
}

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
  adoptGuestData(data.user.id);
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

export async function changePassword(oldPassword, newPassword) {
  const res = await fetch(`${window.__NOTES_API__ || "/api"}/password`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token()}` },
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
  if (res.status === 204) return;
  if (res.status === 401) throw new Error("登录已过期，请重新登录");
  const body = await res.json().catch(() => ({}));
  const messages = {
    "wrong password": "当前密码不对",
    "password too short": "新密码至少 8 位",
    "password unchanged": "新密码和旧密码一样",
  };
  throw new Error(messages[body.error] || body.error || "改密码失败");
}

function passwordDialog() {
  const el = document.createElement("div");
  el.className = "auth-dialog";
  el.setAttribute("role", "dialog");
  el.innerHTML = `
    <form class="auth-form">
      <h2 class="auth-title">改密码</h2>
      <label class="auth-field"><span>当前密码</span><input name="old_password" type="password" autocomplete="current-password" required></label>
      <label class="auth-field"><span>新密码</span><input name="new_password" type="password" autocomplete="new-password" minlength="8" required></label>
      <label class="auth-field"><span>再输一次</span><input name="confirm" type="password" autocomplete="new-password" minlength="8" required></label>
      <p class="auth-hint">改完其它设备会退出登录，这台保持在线。</p>
      <p class="auth-error" hidden></p>
      <div class="auth-actions">
        <button type="button" class="auth-cancel">取消</button>
        <button type="submit">保存</button>
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
    const next = String(data.get("new_password") || "");
    if (next !== String(data.get("confirm") || "")) {
      err.textContent = "两次输入的新密码不一致";
      err.hidden = false;
      return;
    }
    try {
      await changePassword(String(data.get("old_password") || ""), next);
      err.textContent = "已更新";
      err.classList.add("is-ok");
      err.hidden = false;
      setTimeout(() => el.remove(), 900);
    } catch (exc) {
      err.textContent = exc.message || "改密码失败";
      err.classList.remove("is-ok");
      err.hidden = false;
    }
  });
  el.querySelector("input[name=old_password]").focus();
  return el;
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
  bar.innerHTML = `<span class="account-name">${me.username}</span><button type="button" class="account-password">改密码</button><button type="button" class="account-logout">退出</button>`;
  bar.querySelector(".account-logout").addEventListener("click", () => logout());
  bar.querySelector(".account-password").addEventListener("click", () => passwordDialog());
  void button;
}

mountAccountBar();
