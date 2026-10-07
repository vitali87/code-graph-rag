# Real-Memgraph check of issue #2918: the inbound-edge capture of an
# incremental sync starts from this project's modules through the
# Module(path) index and returns no other project's same-path edges. The
# unit tests drive the eval double, which mirrors the query in Python; only
# a real database proves the Cypher parses, filters and plans as intended.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

FILES = {
    "util.py": "def helper():\n    return 1\n",
    "app.py": "from util import helper\n\n\ndef run():\n    return helper()\n",
}


def _updater(ingestor: MemgraphIngestor, root: Path, project: str) -> GraphUpdater:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
    )


@pytest.fixture
def alpha(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> GraphUpdater:
    memgraph_ingestor.ensure_constraints()
    for project in ("beta", "alpha"):
        _updater(memgraph_ingestor, tmp_path / project, project).run(force=True)
    memgraph_ingestor.flush_all()
    return _updater(memgraph_ingestor, tmp_path / "alpha", "alpha")


def test_the_capture_is_this_projects_edges_only(alpha: GraphUpdater) -> None:
    captured = {
        (r[cs.KEY_CALLER_QN], r[cs.KEY_REL], r[cs.KEY_TARGET_QN])
        for r in alpha._capture_inbound_edges(["util.py"])
    }
    assert ("alpha.app.run", "CALLS", "alpha.util.helper") in captured
    assert not [row for row in captured if not str(row[2]).startswith("alpha.")]


def test_the_capture_and_the_delete_start_from_the_module_path_index(
    alpha: GraphUpdater, memgraph_ingestor: MemgraphIngestor
) -> None:
    params = {
        cs.CYPHER_PARAM_PATHS: ["util.py"],
        cs.KEY_PATH: "util.py",
        cs.KEY_PROJECT_NAME: "alpha",
        cs.KEY_PROJECT_PREFIX: "alpha.",
        cs.KEY_NESTED_PROJECTS: [],
    }
    for query in (cs.CYPHER_INBOUND_EDGES, cs.CYPHER_DELETE_MODULE):
        plan = " ".join(
            str(next(iter(row.values()), ""))
            for row in memgraph_ingestor.fetch_all(
                cs.CYPHER_EXPLAIN_PREFIX + query, params
            )
        )
        assert "ScanAllByLabelProperties (m :Module {path})" in plan, plan


@pytest.fixture
def alpha_beside_nested(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> GraphUpdater:
    # `alpha.nested` is a project of its own whose modules sit under
    # `alpha.`, at the same relative paths as alpha's.
    memgraph_ingestor.ensure_constraints()
    for project in ("alpha.nested", "alpha"):
        _updater(memgraph_ingestor, tmp_path / project, project).run(force=True)
    memgraph_ingestor.flush_all()
    return _updater(memgraph_ingestor, tmp_path / "alpha", "alpha")


def test_a_nested_projects_edges_are_not_captured(
    alpha_beside_nested: GraphUpdater,
) -> None:
    captured = {
        (r[cs.KEY_CALLER_QN], r[cs.KEY_REL], r[cs.KEY_TARGET_QN])
        for r in alpha_beside_nested._capture_inbound_edges(["util.py"])
    }
    assert ("alpha.app.run", "CALLS", "alpha.util.helper") in captured
    assert not [row for row in captured if str(row[2]).startswith("alpha.nested.")]
