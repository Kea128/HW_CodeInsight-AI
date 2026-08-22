"""Shared SSH connection and credential helpers for remote desktop features."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Any

import keyring
import paramiko

CREDENTIAL_SERVICE = "CodeInsight-AI.Remote"
DEFAULT_SSH_TIMEOUT = 15.0


class RemoteProjectError(RuntimeError):
    pass


class CredentialStore:
    def set(self, credential_id: str, password: str) -> None:
        keyring.set_password(CREDENTIAL_SERVICE, credential_id, password)

    def get(self, credential_id: str) -> str | None:
        try:
            return keyring.get_password(CREDENTIAL_SERVICE, credential_id)
        except keyring.errors.KeyringError:
            return None

    def delete(self, credential_id: str) -> None:
        try:
            keyring.delete_password(CREDENTIAL_SERVICE, credential_id)
        except keyring.errors.KeyringError:
            pass


def remote_data_root() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    path = root / "CodeInsight-AI"
    path.mkdir(parents=True, exist_ok=True)
    return path


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return f"SHA256:{base64.b64encode(digest).decode().rstrip('=')}"


def friendly_connection_error(error: Exception) -> RemoteProjectError:
    if isinstance(error, paramiko.AuthenticationException):
        return RemoteProjectError("Ubuntu 用户名或密码错误")
    if isinstance(error, paramiko.BadHostKeyException):
        return RemoteProjectError("服务器主机密钥已变化，已拒绝连接")
    if isinstance(error, (TimeoutError, paramiko.SSHException)):
        return RemoteProjectError(f"SSH 连接失败：{error}")
    if isinstance(error, OSError):
        return RemoteProjectError(f"无法连接 Ubuntu 服务器：{error}")
    return RemoteProjectError(f"远程连接失败：{error}")


def ssh_timeout() -> float:
    try:
        configured = float(os.environ.get("CODEINSIGHT_SSH_TIMEOUT", "15"))
        return max(1.0, min(configured, 120.0))
    except ValueError:
        return DEFAULT_SSH_TIMEOUT


def probe_host_fingerprint(host: str, port: int = 22) -> tuple[str, str]:
    """Read the server key before authentication; no username/password is sent."""
    transport: paramiko.Transport | None = None
    connection: socket.socket | None = None
    try:
        connection = socket.create_connection((host, port), timeout=ssh_timeout())
        transport = paramiko.Transport(connection)
        transport.banner_timeout = ssh_timeout()
        transport.start_client(timeout=ssh_timeout())
        key = transport.get_remote_server_key()
        return fingerprint(key), key.get_name()
    except Exception as error:
        raise friendly_connection_error(error) from error
    finally:
        if transport is not None:
            transport.close()
        elif connection is not None:
            connection.close()


class _ExpectedFingerprintPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected: str):
        self.expected = expected

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        actual = fingerprint(key)
        if not secrets.compare_digest(self.expected, actual):
            raise RemoteProjectError("服务器主机密钥已变化，已拒绝连接")
        client.get_host_keys().add(hostname, key.get_name(), key)


def connect_ssh(
    project: dict[str, Any], password: str, known_hosts_path: Path
) -> tuple[paramiko.SSHClient, str]:
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.touch(exist_ok=True)
    expected_fingerprint = project.get("host_fingerprint")
    if not expected_fingerprint:
        raise RemoteProjectError("必须先确认服务器主机指纹")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_ExpectedFingerprintPolicy(expected_fingerprint))
    try:
        client.connect(
            hostname=project["host"],
            port=project["port"],
            username=project["username"],
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=ssh_timeout(),
            auth_timeout=ssh_timeout(),
            banner_timeout=ssh_timeout(),
        )
        transport = client.get_transport()
        if transport is None:
            raise paramiko.SSHException("SSH transport is unavailable")
        transport.set_keepalive(30)
        server_fingerprint = fingerprint(transport.get_remote_server_key())
        if not secrets.compare_digest(expected_fingerprint, server_fingerprint):
            raise RemoteProjectError("服务器主机密钥已变化，已拒绝连接")
        return client, server_fingerprint
    except RemoteProjectError:
        client.close()
        raise
    except Exception as error:
        client.close()
        raise friendly_connection_error(error) from error
