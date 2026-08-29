from pathlib import Path
from typing import Annotated

import typer

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_java_graph
from .l1_eval import run_l1_eval
from .oracles import java_available, run_java_oracle

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
    run_l1_eval(
        target,
        project_name,
        out_dir,
        available=java_available,
        oracle_missing=ls.JAVA_ORACLE_MISSING,
        extract_cgr=extract_cgr_java_graph,
        run_oracle=run_java_oracle,
        oracle_binary=ec.JAVA_BIN,
        scored_node_kinds=ec.JAVA_SCORED_NODE_KINDS,
        extracting_cgr=ls.JAVA_EXTRACTING_CGR,
        cgr_done=ls.JAVA_CGR_DONE,
        extracting_oracle=ls.JAVA_EXTRACTING_ORACLE,
        oracle_done=ls.JAVA_ORACLE_DONE,
        scores_filename=ec.JAVA_SCORES_FILENAME,
        diff_filename=ec.JAVA_DIFF_FILENAME,
        title=_TITLE,
    )


if __name__ == "__main__":
    typer.run(main)
