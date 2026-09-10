"""Password-authenticated Ubuntu directory mirroring for desktop analysis."""

from __future__ import annotations

import asyncio
import hashlib
import os
import posixpath
import shutil
import socket
import stat
import threading
import time
from collections.abc import Callable
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
SFTP_DOWNLOAD_CHUNK_BYTES = 256 * 1024
SFTP_OPERATION_TIMEOUT_SECONDS = 30.0
PROGRESS_EMIT_SECONDS = 0.8
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


def _display_parent(project: dict[str, Any]) -> str:
    return f"{project['username']}@{project['host']}:{project['remote_path']}"


def _scope_label(project: dict[str, Any], included_dirs: list[str] | None) -> str:
    parent = _display_parent(project)
    dirs = [item for item in (included_dirs or []) if item]
    if not dirs:
        return parent
    if len(dirs) == 1:
        return f"{parent} / {dirs[0]}"
    return f"{parent} / {', '.join(dirs)}"


def _normalize_scope_path(value: str) -> str:
    raw = (value or "").replace("\\", "/").strip()
    if not raw or raw in {".", "/"}:
        raise RemoteProjectError("子分析目录不能为空")
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise RemoteProjectError("请填写相对目录，不要使用绝对路径")
    parts = [part for part in raw.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise RemoteProjectError("子分析目录不能包含上级路径")
    return "/".join(parts)


def _require_scope_dir(local_path: str, relative: str) -> None:
    root = Path(local_path).expanduser().resolve()
    target = (root / relative.replace("/", os.sep)).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise RemoteProjectError("子分析目录必须位于同步副本内") from error
    if not target.is_dir():
        raise RemoteProjectError(f"同步副本中找不到目录 {relative}，请先同步")


def _same_local_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(
        os.path.normpath(right)
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


def _verify_login(
    project: dict[str, Any], password: str, known_hosts_path: Path
) -> str:
    """Authenticate now so add-project failures stay on the form."""
    client, fingerprint = _connect(project, password, known_hosts_path)
    client.close()
    return fingerprint


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
    dirs_seen: int = 0
    current_path: str = ""
    progress_message: str = ""

    def __iter__(self):
        """Keep the legacy ``count, changed = result`` contract."""
        yield self.files_seen
        yield self.changed

    def as_stats(self) -> dict[str, Any]:
        return {
            "files_seen": self.files_seen,
            "files_excluded": self.files_excluded,
            "files_oversize": self.files_oversize,
            "symlinks_skipped": self.symlinks_skipped,
            "dirs_seen": self.dirs_seen,
            "current_path": self.current_path or None,
            "progress_message": self.progress_message or None,
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


def _download_remote_file(
    sftp: paramiko.SFTPClient,
    remote_path: str,
    local_path: Path,
    cancel_event: threading.Event | None,
) -> None:
    """Download in bounded chunks so cancellation is observed mid-file."""
    if cancel_event and cancel_event.is_set():
        raise RemoteProjectError("远程同步已取消")
    try:
        with sftp.open(remote_path, "rb") as remote_stream, local_path.open(
            "wb"
        ) as local_stream:
            while True:
                if cancel_event and cancel_event.is_set():
                    raise RemoteProjectError("远程同步已取消")
                chunk = remote_stream.read(SFTP_DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                local_stream.write(chunk)
    except (TimeoutError, socket.timeout) as error:
        if cancel_event and cancel_event.is_set():
            raise RemoteProjectError("远程同步已取消") from error
        raise


def _mirror_directory(
    sftp: paramiko.SFTPClient,
    remote_root: str,
    local_root: Path,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[MirrorResult], None] | None = None,
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
    last_emit = 0.0

    def emit(*, force: bool = False) -> None:
        nonlocal last_emit
        if on_progress is None:
            return
        now = time.monotonic()
        if not force and now - last_emit < PROGRESS_EMIT_SECONDS:
            return
        last_emit = now
        on_progress(result)

    def visit(remote_dir: str, relative_dir: str = "") -> None:
        result.dirs_seen += 1
        result.current_path = remote_dir
        result.progress_message = f"正在列出远程目录 {remote_dir}"
        emit(force=result.dirs_seen <= 1)
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
            result.current_path = remote_path
            result.progress_message = f"正在同步 {relative}"
            emit()
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
                _download_remote_file(sftp, remote_path, temporary, cancel_event)
                if _same_content(local_path, temporary):
                    continue
                os.replace(temporary, local_path)
                os.utime(local_path, (int(entry.st_atime or modified), modified))
                result.changed = True
            finally:
                temporary.unlink(missing_ok=True)

    visit(remote_root)
    result.progress_message = "正在清理本机多余文件"
    emit()

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
    result.progress_message = f"同步完成，共 {result.files_seen} 个文件"
    result.current_path = ""
    emit(force=True)
    return result


def _sync_project(
    project: dict[str, Any],
    password: str,
    known_hosts_path: Path,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[MirrorResult], None] | None = None,
) -> tuple[MirrorResult, str]:
    if on_progress:
        on_progress(
            MirrorResult(
                current_path=project["remote_path"],
                progress_message="正在建立 SSH 连接…",
            )
        )
    client, fingerprint = _connect(project, password, known_hosts_path)
    try:
        if on_progress:
            on_progress(
                MirrorResult(
                    current_path=project["remote_path"],
                    progress_message="正在打开 SFTP…",
                )
            )
        with client.open_sftp() as sftp:
            # Bound stalled reads so cancel/delete can finish; large Ubuntu
            # trees still need more than a couple of seconds per listing.
            sftp.get_channel().settimeout(SFTP_OPERATION_TIMEOUT_SECONDS)
            result = _mirror_directory(
                sftp,
                project["remote_path"],
                Path(project["local_path"]),
                cancel_event,
                on_progress,
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

    def _workspace_root(self, project: dict[str, Any]) -> str:
        path = Path(project["local_path"]).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        try:
            from api.services.local_roots import register_selected_root

            return register_selected_root(str(path))
        except Exception:
            return str(path.resolve())

    def _upsert_scope_space(
        self, project: dict[str, Any], included_dirs: list[str]
    ) -> Any:
        if not hasattr(self.store, "upsert_knowledge_space"):
            raise RemoteProjectError("当前存储不支持知识空间")
        dirs = [item for item in included_dirs if item]
        return self.store.upsert_knowledge_space(
            workspace_root=self._workspace_root(project),
            included_dirs=dirs,
            excluded_dirs=[],
            language=project.get("language") or "zh",
            provider=project.get("provider"),
            model=project.get("model"),
            parent_workspace=_display_parent(project),
            label=_scope_label(project, dirs),
        )

    def _spaces_for_project(self, project: dict[str, Any]) -> list[Any]:
        if not hasattr(self.store, "list_knowledge_spaces"):
            return []
        return [
            space
            for space in self.store.list_knowledge_spaces()
            if _same_local_path(getattr(space, "workspace_root", None), project["local_path"])
        ]

    def _space_belongs(self, project: dict[str, Any], space: Any) -> bool:
        return _same_local_path(
            getattr(space, "workspace_root", None), project["local_path"]
        )

    def _is_root_space(self, space: Any) -> bool:
        return not list(getattr(space, "included_dirs", None) or [])

    def _find_root_space(self, project: dict[str, Any]) -> Any | None:
        return next(
            (space for space in self._spaces_for_project(project) if self._is_root_space(space)),
            None,
        )

    def _analysis_request(self, project: dict[str, Any], space: Any) -> WikiTaskRequest:
        provider = project.get("provider")
        if provider not in {"openai", "google", "ollama", "openai_compatible"}:
            raise RemoteProjectError("请先在设置中配置可用的 AI")
        return WikiTaskRequest(
            repo_url=project["local_path"],
            type="local",
            provider=provider,
            model=project.get("model"),
            language=project["language"],
            owner=_safe_owner(project["username"], project["host"]),
            repo=_safe_repo_name(project["remote_path"]),
            included_dirs=list(getattr(space, "included_dirs", None) or []),
            space_id=space.space_id,
            comprehensive=True,
            force=True,
        )

    def _continuous_projects_for_mirror(
        self, project: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in self.continuous.list_projects()
            if _same_local_path(
                (item.get("request") or {}).get("repo_url"), project["local_path"]
            )
        ]

    def _continuous_for_space(self, space: Any) -> dict[str, Any] | None:
        space_id = getattr(space, "space_id", None)
        repo_key = f"space_{space_id}" if space_id else None
        for item in self.continuous.list_projects():
            request = item.get("request") or {}
            if space_id and request.get("space_id") == space_id:
                return item
            if repo_key and item.get("id") == repo_key:
                return item
        return None

    def _continuous_project(self, project: dict[str, Any]) -> dict[str, Any] | None:
        root_space = self._find_root_space(project)
        if root_space:
            matched = self._continuous_for_space(root_space)
            if matched:
                return matched
        for item in self._continuous_projects_for_mirror(project):
            request = item.get("request") or {}
            if request.get("included_dirs"):
                continue
            if request.get("space_id") and root_space is None:
                continue
            if request.get("space_id") and root_space and request.get("space_id") != root_space.space_id:
                continue
            return item
        return None

    def _task_progress(self, task_id: str | None) -> tuple[str | None, int, int | None]:
        registry = getattr(self.continuous, "registry", None)
        task = registry.get(task_id) if registry and task_id else None
        if task is None:
            return None, 0, None
        status = getattr(task, "status", None)
        return (
            getattr(status, "value", status),
            int(getattr(task, "pages_done", 0) or 0),
            getattr(task, "pages_total", None),
        )

    def _scope_status(self, project: dict[str, Any], space: Any) -> dict[str, Any]:
        continuous = self._continuous_for_space(space)
        task_id = (
            getattr(space, "last_task_id", None)
            or (continuous.get("last_task_id") if continuous else None)
        )
        analysis_status, pages_done, pages_total = self._task_progress(task_id)
        included = list(getattr(space, "included_dirs", None) or [])
        return {
            "space_id": space.space_id,
            "label": getattr(space, "label", None) or _scope_label(project, included),
            "included_dirs": included,
            "last_task_id": task_id,
            "analysis_status": analysis_status,
            "analysis_pages_done": pages_done,
            "analysis_pages_total": pages_total,
        }

    def _apply_current_desktop_model(self, project: dict[str, Any]) -> bool:
        try:
            from api.desktop_settings import load_desktop_settings

            data = load_desktop_settings()
        except Exception:
            return False
        provider = (data.get("provider") or "").lower()
        if provider not in {"openai", "google", "ollama", "openai_compatible"}:
            return False
        if provider != "ollama" and not data.get(f"{provider}_api_key"):
            return False
        if provider == "openai_compatible" and not (
            data.get("base_url") and data.get("selected_model")
        ):
            return False
        project["provider"] = provider
        if provider == "ollama":
            if data.get("ollama_model"):
                project["model"] = data["ollama_model"]
        elif data.get("selected_model"):
            project["model"] = data["selected_model"]
        return True

    def _status(self, project: dict[str, Any]) -> dict[str, Any]:
        continuous_project = self._continuous_project(project)
        task_id = (
            continuous_project.get("last_task_id") if continuous_project else None
        )
        analysis_status, analysis_pages_done, analysis_pages_total = self._task_progress(
            task_id
        )
        return {
            "id": project["id"],
            "host": project["host"],
            "port": project["port"],
            "username": project["username"],
            "remote_path": project["remote_path"],
            "enabled": project["enabled"],
            "poll_seconds": project["poll_seconds"],
            "host_fingerprint": project.get("host_fingerprint"),
            "last_sync_at": project.get("last_sync_at"),
            "last_error": project.get("last_error"),
            "stage": project.get("stage", "saved"),
            "files_seen": project.get("files_seen", 0),
            "files_excluded": project.get("files_excluded", 0),
            "files_oversize": project.get("files_oversize", 0),
            "symlinks_skipped": project.get("symlinks_skipped", 0),
            "dirs_seen": project.get("dirs_seen", 0),
            "current_path": project.get("current_path"),
            "progress_message": project.get("progress_message"),
            "sync_started_at": project.get("sync_started_at"),
            "progress_updated_at": project.get("progress_updated_at"),
            "last_task_id": task_id,
            "analysis_status": analysis_status,
            "analysis_pages_done": analysis_pages_done,
            "analysis_pages_total": analysis_pages_total,
            "scopes": [
                self._scope_status(project, space)
                for space in self._spaces_for_project(project)
            ],
        }

    def _reconcile_analysis_stages(self) -> None:
        """Persist completed analysis stages from the background loop.

        Read endpoints must remain read-only: polling project status should not
        be required for state transitions to reach SQLite.
        """
        registry = getattr(self.continuous, "registry", None)
        if registry is None:
            return
        for project in self.store.list_remote_projects():
            continuous_project = self._continuous_project(project)
            task_id = (
                continuous_project.get("last_task_id")
                if continuous_project
                else None
            )
            task = registry.get(task_id) if task_id else None
            if task and not task.status.is_terminal():
                if project.get("stage") == "ready_for_analysis":
                    self._save_stage(project, "analyzing")
                continue
            if project.get("stage") != "analyzing" or not task:
                continue
            if task.status.value == "failed":
                self._save_stage(
                    project,
                    "failed",
                    str(getattr(task, "error", None) or "AI 分析失败"),
                )
            else:
                self._save_stage(project, "ready_for_analysis")

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
        await asyncio.to_thread(
            _verify_login,
            {
                "host": request.host,
                "port": request.port,
                "username": request.username,
                "host_fingerprint": request.host_fingerprint,
            },
            password,
            self.known_hosts_path,
        )
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
            credential_written = False
            project = {
                "id": project_id,
                "host": request.host,
                "port": request.port,
                "username": request.username,
                "remote_path": remote_path,
                "local_path": str(remote_data_root() / "remote-repos" / project_id),
                "credential_id": project_id,
                "provider": None,
                "model": None,
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
                "dirs_seen": 0,
                "current_path": None,
                "progress_message": None,
                "sync_started_at": None,
                "progress_updated_at": None,
            }
            try:
                self.credentials.set(project["credential_id"], password)
                credential_written = True
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
                if credential_written:
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
                self._initial_sync(project_id, analyze_when_ready=False)
            )
            return self._status(project)

    async def _initial_sync(
        self, project_id: str, *, analyze_when_ready: bool
    ) -> None:
        async with self._concurrency:
            await self._sync(project_id, analyze_when_ready=analyze_when_ready)

    async def _analyze_locked(self, project: dict[str, Any]) -> None:
        if not self._apply_current_desktop_model(project):
            raise RemoteProjectError("请先在设置中配置可用的 AI")
        space = self._upsert_scope_space(project, [])
        project["progress_message"] = "正在启动分析任务…"
        self._save_stage(project, "analyzing")
        try:
            result = await self.continuous.register(
                self._analysis_request(project, space),
                poll_seconds=max(10, project["poll_seconds"]),
                analyze_now=True,
            )
            task_id = result.get("last_task_id") or result.get("id")
            if task_id and hasattr(self.store, "set_knowledge_space_task"):
                self.store.set_knowledge_space_task(space.space_id, task_id)
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

    def list_scopes(self, project_id: str) -> list[dict[str, Any]]:
        project = self.store.get_remote_project(project_id)
        if not project:
            raise KeyError(project_id)
        return [
            self._scope_status(project, space)
            for space in self._spaces_for_project(project)
        ]

    def detect_scopes(self, project_id: str) -> list[Any]:
        project = self.store.get_remote_project(project_id)
        if not project:
            raise KeyError(project_id)
        local_path = project["local_path"]
        if not Path(local_path).is_dir():
            raise RemoteProjectError("请先完成同步后再探测子目录")
        from api.services.knowledge.spaces import detect_subrepos

        return detect_subrepos(local_path)

    def create_scopes(
        self, project_id: str, included_dirs: list[str]
    ) -> list[dict[str, Any]]:
        project = self.store.get_remote_project(project_id)
        if not project:
            raise KeyError(project_id)
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in included_dirs:
            path = _normalize_scope_path(raw)
            if path in seen:
                continue
            seen.add(path)
            _require_scope_dir(project["local_path"], path)
            normalized.append(path)
        if not normalized:
            raise RemoteProjectError("请至少选择一个子目录")
        return [
            self._scope_status(project, self._upsert_scope_space(project, [path]))
            for path in normalized
        ]

    async def analyze_scope(self, project_id: str, space_id: str) -> dict[str, Any]:
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            project = self.store.get_remote_project(project_id)
            if not project:
                raise KeyError(project_id)
            if not hasattr(self.store, "get_knowledge_space"):
                raise KeyError(space_id)
            space = self.store.get_knowledge_space(space_id)
            if not space or not self._space_belongs(project, space):
                raise KeyError(space_id)
            if not self._apply_current_desktop_model(project):
                raise RemoteProjectError("请先在设置中配置可用的 AI")
            self.store.save_remote_project(project)
            result = await self.continuous.register(
                self._analysis_request(project, space),
                poll_seconds=max(10, project["poll_seconds"]),
                analyze_now=True,
            )
            task_id = result.get("last_task_id") or result.get("id")
            if task_id and hasattr(self.store, "set_knowledge_space_task"):
                self.store.set_knowledge_space_task(space.space_id, task_id)
            return self._status(project)

    def delete_scope(self, project_id: str, space_id: str) -> bool:
        project = self.store.get_remote_project(project_id)
        if not project:
            raise KeyError(project_id)
        if not hasattr(self.store, "get_knowledge_space"):
            return False
        space = self.store.get_knowledge_space(space_id)
        if not space or not self._space_belongs(project, space):
            return False
        continuous = self._continuous_for_space(space)
        if continuous:
            self.continuous.remove(continuous["id"])
        if not hasattr(self.store, "delete_knowledge_space"):
            return False
        return self.store.delete_knowledge_space(space_id)

    async def sync(self, project_id: str) -> dict[str, Any]:
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            project = self.store.get_remote_project(project_id)
            if not project:
                raise KeyError(project_id)
            active = self._active.get(project_id)
            if active and not active.done():
                return self._status(project)
            self._save_stage(project, "connecting")
            self._active[project_id] = asyncio.create_task(
                self._initial_sync(project_id, analyze_when_ready=False)
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
                now_ms = int(time.time() * 1000)
                project["sync_started_at"] = now_ms
                project["progress_updated_at"] = now_ms
                project["progress_message"] = "正在建立 SSH 连接…"
                project["current_path"] = project["remote_path"]
                self._save_stage(project, "connecting")
                self._save_stage(project, "syncing")

                def persist_progress(result: MirrorResult) -> None:
                    project.update(result.as_stats())
                    project["progress_updated_at"] = int(time.time() * 1000)
                    self.store.save_remote_project(project)

                result, fingerprint = await asyncio.to_thread(
                    _sync_project,
                    project,
                    password,
                    self.known_hosts_path,
                    cancel_event,
                    persist_progress,
                )
                project["host_fingerprint"] = fingerprint
                project.update(result.as_stats())
                project["last_sync_at"] = int(time.time() * 1000)
                project["progress_message"] = (
                    result.progress_message
                    or f"同步完成，共 {result.files_seen} 个文件"
                )
                project["current_path"] = None
                project["progress_updated_at"] = int(time.time() * 1000)
                self._failures.pop(project_id, None)
                self._retry_after.pop(project_id, None)
                self._save_stage(project, "ready_for_analysis")
                if analyze_when_ready:
                    await self._analyze_locked(project)
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
            spaces = self._spaces_for_project(project)
            for item in self._continuous_projects_for_mirror(project):
                self.continuous.remove(item["id"])
            for space in spaces:
                if hasattr(self.store, "delete_knowledge_space"):
                    self.store.delete_knowledge_space(space.space_id)
            deleted = self.store.delete_remote_project(project_id)
            if not deleted:
                return False
            for callback in self._remove_listeners:
                callback(project_id)
            self.credentials.delete(project["credential_id"])
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
            self._reconcile_analysis_stages()
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
                            await self._sync(identifier, analyze_when_ready=False)

                    self._active[project_id] = asyncio.create_task(run_one(project_id))
            self._active = {
                project_id: task
                for project_id, task in self._active.items()
                if not task.done()
            }
            await asyncio.sleep(SYNC_LOOP_SECONDS)
