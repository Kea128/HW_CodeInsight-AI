from pathlib import Path

from api.schemas.repo import WikiTaskRequest
from api.services.knowledge.spaces import (
    compute_space_id,
    detect_subrepos,
    knowledge_label,
    list_child_candidates,
    upsert_spaces,
)
from api.services.wiki.store import WikiTaskStore  # noqa: E402


def test_space_id_changes_with_included_dirs(tmp_path):
    root = str(tmp_path)
    whole = compute_space_id(root, [], "zh")
    api = compute_space_id(root, ["packages/api"], "zh")
    assert whole != api
    assert len(whole) == 16


def test_detect_subrepos_lists_packages_and_git(tmp_path: Path):
    (tmp_path / "packages" / "api").mkdir(parents=True)
    (tmp_path / "apps" / "web").mkdir(parents=True)
    git_repo = tmp_path / "worker"
    git_repo.mkdir()
    (git_repo / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()

    candidates = detect_subrepos(str(tmp_path))
    paths = {item.path for item in candidates}
    assert "packages/api" in paths
    assert "apps/web" in paths
    assert "worker" in paths
    assert "node_modules" not in paths
    assert {item.path for item in list_child_candidates(tmp_path)} == paths


def test_upsert_spaces_creates_one_space_per_subdir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("CODEINSIGHT_DB_PATH", str(tmp_path / "db.sqlite"))
    (tmp_path / "repo").mkdir()
    store = WikiTaskStore(str(tmp_path / "db.sqlite"))
    spaces = upsert_spaces(
        store,
        workspace_root=str(tmp_path / "repo"),
        included_dirs=["api", "web"],
        excluded_dirs=[],
        language="zh",
        provider="openai_compatible",
        model="deepseek-chat",
    )
    assert len(spaces) == 2
    assert spaces[0].label.endswith("/ api")
    listed = store.list_knowledge_spaces()
    assert {item.space_id for item in listed} == {item.space_id for item in spaces}


def test_wiki_task_repo_key_uses_space_id():
    request = WikiTaskRequest(
        repo_url="D:/repo",
        type="local",
        owner="local",
        repo="repo",
        space_id="abc123",
    )
    assert request.repo_key == "space_abc123"
    assert knowledge_label("D:/Work/repo", ["pkg"]) == "repo / pkg"
