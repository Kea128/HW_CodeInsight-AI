"""End-to-end desktop flows: local knowledge, Q&A, and Ubuntu SSH sync."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from pathlib import Path

import paramiko
import pytest
from fastapi.testclient import TestClient

from api.schemas import WikiCacheData, WikiPage, WikiStructureModel
from api.schemas.repo import RepoInfo
from api.services.remote import RemoteSyncManager
from api.services.ssh_client import fingerprint
from api.services.wiki.store import WikiTaskStore


class _MemoryCredentials:
    def __init__(self):
        self.values = {}

    def set(self, credential_id, password):
        self.values[credential_id] = password

    def get(self, credential_id):
        return self.values.get(credential_id)

    def delete(self, credential_id):
        self.values.pop(credential_id, None)


class _PasswordServer(paramiko.ServerInterface):
    def __init__(self, root: Path):
        self.root = root

    def check_auth_password(self, username, password):
        if username == "ubuntu" and password == "testpass":
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_subsystem_request(self, channel, name):
        return name == "sftp"


class _UbuntuSSH:
    def __init__(self, root: Path):
        self.root = root
        self.host_key = paramiko.RSAKey.generate(2048)
        self.fingerprint = fingerprint(self.host_key)
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.3)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)

    def _handle(self, connection):
        transport = paramiko.Transport(connection)
        transport.add_server_key(self.host_key)
        try:
            transport.start_server(server=_PasswordServer(self.root))
            transport.accept(20)
            while transport.is_active() and not self._stop.is_set():
                time.sleep(0.05)
        except Exception:
            pass
        finally:
            transport.close()

    def _accept(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()


@pytest.fixture
def e2e_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("CODEINSIGHT_DB_PATH", str(tmp_path / "e2e.db"))
    monkeypatch.setenv("NODE_ENV", "development")
    monkeypatch.delenv("CODEINSIGHT_DESKTOP_TOKEN", raising=False)
    cache_dir = tmp_path / "wikicache"
    cache_dir.mkdir()
    monkeypatch.setattr("api.services.wiki.io.WIKI_CACHE_DIR", str(cache_dir))
    store = WikiTaskStore(str(tmp_path / "e2e.db"))
    return {"tmp": tmp_path, "store": store, "cache_dir": cache_dir}


@pytest.fixture
def e2e_client(e2e_env, monkeypatch):
    store = e2e_env["store"]
    import api.routers.continuous as continuous_router
    import api.routers.knowledge as knowledge_router
    import api.routers.remote as remote_router
    import api.services.knowledge.ask as ask_mod

    monkeypatch.setattr(knowledge_router, "wiki_task_store", store)

    async def fake_register(request, **_options):
        return {"id": request.repo_key, "last_task_id": request.repo_key, "request": request.model_dump()}

    monkeypatch.setattr(continuous_router.manager, "register", fake_register)
    monkeypatch.setattr(continuous_router.manager, "store", store)

    async def fake_research(_request):
        async def chunks():
            yield "认证入口在 auth/service.py，使用 JWT。"

        return chunks()

    monkeypatch.setattr(ask_mod, "research_chat", fake_research)
    monkeypatch.setattr(ask_mod, "prepare_repo_index", _fail_index)

    remote_router.manager = RemoteSyncManager(
        continuous_router.manager,
        store=store,
        credentials=_MemoryCredentials(),
    )
    from api.main import app

    with TestClient(app) as client:
        yield client


async def _fail_index(_request):
    raise RuntimeError("index skipped in e2e")


def test_e2e_local_knowledge_and_ask(e2e_client, e2e_env):
    workspace = e2e_env["tmp"] / "workspace"
    (workspace / "packages" / "api").mkdir(parents=True)
    (workspace / "packages" / "api" / "auth").mkdir()
    (workspace / "packages" / "api" / "auth" / "service.py").write_text(
        "def login(): return 'jwt'\n",
        encoding="utf-8",
    )
    (workspace / "apps" / "web").mkdir(parents=True)

    detected = e2e_client.post(
        "/knowledge/spaces/detect",
        json={"workspace_root": str(workspace)},
    )
    assert detected.status_code == 200, detected.text
    paths = {item["path"] for item in detected.json()["candidates"]}
    assert "packages/api" in paths
    assert "apps/web" in paths

    created = e2e_client.post(
        "/knowledge/spaces",
        json={
            "workspace_root": str(workspace),
            "included_dirs": ["packages/api"],
            "language": "zh",
            "provider": "openai_compatible",
            "model": "deepseek-chat",
            "analyze_now": True,
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["task_ids"]
    space = body["spaces"][0]
    assert space["label"].endswith("packages/api")

    listed = e2e_client.get("/knowledge/spaces")
    assert listed.status_code == 200
    assert any(item["space_id"] == space["space_id"] for item in listed.json())

    page = WikiPage(
        id="auth",
        title="认证流程",
        content="登录使用 JWT，入口在 auth/service.py",
        filePaths=["packages/api/auth/service.py"],
        importance="high",
        relatedPages=[],
    )
    from api.services.wiki.io import save_wiki_cache

    saved = asyncio.run(
        save_wiki_cache(
            owner="local",
            repo=space["label"],
            repo_type="local",
            language="zh",
            wiki_cache=WikiCacheData(
                wiki_structure=WikiStructureModel(
                    id="wiki",
                    title=space["label"],
                    description="",
                    pages=[page],
                ),
                generated_pages={"auth": page},
                repo=RepoInfo(owner="local", repo=space["label"], type="local", token=None),
                provider="openai_compatible",
                model="deepseek-chat",
            ),
            space_id=space["space_id"],
        )
    )
    assert saved is True

    answer = e2e_client.post(
        "/knowledge/ask",
        json={"space_id": space["space_id"], "question": "认证流程在哪里实现？"},
    )
    assert answer.status_code == 200, answer.text
    text = answer.text
    assert "JWT" in text
    assert "auth/service.py" in text


class _Channel:
    def settimeout(self, _value):
        return None


class _LocalSFTP:
    def __init__(self, root: Path):
        self.root = root

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get_channel(self):
        return _Channel()

    def close(self):
        return None

    def _local(self, remote_path: str) -> Path:
        return self.root / remote_path.replace("\\", "/").lstrip("/")

    def lstat(self, path):
        local = self._local(path)
        if not local.exists():
            raise OSError(path)
        return local.stat()

    def listdir_attr(self, path):
        local = self._local(path)
        entries = []
        for name in os.listdir(local):
            stat_result = (local / name).stat()
            entries.append(
                type(
                    "Entry",
                    (),
                    {
                        "filename": name,
                        "st_mode": stat_result.st_mode,
                        "st_size": stat_result.st_size,
                        "st_mtime": stat_result.st_mtime,
                        "st_atime": stat_result.st_atime,
                    },
                )()
            )
        return entries

    def open(self, remote_path, mode):
        return self._local(remote_path).open(mode)


class _LocalSSH:
    def __init__(self, root: Path):
        self.root = root

    def open_sftp(self):
        return _LocalSFTP(self.root)

    def close(self):
        return None


def test_e2e_ubuntu_remote_sync(e2e_client, e2e_env, monkeypatch):
    remote_root = e2e_env["tmp"] / "ubuntu-fs"
    project_dir = remote_root / "srv" / "code" / "demo"
    project_dir.mkdir(parents=True)
    (project_dir / "README.md").write_text("# Ubuntu demo\n", encoding="utf-8")
    (project_dir / "src").mkdir()
    (project_dir / "src" / "main.py").write_text("print('remote')\n", encoding="utf-8")

    server = _UbuntuSSH(remote_root).start()

    def fake_connect(project, password, _known_hosts):
        assert password == "testpass"
        assert project["host_fingerprint"] == server.fingerprint
        return _LocalSSH(remote_root), project["host_fingerprint"]

    monkeypatch.setattr("api.services.remote._connect", fake_connect)
    try:
        probe = e2e_client.post(
            "/remote/fingerprint",
            json={"host": "127.0.0.1", "port": server.port},
        )
        assert probe.status_code == 200, probe.text
        assert probe.json()["fingerprint"] == server.fingerprint

        created = e2e_client.post(
            "/remote/projects",
            json={
                "host": "127.0.0.1",
                "port": server.port,
                "username": "ubuntu",
                "password": "testpass",
                "remote_path": "/srv/code/demo",
                "poll_seconds": 60,
                "provider": "openai_compatible",
                "model": "qwen-plus",
                "language": "zh",
                "host_fingerprint": server.fingerprint,
                "analyze_now": True,
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["stage"] != "analyzing"
        project_id = created.json()["id"]

        status = None
        for _ in range(50):
            listing = e2e_client.get("/remote/projects")
            assert listing.status_code == 200
            status = next(item for item in listing.json() if item["id"] == project_id)
            if status["stage"] in {"ready_for_analysis", "failed"} or status["files_seen"]:
                break
            time.sleep(0.1)
        assert status is not None
        assert status["stage"] != "failed", status.get("last_error")
        assert status["stage"] != "analyzing"
        assert status["files_seen"] >= 2

        mirrored = Path(e2e_env["tmp"] / "appdata" / "CodeInsight-AI" / "remote-repos" / project_id)
        assert (mirrored / "README.md").read_text(encoding="utf-8") == "# Ubuntu demo\n"
        assert (mirrored / "src" / "main.py").read_text(encoding="utf-8") == "print('remote')\n"
    finally:
        server.close()
