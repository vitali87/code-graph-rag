# A JS/TS bare call resolves through what its name is bound to (issue #2402).
#
# express's lib/response.js does `var createError = require('http-errors')`
# and calls `createError(406, ...)`; the graph linked that call to six NAMED
# FUNCTION EXPRESSIONS called `createError` inside test callbacks
# (`route.all(function createError (req, res, next) {...})`), fanned out as
# `overload`. Two things were wrong, and each is pinned here on its own:
#
# 1. The caller's own `require`/`import` binding was ignored: the import map
#    records `createError -> http-errors`, but a dot-free package target was
#    not recognised as external, so the call fell to the bare-name trie.
# 2. A named function expression's name is bound inside its own body only,
#    yet it was offered to bare-name lookups from anywhere, same file or not.
#
# The negative tests pin what must not move: a binding to a first-party
# definition, a same-file declaration, a function expression stored under
# its own name, and a named function expression calling itself.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from evals.cgr_graph import _StatefulIngestor

_TEST_CALLBACKS = (
    "var app = require('../lib/handler')\n"
    "describe('app', function () {\n"
    "  it('one', function () {\n"
    "    app.use(function createError (req, res, next) { next() })\n"
    "  })\n"
    "  it('two', function () {\n"
    "    app.use(function createError (req, res, next) { next() })\n"
    "  })\n"
    "})\n"
)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def _calls(mock_ingestor: MagicMock) -> dict[tuple[str, str], str | None]:
    edges: dict[tuple[str, str], str | None] = {}
    for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS):
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        edges[(c.args[0][2], c.args[2][2])] = props.get(cs.KEY_RESOLUTION)
    return edges


def _index(root: Path, files: dict[str, str], mock_ingestor: MagicMock) -> str:
    _write(root, files)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="javascript")
    return root.name


def _targets_of(edges: dict[tuple[str, str], str | None], caller: str) -> set[str]:
    return {callee for (source, callee) in edges if source == caller}


# ---------------------------------------------------------------------------
# 1. A call to a name the caller's module binds by require/import
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler_path", "binding"),
    [
        ("lib/handler.js", "var createError = require('http-errors')"),
        ("lib/handler.js", "const createError = require('node:util')"),
        ("lib/handler.js", "import * as createError from 'http-errors'"),
        ("lib/handler.ts", "const createError = require('http-errors')"),
    ],
    ids=["var-require", "node-scheme", "namespace-import", "ts-require"],
)
def test_call_to_external_binding_never_reaches_other_modules(
    temp_repo: Path, mock_ingestor: MagicMock, handler_path: str, binding: str
) -> None:
    # The issue's reproduction: nothing in lib/ may call into test/.
    root = temp_repo / "extbind"
    project = _index(
        root,
        {
            handler_path: (
                f"{binding}\n"
                "function handler (req, res, next) { next(createError(404, 'x')) }\n"
                "module.exports = handler\n"
            ),
            "test/app.test.js": _TEST_CALLBACKS,
        },
        mock_ingestor,
    )
    edges = _calls(mock_ingestor)
    leaked = {
        (src, dst)
        for (src, dst) in edges
        if src.startswith(f"{project}.lib.") and dst.startswith(f"{project}.test.")
    }
    assert not leaked, sorted(leaked)


def test_call_to_external_binding_is_unresolved_even_with_one_candidate(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A same-named FUNCTION DECLARATION elsewhere is a real module-level
    # definition, so only the binding (not the function-expression rule) can
    # keep the call away from it.
    root = temp_repo / "extdecl"
    project = _index(
        root,
        {
            "lib/handler.js": (
                "var createError = require('http-errors')\n"
                "function handler () { return createError(404) }\n"
                "module.exports = handler\n"
            ),
            "lib/errors.js": "function createError () {}\nmodule.exports = {}\n",
        },
        mock_ingestor,
    )
    assert not _targets_of(_calls(mock_ingestor), f"{project}.lib.handler.handler")


# ---------------------------------------------------------------------------
# 2. A named function expression's name is visible inside its own body only
# ---------------------------------------------------------------------------


def test_unbound_call_never_reaches_function_expression_in_another_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "unbound"
    project = _index(
        root,
        {
            "lib/handler.js": (
                "function handler () { return createError(404) }\n"
                "module.exports = handler\n"
            ),
            "test/app.test.js": _TEST_CALLBACKS,
        },
        mock_ingestor,
    )
    assert not _targets_of(_calls(mock_ingestor), f"{project}.lib.handler.handler")


def test_function_expression_argument_is_not_a_module_level_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "samemod"
    project = _index(
        root,
        {
            "lib/a.js": (
                "app.use(function helper () {})\n"
                "function caller () { return helper() }\n"
            ),
        },
        mock_ingestor,
    )
    assert f"{project}.lib.a.helper" not in _targets_of(
        _calls(mock_ingestor), f"{project}.lib.a.caller"
    )


def test_function_expression_argument_is_not_visible_in_enclosing_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `outer` itself keeps its edge to the callback it hands over; the bare
    # call sits in a sibling so only the name lookup can produce an edge.
    root = temp_repo / "enclosing"
    project = _index(
        root,
        {
            "lib/a.js": (
                "function outer () {\n"
                "  app.use(function inner () {})\n"
                "  function sibling () { return inner() }\n"
                "  return sibling\n"
                "}\n"
            ),
        },
        mock_ingestor,
    )
    assert f"{project}.lib.a.outer.inner" not in _targets_of(
        _calls(mock_ingestor), f"{project}.lib.a.outer.sibling"
    )


def test_function_expression_stored_under_another_name_is_not_callable_by_its_own(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `var foo = function bar () {}` binds `foo`; `bar` exists only inside.
    root = temp_repo / "renamed"
    project = _index(
        root,
        {
            "lib/a.js": (
                "var foo = function bar () {}\nfunction caller () { return bar() }\n"
            ),
        },
        mock_ingestor,
    )
    assert f"{project}.lib.a.bar" not in _targets_of(
        _calls(mock_ingestor), f"{project}.lib.a.caller"
    )


def _stateful_index(root: Path, store: _StatefulIngestor, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(ingestor=store, repo_path=root, parsers=parsers, queries=queries).run(
        force=force
    )


def _stateful_calls_from(store: _StatefulIngestor, caller_suffix: str) -> set[str]:
    return {
        str(target)
        for _, source, rel, _, target in store.edges
        if rel == cs.RelationshipType.CALLS.value
        and str(source).endswith(caller_suffix)
    }


@pytest.mark.parametrize(
    "binding",
    ["var createError = require('http-errors')\n", ""],
    ids=["require-bound", "unbound"],
)
def test_incremental_sync_of_the_caller_adds_no_edge(
    temp_repo: Path, binding: str
) -> None:
    # Re-parsing only lib/handler.js leaves test/app.test.js to rehydration,
    # so the function expressions' scoping must survive the graph round trip.
    parsers, _ = load_parsers()
    if "javascript" not in parsers:
        pytest.skip("javascript parser not available")
    handler_source = (
        f"{binding}"
        "function handler () { return createError(404) }\n"
        "module.exports = handler\n"
    )
    _write(
        temp_repo,
        {"lib/handler.js": handler_source, "test/app.test.js": _TEST_CALLBACKS},
    )
    store = _StatefulIngestor()
    _stateful_index(temp_repo, store, force=True)
    assert not _stateful_calls_from(store, ".lib.handler.handler")

    (temp_repo / "lib" / "handler.js").write_text(
        handler_source + "// touched\n", encoding="utf-8"
    )
    _stateful_index(temp_repo, store, force=False)
    assert not _stateful_calls_from(store, ".lib.handler.handler")


# ---------------------------------------------------------------------------
# Negative tests: resolutions that must stay exactly as they were
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "files",
    [
        {
            "lib/a.js": "const { f } = require('./lib')\nfunction caller () { return f() }\n",
            "lib/lib.js": "function f () {}\nmodule.exports = { f }\n",
        },
        {
            "lib/a.js": "const { f } = require('./lib')\nfunction caller () { return f() }\n",
            "lib/lib.js": "exports.f = function f () {}\n",
        },
        {
            "lib/a.js": "import { f } from './lib'\nfunction caller () { return f() }\n",
            "lib/lib.js": "export function f () {}\n",
        },
        {
            "lib/a.ts": "import { f } from './lib'\nfunction caller () { return f() }\n",
            "lib/lib.ts": "export function f () {}\n",
        },
    ],
    ids=["require-destructure", "require-exports-fn-expr", "import-js", "import-ts"],
)
def test_binding_to_first_party_function_still_resolves(
    temp_repo: Path, mock_ingestor: MagicMock, files: dict[str, str]
) -> None:
    root = temp_repo / "firstparty"
    project = _index(
        root, {**files, "test/app.test.js": _TEST_CALLBACKS}, mock_ingestor
    )
    edges = _calls(mock_ingestor)
    assert _targets_of(edges, f"{project}.lib.a.caller") == {f"{project}.lib.lib.f"}
    assert edges[(f"{project}.lib.a.caller", f"{project}.lib.lib.f")] == (
        cs.EdgeResolution.EXACT
    )


def test_require_of_module_exporting_named_function_expression_still_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `module.exports = function createError () {}` is reached through the
    # require binding, not through the expression's own name.
    root = temp_repo / "directexport"
    project = _index(
        root,
        {
            "lib/a.js": (
                "const createError = require('./errors')\n"
                "function caller () { return createError() }\n"
            ),
            "lib/errors.js": "module.exports = function createError () {}\n",
            "test/app.test.js": _TEST_CALLBACKS,
        },
        mock_ingestor,
    )
    assert _targets_of(_calls(mock_ingestor), f"{project}.lib.a.caller") == {
        f"{project}.lib.errors.createError"
    }


@pytest.mark.parametrize("caller_path", ["lib/a.js", "lib/a.ts"])
def test_default_import_of_module_exporting_function_expression_still_resolves(
    temp_repo: Path, mock_ingestor: MagicMock, caller_path: str
) -> None:
    # An ESM default import of that module has no precise binding to follow
    # (`errors.default` is not registered) and reaches it by name, so the
    # module's exported value keeps its name for the fallback.
    root = temp_repo / "defaultimport"
    project = _index(
        root,
        {
            caller_path: (
                "import createError from './errors'\n"
                "function caller () { return createError() }\n"
            ),
            "lib/errors.js": "module.exports = function createError () {}\n",
            "test/app.test.js": _TEST_CALLBACKS,
        },
        mock_ingestor,
    )
    assert _targets_of(_calls(mock_ingestor), f"{project}.lib.a.caller") == {
        f"{project}.lib.errors.createError"
    }


def test_same_file_function_declaration_still_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "samefile"
    project = _index(
        root,
        {
            "lib/a.js": "function f () {}\nfunction caller () { return f() }\n",
            "test/app.test.js": _TEST_CALLBACKS.replace("createError", "f"),
        },
        mock_ingestor,
    )
    edges = _calls(mock_ingestor)
    assert _targets_of(edges, f"{project}.lib.a.caller") == {f"{project}.lib.a.f"}
    assert edges[(f"{project}.lib.a.caller", f"{project}.lib.a.f")] == (
        cs.EdgeResolution.EXACT
    )


def test_function_expression_stored_under_its_own_name_still_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "selfstored"
    project = _index(
        root,
        {
            "lib/a.js": (
                "var same = function same () {}\nfunction caller () { return same() }\n"
            ),
        },
        mock_ingestor,
    )
    assert _targets_of(_calls(mock_ingestor), f"{project}.lib.a.caller") == {
        f"{project}.lib.a.same"
    }


def test_exported_function_expression_keeps_its_name_fallback(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `exports.handle = function handle () {}` stores the value under the
    # same name, so the bare-name fallback keeps offering it as before.
    root = temp_repo / "exported"
    project = _index(
        root,
        {
            "lib/a.js": "function caller () { return handle() }\n",
            "lib/app.js": "exports.handle = function handle () {}\n",
        },
        mock_ingestor,
    )
    assert _targets_of(_calls(mock_ingestor), f"{project}.lib.a.caller") == {
        f"{project}.lib.app.handle"
    }


def test_recursive_named_function_expression_still_calls_itself(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "recursion"
    project = _index(
        root,
        {
            "lib/a.js": (
                "app.use(function walk (n) { return walk(n - 1) })\n"
                "function outer () {\n"
                "  app.use(function inner (n) {\n"
                "    [n].forEach(function () { inner(n - 1) })\n"
                "  })\n"
                "}\n"
            ),
        },
        mock_ingestor,
    )
    # The call inside the nameless forEach callback is attributed to `inner`,
    # the enclosing named function, so it too reads as `inner -> inner`.
    edges = _calls(mock_ingestor)
    walk = f"{project}.lib.a.walk"
    inner = f"{project}.lib.a.outer.inner"
    assert edges[(walk, walk)] == cs.EdgeResolution.EXACT
    assert edges[(inner, inner)] == cs.EdgeResolution.EXACT


@pytest.mark.parametrize(
    ("source", "callback", "declaration", "outside_caller"),
    [
        (
            "function f () {}\n"
            "app.use(function f () { return f() })\n"
            "function caller () { return f() }\n",
            "f@2",
            "f",
            "caller",
        ),
        (
            "app.use(function f () { return f() })\n"
            "function f () {}\n"
            "function caller () { return f() }\n",
            "f",
            "f@2",
            "caller",
        ),
        (
            "function outer () {\n"
            "  function f () {}\n"
            "  app.use(function f () { return f() })\n"
            "  return f()\n"
            "}\n",
            "outer.f@3",
            "outer.f",
            "outer",
        ),
    ],
    ids=["declaration-first", "callback-first", "nested"],
)
def test_recursive_callback_shadows_same_named_declaration(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    source: str,
    callback: str,
    declaration: str,
    outside_caller: str,
) -> None:
    # Inside `function f`'s own body, `f` is that expression: it shadows the
    # enclosing scope's `function f`, so the recursion must not reach the
    # declaration through their shared `@line` group. Outside the body the
    # declaration still answers the name.
    root = temp_repo / "shadowdecl"
    project = _index(root, {"lib/a.js": source}, mock_ingestor)
    edges = _calls(mock_ingestor)
    callback_qn = f"{project}.lib.a.{callback}"
    declaration_qn = f"{project}.lib.a.{declaration}"
    assert _targets_of(edges, callback_qn) == {callback_qn}
    assert edges[(callback_qn, callback_qn)] == cs.EdgeResolution.EXACT
    assert declaration_qn in _targets_of(edges, f"{project}.lib.a.{outside_caller}")


def test_recursive_callback_without_outer_name_still_calls_only_itself(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # No other `f` in the module: the recursion keeps its single exact edge.
    root = temp_repo / "loneself"
    project = _index(
        root,
        {"lib/a.js": "app.use(function f () { return f() })\n"},
        mock_ingestor,
    )
    callback_qn = f"{project}.lib.a.f"
    edges = _calls(mock_ingestor)
    assert _targets_of(edges, callback_qn) == {callback_qn}
    assert edges[(callback_qn, callback_qn)] == cs.EdgeResolution.EXACT


def test_recursion_in_same_named_function_expressions_matches_main(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Duplicate names still fan out over their `@line` variants as before;
    # that spread is issue #2403's subject, not this one's.
    root = temp_repo / "duprecursion"
    project = _index(
        root,
        {
            "lib/a.js": (
                "app.use(function walk (n) { return walk(n - 1) })\n"
                "app.use(function walk (n) { return walk(n + 1) })\n"
            ),
        },
        mock_ingestor,
    )
    walk, walk2 = f"{project}.lib.a.walk", f"{project}.lib.a.walk@2"
    edges = _calls(mock_ingestor)
    for caller in (walk, walk2):
        assert _targets_of(edges, caller) == {walk, walk2}


def test_callback_edges_to_function_expression_arguments_are_kept(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The pass that links a module to the callbacks it hands over finds them
    # by their own span, not by name lookup, so those edges stay.
    root = temp_repo / "callbacks"
    project = _index(root, {"test/app.test.js": _TEST_CALLBACKS}, mock_ingestor)
    targets = _targets_of(_calls(mock_ingestor), f"{project}.test.app.test")
    assert {
        f"{project}.test.app.test.createError",
        f"{project}.test.app.test.createError@7",
    } <= targets


def test_member_access_still_matches_a_getter_function_expression(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # express's request.js shape: the getter is a call argument, so its own
    # name is body-scoped, but `req.host` is a property read, not a bare name.
    root = temp_repo / "getter"
    project = _index(
        root,
        {
            "lib/request.js": (
                "var req = exports = module.exports = {}\n"
                "function defineGetter (obj, name, getter) {\n"
                "  Object.defineProperty(obj, name, { get: getter })\n"
                "}\n"
                "defineGetter(req, 'host', function host () { return 'h' })\n"
            ),
            "lib/app.js": (
                "function handle (req, res) { res.end(String(req.host)) }\n"
                "module.exports = handle\n"
            ),
        },
        mock_ingestor,
    )
    assert _targets_of(_calls(mock_ingestor), f"{project}.lib.app.handle") == {
        f"{project}.lib.request.host"
    }


def test_declarations_sharing_a_function_expression_name_keep_their_fan_out(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # express's test/app.use.js: `function fn3` declarations in later cases
    # share their name with an earlier `app.use(function fn3 ...)`. The
    # declarations bind the name, so the call still reaches every variant.
    root = temp_repo / "mixedgroup"
    project = _index(
        root,
        {
            "test/use.test.js": (
                "describe('use', function () {\n"
                "  it('x', function () { app.use(function fn3 (req, res) {}) })\n"
                "  it('a', function () {\n"
                "    function fn3 () {}\n"
                "    app.use([fn3])\n"
                "  })\n"
                "  it('b', function () {\n"
                "    function fn3 () {}\n"
                "    app.use([fn3])\n"
                "  })\n"
                "})\n"
            ),
        },
        mock_ingestor,
    )
    module = f"{project}.test.use.test"
    assert {f"{module}.fn3@4", f"{module}.fn3@8"} <= _targets_of(
        _calls(mock_ingestor), module
    )
