"""Issue #2982: a path-qualified Rust `Type::fn()` binds the type its path names.

The ambiguity guard of #2543 declines a `Type::fn()` whose simple type name
several project types share. The resolver reached it with the bare name,
having dropped the call's own path: `grep_searcher::BinaryDetection::convert`
names exactly the `searcher.rs` type (re-exported at the crate root), yet got
no edge once `line_buffer.rs` defined another `BinaryDetection`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

FILES = {
    "Cargo.toml": (
        '[workspace]\nmembers = ["crates/searcher", "crates/app"]\nresolver = "2"\n'
    ),
    "crates/searcher/Cargo.toml": (
        '[package]\nname = "grep-searcher"\nversion = "0.1.0"\nedition = "2021"\n'
    ),
    "crates/searcher/src/lib.rs": (
        "pub mod line_buffer;\npub mod searcher;\n"
        "pub use crate::searcher::BinaryDetection;\n"
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
        "fn main() {\n    let _ = fully_qualified();\n    let _ = imported();\n}\n"
    ),
    # No path and no import: the bare name is one of two types (#2941).
    "crates/app/src/bare.rs": "pub fn bare() {\n    let _ = BinaryDetection::convert(5);\n}\n",
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
    ],
    ids=["crate-path", "use-imported", "crate-relative"],
)
def test_a_path_qualified_type_call_binds_the_named_type(
    graph: RecordedGraph, caller: str
) -> None:
    assert _callees(graph, caller) == {CONVERT: "exact"}


# Negative: what must not change.


def test_a_bare_call_to_a_shared_type_name_still_binds_nothing(
    graph: RecordedGraph,
) -> None:
    assert _callees(graph, "crates.app.src.bare.bare") == {}
