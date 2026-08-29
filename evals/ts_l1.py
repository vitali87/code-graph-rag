from pathlib import Path
from typing import Annotated

import typer

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_ts_graph
from .l1_eval import run_l1_eval
from .oracles import run_typescript_oracle, typescript_available

_TITLE = "cgr L1 structure eval (TypeScript vs tsc)"


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of TypeScript sources to evaluate.")
    ] = Path(ec.GO_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for ts_scores.csv and ts_diff.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    run_l1_eval(
        target,
        project_name,
        out_dir,
        available=typescript_available,
        oracle_missing=ls.TS_ORACLE_MISSING,
        extract_cgr=extract_cgr_ts_graph,
        run_oracle=run_typescript_oracle,
        oracle_binary=ec.NODE_BIN,
        scored_node_kinds=ec.TS_SCORED_NODE_KINDS,
        extracting_cgr=ls.TS_EXTRACTING_CGR,
        cgr_done=ls.TS_CGR_DONE,
        extracting_oracle=ls.TS_EXTRACTING_ORACLE,
        oracle_done=ls.TS_ORACLE_DONE,
        scores_filename=ec.TS_SCORES_FILENAME,
        diff_filename=ec.TS_DIFF_FILENAME,
        title=_TITLE,
    )


if __name__ == "__main__":
    typer.run(main)
