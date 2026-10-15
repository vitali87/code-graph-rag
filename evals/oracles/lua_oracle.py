from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import CallOracle, GraphData, OraclePayload
from ._common import (
    node_oracle_available,
    node_oracle_skip_reason,
    payload_call_edges,
    payload_to_graph,
    run_node_oracle_payload,
)

_ORACLE_DIR = Path(__file__).parent / ec.LUA_ORACLE_DIRNAME
_SCRIPT = _ORACLE_DIR / ec.LUA_ORACLE_SCRIPT
_CALLABLE_KINDS = frozenset({cs.NodeLabel.FUNCTION.value})


def lua_oracle_available() -> bool:
    return node_oracle_available(_ORACLE_DIR)


def lua_oracle_skip_reason() -> str | None:
    """Why this oracle cannot run here, or None when it can (issue #1639).

    On a clean checkout this answers None because the deps are not installed
    yet and the probe cannot run; `run_node_oracle_payload` re-checks once
    `ensure_node_deps` has fetched them and raises `NodeOracleUnavailable`,
    which the test guards below turn into a skip carrying the real reason.
    """
    return node_oracle_skip_reason(_ORACLE_DIR)


def _run_lua_oracle_payload(target: Path) -> OraclePayload:
    return run_node_oracle_payload(_ORACLE_DIR, _SCRIPT, (str(target),))


def run_lua_oracle(target: Path) -> GraphData:
    return payload_to_graph(_run_lua_oracle_payload(target))


def run_lua_call_oracle(target: Path) -> CallOracle:
    return payload_call_edges(_run_lua_oracle_payload(target), _CALLABLE_KINDS)
