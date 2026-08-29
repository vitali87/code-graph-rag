# Multi-language retrieval (Lua). File-level call-localization: for each
# first-party Lua function, which files call it. cgr's Lua CALLS edges
# ((caller_file, callee_simple_name)) are graded against call sites extracted
# by luaparse, over the same first-party name universe. luaparse is independent
# of cgr's tree-sitter Lua frontend, so this measures cgr's cross-file Lua call
# resolution against ground truth (mirrors evals/php_retrieval.py /
# java_retrieval.py / ts_retrieval.py).
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .oracles import lua_oracle_available, run_lua_call_oracle
from .retrieval_eval import (
    CallEdge,
    cgr_call_edges,
    score_retrieval,
)
from .structure_report import render, write_outputs
from .types_defs import ScoreResult

console_target = Path(ec.LUA_DEFAULT_TARGET)


def oracle_lua_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_lua_call_oracle(target)


def cgr_lua_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(
        target,
        project,
        declared,
        suffixes=ec.LUA_SUFFIX,
    )


def score_lua_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(
        cgr,
        oracle,
        label=ec.LUA_RETRIEVAL_LABEL,
        diff_prefix=ec.LUA_RETRIEVAL_DIFF_PREFIX,
        edge_repr=ec.LUA_CALL_EDGE_REPR,
    )


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Lua sources to evaluate call retrieval.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for lua_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not lua_oracle_available():
        logger.error(ls.LUA_ORACLE_MISSING.format(binary=ec.NODE_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.LUA_RETRIEVAL_ORACLE.format(binary=ec.NODE_BIN, target=target))
    oracle, declared = oracle_lua_call_edges(target)
    logger.success(ls.LUA_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.LUA_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_lua_call_edges(target, project, declared)
    logger.success(ls.LUA_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_lua_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.LUA_RETRIEVAL_SCORES_FILENAME,
        ec.LUA_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.LUA_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
