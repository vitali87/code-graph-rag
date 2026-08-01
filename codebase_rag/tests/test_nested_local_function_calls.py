from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_calls(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    # Build the graph for `files` and return CALLS edges as (caller_qn, callee_qn).
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


def test_direct_call_to_nested_function_is_traced(tmp_path: Path) -> None:
    # get_metadata defines traverse() and invokes it directly by name. The edge
    # get_metadata -> get_metadata.traverse must exist so traverse is not dead.
    src = (
        "def get_metadata(root):\n"
        "    def traverse(node):\n"
        "        for child in node.children:\n"
        "            traverse(child)\n"
        "    traverse(root)\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.get_metadata", "m.get_metadata.traverse")


def test_recursive_nested_function_self_call_is_traced(tmp_path: Path) -> None:
    # traverse calls itself; the self-recursive edge must resolve to the nested def.
    src = (
        "def get_metadata(root):\n"
        "    def traverse(node):\n"
        "        for child in node.children:\n"
        "            traverse(child)\n"
        "    traverse(root)\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.get_metadata.traverse", "m.get_metadata.traverse")


def test_method_nested_function_call_is_traced(tmp_path: Path) -> None:
    # A nested def inside a method, called by name; the edge from the method to
    # the nested function must exist (OperationResult.get_metadata.traverse shape).
    src = (
        "class OperationResult:\n"
        "    def get_metadata(self):\n"
        "        def traverse(node):\n"
        "            return node\n"
        "        return traverse(self)\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(
        calls,
        "m.OperationResult.get_metadata",
        "m.OperationResult.get_metadata.traverse",
    )


def test_nested_function_passed_to_builtin_filter_is_traced(tmp_path: Path) -> None:
    # filter_date_fn is defined locally and passed to filter(); the enclosing
    # scope must reference it so it is not reported dead (filter_runs shape).
    src = (
        "def filter_runs(items):\n"
        "    def filter_date_fn(run):\n"
        "        return run.ok\n"
        "    return list(filter(filter_date_fn, items))\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.filter_runs", "m.filter_runs.filter_date_fn")


def test_sibling_methods_resolve_own_nested_function(tmp_path: Path) -> None:
    # Two methods each define a nested traverse and call it. Each call must bind to
    # ITS OWN enclosing scope's nested function, not the first same-named sibling,
    # or the other method's traverse gets no inbound edge (OperationResult shape:
    # get_llm_results.traverse and get_metadata.traverse both exist).
    src = (
        "class Op:\n"
        "    def get_a(self):\n"
        "        def traverse(node):\n"
        "            return node\n"
        "        return traverse(self)\n"
        "    def get_b(self):\n"
        "        def traverse(node):\n"
        "            return node\n"
        "        return traverse(self)\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.Op.get_a", "m.Op.get_a.traverse")
    assert _has(calls, "m.Op.get_b", "m.Op.get_b.traverse")


def test_sibling_module_functions_resolve_own_nested_function(
    tmp_path: Path,
) -> None:
    # Two module-level functions each define a nested helper of the same name; each
    # call must bind to its own (run_determination/run_determinations._get_param).
    src = (
        "def run_one(kwargs):\n"
        "    def _get_param(name):\n"
        "        return kwargs[name]\n"
        "    return _get_param('a')\n\n\n"
        "def run_many(kwargs):\n"
        "    def _get_param(name):\n"
        "        return kwargs[name]\n"
        "    return _get_param('b')\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.run_one", "m.run_one._get_param")
    assert _has(calls, "m.run_many", "m.run_many._get_param")


def test_sibling_callback_args_resolve_own_nested_function(tmp_path: Path) -> None:
    # Two sibling functions each define a nested worker and pass it as a callback to
    # a consumer that invokes it. Each callback arg must resolve to ITS OWN nested
    # worker, so both workers are reached (create_context-as-kwarg shape). Without
    # threading the caller scope into callback resolution, both bind to the first.
    src = (
        "def consume(cb):\n"
        "    return cb()\n\n\n"
        "def outer_a():\n"
        "    def worker():\n"
        "        return 1\n"
        "    return consume(worker)\n\n\n"
        "def outer_b():\n"
        "    def worker():\n"
        "        return 2\n"
        "    return consume(worker)\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.consume", "m.outer_a.worker")
    assert _has(calls, "m.consume", "m.outer_b.worker")
