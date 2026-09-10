const LOCAL_API = "http://127.0.0.1:8001";
const apiBase = LOCAL_API;
localStorage.setItem("codeinsight-api-base", LOCAL_API);
const terminalStates = new Set(["completed", "failed", "cancelled"]);
let tasksLoading = false;
let remoteProjectsLoading = false;
let continuousProjectsLoading = false;
let modelConfigured = false;
let modelUsable = false;
let modelHint = "";
let modelProbeStatus = "untested";
let savedModelProvider = localStorage.getItem("codeinsight-model-provider") || "openai";
let modelProvider = savedModelProvider;
let savedModelId = localStorage.getItem("codeinsight-model-id") || "";
let latestRemoteProjects = [];
let scopeProjectId = "";
let remoteWatchTimer = null;
let latestKnowledgeSpaces = [];
let activeSpaceId = localStorage.getItem("codeinsight-active-space") || "";
let resultTab = "docs";
let ollamaRestarting = false;
let ollamaStatusLoading = false;
let ollamaReady = false;
let latestTasks = [];
let latestContinuousProjects = [];
let taskFilter = "all";
let desktopToken = null;
let restartDeferred = false;
let confirmedRemoteFingerprint = null;
let remoteFormEdited = false;
let engineState = "starting";
let engineProbeGeneration = 0;
let engineLastError = "";
let engineSidecarState = "unknown";
let engineLogPath = "";
let desktopAppVersion = "";
let engineVersion = "";
let refreshPromise = null;
let refreshQueued = false;
let drawerReturnFocus = null;
let drawerFocusGeneration = 0;

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function errorMessage(error) {
  if (typeof error === "string" && error.trim()) return formatApiDetail(error);
  if (error?.message) return formatApiDetail(error.message);
  try {
    const serialized = JSON.stringify(error);
    if (serialized && serialized !== "{}") return serialized;
  } catch {
    // Ignore serialization errors and use the fallback below.
  }
  return "未知错误";
}

function formatApiDetail(detail) {
  if (typeof detail !== "string" || !detail.trim()) return detail || "未知错误";
  try {
    const parsed = JSON.parse(detail);
    if (Array.isArray(parsed)) {
      const messages = parsed.map((item) => item.msg || item.message).filter(Boolean);
      if (messages.length) return messages.join("；");
    }
  } catch {
    /* keep the original engine message */
  }
  if (/openai['\"]?,\s*['\"]google['\"]?.*ollama/i.test(detail)) {
    return "连接 Ubuntu 不需要选择 AI 模型。请只填写服务器信息；自定义 API 在「设置」里配置，同步完成后点「开始分析」才会用到。";
  }
  return detail;
}

function formatAppVersion(version) {
  if (!version || version === "unknown" || version === "development") return "";
  return version.startsWith("v") ? version : `v${version}`;
}

function renderAppVersion() {
  const version = formatAppVersion(desktopAppVersion) || formatAppVersion(engineVersion);
  const header = document.querySelector("#app-version");
  const settings = document.querySelector("#settings-app-version");
  if (header) header.textContent = version || "版本未知";
  if (settings) settings.textContent = version ? `当前版本：${version}` : "当前版本：未知";
}

function setEngineStatus(text, kind) {
  const element = document.querySelector("#engine-status");
  element.textContent = text;
  element.className = `badge ${kind}`;
}

function setEngineState(state, detail = "") {
  engineState = state;
  engineLastError = detail || engineLastError;
  const panel = document.querySelector("#engine-recovery");
  const title = document.querySelector("#engine-recovery-title");
  const message = document.querySelector("#engine-recovery-message");
  const states = {
    starting: ["本地引擎启动中", "waiting", "分析引擎正在启动", detail || "首次启动可能需要一些时间。"],
    ready: ["本地引擎已就绪", "ready", "", ""],
    failed: ["本地引擎启动失败", "failed", "分析引擎未能启动", detail || "请重试；若问题持续，请打开日志目录查看 daemon.log。"],
    auth: ["引擎认证失败", "failed", "桌面会话认证失败", detail || "引擎正在运行，但桌面会话令牌无效。请重启应用后重试。"],
  };
  const [statusText, kind, heading, description] = states[state];
  setEngineStatus(statusText, kind);
  panel.hidden = state === "ready";
  if (heading) title.textContent = heading;
  if (description) message.textContent = description;
  updateSetupBanner();
}

function openDrawer(id) {
  const focusGeneration = ++drawerFocusGeneration;
  drawerReturnFocus = document.activeElement;
  document.querySelectorAll(".drawer.open").forEach((drawer) => {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  });
  const drawer = document.querySelector(`#${id}`);
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  document.querySelector("#drawer-backdrop").hidden = false;
  requestAnimationFrame(() => {
    if (focusGeneration !== drawerFocusGeneration || !drawer.classList.contains("open")) return;
    const target = drawer.querySelector(
      "button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex='0']",
    );
    (target || drawer).focus();
  });
}

function closeDrawers({ restoreFocus = true } = {}) {
  drawerFocusGeneration += 1;
  document.querySelectorAll(".drawer.open").forEach((drawer) => {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  });
  document.querySelector("#drawer-backdrop").hidden = true;
  if (restoreFocus && drawerReturnFocus?.isConnected) drawerReturnFocus.focus();
  drawerReturnFocus = null;
}

function focusableElements(container) {
  return [...container.querySelectorAll(
    "button:not(:disabled):not([hidden]), input:not(:disabled), select:not(:disabled), summary, [href], [tabindex]:not([tabindex='-1'])",
  )].filter((element) => !element.closest("[hidden]"));
}

function updateSetupBanner() {
  document.querySelector("#setup-banner").hidden = modelConfigured || engineState !== "ready";
  const notice = document.querySelector("#remote-ai-notice");
  const manualHint = "连接 Ubuntu 与 AI 模型无关。自定义接口请在设置中保存，同步完成后点「开始分析」。";
  notice.textContent = modelHint ? `${modelHint} ${manualHint}` : manualHint;
  notice.hidden = engineState !== "ready";
  notice.className = modelProbeStatus === "failed" || !modelConfigured
    ? "notice danger"
    : modelProbeStatus === "untested"
      ? "notice warn"
      : "notice";
  const usability = document.querySelector("#ai-usability-hint");
  if (usability) usability.textContent = modelHint;
}

function remoteFormDirty() {
  return remoteFormEdited;
}

const REMOTE_DRAFT_KEY = "codeinsight-remote-draft";
const REMOTE_FINGERPRINT_KEY = "codeinsight-remote-fingerprint";

function readRemoteDraft() {
  try {
    const parsed = JSON.parse(localStorage.getItem(REMOTE_DRAFT_KEY) || "null");
    if (!parsed || typeof parsed !== "object") return null;
    const { password: _ignored, ...safe } = parsed;
    return safe;
  } catch {
    return null;
  }
}

function writeRemoteDraft() {
  const draft = {
    host: document.querySelector("#remote-host").value.trim(),
    port: Number(document.querySelector("#remote-port").value) || 22,
    username: document.querySelector("#remote-username").value.trim(),
    remote_path: document.querySelector("#remote-path").value.trim(),
    poll_seconds: Number(document.querySelector("#remote-poll-seconds").value) || 60,
  };
  localStorage.setItem(REMOTE_DRAFT_KEY, JSON.stringify(draft));
}

function rememberRemoteFingerprint(key, value) {
  confirmedRemoteFingerprint = { key, value };
  localStorage.setItem(REMOTE_FINGERPRINT_KEY, JSON.stringify({ key, value }));
}

function fingerprintForForm(host, port) {
  const key = `${host}:${port}`;
  if (confirmedRemoteFingerprint?.key === key && confirmedRemoteFingerprint.value) {
    return confirmedRemoteFingerprint;
  }
  try {
    const saved = JSON.parse(localStorage.getItem(REMOTE_FINGERPRINT_KEY) || "null");
    if (saved?.key === key && saved.value) return saved;
  } catch {
    /* ignore broken fingerprint cache */
  }
  const known = latestRemoteProjects.find(
    (project) => project.host === host && Number(project.port) === Number(port) && project.host_fingerprint,
  );
  return known ? { key, value: known.host_fingerprint } : null;
}

function restoreRemoteDraft(projects = latestRemoteProjects) {
  if (remoteFormEdited) return;
  const fallback = projects[0]
    ? {
      host: projects[0].host,
      port: projects[0].port,
      username: projects[0].username,
      remote_path: projects[0].remote_path,
      poll_seconds: projects[0].poll_seconds,
    }
    : null;
  const draft = readRemoteDraft() || fallback;
  if (!draft) return;
  if (draft.host) document.querySelector("#remote-host").value = draft.host;
  if (draft.port) document.querySelector("#remote-port").value = draft.port;
  if (draft.username) document.querySelector("#remote-username").value = draft.username;
  if (draft.remote_path) document.querySelector("#remote-path").value = draft.remote_path;
  if (draft.poll_seconds) document.querySelector("#remote-poll-seconds").value = draft.poll_seconds;
  confirmedRemoteFingerprint = fingerprintForForm(
    document.querySelector("#remote-host").value.trim(),
    Number(document.querySelector("#remote-port").value),
  );
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
      const detail = typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail || {});
      throw new ApiError(detail || `请求失败 (${response.status})`, response.status);
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
  const generation = ++engineProbeGeneration;
  const startedAt = performance.now();
  let delay = 500;
  setEngineState("starting");
  while (performance.now() - startedAt < 90000 && generation === engineProbeGeneration) {
    try {
      const health = await api("/health", { timeout: 4000 });
      if (generation !== engineProbeGeneration) return;
      engineVersion = health.engine_version || "";
      renderAppVersion();
      if (
        desktopAppVersion
        && engineVersion
        && engineVersion !== "development"
        && engineVersion !== desktopAppVersion
      ) {
        setEngineState(
          "failed",
          `桌面版本 ${desktopAppVersion} 与分析引擎版本 ${engineVersion} 不一致，请完全退出后重新启动应用。`,
        );
        return;
      }
      setEngineState("ready");
      await Promise.allSettled([
        loadTasks(),
        loadContinuousProjects(),
        loadRemoteProjects(),
        loadModelSettings(),
        loadOllamaStatus(),
      ]);
      return;
    } catch (error) {
      if (error?.status === 401 || error?.status === 403) {
        setEngineState("auth", errorMessage(error));
        return;
      }
      const elapsed = Math.round((performance.now() - startedAt) / 1000);
      setEngineState(
        "starting",
        `正在等待分析引擎响应（${elapsed} 秒）…`,
      );
      engineLastError = errorMessage(error);
      await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(delay * 2, 5000);
    }
  }
  if (generation === engineProbeGeneration) {
    setEngineState("failed", `等待 90 秒后仍无法连接：${engineLastError}`);
  }
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
  if (String(task.id || "").startsWith("space_")) {
    activeSpaceId = task.id.slice("space_".length);
    localStorage.setItem("codeinsight-active-space", activeSpaceId);
  }
  openDrawer("result-panel");
  title.textContent = `${taskLocation(task) || task.name || `${task.owner}/${task.repo}`} 分析结果`;
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
    if (activeSpaceId) query.set("space_id", activeSpaceId);
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

async function loadTaskDetails(task) {
  const title = document.querySelector("#result-title");
  const message = document.querySelector("#result-message");
  const pagesContainer = document.querySelector("#result-pages");
  openDrawer("result-panel");
  title.textContent = `${taskLocation(task) || task.name || `${task.owner}/${task.repo}`} 任务详情`;
  message.className = "message";
  message.textContent = "正在读取任务详情…";
  pagesContainer.replaceChildren();
  try {
    const detail = await api(`/wiki/tasks/${encodeURIComponent(task.id)}`);
    const article = document.createElement("article");
    article.className = "result-page task-detail";
    const fields = [
      ["状态", detail.status],
      ["进度", `${detail.pages_done}/${detail.pages_total || "?"} 页`],
      ["当前页面", (detail.current_page_ids || []).join("、") || "无"],
      ["提交时间", formatSyncTime(detail.submitted_at)],
      ["错误", detail.error || "无"],
    ];
    fields.forEach(([label, value]) => {
      const row = document.createElement("p");
      row.textContent = `${label}：${value}`;
      article.append(row);
    });
    pagesContainer.append(article);
    message.textContent = `任务 ID：${task.id}`;
  } catch (error) {
    message.className = "message error";
    message.textContent = errorMessage(error);
  }
}

async function retryTask(task) {
  const project = latestContinuousProjects.find(
    (candidate) => candidate.last_task_id === task.id,
  );
  if (!project?.request) {
    throw new Error("此任务缺少可安全重试的项目配置；请从项目列表重新添加。");
  }
  await api("/wiki/tasks", {
    method: "POST",
    body: JSON.stringify({ ...project.request, force: true }),
  });
  await refreshWorkspace();
}

function taskStatusKind(status) {
  if (status === "completed") return "ready";
  if (status === "failed" || status === "cancelled") return "failed";
  return "waiting";
}

function taskStatusLabel(status) {
  return {
    pending: "排队中",
    waiting: "等待中",
    running: "运行中",
    indexing: "建立索引",
    determining_structure: "规划文档结构",
    generating: "生成文档",
    paused: "已暂停",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  }[status] || status;
}

function formatElapsed(startedAt) {
  if (!startedAt) return "";
  const seconds = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  if (seconds < 60) return `已用 ${seconds} 秒`;
  return `已用 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function formatHeartbeat(updatedAt) {
  if (!updatedAt) return "";
  const seconds = Math.max(0, Math.round((Date.now() - updatedAt) / 1000));
  if (seconds < 2) return "进度刚刚更新";
  return `进度更新于 ${seconds} 秒前`;
}

function remoteProgressText(project) {
  const busy = ["connecting", "syncing", "analyzing"].includes(project.stage);
  const parts = [];
  if (project.progress_message) parts.push(project.progress_message);
  else if (project.stage === "syncing") parts.push("正在同步，等待首个目录列表…");
  if (project.current_path && !String(project.progress_message || "").includes(project.current_path)) {
    parts.push(project.current_path);
  }
  if (project.stage === "analyzing") {
    if (project.analysis_status) {
      parts.push(`${taskStatusLabel(project.analysis_status)} · ${project.analysis_pages_done || 0}/${project.analysis_pages_total || "?"} 页`);
    } else if (!project.progress_message) {
      parts.push("正在启动分析任务…");
    }
  }
  if (["ready_for_analysis", "failed"].includes(project.stage) && !modelConfigured) {
    parts.push(modelHint || "AI 未配置，请先配置后再点「开始分析」。");
  }
  const updatedAt = project.progress_updated_at || project.sync_started_at;
  const heartbeat = formatHeartbeat(updatedAt);
  const stale = busy && updatedAt && Date.now() - updatedAt > 15000;
  if (stale) parts.push(`${heartbeat}。可能卡住，可取消后重试。`);
  else if (heartbeat && busy) parts.push(heartbeat);
  return parts.filter(Boolean).join(" · ");
}

function watchRemoteProgress(projects) {
  const busy = projects.some((project) => ["connecting", "syncing", "analyzing"].includes(project.stage));
  if (busy && !remoteWatchTimer) {
    remoteWatchTimer = setInterval(() => {
      loadRemoteProjects().catch(() => {});
    }, 2000);
  } else if (!busy && remoteWatchTimer) {
    clearInterval(remoteWatchTimer);
    remoteWatchTimer = null;
  }
}

function renderEmptyState(container, title, hint) {
  const wrap = document.createElement("div");
  wrap.className = "empty-state";
  const text = document.createElement("p");
  text.className = "empty";
  text.textContent = title;
  wrap.append(text);
  if (hint) {
    const extra = document.createElement("p");
    extra.className = "hint";
    extra.textContent = hint;
    wrap.append(extra);
  }
  container.replaceChildren(wrap);
}

function taskLocation(task) {
  if (task.location) return task.location;
  const spaceId = String(task.id || "").startsWith("space_")
    ? task.id.slice("space_".length)
    : "";
  const space = latestKnowledgeSpaces.find((item) => item.space_id === spaceId);
  if (space) {
    const dirs = (space.included_dirs || []).filter(Boolean);
    if (space.parent_workspace) {
      return dirs.length
        ? `${space.parent_workspace} / ${dirs.join(", ")}`
        : space.parent_workspace;
    }
    if (space.label) return space.label;
  }
  const project = latestContinuousProjects.find(
    (item) => item.last_task_id === task.id || item.request?.repo === task.repo,
  );
  const localPath = project?.request?.repo_url || project?.request?.localPath;
  if (localPath) {
    const dirs = (project.request?.included_dirs || []).filter(Boolean);
    return dirs.length ? `${localPath} / ${dirs.join(", ")}` : localPath;
  }
  return "";
}

function renderTask(task) {
  const card = document.createElement("article");
  card.className = "task";
  const detail = document.createElement("div");
  const head = document.createElement("div");
  head.className = "card-head";
  const title = document.createElement("h3");
  const location = taskLocation(task);
  title.textContent = task.repo || task.name || `${task.owner}/${task.repo}`;
  const badge = document.createElement("span");
  badge.className = `badge ${taskStatusKind(task.status)}`;
  badge.textContent = taskStatusLabel(task.status);
  head.append(title, badge);
  const path = document.createElement("p");
  path.className = "card-path";
  path.textContent = location || task.name || `${task.owner}/${task.repo}`;
  path.title = path.textContent;
  const status = document.createElement("p");
  status.className = "meta";
  status.textContent = `${task.pages_done}/${task.pages_total || "?"} 页`;
  const progress = document.createElement("div");
  progress.className = "progress";
  const bar = document.createElement("span");
  const percent = task.pages_total ? (task.pages_done / task.pages_total) * 100 : 3;
  bar.style.width = `${Math.min(100, percent)}%`;
  progress.append(bar);
  detail.append(head, path, status, progress);
  if (task.error) {
    const error = document.createElement("p");
    error.className = "task-error";
    error.textContent = task.error;
    detail.append(error);
  }

  const actions = document.createElement("div");
  actions.className = "task-actions";
  const detailsButton = document.createElement("button");
  detailsButton.className = "ghost";
  detailsButton.textContent = "详情";
  detailsButton.addEventListener("click", () => loadTaskDetails(task));
  if (task.status !== "completed") actions.append(detailsButton);
  if (task.status === "completed") {
    const viewButton = document.createElement("button");
    viewButton.className = "secondary";
    viewButton.textContent = "查看结果";
    viewButton.addEventListener("click", () => loadWikiResult(task));
    actions.append(viewButton);
  } else if (["failed", "cancelled"].includes(task.status)) {
    actions.append(remoteActionButton("重试", () => retryTask(task)));
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
  if (!tasks.length) {
    renderEmptyState(
      list,
      taskFilter === "all" ? "尚无分析任务。" : "当前筛选下没有任务。",
      taskFilter === "all" ? "添加项目后会在这里看到分析进度。" : "",
    );
    return;
  }
  list.replaceChildren();
  tasks.forEach((task) => list.append(renderTask(task)));
}

async function loadTasks() {
  if (engineState !== "ready") return;
  if (tasksLoading) return;
  tasksLoading = true;
  try {
    latestTasks = await api("/wiki/tasks");
    renderTaskList();
  } catch (error) {
    renderListError(document.querySelector("#task-list"), "任务读取失败", error);
    throw error;
  } finally {
    tasksLoading = false;
  }
}

function formatSyncTime(timestamp) {
  if (!timestamp) return "尚未同步";
  const milliseconds = timestamp < 10_000_000_000 ? timestamp * 1000 : timestamp;
  return new Date(milliseconds).toLocaleString("zh-CN");
}

function renderListError(container, title, error) {
  const card = document.createElement("div");
  card.className = "error-card";
  card.setAttribute("role", "alert");
  card.textContent = `${title}：${errorMessage(error)}`;
  container.replaceChildren(card);
}

function renderContinuousProject(project) {
  const card = document.createElement("article");
  card.className = "continuous-project";
  const detail = document.createElement("div");
  const title = document.createElement("h3");
  title.textContent = project.request?.repo || project.id;
  const path = document.createElement("p");
  path.className = "card-path";
  path.textContent = project.request?.repo_url || project.request?.localPath || "本地项目";
  const meta = document.createElement("p");
  meta.className = "meta";
  const schedule = project.night_start
    ? `${project.night_start}–${project.night_end} · 每 ${project.poll_seconds} 秒`
    : `全天监控 · 每 ${project.poll_seconds} 秒`;
  meta.textContent = `${schedule} · ${formatSyncTime(project.last_scan_at)}`;
  detail.append(title, path, meta);
  const actions = document.createElement("div");
  actions.className = "task-actions";
  const pause = document.createElement("button");
  pause.type = "button";
  pause.className = "ghost";
  pause.textContent = project.enabled ? "暂停" : "继续";
  pause.addEventListener("click", async () => {
    pause.disabled = true;
    try {
      const action = project.enabled ? "pause" : "resume";
      await api(`/continuous/projects/${encodeURIComponent(project.id)}/${action}`, {
        method: "POST",
      });
      await refreshWorkspace();
    } catch (error) {
      window.alert(errorMessage(error));
    } finally {
      pause.disabled = false;
    }
  });
  const remove = remoteActionButton("删除", async () => {
    if (!window.confirm(`停止监控“${title.textContent}”并从列表删除？`)) return;
    await api(`/continuous/projects/${encodeURIComponent(project.id)}`, { method: "DELETE" });
    await refreshWorkspace();
  }, "danger");
  actions.append(pause, remove);
  card.append(detail, actions);
  return card;
}

async function loadContinuousProjects() {
  if (engineState !== "ready" || continuousProjectsLoading) return;
  continuousProjectsLoading = true;
  const list = document.querySelector("#continuous-project-list");
  try {
    latestContinuousProjects = await api("/continuous/projects");
    if (!latestContinuousProjects.length) {
      renderEmptyState(list, "尚未添加本地持续项目。", "点击「添加」选择工作目录。");
    } else {
      list.replaceChildren();
      latestContinuousProjects.forEach((project) => {
        list.append(renderContinuousProject(project));
      });
    }
  } catch (error) {
    renderListError(list, "项目读取失败", error);
    throw error;
  } finally {
    continuousProjectsLoading = false;
  }
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

function setRemoteFlow(stage) {
  const order = ["fingerprint", "connect", "sync", "analysis"];
  const activeIndex = order.indexOf(stage);
  document.querySelectorAll("#remote-flow li").forEach((item, index) => {
    item.classList.toggle("active", index === activeIndex);
    item.classList.toggle("done", activeIndex > index || stage === "done");
  });
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
  title.textContent = `${project.username}@${project.host}`;
  const path = document.createElement("p");
  path.className = "card-path";
  path.textContent = project.remote_path;
  path.title = `${project.username}@${project.host}:${project.remote_path}`;
  const status = document.createElement("p");
  status.textContent = project.last_error
    ? `${stageLabels[project.stage] || project.stage}：${project.last_error}`
    : `${stageLabels[project.stage] || "已保存"} · 最近同步：${formatSyncTime(project.last_sync_at)} · 每 ${project.poll_seconds} 秒`;
  status.className = project.last_error ? "remote-error" : "meta";
  const projectFlow = {
    saved: "指纹 ✓  →  等待连接  →  同步  →  分析",
    connecting: "指纹 ✓  →  正在连接…  →  同步  →  分析",
    syncing: "指纹 ✓  →  连接 ✓  →  正在同步…  →  分析",
    ready_for_analysis: "指纹 ✓  →  连接 ✓  →  同步 ✓  →  等待分析",
    analyzing: "指纹 ✓  →  连接 ✓  →  同步 ✓  →  正在分析…",
    failed: "流程中断 · 可查看错误并重试",
  };
  const flow = document.createElement("p");
  flow.className = "meta";
  flow.textContent = projectFlow[project.stage] || projectFlow.saved;
  const counts = document.createElement("p");
  counts.className = "meta";
  const elapsed = ["connecting", "syncing", "analyzing"].includes(project.stage)
    ? formatElapsed(project.sync_started_at)
    : "";
  counts.textContent = [
    `已扫描 ${project.files_seen || 0} 个文件`,
    `${project.dirs_seen || 0} 个目录`,
    elapsed,
  ].filter(Boolean).join(" · ");
  const progressLine = document.createElement("p");
  const progressText = remoteProgressText(project);
  const updatedAt = project.progress_updated_at || project.sync_started_at;
  const stale = ["connecting", "syncing"].includes(project.stage)
    && updatedAt
    && Date.now() - updatedAt > 15000;
  progressLine.className = stale ? "meta stuck-hint" : "meta";
  progressLine.textContent = progressText;
  const extras = document.createElement("details");
  extras.className = "card-extras";
  extras.open = ["connecting", "syncing", "failed"].includes(project.stage);
  const summary = document.createElement("summary");
  summary.textContent = "连接与同步详情";
  const fingerprint = document.createElement("p");
  fingerprint.className = "fingerprint";
  fingerprint.textContent = project.host_fingerprint
    ? `服务器指纹：${project.host_fingerprint}`
    : "服务器指纹：等待首次连接";
  const syncStats = document.createElement("p");
  syncStats.className = "fingerprint";
  syncStats.textContent = `排除 ${project.files_excluded || 0} · 超大 ${project.files_oversize || 0} · 跳过链接 ${project.symlinks_skipped || 0}`;
  extras.append(summary, fingerprint, syncStats);
  detail.append(title, path, status, flow, counts);
  if (progressLine.textContent) detail.append(progressLine);
  if (["connecting", "syncing", "analyzing"].includes(project.stage)) {
    const progress = document.createElement("div");
    progress.className = "progress live";
    const bar = document.createElement("span");
    let percent = 8;
    if (project.stage === "syncing") {
      percent = Math.min(90, 8 + (project.files_seen || 0) * 2 + (project.dirs_seen || 0));
    } else if (project.stage === "analyzing" && project.analysis_pages_total) {
      percent = Math.min(100, (project.analysis_pages_done / project.analysis_pages_total) * 100);
    } else if (project.stage === "analyzing") {
      percent = 20;
    }
    bar.style.width = `${percent}%`;
    progress.append(bar);
    detail.append(progress);
  }
  detail.append(extras);

  const actions = document.createElement("div");
  actions.className = "task-actions";
  const busy = ["connecting", "syncing", "analyzing"].includes(project.stage);
  const hasCopy = (project.files_seen || 0) > 0 || ["ready_for_analysis", "analyzing"].includes(project.stage);
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
      await refreshWorkspace();
    } catch (error) {
      window.alert(errorMessage(error));
    } finally {
      syncButton.disabled = false;
    }
  });
  const analyzeButton = remoteActionButton("分析整个根目录", async () => {
    if (!modelConfigured || modelProbeStatus === "failed") {
      openDrawer("settings-drawer");
      return;
    }
    await api(`/remote/projects/${encodeURIComponent(project.id)}/analyze`, { method: "POST" });
    await refreshWorkspace();
  });
  if (project.stage === "analyzing") {
    analyzeButton.disabled = true;
    analyzeButton.title = "正在分析整个根目录…";
  } else if (busy) {
    analyzeButton.disabled = true;
    analyzeButton.title = "同步完成后可开始";
  } else if (!hasCopy) {
    analyzeButton.disabled = true;
    analyzeButton.title = "请先完成同步";
  } else if (!modelConfigured) {
    analyzeButton.title = "请先配置 AI";
  } else if (modelProbeStatus === "failed") {
    analyzeButton.title = "AI 最近测试失败";
  } else {
    analyzeButton.title = "分析整个根目录";
  }
  const addScopeButton = remoteActionButton("添加子分析", () => {
    if (!hasCopy) {
      window.alert("请先完成同步");
      return;
    }
    openRemoteScopeDrawer(project);
  });
  addScopeButton.disabled = !hasCopy || ["connecting", "syncing"].includes(project.stage);
  addScopeButton.title = addScopeButton.disabled ? "同步完成后可添加子分析" : "在已同步目录上创建子分析";
  const retryButton = project.stage === "failed"
    ? remoteActionButton("重试", async () => {
      await api(`/remote/projects/${encodeURIComponent(project.id)}/retry`, { method: "POST" });
      await loadRemoteProjects();
    })
    : null;
  const cancelButton = busy
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
        await refreshWorkspace();
      } catch (error) {
        window.alert(errorMessage(error));
        deleteButton.disabled = false;
      }
    },
    "danger",
  );
  actions.append(terminalButton, syncButton, analyzeButton, addScopeButton);
  if (retryButton) actions.append(retryButton);
  if (cancelButton) actions.append(cancelButton);
  actions.append(deleteButton);
  card.append(detail, actions);
  const childScopes = (project.scopes || []).filter((scope) => (scope.included_dirs || []).length);
  if (childScopes.length) {
    const scopes = document.createElement("div");
    scopes.className = "remote-scopes";
    childScopes.forEach((scope) => scopes.append(renderRemoteScope(project, scope)));
    card.append(scopes);
  }
  return card;
}

function renderRemoteScope(project, scope) {
  const row = document.createElement("div");
  row.className = "remote-scope";
  const info = document.createElement("div");
  const title = document.createElement("p");
  title.textContent = scope.included_dirs.join(", ");
  const meta = document.createElement("p");
  meta.className = "meta";
  const analyzing = scope.analysis_status && !["completed", "failed", "cancelled"].includes(scope.analysis_status);
  meta.textContent = analyzing
    ? `正在分析${scope.analysis_pages_total ? ` ${scope.analysis_pages_done}/${scope.analysis_pages_total}` : "…"}`
    : (scope.analysis_status === "completed" ? "已分析" : "未分析");
  info.append(title, meta);
  const analyze = remoteActionButton("开始分析", async () => {
    if (!modelConfigured || modelProbeStatus === "failed") {
      openDrawer("settings-drawer");
      return;
    }
    await api(
      `/remote/projects/${encodeURIComponent(project.id)}/scopes/${encodeURIComponent(scope.space_id)}/analyze`,
      { method: "POST" },
    );
    await refreshWorkspace();
  });
  if (analyzing) {
    analyze.disabled = true;
    analyze.title = "正在分析…";
  } else if (!modelConfigured) {
    analyze.title = "请先配置 AI";
  }
  const remove = remoteActionButton("删除", async () => {
    if (!window.confirm(`删除子分析 ${scope.included_dirs.join(", ")}？同步副本和服务器代码不会被修改。`)) return;
    await api(
      `/remote/projects/${encodeURIComponent(project.id)}/scopes/${encodeURIComponent(scope.space_id)}`,
      { method: "DELETE" },
    );
    await refreshWorkspace();
  }, "danger");
  const actions = document.createElement("div");
  actions.className = "task-actions";
  actions.append(analyze, remove);
  row.append(info, actions);
  return row;
}

function openRemoteScopeDrawer(project) {
  scopeProjectId = project.id;
  document.querySelector("#remote-scope-target").textContent =
    `${project.username}@${project.host}:${project.remote_path}`;
  document.querySelector("#remote-scope-custom").value = "";
  document.querySelector("#remote-scope-list").innerHTML =
    '<p class="empty">点「扫描子目录」，或在下方填写相对路径。</p>';
  const message = document.querySelector("#remote-scope-message");
  message.className = "message";
  message.textContent = "";
  openDrawer("remote-scope-drawer");
}

function parseRemoteScopeCustom(value) {
  return value
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function loadRemoteProjects() {
  if (engineState !== "ready") return;
  if (remoteProjectsLoading) return;
  remoteProjectsLoading = true;
  const list = document.querySelector("#remote-project-list");
  try {
    const projects = await api("/remote/projects");
    const placeholder = document.querySelector("#terminal-placeholder");
    if (placeholder) placeholder.hidden = projects.length > 0;
    latestRemoteProjects = projects;
    restoreRemoteDraft(projects);
    watchRemoteProgress(projects);
    if (!projects.length) {
      renderEmptyState(list, "尚未连接 Ubuntu 项目。", "通过顶部「连接 Ubuntu」添加远程目录。");
    } else {
      list.replaceChildren();
      projects.forEach((project) => list.append(renderRemoteProject(project)));
    }
  } catch (error) {
    renderListError(list, "Ubuntu 项目读取失败", error);
    throw error;
  } finally {
    remoteProjectsLoading = false;
  }
}

async function refreshWorkspace({ includeSettings = false } = {}) {
  if (refreshPromise) {
    refreshQueued = true;
    return refreshPromise;
  }
  const button = document.querySelector("#refresh-button");
  const errorCard = document.querySelector("#refresh-error");
  button.disabled = true;
  button.textContent = "刷新中…";
  errorCard.hidden = true;
  refreshPromise = (async () => {
    do {
      refreshQueued = false;
      const jobs = [loadContinuousProjects(), loadRemoteProjects(), loadTasks(), loadKnowledgeSpaces()];
      if (includeSettings) jobs.push(loadModelSettings(), loadOllamaStatus());
      const results = await Promise.allSettled(jobs);
      const rejected = results.filter((result) => result.status === "rejected");
      if (rejected.length) {
        errorCard.textContent = rejected.map((result) => errorMessage(result.reason)).join("；");
        errorCard.hidden = false;
      }
    } while (refreshQueued);
  })().finally(() => {
    refreshPromise = null;
    button.disabled = false;
    button.textContent = "刷新全部";
  });
  return refreshPromise;
}

function updateModelForm() {
  const provider = document.querySelector("#model-provider");
  const keyLabel = document.querySelector("#api-key-label");
  const compatible = document.querySelector("#compatible-fields");
  provider.value = modelProvider;
  keyLabel.hidden = modelProvider === "ollama";
  compatible.hidden = modelProvider !== "openai_compatible";
  const state = document.querySelector("#provider-state");
  state.textContent = modelProvider === savedModelProvider
    ? `当前已保存：${provider.options[provider.selectedIndex]?.text || savedModelProvider}`
    : `草稿：${provider.options[provider.selectedIndex]?.text || modelProvider}（尚未保存；项目仍使用 ${savedModelProvider}）`;
}

function ensureModelOption(modelId) {
  const input = document.querySelector("#model-id");
  const list = document.querySelector("#model-id-options");
  if (!modelId || !input) return;
  if (list && ![...list.options].some((option) => option.value === modelId)) {
    const option = document.createElement("option");
    option.value = modelId;
    list.append(option);
  }
  input.value = modelId;
}

async function loadModelSettings() {
  if (engineState !== "ready") return;
  const status = document.querySelector("#model-status");
  try {
    const settings = await api("/desktop/settings");
    const previousSavedProvider = savedModelProvider;
    savedModelProvider = settings.provider;
    const settingsOpen = document.querySelector("#settings-drawer").classList.contains("open");
    if (!settingsOpen || modelProvider === previousSavedProvider) {
      modelProvider = savedModelProvider;
    }
    modelConfigured = settings.configured;
    modelUsable = settings.usable !== false && settings.configured;
    modelHint = settings.hint || "";
    modelProbeStatus = settings.probe_status || "untested";
    if (settings.base_url) {
      document.querySelector("#model-base-url").value = settings.base_url;
    }
    if (settings.selected_model) {
      savedModelId = settings.selected_model;
      localStorage.setItem("codeinsight-model-id", savedModelId);
      ensureModelOption(savedModelId);
    }
    if (settings.embedder_mode) {
      document.querySelector("#embedder-mode").value = settings.embedder_mode;
    }
    if (settings.wiki_page_concurrency) {
      document.querySelector("#wiki-page-concurrency").value = settings.wiki_page_concurrency;
    }
    const tier = settings.ollama_tier || localStorage.getItem("codeinsight-ollama-tier") || "auto";
    if (document.activeElement !== document.querySelector("#ollama-tier")) {
      document.querySelector("#ollama-tier").value = tier;
    }
    localStorage.setItem("codeinsight-model-provider", savedModelProvider);
    localStorage.setItem("codeinsight-ollama-tier", tier);
    updateModelForm();
    if (!modelConfigured) {
      status.textContent = "AI 需要配置";
      status.className = "badge failed";
    } else if (modelProbeStatus === "failed") {
      status.textContent = "AI 不可用";
      status.className = "badge failed";
    } else if (modelProbeStatus === "ok") {
      status.textContent = "AI 可用";
      status.className = "badge ready";
    } else {
      status.textContent = "AI 已配置（未测试）";
      status.className = "badge waiting";
    }
    status.title = modelHint || "";
    updateSetupBanner();
  } catch (error) {
    status.textContent = "读取失败";
    status.className = "badge failed";
    updateSetupBanner();
  }
}

async function loadOllamaStatus() {
  if (engineState !== "ready") return;
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
      const selectedTier = status.selected_tier
        || localStorage.getItem("codeinsight-ollama-tier")
        || "auto";
      document.querySelector("#ollama-tier").value = selectedTier;
    }
    if (status.ready && !ollamaReady) {
      ollamaReady = true;
      await loadModelSettings();
    } else if (!status.ready) {
      ollamaReady = false;
    }
    if (status.restart_required && !ollamaRestarting) {
      if (remoteFormDirty()) {
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
document.querySelector("#ollama-tier").addEventListener("change", (event) => {
  localStorage.setItem("codeinsight-ollama-tier", event.target.value);
  document.querySelector("#ollama-tier-hint").textContent =
    "质量档位已记住；下次安装或升级本地模型时应用。";
});

document.querySelector("#model-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = document.querySelector("#model-message");
  const apiKey = document.querySelector("#model-api-key").value.trim();
  try {
    message.className = "message";
    message.textContent = "正在保存模型设置…";
    const payload = {
      provider: modelProvider,
      api_key: apiKey || null,
    };
    if (modelProvider === "openai_compatible") {
      payload.base_url = document.querySelector("#model-base-url").value.trim();
      payload.selected_model = document.querySelector("#model-id").value.trim();
      payload.embedder_mode = document.querySelector("#embedder-mode").value;
      payload.wiki_page_concurrency = Number(document.querySelector("#wiki-page-concurrency").value);
      if (!payload.base_url) throw new Error("请填写 API 地址");
    }
    const settings = await api("/desktop/settings", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (!settings.configured) {
      throw new Error(
        modelProvider === "openai_compatible"
          ? "自定义模型需要有效的 API 地址和 API Key"
          : `${modelProvider} 需要有效的 API Key`,
      );
    }
    savedModelProvider = settings.provider;
    modelProvider = savedModelProvider;
    savedModelId = settings.selected_model || savedModelId;
    localStorage.setItem("codeinsight-model-provider", savedModelProvider);
    if (savedModelId) localStorage.setItem("codeinsight-model-id", savedModelId);
    updateModelForm();
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
    language: "zh",
  };
  try {
    message.className = "message";
    setRemoteFlow("fingerprint");
    message.textContent = "正在读取服务器主机指纹（尚未发送用户名和密码）…";
    const probe = await api("/remote/fingerprint", {
      method: "POST",
      timeout: 45000,
      body: JSON.stringify({ host: body.host, port: body.port }),
    });
    const remembered = fingerprintForForm(body.host, body.port);
    if (!remembered || remembered.value !== probe.fingerprint) {
      const approved = window.confirm(
        `首次连接需要确认 Ubuntu 主机身份：\n\n${probe.algorithm}\n${probe.fingerprint}\n\n请与服务器管理员核对。确认信任并继续吗？`,
      );
      if (!approved) throw new Error("已取消：未确认服务器主机指纹");
    }
    rememberRemoteFingerprint(`${body.host}:${body.port}`, probe.fingerprint);
    body.host_fingerprint = probe.fingerprint;
    setRemoteFlow("connect");
    message.textContent = "指纹已确认，正在验证用户名和密码…";
    await api("/remote/projects", {
      method: "POST",
      timeout: 60000,
      body: JSON.stringify(body),
    });
    passwordInput.value = "";
    writeRemoteDraft();
    remoteFormEdited = false;
    setRemoteFlow("sync");
    message.textContent = "远程项目已保存，正在后台同步。完成后请在卡片上点「开始分析」。";
    await refreshWorkspace();
    closeDrawers();
  } catch (error) {
    message.className = "message error";
    message.textContent = errorMessage(error);
  }
});
document.querySelector("#remote-form").addEventListener("input", () => {
  remoteFormEdited = true;
  writeRemoteDraft();
  setRemoteFlow("fingerprint");
});

async function detectRemoteScopeCandidates(projectId) {
  const encoded = encodeURIComponent(projectId);
  const paths = [
    [`/remote/projects/${encoded}/scopes/detect`, { method: "POST", body: "{}" }],
    [`/remote/projects/${encoded}/detect-scopes`, { method: "POST", body: "{}" }],
    [`/remote/projects/${encoded}/scopes/detect`, { method: "GET" }],
  ];
  let lastError = null;
  for (const [path, options] of paths) {
    try {
      return await api(path, options);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("扫描子目录失败");
}

document.querySelector("#remote-scope-detect-button").addEventListener("click", async () => {
  const list = document.querySelector("#remote-scope-list");
  const message = document.querySelector("#remote-scope-message");
  if (!scopeProjectId) {
    list.innerHTML = '<p class="empty">请先选择一个远程项目。</p>';
    return;
  }
  list.innerHTML = '<p class="empty">正在扫描…</p>';
  message.className = "message";
  message.textContent = "";
  try {
    const detected = await detectRemoteScopeCandidates(scopeProjectId);
    list.replaceChildren();
    if (!detected.candidates.length) {
      list.innerHTML = '<p class="empty">未发现子仓，请手动填写相对路径。</p>';
      return;
    }
    detected.candidates.forEach((candidate) => {
      const label = document.createElement("label");
      label.className = "checkbox";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = candidate.path;
      label.append(input, document.createTextNode(`${candidate.label}（${candidate.kind}）`));
      list.append(label);
    });
  } catch (error) {
    const detail = errorMessage(error);
    const missing = error?.status === 404 || /not found/i.test(detail);
    list.innerHTML = `<p class="message error">${
      missing
        ? "未能扫描同步副本。请手动填写相对路径，例如 YinWang/br_feature_ADS_truck_0820"
        : detail
    }</p>`;
  }
});

document.querySelector("#remote-scope-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = document.querySelector("#remote-scope-message");
  const checked = [...document.querySelectorAll("#remote-scope-list input[type=checkbox]:checked")]
    .map((input) => input.value);
  const custom = parseRemoteScopeCustom(document.querySelector("#remote-scope-custom").value);
  const included = [...new Set([...checked, ...custom])];
  try {
    if (!scopeProjectId) throw new Error("请先选择一个远程项目");
    if (!included.length) throw new Error("请勾选或填写至少一个子目录");
    message.className = "message";
    message.textContent = "正在创建子分析…";
    await api(`/remote/projects/${encodeURIComponent(scopeProjectId)}/scopes`, {
      method: "POST",
      body: JSON.stringify({ included_dirs: included }),
    });
    message.textContent = "子分析已创建。完成后请在卡片上点「开始分析」。";
    await refreshWorkspace();
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
    body.task.provider = savedModelProvider;
    if (savedModelId) body.task.model = savedModelId;
    const included = [...document.querySelectorAll("#subdir-list input[type=checkbox]:checked")]
      .map((input) => input.value);
    message.textContent = "正在建立知识库…";
    await api("/knowledge/spaces", {
      method: "POST",
      body: JSON.stringify({
        workspace_root: path,
        included_dirs: included,
        language: "zh",
        provider: savedModelProvider,
        model: savedModelId || null,
        analyze_now: true,
        night_start: body.night_start,
        night_end: body.night_end,
        poll_seconds: body.poll_seconds,
      }),
      timeout: 120000,
    });
    message.textContent = "项目已加入持续分析，并按子仓创建知识库。";
    await refreshWorkspace();
    closeDrawers();
  } catch (error) {
    message.className = "message error";
    message.textContent = errorMessage(error);
  }
});

document.querySelector("#refresh-button").addEventListener("click", () => refreshWorkspace({ includeSettings: true }));
document.querySelector("#add-project-button").addEventListener("click", () => {
  openDrawer("project-drawer");
  selectSource(localStorage.getItem("codeinsight-source-tab") === "remote");
});
document.querySelector("#projects-add-button").addEventListener("click", () => {
  openDrawer("project-drawer");
  selectSource(false);
});
document.querySelector("#terminal-open-project-button").addEventListener("click", () => {
  selectWorkspaceSource(true);
  openDrawer("project-drawer");
  selectSource(true);
});
document.querySelector("#connect-ubuntu-button").addEventListener("click", () => {
  selectWorkspaceSource(true);
  restoreRemoteDraft();
  openDrawer("project-drawer");
  selectSource(true);
});
function formatOperationLogLines(text) {
  if (!text || !String(text).trim()) return "";
  return String(text).split("\n").map((line) => {
    const trimmed = line.trim();
    if (!trimmed) return "";
    try {
      const record = JSON.parse(trimmed);
      if (record && (record.event || record.message)) {
        const extra = record.data && Object.keys(record.data).length
          ? ` ${JSON.stringify(record.data)}`
          : "";
        return `${record.ts || ""} [${record.level || "info"}] ${record.event || ""}: ${record.message || ""}${extra}`.trim();
      }
    } catch {
      /* keep the original line */
    }
    return line;
  }).filter(Boolean).join("\n");
}

async function readOperationLogFromHost() {
  const invoke = window.__TAURI__?.core?.invoke;
  if (!invoke) return "";
  const text = await invoke("read_operation_log", { limit: 200 });
  return formatOperationLogLines(text);
}

async function fetchOperationLogText() {
  const paths = ["/desktop/logs?limit=200", "/desktop/operation-log?limit=200"];
  let lastError = null;
  for (const path of paths) {
    try {
      const payload = await api(path);
      if (payload?.text) return payload.text;
      if (Array.isArray(payload?.events) && payload.events.length) {
        return payload.events.map((record) => {
          const extra = record.data && Object.keys(record.data).length
            ? ` ${JSON.stringify(record.data)}`
            : "";
          return `${record.ts || ""} [${record.level || "info"}] ${record.event || ""}: ${record.message || ""}${extra}`.trim();
        }).filter(Boolean).join("\n");
      }
      return "";
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("读取操作日志失败");
}

async function loadOperationLog() {
  const box = document.querySelector("#operation-log");
  if (!box) return;
  const empty = "暂无操作记录。连接 Ubuntu 或开始分析后会写在这里。";
  try {
    const text = await fetchOperationLogText();
    box.textContent = text || empty;
  } catch (error) {
    try {
      const text = await readOperationLogFromHost();
      box.textContent = text || empty;
    } catch {
      if (error?.status === 404 || /not found/i.test(errorMessage(error))) {
        box.textContent = `${empty}\n\n分析引擎暂未提供日志接口，可点「打开日志目录」查看 operation.log。`;
      } else {
        box.textContent = `读取操作日志失败：${errorMessage(error)}`;
      }
    }
  }
  box.scrollTop = box.scrollHeight;
}

document.querySelector("#settings-button").addEventListener("click", () => {
  openDrawer("settings-drawer");
  loadOperationLog();
});
document.querySelector("#refresh-operation-log-button").addEventListener("click", () => {
  loadOperationLog();
});
document.querySelector("#copy-operation-log-button").addEventListener("click", async () => {
  const text = document.querySelector("#operation-log")?.textContent || "";
  try {
    await navigator.clipboard.writeText(text);
    window.alert("操作日志已复制。发给开发者时请整段粘贴。");
  } catch {
    window.prompt("请复制以下操作日志：", text);
  }
});
document.querySelector("#open-operation-log-button").addEventListener("click", async () => {
  try {
    const invoke = window.__TAURI__?.core?.invoke;
    if (!invoke) throw new Error("日志目录仅可在桌面应用中打开");
    await invoke("open_daemon_log_directory");
  } catch (error) {
    window.alert(errorMessage(error));
  }
});
document.querySelector("#drawer-backdrop").addEventListener("click", () => closeDrawers());
document.querySelectorAll("[data-close-drawer]").forEach((button) => {
  button.addEventListener("click", () => closeDrawers());
});
document.querySelector("#close-result-button").addEventListener("click", () => closeDrawers());

function selectWorkspaceSource(remote) {
  const localTab = document.querySelector("#workspace-local-tab");
  const remoteTab = document.querySelector("#workspace-remote-tab");
  if (!localTab || !remoteTab) return;
  localTab.classList.toggle("active", !remote);
  remoteTab.classList.toggle("active", remote);
  localTab.setAttribute("aria-selected", String(!remote));
  remoteTab.setAttribute("aria-selected", String(remote));
  localTab.tabIndex = remote ? -1 : 0;
  remoteTab.tabIndex = remote ? 0 : -1;
  document.querySelector("#continuous-project-list").hidden = remote;
  document.querySelector("#remote-project-list").hidden = !remote;
  localStorage.setItem("codeinsight-workspace-source", remote ? "remote" : "local");
}

function selectSource(remote) {
  const localTab = document.querySelector("#source-local-tab");
  const remoteTab = document.querySelector("#source-remote-tab");
  localTab.classList.toggle("active", !remote);
  remoteTab.classList.toggle("active", remote);
  localTab.setAttribute("aria-selected", String(!remote));
  remoteTab.setAttribute("aria-selected", String(remote));
  localTab.tabIndex = remote ? -1 : 0;
  remoteTab.tabIndex = remote ? 0 : -1;
  document.querySelector("#source-local-panel").hidden = remote;
  document.querySelector("#source-remote-panel").hidden = !remote;
  localStorage.setItem("codeinsight-source-tab", remote ? "remote" : "local");
  if (remote) {
    restoreRemoteDraft();
    requestAnimationFrame(() => document.querySelector("#remote-host").focus());
  }
}

document.querySelector("#source-local-tab").addEventListener("click", () => selectSource(false));
document.querySelector("#source-remote-tab").addEventListener("click", () => selectSource(true));
document.querySelectorAll("#workspace-source-tabs button").forEach((button) => {
  button.addEventListener("click", () => selectWorkspaceSource(button.dataset.workspaceSource === "remote"));
});
document.querySelector("#deferred-restart-button").addEventListener("click", async () => {
  if (!restartDeferred) return;
  await window.__TAURI__.core.invoke("restart_app");
});
document.querySelectorAll("#task-filters button").forEach((button) => {
  button.addEventListener("click", () => {
    taskFilter = button.dataset.filter;
    document.querySelectorAll("#task-filters button").forEach((item) => {
      item.classList.toggle("active", item === button);
      item.setAttribute("aria-selected", String(item === button));
      item.tabIndex = item === button ? 0 : -1;
    });
    renderTaskList();
  });
});
if (window.__codeInsightDrawerKeydown) {
  document.removeEventListener("keydown", window.__codeInsightDrawerKeydown);
}
window.__codeInsightDrawerKeydown = (event) => {
  const drawer = document.querySelector(".drawer.open");
  if (!drawer) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeDrawers();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = focusableElements(drawer);
  if (!focusable.length) {
    event.preventDefault();
    drawer.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
};
document.addEventListener("keydown", window.__codeInsightDrawerKeydown);

function enableTablistKeyboard(tablist, activate) {
  tablist.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...tablist.querySelectorAll('[role="tab"]')];
    const current = Math.max(0, tabs.indexOf(document.activeElement));
    let next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : current;
    if (event.key === "ArrowLeft") next = (current - 1 + tabs.length) % tabs.length;
    if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
    event.preventDefault();
    tabs[next].focus();
    activate(tabs[next]);
  });
}

enableTablistKeyboard(document.querySelector("#task-filters"), (tab) => tab.click());
enableTablistKeyboard(document.querySelector("#workspace-source-tabs"), (tab) => tab.click());
enableTablistKeyboard(document.querySelector(".drawer-tabs"), (tab) => {
  selectSource(tab.id === "source-remote-tab");
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

async function initializeEngineDiagnostics() {
  const invoke = window.__TAURI__?.core?.invoke;
  const listen = window.__TAURI__?.event?.listen;
  if (invoke) {
    engineLogPath = await invoke("daemon_log_path").catch(() => "");
    desktopAppVersion = await invoke("desktop_app_version").catch(() => "");
    renderAppVersion();
  }
  if (listen) {
    await listen("engine-sidecar", (event) => {
      const payload = event.payload || {};
      engineSidecarState = payload.state || "unknown";
      if (payload.message) engineLastError = payload.message;
      if (payload.state === "started") {
        waitForEngine();
      } else if (["exited", "error", "failed"].includes(payload.state)) {
        engineProbeGeneration += 1;
        const suffix = payload.code == null ? "" : `（退出码 ${payload.code}）`;
        setEngineState(
          "failed",
          `分析引擎进程已停止${suffix}。${payload.message || "请打开日志目录查看详情。"}`,
        );
      }
    });
  }
}

document.querySelector("#retry-engine-button").addEventListener("click", () => {
  engineLastError = "";
  waitForEngine();
});

document.querySelector("#open-engine-logs-button").addEventListener("click", async () => {
  try {
    const invoke = window.__TAURI__?.core?.invoke;
    if (!invoke) throw new Error("日志目录仅可在桌面应用中打开");
    await invoke("open_daemon_log_directory");
  } catch (error) {
    window.alert(errorMessage(error));
  }
});

document.querySelector("#copy-engine-diagnostics-button").addEventListener("click", async () => {
  const diagnostics = [
    `time=${new Date().toISOString()}`,
    `engineState=${engineState}`,
    `sidecarState=${engineSidecarState}`,
    `apiBase=${apiBase}`,
    `desktopVersion=${desktopAppVersion || "unknown"}`,
    `engineVersion=${engineVersion || "unknown"}`,
    `logPath=${engineLogPath || "unknown"}`,
    `lastError=${engineLastError || "none"}`,
    `userAgent=${navigator.userAgent}`,
  ].join("\n");
  let text = diagnostics;
  try {
    const payload = await api("/desktop/logs?limit=80").catch(() => api("/desktop/operation-log?limit=80"));
    text += `\n\n--- operation.log ---\n${payload.text || ""}`;
  } catch (error) {
    try {
      const fileText = await readOperationLogFromHost();
      text += `\n\n--- operation.log ---\n${fileText || errorMessage(error)}`;
    } catch {
      text += `\n\noperation.log: ${errorMessage(error)}`;
    }
  }
  try {
    await navigator.clipboard.writeText(text);
    document.querySelector("#engine-recovery-message").textContent = "诊断信息已复制（不包含令牌或密码）。";
  } catch {
    window.prompt("请复制以下诊断信息：", text);
  }
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
      const current = formatAppVersion(desktopAppVersion) || "未知";
      window.alert(`当前已是最新版本（${current}）。`);
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
      `版本 ${installedVersion} 正在安装。请等待安装程序打开新版本；若仍显示旧版本，请完全退出后从开始菜单打开。`;
  } catch (error) {
    const detail = errorMessage(error);
    try {
      await api("/health", { timeout: 4000 });
      setEngineState("ready");
    } catch (healthError) {
      if (healthError?.status === 401 || healthError?.status === 403) {
        setEngineState("auth", errorMessage(healthError));
      } else {
        waitForEngine();
      }
    }
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

async function loadKnowledgeSpaces() {
  if (engineState !== "ready") return;
  const list = document.querySelector("#knowledge-space-list");
  const select = document.querySelector("#active-space");
  try {
    latestKnowledgeSpaces = await api("/knowledge/spaces");
    const groups = new Map();
    latestKnowledgeSpaces.forEach((space) => {
      const key = space.parent_workspace;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(space);
    });
    select.replaceChildren();
    if (!latestKnowledgeSpaces.length) {
      renderEmptyState(list, "添加项目后会按子仓分类知识库。", "每个工作目录或子仓对应一个可切换的知识空间。");
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "尚未生成知识库";
      select.append(option);
      return;
    }
    list.replaceChildren();
    if (!latestKnowledgeSpaces.some((space) => space.space_id === activeSpaceId)) {
      activeSpaceId = latestKnowledgeSpaces[0].space_id;
      localStorage.setItem("codeinsight-active-space", activeSpaceId);
    }
    for (const [parent, spaces] of groups) {
      const heading = document.createElement("p");
      heading.className = "list-group-label";
      heading.textContent = parent;
      list.append(heading);
      spaces.forEach((space) => {
        const option = document.createElement("option");
        option.value = space.space_id;
        option.textContent = space.label;
        if (space.space_id === activeSpaceId) option.selected = true;
        select.append(option);
        const card = document.createElement("button");
        card.type = "button";
        card.className = `continuous-project ${space.space_id === activeSpaceId ? "active-space" : ""}`;
        const body = document.createElement("div");
        const title = document.createElement("h3");
        title.textContent = space.label;
        const path = document.createElement("p");
        path.className = "card-path";
        path.textContent = space.included_dirs.join(", ") || "整个工作目录";
        body.append(title, path);
        card.append(body);
        card.addEventListener("click", () => {
          activeSpaceId = space.space_id;
          localStorage.setItem("codeinsight-active-space", activeSpaceId);
          loadKnowledgeSpaces();
        });
        list.append(card);
      });
    }
  } catch (error) {
    renderListError(list, "知识库读取失败", error);
  }
}

function appendAskMessage(role, text) {
  const box = document.querySelector("#ask-messages");
  const item = document.createElement("div");
  item.className = `ask-message ${role}`;
  item.textContent = text;
  box.append(item);
  box.scrollTop = box.scrollHeight;
  return item;
}

document.querySelector("#active-space").addEventListener("change", (event) => {
  activeSpaceId = event.target.value;
  localStorage.setItem("codeinsight-active-space", activeSpaceId);
  loadKnowledgeSpaces();
});

document.querySelector("#detect-subdirs-button").addEventListener("click", async () => {
  const path = document.querySelector("#project-path").value.trim();
  const list = document.querySelector("#subdir-list");
  if (!path) {
    list.innerHTML = '<p class="empty">请先填写工作目录。</p>';
    return;
  }
  list.innerHTML = '<p class="empty">正在扫描…</p>';
  try {
    const detected = await api("/knowledge/spaces/detect", {
      method: "POST",
      body: JSON.stringify({ workspace_root: path }),
    });
    list.replaceChildren();
    if (!detected.candidates.length) {
      list.innerHTML = '<p class="empty">未发现子仓，将分析整个工作目录。</p>';
      return;
    }
    detected.candidates.forEach((candidate) => {
      const label = document.createElement("label");
      label.className = "checkbox";
      label.innerHTML = `<input type="checkbox" value="${candidate.path}" />${candidate.label}（${candidate.kind}）`;
      list.append(label);
    });
  } catch (error) {
    list.innerHTML = `<p class="message error">${errorMessage(error)}</p>`;
  }
});

document.querySelector("#discover-models-button").addEventListener("click", async () => {
  const message = document.querySelector("#model-message");
  try {
    message.className = "message";
    message.textContent = "正在获取模型列表…";
    const result = await api("/desktop/models/discover", {
      method: "POST",
      timeout: 60000,
      body: JSON.stringify({
        base_url: document.querySelector("#model-base-url").value.trim(),
        api_key: document.querySelector("#model-api-key").value.trim() || null,
      }),
    });
    const list = document.querySelector("#model-id-options");
    if (list) list.replaceChildren();
    (result.models || []).forEach((model) => ensureModelOption(model.id));
    if (savedModelId) ensureModelOption(savedModelId);
    else if (result.models?.[0]) ensureModelOption(result.models[0].id);
    message.textContent = `已获取 ${result.models?.length || 0} 个模型`;
  } catch (error) {
    message.className = "message error";
    message.textContent = errorMessage(error);
  }
});

document.querySelector("#test-model-button").addEventListener("click", async () => {
  const message = document.querySelector("#model-message");
  try {
    message.className = "message";
    message.textContent = "正在测试连接…";
    await api("/desktop/models/test", {
      method: "POST",
      timeout: 60000,
      body: JSON.stringify({
        base_url: document.querySelector("#model-base-url").value.trim(),
        api_key: document.querySelector("#model-api-key").value.trim() || null,
        model: document.querySelector("#model-id").value.trim(),
      }),
    });
    message.className = "message";
    message.textContent = "连接成功，此 API 可以用于分析。";
    await loadModelSettings();
  } catch (error) {
    message.className = "message error";
    message.textContent = `${errorMessage(error)}。此 API 当前不能用于分析。`;
    await loadModelSettings();
  }
});

document.querySelector("#result-tabs").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-result-tab]");
  if (!tab) return;
  resultTab = tab.dataset.resultTab;
  document.querySelectorAll("#result-tabs [data-result-tab]").forEach((button) => {
    button.classList.toggle("active", button === tab);
    button.setAttribute("aria-selected", button === tab ? "true" : "false");
  });
  document.querySelector("#result-pages").hidden = resultTab !== "docs";
  document.querySelector("#ask-panel").hidden = resultTab !== "ask";
});

document.querySelector("#ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = document.querySelector("#ask-input").value.trim();
  const message = document.querySelector("#result-message");
  if (!question) return;
  if (!activeSpaceId) {
    message.className = "message error";
    message.textContent = "请先选择知识库";
    return;
  }
  appendAskMessage("user", question);
  const answer = appendAskMessage("assistant", "正在检索知识库…");
  document.querySelector("#ask-input").value = "";
  try {
    const invoke = window.__TAURI__?.core?.invoke;
    if (!desktopToken && invoke) {
      desktopToken = await invoke("desktop_session_token");
    }
    const response = await fetch(`${apiBase}/knowledge/ask`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CodeInsight-Token": desktopToken || "",
      },
      body: JSON.stringify({
        space_id: activeSpaceId,
        question,
        provider: savedModelProvider,
        model: savedModelId || null,
        language: "zh",
      }),
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    answer.textContent = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      answer.textContent += decoder.decode(value, { stream: true });
    }
  } catch (error) {
    answer.textContent = errorMessage(error);
  }
});

const WORKBENCH_WIDTHS_KEY = "codeinsight-workbench-widths";
const WORKBENCH_MIN_PX = 200;

function defaultWorkbenchWidths() {
  return [1 / 3, 1 / 3, 1 / 3];
}

function workbenchStacked() {
  return Boolean(window.matchMedia?.("(max-width: 1100px)")?.matches);
}

function readWorkbenchWidths() {
  try {
    const parsed = JSON.parse(localStorage.getItem(WORKBENCH_WIDTHS_KEY) || "null");
    if (
      Array.isArray(parsed)
      && parsed.length === 3
      && parsed.every((value) => typeof value === "number" && value > 0)
    ) {
      const total = parsed[0] + parsed[1] + parsed[2];
      return parsed.map((value) => value / total);
    }
  } catch {
    /* use equal columns */
  }
  return defaultWorkbenchWidths();
}

function applyWorkbenchWidths(fractions) {
  const cols = document.querySelectorAll("#workbench .workbench-col");
  if (cols.length !== 3) return;
  cols.forEach((col, index) => {
    col.style.flexGrow = String(Math.max(fractions[index], 0.05) * 1000);
    col.style.flexShrink = "1";
    col.style.flexBasis = "0";
    col.style.minWidth = `${WORKBENCH_MIN_PX}px`;
    col.style.width = "";
  });
}

function persistWorkbenchWidths() {
  const cols = [...document.querySelectorAll("#workbench .workbench-col")];
  if (cols.length !== 3) return;
  const widths = cols.map((col) => col.getBoundingClientRect().width);
  const total = widths.reduce((sum, width) => sum + width, 0);
  if (total <= 0) return;
  localStorage.setItem(WORKBENCH_WIDTHS_KEY, JSON.stringify(widths.map((width) => width / total)));
}

function resetStackedWorkbench() {
  document.querySelectorAll("#workbench .workbench-col").forEach((col) => {
    col.style.flexGrow = "";
    col.style.flexShrink = "";
    col.style.flexBasis = "";
    col.style.width = "";
    col.style.minWidth = "";
  });
}

function initWorkbenchResize() {
  const workbench = document.querySelector("#workbench");
  if (!workbench) return;
  const apply = () => {
    if (workbenchStacked()) {
      resetStackedWorkbench();
      return;
    }
    applyWorkbenchWidths(readWorkbenchWidths());
  };
  apply();
  window.addEventListener("resize", apply);
  workbench.querySelectorAll(".workbench-splitter").forEach((splitter, splitterIndex) => {
    const startDrag = (clientX) => {
      if (workbenchStacked()) return;
      const cols = [...workbench.querySelectorAll(".workbench-col")];
      const left = cols[splitterIndex];
      const right = cols[splitterIndex + 1];
      if (!left || !right) return;
      const startLeft = left.getBoundingClientRect().width;
      const startRight = right.getBoundingClientRect().width;
      document.body.classList.add("workbench-resizing");
      workbench.classList.add("is-resizing");
      const onMove = (event) => {
        const point = event.touches ? event.touches[0] : event;
        if (!point) return;
        if (event.cancelable) event.preventDefault();
        let nextLeft = startLeft + (point.clientX - clientX);
        let nextRight = startRight - (point.clientX - clientX);
        if (nextLeft < WORKBENCH_MIN_PX) {
          nextRight -= WORKBENCH_MIN_PX - nextLeft;
          nextLeft = WORKBENCH_MIN_PX;
        }
        if (nextRight < WORKBENCH_MIN_PX) {
          nextLeft -= WORKBENCH_MIN_PX - nextRight;
          nextRight = WORKBENCH_MIN_PX;
        }
        if (nextLeft < WORKBENCH_MIN_PX || nextRight < WORKBENCH_MIN_PX) return;
        left.style.flexGrow = "0";
        right.style.flexGrow = "0";
        left.style.flexShrink = "0";
        right.style.flexShrink = "0";
        left.style.flexBasis = `${nextLeft}px`;
        right.style.flexBasis = `${nextRight}px`;
        left.style.width = `${nextLeft}px`;
        right.style.width = `${nextRight}px`;
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.removeEventListener("touchmove", onMove);
        document.removeEventListener("touchend", onUp);
        document.body.classList.remove("workbench-resizing");
        workbench.classList.remove("is-resizing");
        persistWorkbenchWidths();
        apply();
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      document.addEventListener("touchmove", onMove, { passive: false });
      document.addEventListener("touchend", onUp);
    };
    splitter.addEventListener("mousedown", (event) => {
      event.preventDefault();
      startDrag(event.clientX);
    });
    splitter.addEventListener("touchstart", (event) => {
      if (event.touches[0]) startDrag(event.touches[0].clientX);
    }, { passive: true });
    splitter.addEventListener("keydown", (event) => {
      if (workbenchStacked()) return;
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const cols = [...workbench.querySelectorAll(".workbench-col")];
      const left = cols[splitterIndex];
      const right = cols[splitterIndex + 1];
      if (!left || !right) return;
      const step = event.shiftKey ? 48 : 20;
      const delta = event.key === "ArrowLeft" ? -step : step;
      const nextLeft = Math.max(WORKBENCH_MIN_PX, left.getBoundingClientRect().width + delta);
      const nextRight = Math.max(WORKBENCH_MIN_PX, right.getBoundingClientRect().width - delta);
      if (nextLeft === WORKBENCH_MIN_PX && delta < 0) return;
      if (nextRight === WORKBENCH_MIN_PX && delta > 0) return;
      left.style.flexGrow = "0";
      right.style.flexGrow = "0";
      left.style.flexBasis = `${nextLeft}px`;
      right.style.flexBasis = `${nextRight}px`;
      persistWorkbenchWidths();
      apply();
    });
  });
}

document.querySelector("#ollama-tier").value =
  localStorage.getItem("codeinsight-ollama-tier") || "auto";
selectWorkspaceSource(localStorage.getItem("codeinsight-workspace-source") === "remote");
updateModelForm();
restoreRemoteDraft();
initWorkbenchResize();
initializeUpdateProgress();
initializeEngineDiagnostics().finally(waitForEngine);
setInterval(refreshWorkspace, 4000);
setInterval(loadOllamaStatus, 2000);
