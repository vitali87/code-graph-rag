# (H) A graph is a function of (source files, parser code). The incremental
# (H) hash cache keys only the source files, so a parser change with unchanged
# (H) sources leaves stale old-parser edges in the graph. This fingerprint
# (H) keys the second input: it hashes every parse-relevant source file of the
# (H) installed package plus the pinned grammar wheel versions, so a sync can
# (H) detect that the graph was built by a different parser.
import hashlib
from importlib import metadata
from pathlib import Path

from . import constants as cs


def compute_parser_fingerprint(package_root: Path | None = None) -> str:
    root = package_root if package_root is not None else Path(__file__).resolve().parent
    hasher = hashlib.md5(usedforsecurity=False)
    for source in _fingerprint_sources(root):
        hasher.update(source.relative_to(root).as_posix().encode())
        hasher.update(source.read_bytes())
    for entry in _grammar_versions():
        hasher.update(entry.encode())
    return hasher.hexdigest()


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
