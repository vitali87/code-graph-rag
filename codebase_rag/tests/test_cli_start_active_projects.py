from __future__ import annotations

from codebase_rag.cli import _start_active_projects
from codebase_rag.workspaces.models import WorkspaceConfig, WorkspaceRepo

RESOLVED = "resolved"
WORKSPACE = "ws"


def _workspace(*names: str) -> WorkspaceConfig:
    return WorkspaceConfig(
        name=WORKSPACE,
        repos=[WorkspaceRepo(path=f"/tmp/{n}", project_name=n) for n in names],
    )


def test_empty_workspace_with_projects_does_not_crash() -> None:
    assert _start_active_projects(_workspace(), "alpha,beta", RESOLVED) == [
        "alpha",
        "beta",
    ]


def test_empty_workspace_blank_projects_falls_back_to_resolved() -> None:
    assert _start_active_projects(_workspace(), " , ", RESOLVED) == [RESOLVED]


def test_empty_workspace_without_projects_is_empty() -> None:
    assert _start_active_projects(_workspace(), None, RESOLVED) == []


def test_workspace_blank_projects_falls_back_to_first_repo() -> None:
    assert _start_active_projects(_workspace("one", "two"), ",", RESOLVED) == ["one"]


def test_workspace_without_projects_returns_all_repos() -> None:
    assert _start_active_projects(_workspace("one", "two"), None, RESOLVED) == [
        "one",
        "two",
    ]


def test_no_workspace_uses_resolved_project() -> None:
    assert _start_active_projects(None, None, RESOLVED) == [RESOLVED]
