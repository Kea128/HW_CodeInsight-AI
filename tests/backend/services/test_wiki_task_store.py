import asyncio
import json
import sqlite3

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


def test_remote_project_round_trip_never_contains_password(tmp_path):
    store = WikiTaskStore(str(tmp_path / "tasks.db"))
    project = {
        "id": "remote-one",
        "host": "10.0.0.8",
        "port": 22,
        "username": "ubuntu",
        "remote_path": "/srv/code/demo",
        "local_path": str(tmp_path / "mirror"),
        "credential_id": "remote-one",
        "provider": "ollama",
        "model": None,
        "language": "zh",
        "host_fingerprint": "SHA256:test",
        "enabled": True,
        "poll_seconds": 60,
        "last_sync_at": 123,
        "last_error": None,
        "dirs_seen": 4,
        "current_path": "/srv/code/src",
        "progress_message": "正在列出远程目录 /srv/code/src",
        "sync_started_at": 456,
        "progress_updated_at": 789,
        "password": "server-password",
    }

    store.save_remote_project(project)
    restored = store.get_remote_project("remote-one")

    assert restored["host"] == project["host"]
    assert restored["credential_id"] == project["credential_id"]
    assert restored["progress_message"] == "正在列出远程目录 /srv/code/src"
    assert restored["dirs_seen"] == 4
    assert restored["progress_updated_at"] == 789
    assert "password" not in restored
    assert b"server-password" not in (tmp_path / "tasks.db").read_bytes()


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


def test_migration_from_v4_retains_terminal_tasks_and_events(tmp_path):
    path = tmp_path / "upgrade.db"
    request = {
        "owner": "local",
        "repo": "legacy",
        "type": "local",
        "repo_url": "/tmp/legacy",
    }
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL
            );
            INSERT INTO schema_migrations VALUES (4, 1);
            CREATE TABLE wiki_tasks (
                id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                status TEXT NOT NULL, pages_done INTEGER NOT NULL DEFAULT 0,
                current_page_ids_json TEXT NOT NULL DEFAULT '[]',
                wiki_structure_json TEXT,
                generated_pages_json TEXT NOT NULL DEFAULT '{}',
                default_branch TEXT NOT NULL DEFAULT 'main', error TEXT,
                submitted_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                pause_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE task_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL, event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE continuous_projects (
                id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, night_start TEXT,
                night_end TEXT, poll_seconds INTEGER NOT NULL DEFAULT 15,
                file_hashes_json TEXT NOT NULL DEFAULT '{}',
                last_scan_at INTEGER, last_task_id TEXT,
                updated_at INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO wiki_tasks VALUES (?, ?, 'completed', 0, '[]', NULL, "
            "'{}', 'main', NULL, 1, 1, 0, 0)",
            ("local_local_legacy", json.dumps(request)),
        )
        connection.execute(
            "INSERT INTO task_events(task_id, event_type, created_at) "
            "VALUES ('local_local_legacy', 'completed', 1)"
        )

    store = WikiTaskStore(str(path))

    assert store.load("local_local_legacy")["status"] == "completed"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(continuous_projects)")
        }
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    assert "pending_changes" in columns
    assert versions == [4, 5, 6, 7, 8, 9]


def test_fresh_database_runs_every_migration(tmp_path):
    path = tmp_path / "fresh.db"
    WikiTaskStore(str(path))

    with sqlite3.connect(path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]

    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_terminal_event_retention_keeps_recent_history(tmp_path):
    store = WikiTaskStore(str(tmp_path / "retention.db"))
    task = _task()
    task.status = TaskStatus.COMPLETED
    for index in range(5):
        store.save(task.snapshot(), f"event-{index}")

    removed = store.prune_terminal_events(
        retention_days=36500, max_events_per_task=2
    )

    with sqlite3.connect(store.path) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (task.repo_key,),
        ).fetchone()[0]
    assert removed == 3
    assert remaining == 2


@pytest.mark.asyncio
async def test_recovery_skips_terminal_history_and_only_events_resumed_tasks(tmp_path):
    store = WikiTaskStore(str(tmp_path / "tasks.db"))
    running = _task()
    running.status = TaskStatus.GENERATING
    store.save(running.snapshot(), "checkpoint")

    paused = _task()
    paused.persisted_id = "legacy-paused"
    paused.status = TaskStatus.PAUSED
    paused.pause_requested = True
    store.save(paused.snapshot(), "paused")

    completed = _task()
    completed.persisted_id = "legacy-completed"
    completed.status = TaskStatus.COMPLETED
    store.save(completed.snapshot(), "completed")

    registry = TaskRegistry(store=store)

    async def runner(task):
        task.status = TaskStatus.COMPLETED

    assert await registry.recover(runner) == 1
    await registry.get(running.repo_key).task

    assert registry.get("legacy-paused") is not None
    assert registry.get("legacy-completed") is None
    with sqlite3.connect(store.path) as connection:
        recovered_ids = {
            row[0]
            for row in connection.execute(
                "SELECT task_id FROM task_events WHERE event_type = 'recovered'"
            )
        }
    assert recovered_ids == {running.repo_key}


@pytest.mark.asyncio
async def test_new_identity_joins_active_legacy_recovery(tmp_path):
    store = WikiTaskStore(str(tmp_path / "tasks.db"))
    original = _task()
    legacy_id = original.request.legacy_repo_key
    original.persisted_id = legacy_id
    original.status = TaskStatus.GENERATING
    store.save(original.snapshot())
    registry = TaskRegistry(store=store)
    release = asyncio.Event()

    async def runner(task):
        await release.wait()

    await registry.recover(runner)
    result = await registry.submit(_task(), runner)

    assert result.joined is True
    assert result.task_id == legacy_id

    release.set()
    await registry.get(legacy_id).task
