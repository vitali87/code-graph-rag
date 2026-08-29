# Multi-language retrieval (Go). File-level call-localization: for each
# first-party Go symbol, which files call it. cgr's Go CALLS edges (caller file
# plus callee simple name) are graded against go/ast call sites over the same
# first-party name universe. Go's own parser is independent of cgr's tree-sitter
# frontend, so this measures cgr's cross-file Go call resolution against ground
# truth.
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .oracles import go_available, run_go_call_oracle
from .retrieval_eval import (
    CallEdge,
    cgr_call_edges,
    score_retrieval,
)
from .structure_report import render, write_outputs
from .types_defs import ScoreResult

console_target = Path(ec.GO_DEFAULT_TARGET)


def oracle_go_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_go_call_oracle(target)


def cgr_go_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(
        target,
        project,
        declared,
        suffixes=ec.GO_SUFFIX,
    )


def score_go_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(
        cgr,
        oracle,
        label=ec.GO_RETRIEVAL_LABEL,
        diff_prefix=ec.GO_RETRIEVAL_DIFF_PREFIX,
        edge_repr=ec.GO_CALL_EDGE_REPR,
    )


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Go sources to evaluate call retrieval.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for go_retrieval_scores.csv and diff json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not go_available():
        logger.error(ls.GO_ORACLE_MISSING.format(binary=ec.GO_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.GO_RETRIEVAL_ORACLE.format(binary=ec.GO_BIN, target=target))
    oracle, declared = oracle_go_call_edges(target)
    logger.success(ls.GO_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.GO_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_go_call_edges(target, project, declared)
    logger.success(ls.GO_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_go_retrieval(cgr, oracle)
    write_outputs(
        result, out_dir, ec.GO_RETRIEVAL_SCORES_FILENAME, ec.GO_RETRIEVAL_DIFF_FILENAME
    )
    render(result, ec.GO_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
