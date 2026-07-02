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


def test_cpp_factory_alias_callback_stays_reachable(tmp_path: Path) -> None:
    # (H) auto run = makeRunner(); run(target): run is unresolved (holds a returned
    # (H) std::function), so target is kept reachable by the reference edge from the
    # (H) calling scope. The precise closure edge needs the lambda to be a registered
    # (H) function, which C++ does not provide, but the callback must not be dropped.
    src = (
        "#include <functional>\n"
        "std::function<int(std::function<int()>)> makeRunner() {\n"
        "    return [](std::function<int()> cb) { return cb(); };\n"
        "}\n"
        "int target() { return 1; }\n"
        "int main() {\n"
        "    auto run = makeRunner();\n"
        "    return run(target);\n"
        "}\n"
    )
    calls = _run_calls(tmp_path, {"m.cpp": src})
    assert _has(calls, "main", "target")


def test_cpp_nested_lambda_shadowing_suppresses_forbidden_edge(tmp_path: Path) -> None:
    # (H) shadow's own cb parameter is never invoked; the nested lambda has its own
    # (H) cb parameter that shadows it, and only that inner cb is called. The flow
    # (H) tracer must subtract the lambda's bound names before descending, so it must
    # (H) NOT conclude shadow invokes its cb and emit shadow -> target.
    src = (
        "int target() { return 1; }\n"
        "int shadow(int (*cb)()) {\n"
        "    auto inner = [](int (*cb)()) { return cb(); };\n"
        "    return 0;\n"
        "}\n"
        "int main() { return shadow(target); }\n"
    )
    calls = _run_calls(tmp_path, {"m.cpp": src})
    assert not _has(calls, "shadow", "target")
