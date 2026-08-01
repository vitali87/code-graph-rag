# Covers the L1 ast oracle (evals/ast_oracle.py): functions defined inside an
# except handler or a match/case block must be captured. cgr captures these
# function-local defs, so an oracle that skips them produces spurious Function
# false positives (e.g. thrift's sslcompat.py `def match` inside `except`).
from __future__ import annotations

from pathlib import Path

from evals.ast_oracle import extract_oracle_graph

SRC = """\
def with_except():
    try:
        import something
    except ImportError:
        def fallback_in_except():
            return 1
        return fallback_in_except


def with_match(value):
    match value:
        case 1:
            def handler_in_case():
                return 2
            return handler_in_case
        case _:
            return None
"""


def _function_names(target: Path) -> set[str]:
    graph = extract_oracle_graph(target, "proj")
    return {node.name for node in graph.nodes.values() if node.key.kind == "Function"}


def test_oracle_captures_function_in_except_handler(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(SRC, encoding="utf-8")
    names = _function_names(tmp_path)
    assert "fallback_in_except" in names, names


def test_oracle_captures_function_in_match_case(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(SRC, encoding="utf-8")
    names = _function_names(tmp_path)
    assert "handler_in_case" in names, names
