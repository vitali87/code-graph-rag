# (H) A method-body anonymous-class method (`make(){ return new Reader(){ read(){
# (H) helper(); } }; }`) was registered by the definition pass as `Class.read` (the
# (H) unified-FQN scope walk dropped the enclosing method `make`), but the call pass
# (H) attributes its outgoing calls to `Class.make.read` -- a phantom qn with no node.
# (H) So every edge FROM such a method dangled and its callees looked dead. The two
# (H) passes must agree on the qn (both `Class.make.read`).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_anon_method_call_edge_joins_a_real_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jfqn"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "public class M {\n"
        "  interface Reader { int read(); }\n"
        "  static int helper(int x) { return x; }\n"
        "  static Reader make() {\n"
        "    return new Reader() {\n"
        "      @Override public int read() { return helper(1); }\n"
        "    };\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    updater = create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    registry = updater.factory.function_registry
    calls = get_relationships(mock_ingestor, "CALLS")
    # (H) the CALLS edge into helper must originate from a qn that is a registered
    # (H) node -- not a phantom `Class.make.read` that no node carries.
    helper_callers = [
        c.args[0][2] for c in calls if c.args[2][2].endswith(".helper(int)")
    ]
    assert helper_callers, "no caller recorded for helper"
    for caller_qn in helper_callers:
        assert caller_qn in registry, (
            f"caller {caller_qn} is a phantom (no node); "
            f"def-pass and call-pass disagree on the anon method qn"
        )
