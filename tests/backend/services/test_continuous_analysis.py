from types import SimpleNamespace

import pytest

from api.schemas import TaskStatus, WikiTaskRequest
from api.services.continuous import ContinuousAnalysisManager, _scan_files
from api.services.wiki.store import WikiTaskStore


class FakeRegistry:
    def __init__(self, store):
        self.store = store
        self.tasks = {}
        self.submissions = []

    def get(self, task_id):
        return self.tasks.get(task_id)

    async def submit(self, task, runner):
        self.submissions.append(task)
        return SimpleNamespace(task_id=task.repo_key, joined=False)


@pytest.mark.asyncio
async def test_changes_during_active_analysis_queue_follow_up(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "source.py"
    source.write_text("before", encoding="utf-8")
    request = WikiTaskRequest(
        owner="local",
        repo="demo",
        type="local",
        repo_url=str(repository),
    )
    store = WikiTaskStore(str(tmp_path / "tasks.db"))
    registry = FakeRegistry(store)
    manager = ContinuousAnalysisManager(registry, store=store)
    baseline = _scan_files(request)
    project = {
        "id": request.repo_key,
        "request": request.model_dump(),
        "enabled": True,
        "poll_seconds": 2,
        "file_hashes": baseline,
        "last_scan_at": 0,
        "last_task_id": "active-task",
        "pending_changes": False,
    }
    store.save_continuous_project(project)
    active = SimpleNamespace(status=TaskStatus.GENERATING)
    registry.tasks["active-task"] = active
    source.write_text("after", encoding="utf-8")

    await manager.scan_once()

    pending = store.list_continuous_projects()[0]
    assert pending["pending_changes"] is True
    assert pending["file_hashes"] == baseline
    assert registry.submissions == []

    active.status = TaskStatus.COMPLETED
    pending["last_scan_at"] = 0
    store.save_continuous_project(pending)
    await manager.scan_once()

    updated = store.list_continuous_projects()[0]
    assert len(registry.submissions) == 1
    assert updated["pending_changes"] is False
    assert updated["file_hashes"] == _scan_files(request)


def test_continuous_project_can_pause_and_resume(tmp_path):
    store = WikiTaskStore(str(tmp_path / "tasks.db"))
    manager = ContinuousAnalysisManager(FakeRegistry(store), store=store)
    project = {
        "id": "local-demo",
        "request": {
            "owner": "local",
            "repo": "demo",
            "type": "local",
            "repo_url": str(tmp_path),
        },
        "enabled": True,
        "poll_seconds": 15,
        "file_hashes": {},
        "last_scan_at": 0,
        "last_task_id": None,
        "pending_changes": False,
    }
    store.save_continuous_project(project)

    assert manager.set_enabled("local-demo", False)["enabled"] is False
    assert store.list_continuous_projects()[0]["enabled"] is False
    assert manager.set_enabled("local-demo", True)["enabled"] is True
    assert manager.set_enabled("missing", False) is None
