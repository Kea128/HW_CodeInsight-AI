"""Persistent settings used by the frozen desktop analysis engine."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Literal

import keyring

Provider = Literal["openai", "google", "ollama", "openai_compatible"]
SUPPORTED_PROVIDERS = {"openai", "google", "ollama", "openai_compatible"}
KEY_ENVIRONMENTS = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openai_compatible": "OPENAI_API_KEY",
}
KEYRING_SERVICE = "CodeInsight-AI.Models"
EMBEDDER_MODES = {"auto", "openai", "google", "ollama", "none"}


class CredentialStorageUnavailable(RuntimeError):
    """Raised when the operating-system credential vault cannot be used."""


def _credential_error(action: str) -> CredentialStorageUnavailable:
    return CredentialStorageUnavailable(
        f"Windows 凭据管理器不可用，无法{action} AI 密钥。"
        "请确认 Credential Manager 服务正在运行，然后重试"
    )


def _get_password(provider: str) -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, provider)
    except keyring.errors.KeyringError as error:
        raise _credential_error("读取") from error


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
            except keyring.errors.KeyringError as error:
                # Never delete the only copy when migration to the vault failed.
                raise _credential_error("迁移") from error
            data.pop(field)
            changed = True
    if changed:
        _write_settings_file(data)
    return data


def api_key_configured(provider: str) -> bool:
    return bool(_get_password(provider))


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
    base_url: str | None = None,
    selected_model: str | None = None,
    embedder_mode: str | None = None,
    wiki_page_concurrency: int | str | None = None,
    max_concurrent_wiki_tasks: int | str | None = None,
) -> dict[str, str]:
    data = migrate_plaintext_api_keys()
    data["provider"] = provider
    if api_key is not None:
        stripped_key = api_key.strip()
        if stripped_key:
            try:
                keyring.set_password(KEYRING_SERVICE, provider, stripped_key)
            except keyring.errors.KeyringError as error:
                raise _credential_error("保存") from error
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, provider)
            except keyring.errors.PasswordDeleteError:
                pass
            except keyring.errors.KeyringError as error:
                raise _credential_error("删除") from error
    if ollama_tier is not None:
        data["ollama_tier"] = ollama_tier
    if ollama_model is not None:
        data["ollama_model"] = ollama_model
    if base_url is not None:
        data["base_url"] = base_url.strip()
    if selected_model is not None:
        data["selected_model"] = selected_model.strip()
    if embedder_mode is not None and embedder_mode in EMBEDDER_MODES:
        data["embedder_mode"] = embedder_mode
    if wiki_page_concurrency is not None:
        data["wiki_page_concurrency"] = str(max(1, min(8, int(wiki_page_concurrency))))
    if max_concurrent_wiki_tasks is not None:
        data["max_concurrent_wiki_tasks"] = str(
            max(1, min(16, int(max_concurrent_wiki_tasks)))
        )
    for key in (
        "last_probe_ok",
        "last_probe_message",
        "last_probe_at",
        "last_probe_model",
    ):
        data.pop(key, None)

    _write_settings_file(data)
    return load_desktop_settings()


def record_model_probe(
    *, ok: bool, message: str, model: str | None = None
) -> None:
    data = _read_settings_file()
    data["last_probe_ok"] = "1" if ok else "0"
    data["last_probe_message"] = (message or "")[:400]
    data["last_probe_at"] = str(int(time.time() * 1000))
    if model is not None:
        data["last_probe_model"] = model
    _write_settings_file(data)


def infer_probe_status(data: dict[str, str]) -> str:
    if data.get("last_probe_ok") not in {"0", "1"}:
        return "untested"
    selected = data.get("selected_model") or ""
    probed = data.get("last_probe_model") or ""
    if selected and probed and selected != probed:
        return "untested"
    return "ok" if data.get("last_probe_ok") == "1" else "failed"


def is_provider_configured(data: dict[str, str], ollama_status: dict) -> bool:
    provider = data.get("provider", "openai")
    if provider == "ollama":
        return bool(ollama_status.get("ready"))
    if not data.get(f"{provider}_api_key"):
        return False
    if provider == "openai_compatible":
        return bool(data.get("base_url") and data.get("selected_model"))
    return True


def desktop_ai_hint(
    data: dict[str, str],
    *,
    configured: bool,
    ollama_status: dict,
    probe_status: str,
) -> str:
    provider = data.get("provider", "openai")
    labels = {
        "openai": "OpenAI",
        "google": "Google Gemini",
        "ollama": "本机 Ollama",
        "openai_compatible": "自定义 OpenAI 兼容接口",
    }
    name = labels.get(provider, provider)
    if provider == "ollama":
        if configured:
            return f"{name} 可用，同步完成后会自动分析。"
        return str(
            ollama_status.get("message") or f"{name} 未就绪，同步完成后不会自动分析。"
        )
    if provider == "openai_compatible":
        if not data.get("base_url"):
            return "自定义 API 未填写地址，无法开始分析。"
        if not data.get("openai_compatible_api_key"):
            return "自定义 API 未填写密钥，无法开始分析。"
        if not data.get("selected_model"):
            return "自定义 API 未填写模型 ID，无法开始分析。请填写模型 ID 后点「测试连接」。"
        if probe_status == "failed":
            detail = data.get("last_probe_message") or "未知错误"
            return f"自定义 API 最近测试失败：{detail}。分析不会自动开始。"
        if probe_status == "ok":
            return f"自定义 API 可用（模型 {data.get('selected_model')}），同步完成后会自动分析。"
        return (
            f"自定义 API 已填写（模型 {data.get('selected_model')}），但尚未测试。"
            "请点「测试连接」确认可用，否则分析可能失败。"
        )
    if not configured:
        return f"{name} 未填写 API Key，无法开始分析。"
    if probe_status == "failed":
        return f"{name} 最近测试失败，分析可能无法开始。"
    if probe_status == "ok":
        return f"{name} 可用，同步完成后会自动分析。"
    return f"{name} 已配置。建议先测试连接；同步完成后会自动分析。"


def selected_ollama_model() -> str | None:
    return load_desktop_settings().get("ollama_model")


def apply_desktop_settings() -> dict[str, str]:
    try:
        data = load_desktop_settings()
    except CredentialStorageUnavailable:
        # Keep the API alive so the settings endpoint can return an actionable,
        # recoverable error instead of making the whole sidecar fail to start.
        data = _read_settings_file()
        for provider_name in KEY_ENVIRONMENTS:
            data.pop(f"{provider_name}_api_key", None)
    provider = data.get("provider", "openai").lower()
    if provider not in SUPPORTED_PROVIDERS:
        provider = "openai"

    os.environ["CODEINSIGHT_DESKTOP_PROVIDER"] = provider
    embedder_type = _resolve_embedder_type(provider, data.get("embedder_mode", "auto"))
    os.environ["DEEPWIKI_EMBEDDER_TYPE"] = embedder_type
    # api.config snapshots this value at import time. Update the loaded module
    # so desktop settings and post-install Ollama selection take effect without
    # restarting the daemon; future imports still read the environment above.
    config_module = sys.modules.get("api.config")
    if config_module is not None:
        config_module.EMBEDDER_TYPE = embedder_type
    if data.get("ollama_model"):
        os.environ["CODEINSIGHT_OLLAMA_MODEL"] = data["ollama_model"]
    if data.get("selected_model"):
        os.environ["CODEINSIGHT_DESKTOP_MODEL"] = data["selected_model"]
    if data.get("base_url"):
        try:
            from api.services.model_discover import normalize_openai_base_url

            os.environ["OPENAI_BASE_URL"] = normalize_openai_base_url(data["base_url"])
        except ValueError:
            os.environ["OPENAI_BASE_URL"] = data["base_url"].strip()
    if data.get("wiki_page_concurrency"):
        os.environ["DEEPWIKI_WIKI_PAGE_CONCURRENCY"] = data["wiki_page_concurrency"]
    elif provider == "openai_compatible":
        os.environ.setdefault("DEEPWIKI_WIKI_PAGE_CONCURRENCY", "4")
    if data.get("max_concurrent_wiki_tasks"):
        os.environ["DEEPWIKI_MAX_CONCURRENT_WIKI_TASKS"] = data[
            "max_concurrent_wiki_tasks"
        ]
    environment = KEY_ENVIRONMENTS.get(provider)
    try:
        api_key = _get_password(provider)
    except CredentialStorageUnavailable:
        api_key = None
    if environment and api_key:
        os.environ[environment] = api_key
    try:
        from api.services.oplog import log_event

        host = ""
        raw = os.environ.get("OPENAI_BASE_URL") or data.get("base_url") or ""
        if raw:
            host = raw.split("://", 1)[-1].split("/", 1)[0]
        log_event(
            "settings_applied",
            f"已加载设置：{provider}"
            + (f" / {data.get('selected_model') or data.get('ollama_model') or ''}" if data.get("selected_model") or data.get("ollama_model") else ""),
            provider=provider,
            model=data.get("selected_model") or data.get("ollama_model"),
            embedder=embedder_type,
            api_host=host,
        )
    except Exception:
        pass
    return data


def _resolve_embedder_type(provider: str, embedder_mode: str) -> str:
    if embedder_mode in {"openai", "google", "ollama"}:
        return embedder_mode
    if embedder_mode == "none":
        return "none"
    if provider == "openai_compatible":
        return "none"
    if provider in {"openai", "google", "ollama"}:
        return provider
    return "openai"


def selected_desktop_model() -> str | None:
    return load_desktop_settings().get("selected_model")
