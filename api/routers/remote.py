import asyncio

from fastapi import APIRouter, HTTPException, Response

from api.routers.continuous import manager as continuous_manager
from api.schemas import (
    RemoteProjectRequest,
    RemoteProjectStatus,
    RemoteScopeCreateRequest,
    RemoteScopeStatus,
    SSHFingerprintProbeRequest,
    SSHFingerprintProbeResponse,
)
from api.schemas.knowledge import KnowledgeDetectResponse
from api.services.remote import RemoteProjectError, RemoteSyncManager
from api.services.ssh_client import CredentialStoreUnavailable, probe_host_fingerprint

router = APIRouter(prefix="/remote", tags=["remote-analysis"])
manager = RemoteSyncManager(continuous_manager, store=continuous_manager.store)


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
    except CredentialStoreUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
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
    except RemoteProjectError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/projects/{project_id}/scopes", response_model=list[RemoteScopeStatus])
async def list_remote_scopes(project_id: str):
    try:
        return manager.list_scopes(project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="远程项目不存在") from error


@router.post(
    "/projects/{project_id}/scopes/detect", response_model=KnowledgeDetectResponse
)
async def detect_remote_scopes(project_id: str):
    try:
        project = manager.store.get_remote_project(project_id)
        if not project:
            raise KeyError(project_id)
        return {
            "workspace_root": project["remote_path"],
            "candidates": manager.detect_scopes(project_id),
        }
    except KeyError as error:
        raise HTTPException(status_code=404, detail="远程项目不存在") from error
    except RemoteProjectError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/projects/{project_id}/scopes", response_model=list[RemoteScopeStatus])
async def create_remote_scopes(project_id: str, request: RemoteScopeCreateRequest):
    try:
        return manager.create_scopes(project_id, request.included_dirs)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="远程项目不存在") from error
    except RemoteProjectError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post(
    "/projects/{project_id}/scopes/{space_id}/analyze",
    response_model=RemoteProjectStatus,
)
async def analyze_remote_scope(project_id: str, space_id: str):
    try:
        return await manager.analyze_scope(project_id, space_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="远程子分析不存在") from error
    except RemoteProjectError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/projects/{project_id}/scopes/{space_id}", status_code=204)
async def delete_remote_scope(project_id: str, space_id: str):
    try:
        deleted = manager.delete_scope(project_id, space_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="远程项目不存在") from error
    if not deleted:
        raise HTTPException(status_code=404, detail="远程子分析不存在")
    return Response(status_code=204)


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
