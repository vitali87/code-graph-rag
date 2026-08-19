"""FLOWS_TO lean-walk coverage for Scala (issues #1256/#1176). The walk
routed Scala but the descriptor's subscript_type was wired to call_expression,
so _js_expr_taint's member/subscript branch swallowed every nested source
call and Scala emitted zero FLOWS_TO edges; the #1190 corpus caught it.
These tests pin the fixed wiring for the direct-nesting and val-bound
shapes."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])
_ENV_TOKEN = "resource::ENV::TOKEN"
_STDOUT = "resource::STDOUT::<dynamic>"


def _run_flow(tmp_path: Path, source: str) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    if "scala" not in parsers:
        pytest.skip("scala parser not available")
    (tmp_path / "app.scala").write_text(source, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }


def test_scala_env_source_nested_in_print_sink(tmp_path: Path) -> None:
    source = (
        "object App {\n"
        "  def leak(): Unit = {\n"
        '    println(System.getenv("TOKEN"))\n'
        "  }\n"
        "}\n"
    )
    assert (_ENV_TOKEN, _STDOUT) in _run_flow(tmp_path, source)


def test_scala_val_bound_env_flows_to_print(tmp_path: Path) -> None:
    source = (
        "object App {\n"
        "  def leak(): Unit = {\n"
        '    val token = System.getenv("TOKEN")\n'
        "    println(token)\n"
        "  }\n"
        "}\n"
    )
    assert (_ENV_TOKEN, _STDOUT) in _run_flow(tmp_path, source)


def test_scala_constant_never_flows(tmp_path: Path) -> None:
    source = (
        "object App {\n"
        "  def safe(): Unit = {\n"
        '    val fixed = "constant"\n'
        "    println(fixed)\n"
        "  }\n"
        "}\n"
    )
    assert _run_flow(tmp_path, source) == set()


def test_scala_node_constants_register_coverage() -> None:
    # Constants modules import before coverage starts collecting, so their
    # lines never record without a reload (Sonar new-coverage gate).
    from codebase_rag.constants import ast_scala

    importlib.reload(ast_scala)
    assert ast_scala.TS_SCALA_TYPE_IDENTIFIER == "type_identifier"
    assert ast_scala.TS_SCALA_STRING == "string"
    assert ast_scala.TS_SCALA_INSTANCE_EXPRESSION == "instance_expression"
