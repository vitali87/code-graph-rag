# Issue #2801: an MCP `rename` that writes nothing leaves no incomplete-run
# marker. The rename hydrated its re-ingest updater before deciding whether it
# would re-ingest at all, and hydrating writes a `writing=true` marker. A dry
# run or a refused rename never re-ingested, so nothing cleared it, and every
# MCP graph tool then refused the project as "incomplete" for good.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.utils.path_utils import derive_project_name

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

CORE = (
    "def helper(x):\n    return x + 1\n\n\ndef compute(x):\n    return helper(x) * 2\n"
)
# `lonely` is called without being imported: a name-only, heuristic site.
UTIL = "def lonely():\n    return 1\n"
APP = "def run():\n    return lonely()\n"
INCOMPLETE = "failed part way"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def no_vector_store() -> Iterator[None]:
    with patch("codebase_rag.mcp.tools.delete_project_embeddings"):
        yield


@pytest.fixture
async def indexed(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> tuple[MCPToolsRegistry, str]:
    repo = tmp_path / "bricked"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text(CORE, encoding="utf-8")
    (repo / "pkg" / "util.py").write_text(UTIL, encoding="utf-8")
    (repo / "pkg" / "app.py").write_text(APP, encoding="utf-8")
    await _server(repo, memgraph_ingestor).index_repository()
    # A fresh session, as an agent's next MCP connection is: the indexing
    # registry keeps a warm updater, which hid the bug.
    return _server(repo, memgraph_ingestor), derive_project_name(repo)


def _server(repo: Path, ingestor: MemgraphIngestor) -> MCPToolsRegistry:
    return MCPToolsRegistry(
        project_root=str(repo), ingestor=ingestor, cypher_gen=MagicMock()
    )


def _markers(server: MCPToolsRegistry, project: str) -> list[bool]:
    rows = server.ingestor.fetch_all(
        "MATCH (m:IncompleteRun {project: $p}) RETURN m.writing AS writing",
        {"p": project},
    )
    return [bool(row["writing"]) for row in rows]


def _error(result: object) -> str:
    assert isinstance(result, dict), result
    return str(result.get(cs.DICT_KEY_ERROR, ""))


async def test_a_rename_preview_leaves_no_marker(
    indexed: tuple[MCPToolsRegistry, str],
) -> None:
    server, project = indexed

    result = await server.rename(f"{project}.pkg.core.helper", "helper2", dry_run=True)

    assert not _error(result), result
    assert _markers(server, project) == []


async def test_graph_tools_still_answer_after_a_rename_preview(
    indexed: tuple[MCPToolsRegistry, str],
) -> None:
    server, project = indexed
    await server.rename(f"{project}.pkg.core.helper", "helper2", dry_run=True)

    callers = await server.callers(f"{project}.pkg.core.helper")

    assert isinstance(callers, list), callers
    assert [row["qualified_name"] for row in callers] == [f"{project}.pkg.core.compute"]


@pytest.mark.parametrize(
    ("target", "refusal"),
    [
        ("pkg.core.helpr", "No definition named"),
        ("pkg.util.lonely", "resolved heuristically"),
    ],
)
async def test_a_refused_rename_leaves_no_marker(
    indexed: tuple[MCPToolsRegistry, str], target: str, refusal: str
) -> None:
    server, project = indexed

    result = await server.rename(f"{project}.{target}", "renamed")

    assert refusal in _error(result)
    assert _markers(server, project) == []


async def test_a_second_preview_still_plans_after_a_refusal(
    indexed: tuple[MCPToolsRegistry, str],
) -> None:
    server, project = indexed
    await server.rename(f"{project}.pkg.util.lonely", "renamed")

    result = await server.rename(f"{project}.pkg.core.helper", "helper2", dry_run=True)

    assert INCOMPLETE not in _error(result)
    assert isinstance(result, dict)
    assert result["sites"], result


async def test_a_preview_whose_hydration_purged_legacy_structure_keeps_the_marker(
    indexed: tuple[MCPToolsRegistry, str],
) -> None:
    # A keyless File is legacy structure the constraint migration purges
    # when the cold updater hydrates; the graph then lacks nodes until a
    # rebuild, so the preview must not clear the marker that says so
    # (Greptile, PR #2900).
    server, project = indexed
    server.ingestor.execute_write("CREATE (:File {path: 'legacy.py'})", None)

    await server.rename(f"{project}.pkg.core.helper", "helper2", dry_run=True)

    assert _markers(server, project) != []


async def test_a_preview_whose_marker_clear_fails_reports_it(
    indexed: tuple[MCPToolsRegistry, str],
) -> None:
    # The finally block discarded `_require_marker_cleared`'s error, so the
    # preview read as a success over a project left marked (CodeRabbit, PR
    # #2900).
    server, project = indexed
    persist = server._persist_incomplete

    def failing_clear(name: str, incomplete: bool, *, writing: bool = True) -> bool:
        return False if not incomplete else persist(name, incomplete, writing=writing)

    with patch.object(server, "_persist_incomplete", failing_clear):
        result = await server.rename(
            f"{project}.pkg.core.helper", "helper2", dry_run=True
        )

    stuck = cs.MCP_INCOMPLETE_MARKER_STUCK.format(project=project)
    assert _error(result) == stuck


# Negative: what must not change.


async def test_an_applied_rename_still_rewrites_and_clears_its_marker(
    indexed: tuple[MCPToolsRegistry, str],
) -> None:
    server, project = indexed

    result = await server.rename(f"{project}.pkg.core.helper", "helper2")

    assert isinstance(result, dict)
    assert result["applied"] is True, result
    core = (Path(server.project_root) / "pkg" / "core.py").read_text()
    assert "return helper2(x) * 2" in core
    assert _markers(server, project) == []


async def test_a_rename_over_a_partial_graph_is_still_refused(
    indexed: tuple[MCPToolsRegistry, str],
) -> None:
    server, project = indexed
    # Another run stopped part way through writing this project's graph.
    server.ingestor.execute_write(
        "CREATE (:IncompleteRun {project: $p, run_id: 'other', "
        "run_incomplete: true, writing: true})",
        {"p": project},
    )

    result = await server.rename(f"{project}.pkg.core.helper", "helper2", dry_run=True)

    assert INCOMPLETE in _error(result)
    assert (Path(server.project_root) / "pkg" / "core.py").read_text() == CORE
