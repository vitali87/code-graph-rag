# A graph is a function of (source files, parser code, parser config). The
# incremental hash cache keys only the source files, so a parser or config
# change with unchanged sources leaves stale old-parser edges. This
# fingerprint keys the other inputs: parse-relevant source files, the pinned
# grammar wheel versions, and the frontend settings, so a sync can detect the
# graph was built by a different parser or frontend config.
import hashlib
from importlib import metadata
from pathlib import Path

from loguru import logger

from . import constants as cs
from . import logs as ls


def compute_parser_fingerprint(
    package_root: Path | None = None, repo_path: Path | None = None
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
    return hasher.hexdigest()


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
        digest = hashlib.md5(database.read_bytes(), usedforsecurity=False).hexdigest()
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
