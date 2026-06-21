from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .. import constants as ec
from ..types_defs import DefNode, NodeKey, OracleRecord
from ._common import records_to_nodes

_ORACLE_GO = Path(__file__).parent / ec.GO_ORACLE_GO_FILE


def go_available() -> bool:
    return shutil.which(ec.GO_BIN) is not None


def run_go_oracle(target: Path) -> dict[NodeKey, DefNode]:
    proc = subprocess.run(
        [ec.GO_BIN, ec.GO_RUN, str(_ORACLE_GO), str(target)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, ec.GO_MODULE_ENV: ec.GO_MODULE_OFF},
    )
    records: list[OracleRecord] = json.loads(proc.stdout or "[]")
    return records_to_nodes(records)
