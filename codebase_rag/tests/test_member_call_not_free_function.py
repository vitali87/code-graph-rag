"""A Rust or C++ member call never binds to a free function.

`recv.name()` / `p->name()` can only call a method. When cgr could not type
the receiver, its name fallbacks still offered free functions, so
`s.is_none()` bound a `#[test] fn is_none` in another crate and
`h->swap(x)` the file's namespace-level `swap` (issue #3176; helix 404,
ripgrep 315, aria2 282 such edges).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from evals.cgr_graph import _StatefulIngestor

_RUST = {
    "Cargo.toml": '[package]\nname = "rsm2f"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": """\
pub mod util;

pub struct Stats;
pub struct SearchResult;

impl SearchResult {
    pub fn stats(&self) -> Option<Stats> { None }
}

pub struct Writer;

impl Writer {
    pub fn flush(&self) {}
}

fn stats(n: usize) -> Option<Stats> { if n > 0 { None } else { None } }

pub fn run(r: &SearchResult, digits: &str) -> bool {
    let s = r.stats();
    let _ = stats(digits.len());
    s.is_none() && digits.len() > 0
}

pub fn drain() {
    let w = external::make();
    w.flush();
}

pub fn path_call() -> u32 {
    util::helper()
}
""",
    "src/util.rs": "pub fn helper() -> u32 {\n    1\n}\n",
    "tests/basic.rs": "#[test]\nfn is_none() {}\n\n#[test]\nfn len() {}\n",
}
_CPP = {
    "a.cpp": """\
void swap(int& a, int& b) { int t = a; a = b; b = t; }
int data() { return 1; }
namespace ns { int helper() { return 2; } }
struct Holder { int v; };

void use(Holder* h, Holder obj, int x, int y) {
    h->swap(x);
    obj.data();
    swap(x, y);
    ns::helper();
}
""",
    # C++ calls a free function through a callable data member just as C
    # does: a function-pointer field or a `std::function`.
    "cb.cpp": """\
#include <functional>

int open_dev() { return 37; }
int on_close() { return 0; }
struct Ops { int (*open_dev)(); std::function<int()> on_close; };

int start(Ops* o) { return o->open_dev(); }
int stop(Ops& o) { return o.on_close(); }
""",
    "ops.c": """\
int open_dev(void) { return 0; }
struct ops { int (*open_dev)(void); };

int start(struct ops* o) {
    return o->open_dev();
}
""",
}

_Calls = dict[tuple[str, str], str]


def _calls(root: Path, files: dict[str, str], grammar: str) -> _Calls:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing=grammar)
    out: _Calls = {}
    for c in get_relationships(mock, cs.RelationshipType.CALLS):
        props = c.kwargs.get("properties") or {}
        key = (str(c.args[0][2]).split(".", 1)[1], str(c.args[2][2]).split(".", 1)[1])
        out[key] = str(props.get(cs.KEY_RESOLUTION))
    return out


@pytest.fixture(scope="module")
def rust(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("rs3176") / "rsm2f", _RUST, "rust")


@pytest.fixture(scope="module")
def cpp(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("cpp3176") / "cxx", _CPP, "cpp")


def test_a_rust_method_call_skips_free_functions(rust: _Calls) -> None:
    callees = {to for (src, to) in rust if src == "src.lib.run"}
    assert not callees & {"tests.basic.is_none", "tests.basic.len"}, rust


def test_a_cpp_member_call_skips_free_functions(cpp: _Calls) -> None:
    callees = {to for (src, to) in cpp if src == "a.use"}
    assert "a.data" not in callees, cpp
    assert cpp.get(("a.use", "a.swap")) == "exact", cpp


@pytest.mark.parametrize(
    ("caller", "callee", "resolution"),
    [
        ("src.lib.run", "src.lib.SearchResult.stats", "exact"),
        ("src.lib.run", "src.lib.stats", "exact"),
        ("src.lib.path_call", "src.util.helper", "exact"),
        ("src.lib.drain", "src.lib.Writer.flush", "heuristic"),
    ],
    ids=["typed-method", "bare-free-fn", "path-free-fn", "untyped-to-a-method"],
)
def test_rust_calls_that_can_reach_their_target_still_do(
    rust: _Calls, caller: str, callee: str, resolution: str
) -> None:
    # Negatives: a typed call, a bare or path call to a free function, and an
    # untyped member call to a same-named method keep their edges.
    assert rust.get((caller, callee)) == resolution, rust


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("a.use", "a.ns.helper"),
        ("ops.start", "ops.open_dev"),
        ("cb.start", "cb.open_dev"),
        ("cb.stop", "cb.on_close"),
    ],
    ids=[
        "qualified-free-fn",
        "c-function-pointer-table",
        "cpp-function-pointer-field",
        "cpp-std-function-field",
    ],
)
def test_cpp_and_c_calls_that_can_reach_a_free_function_still_do(
    cpp: _Calls, caller: str, callee: str
) -> None:
    # Negatives: `ns::helper()` names a free function, and `o->open_dev()`
    # through a function-pointer table names one too, in C and in C++.
    assert (caller, callee) in cpp, cpp


_OPS_HEADER = """\
#include <functional>

struct Ops { int (*cb)(); std::function<int()> fn; };
"""
_OPS_CALLER = """\
#include "ops.h"

int cb() { return 1; }
int fn() { return 2; }
int other() { return 3; }

int run(Ops* o) { return o->cb() + o->fn() + o->other(); }
"""


def _stored_calls(store: _StatefulIngestor, caller: str) -> set[str]:
    return {
        str(target)
        for _, source, rel, _, target in store.edges
        if rel == cs.RelationshipType.CALLS.value and str(source) == caller
    }


def test_an_incremental_run_keeps_callable_members_of_an_unchanged_header(
    tmp_path: Path,
) -> None:
    # Re-parsing only the caller leaves the struct's header to rehydration
    # from the graph, so its callable members have to survive the round trip
    # (#3220 review). `o->other()` names no callable member and stays unbound.
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.CPP not in parsers:
        pytest.skip("cpp parser not available")
    root = tmp_path / "cbinc"
    root.mkdir()
    (root / "ops.h").write_text(_OPS_HEADER, encoding="utf-8")
    (root / "main.cpp").write_text(_OPS_CALLER, encoding="utf-8")
    expected = {"cbinc.main.cb", "cbinc.main.fn"}
    store = _StatefulIngestor()

    def sync(force: bool) -> set[str]:
        GraphUpdater(
            ingestor=store, repo_path=root, parsers=parsers, queries=queries
        ).run(force=force)
        return _stored_calls(store, "cbinc.main.run")

    assert sync(force=True) == expected
    (root / "main.cpp").write_text(_OPS_CALLER + "// touched\n", encoding="utf-8")
    assert sync(force=False) == expected
