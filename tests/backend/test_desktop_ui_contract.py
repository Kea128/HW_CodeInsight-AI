from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_ubuntu_form_is_reachable_without_ai_configuration():
    html = (ROOT / "desktop-ui" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "desktop-ui" / "app.js").read_text(encoding="utf-8")

    assert 'id="app-version"' in html
    assert 'id="settings-app-version"' in html
    assert 'id="connect-ubuntu-button"' in html
    assert 'value="openai_compatible"' in html
    assert 'id="model-base-url"' in html
    assert "自定义接口通常没有 text-embedding-3-small" in html
    assert 'id="embedder-mode"' in html
    assert 'id="operation-log"' in html
    assert "<textarea id=\"operation-log\"" in html or "<textarea id='operation-log'" in html
    assert 'id="copy-operation-log-button"' in html
    assert "function copyTextToClipboard" in script
    assert 'invoke("write_clipboard"' in script
    assert "window.prompt" not in script
    assert "/desktop/logs" in script
    assert "/desktop/operation-log" in script
    assert "read_operation_log" in script
    assert 'id="workbench"' in html
    assert 'id="workbench-splitter-1"' in html
    assert 'id="workbench-splitter-2"' in html
    assert "codeinsight-workbench-widths" in script
    assert 'id="knowledge-space-list"' in html
    assert 'id="task-list"' in html
    assert "function taskLocation" in script
    assert "card-path" in script
    assert 'id="ask-form"' in html
    assert "/knowledge/spaces" in script
    assert "/desktop/models/discover" in script
    assert "progress_message" in script
    assert "probe_status" in script
    assert "remoteProgressText" in script
    assert "codeinsight-remote-draft" in script
    assert "开始分析" in script
    assert "分析整个根目录" in script
    assert "添加子分析" in script
    assert 'id="remote-scope-drawer"' in html
    assert 'id="remote-scope-form"' in html
    assert 'id="remote-scope-detect-button"' in html
    assert 'id="remote-scope-list"' in html
    assert 'id="remote-scope-custom"' in html
    assert "/remote/projects/${encodeURIComponent(project.id)}/analyze" in script
    assert "/scopes/detect" in script
    assert "/detect-scopes" in script
    assert "function detectRemoteScopeCandidates" in script
    assert "/scopes/" in script
    assert 'timeout: 45000' in script
    assert 'timeout: 60000' in script
    submit_handler = script.split(
        'document.querySelector("#remote-form").addEventListener("submit"', maxsplit=1
    )[1].split(
        'document.querySelector("#remote-form").addEventListener("input"', maxsplit=1
    )[0]
    assert "/remote/fingerprint" in submit_handler
    assert "remembered.value !== probe.fingerprint" in submit_handler
    assert "savedModelProvider" not in submit_handler
    assert "savedModelId" not in submit_handler
    assert "analyze_now" not in submit_handler
    assert "modelConfigured" not in submit_handler
    assert "连接并同步" in html
    write_draft = script.split("function writeRemoteDraft()", maxsplit=1)[1].split(
        "function rememberRemoteFingerprint", maxsplit=1
    )[0]
    assert "password" not in write_draft
    assert 'id="ai-usability-hint"' in html
    assert 'id="remote-host"' in html
    assert 'id="source-remote-tab" type="button"' in html
    assert 'document.querySelector("#connect-ubuntu-button")' in script
    assert "selectSource(true)" in script

    add_project_handler = script.split(
        'document.querySelector("#add-project-button").addEventListener', maxsplit=1
    )[1].split(
        'document.querySelector("#connect-ubuntu-button").addEventListener', maxsplit=1
    )[0]
    assert "modelConfigured" not in add_project_handler

    scope_handler = script.split(
        'document.querySelector("#remote-scope-form").addEventListener("submit"',
        maxsplit=1,
    )[1].split(
        'document.querySelector("#project-form").addEventListener("submit"',
        maxsplit=1,
    )[0]
    assert "savedModelProvider" not in scope_handler
    assert "analyze_now" not in scope_handler
    assert "included_dirs: included" in scope_handler


def test_terminal_auth_token_is_sent_as_subprotocol_not_url_or_message():
    script = (ROOT / "desktop-ui" / "terminal-ui.js").read_text(encoding="utf-8")

    assert "`codeinsight.${token}`" in script
    assert "?token=" not in script
    assert "\n        token," not in script
    assert 'localStorage.getItem("codeinsight-api-base")' in script


def test_engine_recovery_uses_health_backoff_and_safe_diagnostics():
    html = (ROOT / "desktop-ui" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "desktop-ui" / "app.js").read_text(encoding="utf-8")

    assert 'id="engine-recovery"' in html
    assert 'id="retry-engine-button"' in html
    assert 'id="open-engine-logs-button"' in html
    assert 'id="copy-engine-diagnostics-button"' in html
    assert 'api("/health", { timeout: 4000 })' in script
    assert "performance.now() - startedAt < 90000" in script
    assert "Math.min(delay * 2, 5000)" in script
    assert 'setEngineState("auth"' in script
    assert "desktopToken" not in script.split("const diagnostics = [", 1)[1].split(
        "].join", 1
    )[0]
