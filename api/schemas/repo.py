import hashlib
import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from api.schemas.base import RepoRequestBase


class RepoPrepareRequest(RepoRequestBase):
    """Request body for POST /repo/prepare (index warming). No chat messages."""


class WikiTaskRequest(RepoRequestBase):
    """Request body for POST /wiki/tasks, submitting a wiki-generation task."""

    owner: str
    repo: str
    display_location: str | None = Field(
        None,
        description="Human-readable repository location for desktop task lists",
    )
    comprehensive: bool = Field(True, description="Comprehensive vs concise wiki")
    force: bool = Field(
        False,
        description="Rebuild the index and wiki even when a cache already exists",
    )

    @property
    def legacy_repo_key(self) -> str:
        return f"{self.type}_{self.owner}_{self.repo}"

    @property
    def repo_key(self) -> str:
        """Identity for one independently generated wiki.

        Language and the effective provider/model are generation inputs, so
        requests that differ on them must not join the same running task.
        Keep the legacy repository prefix for diagnostics and migration, while
        hashing model coordinates to keep IDs safe in URL path segments.
        """
        if self.space_id:
            return f"space_{self.space_id}"
        language = (
            re.sub(r"[^A-Za-z0-9.-]+", "-", self.language).strip("-") or "default"
        )
        model_identity = f"{self.provider}\0{self.model or 'default'}".encode(
            "utf-8"
        )
        model_digest = hashlib.sha256(model_identity).hexdigest()[:12]
        scope = ",".join(sorted(self.included_dirs or []))
        if scope:
            scope_digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:8]
            return f"{self.legacy_repo_key}_{language}_{model_digest}_{scope_digest}"
        return f"{self.legacy_repo_key}_{language}_{model_digest}"


class TaskStatus(str, Enum):
    PENDING = "pending"
    INDEXING = "indexing"
    DETERMINING_STRUCTURE = "determining_structure"
    GENERATING = "generating"
    PAUSED = "paused"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def is_terminal(self):
        return self in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )


class WikiTaskSubmitResult(BaseModel):
    task_id: str
    status: TaskStatus | str
    created: bool = False
    joined: bool = False
    from_cache: bool = False

    @field_validator(
        "status",
        mode="before",
    )
    @classmethod
    def _status_validate(cls, value):
        if isinstance(value, str):
            return TaskStatus(value.lower())
        return value


class RepoInfo(BaseModel):
    owner: str
    repo: str
    type: str
    token: str | None = None
    localPath: str | None = None
    repoUrl: str | None = None
