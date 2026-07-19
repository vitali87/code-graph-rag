# (H) Roslyn semantic frontend for C# hybrid mode (issue #738). Runs a bundled net
# (H) console tool (`roslyn/`) that loads the target repo's real .csproj/.sln via
# (H) MSBuildWorkspace and emits location-keyed semantic facts the tree-sitter
# (H) heuristics cannot derive: per-type base classifications (INHERITS vs
# (H) IMPLEMENTS), per-invocation exact call targets (overloads by argument
# (H) types, extension methods via the reduced form), partial-type declaration
# (H) groups (exact symbol identity across files), and LINQ query-operator
# (H) calls (query syntax has no invocation nodes). Every join key that misses
# (H) falls back to the tree-sitter heuristics, and no dotnet, no project, or a
# (H) build/restore failure all leave the facts empty, so indexing stays pure
# (H) tree-sitter.
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from defusedxml import ElementTree
from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...config import settings

# (H) Base-classification join key: (rel_file, type_start_line) -> {base_simple_name: kind}.
BaseKindMap = dict[tuple[str, int], dict[str, str]]

# (H) Call-site join key: (rel_file, name_token_line, name_token_col, simple_name).
# (H) The NAME token, not the expression start: nested invocations
# (H) (`Make().Handle(x)` wraps `Make()`) share a start position, but their name
# (H) tokens never collide. Columns are BYTE offsets on both sides: the tool
# (H) re-measures Roslyn's UTF-16 columns in UTF-8 bytes to match tree-sitter.
CallSiteKey = tuple[str, int, int, str]


class CSharpCallSite(NamedTuple):
    """Resolved target of one invocation: the declaration cgr ingested."""

    name: str
    target_file: str
    target_line: int
    target_col: int


class CSharpQueryCall(NamedTuple):
    """One LINQ query operator call, keyed on the enclosing member's decl."""

    caller_file: str
    caller_line: int
    caller_col: int
    target_file: str
    target_line: int
    target_col: int


class CSharpSemanticFacts(NamedTuple):
    """Everything one Roslyn frontend run learned about the repo."""

    base_kinds: BaseKindMap
    call_sites: dict[CallSiteKey, CSharpCallSite]
    partial_groups: list[list[tuple[str, int]]]
    query_calls: list[CSharpQueryCall]
    # (H) Sites Roslyn resolved to a METADATA method (no first-party
    # (H) declaration): the compiler proof that the call leaves the repo, so
    # (H) the name-trie fallback must not fabricate a first-party edge there.
    external_sites: set[CallSiteKey]


def _empty_facts() -> CSharpSemanticFacts:
    # (H) A fresh instance per failure path: the maps are handed to mutable
    # (H) processor state, so a shared constant would alias across runs.
    return CSharpSemanticFacts({}, {}, [], [], set())


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


def resolve_csharp_frontend() -> cs.CSharpFrontend:
    # (H) The single source of truth for the EFFECTIVE frontend: without a
    # (H) dotnet toolchain every Roslyn-backed mode (AUTO, and an explicit
    # (H) HYBRID/ROSLYN, which the graph build degrades to tree-sitter with a
    # (H) warning) resolves to TREESITTER; with one, AUTO means HYBRID. The
    # (H) parser fingerprint resolves through here so a graph's recorded
    # (H) identity always matches the frontend that actually ran.
    mode = settings.CSHARP_FRONTEND
    if mode == cs.CSharpFrontend.TREESITTER:
        return mode
    if not csharp_frontend_available():
        return cs.CSharpFrontend.TREESITTER
    if mode == cs.CSharpFrontend.AUTO:
        return cs.CSharpFrontend.HYBRID
    return mode


def _project_candidates(repo_path: Path) -> list[Path]:
    def not_ignored(p: Path) -> bool:
        return not any(part in _IGNORE_DIRS for part in p.relative_to(repo_path).parts)

    def shortest_first(pattern: str) -> list[Path]:
        return sorted(
            (p for p in repo_path.rglob(pattern) if not_ignored(p)),
            key=lambda p: len(str(p)),
        )

    # (H) Both solution formats count: repos migrated to the XML format ship a
    # (H) .slnx and no .sln (e.g. Polly), and missing it degrades the whole run
    # (H) to the facts of one fallback csproj.
    for pattern in ("*.sln", "*.slnx", "*.csproj"):
        if candidates := shortest_first(pattern):
            return candidates
    return []


def find_csharp_project(repo_path: Path) -> Path | None:
    candidates = _project_candidates(repo_path)
    return candidates[0] if candidates else None


# (H) Matches the project path (second quoted field) of a classic .sln entry:
# (H) Project("{type-guid}") = "Name", "rel\path.csproj", "{project-guid}".
_SLN_PROJECT_RE = re.compile(r'^Project\("[^"]*"\)\s*=\s*"[^"]*",\s*"([^"]+)"', re.M)


def _solution_member_projects(project: Path) -> set[Path]:
    # (H) The set of .csproj files a solution covers, resolved absolute. A bare
    # (H) .csproj input covers only itself.
    suffix = project.suffix.lower()
    base = project.parent
    if suffix == ".sln":
        text = project.read_text(encoding="utf-8", errors="replace")
        rels = [
            m.replace("\\", "/")
            for m in _SLN_PROJECT_RE.findall(text)
            if m.lower().endswith(".csproj")
        ]
        return {(base / rel).resolve() for rel in rels}
    if suffix == ".slnx":
        try:
            tree = ElementTree.parse(project)
        except (ElementTree.ParseError, OSError):
            return set()
        return {
            (base / path.replace("\\", "/")).resolve()
            for element in tree.iter("Project")
            if (path := element.get("Path")) is not None
            and path.lower().endswith(".csproj")
        }
    return {project.resolve()}


def uncovered_csharp_projects(repo_path: Path, project: Path) -> list[Path]:
    # (H) Repos routinely keep bench/samples projects OUTSIDE the solution
    # (H) (Polly's bench/), so a solution-scoped workspace emits no facts for
    # (H) their files and every call in them degrades to tree-sitter
    # (H) heuristics. These uncovered projects load additively.
    members = _solution_member_projects(project)

    def not_ignored(p: Path) -> bool:
        return not any(part in _IGNORE_DIRS for part in p.relative_to(repo_path).parts)

    return sorted(
        p
        for p in repo_path.rglob("*.csproj")
        if not_ignored(p) and p.resolve() not in members
    )


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


def _parse_payload(stdout: str, stderr: str = "") -> CSharpSemanticFacts:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        # (H) No output at all: the tool crashed before printing its JSON line.
        logger.error(
            ls.CSHARP_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr)
        )
        return _empty_facts()
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        # (H) A decode failure means the tool emitted non-JSON after building;
        # (H) surface both streams so it can be debugged, not silently degraded.
        logger.error(
            ls.CSHARP_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr)
        )
        return _empty_facts()
    facts = CSharpSemanticFacts(
        base_kinds={
            (type_fact["file"], int(type_fact["line"])): _base_kinds(
                type_fact.get("bases", [])
            )
            for type_fact in payload.get("types", [])
        },
        call_sites={
            (
                site["file"],
                int(site["line"]),
                int(site["col"]),
                site["name"],
            ): CSharpCallSite(
                site["name"], site["tfile"], int(site["tline"]), int(site["tcol"])
            )
            for site in payload.get("calls", [])
        },
        partial_groups=[
            [(decl["file"], int(decl["line"])) for decl in group]
            for group in payload.get("partials", [])
        ],
        query_calls=[
            CSharpQueryCall(
                query["file"],
                int(query["line"]),
                int(query["col"]),
                query["tfile"],
                int(query["tline"]),
                int(query["tcol"]),
            )
            for query in payload.get("queries", [])
        ],
        external_sites={
            (site["file"], int(site["line"]), int(site["col"]), site["name"])
            for site in payload.get("externals", [])
        },
    )
    if not any(facts) and stderr.strip():
        # (H) A well-formed but entirely empty payload means the workspace load
        # (H) went wrong (SDK pin mismatch, unloadable solution) -- surface the
        # (H) tool's diagnostics instead of looking identical to success.
        logger.warning(ls.CSHARP_FRONTEND_NO_FACTS.format(stderr=stderr.strip()))
    return facts


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


def run_csharp_frontend(repo_path: Path) -> CSharpSemanticFacts:
    dotnet = shutil.which(_DOTNET)
    if dotnet is None:
        return _empty_facts()
    project = find_csharp_project(repo_path)
    if project is None:
        return _empty_facts()
    dll = _build_tool(dotnet)
    if dll is None:
        logger.warning(ls.CSHARP_FRONTEND_BUILD_FAILED)
        return _empty_facts()
    _restore(dotnet, project)
    uncovered = uncovered_csharp_projects(repo_path, project)
    for extra in uncovered:
        _restore(dotnet, extra)
    try:
        proc = subprocess.run(
            [dotnet, str(dll), str(repo_path), str(project), *map(str, uncovered)],
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
    except (subprocess.SubprocessError, OSError) as error:
        logger.warning(ls.CSHARP_FRONTEND_RUN_FAILED.format(error=error))
        return _empty_facts()
    return _parse_payload(proc.stdout, proc.stderr)
