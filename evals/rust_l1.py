from pathlib import Path
from typing import Annotated

import typer

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_rust_graph
from .l1_eval import run_l1_eval
from .oracles import run_rust_oracle, rust_available

_TITLE = "cgr L1 structure eval (Rust vs syn)"


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Rust sources to evaluate.")
    ] = Path(ec.GO_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for rs_scores.csv and rs_diff.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    run_l1_eval(
        target,
        project_name,
        out_dir,
        available=rust_available,
        oracle_missing=ls.RS_ORACLE_MISSING.format(binary=ec.CARGO_BIN),
        extract_cgr=extract_cgr_rust_graph,
        run_oracle=run_rust_oracle,
        oracle_binary=ec.CARGO_BIN,
        scored_node_kinds=ec.RS_SCORED_NODE_KINDS,
        extracting_cgr=ls.RS_EXTRACTING_CGR,
        cgr_done=ls.RS_CGR_DONE,
        extracting_oracle=ls.RS_EXTRACTING_ORACLE,
        oracle_done=ls.RS_ORACLE_DONE,
        scores_filename=ec.RS_SCORES_FILENAME,
        diff_filename=ec.RS_DIFF_FILENAME,
        title=_TITLE,
    )


if __name__ == "__main__":
    typer.run(main)
