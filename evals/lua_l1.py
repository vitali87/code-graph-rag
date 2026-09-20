from pathlib import Path
from typing import Annotated

import typer

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_lua_graph
from .l1_eval import run_l1_eval
from .oracles import lua_oracle_available, lua_oracle_skip_reason, run_lua_oracle

_TITLE = "cgr L1 structure eval (Lua vs luaparse)"


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Lua sources to evaluate.")
    ] = Path(ec.GO_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for lua_scores.csv and lua_diff.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    run_l1_eval(
        target,
        project_name,
        out_dir,
        available=lua_oracle_available,
        skip_reason=lua_oracle_skip_reason,
        oracle_missing=ls.LUA_ORACLE_MISSING,
        extract_cgr=extract_cgr_lua_graph,
        run_oracle=run_lua_oracle,
        oracle_binary=ec.NODE_BIN,
        scored_node_kinds=ec.LUA_SCORED_NODE_KINDS,
        extracting_cgr=ls.LUA_EXTRACTING_CGR,
        cgr_done=ls.LUA_CGR_DONE,
        extracting_oracle=ls.LUA_EXTRACTING_ORACLE,
        oracle_done=ls.LUA_ORACLE_DONE,
        scores_filename=ec.LUA_SCORES_FILENAME,
        diff_filename=ec.LUA_DIFF_FILENAME,
        title=_TITLE,
    )


if __name__ == "__main__":
    typer.run(main)
