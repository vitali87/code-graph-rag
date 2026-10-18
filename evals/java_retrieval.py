# Multi-language retrieval (Java). File-level call-localization: for each
# first-party Java symbol, which files call it. cgr's Java CALLS edges (caller
# file plus callee simple name) are graded against javac method-invocation
# sites over the same first-party name universe. The JDK's Compiler Tree API
# (javac) is independent of cgr's tree-sitter frontend, so this measures cgr's
# cross-file Java call resolution against ground truth.
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
from .types_defs import CallEdge, ScoreResult

_SPEC = RETRIEVAL_SPECS[ec.RetrievalLanguage.JAVA]


def oracle_java_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    oracle = _SPEC.run_oracle(target)
    return oracle.edges, oracle.declared


def cgr_java_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(_SPEC, target, project, declared)


def score_java_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(_SPEC, cgr, oracle)


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Java sources to evaluate call retrieval.")
    ] = Path(ec.LANG_RETRIEVAL_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for java_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    run_retrieval_eval(_SPEC, target, project_name, out_dir)


if __name__ == "__main__":
    typer.run(main)
