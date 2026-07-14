# (H) Roslyn semantic frontend for C# hybrid mode (issue #738). Runs a bundled net
# (H) console tool (`roslyn/`) that loads the target repo's real .csproj/.sln via
# (H) MSBuildWorkspace and emits, per type declaration keyed on (rel_file,
# (H) start_line) matching cgr's tree-sitter node span, each base type classified
# (H) class/interface by the resolved symbol. The tree-sitter C# split
# (H) (split_csharp_bases) consults these and falls back to its I-prefix heuristic
# (H) for any base the semantic model could not resolve. Everything degrades
# (H) gracefully: no dotnet, no project, or a build/restore failure all leave the
# (H) map empty, so indexing stays pure tree-sitter.
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...config import settings

# (H) Base-classification join key: (rel_file, type_start_line) -> {base_simple_name: kind}.
BaseKindMap = dict[tuple[str, int], dict[str, str]]

_DOTNET = "dotnet"
_TOOL_SRC = Path(__file__).parent / "roslyn"
_TOOL_SOURCES = ("Frontend.csproj", "Program.cs", "Frontend.cs")
_DLL_NAME = "Frontend.dll"
_BUILD_LOCK = ".build-lock"
# (H) Same mkdir-lock discipline as the eval oracle: build the assembly ONCE, then
# (H) parallel workers run the DLL read-only and never race a shared MSBuild output.
_LOCK_TRIES = 600
_LOCK_POLL_SECONDS = 0.5
_RESTORE_TIMEOUT = 600
_RUN_TIMEOUT = 900
_DOTNET_ENV = {"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"}
_IGNORE_DIRS = frozenset({"bin", "obj", ".git", "node_modules", "vendor", "packages"})


def csharp_frontend_available() -> bool:
    return shutil.which(_DOTNET) is not None


def _project_candidates(repo_path: Path) -> list[Path]:
    def not_ignored(p: Path) -> bool:
        return not any(part in _IGNORE_DIRS for part in p.relative_to(repo_path).parts)

    slns = sorted(
        (p for p in repo_path.rglob("*.sln") if not_ignored(p)),
        key=lambda p: len(str(p)),
    )
    if slns:
        return slns
    return sorted(
        (p for p in repo_path.rglob("*.csproj") if not_ignored(p)),
        key=lambda p: len(str(p)),
    )


def find_csharp_project(repo_path: Path) -> Path | None:
    candidates = _project_candidates(repo_path)
    return candidates[0] if candidates else None


def _cache_dir() -> Path:
    return settings.CGR_HOME.expanduser() / "csharp_roslyn"


def _newest_source_mtime() -> float:
    return max((_TOOL_SRC / name).stat().st_mtime for name in _TOOL_SOURCES)


def _dll_fresh(dll: Path) -> bool:
    return dll.is_file() and dll.stat().st_mtime >= _newest_source_mtime()


def _acquire_build_lock(lock: Path, dll: Path) -> bool:
    # (H) Serialise the one build across parallel workers. Returns True holding the
    # (H) lock (caller must rmdir); False if it gave up because another worker
    # (H) already produced a fresh DLL or the tries ran out.
    for _ in range(_LOCK_TRIES):
        try:
            lock.mkdir()
            return True
        except FileExistsError:
            time.sleep(_LOCK_POLL_SECONDS)
            if _dll_fresh(dll):
                return False
    return False


def _compile_tool(dotnet: str, src: Path, out: Path) -> bool:
    src.mkdir(parents=True, exist_ok=True)
    for name in _TOOL_SOURCES:
        shutil.copy2(_TOOL_SRC / name, src / name)
    proc = subprocess.run(
        [
            dotnet,
            "build",
            str(src),
            "-c",
            "Release",
            "-o",
            str(out),
            "--verbosity",
            "quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **_DOTNET_ENV},
    )
    return proc.returncode == 0


def _build_tool(dotnet: str) -> Path | None:
    # (H) Build from a copy in the writable cache, never the bundled source dir,
    # (H) which is read-only under a pip install (obj/ intermediates would fail).
    cache = _cache_dir()
    src = cache / "src"
    out = cache / "out"
    dll = out / _DLL_NAME
    if _dll_fresh(dll):
        return dll
    cache.mkdir(parents=True, exist_ok=True)
    lock = cache / _BUILD_LOCK
    if not _acquire_build_lock(lock, dll):
        return dll if _dll_fresh(dll) else None
    try:
        if not _dll_fresh(dll) and not _compile_tool(dotnet, src, out):
            return None
    finally:
        lock.rmdir()
    return dll if _dll_fresh(dll) else None


def _restore(dotnet: str, project: Path) -> None:
    # (H) Best-effort: MSBuildWorkspace needs project.assets.json to resolve NuGet +
    # (H) framework references. A restore failure (offline, private feed) just leaves
    # (H) unresolved bases as kind "unknown" -> the Python heuristic handles those.
    try:
        subprocess.run(
            [dotnet, "restore", str(project), "--verbosity", "quiet"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_RESTORE_TIMEOUT,
            env={**os.environ, **_DOTNET_ENV},
        )
    except (subprocess.SubprocessError, OSError):
        return


def _parse_payload(stdout: str, stderr: str = "") -> BaseKindMap:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        # (H) No output at all: the tool crashed before printing its JSON line.
        logger.error(
            ls.CSHARP_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr)
        )
        return {}
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        # (H) A decode failure means the tool emitted non-JSON after building;
        # (H) surface both streams so it can be debugged, not silently degraded.
        logger.error(
            ls.CSHARP_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr)
        )
        return {}
    return {
        (type_fact["file"], int(type_fact["line"])): _base_kinds(
            type_fact.get("bases", [])
        )
        for type_fact in payload.get("types", [])
    }


def _base_kinds(bases: list[dict[str, str]]) -> dict[str, str]:
    # (H) Fold one type's bases to {simple_name: kind}. Two bases sharing a simple
    # (H) name but differing in kind (e.g. `: A.Widget, B.Widget`, one class + one
    # (H) interface) cannot be told apart by simple name on either side, so the
    # (H) name is dropped and split_csharp_bases falls back to the heuristic rather
    # (H) than letting the last-written kind silently win.
    kinds: dict[str, str] = {}
    conflicting: set[str] = set()
    for base in bases:
        name, kind = base["name"], base["kind"]
        if name in kinds and kinds[name] != kind:
            conflicting.add(name)
        else:
            kinds.setdefault(name, kind)
    for name in conflicting:
        kinds.pop(name, None)
    return kinds


def run_csharp_frontend(repo_path: Path) -> BaseKindMap:
    dotnet = shutil.which(_DOTNET)
    if dotnet is None:
        return {}
    project = find_csharp_project(repo_path)
    if project is None:
        return {}
    dll = _build_tool(dotnet)
    if dll is None:
        return {}
    _restore(dotnet, project)
    try:
        proc = subprocess.run(
            [dotnet, str(dll), str(repo_path), str(project)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_RUN_TIMEOUT,
            env={
                **os.environ,
                **_DOTNET_ENV,
                "CGR_IGNORE_DIRS": ",".join(sorted(cs.IGNORE_PATTERNS)),
            },
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    return _parse_payload(proc.stdout, proc.stderr)
