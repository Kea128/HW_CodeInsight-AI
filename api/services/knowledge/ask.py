"""Answer questions from a knowledge space, then backfill the wiki if needed."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from api.logger import get_logger
from api.schemas import (
    ChatCompletionRequest,
    ChatMessage,
    WikiCacheData,
    WikiPage,
    WikiStructureModel,
)
from api.schemas.knowledge import KnowledgeAskRequest, KnowledgeSpace
from api.schemas.repo import RepoInfo
from api.services.knowledge.search import search_wiki_pages
from api.services.knowledge.spaces import knowledge_label
from api.services.research import prepare_repo_index, research_chat
from api.services.wiki.io import read_wiki_cache, save_wiki_cache
from api.services.wiki.store import WikiTaskStore

logger = get_logger(__name__)
INSUFFICIENT = "INSUFFICIENT_CONTEXT"


def _space_request(space: KnowledgeSpace, provider: str | None, model: str | None):
    root_name = space.workspace_root.replace("\\", "/").rstrip("/").split("/")[-1]
    return {
        "repo_url": space.workspace_root,
        "type": "local",
        "owner": "local",
        "repo": root_name or "project",
        "language": space.language,
        "provider": provider or space.provider or "openai_compatible",
        "model": model or space.model,
        "included_dirs": space.included_dirs,
        "excluded_dirs": space.excluded_dirs,
        "space_id": space.space_id,
    }


async def ask_knowledge_space(
    store: WikiTaskStore,
    request: KnowledgeAskRequest,
) -> AsyncIterator[str]:
    space = store.get_knowledge_space(request.space_id)
    if space is None:
        raise ValueError("知识库不存在，请先选择工作目录并生成知识库")

    cache = await read_wiki_cache(
        owner="local",
        repo=space.label,
        repo_type="local",
        language=space.language,
        space_id=space.space_id,
    )
    pages = dict(cache.generated_pages) if cache else {}
    hits = search_wiki_pages(pages, request.question)
    wiki_context = "\n\n".join(
        f"## {page.title}\n{page.content[:4000]}" for page in hits
    )

    rag_context = ""
    try:
        rag = await prepare_repo_index(
            ChatCompletionRequest(
                **_space_request(space, request.provider, request.model),
                messages=[ChatMessage(role="user", content=request.question)],
            )
        )
        retrieved = rag.call(request.question, language=request.language)
        snippets: list[str] = []
        for output in retrieved or []:
            for document in getattr(output, "documents", []) or []:
                text = getattr(document, "text", None) or str(document)
                snippets.append(text[:1200])
        rag_context = "\n\n".join(snippets[:6])
    except Exception as error:  # noqa: BLE001 - keyword/wiki fallback is intentional
        logger.warning("Knowledge ask RAG unavailable: %s", error)

    if hits or rag_context:
        prompt = (
            "你是代码知识库助手。只根据提供的知识库与代码片段回答。"
            "若信息不足，第一行只输出 INSUFFICIENT_CONTEXT。\n\n"
            f"问题：{request.question}\n\n"
            f"<wiki>\n{wiki_context or '（无wiki命中）'}\n</wiki>\n\n"
            f"<code>\n{rag_context or '（无代码检索）'}\n</code>"
        )
        chat = ChatCompletionRequest(
            **_space_request(space, request.provider, request.model),
            messages=[ChatMessage(role="user", content=prompt)],
        )
        answer = ""
        async for chunk in await research_chat(chat):
            answer += chunk
            if INSUFFICIENT not in answer:
                yield chunk
        if INSUFFICIENT not in answer:
            return
        yield "\n\n正在补充分析当前知识库…\n\n"
    else:
        yield "知识库尚未覆盖该问题，正在补充分析…\n\n"

    supplement = await _generate_supplement(space, request)
    if not supplement:
        yield "当前知识库仍不足以回答该问题，请先对该子目录运行完整分析。"
        return

    pages[supplement.id] = supplement
    await _persist_supplement(space, cache, pages, supplement)
    yield f"已更新知识库页面《{supplement.title}》。\n\n"
    follow = ChatCompletionRequest(
        **_space_request(space, request.provider, request.model),
        messages=[
            ChatMessage(
                role="user",
                content=(
                    f"根据以下新分析回答：{request.question}\n\n"
                    f"## {supplement.title}\n{supplement.content}"
                ),
            )
        ],
    )
    async for chunk in await research_chat(follow):
        yield chunk


async def _persist_supplement(
    space: KnowledgeSpace,
    cache: WikiCacheData | None,
    pages: dict[str, WikiPage],
    supplement: WikiPage,
) -> None:
    if cache is None:
        cache = WikiCacheData(
            wiki_structure=WikiStructureModel(
                id="wiki",
                title=space.label,
                description=space.label,
                pages=list(pages.values()),
            ),
            generated_pages=pages,
            repo=RepoInfo(
                owner="local",
                repo=space.label,
                type="local",
                token=None,
                repoUrl=space.workspace_root,
            ),
            provider=space.provider,
            model=space.model,
        )
    else:
        cache.generated_pages = pages
        if cache.wiki_structure and supplement.id not in {
            page.id for page in cache.wiki_structure.pages
        }:
            cache.wiki_structure.pages.append(supplement)
    await save_wiki_cache(
        owner="local",
        repo=space.label,
        repo_type="local",
        language=space.language,
        wiki_cache=cache,
        space_id=space.space_id,
    )


async def _generate_supplement(
    space: KnowledgeSpace, request: KnowledgeAskRequest
) -> WikiPage | None:
    prompt = (
        f"请针对问题补充分析代码并写成一页中文知识库文档。问题：{request.question}\n"
        f"范围：{knowledge_label(space.workspace_root, space.included_dirs)}"
    )
    chat = ChatCompletionRequest(
        **_space_request(space, request.provider, request.model),
        messages=[ChatMessage(role="user", content=prompt)],
    )
    try:
        content = ""
        async for chunk in await research_chat(chat):
            content += chunk
    except Exception as error:  # noqa: BLE001
        logger.warning("Supplement analysis failed: %s", error)
        return None
    if not content.strip():
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", request.question.lower())[:24] or "ask"
    return WikiPage(
        id=f"ask-{slug}",
        title=f"问答补充：{request.question[:40]}",
        content=content.strip(),
        filePaths=space.included_dirs,
        importance="medium",
        relatedPages=[],
    )
