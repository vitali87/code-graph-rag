from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .. import constants as ec
from ..types_defs import DefNode, NodeKey, OracleRecord
from ._common import records_to_nodes

_ORACLE_DIR = Path(__file__).parent / ec.RS_ORACLE_DIRNAME
_MANIFEST = _ORACLE_DIR / "Cargo.toml"


def rust_available() -> bool:
    return shutil.which(ec.CARGO_BIN) is not None


def run_rust_oracle(target: Path) -> dict[NodeKey, DefNode]:
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
    records: list[OracleRecord] = json.loads(proc.stdout or "[]")
    return records_to_nodes(records)
