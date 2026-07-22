"""Call-registered routes must become ENDPOINT resources (issue #886).

JS and Go frameworks register routes through calls rather than decorators
(`app.get('/path', handler)`, `http.HandleFunc("/path", h)`, `e.GET(...)`),
so cross-language linking was one-directional: clients in any language could
resolve into Python servers, but nothing could resolve into JS or Go ones.
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


def _run(
    tmp_path: Path, files: dict[str, str], language: str
) -> set[tuple[str, str, str]]:
    # Build the graph and return (source_label, source_qn, endpoint_identity)
    # for every EXPOSES edge.
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
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
        (str(c.args[0][0]), c.args[0][2], c.args[2][2].split("::")[-1])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == EXPOSES
    }


def _endpoint(
    edges: set[tuple[str, str, str]], source_suffix: str, identity: str
) -> bool:
    return any(qn.endswith(source_suffix) and e == identity for _label, qn, e in edges)


class TestExpressRoutes:
    def test_verb_calls_with_identifier_handlers(self, tmp_path: Path) -> None:
        files = {
            "server.js": (
                "const express = require('express')\n"
                "const app = express()\n\n"
                "function getProduct(req, res) { res.json({}) }\n"
                "function createOrder(req, res) { res.json({}) }\n\n"
                "app.get('/gateway/products/:id', getProduct)\n"
                "app.post('/gateway/orders', createOrder)\n"
                "app.listen(3000)\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert _endpoint(edges, "server.getProduct", "GET /gateway/products/:id"), edges
        assert _endpoint(edges, "server.createOrder", "POST /gateway/orders"), edges

    def test_route_chain_registers_each_verb(self, tmp_path: Path) -> None:
        files = {
            "routes.js": (
                "const express = require('express')\n"
                "const app = express()\n\n"
                "function list(req, res) { res.json([]) }\n"
                "function create(req, res) { res.json({}) }\n\n"
                "app.route('/todos').get(list).post(create)\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert _endpoint(edges, "routes.list", "GET /todos"), edges
        assert _endpoint(edges, "routes.create", "POST /todos"), edges

    def test_anonymous_handler_anchors_to_module(self, tmp_path: Path) -> None:
        files = {
            "server.js": (
                "const express = require('express')\n"
                "const app = express()\n\n"
                "app.get('/health', (req, res) => res.json({ok: true}))\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        anchors = {(label, qn) for label, qn, e in edges if e == "GET /health"}
        assert anchors, edges
        # The endpoint stays anchored even without a resolvable handler.
        assert any(qn.endswith("server") for _label, qn in anchors), edges

    def test_client_verb_calls_are_ignored(self, tmp_path: Path) -> None:
        # An HTTP client's `.get('/path')` is an OUTBOUND call, not a route
        # registration: no framework-bound receiver and no handler evidence.
        files = {
            "client.js": (
                "const apiClient = require('./api')\n"
                "const request = require('superagent')\n"
                "const options = {timeout: 5}\n\n"
                "apiClient.get('/users', options)\n"
                "request.get('/users')\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert not edges, edges

    def test_static_template_literal_path_registers(self, tmp_path: Path) -> None:
        # A backtick literal with no substitutions is literal route evidence.
        files = {
            "server.js": (
                "const express = require('express')\n"
                "const app = express()\n\n"
                "app.get(`/health`, (req, res) => res.json({ok: true}))\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert _endpoint(edges, "server", "GET /health"), edges

    def test_non_routes_are_ignored(self, tmp_path: Path) -> None:
        files = {
            "server.js": (
                "const express = require('express')\n"
                "const app = express()\n"
                "const cache = new Map()\n\n"
                "function mw(req, res, next) { next() }\n\n"
                "app.use(mw)\n"
                "function peek(userID) { return cache.get(userID) }\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert not edges, edges


class TestGoRoutes:
    def test_handlefunc_registers_method_agnostic_endpoint(
        self, tmp_path: Path
    ) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import (\n\t"fmt"\n\t"net/http"\n)\n\n'
                "func handleInventory(w http.ResponseWriter, r *http.Request) {\n"
                '\tfmt.Fprint(w, "[]")\n'
                "}\n\n"
                "func main() {\n"
                '\thttp.HandleFunc("/inventory", handleInventory)\n'
                '\thttp.ListenAndServe(":9000", nil)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert _endpoint(edges, "main.handleInventory", "ANY /inventory"), edges

    def test_go122_verb_pattern_sets_the_method(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import "net/http"\n\n'
                "func getProduct(w http.ResponseWriter, r *http.Request) {}\n\n"
                "func main() {\n"
                '\thttp.HandleFunc("GET /products/{id}", getProduct)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert _endpoint(edges, "main.getProduct", "GET /products/{id}"), edges

    def test_echo_style_verb_methods(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import "github.com/labstack/echo/v4"\n\n'
                "func main() {\n"
                "\te := echo.New()\n"
                '\te.GET("/version", func(c echo.Context) error { return nil })\n'
                '\te.POST("/login", getLoginHandler())\n'
                '\te.Start(":8000")\n'
                "}\n\n"
                "func getLoginHandler() echo.HandlerFunc { return nil }\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        # Anonymous and wrapped handlers anchor to the registering function.
        assert _endpoint(edges, "main.main", "GET /version"), edges
        assert _endpoint(edges, "main.main", "POST /login"), edges

    def test_gin_style_param_route(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import "github.com/gin-gonic/gin"\n\n'
                "func getUser(c *gin.Context) {}\n\n"
                "func main() {\n"
                "\tr := gin.Default()\n"
                '\tr.GET("/users/:id", getUser)\n'
                "\tr.Run()\n"
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert _endpoint(edges, "main.getUser", "GET /users/:id"), edges

    def test_client_selector_calls_are_ignored(self, tmp_path: Path) -> None:
        # A REST client's `client.GET("/users")` and a plain registry's
        # `registry.Handle("/key", value)` are not route registrations.
        files = {
            "main.go": (
                "package main\n\n"
                "func main() {\n"
                "\tclient := NewClient()\n"
                '\tclient.GET("/users")\n'
                '\tregistry.Handle("/key", value)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert not edges, edges

    def test_raw_string_route_registers(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import "net/http"\n\n'
                "func handleHealth(w http.ResponseWriter, r *http.Request) {}\n\n"
                "func main() {\n"
                "\thttp.HandleFunc(`/health`, handleHealth)\n"
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert _endpoint(edges, "main.handleHealth", "ANY /health"), edges

    def test_chi_pascalcase_verbs(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import "github.com/go-chi/chi/v5"\n\n'
                "func listArticles(w http.ResponseWriter, r *http.Request) {}\n\n"
                "func main() {\n"
                "\tr := chi.NewRouter()\n"
                '\tr.Get("/articles", listArticles)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert _endpoint(edges, "main.listArticles", "GET /articles"), edges

    def test_non_verb_calls_are_ignored(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import "strings"\n\n'
                "func main() {\n"
                '\t_ = strings.Split("/a/b", "/")\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert not edges, edges


class TestRouteTemplateMatching:
    @pytest.mark.parametrize(
        ("url", "template", "matches"),
        [
            ("http://svc:3000/gateway/products/7", "/gateway/products/:id", True),
            ("http://svc:3000/gateway/products", "/gateway/products/:id", False),
            ("http://svc:9000/users/42", "/users/:id", True),
        ],
    )
    def test_colon_params_match_segments(
        self, url: str, template: str, matches: bool
    ) -> None:
        from codebase_rag.parsers.endpoints import url_matches_template

        assert url_matches_template(url, template) is matches

    def test_colon_param_is_not_literal_evidence(self) -> None:
        from codebase_rag.parsers.endpoints import _has_literal_segment

        assert not _has_literal_segment("/:id")
        assert _has_literal_segment("/users/:id")

    def test_any_method_is_direction_compatible(self) -> None:
        from unittest.mock import MagicMock as MM

        from codebase_rag.parsers.endpoints import link_endpoints

        ingestor = MM()
        ingestor.fetch_all.return_value = [
            {
                "qualified_name": "resource::NETWORK::http://svc:9000/inventory/reserve",
                "name": "http://svc:9000/inventory/reserve",
                "kind": "NETWORK",
                "directions": ["WRITES_TO"],
            },
            {
                "qualified_name": "resource::ENDPOINT::inv__1a2::ANY /inventory/reserve",
                "name": "ANY /inventory/reserve",
                "kind": "ENDPOINT",
                "project": "inv__1a2",
            },
        ]
        assert link_endpoints(ingestor) == 1


class TestStaleRouteCleanup:
    def test_module_without_routes_still_drops_stale_exposes(
        self, tmp_path: Path
    ) -> None:
        # An incremental edit that removes a module's LAST route must still
        # clean that module's old EXPOSES edges: cleanup is keyed on every
        # scanned route-language module, not on the new registrations.
        parsers, queries = load_parsers()
        (tmp_path / "server.js").write_text(
            "const express = require('express')\nconst app = express()\n",
            encoding="utf-8",
        )

        class _QueryableIngestor:
            # Concrete (not MagicMock) so isinstance(_, QueryProtocol) holds.
            def __init__(self) -> None:
                self.ensure_node_batch = MagicMock()
                self.ensure_relationship_batch = MagicMock()
                self.flush_all = MagicMock()
                self.fetch_all = MagicMock(return_value=[])
                self.execute_write = MagicMock()

        mock = _QueryableIngestor()
        GraphUpdater(
            ingestor=mock,
            repo_path=tmp_path,
            parsers=parsers,
            queries=queries,
            capture=_CAPTURE_IO,
        ).run()
        module_qn = f"{tmp_path.name}.server"
        cleanups = [
            c
            for c in mock.execute_write.call_args_list
            if c.args and "EXPOSES" in c.args[0] and len(c.args) > 1
        ]
        assert any(
            module_qn in (c.args[1].get("module_qns") or []) for c in cleanups
        ), cleanups
