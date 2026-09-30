"""Issue #2461 against a real Memgraph: `cgr graph` refuses a project or a
qualified name the graph does not hold, and still answers `[]` for a name it
holds that nothing matches.

The unit tests answer the queries from a fake; this runs the real Cypher,
including the label-disjunction existence lookup and the suggestion pool.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.graph_cli import cli as graph_cli
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.utils.path_utils import derive_project_name

pytestmark = [pytest.mark.integration]

SHAPES = """import os


def total(values):
    return sum(values)


def average(values):
    return total(values) / len(values)


def unused(values):
    return os.fspath(values)
"""


@pytest.fixture
def indexed(
    memgraph_ingestor: MemgraphIngestor,
    memgraph_container: dict[str, str | int],
    tmp_path: Path,
) -> Iterator[tuple[Path, str]]:
    repo = tmp_path / "gq"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "shapes.py").write_text(SHAPES, encoding="utf-8")
    parsers, queries = load_parsers()
    project = derive_project_name(repo)
    GraphUpdater(
        ingestor=memgraph_ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
    ).run()
    memgraph_ingestor.flush_all()

    def connect(batch_size: int) -> MemgraphIngestor:
        return MemgraphIngestor(
            host=str(memgraph_container["host"]),
            port=int(memgraph_container["port"]),
            batch_size=batch_size,
        )

    with patch("codebase_rag.cli_runtime.connect_memgraph", side_effect=connect):
        yield repo, project


def _graph(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(graph_cli, list(args))
    return result.exit_code, result.stdout, result.stderr


def test_a_typo_in_the_project_hash_exits_3_with_the_close_match(
    indexed: tuple[Path, str],
) -> None:
    repo, project = indexed
    typo = project[:-2] + project[-1] + project[-2]

    code, out, err = _graph("callers", f"{project}.app.shapes.total", "--project", typo)

    assert code == cs.GRAPH_EXIT_UNKNOWN_PROJECT, err
    assert out == ""
    assert f"Did you mean: {project}" in err


def test_a_never_indexed_directory_exits_3(
    indexed: tuple[Path, str], tmp_path: Path
) -> None:
    elsewhere = tmp_path / "never_indexed"
    elsewhere.mkdir()

    code, out, err = _graph("resolve", "total", "--repo-path", str(elsewhere))

    assert code == cs.GRAPH_EXIT_UNKNOWN_PROJECT, err
    assert out == ""
    assert f"No project is indexed for {elsewhere.resolve()}" in err


def test_a_typo_in_the_name_exits_4_with_the_close_match(
    indexed: tuple[Path, str],
) -> None:
    repo, project = indexed

    code, out, err = _graph(
        "callers", f"{project}.app.shapes.tota", "--repo-path", str(repo)
    )

    assert code == cs.GRAPH_EXIT_UNKNOWN_TARGET, err
    assert out == ""
    assert f"Did you mean: {project}.app.shapes.total" in err


def test_the_real_answer_and_a_real_empty_answer_are_unchanged(
    indexed: tuple[Path, str],
) -> None:
    # Negative: rows for a called function, `[]` for one nothing calls, and
    # `[]` for a node outside the project's definitions (the `os` module).
    repo, project = indexed

    called = _graph("callers", f"{project}.app.shapes.total", "--repo-path", str(repo))
    uncalled = _graph(
        "callers", f"{project}.app.shapes.unused", "--repo-path", str(repo)
    )
    external = _graph("implementors", "os", "--repo-path", str(repo))

    assert called[0] == 0, called[2]
    assert [r["qualified_name"] for r in json.loads(called[1])] == [
        f"{project}.app.shapes.average"
    ]
    assert uncalled[0] == 0, uncalled[2]
    assert json.loads(uncalled[1]) == []
    assert external[0] == 0, external[2]
    assert json.loads(external[1]) == []
