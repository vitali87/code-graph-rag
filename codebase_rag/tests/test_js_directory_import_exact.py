"""A JS/TS import of a directory binds its `index` module exactly (#3249).

`import { pad } from "./fmt"` with `fmt/index.ts` got its IMPORTS edge, but
the named import was recorded as `<dir>.fmt.pad`, a qn that does not exist,
so every call through it bound by name (`heuristic`) while the explicit
`"./fmt/index"` bound `exact`; `cgr rename` then refused the definition.
Node and TypeScript resolve `./fmt` to `fmt.ts`/`fmt.js` first, else to
`fmt/index.*`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater

_TS_INDEX = (
    "export function pad(s: string, n: number): string { return s.padStart(n); }\n"
)
_JS_INDEX = "export function trim(s) { return s.trim(); }\n"

# (caller file, source, caller qn, callee qn)
_CASES = {
    "ts-named-import": (
        "web/a.ts",
        'import { pad } from "./fmt";\n'
        "export function viaDir(x: string): string { return pad(x, 4); }\n",
        "web.a.viaDir",
        "web.fmt.index.pad",
    ),
    "ts-namespace-import": (
        "web/ns.ts",
        'import * as fmt from "./fmt";\n'
        "export function viaNs(x: string): string { return fmt.pad(x, 4); }\n",
        "web.ns.viaNs",
        "web.fmt.index.pad",
    ),
    "ts-parent-directory": (
        "web/deep/c.ts",
        'import { pad } from "../fmt";\n'
        "export function viaParent(x: string): string { return pad(x, 4); }\n",
        "web.deep.c.viaParent",
        "web.fmt.index.pad",
    ),
    "js-named-import": (
        "js/a.js",
        'import { trim } from "./fmt";\nexport function viaDirJs(x) { return trim(x); }\n',
        "js.a.viaDirJs",
        "js.fmt.index.trim",
    ),
    "js-require": (
        "js/c.js",
        'const { trim } = require("./fmt");\n'
        "function viaRequire(x) { return trim(x); }\nmodule.exports = { viaRequire };\n",
        "js.c.viaRequire",
        "js.fmt.index.trim",
    ),
    "explicit-index": (
        "web/b.ts",
        'import { pad } from "./fmt/index";\n'
        "export function viaIndex(x: string): string { return pad(x, 4); }\n",
        "web.b.viaIndex",
        "web.fmt.index.pad",
    ),
}


def _calls(repo: Path, mock: MagicMock) -> dict[str, set[tuple[str, str]]]:
    prefix = f"{repo.name}."
    out: dict[str, set[tuple[str, str]]] = {}
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] != cs.RelationshipType.CALLS.value:
            continue
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        out.setdefault(str(c.args[0][2]).removeprefix(prefix), set()).add(
            (str(c.args[2][2]).removeprefix(prefix), str(props.get(cs.KEY_RESOLUTION)))
        )
    return out


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")


@pytest.mark.parametrize("case", list(_CASES))
def test_a_directory_import_binds_the_index_module_exactly(
    temp_repo: Path, mock_ingestor: MagicMock, case: str
) -> None:
    rel, text, caller, callee = _CASES[case]
    _write(
        temp_repo,
        {"web/fmt/index.ts": _TS_INDEX, "js/fmt/index.js": _JS_INDEX, rel: text},
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert calls.get(caller) == {(callee, cs.EdgeResolution.EXACT)}, calls


def test_a_file_beside_the_directory_wins(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: `./fmt` names `fmt.ts` when it exists, as Node and TypeScript
    # resolve it; the directory's index is only the fallback.
    _write(
        temp_repo,
        {
            "web/fmt/index.ts": _TS_INDEX,
            "web/fmt.ts": "export function pad(s: string): string { return s; }\n",
            "web/a.ts": 'import { pad } from "./fmt";\n'
            "export function viaDir(x: string): string { return pad(x); }\n",
        },
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert calls.get("web.a.viaDir") == {("web.fmt.pad", cs.EdgeResolution.EXACT)}, (
        calls
    )


def test_a_directory_without_an_index_is_not_rewritten(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: a directory with no index entry point resolves to nothing;
    # the import is not pointed at an `index` module that does not exist.
    _write(
        temp_repo,
        {
            "web/fmt/pad.ts": _TS_INDEX,
            "web/a.ts": 'import { pad } from "./fmt";\n'
            "export function viaDir(x: string): string { return pad(x, 4); }\n",
        },
    )
    updater = create_and_run_updater(temp_repo, mock_ingestor)

    mapping = updater.factory.import_processor.import_mapping
    assert mapping[f"{temp_repo.name}.web.a"]["pad"] == f"{temp_repo.name}.web.fmt.pad"


@pytest.mark.parametrize("namespace", ["math", "m"])
def test_a_namespace_reexported_directory_binds_its_members_exactly(
    temp_repo: Path, mock_ingestor: MagicMock, namespace: str
) -> None:
    # `export * as <ns> from "./math"` in a barrel. With the namespace named
    # after the directory, `../lib` + `math` once happened to spell `lib/math`;
    # any other name never resolved. Both now follow the namespace export.
    _write(
        temp_repo,
        {
            "src/lib/math/add.ts": "export function add(a: number, b: number): number"
            " { return a + b; }\n",
            "src/lib/math/index.ts": 'export * from "./add";\n',
            "src/lib/index.ts": f'export * as {namespace} from "./math";\n',
            "src/app.ts": f'import {{ {namespace} }} from "./lib";\n'
            f"export function run(): number {{ return {namespace}.add(1, 2); }}\n",
        },
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert calls.get("src.app.run") == {
        ("src.lib.math.add.add", cs.EdgeResolution.EXACT)
    }, calls
