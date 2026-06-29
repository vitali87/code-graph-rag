from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import is_ignored, payload_to_graph

_ORACLE_DIR = Path(__file__).parent / ec.LUA_ORACLE_DIRNAME
_SCRIPT = _ORACLE_DIR / ec.LUA_ORACLE_SCRIPT
_NODE_MODULES = _ORACLE_DIR / ec.NODE_MODULES_DIRNAME
_CALLABLE_KINDS = frozenset({cs.NodeLabel.FUNCTION.value})


def lua_oracle_available() -> bool:
    return (
        shutil.which(ec.NODE_BIN) is not None and shutil.which(ec.NPM_BIN) is not None
    )


def _ensure_deps() -> None:
    if _NODE_MODULES.is_dir():
        return
    npm = shutil.which(ec.NPM_BIN)
    if npm is None:
        return
    subprocess.run(
        [npm, ec.NPM_INSTALL, *ec.NPM_FLAGS],
        cwd=str(_ORACLE_DIR),
        capture_output=True,
        text=True,
        check=True,
    )


def _run_lua_oracle_payload(target: Path) -> OraclePayload:
    _ensure_deps()
    node = shutil.which(ec.NODE_BIN)
    if node is None:
        return OraclePayload(nodes=[], edges=[], name_edges=[])
    proc = subprocess.run(
        [node, str(_SCRIPT), str(target)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload: OraclePayload = json.loads(proc.stdout or "{}")
    return payload


def run_lua_oracle(target: Path) -> GraphData:
    return payload_to_graph(_run_lua_oracle_payload(target))


def run_lua_call_oracle(target: Path) -> tuple[set[tuple[str, str]], frozenset[str]]:
    # (H) File-level Lua call sites restricted to first-party callees (a callee
    # (H) whose simple name is a declared Function), with the declared name
    # (H) universe so the cgr side can be held to the same set. Mirrors the Go,
    # (H) Rust, Java, TypeScript, and PHP call oracles.
    payload = _run_lua_oracle_payload(target)
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
