import stat
from pathlib import Path
from types import SimpleNamespace

import paramiko
import pytest

from api.schemas import RemoteProjectRequest
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

    def get(self, remote_path, local_path):
        Path(local_path).write_bytes(self.files[remote_path])


class FakeStore:
    def __init__(self):
        self.remote_projects = {}
        self.continuous_projects = {}

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


def test_mirror_detects_same_size_same_mtime_content_change(tmp_path):
    sftp = FakeSftp()
    mirror = tmp_path / "mirror"
    _mirror_directory(sftp, "/srv/code", mirror)
    sftp.files["/srv/code/README.md"] = b"# Damo"

    result = _mirror_directory(sftp, "/srv/code", mirror)

    assert result.changed is True
    assert (mirror / "README.md").read_bytes() == b"# Damo"


def test_remote_project_id_is_stable_and_contains_no_credentials():
    first = _project_id("10.0.0.8", 22, "ubuntu", "/srv/code")
    second = _project_id("10.0.0.8", 22, "ubuntu", "/srv/code")

    assert first == second
    assert first.startswith("remote-")
    assert "ubuntu" not in first


def test_authentication_error_does_not_echo_credentials():
    error = _friendly_connection_error(paramiko.AuthenticationException("denied"))

    assert str(error) == "Ubuntu 用户名或密码错误"
    assert "denied" not in str(error)


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
async def test_create_stores_password_only_in_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(
        "api.services.remote._sync_project",
        lambda project, password, known_hosts, cancel_event=None: (
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
            provider="ollama",
            host_fingerprint="SHA256:test",
        )
    )

    assert status["stage"] == "saved"
    await manager._active[status["id"]]
    project = store.remote_projects[status["id"]]
    assert credentials.values[project["credential_id"]] == "server-secret"
    assert "password" not in project
    assert "credential_id" not in status
    assert continuous.requests[0][0].repo_url == project["local_path"]


@pytest.mark.asyncio
async def test_background_analysis_failure_keeps_project_retryable(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def sync(project, password, known_hosts, cancel_event=None):
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
            provider="ollama",
            host_fingerprint="SHA256:test",
        )
    )
    await manager._active[status["id"]]

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
