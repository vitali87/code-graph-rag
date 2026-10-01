"""Where the tracer agents the dynamic-tracing guide uses are installed.

The C/C++ shim is compiled into the traced program, the Lua module is loaded
by it, and the Dart collector and JVM agent are built from source; none of
them runs inside cgr. They ship as package data so an installed cgr has them,
and `cgr trace agent <language>` prints where (issue #2684).
"""

from __future__ import annotations

from pathlib import Path

from .. import constants as cs

_HERE = Path(__file__).resolve().parent
_DART = _HERE / "dart_collector"
_JVM = _HERE / "jvm_agent"

# What `cgr trace agent` prints: the file a build or `LUA_PATH` names, or the
# directory a build runs in.
AGENT_PATHS: dict[cs.TraceAgent, Path] = {
    cs.TraceAgent.C: _HERE / "c_agent" / "cgr_trace_shim.c",
    cs.TraceAgent.LUA: _HERE / "lua_agent" / "cgr_trace.lua",
    cs.TraceAgent.DART: _DART,
    cs.TraceAgent.JVM: _JVM,
}

# Every file the agents consist of, for the packaging check.
AGENT_FILES: tuple[Path, ...] = (
    AGENT_PATHS[cs.TraceAgent.C],
    AGENT_PATHS[cs.TraceAgent.LUA],
    _DART / "pubspec.yaml",
    _DART / "pubspec.lock",
    *sorted((_DART / "bin").glob("*.dart")),
    _JVM / "MANIFEST.MF",
    *sorted((_JVM / "src" / "cgr" / "trace").glob("*.java")),
)
