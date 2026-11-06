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


@pytest.mark.parametrize(
    "specifier", ["@scope/pkg", "zone.js", "pkg/subpath", "node:fs", "reflect-metadata"]
)
@pytest.mark.parametrize(
    "statement",
    [
        'import "{}";\n',
        'require("{}");\n',
        'import * as ns from "{}";\n',
        'export * from "{}";\n',
    ],
    ids=[
        "side-effect-import",
        "require-statement",
        "namespace-import",
        "star-reexport",
    ],
)
def test_a_whole_module_import_of_a_package_names_the_package_a_named_one_does(
    temp_repo: Path, mock_ingestor: MagicMock, statement: str, specifier: str
) -> None:
    # The specifier names the package itself, so the edge reaches the
    # ExternalModule `import x from` the same specifier reaches. The
    # resolution read its last segment as an imported name, and
    # `import "@scope/pkg"` imported `@scope`, `import "zone.js"` `zone`.
    files = {
        "named.ts": f'import x from "{specifier}";\nexport const a = x;\n',
        "whole.js": statement.format(specifier) + "export const b = 1;\n",
    }
    for rel, text in files.items():
        (temp_repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    edges = _imports(temp_repo, mock_ingestor)
    named = {to for frm, to in edges if frm == "named"}
    whole = {to for frm, to in edges if frm == "whole"}
    assert len(named) == 1, edges
    assert whole == named, edges


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ('if (process.env.TRACE) require("./db");\n', 1),
        ('function boot() {\n  require("./db");\n}\nboot();\n', 2),
        ('try {\n  require("./db");\n} catch (e) {}\n', 2),
        ('async function load() {\n  await import("./db");\n}\nload();\n', 2),
    ],
    ids=["if-branch", "function-body", "try-block", "awaited-in-function"],
)
def test_a_nested_side_effect_call_is_an_imports_edge(
    temp_repo: Path, mock_ingestor: MagicMock, source: str, line: int
) -> None:
    # A statement-level call below the top level still loads the module, as
    # a bound `require` there is already an import.
    (temp_repo / "db.js").write_text("module.exports = {};\n", encoding="utf-8")
    (temp_repo / "main.js").write_text(source, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    props = _imports(temp_repo, mock_ingestor).get(("main", "db"))
    assert props is not None, sorted(_imports(temp_repo, mock_ingestor))
    assert props.get(cs.KEY_LINE) == line, props


def test_a_call_that_is_not_a_statement_is_no_side_effect_import(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: `require.resolve("./db")` and a call whose value is used are
    # no side-effect import of `./db`.
    (temp_repo / "db.js").write_text("module.exports = {};\n", encoding="utf-8")
    (temp_repo / "main.js").write_text(
        'function where() {\n  require.resolve("./db");\n  return lookup("./db");\n}\n'
        "where();\n",
        encoding="utf-8",
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    assert ("main", "db") not in _imports(temp_repo, mock_ingestor)


@pytest.mark.parametrize(
    ("statement", "binds"),
    [
        ('import {} from "./instrument";\n', False),
        ('import type {} from "./instrument";\n', False),
        ('import init, {} from "./instrument";\n', True),
    ],
    ids=["empty-named-imports", "empty-type-imports", "default-beside-empty"],
)
def test_an_import_clause_binding_nothing_is_a_side_effect_import(
    temp_repo: Path, mock_ingestor: MagicMock, statement: str, binds: bool
) -> None:
    # `import {} from "./instrument"` still loads the module, but its clause
    # binds no name, so neither branch recorded it. A clause binding a
    # default beside the empty list keeps its own named edge.
    (temp_repo / "instrument.ts").write_text(
        "export default function init(): void {}\n", encoding="utf-8"
    )
    (temp_repo / "main.ts").write_text(
        statement + "export const x = 1;\n", encoding="utf-8"
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    props = _imports(temp_repo, mock_ingestor).get(("main", "instrument"))
    assert props is not None, sorted(_imports(temp_repo, mock_ingestor))
    assert props.get(cs.KEY_LINE) == 1, props
    assert (cs.KEY_ALIAS in props) is binds, props
