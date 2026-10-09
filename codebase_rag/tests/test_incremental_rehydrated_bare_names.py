"""A rehydrated definition is findable by its name, as a parsed one is.

Parsing a method registers its qualified name AND its name: `Info` ->
`ILog.Info(string)`. Rehydration on an incremental run indexed only the
qn's last segment, `Info(string)`, so a re-parsed caller's name-based
fallback saw only the one same-named method whose qn happens to end in its
name -- a parameterless `Levels.Info()` -- and bound the call there (serilog:
all 33 incremental-only `heuristic` edges) (issue #3261).
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import NodeType
from evals.cgr_graph import _StatefulIngestor

_FILES = {
    "ILog.cs": "namespace App;\n\npublic interface ILog\n{\n    void Info(string m);\n}\n",
    "Levels.cs": (
        "namespace App;\n\npublic class Levels\n{\n    public Levels Info() => this;\n}\n"
    ),
    "Sink.cs": (
        "using System;\n\nnamespace App;\n\npublic static class Sink\n{\n"
        "    public static void Capture(Action<ILog> write)\n    {\n    }\n}\n"
    ),
    "Tests.cs": (
        "namespace App;\n\npublic class Tests\n{\n    public void Hello()\n    {\n"
        '        Sink.Capture(l => l.Info("hi"));\n    }\n}\n'
    ),
}
_EDIT = ("        Sink.Capture", "        // edit\n        Sink.Capture")


def _index(store: _StatefulIngestor, root: Path, force: bool) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=force)
    return updater


def _hello_calls(store: _StatefulIngestor) -> set[str]:
    return {
        str(edge[4])
        for edge in store.edges
        if edge[2] == cs.RelationshipType.CALLS.value
        and str(edge[1]).startswith("proj.Tests.App.Tests.Hello")
    }


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _edit(root: Path, rel: str, text: str) -> None:
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    (root / rel).write_text(text, encoding="utf-8")
    os.utime(root / rel, (cache_mtime + 1, cache_mtime + 1))


def test_a_reparsed_caller_binds_as_a_fresh_index_does(temp_repo: Path) -> None:
    root = temp_repo / "proj"
    _write(root, _FILES)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    fresh = _hello_calls(store)
    assert "proj.ILog.App.ILog.Info(string)" in fresh, fresh

    _edit(root, "Tests.cs", _FILES["Tests.cs"].replace(*_EDIT))
    _index(store, root, force=False)

    assert _hello_calls(store) == fresh


def test_a_rehydrated_method_is_indexed_by_its_name(temp_repo: Path) -> None:
    # The index itself: after an incremental run in a fresh process, `Info`
    # names both methods, as it does after a full parse.
    root = temp_repo / "proj"
    _write(root, _FILES)
    store = _StatefulIngestor()
    parsed = _index(store, root, force=True)
    expected = parsed.function_registry.find_ending_with("Info")
    assert "proj.ILog.App.ILog.Info(string)" in expected, expected

    _edit(root, "Tests.cs", _FILES["Tests.cs"].replace(*_EDIT))
    rehydrated = _index(store, root, force=False)

    assert rehydrated.function_registry.find_ending_with("Info") == expected


def test_a_name_that_is_its_qns_last_segment_is_indexed_once(
    temp_repo: Path,
) -> None:
    # Negative: `Levels.Info` (no parameters) already ends in its name; the
    # index lists it once, never twice.
    root = temp_repo / "proj"
    _write(root, _FILES)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    _edit(root, "Tests.cs", _FILES["Tests.cs"].replace(*_EDIT))
    rehydrated = _index(store, root, force=False)

    found = rehydrated.function_registry.find_ending_with("Info")
    assert found.count("proj.Levels.App.Levels.Info") == 1, found


def test_a_name_indexed_late_reaches_a_lookup_already_cached() -> None:
    # A name lookup made before rehydration (a Pass 2 resolution, say) must
    # not keep answering from its cache once the name gains a definition.
    registry = FunctionRegistryTrie(simple_name_lookup=defaultdict(set))
    registry["p.ILog.Info(string)"] = NodeType.METHOD
    assert registry.find_ending_with("Info") == []

    registry.index_name("p.ILog.Info(string)", "Info")

    assert registry.find_ending_with("Info") == ["p.ILog.Info(string)"]
