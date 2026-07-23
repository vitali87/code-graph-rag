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


class TestGoGeneratedRoutes:
    # Issue #909: oapi-codegen emits `router.Get(BaseURL+"/x", wrapper.H)`;
    # the path is a module-const concatenation and the handler an attribute.

    _GEN_SOURCE = (
        "package main\n\n"
        'import "github.com/go-chi/chi/v5"\n\n'
        'const BaseURL = "/api/v1"\n\n'
        "type ServerInterfaceWrapper struct {\n"
        "\tHandler ServerInterface\n"
        "}\n\n"
        "func (siw *ServerInterfaceWrapper) GetMe(w ResponseWriter, r *Request) {}\n\n"
        "func (siw *ServerInterfaceWrapper) PostLogout(w ResponseWriter, r *Request) {}\n\n"
        "func HandlerFromMux(si ServerInterface, router chi.Router) {\n"
        "\twrapper := ServerInterfaceWrapper{Handler: si}\n"
        '\trouter.Get(BaseURL+"/me", wrapper.GetMe)\n'
        '\trouter.Post(BaseURL+"/logout", wrapper.PostLogout)\n'
        "}\n"
    )

    def test_const_concat_with_attribute_handler_registers(
        self, tmp_path: Path
    ) -> None:
        edges = _run(tmp_path, {"gen.go": self._GEN_SOURCE}, "go")
        assert _endpoint(edges, "gen.HandlerFromMux", "GET /api/v1/me"), edges
        assert _endpoint(edges, "gen.HandlerFromMux", "POST /api/v1/logout"), edges

    def test_const_block_resolves_too(self, tmp_path: Path) -> None:
        files = {
            "routes.go": (
                "package main\n\n"
                "const (\n"
                '\tprefix = "/internal"\n'
                ")\n\n"
                "type healthWrapper struct{}\n\n"
                "func (h healthWrapper) Health(w ResponseWriter, r *Request) {}\n\n"
                "func mount(router Router) {\n"
                "\twrapper := healthWrapper{}\n"
                '\trouter.Get(prefix+"/health", wrapper.Health)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert _endpoint(edges, "routes.mount", "GET /internal/health"), edges

    _FIBER_SOURCE = (
        "package main\n\n"
        'import "github.com/gofiber/fiber/v2"\n\n'
        "type FiberServerOptions struct {\n\tBaseURL string\n}\n\n"
        "type ServerInterfaceWrapper struct {\n\tHandler ServerInterface\n}\n\n"
        "func (siw *ServerInterfaceWrapper) CreateThing(c *fiber.Ctx) error "
        "{ return nil }\n\n"
        "func (siw *ServerInterfaceWrapper) GetThing(c *fiber.Ctx) error "
        "{ return nil }\n\n"
        "func RegisterHandlersWithOptions(router fiber.Router, si ServerInterface, "
        "options FiberServerOptions) {\n"
        "\twrapper := ServerInterfaceWrapper{Handler: si}\n"
        '\trouter.Post(options.BaseURL+"/v2/things", wrapper.CreateThing)\n'
        '\trouter.Get(options.BaseURL+"/v2/things/:id", wrapper.GetThing)\n'
        "}\n"
    )

    def test_unresolvable_prefix_keeps_the_literal_suffix(self, tmp_path: Path) -> None:
        # The codegen mount prefix is a STRUCT FIELD, not a module const, so
        # nothing can resolve it; the literal suffix is still the route, and
        # prefix-tolerant linking already handles an unknown mount (#911).
        edges = _run(tmp_path, {"gen.go": self._FIBER_SOURCE}, "go")
        assert _endpoint(edges, "gen.RegisterHandlersWithOptions", "POST /v2/things"), (
            edges
        )
        assert _endpoint(
            edges, "gen.RegisterHandlersWithOptions", "GET /v2/things/:id"
        ), edges

    def test_unresolvable_prefix_without_handler_is_ignored(
        self, tmp_path: Path
    ) -> None:
        # The same shape on the CLIENT side registers nothing: no handler
        # argument, no wrapper binding, so no server evidence.
        files = {
            "client.go": (
                "package main\n\n"
                "type Options struct {\n\tBaseURL string\n}\n\n"
                "func fetchThing(c HTTPClient, options Options) {\n"
                '\tc.Get(options.BaseURL + "/v2/things")\n'
                "}\n"
            ),
        }
        assert not _run(tmp_path, files, "go")

    def test_unresolvable_prefix_with_relative_suffix_is_ignored(
        self, tmp_path: Path
    ) -> None:
        # A suffix that is not rooted is not a route path on its own.
        files = {
            "gen2.go": (
                "package main\n\n"
                "type Opts struct {\n\tBaseURL string\n}\n\n"
                "type wrap struct{}\n\n"
                "func (w wrap) Thing(c Ctx) error { return nil }\n\n"
                "func mount(router Router, options Opts) {\n"
                "\twrapper := wrap{}\n"
                '\trouter.Get(options.BaseURL+"things", wrapper.Thing)\n'
                "}\n"
            ),
        }
        assert not _run(tmp_path, files, "go")

    def test_client_const_concat_without_handler_is_ignored(
        self, tmp_path: Path
    ) -> None:
        files = {
            "client.go": (
                "package main\n\n"
                'const baseURL = "/api/v1"\n\n'
                "func fetchMe(c HTTPClient) {\n"
                '\tc.Get(baseURL + "/me")\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert not edges, edges

    def test_function_local_strings_do_not_resolve(self, tmp_path: Path) -> None:
        # A flat name map would let one function's local leak into another
        # and mint a WRONG template; only module-level consts resolve.
        files = {
            "routes.go": (
                "package main\n\n"
                "func other() {\n"
                '\tprefix := "/wrong"\n'
                "\t_ = prefix\n"
                "}\n\n"
                "func mount(router Router) {\n"
                '\trouter.Get(prefix+"/health", wrapper.Health)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert not edges, edges

    def test_plain_literal_with_selector_arg_is_ignored(self, tmp_path: Path) -> None:
        # An outbound client call can pass a selector too
        # (`client.Get("/users", opts.Header)`); selector-handler evidence
        # only counts on a const-derived path, the generated shape.
        files = {
            "client.go": (
                "package main\n\n"
                "func fetchUsers(client HTTPClient, opts Options) {\n"
                '\tclient.Get("/users", opts.Header)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert not edges, edges

    def test_concat_path_with_option_selector_is_ignored(self, tmp_path: Path) -> None:
        # `client.Get(baseURL + "/me", opts.Header)` concatenates a const
        # AND passes a selector, but `opts` is a parameter, not a wrapper
        # bound to a module-declared type: no registration.
        files = {
            "client.go": (
                "package main\n\n"
                'const baseURL = "/api/v1"\n\n'
                "func fetchMe(client HTTPClient, opts Options) {\n"
                '\tclient.Get(baseURL + "/me", opts.Header)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert not edges, edges

    def test_bare_const_path_with_selector_arg_is_ignored(self, tmp_path: Path) -> None:
        # A client can hold its URL in a module const too; only a
        # concatenation is the generated registration shape.
        files = {
            "client.go": (
                "package main\n\n"
                'const baseURL = "/users"\n\n'
                "func fetchUsers(client HTTPClient, opts Options) {\n"
                "\tclient.Get(baseURL, opts.Header)\n"
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert not edges, edges

    def test_same_function_options_struct_is_ignored(self, tmp_path: Path) -> None:
        # `opts := Options{}` right next to the call still is not wrapper
        # evidence: `Header` is a field access, not a method declared on
        # `Options` in this module.
        files = {
            "client.go": (
                "package main\n\n"
                'const baseURL = "/api/v1"\n\n'
                "type Options struct{}\n\n"
                "func fetchMe(client HTTPClient) {\n"
                "\topts := Options{}\n"
                '\tclient.Get(baseURL + "/me", opts.Header)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert not edges, edges

    def test_non_handler_method_on_options_struct_is_ignored(
        self, tmp_path: Path
    ) -> None:
        # Even a genuine method on the bound type is not wrapper evidence
        # unless its signature is a handler's; `Header(timeout int)` is an
        # outbound request option, not generated wiring.
        files = {
            "client.go": (
                "package main\n\n"
                'const baseURL = "/api/v1"\n\n'
                "type Options struct{}\n\n"
                'func (o Options) Header(timeout int) string { return "" }\n\n'
                "func fetchMe(client HTTPClient) {\n"
                "\topts := Options{}\n"
                '\tclient.Get(baseURL + "/me", opts.Header)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert not edges, edges

    def test_context_style_wrapper_method_registers(self, tmp_path: Path) -> None:
        # Echo-style codegen hands the wrapper a single Context parameter.
        files = {
            "routes.go": (
                "package main\n\n"
                'const base = "/api"\n\n'
                "type ServerInterfaceWrapper struct{}\n\n"
                "func (w *ServerInterfaceWrapper) GetMe(ctx echo.Context) error"
                " { return nil }\n\n"
                "func register(router Router) {\n"
                "\twrapper := ServerInterfaceWrapper{}\n"
                '\trouter.Get(base+"/me", wrapper.GetMe)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert _endpoint(edges, "routes.register", "GET /api/me"), edges

    def test_wrapper_binding_in_another_function_is_ignored(
        self, tmp_path: Path
    ) -> None:
        # A composite-literal binding legitimises selector handlers only
        # inside its own function; one in `setup` must not turn a client
        # call in `fetchMe` into a registration.
        files = {
            "client.go": (
                "package main\n\n"
                'const baseURL = "/api/v1"\n\n'
                "type Options struct{}\n\n"
                "func setup() {\n"
                "\topts := Options{}\n"
                "\t_ = opts\n"
                "}\n\n"
                "func fetchMe(client HTTPClient, opts Options) {\n"
                '\tclient.Get(baseURL + "/me", opts.Header)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert not edges, edges

    def test_module_level_wrapper_binding_registers(self, tmp_path: Path) -> None:
        # A wrapper bound at FILE scope is visible in every function.
        files = {
            "routes.go": (
                "package main\n\n"
                'const prefix = "/internal"\n\n'
                "type healthWrapper struct{}\n\n"
                "func (h healthWrapper) Health(w ResponseWriter, r *Request) {}\n\n"
                "var wrapper = healthWrapper{}\n\n"
                "func mount(router Router) {\n"
                '\trouter.Get(prefix+"/health", wrapper.Health)\n'
                "}\n"
            ),
        }
        edges = _run(tmp_path, files, "go")
        assert _endpoint(edges, "routes.mount", "GET /internal/health"), edges


class TestOptionsObjectRoutes:
    # Issue #907: one call, one object literal carrying method/path/handler.

    def test_endpoint_options_object_with_inline_handler(self, tmp_path: Path) -> None:
        files = {
            "gateway.ts": (
                "const app = createApp()\n\n"
                "app.endpoint({\n"
                '  method: "GET",\n'
                '  route: "/stems/:stemId/artifacts",\n'
                "  handler: async (ctx) => { return ctx.json([]) },\n"
                "})\n"
            ),
        }
        edges = _run(tmp_path, files, "typescript")
        assert _endpoint(edges, "gateway", "GET /stems/:stemId/artifacts"), edges

    def test_endpoint_options_object_with_method_shorthand_handler(
        self, tmp_path: Path
    ) -> None:
        # Issue #920: `async handler(ctx) {}` is a method_definition child of
        # the object literal, not a pair, and is inline handler evidence.
        files = {
            "gateway.ts": (
                "const app = createApp()\n\n"
                "app.endpoint({\n"
                '  method: "GET",\n'
                '  route: "/users",\n'
                "  async handler(ctx) {\n"
                "    return ctx.json([])\n"
                "  },\n"
                "})\n"
            ),
        }
        edges = _run(tmp_path, files, "typescript")
        assert _endpoint(edges, "gateway", "GET /users"), edges

    def test_client_method_shorthand_without_route_member_is_ignored(
        self, tmp_path: Path
    ) -> None:
        # The member-name gate still applies: `request({...})` with a method
        # shorthand is not a registration.
        files = {
            "client.ts": (
                "const client = getClient()\n\n"
                "client.request({\n"
                '  method: "GET",\n'
                '  url: "/users",\n'
                "  async transform(res) {\n"
                "    return res\n"
                "  },\n"
                "})\n"
            ),
        }
        edges = _run(tmp_path, files, "typescript")
        assert not edges, edges

    def test_fastify_route_with_shorthand_handler(self, tmp_path: Path) -> None:
        files = {
            "server.js": (
                "const fastify = require('fastify')()\n\n"
                "function handler(req, reply) { reply.send({}) }\n\n"
                "fastify.route({ method: 'GET', url: '/users/:id', handler })\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert _endpoint(edges, "server.handler", "GET /users/:id"), edges

    def test_hapi_route_with_declared_handler(self, tmp_path: Path) -> None:
        files = {
            "server.js": (
                "function createOrder(request, h) { return h.response({}) }\n\n"
                "server.route({ method: 'POST', path: '/orders', handler: createOrder })\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert _endpoint(edges, "server.createOrder", "POST /orders"), edges

    def test_method_array_registers_each_verb(self, tmp_path: Path) -> None:
        files = {
            "server.js": (
                "fastify.route({\n"
                "  method: ['GET', 'HEAD'],\n"
                "  url: '/ping',\n"
                "  handler: async () => 'pong',\n"
                "})\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert _endpoint(edges, "server", "GET /ping"), edges
        assert _endpoint(edges, "server", "HEAD /ping"), edges

    def test_client_options_object_without_handler_is_ignored(
        self, tmp_path: Path
    ) -> None:
        # An HTTP client's request({url, method}) has no handler function:
        # it is an outbound call, not a route registration.
        files = {
            "client.js": (
                "const client = require('./api')\n\n"
                "client.request({ url: '/users', method: 'GET' })\n"
                "client.request({ url: '/users', method: 'POST', body: payload })\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert not edges, edges

    def test_imported_handler_identifier_is_not_evidence(self, tmp_path: Path) -> None:
        # A handler referenced through an import is a documented ceiling: the
        # object shape alone must not register when nothing in the module
        # backs the handler up.
        files = {
            "routes.js": (
                "const { listUsers } = require('./handlers')\n\n"
                "sdk.describe({ method: 'GET', path: '/users', handler: listUsers })\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert not edges, edges

    def test_client_request_with_local_handler_is_ignored(self, tmp_path: Path) -> None:
        # A client SDK's request({...}) may pass a locally declared callback
        # under a handler key; only registration members (.route/.endpoint)
        # accept the options-object shape.
        files = {
            "client.js": (
                "const client = require('./api')\n\n"
                "function onDone(res) { return res }\n\n"
                "client.request({ method: 'GET', url: '/users', handler: onDone })\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert not edges, edges

    def test_const_arrow_handler_registers(self, tmp_path: Path) -> None:
        # A handler bound with `const handler = async () => {}` is as much
        # module-declared evidence as a function declaration.
        files = {
            "server.js": (
                "const handler = async (req, reply) => reply.send({})\n\n"
                "fastify.route({ method: 'GET', url: '/x', handler })\n"
            ),
        }
        edges = _run(tmp_path, files, "javascript")
        assert _endpoint(edges, "server.handler", "GET /x"), edges


class TestTestModulesEmitNoEndpoints:
    # Issue #910: routes registered inside test files must not become
    # production ENDPOINT resources.

    _GO_SOURCE = (
        "package main\n\n"
        'import "github.com/gofiber/fiber/v2"\n\n'
        "func serveCases() {\n"
        "\tapp := fiber.New()\n"
        '\tapp.Get("/cases", func(c *fiber.Ctx) error { return nil })\n'
        "}\n"
    )

    def test_go_test_module_registers_nothing(self, tmp_path: Path) -> None:
        edges = _run(tmp_path, {"main_test.go": self._GO_SOURCE}, "go")
        assert not edges, edges

    def test_same_go_registration_outside_tests_registers(self, tmp_path: Path) -> None:
        edges = _run(tmp_path, {"routes.go": self._GO_SOURCE}, "go")
        assert _endpoint(edges, "routes.serveCases", "GET /cases"), edges

    _JS_SOURCE = (
        "const express = require('express')\n"
        "const app = express()\n\n"
        "function getCases(req, res) { res.json([]) }\n\n"
        "app.get('/cases', getCases)\n"
    )

    def test_js_test_module_registers_nothing(self, tmp_path: Path) -> None:
        edges = _run(tmp_path, {"api.test.js": self._JS_SOURCE}, "javascript")
        assert not edges, edges

    _PY_SOURCE = (
        'app = object()\n\n\n@app.get("/cases")\ndef list_cases():\n    return []\n'
    )

    def test_python_decorator_in_tests_dir_registers_nothing(
        self, tmp_path: Path
    ) -> None:
        edges = _run(tmp_path, {"tests/test_api.py": self._PY_SOURCE}, "python")
        assert not edges, edges

    def test_same_python_decorator_outside_tests_registers(
        self, tmp_path: Path
    ) -> None:
        edges = _run(tmp_path, {"api.py": self._PY_SOURCE}, "python")
        assert _endpoint(edges, "api.list_cases", "GET /cases"), edges

    def test_rehydrated_test_module_handler_stays_excluded(
        self, tmp_path: Path
    ) -> None:
        # An incremental run rehydrates unchanged handlers from the graph;
        # dp.module_qn_to_file_path only covers re-parsed files, so the gate
        # must also see graph-backed module paths or a test handler fails
        # open and re-emits after stale cleanup.
        from codebase_rag.parsers.endpoint_prefixes import (
            CYPHER_PROJECT_PY_MODULES,
            CYPHER_PROJECT_ROUTE_HANDLERS,
        )

        parsers, queries = load_parsers()
        source = tmp_path / "tests" / "test_api.py"
        source.parent.mkdir(parents=True)
        source.write_text(self._PY_SOURCE, encoding="utf-8")
        project = tmp_path.name
        module_qn = f"{project}.tests.test_api"

        class _Ingestor:
            # Concrete (not MagicMock) so isinstance(_, QueryProtocol) holds.
            def __init__(self) -> None:
                self.ensure_node_batch = MagicMock()
                self.ensure_relationship_batch = MagicMock()
                self.flush_all = MagicMock()
                self.execute_write = MagicMock()

            def fetch_all(
                self, query: str, params: dict[str, object] | None = None
            ) -> list[dict[str, object]]:
                if query == CYPHER_PROJECT_PY_MODULES:
                    return [
                        {
                            "qualified_name": module_qn,
                            "path": "tests/test_api.py",
                        }
                    ]
                if query == CYPHER_PROJECT_ROUTE_HANDLERS:
                    return [
                        {
                            "qualified_name": f"{module_qn}.list_cases",
                            "decorators": ['@app.get("/cases")'],
                            "labels": ["Function"],
                        }
                    ]
                return []

        ingestor = _Ingestor()
        updater = GraphUpdater(
            ingestor=ingestor,
            repo_path=tmp_path,
            parsers=parsers,
            queries=queries,
            capture=_CAPTURE_IO,
        )
        updater._emit_pending_endpoints()
        exposes = [
            c
            for c in ingestor.ensure_relationship_batch.call_args_list
            if str(c.args[1]) == EXPOSES
        ]
        assert not exposes, exposes
