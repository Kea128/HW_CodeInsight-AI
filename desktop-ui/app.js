const LOCAL_API = "http://127.0.0.1:8001";
const apiBase = LOCAL_API;
localStorage.setItem("codeinsight-api-base", LOCAL_API);
const terminalStates = new Set(["completed", "failed", "cancelled"]);
let tasksLoading = false;
let remoteProjectsLoading = false;
let modelConfigured = false;
let modelProvider = localStorage.getItem("codeinsight-model-provider") || "openai";
let ollamaRestarting = false;
let ollamaStatusLoading = false;
let latestTasks = [];
let taskFilter = "all";
let desktopToken = null;
let restartDeferred = false;
let confirmedRemoteFingerprint = null;

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

function openDrawer(id) {
  document.querySelectorAll(".drawer.open").forEach((drawer) => {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  });
  const drawer = document.querySelector(`#${id}`);
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  document.querySelector("#drawer-backdrop").hidden = false;
}

function closeDrawers() {
  document.querySelectorAll(".drawer.open").forEach((drawer) => {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  });
  document.querySelector("#drawer-backdrop").hidden = true;
}

function updateSetupBanner() {
  document.querySelector("#setup-banner").hidden = modelConfigured;
  document.querySelector("#remote-ai-notice").hidden = modelConfigured;
}

function remoteFormDirty() {
  return ["remote-host", "remote-username", "remote-password", "remote-path"]
    .some((id) => document.querySelector(`#${id}`).value.trim());
}

async function api(path, options = {}) {
  const { headers, timeout = 15000, ...requestOptions } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const invoke = window.__TAURI__?.core?.invoke;
    if (!desktopToken && invoke) {
      desktopToken = await invoke("desktop_session_token");
    }
    const response = await fetch(`${apiBase}${path}`, {
      ...requestOptions,
      headers: {
        "Content-Type": "application/json",
        ...(desktopToken ? { "X-CodeInsight-Token": desktopToken } : {}),
        ...(headers || {}),
      },
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
      await loadOllamaStatus();
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

function renderMarkdown(markdown) {
  const container = document.createElement("div");
  container.className = "markdown-body";
  let code = null;
  for (const rawLine of String(markdown || "").split(/\r?\n/)) {
    if (rawLine.trim().startsWith("```")) {
      if (code) {
        container.append(code);
        code = null;
      } else {
        code = document.createElement("pre");
      }
      continue;
    }
    if (code) {
      code.textContent += `${rawLine}\n`;
      continue;
    }
    const heading = rawLine.match(/^(#{1,4})\s+(.+)$/);
    const element = document.createElement(heading ? `h${heading[1].length + 2}` : "p");
    element.textContent = heading ? heading[2] : rawLine.replace(/^[-*]\s+/, "• ");
    if (!element.textContent.trim()) element.className = "markdown-spacer";
    container.append(element);
  }
  if (code) container.append(code);
  return container;
}

async function loadWikiResult(task) {
  const title = document.querySelector("#result-title");
  const message = document.querySelector("#result-message");
  const pagesContainer = document.querySelector("#result-pages");
  openDrawer("result-panel");
  title.textContent = `${task.owner}/${task.repo} 分析结果`;
  message.className = "message";
  message.textContent = "正在读取已生成页面…";
  pagesContainer.replaceChildren();
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
      const content = renderMarkdown(page.content);
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

function renderTaskList() {
  const list = document.querySelector("#task-list");
  const tasks = latestTasks.filter((task) => {
    if (taskFilter === "active") return !terminalStates.has(task.status);
    if (taskFilter === "completed") return task.status === "completed";
    return true;
  });
  list.replaceChildren();
  if (!tasks.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = taskFilter === "all" ? "尚无分析任务。" : "当前筛选下没有任务。";
    list.append(empty);
    return;
  }
  tasks.forEach((task) => list.append(renderTask(task)));
}

async function loadTasks() {
  if (tasksLoading) return;
  tasksLoading = true;
  try {
    latestTasks = await api("/wiki/tasks");
    renderTaskList();
  } catch (error) {
    document.querySelector("#task-list").textContent = errorMessage(error);
  } finally {
    tasksLoading = false;
  }
}

function formatSyncTime(timestamp) {
  if (!timestamp) return "尚未同步";
  const milliseconds = timestamp < 10_000_000_000 ? timestamp * 1000 : timestamp;
  return new Date(milliseconds).toLocaleString("zh-CN");
}

function remoteActionButton(label, action, className = "secondary") {
  const button = document.createElement("button");
  button.textContent = label;
  button.className = className;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await action();
    } catch (error) {
      window.alert(errorMessage(error));
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

function renderRemoteProject(project) {
  const stageLabels = {
    saved: "已保存",
    connecting: "正在连接",
    syncing: "正在同步",
    ready_for_analysis: "可分析",
    analyzing: "正在分析",
    failed: "操作失败",
  };
  const card = document.createElement("article");
  card.className = "remote-project";
  const detail = document.createElement("div");
  const title = document.createElement("h3");
  title.textContent = `${project.username}@${project.host}:${project.remote_path}`;
  const status = document.createElement("p");
  status.textContent = project.last_error
    ? `${stageLabels[project.stage] || project.stage}：${project.last_error}`
    : `${stageLabels[project.stage] || "已保存"} · 最近同步：${formatSyncTime(project.last_sync_at)} · 每 ${project.poll_seconds} 秒`;
  status.className = project.last_error ? "remote-error" : "";
  const fingerprint = document.createElement("p");
  fingerprint.className = "fingerprint";
  fingerprint.textContent = project.host_fingerprint
    ? `服务器指纹：${project.host_fingerprint}`
    : "服务器指纹：等待首次连接";
  const syncStats = document.createElement("p");
  syncStats.className = "fingerprint";
  syncStats.textContent = `已扫描 ${project.files_seen || 0} 个文件 · 排除 ${project.files_excluded || 0} · 超大 ${project.files_oversize || 0} · 跳过链接 ${project.symlinks_skipped || 0}`;
  detail.append(title, status, fingerprint, syncStats);

  const actions = document.createElement("div");
  actions.className = "task-actions";
  const terminalButton = remoteActionButton("打开终端", () => {
    window.dispatchEvent(
      new CustomEvent("codeinsight:open-terminal", { detail: project }),
    );
  });
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
  const operationButton = project.stage === "failed"
    ? remoteActionButton("重试", async () => {
      await api(`/remote/projects/${encodeURIComponent(project.id)}/retry`, { method: "POST" });
      await loadRemoteProjects();
    })
    : project.stage === "ready_for_analysis"
      ? remoteActionButton("开始 AI 分析", async () => {
        if (!modelConfigured) {
          openDrawer("settings-drawer");
          return;
        }
        await api(`/remote/projects/${encodeURIComponent(project.id)}/analyze`, { method: "POST" });
        await loadRemoteProjects();
      })
      : ["connecting", "syncing", "analyzing"].includes(project.stage)
        ? remoteActionButton("取消", async () => {
          await api(`/remote/projects/${encodeURIComponent(project.id)}/cancel`, { method: "POST" });
          await loadRemoteProjects();
        }, "danger")
        : null;
  const deleteButton = remoteActionButton(
    "删除",
    async () => {
      if (!window.confirm("删除远程项目及本机同步副本？服务器源代码不会被修改。")) return;
      deleteButton.disabled = true;
      try {
        await api(`/remote/projects/${encodeURIComponent(project.id)}`, {
          method: "DELETE",
        });
        window.dispatchEvent(
          new CustomEvent("codeinsight:close-project-terminals", { detail: project.id }),
        );
        await Promise.all([loadRemoteProjects(), loadTasks()]);
      } catch (error) {
        window.alert(errorMessage(error));
        deleteButton.disabled = false;
      }
    },
    "danger",
  );
  actions.append(terminalButton, syncButton);
  if (operationButton) actions.append(operationButton);
  actions.append(deleteButton);
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
    if (!projects.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "尚未连接 Ubuntu 项目。";
      list.append(empty);
    } else {
      projects.forEach((project) => list.append(renderRemoteProject(project)));
    }
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
    document.querySelector("#ollama-tier").value = settings.ollama_tier || "auto";
    localStorage.setItem("codeinsight-model-provider", modelProvider);
    updateModelForm();
    status.textContent = modelConfigured ? "AI 已就绪" : "AI 需要配置";
    status.className = `badge ${modelConfigured ? "ready" : "failed"}`;
    updateSetupBanner();
  } catch (error) {
    status.textContent = "读取失败";
    status.className = "badge failed";
    updateSetupBanner();
  }
}

async function loadOllamaStatus() {
  if (ollamaStatusLoading) return;
  ollamaStatusLoading = true;
  const badge = document.querySelector("#ollama-status");
  const button = document.querySelector("#install-ollama-button");
  const settingsButton = document.querySelector("#settings-install-ollama-button");
  const message = document.querySelector("#ollama-message");
  const progress = document.querySelector("#ollama-progress");
  const progressBar = progress.querySelector("span");
  try {
    const status = await api("/desktop/ollama/status");
    const installing = status.state === "installing";
    const failed = status.state === "error";
    badge.textContent = status.ready ? "已就绪" : installing ? "正在安装" : failed ? "安装失败" : "未安装";
    badge.className = `badge ${status.ready ? "ready" : failed ? "failed" : "waiting"}`;
    button.disabled = installing || status.ready;
    settingsButton.disabled = installing;
    settingsButton.textContent = status.ready ? "检查模型更新" : "安装或升级本地模型";
    button.textContent = status.ready
      ? "本地 AI 已安装"
      : failed
        ? "重试安装"
        : status.installed
          ? "安装所需模型"
          : "一键安装本地 AI";
    progress.hidden = !installing;
    progressBar.style.width = `${Math.max(0, Math.min(100, status.progress || 0))}%`;
    message.className = failed ? "message error" : "message";
    message.textContent = [status.step, status.message].filter(Boolean).join("：");
    const tier = status.tiers?.find((item) => item.id === status.resolved_tier);
    document.querySelector("#ollama-tier-hint").textContent = tier
      ? `检测到 ${status.memory_gb} GB 内存；当前将使用 ${tier.label} ${tier.model}，预计需 ${tier.disk_gb} GB 可用空间。${tier.description}`
      : "";
    if (document.activeElement !== document.querySelector("#ollama-tier")) {
      document.querySelector("#ollama-tier").value = status.selected_tier || "auto";
    }
    if (status.restart_required && !ollamaRestarting) {
      if (document.querySelector("#project-drawer").classList.contains("open") || remoteFormDirty()) {
        restartDeferred = true;
        message.textContent = "本地 AI 已准备完成；为避免丢失 Ubuntu 表单，已延迟重启。";
        document.querySelector("#deferred-restart-button").hidden = false;
        return;
      }
      ollamaRestarting = true;
      message.textContent = "本地 AI 已准备完成，正在重启软件…";
      setTimeout(async () => {
        try {
          await window.__TAURI__.core.invoke("restart_app");
        } catch (error) {
          ollamaRestarting = false;
          message.className = "message error";
          message.textContent = `自动重启失败：${errorMessage(error)}，请手动重启软件。`;
        }
      }, 800);
    }
  } catch (error) {
    badge.textContent = "检查失败";
    badge.className = "badge failed";
    button.disabled = false;
    settingsButton.disabled = false;
    message.className = "message error";
    message.textContent = errorMessage(error);
  } finally {
    ollamaStatusLoading = false;
  }
}

async function startOllamaInstall() {
  const button = document.querySelector("#install-ollama-button");
  const settingsButton = document.querySelector("#settings-install-ollama-button");
  const message = document.querySelector("#ollama-message");
  button.disabled = true;
  settingsButton.disabled = true;
  message.className = "message";
  message.textContent = "正在启动安装任务…";
  try {
    await api("/desktop/ollama/install", {
      method: "POST",
      body: JSON.stringify({ tier: document.querySelector("#ollama-tier").value }),
    });
    await loadOllamaStatus();
  } catch (error) {
    button.disabled = false;
    settingsButton.disabled = false;
    message.className = "message error";
    message.textContent = errorMessage(error);
  }
}

document.querySelector("#install-ollama-button").addEventListener("click", startOllamaInstall);
document
  .querySelector("#settings-install-ollama-button")
  .addEventListener("click", startOllamaInstall);

document.querySelector("#model-provider").addEventListener("change", (event) => {
  modelProvider = event.target.value;
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
    const fingerprintKey = `${body.host}:${body.port}`;
    if (confirmedRemoteFingerprint?.key !== fingerprintKey) {
      message.textContent = "正在读取服务器主机指纹（尚未发送用户名和密码）…";
      const probe = await api("/remote/fingerprint", {
        method: "POST",
        body: JSON.stringify({ host: body.host, port: body.port }),
      });
      const approved = window.confirm(
        `首次连接需要确认 Ubuntu 主机身份：\n\n${probe.algorithm}\n${probe.fingerprint}\n\n请与服务器管理员核对。确认信任并继续吗？`,
      );
      if (!approved) throw new Error("已取消：未确认服务器主机指纹");
      confirmedRemoteFingerprint = { key: fingerprintKey, value: probe.fingerprint };
    }
    body.host_fingerprint = confirmedRemoteFingerprint.value;
    body.analyze_now = modelConfigured;
    message.textContent = "配置已保存，正在后台连接并同步代码…";
    await api("/remote/projects", {
      method: "POST",
      body: JSON.stringify(body),
    });
    passwordInput.value = "";
    message.textContent = modelConfigured
      ? "远程项目已保存，正在后台同步；完成后自动分析。"
      : "远程项目已保存，正在后台同步；AI 就绪后可开始分析。";
    await Promise.all([loadRemoteProjects(), loadTasks()]);
    closeDrawers();
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
    closeDrawers();
  } catch (error) {
    message.className = "message error";
    message.textContent = errorMessage(error);
  }
});

document.querySelector("#refresh-button").addEventListener("click", loadTasks);
document.querySelector("#add-project-button").addEventListener("click", () => {
  openDrawer("project-drawer");
  selectSource(localStorage.getItem("codeinsight-source-tab") === "remote");
});
document.querySelector("#connect-ubuntu-button").addEventListener("click", () => {
  openDrawer("project-drawer");
  selectSource(true);
});
document.querySelector("#settings-button").addEventListener("click", () => {
  openDrawer("settings-drawer");
});
document.querySelector("#drawer-backdrop").addEventListener("click", closeDrawers);
document.querySelectorAll("[data-close-drawer]").forEach((button) => {
  button.addEventListener("click", closeDrawers);
});
document.querySelector("#close-result-button").addEventListener("click", closeDrawers);

function selectSource(remote) {
  document.querySelector("#source-local-tab").classList.toggle("active", !remote);
  document.querySelector("#source-remote-tab").classList.toggle("active", remote);
  document.querySelector("#source-local-panel").hidden = remote;
  document.querySelector("#source-remote-panel").hidden = !remote;
  localStorage.setItem("codeinsight-source-tab", remote ? "remote" : "local");
  if (remote) requestAnimationFrame(() => document.querySelector("#remote-host").focus());
}

document.querySelector("#source-local-tab").addEventListener("click", () => selectSource(false));
document.querySelector("#source-remote-tab").addEventListener("click", () => selectSource(true));
document.querySelector("#deferred-restart-button").addEventListener("click", async () => {
  if (!restartDeferred) return;
  await window.__TAURI__.core.invoke("restart_app");
});
document.querySelectorAll("#task-filters button").forEach((button) => {
  button.addEventListener("click", () => {
    taskFilter = button.dataset.filter;
    document.querySelectorAll("#task-filters button").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    renderTaskList();
  });
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDrawers();
});

let updateSample = { bytes: 0, time: performance.now() };

function formatBytes(value) {
  if (!value) return "0 MB";
  return `${(value / 1024 / 1024).toFixed(value > 100 * 1024 * 1024 ? 0 : 1)} MB`;
}

async function initializeUpdateProgress() {
  const panel = document.querySelector("#update-status-panel");
  const bar = document.querySelector("#update-progress-bar");
  const text = document.querySelector("#update-progress-text");
  const cancel = document.querySelector("#cancel-update-button");
  const listen = window.__TAURI__?.event?.listen;
  if (!listen) return;
  await listen("update-progress", (event) => {
    const progress = event.payload;
    if (progress.phase === "idle") {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    const now = performance.now();
    const elapsed = Math.max(1, now - updateSample.time);
    const speed = ((progress.downloaded - updateSample.bytes) / elapsed) * 1000;
    updateSample = { bytes: progress.downloaded, time: now };
    bar.style.width = `${progress.percent ?? (progress.phase === "checking" ? 8 : 20)}%`;
    const size = progress.total
      ? `${formatBytes(progress.downloaded)} / ${formatBytes(progress.total)}`
      : "";
    const rate = speed > 0 && progress.phase === "downloading"
      ? `${formatBytes(speed)}/s`
      : "";
    text.textContent = [progress.message, size, rate].filter(Boolean).join(" · ");
    cancel.hidden = !progress.canCancel;
  });
  cancel.addEventListener("click", async () => {
    cancel.disabled = true;
    text.textContent = "正在安全取消下载…";
    try {
      await window.__TAURI__.core.invoke("cancel_update");
    } finally {
      cancel.disabled = false;
    }
  });
}

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
    button.textContent = `正在更新 ${version}…`;
    const installedVersion = await invoke("install_update");
    if (!installedVersion) {
      window.alert("更新状态已变化，请重新检查。");
      return;
    }
    document.querySelector("#update-status-panel").hidden = false;
    document.querySelector("#update-progress-text").textContent =
      `版本 ${installedVersion} 已安装，正在自动重启…`;
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
initializeUpdateProgress();
waitForEngine();
setInterval(loadTasks, 3000);
setInterval(loadRemoteProjects, 5000);
setInterval(loadOllamaStatus, 2000);
