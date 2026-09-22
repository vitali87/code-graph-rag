"""Review findings on the MCP extract/inline handlers (PR #2061).

The handlers must edit only beneath the root the selected project was indexed
from, as rename, move and change_signature already do (issue #1542), and the
two write tools must be classified like MOVE for the partial-graph guard.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs

PROJECT = "alpha__aaaa1111"
SERVER_ROOT = "/server/repo"
# Distinct from SERVER_ROOT so a handler that ignores the verified root and
# passes `self.project_root` is told apart from one that passes the result.
VERIFIED_ROOT = Path("/verified/repo")


def _handler() -> object:
    from codebase_rag.mcp.tools import MCPToolsRegistry

    handler = MCPToolsRegistry.__new__(MCPToolsRegistry)
    handler.ingestor = MagicMock()
    handler.project_root = SERVER_ROOT
    handler._reingest_for_contract = MagicMock(return_value=None)
    return handler


def _report() -> MagicMock:
    report = MagicMock()
    report._asdict = MagicMock(return_value={"applied": True, "verdict": None})
    return report


def _run_extract(handler: object) -> object:
    return handler._run_extract(PROJECT, "p.m.f", 3, 5, "helper", False)


def _run_inline(handler: object) -> object:
    return handler._run_inline(PROJECT, "p.m.f", False)


_CASES = [
    pytest.param("codebase_rag.editing.extract.extract", _run_extract, id="extract"),
    pytest.param("codebase_rag.editing.extract.inline", _run_inline, id="inline"),
]


@pytest.mark.parametrize(("target", "run"), _CASES)
def test_a_project_indexed_from_another_checkout_is_refused(
    target: str, run: object
) -> None:
    # A project indexed elsewhere carries relative paths that may also exist
    # under this server's root; editing them here would change an unrelated
    # file, so the handler refuses with the wrong-root error and never calls
    # the operation (Greptile and Copilot, PR #2061).
    handler = _handler()
    operation = MagicMock(return_value=_report())
    with (
        patch("codebase_rag.graph_query.source_root_for", return_value=None),
        patch(target, operation),
    ):
        payload = run(handler)

    assert payload == {cs.DICT_KEY_ERROR: cs.RENAME_WRONG_ROOT.format(project=PROJECT)}
    operation.assert_not_called()


@pytest.mark.parametrize(("target", "run"), _CASES)
def test_the_verified_root_is_what_gets_edited(target: str, run: object) -> None:
    # The root handed to the operation is the one `source_root_for` verified
    # for this project, asked about this server's root (Copilot, PR #2061).
    handler = _handler()
    operation = MagicMock(return_value=_report())
    resolve = MagicMock(return_value=VERIFIED_ROOT)
    with (
        patch("codebase_rag.graph_query.source_root_for", resolve),
        patch(target, operation),
    ):
        payload = run(handler)

    assert payload == {"applied": True, "verdict": None}
    operation.assert_called_once()
    assert operation.call_args.args[0] == VERIFIED_ROOT
    assert operation.call_args.args[2] == PROJECT
    resolve.assert_called_once_with(
        handler.ingestor.fetch_all, PROJECT, Path(SERVER_ROOT)
    )


@pytest.mark.parametrize(
    "tool", [cs.MCPToolName.EXTRACT, cs.MCPToolName.INLINE], ids=lambda t: t.name
)
def test_extract_and_inline_are_edits_not_graph_readers(tool: object) -> None:
    # Graph-driven edits like MOVE: they re-ingest behind the incomplete-run
    # marker themselves, so they are not partial-graph readers (Copilot,
    # PR #2061).
    from codebase_rag.mcp import tools as mcp_tools

    assert tool in mcp_tools._NOT_GRAPH_READERS
    assert tool not in mcp_tools._GRAPH_READING_TOOLS
