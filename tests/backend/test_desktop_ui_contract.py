from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_ubuntu_form_is_reachable_without_ai_configuration():
    html = (ROOT / "desktop-ui" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "desktop-ui" / "app.js").read_text(encoding="utf-8")

    assert 'id="connect-ubuntu-button"' in html
    assert 'value="openai_compatible"' in html
    assert 'id="model-base-url"' in html
    assert 'id="knowledge-space-list"' in html
    assert 'id="ask-form"' in html
    assert "/knowledge/spaces" in script
    assert "/desktop/models/discover" in script
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
