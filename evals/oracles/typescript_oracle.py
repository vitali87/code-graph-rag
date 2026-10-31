from __future__ import annotations

from pathlib import Path

from .. import constants as ec
from ..types_defs import CallOracle, GraphData, OraclePayload
from ._common import (
    node_oracle_available,
    node_oracle_skip_reason,
    payload_call_edges,
    payload_to_graph,
    run_node_oracle_payload,
)

_ORACLE_DIR = Path(__file__).parent / ec.TS_ORACLE_DIRNAME
_SCRIPT = _ORACLE_DIR / ec.TS_ORACLE_SCRIPT


def typescript_available() -> bool:
    return node_oracle_available(_ORACLE_DIR)


def typescript_skip_reason() -> str | None:
    """Why this oracle cannot run here, or None when it can (issue #1639).

    On a clean checkout this answers None because the deps are not installed
    yet and the probe cannot run; `run_node_oracle_payload` re-checks once
    `ensure_node_deps` has fetched them and raises `NodeOracleUnavailable`,
    which the test guards below turn into a skip carrying the real reason.
    """
    return node_oracle_skip_reason(_ORACLE_DIR)


def _run_payload(target: Path, suffixes: tuple[str, ...]) -> OraclePayload:
    return run_node_oracle_payload(_ORACLE_DIR, _SCRIPT, (str(target), *suffixes))


def _run(target: Path, suffixes: tuple[str, ...]) -> GraphData:
    return payload_to_graph(_run_payload(target, suffixes))


def run_typescript_oracle(target: Path) -> GraphData:
    return _run(target, ec.TS_SUFFIXES)


def run_javascript_oracle(target: Path) -> GraphData:
    return _run(target, ec.JS_SUFFIXES)


def run_typescript_call_oracle(target: Path) -> CallOracle:
    return payload_call_edges(_run_payload(target, ec.TS_SUFFIXES))


def run_javascript_call_oracle(target: Path) -> CallOracle:
    # The same tsc oracle over .js/.jsx: tsc's syntactic parse handles JS, so
    # this stays independent of cgr's tree-sitter JS frontend.
    return payload_call_edges(_run_payload(target, ec.JS_SUFFIXES))
