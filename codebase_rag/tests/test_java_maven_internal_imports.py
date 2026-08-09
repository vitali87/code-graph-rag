"""Intra-project imports under src/main/java must resolve to Module nodes,
not dead-end ExternalModule nodes (issue #1121)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships, run_updater

SERVICE = """
package com.example.myapp;

import com.example.myapp.util.MyHelper;

public class MyService {
    public int serve() {
        return MyHelper.help();
    }
}
"""

HELPER = """
package com.example.myapp.util;

public class MyHelper {
    public static int help() {
        return 42;
    }
}
"""


def _write_maven_project(temp_repo: Path) -> None:
    base = temp_repo / "src" / "main" / "java" / "com" / "example" / "myapp"
    (base / "util").mkdir(parents=True)
    (base / "MyService.java").write_text(SERVICE, encoding="utf-8")
    (base / "util" / "MyHelper.java").write_text(HELPER, encoding="utf-8")


def _skip_without_java() -> None:
    parsers, _ = load_parsers()
    if "java" not in parsers:
        pytest.skip("java parser not available")


def _import_edges(mock_ingestor: MagicMock, from_qn: str) -> list[tuple[str, str]]:
    return [
        (str(call.args[2][0]), call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.IMPORTS.value)
        if call.args[0][2] == from_qn
    ]


def test_maven_internal_import_targets_the_module_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _skip_without_java()
    _write_maven_project(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    service_qn = f"{project}.src.main.java.com.example.myapp.MyService"
    helper_qn = f"{project}.src.main.java.com.example.myapp.util.MyHelper"
    edges = _import_edges(mock_ingestor, service_qn)
    assert (str(cs.NodeLabel.MODULE), helper_qn) in edges, edges


def test_maven_internal_import_creates_no_external_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _skip_without_java()
    _write_maven_project(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    external_qns = {
        c.args[1].get("qualified_name")
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == str(cs.NodeLabel.EXTERNAL_MODULE)
    }
    internal = {qn for qn in external_qns if qn and "com.example.myapp" in qn}
    assert not internal, internal
