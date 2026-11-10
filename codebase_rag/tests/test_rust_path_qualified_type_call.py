"""Issue #2982: a path-qualified Rust `Type::fn()` binds the type its path names.

The ambiguity guard of #2543 declines a `Type::fn()` whose simple type name
several project types share. The resolver reached it with the bare name,
having dropped the call's own path: `grep_searcher::BinaryDetection::convert`
names exactly the `searcher.rs` type (re-exported at the crate root), yet got
no edge once `line_buffer.rs` defined another `BinaryDetection`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests import test_incremental_duplicate_variants as incremental
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write
from evals.cgr_graph import _StatefulIngestor

FILES = {
    "Cargo.toml": (
        '[workspace]\nmembers = ["crates/searcher", "crates/app"]\nresolver = "2"\n'
    ),
    "crates/searcher/Cargo.toml": (
        '[package]\nname = "grep-searcher"\nversion = "0.1.0"\nedition = "2021"\n'
    ),
    "crates/searcher/src/lib.rs": (
        "pub mod ext_line_buffer;\npub mod ext_searcher;\n"
        "pub mod line_buffer;\npub mod searcher;\n"
        "pub use crate::searcher::BinaryDetection;\n"
    ),
    # Blocks written away from their types, named so that the wrong one
    # wins a tie on import distance.
    "crates/searcher/src/ext_line_buffer.rs": (
        "impl crate::line_buffer::BinaryDetection {\n    pub fn reset() {}\n}\n"
    ),
    "crates/searcher/src/ext_searcher.rs": (
        "impl crate::searcher::BinaryDetection {\n    pub fn reset() {}\n}\n"
    ),
    "crates/searcher/src/line_buffer.rs": "pub struct BinaryDetection(pub u8);\n",
    "crates/searcher/src/searcher.rs": (
        "pub struct BinaryDetection(u8);\n"
        "impl BinaryDetection {\n"
        "    pub fn convert(b: u8) -> BinaryDetection { BinaryDetection(b) }\n"
        "}\n\n"
        "pub fn in_crate() -> BinaryDetection {\n"
        "    crate::searcher::BinaryDetection::convert(1)\n"
        "}\n"
    ),
    "crates/app/Cargo.toml": (
        '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n\n'
        '[dependencies]\ngrep-searcher = { path = "../searcher" }\n'
    ),
    "crates/app/src/main.rs": (
        "use grep_searcher::BinaryDetection;\n\n"
        "fn fully_qualified() -> grep_searcher::BinaryDetection {\n"
        "    grep_searcher::BinaryDetection::convert(0)\n"
        "}\n\n"
        "fn imported() -> BinaryDetection {\n"
        "    BinaryDetection::convert(2)\n"
        "}\n\n"
        "fn out_of_module_impl() {\n"
        "    grep_searcher::BinaryDetection::reset();\n"
        "}\n\n"
        "fn main() {\n    let _ = fully_qualified();\n    let _ = imported();\n}\n"
    ),
    # No path and no import: the bare name is one of two types (#2941).
    "crates/app/src/bare.rs": "pub fn bare() {\n    let _ = BinaryDetection::convert(5);\n}\n",
    # The head is the caller's own module, which re-exports the type.
    "crates/app/src/local.rs": (
        "mod reexport;\n\n"
        "pub fn local_head() {\n"
        "    let _ = reexport::BinaryDetection::convert(3);\n"
        "}\n"
    ),
    "crates/app/src/local/reexport.rs": "pub use grep_searcher::BinaryDetection;\n",
    # `self::` in an inline mod counts from the mod, not from the file, whose
    # own `BinaryDetection` is another type.
    "crates/app/src/inline.rs": (
        "pub struct BinaryDetection;\n\n"
        "mod inner {\n"
        "    pub use grep_searcher::BinaryDetection;\n\n"
        "    pub fn relative() {\n"
        "        let _ = self::BinaryDetection::convert(4);\n"
        "    }\n"
        "}\n"
    ),
}

CONVERT = "crates.searcher.src.searcher.BinaryDetection.convert"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("rsambig") / "rsambig"
    for rel, text in FILES.items():
        _write(root, rel, text)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src == f"{prefix}{caller}"
    }


@pytest.mark.parametrize(
    "caller",
    [
        "crates.app.src.main.fully_qualified",
        "crates.app.src.main.imported",
        "crates.searcher.src.searcher.in_crate",
        "crates.app.src.local.local_head",
        "crates.app.src.inline.inner.relative",
    ],
    ids=[
        "crate-path",
        "use-imported",
        "crate-relative",
        "local-module-head",
        "inline-mod-self",
    ],
)
def test_a_path_qualified_type_call_binds_the_named_type(
    graph: RecordedGraph, caller: str
) -> None:
    assert _callees(graph, caller) == {CONVERT: "exact"}


def test_a_path_qualified_call_binds_the_block_on_the_named_type(
    graph: RecordedGraph,
) -> None:
    assert _callees(graph, "crates.app.src.main.out_of_module_impl") == {
        "crates.searcher.src.ext_searcher.BinaryDetection.reset": "exact"
    }


# Negative: what must not change.


def test_a_bare_call_to_a_shared_type_name_still_binds_nothing(
    graph: RecordedGraph,
) -> None:
    assert _callees(graph, "crates.app.src.bare.bare") == {}


# An incremental run that re-parses only the caller reads the unchanged impl
# blocks back from the graph. Their written self paths have to come back
# with them, or the block a path-qualified call names fails the self-path
# check and the edge a clean index emits is lost (PR #2993 review).

INCREMENTAL_CARGO = '[package]\nname = "demo"\nversion = "0.1.0"\n'
INCREMENTAL_CALLER = "src/caller.rs"
INCREMENTAL_CALLER_QN = f"{incremental.PROJECT}.src.caller.go"


def _incremental_files(ext: str, other: str, caller_body: str) -> dict[str, str]:
    return {
        "Cargo.toml": INCREMENTAL_CARGO,
        "src/lib.rs": "pub mod caller;\npub mod ext;\npub mod model;\npub mod other;\n",
        "src/model.rs": "pub struct Foo;\n",
        "src/other.rs": "pub struct Foo;\n" + other,
        "src/ext.rs": ext,
        INCREMENTAL_CALLER: f"pub fn go() {{\n{caller_body}}}\n",
    }


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (
            _incremental_files(
                "impl crate::model::Foo {\n    pub fn run() {}\n}\n",
                "impl Foo {\n    pub fn run() {}\n}\n",
                "    crate::model::Foo::run();\n",
            ),
            "src.ext.Foo.run",
        ),
        (
            _incremental_files(
                "use crate::model::Foo;\n\nimpl Foo {\n    pub fn run() {}\n}\n",
                "impl Foo {\n    pub fn run() {}\n}\n",
                "    crate::model::Foo::run();\n",
            ),
            "src.ext.Foo.run",
        ),
        # Both blocks register under `ext.Foo`: each method keeps its own
        # block's path, so `stop` is not the model's, and the `run` the
        # second block took as `run@7` is the other type's.
        (
            _incremental_files(
                "impl crate::model::Foo {\n    pub fn run() {}\n}\n\n"
                "impl crate::other::Foo {\n    pub fn stop() {}\n"
                "    pub fn run() {}\n}\n",
                "",
                "    crate::model::Foo::stop();\n    crate::other::Foo::run();\n",
            ),
            "src.ext.Foo.run@7",
        ),
    ],
    ids=[
        "block-written-with-the-path",
        "bare-block-under-a-use",
        "two-blocks-one-module",
    ],
)
def test_reparsing_only_the_caller_keeps_the_path_qualified_edge(
    temp_repo: Path, files: dict[str, str], expected: str
) -> None:
    root = temp_repo / incremental.PROJECT
    incremental._write(root, files)
    store = _StatefulIngestor()
    incremental._index(store, root, force=True)
    clean_calls = incremental._calls(store)
    assert incremental._calls_from(clean_calls, INCREMENTAL_CALLER_QN) == {
        (INCREMENTAL_CALLER_QN, f"{incremental.PROJECT}.{expected}", "exact")
    }, "fixture must bind the block on the named type"

    incremental._touch_after_cache(root / INCREMENTAL_CALLER, root, "//")
    incremental._index(store, root, force=False)

    assert incremental._calls(store) == clean_calls
