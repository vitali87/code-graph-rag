"""Issue #2758: `get_function_source` is advertised only with `semantic_search`.

Its one parameter is an internal graph node id that only `semantic_search`
returns; every other tool answers with qualified names and paths. Without the
semantic extras `semantic_search` is not registered, yet `get_function_source`
still was, so an agent saw a tool it could never give a valid argument.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.mcp.tools import MCPToolsRegistry


def _advertised(tmp_path: Path, semantic: bool) -> set[str]:
    with (
        patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})),
        patch(
            "codebase_rag.mcp.tools.has_semantic_dependencies", return_value=semantic
        ),
        patch(
            "codebase_rag.tools.semantic_search.create_semantic_search_tool",
            return_value=MagicMock(),
        ),
    ):
        registry = MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=MagicMock(), cypher_gen=MagicMock()
        )
    return {schema.name for schema in registry.get_tool_schemas()}


def test_function_source_is_not_advertised_without_semantic_search(
    tmp_path: Path,
) -> None:
    advertised = _advertised(tmp_path, semantic=False)

    assert cs.MCPToolName.GET_FUNCTION_SOURCE not in advertised


@pytest.mark.parametrize("semantic", [True, False])
def test_function_source_and_semantic_search_come_together(
    tmp_path: Path, semantic: bool
) -> None:
    advertised = _advertised(tmp_path, semantic=semantic)

    assert (cs.MCPToolName.GET_FUNCTION_SOURCE in advertised) == (
        cs.MCPToolName.SEMANTIC_SEARCH in advertised
    )


# Negative: what must not change.


def test_both_are_advertised_with_the_semantic_extras(tmp_path: Path) -> None:
    advertised = _advertised(tmp_path, semantic=True)

    assert {cs.MCPToolName.SEMANTIC_SEARCH, cs.MCPToolName.GET_FUNCTION_SOURCE} <= (
        advertised
    )


def test_source_by_qualified_name_is_still_advertised_without_them(
    tmp_path: Path,
) -> None:
    advertised = _advertised(tmp_path, semantic=False)

    assert cs.MCPToolName.SEMANTIC_SEARCH not in advertised
    assert cs.MCPToolName.GET_CODE_SNIPPET in advertised
