"""Shared SSH connection and credential helpers for remote desktop features."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

import keyring
import paramiko

CREDENTIAL_SERVICE = "CodeInsight-AI.Remote"


class RemoteProjectError(RuntimeError):
    pass


class CredentialStore:
    def set(self, credential_id: str, password: str) -> None:
        keyring.set_password(CREDENTIAL_SERVICE, credential_id, password)

    def get(self, credential_id: str) -> str | None:
        return keyring.get_password(CREDENTIAL_SERVICE, credential_id)

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


def connect_ssh(
    project: dict[str, Any], password: str, known_hosts_path: Path
) -> tuple[paramiko.SSHClient, str]:
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.touch(exist_ok=True)
    client = paramiko.SSHClient()
    client.load_host_keys(str(known_hosts_path))
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=project["host"],
            port=project["port"],
            username=project["username"],
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
            auth_timeout=15,
            banner_timeout=15,
        )
        transport = client.get_transport()
        if transport is None:
            raise paramiko.SSHException("SSH transport is unavailable")
        transport.set_keepalive(30)
        server_fingerprint = fingerprint(transport.get_remote_server_key())
        expected_fingerprint = project.get("host_fingerprint")
        if expected_fingerprint and expected_fingerprint != server_fingerprint:
            raise RemoteProjectError("服务器主机密钥已变化，已拒绝连接")
        client.save_host_keys(str(known_hosts_path))
        return client, server_fingerprint
    except RemoteProjectError:
        client.close()
        raise
    except Exception as error:
        client.close()
        raise friendly_connection_error(error) from error
