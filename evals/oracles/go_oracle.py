from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import is_ignored, payload_to_graph

_ORACLE_GO = Path(__file__).parent / ec.GO_ORACLE_GO_FILE
_CALLABLE_KINDS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})


def go_available() -> bool:
    return shutil.which(ec.GO_BIN) is not None


def _run_go_oracle_payload(target: Path) -> OraclePayload:
    proc = subprocess.run(
        [ec.GO_BIN, ec.GO_RUN, str(_ORACLE_GO), str(target)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, ec.GO_MODULE_ENV: ec.GO_MODULE_OFF},
    )
    payload: OraclePayload = json.loads(proc.stdout or "{}")
    return payload


def run_go_oracle(target: Path) -> GraphData:
    return payload_to_graph(_run_go_oracle_payload(target))


def run_go_call_oracle(target: Path) -> tuple[set[tuple[str, str]], frozenset[str]]:
    # File-level Go call sites restricted to first-party callees (simple name is
    # a declared Function/Method), with the declared name universe so the cgr
    # side is held to the same set.
    payload = _run_go_oracle_payload(target)
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
