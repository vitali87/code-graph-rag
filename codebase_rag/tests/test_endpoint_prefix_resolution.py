"""Router and blueprint mount prefixes must reach ENDPOINT templates (issue #877).

Dogfood finding: every real FastAPI/Flask app mounts its routes behind
``APIRouter(prefix=...)``, ``include_router(..., prefix=...)`` or a Flask
blueprint ``url_prefix``, so decorator-only templates were prefix-stripped
(``GET /{id}`` instead of ``GET /users/{id}``): unlinkable when all-parameter,
and wrong-path matches otherwise.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])
EXPOSES = cs.RelationshipType.EXPOSES.value


def _run(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    # Build the graph and return (handler_qn, endpoint_identity) for every
    # EXPOSES edge.
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], c.args[2][2].removeprefix("resource::ENDPOINT::"))
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == EXPOSES
    }


def _endpoint(edges: set[tuple[str, str]], handler: str, identity: str) -> bool:
    return any(h.endswith(handler) and e == identity for h, e in edges)


class TestFastApiRouterPrefixes:
    def test_router_own_prefix_reaches_template(self, tmp_path: Path) -> None:
        files = {
            "main.py": (
                "from fastapi import APIRouter, FastAPI\n\n"
                "app = FastAPI()\n"
                "router = APIRouter(prefix='/users')\n\n\n"
                "@router.get('/{user_id}')\n"
                "def get_user(user_id: int):\n"
                "    return {}\n\n\n"
                "app.include_router(router)\n"
            ),
        }
        edges = _run(tmp_path, files)
        assert _endpoint(edges, "main.get_user", "GET /users/{user_id}"), edges

    def test_include_router_prefix_prepends(self, tmp_path: Path) -> None:
        files = {
            "main.py": (
                "from fastapi import APIRouter, FastAPI\n\n"
                "app = FastAPI()\n"
                "v2 = APIRouter()\n\n\n"
                "@v2.get('/accounts/{account_id}')\n"
                "def get_account(account_id: int):\n"
                "    return {}\n\n\n"
                "app.include_router(v2, prefix='/api/v2')\n"
            ),
        }
        edges = _run(tmp_path, files)
        assert _endpoint(
            edges, "main.get_account", "GET /api/v2/accounts/{account_id}"
        ), edges

    def test_cross_module_include_with_prefix(self, tmp_path: Path) -> None:
        files = {
            "app/__init__.py": "",
            "app/api/__init__.py": "",
            "app/api/casts.py": (
                "from fastapi import APIRouter\n\n"
                "casts = APIRouter()\n\n\n"
                "@casts.get('/{cast_id}/')\n"
                "def get_cast(cast_id: int):\n"
                "    return {}\n"
            ),
            "app/main.py": (
                "from fastapi import FastAPI\n\n"
                "from app.api.casts import casts\n\n"
                "app = FastAPI()\n"
                "app.include_router(casts, prefix='/api/v1/casts')\n"
            ),
        }
        edges = _run(tmp_path, files)
        assert _endpoint(edges, "casts.get_cast", "GET /api/v1/casts/{cast_id}/"), edges

    def test_nested_router_chain_composes(self, tmp_path: Path) -> None:
        files = {
            "routes.py": (
                "from fastapi import APIRouter\n\n"
                "router = APIRouter(prefix='/users')\n\n\n"
                "@router.get('/{user_id}')\n"
                "def read_user(user_id: int):\n"
                "    return {}\n"
            ),
            "api.py": (
                "from fastapi import APIRouter\n\n"
                "import routes\n\n"
                "api_router = APIRouter()\n"
                "api_router.include_router(routes.router)\n"
            ),
            "main.py": (
                "from fastapi import FastAPI\n\n"
                "from api import api_router\n\n"
                "app = FastAPI()\n"
                "app.include_router(api_router, prefix='/api/v1')\n"
            ),
        }
        edges = _run(tmp_path, files)
        assert _endpoint(edges, "routes.read_user", "GET /api/v1/users/{user_id}"), (
            edges
        )

    def test_dynamic_include_prefix_marks_unknown_lead(self, tmp_path: Path) -> None:
        # settings.API_V1_STR cannot resolve statically; the known tail keeps
        # its own prefix behind an explicit unknown-lead marker instead of
        # masquerading as a complete path.
        files = {
            "main.py": (
                "from fastapi import APIRouter, FastAPI\n\n"
                "from settings import settings\n\n"
                "app = FastAPI()\n"
                "router = APIRouter(prefix='/users')\n\n\n"
                "@router.get('/{user_id}')\n"
                "def get_user(user_id: int):\n"
                "    return {}\n\n\n"
                "app.include_router(router, prefix=settings.API_V1_STR)\n"
            ),
            "settings.py": "class _S:\n    API_V1_STR = '/api/v1'\n\n\nsettings = _S()\n",
        }
        edges = _run(tmp_path, files)
        assert _endpoint(edges, "main.get_user", "GET /**/users/{user_id}"), edges

    def test_app_route_stays_bare(self, tmp_path: Path) -> None:
        files = {
            "main.py": (
                "from fastapi import FastAPI\n\n"
                "app = FastAPI()\n\n\n"
                "@app.get('/health')\n"
                "def health():\n"
                "    return {}\n"
            ),
        }
        edges = _run(tmp_path, files)
        assert _endpoint(edges, "main.health", "GET /health"), edges


class TestFlaskBlueprintPrefixes:
    def test_blueprint_url_prefix_reaches_template(self, tmp_path: Path) -> None:
        files = {
            "main.py": (
                "from flask import Blueprint, Flask\n\n"
                "app = Flask(__name__)\n"
                "bp = Blueprint('payments', __name__, url_prefix='/payments')\n\n\n"
                "@bp.route('/<int:payment_id>', methods=['GET'])\n"
                "def get_payment(payment_id: int):\n"
                "    return {}\n\n\n"
                "app.register_blueprint(bp)\n"
            ),
        }
        edges = _run(tmp_path, files)
        assert _endpoint(edges, "main.get_payment", "GET /payments/<int:payment_id>"), (
            edges
        )

    def test_register_url_prefix_overrides_own(self, tmp_path: Path) -> None:
        files = {
            "main.py": (
                "from flask import Blueprint, Flask\n\n"
                "app = Flask(__name__)\n"
                "bp = Blueprint('payments', __name__, url_prefix='/payments')\n\n\n"
                "@bp.route('/<int:payment_id>', methods=['GET'])\n"
                "def get_payment(payment_id: int):\n"
                "    return {}\n\n\n"
                "app.register_blueprint(bp, url_prefix='/pay')\n"
            ),
        }
        edges = _run(tmp_path, files)
        assert _endpoint(edges, "main.get_payment", "GET /pay/<int:payment_id>"), edges

    def test_app_factory_registration_resolves(self, tmp_path: Path) -> None:
        # register_blueprint inside a create_app() factory is the dominant
        # Flask layout; collection must not stop at module level.
        files = {
            "views.py": (
                "from flask import Blueprint\n\n"
                "bp = Blueprint('users', __name__, url_prefix='/users')\n\n\n"
                "@bp.route('/<int:user_id>')\n"
                "def get_user(user_id: int):\n"
                "    return {}\n"
            ),
            "main.py": (
                "from flask import Flask\n\n"
                "from views import bp\n\n\n"
                "def create_app():\n"
                "    app = Flask(__name__)\n"
                "    app.register_blueprint(bp)\n"
                "    return app\n"
            ),
        }
        edges = _run(tmp_path, files)
        assert _endpoint(edges, "views.get_user", "GET /users/<int:user_id>"), edges


class TestUnknownLeadMatching:
    @pytest.mark.parametrize(
        ("url", "template", "matches"),
        [
            ("http://svc/api/v1/users/42", "/**/users/{id}", True),
            ("http://svc/users/42", "/**/users/{id}", True),
            ("http://svc/api/v1/users/42/x", "/**/users/{id}", False),
            ("http://svc/api/v1/orders/42", "/**/users/{id}", False),
            ("http://svc/users", "/**/users/{id}", False),
        ],
    )
    def test_unknown_lead_templates_tail_match(
        self, url: str, template: str, matches: bool
    ) -> None:
        from codebase_rag.parsers.endpoints import url_matches_template

        assert url_matches_template(url, template) is matches

    def test_unknown_lead_all_parameter_template_has_no_evidence(self) -> None:
        from codebase_rag.parsers.endpoints import _has_literal_segment

        assert not _has_literal_segment("/**/{id}")
        assert _has_literal_segment("/**/users/{id}")


class TestLocalRouterNameCollisions:
    def test_factory_local_shadow_falls_back_to_bare_template(
        self, tmp_path: Path
    ) -> None:
        # A factory-local router sharing the module-level router's name must
        # not hijack its prefix; an ambiguous name yields the bare template
        # rather than a wrong one.
        files = {
            "main.py": (
                "from fastapi import APIRouter, FastAPI\n\n"
                "app = FastAPI()\n"
                "router = APIRouter(prefix='/users')\n\n\n"
                "@router.get('/{user_id}')\n"
                "def get_user(user_id: int):\n"
                "    return {}\n\n\n"
                "def make_admin():\n"
                "    router = APIRouter(prefix='/admin')\n"
                "    return router\n\n\n"
                "app.include_router(router)\n"
            ),
        }
        edges = _run(tmp_path, files)
        assert not _endpoint(edges, "main.get_user", "GET /admin/{user_id}"), edges
        assert _endpoint(edges, "main.get_user", "GET /{user_id}"), edges

    def test_identical_reassignment_keeps_resolution(self, tmp_path: Path) -> None:
        # Re-running the same assignment is not ambiguity.
        files = {
            "main.py": (
                "from fastapi import APIRouter, FastAPI\n\n"
                "app = FastAPI()\n"
                "router = APIRouter(prefix='/users')\n"
                "router = APIRouter(prefix='/users')\n\n\n"
                "@router.get('/{user_id}')\n"
                "def get_user(user_id: int):\n"
                "    return {}\n\n\n"
                "app.include_router(router)\n"
            ),
        }
        edges = _run(tmp_path, files)
        assert _endpoint(edges, "main.get_user", "GET /users/{user_id}"), edges


class TestIncrementalMountChanges:
    def test_empty_pending_rehydrates_handlers_from_graph(self, tmp_path: Path) -> None:
        # A mount-only change re-parses just the mounting module: the
        # unchanged handlers must come back from the graph and re-emit with
        # the new prefix, and their stale EXPOSES edges must be dropped.
        parsers, queries = load_parsers()
        if "python" not in parsers:
            pytest.skip("python parser not available")
        files = {
            "routes.py": (
                "from fastapi import APIRouter\n\n"
                "router = APIRouter(prefix='/users')\n\n\n"
                "@router.get('/{user_id}')\n"
                "def get_user(user_id: int):\n"
                "    return {}\n"
            ),
            "main.py": (
                "from fastapi import FastAPI\n\n"
                "import routes\n\n"
                "app = FastAPI()\n"
                "app.include_router(routes.router, prefix='/api')\n"
            ),
        }
        for rel, content in files.items():
            (tmp_path / rel).write_text(content, encoding="utf-8")

        project = tmp_path.name
        module_rows = [
            {"qualified_name": f"{project}.routes", "path": "routes.py"},
            {"qualified_name": f"{project}.main", "path": "main.py"},
        ]
        handler_rows = [
            {
                "labels": ["Function"],
                "qualified_name": f"{project}.routes.get_user",
                "decorators": ["@router.get('/{user_id}')"],
            }
        ]

        def fake_fetch(query: str, params: object = None) -> list[dict]:
            if "Module" in query:
                return module_rows
            if "decorators" in query:
                return handler_rows
            return []

        class _QueryableIngestor:
            # Concrete (not MagicMock) so isinstance(_, QueryProtocol) holds,
            # matching how the real MemgraphIngestor is detected at runtime.
            def __init__(self) -> None:
                self.ensure_node_batch = MagicMock()
                self.ensure_relationship_batch = MagicMock()
                self.flush_all = MagicMock()
                self.fetch_all = MagicMock(side_effect=fake_fetch)
                self.execute_write = MagicMock()

        mock = _QueryableIngestor()
        updater = GraphUpdater(
            ingestor=mock,
            repo_path=tmp_path,
            parsers=parsers,
            queries=queries,
            capture=_CAPTURE_IO,
        )
        updater._is_full_build = False
        assert not updater.factory.definition_processor.pending_endpoints

        updater._emit_pending_endpoints()

        exposes = {
            (c.args[0][2], c.args[2][2].split("::")[-1])
            for c in mock.ensure_relationship_batch.call_args_list
            if str(c.args[1]) == EXPOSES
        }
        assert (f"{project}.routes.get_user", "GET /api/users/{user_id}") in exposes, (
            exposes
        )
        deletes = [c.args[0] for c in mock.execute_write.call_args_list if c.args]
        assert any("EXPOSES" in q for q in deletes), deletes
