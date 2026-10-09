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
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

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
