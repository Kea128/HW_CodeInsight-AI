import { FitAddon } from "./vendor/addon-fit.mjs";
import { Terminal } from "./vendor/xterm.mjs";

const panel = document.querySelector("#terminal-panel");
const tabsElement = document.querySelector("#terminal-tabs");
const viewsElement = document.querySelector("#terminal-views");
const statusElement = document.querySelector("#terminal-status");
const reconnectButton = document.querySelector("#terminal-reconnect-button");
const collapseButton = document.querySelector("#terminal-collapse-button");
const resizeHandle = document.querySelector("#terminal-resize-handle");
const sessions = new Map();
let activeId = null;
let desktopToken = null;

async function getDesktopToken() {
  if (!desktopToken) {
    desktopToken = await window.__TAURI__.core.invoke("desktop_session_token");
  }
  return desktopToken;
}

function setStatus(text, failed = false) {
  statusElement.textContent = text;
  statusElement.classList.toggle("failed", failed);
}

function activateSession(id) {
  activeId = id;
  sessions.forEach((session, sessionId) => {
    session.tab.classList.toggle("active", sessionId === id);
    session.tab.setAttribute("aria-selected", String(sessionId === id));
    session.tab.tabIndex = sessionId === id ? 0 : -1;
    session.view.hidden = sessionId !== id;
  });
  const session = sessions.get(id);
  if (session) {
    requestAnimationFrame(() => {
      session.fit.fit();
      session.terminal.focus();
    });
    setStatus(session.connected ? "已连接" : "连接已断开", !session.connected);
    reconnectButton.hidden = session.connected;
  }
}

function closeSession(id) {
  const session = sessions.get(id);
  if (!session) return;
  session.connectionGeneration += 1;
  session.socket?.close();
  session.terminal.dispose();
  session.tab.remove();
  session.view.remove();
  sessions.delete(id);
  if (activeId === id) {
    const next = sessions.keys().next().value;
    if (next) activateSession(next);
  }
  if (!sessions.size) {
    panel.hidden = true;
    document.body.classList.remove("terminal-open");
    document.documentElement.style.removeProperty("--terminal-height");
    activeId = null;
  }
}

async function connectSession(session) {
  const generation = session.connectionGeneration + 1;
  session.connectionGeneration = generation;
  session.socket?.close();
  session.connected = false;
  setStatus("正在连接…");
  reconnectButton.hidden = true;
  const token = await getDesktopToken();
  const apiBase = localStorage.getItem("codeinsight-api-base") || "http://127.0.0.1:8001";
  const socketUrl = new URL("/ws/terminal", apiBase);
  socketUrl.protocol = socketUrl.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(socketUrl, [`codeinsight.${token}`]);
  socket.binaryType = "arraybuffer";
  session.socket = socket;

  socket.addEventListener("open", () => {
    if (session.connectionGeneration !== generation) return;
    socket.send(
      JSON.stringify({
        type: "open",
        project_id: session.project.id,
        columns: session.terminal.cols,
        rows: session.terminal.rows,
      }),
    );
  });
  socket.addEventListener("message", (event) => {
    if (session.connectionGeneration !== generation) return;
    if (typeof event.data === "string") {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        session.terminal.writeln("\r\n\x1b[31m终端服务器返回了无效消息\x1b[0m");
        return;
      }
      if (message.type === "ready") {
        session.connected = true;
        session.terminal.writeln("\x1b[32m已安全连接 Ubuntu 终端\x1b[0m");
        if (activeId === session.id) setStatus("已连接");
      } else if (message.type === "error") {
        session.terminal.writeln(`\r\n\x1b[31m${message.message}\x1b[0m`);
        if (activeId === session.id) setStatus("连接失败", true);
      }
      return;
    }
    session.terminal.write(new Uint8Array(event.data));
  });
  socket.addEventListener("close", () => {
    if (session.connectionGeneration !== generation) return;
    session.connected = false;
    session.terminal.writeln("\r\n\x1b[33m连接已断开，可点击重连。\x1b[0m");
    if (activeId === session.id) {
      setStatus("连接已断开", true);
      reconnectButton.hidden = false;
    }
  });
  socket.addEventListener("error", () => {
    if (session.connectionGeneration !== generation) return;
    if (activeId === session.id) setStatus("终端连接错误", true);
  });
}

function createSession(project) {
  const existing = [...sessions.values()].find(
    (session) => session.project.id === project.id,
  );
  if (existing) {
    activateSession(existing.id);
    return;
  }
  const id = crypto.randomUUID();
  const terminal = new Terminal({
    cursorBlink: true,
    convertEol: false,
    fontFamily: '"Cascadia Code", Consolas, monospace',
    fontSize: 13,
    lineHeight: 1.15,
    scrollback: 5000,
    theme: {
      background: "#070b11",
      foreground: "#dce7f5",
      cursor: "#6ed8ff",
      selectionBackground: "#31537d88",
    },
  });
  const fit = new FitAddon();
  terminal.loadAddon(fit);

  const tab = document.createElement("div");
  tab.className = "terminal-tab";
  tab.setAttribute("role", "tab");
  tab.setAttribute("aria-selected", "false");
  tab.tabIndex = -1;
  const label = document.createElement("span");
  label.textContent = `${project.username}@${project.host}`;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "close";
  close.setAttribute("aria-label", "关闭终端标签");
  close.textContent = "×";
  close.addEventListener("click", (event) => {
    event.stopPropagation();
    closeSession(id);
  });
  tab.append(label, close);
  tab.addEventListener("click", () => activateSession(id));
  tab.addEventListener("keydown", (event) => {
    if (event.target !== tab || !["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    activateSession(id);
  });

  const view = document.createElement("div");
  view.className = "terminal-view";
  view.setAttribute("role", "tabpanel");
  view.hidden = true;
  const viewId = `terminal-view-${id}`;
  const tabId = `terminal-tab-${id}`;
  view.id = viewId;
  tab.id = tabId;
  tab.setAttribute("aria-controls", viewId);
  view.setAttribute("aria-labelledby", tabId);
  tabsElement.append(tab);
  viewsElement.append(view);
  terminal.open(view);

  const session = {
    id,
    project,
    terminal,
    fit,
    tab,
    view,
    socket: null,
    connected: false,
    connectionGeneration: 0,
  };
  sessions.set(id, session);
  terminal.onData((data) => {
    if (session.socket?.readyState === WebSocket.OPEN && session.connected) {
      session.socket.send(new TextEncoder().encode(data));
    }
  });
  terminal.onResize(({ cols, rows }) => {
    if (session.socket?.readyState === WebSocket.OPEN && session.connected) {
      session.socket.send(
        JSON.stringify({ type: "resize", columns: cols, rows }),
      );
    }
  });

  panel.hidden = false;
  document.body.classList.add("terminal-open");
  panel.classList.remove("collapsed");
  collapseButton.textContent = "收起";
  activateSession(id);
  requestAnimationFrame(() => fit.fit());
  connectSession(session).catch((error) => {
    terminal.writeln(`\x1b[31m${error.message || error}\x1b[0m`);
    setStatus("连接失败", true);
    reconnectButton.hidden = false;
  });
}

window.addEventListener("codeinsight:open-terminal", (event) => {
  createSession(event.detail);
});
window.addEventListener("codeinsight:close-project-terminals", (event) => {
  [...sessions.values()]
    .filter((session) => session.project.id === event.detail)
    .forEach((session) => closeSession(session.id));
});
window.addEventListener("resize", () => {
  const session = sessions.get(activeId);
  if (session && !panel.classList.contains("collapsed")) session.fit.fit();
});
const panelResizeObserver = new ResizeObserver(() => {
  if (!panel.hidden) {
    document.documentElement.style.setProperty("--terminal-height", `${panel.offsetHeight}px`);
  }
});
panelResizeObserver.observe(panel);
function setPanelHeight(height) {
  const clamped = Math.max(160, Math.min(window.innerHeight * .8, height));
  panel.style.height = `${clamped}px`;
}
resizeHandle.addEventListener("pointerdown", (event) => {
  if (panel.classList.contains("collapsed")) return;
  const startY = event.clientY;
  const startHeight = panel.offsetHeight;
  resizeHandle.setPointerCapture(event.pointerId);
  const move = (moveEvent) => setPanelHeight(startHeight + startY - moveEvent.clientY);
  const stop = () => {
    resizeHandle.removeEventListener("pointermove", move);
    resizeHandle.removeEventListener("pointerup", stop);
    resizeHandle.removeEventListener("pointercancel", stop);
  };
  resizeHandle.addEventListener("pointermove", move);
  resizeHandle.addEventListener("pointerup", stop);
  resizeHandle.addEventListener("pointercancel", stop);
});
resizeHandle.addEventListener("keydown", (event) => {
  if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
  event.preventDefault();
  setPanelHeight(panel.offsetHeight + (event.key === "ArrowUp" ? 24 : -24));
});
tabsElement.addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const ids = [...sessions.keys()];
  const current = Math.max(0, ids.indexOf(activeId));
  let next = event.key === "Home" ? 0 : event.key === "End" ? ids.length - 1 : current;
  if (event.key === "ArrowLeft") next = (current - 1 + ids.length) % ids.length;
  if (event.key === "ArrowRight") next = (current + 1) % ids.length;
  event.preventDefault();
  activateSession(ids[next]);
});
reconnectButton.addEventListener("click", () => {
  const session = sessions.get(activeId);
  if (session) connectSession(session);
});
collapseButton.addEventListener("click", () => {
  panel.classList.toggle("collapsed");
  collapseButton.textContent = panel.classList.contains("collapsed") ? "展开" : "收起";
  if (!panel.classList.contains("collapsed")) {
    requestAnimationFrame(() => sessions.get(activeId)?.fit.fit());
  }
  requestAnimationFrame(() => {
    document.documentElement.style.setProperty("--terminal-height", `${panel.offsetHeight}px`);
  });
});
