from fastapi import APIRouter, HTTPException, Response

from api.routers.continuous import manager as continuous_manager
from api.schemas import RemoteProjectRequest, RemoteProjectStatus
from api.services.remote import RemoteProjectError, RemoteSyncManager

router = APIRouter(prefix="/remote", tags=["remote-analysis"])
manager = RemoteSyncManager(continuous_manager)


@router.get("/projects", response_model=list[RemoteProjectStatus])
async def list_remote_projects():
    return manager.list_projects()


@router.post("/projects", response_model=RemoteProjectStatus)
async def create_remote_project(request: RemoteProjectRequest):
    try:
        return await manager.create(request)
    except RemoteProjectError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="无法安全保存远程项目，请检查 Windows 凭据管理器",
        ) from error


@router.post("/projects/{project_id}/sync", response_model=RemoteProjectStatus)
async def sync_remote_project(project_id: str):
    try:
        return await manager.sync(project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="远程项目不存在") from error


@router.delete("/projects/{project_id}", status_code=204)
async def delete_remote_project(project_id: str):
    if not manager.remove(project_id):
        raise HTTPException(status_code=404, detail="远程项目不存在")
    return Response(status_code=204)
