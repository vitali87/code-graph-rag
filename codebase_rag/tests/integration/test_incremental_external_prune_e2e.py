# (H) End-to-end (real Memgraph) verification that an incremental rebuild prunes
# (H) external import-target Module nodes that are no longer imported by anyone,
# (H) e.g. an imported name renamed on a subsequent index.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


def _index(ingestor: MemgraphIngestor, project_path: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _external_module_qns(ingestor: MemgraphIngestor) -> set[str]:
    rows = ingestor.fetch_all("MATCH (m:ExternalModule) RETURN m.qualified_name AS qn")
    return {r["qn"] for r in rows if r.get("qn")}


def test_incremental_rebuild_prunes_orphaned_external_module(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "__init__.py").touch()
    client = project / "client.py"

    client.write_text("from extlib import old_thing\n\nuse = old_thing\n")
    _index(memgraph_ingestor, project, force=True)

    before = _external_module_qns(memgraph_ingestor)
    assert any(qn.endswith(".old_thing") for qn in before), before

    client.write_text("from extlib import new_thing\n\nuse = new_thing\n")
    _index(memgraph_ingestor, project, force=False)

    after = _external_module_qns(memgraph_ingestor)
    assert not any(qn.endswith(".old_thing") for qn in after), after
    assert any(qn.endswith(".new_thing") for qn in after), after
