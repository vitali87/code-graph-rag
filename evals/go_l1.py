from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_go_nodes
from .oracles import go_available, run_go_oracle
from .score import score_node_kinds
from .structure_report import render, write_outputs
from .types_defs import GraphData

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
    cgr = GraphData(
        nodes=extract_cgr_go_nodes(target, project), edges=set(), name_edges=set()
    )
    logger.success(ls.GO_CGR_DONE.format(count=len(cgr.nodes)))

    logger.info(ls.GO_EXTRACTING_ORACLE.format(binary=ec.GO_BIN, target=target))
    oracle = GraphData(nodes=run_go_oracle(target), edges=set(), name_edges=set())
    logger.success(ls.GO_ORACLE_DONE.format(count=len(oracle.nodes)))

    result = score_node_kinds(cgr, oracle, ec.GO_SCORED_NODE_KINDS)
    write_outputs(result, out_dir, ec.GO_SCORES_FILENAME, ec.GO_DIFF_FILENAME)
    render(result, _TITLE)


if __name__ == "__main__":
    typer.run(main)
