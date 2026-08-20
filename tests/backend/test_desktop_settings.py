import json
import os

from api.desktop_settings import apply_desktop_settings, save_desktop_settings


def test_desktop_settings_persist_and_apply(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPWIKI_EMBEDDER_TYPE", raising=False)

    saved = save_desktop_settings("openai", "secret-key")
    applied = apply_desktop_settings()

    assert saved == applied
    assert json.loads(
        (tmp_path / "CodeInsight-AI" / "settings.json").read_text(encoding="utf-8")
    ) == {"provider": "openai", "openai_api_key": "secret-key"}
    assert applied["openai_api_key"] == "secret-key"
    assert os.environ["OPENAI_API_KEY"] == "secret-key"
    assert os.environ["DEEPWIKI_EMBEDDER_TYPE"] == "openai"
