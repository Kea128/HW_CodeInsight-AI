"""Frozen desktop sidecar entry point."""

import faulthandler
import os
import tempfile
import traceback
from datetime import datetime
from pathlib import Path


def _open_log():
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    path = root / "CodeInsight-AI" / "daemon.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a", encoding="utf-8", buffering=1)


_LOG = _open_log()


def _log(message: str) -> None:
    print(f"{datetime.now().isoformat()} {message}", file=_LOG, flush=True)


faulthandler.enable(file=_LOG)
faulthandler.dump_traceback_later(60, file=_LOG)
_log("daemon bootstrap started")

# Frozen desktop builds must not initialize development reload/watch hooks.
os.environ.setdefault("NODE_ENV", "production")

try:
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
    _log("starting API server on 127.0.0.1:8001")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8001")),
        reload=False,
        access_log=False,
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        traceback.print_exc(file=_LOG)
        _log("daemon terminated with an exception")
        raise
