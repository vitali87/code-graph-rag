# A graph is a function of (source files, parser code, parser config). The
# incremental hash cache keys only the source files, so a parser or config
# change with unchanged sources leaves stale old-parser edges. This
# fingerprint keys the other inputs: parse-relevant source files, the pinned
# grammar wheel versions, and the frontend settings, so a sync can detect the
# graph was built by a different parser or frontend config.
import hashlib
from importlib import metadata
from pathlib import Path

from . import constants as cs
from .config import settings


def compute_parser_fingerprint(
    package_root: Path | None = None, *, repo_path: Path | None = None
) -> str:
    root = package_root if package_root is not None else Path(__file__).resolve().parent
    hasher = hashlib.md5(usedforsecurity=False)
    for source in _fingerprint_sources(root):
        hasher.update(source.relative_to(root).as_posix().encode())
        hasher.update(source.read_bytes())
    for entry in _grammar_versions():
        hasher.update(entry.encode())
    # The active frontend selection changes which edges are produced for
    # unchanged sources (e.g. the C# Roslyn hybrid rewrites
    # INHERITS/IMPLEMENTS), so it is part of the parser identity and must
    # trip the staleness warning.
    for entry in _frontend_settings(repo_path):
        hasher.update(entry.encode())
    return hasher.hexdigest()


def _frontend_settings(repo_path: Path | None) -> list[str]:
    # The C# entry records the RESOLVED mode, not the setting: under AUTO a
    # graph built with dotnet present carries hybrid edges and one without
    # does not, so the two must not share a fingerprint. Imported lazily to
    # keep this module free of the parsers package at import time.
    from .parsers.cpp_frontend import find_compile_commands, resolve_cpp_frontend
    from .parsers.csharp_frontend import resolve_csharp_frontend
    from .parsers.go_frontend import resolve_go_frontend

    cpp_frontend = resolve_cpp_frontend(repo_path)
    entries = [
        f"CPP_FRONTEND={settings.CPP_FRONTEND.value}",
        f"CPP_FRONTEND_RESOLVED={cpp_frontend.value}",
        f"CSHARP_FRONTEND={resolve_csharp_frontend().value}",
        f"GO_FRONTEND={resolve_go_frontend().value}",
    ]
    if repo_path is not None and cpp_frontend != cs.CppFrontend.TREESITTER:
        compdb_dir = find_compile_commands(repo_path)
        if compdb_dir is not None:
            compdb_path = (compdb_dir / "compile_commands.json").resolve()
            compdb_digest = hashlib.sha256(compdb_path.read_bytes()).hexdigest()
            entries.extend(
                [
                    f"CPP_COMPILE_COMMANDS_PATH={compdb_path.as_posix()}",
                    f"CPP_COMPILE_COMMANDS_SHA256={compdb_digest}",
                ]
            )
    return entries


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


def _grammar_versions() -> list[str]:
    return sorted(
        cs.GRAMMAR_VERSION_FMT.format(name=dist.name.lower(), version=dist.version)
        for dist in metadata.distributions()
        if dist.name and dist.name.lower().startswith(cs.GRAMMAR_DIST_PREFIX)
    )
