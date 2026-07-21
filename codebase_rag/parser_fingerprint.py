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


def compute_parser_fingerprint(package_root: Path | None = None) -> str:
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
    for entry in _frontend_settings():
        hasher.update(entry.encode())
    return hasher.hexdigest()


def _frontend_settings() -> list[str]:
    # The C# entry records the RESOLVED mode, not the setting: under AUTO a
    # graph built with dotnet present carries hybrid edges and one without
    # does not, so the two must not share a fingerprint. Imported lazily to
    # keep this module free of the parsers package at import time.
    from .parsers.csharp_frontend import resolve_csharp_frontend

    return [
        f"CPP_FRONTEND={settings.CPP_FRONTEND.value}",
        f"CSHARP_FRONTEND={resolve_csharp_frontend().value}",
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
    # The bundled Roslyn frontend tool (.cs/.csproj) is parser code though not
    # Python; an edit changes the semantic edges produced, so a tool change
    # must trip the staleness warning.
    tool_dir = root / cs.PARSER_FINGERPRINT_TOOL_DIR
    for pattern in cs.PARSER_FINGERPRINT_TOOL_GLOBS:
        sources.extend(path for path in tool_dir.glob(pattern) if path.is_file())
    return sorted(sources)


def _grammar_versions() -> list[str]:
    return sorted(
        cs.GRAMMAR_VERSION_FMT.format(name=dist.name.lower(), version=dist.version)
        for dist in metadata.distributions()
        if dist.name and dist.name.lower().startswith(cs.GRAMMAR_DIST_PREFIX)
    )
