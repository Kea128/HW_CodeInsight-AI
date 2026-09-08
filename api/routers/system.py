from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.config import configs
from api.desktop_settings import (
    CredentialStorageUnavailable,
    apply_desktop_settings,
    desktop_ai_hint,
    infer_probe_status,
    is_provider_configured,
    load_desktop_settings,
    record_model_probe,
    save_desktop_settings,
)
from api.logger import get_logger
from api.schemas import Model, ModelConfig, Provider
from api.services.model_discover import (
    _gateway_error_message,
    discover_openai_models,
    resolve_api_key,
    probe_openai_chat,
)
from api.services.ollama_installer import installer

logger = get_logger(__name__)

router = APIRouter(tags=["system"])


class DesktopSettingsRequest(BaseModel):
    provider: Literal["openai", "google", "ollama", "openai_compatible"]
    api_key: str | None = None
    base_url: str | None = None
    selected_model: str | None = None
    embedder_mode: str | None = None
    wiki_page_concurrency: int | None = Field(None, ge=1, le=8)
    max_concurrent_wiki_tasks: int | None = Field(None, ge=1, le=16)


class DesktopSettingsStatus(BaseModel):
    provider: str
    configured: bool
    usable: bool = False
    hint: str = ""
    probe_status: Literal["untested", "ok", "failed"] = "untested"
    probe_message: str | None = None
    restart_required: bool = False
    ollama_tier: str = "auto"
    ollama_model: str | None = None
    base_url: str | None = None
    selected_model: str | None = None
    embedder_mode: str = "auto"
    wiki_page_concurrency: int = 1
    max_concurrent_wiki_tasks: int | None = None
    analysis_pending: bool = False
    model_error: str | None = None
    disk_error: str | None = None
    ollama_error: str | None = None


class ModelEndpointRequest(BaseModel):
    base_url: str
    api_key: str | None = None
    model: str | None = None
    provider: Literal["openai_compatible"] = "openai_compatible"


class OllamaInstallRequest(BaseModel):
    tier: Literal["auto", "minimal", "balanced", "quality"] = "auto"


def _desktop_settings_status(
    data: dict[str, str], *, restart_required: bool = False
) -> DesktopSettingsStatus:
    provider = data.get("provider", "openai")
    ollama_status = installer.status() if provider == "ollama" else {}
    configured = is_provider_configured(data, ollama_status)
    probe_status = infer_probe_status(data)
    if probe_status not in {"untested", "ok", "failed"}:
        probe_status = "untested"
    usable = configured and probe_status != "failed"
    return DesktopSettingsStatus(
        provider=provider,
        configured=configured,
        usable=usable,
        hint=desktop_ai_hint(
            data,
            configured=configured,
            ollama_status=ollama_status,
            probe_status=probe_status,
        ),
        probe_status=probe_status,
        probe_message=data.get("last_probe_message"),
        restart_required=restart_required,
        ollama_tier=data.get("ollama_tier", "auto"),
        ollama_model=data.get("ollama_model"),
        base_url=data.get("base_url"),
        selected_model=data.get("selected_model"),
        embedder_mode=data.get("embedder_mode", "auto"),
        wiki_page_concurrency=int(data.get("wiki_page_concurrency") or 1),
        max_concurrent_wiki_tasks=(
            int(data["max_concurrent_wiki_tasks"])
            if data.get("max_concurrent_wiki_tasks")
            else None
        ),
        analysis_pending=bool(ollama_status.get("analysis_pending", False)),
        model_error=ollama_status.get("model_error"),
        disk_error=ollama_status.get("disk_error"),
        ollama_error=(
            str(ollama_status.get("message") or "Ollama 安装失败")
            if ollama_status.get("state") == "error"
            else None
        ),
    )


@router.get("/desktop/settings", response_model=DesktopSettingsStatus)
async def get_desktop_settings():
    try:
        return _desktop_settings_status(load_desktop_settings())
    except CredentialStorageUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.post("/desktop/settings", response_model=DesktopSettingsStatus)
async def update_desktop_settings(request: DesktopSettingsRequest):
    try:
        save_desktop_settings(
            request.provider,
            request.api_key,
            base_url=request.base_url,
            selected_model=request.selected_model,
            embedder_mode=request.embedder_mode,
            wiki_page_concurrency=request.wiki_page_concurrency,
            max_concurrent_wiki_tasks=request.max_concurrent_wiki_tasks,
        )
        data = apply_desktop_settings()
        return _desktop_settings_status(data, restart_required=False)
    except CredentialStorageUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.post("/desktop/models/discover")
async def discover_desktop_models(request: ModelEndpointRequest):
    try:
        api_key = resolve_api_key(request.provider, request.api_key)
        models = await discover_openai_models(request.base_url, api_key)
        return {"models": models}
    except CredentialStorageUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail=_gateway_error_message(error, "获取模型列表"),
        ) from error


@router.post("/desktop/models/test")
async def test_desktop_model(request: ModelEndpointRequest):
    try:
        api_key = resolve_api_key(request.provider, request.api_key)
        result = await probe_openai_chat(
            request.base_url, api_key, request.model or ""
        )
        _remember_probe(ok=True, message="连接成功", model=request.model)
        return result
    except CredentialStorageUnavailable as error:
        _remember_probe(ok=False, message=str(error), model=request.model)
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        _remember_probe(ok=False, message=str(error), model=request.model)
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPError as error:
        message = _gateway_error_message(error, "连接测试")
        _remember_probe(ok=False, message=message, model=request.model)
        raise HTTPException(
            status_code=502,
            detail=message,
        ) from error


def _remember_probe(*, ok: bool, message: str, model: str | None) -> None:
    try:
        record_model_probe(ok=ok, message=message, model=model)
    except Exception:
        logger.exception("Failed to persist model probe result")


@router.get("/desktop/ollama/status")
def get_ollama_install_status():
    return installer.status()


@router.post("/desktop/ollama/install")
def install_ollama(request: OllamaInstallRequest | None = None):
    return installer.start(request.tier if request else "auto")


@router.get("/lang/config")
async def lang_config():
    return configs["lang_config"]


@router.get("/models/config", response_model=ModelConfig)
async def get_model_config():
    """
    Get available model providers and their models.

    This endpoint returns the configuration of available model providers and their
    respective models that can be used throughout the application.

    Returns:
        ModelConfig: A configuration object containing providers and their models
    """
    try:
        logger.info("Fetching model configurations")

        # Create providers from the config file
        providers = []
        default_provider = configs.get("default_provider", "google")

        # Add provider configuration based on config.py
        for provider_id, provider_config in configs["providers"].items():
            models = []
            # Add models from config
            for model_id in provider_config["models"]:
                # Get a more user-friendly display name if possible
                models.append(Model(id=model_id, name=model_id))

            # Add provider with its models
            providers.append(
                Provider(
                    id=provider_id,
                    name=f"{provider_id.capitalize()}",
                    supportsCustomModel=provider_config.get(
                        "supportsCustomModel", False
                    ),
                    models=models,
                )
            )

        # Create and return the full configuration
        config = ModelConfig(providers=providers, defaultProvider=default_provider)
        return config

    except Exception as e:  # noqa: BLE001 - preserve API fallback on malformed config
        logger.error(f"Error creating model configuration: {e!s}")
        # Return some default configuration in case of error
        return ModelConfig(
            providers=[
                Provider(
                    id="google",
                    name="Google",
                    supportsCustomModel=True,
                    models=[Model(id="gemini-2.5-flash", name="Gemini 2.5 Flash")],
                )
            ],
            defaultProvider="google",
        )
