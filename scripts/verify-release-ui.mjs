import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import assert from "node:assert/strict";
import { createContext, runInContext } from "node:vm";

const root = resolve(import.meta.dirname, "..");
const html = readFileSync(resolve(root, "desktop-ui/index.html"), "utf8");
const script = readFileSync(resolve(root, "desktop-ui/app.js"), "utf8");
const rust = readFileSync(resolve(root, "src-tauri/src/lib.rs"), "utf8");

function sliceFn(name, nextName) {
  const start = script.indexOf(name);
  const end = script.indexOf(nextName, start + 1);
  assert.ok(start >= 0, `missing ${name}`);
  assert.ok(end > start, `missing ${nextName} after ${name}`);
  return script.slice(start, end);
}

assert.match(html, /<textarea id="operation-log"/);
assert.match(html, /id="copy-operation-log-button"/);
assert.match(html, /id="workbench-splitter-1"/);
assert.match(html, /id="workbench-splitter-2"/);
assert.match(html, /id="remote-scope-detect-button"/);
assert.doesNotMatch(script, /window\.prompt/);
assert.match(script, /function copyTextToClipboard/);
assert.match(script, /invoke\("write_clipboard"/);
assert.match(script, /function detectRemoteScopeCandidates/);
assert.match(script, /\/desktop\/logs/);
assert.match(script, /\/desktop\/operation-log/);
assert.match(script, /\/detect-scopes/);
assert.match(rust, /fn write_clipboard/);
assert.match(rust, /fn read_operation_log/);
assert.match(rust, /fn tail_text_file/);

const written = [];
const created = [];
const copySource = sliceFn("async function copyTextToClipboard", "async function loadOperationLog");
const window = {
  __TAURI__: {
    core: {
      invoke: async (command, payload) => {
        assert.equal(command, "write_clipboard");
        written.push(payload.text);
      },
    },
  },
};
const navigator = {
  clipboard: {
    writeText: async () => {
      throw new Error("blocked");
    },
  },
};
const document = {
  createElement() {
    const node = {
      value: "",
      style: {},
      setAttribute() {},
      focus() {},
      select() {},
      setSelectionRange() {},
      remove() {},
    };
    created.push(node);
    return node;
  },
  body: {
    append(node) {
      created.push(["append", node.value]);
    },
  },
  execCommand(cmd) {
    created.push(["exec", cmd]);
    return true;
  },
  querySelector() {
    return { value: "box-log", select() {} };
  },
};

// Node 22+ exposes a read-only global navigator; run the helper in a VM sandbox.
const sandbox = createContext({ window, navigator, document });
const copyTextToClipboard = runInContext(
  `${copySource}; copyTextToClipboard`,
  sandbox,
);
await copyTextToClipboard("本机验证：同步完成\n第二行");
assert.deepEqual(written, ["本机验证：同步完成\n第二行"]);

window.__TAURI__ = undefined;
await copyTextToClipboard("exec-fallback-log");
assert.ok(
  created.some((item) => Array.isArray(item) && item[0] === "exec" && item[1] === "copy"),
  "execCommand fallback was not used",
);

const formatSource = sliceFn("function formatOperationLogLines", "async function readOperationLogFromHost");
const formatOperationLogLines = new Function(`${formatSource}; return formatOperationLogLines;`)();
assert.match(
  formatOperationLogLines(
    JSON.stringify({
      ts: "2026-09-10T19:00:00",
      level: "info",
      event: "sync_ok",
      message: "同步完成",
    }),
  ),
  /sync_ok: 同步完成/,
);

console.log("verify-release-ui: ok");
