"""A reused updater holds what a fresh index of the tree left holds.

The watcher, the MCP server's re-index and any caller that keeps one
`GraphUpdater` across runs drop a deleted file's state with
`remove_file_from_state`. A table it misses keeps the file for the updater's
life: the project index hand-off writes it, though a fresh index of the same
tree has none of it, and neither has the Rust engine, which reads every file
again (issue #3787). Each case deletes a file that imports another, one per
way the import processor reads a language's imports, a PHP file declaring a
class, and a C++ header beside its source, whose method the header's leftover
span records kept alive.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.test_graph_updater_index_engine import LANGUAGE_FIXTURES, _write
from codebase_rag.tests.test_handoff import (
    _FRONTENDS,
    _INDEX,
    _embedded_graph,
    _lines,
    _updater,
)

L = cs.SupportedLanguage

# Each importer below names its provider in another package, so its import
# map has an entry; the shared fixtures' Go, Java, Rust, C# and Scala files
# call into their own package, which needs no import.
GO_FIXTURE = {
    "go.mod": "module example.com/shapes\n\ngo 1.22\n",
    "main.go": (
        'package main\n\nimport "example.com/shapes/geo"\n\n'
        "func main() {\n\tgeo.Area(2)\n}\n"
    ),
    "geo/shape.go": "package geo\n\nfunc Area(side int) int {\n\treturn side * side\n}\n",
}
JAVA_FIXTURE = {
    "src/demo/util/Util.java": (
        "package demo.util;\n\npublic class Util {\n"
        "    public static int helper(int x) { return x + 1; }\n}\n"
    ),
    "src/demo/Main.java": (
        "package demo;\n\nimport demo.util.Util;\n\npublic class Main {\n"
        "    public int run() { return Util.helper(2); }\n}\n"
    ),
}
RUST_FIXTURE = {
    "src/lib.rs": "mod util;\nuse util::helper;\n\npub fn run() -> i32 {\n    helper(1)\n}\n",
    "src/util.rs": "pub fn helper(x: i32) -> i32 {\n    x + 1\n}\n",
}
CSHARP_FIXTURE = {
    "Util.cs": (
        "namespace Demo.Lib {\n  public class Util {\n"
        "    public static int Helper(int x) { return x + 1; }\n  }\n}\n"
    ),
    "Program.cs": (
        "using Demo.Lib;\nnamespace Demo {\n  public class Program {\n"
        "    public static int Main() { return Util.Helper(2); }\n  }\n}\n"
    ),
}
SCALA_FIXTURE = {
    "Util.scala": (
        "package demo.lib\n\nobject Util {\n  def helper(x: Int): Int = x + 1\n}\n"
    ),
    "Main.scala": (
        "package demo\n\nimport demo.lib.Util\n\nobject Main {\n"
        "  def run(): Int = Util.helper(2)\n}\n"
    ),
}
# Nested `module`s key their bindings under their own qns
# (`demo.src.App.App`, `demo.src.App.App.Inner`), beside the file's.
JULIA_NESTED_FIXTURE = {
    "src/Util.jl": "module Util\nexport helper\nhelper(x) = x + 1\nend\n",
    "src/App.jl": (
        "module App\nusing ..Util\nmodule Inner\nusing ..Util: helper\nend\n"
        "run() = Util.helper(2)\nend\n"
    ),
}
# Added between the run that parsed a file and the one that deletes it, so
# that run's per-run resets come between, the Julia path caches' among them.
LATER_ADDITIONS = {
    "src/Extra.jl": "module Extra\nf() = 1\nend\n",
    "extra.py": "def f():\n    return 1\n",
}
# A tree, and the file deleted from it.
DELETIONS = {
    "python": (LANGUAGE_FIXTURES[L.PYTHON], "main.py"),
    "javascript": (LANGUAGE_FIXTURES[L.JS], "main.js"),
    "typescript": (LANGUAGE_FIXTURES[L.TS], "main.ts"),
    "go": (GO_FIXTURE, "main.go"),
    "java": (JAVA_FIXTURE, "src/demo/Main.java"),
    "rust": (RUST_FIXTURE, "src/lib.rs"),
    "c_sharp": (CSHARP_FIXTURE, "Program.cs"),
    "scala": (SCALA_FIXTURE, "Main.scala"),
    "cpp": (LANGUAGE_FIXTURES[L.CPP], "main.cpp"),
    "cpp_header": (LANGUAGE_FIXTURES[L.CPP], "util.hpp"),
    "php": (LANGUAGE_FIXTURES[L.PHP], "src/Main.php"),
    "php_class": (LANGUAGE_FIXTURES[L.PHP], "src/Util.php"),
    "lua": (LANGUAGE_FIXTURES[L.LUA], "main.lua"),
    "dart": (LANGUAGE_FIXTURES[L.DART], "lib/main.dart"),
    "julia": (LANGUAGE_FIXTURES[L.JULIA], "src/Main.jl"),
    "julia_nested": (JULIA_NESTED_FIXTURE, "src/App.jl"),
}
_CASES = pytest.mark.parametrize(
    ("files", "deleted"), list(DELETIONS.values()), ids=list(DELETIONS)
)


class _Indexed(NamedTuple):
    reused: GraphUpdater
    fresh: GraphUpdater
    module_qns: set[str]
    path: Path


@pytest.fixture(autouse=True)
def _frontends_need_no_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _FRONTENDS.items():
        monkeypatch.setattr(settings, name, value)


def _index(
    files: dict[str, str],
    deleted: str,
    tmp_path: Path,
    added_between: dict[str, str] | None = None,
) -> _Indexed:
    """One updater run over `files`, then again once `deleted` is gone, and
    one run afresh over what is left; with the module qns `deleted` held.
    With `added_between`, the reused updater runs once more before the
    deletion, over those files added."""
    repo = _write(tmp_path / "repo", files)
    path = repo / deleted
    with _embedded_graph(tmp_path / "reused.sqlite") as ingestor:
        reused = _updater(
            repo,
            ingestor,
            state_dir=tmp_path / "reused" / "state",
            handoff_dir=tmp_path / "reused" / "handoffs",
        )
        reused.run()
        module_map = reused.factory.definition_processor.module_qn_to_file_path
        module_qns = {qn for qn, owner in module_map.items() if owner == path}
        if added_between is not None:
            _write(repo, added_between)
            reused.run()
        path.unlink()
        reused.run()
    with _embedded_graph(tmp_path / "fresh.sqlite") as ingestor:
        fresh = _updater(
            repo,
            ingestor,
            state_dir=tmp_path / "fresh" / "state",
            handoff_dir=tmp_path / "fresh" / "handoffs",
        )
        fresh.run()
    assert module_qns
    return _Indexed(reused, fresh, module_qns, path)


def _attribute_names(holder) -> set[str]:
    names = set(getattr(holder, "__dict__", {}))
    for cls in type(holder).__mro__:
        slots = getattr(cls, "__slots__", ())
        names.update((slots,) if isinstance(slots, str) else slots)
    return {name for name in names if hasattr(holder, name)}


def _entries(table) -> Iterable:
    """A table's keys, and the paths it maps them to; a collection's items."""
    if isinstance(table, dict):
        return [*table, *(value for value in table.values() if isinstance(value, Path))]
    if isinstance(table, set | frozenset | list | tuple):
        return table
    return ()


def _names(entry, module_qns: set[str], path: Path) -> bool:
    """Whether `entry` names one of the modules, a definition in one, or the
    file itself."""
    match entry:
        case str():
            return any(entry == qn or entry.startswith(f"{qn}.") for qn in module_qns)
        case Path():
            return entry == path
        case tuple():
            return any(_names(part, module_qns, path) for part in entry)
        case _:
            return False


def _naming(
    updater: GraphUpdater, module_qns: set[str], path: Path
) -> dict[str, set[str]]:
    """The entries of each table of the updater and its processors that
    name the deleted file, its modules or their definitions."""
    factory = updater.factory
    holders = {
        "updater": updater,
        "factory": factory,
        "imports": factory.import_processor,
        "definitions": factory.definition_processor,
        "types": factory.type_inference,
        "calls": factory.call_processor,
    }
    found: dict[str, set[str]] = {}
    for holder_name, holder in holders.items():
        for name in _attribute_names(holder):
            entries = {
                repr(entry)
                for entry in _entries(getattr(holder, name))
                if _names(entry, module_qns, path)
            }
            if entries:
                found[f"{holder_name}.{name}"] = entries
    return found


@_CASES
def test_its_project_index_is_a_fresh_index_s(
    files: dict[str, str], deleted: str, tmp_path: Path
) -> None:
    _index(files, deleted, tmp_path)

    assert _lines(tmp_path / "reused" / "handoffs" / _INDEX) == _lines(
        tmp_path / "fresh" / "handoffs" / _INDEX
    )


@_CASES
def test_its_tables_name_the_file_as_a_fresh_index_s_do(
    files: dict[str, str], deleted: str, tmp_path: Path
) -> None:
    # Beside the index: the import sites, the span records, the PHP class
    # index and the rest the hand-off does not write, but resolution reads.
    indexed = _index(files, deleted, tmp_path)

    assert _naming(indexed.reused, indexed.module_qns, indexed.path) == _naming(
        indexed.fresh, indexed.module_qns, indexed.path
    )


@pytest.mark.parametrize(
    "case", ["julia_nested", "python", "rust"], ids=["julia_nested", "python", "rust"]
)
def test_a_deletion_runs_later_leaves_what_a_fresh_index_leaves(
    case: str, tmp_path: Path
) -> None:
    # What the file's parse recorded must outlast the runs between, and the
    # per-run resets they make, to be found and dropped when it goes.
    files, deleted = DELETIONS[case]
    indexed = _index(files, deleted, tmp_path, added_between=LATER_ADDITIONS)

    assert _lines(tmp_path / "reused" / "handoffs" / _INDEX) == _lines(
        tmp_path / "fresh" / "handoffs" / _INDEX
    )
    assert _naming(indexed.reused, indexed.module_qns, indexed.path) == _naming(
        indexed.fresh, indexed.module_qns, indexed.path
    )
