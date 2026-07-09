# (H) `macro_rules!` definitions were invisible: invocation SITES were captured
# (H) (macro_invocation is in SPEC_RS_CALL_TYPES) but there was no definition
# (H) node to bind to, so `square!(3)` could never resolve to first-party code.
# (H) Macros register as Function nodes (the cross-language decision: C/C++/Rust
# (H) macros all map onto Function); invocations then resolve like any call and
# (H) dead-code treats macros like any function.
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
    # (H) Rust macros and functions live in SEPARATE namespaces: `write!(f, ..)`
    # (H) (std prelude, no use statement) must not bind a same-module `fn write`
    # (H) (a false edge that revives dead code), and `write(buf)` must not bind
    # (H) a same-module `macro_rules! write`-alike either.
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


def test_macro_export_attribute_marks_exported(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) macro_rules! takes no `pub`; #[macro_export] is what publishes it (to
    # (H) the crate root) as library API -- without is_exported an exported but
    # (H) internally-uninvoked macro would report dead.
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
    assert exported[cs.KEY_IS_EXPORTED] is True, exported
    assert private[cs.KEY_IS_EXPORTED] is False, private
