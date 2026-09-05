"""Keep a single analysis daemon bound to the desktop loopback port."""

from __future__ import annotations

import csv
import io
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
_PID_FILE_NAME = "daemon.pid"
_CREATE_NO_WINDOW = 0x08000000
_DAEMON_NAME_MARKERS = ("codeinsight-daemon",)
LogFn = Callable[[str], None]


def desktop_port() -> int:
    return int(os.environ.get("PORT", str(DEFAULT_PORT)))


def pid_file_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    return root / "CodeInsight-AI" / _PID_FILE_NAME


def port_is_open(host: str, port: int, timeout: float = 0.3) -> bool:
    del timeout
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def read_pid_file(path: Path | None = None) -> int | None:
    target = path or pid_file_path()
    try:
        raw = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    return int(raw)


def write_current_pid(path: Path | None = None) -> Path:
    target = path or pid_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(os.getpid()), encoding="utf-8")
    return target


def remove_pid_file(path: Path | None = None) -> None:
    target = path or pid_file_path()
    try:
        if read_pid_file(target) in {None, os.getpid()}:
            target.unlink(missing_ok=True)
    except OSError:
        pass


def is_our_daemon_name(name: str) -> bool:
    lowered = Path(name).name.lower()
    return any(marker in lowered for marker in _DAEMON_NAME_MARKERS)


def kill_known_daemon_images() -> None:
    """End leftover sidecar processes, but never the process that is starting."""
    protected = _protected_pids()
    for name, pid in list_windows_processes():
        if pid not in protected and is_our_daemon_name(name):
            kill_process_tree(pid)


def list_windows_processes() -> Iterator[tuple[str, int]]:
    if os.name != "nt":
        return
    completed = _run(["tasklist", "/FO", "CSV", "/NH"])
    if completed.returncode != 0:
        return
    yield from parse_tasklist_csv(completed.stdout)


def parse_tasklist_csv(output: str) -> Iterator[tuple[str, int]]:
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 2 or not row[1].isdigit():
            continue
        yield row[0], int(row[1])


def reclaim_listen_port(
    host: str = DEFAULT_HOST,
    port: int | None = None,
    *,
    log: LogFn | None = None,
    timeout: float = 8.0,
) -> None:
    """Free the dedicated desktop port, including leftover 0.2.6/0.2.7 daemons."""
    listen_port = DEFAULT_PORT if port is None else port
    stale = _reclaim_candidates(listen_port)
    if stale:
        _emit(log, f"reclaiming leftover analysis daemon on {host}:{listen_port} ({_format_pids(stale)})")
        for pid in stale:
            kill_process_tree(pid)
    deadline = time.monotonic() + timeout
    while port_is_open(host, listen_port) and time.monotonic() < deadline:
        leftover = _reclaim_candidates(listen_port)
        for pid in leftover:
            kill_process_tree(pid)
        time.sleep(0.2)
    if not port_is_open(host, listen_port):
        return
    raise RuntimeError(
        f"分析引擎端口 {host}:{listen_port} 仍被占用。"
        "请完全退出 CodeInsight-AI 后重试，或结束残留的 codeinsight-daemon 进程。"
    )


def _protected_pids() -> set[int]:
    pids = {os.getpid(), 0}
    parent = os.getppid()
    if parent > 0:
        pids.add(parent)
    return pids


def kill_process_tree(pid: int) -> None:
    if pid in _protected_pids():
        return
    if os.name == "nt":
        _run(["taskkill", "/F", "/T", "/PID", str(pid)])
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def _reclaim_candidates(port: int) -> set[int]:
    protected = _protected_pids()
    pids: set[int] = set()
    recorded = read_pid_file()
    if recorded and recorded not in protected and _process_exists(recorded):
        pids.add(recorded)
    for pid in _listener_pids(port):
        if pid not in protected:
            pids.add(pid)
    return pids


def _listener_pids(port: int) -> set[int]:
    if os.name != "nt":
        return set()
    completed = _run(["netstat", "-ano", "-p", "tcp"])
    if completed.returncode != 0:
        return set()
    pids: set[int] = set()
    for raw_line in completed.stdout.splitlines():
        line = raw_line.split()
        if len(line) < 5 or line[0].upper() != "TCP":
            continue
        if line[3].upper() != "LISTENING":
            continue
        local = line[1]
        _host, separator, local_port = local.rpartition(":")
        if not separator or local_port != str(port):
            continue
        if line[4].isdigit():
            pids.add(int(line[4]))
    return pids


def _process_name(pid: int) -> str:
    if os.name != "nt":
        return ""
    completed = _run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return ""
    row = completed.stdout.splitlines()[0]
    if row.startswith('"'):
        return row.split('"', 2)[1]
    return row.split(",", 1)[0]


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return bool(_process_name(pid))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "args": args,
        "capture_output": True,
        "text": True,
        "timeout": 10,
    }
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        return subprocess.run(**kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 1, "", "")


def _format_pids(pids: set[int]) -> str:
    return "pid " + ", ".join(str(pid) for pid in sorted(pids))


def _emit(log: LogFn | None, message: str) -> None:
    if log is not None:
        log(message)
