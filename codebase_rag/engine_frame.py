"""The Rust engine's frame (docs/reports/PHASE2_FRAME.md).

Phase 2 of the Rust rewrite moves the indexing work that does not depend on
a language into the extension: finding the files, hashing them, telling what
changed, the folder structure and the File nodes. It lands in steps behind
`ENABLED`, which stays off until the last one. Until then the indexer uses
Python's answers only, and tests hold each part to the Python code it
replaces.

The extension's `Engine` (rust/cgr_graph_rs/src/engine) runs its work on a
thread pool of its own, with the interpreter lock released, and returns
results in the order of its inputs whatever the number of threads. Its
`IgnoreRules` (src/engine/ignore.rs) are the walk's ignore rules, a port of
`utils/path_utils.py`'s matching and of pathspec's patterns.

The file plan is the walk's answer (src/engine/discover.rs): the files in
walk order with their hashes, the folders listed, and the entries reported.
`plan_for_run` is where a run takes it. With `ENABLED` on, Python's walk
still runs beside the frame's, and the run uses the frame's plan only when
the two agree: on any difference it logs one warning naming the first
differing lines and uses Python's. Tests put a strict `CHECK` in place of
that warning, which fails them on any difference.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from functools import cache
from pathlib import Path
from typing import NamedTuple, Protocol

from loguru import logger

from . import constants as cs
from . import logs as ls
from .canonical_graph import difference_in_order
from .embedded_graph.extension import rust_extension
from .utils.path_utils import readable_name, walk_eligible_files

# Whether the indexer uses the frame. Off until phase 2's last step; the
# parity tests compare the frame with the Python engine either way.
ENABLED: bool = False


class WalkedFile(NamedTuple):
    """A file the walk yields: `walk_eligible_files`' tuple."""

    dirpath: str
    name: str
    rel_path: str


class VisitedDir(NamedTuple):
    """A folder the walk lists: what its `on_dir` receives."""

    key: str
    dirpath: str


class FilePlan(NamedTuple):
    """What a repository walk finds.

    The files in walk order, the folders listed in the order they were
    listed, each entry reported as a link, a special file or a name that is
    not UTF-8 by its path from the root, and each file's md5, or None for a
    file that cannot be read. Python's walk hashes nothing: its plan has no
    hashes until `hashed` adds them.
    """

    files: tuple[WalkedFile, ...]
    dirs: tuple[VisitedDir, ...]
    symlinks: tuple[str, ...]
    special: tuple[str, ...]
    undecodable: tuple[str, ...]
    hashes: tuple[str | None, ...]


class IgnoreFiles(NamedTuple):
    """What the frame reads from the root's two ignore files.

    `exclude` and `unignore` are `config.load_ignore_patterns`' sets, the
    tracked-source rescue included. The rest is what Python logs while
    reading: each line left out as `(file name, line number)`, each file it
    could not read, and the folders the rescue keeps files in.
    """

    exclude: frozenset[str]
    unignore: frozenset[str]
    refused_lines: tuple[tuple[str, int], ...]
    unreadable: tuple[str, ...]
    tracked_dirs: tuple[str, ...]


type RawFilePlan = tuple[
    list[tuple[str, str, str]],
    list[tuple[str, str]],
    list[str],
    list[str],
    list[str],
    list[str | None],
]
type RawIgnoreFiles = tuple[
    list[str], list[str], list[tuple[str, int]], list[str], list[str]
]


class Engine(Protocol):
    @property
    def threads(self) -> int: ...

    def md5_files(self, paths: Sequence[Path]) -> list[str | None]: ...

    def file_plan(
        self,
        root: Path,
        exclude: Iterable[str] | None = None,
        unignore: Iterable[str] | None = None,
    ) -> RawFilePlan: ...

    def ignore_patterns(self, root: Path) -> RawIgnoreFiles: ...


class IgnoreRules(Protocol):
    """The ignore rules over one exclude set and one unignore set.

    Each method answers what the `utils/path_utils.py` function of its name
    answers when given those sets as `exclude_paths` and `unignore_paths`,
    and raises `ValueError` where that function raises on a pattern pathspec
    refuses. `should_skip_rel_path` is `should_skip_path` for the path's
    relative POSIX spelling, once it is known to resolve inside the root.
    """

    def matches_exclude(self, rel_path: str) -> bool: ...

    def matches_unignore(self, rel_path: str) -> bool: ...

    def should_keep_dir(self, dirname: str, dir_prefix: str) -> bool: ...

    def should_skip_rel_file(self, rel_path: str, dir_parts: Sequence[str]) -> bool: ...

    def should_skip_rel_path(self, rel_path: str, is_file: bool) -> bool: ...

    def unignore_names_this_file(self, rel_path: str) -> bool: ...

    def unignore_could_match_within(self, rel_dir: str) -> bool: ...

    def unignore_names_vcs_dir(self, rel_dir: str) -> bool: ...


class PlanCheck(Protocol):
    """What replaces the warning when the frame's plan and Python's differ.

    The tests' strict check (`codebase_rag/tests/engine_modes.py`) and the
    pinned repositories' (`scripts/handoff.py`): `compares` says whether this
    run can be compared at all, and `differs` receives the difference, as
    `-` lines of Python's plan and `+` lines of the frame's.
    """

    def compares(self) -> bool: ...

    def differs(self, repo_path: Path, difference: Sequence[str]) -> None: ...


# None in production. While a check is in place every run compares the two
# plans, with the frame on or off, and a run the check compares uses the
# frame's plan whenever the frame is on, even where the two differ.
CHECK: PlanCheck | None = None


def new_engine(threads: int = 0) -> Engine | None:
    """An engine on `threads` worker threads (0: one per core), or None when
    the extension is from before the frame."""
    engine_type = getattr(rust_extension(), cs.EG_RUST_ENGINE, None)
    return None if engine_type is None else engine_type(threads)


@cache
def engine() -> Engine | None:
    """The process's engine, one thread per core, made on first use."""
    return new_engine()


def new_ignore_rules(
    exclude: Iterable[str] | None = None, unignore: Iterable[str] | None = None
) -> IgnoreRules | None:
    """The frame's ignore rules for `exclude` and `unignore`, or None when the
    extension is from before them."""
    rules_type = getattr(rust_extension(), cs.EG_RUST_IGNORE_RULES, None)
    return None if rules_type is None else rules_type(exclude, unignore)


def frame_file_plan(
    repo_path: Path,
    exclude: Iterable[str] | None = None,
    unignore: Iterable[str] | None = None,
) -> FilePlan | None:
    """The frame's plan for the repository under these sets, as
    `walk_eligible_files(repo_path, exclude, unignore)` walks it, with every
    file hashed; None when the extension is from before the walk. Raises
    `ValueError` where Python's walk raises on a pattern."""
    made = engine()
    if made is None or not hasattr(made, cs.EG_RUST_FILE_PLAN):
        return None
    files, dirs, symlinks, special, undecodable, hashes = made.file_plan(
        repo_path, exclude, unignore
    )
    return FilePlan(
        files=tuple(WalkedFile(*file) for file in files),
        dirs=tuple(VisitedDir(*visited) for visited in dirs),
        symlinks=tuple(symlinks),
        special=tuple(special),
        undecodable=tuple(undecodable),
        hashes=tuple(hashes),
    )


def frame_ignore_patterns(repo_path: Path) -> IgnoreFiles | None:
    """What the frame reads from the repository's ignore files, or None when
    the extension is from before its reader."""
    made = engine()
    if made is None or not hasattr(made, cs.EG_RUST_IGNORE_PATTERNS):
        return None
    exclude, unignore, refused, unreadable, tracked = made.ignore_patterns(repo_path)
    return IgnoreFiles(
        exclude=frozenset(exclude),
        unignore=frozenset(unignore),
        refused_lines=tuple(refused),
        unreadable=tuple(unreadable),
        tracked_dirs=tuple(tracked),
    )


def python_file_plan(
    repo_path: Path,
    exclude: frozenset[str] | None = None,
    unignore: frozenset[str] | None = None,
    on_dir: Callable[[str, str], None] | None = None,
) -> FilePlan:
    """Python's walk of the repository as a plan, without hashes, handing
    each folder to `on_dir` as the walk lists it."""
    dirs: list[VisitedDir] = []
    symlinks: list[str] = []
    special: list[str] = []
    undecodable: list[str] = []

    def visited(key: str, dirpath: str) -> None:
        dirs.append(VisitedDir(key, dirpath))
        if on_dir is not None:
            on_dir(key, dirpath)

    files = tuple(
        WalkedFile(*walked)
        for walked in walk_eligible_files(
            repo_path,
            exclude,
            unignore,
            on_dir=visited,
            on_symlink=symlinks.append,
            on_special=special.append,
            on_undecodable=undecodable.append,
        )
    )
    return FilePlan(
        files=files,
        dirs=tuple(dirs),
        symlinks=tuple(symlinks),
        special=tuple(special),
        undecodable=tuple(undecodable),
        hashes=(),
    )


def file_path(file: WalkedFile) -> Path:
    """The path the indexer opens the file by."""
    return Path(f"{file.dirpath}/{file.name}")


def _md5(path: Path) -> str | None:
    # What the sync records: the md5 of the raw bytes, none when unreadable.
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def hashed(plan: FilePlan) -> FilePlan:
    """The plan with each file's md5 as Python reads it."""
    return plan._replace(hashes=tuple(_md5(file_path(file)) for file in plan.files))


def plan_lines(plan: FilePlan) -> list[str]:
    """The plan as lines: each file in walk order, with its hash, each folder
    in the order it was listed, then each report, sorted.

    Reports are compared as sets: Python meets a folder's subfolders in the
    order the system lists them, and the indexer sorts its reports before
    logging them. A name that is not UTF-8 is spelled as the log spells it.
    """
    lines = [
        cs.FRAME_PLAN_FILE_LINE.format(
            rel_path=file.rel_path,
            md5=cs.HASH_CACHE_UNREADABLE if md5 is None else md5,
            dirpath=readable_name(file.dirpath),
            name=file.name,
        )
        for file, md5 in zip(plan.files, plan.hashes, strict=True)
    ]
    lines.extend(
        cs.FRAME_PLAN_DIR_LINE.format(key=key, dirpath=readable_name(dirpath))
        for key, dirpath in plan.dirs
    )
    reports = (
        (cs.FramePlanReport.SYMLINK, plan.symlinks),
        (cs.FramePlanReport.SPECIAL, plan.special),
        (cs.FramePlanReport.UNDECODABLE, plan.undecodable),
    )
    for kind, paths in reports:
        lines.extend(
            cs.FRAME_PLAN_REPORT_LINE.format(kind=kind, rel_path=readable_name(path))
            for path in sorted(paths)
        )
    return lines


def plan_difference(python: FilePlan, frame: FilePlan) -> list[str]:
    """How the frame's plan differs from Python's, in order: `-` for a line
    only Python's holds, `+` for one only the frame's does."""
    return difference_in_order(plan_lines(python), plan_lines(frame))


def _frame_and_difference(
    repo_path: Path,
    exclude: frozenset[str] | None,
    unignore: frozenset[str] | None,
    python: FilePlan,
) -> tuple[FilePlan | None, list[str]] | None:
    """The frame's plan and its difference from Python's, or None when the
    frame has no plan to offer.

    A pattern the frame refuses is a difference of its own, with no plan.
    """
    try:
        frame = frame_file_plan(repo_path, exclude, unignore)
    except ValueError as error:
        return None, [
            cs.REFERENCE_ONLY_ACTUAL.format(
                line=cs.FRAME_PLAN_RAISED_LINE.format(error=error)
            )
        ]
    if frame is None:
        return None
    return frame, plan_difference(hashed(python), frame)


def _plan_taken(frame: FilePlan | None, python: FilePlan) -> FilePlan:
    """The frame's plan when the frame is on and has one, else Python's."""
    return frame if ENABLED and frame is not None else python


def plan_for_run(
    repo_path: Path,
    exclude: frozenset[str] | None,
    unignore: frozenset[str] | None,
    python: FilePlan,
) -> FilePlan:
    """The plan a run uses, given Python's walk of the repository.

    With the frame off and no check in place, Python's, the frame never
    asked. Otherwise the frame walks the repository too and the two plans
    are compared, hashes and all. The frame's is used only when it is on
    and the two agree; on a difference the check is told, or, with no
    check, one warning names the first differing lines and the run uses
    Python's.
    """
    check = CHECK
    if (not ENABLED and check is None) or (check is not None and not check.compares()):
        return python
    compared = _frame_and_difference(repo_path, exclude, unignore, python)
    if compared is None:
        return python
    frame, difference = compared
    if not difference:
        return _plan_taken(frame, python)
    if check is not None:
        check.differs(repo_path, difference)
        return _plan_taken(frame, python)
    logger.warning(
        ls.ENGINE_FRAME_PLAN_DIFFERS.format(
            repo=repo_path,
            first=cs.FRAME_PLAN_NAMES_SEPARATOR.join(
                difference[: cs.FRAME_PLAN_NAMED_LINES]
            ),
        )
    )
    return python
