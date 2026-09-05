import os
import socket
import subprocess
import sys
import time

import pytest

from api import desktop_port


def test_port_is_open_detects_listener():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert desktop_port.port_is_open("127.0.0.1", port)
    finally:
        server.close()
    deadline = time.monotonic() + 2
    while desktop_port.port_is_open("127.0.0.1", port) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not desktop_port.port_is_open("127.0.0.1", port)


def test_pid_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = desktop_port.write_current_pid()
    assert path == tmp_path / "CodeInsight-AI" / "daemon.pid"
    assert desktop_port.read_pid_file() == os.getpid()
    desktop_port.remove_pid_file()
    assert desktop_port.read_pid_file() is None


def test_is_our_daemon_name():
    assert desktop_port.is_our_daemon_name(
        "codeinsight-daemon-x86_64-pc-windows-msvc.exe"
    )
    assert desktop_port.is_our_daemon_name(r"C:\Temp\codeinsight-daemon.exe")
    assert not desktop_port.is_our_daemon_name("chrome.exe")
    assert not desktop_port.is_our_daemon_name("python.exe")


def test_reclaim_kills_process_recorded_in_pid_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket,time;"
                "s=socket.socket();s.bind(('127.0.0.1',0));s.listen(1);"
                "print(s.getsockname()[1], flush=True);time.sleep(30)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        port = int(holder.stdout.readline().strip())
        pid_path = tmp_path / "CodeInsight-AI" / "daemon.pid"
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(holder.pid), encoding="utf-8")
        assert desktop_port.port_is_open("127.0.0.1", port)
        desktop_port.reclaim_listen_port("127.0.0.1", port, timeout=5)
        assert not desktop_port.port_is_open("127.0.0.1", port)
        holder.wait(timeout=5)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_reclaim_leaves_foreign_listener_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        with pytest.raises(RuntimeError, match="仍被占用"):
            desktop_port.reclaim_listen_port("127.0.0.1", port, timeout=0.4)
        assert desktop_port.port_is_open("127.0.0.1", port)
    finally:
        server.close()
