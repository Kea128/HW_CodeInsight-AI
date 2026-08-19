import pytest

from api.schemas import WikiPage, WikiStructureModel, WikiTaskRequest
from api.services.wiki.store import WikiTaskStore
from api.services.wiki.tasks import TaskRegistry, TaskStatus, WikiTask


def _task() -> WikiTask:
    return WikiTask.from_wiki_request(
        WikiTaskRequest(
            owner="local",
            repo="demo",
            type="local",
            repo_url="/tmp/demo",
            token="do-not-persist",
        )
    )


def test_store_round_trip_omits_token(tmp_path):
    store = WikiTaskStore(str(tmp_path / "tasks.db"))
    task = _task()
    task.status = TaskStatus.GENERATING
    task.wiki_structure = WikiStructureModel(
        id="wiki",
        title="Demo",
        description="",
        pages=[
            WikiPage(
                id="one",
                title="One",
                content="",
                filePaths=[],
                importance="high",
                relatedPages=[],
            )
        ],
    )
    task.generated_pages["one"] = task.wiki_structure.pages[0].model_copy(
        update={"content": "checkpoint"}
    )
    task.pages_done = 1

    store.save(task.snapshot())
    restored = store.load(task.repo_key)

    assert restored is not None
    assert "token" not in restored["request"]
    assert restored["generated_pages"]["one"]["content"] == "checkpoint"
    assert restored["status"] == "generating"


@pytest.mark.asyncio
async def test_registry_recovers_only_missing_pages(tmp_path):
    store = WikiTaskStore(str(tmp_path / "tasks.db"))
    original = _task()
    original.status = TaskStatus.GENERATING
    original.wiki_structure = WikiStructureModel(
        id="wiki",
        title="Demo",
        description="",
        pages=[
            WikiPage(
                id=page_id,
                title=page_id,
                content="",
                filePaths=[],
                importance="high",
                relatedPages=[],
            )
            for page_id in ("done", "remaining")
        ],
    )
    original.generated_pages["done"] = original.wiki_structure.pages[0].model_copy(
        update={"content": "saved"}
    )
    original.pages_done = 1
    store.save(original.snapshot())

    registry = TaskRegistry(store=store)
    resumed = []

    async def runner(task):
        resumed.append(task)
        task.status = TaskStatus.COMPLETED
        task.persist("completed")

    assert await registry.recover(runner) == 1
    task = registry.get(original.repo_key)
    await task.task

    assert set(task.generated_pages) == {"done"}
    assert task.pages_done == 1
    assert task.request.token is None
    assert resumed == [task]
