"""An incremental run keeps the export bindings of unchanged JS/TS modules.

A default import, `export { trim as strip }` and a barrel's
`export { add as plus } from "./add"` publish names no definition is
registered under; `js_export_bindings` maps them, but only while the
exporting module is parsed. A comment edit to the importer re-parsed it
alone, the table was empty for the exporters, and every one of those calls
re-bound `heuristic` to a same-named function in a module the importer never
imports (issue #3274).
"""

from __future__ import annotations

import os
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_FILES = {
    "src/is-network-error.ts": (
        "export default function isNetworkError(e: unknown): boolean {\n"
        "  return e instanceof TypeError;\n}\n"
    ),
    "src/text.ts": (
        "function trim(s: string): string {\n  return s.trim();\n}\n\n"
        "function pad(s: string): string {\n  return s;\n}\n\n"
        "export { trim as strip };\n"
    ),
    "src/add.ts": "export function add(a: number, b: number): number {\n  return a + b;\n}\n",
    "src/index.ts": 'export { add as plus } from "./add";\n',
    # Two barrels deep: `outer.times` names `mid.times`, which names nothing
    # registered until `mid`'s own table is followed in turn.
    "src/mul.ts": "export function mul(a: number, b: number): number {\n  return a * b;\n}\n",
    "src/mid.ts": 'export { mul as times } from "./mul";\n',
    "src/outer.ts": 'export { times } from "./mid";\n',
    # Exports every one of those names, and `main.ts` never imports it.
    "src/decoy.ts": (
        "export function isNetworkError(n: number): boolean {\n  return n > 0;\n}\n\n"
        "export function strip(s: string): string {\n  return s;\n}\n\n"
        "export function plus(a: number): number {\n  return a;\n}\n\n"
        "export function times(a: number): number {\n  return a;\n}\n"
    ),
    "src/main.ts": (
        'import isNetworkError from "./is-network-error";\n'
        'import { strip } from "./text";\nimport { plus } from "./index";\n'
        'import { times } from "./outer";\n\n'
        "export function run(e: unknown): string {\n"
        '  return String(isNetworkError(e)) + strip(" a ") + plus(1, 2) + times(2, 3);\n}\n'
    ),
}
_FRESH = {
    ("proj.src.is-network-error.isNetworkError", cs.EdgeResolution.EXACT),
    ("proj.src.text.trim", cs.EdgeResolution.EXACT),
    ("proj.src.add.add", cs.EdgeResolution.EXACT),
    ("proj.src.mul.mul", cs.EdgeResolution.EXACT),
}


def _index(store: _StatefulIngestor, root: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _run_calls(store: _StatefulIngestor) -> set[tuple[str, str]]:
    return {
        (str(edge[4]), str(props.get(cs.KEY_RESOLUTION)))
        for edge, props in store.edge_props.items()
        if edge[2] == cs.RelationshipType.CALLS.value and edge[1] == "proj.src.main.run"
    }


def _edit(root: Path, rel: str, text: str) -> None:
    # The mtime lands past the hash cache's so the edit is never skipped.
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / rel
    path.write_text(text, encoding="utf-8")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


def _fresh(temp_repo: Path) -> tuple[Path, _StatefulIngestor]:
    root = temp_repo / "proj"
    for rel, text in _FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    return root, store


def test_a_reparsed_importer_keeps_its_exact_bindings(temp_repo: Path) -> None:
    root, store = _fresh(temp_repo)
    assert _run_calls(store) == _FRESH

    _edit(
        root,
        "src/main.ts",
        _FILES["src/main.ts"].replace("  return String", "  // edit\n  return String"),
    )
    _index(store, root, force=False)

    assert _run_calls(store) == _FRESH


def test_an_edited_exporter_binds_what_it_exports_now(temp_repo: Path) -> None:
    # Negative: the stored bindings stand in only for modules the run does
    # not parse. `text.ts` now exports `pad` as `strip`, and the importer
    # follows it, never the table the graph last held.
    root, store = _fresh(temp_repo)
    _edit(
        root,
        "src/text.ts",
        _FILES["src/text.ts"].replace(
            "export { trim as strip }", "export { pad as strip }"
        ),
    )
    _edit(
        root,
        "src/main.ts",
        _FILES["src/main.ts"].replace("  return String", "  // edit\n  return String"),
    )
    _index(store, root, force=False)

    calls = _run_calls(store)
    assert ("proj.src.text.pad", cs.EdgeResolution.EXACT) in calls, calls
    assert not any(target == "proj.src.text.trim" for target, _r in calls), calls


def test_an_export_the_file_dropped_is_not_read_back_later(temp_repo: Path) -> None:
    # Negative: `text.ts` stops exporting `strip` (its table is now empty),
    # then a later run re-parses only the importer. The stored table must
    # have been cleared, or the stale `strip -> trim` binds again.
    root, store = _fresh(temp_repo)
    _edit(
        root,
        "src/text.ts",
        _FILES["src/text.ts"].replace("export { trim as strip };\n", ""),
    )
    _index(store, root, force=False)
    _edit(
        root,
        "src/main.ts",
        _FILES["src/main.ts"].replace("  return String", "  // edit\n  return String"),
    )
    _index(store, root, force=False)

    calls = _run_calls(store)
    assert not any(target == "proj.src.text.trim" for target, _r in calls), calls
