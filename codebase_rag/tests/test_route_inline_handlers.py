"""Express route handlers passed inline are the EXPOSES source (issue #2521).

`app.get('/orders/:id', function getOrder(req, res) {...})` and
`app.delete('/orders/:id', (req, res) => ...)` recorded the Module as the
handler, so every route of a file shared one handler and neither
`endpoint_callers` nor an impact question could be answered per route. The
definition pass already registers each inline function as its own node; the
route pass now finds that node.
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
_EXPOSES = cs.RelationshipType.EXPOSES.value


def _exposes(
    tmp_path: Path, files: dict[str, str], language: str
) -> dict[str, set[tuple[str, str]]]:
    # endpoint identity -> {(source label, source qn relative to the project)}
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    prefix = f"{tmp_path.name}."
    out: dict[str, set[tuple[str, str]]] = {}
    for call in mock.ensure_relationship_batch.call_args_list:
        if str(call.args[1]) != _EXPOSES:
            continue
        label, _key, qn = call.args[0]
        identity = call.args[2][2].split("::")[-1]
        out.setdefault(identity, set()).add((str(label), qn.removeprefix(prefix)))
    return out


_FUNCTION = cs.NodeLabel.FUNCTION.value
_MODULE = cs.NodeLabel.MODULE.value


def _anonymous(source: str, row: int, marker: str) -> str:
    # The definition pass names an unnamed function by its 0-based start
    # row and column.
    return f"anonymous_{row}_{source.splitlines()[row].index(marker)}"


_ISSUE_SERVER = (
    "const express = require('express');\n"
    "const app = express();\n"
    "app.get('/orders/:id', function getOrder(req, res) { res.json({ id: req.params.id }); });\n"
    "app.delete('/orders/:id', (req, res) => res.sendStatus(204));\n"
    "app.listen(3000);\n"
)


class TestInlineHandlers:
    def test_named_function_expression_is_the_handler(self, tmp_path: Path) -> None:
        exposes = _exposes(tmp_path, {"node/server.js": _ISSUE_SERVER}, "javascript")
        assert exposes["GET /orders/:id"] == {(_FUNCTION, "node.server.getOrder")}

    def test_inline_arrow_is_the_handler(self, tmp_path: Path) -> None:
        exposes = _exposes(tmp_path, {"node/server.js": _ISSUE_SERVER}, "javascript")
        arrow = _anonymous(_ISSUE_SERVER, 3, "(req, res) =>")
        assert exposes["DELETE /orders/:id"] == {(_FUNCTION, f"node.server.{arrow}")}

    def test_routes_of_one_file_do_not_share_a_handler(self, tmp_path: Path) -> None:
        exposes = _exposes(tmp_path, {"node/server.js": _ISSUE_SERVER}, "javascript")
        assert exposes["GET /orders/:id"].isdisjoint(exposes["DELETE /orders/:id"])
        assert all(
            label != _MODULE for sources in exposes.values() for label, _ in sources
        )

    def test_inline_arrow_in_a_function_scope(self, tmp_path: Path) -> None:
        source = (
            "const express = require('express');\n"
            "const mount = (router) => {\n"
            "  router.post('/carts', (req, res) => res.sendStatus(201));\n"
            "};\n"
            "mount(express.Router());\n"
        )
        exposes = _exposes(tmp_path, {"routes.js": source}, "javascript")
        arrow = _anonymous(source, 2, "(req, res) =>")
        assert exposes["POST /carts"] == {(_FUNCTION, f"routes.mount.{arrow}")}

    def test_last_argument_is_the_handler_after_middleware(
        self, tmp_path: Path
    ) -> None:
        # Express runs every argument after the path in order; the last one
        # answers the request, the ones before it are middleware.
        source = (
            "const express = require('express');\n"
            "const app = express();\n"
            "function auth(req, res, next) { next(); }\n"
            "function listOrders(req, res) { res.json([]); }\n"
            "app.get('/orders', auth, listOrders);\n"
            "app.post('/orders', auth, (req, res) => res.sendStatus(201));\n"
        )
        exposes = _exposes(tmp_path, {"server.js": source}, "javascript")
        assert exposes["GET /orders"] == {(_FUNCTION, "server.listOrders")}
        arrow = _anonymous(source, 5, "(req, res) =>")
        assert exposes["POST /orders"] == {(_FUNCTION, f"server.{arrow}")}

    def test_identifier_naming_a_function_in_the_registering_scope(
        self, tmp_path: Path
    ) -> None:
        source = (
            "const express = require('express');\n"
            "function setup(router) {\n"
            "  function getCart(req, res) { res.json({}); }\n"
            "  router.get('/cart', getCart);\n"
            "}\n"
            "setup(express.Router());\n"
        )
        exposes = _exposes(tmp_path, {"cart.js": source}, "javascript")
        assert exposes["GET /cart"] == {(_FUNCTION, "cart.setup.getCart")}

    def test_options_object_method_handler(self, tmp_path: Path) -> None:
        source = (
            "const fastify = require('fastify')();\n"
            "fastify.route({ method: 'GET', url: '/health', async handler(req) {"
            " return { ok: true }; } });\n"
        )
        exposes = _exposes(tmp_path, {"app.js": source}, "javascript")
        assert exposes["GET /health"] == {(_FUNCTION, "app.handler")}


class TestAttributionThatStays:
    def test_flask_and_fastapi_decorators_keep_their_function(
        self, tmp_path: Path
    ) -> None:
        source = (
            "from flask import Flask, jsonify\n"
            "app = Flask(__name__)\n\n\n"
            '@app.route("/users/<int:user_id>", methods=["GET"])\n'
            "def get_user(user_id):\n"
            '    return jsonify({"id": user_id})\n\n\n'
            '@app.post("/users")\n'
            "def create_user():\n"
            "    return jsonify({}), 201\n"
        )
        exposes = _exposes(tmp_path, {"api/app.py": source}, "python")
        assert exposes["GET /users/<int:user_id>"] == {(_FUNCTION, "api.app.get_user")}
        assert exposes["POST /users"] == {(_FUNCTION, "api.app.create_user")}

    def test_module_level_identifier_handler_is_unchanged(self, tmp_path: Path) -> None:
        source = (
            "const express = require('express');\n"
            "const app = express();\n"
            "function getProduct(req, res) { res.json({}); }\n"
            "app.get('/products/:id', getProduct);\n"
        )
        exposes = _exposes(tmp_path, {"server.js": source}, "javascript")
        assert exposes["GET /products/:id"] == {(_FUNCTION, "server.getProduct")}

    def test_imported_handler_stays_anchored_to_the_module(
        self, tmp_path: Path
    ) -> None:
        # A handler the module does not define has no node here; naming one
        # would mint a phantom `server.getUser`, so the module keeps it.
        source = (
            "const express = require('express');\n"
            "const { getUser } = require('./handlers');\n"
            "const app = express();\n"
            "app.get('/users/:id', getUser);\n"
        )
        exposes = _exposes(tmp_path, {"server.js": source}, "javascript")
        assert exposes["GET /users/:id"] == {(_MODULE, "server")}

    def test_go_func_literal_keeps_the_registering_function(
        self, tmp_path: Path
    ) -> None:
        # Go function literals are not graph nodes, so the enclosing
        # function stays the handler.
        source = (
            "package main\n\n"
            'import "github.com/labstack/echo/v4"\n\n'
            "func main() {\n"
            "\te := echo.New()\n"
            '\te.GET("/items/:id", func(c echo.Context) error { return nil })\n'
            "}\n"
        )
        exposes = _exposes(tmp_path, {"main.go": source}, "go")
        assert exposes["GET /items/:id"] == {(_FUNCTION, "main.main")}
