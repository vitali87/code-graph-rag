# The route, Python-module and package readers select by `STARTS WITH
# $project_prefix`; with projects `svc` and `svc.v2` in one graph, `svc.`
# selects `svc.v2`'s rows too. Each reader keeps only the rows this project
# owns by its longest registered name (issues #2126 and #1991). Every test
# pairs the foreign row with a known positive from `svc`, so an empty result
# cannot pass.
from pathlib import Path

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_ROUTES = (
    "from flask import Flask\n\napp = Flask(__name__)\n\n\n"
    '@app.get("/items")\ndef items():\n    return 1\n'
)
# Route MODULES are the call-style languages (JS/TS, Go), not Python.
_JS_ROUTES = (
    'const express = require("express");\nconst app = express();\n'
    'app.get("/r", (req, res) => res.send("ok"));\n'
)


def _updater(root: Path, store: _StatefulIngestor, project: str) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
    )


def _svc_after_both_indexed(tmp_path: Path) -> GraphUpdater:
    store = _StatefulIngestor()
    for project in ("svc", "svc.v2"):
        root = tmp_path / project
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (root / "api.py").write_text(_ROUTES, encoding="utf-8")
        (root / "routes.js").write_text(_JS_ROUTES, encoding="utf-8")
        if project == "svc.v2":
            # Held only by the longer-named project: nothing but a
            # prefix-scoped read can put these in `svc`'s results.
            (root / "v2only").mkdir()
            (root / "v2only" / "__init__.py").write_text("", encoding="utf-8")
            (root / "extra.py").write_text(
                '@app.get("/extra")\ndef extra():\n    return 2\n', encoding="utf-8"
            )
        _updater(root, store, project).run(force=True)
    return _updater(tmp_path / "svc", store, "svc")


def _foreign(qns: set[str]) -> set[str]:
    return {qn for qn in qns if qn == "svc.v2" or qn.startswith("svc.v2.")}


def test_package_paths_skip_a_package_only_the_longer_project_holds(
    tmp_path: Path,
) -> None:
    paths = _svc_after_both_indexed(tmp_path)._package_paths()
    assert paths is not None
    assert "pkg" in paths
    assert "v2only" not in paths


def test_python_modules_are_this_projects_own(tmp_path: Path) -> None:
    qns = {
        qn for qn, _path in _svc_after_both_indexed(tmp_path)._graph_python_modules()
    }
    assert "svc.api" in qns
    assert _foreign(qns) == set()


def test_route_modules_are_this_projects_own(tmp_path: Path) -> None:
    """The EXPOSES cleanup is keyed on these module qns (#1991)."""
    updater = _svc_after_both_indexed(tmp_path)
    qns = {qn for qn, _path in updater._graph_route_module_paths()}
    assert "svc.routes" in qns
    assert _foreign(qns) == set()


def test_route_handlers_are_this_projects_own(tmp_path: Path) -> None:
    updater = _svc_after_both_indexed(tmp_path)
    qns = {
        qn
        for _label, qn, _routes, _module in updater._rehydrated_route_handlers(
            set(), set()
        )
    }
    assert "svc.api.items" in qns
    assert _foreign(qns) == set()


class _RegistryDown(_StatefulIngestor):
    def fetch_all(self, query, params=None):  # type: ignore[override]
        from codebase_rag import cypher_queries as cq

        if query == cq.CYPHER_LIST_PROJECTS:
            raise RuntimeError("registry unreadable")
        return super().fetch_all(query, params)


def test_route_modules_read_nothing_when_the_registry_is_unread(
    tmp_path: Path,
) -> None:
    """Route modules feed the EXPOSES cleanup, a delete. With the project
    registry unreadable, ownership falls back to the prefix rule and would
    pass `svc.v2`'s modules as `svc`'s, so the reader returns nothing, as it
    does when its own read fails (CodeRabbit, PR #2129)."""
    updater = _svc_after_both_indexed(tmp_path)
    healthy = {qn for qn, _path in updater._graph_route_module_paths()}
    assert "svc.routes" in healthy

    down = _RegistryDown()
    down.__dict__.update(updater.ingestor.__dict__)
    blind = _updater(tmp_path / "svc", down, "svc")
    assert blind._graph_route_module_paths() == []
