"""Shared driver for the per-language L1 structure evals.

Every evals/*_l1.py ran the same program: refuse to run without the language
oracle, extract cgr's graph for the target, extract the oracle's, score the
two against that language's node kinds, then write and render the result.
The languages differ only in their oracle wiring and their message
constants, which they pass in.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer
from loguru import logger

from . import constants as ec
from .score import score_structure
from .structure_report import render, write_outputs
from .types_defs import GraphData


def run_l1_eval(
    target: Path,
    project_name: str,
    out_dir: Path,
    *,
    available: Callable[[], bool],
    oracle_missing: str,
    extract_cgr: Callable[[Path, str], GraphData],
    run_oracle: Callable[[Path], GraphData],
    oracle_binary: str,
    scored_node_kinds: frozenset[str] | tuple[str, ...],
    extracting_cgr: str,
    cgr_done: str,
    extracting_oracle: str,
    oracle_done: str,
    scores_filename: str,
    diff_filename: str,
    title: str,
) -> None:
    if not available():
        logger.error(oracle_missing)
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(extracting_cgr.format(target=target, project=project))
    cgr = extract_cgr(target, project)
    logger.success(cgr_done.format(count=len(cgr.nodes)))

    logger.info(extracting_oracle.format(binary=oracle_binary, target=target))
    oracle = run_oracle(target)
    logger.success(oracle_done.format(count=len(oracle.nodes)))

    result = score_structure(
        cgr, oracle, scored_node_kinds, ec.SCORED_EDGE_TYPES, grade_spans=True
    )
    write_outputs(result, out_dir, scores_filename, diff_filename)
    render(result, title)
