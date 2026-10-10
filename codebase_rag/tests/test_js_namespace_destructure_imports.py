"""A destructure of a module namespace binds its names as a named import does.

`import * as U from "./u.js"; const { pad } = U;` reads the export `pad`
into a local `pad`, exactly as `import { pad } from "./u.js"` or
`const { pad } = require("./u")` would. The index bound only the last two:
`pad("y")` matched `u.pad` by name alone (`heuristic`), so a rename of
`u.pad` could neither trust nor rewrite it (issue #3252).
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_U = 'export function pad(s) { return " " + s; }\nexport const api = { pad };\n'


def _calls(files: dict[str, str], root: Path) -> set[tuple[str, str, str]]:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=True)
    return {
        (str(edge[1]), str(edge[4]), str(props.get(cs.KEY_RESOLUTION)))
        for edge, props in store.edge_props.items()
        if edge[2] == cs.RelationshipType.CALLS.value
        and str(edge[1]).startswith("proj.main")
    }


def test_a_namespace_destructure_binds_its_names_exactly(temp_repo: Path) -> None:
    files = {
        "package.json": '{"type": "module"}\n',
        "u.js": _U,
        # An ES import is hoisted: written after the destructure, it still
        # binds `U` for it.
        "main.js": (
            "const { pad } = U;\nconst { pad: p } = U;\n"
            'export function run() { return pad("y") + p("z"); }\n'
            'import * as U from "./u.js";\n'
        ),
    }

    calls = _calls(files, temp_repo / "proj")

    assert calls == {("proj.main.run", "proj.u.pad", cs.EdgeResolution.EXACT)}, calls


def test_a_required_module_destructure_binds_its_names_exactly(
    temp_repo: Path,
) -> None:
    files = {
        "u.js": 'function pad(s) { return " " + s; }\nmodule.exports = { pad };\n',
        "main.js": (
            'const U = require("./u");\nconst { pad } = U;\n'
            'function run() { return pad("y"); }\nmodule.exports = { run };\n'
        ),
    }

    calls = _calls(files, temp_repo / "proj")

    assert ("proj.main.run", "proj.u.pad", cs.EdgeResolution.EXACT) in calls, calls


def test_a_destructure_of_an_imported_object_reads_its_member(
    temp_repo: Path,
) -> None:
    # Negative: `api` is an exported object, not the module namespace, so
    # `{ pad } = api` reads `api.pad`; whatever that holds, the destructure
    # never binds it to the module's own export `pad`.
    files = {
        "package.json": '{"type": "module"}\n',
        "u.js": _U,
        "main.js": (
            'import { api } from "./u.js";\nconst { pad } = api;\n'
            'export function run() { return pad("y"); }\n'
        ),
    }

    calls = _calls(files, temp_repo / "proj")

    assert ("proj.main.run", "proj.u.pad", cs.EdgeResolution.EXACT) not in calls, calls


def test_a_name_read_out_of_a_package_stays_the_packages(temp_repo: Path) -> None:
    # Negative: `{ Item } = Prim` with `Prim` a package namespace names the
    # package's `Item`, so `Item.Root()` is never licence to bind the
    # project's own same-named class (as for `Prim.Item`, issue #2535).
    files = {
        "package.json": '{"type": "module"}\n',
        "lib.js": "export class Item {\n  static Root() { return 1; }\n}\n",
        "main.js": (
            'import * as Prim from "some-pkg";\nconst { Item } = Prim;\n'
            "export function run() { return Item.Root(); }\n"
        ),
    }

    calls = _calls(files, temp_repo / "proj")

    assert not any(callee.startswith("proj.lib") for _c, callee, _r in calls), calls
