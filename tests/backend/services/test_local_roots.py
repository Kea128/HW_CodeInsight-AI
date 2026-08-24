from pathlib import Path

import pytest

from api.services.local_roots import (
    canonical_repository_root,
    register_selected_root,
    require_registered_root,
)


class FakeStore:
    def __init__(self, continuous=None, remote=None):
        self.continuous = continuous or []
        self.remote = remote or []

    def list_continuous_projects(self):
        return self.continuous

    def list_remote_projects(self):
        return self.remote


def test_structure_root_must_match_registered_root_exactly(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    repository = tmp_path / "repository"
    child = repository / "src"
    sibling = tmp_path / "other"
    child.mkdir(parents=True)
    sibling.mkdir()
    store = FakeStore(
        continuous=[
            {"request": {"type": "local", "repo_url": str(repository)}}
        ]
    )

    assert require_registered_root(str(repository), store) == repository.resolve()
    with pytest.raises(PermissionError, match="not registered"):
        require_registered_root(str(child), store)
    with pytest.raises(PermissionError, match="not registered"):
        require_registered_root(str(sibling), store)


def test_explicit_desktop_selection_persists_safe_exact_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    repository = tmp_path / "selected"
    repository.mkdir()

    registered = register_selected_root(str(repository))

    assert registered == str(repository.resolve())
    assert require_registered_root(str(repository), FakeStore()) == repository.resolve()


def test_filesystem_root_cannot_be_selected():
    anchor = Path(Path.cwd().anchor)
    with pytest.raises(ValueError, match="filesystem root"):
        canonical_repository_root(str(anchor))
