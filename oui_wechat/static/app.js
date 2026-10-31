"use strict";

const $ = (id) => document.getElementById(id);
let initialized = true;
let authenticated = false;
let currentView = "accounts";
let accounts = [];
let currentStatus = null;
let selectedAccount = null;
let pairingId = null;
let pairingTimer = null;
let polling = false;
let logs = [];
let confirmAction = null;
let toastTimer = null;
let role = "admin";
let signedInUser = null;
let userLogin = false;
let loginMode = "admin";
let authRetryUntil = 0;

function icon(name) {
  const img = document.createElement("img");
  img.src = `/static/icons/${name}.svg`;
  img.alt = "";
  return img;
}

function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function errorAt(id, error) {
  $(id).textContent = error ? String(error.message || error) : "";
  $(id).hidden = !error;
}

function toast(message) {
  $("toast").querySelector("span").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 3500);
}

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-OUI-Request": "1", ...options.headers },
  });
  const data = await response.json();
  if (response.status === 401) {
    authenticated = false;
    accounts = [];
    document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
    showAuth();
  }
  if (!response.ok) {
    const error = new Error(data.error || `请求失败 (${response.status})`);
    error.retryAfter = data.retry_after;
    throw error;
  }
  return data;
}

const post = (path, data = {}) => api(path, { method: "POST", body: JSON.stringify(data) });

async function withBusy(button, fn, errorId = "page-error") {
  if (button.disabled) return;
  button.disabled = true;
  button.classList.add("is-busy");
  button.setAttribute("aria-busy", "true");
  errorAt(errorId, null);
  try {
    return await fn();
  } catch (error) {
    errorAt(errorId, error);
    if (errorId === "auth-error" && error.retryAfter) {
      authRetryUntil = Date.now() + error.retryAfter * 1000;
    }
  } finally {
    button.disabled = false;
    button.classList.remove("is-busy");
    button.removeAttribute("aria-busy");
    if (button.id === "auth-submit") updateAuthButton();
  }
}

function updateAuthButton() {
  const remaining = Math.max(0, Math.ceil((authRetryUntil - Date.now()) / 1000));
  $("auth-submit").disabled = remaining > 0;
  $("auth-submit").textContent = remaining ? `${remaining} 秒后重试` : initialized ? "登录" : "创建管理密码";
}

function showAuth() {
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  $("toast").hidden = true;
  for (const id of ["bind-password", "current-password", "new-password", "new-password-confirm"]) $(id).value = "";
  $("app").hidden = true;
  $("auth-screen").hidden = false;
  const isUser = initialized && userLogin && loginMode === "user";
  $("auth-title").textContent = initialized ? isUser ? "登录 Open WebUI" : "登录管理" : "设置管理密码";
  $("auth-modes").hidden = !initialized || !userLogin;
  $("mode-user").setAttribute("aria-pressed", String(isUser));
  $("mode-admin").setAttribute("aria-pressed", String(!isUser));
  $("auth-email-field").hidden = !isUser;
  $("auth-email").required = isUser;
  $("auth-password-label").textContent = isUser ? "Open WebUI 密码" : "管理密码";
  $("auth-password").minLength = isUser ? 1 : 8;
  $("auth-password").maxLength = isUser ? 1024 : 256;
  updateAuthButton();
  $("auth-confirm-field").hidden = initialized;
  $("auth-confirm").required = !initialized;
  $("auth-password").autocomplete = initialized ? "current-password" : "new-password";
  $("auth-password").value = "";
  $("auth-password").type = "password";
  $("auth-confirm").value = "";
}

for (const mode of ["user", "admin"]) {
  $(`mode-${mode}`).addEventListener("click", () => {
    loginMode = mode;
    errorAt("auth-error", null);
    showAuth();
  });
}
setInterval(() => {
  if (!$("auth-screen").hidden && !$("auth-submit").classList.contains("is-busy")) updateAuthButton();
}, 1000);

async function showApp(state) {
  role = state.role;
  signedInUser = state.user;
  document.querySelectorAll("[data-admin]").forEach((item) => { item.hidden = role !== "admin"; });
  $("panel-name").textContent = role === "admin" ? "管理面板" : "我的微信";
  $("nav-label").textContent = role === "admin" ? "管理" : "我的账号";
  $("signed-in-user").hidden = role === "admin";
  $("signed-in-user").textContent = signedInUser ? `${signedInUser.name} · ${signedInUser.email}` : "";
  currentView = "accounts";
  accounts = [];
  logs = [];
  $("log-entries").replaceChildren();
  $("accounts-list").replaceChildren();
  document.querySelectorAll(".view").forEach((view) => { view.hidden = view.id !== "view-accounts"; });
  document.querySelectorAll("[data-view]").forEach((nav) => nav.classList.toggle("active", nav.dataset.view === "accounts"));
  $("view-name").textContent = "微信账号";
  authenticated = true;
  initialized = true;
  $("auth-screen").hidden = true;
  $("app").hidden = false;
  errorAt("page-error", null);
  await refresh(true);
}

$("auth-form").addEventListener("submit", (event) => {
  event.preventDefault();
  withBusy(
    $("auth-submit"),
    async () => {
      const password = $("auth-password").value;
      if (!initialized && password !== $("auth-confirm").value) throw new Error("两次输入的密码不一致");
      const isUser = initialized && userLogin && loginMode === "user";
      const state = await post(
        initialized ? isUser ? "/auth/owui" : "/auth/login" : "/auth/setup",
        isUser ? { email: $("auth-email").value, password } : { password },
      );
      $("auth-password").value = "";
      $("auth-confirm").value = "";
      await showApp(state);
    },
    "auth-error",
  );
});

function badge(text, type = "") {
  const item = element("span", `status-badge ${type}`);
  const dot = element("i", `dot ${type === "" ? "green" : type === "waiting" ? "amber" : ""}`);
  item.append(dot, document.createTextNode(text));
  return item;
}

function actionButton(label, iconName, action, account, className = "") {
  const button = element("button", className);
  button.type = "button";
  button.append(icon(iconName), document.createTextNode(label));
  button.addEventListener("click", () => {
    document.querySelectorAll(".row-menu[open]").forEach((menu) => menu.removeAttribute("open"));
    runAccountAction(action, account, button);
  });
  return button;
}

function renderAccounts() {
  const list = $("accounts-list");
  list.replaceChildren();
  $("accounts-empty").hidden = accounts.length > 0;
  $("nav-count").textContent = accounts.length;
  $("account-total").textContent = `共 ${accounts.length} 个账号`;
  for (const account of accounts) {
    const row = element("article", "account-row");
    const user = element("div", "account-user");
    const avatar = element("span", "avatar", [...account.name.trim()][0]?.toUpperCase() || "W");
    const info = element("div", "user-info");
    info.append(element("div", "user-name", account.name));
    info.append(element("div", "user-detail", account.email || "未绑定 Open WebUI"));
    user.append(avatar, info);
    const session = element("div", "session-cell");
    session.append(
      element("div", "session-title", account.chat_title || (account.bound ? "新会话" : "等待绑定")),
      element("div", "session-subtitle", account.bound ? "Open WebUI 会话" : "微信已授权"),
    );
    row.append(user, session);
    if (!account.bound) {
      row.append(actionButton("绑定账号", "link-2", "bind", account, "button secondary small"));
    } else {
      row.append(
        badge(
          account.expired ? "登录已过期" : account.connected ? "已连接" : "已暂停",
          account.expired ? "waiting" : account.connected ? "" : "stopped",
        ),
      );
    }
    const menu = element("details", "row-menu");
    const summary = element("summary", "icon-button");
    summary.title = "账号操作";
    summary.setAttribute("aria-label", `${account.name}的账号操作`);
    summary.append(icon("ellipsis"));
    const popover = element("div", "menu-popover");
    popover.append(actionButton(account.bound ? "更新登录" : "绑定账号", "key-round", "bind", account));
    popover.append(actionButton("修改名称", "pencil", "rename", account));
    if (account.bound) {
      popover.append(
        actionButton("模型与会话", "settings-2", "model", account),
        actionButton("检查登录状态", "shield-check", "verify", account),
        actionButton("解除绑定", "unlink", "unbind", account),
      );
    }
    popover.append(actionButton("移除微信", "trash-2", "remove", account, "danger-text"));
    menu.append(summary, popover);
    row.append(menu);
    list.append(row);
  }
}

function renderStatus(data) {
  currentStatus = data;
  $("version").textContent = `v${data.version}`;
  const connection = $("backend-status");
  connection.replaceChildren(
    element("i", `dot ${data.owui.connected ? "green" : "amber"}`),
    document.createTextNode(
      data.owui.connected ? `Open WebUI ${data.owui.version || "已连接"}` : "Open WebUI 未连接",
    ),
  );
  $("online-count").textContent = data.connected_accounts;
  $("bound-count").textContent = data.bound_accounts;
  $("service-indicator").textContent =
    data.error || (data.running ? "服务运行中" : data.enabled ? "等待添加账号" : "服务已暂停");
  const button = $("service-toggle");
  button.replaceChildren(
    icon(data.enabled ? "pause" : "play"),
    element("span", "", data.enabled ? "暂停服务" : "启动服务"),
  );
  if (data.error) errorAt("page-error", data.error);
}

async function refresh(force = false) {
  if (!authenticated || polling) return;
  polling = true;
  try {
    const status = await api(`/status${force ? "?refresh=1" : ""}`);
    renderStatus(status);
    if (currentView === "accounts" && (force || !document.querySelector("dialog[open], .row-menu[open]"))) {
      const data = await api("/accounts");
      const changed = JSON.stringify(accounts) !== JSON.stringify(data.accounts);
      accounts = data.accounts;
      if (changed || force) renderAccounts();
    }
    if (currentView === "logs" && $("auto-logs").checked) await loadLogs();
  } catch (error) {
    if (authenticated) errorAt("page-error", error);
  } finally {
    polling = false;
  }
}

const displaySettings = [
  ["display.reasoning.enable", "思考状态提示"],
  ["display.reasoning.detailed", "思考正文"],
  ["display.typing", "微信正在输入"],
  ["display.citations", "引用来源"],
  ["display.tool_status.enable", "工具调用提示"],
  ["display.tool_status.detailed", "工具调用详情"],
];
const capabilitySettings = [
  ["capabilities.memory", "长期记忆"],
  ["capabilities.tools", "模型默认工具"],
  ["capabilities.web_search", "联网搜索"],
  ["capabilities.allow_terminal", "允许模型使用终端"],
  ["capabilities.image_generation", "允许 Open WebUI 图像生成"],
];

function renderSwitches(container, options, data) {
  $(container).replaceChildren();
  for (const [path, label] of options) {
    const row = element("label", "switch-row");
    const control = element("span", "switch");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.path = path;
    input.checked = Boolean(path.split(".").reduce((value, key) => value?.[key], data));
    input.setAttribute("aria-label", label);
    control.append(input, element("span", "switch-track"));
    row.append(element("span", "", label), control);
    if (path === "capabilities.allow_terminal") row.title = "允许模型使用已配置的远程终端执行命令";
    $(container).append(row);
  }
}

async function loadSettings() {
  const data = await api("/settings");
  $("setting-url").value = data.owui.base_url;
  $("setting-length").value = data.reply.max_length;
  $("setting-interval").value = data.reply.segment_interval;
  $("setting-media-ttl").value = data.media.pending_ttl_seconds;
  $("setting-media-count").value = data.media.max_images_per_turn;
  $("setting-media-single").value = data.media.max_image_bytes / (1024 * 1024);
  $("setting-media-batch").value = data.media.max_batch_bytes / (1024 * 1024);
  renderSwitches("display-switches", displaySettings, data);
  renderSwitches("capability-switches", capabilitySettings, data);
  renderSwitches("access-switches", [["access.user_login", "允许 Open WebUI 账号登录"]], data);
  renderSwitches("media-switches", [["media.enabled", "微信图片收发"]], data);
}

$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  withBusy(
    event.submitter,
    async () => {
      const data = {
        owui: { base_url: $("setting-url").value },
        reply: {
          max_length: Number($("setting-length").value),
          segment_interval: Number($("setting-interval").value),
        },
        media: {
          pending_ttl_seconds: Number($("setting-media-ttl").value),
          max_images_per_turn: Number($("setting-media-count").value),
          max_image_bytes: Number($("setting-media-single").value) * 1024 * 1024,
          max_batch_bytes: Number($("setting-media-batch").value) * 1024 * 1024,
        },
      };
      for (const input of document.querySelectorAll("[data-path]")) {
        const keys = input.dataset.path.split(".");
        let target = data;
        for (const key of keys.slice(0, -1)) target = target[key] ||= {};
        target[keys.at(-1)] = input.checked;
      }
      const saved = await api("/settings", { method: "PUT", body: JSON.stringify(data) });
      userLogin = saved.access.user_login;
      toast("设置已保存");
      await refresh(true);
    },
    "settings-error",
  );
});

$("password-form").addEventListener("submit", (event) => {
  event.preventDefault();
  withBusy(
    event.submitter,
    async () => {
      if ($("new-password").value !== $("new-password-confirm").value)
        throw new Error("两次输入的密码不一致");
      await post("/auth/password", {
        current: $("current-password").value,
        password: $("new-password").value,
      });
      event.target.reset();
      toast("管理密码已更新");
    },
    "password-error",
  );
});

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", async () => {
    currentView = button.dataset.view;
    document.querySelectorAll(".view").forEach((view) => {
      view.hidden = view.id !== `view-${currentView}`;
    });
    document.querySelectorAll("[data-view]").forEach((nav) => nav.classList.toggle("active", nav === button));
    $("view-name").textContent = button.querySelector("span").textContent;
    errorAt("page-error", null);
    try {
      if (currentView === "settings") await loadSettings();
      else if (currentView === "logs") await loadLogs();
      else await refresh(true);
    } catch (error) {
      errorAt("page-error", error);
    }
  });
});

async function cancelPairing() {
  clearTimeout(pairingTimer);
  const id = pairingId;
  pairingId = null;
  if (id) await api(`/pairings/${id}`, { method: "DELETE" }).catch(() => {});
}

async function openPairing() {
  await cancelPairing();
  if (!$("pair-dialog").open) $("pair-dialog").showModal();
  $("qr-image").hidden = true;
  $("qr-spinner").hidden = false;
  $("pair-code-form").hidden = true;
  $("pair-status").textContent = "正在获取二维码";
  errorAt("pair-error", null);
  try {
    const job = await post("/pairings");
    pairingId = job.id;
    pollPairing(job.id);
  } catch (error) {
    $("qr-spinner").hidden = true;
    $("pair-status").textContent = "获取失败";
    errorAt("pair-error", error);
  }
}

async function pollPairing(id) {
  if (pairingId !== id || !$("pair-dialog").open) return;
  try {
    const job = await api(`/pairings/${id}`);
    if (pairingId !== id) return;
    const messages = {
      creating: "正在获取二维码",
      wait: "等待扫码",
      scaned: "请在微信确认授权",
      need_verifycode: "等待数字配对码",
      expired: "二维码已过期，正在更新",
      error: "授权未完成",
      confirmed: "微信已授权",
      scaned_but_redirect: "正在连接微信",
      binded_redirect: "微信已绑定其他实例",
      cancelled: "已取消授权",
    };
    $("pair-status").textContent = messages[job.status] || "等待微信授权";
    $("pair-code-form").hidden = job.status !== "need_verifycode";
    if (job.image_url && $("qr-image").getAttribute("src") !== job.image_url) {
      $("qr-image").src = job.image_url;
      $("qr-image").hidden = false;
      $("qr-spinner").hidden = true;
    }
    if (job.status === "confirmed" && job.account_id) {
      pairingId = null;
      $("pair-dialog").close();
      const data = await api("/accounts");
      accounts = data.accounts;
      renderAccounts();
      const account = accounts.find((item) => item.id === job.account_id);
      toast("微信授权成功");
      if (account && !account.bound) openBind(account);
      await refresh(true);
      return;
    }
    if (job.error) errorAt("pair-error", job.error);
    if (job.status === "error" || job.status === "cancelled") return;
  } catch (error) {
    errorAt("pair-error", error);
  }
  pairingTimer = setTimeout(() => pollPairing(id), 1500);
}

$("pair-code-form").addEventListener("submit", (event) => {
  event.preventDefault();
  withBusy(
    event.submitter,
    async () => {
      if (!pairingId) throw new Error("二维码已失效");
      await post(`/pairings/${pairingId}/code`, { code: $("pair-code").value });
      $("pair-code").value = "";
      toast("配对码已提交");
    },
    "pair-error",
  );
});

function openBind(account) {
  selectedAccount = account;
  $("bind-context").textContent = account.name;
  $("bind-email").value = account.email || "";
  $("bind-password").value = "";
  $("bind-password").type = "password";
  errorAt("bind-error", null);
  $("bind-dialog").showModal();
}

$("bind-form").addEventListener("submit", (event) => {
  event.preventDefault();
  withBusy(
    event.submitter,
    async () => {
      try {
        await post(`/accounts/${encodeURIComponent(selectedAccount.id)}/bind`, {
          email: $("bind-email").value,
          password: $("bind-password").value,
        });
        $("bind-dialog").close();
        toast("Open WebUI 账号已绑定");
        await refresh(true);
      } finally {
        $("bind-password").value = "";
      }
    },
    "bind-error",
  );
});

async function openModels(account) {
  selectedAccount = account;
  $("model-context").textContent = account.name;
  errorAt("model-error", null);
  $("model-select").replaceChildren(element("option", "", "正在读取模型"));
  $("model-select").disabled = true;
  $("model-form").querySelector('[type="submit"]').disabled = true;
  $("model-dialog").showModal();
  try {
    const data = await api(`/accounts/${encodeURIComponent(account.id)}/models`);
    $("model-select").replaceChildren();
    for (const model of data.models) {
      const option = element("option", "", model.name);
      option.value = model.id;
      option.selected = model.id === account.model_id;
      $("model-select").append(option);
    }
    if (!data.models.length) throw new Error("该账号暂无可用模型");
    $("model-select").disabled = false;
    $("model-form").querySelector('[type="submit"]').disabled = false;
  } catch (error) {
    errorAt("model-error", error);
  }
}

$("model-form").addEventListener("submit", (event) => {
  event.preventDefault();
  withBusy(
    event.submitter,
    async () => {
      await post(`/accounts/${encodeURIComponent(selectedAccount.id)}/model`, {
        model_id: $("model-select").value,
      });
      $("model-dialog").close();
      toast("模型已切换");
      await refresh(true);
    },
    "model-error",
  );
});

$("new-chat").addEventListener("click", (event) =>
  withBusy(
    event.currentTarget,
    async () => {
      await post(`/accounts/${encodeURIComponent(selectedAccount.id)}/new-chat`);
      $("model-dialog").close();
      toast("已切换到新会话");
      await refresh(true);
    },
    "model-error",
  ),
);

$("rename-form").addEventListener("submit", (event) => {
  event.preventDefault();
  withBusy(
    event.submitter,
    async () => {
      await post(`/accounts/${encodeURIComponent(selectedAccount.id)}/rename`, {
        name: $("rename-input").value,
      });
      $("rename-dialog").close();
      toast("名称已更新");
      await refresh(true);
    },
    "rename-error",
  );
});

function runAccountAction(action, account, button) {
  if (action === "bind" && role !== "admin") {
    return withBusy(button, async () => {
      await post(`/accounts/${encodeURIComponent(account.id)}/bind`);
      toast("已绑定当前 Open WebUI 账号");
      await refresh(true);
    });
  }
  if (action === "bind") return openBind(account);
  if (action === "model") return openModels(account);
  if (action === "rename") {
    selectedAccount = account;
    $("rename-input").value = account.name;
    errorAt("rename-error", null);
    return $("rename-dialog").showModal();
  }
  if (action === "verify")
    return withBusy(button, async () => {
      const data = await post(`/accounts/${encodeURIComponent(account.id)}/verify`);
      if (!data.valid) throw new Error("账号身份校验失败，请更新登录");
      toast("Open WebUI 登录有效");
    });
  $("confirm-title").textContent = action === "remove" ? "移除微信" : "解除账号绑定";
  $("confirm-message").textContent =
    action === "remove"
      ? `移除“${account.name}”的微信授权与本地账号绑定？网页聊天记录保留。`
      : `解除“${account.name}”的 Open WebUI 绑定？微信授权与网页聊天记录保留。`;
  $("confirm-submit").textContent = action === "remove" ? "移除微信" : "解除绑定";
  errorAt("confirm-error", null);
  confirmAction = async () => {
    await post(`/accounts/${encodeURIComponent(account.id)}/${action}`);
    $("confirm-dialog").close();
    toast(action === "remove" ? "微信连接已移除" : "账号已解除绑定");
    await refresh(true);
  };
  $("confirm-dialog").showModal();
}

$("confirm-submit").addEventListener("click", (event) =>
  withBusy(event.currentTarget, () => confirmAction?.(), "confirm-error"),
);

async function loadLogs() {
  const data = await api("/logs");
  logs = data.entries;
  renderLogs();
}

function renderLogs() {
  const filter = $("log-level").value;
  const visible = logs.filter(
    (item) =>
      filter === "all" ||
      (filter === "ERROR"
        ? ["ERROR", "CRITICAL"].includes(item.level)
        : ["WARNING", "ERROR", "CRITICAL"].includes(item.level)),
  );
  const container = $("log-entries");
  const nearBottom = container.scrollTop + container.clientHeight >= container.scrollHeight - 35;
  container.replaceChildren();
  for (const item of visible) {
    const line = element("div", "log-line");
    const at = new Date(item.at);
    line.append(
      element("span", "log-time", at.toLocaleTimeString("zh-CN", { hour12: false })),
      element("span", `log-level ${item.level.toLowerCase()}`, item.level),
      element("span", "log-message", `[${item.source}] ${item.message}`),
    );
    container.append(line);
  }
  $("log-count").textContent = `${visible.length} 条`;
  $("logs-empty").hidden = visible.length > 0;
  container.hidden = visible.length === 0;
  if (nearBottom) container.scrollTop = container.scrollHeight;
}

$("download-logs").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(logs, null, 2)], { type: "application/json" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "oui-wechat-logs.json";
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

$("log-level").addEventListener("change", renderLogs);
$("refresh-logs").addEventListener("click", (event) => withBusy(event.currentTarget, loadLogs));
$("refresh-status").addEventListener("click", (event) => withBusy(event.currentTarget, () => refresh(true)));
$("service-toggle").addEventListener("click", (event) =>
  withBusy(event.currentTarget, async () => {
    await post(`/service/${currentStatus?.enabled ? "stop" : "start"}`);
    await refresh(true);
  }),
);
$("add-account").addEventListener("click", openPairing);
$("add-first-account").addEventListener("click", openPairing);
$("refresh-qr").addEventListener("click", openPairing);
$("logout").addEventListener("click", (event) =>
  withBusy(event.currentTarget, async () => {
    await cancelPairing();
    await post("/auth/logout");
    authenticated = false;
    accounts = [];
    const state = await api("/auth/state");
    userLogin = state.user_login;
    loginMode = userLogin ? "user" : "admin";
    showAuth();
  }),
);

document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", () => $(button.dataset.close).close());
});
$("pair-dialog").addEventListener("close", cancelPairing);
$("bind-dialog").addEventListener("close", () => {
  $("bind-password").value = "";
});
document.querySelectorAll("[data-password]").forEach((button) => {
  button.addEventListener("click", () => {
    const input = $(button.dataset.password);
    input.type = input.type === "password" ? "text" : "password";
    const text = input.type === "password" ? "显示密码" : "隐藏密码";
    button.title = text;
    button.setAttribute("aria-label", text);
    button.replaceChildren(icon(input.type === "password" ? "eye" : "eye-off"));
  });
});
document.addEventListener("click", (event) => {
  document.querySelectorAll(".row-menu[open]").forEach((menu) => {
    if (!menu.contains(event.target)) menu.removeAttribute("open");
  });
});

(async () => {
  try {
    const state = await api("/auth/state");
    initialized = state.initialized;
    userLogin = state.user_login;
    loginMode = userLogin ? "user" : "admin";
    if (state.authenticated) await showApp(state);
    else showAuth();
  } catch (error) {
    showAuth();
    errorAt("auth-error", error);
  }
  setInterval(() => {
    if (!document.hidden) refresh();
  }, 5000);
})();
