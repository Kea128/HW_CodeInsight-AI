import os
import sys

from api.desktop_runtime import configure_bundled_tiktoken_cache


def test_frozen_runtime_uses_bundled_tiktoken_cache(monkeypatch, tmp_path):
    cache = tmp_path / "tiktoken_cache"
    cache.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)

    configured = configure_bundled_tiktoken_cache()

    assert configured == cache
    assert os.environ["TIKTOKEN_CACHE_DIR"] == str(cache)


def test_development_runtime_does_not_override_tiktoken_cache(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "developer-cache")

    assert configure_bundled_tiktoken_cache() is None
    assert os.environ["TIKTOKEN_CACHE_DIR"] == "developer-cache"
