from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from api.config import configs
from api.desktop_settings import load_desktop_settings, save_desktop_settings
from api.logger import get_logger
from api.schemas import Model, ModelConfig, Provider
from api.services.ollama_installer import installer

logger = get_logger(__name__)

router = APIRouter(tags=["system"])


class DesktopSettingsRequest(BaseModel):
    provider: Literal["openai", "google", "ollama"]
    api_key: str | None = None


class DesktopSettingsStatus(BaseModel):
    provider: str
    configured: bool
    restart_required: bool = False
    ollama_tier: str = "auto"
    ollama_model: str | None = None


class OllamaInstallRequest(BaseModel):
    tier: Literal["auto", "minimal", "balanced", "quality"] = "auto"


def _desktop_settings_status(
    data: dict[str, str], *, restart_required: bool = False
) -> DesktopSettingsStatus:
    provider = data.get("provider", "openai")
    configured = (
        installer.status()["ready"]
        if provider == "ollama"
        else bool(data.get(f"{provider}_api_key"))
    )
    return DesktopSettingsStatus(
        provider=provider,
        configured=configured,
        restart_required=restart_required,
        ollama_tier=data.get("ollama_tier", "auto"),
        ollama_model=data.get("ollama_model"),
    )


@router.get("/health")
async def health_check():
    """Health check endpoint for Docker and monitoring"""
    return {
        "status": "healthy",
        "timestamp": datetime.now(UTC).isoformat(),
        "service": "codeinsight-engine",
    }


@router.get("/desktop/settings", response_model=DesktopSettingsStatus)
async def get_desktop_settings():
    return _desktop_settings_status(load_desktop_settings())


@router.post("/desktop/settings", response_model=DesktopSettingsStatus)
async def update_desktop_settings(request: DesktopSettingsRequest):
    data = save_desktop_settings(request.provider, request.api_key)
    return _desktop_settings_status(data, restart_required=True)


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
