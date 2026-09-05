from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from api.logger import get_logger
from api.schemas import WikiTaskRequest
from api.schemas.knowledge import (
    KnowledgeAskRequest,
    KnowledgeDetectRequest,
    KnowledgeDetectResponse,
    KnowledgeSpace,
    KnowledgeSpaceCreateRequest,
    KnowledgeSpaceCreateResponse,
)
from api.services.continuous import ContinuousAnalysisManager
from api.services.knowledge.ask import ask_knowledge_space
from api.services.knowledge.spaces import detect_subrepos, upsert_spaces
from api.services.wiki import generate_repo_wiki, registry, wiki_task_store
from api.services.wiki.tasks import WikiTask

logger = get_logger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _task_request(space: KnowledgeSpace, request: KnowledgeSpaceCreateRequest) -> WikiTaskRequest:
    root_name = space.workspace_root.replace("\\", "/").rstrip("/").split("/")[-1]
    return WikiTaskRequest(
        repo_url=space.workspace_root,
        type="local",
        owner="local",
        repo=root_name or "project",
        language=space.language,
        provider=request.provider or space.provider or "openai_compatible",
        model=request.model or space.model,
        included_dirs=space.included_dirs,
        excluded_dirs=space.excluded_dirs,
        comprehensive=True,
        space_id=space.space_id,
    )


@router.get("/spaces", response_model=list[KnowledgeSpace])
async def list_knowledge_spaces():
    return wiki_task_store.list_knowledge_spaces()


@router.post("/spaces/detect", response_model=KnowledgeDetectResponse)
async def detect_knowledge_spaces(request: KnowledgeDetectRequest):
    try:
        candidates = detect_subrepos(request.workspace_root)
        from api.services.knowledge.spaces import normalize_workspace_root

        return KnowledgeDetectResponse(
            workspace_root=normalize_workspace_root(request.workspace_root),
            candidates=candidates,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/spaces", response_model=KnowledgeSpaceCreateResponse)
async def create_knowledge_spaces(request: KnowledgeSpaceCreateRequest):
    try:
        spaces = upsert_spaces(
            wiki_task_store,
            workspace_root=request.workspace_root,
            included_dirs=request.included_dirs,
            excluded_dirs=request.excluded_dirs,
            language=request.language,
            provider=request.provider,
            model=request.model,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    task_ids: list[str] = []
    if request.analyze_now:
        from api.routers.continuous import manager as continuous_manager

        manager: ContinuousAnalysisManager = continuous_manager
        for space in spaces:
            task_request = _task_request(space, request)
            try:
                project = await manager.register(
                    task_request,
                    night_start=request.night_start,
                    night_end=request.night_end,
                    poll_seconds=request.poll_seconds,
                    analyze_now=True,
                )
                task_id = project.get("last_task_id") or project["id"]
            except Exception:
                logger.exception("Falling back to direct wiki submit for %s", space.space_id)
                result = await registry.submit(
                    WikiTask.from_wiki_request(task_request),
                    generate_repo_wiki,
                )
                task_id = result.task_id
            wiki_task_store.set_knowledge_space_task(space.space_id, task_id)
            space.last_task_id = task_id
            task_ids.append(task_id)
    return KnowledgeSpaceCreateResponse(spaces=spaces, task_ids=task_ids)


@router.post("/ask")
async def ask_knowledge(request: KnowledgeAskRequest):
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        stream = ask_knowledge_space(wiki_task_store, request)
        first = await stream.__anext__()
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except StopAsyncIteration:
        raise HTTPException(status_code=500, detail="问答没有返回内容") from None

    async def merge():
        yield first
        async for chunk in stream:
            yield chunk

    return StreamingResponse(merge(), media_type="text/plain; charset=utf-8")
