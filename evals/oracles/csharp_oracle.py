from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from functools import lru_cache
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
# Class names count as callables: `new T()` on a type with no explicit
# constructor is a real creation site with no ctor Method to carry the name
# (Python retrieval has the same shape). A C# method cannot share its
# enclosing type's name, so admitting Class names never collides with a type.
_CALLABLE_KINDS = frozenset(
    {cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value, cs.NodeLabel.CLASS.value}
)
_DOTNET_ENV = {ec.DOTNET_TELEMETRY_ENV: "1", ec.DOTNET_NOLOGO_ENV: "1"}


@lru_cache(maxsize=1)
def csharp_oracle_skip_reason() -> str | None:
    """Why the C# oracle cannot run here, or None when it can (issue #1639).

    "dotnet toolchain not installed" is FALSE on the reporting machine: dotnet
    IS installed there and the build fails with NETSDK1045 because the SDK is
    too old for the csproj. The issue asks for the captured stderr so the skip
    line names the real obstacle.
    """
    dotnet = shutil.which(ec.DOTNET_BIN)
    if dotnet is None:
        return ec.DOTNET_SKIP_NO_BINARY.format(binary=ec.DOTNET_BIN)
    try:
        probe = subprocess.run(
            [dotnet, ec.DOTNET_LIST_SDKS_FLAG],
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
            timeout=ec.DOTNET_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return str(e)
    if probe.returncode != 0:
        return ec.DOTNET_SKIP_NO_SDKS.format(stderr=(probe.stderr or "").strip())
    listed = [line for line in probe.stdout.splitlines() if line.strip()]
    if not any(_sdk_major(line) >= ec.CSHARP_ORACLE_MIN_SDK_MAJOR for line in listed):
        return ec.DOTNET_SKIP_SDK_TOO_OLD.format(
            needed=ec.CSHARP_ORACLE_MIN_SDK_MAJOR,
            found=", ".join(listed) or "none",
        )
    try:
        if not _ensure_built(dotnet):
            return ec.DOTNET_SKIP_BUILD_INCOMPLETE
    except subprocess.CalledProcessError as e:
        return ec.DOTNET_SKIP_BUILD_FAILED.format(
            stderr=((e.stderr or e.stdout or "").strip())[: ec.SKIP_REASON_STDERR_CHARS]
        )
    except subprocess.TimeoutExpired as e:
        return ec.DOTNET_SKIP_BUILD_TIMEOUT.format(seconds=e.timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return ec.DOTNET_SKIP_BUILD_FAILED.format(stderr=str(e))
    return None


def csharp_oracle_available() -> bool:
    """Whether the C# oracle can run here (issue #1639).

    Thin wrapper over `csharp_oracle_skip_reason`, which holds the reasoning
    and the caching, so the two can never disagree about availability and the
    build is probed once rather than once per caller.
    """
    return csharp_oracle_skip_reason() is None


def _sdk_major(sdk_line: str) -> int:
    """Major version from a `dotnet --list-sdks` line, or 0 if unreadable.

    Lines look like `10.0.400 [/usr/share/dotnet/sdk]`. A line this cannot
    parse must not count as a usable SDK, hence 0 rather than a raise: the
    caller is a skip guard, and failing it closed costs a skip while failing
    it open costs the hard failure this exists to prevent.
    """
    head = sdk_line.strip().split(ec.DOTNET_SDK_LINE_SEP, 1)[0]
    major = head.split(".", 1)[0]
    return int(major) if major.isdigit() else 0


def _dll_fresh() -> bool:
    return _DLL.is_file() and _DLL.stat().st_mtime >= _SOURCE.stat().st_mtime


def _ensure_built(dotnet: str) -> bool:
    # Build the oracle assembly ONCE, then invocations run the DLL read-only,
    # so parallel pytest-xdist workers never race on a shared MSBuild output.
    # The mkdir lock serialises the one build; a rebuild triggers only when the
    # DLL is missing or older than the source. Same as _common.ensure_node_deps.
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
                encoding=cs.ENCODING_UTF8,
                check=True,
                # Bounded: an unreachable NuGet feed makes `dotnet build`
                # block indefinitely, and the guard calls this, so a stalled
                # restore would hang the whole run instead of skipping it.
                timeout=ec.DOTNET_BUILD_TIMEOUT_S,
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
        encoding=cs.ENCODING_UTF8,
        check=True,
        env={
            **os.environ,
            **_DOTNET_ENV,
            # Hand cgr's full ignore set to the oracle so its file walk (and the
            # declared-type universe it builds) matches what cgr indexes. Otherwise
            # types under an ignored dir (build/, .venv/) could misclassify a real
            # file's inheritance edge.
            ec.CGR_IGNORE_DIRS_ENV: ",".join(sorted(cs.IGNORE_PATTERNS)),
        },
    )
    # The program prints one JSON line; take the last non-empty stdout line so a
    # stray runtime notice printed before it cannot corrupt the parse. Surface
    # both streams on a decode failure so a broken build/run is not reduced to a
    # context-free JSONDecodeError.
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
    # File-level C# call sites restricted to first-party callees (simple name is
    # a declared Function/Method), with the declared name universe so the cgr
    # side is held to the same set. Mirrors run_go_call_oracle / run_java_call_oracle.
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
