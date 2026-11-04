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

The exported value can sit under parentheses or a TS cast, and the import can
name a directory whose `index` file is the module; both missed the record.
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

FN_TYPE = "type Fn = () => number;\n"
DEFAULT_FN = "export default function () {\n  return 1;\n}\n"

FILES = {
    "src/anonfn.ts": "export default function (n: number): string {\n  return n.toFixed(2);\n}\n",
    "src/namedfn.ts": "export default function named(n: number): string {\n  return String(n);\n}\n",
    "src/arrow.ts": "export default (n: number): string => n.toString(16);\n",
    "src/ident.ts": "const hex = (n: number): string => n.toString(16);\nexport default hex;\n",
    "src/aliased.ts": "function pad(n: number): string {\n  return String(n);\n}\nexport { pad as default };\n",
    # The exported value under parentheses or a TS cast.
    "src/paren.ts": "export default (() => 42);\n",
    "src/nested.ts": "export default ((() => 42));\n",
    "src/cast.ts": FN_TYPE + "export default (() => 42) as Fn;\n",
    "src/satisfied.ts": FN_TYPE + "export default (() => 42) satisfies Fn;\n",
    "src/asserted.ts": FN_TYPE + "export default <Fn>(() => 42);\n",
    "src/nonnull.ts": "const f = () => 42;\nexport default f!;\n",
    "src/identcast.ts": "const hex = (n: number): string => n.toString(16);\nexport default hex as (n: number) => string;\n",
    "src/namedexpr.ts": "export default (function named() {\n  return 1;\n});\n",
    "src/classexpr.ts": "export default (class Named {\n  run() {\n    return 1;\n  }\n});\n",
    # A directory imported by name, its module being its `index` file.
    "src/worker/index.ts": "export default () => 42;\n",
    "src/jsdir/index.js": DEFAULT_FN,
    "src/mjsdir/index.mjs": DEFAULT_FN,
    "src/tsxdir/index.tsx": DEFAULT_FN,
    "src/cjsdir/index.js": "module.exports = function () {\n  return 1;\n};\n",
    # `both.ts` is what `./both` names, as on disk, not `both/index.ts`.
    "src/both.ts": DEFAULT_FN,
    "src/both/index.ts": DEFAULT_FN,
    "src/cjs.js": "module.exports = function (n) {\n  return n * 2;\n};\n",
    "src/plain.ts": "export function other(n: number): number {\n  return n;\n}\n",
    "src/main.ts": (
        'import anon from "./anonfn";\n'
        'import named from "./namedfn";\n'
        'import arrow from "./arrow";\n'
        'import ident from "./ident";\n'
        'import padded from "./aliased";\n'
        'import paren from "./paren";\n'
        'import nested from "./nested";\n'
        'import cast from "./cast";\n'
        'import satisfied from "./satisfied";\n'
        'import asserted from "./asserted";\n'
        'import nonnull from "./nonnull";\n'
        'import identcast from "./identcast";\n'
        'import namedexpr from "./namedexpr";\n'
        'import Klass from "./classexpr";\n'
        'import worker from "./worker";\n'
        'import jsdir from "./jsdir";\n'
        'import mjsdir from "./mjsdir";\n'
        'import tsxdir from "./tsxdir";\n'
        'import both from "./both";\n'
        'import nothing from "./plain";\n'
        'const cjs = require("./cjs");\n'
        'const cjsdir = require("./cjsdir");\n'
        "\n"
        "export function main(): unknown[] {\n"
        "  new Klass();\n"
        "  return [\n"
        "    anon(1), named(2), arrow(3), ident(4), padded(5), paren(), nested(),\n"
        "    cast(), satisfied(), asserted(), nonnull(), identcast(6), namedexpr(),\n"
        "    worker(), jsdir(), mjsdir(), tsxdir(), both(), cjs(7), cjsdir(), nothing(8),\n"
        "  ];\n"
        "}\n"
    ),
}

MAIN = f"{PROJECT}.src.main.main"
IMPORTER = "src/main.ts"
DEFAULT_EXPORTERS = [
    "anonfn",
    "arrow",
    "ident",
    "aliased",
    "namedfn",
    "paren",
    "nested",
    "cast",
    "satisfied",
    "asserted",
    "nonnull",
    "identcast",
    "namedexpr",
    "worker",
    "jsdir",
    "mjsdir",
    "tsxdir",
]


def _materialise(root: Path) -> None:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _updater(store: _StatefulIngestor, root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    missing = sorted(
        str(lang.value)
        for lang in (
            cs.SupportedLanguage.JS,
            cs.SupportedLanguage.TS,
            cs.SupportedLanguage.TSX,
        )
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


def _main_edges(
    store: _StatefulIngestor,
    rel: cs.RelationshipType = cs.RelationshipType.CALLS,
) -> dict[str, str]:
    return {
        str(edge[4]): str(store.props_for(edge).get(cs.KEY_RESOLUTION))
        for edge in store.keyed_edges
        if edge[1] == MAIN and edge[2] == rel.value
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
def indexed(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    root = tmp_path_factory.mktemp("repo") / PROJECT
    _materialise(root)
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    return store


@pytest.fixture(scope="module")
def callees(indexed: _StatefulIngestor) -> dict[str, str]:
    return _main_edges(indexed)


@pytest.mark.parametrize("module", DEFAULT_EXPORTERS)
def test_a_default_import_call_is_an_exact_call_to_the_default_export(
    callees: dict[str, str], module: str
) -> None:
    _exact_default_export_call(callees, module)


def test_the_targets_are_the_exported_definitions(callees: dict[str, str]) -> None:
    assert f"{PROJECT}.src.namedfn.named" in callees
    assert f"{PROJECT}.src.ident.hex" in callees
    assert f"{PROJECT}.src.aliased.pad" in callees
    assert f"{PROJECT}.src.nonnull.f" in callees
    assert f"{PROJECT}.src.identcast.hex" in callees
    assert f"{PROJECT}.src.namedexpr.named" in callees


def test_a_directory_import_reaches_its_index_module(callees: dict[str, str]) -> None:
    for directory in ("worker", "jsdir", "mjsdir", "tsxdir"):
        assert [qn for qn in callees if f".src.{directory}.index." in qn], callees


def test_a_default_imported_class_expression_is_instantiated_exactly(
    indexed: _StatefulIngestor,
) -> None:
    instantiated = _main_edges(indexed, cs.RelationshipType.INSTANTIATES)

    assert instantiated.get(f"{PROJECT}.src.classexpr.Named") == (
        cs.EdgeResolution.EXACT.value
    ), instantiated


def test_a_commonjs_require_of_a_directory_reaches_its_index_export(
    callees: dict[str, str],
) -> None:
    _exact_default_export_call(callees, "cjsdir")
    assert [qn for qn in callees if ".src.cjsdir.index." in qn], callees


def test_an_incremental_sync_of_the_importer_keeps_the_default_import_calls(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    store = _index_then_edit_importer(root)
    # A fresh updater, as `cgr start --update-graph` builds one per sync: it
    # holds only what this run parses and reads back from the graph.
    _updater(store, root).run()
    callees = _main_edges(store)

    for module in DEFAULT_EXPORTERS:
        _exact_default_export_call(callees, module)


def test_a_scoped_reingest_of_the_importer_keeps_the_default_import_calls(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    store = _index_then_edit_importer(root)
    # The MCP path: a new updater per call re-ingests just the named file.
    _updater(store, root).reingest([IMPORTER])
    callees = _main_edges(store)

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


def test_a_module_file_wins_over_a_same_named_directory(
    callees: dict[str, str],
) -> None:
    _exact_default_export_call(callees, "both")
    assert not [qn for qn in callees if ".src.both.index." in qn], callees
