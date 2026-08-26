# Multi-language retrieval (C#). File-level call-localization: for each
# first-party C# symbol, which files call it. cgr's C# CALLS edges (caller file
# plus callee simple name) are graded against Roslyn invocation sites over the
# same first-party name universe. Roslyn's syntax parser is independent of cgr's
# tree-sitter frontend, so this measures cgr's cross-file C# call resolution
# against ground truth (mirrors evals/java_retrieval.py). Run with
# CSHARP_FRONTEND=hybrid to grade the opt-in Roslyn semantic frontend.
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .oracles import csharp_oracle_available, run_csharp_call_oracle
from .retrieval_eval import (
    CALLS,
    INSTANTIATES,
    CallEdge,
    cgr_call_edges,
    score_retrieval,
)
from .structure_report import render, write_outputs
from .types_defs import ScoreResult

console_target = Path(ec.CSHARP_DEFAULT_TARGET)


def oracle_csharp_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_csharp_call_oracle(target)


def cgr_csharp_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(
        target,
        project,
        declared,
        suffixes=ec.CS_SUFFIX,
        # INSTANTIATES counts too (as in the Python retrieval): `new T()` on
        # a type with no explicit constructor has no ctor node to CALL, only
        # an INSTANTIATES edge to the class, which the oracle records by type
        # name.
        rel_types=(CALLS, INSTANTIATES),
        # A C# Method qn carries its overload signature (Class.Name(args)).
        strip_signature=True,
    )


def score_csharp_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(
        cgr,
        oracle,
        label=ec.CSHARP_RETRIEVAL_LABEL,
        diff_prefix=ec.CSHARP_RETRIEVAL_DIFF_PREFIX,
        edge_repr=ec.CSHARP_CALL_EDGE_REPR,
    )


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of C# sources to evaluate call retrieval.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for csharp_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not csharp_oracle_available():
        logger.error(ls.CSHARP_ORACLE_MISSING)
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.CSHARP_RETRIEVAL_ORACLE.format(binary=ec.DOTNET_BIN, target=target))
    oracle, declared = oracle_csharp_call_edges(target)
    logger.success(ls.CSHARP_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.CSHARP_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_csharp_call_edges(target, project, declared)
    logger.success(ls.CSHARP_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_csharp_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.CSHARP_RETRIEVAL_SCORES_FILENAME,
        ec.CSHARP_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.CSHARP_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
