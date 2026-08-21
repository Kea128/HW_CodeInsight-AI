const LOCAL_API = "http://127.0.0.1:8001";
let apiBase = localStorage.getItem("codeinsight-api-base") || LOCAL_API;
const terminalStates = new Set(["completed", "failed", "cancelled"]);
let tasksLoading = false;
let modelConfigured = false;
let modelProvider = localStorage.getItem("codeinsight-model-provider") || "openai";

function errorMessage(error) {
  if (typeof error === "string" && error.trim()) return error;
  if (error?.message) return error.message;
  try {
    const serialized = JSON.stringify(error);
    if (serialized && serialized !== "{}") return serialized;
  } catch {
    // Ignore serialization errors and use the fallback below.
  }
  return "未知错误";
}

function setEngineStatus(text, kind) {
  const element = document.querySelector("#engine-status");
  element.textContent = text;
  element.className = `badge ${kind}`;
}

async function api(path, options = {}) {
  const { headers, timeout = 15000, ...requestOptions } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(`${apiBase}${path}`, {
      ...requestOptions,
      headers: { "Content-Type": "application/json", ...(headers || {}) },
      signal: controller.signal,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `请求失败 (${response.status})`);
    }
    if (response.status === 204) return null;
    return response.json();
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error(`连接分析引擎超时 (${timeout / 1000} 秒)`);
    }
    if (error instanceof TypeError) {
      throw new Error(`无法连接分析引擎 ${apiBase}，请恢复本机引擎后重试`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function updateConnectionUi() {
  const remote = apiBase !== LOCAL_API;
  document.querySelector("#engine-url").value = apiBase;
  const kind = document.querySelector("#connection-kind");
  kind.textContent = remote ? "Ubuntu（SSH 隧道）" : "本机";
  kind.className = "badge ready";
}

function normalizeLoopbackUrl(value) {
  const url = new URL(value);
  if (!["127.0.0.1", "localhost", "::1"].includes(url.hostname)) {
    throw new Error("为保护源码，远程引擎必须通过 SSH 隧道连接到本机地址");
  }
  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error("引擎地址仅支持 HTTP 或 HTTPS");
  }
  return url.origin;
}

async function waitForEngine() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      await api("/");
      setEngineStatus(
        apiBase === LOCAL_API ? "本地引擎已就绪" : "Ubuntu 引擎已连接",
        "ready",
      );
      await loadTasks();
      await loadModelSettings();
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  setEngineStatus(
    apiBase === LOCAL_API ? "本地引擎启动失败" : "SSH 隧道未连接",
    "failed",
  );
}

function addButton(container, label, action, taskId, className = "secondary") {
  const button = document.createElement("button");
  button.textContent = label;
  button.className = className;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(`/wiki/tasks/${encodeURIComponent(taskId)}/${action}`, {
        method: "POST",
      });
      await loadTasks();
    } catch (error) {
      window.alert(errorMessage(error));
    } finally {
      button.disabled = false;
    }
  });
  container.append(button);
}

function renderTask(task) {
  const card = document.createElement("article");
  card.className = "task";
  const detail = document.createElement("div");
  const title = document.createElement("h3");
  title.textContent = task.name || `${task.owner}/${task.repo}`;
  const status = document.createElement("p");
  status.textContent = `${task.status} · ${task.pages_done}/${task.pages_total || "?"} 页`;
  const progress = document.createElement("div");
  progress.className = "progress";
  const bar = document.createElement("span");
  const percent = task.pages_total ? (task.pages_done / task.pages_total) * 100 : 3;
  bar.style.width = `${Math.min(100, percent)}%`;
  progress.append(bar);
  detail.append(title, status, progress);
  if (task.error) {
    const error = document.createElement("p");
    error.className = "task-error";
    error.textContent = task.error;
    detail.append(error);
  }

  const actions = document.createElement("div");
  actions.className = "task-actions";
  if (!terminalStates.has(task.status)) {
    if (task.status === "paused") {
      addButton(actions, "继续", "resume", task.id);
    } else {
      addButton(actions, "暂停", "pause", task.id);
    }
    addButton(actions, "取消", "cancel", task.id, "danger");
  }
  card.append(detail, actions);
  return card;
}

async function loadTasks() {
  if (tasksLoading) return;
  tasksLoading = true;
  const list = document.querySelector("#task-list");
  try {
    const tasks = await api("/wiki/tasks");
    list.replaceChildren();
    if (!tasks.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "尚无分析任务。";
      list.append(empty);
      return;
    }
    tasks.forEach((task) => list.append(renderTask(task)));
  } catch (error) {
    list.textContent = errorMessage(error);
  } finally {
    tasksLoading = false;
  }
}

function updateModelForm() {
  const provider = document.querySelector("#model-provider");
  const keyLabel = document.querySelector("#api-key-label");
  provider.value = modelProvider;
  keyLabel.hidden = modelProvider === "ollama";
}

async function loadModelSettings() {
  const status = document.querySelector("#model-status");
  try {
    const settings = await api("/desktop/settings");
    modelProvider = settings.provider;
    modelConfigured = settings.configured;
    localStorage.setItem("codeinsight-model-provider", modelProvider);
    updateModelForm();
    status.textContent = modelConfigured ? "已配置" : "需要配置";
    status.className = `badge ${modelConfigured ? "ready" : "failed"}`;
  } catch (error) {
    status.textContent = "读取失败";
    status.className = "badge failed";
  }
}

document.querySelector("#model-provider").addEventListener("change", (event) => {
  modelProvider = event.target.value;
  updateModelForm();
});

document.querySelector("#model-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = document.querySelector("#model-message");
  const apiKey = document.querySelector("#model-api-key").value.trim();
  try {
    message.className = "message";
    message.textContent = "正在保存模型设置…";
    const settings = await api("/desktop/settings", {
      method: "POST",
      body: JSON.stringify({ provider: modelProvider, api_key: apiKey || null }),
    });
    if (!settings.configured) {
      throw new Error(`${modelProvider} 需要有效的 API Key`);
    }
    localStorage.setItem("codeinsight-model-provider", modelProvider);
    message.textContent = "设置已保存，正在重启应用并加载模型配置…";
    const invoke = window.__TAURI__?.core?.invoke;
    if (!invoke) throw new Error("桌面重启组件不可用，请手动重启软件");
    setTimeout(() => invoke("restart_app"), 300);
  } catch (error) {
    message.className = "message error";
    message.textContent = errorMessage(error);
  }
});

document.querySelector("#project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = document.querySelector("#form-message");
  const path = document.querySelector("#project-path").value.trim();
  const parts = path.split(/[\\/]/).filter(Boolean);
  const repo = parts.at(-1) || "project";
  const nightOnly = document.querySelector("#night-only").checked;
  const body = {
    task: {
      owner: "local",
      repo,
      type: "local",
      repo_url: path,
      language: "zh",
      comprehensive: true,
    },
    night_start: nightOnly ? document.querySelector("#night-start").value : null,
    night_end: nightOnly ? document.querySelector("#night-end").value : null,
    poll_seconds: Number(document.querySelector("#poll-seconds").value),
    analyze_now: true,
  };
  try {
    message.className = "message";
    if (/^[a-zA-Z]:[\\/]/.test(path) && apiBase !== LOCAL_API) {
      apiBase = LOCAL_API;
      localStorage.setItem("codeinsight-api-base", apiBase);
      updateConnectionUi();
    }
    await api("/health");
    if (!modelConfigured) {
      throw new Error("请先在“AI 模型设置”中配置模型服务");
    }
    body.task.provider = modelProvider;
    message.textContent = "正在建立文件快照…";
    await api("/continuous/projects", {
      method: "POST",
      body: JSON.stringify(body),
      timeout: 120000,
    });
    message.textContent = "项目已加入持续分析。";
    await loadTasks();
  } catch (error) {
    message.className = "message error";
    message.textContent = errorMessage(error);
  }
});

document.querySelector("#connection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = document.querySelector("#connection-message");
  try {
    const nextBase = normalizeLoopbackUrl(
      document.querySelector("#engine-url").value.trim(),
    );
    const previousBase = apiBase;
    apiBase = nextBase;
    try {
      await api("/health");
    } catch (error) {
      apiBase = previousBase;
      throw error;
    }
    localStorage.setItem("codeinsight-api-base", apiBase);
    updateConnectionUi();
    setEngineStatus("分析引擎已连接", "ready");
    message.className = "message";
    message.textContent = apiBase === LOCAL_API ? "已连接本机引擎。" : "已通过 SSH 隧道连接 Ubuntu 引擎。";
    await loadTasks();
  } catch (error) {
    message.className = "message error";
    message.textContent = errorMessage(error);
  }
});

document.querySelector("#use-local-button").addEventListener("click", async () => {
  apiBase = LOCAL_API;
  localStorage.setItem("codeinsight-api-base", apiBase);
  updateConnectionUi();
  await waitForEngine();
});

document.querySelector("#refresh-button").addEventListener("click", loadTasks);
document.querySelector("#update-button").addEventListener("click", async () => {
  const button = document.querySelector("#update-button");
  const idleLabel = button.textContent;
  button.disabled = true;
  button.textContent = "正在检查…";
  try {
    const invoke = window.__TAURI__?.core?.invoke;
    if (!invoke) throw new Error("更新组件不可用");
    const version = await invoke("check_update");
    if (!version) {
      window.alert("当前已是最新版本。");
      return;
    }
    if (!window.confirm(`发现新版本 ${version}，是否立即下载并安装？`)) {
      return;
    }
    button.textContent = `正在安装 ${version}…`;
    const installedVersion = await invoke("install_update");
    if (!installedVersion) {
      window.alert("更新状态已变化，请重新检查。");
      return;
    }
    window.alert(`版本 ${installedVersion} 已安装，请重新启动应用。`);
  } catch (error) {
    const detail = errorMessage(error);
    const openDownload = window.confirm(
      `自动更新失败：${detail}\n\n是否在浏览器中打开官方下载页面？`,
    );
    if (openDownload) {
      try {
        await window.__TAURI__.core.invoke("open_manual_update");
      } catch (openError) {
        window.alert(`打开下载页面失败：${errorMessage(openError)}`);
      }
    }
  } finally {
    button.disabled = false;
    button.textContent = idleLabel;
  }
});

updateConnectionUi();
updateModelForm();
waitForEngine();
setInterval(loadTasks, 3000);
