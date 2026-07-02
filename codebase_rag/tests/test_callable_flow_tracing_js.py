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


JS_PASS_THROUGH = (
    "function apply(cb) {\n"
    "    return cb();\n"
    "}\n\n"
    "function wrapper(cb) {\n"
    "    return apply(cb);\n"
    "}\n\n"
    "function target() { return 1; }\n\n"
    "wrapper(target);\n"
)


def test_js_callback_passed_through_wrapper_is_traced(tmp_path: Path) -> None:
    # (H) target flows into wrapper's cb param, which wrapper hands to apply; the
    # (H) fixpoint must propagate target through the pass-through param so apply,
    # (H) the function that actually invokes cb, gets a CALLS edge to target.
    calls = _run_calls(tmp_path, {"m.js": JS_PASS_THROUGH}, "javascript")
    assert _has(calls, "m.apply", "m.target")


JS_EXTERNAL_CALLBACK = (
    "function target() { return 1; }\n\n"
    "function liveEntry() {\n"
    "    setTimeout(target, 1);\n"
    "}\n\n"
    "liveEntry();\n"
)


def test_js_callback_handed_to_external_callee_is_referenced(tmp_path: Path) -> None:
    # (H) setTimeout is not first-party, so the chain cannot be followed into it,
    # (H) but target is handed to it to be invoked; a reference edge from the
    # (H) enclosing scope must keep target reachable (matching the Python path).
    calls = _run_calls(tmp_path, {"m.js": JS_EXTERNAL_CALLBACK}, "javascript")
    assert _has(calls, "m.liveEntry", "m.target")


JS_FACTORY_ALIAS = (
    "function makeRunner() {\n"
    "    function runner(cb) { return cb(); }\n"
    "    return runner;\n"
    "}\n\n"
    "function target() { return 1; }\n\n"
    "const run = makeRunner();\n"
    "run(target);\n"
)


def test_js_factory_returned_closure_flows_callback(tmp_path: Path) -> None:
    # (H) const run = makeRunner(); run(target): run holds the closure makeRunner
    # (H) returns, and invoking it hands target to the closure's cb param, so the
    # (H) closure that invokes cb must get a CALLS edge to target.
    calls = _run_calls(tmp_path, {"m.js": JS_FACTORY_ALIAS}, "javascript")
    assert _has(calls, "m.makeRunner.runner", "m.target")


JS_DISPATCH_TABLE = (
    "function _handleA() { return 1; }\n"
    "function _handleB() { return 2; }\n\n"
    "const HANDLERS = { a: _handleA, b: _handleB };\n"
    "const LIST = [_handleA];\n"
)


def test_js_object_dispatch_table_keeps_handlers_reachable(tmp_path: Path) -> None:
    # (H) Handlers stored only in an object/array dispatch table are wired to be
    # (H) invoked later (HANDLERS[key](...)); the enclosing scope must reference them
    # (H) so they are not reported dead, matching the Python dispatch-table behaviour.
    calls = _run_calls(tmp_path, {"m.js": JS_DISPATCH_TABLE}, "javascript")
    assert _has(calls, "m", "m._handleA")
    assert _has(calls, "m", "m._handleB")
