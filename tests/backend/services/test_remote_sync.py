import stat
from pathlib import Path
from types import SimpleNamespace

import paramiko
import pytest

from api.schemas import RemoteProjectRequest
from api.services.remote import (
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
                _entry("linked.py", stat.S_IFLNK),
                _entry("bad:name.py", stat.S_IFREG, 4),
            ],
            "/srv/code/src": [_entry("main.py", stat.S_IFREG, 12)],
        }
        self.files = {
            "/srv/code/README.md": b"# Demo",
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


def test_mirror_directory_updates_and_deletes_without_following_links(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "stale.txt").write_text("remove me", encoding="utf-8")

    count, changed = _mirror_directory(FakeSftp(), "/srv/code", mirror)

    assert changed is True
    assert count == 2
    assert (mirror / "README.md").read_bytes() == b"# Demo"
    assert (mirror / "src" / "main.py").read_bytes() == b"print('ok')\n"
    assert not (mirror / "stale.txt").exists()
    assert not (mirror / "linked.py").exists()
    assert not (mirror / "bad:name.py").exists()
    assert not (mirror / "build").exists()


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
async def test_create_stores_password_only_in_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(
        "api.services.remote._sync_project",
        lambda project, password, known_hosts: (2, True, "SHA256:test"),
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
        )
    )

    project = store.remote_projects[status["id"]]
    assert credentials.values[project["credential_id"]] == "server-secret"
    assert "password" not in project
    assert "credential_id" not in status
    assert continuous.requests[0][0].repo_url == project["local_path"]
