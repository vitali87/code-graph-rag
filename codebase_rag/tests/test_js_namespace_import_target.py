"""Issue #2934: a JS/TS namespace import records IMPORTS to the imported file.

`import * as math from "./utils/math.js"` stores the module itself as the
import's name, and the flush read that as `module.symbol`: next to a
`utils/index.ts` (or a sibling `utils.ts`) it stripped `math` and recorded
`IMPORTS app -> utils.index`, so `importers utils.math` was empty and the
barrel gained importers it never had.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

FILES = {
    "src/utils/strings.ts": "export function slug(s: string) {\n  return s;\n}\n",
    "src/utils/math.ts": (
        "export function sum(a: number, b: number) {\n  return a + b;\n}\n"
    ),
    "src/utils/index.ts": 'export const VERSION = "1";\n',
    "src/app.ts": (
        'import * as strings from "./utils/strings";\n'
        'import * as math from "./utils/math.js";\n\n'
        "export function run() {\n"
        '  return strings.slug("a") + math.sum(1, 2);\n'
        "}\n"
    ),
    "src/named.ts": (
        'import { sum } from "./utils/math.js";\n\n'
        "export function total() {\n  return sum(1, 2);\n}\n"
    ),
    "src/barrel_user.ts": (
        'import { VERSION } from "./utils";\n\n'
        "export function version() {\n  return VERSION;\n}\n"
    ),
    "src/lib/helpers.ts": "export function help() {\n  return 1;\n}\n",
    "src/lib.ts": 'export const LIB = "lib";\n',
    "src/sibling_user.ts": (
        'import * as helpers from "./lib/helpers";\n\n'
        "export function go() {\n  return helpers.help();\n}\n"
    ),
}


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("nsimp") / "nsimp"
    for rel, text in FILES.items():
        _write(root, rel, text)
    return _index(root, MagicMock())


def _imports(graph: RecordedGraph, module: str) -> set[str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix)
        for src, rel, dst, _props in graph.edges
        if rel == cs.RelationshipType.IMPORTS and src == f"{prefix}{module}"
    }


@pytest.mark.parametrize(
    ("module", "target"),
    [
        ("src.app", "src.utils.strings"),
        ("src.app", "src.utils.math"),
        ("src.sibling_user", "src.lib.helpers"),
    ],
    ids=["beside-an-index", "with-a-js-extension", "beside-a-same-named-file"],
)
def test_a_namespace_import_records_the_imported_file(
    graph: RecordedGraph, module: str, target: str
) -> None:
    assert target in _imports(graph, module)


@pytest.mark.parametrize(
    ("module", "barrel"),
    [("src.app", "src.utils.index"), ("src.sibling_user", "src.lib")],
    ids=["index", "same-named-file"],
)
def test_a_namespace_import_does_not_import_the_folder_barrel(
    graph: RecordedGraph, module: str, barrel: str
) -> None:
    assert barrel not in _imports(graph, module)


# Negative: what must not change.


def test_a_named_import_still_records_its_module(graph: RecordedGraph) -> None:
    assert _imports(graph, "src.named") == {"src.utils.math"}


def test_an_import_of_the_folder_still_records_its_index(graph: RecordedGraph) -> None:
    assert _imports(graph, "src.barrel_user") == {"src.utils.index"}
