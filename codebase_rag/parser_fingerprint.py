# A graph is a function of (source files, parser code, parser config). The
# incremental hash cache keys only the source files, so a parser or config
# change with unchanged sources leaves stale old-parser edges. This
# fingerprint keys the other inputs: parse-relevant source files, the pinned
# grammar wheel versions, and the frontend settings, so a sync can detect the
# graph was built by a different parser or frontend config.
import hashlib
import shutil
import subprocess
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from . import constants as cs
from . import logs as ls

if TYPE_CHECKING:
    from .capture import CaptureSelection

_FILE_HASH_CHUNK_SIZE = 1024 * 1024

# External toolchains whose VERSION changes what a frontend extracts, as
# (fingerprint key, executable, version argument). The resolved frontend mode
# already recorded in `_frontend_settings` says which frontend ran; this says
# what it knew. A Go release with better inference, a Roslyn update resolving
# an overload differently, or a javac that models a new language feature all
# change the emitted edges while the mode string is identical (issue #1465).
#
# `LOMBOK=` in `_frontend_settings` is the same idea applied to one tool, and
# is left where it is rather than moved here: it is read from a jar rather
# than probed by running anything.
_VERSIONED_TOOLS: tuple[tuple[str, str, str], ...] = (
    ("GO_VERSION", "go", "version"),
    ("JAVAC_VERSION", "javac", "-version"),
    ("DOTNET_VERSION", "dotnet", "--version"),
)

# Bounded because this runs on every index. A tool that cannot be probed
# within it cannot be doing useful work either, so the timeout and a missing
# binary reach the same answer.
_VERSION_PROBE_TIMEOUT = 10.0

# A toolchain that DISAPPEARED changes extraction as much as one that moved,
# so absence is recorded rather than omitted -- and it must not collide with
# any real version string.
_TOOL_ABSENT = "absent"

# Recorded when the tool's frontend is resolved to a mode that cannot use it.
# Distinct from `absent`: a tool that is installed but unused and one that is
# missing entirely are different states, and switching a frontend off changes
# what is extracted just as much as removing its toolchain.
_TOOL_INACTIVE = "inactive"


def _digest_file(path: Path) -> str:
    # compile_commands.json is repository-controlled and can be very large,
    # so stream it in bounded chunks: reading it whole lets an oversized
    # database exhaust memory and fail indexing (issue #1177 review).
    hasher = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while chunk := stream.read(_FILE_HASH_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def _probe_version(executable: str, argument: str, cwd: Path | None = None) -> str:
    """One tool's version string, or `absent` when it cannot be determined.

    Never raises: the fingerprint is computed on every index, so a probe that
    escaped would turn a missing or wedged toolchain into a failed run. Some
    tools print their version on stderr (javac before 9), so both streams are
    considered.

    Run from `cwd` when one is given, because Go and .NET SELECT a toolchain
    from the working directory: a `go.work` or `global.json` can pin a
    different version, so the same command answers differently depending on
    where it runs. Probing from the caller's directory would record a
    toolchain the indexed repository never uses. `None` inherits the caller's
    cwd rather than inventing one.
    """
    binary = shutil.which(executable)
    if binary is None:
        return _TOOL_ABSENT
    try:
        proc = subprocess.run(
            [binary, argument],
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
            timeout=_VERSION_PROBE_TIMEOUT,
            cwd=None if cwd is None else str(cwd),
        )
    except (subprocess.SubprocessError, OSError):
        return _TOOL_ABSENT
    if proc.returncode != 0:
        return _TOOL_ABSENT
    output = (proc.stdout or proc.stderr or "").strip()
    # First line only: `dotnet --version` is one line, but `go version` and
    # `javac -version` can carry extra lines on some installs, and a
    # multi-line value would make the fingerprint sensitive to noise.
    return output.splitlines()[0].strip() if output else _TOOL_ABSENT


def _active_tools(repo_path: Path | None = None) -> dict[str, bool]:
    """Whether each tool can participate in extraction for THIS repository.

    Two gates, and both are needed. The resolved frontend mode says the
    frontend could run at all; the repository markers say it can run *here*.
    Go resolves to `gotypes` whenever a go toolchain exists, so without the
    second gate a Go upgrade invalidates the graph of a Python-only repo it
    can extract nothing from -- false staleness, which costs more than the
    drift the versions were added to catch (issue #1465 review).

    A repo-less call keeps mode gating only, matching `_repo_frontend_inputs`:
    those callers have no repository to ask, and reporting everything inactive
    would make their fingerprint disagree with a repo-scoped one for the same
    toolchain.

    Imported lazily, as `_frontend_settings` does, to keep this module free of
    the parsers package at import time.
    """
    from .parsers.csharp_frontend import find_csharp_project, resolve_csharp_frontend
    from .parsers.go_frontend import find_go_module, resolve_go_frontend
    from .parsers.java_frontend import resolve_java_frontend

    def _in_repo(probe: Callable[[Path], object | None]) -> bool:
        # No repository to ask: fall back to mode gating alone rather than
        # declaring the tool inactive. A probe that raises on a malformed
        # tree must not abort a fingerprint computed on every index.
        if repo_path is None:
            return True
        try:
            return probe(repo_path) is not None
        except OSError:
            return True

    # The probes below are DELIBERATELY the same predicates the frontends use
    # in their own `applies()` (`frontends/go.py`, `frontends/csharp.py`), so
    # "tool active" means exactly "this frontend will run against this repo".
    # That identity is the point, not a coincidence: a fingerprint describes
    # what produced the graph, so any divergence from the thing that produced
    # it is a bug by construction.
    #
    # Do NOT tighten one side alone. Requiring matching source files here --
    # a `go.mod` with no `.go` files, say -- looks safe and is the inverse
    # defect: the frontend still runs, so a toolchain upgrade before someone
    # adds the first `.go` file would go unrecorded and that file would be
    # indexed by a compiler the fingerprint does not name. False staleness
    # costs a re-index; missed staleness costs a silently wrong graph.
    #
    # The source-file question is already answered a layer up: the updater
    # gates on the files it actually parsed, which is why `applies()` only
    # asks about project markers (see the comment in `frontends/python.py`).
    return {
        "GO_VERSION": (
            resolve_go_frontend() is not cs.GoFrontend.TREESITTER
            and _in_repo(find_go_module)
        ),
        "JAVAC_VERSION": resolve_java_frontend() is not cs.JavaFrontend.HEURISTIC,
        "DOTNET_VERSION": (
            resolve_csharp_frontend() is not cs.CSharpFrontend.TREESITTER
            and _in_repo(find_csharp_project)
        ),
    }


def _tool_versions(repo_path: Path | None = None) -> list[str]:
    """`KEY=version` for each ACTIVE external toolchain, in a stable order.

    Stable order matters as much as the values: the entries are hashed in
    sequence, so a set or a dict iteration order would make the fingerprint
    change without the toolchain changing, which invalidates the cache always
    and costs more than never invalidating it.

    An inactive tool is recorded as `inactive` rather than omitted. Omitting
    it would make "tool absent" and "frontend disabled" hash identically, and
    those are different states: turning a frontend off changes what is
    extracted and must still trip the warning.
    """
    active = _active_tools(repo_path)
    entries: list[str] = []
    for key, executable, argument in _VERSIONED_TOOLS:
        value = (
            _probe_version(executable, argument, repo_path)
            if active.get(key)
            else _TOOL_INACTIVE
        )
        entries.append(f"{key}={value}")
    return entries


def compute_parser_fingerprint(
    package_root: Path | None = None,
    repo_path: Path | None = None,
    capture: "CaptureSelection | None" = None,
) -> str:
    root = package_root if package_root is not None else Path(__file__).resolve().parent
    hasher = hashlib.md5(usedforsecurity=False)
    for source in _fingerprint_sources(root):
        hasher.update(source.relative_to(root).as_posix().encode())
        hasher.update(source.read_bytes())
    for entry in _grammar_versions():
        hasher.update(entry.encode())
    for entry in _repo_frontend_inputs(repo_path):
        hasher.update(entry.encode())
    # The active frontend selection changes which edges are produced for
    # unchanged sources (e.g. the C# Roslyn hybrid rewrites
    # INHERITS/IMPLEMENTS), so it is part of the parser identity and must
    # trip the staleness warning.
    for entry in _frontend_settings():
        hasher.update(entry.encode())
    # The mode above says WHICH frontend ran; these say what it knew. A
    # compiler upgrade changes the emitted edges with the mode unchanged, so
    # without this an incremental run reuses a graph the older tool produced
    # (issue #1465).
    for entry in _tool_versions(repo_path):
        hasher.update(entry.encode())
    # The capture selection decides which edges exist for unchanged sources,
    # the same criterion the frontend mode above is hashed under: enabling a
    # group must not leave an "already in sync" graph that predates it, and
    # disabling one must not leave its edges behind (issue #1630).
    for entry in _capture_entries(capture):
        hasher.update(entry.encode())
    return hasher.hexdigest()


def _capture_entries(capture: "CaptureSelection | None") -> list[str]:
    """Stable identity of the resolved capture selection.

    The RESOLVED selection is hashed rather than the raw `CGR_CAPTURE` spec,
    so `io,io` and `io` are one identity and do not force a needless rebuild.
    Sorted because the selection holds frozensets, whose iteration order is
    not stable across processes and would otherwise make one selection hash
    many ways.
    """
    from .capture import default_capture
    from .config import settings

    selection = capture if capture is not None else default_capture()
    rels = sorted(rel.value for rel in selection.enabled_rels)
    labels = sorted(label.value for label in selection.enabled_node_labels)
    # Prefixed and counted so a relationship named like a node label cannot
    # move between the two lists without changing the digest.
    return [
        f"capture-rel:{len(rels)}",
        *rels,
        f"capture-node:{len(labels)}",
        *labels,
        # Not part of CaptureSelection, but the same kind of input: it picks
        # between two predicates for whether a nested definition becomes a
        # method, so it changes which Method nodes unchanged sources produce.
        f"capture-function-local-definitions:{settings.CAPTURE_FUNCTION_LOCAL_DEFINITIONS}",
    ]


def _repo_frontend_inputs(repo_path: Path | None) -> list[str]:
    # The selected compile database is as much a part of what the C++
    # semantic mode produces as libclang availability: generating one after a
    # tree-sitter-only index, editing its flags, or a different database
    # winning discovery all change the edges for unchanged sources, so the
    # entry records which database was selected and a digest of its content
    # (issue #1177 review). Only the updater passes a repo; repo-less calls
    # omit the entry consistently.
    if repo_path is None:
        return []
    from .parsers.cpp_frontend import find_compile_commands, resolve_cpp_frontend

    if resolve_cpp_frontend() is cs.CppFrontend.TREESITTER:
        return []
    compdb_dir = find_compile_commands(repo_path)
    if compdb_dir is None:
        return ["CPP_COMPDB=absent"]
    database = compdb_dir / "compile_commands.json"
    try:
        digest = _digest_file(database)
    except OSError:
        return ["CPP_COMPDB=absent"]
    return [f"CPP_COMPDB={database.as_posix()}:{digest}"]


def _frontend_settings() -> list[str]:
    # The C# and C++ entries record the RESOLVED mode, not the setting:
    # under AUTO/HYBRID a graph built with the toolchain present carries
    # hybrid edges and one without does not, so the two must not share a
    # fingerprint (dotnet for C#, libclang for C++, issue #1177). Imported lazily to
    # keep this module free of the parsers package at import time.
    from .parsers.cpp_frontend import resolve_cpp_frontend
    from .parsers.csharp_frontend import resolve_csharp_frontend
    from .parsers.go_frontend import resolve_go_frontend
    from .parsers.java_frontend import resolve_java_frontend
    from .parsers.java_lombok import current_lombok_version
    from .parsers.py_frontend import resolve_python_frontend

    return [
        f"CPP_FRONTEND={resolve_cpp_frontend().value}",
        f"CSHARP_FRONTEND={resolve_csharp_frontend().value}",
        f"GO_FRONTEND={resolve_go_frontend().value}",
        f"PYTHON_FRONTEND={resolve_python_frontend().value}",
        f"JAVA_FRONTEND={resolve_java_frontend().value}",
        f"LOMBOK={current_lombok_version() or 'absent'}",
    ]


def _fingerprint_sources(root: Path) -> list[Path]:
    sources: list[Path] = []
    for dirname in cs.PARSER_FINGERPRINT_SOURCE_DIRS:
        sources.extend(
            path for path in (root / dirname).rglob(cs.PY_SOURCE_GLOB) if path.is_file()
        )
    sources.extend(
        path
        for name in cs.PARSER_FINGERPRINT_SOURCE_FILES
        if (path := root / name).is_file()
    )
    # The bundled semantic-frontend tools (Roslyn .cs/.csproj, gotypes
    # .go/.mod/.sum) are parser code though not Python; an edit changes the
    # semantic edges produced, so a tool change must trip the staleness warning.
    for dirname, globs in cs.PARSER_FINGERPRINT_TOOL_SOURCES:
        tool_dir = root / dirname
        for pattern in globs:
            sources.extend(path for path in tool_dir.glob(pattern) if path.is_file())
    return sorted(sources)


def _grammar_entry(dist: metadata.Distribution) -> str | None:
    # Reading a distribution's metadata can raise, not just return None: a
    # half-written or malformed dist-info anywhere in site-packages makes
    # importlib_metadata fail inside the `name` property. That must not abort
    # the whole index run, so an unreadable distribution is skipped -- it can
    # only be one this fingerprint would have listed, and a MISSING grammar
    # entry changes the fingerprint, which already surfaces as the
    # stale-graph warning rather than a silent wrong answer (issue #1339).
    try:
        name = dist.name
        if not name or not name.lower().startswith(cs.GRAMMAR_DIST_PREFIX):
            return None
        return cs.GRAMMAR_VERSION_FMT.format(name=name.lower(), version=dist.version)
    except Exception:
        logger.debug(ls.FINGERPRINT_DIST_UNREADABLE)
        return None


def _grammar_versions() -> list[str]:
    return sorted(
        entry
        for dist in metadata.distributions()
        if (entry := _grammar_entry(dist)) is not None
    )
