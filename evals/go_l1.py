from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_go_graph
from .oracles import go_available, run_go_oracle
from .score import score_structure
from .structure_report import render, write_outputs

_TITLE = "cgr L1 structure eval (Go vs go/ast)"


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
    cgr = extract_cgr_go_graph(target, project)
    logger.success(ls.GO_CGR_DONE.format(count=len(cgr.nodes)))

    logger.info(ls.GO_EXTRACTING_ORACLE.format(binary=ec.GO_BIN, target=target))
    oracle = run_go_oracle(target)
    logger.success(ls.GO_ORACLE_DONE.format(count=len(oracle.nodes)))

    result = score_structure(
        cgr, oracle, ec.GO_SCORED_NODE_KINDS, ec.SCORED_EDGE_TYPES, grade_spans=True
    )
    write_outputs(result, out_dir, ec.GO_SCORES_FILENAME, ec.GO_DIFF_FILENAME)
    render(result, _TITLE)


if __name__ == "__main__":
    typer.run(main)
