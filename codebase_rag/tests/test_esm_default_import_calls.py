"""Issue #2724: a call through an ES-module default import reaches the
definition the imported module default-exports.

`import fmt from "./fmt"` mapped `fmt` to `<module>.default`, a name no
definition is registered under, so `fmt()` got no CALLS edge, or a
`heuristic` one when the local name happened to equal the exported
function's. The CommonJS form (`module.exports = function`) already
resolved `exact`.
"""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
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


@pytest.fixture(scope="module")
def callees(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("repo") / PROJECT
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return {
        str(edge[4]): str(store.props_for(edge).get(cs.KEY_RESOLUTION))
        for edge in store.keyed_edges
        if edge[1] == MAIN and edge[2] == cs.RelationshipType.CALLS.value
    }


@pytest.mark.parametrize(
    "module",
    ["anonfn", "arrow", "ident", "aliased", "namedfn"],
)
def test_a_default_import_call_is_an_exact_call_to_the_default_export(
    callees: dict[str, str], module: str
) -> None:
    targets = {qn: res for qn, res in callees.items() if f".src.{module}." in qn}

    assert len(targets) == 1, callees
    assert set(targets.values()) == {cs.EdgeResolution.EXACT.value}, targets


def test_the_targets_are_the_exported_definitions(callees: dict[str, str]) -> None:
    assert f"{PROJECT}.src.namedfn.named" in callees
    assert f"{PROJECT}.src.ident.hex" in callees
    assert f"{PROJECT}.src.aliased.pad" in callees


# Negative: what must not change.


def test_the_commonjs_require_call_is_still_exact(callees: dict[str, str]) -> None:
    targets = [qn for qn in callees if ".src.cjs." in qn]

    assert len(targets) == 1
    assert callees[targets[0]] == cs.EdgeResolution.EXACT.value


def test_a_default_import_of_a_module_without_a_default_export_binds_nothing(
    callees: dict[str, str],
) -> None:
    assert not [qn for qn in callees if ".src.plain." in qn]
