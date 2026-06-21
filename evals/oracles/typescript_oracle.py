from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .. import constants as ec
from ..types_defs import DefNode, NodeKey, OracleRecord
from ._common import records_to_nodes

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


def run_typescript_oracle(target: Path) -> dict[NodeKey, DefNode]:
    _ensure_deps()
    node = shutil.which(ec.NODE_BIN)
    if node is None:
        return {}
    proc = subprocess.run(
        [node, str(_SCRIPT), str(target)],
        capture_output=True,
        text=True,
        check=True,
    )
    records: list[OracleRecord] = json.loads(proc.stdout or "[]")
    return records_to_nodes(records)
