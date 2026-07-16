from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import is_ignored, payload_to_graph

_ORACLE_DIR = Path(__file__).parent / ec.CSHARP_ORACLE_DIRNAME
_SOURCE = _ORACLE_DIR / ec.CSHARP_ORACLE_SOURCE
_BUILD_DIR = _ORACLE_DIR / ec.CSHARP_ORACLE_BUILD_DIRNAME
_DLL = _BUILD_DIR / ec.CSHARP_ORACLE_DLL
_LOCK = _ORACLE_DIR / ec.CSHARP_ORACLE_BUILD_LOCK
# (H) Class names count as callables: `new T()` on a type with no explicit
# (H) constructor is a real creation site with no ctor Method to carry the name
# (H) (Python's retrieval has the same shape -- a class IS a callable there).
# (H) A C# method cannot share its enclosing type's name, so admitting Class
# (H) names never lets a plain invocation collide with a type.
_CALLABLE_KINDS = frozenset(
    {cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value, cs.NodeLabel.CLASS.value}
)
_DOTNET_ENV = {ec.DOTNET_TELEMETRY_ENV: "1", ec.DOTNET_NOLOGO_ENV: "1"}


def csharp_oracle_available() -> bool:
    return shutil.which(ec.DOTNET_BIN) is not None


def _dll_fresh() -> bool:
    return _DLL.is_file() and _DLL.stat().st_mtime >= _SOURCE.stat().st_mtime


def _ensure_built(dotnet: str) -> bool:
    # (H) Build the oracle assembly ONCE, then invocations run the DLL read-only,
    # (H) so parallel pytest-xdist workers never race on a shared MSBuild output
    # (H) (which is what `dotnet run` per call would do). The mkdir lock serialises
    # (H) the one build; a rebuild is triggered only when the DLL is missing or
    # (H) older than the source. Same discipline as _common.ensure_node_deps.
    if _dll_fresh():
        return True
    for _ in range(ec.NODE_DEPS_LOCK_TRIES):
        try:
            _LOCK.mkdir()
            break
        except FileExistsError:
            time.sleep(ec.NODE_DEPS_LOCK_POLL_SECONDS)
            if _dll_fresh():
                return True
    else:
        return _dll_fresh()
    try:
        if not _dll_fresh():
            subprocess.run(
                [
                    dotnet,
                    ec.DOTNET_BUILD,
                    str(_ORACLE_DIR),
                    ec.DOTNET_CONFIG_FLAG,
                    ec.DOTNET_CONFIG_RELEASE,
                    ec.DOTNET_OUTPUT_FLAG,
                    str(_BUILD_DIR),
                    ec.DOTNET_VERBOSITY_FLAG,
                    ec.DOTNET_VERBOSITY_QUIET,
                ],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, **_DOTNET_ENV},
            )
    finally:
        _LOCK.rmdir()
    return _dll_fresh()


def _run_csharp_oracle_payload(target: Path) -> OraclePayload:
    dotnet = shutil.which(ec.DOTNET_BIN)
    if dotnet is None or not _ensure_built(dotnet):
        return OraclePayload(nodes=[], edges=[], name_edges=[])
    proc = subprocess.run(
        [dotnet, str(_DLL), str(target)],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            **_DOTNET_ENV,
            # (H) Hand cgr's full ignore set to the oracle so its file walk (and the
            # (H) declared-type universe it builds) matches what cgr indexes, not a
            # (H) smaller hardcoded subset -- otherwise types under an ignored dir
            # (H) (build/, .venv/) could misclassify a real file's inheritance edge.
            ec.CGR_IGNORE_DIRS_ENV: ",".join(sorted(cs.IGNORE_PATTERNS)),
        },
    )
    # (H) The program prints exactly one JSON line; take the last non-empty stdout
    # (H) line so any stray runtime notice printed before it cannot corrupt parse.
    # (H) Surface both streams on a decode failure so a broken build/run is not
    # (H) reduced to a context-free JSONDecodeError.
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        payload: OraclePayload = json.loads(lines[-1] if lines else "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            ec.CSHARP_ORACLE_PARSE_FAILED.format(
                error=exc, stdout=proc.stdout, stderr=proc.stderr
            )
        ) from exc
    return payload


def run_csharp_oracle(target: Path) -> GraphData:
    return payload_to_graph(_run_csharp_oracle_payload(target))


def run_csharp_call_oracle(target: Path) -> tuple[set[tuple[str, str]], frozenset[str]]:
    # (H) File-level C# call sites restricted to first-party callees (a callee
    # (H) whose simple name is a declared Function/Method), with the declared name
    # (H) universe so the cgr side can be held to the same set. Mirrors the Go and
    # (H) Java call oracles (run_go_call_oracle / run_java_call_oracle).
    payload = _run_csharp_oracle_payload(target)
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
