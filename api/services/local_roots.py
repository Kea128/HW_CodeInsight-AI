"""Allow-list local repository roots exposed to desktop file-tree APIs."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()


def _registry_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    return root / "CodeInsight-AI" / "selected-repository-roots.json"


def canonical_repository_root(value: str) -> Path:
    try:
        root = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("Local repository directory does not exist") from error
    if not root.is_dir():
        raise ValueError("Local repository root must be a directory")
    if root == Path(root.anchor):
        raise ValueError("A filesystem root cannot be registered as a repository")
    return root


def _read_selected_roots() -> set[str]:
    try:
        payload = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(payload, list):
        return set()
    return {str(value) for value in payload}


def _write_selected_roots(roots: set[str]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(sorted(roots), ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def register_selected_root(value: str) -> str:
    root = str(canonical_repository_root(value))
    with _LOCK:
        roots = _read_selected_roots()
        roots.add(root)
        _write_selected_roots(roots)
    return root


def unregister_selected_root(value: str) -> bool:
    root = str(Path(value).expanduser().resolve())
    with _LOCK:
        roots = _read_selected_roots()
        matching = next(
            (
                registered
                for registered in roots
                if os.path.normcase(registered) == os.path.normcase(root)
            ),
            None,
        )
        if matching is None:
            return False
        roots.remove(matching)
        _write_selected_roots(roots)
    return True


def registered_repository_roots(store: Any) -> set[str]:
    roots = _read_selected_roots()
    for project in store.list_continuous_projects():
        request = project.get("request") or {}
        if request.get("type") == "local" and request.get("repo_url"):
            roots.add(str(Path(request["repo_url"]).expanduser().resolve()))
    for project in store.list_remote_projects():
        if project.get("local_path"):
            roots.add(str(Path(project["local_path"]).expanduser().resolve()))
    return roots


def require_registered_root(value: str, store: Any) -> Path:
    root = canonical_repository_root(value)
    allowed = {
        os.path.normcase(registered)
        for registered in registered_repository_roots(store)
    }
    if os.path.normcase(str(root)) not in allowed:
        raise PermissionError(
            "Local repository is not registered; select it in the desktop app first"
        )
    return root
