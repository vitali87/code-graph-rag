"""Issue #2724: a call through an ES-module default import reaches the
definition the imported module default-exports.

`import fmt from "./fmt"` mapped `fmt` to `<module>.default`, a name no
definition is registered under, so `fmt()` got no CALLS edge, or a
`heuristic` one when the local name happened to equal the exported
function's. The CommonJS form (`module.exports = function`) already
resolved `exact`.

An incremental sync that re-parses only the importer must keep those edges:
the exporters are unchanged, so their `export default` is not parsed again
unless the run reads it back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import force_mtime_after_cache
from evals.cgr_graph import _StatefulIngestor

PROJECT = "jsdef"

FILES = {
    "src/anonfn.ts": "export default function (n: number): string {\n  return n.toFixed(2);\n}\n",
    "src/namedfn.ts": "export default function named(n: number): string {\n  return String(n);\n}\n",
    "src/arrow.ts": "export default (n: number): string => n.toString(16);\n",
    "src/ident.ts": "const hex = (n: number): string => n.toString(16);\nexport default hex;\n",
    "src/aliased.ts": "function pad(n: number): string {\n  return String(n);\n}\nexport { pad as default };\n",
    "src/cjs.js": "module.exports = function (n) {\n  return n * 2;\n};\n",
    "src/plain.ts": "export function other(n: number): number {\n  return n;\n}\n",
    "src/main.ts": (
        'import anon from "./anonfn";\n'
        'import named from "./namedfn";\n'
        'import arrow from "./arrow";\n'
        'import ident from "./ident";\n'
        'import padded from "./aliased";\n'
        'import nothing from "./plain";\n'
        'const cjs = require("./cjs");\n'
        "\n"
        "export function main(): string {\n"
        "  return anon(1) + named(2) + arrow(3) + ident(4) + padded(5) + cjs(6) + nothing(7);\n"
        "}\n"
    ),
}

MAIN = f"{PROJECT}.src.main.main"
IMPORTER = "src/main.ts"
DEFAULT_EXPORTERS = ["anonfn", "arrow", "ident", "aliased", "namedfn"]


def _materialise(root: Path) -> None:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _updater(store: _StatefulIngestor, root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    missing = sorted(
        str(lang.value)
        for lang in (cs.SupportedLanguage.JS, cs.SupportedLanguage.TS)
        if lang not in parsers
    )
    if missing:
        # A module-scoped fixture runs before the per-test grammar skip
        # hook is installed, so a base install must skip here.
        pytest.skip(f"{', '.join(missing)} parser not available")
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )


def _main_callees(store: _StatefulIngestor) -> dict[str, str]:
    return {
        str(edge[4]): str(store.props_for(edge).get(cs.KEY_RESOLUTION))
        for edge in store.keyed_edges
        if edge[1] == MAIN and edge[2] == cs.RelationshipType.CALLS.value
    }


def _exact_default_export_call(callees: dict[str, str], module: str) -> None:
    targets = {qn: res for qn, res in callees.items() if f".src.{module}." in qn}

    assert len(targets) == 1, callees
    assert set(targets.values()) == {cs.EdgeResolution.EXACT.value}, targets


def _index_then_edit_importer(root: Path) -> _StatefulIngestor:
    _materialise(root)
    store = _StatefulIngestor()
    _updater(store, root).run()
    importer = root / IMPORTER
    importer.write_text(
        FILES[IMPORTER] + "\nexport function unrelated(): number {\n  return 0;\n}\n",
        encoding="utf-8",
    )
    force_mtime_after_cache(root, importer)
    return store


@pytest.fixture(scope="module")
def callees(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("repo") / PROJECT
    _materialise(root)
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    return _main_callees(store)


@pytest.mark.parametrize("module", DEFAULT_EXPORTERS)
def test_a_default_import_call_is_an_exact_call_to_the_default_export(
    callees: dict[str, str], module: str
) -> None:
    _exact_default_export_call(callees, module)


def test_the_targets_are_the_exported_definitions(callees: dict[str, str]) -> None:
    assert f"{PROJECT}.src.namedfn.named" in callees
    assert f"{PROJECT}.src.ident.hex" in callees
    assert f"{PROJECT}.src.aliased.pad" in callees


def test_an_incremental_sync_of_the_importer_keeps_the_default_import_calls(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    store = _index_then_edit_importer(root)
    # A fresh updater, as `cgr start --update-graph` builds one per sync: it
    # holds only what this run parses and reads back from the graph.
    _updater(store, root).run()
    callees = _main_callees(store)

    for module in DEFAULT_EXPORTERS:
        _exact_default_export_call(callees, module)


def test_a_scoped_reingest_of_the_importer_keeps_the_default_import_calls(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    store = _index_then_edit_importer(root)
    # The MCP path: a new updater per call re-ingests just the named file.
    _updater(store, root).reingest([IMPORTER])
    callees = _main_callees(store)

    for module in DEFAULT_EXPORTERS:
        _exact_default_export_call(callees, module)


# Negative: what must not change.


def test_the_commonjs_require_call_is_still_exact(callees: dict[str, str]) -> None:
    targets = [qn for qn in callees if ".src.cjs." in qn]

    assert len(targets) == 1
    assert callees[targets[0]] == cs.EdgeResolution.EXACT.value


def test_a_default_import_of_a_module_without_a_default_export_binds_nothing(
    callees: dict[str, str],
) -> None:
    assert not [qn for qn in callees if ".src.plain." in qn]
