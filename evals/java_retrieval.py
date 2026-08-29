# Multi-language retrieval (Java). File-level call-localization: for each
# first-party Java symbol, which files call it. cgr's Java CALLS edges (caller
# file plus callee simple name) are graded against javac method-invocation
# sites over the same first-party name universe. The JDK's Compiler Tree API
# (javac) is independent of cgr's tree-sitter frontend, so this measures cgr's
# cross-file Java call resolution against ground truth (mirrors
# evals/rust_retrieval.py).
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .oracles import java_available, run_java_call_oracle
from .retrieval_eval import (
    CallEdge,
    cgr_call_edges,
    score_retrieval,
)
from .structure_report import render, write_outputs
from .types_defs import ScoreResult

console_target = Path(ec.JAVA_DEFAULT_TARGET)


def oracle_java_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_java_call_oracle(target)


def cgr_java_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(
        target,
        project,
        declared,
        suffixes=ec.JAVA_SUFFIX,
        strip_signature=True,
    )


def score_java_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(
        cgr,
        oracle,
        label=ec.JAVA_RETRIEVAL_LABEL,
        diff_prefix=ec.JAVA_RETRIEVAL_DIFF_PREFIX,
        edge_repr=ec.JAVA_CALL_EDGE_REPR,
    )


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Java sources to evaluate call retrieval.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for java_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not java_available():
        logger.error(ls.JAVA_ORACLE_MISSING.format(binary=ec.JAVAC_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.JAVA_RETRIEVAL_ORACLE.format(binary=ec.JAVAC_BIN, target=target))
    oracle, declared = oracle_java_call_edges(target)
    logger.success(ls.JAVA_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.JAVA_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_java_call_edges(target, project, declared)
    logger.success(ls.JAVA_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_java_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.JAVA_RETRIEVAL_SCORES_FILENAME,
        ec.JAVA_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.JAVA_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
