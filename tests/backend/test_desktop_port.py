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


def test_parse_tasklist_csv_reads_name_and_pid():
    rows = list(
        desktop_port.parse_tasklist_csv(
            '"codeinsight-daemon-x86_64-pc-windows-msvc.exe","4321","RDP-Tcp#0","2","80 K"\n'
            '"chrome.exe","99","Console","1","1 K"\n'
        )
    )
    assert rows == [
        ("codeinsight-daemon-x86_64-pc-windows-msvc.exe", 4321),
        ("chrome.exe", 99),
    ]


def test_kill_known_daemon_images_skips_current_process(monkeypatch):
    current = os.getpid()
    killed: list[int] = []
    monkeypatch.setattr(
        desktop_port,
        "list_windows_processes",
        lambda: [
            ("codeinsight-daemon.exe", current),
            ("codeinsight-daemon.exe", os.getppid()),
            ("codeinsight-daemon-x86_64-pc-windows-msvc.exe", 4242),
            ("chrome.exe", 7),
        ],
    )
    monkeypatch.setattr(desktop_port, "kill_process_tree", killed.append)

    desktop_port.kill_known_daemon_images()

    assert killed == [4242]


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


def test_reclaim_does_not_kill_current_process_listener(tmp_path, monkeypatch):
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


def test_reclaim_kills_unnamed_listener_without_pid_file(tmp_path, monkeypatch):
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
        assert desktop_port.port_is_open("127.0.0.1", port)
        desktop_port.reclaim_listen_port("127.0.0.1", port, timeout=5)
        assert not desktop_port.port_is_open("127.0.0.1", port)
        holder.wait(timeout=5)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
