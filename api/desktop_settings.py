"""Persistent settings used by the frozen desktop analysis engine."""

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

Provider = Literal["openai", "google", "ollama"]
SUPPORTED_PROVIDERS = {"openai", "google", "ollama"}
KEY_ENVIRONMENTS = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def settings_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    return root / "CodeInsight-AI" / "settings.json"


def load_desktop_settings() -> dict[str, str]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if value is not None}


def save_desktop_settings(provider: Provider, api_key: str | None) -> dict[str, str]:
    data = load_desktop_settings()
    data["provider"] = provider
    if api_key is not None:
        stripped_key = api_key.strip()
        if stripped_key:
            data[f"{provider}_api_key"] = stripped_key
        else:
            data.pop(f"{provider}_api_key", None)

    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, path)
    return data


def apply_desktop_settings() -> dict[str, str]:
    data = load_desktop_settings()
    provider = data.get("provider", "openai").lower()
    if provider not in SUPPORTED_PROVIDERS:
        provider = "openai"

    os.environ["CODEINSIGHT_DESKTOP_PROVIDER"] = provider
    os.environ["DEEPWIKI_EMBEDDER_TYPE"] = provider
    environment = KEY_ENVIRONMENTS.get(provider)
    api_key = data.get(f"{provider}_api_key")
    if environment and api_key:
        os.environ[environment] = api_key
    return data
