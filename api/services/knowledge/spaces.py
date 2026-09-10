"""Knowledge-space identity, detection, and persistence helpers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from typing import TYPE_CHECKING

from api.schemas.knowledge import KnowledgeCandidate, KnowledgeSpace
from api.services.local_roots import canonical_repository_root, register_selected_root

if TYPE_CHECKING:
    from api.services.wiki.store import WikiTaskStore

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    "coverage",
}


def normalize_workspace_root(value: str) -> str:
    return str(canonical_repository_root(value))


def compute_space_id(
    workspace_root: str, included_dirs: list[str] | None, language: str
) -> str:
    root = os.path.normpath(workspace_root).replace("\\", "/").rstrip("/").lower()
    scope = ",".join(
        sorted(item.replace("\\", "/").strip("/") for item in (included_dirs or []))
    )
    payload = f"{root}|{scope}|{(language or 'zh').lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def knowledge_label(workspace_root: str, included_dirs: list[str] | None) -> str:
    root_name = Path(workspace_root).name or workspace_root
    dirs = [item.replace("\\", "/").strip("/") for item in (included_dirs or []) if item]
    if not dirs:
        return root_name
    if len(dirs) == 1:
        return f"{root_name} / {dirs[0]}"
    return f"{root_name} / {', '.join(dirs)}"


def list_child_candidates(root: Path) -> list[KnowledgeCandidate]:
    if not root.is_dir():
        raise ValueError("目录不存在，请先完成同步或检查路径")
    candidates: list[KnowledgeCandidate] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name.lower()):
        if not child.is_dir() or child.name.startswith(".") or child.name in _SKIP_DIRS:
            continue
        if child.name in {"packages", "apps", "services"}:
            for package in sorted(child.iterdir(), key=lambda path: path.name.lower()):
                if (
                    not package.is_dir()
                    or package.name.startswith(".")
                    or package.name in _SKIP_DIRS
                ):
                    continue
                relative = f"{child.name}/{package.name}"
                kind = "git" if (package / ".git").exists() else "package"
                candidates.append(
                    KnowledgeCandidate(
                        path=relative,
                        kind=kind,
                        label=f"{root.name} / {relative}",
                    )
                )
            continue
        kind = "git" if (child / ".git").exists() else "dir"
        candidates.append(
            KnowledgeCandidate(
                path=child.name,
                kind=kind,
                label=f"{root.name} / {child.name}",
            )
        )
    return candidates


def detect_subrepos(workspace_root: str) -> list[KnowledgeCandidate]:
    return list_child_candidates(Path(normalize_workspace_root(workspace_root)))


def upsert_spaces(
    store: "WikiTaskStore",
    *,
    workspace_root: str,
    included_dirs: list[str] | None,
    excluded_dirs: list[str] | None,
    language: str,
    provider: str | None,
    model: str | None,
) -> list[KnowledgeSpace]:
    root = register_selected_root(workspace_root)
    dirs = [item.replace("\\", "/").strip("/") for item in (included_dirs or []) if item]
    groups = [dirs] if not dirs else [[item] for item in dirs]
    spaces: list[KnowledgeSpace] = []
    for group in groups:
        space = store.upsert_knowledge_space(
            workspace_root=root,
            included_dirs=group,
            excluded_dirs=excluded_dirs or [],
            language=language,
            provider=provider,
            model=model,
        )
        spaces.append(space)
    return spaces
