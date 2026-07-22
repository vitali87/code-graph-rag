# A C/C++ program entry the OS runtime invokes (`main`, Windows'
# `wWinMain`/`WinMain`/`wmain`, a DLL's `DllMain`) has no call site the graph
# can see, so it reported dead and took its whole call tree with it: all 34
# windows/runner candidates on the wonderous Flutter app cascaded from one
# unrooted `wWinMain`.
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.types_defs import ResultRow

_FUNCTION = cs.NodeLabel.FUNCTION.value
_CALLS = cs.RelationshipType.CALLS.value


class FakeIngestor:
    def __init__(self, nodes: list[ResultRow], rels: list[ResultRow]) -> None:
        self._nodes = nodes
        self._rels = rels

    def fetch_all(
        self, query: str, params: dict[str, str] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return self._nodes
        return self._rels


def _node(qn: str, name: str, path: str) -> ResultRow:
    return {
        "label": _FUNCTION,
        "qualified_name": qn,
        "name": name,
        "path": path,
        "start_line": 1,
        "end_line": 2,
        "decorators": [],
        "is_exported": False,
        "overrides_external": False,
    }


def test_windows_entry_points_root_their_call_tree() -> None:
    nodes = [
        _node("proj.runner.main.wWinMain", "wWinMain", "runner/main.cpp"),
        _node("proj.runner.window.Create", "Create", "runner/window.cpp"),
        _node("proj.runner.window.orphan", "orphan", "runner/window.cpp"),
    ]
    rels = [
        {
            "from_label": _FUNCTION,
            "from_qn": "proj.runner.main.wWinMain",
            "rel_type": _CALLS,
            "to_label": _FUNCTION,
            "to_qn": "proj.runner.window.Create",
        },
    ]
    config = default_dead_code_config(include_tests=True, include_classes=False)
    reported = {
        row["qualified_name"]
        for row in collect_dead_code(FakeIngestor(nodes, rels), "proj", config)
    }
    assert "proj.runner.main.wWinMain" not in reported, reported
    assert "proj.runner.window.Create" not in reported, reported
    assert "proj.runner.window.orphan" in reported, reported


def test_c_main_is_rooted() -> None:
    nodes = [_node("proj.tool.entry.main", "main", "tool/entry.c")]
    config = default_dead_code_config(include_tests=True, include_classes=False)
    reported = collect_dead_code(FakeIngestor(nodes, []), "proj", config)
    assert reported == [], reported


def test_header_defined_entry_name_is_not_rooted() -> None:
    # A header is not an OS entry translation unit: a function named WinMain
    # defined in a .h/.hpp is ordinary code and must stay a candidate.
    nodes = [_node("proj.lib.api.WinMain", "WinMain", "lib/api.h")]
    config = default_dead_code_config(include_tests=True, include_classes=False)
    reported = collect_dead_code(FakeIngestor(nodes, []), "proj", config)
    assert len(reported) == 1, reported


def test_namespace_scoped_main_is_not_rooted() -> None:
    # `namespace detail { int main(); }` is an ordinary function the OS
    # cannot invoke; only a file-scope entry (qn directly under the module,
    # whose segment matches the file stem) earns the root.
    nodes = [_node("proj.app.util.detail.main", "main", "app/util.cpp")]
    config = default_dead_code_config(include_tests=True, include_classes=False)
    reported = collect_dead_code(FakeIngestor(nodes, []), "proj", config)
    assert len(reported) == 1, reported


def test_entry_name_on_other_language_is_not_rooted() -> None:
    # The names are OS-runtime contracts of C/C++ translation units only; a
    # same-named Python function earns no root.
    nodes = [_node("proj.tool.wWinMain", "wWinMain", "tool/entry.py")]
    config = default_dead_code_config(include_tests=True, include_classes=False)
    reported = collect_dead_code(FakeIngestor(nodes, []), "proj", config)
    assert len(reported) == 1, reported
