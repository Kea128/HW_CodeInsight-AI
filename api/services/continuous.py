"""Persistent file watching and night-window analysis scheduling."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from api.logger import get_logger
from api.schemas import WikiTaskRequest
from api.services.wiki.store import WikiTaskStore
from api.services.wiki.tasks import TaskRegistry, WikiTask, generate_repo_wiki

logger = get_logger(__name__)

IGNORED_DIRS = {
    ".git",
    ".next",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "out",
    "target",
}


def _minute_of_day(value: str) -> int:
    hour, minute = value.split(":", 1)
    result = int(hour) * 60 + int(minute)
    if result < 0 or result >= 24 * 60:
        raise ValueError("Time must use HH:MM in the 00:00-23:59 range")
    return result


def _inside_window(start: str | None, end: str | None, now: datetime) -> bool:
    if not start or not end:
        return True
    current = now.hour * 60 + now.minute
    start_minute = _minute_of_day(start)
    end_minute = _minute_of_day(end)
    if start_minute <= end_minute:
        return start_minute <= current < end_minute
    return current >= start_minute or current < end_minute


def _scan_files(request: WikiTaskRequest) -> dict[str, str]:
    root = Path(request.repo_url).expanduser().resolve()
    if request.type != "local" or not root.is_dir():
        return {}

    hashes: dict[str, str] = {}
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [
            name
            for name in dirs
            if name not in IGNORED_DIRS and not name.startswith(".")
        ]
        for filename in files:
            path = Path(current_root, filename)
            relative = path.relative_to(root).as_posix()
            if filename.startswith(".") or _excluded(relative, request):
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, PermissionError):
                continue
            hashes[relative] = digest
    return hashes


def _excluded(relative: str, request: WikiTaskRequest) -> bool:
    parts = relative.split("/")
    if request.included_dirs and not any(
        relative.startswith(value.rstrip("/") + "/") for value in request.included_dirs
    ):
        return True
    if request.included_files and not any(
        fnmatch.fnmatch(relative, pattern) for pattern in request.included_files
    ):
        return True
    if any(part in request.excluded_dirs for part in parts[:-1]):
        return True
    return any(fnmatch.fnmatch(relative, pattern) for pattern in request.excluded_files)


class ContinuousAnalysisManager:
    def __init__(
        self,
        registry: TaskRegistry,
        store: WikiTaskStore | None = None,
    ):
        self.registry = registry
        self.store = store or WikiTaskStore()
        self._runner: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def list_projects(self) -> list[dict[str, Any]]:
        return self.store.list_continuous_projects()

    async def register(
        self,
        request: WikiTaskRequest,
        *,
        night_start: str | None = None,
        night_end: str | None = None,
        poll_seconds: int = 15,
        analyze_now: bool = True,
    ) -> dict[str, Any]:
        if request.type != "local" or not os.path.isdir(request.repo_url):
            raise ValueError("Continuous analysis currently requires a local repository")
        if night_start:
            _minute_of_day(night_start)
        if night_end:
            _minute_of_day(night_end)
        if bool(night_start) != bool(night_end):
            raise ValueError("night_start and night_end must be provided together")

        project = {
            "id": request.repo_key,
            "request": request.model_dump(exclude={"token"}),
            "enabled": True,
            "night_start": night_start,
            "night_end": night_end,
            "poll_seconds": max(2, poll_seconds),
            "file_hashes": await asyncio.to_thread(_scan_files, request),
            "last_scan_at": int(time.time() * 1000),
            "last_task_id": None,
        }
        self.store.save_continuous_project(project)
        if analyze_now and _inside_window(night_start, night_end, datetime.now()):
            result = await self.registry.submit(
                WikiTask.from_wiki_request(request.model_copy(update={"force": True})),
                generate_repo_wiki,
            )
            project["last_task_id"] = result.task_id
            self.store.save_continuous_project(project)
        return project

    def remove(self, project_id: str) -> bool:
        return self.store.delete_continuous_project(project_id)

    def start(self) -> None:
        if self._runner is None or self._runner.done():
            self._stopping.clear()
            self._runner = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping.set()
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
            self._runner = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            await self.scan_once()
            await asyncio.sleep(2)

    async def scan_once(self) -> None:
        now_ms = int(time.time() * 1000)
        for project in self.store.list_continuous_projects():
            if not project["enabled"]:
                continue
            elapsed = now_ms - (project.get("last_scan_at") or 0)
            if elapsed < project["poll_seconds"] * 1000:
                continue
            request = WikiTaskRequest.model_validate(project["request"])
            current = await asyncio.to_thread(_scan_files, request)
            changed = current != project.get("file_hashes", {})
            project["last_scan_at"] = now_ms
            inside_window = _inside_window(
                project.get("night_start"),
                project.get("night_end"),
                datetime.now(),
            )
            if changed and inside_window:
                result = await self.registry.submit(
                    WikiTask.from_wiki_request(
                        request.model_copy(update={"force": True})
                    ),
                    generate_repo_wiki,
                )
                project["last_task_id"] = result.task_id
                if not result.joined:
                    project["file_hashes"] = current
                logger.info(
                    "File changes queued continuous analysis for %s", project["id"]
                )
            elif not changed:
                project["file_hashes"] = current
            self.store.save_continuous_project(project)
