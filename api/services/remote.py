"""Password-authenticated Ubuntu directory mirroring for desktop analysis."""

from __future__ import annotations

import asyncio
import hashlib
import os
import posixpath
import shutil
import stat
import time
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


def _mirror_directory(
    sftp: paramiko.SFTPClient, remote_root: str, local_root: Path
) -> tuple[int, bool]:
    try:
        root_attributes = sftp.lstat(remote_root)
    except OSError as error:
        raise RemoteProjectError("远程目录不存在或没有读取权限") from error
    if not stat.S_ISDIR(root_attributes.st_mode):
        raise RemoteProjectError("远程路径不是目录")

    local_root.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    changed = False
    file_count = 0

    def visit(remote_dir: str, relative_dir: str = "") -> None:
        nonlocal changed, file_count
        try:
            entries = sftp.listdir_attr(remote_dir)
        except OSError as error:
            raise RemoteProjectError("远程目录没有读取权限") from error
        for entry in entries:
            name = entry.filename
            if not _valid_remote_name(name) or name.startswith("."):
                continue
            relative = f"{relative_dir}/{name}".lstrip("/")
            remote_path = posixpath.join(remote_dir, name)
            if stat.S_ISLNK(entry.st_mode):
                continue
            if stat.S_ISDIR(entry.st_mode):
                if name in IGNORED_DIRS:
                    continue
                visit(remote_path, relative)
                continue
            if not stat.S_ISREG(entry.st_mode):
                continue
            if entry.st_size is None or entry.st_size > MAX_REMOTE_FILE_BYTES:
                continue

            seen.add(relative)
            file_count += 1
            local_path = local_root.joinpath(*relative.split("/"))
            local_path.parent.mkdir(parents=True, exist_ok=True)
            existing = local_path.stat() if local_path.is_file() else None
            modified = int(entry.st_mtime or time.time())
            if (
                existing
                and existing.st_size == entry.st_size
                and int(existing.st_mtime) == modified
            ):
                continue
            temporary = local_path.with_name(f".{local_path.name}.codeinsight.tmp")
            try:
                sftp.get(remote_path, str(temporary))
                os.replace(temporary, local_path)
                os.utime(local_path, (int(entry.st_atime or modified), modified))
                changed = True
            finally:
                temporary.unlink(missing_ok=True)

    visit(remote_root)

    for local_path in sorted(local_root.rglob("*"), reverse=True):
        if local_path.is_symlink():
            local_path.unlink(missing_ok=True)
            changed = True
        elif local_path.is_file():
            relative = local_path.relative_to(local_root).as_posix()
            if relative not in seen:
                local_path.unlink()
                changed = True
        elif local_path.is_dir():
            try:
                local_path.rmdir()
            except OSError:
                pass
    return file_count, changed


def _sync_project(
    project: dict[str, Any], password: str, known_hosts_path: Path
) -> tuple[int, bool, str]:
    client, fingerprint = _connect(project, password, known_hosts_path)
    try:
        with client.open_sftp() as sftp:
            file_count, changed = _mirror_directory(
                sftp, project["remote_path"], Path(project["local_path"])
            )
        return file_count, changed, fingerprint
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
            )
        } | {
            "last_task_id": continuous_project.get("last_task_id")
            if continuous_project
            else None
        }

    def list_projects(self) -> list[dict[str, Any]]:
        return [self._status(project) for project in self.store.list_remote_projects()]

    async def create(self, request: RemoteProjectRequest) -> dict[str, Any]:
        password = request.password.get_secret_value()
        if not password:
            raise RemoteProjectError("服务器密码不能为空")
        remote_path = posixpath.normpath(request.remote_path)
        project_id = _project_id(
            request.host, request.port, request.username, remote_path
        )
        existing_project = self.store.get_remote_project(project_id)
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
            "host_fingerprint": None,
            "enabled": True,
            "poll_seconds": request.poll_seconds,
            "last_sync_at": None,
            "last_error": None,
        }
        try:
            _, _, fingerprint = await asyncio.to_thread(
                _sync_project, project, password, self.known_hosts_path
            )
            project["host_fingerprint"] = fingerprint
            project["last_sync_at"] = int(time.time() * 1000)
            self.credentials.set(project["credential_id"], password)
            self.store.save_remote_project(project)
            analysis_request = self._analysis_request(project)
            await self.continuous.register(
                analysis_request,
                poll_seconds=max(10, request.poll_seconds),
                analyze_now=True,
            )
            return self._status(project)
        except Exception:
            if not existing_project:
                continuous_project = self._continuous_project(project)
                if continuous_project:
                    self.continuous.remove(continuous_project["id"])
                self.credentials.delete(project["credential_id"])
                self.store.delete_remote_project(project_id)
                shutil.rmtree(project["local_path"], ignore_errors=True)
            raise

    async def sync(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_remote_project(project_id)
        if not project:
            raise KeyError(project_id)
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            password = self.credentials.get(project["credential_id"])
            if not password:
                project["last_error"] = "Windows 凭据管理器中找不到服务器密码"
                self.store.save_remote_project(project)
                return self._status(project)
            try:
                _, changed, fingerprint = await asyncio.to_thread(
                    _sync_project, project, password, self.known_hosts_path
                )
                project["host_fingerprint"] = fingerprint
                project["last_sync_at"] = int(time.time() * 1000)
                project["last_error"] = None
                self.store.save_remote_project(project)
                continuous_project = self._continuous_project(project)
                if not continuous_project:
                    await self.continuous.register(
                        self._analysis_request(project),
                        poll_seconds=max(10, project["poll_seconds"]),
                        analyze_now=True,
                    )
                elif changed:
                    continuous_project["last_scan_at"] = 0
                    self.store.save_continuous_project(continuous_project)
                    await self.continuous.scan_once()
                return self._status(project)
            except Exception as error:  # noqa: BLE001 - persisted for UI diagnostics
                project["last_error"] = str(error)
                self.store.save_remote_project(project)
                logger.warning("Remote project %s sync failed: %s", project_id, error)
                return self._status(project)

    def remove(self, project_id: str) -> bool:
        project = self.store.get_remote_project(project_id)
        if not project:
            return False
        continuous_project = self._continuous_project(project)
        if continuous_project:
            self.continuous.remove(continuous_project["id"])
        self.credentials.delete(project["credential_id"])
        deleted = self.store.delete_remote_project(project_id)
        shutil.rmtree(project["local_path"], ignore_errors=True)
        self._locks.pop(project_id, None)
        return deleted

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
            now = int(time.time() * 1000)
            for project in self.store.list_remote_projects():
                if not project["enabled"]:
                    continue
                elapsed = now - (project.get("last_sync_at") or 0)
                if elapsed >= project["poll_seconds"] * 1000:
                    await self.sync(project["id"])
            await asyncio.sleep(SYNC_LOOP_SECONDS)
