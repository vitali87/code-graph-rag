# Multi-language retrieval (TypeScript). Extends the file-level
# call-localization benchmark to TypeScript: for each first-party TS symbol,
# which files call it. cgr's TS CALLS edges (reduced to (caller_file,
# callee_simple_name)) are graded against call sites from the TypeScript
# compiler API (tsc), over the same first-party name universe. tsc is
# independent of cgr's tree-sitter TS frontend, so this measures cgr's
# cross-file TS call resolution against ground truth.
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

_SPEC = RETRIEVAL_SPECS[ec.RetrievalLanguage.TS]


def oracle_ts_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    oracle = _SPEC.run_oracle(target)
    return oracle.edges, oracle.declared


def cgr_ts_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(_SPEC, target, project, declared)


def score_ts_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(_SPEC, cgr, oracle)


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of TypeScript sources to evaluate.")
    ] = Path(ec.LANG_RETRIEVAL_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for ts_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    run_retrieval_eval(_SPEC, target, project_name, out_dir)


if __name__ == "__main__":
    typer.run(main)
