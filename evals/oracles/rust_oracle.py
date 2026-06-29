from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import is_ignored, payload_to_graph

_ORACLE_DIR = Path(__file__).parent / ec.RS_ORACLE_DIRNAME
_MANIFEST = _ORACLE_DIR / "Cargo.toml"
_CALLABLE_KINDS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})


def rust_available() -> bool:
    return shutil.which(ec.CARGO_BIN) is not None


def _run_rust_oracle_payload(target: Path) -> OraclePayload:
    proc = subprocess.run(
        [
            ec.CARGO_BIN,
            ec.CARGO_RUN,
            ec.CARGO_RELEASE,
            ec.CARGO_QUIET,
            ec.CARGO_MANIFEST,
            str(_MANIFEST),
            ec.CARGO_ARG_SEP,
            str(target),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload: OraclePayload = json.loads(proc.stdout or "{}")
    return payload


def run_rust_oracle(target: Path) -> GraphData:
    return payload_to_graph(_run_rust_oracle_payload(target))


def run_rust_call_oracle(target: Path) -> tuple[set[tuple[str, str]], frozenset[str]]:
    # (H) File-level Rust call sites restricted to first-party callees (a callee
    # (H) whose simple name is a declared Function/Method), with the declared name
    # (H) universe so the cgr side can be held to the same set. Mirrors the Go
    # (H) call oracle (run_go_call_oracle).
    payload = _run_rust_oracle_payload(target)
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
