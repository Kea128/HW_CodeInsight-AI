import asyncio
import io
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import paramiko
import pytest

from api.schemas import RemoteProjectRequest, TaskStatus
from api.schemas.knowledge import KnowledgeSpace
from api.services.knowledge.spaces import compute_space_id, knowledge_label
from api.services import ssh_client
from api.services.ssh_client import fingerprints_match
from api.services.remote import (
    MAX_REMOTE_FILE_BYTES,
    MirrorResult,
    RemoteProjectError,
    RemoteSyncManager,
    _friendly_connection_error,
    _mirror_directory,
    _project_id,
)


def _entry(name: str, mode: int, size: int = 0, modified: int = 100):
    return SimpleNamespace(
        filename=name,
        st_mode=mode,
        st_size=size,
        st_mtime=modified,
        st_atime=modified,
    )


class FakeSftp:
    def __init__(self):
        self.directories = {
            "/srv/code": [
                _entry("src", stat.S_IFDIR),
                _entry("build", stat.S_IFDIR),
                _entry("README.md", stat.S_IFREG, 6),
                _entry(".gitignore", stat.S_IFREG, 4),
                _entry(".env", stat.S_IFREG, 6),
                _entry("huge.bin", stat.S_IFREG, MAX_REMOTE_FILE_BYTES + 1),
                _entry("linked.py", stat.S_IFLNK),
                _entry("bad:name.py", stat.S_IFREG, 4),
            ],
            "/srv/code/src": [_entry("main.py", stat.S_IFREG, 12)],
        }
        self.files = {
            "/srv/code/README.md": b"# Demo",
            "/srv/code/.gitignore": b"dist",
            "/srv/code/src/main.py": b"print('ok')\n",
        }

    def lstat(self, path):
        if path not in self.directories:
            raise OSError(path)
        return _entry(Path(path).name, stat.S_IFDIR)

    def listdir_attr(self, path):
        return self.directories[path]

    def open(self, remote_path, mode):
        assert mode == "rb"
        return io.BytesIO(self.files[remote_path])


class FakeStore:
    def __init__(self):
        self.remote_projects = {}
        self.continuous_projects = {}
        self.knowledge_spaces = {}

    def get_remote_project(self, project_id):
        return self.remote_projects.get(project_id)

    def save_remote_project(self, project):
        self.remote_projects[project["id"]] = project.copy()

    def delete_remote_project(self, project_id):
        return self.remote_projects.pop(project_id, None) is not None

    def list_remote_projects(self):
        return list(self.remote_projects.values())

    def save_continuous_project(self, project):
        self.continuous_projects[project["id"]] = project.copy()

    def upsert_knowledge_space(
        self,
        *,
        workspace_root,
        included_dirs,
        excluded_dirs,
        language,
        provider,
        model,
        last_task_id=None,
        parent_workspace=None,
        label=None,
    ):
        space_id = compute_space_id(workspace_root, included_dirs, language)
        existing = self.knowledge_spaces.get(space_id)
        space = KnowledgeSpace(
            space_id=space_id,
            workspace_root=workspace_root,
            included_dirs=list(included_dirs or []),
            excluded_dirs=list(excluded_dirs or []),
            label=label or knowledge_label(workspace_root, included_dirs),
            parent_workspace=parent_workspace or workspace_root,
            language=language,
            provider=provider,
            model=model,
            last_task_id=last_task_id
            or (existing.last_task_id if existing else None),
            created_at=existing.created_at if existing else 1,
            updated_at=2,
        )
        self.knowledge_spaces[space_id] = space
        return space

    def list_knowledge_spaces(self):
        return list(self.knowledge_spaces.values())

    def get_knowledge_space(self, space_id):
        return self.knowledge_spaces.get(space_id)

    def set_knowledge_space_task(self, space_id, task_id):
        space = self.knowledge_spaces.get(space_id)
        if space is None:
            return
        self.knowledge_spaces[space_id] = space.model_copy(
            update={"last_task_id": task_id}
        )

    def delete_knowledge_space(self, space_id):
        return self.knowledge_spaces.pop(space_id, None) is not None


class FakeCredentials:
    def __init__(self):
        self.values = {}

    def set(self, credential_id, password):
        self.values[credential_id] = password

    def get(self, credential_id):
        return self.values.get(credential_id)

    def delete(self, credential_id):
        self.values.pop(credential_id, None)


class FakeContinuous:
    def __init__(self):
        self.projects = []
        self.requests = []

    def list_projects(self):
        return self.projects

    async def register(self, request, **options):
        self.requests.append((request, options))
        project = {
            "id": request.repo_key,
            "request": request.model_dump(),
            "last_task_id": request.repo_key,
        }
        self.projects.append(project)
        return project

    def remove(self, project_id):
        self.projects = [item for item in self.projects if item["id"] != project_id]

    async def scan_once(self):
        return None


class FailingContinuous(FakeContinuous):
    async def register(self, request, **options):
        raise RuntimeError("analysis registration failed")


class FailingSaveStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.fail_next_save = True

    def save_remote_project(self, project):
        super().save_remote_project(project)
        if self.fail_next_save:
            self.fail_next_save = False
            raise OSError("database write failed")


def test_mirror_directory_updates_and_deletes_without_following_links(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "stale.txt").write_text("remove me", encoding="utf-8")

    result = _mirror_directory(FakeSftp(), "/srv/code", mirror)
    count, changed = result

    assert changed is True
    assert count == 3
    assert (mirror / "README.md").read_bytes() == b"# Demo"
    assert (mirror / "src" / "main.py").read_bytes() == b"print('ok')\n"
    assert not (mirror / "stale.txt").exists()
    assert not (mirror / "linked.py").exists()
    assert not (mirror / "bad:name.py").exists()
    assert not (mirror / "build").exists()
    assert (mirror / ".gitignore").read_text(encoding="utf-8") == "dist"
    assert not (mirror / ".env").exists()
    assert result.files_oversize == 1
    assert result.symlinks_skipped == 1
    assert result.files_excluded >= 3


def test_mirror_emits_live_progress_before_finishing(tmp_path):
    updates = []

    result = _mirror_directory(
        FakeSftp(),
        "/srv/code",
        tmp_path / "mirror",
        on_progress=lambda item: updates.append(item.as_stats()),
    )

    assert updates
    assert (updates[0]["progress_message"] or "").startswith("正在列出远程目录")
    assert any(item["files_seen"] > 0 for item in updates)
    assert result.dirs_seen >= 2
    assert result.progress_message.startswith("同步完成")


def test_mirror_detects_same_size_same_mtime_content_change(tmp_path):
    sftp = FakeSftp()
    mirror = tmp_path / "mirror"
    _mirror_directory(sftp, "/srv/code", mirror)
    sftp.files["/srv/code/README.md"] = b"# Damo"

    result = _mirror_directory(sftp, "/srv/code", mirror)

    assert result.changed is True
    assert (mirror / "README.md").read_bytes() == b"# Damo"


def test_mirror_cancels_during_chunked_file_download(monkeypatch, tmp_path):
    cancel_event = threading.Event()

    class CancellingStream(io.BytesIO):
        def read(self, size=-1):
            chunk = super().read(size)
            if chunk:
                cancel_event.set()
            return chunk

    sftp = FakeSftp()
    sftp.directories = {
        "/srv/code": [_entry("README.md", stat.S_IFREG, 1024)]
    }
    sftp.files["/srv/code/README.md"] = b"x" * 1024
    sftp.open = lambda remote_path, mode: CancellingStream(sftp.files[remote_path])
    monkeypatch.setattr("api.services.remote.SFTP_DOWNLOAD_CHUNK_BYTES", 16)
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    original = mirror / "README.md"
    original.write_bytes(b"keep")

    with pytest.raises(RemoteProjectError, match="取消"):
        _mirror_directory(sftp, "/srv/code", mirror, cancel_event)

    assert original.read_bytes() == b"keep"
    assert not (mirror / ".README.md.codeinsight.tmp").exists()


def _pass_login(monkeypatch):
    monkeypatch.setattr(
        "api.services.remote._verify_login",
        lambda *_args, **_kwargs: "SHA256:test",
    )


def test_remote_project_id_is_stable_and_contains_no_credentials():
    first = _project_id("10.0.0.8", 22, "ubuntu", "/srv/code")
    second = _project_id("10.0.0.8", 22, "ubuntu", "/srv/code")

    assert first == second
    assert first.startswith("remote-")
    assert "ubuntu" not in first


def test_fingerprint_compare_rejects_different_lengths():
    assert fingerprints_match("SHA256:abc", "SHA256:abc") is True
    assert fingerprints_match("SHA256:abc", "SHA256:abcd") is False
    assert fingerprints_match("", "SHA256:abc") is False


def test_authentication_error_does_not_echo_credentials():
    error = _friendly_connection_error(paramiko.AuthenticationException("denied"))

    assert str(error) == "Ubuntu 用户名或密码错误"
    assert "denied" not in str(error)


def test_credential_store_reports_unavailable_vault_without_secret(monkeypatch):
    def fail(*_):
        raise ssh_client.keyring.errors.KeyringError("backend details")

    monkeypatch.setattr(ssh_client.keyring, "set_password", fail)

    with pytest.raises(ssh_client.CredentialStoreUnavailable) as captured:
        ssh_client.CredentialStore().set("remote-one", "server-secret")

    assert "Credential Manager" in str(captured.value)
    assert "server-secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_create_requires_confirmed_host_fingerprint(tmp_path):
    manager = RemoteSyncManager(
        FakeContinuous(), store=FakeStore(), credentials=FakeCredentials()
    )
    manager.known_hosts_path = tmp_path / "known_hosts"

    with pytest.raises(RemoteProjectError, match="指纹"):
        await manager.create(
            RemoteProjectRequest(
                host="10.0.0.8",
                username="ubuntu",
                password="server-secret",
                remote_path="/srv/code/demo",
            )
        )


@pytest.mark.asyncio
async def test_create_rejects_unreachable_host_without_saving(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def fail(*_args, **_kwargs):
        raise RemoteProjectError("无法连接 Ubuntu 服务器：timed out")

    monkeypatch.setattr("api.services.remote._verify_login", fail)
    store = FakeStore()
    credentials = FakeCredentials()
    manager = RemoteSyncManager(
        FakeContinuous(), store=store, credentials=credentials
    )

    with pytest.raises(RemoteProjectError, match="无法连接"):
        await manager.create(
            RemoteProjectRequest(
                host="10.0.0.8",
                username="ubuntu",
                password="server-secret",
                remote_path="/srv/code/demo",
                host_fingerprint="SHA256:test",
            )
        )

    assert store.remote_projects == {}
    assert credentials.values == {}


@pytest.mark.asyncio
async def test_create_stores_password_only_in_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _pass_login(monkeypatch)
    monkeypatch.setattr(
        "api.services.remote._sync_project",
        lambda project, password, known_hosts, cancel_event=None, on_progress=None: (
            MirrorResult(files_seen=2, changed=True),
            "SHA256:test",
        ),
    )
    store = FakeStore()
    credentials = FakeCredentials()
    continuous = FakeContinuous()
    manager = RemoteSyncManager(continuous, store=store, credentials=credentials)

    status = await manager.create(
        RemoteProjectRequest(
            host="10.0.0.8",
            username="ubuntu",
            password="server-secret",
            remote_path="/srv/code/demo",
            host_fingerprint="SHA256:test",
        )
    )

    assert status["stage"] == "saved"
    await manager._active[status["id"]]
    project = store.remote_projects[status["id"]]
    assert credentials.values[project["credential_id"]] == "server-secret"
    assert "password" not in project
    assert "credential_id" not in status
    assert continuous.requests == []
    assert project["stage"] == "ready_for_analysis"


@pytest.mark.asyncio
async def test_first_sync_without_ai_stays_ready_for_analysis(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _pass_login(monkeypatch)
    monkeypatch.setattr(
        "api.services.remote._sync_project",
        lambda project, password, known_hosts, cancel_event=None, on_progress=None: (
            MirrorResult(files_seen=2, changed=True),
            "SHA256:test",
        ),
    )
    store = FakeStore()
    continuous = FakeContinuous()
    manager = RemoteSyncManager(
        continuous, store=store, credentials=FakeCredentials()
    )

    status = await manager.create(
        RemoteProjectRequest(
            host="10.0.0.8",
            username="ubuntu",
            password="server-secret",
            remote_path="/srv/code/demo",
            host_fingerprint="SHA256:test",
        )
    )
    await manager._active[status["id"]]

    assert store.remote_projects[status["id"]]["stage"] == "ready_for_analysis"
    assert store.remote_projects[status["id"]]["provider"] is None
    assert continuous.requests == []


@pytest.mark.asyncio
async def test_concurrent_manual_sync_only_starts_one_task(tmp_path):
    store = FakeStore()
    credentials = FakeCredentials()
    manager = RemoteSyncManager(
        FakeContinuous(), store=store, credentials=credentials
    )
    project_id = "remote-one"
    store.remote_projects[project_id] = {
        "id": project_id,
        "host": "10.0.0.8",
        "port": 22,
        "username": "ubuntu",
        "remote_path": "/srv/code",
        "local_path": str(tmp_path / "mirror"),
        "credential_id": project_id,
        "provider": "ollama",
        "model": None,
        "language": "zh",
        "host_fingerprint": "SHA256:test",
        "enabled": True,
        "poll_seconds": 60,
        "last_sync_at": None,
        "last_error": None,
        "stage": "saved",
        "files_seen": 0,
        "files_excluded": 0,
        "files_oversize": 0,
        "symlinks_skipped": 0,
    }
    started = 0
    release = asyncio.Event()

    async def initial_sync(identifier, *, analyze_when_ready):
        nonlocal started
        started += 1
        await release.wait()

    manager._initial_sync = initial_sync
    await asyncio.gather(manager.sync(project_id), manager.sync(project_id))
    await asyncio.sleep(0)

    assert started == 1
    release.set()
    await manager._active[project_id]


@pytest.mark.asyncio
async def test_background_analysis_failure_keeps_project_retryable(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _pass_login(monkeypatch)

    def sync(project, password, known_hosts, cancel_event=None, on_progress=None):
        mirror = Path(project["local_path"])
        mirror.mkdir(parents=True)
        (mirror / "source.py").write_text("print('new')", encoding="utf-8")
        return MirrorResult(files_seen=1, changed=True), "SHA256:test"

    monkeypatch.setattr("api.services.remote._sync_project", sync)
    store = FakeStore()
    credentials = FakeCredentials()
    manager = RemoteSyncManager(
        FailingContinuous(), store=store, credentials=credentials
    )

    status = await manager.create(
        RemoteProjectRequest(
            host="10.0.0.8",
            username="ubuntu",
            password="server-secret",
            remote_path="/srv/code/demo",
            host_fingerprint="SHA256:test",
        )
    )
    await manager._active[status["id"]]
    assert store.remote_projects[status["id"]]["stage"] == "ready_for_analysis"

    monkeypatch.setattr(
        "api.desktop_settings.load_desktop_settings",
        lambda: {"provider": "ollama", "ollama_model": "qwen3:4b"},
    )
    with pytest.raises(RuntimeError, match="analysis registration failed"):
        await manager.analyze(status["id"])

    project = store.remote_projects[status["id"]]
    assert project["stage"] == "failed"
    assert project["last_error"] == "analysis registration failed"
    assert credentials.values[project["credential_id"]] == "server-secret"
    assert Path(project["local_path"], "source.py").is_file()


@pytest.mark.asyncio
async def test_create_rolls_back_credential_when_record_save_fails(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _pass_login(monkeypatch)
    store = FailingSaveStore()
    credentials = FakeCredentials()
    manager = RemoteSyncManager(
        FakeContinuous(), store=store, credentials=credentials
    )

    with pytest.raises(OSError, match="database write failed"):
        await manager.create(
            RemoteProjectRequest(
                host="10.0.0.8",
                username="ubuntu",
                password="server-secret",
                remote_path="/srv/code/demo",
                host_fingerprint="SHA256:test",
            )
        )

    assert store.remote_projects == {}
    assert credentials.values == {}


def test_project_listing_is_read_only_and_background_reconciles_analysis(tmp_path):
    store = FakeStore()
    continuous = FakeContinuous()
    task = SimpleNamespace(status=TaskStatus.COMPLETED, error=None)
    continuous.registry = SimpleNamespace(get=lambda task_id: task)
    project = {
        "id": "remote-one",
        "host": "10.0.0.8",
        "port": 22,
        "username": "ubuntu",
        "remote_path": "/srv/code",
        "local_path": str(tmp_path / "mirror"),
        "credential_id": "remote-one",
        "provider": "ollama",
        "model": None,
        "language": "zh",
        "host_fingerprint": "SHA256:test",
        "enabled": True,
        "poll_seconds": 60,
        "last_sync_at": 1,
        "last_error": None,
        "stage": "analyzing",
        "files_seen": 1,
        "files_excluded": 0,
        "files_oversize": 0,
        "symlinks_skipped": 0,
    }
    store.save_remote_project(project)
    continuous.projects.append(
        {
            "id": "continuous-one",
            "request": {"repo_url": project["local_path"]},
            "last_task_id": "wiki-one",
        }
    )
    manager = RemoteSyncManager(
        continuous, store=store, credentials=FakeCredentials()
    )

    assert manager.list_projects()[0]["stage"] == "analyzing"
    assert store.remote_projects["remote-one"]["stage"] == "analyzing"

    manager._reconcile_analysis_stages()

    assert store.remote_projects["remote-one"]["stage"] == "ready_for_analysis"


@pytest.mark.asyncio
async def test_sync_persists_live_progress_before_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _pass_login(monkeypatch)
    store = FakeStore()
    seen = []

    def sync(project, password, known_hosts, cancel_event=None, on_progress=None):
        result = MirrorResult(
            files_seen=3,
            dirs_seen=2,
            current_path="/srv/code/src",
            progress_message="正在列出远程目录 /srv/code/src",
        )
        if on_progress:
            on_progress(result)
            seen.append(store.remote_projects[project["id"]]["files_seen"])
        return result, "SHA256:test"

    monkeypatch.setattr("api.services.remote._sync_project", sync)
    manager = RemoteSyncManager(
        FakeContinuous(), store=store, credentials=FakeCredentials()
    )
    status = await manager.create(
        RemoteProjectRequest(
            host="10.0.0.8",
            username="ubuntu",
            password="server-secret",
            remote_path="/srv/code/demo",
            host_fingerprint="SHA256:test",
        )
    )
    await manager._active[status["id"]]

    assert seen == [3]
    project = store.remote_projects[status["id"]]
    assert project["progress_updated_at"]
    assert project["progress_message"].startswith("同步完成") or project[
        "files_seen"
    ] == 3


@pytest.mark.asyncio
async def test_create_does_not_require_ai_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _pass_login(monkeypatch)
    monkeypatch.setattr(
        "api.services.remote._sync_project",
        lambda project, password, known_hosts, cancel_event=None, on_progress=None: (
            MirrorResult(files_seen=1, changed=True),
            "SHA256:test",
        ),
    )
    store = FakeStore()
    manager = RemoteSyncManager(
        FakeContinuous(), store=store, credentials=FakeCredentials()
    )

    status = await manager.create(
        RemoteProjectRequest(
            host="10.0.0.8",
            username="ubuntu",
            password="server-secret",
            remote_path="/srv/code/demo",
            host_fingerprint="SHA256:test",
        )
    )
    await manager._active[status["id"]]

    project = store.remote_projects[status["id"]]
    assert project["stage"] == "ready_for_analysis"
    assert project["provider"] is None
    assert project["model"] is None


def test_create_request_ignores_legacy_ai_fields():
    request = RemoteProjectRequest.model_validate(
        {
            "host": "10.0.0.8",
            "username": "ubuntu",
            "password": "server-secret",
            "remote_path": "/srv/code/demo",
            "host_fingerprint": "SHA256:test",
            "provider": "openai_compatible",
            "model": "qwen-plus",
            "analyze_now": True,
        }
    )

    dumped = request.model_dump()
    assert "provider" not in dumped
    assert "model" not in dumped
    assert "analyze_now" not in dumped
    assert "provider" not in RemoteProjectRequest.model_fields
    assert "model" not in RemoteProjectRequest.model_fields
    assert "analyze_now" not in RemoteProjectRequest.model_fields


@pytest.mark.asyncio
async def test_create_ignores_analyze_now_from_legacy_client(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _pass_login(monkeypatch)
    monkeypatch.setattr(
        "api.services.remote._sync_project",
        lambda project, password, known_hosts, cancel_event=None, on_progress=None: (
            MirrorResult(files_seen=1, changed=True),
            "SHA256:test",
        ),
    )
    store = FakeStore()
    continuous = FakeContinuous()
    manager = RemoteSyncManager(
        continuous, store=store, credentials=FakeCredentials()
    )
    request = RemoteProjectRequest.model_validate(
        {
            "host": "10.0.0.8",
            "username": "ubuntu",
            "password": "server-secret",
            "remote_path": "/srv/code/demo",
            "host_fingerprint": "SHA256:test",
            "provider": "openai_compatible",
            "model": "qwen-plus",
            "analyze_now": True,
        }
    )

    status = await manager.create(request)
    await manager._active[status["id"]]

    project = store.remote_projects[status["id"]]
    assert project["stage"] == "ready_for_analysis"
    assert project["provider"] is None
    assert continuous.requests == []


@pytest.mark.asyncio
async def test_analyze_uses_current_desktop_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _pass_login(monkeypatch)
    monkeypatch.setattr(
        "api.services.remote._sync_project",
        lambda project, password, known_hosts, cancel_event=None, on_progress=None: (
            MirrorResult(files_seen=1, changed=True),
            "SHA256:test",
        ),
    )
    monkeypatch.setattr(
        "api.desktop_settings.load_desktop_settings",
        lambda: {
            "provider": "openai_compatible",
            "openai_compatible_api_key": "__keyring__",
            "base_url": "https://api.example.com/v1",
            "selected_model": "qwen-plus",
        },
    )
    store = FakeStore()
    continuous = FakeContinuous()
    manager = RemoteSyncManager(
        continuous, store=store, credentials=FakeCredentials()
    )

    status = await manager.create(
        RemoteProjectRequest(
            host="10.0.0.8",
            username="ubuntu",
            password="server-secret",
            remote_path="/srv/code/demo",
            host_fingerprint="SHA256:test",
        )
    )
    await manager._active[status["id"]]
    assert continuous.requests == []

    await manager.analyze(status["id"])

    request = continuous.requests[0][0]
    assert request.provider == "openai_compatible"
    assert request.model == "qwen-plus"
    assert request.space_id
    assert request.included_dirs == []
    assert store.remote_projects[status["id"]]["provider"] == "openai_compatible"
    assert store.remote_projects[status["id"]]["model"] == "qwen-plus"


@pytest.mark.asyncio
async def test_analyze_requires_desktop_ai_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _pass_login(monkeypatch)
    monkeypatch.setattr(
        "api.services.remote._sync_project",
        lambda project, password, known_hosts, cancel_event=None, on_progress=None: (
            MirrorResult(files_seen=1, changed=True),
            "SHA256:test",
        ),
    )
    monkeypatch.setattr("api.desktop_settings.load_desktop_settings", lambda: {})
    store = FakeStore()
    continuous = FakeContinuous()
    manager = RemoteSyncManager(
        continuous, store=store, credentials=FakeCredentials()
    )

    status = await manager.create(
        RemoteProjectRequest(
            host="10.0.0.8",
            username="ubuntu",
            password="server-secret",
            remote_path="/srv/code/demo",
            host_fingerprint="SHA256:test",
        )
    )
    await manager._active[status["id"]]

    with pytest.raises(RemoteProjectError, match="请先在设置中配置可用的 AI"):
        await manager.analyze(status["id"])

    assert continuous.requests == []
    assert store.remote_projects[status["id"]]["stage"] == "ready_for_analysis"


def _compatible_settings():
    return {
        "provider": "openai_compatible",
        "openai_compatible_api_key": "__keyring__",
        "base_url": "https://api.example.com/v1",
        "selected_model": "qwen-plus",
    }


async def _synced_remote(monkeypatch, tmp_path, store=None, continuous=None):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _pass_login(monkeypatch)
    monkeypatch.setattr(
        "api.services.remote._sync_project",
        lambda project, password, known_hosts, cancel_event=None, on_progress=None: (
            MirrorResult(files_seen=3, changed=True),
            "SHA256:test",
        ),
    )
    store = store or FakeStore()
    continuous = continuous or FakeContinuous()
    manager = RemoteSyncManager(
        continuous, store=store, credentials=FakeCredentials()
    )
    status = await manager.create(
        RemoteProjectRequest(
            host="10.0.0.8",
            username="ubuntu",
            password="server-secret",
            remote_path="/srv/code/demo",
            host_fingerprint="SHA256:test",
        )
    )
    await manager._active[status["id"]]
    project = store.remote_projects[status["id"]]
    mirror = Path(project["local_path"])
    (mirror / "packages" / "api").mkdir(parents=True)
    (mirror / "YinWang" / "br_feature_ADS_truck_0820").mkdir(parents=True)
    (mirror / "README.md").write_text("demo", encoding="utf-8")
    return manager, store, continuous, status, mirror


def test_scope_create_request_ignores_legacy_ai_fields():
    from api.schemas import RemoteScopeCreateRequest

    request = RemoteScopeCreateRequest.model_validate(
        {
            "included_dirs": ["packages/api"],
            "provider": "openai_compatible",
            "model": "qwen-plus",
            "analyze_now": True,
        }
    )
    dumped = request.model_dump()
    assert dumped["included_dirs"] == ["packages/api"]
    assert "provider" not in dumped
    assert "analyze_now" not in dumped


@pytest.mark.asyncio
async def test_detect_scopes_finds_packages(monkeypatch, tmp_path):
    manager, _store, _continuous, status, _mirror = await _synced_remote(
        monkeypatch, tmp_path
    )

    candidates = manager.detect_scopes(status["id"])
    paths = {item.path for item in candidates}
    assert "packages/api" in paths


@pytest.mark.asyncio
async def test_create_scopes_accepts_detect_and_custom_paths(monkeypatch, tmp_path):
    manager, store, continuous, status, _mirror = await _synced_remote(
        monkeypatch, tmp_path
    )

    scopes = manager.create_scopes(
        status["id"],
        ["packages/api", "YinWang/br_feature_ADS_truck_0820"],
    )

    assert {tuple(item["included_dirs"]) for item in scopes} == {
        ("packages/api",),
        ("YinWang/br_feature_ADS_truck_0820",),
    }
    assert all(item["label"].startswith("ubuntu@10.0.0.8:/srv/code/demo") for item in scopes)
    assert continuous.requests == []
    assert len(store.knowledge_spaces) == 2
    listed = manager.list_scopes(status["id"])
    assert len(listed) == 2
    assert manager._status(store.remote_projects[status["id"]])["stage"] != "analyzing"


@pytest.mark.asyncio
async def test_create_scopes_rejects_parent_and_missing_paths(monkeypatch, tmp_path):
    manager, _store, _continuous, status, _mirror = await _synced_remote(
        monkeypatch, tmp_path
    )

    with pytest.raises(RemoteProjectError, match="上级路径"):
        manager.create_scopes(status["id"], ["../escape"])
    with pytest.raises(RemoteProjectError, match="找不到目录"):
        manager.create_scopes(status["id"], ["does-not-exist"])
    with pytest.raises(RemoteProjectError, match="绝对路径"):
        manager.create_scopes(status["id"], ["/srv/code/demo/packages"])
    with pytest.raises(RemoteProjectError, match="请至少选择一个子目录"):
        manager.create_scopes(status["id"], [])


@pytest.mark.asyncio
async def test_create_scopes_ignores_analyze_now_and_does_not_register(
    monkeypatch, tmp_path
):
    from api.schemas import RemoteScopeCreateRequest

    manager, _store, continuous, status, _mirror = await _synced_remote(
        monkeypatch, tmp_path
    )
    request = RemoteScopeCreateRequest.model_validate(
        {
            "included_dirs": ["packages/api"],
            "provider": "openai_compatible",
            "analyze_now": True,
        }
    )

    manager.create_scopes(status["id"], request.included_dirs)
    assert continuous.requests == []


@pytest.mark.asyncio
async def test_root_and_child_analysis_use_different_repo_keys(monkeypatch, tmp_path):
    manager, store, continuous, status, _mirror = await _synced_remote(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(
        "api.desktop_settings.load_desktop_settings", _compatible_settings
    )
    child = manager.create_scopes(status["id"], ["packages/api"])[0]

    await manager.analyze(status["id"])
    await manager.analyze_scope(status["id"], child["space_id"])

    root_request, child_request = continuous.requests[0][0], continuous.requests[1][0]
    assert root_request.space_id != child_request.space_id
    assert root_request.repo_key != child_request.repo_key
    assert root_request.repo_key == f"space_{root_request.space_id}"
    assert child_request.included_dirs == ["packages/api"]
    assert store.remote_projects[status["id"]]["stage"] == "analyzing"
    children = [
        item
        for item in manager.list_scopes(status["id"])
        if item["included_dirs"]
    ]
    assert children[0]["last_task_id"]


@pytest.mark.asyncio
async def test_analyze_scope_requires_desktop_ai_settings(monkeypatch, tmp_path):
    manager, _store, continuous, status, _mirror = await _synced_remote(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr("api.desktop_settings.load_desktop_settings", lambda: {})
    child = manager.create_scopes(status["id"], ["packages/api"])[0]

    with pytest.raises(RemoteProjectError, match="请先在设置中配置可用的 AI"):
        await manager.analyze_scope(status["id"], child["space_id"])
    assert continuous.requests == []


@pytest.mark.asyncio
async def test_remove_remote_project_deletes_child_spaces(monkeypatch, tmp_path):
    manager, store, continuous, status, _mirror = await _synced_remote(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(
        "api.desktop_settings.load_desktop_settings", _compatible_settings
    )
    child = manager.create_scopes(status["id"], ["packages/api"])[0]
    await manager.analyze_scope(status["id"], child["space_id"])
    assert store.knowledge_spaces
    assert continuous.projects

    deleted = await manager.remove(status["id"])

    assert deleted is True
    assert store.knowledge_spaces == {}
    assert continuous.projects == []
    assert store.remote_projects == {}
