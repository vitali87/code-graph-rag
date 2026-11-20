"""The Rust callable-parameter scan against the Python twin it replaces.

`TreeBridge.callable_parameter_indices` must find what
`python_callable_parameter_indices` finds, for the languages whose scan it
carries (Python and JS/TS), and decline every other language, which the twin
keeps. These compare the two on every node of samples written to reach each
way a parameter is called, captured or shadowed, on the JS/TS files the
repository tracks, and on every def and lambda of cgr's own Python package.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.embedded_graph.extension import rust_extension
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import native_trees, utils
from codebase_rag.parsers.utils import (
    callable_parameter_indices,
    python_callable_parameter_indices,
)

RUST = rust_extension()
PARSERS, _ = load_parsers()
PACKAGE_DIR = Path(native_trees.__file__).parents[1]
REPO_ROOT = PACKAGE_DIR.parent

needs_callable_scan = pytest.mark.skipif(
    RUST is None
    or not hasattr(
        getattr(RUST, cs.EG_RUST_TREE_BRIDGE, None),
        cs.EG_RUST_CALLABLE_PARAMETER_INDICES,
    ),
    reason="cgr_graph_rs with the callable-parameter scan is not installed",
)

# Parameters called directly, from a nested def, a lambda and a comprehension,
# shadowed by a nested def's own parameter or binding, behind a class (which
# the scan does not enter), through an attribute or a subscript (not a bare
# call), splats and defaults, typed parameters, a duplicated name, `self`
# dropped, and an async def.
PY_SAMPLE = b"""\
def direct(self, cb, other):
    cb()

def nested(cb, keep, gone):
    def inner():
        cb()
    def shadow(gone):
        gone()
    def rebinds():
        keep = make()
        keep()
    lam = lambda: keep()
    class C:
        def m(self):
            gone()
    return inner, shadow, rebinds, lam, C

def defaults(a=1, *args, b, **kw):
    args()
    kw()
    b()

def typed(cb: Callable[[], None], n: int = 2) -> None:
    cb()

def not_bare(a, b):
    a.b()
    b[0]()

def dup(a, a):
    a()

async def asy(cb):
    await cb()

def comp(cb, items):
    return [cb(i) for i in items]

def none_called(a, b):
    return a + b

def no_params():
    run()

top = lambda cb, x: cb(x)
"""

JS_SAMPLE = b"""\
function f(cb, opts) { cb(); }
function g(cb) { function inner() { cb(); } return inner; }
function h(cb, keep) { function shadow(cb) { cb(); } return shadow; }
const a = (cb, x = 1, ...rest) => { rest(); x(); };
class K { m(cb) { cb(); } }
function d({ cb }, [first]) { cb(); first(); }
function e(cb) { const z = () => cb(); return z; }
function n(a, b) { a.b(); b[0](); }
function none(a) { return a; }
function two(cb, done) { cb(); done(); }
"""

TS_SAMPLE = JS_SAMPLE + (
    b"function t(cb: () => void, n?: number): void { cb(); }\n"
    b"const u = (cb: (x: number) => number): number => cb(1);\n"
)

SAMPLES = [
    pytest.param(cs.SupportedLanguage.PYTHON, PY_SAMPLE, id="python"),
    pytest.param(cs.SupportedLanguage.JS, JS_SAMPLE, id="javascript"),
    pytest.param(cs.SupportedLanguage.TS, TS_SAMPLE, id="typescript"),
    pytest.param(cs.SupportedLanguage.TSX, TS_SAMPLE, id="tsx"),
]

TRACKED_JS_TS = {
    "evals/flows_corpus/javascript/app.js": cs.SupportedLanguage.JS,
    "evals/oracles/lua_oracle/lua_ast.js": cs.SupportedLanguage.JS,
    "evals/oracles/php_oracle/php_ast.js": cs.SupportedLanguage.JS,
    "evals/oracles/ruby_oracle/ruby_ast.js": cs.SupportedLanguage.JS,
    "evals/oracles/ts_oracle/ts_ast.js": cs.SupportedLanguage.JS,
    "fixtures/well_known_symbols/box.js": cs.SupportedLanguage.JS,
    "fixtures/well_known_symbols/iterables.ts": cs.SupportedLanguage.TS,
}


def _nodes(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        yield node


def _parse(language: cs.SupportedLanguage, source: bytes) -> Node:
    if language not in PARSERS:
        pytest.skip(f"{language} parser not available")
    return PARSERS[language].parse(source).root_node


@pytest.fixture
def bridge() -> native_trees.TreeBridge:
    native_trees._bridge.cache_clear()
    found = native_trees.callable_bridge()
    assert found is not None, "the extension carries the scan but it is off"
    return found


def _assert_twins_agree(
    bridge: native_trees.TreeBridge, node: Node, language: cs.SupportedLanguage
) -> None:
    native = bridge.callable_parameter_indices(node, language)
    assert native is not None, node
    # A list, so a duplicated name keeps the twin's last index once dict()-ed.
    assert dict(native) == python_callable_parameter_indices(node, language), node


@needs_callable_scan
class TestParity:
    @pytest.mark.parametrize(("language", "source"), SAMPLES)
    def test_every_node_of_the_sample(
        self,
        bridge: native_trees.TreeBridge,
        language: cs.SupportedLanguage,
        source: bytes,
    ) -> None:
        compared = 0
        for node in _nodes(_parse(language, source)):
            _assert_twins_agree(bridge, node, language)
            compared += 1
        assert compared > 100

    @pytest.mark.parametrize(("language", "source"), SAMPLES)
    def test_the_sample_reaches_every_kind_of_call(
        self, language: cs.SupportedLanguage, source: bytes
    ) -> None:
        # Guards the samples themselves: parity over functions that call no
        # parameter would pass for a scan that finds none.
        found = [
            indices
            for node in _nodes(_parse(language, source))
            if (indices := python_callable_parameter_indices(node, language))
        ]
        assert len(found) >= 5
        assert any(len(indices) > 1 for indices in found)

    def test_every_tracked_js_ts_file(self, bridge: native_trees.TreeBridge) -> None:
        compared = 0
        for path, language in TRACKED_JS_TS.items():
            source = (REPO_ROOT / path).read_bytes()
            for node in _nodes(_parse(language, source)):
                _assert_twins_agree(bridge, node, language)
                compared += 1
        assert compared > 5_000

    def test_every_def_and_lambda_of_cgr(self, bridge: native_trees.TreeBridge) -> None:
        python = cs.SupportedLanguage.PYTHON
        compared = 0
        found = 0
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            for node in _nodes(_parse(python, path.read_bytes())):
                if node.type in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_LAMBDA):
                    _assert_twins_agree(bridge, node, python)
                    compared += 1
                    found += bool(python_callable_parameter_indices(node, python))
        assert compared > 30_000
        assert found > 100

    def test_the_scan_takes_the_bridge(
        self, bridge: native_trees.TreeBridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        python = cs.SupportedLanguage.PYTHON
        root = _parse(python, PY_SAMPLE)
        expected = [
            python_callable_parameter_indices(node, python) for node in _nodes(root)
        ]

        def refuse(_node: Node, _language: cs.SupportedLanguage | None) -> NoReturn:
            raise AssertionError("the Python twin ran")

        monkeypatch.setattr(utils, "python_callable_parameter_indices", refuse)
        assert [
            callable_parameter_indices(node, python) for node in _nodes(root)
        ] == expected


@needs_callable_scan
@pytest.mark.parametrize(
    "language",
    [
        cs.SupportedLanguage.GO,
        cs.SupportedLanguage.CPP,
        cs.SupportedLanguage.JAVA,
        cs.SupportedLanguage.RUST,
    ],
)
def test_the_bridge_declines_a_language_the_twin_scans(
    bridge: native_trees.TreeBridge, language: cs.SupportedLanguage
) -> None:
    node = _parse(cs.SupportedLanguage.PYTHON, PY_SAMPLE)
    assert bridge.callable_parameter_indices(node, language) is None


@needs_callable_scan
def test_a_node_the_bridge_cannot_read_runs_the_twin(
    bridge: native_trees.TreeBridge,
) -> None:
    python = cs.SupportedLanguage.PYTHON
    source = PY_SAMPLE
    tree = PARSERS[python].parse(lambda byte, _point: source[byte : byte + 1])
    function = next(
        node for node in _nodes(tree.root_node) if node.type == "function_definition"
    )
    with pytest.raises(native_trees.NotBorrowed):
        bridge.callable_parameter_indices(function, python)
    assert callable_parameter_indices(
        function, python
    ) == python_callable_parameter_indices(function, python)


def test_the_scan_runs_the_twin_without_the_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_trees, "ENABLED", False)
    python = cs.SupportedLanguage.PYTHON
    for node in _nodes(_parse(python, PY_SAMPLE)):
        assert callable_parameter_indices(
            node, python
        ) == python_callable_parameter_indices(node, python)
