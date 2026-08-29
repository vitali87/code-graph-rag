from pathlib import Path
from typing import Annotated

import typer

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_js_graph
from .l1_eval import run_l1_eval
from .oracles import run_javascript_oracle, typescript_available

_TITLE = "cgr L1 structure eval (JavaScript vs tsc)"


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of JavaScript sources to evaluate.")
    ] = Path(ec.GO_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for js_scores.csv and js_diff.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    run_l1_eval(
        target,
        project_name,
        out_dir,
        available=typescript_available,
        oracle_missing=ls.TS_ORACLE_MISSING,
        extract_cgr=extract_cgr_js_graph,
        run_oracle=run_javascript_oracle,
        oracle_binary=ec.NODE_BIN,
        scored_node_kinds=ec.JS_SCORED_NODE_KINDS,
        extracting_cgr=ls.JS_EXTRACTING_CGR,
        cgr_done=ls.JS_CGR_DONE,
        extracting_oracle=ls.JS_EXTRACTING_ORACLE,
        oracle_done=ls.JS_ORACLE_DONE,
        scores_filename=ec.JS_SCORES_FILENAME,
        diff_filename=ec.JS_DIFF_FILENAME,
        title=_TITLE,
    )


if __name__ == "__main__":
    typer.run(main)
