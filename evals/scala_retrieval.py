# Multi-language retrieval (Scala). Extends the file-level call-localization
# benchmark to Scala: for each first-party Scala symbol, which files call it.
# cgr's Scala CALLS edges (reduced to caller file + callee simple name) are
# graded against scalameta call sites over the same first-party name universe.
# scalameta (via scala-cli) is independent of cgr's tree-sitter frontend, so
# this measures cgr's cross-file Scala call resolution.
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

_SPEC = RETRIEVAL_SPECS[ec.RetrievalLanguage.SCALA]


def oracle_scala_call_edges(target: Path) -> CallOracle:
    return _SPEC.run_oracle(target)


def cgr_scala_call_edges(
    target: Path, project: str, declared: frozenset[str], covered: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(_SPEC, target, project, declared, covered)


def score_scala_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(_SPEC, cgr, oracle)


def main(
    target: Annotated[
        Path,
        typer.Option(help="Directory of Scala sources to evaluate call retrieval."),
    ] = Path(ec.LANG_RETRIEVAL_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for scala_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    run_retrieval_eval(_SPEC, target, project_name, out_dir)


if __name__ == "__main__":
    typer.run(main)
