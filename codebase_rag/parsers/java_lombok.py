"""Delombok overlay (issue #1140, tier 1).

Lombok annotations (@Getter, @Builder, @Data) expand to real API surface only
inside the compiler; the checked-in source never contains the generated
members, so calls into them dangle. When Lombok is in play and its jar is
locatable, `delombok` (Lombok's official source-to-source expander) runs into
a per-run scratch directory and the expanded BYTES are parsed in place of the
raw file -- keyed by the ORIGINAL path, so qualified names, containment, and
the hash cache (which must track the checked-in source) are untouched. Any
missing piece (no jar, no java, a failed run) degrades to parsing the raw
source exactly as before.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..config import settings
from ..utils.path_utils import is_enterable_dir, is_utf8_name
from .java_generated import _build_dirs

_LOMBOK_MARKER = "lombok"
_DELOMBOK_TIMEOUT = 300
_M2_LOMBOK_GLOB = "repository/org/projectlombok/lombok/*/lombok-*.jar"
_JAR_VERSION_RE = re.compile(r"lombok-(.+)\.jar$")


def _version_sort_key(jar: Path) -> tuple:
    # Numeric-aware: lombok-1.18.30 outranks lombok-1.18.9, which plain
    # lexicographic path sorting gets backwards.
    version = lombok_jar_version(jar)
    parts = []
    for piece in version.split("."):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def find_lombok_jar() -> Path | None:
    configured = settings.LOMBOK_JAR
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    m2 = Path.home() / ".m2"
    candidates = sorted(m2.glob(_M2_LOMBOK_GLOB), key=_version_sort_key)
    return candidates[-1] if candidates else None


def lombok_jar_version(jar: Path) -> str:
    match = _JAR_VERSION_RE.search(jar.name)
    return match.group(1) if match else jar.name


def _lombok_used(build_dir: Path) -> bool:
    for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
        build_file = build_dir / name
        try:
            if build_file.is_file() and _LOMBOK_MARKER in build_file.read_text(
                encoding=cs.ENCODING_UTF8, errors="replace"
            ):
                return True
        except OSError:
            continue
    return False


def _run_delombok(java: str, jar: Path, source_root: Path, out_dir: Path) -> bool:
    try:
        proc = subprocess.run(
            [
                java,
                "-jar",
                str(jar),
                "delombok",
                str(source_root),
                "-d",
                str(out_dir),
                "--onlyChanged",
            ],
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
            timeout=_DELOMBOK_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as error:
        logger.warning(ls.DELOMBOK_RUN_FAILED.format(error=error))
        return False
    if proc.returncode != 0:
        logger.warning(ls.DELOMBOK_RUN_FAILED.format(error=proc.stderr.strip()[:300]))
        return False
    return True


def _overlay_from_root(
    repo_path: Path, source_root: Path, out_dir: Path
) -> dict[str, bytes]:
    overlay: dict[str, bytes] = {}
    for expanded in out_dir.rglob(f"*{cs.EXT_JAVA}"):
        rel_in_root = expanded.relative_to(out_dir)
        original = source_root / rel_in_root
        key = original.relative_to(repo_path).as_posix()
        # The walk leaves out a name that is not UTF-8, so no parse asks for
        # its expansion, and `overlay_identity` cannot encode it (#3722).
        if not original.is_file() or not is_utf8_name(key):
            continue
        try:
            expanded_bytes = expanded.read_bytes()
            if expanded_bytes == original.read_bytes():
                # --onlyChanged should skip these, but an identical copy
                # would only waste overlay memory.
                continue
        except OSError:
            continue
        overlay[key] = expanded_bytes
    return overlay


def _delombok_roots(build_dir: Path) -> list[Path]:
    """The source roots of `build_dir` delombok expands: none unless its
    build file names Lombok."""
    if not _lombok_used(build_dir):
        return []
    return [
        root
        for parts in cs.JAVA_MAVEN_SOURCE_ROOTS
        # A source root cgr may not enter has nothing the walk indexes, and
        # `Path.is_dir` raised there on Python 3.12 and 3.13 (#3748).
        if is_enterable_dir(root := build_dir.joinpath(*parts))
    ]


def build_delombok_overlay(repo_path: Path) -> dict[str, bytes]:
    """Repo-relative path -> delomboked bytes for every Lombok-affected file;
    empty (raw parsing everywhere) unless Lombok, its jar, and java all line up."""
    repo_path = repo_path.resolve()
    java = shutil.which("java")
    if java is None:
        return {}
    jar = find_lombok_jar()
    if jar is None:
        return {}
    overlay: dict[str, bytes] = {}
    for build_dir in _build_dirs(repo_path):
        for source_root in _delombok_roots(build_dir):
            with tempfile.TemporaryDirectory(prefix="cgr-delombok-") as scratch:
                if _run_delombok(java, jar, source_root, Path(scratch)):
                    overlay.update(
                        _overlay_from_root(repo_path, source_root, Path(scratch))
                    )
    if overlay:
        logger.info(ls.DELOMBOK_OVERLAY_BUILT, count=len(overlay))
    return overlay


def may_parse_delomboked(repo_path: Path, rel_path: str) -> bool:
    """Whether the indexer may have parsed `rel_path` from its delombok
    expansion rather than from the file: a Java source under a root
    `build_delombok_overlay` expands.

    Answered from the repository alone. Whether java and a Lombok jar were
    at hand when the project was indexed is not known to a later reader, and
    what delombok makes of the file is not known without running it, so a
    file it would leave unchanged answers True as well (issue #3162).
    """
    file = repo_path / rel_path
    if file.suffix != cs.EXT_JAVA:
        return False
    return any(
        file.is_relative_to(root)
        for build_dir in file.parents
        if build_dir.is_relative_to(repo_path)
        for root in _delombok_roots(build_dir)
    )


def current_lombok_version() -> str:
    # Module-global lookup on purpose: tests and callers patch
    # find_lombok_jar on THIS module, and an import-by-value caller would
    # bypass the patch (and the configured jar) silently.
    jar = find_lombok_jar()
    return lombok_jar_version(jar) if jar is not None else ""


def current_lombok_identity() -> str:
    """Version plus a content digest: a same-named configured jar
    (/tools/lombok.jar) replaced in place must still flip the persisted
    state, or its unchanged expansions would never reparse."""
    jar = find_lombok_jar()
    if jar is None:
        return ""
    try:
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()[:16]
    except OSError:
        digest = "unreadable"
    return f"{lombok_jar_version(jar)}:{digest}"


def overlay_identity(overlay: dict[str, bytes]) -> str:
    """A stable digest of the overlay's effect: which files it covers and what
    it expands them to. Any change (jar appearing/vanishing, version bump,
    annotation edits changing the expansion) must force those files through a
    reparse, or the graph keeps stale generated members."""
    if not overlay:
        return ""
    digest = hashlib.sha256()
    for key in sorted(overlay):
        digest.update(key.encode(cs.ENCODING_UTF8))
        digest.update(hashlib.sha256(overlay[key]).digest())
    return digest.hexdigest()
