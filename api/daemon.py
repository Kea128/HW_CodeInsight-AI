"""Frozen desktop sidecar entry point."""

import atexit
import faulthandler
import os
import shutil
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

from api.desktop_port import (
    DEFAULT_HOST,
    desktop_port,
    reclaim_listen_port,
    remove_pid_file,
    write_current_pid,
)
from api.desktop_runtime import configure_runtime


def _open_log():
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    path = root / "CodeInsight-AI" / "daemon.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a", encoding="utf-8", buffering=1)


_LOG = _open_log()


def _log(message: str) -> None:
    print(
        f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')} {message}",
        file=_LOG,
        flush=True,
    )


def _configure_git() -> None:
    executable = os.environ.get("GIT_PYTHON_GIT_EXECUTABLE") or shutil.which("git")
    if not executable:
        candidates = [
            Path(os.environ.get("ProgramFiles", "")) / "Git" / "cmd" / "git.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Git" / "cmd" / "git.exe",
        ]
        executable = next((str(path) for path in candidates if path.is_file()), None)
    if executable:
        os.environ["GIT_PYTHON_GIT_EXECUTABLE"] = executable
        _log(f"using Git executable: {executable}")
    else:
        _log("Git executable not found; repository clone features will be unavailable")
    os.environ.setdefault("GIT_PYTHON_REFRESH", "quiet")


faulthandler.enable(file=_LOG)
faulthandler.dump_traceback_later(60, file=_LOG)
_log("daemon bootstrap started")
_log(
    "desktop engine version: "
    f"{os.environ.get('CODEINSIGHT_DESKTOP_VERSION', 'unknown')}"
)

# Frozen desktop builds must not initialize development reload/watch hooks.
os.environ.setdefault("NODE_ENV", "production")
_configure_git()
try:
    runtime = configure_runtime()
    tiktoken_cache = runtime.get("tiktoken_cache")
    if tiktoken_cache:
        _log(f"using bundled tiktoken cache: {tiktoken_cache}")
    ssl_bundle = runtime.get("ssl_bundle")
    if ssl_bundle:
        _log(f"using Windows CA bundle: {ssl_bundle}")
except BaseException:
    traceback.print_exc(file=_LOG)
    _log("offline runtime resource validation failed")
    raise

try:
    from api.desktop_settings import apply_desktop_settings

    desktop_settings = apply_desktop_settings()
    _log(
        "desktop model provider: "
        f"{desktop_settings.get('provider', 'openai')}"
    )
    import uvicorn

    _log("uvicorn imported")
    from api.main import app  # noqa: E402

    _log("API application imported")
except BaseException:
    traceback.print_exc(file=_LOG)
    _log("daemon import failed")
    raise
finally:
    faulthandler.cancel_dump_traceback_later()


def main() -> None:
    host = DEFAULT_HOST
    port = desktop_port()
    reclaim_listen_port(host, port, log=_log)
    write_current_pid()
    atexit.register(remove_pid_file)
    _log(f"starting API server on {host}:{port}")
    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        traceback.print_exc(file=_LOG)
        _log("daemon terminated with an exception")
        raise
