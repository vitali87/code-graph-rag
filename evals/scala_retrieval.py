# Multi-language retrieval (Scala). Extends the file-level call-localization
# benchmark to Scala: for each first-party Scala symbol, which files call it.
# cgr's Scala CALLS edges (reduced to caller file + callee simple name) are
# graded against scalameta call sites over the same first-party name universe.
# scalameta (via scala-cli) is independent of cgr's tree-sitter frontend, so
# this measures cgr's cross-file Scala call resolution (mirrors
# evals/java_retrieval.py).
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .oracles import run_scala_call_oracle, scala_available
from .retrieval_eval import (
    CallEdge,
    cgr_call_edges,
    score_retrieval,
)
from .structure_report import render, write_outputs
from .types_defs import ScoreResult

console_target = Path(ec.SCALA_DEFAULT_TARGET)


def oracle_scala_call_edges(
    target: Path,
) -> tuple[set[CallEdge], frozenset[str], frozenset[str]]:
    return run_scala_call_oracle(target)


def cgr_scala_call_edges(
    target: Path, project: str, declared: frozenset[str], covered: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(
        target,
        project,
        declared,
        suffixes=ec.SCALA_SUFFIXES,
        # Reduce a callee qn to its trailing simple name to match the oracle,
        # dropping dotted scope and (defensively) a parameter signature.
        strip_signature=True,
        covered=covered,
    )


def score_scala_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(
        cgr,
        oracle,
        label=ec.SCALA_RETRIEVAL_LABEL,
        diff_prefix=ec.SCALA_RETRIEVAL_DIFF_PREFIX,
        edge_repr=ec.SCALA_CALL_EDGE_REPR,
    )


def main(
    target: Annotated[
        Path,
        typer.Option(help="Directory of Scala sources to evaluate call retrieval."),
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for scala_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not scala_available():
        logger.error(ls.SCALA_ORACLE_MISSING.format(binary=ec.SCALA_CLI_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(
        ls.SCALA_RETRIEVAL_ORACLE.format(binary=ec.SCALA_CLI_BIN, target=target)
    )
    oracle, declared, covered = oracle_scala_call_edges(target)
    logger.success(ls.SCALA_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))
    logger.info(ls.SCALA_RETRIEVAL_COVERED.format(count=len(covered)))

    logger.info(ls.SCALA_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_scala_call_edges(target, project, declared, covered)
    logger.success(ls.SCALA_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_scala_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.SCALA_RETRIEVAL_SCORES_FILENAME,
        ec.SCALA_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.SCALA_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
