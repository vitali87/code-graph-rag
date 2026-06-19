import csv
import json
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from . import constants as ec
from . import logs as ls
from .ast_oracle import extract_oracle_graph
from .cgr_graph import extract_cgr_graph
from .score import score
from .types_defs import ScoreResult

console = Console()


def main(
    target: Annotated[
        Path, typer.Option(help="Directory to evaluate (cgr repo source).")
    ] = Path(ec.DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for scores.csv and diff.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.EXTRACTING_CGR.format(target=target, project=project))
    cgr_graph = extract_cgr_graph(target, project)
    logger.success(
        ls.CGR_GRAPH_DONE.format(nodes=len(cgr_graph.nodes), edges=len(cgr_graph.edges))
    )

    logger.info(ls.EXTRACTING_ORACLE.format(target=target))
    oracle_graph = extract_oracle_graph(target)
    logger.success(
        ls.ORACLE_GRAPH_DONE.format(
            nodes=len(oracle_graph.nodes), edges=len(oracle_graph.edges)
        )
    )

    result = score(cgr_graph, oracle_graph)
    _write_outputs(result, out_dir)
    _render(result)


def _write_outputs(result: ScoreResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_path = out_dir / ec.SCORES_FILENAME
    with scores_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ec.CSV_FIELDS))
        writer.writeheader()
        for row in result.rows:
            writer.writerow(row)
    logger.success(ls.WROTE_SCORES.format(path=scores_path))

    diff_path = out_dir / ec.DIFF_FILENAME
    diff_path.write_text(json.dumps(result.diff, indent=2), encoding="utf-8")
    logger.success(ls.WROTE_DIFF.format(path=diff_path))


def _render(result: ScoreResult) -> None:
    table = Table(title="cgr L1 structure eval (Python)")
    table.add_column("category")
    table.add_column("label")
    table.add_column("tp", justify="right")
    table.add_column("fp", justify="right")
    table.add_column("fn", justify="right")
    table.add_column("precision", justify="right")
    table.add_column("recall", justify="right")
    table.add_column("f1", justify="right")
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
    console.print(table)

    loc = result.location
    location_table = Table(title="span (end_line) accuracy on matched defs")
    location_table.add_column("matched", justify="right")
    location_table.add_column("end_exact", justify="right")
    location_table.add_column("end_within_1", justify="right")
    location_table.add_column("mean_abs_delta", justify="right")
    location_table.add_column("max_abs_delta", justify="right")
    location_table.add_row(
        str(loc.matched),
        str(loc.end_exact),
        str(loc.end_within_one),
        f"{loc.mean_abs_delta:.4f}",
        str(loc.max_abs_delta),
    )
    console.print(location_table)


if __name__ == "__main__":
    typer.run(main)
