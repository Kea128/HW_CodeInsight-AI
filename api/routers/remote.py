import asyncio

from fastapi import APIRouter, HTTPException, Response

from api.routers.continuous import manager as continuous_manager
from api.schemas import (
    RemoteProjectRequest,
    RemoteProjectStatus,
    SSHFingerprintProbeRequest,
    SSHFingerprintProbeResponse,
)
from api.services.remote import RemoteProjectError, RemoteSyncManager
from api.services.ssh_client import probe_host_fingerprint

router = APIRouter(prefix="/remote", tags=["remote-analysis"])
manager = RemoteSyncManager(continuous_manager)


@router.post("/fingerprint", response_model=SSHFingerprintProbeResponse)
async def probe_remote_fingerprint(request: SSHFingerprintProbeRequest):
    try:
        value, algorithm = await asyncio.to_thread(
            probe_host_fingerprint, request.host, request.port
        )
        return {
            "host": request.host,
            "port": request.port,
            "fingerprint": value,
            "algorithm": algorithm,
            "confirmation_required": True,
        }
    except RemoteProjectError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


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


@router.post("/projects/{project_id}/retry", response_model=RemoteProjectStatus)
async def retry_remote_project(project_id: str):
    return await sync_remote_project(project_id)


@router.post("/projects/{project_id}/analyze", response_model=RemoteProjectStatus)
async def analyze_remote_project(project_id: str):
    try:
        return await manager.analyze(project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="远程项目不存在") from error


@router.post("/projects/{project_id}/cancel", status_code=202)
async def cancel_remote_operation(project_id: str):
    try:
        cancelled = await manager.cancel(project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="远程项目不存在") from error
    if not cancelled:
        raise HTTPException(status_code=409, detail="远程项目当前没有可取消的后台操作")
    return {"cancelled": True}


@router.delete("/projects/{project_id}", status_code=204)
async def delete_remote_project(project_id: str):
    if not await manager.remove(project_id):
        raise HTTPException(status_code=404, detail="远程项目不存在")
    return Response(status_code=204)
