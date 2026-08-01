from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
    }


def test_nested_class_method_call_edge_uses_full_qn(tmp_path: Path) -> None:
    # A method of a nested class (Outer.Inner.run) must emit its CALLS edge
    # from the FULL qn that the definition pass registered
    # (module.Outer.Inner.run), not the enclosing-class-dropping
    # module.Inner.run, or the edge dangles off a phantom node.
    files = {
        "m.py": (
            "def helper():\n"
            "    return 1\n\n\n"
            "class Outer:\n"
            "    class Inner:\n"
            "        def run(self):\n"
            "            return helper()\n"
        ),
    }
    rels = _run(tmp_path, files)
    calls = {(a, b) for a, r, b in rels if r == "CALLS"}
    assert any(
        a.endswith("m.Outer.Inner.run") and b.endswith("m.helper") for a, b in calls
    )
    assert not any(
        a.endswith("m.Inner.run") and not a.endswith("Outer.Inner.run")
        for a, _ in calls
    )


def test_nested_class_method_qn_matches_registered_node(tmp_path: Path) -> None:
    # The caller qn of a nested-class method CALLS edge must be a real
    # registered node, so an INSTANTIATES/CALLS traversal reaches it.
    files = {
        "m.py": (
            "def helper():\n"
            "    return 1\n\n\n"
            "class Outer:\n"
            "    class Inner:\n"
            "        def run(self):\n"
            "            return helper()\n"
        ),
    }
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    (tmp_path / "m.py").write_text(files["m.py"], encoding="utf-8")
    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    )
    updater.run()
    registry = updater.factory.call_processor._resolver.function_registry
    callers = {
        c.args[0][2]
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == "CALLS" and c.args[2][2].endswith("m.helper")
    }
    run_callers = {q for q in callers if q.endswith("run")}
    assert run_callers
    assert all(q in registry for q in run_callers)
