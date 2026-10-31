"""Issue #2656: only a definite arity verdict trips `cgr check --fail-on-found`.

`has_findings` counted every signature-change site that was not `ok` or
`unknown`, so a `possibly_missing` site failed the gate. That is the verdict
for `def send(msg)` becoming `def send(msg, channel=None)`: the graph records
no defaults, so fewer arguments than parameters is a hint, not a finding, as
docs/architecture/structural-delta.md says. The gate blocked the most common
backward-compatible API change.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner, Result

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import StructuralDelta, has_findings
from evals.cgr_graph import _StatefulIngestor

PROJECT = "reqp"
LIB = "def send(msg):\n    return msg\n"
APP = 'from lib import send\n\n\ndef run():\n    return send("hi")\n'


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def indexed(temp_repo: Path) -> tuple[Path, _StatefulIngestor]:
    root = temp_repo / PROJECT
    root.mkdir()
    (root / "lib.py").write_text(LIB)
    (root / "app.py").write_text(APP)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return root, store


def _check(root: Path, store: _StatefulIngestor) -> Result:
    cli_store = MagicMock(wraps=store)
    cli_store.list_projects = MagicMock(return_value=[PROJECT])
    context = MagicMock()
    context.__enter__.return_value = cli_store
    context.__exit__.return_value = False
    with patch("codebase_rag.cli.connect_memgraph", return_value=context):
        return CliRunner().invoke(
            app,
            [
                "check",
                "--repo-path",
                str(root),
                "--project",
                PROJECT,
                "--fail-on-found",
            ],
        )


def _verdicts(result: Result) -> list[str]:
    delta = json.loads(result.stdout)
    return [
        site["verdict"]
        for change in delta["signature_changes"]
        for site in change["sites"]
    ]


def test_an_added_optional_parameter_passes_the_gate(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    (root / "lib.py").write_text(LIB.replace("(msg)", "(msg, channel=None)"))

    result = _check(root, store)

    assert result.exit_code == 0, result.output
    # Still reported: the hint stays in the JSON for a reviewer.
    assert _verdicts(result) == [cs.DELTA_ARITY_POSSIBLY_MISSING]


def _delta_with_sites(*verdicts: str) -> StructuralDelta:
    return cast(
        StructuralDelta,
        {
            "dangling_callers": [],
            "dangling_importers": [],
            "signature_changes": [
                {"sites": [{"verdict": v} for v in verdicts], "remote_callers": []}
            ],
            "arity_findings": [],
            "new_duplicates": [],
            "new_import_cycles": [],
            "parse_errors": [],
        },
    )


def test_a_possibly_missing_site_is_no_finding() -> None:
    assert not has_findings(_delta_with_sites(cs.DELTA_ARITY_POSSIBLY_MISSING))


# Negative: what must not change.


def test_a_removed_parameter_a_caller_passes_still_fails_the_gate(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    (root / "lib.py").write_text("def send():\n    return 1\n")

    result = _check(root, store)

    assert result.exit_code == 1, result.output
    assert _verdicts(result) == [cs.DELTA_ARITY_TOO_MANY]


@pytest.mark.parametrize(
    "key",
    [
        "dangling_callers",
        "dangling_importers",
        "arity_findings",
        "new_duplicates",
        "new_import_cycles",
        "parse_errors",
    ],
)
def test_every_other_finding_still_trips_the_gate(key: str) -> None:
    delta = dict(_delta_with_sites(cs.DELTA_ARITY_POSSIBLY_MISSING))
    delta[key] = [{"qualified_name": "reqp.lib.send"}]

    assert has_findings(cast(StructuralDelta, delta))


def test_a_remote_caller_of_a_changed_handler_still_trips_the_gate() -> None:
    delta = _delta_with_sites(cs.DELTA_ARITY_POSSIBLY_MISSING)
    delta["signature_changes"][0]["remote_callers"] = [{"caller": "other.svc.call"}]

    assert has_findings(delta)


def test_an_untouched_tree_passes_the_gate(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed

    assert _check(root, store).exit_code == 0


@pytest.mark.parametrize(
    ("verdicts", "found"),
    [
        ((cs.DELTA_ARITY_TOO_MANY,), True),
        ((cs.DELTA_ARITY_POSSIBLY_MISSING, cs.DELTA_ARITY_TOO_MANY), True),
        ((cs.DELTA_ARITY_OK,), False),
        ((cs.DELTA_ARITY_UNKNOWN,), False),
    ],
)
def test_the_other_verdicts_keep_their_meaning(
    verdicts: tuple[str, ...], found: bool
) -> None:
    assert has_findings(_delta_with_sites(*verdicts)) is found
