from fastapi import APIRouter, HTTPException, Response

from api.schemas import ContinuousAnalysisRequest, ContinuousProject
from api.services.continuous import ContinuousAnalysisManager
from api.services.wiki import registry, wiki_task_store

router = APIRouter(prefix="/continuous", tags=["continuous-analysis"])
manager = ContinuousAnalysisManager(registry, store=wiki_task_store)


@router.get("/projects", response_model=list[ContinuousProject])
async def list_continuous_projects():
    return manager.list_projects()


@router.post("/projects", response_model=ContinuousProject)
async def register_continuous_project(request: ContinuousAnalysisRequest):
    try:
        return await manager.register(
            request.task,
            night_start=request.night_start,
            night_end=request.night_end,
            poll_seconds=request.poll_seconds,
            analyze_now=request.analyze_now,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/projects/{project_id}/pause", response_model=ContinuousProject)
async def pause_continuous_project(project_id: str):
    project = manager.set_enabled(project_id, False)
    if project is None:
        raise HTTPException(status_code=404, detail="Continuous project not found")
    return project


@router.post("/projects/{project_id}/resume", response_model=ContinuousProject)
async def resume_continuous_project(project_id: str):
    project = manager.set_enabled(project_id, True)
    if project is None:
        raise HTTPException(status_code=404, detail="Continuous project not found")
    return project


@router.delete("/projects/{project_id}", status_code=204)
async def delete_continuous_project(project_id: str):
    if not manager.remove(project_id):
        raise HTTPException(status_code=404, detail="Continuous project not found")
    return Response(status_code=204)
