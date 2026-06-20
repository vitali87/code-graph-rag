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
from .cgr_graph import extract_cgr_go_nodes
from .oracles import go_available, run_go_oracle
from .score import score_node_kinds
from .types_defs import GraphData, ScoreResult

console = Console()


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Go sources to evaluate.")
    ] = Path(ec.GO_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for go_scores.csv and go_diff.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not go_available():
        logger.error(ls.GO_ORACLE_MISSING.format(binary=ec.GO_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.GO_EXTRACTING_CGR.format(target=target, project=project))
    cgr = GraphData(
        nodes=extract_cgr_go_nodes(target, project), edges=set(), name_edges=set()
    )
    logger.success(ls.GO_CGR_DONE.format(count=len(cgr.nodes)))

    logger.info(ls.GO_EXTRACTING_ORACLE.format(binary=ec.GO_BIN, target=target))
    oracle = GraphData(nodes=run_go_oracle(target), edges=set(), name_edges=set())
    logger.success(ls.GO_ORACLE_DONE.format(count=len(oracle.nodes)))

    result = score_node_kinds(cgr, oracle, ec.GO_SCORED_NODE_KINDS)
    _write_outputs(result, out_dir)
    _render(result)


def _write_outputs(result: ScoreResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_path = out_dir / ec.GO_SCORES_FILENAME
    with scores_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ec.CSV_FIELDS))
        writer.writeheader()
        for row in result.rows:
            writer.writerow(row)
    logger.success(ls.WROTE_SCORES.format(path=scores_path))

    diff_path = out_dir / ec.GO_DIFF_FILENAME
    diff_path.write_text(json.dumps(result.diff, indent=2), encoding="utf-8")
    logger.success(ls.WROTE_DIFF.format(path=diff_path))


def _render(result: ScoreResult) -> None:
    table = Table(title="cgr L1 structure eval (Go vs go/ast)")
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
    console.print(table)


if __name__ == "__main__":
    typer.run(main)
