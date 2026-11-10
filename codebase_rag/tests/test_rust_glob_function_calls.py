"""A Rust free function reached through a glob `use` binds exactly.

rustc resolves `use crate::shapes::*;` at compile time, as it does a named
`use`, yet every function call through a glob was labelled `heuristic`, so
`rename` refused it (mdBook's `set_dest_dir`). And a globbed module holding
the function only through its own `pub use self::inner::*;` was not
followed: the call fell to the project-wide name search and bound a
same-named decoy (issue #3172).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    force_mtime_after_cache,
    get_relationships,
)
from evals.cgr_graph import _StatefulIngestor

_RUST = {
    "Cargo.toml": '[package]\nname = "rsglob"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": (
        "pub mod app;\npub mod child;\npub mod decoy;\npub mod prelude;\n"
        "pub mod shapes;\npub mod util;\n"
        "use crate::shapes::*;\n"
        "pub fn top_helper() -> u32 { 1 }\n"
        "pub fn via_glob() -> u32 {\n    total() + helper()\n}\n"
        "pub fn via_named() -> u32 { crate::util::build() }\n"
    ),
    "src/shapes/mod.rs": "mod inner;\npub use self::inner::*;\npub fn total() -> u32 { 1 }\n",
    "src/shapes/inner.rs": "pub fn helper() -> u32 { 2 }\n",
    "src/decoy.rs": (
        "pub fn total() -> u32 { 99 }\npub fn helper() -> u32 { 99 }\n"
        "pub fn build() -> u32 { 99 }\npub fn top_helper() -> u32 { 99 }\n"
    ),
    "src/child.rs": "use super::*;\npub fn f() -> u32 { top_helper() }\n",
    "src/util.rs": "pub fn build() -> u32 { 3 }\n",
    "src/prelude.rs": "pub use crate::util::build;\n",
    "src/app.rs": "use crate::prelude::*;\npub fn go() -> u32 { build() }\n",
}
_PYTHON = {
    "pkg/__init__.py": "",
    "pkg/helpers.py": "def shout():\n    return 1\n",
    "pkg/use.py": "from pkg.helpers import *\n\n\ndef run():\n    return shout()\n",
}

_Calls = dict[tuple[str, str], str]


def _calls(root: Path, files: dict[str, str], grammar: str) -> _Calls:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing=grammar)
    return {
        (str(c.args[0][2]).split(".", 1)[1], str(c.args[2][2]).split(".", 1)[1]): str(
            (c.kwargs.get("properties") or {}).get(cs.KEY_RESOLUTION)
        )
        for c in get_relationships(mock, cs.RelationshipType.CALLS)
    }


@pytest.fixture(scope="module")
def rust(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("rs3172") / "rsglob", _RUST, "rust")


def _callees(calls: _Calls, caller: str) -> dict[str, str]:
    return {to: res for (src, to), res in calls.items() if src == caller}


def test_a_glob_imported_function_binds_exactly(rust: _Calls) -> None:
    assert _callees(rust, "src.lib.via_glob") == {
        "src.shapes.total": "exact",
        "src.shapes.inner.helper": "exact",
    }, rust


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("src.child.f", "src.lib.top_helper"),
        ("src.app.go", "src.util.build"),
    ],
    ids=["use-super-glob", "prelude-named-reexport"],
)
def test_other_glob_shapes_bind_exactly(rust: _Calls, caller: str, callee: str) -> None:
    assert _callees(rust, caller) == {callee: "exact"}, rust


def test_a_named_use_and_a_python_star_import_are_unchanged(
    rust: _Calls, tmp_path: Path
) -> None:
    # Negatives: a path call stays exact, and a Python `import *` stays a
    # heuristic, since a run-time `__all__` can change what it binds.
    assert _callees(rust, "src.lib.via_named") == {"src.util.build": "exact"}, rust
    python = _calls(tmp_path / "pystar", _PYTHON, "python")
    assert _callees(python, "pkg.use.run") == {"pkg.helpers.shout": "heuristic"}


_RUST_VISIBILITY = {
    "Cargo.toml": '[package]\nname = "rsvis"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": (
        "pub mod api;\npub mod app;\npub mod facade;\npub mod hidden;\n"
        "pub mod pick;\npub mod private_build;\npub mod util;\npub mod visible;\n"
    ),
    # `helper` is private to `hidden`, so `use hidden::*` cannot import it.
    "src/hidden.rs": "fn helper() -> u32 { 99 }\npub fn other() -> u32 { 0 }\n",
    "src/visible.rs": "pub fn helper() -> u32 { 1 }\n",
    "src/pick.rs": (
        "use crate::hidden::*;\nuse crate::visible::*;\n"
        "pub fn go() -> u32 { helper() + other() }\n"
    ),
    # `facade`'s private `use` binds `make` for `facade` alone.
    "src/facade.rs": (
        "use crate::private_build::build as make;\n"
        "pub fn unrelated() -> u32 { make() }\n"
    ),
    "src/api.rs": "pub use crate::util::build as make;\n",
    "src/private_build.rs": "pub fn build() -> u32 { 99 }\n",
    "src/util.rs": "pub fn build() -> u32 { 3 }\n",
    "src/app.rs": (
        "use crate::facade::*;\nuse crate::api::*;\npub fn go() -> u32 { make() }\n"
    ),
}


@pytest.fixture(scope="module")
def rust_visibility(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(
        tmp_path_factory.mktemp("rs3172vis") / "rsvis", _RUST_VISIBILITY, "rust"
    )


def test_two_globs_supplying_a_name_bind_the_one_they_can_see(
    rust_visibility: _Calls,
) -> None:
    # `hidden::helper` is private, so `use crate::hidden::*;` does not import
    # it and rustc takes `visible`'s; `other`, which only `hidden` supplies,
    # is exact too.
    assert _callees(rust_visibility, "src.pick.go") == {
        "src.visible.helper": "exact",
        "src.hidden.other": "exact",
    }, rust_visibility


def test_a_glob_does_not_follow_a_private_use_of_its_base(
    rust_visibility: _Calls,
) -> None:
    # `facade`'s private `use ... as make` is not importable through
    # `facade::*`; `api`'s public re-export is what `make` names.
    assert _callees(rust_visibility, "src.app.go") == {"src.util.build": "exact"}, (
        rust_visibility
    )


_RUST_PRIVATE = {
    "Cargo.toml": '[package]\nname = "rspriv"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": (
        "pub mod hidden;\npub mod shapes;\npub mod user;\nfn secret() -> u32 { 1 }\n"
    ),
    # `drop` is private to `hidden`: a glob outside it does not import it,
    # and `drop(v)` there calls the prelude's. `hidden::below` sees it.
    "src/hidden.rs": "pub mod below;\nfn drop<T>(_value: T) {}\npub fn keep() {}\n",
    "src/hidden/below.rs": "use super::*;\npub fn go(v: u32) { keep(); drop(v) }\n",
    "src/user.rs": (
        "use crate::hidden::*;\nuse super::*;\n"
        "pub fn go(v: u32) -> u32 { keep(); drop(v); secret() }\n"
    ),
    # `inner::child` sees `inner`'s private `drop`, but `shapes` does not,
    # so its `pub use self::inner::*;` does not pass `drop` on.
    "src/shapes/mod.rs": "pub mod inner;\npub use self::inner::*;\n",
    "src/shapes/inner.rs": "pub mod child;\nfn drop<T>(_value: T) {}\n",
    "src/shapes/inner/child.rs": (
        "use crate::shapes::*;\npub fn go(v: u32) { drop(v) }\n"
    ),
}


@pytest.fixture(scope="module")
def rust_private(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(
        tmp_path_factory.mktemp("rs3242priv") / "rspriv", _RUST_PRIVATE, "rust"
    )


def test_a_private_function_outside_the_globbing_module_is_not_exact(
    rust_private: _Calls,
) -> None:
    callees = _callees(rust_private, "src.user.go")
    assert callees.get("src.hidden.keep") == "exact", rust_private
    assert callees.get("src.hidden.drop") != "exact", rust_private


def test_a_private_function_the_glob_can_see_stays_exact(
    rust_private: _Calls,
) -> None:
    # A private item is visible to its module and every module below it, so
    # `use super::*;` imports it, from a crate root's sibling files as well.
    assert _callees(rust_private, "src.user.go").get("src.lib.secret") == "exact"
    assert _callees(rust_private, "src.hidden.below.go") == {
        "src.hidden.keep": "exact",
        "src.hidden.drop": "exact",
    }, rust_private


def test_a_function_the_reexporting_module_cannot_see_is_not_exact(
    rust_private: _Calls,
) -> None:
    callees = _callees(rust_private, "src.shapes.inner.child.go")
    assert callees.get("src.shapes.inner.drop") != "exact", rust_private


def _store_calls(store: _StatefulIngestor, caller: str) -> dict[str, str]:
    return {
        str(edge[4]): str(store.props_for(edge).get(cs.KEY_RESOLUTION))
        for edge in store.edges
        if edge[2] == cs.RelationshipType.CALLS.value and edge[1] == caller
    }


def _index(store: _StatefulIngestor, root: Path, *, force: bool = False) -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.RUST not in parsers:
        pytest.skip("rust parser not available")
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def test_an_unchanged_private_function_stays_not_exact_after_a_sync(
    tmp_path: Path,
) -> None:
    # Only the caller is re-parsed; `hidden.rs` is read back from the graph,
    # and its private `drop` must not turn exact there.
    root = tmp_path / "proj"
    for rel, text in _RUST_PRIVATE.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root)
    caller = root / "src/user.rs"
    caller.write_text(
        _RUST_PRIVATE["src/user.rs"] + "pub fn unrelated() {}\n", encoding="utf-8"
    )
    force_mtime_after_cache(root, caller)
    _index(store, root)
    clean = _StatefulIngestor()
    _index(clean, root, force=True)
    synced = _store_calls(store, "proj.src.user.go")
    assert synced == _store_calls(clean, "proj.src.user.go"), synced
    assert synced.get("proj.src.hidden.drop") != "exact", synced
