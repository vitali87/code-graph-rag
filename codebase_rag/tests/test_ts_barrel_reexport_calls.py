"""Issue #2464: a call through a TS/JS barrel's re-export binds exactly.

Barrel files (`index.ts`) are how TS/JS libraries expose their API. A call
through a renamed re-export (`export { add as plus } from`) or a default
re-export (`export { default as times } from`) got no CALLS edge, because the
importer's map named the barrel (`lib.plus`), where nothing is registered, and
the name-only fallback found no `plus` anywhere. A plain named re-export
(`export { x } from`), `export *` and a direct default import got only a
`heuristic` edge from that fallback, which `cgr rename` refuses to rewrite
through. The re-export itself names the definition, so each of these binds
`exact` when the chain of barrels leads to exactly one definition.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_query
from codebase_rag.editing.rename import Renamer, RenameRefused, rename
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.tests.test_rename_op import RecordedGraph
from evals.cgr_graph import _StatefulIngestor

EXACT = cs.EdgeResolution.EXACT.value
CALLS = cs.RelationshipType.CALLS.value

# The issue's layout, verbatim.
ISSUE_FILES = {
    "src/lib/math/add.ts": (
        "export function add(a: number, b: number): number {\n  return a + b;\n}\n"
    ),
    "src/lib/math/mul.ts": (
        "export default function mul(a: number, b: number): number {\n"
        "  return a * b;\n}\n"
    ),
    "src/lib/math/index.ts": (
        'export * from "./add";\nexport { default as times } from "./mul";\n'
    ),
    "src/lib/index.ts": (
        'export * as math from "./math";\nexport { add as plus } from "./math/add";\n'
    ),
    "src/app/main.ts": (
        'import { add, times } from "../lib/math";\n'
        'import { math, plus } from "../lib";\n'
        'import mul from "../lib/math/mul";\n'
        "\n"
        "export function run(): number {\n"
        "  return add(1, 2) + times(2, 3) + math.add(4, 5) + plus(6, 7) + mul(8, 9);\n"
        "}\n"
    ),
}
ISSUE_ADD = "barrel.src.lib.math.add.add"
ISSUE_MUL = "barrel.src.lib.math.mul.mul"

# The issue comment's layout: a plain named re-export, and the forms that
# already resolved beside it.
COMMENT_FILES = {
    "src/lib/util.ts": (
        "export function formatPrice(cents: number): string {\n"
        "  return `$${(cents / 100).toFixed(2)}`;\n}\n"
    ),
    "src/lib/index.ts": 'export { formatPrice } from "./util";\n',
    "src/app.ts": (
        'import { formatPrice } from "./lib";\n\n'
        "export function show(): string {\n  return formatPrice(7);\n}\n"
    ),
    "src/alias.ts": (
        'import { formatPrice as fp } from "./lib";\n\n'
        "export function short(): string {\n  return fp(1);\n}\n"
    ),
    "src/cart.ts": (
        'import { formatPrice } from "./lib/util";\n'
        'import * as u from "./lib/util";\n\n'
        "export function total(): string {\n"
        "  return formatPrice(10) + u.formatPrice(2);\n}\n"
    ),
}
FORMAT_PRICE = "shop.src.lib.util.formatPrice"

# Barrels of barrels: an alias of an alias, a default re-exported twice, and
# a star of a star, each three hops from the definition.
CHAIN_FILES = {
    "src/core/scale.ts": (
        "export function scale(n: number): number {\n  return n * 2;\n}\n\n"
        "export default function shift(n: number): number {\n  return n + 1;\n}\n"
    ),
    "src/core/index.ts": (
        'export * from "./scale";\nexport { default as nudge } from "./scale";\n'
    ),
    "src/api/index.ts": (
        'export { scale as resize, nudge } from "../core";\nexport * from "../core";\n'
    ),
    "src/index.ts": (
        'export { resize as grow } from "./api";\nexport { nudge as bump } from "./api";\n'
    ),
    "src/use.ts": (
        'import { grow, bump } from ".";\n'
        'import { scale, resize } from "./api";\n\n'
        "export function use(): number {\n"
        "  return grow(1) + bump(2) + scale(3) + resize(4);\n}\n"
    ),
}
CHAIN_SCALE = "chain.src.core.scale.scale"
CHAIN_SHIFT = "chain.src.core.scale.shift"

# A barrel that imports and then exports (no `from` on the export), and a
# module exporting its own definitions under another name.
LOCAL_FILES = {
    "src/text/pad.ts": (
        "export function pad(s: string): string {\n  return ` ${s}`;\n}\n\n"
        "function trim(s: string): string {\n  return s.trim();\n}\n\n"
        "export { trim as strip };\n\n"
        "function upper(s: string): string {\n  return s.toUpperCase();\n}\n\n"
        "export default upper;\n"
    ),
    "src/text/index.ts": (
        'import { pad } from "./pad";\n'
        'import upper, { strip } from "./pad";\n\n'
        "export { pad as leftPad, strip, upper as shout };\n"
    ),
    "src/main.ts": (
        'import { leftPad, strip, shout } from "./text";\n'
        'import { strip as clean } from "./text/pad";\n\n'
        "export function main(): string {\n"
        '  return leftPad("a") + strip(" b ") + shout("c") + clean("d");\n}\n'
    ),
}
LOCAL_PAD = "local.src.text.pad.pad"
LOCAL_TRIM = "local.src.text.pad.trim"
LOCAL_UPPER = "local.src.text.pad.upper"

# What must stay unbound: two star sources exporting one name, a default a
# star does not carry, and a cycle of stars. `private/index.ts` imports `dup`
# for its own use and exports another `dup` through `export *`.
NEGATIVE_FILES = {
    "src/a.ts": (
        "export function dup(): number {\n  return 1;\n}\n\n"
        "export function onlyA(): number {\n  return 2;\n}\n"
    ),
    "src/b.ts": (
        "export function dup(): number {\n  return 3;\n}\n\n"
        "export default function bdef(): number {\n  return 4;\n}\n"
    ),
    "src/c.ts": "export function other(): number {\n  return 5;\n}\n",
    "src/stars/index.ts": 'export * from "../a";\nexport * from "../b";\n',
    "src/pick/index.ts": (
        'export * from "../a";\nexport { other as dup } from "../c";\n'
    ),
    "src/loop1.ts": 'export * from "./loop2";\n',
    "src/loop2.ts": 'export * from "./loop1";\n',
    "src/d.ts": "export function dup(): number {\n  return 6;\n}\n",
    "src/private/index.ts": (
        'import { dup } from "../a";\n\nexport * from "../d";\n\n'
        "export function viaA(): number {\n  return dup();\n}\n"
    ),
    "src/neg.ts": (
        'import { dup, onlyA } from "./stars";\n'
        'import starDefault from "./stars";\n'
        'import { dup as picked } from "./pick";\n'
        'import { ghost } from "./loop1";\n'
        'import { dup as hidden } from "./private";\n\n'
        "export function neg(): number {\n"
        "  return dup() + onlyA() + starDefault() + picked() + ghost() + hidden();\n}\n"
    ),
}


def _run(root: Path, files: dict[str, str]) -> tuple[GraphUpdater, _StatefulIngestor]:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.TS not in parsers:
        pytest.skip("typescript parser not available")
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=root.name,
    )
    updater.run(force=True)
    return updater, store


# The React barrel shape: a default-exported component re-exported under its
# own name, constructed through the barrel and through a default import.
WIDGET_FILES = {
    "src/widget.ts": (
        "export default class Widget {\n  draw(): number {\n    return 1;\n  }\n}\n"
    ),
    "src/kit/index.ts": 'export { default as Widget } from "../widget";\n',
    "src/app.ts": (
        'import Widget from "./widget";\n\n'
        "export function make() {\n  return new Widget();\n}\n"
    ),
    "src/app2.ts": (
        'import { Widget } from "./kit";\n\n'
        "export function make2() {\n  return new Widget();\n}\n"
    ),
}


def _index(root: Path, files: dict[str, str]) -> _StatefulIngestor:
    return _run(root, files)[1]


def _calls(store: _StatefulIngestor, caller: str) -> dict[int, tuple[str, str]]:
    """The CALLS edges out of `caller`, by call column: (target, resolution)."""
    out: dict[int, tuple[str, str]] = {}
    for edge in store.keyed_edges:
        if edge[1] != caller or edge[2] != CALLS:
            continue
        props = store.props_for(edge)
        col = props.get(cs.KEY_COL)
        assert isinstance(col, int), props
        out[col] = (str(edge[4]), str(props.get(cs.KEY_RESOLUTION)))
    return out


@pytest.fixture(scope="module")
def issue_store(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    return _index(tmp_path_factory.mktemp("issue") / "barrel", ISSUE_FILES)


@pytest.fixture(scope="module")
def comment_store(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    return _index(tmp_path_factory.mktemp("comment") / "shop", COMMENT_FILES)


@pytest.fixture(scope="module")
def chain_store(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    return _index(tmp_path_factory.mktemp("chain") / "chain", CHAIN_FILES)


@pytest.fixture(scope="module")
def local_store(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    return _index(tmp_path_factory.mktemp("local") / "local", LOCAL_FILES)


@pytest.fixture(scope="module")
def negative_store(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    return _index(tmp_path_factory.mktemp("negative") / "neg", NEGATIVE_FILES)


# --- the issue's table ----------------------------------------------------------


@pytest.mark.parametrize(
    ("col", "target"),
    [
        pytest.param(9, ISSUE_ADD, id="add-through-export-star"),
        pytest.param(21, ISSUE_MUL, id="times-through-default-as"),
        pytest.param(52, ISSUE_ADD, id="plus-through-named-alias"),
        pytest.param(65, ISSUE_MUL, id="mul-direct-default-import"),
    ],
)
def test_each_call_through_the_barrel_is_an_exact_call_to_the_definition(
    issue_store: _StatefulIngestor, col: int, target: str
) -> None:
    calls = _calls(issue_store, "barrel.src.app.main.run")
    assert calls.get(col) == (target, EXACT), calls


def test_callers_of_the_default_export_include_its_barrel_alias(
    issue_store: _StatefulIngestor,
) -> None:
    # `cgr graph callers …mul.mul` listed only the direct import (col 65).
    calls = _calls(issue_store, "barrel.src.app.main.run")
    assert sorted(col for col, (target, _) in calls.items() if target == ISSUE_MUL) == [
        21,
        65,
    ]


def test_the_barrel_maps_record_what_each_export_names(tmp_path: Path) -> None:
    # `export * from` never reached the import map: the parser compared the
    # child with Java's named `asterisk` node, while the JS/TS grammars spell
    # `*` as an anonymous token. A module's own bindings exported under
    # another name were recorded nowhere.
    updater, _store = _run(tmp_path / "maps", {**ISSUE_FILES, **LOCAL_FILES})
    imports = updater.factory.import_processor.import_mapping
    assert imports["maps.src.lib.math.index"] == {
        "*maps.src.lib.math.add": "maps.src.lib.math.add",
        "times": "maps.src.lib.math.mul.default",
    }
    # Only what a module exports may be followed through it: a re-export
    # names another module's export, a local export (True) one of its own
    # bindings.
    exports = updater.factory.import_processor.js_export_bindings
    assert exports["maps.src.lib.math.index"] == {
        "times": ("maps.src.lib.math.mul.default", False)
    }
    assert exports["maps.src.lib.index"] == {
        "plus": ("maps.src.lib.math.add.add", False)
    }
    assert exports["maps.src.lib.math.mul"] == {
        "default": ("maps.src.lib.math.mul.mul", True)
    }
    # An exported declaration is recorded too (PR #2965).
    assert exports["maps.src.text.pad"] == {
        "pad": ("maps.src.text.pad.pad", True),
        "strip": ("maps.src.text.pad.trim", True),
        "default": ("maps.src.text.pad.upper", True),
    }
    assert exports["maps.src.text.index"] == {
        "leftPad": ("maps.src.text.index.pad", True),
        "strip": ("maps.src.text.index.strip", True),
        "shout": ("maps.src.text.index.upper", True),
    }


# Export statements that publish no name a binding could follow: an anonymous
# default, a default expression, and a re-export whose source names no
# module. A comment inside a clause is no specifier either.
UNNAMED_EXPORT_FILES = {
    "src/anon.ts": "export default function () {\n  return 1;\n}\n",
    "src/expr.ts": "const base = 40;\n\nexport default base + 2;\n",
    "src/blank.ts": 'export { gone } from "";\n',
    "src/listed.ts": (
        "function first(): number {\n  return 1;\n}\n\n"
        "function last(): number {\n  return 2;\n}\n\n"
        "export { first, /* middle was removed */ last as final };\n"
    ),
}


def test_an_export_naming_no_binding_records_nothing(tmp_path: Path) -> None:
    updater, _store = _run(tmp_path / "unnamed", UNNAMED_EXPORT_FILES)
    exports = updater.factory.import_processor.js_export_bindings
    assert "unnamed.src.anon" not in exports, exports
    assert "unnamed.src.expr" not in exports, exports
    assert "unnamed.src.blank" not in exports, exports
    assert exports["unnamed.src.listed"] == {
        "first": ("unnamed.src.listed.first", True),
        "final": ("unnamed.src.listed.last", True),
    }


# --- the issue comment: a plain named re-export ---------------------------------


@pytest.mark.parametrize(
    ("caller", "col"),
    [
        pytest.param("shop.src.app.show", 9, id="named-reexport"),
        pytest.param("shop.src.alias.short", 9, id="aliased-import-of-reexport"),
    ],
)
def test_a_named_reexport_binds_exactly(
    comment_store: _StatefulIngestor, caller: str, col: int
) -> None:
    calls = _calls(comment_store, caller)
    assert calls.get(col) == (FORMAT_PRICE, EXACT), calls


# --- chains of barrels -----------------------------------------------------------


@pytest.mark.parametrize(
    ("col", "target"),
    [
        pytest.param(9, CHAIN_SCALE, id="alias-of-alias-three-barrels"),
        pytest.param(19, CHAIN_SHIFT, id="default-renamed-twice"),
        pytest.param(29, CHAIN_SCALE, id="star-of-star"),
        pytest.param(40, CHAIN_SCALE, id="alias-through-star"),
    ],
)
def test_a_chain_of_barrels_reaches_the_definition(
    chain_store: _StatefulIngestor, col: int, target: str
) -> None:
    calls = _calls(chain_store, "chain.src.use.use")
    assert calls.get(col) == (target, EXACT), calls


# --- exports of a module's own bindings -----------------------------------------


@pytest.mark.parametrize(
    ("col", "target"),
    [
        pytest.param(9, LOCAL_PAD, id="imported-then-exported-as"),
        pytest.param(24, LOCAL_TRIM, id="reexport-of-a-local-alias"),
        pytest.param(39, LOCAL_UPPER, id="default-identifier-exported-as"),
        pytest.param(52, LOCAL_TRIM, id="local-export-alias-imported-as"),
    ],
)
def test_an_export_of_a_local_binding_reaches_the_definition(
    local_store: _StatefulIngestor, col: int, target: str
) -> None:
    calls = _calls(local_store, "local.src.main.main")
    assert calls.get(col) == (target, EXACT), calls


# --- negative: what must not change, and what must stay unbound -------------------


def test_the_namespace_reexport_call_stays_exact(
    issue_store: _StatefulIngestor,
) -> None:
    calls = _calls(issue_store, "barrel.src.app.main.run")
    assert calls.get(35) == (ISSUE_ADD, EXACT), calls


@pytest.mark.parametrize(
    "col",
    [
        pytest.param(9, id="direct-named-import"),
        pytest.param(27, id="namespace-import-member"),
    ],
)
def test_direct_imports_of_the_definition_stay_exact(
    comment_store: _StatefulIngestor, col: int
) -> None:
    calls = _calls(comment_store, "shop.src.cart.total")
    assert calls.get(col) == (FORMAT_PRICE, EXACT), calls


def _imports(store: _StatefulIngestor) -> set[tuple[str, str]]:
    return {
        (str(edge[1]), str(edge[4]))
        for edge in store.keyed_edges
        if edge[2] == cs.RelationshipType.IMPORTS.value
    }


def test_a_star_barrel_imports_the_module_it_reexports(
    issue_store: _StatefulIngestor, chain_store: _StatefulIngestor
) -> None:
    # `export * from "./add"` in `math/index.ts` names the module `math.add`,
    # which the IMPORTS flush read as the name `add` of the `math` barrel:
    # the barrel itself, so the edge to `add.ts` never existed.
    assert ("barrel.src.lib.math.index", "barrel.src.lib.math.add") in _imports(
        issue_store
    )
    # A star of a directory barrel, and one into a sibling file.
    assert {
        ("chain.src.api.index", "chain.src.core.index"),
        ("chain.src.core.index", "chain.src.core.scale"),
    } <= _imports(chain_store)


def test_a_star_barrel_does_not_import_itself(
    issue_store: _StatefulIngestor, chain_store: _StatefulIngestor
) -> None:
    for store in (issue_store, chain_store):
        imports = _imports(store)
        assert all(source != target for source, target in imports), imports


def test_two_star_sources_exporting_one_name_bind_neither_exactly(
    negative_store: _StatefulIngestor,
) -> None:
    # TypeScript exports neither of two conflicting star names; picking one
    # would be a guess, so the call is not an exact edge to either.
    calls = _calls(negative_store, "neg.src.neg.neg")
    assert calls.get(9, (None, None))[1] != EXACT, calls


def test_a_name_only_one_star_source_exports_still_binds(
    negative_store: _StatefulIngestor,
) -> None:
    calls = _calls(negative_store, "neg.src.neg.neg")
    assert calls.get(17) == ("neg.src.a.onlyA", EXACT), calls


def test_a_star_reexport_does_not_carry_the_default(
    negative_store: _StatefulIngestor,
) -> None:
    # `export *` never re-exports `default`, so the barrel has no default and
    # `b`'s default export is not what `starDefault` names.
    calls = _calls(negative_store, "neg.src.neg.neg")
    assert 27 not in calls, calls
    assert "neg.src.b.bdef" not in {target for target, _ in calls.values()}, calls


def test_a_barrels_private_import_does_not_stand_in_for_its_export(
    negative_store: _StatefulIngestor,
) -> None:
    # `private/index.ts` imports `dup` from `a` for its own use and exports
    # `d`'s `dup` through `export *`; the consumer's `dup` is `d`'s.
    calls = _calls(negative_store, "neg.src.neg.neg")
    assert calls.get(64) == ("neg.src.d.dup", EXACT), calls


def test_the_barrels_own_call_keeps_its_private_import(
    negative_store: _StatefulIngestor,
) -> None:
    calls = _calls(negative_store, "neg.src.private.index.viaA")
    assert calls == {9: ("neg.src.a.dup", EXACT)}, calls


def test_an_explicit_reexport_outranks_a_star(
    negative_store: _StatefulIngestor,
) -> None:
    calls = _calls(negative_store, "neg.src.neg.neg")
    assert calls.get(43) == ("neg.src.c.other", EXACT), calls


def test_a_cycle_of_stars_ends_without_an_edge(
    negative_store: _StatefulIngestor,
) -> None:
    calls = _calls(negative_store, "neg.src.neg.neg")
    assert 54 not in calls, calls


# --- cgr rename through a barrel (the issue comment) -----------------------------


def _rename_graph(root: Path, files: dict[str, str], mock: MagicMock) -> RecordedGraph:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    updater = create_and_run_updater(root, mock)
    graph = RecordedGraph(mock, updater.project_name)
    graph.root_path = str(root.resolve())
    return graph


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def test_a_rename_through_a_named_reexport_needs_no_allow_heuristic(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The call through `./lib` was `heuristic`, so the rename refused the
    # most common barrel layout unless the caller accepted guesses.
    graph = _rename_graph(temp_repo, COMMENT_FILES, mock_ingestor)
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.src.lib.util.formatPrice",
        "formatMoney",
    )
    assert report.applied, report.message
    assert _read(temp_repo, "src/lib/util.ts").startswith(
        "export function formatMoney(cents: number)"
    )
    assert _read(temp_repo, "src/lib/index.ts") == (
        'export { formatMoney } from "./util";\n'
    )
    assert _read(temp_repo, "src/app.ts") == (
        'import { formatMoney } from "./lib";\n\n'
        "export function show(): string {\n  return formatMoney(7);\n}\n"
    )
    alias = _read(temp_repo, "src/alias.ts")
    assert alias.startswith('import { formatMoney as fp } from "./lib";\n')
    assert "return fp(1);" in alias
    assert "return formatMoney(10) + u.formatMoney(2);" in _read(
        temp_repo, "src/cart.ts"
    )


def test_a_default_import_call_keeps_the_importers_own_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `import mul from` binds a name the importer chose. Renaming the function
    # leaves that import and its `mul(8, 9)` alone; rewriting only the call
    # would leave it naming nothing.
    graph = _rename_graph(temp_repo, ISSUE_FILES, mock_ingestor)
    before = _read(temp_repo, "src/app/main.ts")
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.src.lib.math.mul.mul",
        "product",
    )
    assert report.applied, report.message
    assert _read(temp_repo, "src/lib/math/mul.ts").startswith(
        "export default function product(a: number, b: number)"
    )
    assert _read(temp_repo, "src/app/main.ts") == before


@pytest.mark.parametrize("allow_heuristic", [False, True])
def test_a_call_through_a_star_barrel_refuses_even_with_allow_heuristic(
    temp_repo: Path, mock_ingestor: MagicMock, allow_heuristic: bool
) -> None:
    # `import { add } from "../lib/math"` binds `add` through `export *`, an
    # import the rename cannot rewrite: renaming the call alone would leave
    # the import naming a function the barrel no longer exports, so no
    # leave to guess makes the rename safe.
    graph = _rename_graph(temp_repo, ISSUE_FILES, mock_ingestor)
    before = {rel: _read(temp_repo, rel) for rel in ISSUE_FILES}
    with pytest.raises(RenameRefused) as refused:
        rename(
            temp_repo,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.src.lib.math.add.add",
            "sum",
            allow_heuristic=allow_heuristic,
        )
    assert "src/lib/math/index.ts" in str(refused.value)
    assert [(s.path, s.line, s.col, s.resolution) for s in refused.value.ambiguous] == [
        ("src/app/main.ts", 6, 9, cs.RENAME_SITE_STAR_REEXPORT)
    ]
    assert {rel: _read(temp_repo, rel) for rel in ISSUE_FILES} == before


def test_a_default_reexported_under_its_own_name_keeps_its_importers(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `export { default as Widget } from` names the barrel's export as the
    # barrel chose; a consumer of `Widget` binds that name, not the class's,
    # so renaming the class leaves every importer as it is.
    graph = _rename_graph(temp_repo, WIDGET_FILES, mock_ingestor)
    before = {rel: _read(temp_repo, rel) for rel in WIDGET_FILES}
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.src.widget.Widget",
        "Gadget",
    )
    assert report.applied, report.message
    assert _read(temp_repo, "src/widget.ts").startswith("export default class Gadget {")
    for rel in ("src/kit/index.ts", "src/app.ts", "src/app2.ts"):
        assert _read(temp_repo, rel) == before[rel], rel


def test_a_require_binding_keeps_the_importers_own_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `const mul = require("./mul")` names the binding as the importer chose,
    # as a default import does; the call was rewritten and the binding left.
    files = {
        "src/mul.js": "module.exports = function mul(a, b) {\n  return a * b;\n};\n",
        "src/app.js": (
            'const mul = require("./mul");\n\n'
            "function run() {\n  return mul(2, 3);\n}\n\n"
            "module.exports = { run };\n"
        ),
    }
    graph = _rename_graph(temp_repo, files, mock_ingestor)
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.src.mul.mul",
        "product",
    )
    assert report.applied, report.message
    assert "function product(a, b)" in _read(temp_repo, "src/mul.js")
    assert _read(temp_repo, "src/app.js") == files["src/app.js"]


def _chain_files(c_barrel: str) -> dict[str, str]:
    # `foo` passes from `a` through the named re-export in `b` and then
    # through `c`, which either re-exports it by name or with `export *`.
    return {
        "src/a.ts": "export function foo(): number {\n  return 1;\n}\n",
        "src/b/index.ts": 'export { foo } from "../a";\n',
        "src/c/index.ts": c_barrel,
        "src/app.ts": (
            'import { foo } from "./c";\n\n'
            "export function run(): number {\n  return foo();\n}\n"
        ),
    }


def test_a_star_after_a_named_reexport_refuses_even_with_allow_heuristic(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The star is one hop past the named re-export `b`: the rename rewrites
    # `b`'s `export { foo }` and the call, but `app.ts` imports `foo` from
    # `c`, through `export *`, and that import is out of its reach.
    files = _chain_files('export * from "../b";\n')
    graph = _rename_graph(temp_repo, files, mock_ingestor)
    with pytest.raises(RenameRefused) as refused:
        rename(
            temp_repo,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.src.a.foo",
            "bar",
            allow_heuristic=True,
        )
    assert "src/c/index.ts" in str(refused.value)
    sites = [(s.path, s.resolution) for s in refused.value.ambiguous]
    assert sites == [("src/app.ts", cs.RENAME_SITE_STAR_REEXPORT)]
    assert {rel: _read(temp_repo, rel) for rel in files} == files


def test_a_chain_of_named_reexports_renames_without_leave(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # With no star on the way, every hop names `foo`, so each re-export and
    # the consumer's import are rewritten with the definition.
    files = _chain_files('export { foo } from "../b";\n')
    graph = _rename_graph(temp_repo, files, mock_ingestor)
    report = rename(
        temp_repo, graph.fetch_all, graph.project, f"{graph.project}.src.a.foo", "bar"
    )
    assert report.applied, report.message
    assert _read(temp_repo, "src/b/index.ts") == 'export { bar } from "../a";\n'
    assert _read(temp_repo, "src/c/index.ts") == 'export { bar } from "../b";\n'
    app = _read(temp_repo, "src/app.ts")
    assert app.startswith('import { bar } from "./c";\n')
    assert "return bar();" in app


# --- the star walk behind the refusal --------------------------------------------


def _importer(
    module: str, path: str | None, alias: str | None, imported_name: str | None
) -> graph_query.ImporterRow:
    return graph_query.ImporterRow(
        module=module,
        path=path,
        line=1,
        col=0,
        end_line=1,
        end_col=1,
        alias=alias,
        imported_name=imported_name,
    )


STAR = cs.IMPORTED_NAME_WILDCARD

# Who imports each module, and how. `p.a` defines `foo`; `b` and `c` star it
# and `d` stars both, so the walk reaches `d` twice past a star. `direct`
# imports `foo` by name before `e` stars it. A row with no path, and one
# naming another export, lead nowhere even past a star.
STAR_WALK_IMPORTERS = {
    "p.a": [
        _importer("p.b", "src/b.ts", None, STAR),
        _importer("p.c", "src/c.ts", None, STAR),
        _importer("p.direct", "src/direct.ts", "foo", "foo"),
    ],
    "p.b": [
        _importer("p.d", "src/d.ts", None, STAR),
        _importer("p.wrong", "src/wrong.ts", "bar", "bar"),
    ],
    "p.c": [
        _importer("p.d", "src/d.ts", None, STAR),
        _importer("p.ghost", None, "foo", "foo"),
        _importer("p.near", "src/near.ts", "foo", "foo"),
    ],
    "p.d": [_importer("p.app", "src/app.ts", "foo", "foo")],
    "p.direct": [_importer("p.e", "src/e.ts", None, STAR)],
    "p.e": [_importer("p.late", "src/late.ts", "foo", "foo")],
    "p.ghost": [_importer("p.haunted", "src/haunted.ts", "foo", "foo")],
    "p.wrong": [_importer("p.astray", "src/astray.ts", "foo", "foo")],
}


def test_the_star_walk_binds_each_consumer_to_its_first_star_barrel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def importers(
        _fetch_all: object, _project: str, source: str
    ) -> list[graph_query.ImporterRow]:
        asked.append(source)
        return STAR_WALK_IMPORTERS.get(source, [])

    monkeypatch.setattr(graph_query, "importers", importers)
    bound = Renamer(tmp_path, MagicMock(), "p")._js_star_bound("p.a", "foo")
    assert bound == {
        "src/near.ts": "src/c.ts",
        "src/app.ts": "src/c.ts",
        "src/late.ts": "src/e.ts",
    }
    # Each module is walked once past a star; the pathless and the unrelated
    # rows are not followed at all.
    assert sorted(asked) == [
        "p.a",
        "p.app",
        "p.b",
        "p.c",
        "p.d",
        "p.direct",
        "p.e",
        "p.late",
        "p.near",
    ]
