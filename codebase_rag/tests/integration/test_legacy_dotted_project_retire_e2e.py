# Real-Memgraph check of the retirement of a dotted checkout's pre-#2412
# project (review of PR 2497). The unit tests drive the eval double, which
# mirrors CYPHER_RETIRE_PROJECT in Python; only a real database proves the
# Cypher itself stops at the Folder nodes the old and new project share.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.utils.path_utils import derive_project_name

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


def _index(ingestor: MemgraphIngestor, root: Path, project: str | None) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
    ).run(force=True)


def _strings(ingestor: MemgraphIngestor, query: str) -> set[str]:
    rows = ingestor.fetch_all(query)
    return {value for row in rows if isinstance(value := row.get("v"), str)}


def _modules(ingestor: MemgraphIngestor) -> set[str]:
    return _strings(ingestor, "MATCH (m:Module) RETURN m.qualified_name AS v")


def _functions(ingestor: MemgraphIngestor) -> set[str]:
    return _strings(ingestor, "MATCH (f:Function) RETURN f.qualified_name AS v")


def _projects(ingestor: MemgraphIngestor) -> set[str]:
    return _strings(ingestor, "MATCH (p:Project) RETURN p.name AS v")


def test_retiring_the_old_project_keeps_the_new_ones_nodes(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    root = tmp_path / "acme.web"
    (root / "app").mkdir(parents=True)
    (root / "views.py").write_text("def render_page():\n    return 1\n")
    (root / "app" / "routes.py").write_text("def list_pages():\n    return []\n")
    _index(memgraph_ingestor, root, "acme.web")
    new = derive_project_name(root)

    _index(memgraph_ingestor, root, None)

    assert _projects(memgraph_ingestor) == {new}
    assert _modules(memgraph_ingestor) == {f"{new}.views", f"{new}.app.routes"}
    assert _functions(memgraph_ingestor) == {
        f"{new}.views.render_page",
        f"{new}.app.routes.list_pages",
    }
    folders = _strings(
        memgraph_ingestor,
        f"MATCH (:Project {{name: '{new}'}})-[:CONTAINS_FOLDER]->(f:Folder) "
        "RETURN f.absolute_path AS v",
    )
    assert folders == {(root / "app").resolve().as_posix()}


def test_a_project_named_under_the_old_one_keeps_their_shared_module(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    web = tmp_path / "acme.web"
    (web / "api").mkdir(parents=True)
    (web / "api" / "__init__.py").write_text("")
    (web / "api" / "views.py").write_text("def web_view():\n    return 1\n")
    api = tmp_path / "acme.web.api"
    api.mkdir()
    (api / "views.py").write_text("def api_view():\n    return 2\n")
    _index(memgraph_ingestor, web, "acme.web")
    _index(memgraph_ingestor, api, "acme.web.api")
    assert "acme.web.api.views.api_view" in _functions(memgraph_ingestor)

    _index(memgraph_ingestor, web, None)

    assert {"acme.web", "acme.web.api"} <= _projects(memgraph_ingestor)
    assert "acme.web.api.views" in _modules(memgraph_ingestor)
    assert "acme.web.api.views.api_view" in _functions(memgraph_ingestor)
