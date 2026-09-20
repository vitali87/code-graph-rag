"""MCP index/update must honour the same ignore files the CLI honours (#1616).

`GraphUpdater`'s `exclude_paths`/`unignore_paths` both default to `None`, so a
call site that omits them parses everything `.cgrignore` and `.gitignore` were
meant to skip. The defect was an OMISSION at the two MCP call sites, which is
why these tests assert on the kwargs `GraphUpdater` is CALLED with rather than
on the resulting graph: a missing argument leaves the graph well-formed but
wrong, and an end-to-end assertion would pass against the broken code.

The CLI resolves the merged set with `load_ignore_patterns`, which unions
`.cgrignore` with the root `.gitignore` minus negations. MCP has no `--exclude`
flag and no interactive setup, so that loader alone is the whole contract here.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.config import load_ignore_patterns
from codebase_rag.mcp.tools import MCPToolsRegistry

_SYNC_METHODS = ("_index_repository_sync", "_update_repository_sync")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "backend"
    repo.mkdir()
    (repo / "app.py").write_text("def main(): pass\n", encoding="utf-8")
    (repo / ".cgrignore").write_text("vendor/\ngenerated/\n", encoding="utf-8")
    (repo / ".gitignore").write_text("dist/\nbuild/\n", encoding="utf-8")
    return repo


@pytest.fixture
def registry(repo: Path) -> MCPToolsRegistry:
    return MCPToolsRegistry(
        project_root=str(repo),
        ingestor=MagicMock(),
        cypher_gen=MagicMock(),
    )


@pytest.mark.parametrize("sync_method", _SYNC_METHODS)
def test_mcp_passes_the_cli_exclude_set(
    registry: MCPToolsRegistry, repo: Path, sync_method: str
) -> None:
    expected = load_ignore_patterns(repo).exclude
    # Guard the fixture: an empty expectation would make the assertion below
    # true of the broken code too, since `exclude_paths` would be None-vs-empty
    # rather than a real difference.
    assert {"vendor/", "generated/", "dist/", "build/"} <= expected

    with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
        getattr(registry, sync_method)()

    assert updater_cls.call_args.kwargs["exclude_paths"] == expected


@pytest.mark.parametrize("sync_method", _SYNC_METHODS)
def test_mcp_passes_the_cli_unignore_set(
    registry: MCPToolsRegistry, repo: Path, sync_method: str
) -> None:
    # A .gitignore exclude cancelled by a negation must reach the updater as an
    # unignore, not silently vanish: dropping it would re-exclude `generated/`.
    (repo / ".cgrignore").write_text("vendor/\n!generated/\n", encoding="utf-8")
    (repo / ".gitignore").write_text("generated/\ndist/\n", encoding="utf-8")

    patterns = load_ignore_patterns(repo)
    assert "generated/" in patterns.unignore
    assert "generated/" not in patterns.exclude

    with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
        getattr(registry, sync_method)()

    assert updater_cls.call_args.kwargs["unignore_paths"] == patterns.unignore


@pytest.mark.parametrize("sync_method", _SYNC_METHODS)
def test_mcp_passes_none_when_the_repo_has_no_ignore_files(
    tmp_path: Path, sync_method: str
) -> None:
    # The CLI's `... or None` idiom yields None for an empty set. Consumers
    # gate on truthiness, so this pins the CALL SHAPE rather than behaviour:
    # it keeps the two entry points passing identical arguments, so a future
    # consumer that does distinguish them cannot diverge by entry point.
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "app.py").write_text("def main(): pass\n", encoding="utf-8")
    registry = MCPToolsRegistry(
        project_root=str(bare), ingestor=MagicMock(), cypher_gen=MagicMock()
    )
    assert not load_ignore_patterns(bare).exclude

    with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
        getattr(registry, sync_method)()

    assert updater_cls.call_args.kwargs["exclude_paths"] is None
    assert updater_cls.call_args.kwargs["unignore_paths"] is None
