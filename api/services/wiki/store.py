"""SQLite persistence for long-running wiki analysis tasks."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from api.utils import deepwiki_root


SCHEMA_VERSION = 4


def default_database_path() -> str:
    configured = os.environ.get("CODEINSIGHT_DB_PATH")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(deepwiki_root(), "codeinsight.db")


class WikiTaskStore:
    """Small synchronous store; writes are short and protected across workers."""

    def __init__(self, path: str | None = None):
        self.path = path or default_database_path()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wiki_tasks (
                    id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pages_done INTEGER NOT NULL DEFAULT 0,
                    current_page_ids_json TEXT NOT NULL DEFAULT '[]',
                    wiki_structure_json TEXT,
                    generated_pages_json TEXT NOT NULL DEFAULT '{}',
                    default_branch TEXT NOT NULL DEFAULT 'main',
                    error TEXT,
                    submitted_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES wiki_tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_wiki_tasks_status
                    ON wiki_tasks(status, submitted_at);
                CREATE INDEX IF NOT EXISTS idx_task_events_task
                    ON task_events(task_id, sequence);

                CREATE TABLE IF NOT EXISTS continuous_projects (
                    id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    night_start TEXT,
                    night_end TEXT,
                    poll_seconds INTEGER NOT NULL DEFAULT 15,
                    file_hashes_json TEXT NOT NULL DEFAULT '{}',
                    last_scan_at INTEGER,
                    last_task_id TEXT,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS remote_projects (
                    id TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 22,
                    username TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    credential_id TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'ollama',
                    model TEXT,
                    language TEXT NOT NULL DEFAULT 'zh',
                    host_fingerprint TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    poll_seconds INTEGER NOT NULL DEFAULT 60,
                    last_sync_at INTEGER,
                    last_error TEXT,
                    stage TEXT NOT NULL DEFAULT 'saved',
                    files_seen INTEGER NOT NULL DEFAULT 0,
                    files_excluded INTEGER NOT NULL DEFAULT 0,
                    files_oversize INTEGER NOT NULL DEFAULT 0,
                    symlinks_skipped INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            remote_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(remote_projects)")
            }
            for name, definition in {
                "stage": "TEXT NOT NULL DEFAULT 'saved'",
                "files_seen": "INTEGER NOT NULL DEFAULT 0",
                "files_excluded": "INTEGER NOT NULL DEFAULT 0",
                "files_oversize": "INTEGER NOT NULL DEFAULT 0",
                "symlinks_skipped": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in remote_columns:
                    connection.execute(
                        f"ALTER TABLE remote_projects ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, int(time.time() * 1000)),
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def save(self, snapshot: dict[str, Any], event_type: str = "checkpoint") -> None:
        now = int(time.time() * 1000)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO wiki_tasks (
                    id, request_json, status, pages_done,
                    current_page_ids_json, wiki_structure_json,
                    generated_pages_json, default_branch, error,
                    submitted_at, updated_at, pause_requested, cancel_requested
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    request_json=excluded.request_json,
                    status=excluded.status,
                    pages_done=excluded.pages_done,
                    current_page_ids_json=excluded.current_page_ids_json,
                    wiki_structure_json=excluded.wiki_structure_json,
                    generated_pages_json=excluded.generated_pages_json,
                    default_branch=excluded.default_branch,
                    error=excluded.error,
                    updated_at=excluded.updated_at,
                    pause_requested=excluded.pause_requested,
                    cancel_requested=excluded.cancel_requested
                """,
                (
                    snapshot["id"],
                    self._json(snapshot["request"]),
                    snapshot["status"],
                    snapshot["pages_done"],
                    self._json(snapshot["current_page_ids"]),
                    self._json(snapshot["wiki_structure"])
                    if snapshot.get("wiki_structure") is not None
                    else None,
                    self._json(snapshot.get("generated_pages", {})),
                    snapshot.get("default_branch", "main"),
                    snapshot.get("error"),
                    snapshot["submitted_at"],
                    now,
                    int(snapshot.get("pause_requested", False)),
                    int(snapshot.get("cancel_requested", False)),
                ),
            )
            connection.execute(
                "INSERT INTO task_events(task_id, event_type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    snapshot["id"],
                    event_type,
                    self._json(
                        {
                            "status": snapshot["status"],
                            "pages_done": snapshot["pages_done"],
                        }
                    ),
                    now,
                ),
            )

    def load(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM wiki_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._decode(row) if row else None

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM wiki_tasks ORDER BY submitted_at"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def list_recoverable(self) -> list[dict[str, Any]]:
        terminal = ("completed", "failed", "cancelled")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM wiki_tasks WHERE status NOT IN (?, ?, ?) "
                "ORDER BY submitted_at",
                terminal,
            ).fetchall()
        return [self._decode(row) for row in rows]

    def save_continuous_project(self, project: dict[str, Any]) -> None:
        now = int(time.time() * 1000)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO continuous_projects (
                    id, request_json, enabled, night_start, night_end,
                    poll_seconds, file_hashes_json, last_scan_at,
                    last_task_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    request_json=excluded.request_json,
                    enabled=excluded.enabled,
                    night_start=excluded.night_start,
                    night_end=excluded.night_end,
                    poll_seconds=excluded.poll_seconds,
                    file_hashes_json=excluded.file_hashes_json,
                    last_scan_at=excluded.last_scan_at,
                    last_task_id=excluded.last_task_id,
                    updated_at=excluded.updated_at
                """,
                (
                    project["id"],
                    self._json(project["request"]),
                    int(project.get("enabled", True)),
                    project.get("night_start"),
                    project.get("night_end"),
                    project.get("poll_seconds", 15),
                    self._json(project.get("file_hashes", {})),
                    project.get("last_scan_at"),
                    project.get("last_task_id"),
                    now,
                ),
            )

    def list_continuous_projects(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM continuous_projects ORDER BY id"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "request": json.loads(row["request_json"]),
                "enabled": bool(row["enabled"]),
                "night_start": row["night_start"],
                "night_end": row["night_end"],
                "poll_seconds": row["poll_seconds"],
                "file_hashes": json.loads(row["file_hashes_json"]),
                "last_scan_at": row["last_scan_at"],
                "last_task_id": row["last_task_id"],
            }
            for row in rows
        ]

    def delete_continuous_project(self, project_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM continuous_projects WHERE id = ?", (project_id,)
            )
        return cursor.rowcount > 0

    def save_remote_project(self, project: dict[str, Any]) -> None:
        now = int(time.time() * 1000)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO remote_projects (
                    id, host, port, username, remote_path, local_path,
                    credential_id, provider, model, language, host_fingerprint,
                    enabled, poll_seconds, last_sync_at, last_error, stage,
                    files_seen, files_excluded, files_oversize, symlinks_skipped,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    host=excluded.host,
                    port=excluded.port,
                    username=excluded.username,
                    remote_path=excluded.remote_path,
                    local_path=excluded.local_path,
                    credential_id=excluded.credential_id,
                    provider=excluded.provider,
                    model=excluded.model,
                    language=excluded.language,
                    host_fingerprint=excluded.host_fingerprint,
                    enabled=excluded.enabled,
                    poll_seconds=excluded.poll_seconds,
                    last_sync_at=excluded.last_sync_at,
                    last_error=excluded.last_error,
                    stage=excluded.stage,
                    files_seen=excluded.files_seen,
                    files_excluded=excluded.files_excluded,
                    files_oversize=excluded.files_oversize,
                    symlinks_skipped=excluded.symlinks_skipped,
                    updated_at=excluded.updated_at
                """,
                (
                    project["id"],
                    project["host"],
                    project.get("port", 22),
                    project["username"],
                    project["remote_path"],
                    project["local_path"],
                    project["credential_id"],
                    project.get("provider", "ollama"),
                    project.get("model"),
                    project.get("language", "zh"),
                    project.get("host_fingerprint"),
                    int(project.get("enabled", True)),
                    project.get("poll_seconds", 60),
                    project.get("last_sync_at"),
                    project.get("last_error"),
                    project.get("stage", "saved"),
                    project.get("files_seen", 0),
                    project.get("files_excluded", 0),
                    project.get("files_oversize", 0),
                    project.get("symlinks_skipped", 0),
                    now,
                ),
            )

    def get_remote_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_projects WHERE id = ?", (project_id,)
            ).fetchone()
        return self._decode_remote_project(row) if row else None

    def list_remote_projects(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_projects ORDER BY id"
            ).fetchall()
        return [self._decode_remote_project(row) for row in rows]

    def delete_remote_project(self, project_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM remote_projects WHERE id = ?", (project_id,)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _decode_remote_project(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "host": row["host"],
            "port": row["port"],
            "username": row["username"],
            "remote_path": row["remote_path"],
            "local_path": row["local_path"],
            "credential_id": row["credential_id"],
            "provider": row["provider"],
            "model": row["model"],
            "language": row["language"],
            "host_fingerprint": row["host_fingerprint"],
            "enabled": bool(row["enabled"]),
            "poll_seconds": row["poll_seconds"],
            "last_sync_at": row["last_sync_at"],
            "last_error": row["last_error"],
            "stage": row["stage"],
            "files_seen": row["files_seen"],
            "files_excluded": row["files_excluded"],
            "files_oversize": row["files_oversize"],
            "symlinks_skipped": row["symlinks_skipped"],
        }

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "request": json.loads(row["request_json"]),
            "status": row["status"],
            "pages_done": row["pages_done"],
            "current_page_ids": json.loads(row["current_page_ids_json"]),
            "wiki_structure": json.loads(row["wiki_structure_json"])
            if row["wiki_structure_json"]
            else None,
            "generated_pages": json.loads(row["generated_pages_json"]),
            "default_branch": row["default_branch"],
            "error": row["error"],
            "submitted_at": row["submitted_at"],
            "updated_at": row["updated_at"],
            "pause_requested": bool(row["pause_requested"]),
            "cancel_requested": bool(row["cancel_requested"]),
        }
