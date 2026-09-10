"""Ring-buffer + file operation log for desktop diagnosis."""

from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from api.logger import get_logger

logger = get_logger(__name__)

MAX_EVENTS = 400
_SECRET_KEYS = {
    "api_key",
    "openai_api_key",
    "google_api_key",
    "password",
    "token",
    "authorization",
    "secret",
    "credential",
}
_events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)


def operation_log_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    return root / "CodeInsight-AI" / "operation.log"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SECRET_KEYS or lowered.endswith("_key") or "password" in lowered:
                redacted[key] = "***"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value[:20]]
    if isinstance(value, str) and len(value) > 400:
        return value[:400] + "…"
    return value


def log_event(event: str, message: str, *, level: str = "info", **data: Any) -> None:
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "level": level,
        "event": event,
        "message": (message or "")[:500],
        "data": _redact(data),
    }
    _events.append(record)
    line = json.dumps(record, ensure_ascii=False)
    try:
        path = operation_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        logger.warning("Could not write operation log")
    log = logger.warning if level in {"warn", "warning", "error"} else logger.info
    log("oplog %s %s", event, record["message"])


def recent_events(limit: int = 200) -> list[dict[str, Any]]:
    items = list(_events)
    if limit > 0:
        items = items[-limit:]
    return items


def export_text(limit: int = 300) -> str:
    lines = []
    for record in recent_events(limit):
        extra = record.get("data") or {}
        suffix = f" {json.dumps(extra, ensure_ascii=False)}" if extra else ""
        lines.append(
            f"{record['ts']} [{record['level']}] {record['event']}: {record['message']}{suffix}"
        )
    return "\n".join(lines)


def clear_events_for_tests() -> None:
    _events.clear()
