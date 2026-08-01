from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_nodes, get_relationships, run_updater


def _function_qns(ingestor: MagicMock) -> set[str]:
    return {c.args[1]["qualified_name"] for c in get_nodes(ingestor, "Function")}


def _call_pairs(ingestor: MagicMock) -> set[tuple[str, str]]:
    return {(c.args[0][2], c.args[2][2]) for c in get_relationships(ingestor, "CALLS")}


def test_mjs_module_functions_and_calls_ingested(temp_repo: Path) -> None:
    # ESM packages ship .mjs files (config files, dual-package libs); they
    # parse with the same grammar as .js and must produce the same graph.
    root = temp_repo / "mjsproj"
    root.mkdir()
    (root / "util.mjs").write_text(
        "export function helper() { return 1 }\n", encoding="utf-8"
    )
    (root / "main.mjs").write_text(
        "import { helper } from './util.mjs'\n"
        "export function run() { return helper() }\n",
        encoding="utf-8",
    )

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing="javascript")

    qns = _function_qns(ingestor)
    assert any(qn.endswith("util.helper") for qn in qns), qns
    assert any(qn.endswith("main.run") for qn in qns), qns
    assert any(
        caller.endswith("main.run") and callee.endswith("util.helper")
        for caller, callee in _call_pairs(ingestor)
    ), _call_pairs(ingestor)


def test_cjs_module_functions_and_calls_ingested(temp_repo: Path) -> None:
    root = temp_repo / "cjsproj"
    root.mkdir()
    (root / "util.cjs").write_text(
        "function helper() { return 1 }\nmodule.exports = { helper }\n",
        encoding="utf-8",
    )
    (root / "main.cjs").write_text(
        "const { helper } = require('./util.cjs')\n"
        "function run() { return helper() }\n"
        "module.exports = { run }\n",
        encoding="utf-8",
    )

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing="javascript")

    qns = _function_qns(ingestor)
    assert any(qn.endswith("util.helper") for qn in qns), qns
    assert any(qn.endswith("main.run") for qn in qns), qns
    assert any(
        caller.endswith("main.run") and callee.endswith("util.helper")
        for caller, callee in _call_pairs(ingestor)
    ), _call_pairs(ingestor)


def test_mts_and_cts_typescript_variants_ingested(temp_repo: Path) -> None:
    # TypeScript's ESM/CJS variants of .ts: same grammar, same graph.
    root = temp_repo / "mtsproj"
    root.mkdir()
    (root / "util.mts").write_text(
        "export function helper(): number { return 1 }\n", encoding="utf-8"
    )
    (root / "legacy.cts").write_text(
        "export function older(): number { return 2 }\n", encoding="utf-8"
    )

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing="typescript")

    qns = _function_qns(ingestor)
    assert any(qn.endswith("util.helper") for qn in qns), qns
    assert any(qn.endswith("legacy.older") for qn in qns), qns


def test_extensionless_relative_import_resolves_to_mjs(temp_repo: Path) -> None:
    # `import './util'` must probe the .mjs/.cjs candidates like .js/.ts.
    root = temp_repo / "resolveproj"
    root.mkdir()
    (root / "util.mjs").write_text(
        "export function helper() { return 1 }\n", encoding="utf-8"
    )
    (root / "main.js").write_text(
        "import { helper } from './util'\nexport function run() { return helper() }\n",
        encoding="utf-8",
    )

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing="javascript")

    assert any(
        caller.endswith("main.run") and callee.endswith("util.helper")
        for caller, callee in _call_pairs(ingestor)
    ), _call_pairs(ingestor)
