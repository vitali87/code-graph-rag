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

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .oracles import NodeOracleUnavailable
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
    skip_reason: Callable[[], str | None] | None = None,
    extract_cgr: Callable[[Path, str], GraphData],
    run_oracle: Callable[[Path], GraphData],
    oracle_binary: str,
    # `tuple[NodeLabel, ...]` because that is what every caller passes
    # (GO_/RS_/TS_/JS_/JAVA_/CSHARP_/LUA_/PHP_SCORED_NODE_KINDS are all
    # declared so) AND what `score_structure` requires. The wider
    # `frozenset[str] | tuple[str, ...]` admitted neither caller nor
    # callee faithfully: no caller passes a frozenset, and a `str`
    # element is rejected downstream, so the annotation described a
    # contract this function never had.
    scored_node_kinds: tuple[cs.NodeLabel, ...],
    extracting_cgr: str,
    cgr_done: str,
    extracting_oracle: str,
    oracle_done: str,
    scores_filename: str,
    diff_filename: str,
    title: str,
) -> None:
    if not available():
        # Prefer the probe's own reason when the caller can supply one: the
        # fixed message says "not found on PATH", which is FALSE in the case
        # that matters -- the binary is there and cannot load the parser
        # (issue #1639). Fall back to the fixed string for oracles with no
        # reason probe.
        #
        # Formatted here, as `extracting_oracle` already is below, because the
        # arm that reports a MISSING toolchain is the one arm that never runs
        # and so is never seen by whoever tested the language (issue #1518).
        # Callers that pre-format, or whose message has no placeholder, are
        # unaffected: `str.format` on a string with no fields is the identity.
        reason = skip_reason() if skip_reason is not None else None
        logger.error(
            ls.ORACLE_UNAVAILABLE.format(reason=reason)
            if reason
            else oracle_missing.format(binary=oracle_binary)
        )
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(extracting_cgr.format(target=target, project=project))
    cgr = extract_cgr(target, project)
    logger.success(cgr_done.format(count=len(cgr.nodes)))

    logger.info(extracting_oracle.format(binary=oracle_binary, target=target))
    try:
        oracle = run_oracle(target)
    except NodeOracleUnavailable as unavailable:
        # The `available()` arm above cannot catch this one. On a clean
        # checkout the dependencies are not installed when the guard runs, so
        # it honestly answers "cannot tell yet"; the toolchain's real verdict
        # only exists once `ensure_node_deps` has fetched them, which happens
        # inside `run_oracle`. Reported the same way as the arm above rather
        # than escaping as a traceback: an unavailable toolchain is a result
        # this command should state, not a crash (issue #1639).
        logger.error(ls.ORACLE_UNAVAILABLE.format(reason=unavailable))
        raise typer.Exit(code=1) from unavailable
    logger.success(oracle_done.format(count=len(oracle.nodes)))

    result = score_structure(
        cgr, oracle, scored_node_kinds, ec.SCORED_EDGE_TYPES, grade_spans=True
    )
    write_outputs(result, out_dir, scores_filename, diff_filename)
    render(result, title)
