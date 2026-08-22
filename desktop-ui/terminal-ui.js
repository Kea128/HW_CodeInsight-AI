import { FitAddon } from "./vendor/addon-fit.mjs";
import { Terminal } from "./vendor/xterm.mjs";

const panel = document.querySelector("#terminal-panel");
const tabsElement = document.querySelector("#terminal-tabs");
const viewsElement = document.querySelector("#terminal-views");
const statusElement = document.querySelector("#terminal-status");
const reconnectButton = document.querySelector("#terminal-reconnect-button");
const collapseButton = document.querySelector("#terminal-collapse-button");
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
    activeId = null;
  }
}

async function connectSession(session) {
  session.socket?.close();
  session.connected = false;
  setStatus("正在连接…");
  reconnectButton.hidden = true;
  const token = await getDesktopToken();
  const apiBase = localStorage.getItem("codeinsight-api-base") || "http://127.0.0.1:8001";
  const socketUrl = new URL("/ws/terminal", apiBase);
  socketUrl.protocol = socketUrl.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(socketUrl);
  socket.binaryType = "arraybuffer";
  session.socket = socket;

  socket.addEventListener("open", () => {
    socket.send(
      JSON.stringify({
        type: "open",
        token,
        project_id: session.project.id,
        columns: session.terminal.cols,
        rows: session.terminal.rows,
      }),
    );
  });
  socket.addEventListener("message", (event) => {
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
    session.connected = false;
    session.terminal.writeln("\r\n\x1b[33m连接已断开，可点击重连。\x1b[0m");
    if (activeId === session.id) {
      setStatus("连接已断开", true);
      reconnectButton.hidden = false;
    }
  });
  socket.addEventListener("error", () => {
    if (activeId === session.id) setStatus("终端连接错误", true);
  });
}

function createSession(project) {
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
  const label = document.createElement("span");
  label.textContent = `${project.username}@${project.host}`;
  const close = document.createElement("span");
  close.className = "close";
  close.textContent = "×";
  close.addEventListener("click", (event) => {
    event.stopPropagation();
    closeSession(id);
  });
  tab.append(label, close);
  tab.addEventListener("click", () => activateSession(id));

  const view = document.createElement("div");
  view.className = "terminal-view";
  view.hidden = true;
  tabsElement.append(tab);
  viewsElement.append(view);
  terminal.open(view);

  const session = { id, project, terminal, fit, tab, view, socket: null, connected: false };
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
});
