from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_calls(
    tmp_path: Path, files: dict[str, str], lang_key: str
) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    if lang_key not in parsers:
        pytest.skip(f"{lang_key} parser not available")
    for name, src in files.items():
        (tmp_path / name).write_text(src, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    out: set[tuple[str, str]] = set()
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] == "CALLS":
            out.add((c.args[0][2], c.args[2][2]))
    return out


def _has(calls: set[tuple[str, str]], caller_suffix: str, callee_suffix: str) -> bool:
    return any(
        a.endswith(caller_suffix) and b.endswith(callee_suffix) for a, b in calls
    )


JS_DIRECT = (
    "function apply(cb) {\n"
    "    return cb();\n"
    "}\n\n"
    "function target() { return 1; }\n\n"
    "apply(target);\n"
)

JS_CLOSURE = (
    "function apply(cb) {\n"
    "    const run = () => cb();\n"
    "    return run;\n"
    "}\n\n"
    "function target() { return 1; }\n\n"
    "apply(target);\n"
)

TS_TYPED = (
    "function apply(cb: () => number): number {\n"
    "    return cb();\n"
    "}\n\n"
    "function target(): number { return 1; }\n\n"
    "apply(target);\n"
)


def test_js_callback_invoked_directly_is_traced(tmp_path: Path) -> None:
    calls = _run_calls(tmp_path, {"m.js": JS_DIRECT}, "javascript")
    assert _has(calls, "m.apply", "m.target")


def test_js_callback_invoked_in_arrow_closure_is_traced(tmp_path: Path) -> None:
    calls = _run_calls(tmp_path, {"m.js": JS_CLOSURE}, "javascript")
    assert _has(calls, "m.apply", "m.target")


def test_ts_typed_callback_param_is_traced(tmp_path: Path) -> None:
    calls = _run_calls(tmp_path, {"m.ts": TS_TYPED}, "typescript")
    assert _has(calls, "m.apply", "m.target")
