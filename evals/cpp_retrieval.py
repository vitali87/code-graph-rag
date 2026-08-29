# Multi-language retrieval (C++). Extends the file-level call-localization
# benchmark to C++: for each first-party C++ function/method, which files call
# it. cgr's C++ CALLS edges (reduced to (caller_file, callee_simple_name)) are
# graded against call sites libclang extracts over the same name universe.
# libclang resolves the true translation-unit call graph independent of cgr's
# tree-sitter C++ frontend (CPP_FRONTEND=libclang is off), so this measures
# cgr's cross-file C++ call resolution (mirrors evals/c_retrieval.py).
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .oracles import cpp_available, run_cpp_call_oracle
from .retrieval_eval import (
    CallEdge,
    cgr_call_edges,
    score_retrieval,
)
from .structure_report import render, write_outputs
from .types_defs import ScoreResult

console_target = Path(ec.CPP_DEFAULT_TARGET)


def oracle_cpp_call_edges(
    target: Path, extra_defines: tuple[str, ...] = ()
) -> tuple[set[CallEdge], frozenset[str], frozenset[str]]:
    return run_cpp_call_oracle(target, extra_defines)


def cgr_cpp_call_edges(
    target: Path, project: str, declared: frozenset[str], covered: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(
        target,
        project,
        declared,
        suffixes=ec.CPP_SUFFIXES,
        covered=covered,
    )


def score_cpp_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(
        cgr,
        oracle,
        label=ec.CPP_RETRIEVAL_LABEL,
        diff_prefix=ec.CPP_RETRIEVAL_DIFF_PREFIX,
        edge_repr=ec.CPP_CALL_EDGE_REPR,
    )


def main(
    target: Annotated[
        Path,
        typer.Option(help="Directory of C++ sources to evaluate call retrieval."),
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    define: Annotated[
        list[str],
        typer.Option(help="Preprocessor macro the build would supply, e.g. NAME=1."),
    ] = [],
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for cpp_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not cpp_available():
        logger.error(ls.CPP_RETRIEVAL_ORACLE_MISSING)
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.CPP_RETRIEVAL_ORACLE.format(target=target))
    oracle, declared, covered = oracle_cpp_call_edges(target, tuple(define))
    logger.success(ls.CPP_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))
    logger.info(ls.CPP_RETRIEVAL_COVERED.format(count=len(covered)))

    logger.info(ls.CPP_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_cpp_call_edges(target, project, declared, covered)
    logger.success(ls.CPP_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_cpp_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.CPP_RETRIEVAL_SCORES_FILENAME,
        ec.CPP_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.CPP_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
