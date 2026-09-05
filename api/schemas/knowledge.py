from pydantic import BaseModel, Field


class KnowledgeSpace(BaseModel):
    space_id: str
    workspace_root: str
    included_dirs: list[str] = Field(default_factory=list)
    excluded_dirs: list[str] = Field(default_factory=list)
    label: str
    parent_workspace: str
    language: str = "zh"
    provider: str | None = None
    model: str | None = None
    last_task_id: str | None = None
    created_at: int = 0
    updated_at: int = 0


class KnowledgeCandidate(BaseModel):
    path: str
    kind: str
    label: str


class KnowledgeDetectRequest(BaseModel):
    workspace_root: str


class KnowledgeDetectResponse(BaseModel):
    workspace_root: str
    candidates: list[KnowledgeCandidate]


class KnowledgeSpaceCreateRequest(BaseModel):
    workspace_root: str
    included_dirs: list[str] = Field(default_factory=list)
    excluded_dirs: list[str] = Field(default_factory=list)
    language: str = "zh"
    provider: str | None = None
    model: str | None = None
    analyze_now: bool = True
    night_start: str | None = None
    night_end: str | None = None
    poll_seconds: int = 15


class KnowledgeSpaceCreateResponse(BaseModel):
    spaces: list[KnowledgeSpace]
    task_ids: list[str] = Field(default_factory=list)


class KnowledgeAskRequest(BaseModel):
    space_id: str
    question: str
    provider: str | None = None
    model: str | None = None
    language: str = "zh"
