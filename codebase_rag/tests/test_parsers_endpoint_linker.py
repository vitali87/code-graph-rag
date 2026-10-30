"""`EndpointLinker`: the endpoints a project's decorated and call-registered
routes expose once every module is parsed, the module a handler belongs to,
and the stale-EXPOSES cleanup it owes when that cleanup cannot run."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.parsers.endpoint_linker import (
    EndpointLinker,
    _owning_module_qn,
    _package_modules,
    _route_handler_entry,
)
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from codebase_rag.types_defs import PropertyParams
from evals.recorded_graph import RecordingIngestor

_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])
_PROJECT = "svc"

_FILES = {
    # The router's handlers take the prefix their mount in another module
    # gives them, which only a pass after every module is parsed can see.
    "api/routes.py": (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n\n"
        "@router.get('/items')\n"
        "def list_items():\n    return []\n"
    ),
    "api/main.py": (
        "from fastapi import FastAPI\n"
        "from api.routes import router\n"
        "app = FastAPI()\n"
        "app.include_router(router, prefix='/api')\n\n"
        "@app.post('/orders')\n"
        "def create_order():\n    return 1\n"
    ),
    "tests/test_api.py": (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n\n"
        "@app.get('/probe')\n"
        "def probe():\n    return 1\n"
    ),
    "server.js": (
        "const express = require('express')\n"
        "const app = express()\n"
        "function getProduct(req, res) { res.json({}) }\n"
        "app.get('/products/:id', getProduct)\n"
    ),
}


def _exposes(temp_repo: Path, mock_ingestor: RecordingIngestor) -> set[tuple[str, str]]:
    repo = temp_repo / _PROJECT
    for rel, text in _FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    updater = create_and_run_updater(repo, mock_ingestor, capture=_CAPTURE_IO)
    assert isinstance(updater.endpoint_linker, EndpointLinker)
    return {
        (str(rel.source), str(rel.target).rsplit("::", 1)[-1])
        for rel in get_relationships(mock_ingestor, cs.RelationshipType.EXPOSES)
    }


def test_routes_expose_their_endpoints_under_their_mount_prefix(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    exposes = _exposes(temp_repo, mock_ingestor)
    assert (f"{_PROJECT}.api.routes.list_items", "GET /api/items") in exposes
    assert (f"{_PROJECT}.api.main.create_order", "POST /orders") in exposes
    # A call-registered route anchors at its named handler.
    assert (f"{_PROJECT}.server.getProduct", "GET /products/:id") in exposes


def test_a_route_inside_a_test_module_exposes_nothing(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    exposes = _exposes(temp_repo, mock_ingestor)
    assert not any(source.endswith(".probe") for source, _ in exposes)
    assert len(exposes) == 3


def test_the_owning_module_is_the_longest_enclosing_one() -> None:
    modules = {"svc.api", "svc.api.routes", "svc.apis"}
    assert _owning_module_qn("svc.api.routes.list_items", modules) == "svc.api.routes"
    assert _owning_module_qn("svc.api.helper", modules) == "svc.api"
    # A module is not its own owner, and a shared spelling is not a prefix.
    assert _owning_module_qn("svc.api", {"svc.api"}) is None
    assert _owning_module_qn("svc.apix.f", {"svc.api"}) is None


def test_a_handler_row_needs_a_route_decorator() -> None:
    modules = {"svc.api"}
    row = {
        cs.KEY_QUALIFIED_NAME: "svc.api.Items.get",
        cs.KEY_DECORATORS: ["@staticmethod", "@router.get('/items')"],
        "labels": [cs.NodeLabel.METHOD.value],
    }
    assert _route_handler_entry(row, set(), modules) == (
        cs.NodeLabel.METHOD,
        "svc.api.Items.get",
        ["@router.get('/items')"],
        "svc.api",
    )
    plain = {**row, cs.KEY_DECORATORS: ["@staticmethod"]}
    assert _route_handler_entry(plain, set(), modules) is None
    # A handler this run already queued is not rehydrated a second time.
    assert _route_handler_entry(row, {"svc.api.Items.get"}, modules) is None
    assert (
        _route_handler_entry({**row, cs.KEY_DECORATORS: None}, set(), modules) is None
    )


def test_a_handler_row_without_a_method_label_is_a_function() -> None:
    row = {
        cs.KEY_QUALIFIED_NAME: "svc.api.handler",
        cs.KEY_DECORATORS: ["@app.get('/x')"],
    }
    entry = _route_handler_entry(row, set(), set())
    assert entry is not None
    assert entry[0] == cs.NodeLabel.FUNCTION
    assert entry[3] is None


def test_only_package_inits_are_package_modules() -> None:
    files = {
        "svc.api": Path("api/__init__.py"),
        "svc.api.routes": Path("api/routes.py"),
    }
    assert _package_modules(files) == frozenset({"svc.api"})


@dataclass
class _Run:
    is_full_build: bool = False
    registry_unread: bool = False
    exposes_cleanup_skipped: bool = False


@dataclass
class _Graph:
    fails: bool = False
    writes: list[str] = field(default_factory=list)

    def execute_write(self, query: str, params: PropertyParams | None = None) -> None:
        if self.fails:
            raise RuntimeError("graph down")
        self.writes.append(query)


def _linker(
    temp_repo: Path, mock_ingestor: RecordingIngestor, graph: _Graph
) -> EndpointLinker:
    updater = create_and_run_updater(temp_repo, mock_ingestor, capture=_CAPTURE_IO)
    live = updater.endpoint_linker
    return EndpointLinker(
        mock_ingestor,
        graph,  # the cleanup reads only the graph's write
        mock_ingestor,
        _CAPTURE_IO,
        updater.factory,
        updater.ast_cache,
        temp_repo,
        _PROJECT,
        live._owns,
    )


def test_an_unread_registry_skips_the_cleanup_and_owes_it(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    graph = _Graph()
    run = _Run(registry_unread=True)
    _linker(temp_repo, mock_ingestor, graph)._drop_stale_module_exposes(run, ["svc.a"])
    assert graph.writes == []
    assert run.exposes_cleanup_skipped


def test_a_failed_cleanup_is_owed_like_a_skipped_one(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    run = _Run()
    _linker(temp_repo, mock_ingestor, _Graph(fails=True))._drop_stale_module_exposes(
        run, ["svc.a"]
    )
    assert run.exposes_cleanup_skipped


def test_a_healthy_cleanup_runs_and_owes_nothing(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    graph = _Graph()
    run = _Run()
    _linker(temp_repo, mock_ingestor, graph)._drop_stale_handler_exposes(
        run, ["svc.api.list_items"]
    )
    assert len(graph.writes) == 1
    assert not run.exposes_cleanup_skipped
