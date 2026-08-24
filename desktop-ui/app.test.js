import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const html = readFileSync(resolve(root, "desktop-ui/index.html"), "utf8");
const appSource = readFileSync(resolve(root, "desktop-ui/app.js"), "utf8");

const tasks = [{
  id: "failed-task",
  owner: "local",
  repo: "demo",
  repo_type: "local",
  language: "zh",
  status: "failed",
  pages_done: 1,
  pages_total: 4,
  error: "模型连接失败",
  submitted_at: 1_700_000_000,
}];

function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(body),
  };
}

function route(url, options = {}) {
  const path = new URL(url).pathname;
  if (path === "/health") return response({ status: "ok" });
  if (path === "/wiki/tasks" && options.method === "POST") {
    return response({ task_id: "retry", status: "pending", created: true });
  }
  if (path === "/wiki/tasks") return response(tasks);
  if (path === "/wiki/tasks/failed-task") return response(tasks[0]);
  if (path === "/remote/projects") return response([]);
  if (path === "/continuous/projects") {
    return response([{
      id: "local-demo",
      enabled: true,
      poll_seconds: 15,
      last_task_id: "failed-task",
      request: {
        owner: "local",
        repo: "demo",
        type: "local",
        repo_url: "D:\\demo",
        language: "zh",
        provider: "ollama",
        comprehensive: true,
      },
    }]);
  }
  if (path === "/desktop/settings") {
    return response({ provider: "ollama", configured: true, ollama_tier: "balanced" });
  }
  if (path === "/desktop/ollama/status") {
    return response({ state: "ready", ready: true, selected_tier: "balanced", tiers: [] });
  }
  return response({ detail: "not found" }, 404);
}

async function boot() {
  document.open();
  document.write(html.replace(/<script[\s\S]*?<\/script>/g, ""));
  document.close();
  window.alert = vi.fn();
  window.confirm = vi.fn(() => true);
  window.fetch = vi.fn(route);
  window.__TAURI__ = undefined;
  new Function(appSource)();
  await vi.waitFor(() => {
    expect(document.querySelector("#engine-status").textContent).toContain("已就绪");
  });
}

describe("desktop workspace UI", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    document.body.replaceChildren();
  });

  it("distinguishes the saved provider from an unsaved draft", async () => {
    await boot();
    document.querySelector("#settings-button").click();
    const provider = document.querySelector("#model-provider");
    provider.value = "openai";
    provider.dispatchEvent(new Event("change", { bubbles: true }));

    expect(document.querySelector("#provider-state").textContent).toContain("草稿");
    expect(document.querySelector("#provider-state").textContent).toContain("ollama");
    expect(localStorage.getItem("codeinsight-model-provider")).toBe("ollama");
  });

  it("supports keyboard tab selection and dialog focus return", async () => {
    await boot();
    const trigger = document.querySelector("#add-project-button");
    trigger.focus();
    trigger.click();
    const localTab = document.querySelector("#source-local-tab");
    localTab.focus();
    localTab.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));

    expect(document.querySelector("#source-remote-tab").getAttribute("aria-selected")).toBe("true");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(document.activeElement).toBe(trigger);
  });

  it("renders failed task details and a working retry action", async () => {
    await boot();
    await vi.waitFor(() => {
      expect(document.querySelector("#task-list").textContent).toContain("模型连接失败");
    });
    const buttons = [...document.querySelectorAll("#task-list button")];
    buttons.find((button) => button.textContent === "详情").click();
    await vi.waitFor(() => {
      expect(document.querySelector("#result-pages").textContent).toContain("状态：failed");
    });
    document.querySelector("#close-result-button").click();
    buttons.find((button) => button.textContent === "重试").click();
    await vi.waitFor(() => {
      expect(window.fetch).toHaveBeenCalledWith(
        "http://127.0.0.1:8001/wiki/tasks",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });
});
