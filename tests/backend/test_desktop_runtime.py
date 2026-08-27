import hashlib
import os
import sys

import pytest

from api import desktop_runtime


def test_frozen_runtime_uses_bundled_tiktoken_cache(monkeypatch, tmp_path):
    cache = tmp_path / "tiktoken_cache"
    cache.mkdir()
    payload = b"offline tokenizer fixture"
    (cache / desktop_runtime.TIKTOKEN_CACHE_KEY).write_bytes(payload)
    monkeypatch.setattr(
        desktop_runtime,
        "TIKTOKEN_CACHE_SHA256",
        hashlib.sha256(payload).hexdigest(),
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)

    configured = desktop_runtime.configure_bundled_tiktoken_cache()

    assert configured == cache
    assert os.environ["TIKTOKEN_CACHE_DIR"] == str(cache)


def test_development_runtime_does_not_override_tiktoken_cache(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "developer-cache")

    assert desktop_runtime.configure_bundled_tiktoken_cache() is None
    assert os.environ["TIKTOKEN_CACHE_DIR"] == "developer-cache"


def test_frozen_runtime_rejects_missing_tiktoken_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    with pytest.raises(RuntimeError, match="encoding is missing"):
        desktop_runtime.configure_bundled_tiktoken_cache()


def test_frozen_runtime_rejects_corrupt_tiktoken_cache(monkeypatch, tmp_path):
    cache = tmp_path / "tiktoken_cache"
    cache.mkdir()
    (cache / desktop_runtime.TIKTOKEN_CACHE_KEY).write_bytes(b"corrupt")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    with pytest.raises(RuntimeError, match="integrity validation"):
        desktop_runtime.configure_bundled_tiktoken_cache()
