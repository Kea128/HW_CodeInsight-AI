const LOCAL_API = "http://127.0.0.1:8001";
const apiBase = LOCAL_API;
localStorage.setItem("codeinsight-api-base", LOCAL_API);
const terminalStates = new Set(["completed", "failed", "cancelled"]);
let tasksLoading = false;
let remoteProjectsLoading = false;
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

async function waitForEngine() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      await api("/");
      setEngineStatus("本地引擎已就绪", "ready");
      await loadTasks();
      await loadRemoteProjects();
      await loadModelSettings();
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  setEngineStatus("本地引擎启动失败", "failed");
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

async function loadWikiResult(task) {
  const panel = document.querySelector("#result-panel");
  const title = document.querySelector("#result-title");
  const message = document.querySelector("#result-message");
  const pagesContainer = document.querySelector("#result-pages");
  panel.hidden = false;
  title.textContent = `${task.owner}/${task.repo} 分析结果`;
  message.className = "message";
  message.textContent = "正在读取已生成页面…";
  pagesContainer.replaceChildren();
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const query = new URLSearchParams({
      owner: task.owner,
      repo: task.repo,
      repo_type: task.repo_type,
      language: task.language,
    });
    const cache = await api(`/api/wiki_cache?${query}`);
    if (!cache) throw new Error("分析缓存不存在，请重新运行分析");
    const generated = cache.generated_pages || {};
    const orderedPages = (cache.wiki_structure?.pages || [])
      .map((page) => generated[page.id] || page)
      .filter(Boolean);
    if (!orderedPages.length) throw new Error("分析完成但没有生成可显示的页面");
    for (const page of orderedPages) {
      const article = document.createElement("article");
      article.className = "result-page";
      const heading = document.createElement("h3");
      heading.textContent = page.title;
      const content = document.createElement("pre");
      content.textContent = page.content;
      article.append(heading, content);
      pagesContainer.append(article);
    }
    message.textContent = `共 ${orderedPages.length} 页`;
  } catch (error) {
    message.className = "message error";
    message.textContent = errorMessage(error);
  }
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
  if (task.status === "completed") {
    const viewButton = document.createElement("button");
    viewButton.className = "secondary";
    viewButton.textContent = "查看结果";
    viewButton.addEventListener("click", () => loadWikiResult(task));
    actions.append(viewButton);
  } else if (!terminalStates.has(task.status)) {
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

function formatSyncTime(timestamp) {
  return timestamp ? new Date(timestamp).toLocaleString("zh-CN") : "尚未同步";
}

function remoteActionButton(label, action, className = "secondary") {
  const button = document.createElement("button");
  button.textContent = label;
  button.className = className;
  button.addEventListener("click", action);
  return button;
}

function renderRemoteProject(project) {
  const card = document.createElement("article");
  card.className = "remote-project";
  const detail = document.createElement("div");
  const title = document.createElement("h3");
  title.textContent = `${project.username}@${project.host}:${project.remote_path}`;
  const status = document.createElement("p");
  status.textContent = project.last_error
    ? `同步失败：${project.last_error}`
    : `最近同步：${formatSyncTime(project.last_sync_at)} · 每 ${project.poll_seconds} 秒`;
  status.className = project.last_error ? "remote-error" : "";
  const fingerprint = document.createElement("p");
  fingerprint.className = "fingerprint";
  fingerprint.textContent = project.host_fingerprint
    ? `服务器指纹：${project.host_fingerprint}`
    : "服务器指纹：等待首次连接";
  detail.append(title, status, fingerprint);

  const actions = document.createElement("div");
  actions.className = "task-actions";
  const syncButton = remoteActionButton("立即同步", async () => {
    syncButton.disabled = true;
    try {
      await api(`/remote/projects/${encodeURIComponent(project.id)}/sync`, {
        method: "POST",
        timeout: 600000,
      });
      await Promise.all([loadRemoteProjects(), loadTasks()]);
    } catch (error) {
      window.alert(errorMessage(error));
    } finally {
      syncButton.disabled = false;
    }
  });
  const deleteButton = remoteActionButton(
    "删除",
    async () => {
      if (!window.confirm("删除远程项目及本机同步副本？服务器源代码不会被修改。")) return;
      deleteButton.disabled = true;
      try {
        await api(`/remote/projects/${encodeURIComponent(project.id)}`, {
          method: "DELETE",
        });
        await Promise.all([loadRemoteProjects(), loadTasks()]);
      } catch (error) {
        window.alert(errorMessage(error));
        deleteButton.disabled = false;
      }
    },
    "danger",
  );
  actions.append(syncButton, deleteButton);
  card.append(detail, actions);
  return card;
}

async function loadRemoteProjects() {
  if (remoteProjectsLoading) return;
  remoteProjectsLoading = true;
  const list = document.querySelector("#remote-project-list");
  try {
    const projects = await api("/remote/projects");
    list.replaceChildren();
    projects.forEach((project) => list.append(renderRemoteProject(project)));
  } catch (error) {
    list.textContent = errorMessage(error);
  } finally {
    remoteProjectsLoading = false;
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
  modelConfigured = false;
  updateModelForm();
  const status = document.querySelector("#model-status");
  status.textContent = "待保存";
  status.className = "badge waiting";
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

document.querySelector("#remote-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = document.querySelector("#remote-message");
  const passwordInput = document.querySelector("#remote-password");
  const body = {
    host: document.querySelector("#remote-host").value.trim(),
    port: Number(document.querySelector("#remote-port").value),
    username: document.querySelector("#remote-username").value.trim(),
    password: passwordInput.value,
    remote_path: document.querySelector("#remote-path").value.trim(),
    poll_seconds: Number(document.querySelector("#remote-poll-seconds").value),
    provider: modelProvider,
    language: "zh",
  };
  try {
    message.className = "message";
    if (!modelConfigured) {
      throw new Error("请先在“AI 模型设置”中配置模型服务");
    }
    message.textContent = "正在连接 Ubuntu 并安全同步代码，首次同步可能需要几分钟…";
    await api("/remote/projects", {
      method: "POST",
      body: JSON.stringify(body),
      timeout: 600000,
    });
    passwordInput.value = "";
    message.textContent = "远程目录已连接，正在执行首次代码分析。";
    await Promise.all([loadRemoteProjects(), loadTasks()]);
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

document.querySelector("#refresh-button").addEventListener("click", loadTasks);
document.querySelector("#close-result-button").addEventListener("click", () => {
  document.querySelector("#result-panel").hidden = true;
});
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

updateModelForm();
waitForEngine();
setInterval(loadTasks, 3000);
setInterval(loadRemoteProjects, 5000);
