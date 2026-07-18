from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_calls(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    # (H) Build the graph for `files` and return CALLS edges as (caller_qn, callee_qn),
    # (H) running the callable-flow finalize pass the real pipeline runs.
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    for name, src in files.items():
        (tmp_path / name).write_text(src, encoding="utf-8")
    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    )
    updater.run()
    out: set[tuple[str, str]] = set()
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] == "CALLS":
            out.add((c.args[0][2], c.args[2][2]))
    return out


def _has(calls: set[tuple[str, str]], caller_suffix: str, callee_suffix: str) -> bool:
    return any(
        a.endswith(caller_suffix) and b.endswith(callee_suffix) for a, b in calls
    )


def test_callback_invoked_in_nested_closure_is_traced(tmp_path: Path) -> None:
    # (H) `columns` invokes its callback only inside the returned closure. The edge
    # (H) columns -> _cells must still be emitted so _cells is not dead code.
    src = (
        "def columns(headers, cells_for):\n"
        "    def formatter(rows):\n"
        "        return [cells_for(r) for r in rows]\n"
        "    return formatter\n\n\n"
        "def _cells(o):\n"
        "    return ['x']\n\n\n"
        "table = columns(['H'], _cells)\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.columns", "m._cells")


def test_keyword_callback_invoked_in_nested_closure_is_traced(tmp_path: Path) -> None:
    # (H) The retry-decorator shape: is_retryable passed by keyword, invoked only in
    # (H) the nested wrapper closure.
    src = (
        "def with_retry(is_retryable=None):\n"
        "    def decorator(fn):\n"
        "        def wrapper(*a):\n"
        "            if is_retryable is not None:\n"
        "                is_retryable(Exception())\n"
        "            return fn(*a)\n"
        "        return wrapper\n"
        "    return decorator\n\n\n"
        "def _is_retryable(e):\n"
        "    return True\n\n\n"
        "d = with_retry(is_retryable=_is_retryable)\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.with_retry", "m._is_retryable")


def test_shadowed_name_in_nested_scope_is_not_traced(tmp_path: Path) -> None:
    # (H) A nested function that rebinds the callback name as its own parameter must
    # (H) NOT cause the outer parameter to be treated as invoked.
    src = (
        "def outer(cb):\n"
        "    def inner(cb):\n"
        "        return cb()\n"
        "    return inner\n\n\n"
        "def _target():\n"
        "    return 1\n\n\n"
        "x = outer(_target)\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert not _has(calls, "m.outer", "m._target")


def test_dict_dispatch_table_references_are_traced(tmp_path: Path) -> None:
    # (H) Functions placed as dict values form a dispatch table invoked elsewhere by
    # (H) a dynamic key; the module-level table reference keeps them reachable.
    src = (
        "def _handle_a(x):\n"
        "    return 1\n\n\n"
        "def _handle_b(x):\n"
        "    return 2\n\n\n"
        "HANDLERS = {'a': _handle_a, 'b': _handle_b}\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m", "m._handle_a")
    assert _has(calls, "m", "m._handle_b")


def test_list_dispatch_table_references_are_traced(tmp_path: Path) -> None:
    # (H) A list of functions (a formatter/pipeline table) keeps its entries reachable.
    src = (
        "def _step_one(x):\n"
        "    return 1\n\n\n"
        "def _step_two(x):\n"
        "    return 2\n\n\n"
        "STEPS = [_step_one, _step_two]\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m", "m._step_one")
    assert _has(calls, "m", "m._step_two")


def test_factory_returned_closure_flows_callback(tmp_path: Path) -> None:
    # (H) The imperative-decorator pattern: a factory returns a closure that invokes
    # (H) its argument; the callback passed at the alias call site must be traced.
    src = (
        "def with_retry(is_retryable=None):\n"
        "    def decorator(fn):\n"
        "        def wrapper(*a):\n"
        "            return fn(*a)\n"
        "        return wrapper\n"
        "    return decorator\n\n\n"
        "class C:\n"
        "    def run(self):\n"
        "        deco = with_retry()\n"
        "        wrapped = deco(self._api_call)\n"
        "        return wrapped()\n\n"
        "    def _api_call(self):\n"
        "        return 1\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    # (H) with_retry.decorator receives _api_call and its wrapper invokes it.
    assert _has(calls, "with_retry.decorator", "m.C._api_call")


def test_method_passed_to_external_callee_is_referenced(tmp_path: Path) -> None:
    # (H) The grpclib/betterproto shape: a handler method is passed to an external
    # (H) framework constructor and dispatched by the runtime. The call chain cannot
    # (H) enter the external callee, but the reference keeps the handler reachable.
    src = (
        "import grpclib.const\n\n\n"
        "class Service:\n"
        "    def __mapping__(self):\n"
        "        return {\n"
        "            '/svc/Get': grpclib.const.Handler(self.__rpc_get, 1),\n"
        "        }\n\n"
        "    async def __rpc_get(self, stream):\n"
        "        return 1\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "Service.__mapping__", "Service.__rpc_get")


def test_first_party_callee_keeps_precise_attribution(tmp_path: Path) -> None:
    # (H) When the callee IS first-party, the callback is attributed to the callee
    # (H) (precise call graph), NOT to the enclosing scope via the reference fallback.
    src = (
        "def apply(fn):\n"
        "    def run():\n"
        "        return fn()\n"
        "    return run\n\n\n"
        "def _cb():\n"
        "    return 1\n\n\n"
        "def caller():\n"
        "    return apply(_cb)\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.apply", "m._cb")  # (H) precise: callee -> callback
    assert not _has(calls, "m.caller", "m._cb")  # (H) not the enclosing scope


def test_self_method_callback_in_closure_is_traced(tmp_path: Path) -> None:
    # (H) A bound method passed as a callback, invoked in the callee's nested closure.
    src = (
        "def apply(fn):\n"
        "    def run():\n"
        "        return fn()\n"
        "    return run\n\n\n"
        "class C:\n"
        "    def go(self):\n"
        "        return apply(self._helper)\n\n"
        "    def _helper(self):\n"
        "        return 1\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.apply", "m.C._helper")
