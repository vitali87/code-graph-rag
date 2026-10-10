"""An incremental run keeps the declared namespaces of unchanged PHP files.

`use function App\\Util\\slug` resolves by matching `App\\Util` against
`php_module_namespaces`, which only a parse of the declaring file fills. A
comment edit to the caller re-parsed it alone, no module matched, and the
call re-bound `heuristic` to `App\\Other\\slug`, a namespace the file never
imports (issue #3277).
"""

from __future__ import annotations

import os
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_FILES = {
    "src/Util/strings.php": (
        "<?php\n\nnamespace App\\Util;\n\n"
        "function slug(string $s): string\n{\n    return strtolower($s);\n}\n"
    ),
    "src/Util/other.php": (
        "<?php\n\nnamespace App\\Other;\n\nfunction slug(int $n): int\n{\n    return $n;\n}\n"
    ),
    "src/main.php": (
        "<?php\n\nnamespace App;\n\nuse function App\\Util\\slug;\n\n"
        'function run(): string\n{\n    return slug("A");\n}\n'
    ),
}
_EDIT = ("    return slug", "    // edit\n    return slug")


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
        if edge[2] == cs.RelationshipType.CALLS.value and edge[1] == "proj.src.main.run"
    }


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _edit(root: Path, rel: str, text: str) -> None:
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    (root / rel).write_text(text, encoding="utf-8")
    os.utime(root / rel, (cache_mtime + 1, cache_mtime + 1))


def test_a_reparsed_caller_keeps_its_use_function_binding(temp_repo: Path) -> None:
    root = temp_repo / "proj"
    _write(root, _FILES)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    fresh = _run_calls(store)
    assert fresh == {("proj.src.Util.strings.slug", cs.EdgeResolution.EXACT)}, fresh

    _edit(root, "src/main.php", _FILES["src/main.php"].replace(*_EDIT))
    _index(store, root, force=False)

    assert _run_calls(store) == fresh


def test_the_rehydrated_map_is_the_one_a_parse_builds(temp_repo: Path) -> None:
    # A file declaring several namespaces maps to none, as a parse leaves it.
    files = {
        **_FILES,
        "src/multi.php": (
            "<?php\n\nnamespace App\\First {\n    function a() {}\n}\n\n"
            "namespace App\\Second {\n    function b() {}\n}\n"
        ),
    }
    root = temp_repo / "proj"
    _write(root, files)
    store = _StatefulIngestor()
    expected = dict(
        _index(store, root, force=True).factory.import_processor.php_module_namespaces
    )
    assert "proj.src.multi" not in expected, expected

    _edit(root, "src/main.php", _FILES["src/main.php"].replace(*_EDIT))
    rehydrated = _index(store, root, force=False)

    assert dict(rehydrated.factory.import_processor.php_module_namespaces) == expected


def test_an_edited_namespace_binds_as_a_fresh_index_does(temp_repo: Path) -> None:
    # Negative: `strings.php` re-parsed into `App\\Text` no longer answers
    # `use function App\\Util\\slug`; the incremental run binds what a fresh
    # index of the same tree binds, never the namespace the graph stored.
    root = temp_repo / "proj"
    _write(root, _FILES)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    edited = {
        "src/Util/strings.php": _FILES["src/Util/strings.php"].replace(
            "App\\Util;", "App\\Text;"
        ),
        "src/main.php": _FILES["src/main.php"].replace(*_EDIT),
    }
    for rel, text in edited.items():
        _edit(root, rel, text)
    _index(store, root, force=False)

    clean = temp_repo / "clean" / "proj"
    _write(clean, {**_FILES, **edited})
    clean_store = _StatefulIngestor()
    _index(clean_store, clean, force=True)

    assert _run_calls(store) == _run_calls(clean_store)
    assert ("proj.src.Util.strings.slug", cs.EdgeResolution.EXACT) not in (
        _run_calls(store)
    )
