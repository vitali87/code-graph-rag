# Multi-language retrieval (TypeScript). Extends the file-level
# call-localization benchmark to TypeScript: for each first-party TS symbol,
# which files call it. cgr's TS CALLS edges (reduced to (caller_file,
# callee_simple_name)) are graded against call sites from the TypeScript
# compiler API (tsc), over the same first-party name universe. tsc is
# independent of cgr's tree-sitter TS frontend, so this measures cgr's
# cross-file TS call resolution against ground truth (mirrors java_retrieval.py).
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .oracles import run_typescript_call_oracle, typescript_available
from .retrieval_eval import (
    CallEdge,
    cgr_call_edges,
    score_retrieval,
)
from .structure_report import render, write_outputs
from .types_defs import ScoreResult

console_target = Path(ec.TS_DEFAULT_TARGET)


def oracle_ts_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_typescript_call_oracle(target)


def cgr_ts_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(
        target,
        project,
        declared,
        suffixes=ec.TS_SUFFIXES,
    )


def score_ts_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(
        cgr,
        oracle,
        label=ec.TS_RETRIEVAL_LABEL,
        diff_prefix=ec.TS_RETRIEVAL_DIFF_PREFIX,
        edge_repr=ec.TS_CALL_EDGE_REPR,
    )


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of TypeScript sources to evaluate.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for ts_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not typescript_available():
        logger.error(ls.TS_ORACLE_MISSING.format(binary=ec.NODE_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.TS_RETRIEVAL_ORACLE.format(binary=ec.NODE_BIN, target=target))
    oracle, declared = oracle_ts_call_edges(target)
    logger.success(ls.TS_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.TS_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_ts_call_edges(target, project, declared)
    logger.success(ls.TS_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_ts_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.TS_RETRIEVAL_SCORES_FILENAME,
        ec.TS_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.TS_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
