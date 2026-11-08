"""The Rust alias walk against the Python twin it replaces.

`TreeBridge.local_aliases` must find what `python_local_aliases` finds:
the same locals in the same order, the same dunder, and leaves that turn
back into the very nodes the twin returns. These compare the two on every
node of a sample written to reach each form an alias reads through, and on
every module, def, class and lambda of cgr's own Python package; they also
pin when the walk takes the bridge and when it steps aside.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.embedded_graph.extension import rust_extension
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import native_trees
from codebase_rag.parsers.py import variable_analyzer
from codebase_rag.parsers.py.variable_analyzer import (
    aliases_from_native,
    local_aliases,
    python_local_aliases,
)

RUST = rust_extension()
PARSERS, _ = load_parsers()
PACKAGE_DIR = Path(native_trees.__file__).parents[1]

needs_alias_walk = pytest.mark.skipif(
    RUST is None
    or not hasattr(
        getattr(RUST, cs.EG_RUST_TREE_BRIDGE, None), cs.EG_RUST_LOCAL_ALIASES
    ),
    reason="cgr_graph_rs with the alias walk is not installed",
)

_DEEP = 40

# Each value form `_value_leaves` reads (`or`, `and`, a conditional, parentheses,
# operators with and without a dunder), leaves that are names, attributes and
# calls, a local assigned twice (the walk keeps the one it meets first), a
# chained assignment, targets that are not plain names, nested defs and
# classes (skipped), lambdas and comprehensions (walked), a comment inside a
# value, chains deeper than the cap, a non-ASCII name and a byte that is not
# UTF-8.
SAMPLE = (
    b"import os\n"
    b"alias = os.path or os.sep\n"
    b"def f(self, a, b, cb, *rest, **kw):\n"
    b"    x = a or b.c\n"
    b"    y = a and Make()\n"
    b"    z = (a if b else cb.d)\n"
    b"    zz = (a  # why\n"
    b"          if b else cb)\n"
    b"    w = a / b\n"
    b"    v = (a) @ (b or kw)\n"
    b"    u = a // b ** c\n"
    b"    t = a - b - c\n"
    b"    s = a + 1\n"
    b"    r = 1 + a\n"
    b"    o = a < b\n"
    b"    p = q = a\n"
    b"    x = b\n"
    b"    n = not a\n"
    b"    m = [a]\n"
    b"    obj.attr = a\n"
    b"    a2, b2 = a, b\n"
    b"    lam = lambda: (inner := a)\n"
    b"    comp = [k for k in (kk := a)]\n"
    b"    h = a if b else c if d else e\n"
    b"    g = a.b.c()\n"
    b"    g2 = a.b.c\n"
    b"    e2 = f(x) or g\n"
    b"    c2 = a ^ b | c & d\n"
    b"    i = ((a))\n"
    b"    j = (a, b)\n"
    b"    caf\xc3\xa9 = a or b\n"
    b"    \xff = a\n"
    b"    deep = " + b" or ".join([b"a"] * _DEEP) + b"\n"
    b"    nest = " + b"(" * _DEEP + b"a" + b")" * _DEEP + b"\n"
    b"    def nested():\n"
    b"        hidden = a\n"
    b"        return hidden\n"
    b"    class Inner:\n"
    b"        hidden2 = a or b\n"
    b"    for item in rest:\n"
    b"        while item:\n"
    b"            looped = item or kw\n"
    b"    return x\n"
    b"class K(Base):\n"
    b"    field = Base.default or None\n"
    b"    def method(self):\n"
    b"        me = self.field\n"
    b"        return me\n"
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


def _scopes(root: Node) -> Iterator[Node]:
    for node in _nodes(root):
        if node.type in cs.PY_NESTED_SCOPE_TYPES or node.type == cs.TS_PY_MODULE:
            yield node


@pytest.fixture
def bridge() -> native_trees.TreeBridge:
    native_trees._bridge.cache_clear()
    found = native_trees.alias_bridge()
    assert found is not None, "the extension carries the alias walk but it is off"
    return found


def _assert_twins_agree(bridge: native_trees.TreeBridge, node: Node) -> None:
    native = aliases_from_native(node, bridge.local_aliases(node))
    python = python_local_aliases(node)
    # Order counts: the expansion pass reads the aliases in this order.
    assert list(native.items()) == list(python.items()), node


@needs_alias_walk
class TestParity:
    def test_every_node_of_the_sample(self, bridge: native_trees.TreeBridge) -> None:
        compared = 0
        for node in _nodes(_parse(SAMPLE)):
            _assert_twins_agree(bridge, node)
            compared += 1
        assert compared > 500

    def test_the_sample_reaches_every_kind_of_alias(self) -> None:
        # Guards the sample itself: a parity test over aliases that are all
        # alike would pass for a walk that reads one form.
        root = _parse(SAMPLE)
        function = next(
            node
            for node in _nodes(root)
            if node.type == cs.TS_PY_FUNCTION_DEFINITION
            and (name := node.child_by_field_name(cs.FIELD_NAME)) is not None
            and name.text == b"f"
        )
        aliases = python_local_aliases(function)
        assert {"x", "y", "z", "zz", "w", "v", "s", "r", "q", "café"} <= set(aliases)
        assert not {"o", "n", "m", "a2", "hidden", "hidden2", "nest", "p"} & set(
            aliases
        )
        assert aliases["x"].candidates[0].text == b"b"
        assert aliases["w"].dunder == "__truediv__"
        assert [leaf.text for leaf in aliases["v"].reflected] == [b"b", b"kw"]
        assert aliases["r"].candidates == []
        assert len(aliases["deep"].candidates) < _DEEP
        assert python_local_aliases(root)["alias"].candidates

    def test_every_scope_of_cgr(self, bridge: native_trees.TreeBridge) -> None:
        # Real code: every module, def, class and lambda of the package.
        compared = 0
        found = 0
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            for node in _scopes(_parse(path.read_bytes())):
                _assert_twins_agree(bridge, node)
                compared += 1
                found += bool(python_local_aliases(node))
        assert compared > 10_000
        assert found > 1_000

    def test_the_walk_takes_the_bridge(
        self, bridge: native_trees.TreeBridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _parse(SAMPLE)
        expected = [python_local_aliases(node) for node in _scopes(root)]

        def refuse(_node: Node) -> NoReturn:
            raise AssertionError("the Python twin ran")

        monkeypatch.setattr(variable_analyzer, "python_local_aliases", refuse)
        assert [local_aliases(node) for node in _scopes(root)] == expected


@needs_alias_walk
def test_a_node_the_bridge_cannot_read_runs_the_twin(
    bridge: native_trees.TreeBridge,
) -> None:
    # A tree parsed from a callback keeps no `bytes` source, which the walk
    # reads names from; that one call runs the twin instead.
    source = SAMPLE
    tree = PARSERS[cs.SupportedLanguage.PYTHON].parse(
        lambda byte, _point: source[byte : byte + 1]
    )
    with pytest.raises(native_trees.NotBorrowed):
        bridge.local_aliases(tree.root_node)
    assert local_aliases(tree.root_node) == python_local_aliases(tree.root_node)


def test_the_walk_runs_the_twin_without_the_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_trees, "ENABLED", False)
    root = _parse(SAMPLE)
    for node in _scopes(root):
        assert local_aliases(node) == python_local_aliases(node)


@needs_alias_walk
def test_rules_from_before_the_alias_walk_still_build_the_bridge() -> None:
    # An older code-graph-rag hands over no `aliases` rules and never asks for
    # the walk; the rest of the bridge must still run for it.
    rules = {
        key: value for key, value in native_trees.rules().items() if key != "aliases"
    }
    bridge = getattr(RUST, cs.EG_RUST_TREE_BRIDGE)(rules)
    root = _parse(SAMPLE)
    assert bridge.inner_scope_only_names(root) is not None
    with pytest.raises(native_trees.NotBorrowed):
        bridge.local_aliases(root)
