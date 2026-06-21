from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_java_graph
from .oracles import java_available, run_java_oracle
from .score import score_structure
from .structure_report import render, write_outputs

_TITLE = "cgr L1 structure eval (Java vs JDK Compiler Tree API)"


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Java sources to evaluate.")
    ] = Path(ec.GO_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for java_scores.csv and java_diff.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not java_available():
        logger.error(ls.JAVA_ORACLE_MISSING)
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.JAVA_EXTRACTING_CGR.format(target=target, project=project))
    cgr = extract_cgr_java_graph(target, project)
    logger.success(ls.JAVA_CGR_DONE.format(count=len(cgr.nodes)))

    logger.info(ls.JAVA_EXTRACTING_ORACLE.format(binary=ec.JAVA_BIN, target=target))
    oracle = run_java_oracle(target)
    logger.success(ls.JAVA_ORACLE_DONE.format(count=len(oracle.nodes)))

    result = score_structure(
        cgr, oracle, ec.JAVA_SCORED_NODE_KINDS, ec.SCORED_EDGE_TYPES
    )
    write_outputs(result, out_dir, ec.JAVA_SCORES_FILENAME, ec.JAVA_DIFF_FILENAME)
    render(result, _TITLE)


if __name__ == "__main__":
    typer.run(main)
