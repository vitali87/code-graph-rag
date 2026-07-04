from __future__ import annotations

import shutil
from pathlib import Path

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import is_ignored, payload_to_graph, run_node_oracle_payload

_ORACLE_DIR = Path(__file__).parent / ec.PHP_ORACLE_DIRNAME
_SCRIPT = _ORACLE_DIR / ec.PHP_ORACLE_SCRIPT
_NODE_MODULES = _ORACLE_DIR / ec.NODE_MODULES_DIRNAME
_CALLABLE_KINDS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})


def php_oracle_available() -> bool:
    return (
        shutil.which(ec.NODE_BIN) is not None and shutil.which(ec.NPM_BIN) is not None
    )


def _run_php_oracle_payload(target: Path) -> OraclePayload:
    return run_node_oracle_payload(_ORACLE_DIR, _SCRIPT, (str(target),))


def run_php_oracle(target: Path) -> GraphData:
    return payload_to_graph(_run_php_oracle_payload(target))


def run_php_call_oracle(target: Path) -> tuple[set[tuple[str, str]], frozenset[str]]:
    # (H) File-level PHP call sites restricted to first-party callees (a callee
    # (H) whose simple name is a declared Function/Method), with the declared name
    # (H) universe so the cgr side can be held to the same set. Mirrors the Go,
    # (H) Rust, Java, and TypeScript call oracles.
    payload = _run_php_oracle_payload(target)
    declared = frozenset(
        rec[ec.ORACLE_KEY_NAME]
        for rec in payload.get(ec.ORACLE_KEY_NODES, [])
        if rec.get(ec.ORACLE_KEY_KIND) in _CALLABLE_KINDS
    )
    edges = {
        (call[ec.ORACLE_KEY_FILE], call[ec.ORACLE_KEY_NAME])
        for call in payload.get(ec.ORACLE_KEY_CALLS, [])
        if call[ec.ORACLE_KEY_NAME] in declared
        and not is_ignored(call[ec.ORACLE_KEY_FILE])
    }
    return edges, declared
