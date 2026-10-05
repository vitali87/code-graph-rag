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


def _qns_of(ingestor: MemgraphIngestor, project: str) -> set[str]:
    # The bare name (a root Package and Module) and everything under it.
    every = _strings(
        ingestor,
        "MATCH (n) WHERE n.qualified_name IS NOT NULL RETURN n.qualified_name AS v",
    )
    return {qn for qn in every if qn == project or qn.startswith(f"{project}.")}


def _with_root_package(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text("def root_helper():\n    return 0\n")
    (root / "views.py").write_text("def render_page():\n    return 1\n")
    return root


def test_retiring_the_old_project_takes_its_root_package_and_module(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Greptile on PR 2497: the root Package and Module of a checkout with an
    # `__init__.py` are named `acme.web` exactly, not `acme.web.`-prefixed.
    root = _with_root_package(tmp_path / "acme.web")
    _index(memgraph_ingestor, root, "acme.web")
    assert {"acme.web", "acme.web.root_helper"} <= _qns_of(
        memgraph_ingestor, "acme.web"
    )
    new = derive_project_name(root)

    _index(memgraph_ingestor, root, None)

    assert _projects(memgraph_ingestor) == {new}
    assert _qns_of(memgraph_ingestor, "acme.web") == set()
    assert {new, f"{new}.root_helper"} <= _qns_of(memgraph_ingestor, new)


def test_retiring_the_old_project_keeps_one_whose_name_only_starts_with_it(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Negative: `acme.webapp`'s names start with `acme.web` but are not that
    # name or under `acme.web.`. Its checkout sits inside the old one's, so
    # the retirement walk reaches its modules through the Folders they share.
    # It is indexed first: indexing it after drops the outer project's Folder
    # at its root, which would cut the walk off before it got there.
    root = _with_root_package(tmp_path / "acme.web")
    nested = root / "plugins"
    (nested / "app").mkdir(parents=True)
    (nested / "app" / "hooks.py").write_text("def on_load():\n    return 1\n")
    _index(memgraph_ingestor, nested, "acme.webapp")
    _index(memgraph_ingestor, root, "acme.web")
    kept = _qns_of(memgraph_ingestor, "acme.webapp")
    assert "acme.webapp.app.hooks.on_load" in kept

    _index(memgraph_ingestor, root, None)

    assert "acme.web" not in _projects(memgraph_ingestor)
    assert _qns_of(memgraph_ingestor, "acme.web") == set()
    assert _qns_of(memgraph_ingestor, "acme.webapp") == kept
