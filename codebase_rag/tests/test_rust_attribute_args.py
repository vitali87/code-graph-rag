"""Rust attribute arguments are not call sites (issue #2541).

tree-sitter-rust gives an attribute's arguments the same `token_tree` node a
macro body gets, so the macro-internal call pattern (`ident` followed by a
`(...)` group) also matched `skip(self)` in `#[instrument(skip(self))]` and
`all(..)`/`not(..)` in `#[cfg(all(not(test)))]`. Each bound heuristically to
any same-named project function, from the enclosing module, even when the
function was private to another module and never imported.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Language, Node, Parser

from codebase_rag import constants as cs
from codebase_rag.parsers.rs.utils import in_attribute_arguments
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import PropertyDict
from evals.dead_code import cgr_dead_code, default_dead_code_config

try:
    import tree_sitter_rust as tsrust

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

_CARGO = '[package]\nname = "rsattr"\nversion = "0.1.0"\n'

_UTIL = (
    "pub fn skip(n: usize) -> usize { n + 1 }\n"
    "pub fn all(v: &[bool]) -> bool { v.iter().all(|b| *b) }\n"
    "pub fn not(b: bool) -> bool { !b }\n"
)


def _write(project: Path, files: dict[str, str]) -> None:
    for rel_path, source in files.items():
        target = project / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")


def _index(project: Path, mock_ingestor: MagicMock, files: dict[str, str]) -> None:
    _write(project, {"Cargo.toml": _CARGO, **files})
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str, str]]:
    return {
        (str(c.args[0][0]), str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == cs.RelationshipType.CALLS
    }


def _callers_of(mock_ingestor: MagicMock, callee_suffix: str) -> set[str]:
    return {
        caller
        for _, caller, callee in _calls(mock_ingestor)
        if callee.endswith(callee_suffix)
    }


def _dead_code_repo(tmp_path: Path) -> Path:
    root = tmp_path / "rsattr_dead"
    _write(
        root,
        {
            "Cargo.toml": _CARGO,
            "src/main.rs": (
                "mod frame;\n"
                "mod server;\n"
                "\n"
                "#[tokio::main]\n"
                "async fn main() {\n"
                "    server::Handler { n: 1 }.run();\n"
                "}\n"
            ),
            "src/frame.rs": "fn skip(n: usize) -> usize { n + 1 }\n",
            "src/server.rs": (
                "use tracing::instrument;\n"
                "\n"
                "pub struct Handler { pub n: usize }\n"
                "\n"
                "impl Handler {\n"
                "    #[instrument(skip(self))]\n"
                "    pub fn run(&self) -> usize { self.n }\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    fn fixture() -> usize { 3 }\n"
                "\n"
                "    #[test]\n"
                "    fn plain() { assert_eq!(fixture(), 3); }\n"
                "\n"
                "    #[tokio::test]\n"
                "    async fn scoped() {}\n"
                "}\n"
            ),
        },
    )
    return root


def _node_props(
    mock_ingestor: MagicMock, label: cs.NodeLabel, qn_suffix: str
) -> PropertyDict:
    for c in mock_ingestor.ensure_node_batch.call_args_list:
        node_label, props = c.args
        if node_label == label and str(props.get(cs.KEY_QUALIFIED_NAME, "")).endswith(
            qn_suffix
        ):
            return props
    raise AssertionError(f"no {label} node ending in {qn_suffix}")


class TestAttributeArgumentsAreNotCalls:
    def test_issue_repro_records_no_calls(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The issue's repro verbatim: app.rs never imports util, and nothing
        # in either file calls anything, so the graph must hold no CALLS.
        project = temp_repo / "rsattr"
        _index(
            project,
            mock_ingestor,
            {
                "src/lib.rs": "pub mod app;\npub mod util;\n",
                "src/util.rs": _UTIL,
                "src/app.rs": (
                    "use tracing::instrument;\n"
                    "\n"
                    "#[derive(Debug, Clone)]\n"
                    "pub struct App { n: usize }\n"
                    "\n"
                    "impl App {\n"
                    "    #[instrument(skip(self))]\n"
                    "    pub fn step(&self, k: usize) -> usize { self.n + k }\n"
                    "\n"
                    '    #[cfg(all(not(test), target_os = "linux"))]\n'
                    "    pub fn only_linux(&self) -> usize { self.n }\n"
                    "}\n"
                ),
            },
        )
        calls = _calls(mock_ingestor)
        assert calls == set(), f"attribute arguments indexed as calls: {sorted(calls)}"

    def test_instrument_skip_keeps_only_the_real_callers(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The mini-redis shape: a crate-private `frame::skip` with two real
        # callers, and `#[instrument(skip(self))]` on methods elsewhere.
        project = temp_repo / "rsattr_frame"
        _index(
            project,
            mock_ingestor,
            {
                "src/lib.rs": "pub mod frame;\npub mod server;\n",
                "src/frame.rs": (
                    "pub struct Frame;\n"
                    "\n"
                    "impl Frame {\n"
                    "    pub fn check(n: usize) -> usize { skip(n, 2) }\n"
                    "    pub fn parse(n: usize) -> usize { skip(n, 1) }\n"
                    "}\n"
                    "\n"
                    "fn skip(n: usize, by: usize) -> usize { n + by }\n"
                ),
                "src/server.rs": (
                    "use tracing::instrument;\n"
                    "\n"
                    "pub struct Handler { n: usize }\n"
                    "\n"
                    "impl Handler {\n"
                    "    #[instrument(skip(self))]\n"
                    "    pub fn run(&self) -> usize { self.n }\n"
                    "\n"
                    '    #[instrument(level = "debug", skip(self, dst))]\n'
                    "    pub fn apply(&self, dst: usize) -> usize { self.n + dst }\n"
                    "}\n"
                ),
            },
        )
        callers = _callers_of(mock_ingestor, ".frame.skip")
        assert {c.removeprefix("rsattr_frame.src.") for c in callers} == {
            "frame.Frame.check",
            "frame.Frame.parse",
        }, f"skip() callers: {sorted(callers)}"

    def test_cfg_combinators_on_a_function_are_not_calls(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project = temp_repo / "rsattr_cfg"
        _index(
            project,
            mock_ingestor,
            {
                "src/lib.rs": "pub mod util;\npub mod gated;\n",
                "src/util.rs": _UTIL,
                "src/gated.rs": (
                    "use crate::util::{all, not};\n"
                    "\n"
                    '#[cfg(any(all(unix, not(test)), target_os = "wasi"))]\n'
                    "pub fn unix_only() -> u8 { 1 }\n"
                ),
            },
        )
        calls = _calls(mock_ingestor)
        assert not _callers_of(mock_ingestor, ".util.all"), sorted(calls)
        assert not _callers_of(mock_ingestor, ".util.not"), sorted(calls)

    def test_inner_attribute_arguments_are_not_calls(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `#![...]` is an inner_attribute_item with the same token_tree shape.
        project = temp_repo / "rsattr_inner"
        _index(
            project,
            mock_ingestor,
            {
                "src/lib.rs": (
                    '#![cfg_attr(not(feature = "std"), no_std)]\n'
                    "\n"
                    "pub fn not(b: bool) -> bool { !b }\n"
                ),
            },
        )
        calls = _calls(mock_ingestor)
        assert not _callers_of(mock_ingestor, ".not"), sorted(calls)

    def test_cfg_attr_derive_is_not_a_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # A nested `derive(...)` inside `cfg_attr` sits in a token_tree too.
        project = temp_repo / "rsattr_cfg_attr"
        _index(
            project,
            mock_ingestor,
            {
                "src/lib.rs": (
                    '#[cfg_attr(feature = "serde", derive(Serialize))]\n'
                    "pub struct Point { x: i32 }\n"
                    "\n"
                    "pub fn derive() -> u8 { 0 }\n"
                ),
            },
        )
        calls = _calls(mock_ingestor)
        assert not _callers_of(mock_ingestor, ".derive"), sorted(calls)

    def test_attribute_on_a_statement_is_not_a_call_from_the_function(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # An attribute inside a body lies within the function's span, so the
        # edge used to come from the function rather than the module.
        project = temp_repo / "rsattr_stmt"
        _index(
            project,
            mock_ingestor,
            {
                "src/lib.rs": (
                    "pub fn not(b: bool) -> bool { !b }\n"
                    "\n"
                    "pub fn caller() -> u8 {\n"
                    "    #[cfg(not(test))]\n"
                    "    let x = 1;\n"
                    "    #[cfg(test)]\n"
                    "    let x = 2;\n"
                    "    x\n"
                    "}\n"
                ),
            },
        )
        calls = _calls(mock_ingestor)
        assert not _callers_of(mock_ingestor, ".not"), sorted(calls)

    def test_attributes_written_in_a_macro_body_are_not_calls(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # A macro body keeps the attributes it emits as raw tokens (`#` then a
        # `[...]` group), so no `attribute` node marks them; the real call
        # beside them in the same arm is still a call.
        project = temp_repo / "rsattr_macro_attr"
        _index(
            project,
            mock_ingestor,
            {
                "src/lib.rs": (
                    "pub fn not(b: bool) -> bool { !b }\n"
                    "pub fn skip(n: usize) -> usize { n }\n"
                    "pub fn helper(n: usize) -> usize { n }\n"
                    "\n"
                    "macro_rules! handler {\n"
                    "    ($name:ident) => {\n"
                    "        #[cfg(not(test))]\n"
                    "        #[instrument(skip(self))]\n"
                    "        pub fn $name(n: usize) -> usize { helper(n) }\n"
                    "    };\n"
                    "}\n"
                ),
            },
        )
        calls = _calls(mock_ingestor)
        assert not _callers_of(mock_ingestor, ".not"), sorted(calls)
        assert not _callers_of(mock_ingestor, ".skip"), sorted(calls)
        assert any(
            c.endswith(".handler") for c in _callers_of(mock_ingestor, ".helper")
        ), sorted(calls)

    def test_function_named_only_by_attributes_is_reported_dead(
        self, tmp_path: Path
    ) -> None:
        # The attribute "callers" kept a never-called private fn alive.
        dead = cgr_dead_code(
            _dead_code_repo(tmp_path), "proj", default_dead_code_config(True, False)
        )
        assert "proj.src.frame.skip" in dead, sorted(dead)


class TestNeighbouringBehaviourIsUnchanged:
    def test_same_named_real_call_in_the_body_still_resolves(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `skip` appears both in the attribute and as a real, imported call
        # in the body: only the body call is an edge, from the method.
        project = temp_repo / "rsattr_real_call"
        _index(
            project,
            mock_ingestor,
            {
                "src/lib.rs": "pub mod app;\npub mod util;\n",
                "src/util.rs": _UTIL,
                "src/app.rs": (
                    "use crate::util::skip;\n"
                    "use tracing::instrument;\n"
                    "\n"
                    "pub struct App { n: usize }\n"
                    "\n"
                    "impl App {\n"
                    "    #[instrument(skip(self))]\n"
                    "    pub fn step(&self) -> usize { skip(self.n) }\n"
                    "}\n"
                ),
            },
        )
        edges = {
            (label, caller)
            for label, caller, callee in _calls(mock_ingestor)
            if callee.endswith(".util.skip")
        }
        assert len(edges) == 1, sorted(edges)
        ((label, caller),) = edges
        assert label == cs.NodeLabel.METHOD
        assert caller.endswith(".app.App.step")

    def test_calls_inside_macro_bodies_are_still_captured(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The macro-internal pattern exists for these: a call at the top of a
        # macro's token_tree and one nested a group deeper.
        project = temp_repo / "rsattr_macro_body"
        _index(
            project,
            mock_ingestor,
            {
                "src/lib.rs": (
                    "pub fn describe(x: i32) -> i32 { x }\n"
                    "pub fn inner(x: i32) -> i32 { x }\n"
                    "pub fn outer(x: i32) -> i32 { x }\n"
                    "\n"
                    "pub fn caller() {\n"
                    '    println!("{}", describe(5));\n'
                    '    println!("{}", outer(inner(1)));\n'
                    "}\n"
                ),
            },
        )
        for name in ("describe", "inner", "outer"):
            callers = _callers_of(mock_ingestor, f".{name}")
            assert any(c.endswith(".caller") for c in callers), (
                name,
                sorted(_calls(mock_ingestor)),
            )

    def test_macro_invocation_and_macro_rules_body_calls_are_unchanged(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # A project macro invoked in a body stays a call, and so does a call
        # written in a macro_rules! arm (its body is a token_tree as well).
        project = temp_repo / "rsattr_macro_rules"
        _index(
            project,
            mock_ingestor,
            {
                "src/lib.rs": (
                    "pub fn helper(x: i32) -> i32 { x }\n"
                    "\n"
                    "macro_rules! twice {\n"
                    "    ($x:expr) => { helper($x) + helper($x) };\n"
                    "}\n"
                    "\n"
                    "pub fn caller() -> i32 { twice!(3) }\n"
                ),
            },
        )
        assert any(
            c.endswith(".caller") for c in _callers_of(mock_ingestor, ".twice")
        ), sorted(_calls(mock_ingestor))
        assert any(
            c.endswith(".twice") for c in _callers_of(mock_ingestor, ".helper")
        ), sorted(_calls(mock_ingestor))

    def test_derive_and_test_attributes_are_still_recorded(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Attributes reach the graph as decorator text, which dead-code reads
        # to root test functions; that path does not go through calls.
        project = temp_repo / "rsattr_decorators"
        _index(
            project,
            mock_ingestor,
            {
                "src/main.rs": (
                    "#[derive(Debug, Clone)]\n"
                    "pub struct App { n: usize }\n"
                    "\n"
                    "#[tokio::main]\n"
                    "async fn main() {}\n"
                    "\n"
                    "#[cfg(test)]\n"
                    "mod tests {\n"
                    "    #[test]\n"
                    "    fn plain() {}\n"
                    "\n"
                    "    #[tokio::test]\n"
                    "    async fn scoped() {}\n"
                    "}\n"
                ),
            },
        )
        app = _node_props(mock_ingestor, cs.NodeLabel.CLASS, ".App")
        assert app.get(cs.KEY_DECORATORS) == ["#[derive(Debug, Clone)]"]
        main = _node_props(mock_ingestor, cs.NodeLabel.FUNCTION, ".main")
        assert main.get(cs.KEY_DECORATORS) == ["#[tokio::main]"]
        for name, attribute in (("plain", "#[test]"), ("scoped", "#[tokio::test]")):
            props = _node_props(mock_ingestor, cs.NodeLabel.FUNCTION, f".tests.{name}")
            assert props.get(cs.KEY_DECORATORS) == [attribute]
        assert _calls(mock_ingestor) == set(), sorted(_calls(mock_ingestor))

    def test_test_and_entry_attributes_still_root_dead_code(
        self, tmp_path: Path
    ) -> None:
        dead = cgr_dead_code(
            _dead_code_repo(tmp_path), "proj", default_dead_code_config(True, False)
        )
        for alive in (
            "proj.src.main.main",
            "proj.src.server.Handler.run",
            "proj.src.server.tests.plain",
            "proj.src.server.tests.scoped",
            "proj.src.server.tests.fixture",
        ):
            assert alive not in dead, (alive, sorted(dead))


def _identifiers(node: Node) -> list[Node]:
    found = [node] if node.type == cs.TS_IDENTIFIER else []
    for child in node.children:
        found.extend(_identifiers(child))
    return found


def _flagged(source: str) -> dict[str, bool]:
    parser = Parser(Language(tsrust.language()))
    root = parser.parse(source.encode()).root_node
    return {
        ident.text.decode(): in_attribute_arguments(ident)
        for ident in _identifiers(root)
        if ident.text is not None
    }


@pytest.mark.skipif(not RUST_AVAILABLE, reason="tree-sitter-rust not installed")
class TestInAttributeArguments:
    @pytest.mark.parametrize(
        ("source", "callee"),
        [
            ("#[instrument(skip(self))]\nfn f() {}\n", "skip"),
            ('#[cfg(all(not(test), target_os = "linux"))]\nfn f() {}\n', "all"),
            ('#[cfg(all(not(test), target_os = "linux"))]\nfn f() {}\n', "not"),
            ('#![cfg_attr(not(feature = "std"), no_std)]\n', "not"),
            ("fn f() { #[cfg(not(test))] let x = 1; }\n", "not"),
            ("macro_rules! m { () => { #[cfg(not(test))] fn f() {} }; }\n", "not"),
            ("macro_rules! m { () => { #![allow(not(x))] }; }\n", "not"),
        ],
    )
    def test_attribute_tokens_are_flagged(self, source: str, callee: str) -> None:
        flagged = _flagged(source)
        assert flagged[callee] is True, flagged

    @pytest.mark.parametrize(
        ("source", "callee"),
        [
            ('fn f() { println!("{}", skip(1)); }\n', "skip"),
            ('fn f() { println!("{}", outer(skip(1))); }\n', "skip"),
            ('fn f() { println!("{:?}", vec![skip(1)]); }\n', "skip"),
            ('fn f() { println!("{}", xs[skip(1)]); }\n', "skip"),
            ("fn f() { quote! { #skip(1) }; }\n", "skip"),
            ("macro_rules! m { () => { skip(1) }; }\n", "skip"),
            ('#[doc = include_str!("x")]\nfn f() {}\n', "include_str"),
        ],
    )
    def test_macro_tokens_are_not_flagged(self, source: str, callee: str) -> None:
        flagged = _flagged(source)
        assert flagged[callee] is False, flagged
