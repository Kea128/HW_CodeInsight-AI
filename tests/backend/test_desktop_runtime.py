import hashlib
import os
import sys
from pathlib import Path

import pytest

from api import desktop_runtime


def _write_verified_cache(directory: Path, payload: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / desktop_runtime.TIKTOKEN_CACHE_KEY
    path.write_bytes(payload)
    return path


def test_frozen_runtime_uses_bundled_tiktoken_cache(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    payload = b"offline tokenizer fixture"
    _write_verified_cache(bundle / "tiktoken_cache", payload)
    monkeypatch.setattr(
        desktop_runtime,
        "TIKTOKEN_CACHE_SHA256",
        hashlib.sha256(payload).hexdigest(),
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)

    configured = desktop_runtime.configure_bundled_tiktoken_cache()

    user_cache = tmp_path / "local" / "CodeInsight-AI" / "tiktoken_cache"
    assert configured == user_cache
    assert os.environ["TIKTOKEN_CACHE_DIR"] == str(user_cache)
    assert (user_cache / desktop_runtime.TIKTOKEN_CACHE_KEY).read_bytes() == payload
    assert (
        user_cache / desktop_runtime.TIKTOKEN_CACHE_SHA256
    ).read_bytes() == payload


def test_development_runtime_does_not_override_tiktoken_cache(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "developer-cache")

    assert desktop_runtime.configure_bundled_tiktoken_cache() is None
    assert os.environ["TIKTOKEN_CACHE_DIR"] == "developer-cache"


def test_development_runtime_uses_vendored_tiktoken_cache(monkeypatch, tmp_path):
    payload = b"vendored tokenizer fixture"
    vendor = tmp_path / "tiktoken-cache"
    _write_verified_cache(vendor, payload)
    monkeypatch.setattr(
        desktop_runtime,
        "TIKTOKEN_CACHE_SHA256",
        hashlib.sha256(payload).hexdigest(),
    )
    monkeypatch.setattr(desktop_runtime, "_package_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)

    configured = desktop_runtime.configure_bundled_tiktoken_cache()

    user_cache = tmp_path / "local" / "CodeInsight-AI" / "tiktoken_cache"
    assert configured == user_cache
    assert os.environ["TIKTOKEN_CACHE_DIR"] == str(user_cache)


def test_frozen_runtime_rejects_missing_tiktoken_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "daemon.exe"), raising=False)

    with pytest.raises(RuntimeError, match="encoding is missing"):
        desktop_runtime.configure_bundled_tiktoken_cache()


def test_frozen_runtime_rejects_corrupt_tiktoken_cache(monkeypatch, tmp_path):
    cache = tmp_path / "tiktoken_cache"
    cache.mkdir()
    (cache / desktop_runtime.TIKTOKEN_CACHE_KEY).write_bytes(b"corrupt")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "daemon.exe"), raising=False)

    with pytest.raises(RuntimeError, match="encoding is missing"):
        desktop_runtime.configure_bundled_tiktoken_cache()


def test_windows_ssl_bundle_trusts_system_certificates(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        desktop_runtime,
        "_windows_store_certificates",
        lambda: [b"fake-corporate-root"],
    )
    monkeypatch.setattr(
        desktop_runtime,
        "_combined_ca_bundle",
        lambda certificates: "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)

    bundle = desktop_runtime.configure_windows_ssl_bundle()

    assert bundle == tmp_path / "CodeInsight-AI" / "windows-ca-bundle.pem"
    assert bundle.is_file()
    assert os.environ["SSL_CERT_FILE"] == str(bundle)
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(bundle)
    assert os.environ["CURL_CA_BUNDLE"] == str(bundle)


def test_tiktoken_loads_cl100k_from_prepared_cache(monkeypatch, tmp_path):
    source = (
        Path(__file__).resolve().parents[2]
        / "api"
        / "tiktoken-cache"
        / desktop_runtime.TIKTOKEN_CACHE_KEY
    )
    if not source.is_file():
        pytest.skip("vendored tiktoken encoding is not present")
    cache = tmp_path / "tiktoken_cache"
    cache.mkdir()
    (cache / desktop_runtime.TIKTOKEN_CACHE_KEY).write_bytes(source.read_bytes())
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(cache))

    import requests
    import tiktoken
    import tiktoken.registry

    def blocked_get(*_args, **_kwargs):
        raise AssertionError("tiktoken must not download encodings")

    monkeypatch.setattr(requests, "get", blocked_get)
    tiktoken.registry.ENCODINGS.clear()

    encoding = tiktoken.get_encoding("cl100k_base")

    assert encoding.encode("hello") == [15339]
