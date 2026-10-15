# Multi-language retrieval (C++). Extends the file-level call-localization
# benchmark to C++: for each first-party C++ function/method, which files call
# it. cgr's C++ CALLS edges (reduced to (caller_file, callee_simple_name)) are
# graded against call sites libclang extracts over the same name universe.
# libclang resolves the true translation-unit call graph independent of cgr's
# tree-sitter C++ frontend (CPP_FRONTEND=libclang is off), so this measures
# cgr's cross-file C++ call resolution.
from functools import partial
from pathlib import Path
from typing import Annotated

import typer

from . import constants as ec
from .oracles import run_cpp_call_oracle
from .retrieval_eval import (
    RETRIEVAL_SPECS,
    cgr_call_edges,
    run_retrieval_eval,
    score_retrieval,
)
from .types_defs import CallEdge, CallOracle, ScoreResult

_SPEC = RETRIEVAL_SPECS[ec.RetrievalLanguage.CPP]


def oracle_cpp_call_edges(
    target: Path, extra_defines: tuple[str, ...] = ()
) -> CallOracle:
    return run_cpp_call_oracle(target, extra_defines)


def cgr_cpp_call_edges(
    target: Path, project: str, declared: frozenset[str], covered: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(_SPEC, target, project, declared, covered)


def score_cpp_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(_SPEC, cgr, oracle)


def main(
    target: Annotated[
        Path,
        typer.Option(help="Directory of C++ sources to evaluate call retrieval."),
    ] = Path(ec.LANG_RETRIEVAL_DEFAULT_TARGET),
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
    # The defines a build would supply reach only the oracle: cgr's tree-sitter
    # frontend does not preprocess, so there is nothing to hand it.
    run_oracle = partial(oracle_cpp_call_edges, extra_defines=tuple(define))
    run_retrieval_eval(
        _SPEC._replace(run_oracle=run_oracle), target, project_name, out_dir
    )


if __name__ == "__main__":
    typer.run(main)
