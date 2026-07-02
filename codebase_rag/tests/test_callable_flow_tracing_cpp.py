from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_calls(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    if "cpp" not in parsers:
        pytest.skip("cpp parser not available")
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


def test_cpp_function_pointer_callback_is_traced(tmp_path: Path) -> None:
    # (H) apply invokes its function-pointer parameter cb; the function passed at the
    # (H) call site must be traced apply -> target.
    src = (
        "int apply(int (*cb)()) {\n"
        "    return cb();\n"
        "}\n\n"
        "int target() { return 1; }\n\n"
        "int use() { return apply(target); }\n"
    )
    calls = _run_calls(tmp_path, {"m.cpp": src})
    assert _has(calls, "apply", "target")
