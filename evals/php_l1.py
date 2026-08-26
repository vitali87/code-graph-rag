from pathlib import Path
from typing import Annotated

import typer

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_php_graph
from .l1_eval import run_l1_eval
from .oracles import php_oracle_available, run_php_oracle

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
    run_l1_eval(
        target,
        project_name,
        out_dir,
        available=php_oracle_available,
        oracle_missing=ls.PHP_ORACLE_MISSING,
        extract_cgr=extract_cgr_php_graph,
        run_oracle=run_php_oracle,
        oracle_binary=ec.NODE_BIN,
        scored_node_kinds=ec.PHP_SCORED_NODE_KINDS,
        extracting_cgr=ls.PHP_EXTRACTING_CGR,
        cgr_done=ls.PHP_CGR_DONE,
        extracting_oracle=ls.PHP_EXTRACTING_ORACLE,
        oracle_done=ls.PHP_ORACLE_DONE,
        scores_filename=ec.PHP_SCORES_FILENAME,
        diff_filename=ec.PHP_DIFF_FILENAME,
        title=_TITLE,
    )


if __name__ == "__main__":
    typer.run(main)
