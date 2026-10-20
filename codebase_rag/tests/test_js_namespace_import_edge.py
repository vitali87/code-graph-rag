"""A JS/TS import naming a whole module keeps its IMPORTS edge on it (#2934).

`import * as math from "./utils/math.js"` beside `utils/index.ts` recorded
`IMPORTS app -> utils.index`: the flush read the module `utils.math` as the
name `math` of the module `utils`, which the directory's index (or a sibling
`utils.ts`) answers. A barrel namespace-importing its own sibling imported
itself, and the edge was dropped. A whole-module `require` went the same way.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater

_FILES = {
    "src/utils/strings.ts": "export function slug(s: string) {\n\treturn s;\n}\n",
    "src/utils/math.ts": (
        "export default function sum(a: number, b: number) {\n\treturn a + b;\n}\n"
        "export const PI = 3;\n"
    ),
    "src/utils/index.ts": (
        'import * as math from "./math";\nexport { math };\n'
        'export const VERSION = "1";\n'
    ),
    "src/app.ts": (
        'import * as strings from "./utils/strings";\n'
        'import * as math from "./utils/math.js";\n\n'
        'export function run() {\n\treturn strings.slug("a") + math.default(1, 2);\n}\n'
    ),
    "src/mixed.ts": (
        'import sum, * as all from "./utils/math";\n'
        "export const b = sum(1, 2) + all.PI;\n"
    ),
    "src/named.ts": 'import { PI } from "./utils/math";\nexport const n = PI;\n',
    "src/dir.ts": 'import * as u from "./utils";\nexport const v = u.VERSION;\n',
    "src/pkg.ts": 'import * as path from "path";\nexport const p = path.join("a");\n',
    "src/req.js": 'const m = require("./utils/math");\nmodule.exports = m.PI;\n',
    # A file named like the directory beside it: `./util/fmt` is the file in
    # the directory, never `util.ts`.
    "lib/util.ts": "export const x = 1;\n",
    "lib/util/fmt.ts": "export function f() {\n\treturn 1;\n}\n",
    "lib/use.ts": 'import * as fmt from "./util/fmt";\nexport const y = fmt.f();\n',
}


def _imports(repo: Path, mock: MagicMock) -> set[tuple[str, str, int, str | None]]:
    # (importer, target, line, imported_name) of every IMPORTS edge.
    prefix = f"{repo.name}."
    out: set[tuple[str, str, int, str | None]] = set()
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] != cs.RelationshipType.IMPORTS.value:
            continue
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        out.add(
            (
                str(c.args[0][2]).removeprefix(prefix),
                str(c.args[2][2]).removeprefix(prefix),
                int(props.get(cs.KEY_LINE, 0)),
                props.get(cs.KEY_IMPORTED_NAME),
            )
        )
    return out


@pytest.fixture
def imports(
    temp_repo: Path, mock_ingestor: MagicMock
) -> set[tuple[str, str, int, str | None]]:
    for rel, text in _FILES.items():
        (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (temp_repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)
    return _imports(temp_repo, mock_ingestor)


def _from(
    imports: set[tuple[str, str, int, str | None]], importer: str
) -> set[tuple[str, int, str | None]]:
    return {(to, line, name) for frm, to, line, name in imports if frm == importer}


@pytest.mark.parametrize(
    ("importer", "expected"),
    [
        (
            "src.app",
            {("src.utils.strings", 1, "*"), ("src.utils.math", 2, "*")},
        ),
        ("src.utils.index", {("src.utils.math", 1, "*")}),
        (
            "src.mixed",
            {("src.utils.math", 1, "default"), ("src.utils.math", 1, "*")},
        ),
        ("src.req", {("src.utils.math", 1, None)}),
        ("lib.use", {("lib.util.fmt", 1, "*")}),
    ],
    ids=[
        "namespace-beside-an-index",
        "barrel-namespace-imports-its-sibling",
        "default-and-namespace",
        "whole-module-require",
        "file-named-like-the-directory",
    ],
)
def test_a_whole_module_import_targets_that_module(
    imports: set[tuple[str, str, int, str | None]],
    importer: str,
    expected: set[tuple[str, int, str | None]],
) -> None:
    assert _from(imports, importer) == expected


def test_the_directory_index_keeps_only_its_real_importers(
    imports: set[tuple[str, str, int, str | None]],
) -> None:
    importers = {frm for frm, to, _line, _name in imports if to == "src.utils.index"}
    assert importers == {"src.dir"}, sorted(imports)


@pytest.mark.parametrize(
    ("importer", "expected"),
    [
        ("src.named", {("src.utils.math", 1, "PI")}),
        ("src.dir", {("src.utils.index", 1, "*")}),
        ("src.pkg", {("path", 1, "*")}),
    ],
    ids=["named-import", "directory-namespace-import", "package-namespace-import"],
)
def test_other_imports_keep_their_targets(
    imports: set[tuple[str, str, int, str | None]],
    importer: str,
    expected: set[tuple[str, int, str | None]],
) -> None:
    # Negatives: a named import strips its name, a directory's namespace
    # import reaches the index, and a package stays external.
    assert _from(imports, importer) == expected


def test_calls_through_the_namespace_still_bind_exactly(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    for rel, text in _FILES.items():
        (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (temp_repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    prefix = f"{temp_repo.name}."
    calls = {
        str(c.args[2][2]).removeprefix(prefix)
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == cs.RelationshipType.CALLS.value
        and str(c.args[0][2]).removeprefix(prefix) == "src.app.run"
    }
    assert "src.utils.strings.slug" in calls, calls
