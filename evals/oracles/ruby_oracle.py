from __future__ import annotations

from pathlib import Path

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import (
    node_oracle_skip_reason,
    payload_to_graph,
    run_node_oracle_payload,
)

_ORACLE_DIR = Path(__file__).parent / ec.RUBY_ORACLE_DIRNAME
_SCRIPT = _ORACLE_DIR / ec.RUBY_ORACLE_SCRIPT


def ruby_oracle_skip_reason() -> str | None:
    """Why this oracle cannot run here, or None when it can (issue #1639).

    On a clean checkout this answers None because the deps are not installed
    yet and the probe cannot run; `run_node_oracle_payload` re-checks once
    `ensure_node_deps` has fetched them and raises `NodeOracleUnavailable`,
    which the test guards below turn into a skip carrying the real reason.
    """
    return node_oracle_skip_reason(_ORACLE_DIR)


def _run_ruby_oracle_payload(target: Path) -> OraclePayload:
    return run_node_oracle_payload(_ORACLE_DIR, _SCRIPT, (str(target),))


def run_ruby_oracle(target: Path) -> GraphData:
    return payload_to_graph(_run_ruby_oracle_payload(target))
