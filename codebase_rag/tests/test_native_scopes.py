"""The Rust scope walks against the Python twins they replace (issue #3473).

`TreeBridge.scope_facts` and `TreeBridge.inner_scope_only_names` must return
exactly what `python_scope_facts` and `python_inner_scope_only_names` return.
These compare the two on every node of a source written to reach every
binding form, and on every scope of cgr's own Python package; they also pin
the cache in front of both and when the bridge must step aside.

The behaviours the walks carry (#1907, #1922, #1928, #2124, #2666) are
pinned on both paths by their own modules, through the `both_scope_walks`
fixture.
"""

import importlib
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.embedded_graph.extension import rust_extension
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import native_trees
from codebase_rag.parsers.py import ast_analyzer
from codebase_rag.parsers.py.ast_analyzer import (
    clear_scope_facts,
    local_names,
    python_inner_scope_only_names,
    python_scope_facts,
    scope_facts,
    scope_facts_from_native,
)
from codebase_rag.types_defs import ScopeFacts

RUST = rust_extension()
ANALYZER = importlib.import_module("codebase_rag.parsers.py.ast_analyzer")
PARSERS, _ = load_parsers()
PACKAGE_DIR = Path(native_trees.__file__).parents[1]

needs_scope_walks = pytest.mark.skipif(
    RUST is None
    or not hasattr(getattr(RUST, cs.EG_RUST_TREE_BRIDGE, None), cs.EG_RUST_SCOPE_FACTS),
    reason="cgr_graph_rs with the scope walks is not installed",
)

# Every binding form `_binding_targets` reads, the parameter shapes, both
# declarations, the optional-import idiom inside and outside a def, imports
# of every shape, comprehensions and lambdas that do and do not leak a read,
# a non-ASCII name, and a byte that is not UTF-8 (tree-sitter keeps it out
# of any identifier, in an ERROR node).
SAMPLE = (
    b"import a.b, c as d\n"
    b"from .pkg import e, f as g\n"
    b"from . import helpers\n"
    b"from m import *\n"
    b"try:\n"
    b"    from .opt import fast\n"
    b"except ImportError as err:\n"
    b"    fast = None\n"
    b"    class Fallback: pass\n"
    b"def outer(p, /, q: int = (r := 1), *args, s, t=2, **kw):\n"
    b"    global g\n"
    b"    import os.path as osp, json\n"
    b"    from . import helpers\n"
    b"    try:\n"
    b"        import helpers\n"
    b"    except ImportError:\n"
    b"        helpers = None\n"
    b"        def handler(): pass\n"
    b"    x, (y, [z, *w]) = 1, (2, [3, 4])\n"
    b"    x += 1\n"
    b"    n: int\n"
    b"    n = 2\n"
    b"    for i, (j, *k) in enumerate(args):\n"
    b"        pass\n"
    b"    with open(p) as fh, open(q) as (u, v), open(s) as self.attr:\n"
    b"        pass\n"
    b"    match p:\n"
    b"        case [first, *rest] if first:\n"
    b"            pass\n"
    b"        case Point(x=_, y=yy) | other:\n"
    b"            pass\n"
    b"        case _:\n"
    b"            pass\n"
    b"    keys = [key for key in kw] + [v2 for v2 in {k2: k2 for k2 in args}]\n"
    b"    key(0)\n"
    b"    only = [c2 for c2 in args if (seen := c2)]\n"
    b"    fn = lambda a1, b1=1, *c1, **d1: a1 + b1 + call(name=a1).attr\n"
    b"    gen = (g2 for g2 in (lambda: g2)())\n"
    b"    def middle(v):\n"
    b"        nonlocal x\n"
    b"        def inner():\n"
    b"            nonlocal v, x\n"
    b"            v = x = 0\n"
    b"        class C:\n"
    b"            nonlocal v\n"
    b"            v = 1\n"
    b"        return inner, C\n"
    b"    caf\xc3\xa9 = \xff = 0\n"
    b"    return middle\n"
    b"class K(Base):\n"
    b"    attr = lambda self: self\n"
    b"    def method(self, *, flag):\n"
    b"        return [flag for flag in self]\n"
)


def _nodes(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        yield node


def _parse(source: bytes) -> Node:
    if cs.SupportedLanguage.PYTHON not in PARSERS:
        pytest.skip("python parser not available")
    return PARSERS[cs.SupportedLanguage.PYTHON].parse(source).root_node


def _assert_twins_agree(bridge: native_trees.TreeBridge, node: Node) -> None:
    assert scope_facts_from_native(bridge.scope_facts(node)) == (
        python_scope_facts(node)
    ), node
    assert frozenset(bridge.inner_scope_only_names(node)) == (
        python_inner_scope_only_names(node)
    ), node


@pytest.fixture
def bridge() -> native_trees.TreeBridge:
    native_trees._bridge.cache_clear()
    found = native_trees.scope_bridge()
    assert found is not None, "the extension carries the scope walks but they are off"
    return found


@pytest.fixture(autouse=True)
def _fresh_scope_caches() -> Iterator[None]:
    clear_scope_facts()
    yield
    clear_scope_facts()


@needs_scope_walks
class TestParity:
    def test_every_node_of_the_sample(self, bridge: native_trees.TreeBridge) -> None:
        compared = 0
        for node in _nodes(_parse(SAMPLE)):
            _assert_twins_agree(bridge, node)
            compared += 1
        assert compared > 500

    def test_the_sample_reaches_every_kind_of_fact(self) -> None:
        # Guards the sample itself: a parity test over facts that are all
        # empty would pass for walks that read nothing.
        facts = [python_scope_facts(node) for node in _nodes(_parse(SAMPLE))]
        assert any(f.conditional for f in facts)
        assert any(f.declared for f in facts)
        assert any(f.nonlocals for f in facts)
        assert any(f.params for f in facts)
        assert any("caf\u00e9" in f.plain for f in facts)
        assert any(
            python_inner_scope_only_names(node) for node in _nodes(_parse(SAMPLE))
        )

    def test_every_scope_of_cgr(self, bridge: native_trees.TreeBridge) -> None:
        # Real code, every def, class, lambda and module of the package.
        compared = 0
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            for node in _nodes(_parse(path.read_bytes())):
                if node.type in cs.PY_NESTED_SCOPE_TYPES or node.type == (
                    cs.TS_PY_MODULE
                ):
                    _assert_twins_agree(bridge, node)
                    compared += 1
        assert compared > 10_000

    def test_the_walks_take_the_bridge(
        self, bridge: native_trees.TreeBridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _parse(SAMPLE)
        expected = [
            (python_scope_facts(node), python_inner_scope_only_names(node))
            for node in _nodes(root)
        ]

        def refuse(_node: Node) -> NoReturn:
            raise AssertionError("the Python twin ran")

        monkeypatch.setattr(ANALYZER, "python_scope_facts", refuse)
        monkeypatch.setattr(ANALYZER, "python_inner_scope_only_names", refuse)
        assert [
            (scope_facts(node), ast_analyzer._inner_scope_only_names(node))
            for node in _nodes(root)
        ] == expected


class TestCache:
    def test_a_scope_is_walked_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_trees, "ENABLED", False)
        function = _parse(b"def f(a):\n    b = a\n").children[0]
        walked: list[Node] = []
        twin = ANALYZER.python_scope_facts

        def counting(node: Node) -> ScopeFacts:
            walked.append(node)
            return twin(node)

        monkeypatch.setattr(ANALYZER, "python_scope_facts", counting)
        assert local_names(function, {}) == local_names(function, {"b": "m.b"})
        assert walked == [function]
        clear_scope_facts()
        scope_facts(function)
        assert walked == [function, function]

    def test_another_parse_of_the_same_source_is_its_own_scope(self) -> None:
        source = b"def f(a):\n    b = a\n"
        first = _parse(source).children[0]
        second = _parse(source).children[0]
        assert scope_facts(first) == scope_facts(second)
        assert scope_facts.trees == 2

    @pytest.mark.parametrize(
        ("cached", "twin"),
        [
            ("scope_facts", "python_scope_facts"),
            ("_inner_scope_only_names", "python_inner_scope_only_names"),
        ],
    )
    def test_it_keeps_the_trees_read_last(
        self, monkeypatch: pytest.MonkeyPatch, cached: str, twin: str
    ) -> None:
        # A cached scope keeps its whole tree alive, outside the AST cache's
        # memory cap, so the bound is on trees: past it, the tree read
        # longest ago lets go of every scope it holds.
        monkeypatch.setattr(native_trees, "ENABLED", False)
        walk = getattr(ANALYZER, cached)
        walked: list[Node] = []
        reference = getattr(ANALYZER, twin)

        def counting(node: Node) -> ScopeFacts | frozenset[str]:
            walked.append(node)
            return reference(node)

        monkeypatch.setattr(ANALYZER, twin, counting)
        source = b"def f(a):\n    b = a\ndef g(c):\n    return [d for d in c]\n"
        roots = [_parse(source) for _ in range(cs.PY_SCOPE_CACHE_TREES + 1)]
        for root in roots[:-1]:
            for function in root.children:
                walk(function)
        assert walk.trees == cs.PY_SCOPE_CACHE_TREES
        oldest, newest = roots[0].children, roots[-1].children
        walk(oldest[0])
        for function in newest:
            walk(function)
        assert walk.trees == cs.PY_SCOPE_CACHE_TREES
        walked.clear()
        # Reading the oldest tree again kept it, so the second tree went.
        walk(oldest[1])
        assert walked == []
        for function in roots[1].children:
            walk(function)
        assert walked == roots[1].children


@needs_scope_walks
class TestFallback:
    def test_a_node_it_cannot_read_runs_the_python_twin(
        self, bridge: native_trees.TreeBridge
    ) -> None:
        node = MagicMock(spec=Node)
        with pytest.raises(native_trees.NotBorrowed):
            bridge.scope_facts(node)
        with pytest.raises(native_trees.NotBorrowed):
            bridge.inner_scope_only_names(node)

    def test_a_tree_without_its_source_runs_the_python_twin(
        self, bridge: native_trees.TreeBridge
    ) -> None:
        source = b"def f(a):\n    b = [c for c in a]\n"
        parser = PARSERS[cs.SupportedLanguage.PYTHON]
        tree = parser.parse(lambda byte, _point: source[byte : byte + 4])
        function = tree.root_node.children[0]
        with pytest.raises(native_trees.NotBorrowed):
            bridge.scope_facts(function)
        with pytest.raises(native_trees.NotBorrowed):
            bridge.inner_scope_only_names(function)
        assert scope_facts(function) == python_scope_facts(function)
        assert ast_analyzer._inner_scope_only_names(function) == (
            python_inner_scope_only_names(function)
        )

    def test_switched_off_it_is_not_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_trees, "ENABLED", False)
        assert native_trees.scope_bridge() is None

    def test_a_build_without_the_walks_leaves_them_to_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        older = MagicMock(spec=["anchor_hash", "ast_fingerprint", "describe"])
        monkeypatch.setattr(native_trees, "_bridge", lambda: older)
        assert native_trees.tree_bridge() is older
        assert native_trees.scope_bridge() is None

    def test_rules_it_cannot_apply_turn_it_off(self) -> None:
        bridge_type = getattr(RUST, cs.EG_RUST_TREE_BRIDGE)
        without_scopes = native_trees.rules()
        del without_scopes["scopes"]
        with pytest.raises(KeyError):
            bridge_type(without_scopes)

    @pytest.mark.parametrize(
        ("twin", "check"),
        [
            ("python_scope_facts", cs.TreeBridgeCheck.SCOPE_FACTS),
            ("python_inner_scope_only_names", cs.TreeBridgeCheck.INNER_SCOPE_NAMES),
        ],
    )
    def test_a_disagreeing_probe_turns_it_off(
        self,
        bridge: native_trees.TreeBridge,
        monkeypatch: pytest.MonkeyPatch,
        twin: str,
        check: cs.TreeBridgeCheck,
    ) -> None:
        monkeypatch.setattr(ANALYZER, twin, lambda _node: None)
        found = native_trees._disagreement(bridge)
        assert found is not None
        assert found[0] == check
