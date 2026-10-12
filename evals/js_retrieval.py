# Multi-language retrieval (JavaScript). Mirrors ts_retrieval.py for .js/.jsx:
# cgr's JS CALLS edges ((caller_file, callee_simple_name)) are graded against
# call sites the TypeScript compiler API (tsc) extracts from the same files,
# over the same first-party name universe. tsc parses JS syntactically and is
# independent of cgr's tree-sitter JS frontend, so this measures cgr's
# cross-file JS call resolution against ground truth.
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

_SPEC = RETRIEVAL_SPECS[ec.RetrievalLanguage.JS]


def oracle_js_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    oracle = _SPEC.run_oracle(target)
    return oracle.edges, oracle.declared


def cgr_js_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(_SPEC, target, project, declared)


def score_js_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(_SPEC, cgr, oracle)


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of JavaScript sources to evaluate.")
    ] = Path(ec.LANG_RETRIEVAL_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for js_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    run_retrieval_eval(_SPEC, target, project_name, out_dir)


if __name__ == "__main__":
    typer.run(main)
