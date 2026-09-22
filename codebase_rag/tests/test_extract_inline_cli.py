"""`cgr extract` and `cgr inline` through the CLI (Copilot, PR #2063).

The operations have their own tests; these pin the command wiring: the
graph context reaches the operation, the report is printed as JSON with its
verdict, a dry run writes nothing, and a refused or rolled-back edit exits
non-zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.extract_inline_helpers import (
    PROJECT,
    REPORT_PY,
    _extract_inline_repo,  # noqa: F401 - pytest fixture
    _project_qn,
)
from evals.cgr_graph import _StatefulIngestor

Repo = tuple[Path, _StatefulIngestor, GraphUpdater]


def _invoke(repo: Repo, args: list[str]):
    root, store, updater = repo
    context = (PROJECT, store.fetch_all, MagicMock(), updater)
    with patch("codebase_rag.cli._edit_context", return_value=context):
        return CliRunner().invoke(app, [*args, "--repo-path", str(root)])


def test_extract_prints_the_applied_report_with_its_verdict(
    extract_inline_repo: Repo,
) -> None:
    root = extract_inline_repo[0]
    result = _invoke(
        extract_inline_repo,
        ["extract", _project_qn("pkg.report.build"), "3", "11", "accumulate"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert payload["verdict"]["ok"] is True
    assert payload["outputs"] == ["total", "average"]
    assert "def accumulate(items, factor):" in (root / "pkg/report.py").read_text()


def test_extract_dry_run_writes_nothing_and_exits_zero(
    extract_inline_repo: Repo,
) -> None:
    root = extract_inline_repo[0]
    result = _invoke(
        extract_inline_repo,
        [
            "extract",
            _project_qn("pkg.report.build"),
            "3",
            "11",
            "accumulate",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["applied"] is False
    assert (root / "pkg/report.py").read_text() == REPORT_PY


def test_a_refused_extract_exits_nonzero_with_the_reason(
    extract_inline_repo: Repo,
) -> None:
    root = extract_inline_repo[0]
    result = _invoke(
        extract_inline_repo,
        ["extract", _project_qn("pkg.report.build"), "3", "6", "part"],
    )

    assert result.exit_code == 1
    assert "cuts through the statement" in result.stderr
    assert (root / "pkg/report.py").read_text() == REPORT_PY


def test_a_rolled_back_edit_exits_nonzero_and_still_prints_the_report(
    extract_inline_repo: Repo,
) -> None:
    """A report with `applied` False on a real run is a failed edit: the JSON
    still reaches stdout for the caller, and the exit code says it failed."""
    from codebase_rag.editing.extract import extract

    root, store, _updater = extract_inline_repo
    planned = extract(
        root,
        store.fetch_all,
        PROJECT,
        _project_qn("pkg.report.build"),
        (3, 11),
        "accumulate",
        dry_run=True,
    )
    rolled_back = planned._replace(message="rolled back")
    with patch("codebase_rag.editing.extract.extract", return_value=rolled_back):
        result = _invoke(
            extract_inline_repo,
            ["extract", _project_qn("pkg.report.build"), "3", "11", "accumulate"],
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["message"] == "rolled back"


def test_inline_prints_the_applied_report(extract_inline_repo: Repo) -> None:
    root = extract_inline_repo[0]
    result = _invoke(extract_inline_repo, ["inline", _project_qn("pkg.util.wrapper")])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["applied"] is True
    assert "wrapper(" not in (root / "pkg/app.py").read_text()


def test_inline_dry_run_writes_nothing(extract_inline_repo: Repo) -> None:
    root = extract_inline_repo[0]
    before = (root / "pkg/app.py").read_text()
    result = _invoke(
        extract_inline_repo, ["inline", _project_qn("pkg.util.wrapper"), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["applied"] is False
    assert (root / "pkg/app.py").read_text() == before
