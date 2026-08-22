"""Interactive SSH PTY sessions used by the embedded desktop terminal."""

from __future__ import annotations

import asyncio
import secrets
import shlex
import threading
import time
from dataclasses import dataclass
from typing import Any

import paramiko

from api.services.ssh_client import (
    CredentialStore,
    RemoteProjectError,
    connect_ssh,
    remote_data_root,
)

MAX_TERMINAL_SESSIONS = 8
MAX_INPUT_BYTES = 64 * 1024
SESSION_IDLE_SECONDS = 60 * 60
DESKTOP_ORIGINS = {
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
}


def authorized_terminal_request(
    expected_token: str | None,
    supplied_token: str,
    origin: str | None,
    *,
    production: bool,
) -> bool:
    if not expected_token or not secrets.compare_digest(expected_token, supplied_token):
        return False
    if origin in DESKTOP_ORIGINS:
        return True
    return not production and bool(
        origin and origin.startswith(("http://127.0.0.1:", "http://localhost:"))
    )


@dataclass
class TerminalSession:
    id: str
    project_id: str
    client: paramiko.SSHClient
    channel: paramiko.Channel
    last_activity: float

    def receive(self) -> bytes:
        try:
            data = self.channel.recv(32768)
        except TimeoutError:
            return b""
        if data:
            self.last_activity = time.monotonic()
        return data

    def send(self, data: bytes) -> None:
        if len(data) > MAX_INPUT_BYTES:
            raise RemoteProjectError("终端输入数据过大")
        self.channel.sendall(data)
        self.last_activity = time.monotonic()

    def resize(self, columns: int, rows: int) -> None:
        self.channel.resize_pty(
            width=max(20, min(columns, 500)),
            height=max(5, min(rows, 200)),
        )
        self.last_activity = time.monotonic()

    def close(self) -> None:
        self.channel.close()
        self.client.close()


class TerminalSessionManager:
    def __init__(
        self,
        store: Any,
        credentials: CredentialStore | None = None,
    ):
        self.store = store
        self.credentials = credentials or CredentialStore()
        self.known_hosts_path = remote_data_root() / "known_hosts"
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = threading.RLock()
        self._opening = 0

    def open(
        self, project_id: str, columns: int = 120, rows: int = 32
    ) -> TerminalSession:
        with self._lock:
            self._remove_expired_locked()
            if len(self._sessions) + self._opening >= MAX_TERMINAL_SESSIONS:
                raise RemoteProjectError("终端标签数量已达到上限")
            self._opening += 1
        try:
            project = self.store.get_remote_project(project_id)
            if not project:
                raise RemoteProjectError("远程项目不存在")
            password = self.credentials.get(project["credential_id"])
            if not password:
                raise RemoteProjectError("Windows 凭据管理器中找不到服务器密码")

            client, _ = connect_ssh(project, password, self.known_hosts_path)
            try:
                channel = client.invoke_shell(
                    term="xterm-256color",
                    width=max(20, min(columns, 500)),
                    height=max(5, min(rows, 200)),
                )
                channel.settimeout(0.25)
                channel.sendall(
                    (
                        f"cd -- {shlex.quote(project['remote_path'])} "
                        "2>/dev/null || true\r\n"
                    ).encode()
                )
            except Exception as error:
                client.close()
                raise RemoteProjectError(f"远程终端启动失败：{error}") from error
        finally:
            with self._lock:
                self._opening -= 1

        session = TerminalSession(
            id=secrets.token_hex(16),
            project_id=project_id,
            client=client,
            channel=channel,
            last_activity=time.monotonic(),
        )
        with self._lock:
            self._sessions[session.id] = session
        return session

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            session.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def _remove_expired_locked(self) -> None:
        deadline = time.monotonic() - SESSION_IDLE_SECONDS
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.last_activity < deadline or session.channel.closed
        ]
        sessions = [self._sessions.pop(session_id) for session_id in expired]
        for session in sessions:
            session.close()


async def relay_terminal(websocket: Any, session: TerminalSession) -> None:
    async def send_output() -> None:
        while not session.channel.closed:
            data = await asyncio.to_thread(session.receive)
            if data:
                await websocket.send_bytes(data)
            elif session.channel.exit_status_ready():
                break

    async def receive_input() -> None:
        while not session.channel.closed:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                await asyncio.to_thread(session.send, data)
                continue
            text = message.get("text")
            if not text:
                continue
            import json

            control = json.loads(text)
            if control.get("type") == "resize":
                await asyncio.to_thread(
                    session.resize,
                    int(control.get("columns", 120)),
                    int(control.get("rows", 32)),
                )

    tasks = [
        asyncio.create_task(send_output()),
        asyncio.create_task(receive_input()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)
