# Multi-language retrieval (PHP). Extends the file-level call-localization
# benchmark to PHP: for each first-party PHP symbol, which files call it.
# cgr's PHP CALLS edges (reduced to (caller_file, callee_simple_name)) are
# graded against php-parser call sites over the same first-party name universe.
# php-parser is independent of cgr's tree-sitter PHP frontend, so this measures
# cgr's cross-file PHP call resolution (mirrors evals/java_retrieval.py /
# ts_retrieval.py).
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .oracles import php_oracle_available, run_php_call_oracle
from .retrieval_eval import (
    CallEdge,
    cgr_call_edges,
    score_retrieval,
)
from .structure_report import render, write_outputs
from .types_defs import ScoreResult

console_target = Path(ec.PHP_DEFAULT_TARGET)


def oracle_php_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_php_call_oracle(target)


def cgr_php_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(
        target,
        project,
        declared,
        suffixes=ec.PHP_SUFFIX,
    )


def score_php_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(
        cgr,
        oracle,
        label=ec.PHP_RETRIEVAL_LABEL,
        diff_prefix=ec.PHP_RETRIEVAL_DIFF_PREFIX,
        edge_repr=ec.PHP_CALL_EDGE_REPR,
    )


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of PHP sources to evaluate call retrieval.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for php_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not php_oracle_available():
        logger.error(ls.PHP_ORACLE_MISSING.format(binary=ec.NODE_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.PHP_RETRIEVAL_ORACLE.format(binary=ec.NODE_BIN, target=target))
    oracle, declared = oracle_php_call_edges(target)
    logger.success(ls.PHP_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.PHP_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_php_call_edges(target, project, declared)
    logger.success(ls.PHP_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_php_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.PHP_RETRIEVAL_SCORES_FILENAME,
        ec.PHP_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.PHP_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
