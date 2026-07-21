# A base class that is POSITIVELY external (import-mapped like
# typing.Protocol, ::-qualified like std::fmt::Display, or a JS global
# like Error) carries real information the dead-code and protocol-dispatch
# layers consume, but its INHERITS/IMPLEMENTS edge always pointed at a
# first-party label that has no node, so the database silently dropped it
# (issue #652). Mirroring the ExternalModule convention for imports, the
# edge now targets an ExternalModule node that is minted if the import
# pass did not already create it. A module-anchored GUESS that resolves
# nowhere still emits no edge; knowledge and guesses are not the same.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

PY_PROTOCOL = """
from typing import Protocol


class Loadable(Protocol):
    def _ensure_loaded(self) -> None: ...
"""

PY_ATTRIBUTE_BASE = """
from collections import abc


class Registry(abc.Mapping):
    def __getitem__(self, key):
        return None
"""

JS_ERRORS = """
class ValidationError extends Error {
    constructor(message) {
        super(message);
    }
}

class TimeoutError extends ValidationError {
    constructor() {
        super("timeout");
    }
}
"""


def _inherits_pairs(mock_ingestor: MagicMock) -> set[tuple[str, str, str]]:
    return {
        (call.args[0][2], str(call.args[2][0]), call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.INHERITS.value)
    }


def _node_keys(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0]), c.args[1].get("qualified_name"))
        for c in mock_ingestor.ensure_node_batch.call_args_list
    }


def test_python_protocol_base_emits_external_edge_and_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "m.py").write_text(PY_PROTOCOL)
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    pairs = _inherits_pairs(mock_ingestor)
    expected = (
        f"{project}.m.Loadable",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        "typing.Protocol",
    )
    assert expected in pairs, pairs
    assert (
        cs.NodeLabel.EXTERNAL_MODULE.value,
        "typing.Protocol",
    ) in _node_keys(mock_ingestor)


def test_python_attribute_base_emits_external_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "reg.py").write_text(PY_ATTRIBUTE_BASE)
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    pairs = _inherits_pairs(mock_ingestor)
    expected = (
        f"{project}.reg.Registry",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        "collections.abc.Mapping",
    )
    assert expected in pairs, pairs


def test_js_global_base_emits_external_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "errors.js").write_text(JS_ERRORS)
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    pairs = _inherits_pairs(mock_ingestor)
    external_error_qn = f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}Error"
    assert (
        f"{project}.errors.ValidationError",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        external_error_qn,
    ) in pairs, pairs
    # The first-party chain must keep its first-party label.
    assert (
        f"{project}.errors.TimeoutError",
        cs.NodeLabel.CLASS.value,
        f"{project}.errors.ValidationError",
    ) in pairs, pairs
