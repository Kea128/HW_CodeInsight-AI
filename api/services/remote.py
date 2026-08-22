"""Password-authenticated Ubuntu directory mirroring for desktop analysis."""

from __future__ import annotations

import asyncio
import hashlib
import os
import posixpath
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import paramiko

from api.logger import get_logger
from api.schemas import RemoteProjectRequest, WikiTaskRequest
from api.services.ssh_client import (
    CredentialStore,
    RemoteProjectError,
    connect_ssh,
    friendly_connection_error,
    remote_data_root,
)

if TYPE_CHECKING:
    from api.services.continuous import ContinuousAnalysisManager
    from api.services.wiki.store import WikiTaskStore

logger = get_logger(__name__)

SYNC_LOOP_SECONDS = 2
MAX_REMOTE_FILE_BYTES = 100 * 1024 * 1024
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
SECRET_FILE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials.json",
    "secrets.json",
}
SECRET_FILE_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _project_id(host: str, port: int, username: str, remote_path: str) -> str:
    identity = f"{host.lower()}:{port}:{username}:{remote_path}".encode()
    return f"remote-{hashlib.sha256(identity).hexdigest()[:20]}"


def _safe_repo_name(remote_path: str) -> str:
    name = posixpath.basename(remote_path.rstrip("/")) or "remote"
    cleaned = "".join(character if character.isalnum() else "-" for character in name)
    return cleaned.strip("-") or "remote"


def _safe_owner(username: str, host: str) -> str:
    value = f"{username}@{host}"
    return "".join(
        character if character.isalnum() or character in {"@", ".", "-"} else "-"
        for character in value
    )


def _background_concurrency() -> int:
    try:
        return max(
            1, min(int(os.environ.get("CODEINSIGHT_REMOTE_CONCURRENCY", "3")), 16)
        )
    except ValueError:
        return 3


def _friendly_connection_error(error: Exception) -> RemoteProjectError:
    return friendly_connection_error(error)


def _connect(
    project: dict[str, Any], password: str, known_hosts_path: Path
) -> tuple[paramiko.SSHClient, str]:
    return connect_ssh(project, password, known_hosts_path)


def _valid_remote_name(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and not any(character in '<>:"|?*' for character in name)
        and name.rstrip(" .") == name
        and name.split(".", 1)[0].upper() not in WINDOWS_RESERVED_NAMES
    )


@dataclass
class MirrorResult:
    files_seen: int = 0
    changed: bool = False
    files_excluded: int = 0
    files_oversize: int = 0
    symlinks_skipped: int = 0

    def __iter__(self):
        """Keep the legacy ``count, changed = result`` contract."""
        yield self.files_seen
        yield self.changed

    def as_stats(self) -> dict[str, int]:
        return {
            "files_seen": self.files_seen,
            "files_excluded": self.files_excluded,
            "files_oversize": self.files_oversize,
            "symlinks_skipped": self.symlinks_skipped,
        }


def _secret_remote_file(name: str) -> bool:
    lowered = name.lower()
    return lowered in SECRET_FILE_NAMES or lowered.endswith(SECRET_FILE_SUFFIXES)


def _same_content(first: Path, second: Path) -> bool:
    if not first.is_file() or first.stat().st_size != second.stat().st_size:
        return False
    first_hash = hashlib.sha256()
    second_hash = hashlib.sha256()
    with first.open("rb") as left, second.open("rb") as right:
        for chunk in iter(lambda: left.read(1024 * 1024), b""):
            first_hash.update(chunk)
        for chunk in iter(lambda: right.read(1024 * 1024), b""):
            second_hash.update(chunk)
    return first_hash.digest() == second_hash.digest()


def _mirror_directory(
    sftp: paramiko.SFTPClient,
    remote_root: str,
    local_root: Path,
    cancel_event: threading.Event | None = None,
) -> MirrorResult:
    try:
        root_attributes = sftp.lstat(remote_root)
    except OSError as error:
        raise RemoteProjectError("远程目录不存在或没有读取权限") from error
    if not stat.S_ISDIR(root_attributes.st_mode):
        raise RemoteProjectError("远程路径不是目录")

    local_root.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    result = MirrorResult()

    def visit(remote_dir: str, relative_dir: str = "") -> None:
        try:
            entries = sftp.listdir_attr(remote_dir)
        except OSError as error:
            raise RemoteProjectError("远程目录没有读取权限") from error
        for entry in entries:
            if cancel_event and cancel_event.is_set():
                raise RemoteProjectError("远程同步已取消")
            name = entry.filename
            if not _valid_remote_name(name):
                result.files_excluded += 1
                continue
            relative = f"{relative_dir}/{name}".lstrip("/")
            remote_path = posixpath.join(remote_dir, name)
            if stat.S_ISLNK(entry.st_mode):
                result.symlinks_skipped += 1
                continue
            if stat.S_ISDIR(entry.st_mode):
                if name in IGNORED_DIRS:
                    result.files_excluded += 1
                    continue
                local_directory = local_root.joinpath(*relative.split("/"))
                if local_directory.is_symlink() or (
                    local_directory.exists() and not local_directory.is_dir()
                ):
                    local_directory.unlink()
                    result.changed = True
                local_directory.mkdir(parents=True, exist_ok=True)
                visit(remote_path, relative)
                continue
            if not stat.S_ISREG(entry.st_mode):
                result.files_excluded += 1
                continue
            if _secret_remote_file(name):
                result.files_excluded += 1
                continue
            if entry.st_size is None or entry.st_size > MAX_REMOTE_FILE_BYTES:
                result.files_oversize += 1
                continue

            seen.add(relative)
            result.files_seen += 1
            local_path = local_root.joinpath(*relative.split("/"))
            if local_path.is_symlink():
                local_path.unlink()
                result.changed = True
            elif local_path.is_dir():
                shutil.rmtree(local_path)
                result.changed = True
            local_path.parent.mkdir(parents=True, exist_ok=True)
            modified = int(entry.st_mtime or time.time())
            temporary = local_path.with_name(f".{local_path.name}.codeinsight.tmp")
            try:
                sftp.get(remote_path, str(temporary))
                if _same_content(local_path, temporary):
                    continue
                os.replace(temporary, local_path)
                os.utime(local_path, (int(entry.st_atime or modified), modified))
                result.changed = True
            finally:
                temporary.unlink(missing_ok=True)

    visit(remote_root)

    for local_path in sorted(local_root.rglob("*"), reverse=True):
        if cancel_event and cancel_event.is_set():
            raise RemoteProjectError("远程同步已取消")
        if local_path.is_symlink():
            local_path.unlink(missing_ok=True)
            result.changed = True
        elif local_path.is_file():
            relative = local_path.relative_to(local_root).as_posix()
            if relative not in seen:
                local_path.unlink()
                result.changed = True
        elif local_path.is_dir():
            try:
                local_path.rmdir()
            except OSError:
                pass
    return result


def _sync_project(
    project: dict[str, Any],
    password: str,
    known_hosts_path: Path,
    cancel_event: threading.Event | None = None,
) -> tuple[MirrorResult, str]:
    client, fingerprint = _connect(project, password, known_hosts_path)
    try:
        with client.open_sftp() as sftp:
            result = _mirror_directory(
                sftp,
                project["remote_path"],
                Path(project["local_path"]),
                cancel_event,
            )
        return result, fingerprint
    except RemoteProjectError:
        raise
    except Exception as error:
        raise _friendly_connection_error(error) from error
    finally:
        client.close()


class RemoteSyncManager:
    def __init__(
        self,
        continuous: ContinuousAnalysisManager,
        store: WikiTaskStore | None = None,
        credentials: CredentialStore | None = None,
    ):
        if store is None:
            from api.services.wiki.store import WikiTaskStore

            store = WikiTaskStore()
        self.continuous = continuous
        self.store = store
        self.credentials = credentials or CredentialStore()
        self.known_hosts_path = remote_data_root() / "known_hosts"
        self._runner: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._failures: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        self._remove_listeners: list[Any] = []
        self._concurrency = asyncio.Semaphore(_background_concurrency())

    def _analysis_request(self, project: dict[str, Any]) -> WikiTaskRequest:
        return WikiTaskRequest(
            repo_url=project["local_path"],
            type="local",
            provider=project["provider"],
            model=project.get("model"),
            language=project["language"],
            owner=_safe_owner(project["username"], project["host"]),
            repo=_safe_repo_name(project["remote_path"]),
            comprehensive=True,
            force=True,
        )

    def _continuous_project(self, project: dict[str, Any]) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.continuous.list_projects()
                if item["request"]["repo_url"] == project["local_path"]
            ),
            None,
        )

    def _status(self, project: dict[str, Any]) -> dict[str, Any]:
        continuous_project = self._continuous_project(project)
        registry = getattr(self.continuous, "registry", None)
        task_id = continuous_project.get("last_task_id") if continuous_project else None
        task = registry.get(task_id) if registry and task_id else None
        if project.get("stage") == "analyzing" and task and task.status.is_terminal():
            if task.status.value == "failed":
                self._save_stage(
                    project, "failed", str(getattr(task, "error", None) or "AI 分析失败")
                )
            else:
                self._save_stage(project, "ready_for_analysis")
        return {
            key: project[key]
            for key in (
                "id",
                "host",
                "port",
                "username",
                "remote_path",
                "enabled",
                "poll_seconds",
                "host_fingerprint",
                "last_sync_at",
                "last_error",
                "stage",
                "files_seen",
                "files_excluded",
                "files_oversize",
                "symlinks_skipped",
            )
        } | {
            "last_task_id": continuous_project.get("last_task_id")
            if continuous_project
            else None
        }

    def list_projects(self) -> list[dict[str, Any]]:
        return [self._status(project) for project in self.store.list_remote_projects()]

    def add_remove_listener(self, callback: Any) -> None:
        self._remove_listeners.append(callback)

    def _save_stage(
        self, project: dict[str, Any], stage: str, error: str | None = None
    ) -> None:
        project["stage"] = stage
        project["last_error"] = error
        self.store.save_remote_project(project)

    async def create(self, request: RemoteProjectRequest) -> dict[str, Any]:
        password = request.password.get_secret_value()
        if not password:
            raise RemoteProjectError("服务器密码不能为空")
        if not request.host_fingerprint:
            raise RemoteProjectError("请先探测并确认服务器主机指纹")
        remote_path = posixpath.normpath(request.remote_path)
        project_id = _project_id(
            request.host, request.port, request.username, remote_path
        )
        active = self._active.pop(project_id, None)
        if active and not active.done():
            cancel_event = self._cancel_events.get(project_id)
            if cancel_event:
                cancel_event.set()
            else:
                active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            existing_project = self.store.get_remote_project(project_id)
            previous_password = (
                self.credentials.get(existing_project["credential_id"])
                if existing_project
                else None
            )
            project = {
                "id": project_id,
                "host": request.host,
                "port": request.port,
                "username": request.username,
                "remote_path": remote_path,
                "local_path": str(remote_data_root() / "remote-repos" / project_id),
                "credential_id": project_id,
                "provider": request.provider,
                "model": request.model,
                "language": request.language,
                "host_fingerprint": request.host_fingerprint,
                "enabled": True,
                "poll_seconds": request.poll_seconds,
                "last_sync_at": existing_project.get("last_sync_at")
                if existing_project
                else None,
                "last_error": None,
                "stage": "saved",
                "files_seen": 0,
                "files_excluded": 0,
                "files_oversize": 0,
                "symlinks_skipped": 0,
            }
            try:
                self.credentials.set(project["credential_id"], password)
                self._save_stage(project, "saved")
            except BaseException as original:
                rollback_errors: list[Exception] = []
                try:
                    if existing_project:
                        self.store.save_remote_project(existing_project)
                    else:
                        self.store.delete_remote_project(project_id)
                except Exception as error:
                    rollback_errors.append(error)
                try:
                    if existing_project and previous_password is not None:
                        self.credentials.set(
                            project["credential_id"], previous_password
                        )
                    else:
                        self.credentials.delete(project["credential_id"])
                except Exception as error:
                    rollback_errors.append(error)
                if rollback_errors:
                    logger.error(
                        "Remote project %s rollback incomplete: %s",
                        project_id,
                        rollback_errors,
                    )
                    raise RemoteProjectError(
                        "远程项目保存失败，且本地回滚不完整；请检查日志"
                    ) from original
                raise
            self._active[project_id] = asyncio.create_task(
                self._initial_sync(project_id, analyze_when_ready=request.analyze_now)
            )
            return self._status(project)

    async def _initial_sync(
        self, project_id: str, *, analyze_when_ready: bool
    ) -> None:
        async with self._concurrency:
            await self._sync(project_id, analyze_when_ready=analyze_when_ready)

    async def _analyze_locked(self, project: dict[str, Any]) -> None:
        self._save_stage(project, "analyzing")
        try:
            await self.continuous.register(
                self._analysis_request(project),
                poll_seconds=max(10, project["poll_seconds"]),
                analyze_now=True,
            )
        except Exception as error:
            self._save_stage(project, "failed", str(error))
            raise

    async def analyze(self, project_id: str) -> dict[str, Any]:
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            project = self.store.get_remote_project(project_id)
            if not project:
                raise KeyError(project_id)
            await self._analyze_locked(project)
            return self._status(project)

    async def sync(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_remote_project(project_id)
        if not project:
            raise KeyError(project_id)
        active = self._active.get(project_id)
        if active and not active.done():
            return self._status(project)
        self._save_stage(project, "connecting")
        self._active[project_id] = asyncio.create_task(
            self._initial_sync(project_id, analyze_when_ready=True)
        )
        return self._status(project)

    async def _sync(
        self, project_id: str, *, analyze_when_ready: bool
    ) -> dict[str, Any]:
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            project = self.store.get_remote_project(project_id)
            if not project:
                raise KeyError(project_id)
            password = self.credentials.get(project["credential_id"])
            if not password:
                self._save_stage(
                    project, "failed", "Windows 凭据管理器中找不到服务器密码"
                )
                return self._status(project)
            cancel_event = threading.Event()
            self._cancel_events[project_id] = cancel_event
            try:
                self._save_stage(project, "connecting")
                self._save_stage(project, "syncing")
                result, fingerprint = await asyncio.to_thread(
                    _sync_project,
                    project,
                    password,
                    self.known_hosts_path,
                    cancel_event,
                )
                project["host_fingerprint"] = fingerprint
                project.update(result.as_stats())
                project["last_sync_at"] = int(time.time() * 1000)
                self._failures.pop(project_id, None)
                self._retry_after.pop(project_id, None)
                self._save_stage(project, "ready_for_analysis")
                continuous_project = self._continuous_project(project)
                if not continuous_project and analyze_when_ready:
                    await self._analyze_locked(project)
                elif result.changed:
                    continuous_project["last_scan_at"] = 0
                    self.store.save_continuous_project(continuous_project)
                    self._save_stage(project, "analyzing")
                    await self.continuous.scan_once()
                return self._status(project)
            except Exception as error:  # noqa: BLE001 - persisted for UI diagnostics
                self._save_stage(project, "failed", str(error))
                failures = self._failures.get(project_id, 0) + 1
                self._failures[project_id] = failures
                delay = min(project["poll_seconds"], 2 ** min(failures, 8))
                self._retry_after[project_id] = time.monotonic() + delay
                logger.warning("Remote project %s sync failed: %s", project_id, error)
                return self._status(project)
            finally:
                if self._cancel_events.get(project_id) is cancel_event:
                    self._cancel_events.pop(project_id, None)

    async def remove(self, project_id: str) -> bool:
        active = self._active.pop(project_id, None)
        if active and not active.done():
            cancel_event = self._cancel_events.get(project_id)
            if cancel_event:
                cancel_event.set()
            else:
                active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            project = self.store.get_remote_project(project_id)
            if not project:
                return False
            continuous_project = self._continuous_project(project)
            if continuous_project:
                self.continuous.remove(continuous_project["id"])
            for callback in self._remove_listeners:
                callback(project_id)
            self.credentials.delete(project["credential_id"])
            deleted = self.store.delete_remote_project(project_id)
            shutil.rmtree(project["local_path"], ignore_errors=True)
            self._failures.pop(project_id, None)
            self._retry_after.pop(project_id, None)
            return deleted

    async def cancel(self, project_id: str) -> bool:
        task = self._active.get(project_id)
        if task and not task.done():
            cancel_event = self._cancel_events.get(project_id)
            if cancel_event:
                cancel_event.set()
            else:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            project = self.store.get_remote_project(project_id)
            if project:
                self._save_stage(project, "failed", "远程操作已取消")
            return True
        project = self.store.get_remote_project(project_id)
        if not project:
            raise KeyError(project_id)
        continuous_project = self._continuous_project(project)
        task_id = continuous_project.get("last_task_id") if continuous_project else None
        registry = getattr(self.continuous, "registry", None)
        wiki_task = registry.get(task_id) if registry and task_id else None
        if not wiki_task or wiki_task.status.is_terminal():
            return False
        await registry.cancel(task_id)
        self._save_stage(project, "ready_for_analysis")
        return True

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
        tasks = list(self._active.values())
        for project_id, task in self._active.items():
            cancel_event = self._cancel_events.get(project_id)
            if cancel_event:
                cancel_event.set()
            else:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        self._cancel_events.clear()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            now = int(time.time() * 1000)
            for project in self.store.list_remote_projects():
                if not project["enabled"]:
                    continue
                elapsed = now - (project.get("last_sync_at") or 0)
                project_id = project["id"]
                active = self._active.get(project_id)
                if elapsed >= project["poll_seconds"] * 1000 and (
                    active is None or active.done()
                ) and time.monotonic() >= self._retry_after.get(project_id, 0):
                    async def run_one(identifier: str) -> None:
                        async with self._concurrency:
                            await self._sync(identifier, analyze_when_ready=True)

                    self._active[project_id] = asyncio.create_task(run_one(project_id))
            self._active = {
                project_id: task
                for project_id, task in self._active.items()
                if not task.done()
            }
            await asyncio.sleep(SYNC_LOOP_SECONDS)
