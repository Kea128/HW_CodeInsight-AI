from types import SimpleNamespace

from api.services import ollama_installer


def test_status_reports_required_models_ready(monkeypatch):
    monkeypatch.setattr(ollama_installer, "total_memory_gb", lambda: 8)
    monkeypatch.setattr(ollama_installer, "load_desktop_settings", dict)
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
    monkeypatch.setattr(ollama_installer, "total_memory_gb", lambda: 8)
    monkeypatch.setattr(ollama_installer, "load_desktop_settings", dict)
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
        lambda: ["nomic-embed-text:latest"],
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
        lambda provider, key, **options: saved.append((provider, key, options)),
    )
    monkeypatch.setattr(ollama_installer, "load_desktop_settings", dict)
    monkeypatch.setattr(ollama_installer, "total_memory_gb", lambda: 16)
    monkeypatch.setattr(ollama_installer.tempfile, "gettempdir", lambda: str(tmp_path))

    service = ollama_installer.OllamaInstaller()
    service._requested_tier = "balanced"
    service._install()

    assert [command[-1] for command in pulled] == ["qwen3:4b"]
    assert saved == [
        (
            "ollama",
            None,
            {"ollama_tier": "balanced", "ollama_model": "qwen3:4b"},
        )
    ]
    assert service.status()["restart_required"] is True


def test_recommended_tier_tracks_memory():
    assert ollama_installer.recommended_tier(8) == "minimal"
    assert ollama_installer.recommended_tier(16) == "balanced"
    assert ollama_installer.recommended_tier(32) == "quality"
