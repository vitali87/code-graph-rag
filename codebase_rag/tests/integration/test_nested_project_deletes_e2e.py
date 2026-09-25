# Real-Memgraph check of issue #1985: with projects `svc` and `svc.v2` in one
# graph, an incremental run of `svc` deleted `svc.v2`'s modules, because both
# the module delete and the orphan prune were ruled by the `svc.` prefix. The
# unit tests drive the eval double, which mirrors the query in Python; only a
# real database proves the Cypher itself parses and filters.
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

_API = "def helper():\n    return 1\n"


def _index(ingestor: MemgraphIngestor, root: Path, project: str, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
    ).run(force=force)


def _module_qns(ingestor: MemgraphIngestor) -> set[str]:
    rows = ingestor.fetch_all("MATCH (m:Module) RETURN m.qualified_name AS qn")
    return {qn for r in rows if isinstance(qn := r.get("qn"), str)}


def test_an_incremental_run_keeps_the_longer_named_projects_modules(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    for project in ("svc", "svc.v2"):
        root = tmp_path / project
        root.mkdir()
        (root / "api.py").write_text(_API, encoding="utf-8")
    (tmp_path / "svc.v2" / "extra.py").write_text(_API, encoding="utf-8")
    _index(memgraph_ingestor, tmp_path / "svc", "svc", force=True)
    _index(memgraph_ingestor, tmp_path / "svc.v2", "svc.v2", force=True)
    before = _module_qns(memgraph_ingestor)
    assert {"svc.api", "svc.v2.api", "svc.v2.extra"} <= before, before

    (tmp_path / "svc" / "api.py").write_text(_API + "\n\ndef added():\n    return 2\n")
    _index(memgraph_ingestor, tmp_path / "svc", "svc", force=False)

    assert _module_qns(memgraph_ingestor) == before


def test_the_module_delete_query_excludes_nested_projects(
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    for qn in ("svc.api", "svc.v2.api", "svc.v2", "other.api"):
        memgraph_ingestor.execute_write(
            "CREATE (:Module {qualified_name: $qn, path: 'api.py'})", {"qn": qn}
        )

    memgraph_ingestor.execute_write(
        cs.CYPHER_DELETE_MODULE,
        {
            cs.KEY_PATH: "api.py",
            cs.KEY_PROJECT_NAME: "svc",
            cs.KEY_PROJECT_PREFIX: "svc.",
            cs.KEY_NESTED_PROJECTS: ["svc.v2"],
        },
    )

    assert _module_qns(memgraph_ingestor) == {"svc.v2.api", "svc.v2", "other.api"}
