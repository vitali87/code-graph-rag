from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_calls(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    # (H) Build the graph for `files` and return CALLS edges as (caller_qn, callee_qn).
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
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


def test_self_call_dispatches_to_subclass_override(tmp_path: Path) -> None:
    # (H) Base.run invokes self.op(); Sub overrides op. The runtime receiver may be a
    # (H) Sub, so the call graph must connect Base.run -> Sub.op or Sub.op is dead.
    src = (
        "class Base:\n"
        "    def run(self):\n"
        "        return self.op()\n"
        "    def op(self):\n"
        "        return 0\n\n\n"
        "class Sub(Base):\n"
        "    def op(self):\n"
        "        return 1\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.Base.run", "m.Sub.op")
    # (H) The base edge must still be present.
    assert _has(calls, "m.Base.run", "m.Base.op")


def test_self_call_dispatches_to_transitive_override(tmp_path: Path) -> None:
    # (H) Override two levels down must also be connected.
    src = (
        "class Base:\n"
        "    def run(self):\n"
        "        return self.op()\n"
        "    def op(self):\n"
        "        return 0\n\n\n"
        "class Mid(Base):\n"
        "    pass\n\n\n"
        "class Leaf(Mid):\n"
        "    def op(self):\n"
        "        return 2\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.Base.run", "m.Leaf.op")


def test_no_override_dispatch_without_subclasses(tmp_path: Path) -> None:
    # (H) A method with no overriding subclass must not gain spurious edges.
    src = (
        "class Only:\n"
        "    def run(self):\n"
        "        return self.op()\n"
        "    def op(self):\n"
        "        return 0\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.Only.run", "m.Only.op")
    assert not any(
        a.endswith("m.Only.run") and b.endswith(".op") and not b.endswith("Only.op")
        for a, b in calls
    )
