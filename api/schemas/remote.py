from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator


class RemoteProjectRequest(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(repr=False)
    remote_path: str = Field(min_length=1, max_length=4096)
    poll_seconds: int = Field(60, ge=10, le=3600)
    provider: Literal["openai", "google", "ollama"] = "ollama"
    model: str | None = None
    language: str = "zh"

    @field_validator("host", "username")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be empty")
        return stripped

    @field_validator("remote_path")
    @classmethod
    def validate_remote_path(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped.startswith("/"):
            raise ValueError("remote_path must be an absolute Ubuntu path")
        return stripped


class RemoteProjectStatus(BaseModel):
    id: str
    host: str
    port: int
    username: str
    remote_path: str
    enabled: bool = True
    poll_seconds: int
    host_fingerprint: str | None = None
    last_sync_at: int | None = None
    last_error: str | None = None
    last_task_id: str | None = None
