from types import SimpleNamespace

from api.services import ollama_installer


def test_status_reports_required_models_ready(monkeypatch):
    monkeypatch.setattr(
        ollama_installer, "find_ollama_executable", lambda: "ollama.exe"
    )
    monkeypatch.setattr(
        ollama_installer,
        "_ollama_tags",
        lambda: ["qwen3:1.7b", "nomic-embed-text:latest"],
    )

    status = ollama_installer.OllamaInstaller().status()

    assert status["installed"] is True
    assert status["running"] is True
    assert status["ready"] is True
    assert status["state"] == "ready"
    assert status["progress"] == 100


def test_status_distinguishes_installed_runtime_from_models(monkeypatch):
    monkeypatch.setattr(
        ollama_installer, "find_ollama_executable", lambda: "ollama.exe"
    )
    monkeypatch.setattr(ollama_installer, "_ollama_tags", list)

    status = ollama_installer.OllamaInstaller().status()

    assert status["installed"] is True
    assert status["running"] is True
    assert status["ready"] is False


def test_install_pulls_models_and_selects_ollama(monkeypatch, tmp_path):
    pulled = []
    saved = []
    monkeypatch.setattr(
        ollama_installer, "find_ollama_executable", lambda: "ollama.exe"
    )
    monkeypatch.setattr(
        ollama_installer,
        "_ollama_tags",
        lambda: ["qwen3:1.7b", "nomic-embed-text:latest"],
    )
    monkeypatch.setattr(
        ollama_installer.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(free=10 * 1024**3),
    )
    monkeypatch.setattr(
        ollama_installer.subprocess,
        "run",
        lambda command, **_: pulled.append(command) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        ollama_installer,
        "save_desktop_settings",
        lambda provider, key: saved.append((provider, key)),
    )
    monkeypatch.setattr(ollama_installer.tempfile, "gettempdir", lambda: str(tmp_path))

    service = ollama_installer.OllamaInstaller()
    service._install()

    assert [command[-1] for command in pulled] == list(ollama_installer.REQUIRED_MODELS)
    assert saved == [("ollama", None)]
    assert service.status()["restart_required"] is True
