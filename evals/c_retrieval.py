# Multi-language retrieval (C). Extends the file-level call-localization
# benchmark to C: for each first-party C function, which files call it.
# cgr's C CALLS edges (reduced to (caller_file, callee_simple_name)) are
# graded against call sites libclang extracts over the same name universe.
# libclang resolves the true translation-unit call graph independent of
# cgr's tree-sitter C frontend (CPP_FRONTEND=libclang is off), so this
# measures cgr's cross-file C call resolution.
from pathlib import Path
from typing import Annotated

import typer

from . import constants as ec
from .retrieval_eval import (
    RETRIEVAL_SPECS,
    cgr_call_edges,
    run_retrieval_eval,
    score_retrieval,
)
from .types_defs import CallEdge, CallOracle, ScoreResult

_SPEC = RETRIEVAL_SPECS[ec.RetrievalLanguage.C]


def oracle_c_call_edges(target: Path) -> CallOracle:
    return _SPEC.run_oracle(target)


def cgr_c_call_edges(
    target: Path, project: str, declared: frozenset[str], covered: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(_SPEC, target, project, declared, covered)


def score_c_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(_SPEC, cgr, oracle)


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of C sources to evaluate call retrieval.")
    ] = Path(ec.LANG_RETRIEVAL_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for c_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    run_retrieval_eval(_SPEC, target, project_name, out_dir)


if __name__ == "__main__":
    typer.run(main)
