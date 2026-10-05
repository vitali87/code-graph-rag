"""`cgr edits undo` must leave the graph describing the files it restored (#2515).

`cgr rename` re-ingests the files it rewrites, so the graph follows the new
name. The undo reverted the files but not the graph: the renamed definition
stayed, the restored one was missing, and a follow-up rename of the restored
name was refused with "No definition named ... in the graph" until a manual
`cgr start --update-graph`. These drive the real CLI against a real Memgraph.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import Result
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.utils.path_utils import derive_project_name

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_CORE = "def compute_total(items):\n    return sum(items)\n"
_API = (
    "from pkg.core import compute_total\n\n\n"
    "def endpoint():\n    return compute_total([1, 2])\n"
)


@pytest.fixture
def indexed(
    tmp_path: Path,
    memgraph_ingestor: MemgraphIngestor,
    memgraph_container: dict[str, str | int],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str]:
    # The CLI connects through `settings`, so point it at the test container.
    monkeypatch.setattr(settings, "MEMGRAPH_HOST", str(memgraph_container["host"]))
    monkeypatch.setattr(settings, "MEMGRAPH_PORT", int(memgraph_container["port"]))
    monkeypatch.setattr(settings, "MEMGRAPH_USERNAME", None)
    monkeypatch.setattr(settings, "MEMGRAPH_PASSWORD", None)
    root = tmp_path / "undodemo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "core.py").write_text(_CORE)
    (root / "pkg" / "api.py").write_text(_API)
    # The name `cgr rename` and `cgr edits undo` derive when no --project is
    # passed, which is how the issue ran them.
    name = derive_project_name(root)
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=memgraph_ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=name,
        skip_embeddings=True,
    ).run()
    return root, name


def _cgr(*args: str) -> Result:
    return CliRunner().invoke(app, list(args))


def _functions(ingestor: MemgraphIngestor, name: str) -> set[str]:
    rows = ingestor.fetch_all(
        "MATCH (f:Function) WHERE f.qualified_name STARTS WITH $prefix "
        "RETURN f.qualified_name AS qn",
        {"prefix": f"{name}."},
    )
    return {str(row["qn"]) for row in rows}


def _callees(ingestor: MemgraphIngestor, caller: str) -> set[str]:
    rows = ingestor.fetch_all(
        "MATCH (:Function {qualified_name: $qn})-[:CALLS]->(f) "
        "RETURN f.qualified_name AS qn",
        {"qn": caller},
    )
    return {str(row["qn"]) for row in rows}


def _rename(root: Path, qn: str, new_name: str) -> Result:
    return _cgr("rename", qn, new_name, "--repo-path", str(root))


def test_undo_puts_the_graph_back_on_the_restored_names(
    indexed: tuple[Path, str], memgraph_ingestor: MemgraphIngestor
) -> None:
    root, name = indexed
    old_qn, new_qn = f"{name}.pkg.core.compute_total", f"{name}.pkg.core.sum_items"
    renamed = _rename(root, old_qn, "sum_items")
    assert renamed.exit_code == 0, renamed.output
    assert new_qn in _functions(memgraph_ingestor, name)

    undo = _cgr("edits", "undo", "--repo-path", str(root))

    assert undo.exit_code == 0, undo.output
    assert (root / "pkg" / "core.py").read_text() == _CORE
    assert (root / "pkg" / "api.py").read_text() == _API
    functions = _functions(memgraph_ingestor, name)
    assert old_qn in functions
    assert new_qn not in functions
    # The caller in the other restored file points at the restored name again.
    assert _callees(memgraph_ingestor, f"{name}.pkg.api.endpoint") == {old_qn}


def test_the_restored_name_can_be_renamed_again_right_after_the_undo(
    indexed: tuple[Path, str], memgraph_ingestor: MemgraphIngestor
) -> None:
    # The agent loop from the issue: rename, undo on a failed check, retry.
    root, name = indexed
    assert _rename(root, f"{name}.pkg.core.compute_total", "sum_items").exit_code == 0
    assert _cgr("edits", "undo", "--repo-path", str(root)).exit_code == 0

    retry = _rename(root, f"{name}.pkg.core.compute_total", "total")

    assert retry.exit_code == 0, retry.output
    assert "def total(items):" in (root / "pkg" / "core.py").read_text()
    assert f"{name}.pkg.core.total" in _functions(memgraph_ingestor, name)


def test_a_refused_undo_leaves_files_and_graph_on_the_renamed_state(
    indexed: tuple[Path, str], memgraph_ingestor: MemgraphIngestor
) -> None:
    # Negative: an undo that refuses restores nothing, so it must re-ingest
    # nothing either; the graph keeps describing the (renamed) tree.
    root, name = indexed
    assert _rename(root, f"{name}.pkg.core.compute_total", "sum_items").exit_code == 0
    core = root / "pkg" / "core.py"
    hand_edited = core.read_text() + "\n# hand edit\n"
    core.write_text(hand_edited)

    undo = _cgr("edits", "undo", "--repo-path", str(root))

    assert undo.exit_code == 1
    assert core.read_text() == hand_edited
    functions = _functions(memgraph_ingestor, name)
    assert f"{name}.pkg.core.sum_items" in functions
    assert f"{name}.pkg.core.compute_total" not in functions


def test_undo_leaves_other_projects_in_the_graph_alone(
    indexed: tuple[Path, str], memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Negative: the re-ingest is scoped to the restored files of this project.
    root, name = indexed
    other = tmp_path / "bystander"
    other.mkdir()
    (other / "tool.py").write_text("def compute_total(items):\n    return 0\n")
    parsers, queries = load_parsers()
    other_name = derive_project_name(other)
    GraphUpdater(
        ingestor=memgraph_ingestor,
        repo_path=other,
        parsers=parsers,
        queries=queries,
        project_name=other_name,
        skip_embeddings=True,
    ).run()
    before = _functions(memgraph_ingestor, other_name)
    assert _rename(root, f"{name}.pkg.core.compute_total", "sum_items").exit_code == 0

    assert _cgr("edits", "undo", "--repo-path", str(root)).exit_code == 0

    assert _functions(memgraph_ingestor, other_name) == before
