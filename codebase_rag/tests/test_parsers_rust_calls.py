"""`RustCallResolution` and the macro-token call naming: the type an impl
block's methods are attributed to, the callee a macro's token stream spells,
the name a member call or macro invocation carries, and the calls a closure
leaves to its enclosing function."""

from __future__ import annotations

from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.rust.calls import RustCallResolution, macro_token_call_name
from codebase_rag.tests.collaborator_support import edges, first, index, nodes_of, parse
from codebase_rag.tests.rust_fixture import RUST_CRATE, function_named
from evals.recorded_graph import RecordingIngestor

_RS = cs.SupportedLanguage.RUST


@pytest.fixture
def strategy(temp_repo: Path, mock_ingestor: RecordingIngestor) -> RustCallResolution:
    updater = index(temp_repo / "rp", mock_ingestor, RUST_CRATE)
    calls = updater.factory.call_processor.calls_for(_RS)
    assert isinstance(calls, RustCallResolution)
    return calls


def test_the_run_functions_calls_bind_through_use_impl_and_macro(
    strategy: RustCallResolution, mock_ingestor: RecordingIngestor
) -> None:
    calls = {
        t
        for s, t in edges(mock_ingestor, cs.RelationshipType.CALLS)
        if s == "rp.src.lib.run"
    }
    assert calls == {
        "rp.src.shapes.Circle.new",
        "rp.src.shapes.Circle.area",
        # Inside `println!`, a call the grammar leaves as loose tokens.
        "rp.src.lib.util.helper",
        "rp.src.lib.local",
    }


def test_an_impl_block_is_named_by_the_type_it_implements(
    strategy: RustCallResolution,
) -> None:
    root = parse(_RS, RUST_CRATE["src/shapes.rs"])
    impls = nodes_of(root, cs.TS_IMPL_ITEM)
    assert {strategy.class_name(impl) for impl in impls} == {"Circle"}
    trait = first(root, "trait_item")
    assert strategy.class_name(trait) == "Area"


def test_a_closure_gets_no_caller_pass_of_its_own(
    strategy: RustCallResolution,
) -> None:
    root = parse(_RS, RUST_CRATE["src/lib.rs"])
    run = function_named(root, "run")
    closure = first(root, cs.TS_RS_CLOSURE_EXPRESSION)
    assert strategy.attributable([run, closure], "rp.src.lib") == [run]


def test_a_macro_and_a_field_call_are_named_by_their_own_shapes(
    strategy: RustCallResolution,
) -> None:
    root = parse(_RS, "fn f() { vec![1]; (self.cb)(); }\n")
    assert strategy.node_call_name(first(root, cs.TS_RS_MACRO_INVOCATION)) == "vec"
    field = first(root, cs.TS_RS_FIELD_EXPRESSION)
    assert strategy.function_field_name(field) == "self.cb"


def _macro_idents(source: str) -> list[Node]:
    root = parse(_RS, source)
    tree = first(root, "token_tree")
    return [n for n in nodes_of(tree, "identifier")]


def test_a_macro_token_call_spells_its_receiver_chain() -> None:
    idents = _macro_idents('fn f() { println!("{}", a.b().c(), Path::new(x)); }\n')
    names = {ident.text: macro_token_call_name(ident) for ident in idents}
    assert names[b"b"] == "a.b"
    assert names[b"c"] == "a.b().c"
    assert names[b"new"] == "Path::new"


def test_a_turbofish_reaches_its_call_only_when_args_follow() -> None:
    idents = _macro_idents("fn f() { assert!(make::<u64>() == a::<B>::c()); }\n")
    names = {ident.text: macro_token_call_name(ident) for ident in idents}
    assert names[b"make"] == "make"
    # `a::<B>::c` continues the path: `a` is no call.
    assert names[b"a"] is None
