"""`evals.structure_report` writes a scored structure eval to `scores.csv`
and `diff.json` and prints it as a table; the L1 drivers all report through
it. Pinned: the files round-trip what was scored, and the table shows every
row with names left-aligned and figures right-aligned to four places."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger
from rich.console import Console

from evals import constants as ec
from evals import structure_report as report
from evals.types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow


def _row(category: str, label: str, tp: int, fp: int, fn: int) -> ScoreRow:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ScoreRow(
        category=category,
        label=label,
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=f1,
    )


_RESULT = ScoreResult(
    rows=[_row("node", "Function", 3, 1, 0), _row("edge", "ALL", 1, 0, 2)],
    location=LocationStats(3, 3, 3, 0.0, 0),
    diff={
        "node:Function": DiffBucket(missing=[], extra=["Function a.py:9 extra"]),
        "edge:DEFINES": DiffBucket(missing=["DEFINES a.py:1 -> a.py:3"], extra=[]),
    },
)


@pytest.fixture
def messages() -> Iterator[list[str]]:
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="DEBUG")
    yield lines
    logger.remove(sink)


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> Console:
    recording = Console(record=True, width=120, color_system=None)
    monkeypatch.setattr(report, "_console", recording)
    return recording


def test_the_scores_and_diff_round_trip(tmp_path: Path, messages: list[str]) -> None:
    out_dir = tmp_path / "results" / "l1"

    report.write_outputs(_RESULT, out_dir, "scores.csv", "diff.json")

    with (out_dir / "scores.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames or ()) == ec.CSV_FIELDS
        rows = list(reader)
    assert [(r["category"], r["label"], r["tp"], r["fn"]) for r in rows] == [
        ("node", "Function", "3", "0"),
        ("edge", "ALL", "1", "2"),
    ]
    assert float(rows[0]["precision"]) == pytest.approx(0.75)
    diff = json.loads((out_dir / "diff.json").read_text(encoding="utf-8"))
    assert diff == _RESULT.diff
    assert messages == [
        f"Wrote scores to {out_dir / 'scores.csv'}",
        f"Wrote diff to {out_dir / 'diff.json'}",
    ]


def test_an_empty_result_still_writes_both_files(tmp_path: Path) -> None:
    empty = ScoreResult(rows=[], location=LocationStats(0, 0, 0, 0.0, 0), diff={})
    report.write_outputs(empty, tmp_path, "s.csv", "d.json")
    header = (tmp_path / "s.csv").read_text(encoding="utf-8").splitlines()
    assert header == [",".join(ec.CSV_FIELDS)]
    assert json.loads((tmp_path / "d.json").read_text(encoding="utf-8")) == {}


def test_a_rewrite_replaces_the_previous_scores(tmp_path: Path) -> None:
    report.write_outputs(_RESULT, tmp_path, "s.csv", "d.json")
    one_row = _RESULT._replace(rows=_RESULT.rows[:1])
    report.write_outputs(one_row, tmp_path, "s.csv", "d.json")
    assert len((tmp_path / "s.csv").read_text(encoding="utf-8").splitlines()) == 2


def test_the_table_shows_every_row_aligned_by_kind(console: Console) -> None:
    report.render(_RESULT, "cgr L1 structure eval (Python)")

    text = console.export_text()
    assert "cgr L1 structure eval (Python)" in text
    header = next(line for line in text.splitlines() if "category" in line)
    assert re.findall(r"\w+", header) == list(ec.CSV_FIELDS)
    function_row = next(line for line in text.splitlines() if "Function" in line)
    cells = [cell.strip() for cell in function_row.strip("│┃ ").split("│")]
    assert cells == ["node", "Function", "3", "1", "0", "0.7500", "1.0000", "0.8571"]
    # Names hug the left edge of their column, figures the right.
    all_row = next(line for line in text.splitlines() if "ALL" in line)
    assert re.search(r"│ ALL {2,}│", all_row)
    assert re.search(r"│ +1 │", all_row)
