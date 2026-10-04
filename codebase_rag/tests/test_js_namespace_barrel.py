"""Issue #2933: a namespace import of a barrel file reaches what it re-exports.

`import * as idx from "./classic/index.js"; idx.object()` named the barrel,
where nothing is registered: `object` is defined in `./schemas` and passed on
by `export * from "./schemas"`. Named imports through a barrel already
followed its re-exports (#2464), but a namespace member never did, so the
call bound nothing. The directory spelling (`import * as dir from
"./classic"`) bound only through the Go-style package-member search, which
looks for any file one level down defining the name.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

APP = """\
import * as dir from "./classic";
import * as idxJs from "./classic/index.js";
import * as idx from "./classic/index";
import * as schemasJs from "./classic/schemas.js";
import * as top from "./v4/index.js";
import * as renamed from "./renamed/index.js";
import * as ambiguous from "./ambiguous/index.js";
import * as priv from "./private/index.js";
import * as mixed from "./mixed/index.js";
import * as consts from "./consts/index.js";
import * as clauses from "./clauses/index.js";

export function nsDirImport() { return dir.object({}); }
export function nsIndexJs() { return idxJs.object({}); }
export function nsIndex() { return idx.object({}); }
export function nsChain() { return top.number(1); }
export function nsRenamed() { return renamed.make(); }
export function nsDefiningModuleJs() { return schemasJs.object({}); }
export function nsNotExported() { return idxJs.internal(); }
export function nsAmbiguous() { return ambiguous.clash(); }
export function nsMissing() { return idxJs.nothing(); }
export function nsPrivate() { return priv.helper(); }
export function nsPrivateBesideExported() { return mixed.pick(); }
export function nsExportedConst() { return consts.make(); }
export function nsExportClause() { return clauses.later(); }
"""

FILES = {
    "src/classic/schemas.ts": (
        "export function object(shape: object) { return shape; }\n"
        "export function number(n: number) { return n; }\n"
    ),
    "src/classic/internal.ts": "export function internal() { return 1; }\n",
    "src/classic/index.ts": 'export * from "./schemas";\n',
    # A chain of star barrels, as zod's v4/index -> classic/index -> schemas.
    "src/v4/index.ts": 'export * from "../classic/index.js";\n',
    "src/renamed/impl.ts": "export function build() { return 2; }\n",
    "src/renamed/index.ts": 'export { build as make } from "./impl";\n',
    "src/ambiguous/a.ts": "export function clash() { return 1; }\n",
    "src/ambiguous/b.ts": "export function clash() { return 2; }\n",
    "src/ambiguous/index.ts": 'export * from "./a";\nexport * from "./b";\n',
    # `export *` passes on a source's exports only: `helper` and `a.pick`
    # are private to their files.
    "src/private/hidden.ts": (
        "function helper() { return 1; }\n"
        "export function shown() { return helper(); }\n"
    ),
    "src/private/index.ts": 'export * from "./hidden";\n',
    "src/mixed/a.ts": (
        "function pick() { return 1; }\nexport function other() { return pick(); }\n"
    ),
    "src/mixed/b.ts": "export function pick() { return 2; }\n",
    "src/mixed/index.ts": 'export * from "./a";\nexport * from "./b";\n',
    "src/consts/impl.ts": "export const make = () => 1;\n",
    "src/consts/index.ts": 'export * from "./impl";\n',
    "src/clauses/impl.ts": "function later() { return 1; }\nexport { later };\n",
    "src/clauses/index.ts": 'export * from "./impl";\n',
    "src/app.ts": APP,
}


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("barrel") / "barrel"
    for path, text in FILES.items():
        _write(root, path, text)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src == f"{prefix}src.app.{caller}"
    }


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("nsIndexJs", "src.classic.schemas.object"),
        ("nsIndex", "src.classic.schemas.object"),
        ("nsChain", "src.classic.schemas.number"),
        ("nsRenamed", "src.renamed.impl.build"),
        ("nsPrivateBesideExported", "src.mixed.b.pick"),
    ],
    ids=[
        "index-js-specifier",
        "index-specifier",
        "star-chain",
        "renamed-export",
        "exported-beside-a-private-twin",
    ],
)
def test_a_namespace_member_follows_the_barrels_reexports(
    graph: RecordedGraph, caller: str, callee: str
) -> None:
    assert _callees(graph, caller) == {callee: "exact"}


# Negative: what must not change.


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("nsDirImport", "src.classic.schemas.object"),
        ("nsDefiningModuleJs", "src.classic.schemas.object"),
        ("nsExportedConst", "src.consts.impl.make"),
        ("nsExportClause", "src.clauses.impl.later"),
    ],
    ids=[
        "directory-specifier",
        "defining-module",
        "exported-const-function",
        "export-clause",
    ],
)
def test_a_namespace_that_already_bound_still_binds(
    graph: RecordedGraph, caller: str, callee: str
) -> None:
    assert _callees(graph, caller) == {callee: "exact"}


@pytest.mark.parametrize(
    "caller",
    ["nsAmbiguous", "nsMissing", "nsNotExported", "nsPrivate"],
    ids=[
        "two-star-sources",
        "no-such-export",
        "sibling-the-barrel-skips",
        "private-to-the-star-source",
    ],
)
def test_a_name_the_barrel_does_not_single_out_binds_nothing(
    graph: RecordedGraph, caller: str
) -> None:
    # Two `export *` sources exporting `clash` make it ambiguous, which
    # TypeScript exports from neither.
    assert _callees(graph, caller) == {}
