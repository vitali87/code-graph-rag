# (H) A graph is a function of (source files, parser code, parser config). The
# (H) incremental hash cache keys only the source files, so a parser or config
# (H) change with unchanged sources leaves stale old-parser edges in the graph.
# (H) This fingerprint keys the other inputs: it hashes every parse-relevant
# (H) source file of the installed package, the pinned grammar wheel versions,
# (H) and the active frontend settings, so a sync can detect that the graph was
# (H) built by a different parser or frontend configuration.
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
    # (H) The active frontend selection changes which edges are produced for
    # (H) unchanged sources (e.g. enabling the C# Roslyn hybrid rewrites
    # (H) INHERITS/IMPLEMENTS), so it is part of the parser identity and must
    # (H) trip the staleness warning when it changes.
    for entry in _frontend_settings():
        hasher.update(entry.encode())
    return hasher.hexdigest()


def _frontend_settings() -> list[str]:
    return [
        f"CPP_FRONTEND={settings.CPP_FRONTEND.value}",
        f"CSHARP_FRONTEND={settings.CSHARP_FRONTEND.value}",
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
    return sorted(sources)


def _grammar_versions() -> list[str]:
    return sorted(
        cs.GRAMMAR_VERSION_FMT.format(name=dist.name.lower(), version=dist.version)
        for dist in metadata.distributions()
        if dist.name and dist.name.lower().startswith(cs.GRAMMAR_DIST_PREFIX)
    )
