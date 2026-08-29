# Multi-language retrieval (JavaScript). Mirrors ts_retrieval.py for .js/.jsx:
# cgr's JS CALLS edges ((caller_file, callee_simple_name)) are graded against
# call sites the TypeScript compiler API (tsc) extracts from the same files,
# over the same first-party name universe. tsc parses JS syntactically and is
# independent of cgr's tree-sitter JS frontend, so this measures cgr's
# cross-file JS call resolution against ground truth.
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as ec
from . import logs as ls
from .oracles import run_javascript_call_oracle, typescript_available
from .retrieval_eval import (
    CallEdge,
    cgr_call_edges,
    score_retrieval,
)
from .structure_report import render, write_outputs
from .types_defs import ScoreResult

console_target = Path(ec.JS_DEFAULT_TARGET)


def oracle_js_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_javascript_call_oracle(target)


def cgr_js_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    return cgr_call_edges(
        target,
        project,
        declared,
        suffixes=ec.JS_SUFFIXES,
    )


def score_js_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    return score_retrieval(
        cgr,
        oracle,
        label=ec.JS_RETRIEVAL_LABEL,
        diff_prefix=ec.JS_RETRIEVAL_DIFF_PREFIX,
        edge_repr=ec.JS_CALL_EDGE_REPR,
    )


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of JavaScript sources to evaluate.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for js_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not typescript_available():
        logger.error(ls.TS_ORACLE_MISSING.format(binary=ec.NODE_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.JS_RETRIEVAL_ORACLE.format(binary=ec.NODE_BIN, target=target))
    oracle, declared = oracle_js_call_edges(target)
    logger.success(ls.JS_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.JS_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_js_call_edges(target, project, declared)
    logger.success(ls.JS_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_js_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.JS_RETRIEVAL_SCORES_FILENAME,
        ec.JS_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.JS_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
