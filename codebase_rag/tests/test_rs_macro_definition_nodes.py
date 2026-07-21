# `macro_rules!` definitions were invisible: invocation SITES were captured
# (macro_invocation is in SPEC_RS_CALL_TYPES) but there was no definition
# node to bind to, so `square!(3)` could never resolve to first-party code.
# Macros register as Function nodes (the cross-language decision: C/C++/Rust
# macros all map onto Function); invocations then resolve like any call and
# dead-code treats macros like any function.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_macro_rules_registers_function_and_invocation_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "rsmacro"
    src = root / "src"
    src.mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "rsmacro"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (src / "lib.rs").write_text(
        "macro_rules! square {\n"
        "    ($x:expr) => { $x * $x };\n"
        "}\n"
        "pub fn use_it() -> i32 {\n"
        "    square!(3)\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="rust")
    functions = {
        c.args[1][cs.KEY_QUALIFIED_NAME]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.FUNCTION
    }
    assert any(qn.endswith(".square") for qn in functions), sorted(functions)
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(f.endswith(".use_it") and t.endswith(".square") for f, t in calls), (
        sorted(t for _, t in calls)
    )


def test_macro_and_fn_namespaces_do_not_cross_bind(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Rust macros and functions live in SEPARATE namespaces: `write!(f, ..)`
    # (std prelude, no use statement) must not bind a same-module `fn write`
    # (a false edge that revives dead code), and `write(buf)` must not bind
    # a same-module `macro_rules! write`-alike either.
    root = temp_repo / "rsns"
    src = root / "src"
    src.mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "rsns"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (src / "lib.rs").write_text(
        "use std::fmt;\n"
        "pub fn write(buf: &[u8]) -> usize {\n"
        "    buf.len()\n"
        "}\n"
        "macro_rules! trace {\n"
        "    ($x:expr) => { $x };\n"
        "}\n"
        "pub struct W;\n"
        "impl fmt::Display for W {\n"
        "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {\n"
        '        write!(f, "w")\n'
        "    }\n"
        "}\n"
        "pub fn use_fn(x: i32) -> i32 {\n"
        "    trace(x)\n"
        "}\n"
        "pub fn use_macro(x: i32) -> i32 {\n"
        "    trace!(x)\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="rust")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert not any(f.endswith(".fmt") and t.endswith(".write") for f, t in calls), (
        "std write! macro bound the first-party fn write"
    )
    assert not any(f.endswith(".use_fn") and t.endswith(".trace") for f, t in calls), (
        "fn-namespace call bound the first-party macro"
    )
    assert any(f.endswith(".use_macro") and t.endswith(".trace") for f, t in calls), (
        sorted(calls)
    )


def test_incremental_reparse_keeps_cross_file_macro_call(temp_repo: Path) -> None:
    # Incremental runs rehydrate function_registry from the graph, but the
    # macro-namespace gate consults macro_qns, which only definition
    # ingest populated. A re-parsed file invoking a macro from an UNCHANGED
    # file would resolve the macro qn, see it missing from macro_qns, and
    # drop the CALLS edge -- the macro then reports dead only on
    # incremental runs. Macro nodes persist is_macro and rehydration
    # restores it (the is_property pattern).
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers
    from evals.cgr_graph import _StatefulIngestor

    src = temp_repo / "src"
    src.mkdir(parents=True)
    (temp_repo / "Cargo.toml").write_text(
        f'[package]\nname = "{temp_repo.name}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (src / "util.rs").write_text(
        "#[macro_export]\nmacro_rules! square {\n    ($x:expr) => { $x * $x };\n}\n",
        encoding="utf-8",
    )
    lib_src = (
        "#[macro_use]\nmod util;\n"
        "use crate::util::square;\n"
        "pub fn use_it() -> i32 {\n"
        "    square!(3)\n"
        "}\n"
    )
    (src / "lib.rs").write_text(lib_src, encoding="utf-8")

    def _calls(store: _StatefulIngestor) -> set[tuple[str, str]]:
        return {
            (str(f), str(t))
            for _, f, rel, _, t in store.edges
            if rel == cs.RelationshipType.CALLS.value
        }

    def _index(store: _StatefulIngestor, force: bool) -> None:
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=store, repo_path=temp_repo, parsers=parsers, queries=queries
        ).run(force=force)

    store = _StatefulIngestor()
    _index(store, force=True)
    expected = [
        (f, t)
        for f, t in _calls(store)
        if f.endswith(".use_it") and t.endswith(".square")
    ]
    assert expected, sorted(_calls(store))

    # touch only the CALLING file; util.rs stays rehydration-only
    (src / "lib.rs").write_text(lib_src + "// touched\n", encoding="utf-8")
    _index(store, force=False)
    assert any(
        f.endswith(".use_it") and t.endswith(".square") for f, t in _calls(store)
    ), "incremental run dropped the cross-file macro CALLS edge"


def test_macro_export_attribute_marks_exported(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # macro_rules! takes no `pub`; #[macro_export] is what publishes it (to
    # the crate root) as library API -- without is_exported an exported but
    # internally-uninvoked macro would report dead.
    root = temp_repo / "rsexport"
    src = root / "src"
    src.mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "rsexport"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (src / "lib.rs").write_text(
        "#[macro_export]\n"
        "macro_rules! pub_square {\n"
        "    ($x:expr) => { $x * $x };\n"
        "}\n"
        "macro_rules! private_square {\n"
        "    ($x:expr) => { $x * $x };\n"
        "}\n"
        "#[macro_export]\n"
        "/// commented macro (doc comments are named siblings between the\n"
        "/// attribute and the definition)\n"
        "macro_rules! commented_square {\n"
        "    ($x:expr) => { $x * $x };\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="rust")
    props = {
        c.args[1][cs.KEY_QUALIFIED_NAME]: c.args[1]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.FUNCTION
    }
    exported = next(v for k, v in props.items() if k.endswith(".pub_square"))
    private = next(v for k, v in props.items() if k.endswith(".private_square"))
    commented = next(v for k, v in props.items() if k.endswith(".commented_square"))
    assert exported[cs.KEY_IS_EXPORTED] is True, exported
    assert private[cs.KEY_IS_EXPORTED] is False, private
    assert commented[cs.KEY_IS_EXPORTED] is True, commented
