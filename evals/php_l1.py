from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_php_nodes
from .oracles import php_oracle_available, run_php_oracle
from .score import score_node_kinds
from .structure_report import render, write_outputs
from .types_defs import GraphData

_TITLE = "cgr L1 structure eval (PHP vs php-parser)"


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of PHP sources to evaluate.")
    ] = Path(ec.GO_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for php_scores.csv and php_diff.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not php_oracle_available():
        logger.error(ls.PHP_ORACLE_MISSING)
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.PHP_EXTRACTING_CGR.format(target=target, project=project))
    cgr = GraphData(
        nodes=extract_cgr_php_nodes(target, project), edges=set(), name_edges=set()
    )
    logger.success(ls.PHP_CGR_DONE.format(count=len(cgr.nodes)))

    logger.info(ls.PHP_EXTRACTING_ORACLE.format(binary=ec.NODE_BIN, target=target))
    oracle = GraphData(nodes=run_php_oracle(target), edges=set(), name_edges=set())
    logger.success(ls.PHP_ORACLE_DONE.format(count=len(oracle.nodes)))

    result = score_node_kinds(cgr, oracle, ec.PHP_SCORED_NODE_KINDS)
    write_outputs(result, out_dir, ec.PHP_SCORES_FILENAME, ec.PHP_DIFF_FILENAME)
    render(result, _TITLE)


if __name__ == "__main__":
    typer.run(main)
