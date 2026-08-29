# Multi-language retrieval (Rust). Extends the file-level call-localization
# benchmark to Rust: for each first-party Rust symbol, which files call it.
# cgr's Rust CALLS edges (reduced to caller file + callee simple name) are
# graded against syn call sites over the same first-party name universe. syn is
# Rust's own parser, independent of cgr's tree-sitter frontend, so this measures
# cgr's cross-file Rust call resolution (mirrors evals/go_retrieval.py).
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .oracles import run_rust_call_oracle, rust_available
from .retrieval_eval import (
    CallEdge,
    cgr_call_edges,
    score_retrieval,
)
from .structure_report import render, write_outputs
from .types_defs import ScoreResult

console_target = Path(ec.RUST_DEFAULT_TARGET)


def oracle_rust_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_rust_call_oracle(target)


def cgr_rust_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(
        target,
        project,
        declared,
        suffixes=ec.RS_SUFFIX,
    )


def score_rust_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(
        cgr,
        oracle,
        label=ec.RUST_RETRIEVAL_LABEL,
        diff_prefix=ec.RUST_RETRIEVAL_DIFF_PREFIX,
        edge_repr=ec.RUST_CALL_EDGE_REPR,
    )


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Rust sources to evaluate call retrieval.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for rust_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not rust_available():
        logger.error(ls.RUST_ORACLE_MISSING.format(binary=ec.CARGO_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.RUST_RETRIEVAL_ORACLE.format(binary=ec.CARGO_BIN, target=target))
    oracle, declared = oracle_rust_call_edges(target)
    logger.success(ls.RUST_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.RUST_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_rust_call_edges(target, project, declared)
    logger.success(ls.RUST_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_rust_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.RUST_RETRIEVAL_SCORES_FILENAME,
        ec.RUST_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.RUST_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
