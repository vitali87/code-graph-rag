from pathlib import Path

import pytest

from codebase_rag.utils.path_utils import derive_project_name, resolve_repo_path


def test_derive_project_name_is_stable(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    first = derive_project_name(repo)
    second = derive_project_name(repo)
    assert first == second


def test_derive_project_name_includes_basename(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    name = derive_project_name(repo)
    assert name.startswith("myrepo__")
    assert len(name.split("__")[1]) == 8


def test_derive_project_name_disambiguates_same_basename(tmp_path: Path) -> None:
    repo_a = tmp_path / "a" / "frontend"
    repo_b = tmp_path / "b" / "frontend"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    assert derive_project_name(repo_a) != derive_project_name(repo_b)
    assert derive_project_name(repo_a).startswith("frontend__")
    assert derive_project_name(repo_b).startswith("frontend__")


def test_derive_project_name_slugifies_special_chars(tmp_path: Path) -> None:
    weird = tmp_path / "my repo (v2)!"
    weird.mkdir()
    name = derive_project_name(weird)
    base = name.split("__")[0]
    assert all(c.isalnum() or c in "_-" for c in base)


def test_derive_project_name_fallback_for_root() -> None:
    name = derive_project_name(Path("/"))
    assert name.startswith("repo__")


def test_resolve_repo_path_explicit_wins(tmp_path: Path) -> None:
    repo = tmp_path / "explicit"
    repo.mkdir()
    resolved = resolve_repo_path(str(repo), "/some/other/path")
    assert resolved == repo.resolve()


def test_resolve_repo_path_uses_target_default(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    resolved = resolve_repo_path(None, str(repo))
    assert resolved == repo.resolve()


def test_resolve_repo_path_dot_falls_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = resolve_repo_path(None, ".")
    assert resolved == tmp_path.resolve()


def test_resolve_repo_path_empty_falls_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = resolve_repo_path(None, "")
    assert resolved == tmp_path.resolve()
