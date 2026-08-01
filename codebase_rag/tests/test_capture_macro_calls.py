# Hybrid C++ macro call edges must go through the capture filter, not the
# raw ingestor, so `--capture none` drops them like every other CALLS edge
# (PR #638 review).

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PendingMacroCall

_CALL = PendingMacroCall(
    rel_path="a.c", line=5, callee_qn="proj.a.MACRO", fallback_module_qn="proj.a"
)


def _updater(tmp_path: Path, mock: MagicMock, tokens: list[str]) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(tokens),
    )
    updater._pending_cpp_macro_calls = [_CALL]
    updater._reparsed_file_keys = {_CALL.rel_path}
    return updater


def _emitted_calls(mock: MagicMock) -> int:
    return sum(
        1
        for c in mock.ensure_relationship_batch.call_args_list
        if c.args[1] == cs.RelationshipType.CALLS
    )


def test_macro_call_emitted_by_default(tmp_path: Path) -> None:
    mock = MagicMock()
    _updater(tmp_path, mock, [])._resolve_hybrid_macro_calls()
    assert _emitted_calls(mock) == 1


def test_macro_call_dropped_when_calls_disabled(tmp_path: Path) -> None:
    mock = MagicMock()
    _updater(tmp_path, mock, ["none"])._resolve_hybrid_macro_calls()
    assert _emitted_calls(mock) == 0
