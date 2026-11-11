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


def test_an_explicit_extension_never_names_the_directory(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: `./fmt.js` names the file `fmt.js` (or `fmt.ts`), never the
    # `fmt/` directory's index, so with no such file nothing is bound exactly.
    _write(
        temp_repo,
        {
            "js/fmt/index.js": _JS_INDEX,
            "js/a.js": 'import { trim } from "./fmt.js";\n'
            "export function viaExt(x) { return trim(x); }\n",
        },
    )
    updater = create_and_run_updater(temp_repo, mock_ingestor)

    mapping = updater.factory.import_processor.import_mapping
    assert mapping[f"{temp_repo.name}.js.a"]["trim"] == f"{temp_repo.name}.js.fmt.trim"
    calls = _calls(temp_repo, mock_ingestor)
    assert ("js.fmt.index.trim", cs.EdgeResolution.EXACT) not in calls.get(
        "js.a.viaExt", set()
    ), calls


_ADD = "export function add(a: number, b: number): number { return a + b; }\n"


def test_a_local_shadowing_a_namespace_import_keeps_its_own_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `const math = new Local()` shadows `import * as math`, so `math.add()`
    # is `Local.add`, not the barrel's re-exported `add`.
    _write(
        temp_repo,
        {
            "src/lib/ops.ts": _ADD,
            "src/lib/index.ts": 'export * from "./ops";\n',
            "src/app.ts": 'import * as math from "./lib";\n'
            "class Local { add(a: number, b: number): number { return a - b; } }\n"
            "export function run(): number {\n"
            "  const math = new Local();\n  return math.add(1, 2);\n}\n",
        },
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert calls.get("src.app.run") == {
        ("src.app.Local.add", cs.EdgeResolution.EXACT)
    }, calls


@pytest.mark.parametrize(
    "barrel",
    [
        'import * as math from "./local";\nexport * as math from "./remote";\n',
        'export * as math from "./remote";\nimport * as math from "./local";\n',
    ],
    ids=["import-first", "export-first"],
)
def test_a_namespace_export_does_not_rebind_the_barrels_own_import(
    temp_repo: Path, mock_ingestor: MagicMock, barrel: str
) -> None:
    # `export * as math` publishes `remote` without binding a local `math`:
    # the barrel's own `math` is still its import of `local`.
    _write(
        temp_repo,
        {
            "src/local.ts": _ADD,
            "src/remote.ts": _ADD,
            "src/barrel.ts": f"{barrel}"
            "export function run(): number { return math.add(1, 2); }\n",
            "src/app.ts": 'import { math } from "./barrel";\n'
            "export function use(): number { return math.add(1, 2); }\n",
        },
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert calls.get("src.barrel.run") == {
        ("src.local.add", cs.EdgeResolution.EXACT)
    }, calls
    assert calls.get("src.app.use") == {("src.remote.add", cs.EdgeResolution.EXACT)}, (
        calls
    )


def test_a_nested_namespace_export_binds_its_members_exactly(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `math.util.add()` through `export * as math` then `export * as util`:
    # each namespace is followed to the module it names.
    _write(
        temp_repo,
        {
            "src/lib/util.ts": _ADD,
            "src/lib/math.ts": 'export * as util from "./util";\n',
            "src/lib/index.ts": 'export * as math from "./math";\n',
            "src/app.ts": 'import { math } from "./lib";\n'
            "export function run(): number { return math.util.add(1, 2); }\n",
        },
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert calls.get("src.app.run") == {
        ("src.lib.util.add", cs.EdgeResolution.EXACT)
    }, calls


def test_a_namespace_export_passes_through_a_star_reexport(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `outer` re-exports everything `inner` exports, its `math` namespace
    # included, so `math.add()` imported from `outer` is `ops.add`.
    _write(
        temp_repo,
        {
            "src/inner/ops.ts": _ADD,
            "src/inner/index.ts": 'export * as math from "./ops";\n',
            "src/outer/index.ts": 'export * from "../inner";\n',
            "src/app.ts": 'import { math } from "./outer";\n'
            "export function run(): number { return math.add(1, 2); }\n",
        },
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert calls.get("src.app.run") == {
        ("src.inner.ops.add", cs.EdgeResolution.EXACT)
    }, calls


def test_a_star_reexport_does_not_pass_on_a_module_its_source_never_exports(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: `lib.ts` exports no `util`; `lib` + `util` only spells the
    # `lib/util.ts` module, which the star re-export does not pass on.
    _write(
        temp_repo,
        {
            "src/lib/util.ts": _ADD,
            "src/lib.ts": "export function other(): number { return 0; }\n",
            "src/barrel.ts": 'export * from "./lib";\n',
            "src/app.ts": 'import { util } from "./barrel";\n'
            "export function run(): number { return util.add(1, 2); }\n",
        },
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert ("src.lib.util.add", cs.EdgeResolution.EXACT) not in calls.get(
        "src.app.run", set()
    ), calls
