import csv
import json
from pathlib import Path

from loguru import logger
from rich.console import Console
from rich.table import Table

from . import constants as ec
from . import logs as ls
from .types_defs import ScoreResult

_console = Console()


def write_outputs(
    result: ScoreResult, out_dir: Path, scores_filename: str, diff_filename: str
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_path = out_dir / scores_filename
    with scores_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ec.CSV_FIELDS))
        writer.writeheader()
        for row in result.rows:
            writer.writerow(row)
    logger.success(ls.WROTE_SCORES.format(path=scores_path))

    diff_path = out_dir / diff_filename
    diff_path.write_text(json.dumps(result.diff, indent=2), encoding="utf-8")
    logger.success(ls.WROTE_DIFF.format(path=diff_path))


def render(result: ScoreResult, title: str) -> None:
    table = Table(title=title)
    for column in ec.CSV_FIELDS:
        justify = "left" if column in ec.LEFT_COLUMNS else "right"
        table.add_column(column, justify=justify)
    for row in result.rows:
        table.add_row(
            row["category"],
            row["label"],
            str(row["tp"]),
            str(row["fp"]),
            str(row["fn"]),
            f"{row['precision']:.4f}",
            f"{row['recall']:.4f}",
            f"{row['f1']:.4f}",
        )
    _console.print(table)
