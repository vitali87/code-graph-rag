from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import DefNode, GoOracleRecord, NodeKey

_ORACLE_GO = Path(__file__).parent / ec.GO_ORACLE_GO_FILE


def go_available() -> bool:
    return shutil.which(ec.GO_BIN) is not None


def _is_ignored(rel_file: str) -> bool:
    # (H) Mirror cgr's directory-component ignore (path_utils.should_skip_path)
    # (H) so the oracle grades the same file set cgr indexes.
    dir_parts = PurePosixPath(rel_file).parent.parts
    return not cs.IGNORE_PATTERNS.isdisjoint(dir_parts)


def run_go_oracle(target: Path) -> dict[NodeKey, DefNode]:
    proc = subprocess.run(
        [ec.GO_BIN, ec.GO_RUN, str(_ORACLE_GO), str(target)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, ec.GO_MODULE_ENV: ec.GO_MODULE_OFF},
    )
    records: list[GoOracleRecord] = json.loads(proc.stdout or "[]")
    nodes: dict[NodeKey, DefNode] = {}
    for rec in records:
        rel_file = rec[ec.ORACLE_KEY_FILE]
        if _is_ignored(rel_file):
            continue
        line = int(rec[ec.ORACLE_KEY_LINE])
        key = NodeKey(rec[ec.ORACLE_KEY_KIND], rel_file, line)
        nodes[key] = DefNode(key, rec[ec.ORACLE_KEY_NAME], line)
    return nodes
