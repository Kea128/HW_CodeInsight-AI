import json
import os
from types import SimpleNamespace

import pytest

from api import desktop_settings
from api.desktop_settings import (
    apply_desktop_settings,
    load_desktop_settings,
    save_desktop_settings,
)


def test_desktop_settings_persist_and_apply(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPWIKI_EMBEDDER_TYPE", raising=False)
    values = {}
    monkeypatch.setattr(
        desktop_settings.keyring,
        "set_password",
        lambda service, provider, value: values.__setitem__((service, provider), value),
    )
    monkeypatch.setattr(
        desktop_settings.keyring,
        "get_password",
        lambda service, provider: values.get((service, provider)),
    )

    saved = save_desktop_settings("openai", "secret-key")
    applied = apply_desktop_settings()

    assert saved == applied
    assert json.loads(
        (tmp_path / "CodeInsight-AI" / "settings.json").read_text(encoding="utf-8")
    ) == {"provider": "openai"}
    assert applied["openai_api_key"] == "__keyring__"
    assert os.environ["OPENAI_API_KEY"] == "secret-key"
    assert os.environ["DEEPWIKI_EMBEDDER_TYPE"] == "openai"


def test_plaintext_key_is_migrated_and_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = tmp_path / "CodeInsight-AI" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"provider": "google", "google_api_key": "legacy-secret"}),
        encoding="utf-8",
    )
    values = {}
    monkeypatch.setattr(
        desktop_settings.keyring,
        "set_password",
        lambda service, provider, value: values.__setitem__((service, provider), value),
    )
    monkeypatch.setattr(
        desktop_settings.keyring,
        "get_password",
        lambda service, provider: values.get((service, provider)),
    )

    loaded = load_desktop_settings()

    assert loaded["google_api_key"] == "__keyring__"
    assert "legacy-secret" not in path.read_text(encoding="utf-8")


def test_keyring_failure_keeps_legacy_secret_on_disk_without_exposing_it(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = tmp_path / "CodeInsight-AI" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"provider": "openai", "openai_api_key": "legacy-secret"}),
        encoding="utf-8",
    )

    def fail(*_):
        raise desktop_settings.keyring.errors.KeyringError("unavailable")

    monkeypatch.setattr(desktop_settings.keyring, "set_password", fail)
    monkeypatch.setattr(desktop_settings.keyring, "get_password", fail)

    with pytest.raises(
        desktop_settings.CredentialStorageUnavailable,
        match="Credential Manager",
    ):
        load_desktop_settings()

    assert "legacy-secret" in path.read_text(encoding="utf-8")


def test_new_key_is_not_written_to_settings_when_keyring_is_unavailable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def fail(*_):
        raise desktop_settings.keyring.errors.KeyringError("unavailable")

    monkeypatch.setattr(desktop_settings.keyring, "set_password", fail)
    monkeypatch.setattr(desktop_settings.keyring, "get_password", lambda *_: None)

    with pytest.raises(desktop_settings.CredentialStorageUnavailable, match="重试"):
        save_desktop_settings("openai", "never-write-me")

    path = tmp_path / "CodeInsight-AI" / "settings.json"
    assert not path.exists() or "never-write-me" not in path.read_text(encoding="utf-8")


def test_apply_settings_hot_reloads_loaded_embedder_config(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "CodeInsight-AI").mkdir()
    (tmp_path / "CodeInsight-AI" / "settings.json").write_text(
        json.dumps({"provider": "ollama", "ollama_model": "qwen3:4b"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(desktop_settings, "_get_password", lambda provider: None)
    loaded_config = SimpleNamespace(EMBEDDER_TYPE="openai")
    monkeypatch.setitem(desktop_settings.sys.modules, "api.config", loaded_config)

    applied = apply_desktop_settings()

    assert applied["provider"] == "ollama"
    assert loaded_config.EMBEDDER_TYPE == "ollama"
    assert os.environ["CODEINSIGHT_OLLAMA_MODEL"] == "qwen3:4b"
