"""The harness fails a run that swallowed a per-file pass failure (#1070).

One case per pass, since each swallows into its own message at its own level,
and a prefix the gate does not watch is a pass it does not cover.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers import dependency_parser
from codebase_rag.parsers.call_processor import CallProcessor
from codebase_rag.parsers.definition_processor import DefinitionProcessor
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.tests.conftest import assert_no_pass_failures, create_and_run_updater

_SOURCE = (
    "import os\n\n\ndef alpha():\n    return beta()\n\n\ndef beta():\n    return 1\n"
)


def _boom(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("pass exploded")


def _run_with_broken_pass(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    attribute: str,
) -> None:
    (temp_repo / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\ndependencies = ["requests"]\n',
        encoding="utf-8",
    )
    (temp_repo / "m.py").write_text(_SOURCE, encoding="utf-8")
    monkeypatch.setattr(target, attribute, _boom)
    create_and_run_updater(temp_repo, mock_ingestor)


@pytest.mark.parametrize(
    ("target", "attribute", "expected"),
    [
        (CallProcessor, "_ingest_function_calls", "Failed to process calls in "),
        (DefinitionProcessor, "_ingest_all_functions", "Failed to parse or ingest "),
        (ImportProcessor, "_parse_python_imports", "Failed to parse imports in "),
        (dependency_parser.toml, "load", "Error parsing "),
    ],
    ids=["calls", "definitions", "imports", "dependencies"],
)
def test_a_raising_pass_is_caught_and_would_fail_the_run(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    _fail_on_swallowed_pass_errors: list[str],
    target: type,
    attribute: str,
    expected: str,
) -> None:
    # Each pass swallows into its OWN message, at its own level, so one entry
    # per pass is what the gate needs and each is pinned separately here. The
    # patched target sits INSIDE the pass's own try, since replacing the method
    # that holds the handler would let the caller's handler catch it instead.
    _run_with_broken_pass(temp_repo, mock_ingestor, monkeypatch, target, attribute)
    failures = _fail_on_swallowed_pass_errors
    assert any(f.startswith(expected) for f in failures), failures
    # What the autouse gate does with them at teardown, asserted here because a
    # teardown failure is not observable from inside the test.
    with pytest.raises(AssertionError, match="a per-file pass failed"):
        assert_no_pass_failures(failures)
    # Owned by this test: the run really did fail, so clear it before teardown.
    failures.clear()


def test_a_clean_run_leaves_the_gate_quiet(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    _fail_on_swallowed_pass_errors: list[str],
) -> None:
    # The gate has to stay silent on a working index, or it is unusable: this
    # is the same fixture, unbroken, producing the call edge it should.
    (temp_repo / "m.py").write_text(_SOURCE, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)
    assert not _fail_on_swallowed_pass_errors, _fail_on_swallowed_pass_errors
    calls = {
        (c.args[0][2], c.args[2][2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "CALLS"
    }
    base = temp_repo.name
    assert (f"{base}.m.alpha", f"{base}.m.beta") in calls, calls
