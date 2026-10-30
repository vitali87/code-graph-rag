"""`python -m evals.cli` is the L1 structure eval: it indexes a Python target
with cgr, extracts the same structure with the `ast` oracle, scores one
against the other and writes `scores.csv` and `diff.json`. Run here end to
end on a three-file package, where the two sides must agree exactly."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from codebase_rag.config import settings
from evals import cli, structure_report
from evals import constants as ec

_FILES = {
    "__init__.py": "",
    "util.py": (
        "def helper():\n    return 1\n\n\n"
        "class Cart:\n    def total(self):\n        return helper()\n"
    ),
    "app.py": "from .util import Cart, helper\n\n\ndef run():\n    return helper()\n",
}


@pytest.fixture(autouse=True)
def _skip_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    # The structure eval grades nodes and edges; embedding the package would
    # only import torch.
    monkeypatch.setattr(settings, "SKIP_EMBEDDINGS", True)


@pytest.fixture
def target(tmp_path: Path) -> Path:
    package = tmp_path / "shop"
    package.mkdir()
    for name, text in _FILES.items():
        (package / name).write_text(text, encoding="utf-8")
    return package


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> Console:
    # One recording console for both tables: the score table is printed by
    # structure_report, the span table by the command itself.
    recording = Console(record=True, width=160, color_system=None)
    monkeypatch.setattr(cli, "console", recording)
    monkeypatch.setattr(structure_report, "_console", recording)
    return recording


def _scores(out_dir: Path) -> dict[tuple[str, str], dict[str, str]]:
    with (out_dir / ec.SCORES_FILENAME).open(encoding="utf-8", newline="") as f:
        return {(r["category"], r["label"]): r for r in csv.DictReader(f)}


def test_cgr_and_the_oracle_agree_on_a_small_package(
    target: Path, tmp_path: Path, console: Console
) -> None:
    out_dir = tmp_path / "out"

    cli.main(target=target, project_name="", out_dir=out_dir)

    scores = _scores(out_dir)
    assert scores[("node", "Module")]["tp"] == "3"
    assert scores[("node", "Function")]["tp"] == "2"
    assert scores[("node", "Class")]["tp"] == "1"
    assert scores[("node", "Method")]["tp"] == "1"
    assert scores[("edge", "DEFINES")]["tp"] == "3"
    assert scores[("edge", "DEFINES_METHOD")]["tp"] == "1"
    assert scores[("edge", "IMPORTS")]["tp"] == "1"
    disagreements = {
        key: (row["fp"], row["fn"])
        for key, row in scores.items()
        if (row["fp"], row["fn"]) != ("0", "0")
    }
    assert disagreements == {}

    diff = json.loads((out_dir / ec.DIFF_FILENAME).read_text(encoding="utf-8"))
    assert diff
    assert all(bucket == {"missing": [], "extra": []} for bucket in diff.values())

    text = console.export_text()
    assert "cgr L1 structure eval (Python)" in text
    assert "span (end_line) accuracy on matched defs" in text


def test_the_command_parses_its_options_and_writes_where_told(
    target: Path, tmp_path: Path, console: Console
) -> None:
    # What `typer.run(main)` builds when the module runs.
    app = typer.Typer()
    app.command()(cli.main)
    out_dir = tmp_path / "nested" / "out"

    result = CliRunner().invoke(
        app,
        ["--target", str(target), "--project-name", "store", "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0, result.output
    assert _scores(out_dir)[("node", "ALL")]["fn"] == "0"
    diff = (out_dir / ec.DIFF_FILENAME).read_text(encoding="utf-8")
    assert json.loads(diff)
