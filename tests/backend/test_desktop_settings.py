import json
import os
from types import SimpleNamespace

import pytest

from api import desktop_settings
from api.desktop_settings import (
    apply_desktop_settings,
    desktop_ai_hint,
    infer_probe_status,
    is_provider_configured,
    load_desktop_settings,
    record_model_probe,
    save_desktop_settings,
    _resolve_embedder_type,
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


def test_openai_compatible_settings_apply_base_url_and_model(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
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

    saved = save_desktop_settings(
        "openai_compatible",
        "sk-test",
        base_url="https://api.deepseek.com",
        selected_model="deepseek-chat",
        embedder_mode="auto",
        wiki_page_concurrency=4,
    )
    applied = apply_desktop_settings()

    assert saved["provider"] == "openai_compatible"
    assert applied["openai_compatible_api_key"] == "__keyring__"
    assert os.environ["OPENAI_API_KEY"] == "sk-test"
    assert os.environ["OPENAI_BASE_URL"] == "https://api.deepseek.com/v1"
    assert os.environ["CODEINSIGHT_DESKTOP_MODEL"] == "deepseek-chat"
    assert os.environ["DEEPWIKI_WIKI_PAGE_CONCURRENCY"] == "4"
    assert os.environ["DEEPWIKI_EMBEDDER_TYPE"] == "none"


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


def test_compatible_api_is_not_configured_without_model():
    data = {
        "provider": "openai_compatible",
        "openai_compatible_api_key": "__keyring__",
        "base_url": "https://api.example.com",
    }

    assert is_provider_configured(data, {}) is False
    assert "模型 ID" in desktop_ai_hint(
        data, configured=False, ollama_status={}, probe_status="untested"
    )


def test_compatible_api_hint_reports_untested_and_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(desktop_settings.keyring, "get_password", lambda *_: None)
    data = {
        "provider": "openai_compatible",
        "openai_compatible_api_key": "__keyring__",
        "base_url": "https://api.example.com",
        "selected_model": "qwen-plus",
    }

    assert is_provider_configured(data, {}) is True
    assert infer_probe_status(data) == "untested"
    assert "尚未测试" in desktop_ai_hint(
        data, configured=True, ollama_status={}, probe_status="untested"
    )

    record_model_probe(ok=False, message="上游返回 401", model="qwen-plus")
    probed = load_desktop_settings()
    data.update(probed)
    data["openai_compatible_api_key"] = "__keyring__"
    data["selected_model"] = "qwen-plus"
    assert infer_probe_status(data) == "failed"
    assert "测试失败" in desktop_ai_hint(
        data, configured=True, ollama_status={}, probe_status="failed"
    )


def test_resolve_embedder_type_uses_local_for_compatible_and_none():
    assert _resolve_embedder_type("openai_compatible", "auto") == "none"
    assert _resolve_embedder_type("openai", "none") == "none"
    assert _resolve_embedder_type("google", "none") == "none"
    assert _resolve_embedder_type("openai", "auto") == "openai"
    assert _resolve_embedder_type("openai_compatible", "openai") == "openai"
    assert _resolve_embedder_type("ollama", "auto") == "ollama"


def test_embedder_mode_none_applies_local_embedder(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
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

    save_desktop_settings("openai", "secret-key", embedder_mode="none")
    apply_desktop_settings()

    assert os.environ["DEEPWIKI_EMBEDDER_TYPE"] == "none"
