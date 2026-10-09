"""A JS/TS side-effect import is an IMPORTS edge (issue #3267).

`import "./instrument"`, `import "reflect-metadata"` and a statement-level
`require("./db");` bind no name, and the import pass recorded only bindings,
so none of them produced an edge: `graph importers` returned nothing for a
module that is imported, and a deleted `./instrument` was invisible to
`cgr check`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater

_FILES = {
    "src/instrument.ts": "export function initTracing(): void {}\ninitTracing();\n",
    "src/routes.ts": 'export function route(): string { return "/"; }\n',
    "src/main.ts": (
        'import "./instrument";\nimport "reflect-metadata";\n'
        'import { route } from "./routes";\n\nexport const app = route();\n'
    ),
    "src/db.js": "function connect() { return 1; }\nconnect();\nmodule.exports = {};\n",
    "src/cache.js": "module.exports = {};\n",
    "src/polyfill.ts": "export const ok = true;\n",
    "src/boot.ts": 'await import("./polyfill");\nexport const booted = true;\n',
    "src/server.js": (
        'require("./db");\nconst routes = require("./routes");\n'
        'import("./cache");\nmodule.exports = routes;\n'
    ),
}


def _imports(repo: Path, mock: MagicMock) -> dict[tuple[str, str], dict]:
    # (importer, target) -> the edge's properties.
    prefix = f"{repo.name}."
    out: dict[tuple[str, str], dict] = {}
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] != cs.RelationshipType.IMPORTS.value:
            continue
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        out[
            (
                str(c.args[0][2]).removeprefix(prefix),
                str(c.args[2][2]).removeprefix(prefix),
            )
        ] = dict(props)
    return out


@pytest.fixture
def imports(temp_repo: Path, mock_ingestor: MagicMock) -> dict[tuple[str, str], dict]:
    for rel, text in _FILES.items():
        (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (temp_repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)
    return _imports(temp_repo, mock_ingestor)


@pytest.mark.parametrize(
    ("importer", "target", "line"),
    [
        ("src.main", "src.instrument", 1),
        ("src.server", "src.db", 1),
        ("src.server", "src.cache", 3),
        ("src.boot", "src.polyfill", 1),
    ],
    ids=[
        "ts-import-statement",
        "js-require-statement",
        "js-dynamic-import",
        "ts-awaited-dynamic-import",
    ],
)
def test_a_side_effect_import_of_a_module_is_an_imports_edge(
    imports: dict[tuple[str, str], dict], importer: str, target: str, line: int
) -> None:
    props = imports.get((importer, target))
    assert props is not None, sorted(imports)
    # Where the statement is, and no bound or imported name.
    assert props.get(cs.KEY_LINE) == line, props
    assert cs.KEY_ALIAS not in props, props
    assert cs.KEY_IMPORTED_NAME not in props, props


def test_a_side_effect_import_of_a_package_reaches_its_external_module(
    imports: dict[tuple[str, str], dict],
) -> None:
    assert ("src.main", "reflect-metadata") in imports, sorted(imports)


def test_binding_imports_keep_their_edges(
    imports: dict[tuple[str, str], dict],
) -> None:
    # Negative: the named import and the bound require are unchanged.
    assert imports[("src.main", "src.routes")].get(cs.KEY_LINE) == 3
    assert imports[("src.server", "src.routes")].get(cs.KEY_LINE) == 2


def test_the_target_is_the_imported_file_beside_a_directory_index(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The specifier names the whole module, so a sibling `index.ts` in the
    # same directory must not take the edge from `setup.ts`.
    files = {
        "lib/index.ts": "export const ready = true;\n",
        "lib/setup.ts": "export const done = true;\n",
        "app.ts": 'import "./lib/setup";\nexport const x = 1;\n',
    }
    for rel, text in files.items():
        (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (temp_repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    targets = {to for frm, to in _imports(temp_repo, mock_ingestor) if frm == "app"}
    assert targets == {"lib.setup"}, targets


def test_a_removed_side_effect_import_loses_its_edge(temp_repo: Path) -> None:
    # Negative: the side-effect list is per parse, so an edit that drops
    # `import "./instrument"` leaves no edge behind on re-ingest.
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers
    from evals.cgr_graph import _StatefulIngestor

    root = temp_repo / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src/instrument.ts").write_text(_FILES["src/instrument.ts"])
    (root / "src/main.ts").write_text('import "./instrument";\nexport const x = 1;\n')
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=True)

    def importers() -> set[str]:
        return {
            str(edge[1])
            for edge in store.edges
            if edge[2] == cs.RelationshipType.IMPORTS.value
            and edge[4] == "proj.src.instrument"
        }

    assert importers() == {"proj.src.main"}
    (root / "src/main.ts").write_text("export const x = 1;\n")
    updater.reingest(["src/main.ts"], deleted=[])
    assert importers() == set()
