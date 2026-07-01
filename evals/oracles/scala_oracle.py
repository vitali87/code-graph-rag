from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from codebase_rag import constants as cs

from .. import constants as ec
from .. import logs as ls
from ..types_defs import GraphData, OraclePayload
from ._common import is_ignored, payload_to_graph

_ORACLE_DIR = Path(__file__).parent / ec.SCALA_ORACLE_DIRNAME
_SOURCE = _ORACLE_DIR / ec.SCALA_ORACLE_SOURCE
_CALLABLE_KINDS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})


def scala_available() -> bool:
    return shutil.which(ec.SCALA_CLI_BIN) is not None


def _run_scala_oracle_payload(target: Path) -> OraclePayload:
    # (H) scala-cli compiles Oracle.scala (fetching scalameta on first run) and
    # (H) prints one JSON payload to stdout; its own build/download chatter goes to
    # (H) stderr. cwd is pinned to the oracle dir so the .scala-build cache lands
    # (H) there (gitignored) rather than wherever the eval was invoked from.
    scala_cli = shutil.which(ec.SCALA_CLI_BIN)
    if scala_cli is None:
        return OraclePayload(nodes=[], edges=[], name_edges=[])
    proc = subprocess.run(
        [scala_cli, ec.SCALA_CLI_RUN, str(_SOURCE), ec.SCALA_CLI_ARG_SEP, str(target)],
        cwd=str(_ORACLE_DIR),
        capture_output=True,
        text=True,
        check=True,
    )
    # (H) capture_output hides scala-cli's stderr; if stdout is not the expected
    # (H) JSON (a compile error, a changed launcher banner) surface both streams
    # (H) instead of a bare JSONDecodeError with no context.
    try:
        payload: OraclePayload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            ls.SCALA_ORACLE_DECODE_FAILED.format(stderr=proc.stderr, stdout=proc.stdout)
        ) from exc
    return payload


def run_scala_oracle(target: Path) -> GraphData:
    return payload_to_graph(_run_scala_oracle_payload(target))


def run_scala_call_oracle(
    target: Path,
) -> tuple[set[tuple[str, str]], frozenset[str], frozenset[str]]:
    # (H) File-level Scala call sites restricted to first-party callees (a callee
    # (H) whose simple name is a declared def), with the declared-name universe and
    # (H) the set of cleanly-parsed files so the cgr side is held to the same files
    # (H) and names. scalameta silently skips files it cannot parse (e.g. Scala 3
    # (H) syntax), so grading only covered files keeps those out of both sides.
    # (H) Mirrors run_cpp_call_oracle.
    payload = _run_scala_oracle_payload(target)
    declared = frozenset(
        rec[ec.ORACLE_KEY_NAME]
        for rec in payload.get(ec.ORACLE_KEY_NODES, [])
        if rec.get(ec.ORACLE_KEY_KIND) in _CALLABLE_KINDS
    )
    covered = frozenset(
        rel for rel in payload.get(ec.ORACLE_KEY_COVERED, []) if not is_ignored(rel)
    )
    edges = {
        (call[ec.ORACLE_KEY_FILE], call[ec.ORACLE_KEY_NAME])
        for call in payload.get(ec.ORACLE_KEY_CALLS, [])
        if call[ec.ORACLE_KEY_NAME] in declared and call[ec.ORACLE_KEY_FILE] in covered
    }
    return edges, declared, covered
