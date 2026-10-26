"""The Rust tree walks against the Python twins they replace.

`native_trees.TreeBridge` walks py-tree-sitter's own trees from Rust and
must return exactly what `python_anchor_hash` and `python_ast_fingerprint`
return. These parse sources in every language with a grammar installed, plus
cgr's own parser package, and compare the two on every named node; they
also pin when the bridge must step aside for the Python twin, and that the
py-tree-sitter and tree-sitter crate pins name the pair the bridge was
checked with (AGENTS.md, "Bumping tree-sitter").
"""

import importlib
import importlib.metadata
import tomllib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock

import pytest
from packaging.requirements import Requirement
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.embedded_graph.extension import rust_extension
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import native_trees
from codebase_rag.parsers.anchor_hash import anchor_hash, python_anchor_hash
from codebase_rag.parsers.ast_fingerprint import (
    compute_ast_fingerprint,
    python_ast_fingerprint,
)
from codebase_rag.parsers.cpp.preproc_recovery import parse_with_preproc_recovery

RUST = rust_extension()
ANCHOR_MODULE = importlib.import_module("codebase_rag.parsers.anchor_hash")
FINGERPRINT_MODULE = importlib.import_module("codebase_rag.parsers.ast_fingerprint")
PARSERS, _ = load_parsers()
PARSERS_DIR = Path(native_trees.__file__).parent
REPO_ROOT = Path(__file__).resolve().parents[2]
CRATE_DIR = REPO_ROOT / "rust" / "cgr_graph_rs"
BUMP_GUIDE = "see AGENTS.md, 'Bumping tree-sitter'"

# A build from before the bridge has none of it.
needs_bridge = pytest.mark.skipif(
    RUST is None or not hasattr(RUST, cs.EG_RUST_TREE_BRIDGE),
    reason="cgr_graph_rs with the tree bridge is not installed",
)

# One source per language, written to reach what the walks special-case:
# comments, punctuation, literals, wrapped (C++ template) and expression-
# bodied (C#) definitions, Dart's sibling bodies, Rust's macro_rules!.
SAMPLES: dict[cs.SupportedLanguage, str] = {
    cs.SupportedLanguage.PYTHON: (
        "@dec\nclass A(B):\n    def m(self, x=1):  # c\n"
        "        return [y for y in x if y] or f'{x}é'\n"
    ),
    cs.SupportedLanguage.JS: (
        "class A { m(x) { return x ?? `t${x}`; } }\n"
        "const f = (a, b) => { /* c */ return a + b; };\n"
    ),
    cs.SupportedLanguage.TS: (
        "export abstract class A<T> { m(x: T): T[] { return [x]; } }\n"
        "function g(a: number): string { return `${a}`; }\n"
    ),
    cs.SupportedLanguage.TSX: (
        "export function C({ a }: P) { return <div className='x'>{a}</div>; }\n"
    ),
    cs.SupportedLanguage.RUST: (
        "macro_rules! m { ($x:expr) => { $x + 1 }; }\n"
        "impl A { fn f(&self, x: u8) -> u8 { if x > 0 { m!(x) } else { 0 } } }\n"
    ),
    cs.SupportedLanguage.GO: (
        'func (s *S) F(x int) string { for i := 0; i < x; i++ { s.n++ }; return "é" }\n'
    ),
    cs.SupportedLanguage.JAVA: (
        "@A class C { @Override public int m(int x) { return x > 0 ? x : -x; } }\n"
    ),
    cs.SupportedLanguage.C: (
        "#define M(x) ((x) + 1)\nint f(int a) { /* c */ return M(a) * 2; }\n"
    ),
    cs.SupportedLanguage.CPP: (
        "template <typename T> T f(T a) { return a + T{1}; }\n"
        "class C { public: int m() const { return 'c'; } };\n"
    ),
    cs.SupportedLanguage.CSHARP: (
        "class C { int P => 1 + 2; void M(int x) { if (x > 0) { M(x - 1); } } }\n"
    ),
    cs.SupportedLanguage.PHP: (
        "<?php class C { function m($x) { return $x . 'é'; } }\n"
    ),
    cs.SupportedLanguage.LUA: (
        "local function f(a) -- c\n  return a .. 'x', nil end\n"
    ),
    cs.SupportedLanguage.SCALA: (
        "object O { def f(x: Int): Int = if (x > 0) x else -x }\n"
    ),
    cs.SupportedLanguage.JULIA: ("function f(x::Int)\n  return x + 1.5\nend\n"),
    cs.SupportedLanguage.DART: (
        "class A { int get g => 1; A(this.x) : super(); int m(int x) { return x; } }\n"
        "int top(int a) { return a * 2; }\n"
    ),
}


def _named_nodes(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.is_named:
            yield node


def _parse(language: cs.SupportedLanguage, source: bytes) -> Node:
    if language not in PARSERS:
        pytest.skip(f"{language} parser not available")
    return parse_with_preproc_recovery(PARSERS[language], source, language).root_node


def _assert_twins_agree(bridge: native_trees.TreeBridge, root: Node) -> int:
    assert bridge.describe(root) == native_trees.describe(root)
    compared = 0
    for node in _named_nodes(root):
        decorators = ("property", "é") if compared % 3 == 0 else ()
        assert bridge.anchor_hash(node, decorators) == python_anchor_hash(
            node, decorators
        ), node
        native = bridge.ast_fingerprint(node)
        python = python_ast_fingerprint(node)
        assert (None if native is None else tuple(native)) == (
            None if python is None else tuple(python)
        ), node
        compared += 1
    return compared


@pytest.fixture
def bridge() -> native_trees.TreeBridge:
    native_trees._bridge.cache_clear()
    found = native_trees.tree_bridge()
    assert found is not None, (
        "the extension carries the bridge but it is off; if py-tree-sitter or "
        f"the tree-sitter crate moved, {BUMP_GUIDE}"
    )
    return found


@needs_bridge
class TestParity:
    @pytest.mark.parametrize("language", list(SAMPLES), ids=str)
    def test_every_node_of_each_language(
        self, bridge: native_trees.TreeBridge, language: cs.SupportedLanguage
    ) -> None:
        root = _parse(language, SAMPLES[language].encode())
        assert _assert_twins_agree(bridge, root) > 0

    def test_cgr_parser_sources(self, bridge: native_trees.TreeBridge) -> None:
        # Real code, nested deeply enough to reach every branch of both walks.
        compared = 0
        for path in sorted(PARSERS_DIR.glob("*.py"))[:40]:
            root = _parse(cs.SupportedLanguage.PYTHON, path.read_bytes())
            compared += _assert_twins_agree(bridge, root)
        assert compared > 10_000

    def test_the_public_functions_take_the_bridge(
        self, bridge: native_trees.TreeBridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _parse(
            cs.SupportedLanguage.PYTHON, SAMPLES[cs.SupportedLanguage.PYTHON].encode()
        )
        expected = [
            (python_anchor_hash(node, ("d",)), python_ast_fingerprint(node))
            for node in _named_nodes(root)
        ]

        def refuse(*_args: Node | Sequence[str]) -> NoReturn:
            raise AssertionError("the Python twin ran")

        monkeypatch.setattr(ANCHOR_MODULE, "python_anchor_hash", refuse)
        monkeypatch.setattr(FINGERPRINT_MODULE, "python_ast_fingerprint", refuse)
        assert [
            (anchor_hash(node, ("d",)), compute_ast_fingerprint(node))
            for node in _named_nodes(root)
        ] == expected


@needs_bridge
class TestFallback:
    def test_a_node_it_cannot_read_runs_the_python_twin(
        self, bridge: native_trees.TreeBridge
    ) -> None:
        node = MagicMock(spec=Node)
        with pytest.raises(native_trees.NotBorrowed):
            bridge.anchor_hash(node, ())
        with pytest.raises(native_trees.NotBorrowed):
            bridge.ast_fingerprint(node)

    def test_a_source_that_is_not_bytes_runs_the_python_twin(
        self, bridge: native_trees.TreeBridge
    ) -> None:
        source = b"def f(x):\n    return x + 1\n"
        parser = PARSERS[cs.SupportedLanguage.PYTHON]
        tree = parser.parse(lambda byte, _point: source[byte : byte + 4])
        function = tree.root_node.children[0]
        with pytest.raises(native_trees.NotBorrowed):
            bridge.anchor_hash(function, ())
        assert anchor_hash(function) == python_anchor_hash(function)

    def test_decorators_it_cannot_read_run_the_python_twin(
        self, bridge: native_trees.TreeBridge
    ) -> None:
        root = _parse(cs.SupportedLanguage.PYTHON, b"def f(): pass\n")
        with pytest.raises(native_trees.NotBorrowed):
            bridge.anchor_hash(root, "not a list")
        assert anchor_hash(root, "ab") == python_anchor_hash(root, "ab")

    def test_switched_off_it_is_not_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_trees, "ENABLED", False)
        assert native_trees.tree_bridge() is None

    def test_another_py_tree_sitter_turns_it_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = importlib.metadata.version
        monkeypatch.setattr(
            importlib.metadata,
            "version",
            lambda name: "0.0.1" if name == "tree-sitter" else real(name),
        )
        native_trees._bridge.cache_clear()
        try:
            assert native_trees.tree_bridge() is None
        finally:
            native_trees._bridge.cache_clear()

    def test_rules_it_cannot_apply_turn_it_off(self) -> None:
        bridge_type = getattr(RUST, cs.EG_RUST_TREE_BRIDGE)
        wider_digest = native_trees.rules()
        wider_digest["digest_size"] = 16
        with pytest.raises(ValueError):
            bridge_type(wider_digest)
        with pytest.raises(KeyError):
            bridge_type({})

    def test_a_probe_that_fails_turns_it_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(_bridge: native_trees.TreeBridge) -> NoReturn:
            raise ImportError("no probe grammar")

        monkeypatch.setattr(native_trees, "_disagreement", fail)
        native_trees._bridge.cache_clear()
        try:
            assert native_trees.tree_bridge() is None
        finally:
            native_trees._bridge.cache_clear()

    def test_a_disagreeing_probe_turns_it_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_trees, "describe", lambda node: [])
        native_trees._bridge.cache_clear()
        try:
            assert native_trees.tree_bridge() is None
        finally:
            native_trees._bridge.cache_clear()


def _toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _exact_pin(name: str) -> str | None:
    """The version `pyproject.toml` pins `name` to exactly, if it does."""
    for dependency in _toml(REPO_ROOT / "pyproject.toml")["project"]["dependencies"]:
        requirement = Requirement(dependency)
        if requirement.name != name:
            continue
        specifiers = list(requirement.specifier)
        if len(specifiers) == 1 and specifiers[0].operator == "==":
            return specifiers[0].version
        return None
    return None


@needs_bridge
class TestVersionPair:
    """Both runtimes read one tree, so both pins must name the checked pair."""

    def test_pyproject_pins_the_py_tree_sitter_the_bridge_reads(self) -> None:
        checked = getattr(RUST, cs.EG_RUST_TREE_BRIDGE).PY_TREE_SITTER
        assert _exact_pin("tree-sitter") == checked, (
            f"pyproject.toml must pin tree-sitter=={checked}, the release the "
            f"bridge was checked against; {BUMP_GUIDE}"
        )

    def test_the_crate_pins_the_runtime_checked_with_it(self) -> None:
        checked = getattr(RUST, cs.EG_RUST_TREE_BRIDGE).TREE_SITTER_CRATE
        pinned = _toml(CRATE_DIR / "Cargo.toml")["dependencies"]["tree-sitter"]
        locked = [
            package["version"]
            for package in _toml(CRATE_DIR / "Cargo.lock")["package"]
            if package["name"] == "tree-sitter"
        ]
        assert (pinned, locked) == (f"={checked}", [checked]), (
            f"rust/cgr_graph_rs must pin and lock the tree-sitter crate at "
            f"exactly {checked}, the release checked to walk py-tree-sitter's "
            f"trees; {BUMP_GUIDE}"
        )
