from api.services import terminal


class FakeChannel:
    def __init__(self):
        self.closed = False
        self.timeout = None
        self.sent = []
        self.size = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.sent.append(data)

    def resize_pty(self, width, height):
        self.size = (width, height)

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, channel):
        self.channel = channel
        self.closed = False

    def invoke_shell(self, **_):
        return self.channel

    def close(self):
        self.closed = True


class FakeStore:
    def get_remote_project(self, project_id):
        if project_id != "remote-one":
            return None
        return {
            "id": project_id,
            "credential_id": project_id,
            "remote_path": "/srv/code/demo project",
        }


class FakeCredentials:
    def get(self, credential_id):
        return "secret" if credential_id == "remote-one" else None


def test_terminal_session_opens_in_remote_directory(monkeypatch, tmp_path):
    channel = FakeChannel()
    client = FakeClient(channel)
    monkeypatch.setattr(
        terminal,
        "connect_ssh",
        lambda project, password, known_hosts: (client, "SHA256:test"),
    )
    manager = terminal.TerminalSessionManager(
        FakeStore(), credentials=FakeCredentials()
    )
    manager.known_hosts_path = tmp_path / "known_hosts"

    session = manager.open("remote-one", columns=100, rows=28)
    session.resize(120, 32)
    session.send(b"pwd\r")
    manager.close(session.id)

    assert channel.timeout == 0.25
    assert channel.sent[0] == b"cd -- '/srv/code/demo project' 2>/dev/null || true\r\n"
    assert channel.sent[1] == b"pwd\r"
    assert channel.size == (120, 32)
    assert channel.closed is True
    assert client.closed is True


def test_terminal_rejects_unknown_project(tmp_path):
    manager = terminal.TerminalSessionManager(
        FakeStore(), credentials=FakeCredentials()
    )
    manager.known_hosts_path = tmp_path / "known_hosts"

    try:
        manager.open("missing")
    except terminal.RemoteProjectError as error:
        assert str(error) == "远程项目不存在"
    else:
        raise AssertionError("unknown remote project was accepted")


def test_terminal_enforces_concurrency_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal, "MAX_TERMINAL_SESSIONS", 0)
    manager = terminal.TerminalSessionManager(
        FakeStore(), credentials=FakeCredentials()
    )
    manager.known_hosts_path = tmp_path / "known_hosts"

    try:
        manager.open("remote-one")
    except terminal.RemoteProjectError as error:
        assert "上限" in str(error)
    else:
        raise AssertionError("terminal concurrency limit was ignored")


def test_terminal_authorization_requires_token_and_desktop_origin():
    authorize = terminal.authorized_terminal_request

    assert authorize("secret", "secret", "http://tauri.localhost", production=True)
    assert not authorize("secret", "wrong", "http://tauri.localhost", production=True)
    assert not authorize("secret", "secret", "https://evil.example", production=True)
    assert authorize("secret", "secret", "http://localhost:1420", production=False)
    assert not authorize("secret", "secret", "http://localhost:1420", production=True)
