from pathlib import Path
from typing import Annotated

import typer

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_csharp_graph
from .l1_eval import run_l1_eval
from .oracles import csharp_oracle_available, run_csharp_oracle

_TITLE = "cgr L1 structure eval (C# vs Roslyn syntax API)"


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of C# sources to evaluate.")
    ] = Path(ec.CSHARP_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for csharp_scores.csv and csharp_diff.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    run_l1_eval(
        target,
        project_name,
        out_dir,
        available=csharp_oracle_available,
        oracle_missing=ls.CSHARP_ORACLE_MISSING,
        extract_cgr=extract_cgr_csharp_graph,
        run_oracle=run_csharp_oracle,
        oracle_binary=ec.DOTNET_BIN,
        scored_node_kinds=ec.CSHARP_SCORED_NODE_KINDS,
        extracting_cgr=ls.CSHARP_EXTRACTING_CGR,
        cgr_done=ls.CSHARP_CGR_DONE,
        extracting_oracle=ls.CSHARP_EXTRACTING_ORACLE,
        oracle_done=ls.CSHARP_ORACLE_DONE,
        scores_filename=ec.CSHARP_SCORES_FILENAME,
        diff_filename=ec.CSHARP_DIFF_FILENAME,
        title=_TITLE,
    )


if __name__ == "__main__":
    typer.run(main)
