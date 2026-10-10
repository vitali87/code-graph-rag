"""An incremental run keeps the extension methods of unchanged C# files.

`b.Describe()` binds to `static string Describe(this Box b)` through
`csharp_extension_methods`, which only a parse of the declaring class
fills. A comment edit to the caller re-parsed it alone, the index was empty
for `BoxExtensions.cs`, and every such call lost its CALLS edge (serilog:
21 edges into `Extensions.LiteralValue`) (issue #3275).
"""

from __future__ import annotations

import os
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_FILES = {
    "Types.cs": (
        "namespace App;\n\npublic class Box\n{\n    public int N = 1;\n}\n\n"
        "public class Services\n{\n}\n\n"
        # Generic twins: an extension for one must never take the other's call.
        "public class Builder\n{\n}\n\npublic class Builder<T>\n{\n}\n"
    ),
    "BoxExtensions.cs": (
        "namespace App;\n\npublic static class BoxExtensions\n{\n"
        '    public static string Describe(this Box b) => "box " + b.N;\n\n'
        "    public static Services AddMyServices(this Services s) => s;\n\n"
        "    public static int Build(this Builder b) => 0;\n\n"
        "    public static int Build<T>(this Builder<T> b) => 1;\n}\n"
    ),
    "Program.cs": (
        "namespace App;\n\npublic class Program\n{\n    public string Run()\n    {\n"
        "        Box b = new Box();\n        Services s = new Services();\n"
        "        Builder<int> g = new Builder<int>();\n"
        "        s.AddMyServices();\n        g.Build();\n"
        "        return b.Describe();\n    }\n}\n"
    ),
}
_EXT = "proj.BoxExtensions.App.BoxExtensions"


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


def _run_calls(store: _StatefulIngestor) -> set[tuple[str, str]]:
    return {
        (str(edge[4]), str(props.get(cs.KEY_RESOLUTION)))
        for edge, props in store.edge_props.items()
        if edge[2] == cs.RelationshipType.CALLS.value
        and str(edge[1]).startswith("proj.Program.App.Program.Run")
        and str(edge[4]).startswith(_EXT)
    }


def _edit(root: Path, rel: str, text: str) -> None:
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / rel
    path.write_text(text, encoding="utf-8")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


def _fresh(temp_repo: Path) -> tuple[Path, _StatefulIngestor, set[tuple[str, str]]]:
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in _FILES.items():
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    return root, store, _run_calls(store)


def test_a_reparsed_caller_keeps_its_extension_calls(temp_repo: Path) -> None:
    root, store, fresh = _fresh(temp_repo)
    targets = {target.split("(", 1)[0] for target, _r in fresh}
    assert targets == {
        f"{_EXT}.Describe",
        f"{_EXT}.AddMyServices",
        f"{_EXT}.Build",
    }, fresh
    # `g` is a `Builder<int>`: the generic twin's extension, the `Build`
    # declared on line 11, never the one for the plain `Builder`.
    assert (f"{_EXT}.Build(Builder)@11", cs.EdgeResolution.EXACT) in fresh, fresh

    _edit(
        root,
        "Program.cs",
        _FILES["Program.cs"].replace("        Box b", "        // edit\n        Box b"),
    )
    _index(store, root, force=False)

    assert _run_calls(store) == fresh


def test_an_edited_extension_binds_as_a_fresh_index_does(temp_repo: Path) -> None:
    # Negative: the stored facts stand in only for files the run does not
    # parse. With `BoxExtensions.cs` re-parsed (`Describe` now extends
    # `Services`), the incremental run must bind exactly what a fresh index
    # of the same tree binds, never the receiver the graph stored before.
    root, store, _fresh_calls = _fresh(temp_repo)
    edited = {
        "BoxExtensions.cs": _FILES["BoxExtensions.cs"].replace(
            'Describe(this Box b) => "box " + b.N', 'Describe(this Services b) => "s"'
        ),
        "Program.cs": _FILES["Program.cs"].replace(
            "        Box b", "        // edit\n        Box b"
        ),
    }
    for rel, text in edited.items():
        _edit(root, rel, text)
    _index(store, root, force=False)

    clean = temp_repo / "clean" / "proj"
    clean.mkdir(parents=True)
    for rel, text in {**_FILES, **edited}.items():
        (clean / rel).write_text(text, encoding="utf-8")
    clean_store = _StatefulIngestor()
    _index(clean_store, clean, force=True)

    assert _run_calls(store) == _run_calls(clean_store)
    assert (f"{_EXT}.Describe(Box)", cs.EdgeResolution.EXACT) not in _run_calls(store)


def test_the_rehydrated_index_is_the_one_a_parse_builds(temp_repo: Path) -> None:
    # Every field of each entry comes back: the receiver type, the declaring
    # namespace (`App`, which qualifies an unqualified `this Box`) and the
    # receiver's generic arity (`this Builder<T>` -> 1).
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in _FILES.items():
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    parsed = _index(store, root, force=True)
    expected = {
        name: sorted(entries)
        for name, entries in parsed.factory.definition_processor.csharp_extension_methods.items()
    }
    assert any(entry[3] == 1 for entry in expected["Build"]), expected
    assert all(entry[2] == "App" for entries in expected.values() for entry in entries)

    _edit(
        root,
        "Program.cs",
        _FILES["Program.cs"].replace("        Box b", "        // edit\n        Box b"),
    )
    rehydrated = _index(store, root, force=False)

    assert {
        name: sorted(entries)
        for name, entries in rehydrated.factory.definition_processor.csharp_extension_methods.items()
    } == expected
