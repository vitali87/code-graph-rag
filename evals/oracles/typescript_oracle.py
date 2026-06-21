from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import payload_to_graph

_ORACLE_DIR = Path(__file__).parent / ec.TS_ORACLE_DIRNAME
_SCRIPT = _ORACLE_DIR / ec.TS_ORACLE_SCRIPT
_NODE_MODULES = _ORACLE_DIR / ec.NODE_MODULES_DIRNAME


def typescript_available() -> bool:
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


def _run(target: Path, suffixes: tuple[str, ...]) -> GraphData:
    _ensure_deps()
    node = shutil.which(ec.NODE_BIN)
    if node is None:
        return GraphData(nodes={}, edges=set(), name_edges=set())
    proc = subprocess.run(
        [node, str(_SCRIPT), str(target), *suffixes],
        capture_output=True,
        text=True,
        check=True,
    )
    payload: OraclePayload = json.loads(proc.stdout or "{}")
    return payload_to_graph(payload)


def run_typescript_oracle(target: Path) -> GraphData:
    return _run(target, ec.TS_SUFFIXES)


def run_javascript_oracle(target: Path) -> GraphData:
    return _run(target, ec.JS_SUFFIXES)
