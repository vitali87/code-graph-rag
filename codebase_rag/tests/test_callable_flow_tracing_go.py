from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_calls(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    if "go" not in parsers:
        pytest.skip("go parser not available")
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


def test_go_callback_invoked_directly_is_traced(tmp_path: Path) -> None:
    # apply invokes its parameter cb directly; the function value passed at the
    # call site must be traced apply -> target.
    src = (
        "package m\n\n"
        "func apply(cb func() int) int {\n"
        "\treturn cb()\n"
        "}\n\n"
        "func target() int { return 1 }\n\n"
        "var _ = apply(target)\n"
    )
    calls = _run_calls(tmp_path, {"m.go": src})
    assert _has(calls, "m.apply", "m.target")


def test_go_callback_invoked_in_closure_is_traced(tmp_path: Path) -> None:
    # apply invokes cb only inside a returned func_literal closure.
    src = (
        "package m\n\n"
        "func apply(cb func() int) func() int {\n"
        "\trun := func() int { return cb() }\n"
        "\treturn run\n"
        "}\n\n"
        "func target() int { return 1 }\n\n"
        "var _ = apply(target)\n"
    )
    calls = _run_calls(tmp_path, {"m.go": src})
    assert _has(calls, "m.apply", "m.target")


def test_go_factory_alias_callback_stays_reachable(tmp_path: Path) -> None:
    # run := makeRunner(); run(target): run is unresolved (holds a returned func
    # value), so target stays reachable via the reference edge. The precise
    # closure edge needs the func literal registered as a function, which Go
    # lacks, but the callback must not be dropped.
    src = (
        "package m\n\n"
        "func makeRunner() func(func() int) int {\n"
        "\trunner := func(cb func() int) int { return cb() }\n"
        "\treturn runner\n"
        "}\n\n"
        "func target() int { return 1 }\n\n"
        "func main() {\n"
        "\trun := makeRunner()\n"
        "\trun(target)\n"
        "}\n"
    )
    calls = _run_calls(tmp_path, {"m.go": src})
    assert _has(calls, "m.main", "m.target")
