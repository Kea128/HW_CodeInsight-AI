"""Persistent settings used by the frozen desktop analysis engine."""

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

import keyring

Provider = Literal["openai", "google", "ollama"]
SUPPORTED_PROVIDERS = {"openai", "google", "ollama"}
KEY_ENVIRONMENTS = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
}
KEYRING_SERVICE = "CodeInsight-AI.Models"


def settings_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    return root / "CodeInsight-AI" / "settings.json"


def _read_settings_file() -> dict[str, str]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if value is not None}


def _write_settings_file(data: dict[str, str]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, path)


def migrate_plaintext_api_keys() -> dict[str, str]:
    data = _read_settings_file()
    changed = False
    for provider in KEY_ENVIRONMENTS:
        field = f"{provider}_api_key"
        if field in data:
            value = data[field]
            try:
                if value:
                    keyring.set_password(KEYRING_SERVICE, provider, value)
            except keyring.errors.KeyringError:
                continue
            data.pop(field)
            changed = True
    if changed:
        _write_settings_file(data)
    return data


def api_key_configured(provider: str) -> bool:
    try:
        return bool(keyring.get_password(KEYRING_SERVICE, provider))
    except keyring.errors.KeyringError:
        return False


def load_desktop_settings() -> dict[str, str]:
    data = migrate_plaintext_api_keys().copy()
    for provider in KEY_ENVIRONMENTS:
        data.pop(f"{provider}_api_key", None)
        if api_key_configured(provider):
            data[f"{provider}_api_key"] = "__keyring__"
    return data


def save_desktop_settings(
    provider: Provider,
    api_key: str | None,
    *,
    ollama_tier: str | None = None,
    ollama_model: str | None = None,
) -> dict[str, str]:
    data = migrate_plaintext_api_keys()
    data["provider"] = provider
    if api_key is not None:
        stripped_key = api_key.strip()
        if stripped_key:
            keyring.set_password(KEYRING_SERVICE, provider, stripped_key)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, provider)
            except keyring.errors.KeyringError:
                pass
    if ollama_tier is not None:
        data["ollama_tier"] = ollama_tier
    if ollama_model is not None:
        data["ollama_model"] = ollama_model

    _write_settings_file(data)
    return load_desktop_settings()


def selected_ollama_model() -> str | None:
    return load_desktop_settings().get("ollama_model")


def apply_desktop_settings() -> dict[str, str]:
    data = load_desktop_settings()
    provider = data.get("provider", "openai").lower()
    if provider not in SUPPORTED_PROVIDERS:
        provider = "openai"

    os.environ["CODEINSIGHT_DESKTOP_PROVIDER"] = provider
    os.environ["DEEPWIKI_EMBEDDER_TYPE"] = provider
    if data.get("ollama_model"):
        os.environ["CODEINSIGHT_OLLAMA_MODEL"] = data["ollama_model"]
    environment = KEY_ENVIRONMENTS.get(provider)
    try:
        api_key = keyring.get_password(KEYRING_SERVICE, provider)
    except keyring.errors.KeyringError:
        api_key = None
    if environment and api_key:
        os.environ[environment] = api_key
    return data
