from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import payload_to_graph

_ORACLE_DIR = Path(__file__).parent / ec.JAVA_ORACLE_DIRNAME
_SOURCE = _ORACLE_DIR / ec.JAVA_ORACLE_SOURCE
_CLASS = _ORACLE_DIR / f"{ec.JAVA_ORACLE_CLASS}.class"


def java_available() -> bool:
    return (
        shutil.which(ec.JAVAC_BIN) is not None and shutil.which(ec.JAVA_BIN) is not None
    )


def _ensure_compiled() -> None:
    if _CLASS.is_file():
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


def run_java_oracle(target: Path) -> GraphData:
    _ensure_compiled()
    java = shutil.which(ec.JAVA_BIN)
    if java is None:
        return GraphData(nodes={}, edges=set(), name_edges=set())
    proc = subprocess.run(
        [java, ec.JAVA_CP_FLAG, str(_ORACLE_DIR), ec.JAVA_ORACLE_CLASS, str(target)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload: OraclePayload = json.loads(proc.stdout or "{}")
    return payload_to_graph(payload)
