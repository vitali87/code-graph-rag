from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from filelock import FileLock

from codebase_rag import constants as cs
from codebase_rag.parsers.build_lock import toolchain_runs

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import is_ignored, payload_to_graph

_ORACLE_DIR = Path(__file__).parent / ec.JAVA_ORACLE_DIRNAME
_SOURCE = _ORACLE_DIR / ec.JAVA_ORACLE_SOURCE
_CLASS = _ORACLE_DIR / f"{ec.JAVA_ORACLE_CLASS}.class"
_CALLABLE_KINDS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})

# A real `-version` probe answers in well under a second; the bound exists so a
# wedged shim can never stall pytest collection through a skip condition.
_PROBE_TIMEOUT_SECONDS = 10.0


def _toolchain_runs(bin_name: str) -> bool:
    # The frontend owns the probe (a which-only check reports the macOS no-JDK
    # shims as a working toolchain); the oracle reuses it so both skip on the
    # same evidence.
    return toolchain_runs(bin_name, _PROBE_TIMEOUT_SECONDS)


def java_available() -> bool:
    return _toolchain_runs(ec.JAVAC_BIN) and _toolchain_runs(ec.JAVA_BIN)


def _class_is_fresh() -> bool:
    return _CLASS.is_file() and _CLASS.stat().st_mtime >= _SOURCE.stat().st_mtime


def _ensure_compiled() -> None:
    # Recompile when the class is missing or older than the source, so an
    # edited Oracle.java is never shadowed by a stale (gitignored) .class.
    # The freshness test is a check-then-act and javac writes the class in
    # place, so parallel `pytest -n auto` workers would otherwise exec the JVM
    # against a class file another worker is mid-write (issue #1366). Lock the
    # build and re-test inside it, as the Rust and C# oracles already do; every
    # path that runs the JVM comes through here, so a caller that waits on the
    # lock only proceeds once the class is complete.
    if _class_is_fresh():
        return
    javac = shutil.which(ec.JAVAC_BIN)
    if javac is None:
        return
    with FileLock(str(_ORACLE_DIR / ec.JAVA_ORACLE_BUILD_LOCK)):
        if _class_is_fresh():
            return
        subprocess.run(
            [javac, str(_SOURCE)],
            cwd=str(_ORACLE_DIR),
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
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
        encoding=cs.ENCODING_UTF8,
        check=True,
    )
    payload: OraclePayload = json.loads(proc.stdout or "{}")
    return payload


def run_java_oracle(target: Path) -> GraphData:
    return payload_to_graph(_run_java_oracle_payload(target))


def run_java_call_oracle(target: Path) -> tuple[set[tuple[str, str]], frozenset[str]]:
    # File-level Java call sites restricted to first-party callees (simple name
    # is a declared Function/Method), with the declared name universe so the cgr
    # side is held to the same set. Mirrors run_go_call_oracle / run_rust_call_oracle.
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
