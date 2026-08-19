from pydantic import BaseModel, Field

from api.schemas.repo import WikiTaskRequest


class ContinuousAnalysisRequest(BaseModel):
    task: WikiTaskRequest
    night_start: str | None = Field(
        None, description="Local start time in HH:MM; omit for all-day analysis"
    )
    night_end: str | None = Field(
        None, description="Local end time in HH:MM; may cross midnight"
    )
    poll_seconds: int = Field(15, ge=2, le=3600)
    analyze_now: bool = True


class ContinuousProject(BaseModel):
    id: str
    request: WikiTaskRequest
    enabled: bool = True
    night_start: str | None = None
    night_end: str | None = None
    poll_seconds: int
    file_hashes: dict[str, str] = Field(default_factory=dict, exclude=True)
    last_scan_at: int | None = None
    last_task_id: str | None = None
