from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import is_ignored, payload_to_graph

_ORACLE_DIR = Path(__file__).parent / ec.JAVA_ORACLE_DIRNAME
_SOURCE = _ORACLE_DIR / ec.JAVA_ORACLE_SOURCE
_CLASS = _ORACLE_DIR / f"{ec.JAVA_ORACLE_CLASS}.class"
_CALLABLE_KINDS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})


def java_available() -> bool:
    return (
        shutil.which(ec.JAVAC_BIN) is not None and shutil.which(ec.JAVA_BIN) is not None
    )


def _ensure_compiled() -> None:
    # (H) Recompile when the class is missing OR older than the source, so an
    # (H) edited Oracle.java is never shadowed by a stale (gitignored) .class.
    if _CLASS.is_file() and _CLASS.stat().st_mtime >= _SOURCE.stat().st_mtime:
        return
    javac = shutil.which(ec.JAVAC_BIN)
    if javac is None:
        return
    subprocess.run(
        [javac, str(_SOURCE)],
        cwd=str(_ORACLE_DIR),
        capture_output=True,
        text=True,
        check=True,
    )


def _run_java_oracle_payload(target: Path) -> OraclePayload:
    _ensure_compiled()
    java = shutil.which(ec.JAVA_BIN)
    if java is None:
        return OraclePayload(nodes=[], edges=[], name_edges=[])
    proc = subprocess.run(
        [java, ec.JAVA_CP_FLAG, str(_ORACLE_DIR), ec.JAVA_ORACLE_CLASS, str(target)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload: OraclePayload = json.loads(proc.stdout or "{}")
    return payload


def run_java_oracle(target: Path) -> GraphData:
    return payload_to_graph(_run_java_oracle_payload(target))


def run_java_call_oracle(target: Path) -> tuple[set[tuple[str, str]], frozenset[str]]:
    # (H) File-level Java call sites restricted to first-party callees (a callee
    # (H) whose simple name is a declared Function/Method), with the declared name
    # (H) universe so the cgr side can be held to the same set. Mirrors the Go and
    # (H) Rust call oracles (run_go_call_oracle / run_rust_call_oracle).
    payload = _run_java_oracle_payload(target)
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
