from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_ubuntu_form_is_reachable_without_ai_configuration():
    html = (ROOT / "desktop-ui" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "desktop-ui" / "app.js").read_text(encoding="utf-8")

    assert 'id="connect-ubuntu-button"' in html
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


def test_terminal_auth_token_is_sent_in_handshake_not_url():
    script = (ROOT / "desktop-ui" / "terminal-ui.js").read_text(encoding="utf-8")

    assert "token," in script
    assert "?token=" not in script
    assert 'localStorage.getItem("codeinsight-api-base")' in script
