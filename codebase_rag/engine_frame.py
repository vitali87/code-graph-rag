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

Change detection's file half is the frame's too (src/engine/state.rs): the
in-sync check (`in_sync_for_run`), and each run's new, changed and deleted
files with the hash cache and folder times it would publish
(`changes_for_run`, from a baseline `baseline_for_run` reads before the run
writes its placeholder cache). Both are checked the way the plan is. The
frame prepares the cache and the folder times and never writes them: the
updater publishes the ones the run used, at its commit point.

The structure pass is the frame's as well (src/engine/structure.rs): the
Package and Folder rows and the map of each folder's package
(`structure_for_run`), and the File rows of the files a run indexes
(`file_rows_for_run`), checked the same way. The frame builds the rows;
the structure processor still writes them, at the point it always has.

The merge (src/engine/merge.rs) folds each parsed file's summary into the
project's index in walk order. In phase 2 it fills the module names: which
file claims each module's bare name and which keep their extension
(`modules_for_run`), compared with the module map the definition processor
filled, as `index.txt`'s `module` lines. Python's map is the one every run
goes on with until phase 3, so a difference is only told or warned of.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import cache
from pathlib import Path, PurePath
from typing import NamedTuple, Protocol

from loguru import logger

from . import constants as cs
from . import logs as ls
from .canonical_graph import difference_in_order
from .embedded_graph.extension import rust_extension
from .language_spec import LANGUAGE_SPECS, get_language_for_extension
from .types_defs import NodeIdentifier
from .utils.path_utils import (
    cached_relative_path,
    readable_name,
    walk_eligible_files,
)

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


class StateInputs(NamedTuple):
    """Where a run's change detection reads its state, and under which sets.

    `cgr_home` is CGR_HOME, its `~` expanded, when the run's state folder is
    the one `checkout_state.state_dir` names, which the frame then names
    too; None for a run given a folder of its own.
    """

    repo_path: Path
    cache_path: Path
    dir_mtimes_path: Path
    exclude: frozenset[str] | None
    unignore: frozenset[str] | None
    cgr_home: Path | None


class FileChanges(NamedTuple):
    """What a run's change detection decides.

    The state folder (when it is the default one), the cache mtime the run
    compares files against and whether it is a full build, the files forced
    through a re-parse, then each settled file's hash or `unreadable` mark
    in walk order, the files unreadable and still current, those to
    re-parse in walk order with whether each is new, a single-file target
    that is gone, the files deleted before and after the parse, a
    single-file run's cache to merge into, the stamp the cache is published
    with (a project run's watermark, or a single-file run's previous cache
    mtime), the folder times in walk order, and the cache and folder times
    as the text that would be written: `cache_text` None when the run
    publishes no cache, `mtimes_text` None when it writes no folder times.
    """

    state_dir: str | None
    cache_mtime: float
    full_build: bool
    forced: tuple[str, ...]
    hashes: tuple[tuple[str, str], ...]
    unreadable: tuple[str, ...]
    current: frozenset[str]
    changed: tuple[tuple[str, bool], ...]
    gone: str | None
    deleted_first: tuple[str, ...]
    deleted: tuple[str, ...]
    merge_base: tuple[tuple[str, str], ...] | None
    stamp: float
    mtimes: tuple[tuple[str, float], ...] | None
    cache_text: str | None
    mtimes_text: str | None


class NodeRow(NamedTuple):
    """A node the ingestor writes: its label and its properties, in the
    order its dict holds them."""

    label: cs.NodeLabel
    properties: tuple[tuple[str, str], ...]


class RelationshipRow(NamedTuple):
    """An edge the ingestor writes: `start -[rel_type]-> end`."""

    start: NodeIdentifier
    rel_type: cs.RelationshipType
    end: NodeIdentifier


class ContainedRow(NamedTuple):
    """A node and the edge from what contains it, written in that order:
    what the structure pass writes for each package, folder and file."""

    node: NodeRow
    relationship: RelationshipRow


class Structure(NamedTuple):
    """What the structure pass derives for a repository.

    `elements` is `structural_elements` in the order it is filled: each
    folder's path from the root as `as_posix()` spells it (`.` for the
    root) and its package's qualified name, None for a plain folder.
    `rows` are the Package and Folder rows in the order they are written.
    """

    elements: tuple[tuple[str, str | None], ...]
    rows: tuple[ContainedRow, ...]


class ModuleClaim(NamedTuple):
    """A file a run parsed into the module map, as the frame's summary of it
    carries it: its place in the order the run parses its files, which is
    the file plan's, its path from the root, and the language it was read
    as."""

    walk_index: int
    rel_path: str
    language: cs.SupportedLanguage


class ModuleNaming(Protocol):
    """The language tables the frame derives module names by, held for
    `Engine.module_plan`."""


class ChangeBaseline(Protocol):
    """The frame's baseline for a run, held for `Engine.changes`."""

    @property
    def cache_mtime(self) -> float: ...

    @property
    def full_build(self) -> bool: ...


type RawFilePlan = tuple[
    list[tuple[str, str, str]],
    list[tuple[str, str]],
    list[str],
    list[str],
    list[str],
    list[str | None],
]
# `Engine.changes`: `FileChanges` but its state folder, in its order.
type RawChanges = tuple[
    float,
    bool,
    list[str],
    list[tuple[str, str]],
    list[str],
    list[str],
    list[tuple[str, bool]],
    str | None,
    list[str],
    list[str],
    list[tuple[str, str]] | None,
    float,
    list[tuple[str, float]] | None,
    str | None,
    str | None,
]
type RawIgnoreFiles = tuple[
    list[str], list[str], list[tuple[str, int]], list[str], list[str]
]
type RawNodeId = tuple[str, str, str]
# A row as `Engine` hands it on: the node's label and properties, then the
# edge from what contains it.
type RawRow = tuple[str, list[tuple[str, str]], RawNodeId, str, RawNodeId]
type RawStructure = tuple[list[tuple[str, str | None]], list[RawRow]]
# A module the frame's plan holds: its qualified name, then the path from the
# root and the language of the file that claimed it.
type PlannedModule = tuple[str, str, str]


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

    def state_dir(self, cgr_home: Path, repo: Path) -> str | None: ...

    def prepare_state_dir(
        self, cgr_home: Path, repo: Path
    ) -> tuple[str, list[str], list[tuple[str, str]], str | None] | None: ...

    def read_hash_cache(
        self, path: Path
    ) -> tuple[list[tuple[str, str]], str | None]: ...

    def read_dir_mtimes(self, path: Path) -> list[tuple[str, float]]: ...

    def hash_cache_text(self, entries: Sequence[tuple[str, str]]) -> str: ...

    def dir_mtimes_text(self, entries: Sequence[tuple[str, float]]) -> str: ...

    def publish_hash_cache(
        self, path: Path, entries: Sequence[tuple[str, str]], stamp: float
    ) -> None: ...

    def save_dir_mtimes(
        self, path: Path, entries: Sequence[tuple[str, float]]
    ) -> None: ...

    def in_sync(
        self,
        repo: Path,
        cache_path: Path,
        dir_mtimes_path: Path,
        discard_cache: bool,
        exclude: Iterable[str] | None = None,
        unignore: Iterable[str] | None = None,
    ) -> bool: ...

    def change_baseline(
        self,
        cache_path: Path,
        force: bool,
        single: bool,
        cache_dead: bool,
        project_mismatch: bool,
        parser_changed: bool,
        stale_keys: Iterable[str],
    ) -> ChangeBaseline: ...

    def changes(
        self,
        baseline: ChangeBaseline,
        repo: Path,
        exclude: Iterable[str] | None = None,
        unignore: Iterable[str] | None = None,
        single_path: Path | None = None,
        single_key: str | None = None,
        graph_paths: Iterable[str] | None = None,
    ) -> RawChanges: ...

    def structure(
        self,
        root: Path,
        project_name: str,
        markers: Iterable[str],
        exclude: Iterable[str] | None = None,
        unignore: Iterable[str] | None = None,
    ) -> RawStructure: ...

    def file_rows(
        self,
        root: Path,
        project_name: str,
        elements: Sequence[tuple[str, str | None]],
        paths: Sequence[Path],
        dot_suffix: bool,
    ) -> list[RawRow | None]: ...

    def module_plan(
        self,
        root: Path,
        project_name: str,
        naming: ModuleNaming,
        claims: Sequence[ModuleClaim],
        dot_suffix: bool,
        exclude: Iterable[str] | None = None,
        unignore: Iterable[str] | None = None,
    ) -> list[PlannedModule]: ...


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
    """What replaces the warning when the frame's answer and Python's differ.

    The tests' strict check (`codebase_rag/tests/engine_modes.py`) and the
    pinned repositories' (`scripts/handoff.py`): `compares` says whether this
    run's plan can be compared at all, and `differs` receives the
    difference, as `-` lines of Python's plan and `+` lines of the frame's;
    `compares_changes` and `changes_differ` do the same for the run's change
    detection, `compares_structure` and `structure_differs` for its
    structure and File rows, and `compares_modules` and `modules_differ`
    for its module-name plan.
    """

    def compares(self) -> bool: ...

    def differs(self, repo_path: Path, difference: Sequence[str]) -> None: ...

    def compares_changes(self) -> bool: ...

    def changes_differ(self, repo_path: Path, difference: Sequence[str]) -> None: ...

    def compares_structure(self) -> bool: ...

    def structure_differs(self, repo_path: Path, difference: Sequence[str]) -> None: ...

    def compares_modules(self) -> bool: ...

    def modules_differ(self, repo_path: Path, difference: Sequence[str]) -> None: ...


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
        return None, _raised(error)
    if frame is None:
        return None
    return frame, plan_difference(hashed(python), frame)


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
    return _settled(
        repo_path,
        python,
        frame,
        difference,
        None if check is None else check.differs,
        ls.ENGINE_FRAME_PLAN_DIFFERS,
    )


def _settled[T](
    repo_path: Path,
    python: T,
    frame: T | None,
    difference: Sequence[str],
    report: Callable[[Path, Sequence[str]], None] | None,
    warning: str,
) -> T:
    """The answer a run uses once the frame's is compared with Python's.

    The frame's when it is on and the two agree; on a difference, the check
    is told and a frame that is on is still used, or, with no check, one
    warning names the first differing lines and the run uses Python's.
    """
    if _warned(repo_path, difference, report, warning):
        return python
    return frame if ENABLED and frame is not None else python


def _warned(
    repo_path: Path,
    difference: Sequence[str],
    report: Callable[[Path, Sequence[str]], None] | None,
    warning: str,
) -> bool:
    """Tell the check of a difference, or, with no check in place, log one
    warning naming its first lines; whether the warning was logged."""
    if not difference:
        return False
    if report is not None:
        report(repo_path, difference)
        return False
    logger.warning(
        warning.format(
            repo=repo_path,
            first=cs.FRAME_PLAN_NAMES_SEPARATOR.join(
                difference[: cs.FRAME_PLAN_NAMED_LINES]
            ),
        )
    )
    return True


def _raised(error: ValueError) -> list[str]:
    """A frame that raised, as the one line only its side holds."""
    return [
        cs.REFERENCE_ONLY_ACTUAL.format(
            line=cs.FRAME_PLAN_RAISED_LINE.format(error=error)
        )
    ]


def _asks_changes(check: PlanCheck | None) -> bool:
    """Whether a run's change detection is compared: with the frame on or a
    check in place, unless the check cannot compare this run."""
    if check is None:
        return ENABLED
    return check.compares_changes()


def _difference(python: Sequence[str], frame: Sequence[str]) -> list[str]:
    # Most runs agree, and a large cache is long: equal lines need no diff.
    return [] if python == frame else difference_in_order(python, frame)


def frame_state_dir(cgr_home: Path, repo_path: Path) -> str | None:
    """The state folder the frame names for the checkout under `cgr_home`,
    or None when the extension is from before it or Python cannot name the
    path."""
    made = engine()
    if made is None or not hasattr(made, cs.EG_RUST_STATE_DIR):
        return None
    return made.state_dir(cgr_home, repo_path)


def frame_in_sync(inputs: StateInputs, discard_cache: bool) -> bool | None:
    """The frame's in-sync check, or None when the extension is from before
    it. Raises `ValueError` where Python raises on a pattern."""
    made = engine()
    if made is None or not hasattr(made, cs.EG_RUST_IN_SYNC):
        return None
    return made.in_sync(
        inputs.repo_path,
        inputs.cache_path,
        inputs.dir_mtimes_path,
        discard_cache,
        inputs.exclude,
        inputs.unignore,
    )


def in_sync_for_run(inputs: StateInputs, discard_cache: bool, python: bool) -> bool:
    """The in-sync verdict a run acts on, given Python's.

    Compared, and chosen, as `plan_for_run` chooses a plan: with the frame
    off and no check in place, Python's, the frame never asked.
    """
    check = CHECK
    if not _asks_changes(check):
        return python
    try:
        frame = frame_in_sync(inputs, discard_cache)
    except ValueError as error:
        frame = None
        difference = _raised(error)
    else:
        if frame is None:
            return python
        difference = _difference(
            [cs.FRAME_IN_SYNC_LINE.format(in_sync=python)],
            [cs.FRAME_IN_SYNC_LINE.format(in_sync=frame)],
        )
    return _settled(
        inputs.repo_path,
        python,
        frame,
        difference,
        None if check is None else check.changes_differ,
        ls.ENGINE_FRAME_CHANGES_DIFFER,
    )


def baseline_for_run(
    cache_path: Path,
    *,
    force: bool,
    single: bool,
    cache_dead: bool,
    project_mismatch: bool,
    parser_changed: bool,
    stale_keys: Iterable[str],
) -> ChangeBaseline | None:
    """The frame's baseline for a run whose change detection is compared,
    read as `_hash_baseline` reads Python's, before the run writes its
    placeholder cache; None when the run is not compared or the extension
    is from before it."""
    if not _asks_changes(CHECK):
        return None
    made = engine()
    if made is None or not hasattr(made, cs.EG_RUST_CHANGE_BASELINE):
        return None
    return made.change_baseline(
        cache_path,
        force,
        single,
        cache_dead,
        project_mismatch,
        parser_changed,
        sorted(stale_keys),
    )


def frame_changes(
    baseline: ChangeBaseline,
    inputs: StateInputs,
    single: tuple[Path, str | None] | None,
    graph_paths: Iterable[str] | None,
) -> FileChanges | None:
    """The frame's change detection from its baseline: the repository walked
    under the inputs' sets, or the single target `(path, key)` (the key None
    when the rules leave it out), with the graph's module paths (None when
    the graph could not answer). None when the extension is from before it.
    Raises `ValueError` where Python's walk raises on a pattern."""
    made = engine()
    if made is None or not hasattr(made, cs.EG_RUST_CHANGES):
        return None
    single_path, single_key = (None, None) if single is None else single
    found = made.changes(
        baseline,
        inputs.repo_path,
        inputs.exclude,
        inputs.unignore,
        single_path,
        single_key,
        graph_paths,
    )
    state_dir = (
        None
        if inputs.cgr_home is None
        else made.state_dir(inputs.cgr_home, inputs.repo_path)
    )
    (
        cache_mtime,
        full_build,
        forced,
        hashes,
        unreadable,
        current,
        changed,
        gone,
        deleted_first,
        deleted,
        merge_base,
        stamp,
        mtimes,
        cache_text,
        mtimes_text,
    ) = found
    return FileChanges(
        state_dir=state_dir,
        cache_mtime=cache_mtime,
        full_build=full_build,
        forced=tuple(forced),
        hashes=tuple(hashes),
        unreadable=tuple(unreadable),
        current=frozenset(current),
        changed=tuple(changed),
        gone=gone,
        deleted_first=tuple(deleted_first),
        deleted=tuple(deleted),
        merge_base=None if merge_base is None else tuple(merge_base),
        stamp=stamp,
        mtimes=None if mtimes is None else tuple(mtimes),
        cache_text=cache_text,
        mtimes_text=mtimes_text,
    )


def prepared_cache(
    hashes: Mapping[str, str],
    merge_base: Mapping[str, str] | None,
    gone: str | None,
) -> dict[str, str] | None:
    """The cache a run publishes: a project run's hashes, or a single-file
    run's merged over the previous cache, its gone target left out, and
    nothing when there was no usable cache to merge into."""
    if merge_base is None:
        return dict(hashes)
    if not merge_base:
        return None
    merged = {**merge_base, **hashes}
    if gone is not None:
        merged.pop(gone, None)
    return merged


def cache_text(hashes: Mapping[str, str] | None) -> str | None:
    """The hash cache as `_publish_hash_cache` writes it, None for none."""
    if hashes is None:
        return None
    return json.dumps(hashes, indent=cs.HASH_CACHE_JSON_INDENT)


def mtimes_text(mtimes: Mapping[str, float] | None) -> str | None:
    """The folder times as `_save_dir_mtimes` writes them, None for none."""
    return None if mtimes is None else json.dumps(mtimes)


def change_lines(changes: FileChanges) -> list[str]:
    """A run's change detection as lines: the state folder, the baseline,
    the cache it would publish as the lines it would write, then the files
    unreadable, changed (in walk order), gone and deleted, then the folder
    times, each and as the text written, and a single-file run's kept
    mtime. A project run's watermark is each engine's own and is left out.
    """
    lines = []
    if changes.state_dir is not None:
        lines.append(cs.FRAME_STATE_DIR_LINE.format(path=changes.state_dir))
    lines.append(cs.FRAME_CACHE_MTIME_LINE.format(mtime=repr(changes.cache_mtime)))
    lines.append(cs.FRAME_FULL_BUILD_LINE.format(full_build=changes.full_build))
    lines.extend(cs.FRAME_FORCED_LINE.format(key=key) for key in changes.forced)
    if changes.cache_text is None:
        lines.append(cs.FRAME_NO_CACHE_LINE)
    else:
        lines.extend(
            cs.FRAME_CACHE_LINE.format(line=line)
            for line in changes.cache_text.split(cs.LINE_FEED)
        )
    lines.extend(cs.FRAME_UNREADABLE_LINE.format(key=key) for key in changes.unreadable)
    lines.extend(
        cs.FRAME_CHANGED_LINE.format(
            key=key, kind=cs.FrameChange.NEW if is_new else cs.FrameChange.CHANGED
        )
        for key, is_new in changes.changed
    )
    if changes.gone is not None:
        lines.append(cs.FRAME_GONE_LINE.format(key=changes.gone))
    lines.extend(
        cs.FRAME_DELETED_FIRST_LINE.format(key=key) for key in changes.deleted_first
    )
    lines.extend(cs.FRAME_DELETED_LINE.format(key=key) for key in changes.deleted)
    if changes.mtimes is not None:
        lines.extend(
            cs.FRAME_MTIME_LINE.format(key=key, mtime=repr(mtime))
            for key, mtime in changes.mtimes
        )
    if changes.mtimes_text is not None:
        lines.append(cs.FRAME_MTIMES_LINE.format(text=changes.mtimes_text))
    if changes.merge_base is not None:
        lines.append(cs.FRAME_KEPT_MTIME_LINE.format(mtime=repr(changes.stamp)))
    return lines


def changes_difference(python: FileChanges, frame: FileChanges) -> list[str]:
    """How the frame's change detection differs from Python's, in order:
    `-` for a line only Python's holds, `+` for one only the frame's does."""
    return _difference(change_lines(python), change_lines(frame))


def changes_for_run(
    baseline: ChangeBaseline | None,
    inputs: StateInputs,
    single: tuple[Path, str | None] | None,
    graph_paths: Iterable[str] | None,
    python: FileChanges,
) -> FileChanges:
    """The change detection a run goes on with, given Python's.

    None for `baseline` means the run is not compared, and Python's is
    used. Otherwise the frame detects the changes too, from its baseline,
    and the two are compared and chosen as `plan_for_run` chooses a plan.
    """
    if baseline is None:
        return python
    check = CHECK
    try:
        frame = frame_changes(baseline, inputs, single, graph_paths)
    except ValueError as error:
        frame = None
        difference = _raised(error)
    else:
        if frame is None:
            return python
        difference = changes_difference(python, frame)
    return _settled(
        inputs.repo_path,
        python,
        frame,
        difference,
        None if check is None else check.changes_differ,
        ls.ENGINE_FRAME_CHANGES_DIFFER,
    )


# Whether this Python gives a name ending in a dot a suffix (3.14 on), which
# the frame's File rows are told, so their extension is this Python's.
_DOT_SUFFIX: bool = (
    PurePath(cs.FRAME_TRAILING_DOT_NAME).suffix == cs.FRAME_TRAILING_DOT_SUFFIX
)


def _asks_structure(check: PlanCheck | None) -> bool:
    """Whether a run's structure is compared: with the frame on or a check
    in place, unless the check cannot compare this run."""
    if check is None:
        return ENABLED
    return check.compares_structure()


def _identifier(raw: RawNodeId) -> NodeIdentifier:
    label, key, value = raw
    return (cs.NodeLabel(label), key, value)


def _row(raw: RawRow) -> ContainedRow:
    """A row the frame built, with the schema's own label and edge type."""
    label, properties, start, rel_type, end = raw
    return ContainedRow(
        node=NodeRow(label=cs.NodeLabel(label), properties=tuple(properties)),
        relationship=RelationshipRow(
            start=_identifier(start),
            rel_type=cs.RelationshipType(rel_type),
            end=_identifier(end),
        ),
    )


def frame_structure(
    repo_path: Path,
    project_name: str,
    markers: Iterable[str],
    exclude: frozenset[str] | None = None,
    unignore: frozenset[str] | None = None,
) -> Structure | None:
    """The frame's structure of the repository under these sets, a folder
    holding a file named as any of `markers` a package; None when the
    extension is from before it. Raises `ValueError` where Python's walk
    raises on a pattern."""
    made = engine()
    if made is None or not hasattr(made, cs.EG_RUST_STRUCTURE):
        return None
    elements, rows = made.structure(
        repo_path, project_name, sorted(markers), exclude, unignore
    )
    return Structure(elements=tuple(elements), rows=tuple(_row(row) for row in rows))


def frame_file_rows(
    repo_path: Path,
    project_name: str,
    elements: Sequence[tuple[str, str | None]],
    paths: Sequence[Path],
) -> tuple[ContainedRow | None, ...] | None:
    """The frame's File row of each of `paths`, under the structure map
    `elements`, None for a path it cannot place under the root; None when
    the extension is from before it."""
    made = engine()
    if made is None or not hasattr(made, cs.EG_RUST_FILE_ROWS):
        return None
    rows = made.file_rows(
        repo_path, project_name, list(elements), list(paths), _DOT_SUFFIX
    )
    return tuple(None if row is None else _row(row) for row in rows)


def _identifier_text(identifier: NodeIdentifier) -> str:
    label, key, value = identifier
    return cs.FRAME_ROW_IDENTIFIER.format(
        label=label, key=key, value=readable_name(str(value))
    )


def row_lines(row: ContainedRow) -> list[str]:
    """A row as two lines: its node, with each property in order, then the
    edge from what contains it."""
    properties = cs.FRAME_ROW_FIELDS_SEPARATOR.join(
        cs.FRAME_ROW_PROPERTY.format(key=key, value=readable_name(value))
        for key, value in row.node.properties
    )
    relationship = row.relationship
    return [
        cs.FRAME_NODE_ROW_LINE.format(label=row.node.label, properties=properties),
        cs.FRAME_EDGE_ROW_LINE.format(
            start=_identifier_text(relationship.start),
            rel_type=relationship.rel_type,
            end=_identifier_text(relationship.end),
        ),
    ]


def structure_lines(structure: Structure) -> list[str]:
    """A structure as lines: each folder's map entry in order, then each
    row in the order it is written."""
    lines = [
        cs.FRAME_ELEMENT_LINE.format(key=key)
        if qn is None
        else cs.FRAME_PACKAGE_ELEMENT_LINE.format(key=key, qn=qn)
        for key, qn in structure.elements
    ]
    lines.extend(line for row in structure.rows for line in row_lines(row))
    return lines


def structure_difference(python: Structure, frame: Structure) -> list[str]:
    """How the frame's structure differs from Python's, in order: `-` for a
    line only Python's holds, `+` for one only the frame's does."""
    return _difference(structure_lines(python), structure_lines(frame))


def file_row_lines(
    paths: Sequence[Path], rows: Sequence[ContainedRow | None]
) -> list[str]:
    """Each path's File row as lines, in order, or a line naming a path
    that has none."""
    lines: list[str] = []
    for path, row in zip(paths, rows, strict=True):
        if row is None:
            lines.append(
                cs.FRAME_NO_FILE_ROW_LINE.format(path=readable_name(str(path)))
            )
        else:
            lines.extend(row_lines(row))
    return lines


def file_rows_difference(
    paths: Sequence[Path],
    python: Sequence[ContainedRow | None],
    frame: Sequence[ContainedRow | None],
) -> list[str]:
    """How the frame's File rows for `paths` differ from Python's, in order:
    `-` for a line only Python's hold, `+` for one only the frame's do."""
    return _difference(file_row_lines(paths, python), file_row_lines(paths, frame))


def structure_for_run(
    repo_path: Path,
    project_name: str,
    markers: Iterable[str],
    exclude: frozenset[str] | None,
    unignore: frozenset[str] | None,
    python: Structure,
) -> tuple[Structure, Structure | None]:
    """The structure a run writes, given Python's, and the frame's when it
    was asked.

    Compared, and chosen, as `plan_for_run` chooses a plan: with the frame
    off and no check in place, Python's, the frame never asked.
    """
    check = CHECK
    if not _asks_structure(check):
        return python, None
    try:
        frame = frame_structure(repo_path, project_name, markers, exclude, unignore)
    except ValueError as error:
        frame = None
        difference = _raised(error)
    else:
        if frame is None:
            return python, None
        difference = structure_difference(python, frame)
    used = _settled(
        repo_path,
        python,
        frame,
        difference,
        None if check is None else check.structure_differs,
        ls.ENGINE_FRAME_STRUCTURE_DIFFERS,
    )
    return used, frame


def file_rows_for_run(
    repo_path: Path,
    project_name: str,
    elements: Sequence[tuple[str, str | None]],
    paths: Sequence[Path],
    python: tuple[ContainedRow | None, ...],
) -> tuple[ContainedRow | None, ...]:
    """The File rows a run writes for `paths`, given Python's: the frame's
    under its own structure map `elements`, compared and chosen as
    `structure_for_run` chooses."""
    check = CHECK
    if not _asks_structure(check):
        return python
    frame = frame_file_rows(repo_path, project_name, elements, paths)
    if frame is None:
        return python
    return _settled(
        repo_path,
        python,
        frame,
        file_rows_difference(paths, python, frame),
        None if check is None else check.structure_differs,
        ls.ENGINE_FRAME_FILE_ROWS_DIFFER,
    )


def modules_compared() -> bool:
    """Whether a run's module-name plan is compared: with the frame on or a
    check in place, unless the check cannot compare this run."""
    check = CHECK
    if check is None:
        return ENABLED
    return check.compares_modules()


@cache
def naming_rules() -> ModuleNaming | None:
    """The language tables the frame names modules by, as the definition
    processor's naming reads them, or None when the extension is from before
    the merge: each extension's language, the families, the module
    extensions a stem drops whole and the prefix marking a declaration, the
    files named after their folder, and a notebook's suffix."""
    naming_type = getattr(rust_extension(), cs.EG_RUST_MODULE_NAMING, None)
    if naming_type is None:
        return None
    languages = {
        extension: language
        for spec in LANGUAGE_SPECS.values()
        for extension in spec.file_extensions
        if (language := get_language_for_extension(extension)) is not None
    }
    return naming_type(
        sorted(languages.items()),
        [sorted(family) for family in cs.LANGUAGE_FAMILIES],
        list(cs.JS_TS_MODULE_EXTENSIONS),
        cs.DECLARATION_EXT_PREFIX,
        [*cs.PY_PACKAGE_INIT_FILES, cs.MOD_RS],
        cs.EXT_IPYNB,
    )


def frame_module_plan(
    repo_path: Path,
    project_name: str,
    claims: Sequence[ModuleClaim],
    exclude: frozenset[str] | None = None,
    unignore: frozenset[str] | None = None,
) -> tuple[PlannedModule, ...] | None:
    """The frame's module-name plan for the files a run parsed into the
    module map, by qualified name; None when the extension is from before
    the merge. Raises `ValueError` where Python raises on a pattern."""
    made = engine()
    naming = naming_rules()
    if made is None or naming is None or not hasattr(made, cs.EG_RUST_MODULE_PLAN):
        return None
    return tuple(
        made.module_plan(
            repo_path,
            project_name,
            naming,
            list(claims),
            _DOT_SUFFIX,
            exclude,
            unignore,
        )
    )


def module_lines(modules: Iterable[tuple[str, str, str | None]]) -> list[str]:
    """Modules, each `(qualified name, path, language)`, as `index.txt`'s
    `module` lines, sorted as it writes them."""
    # Here, not at the top: the hand-off module reads the notebook rule from
    # the parsers, whose structure processor imports this module.
    from .handoff import module_line

    return sorted(module_line(qn, path, language) for qn, path, language in modules)


def python_modules(
    repo_path: Path, modules: Mapping[str, Path]
) -> list[tuple[str, str, str | None]]:
    """The definition processor's module map as the frame's plan holds its
    modules: each name, with its file's path from the root and language."""
    from .handoff import module_language

    return [
        (qn, cached_relative_path(path, repo_path).as_posix(), module_language(path))
        for qn, path in modules.items()
    ]


def modules_difference(
    repo_path: Path,
    python: Mapping[str, Path],
    frame: Iterable[tuple[str, str, str | None]],
) -> list[str]:
    """How the frame's module-name plan differs from Python's module map,
    line by line of `index.txt`: `-` for a line only Python's holds, `+` for
    one only the frame's does."""
    return _difference(
        module_lines(python_modules(repo_path, python)), module_lines(frame)
    )


def modules_for_run(
    repo_path: Path,
    project_name: str,
    claims: Sequence[ModuleClaim],
    exclude: frozenset[str] | None,
    unignore: frozenset[str] | None,
    python: Mapping[str, Path],
) -> None:
    """Compare the frame's module-name plan for a run's claims with the
    module map Python's pass 2 filled from the same files.

    For a run `modules_compared` says is compared. Python's map is the one
    the run goes on with, whatever the comparison finds (docs/reports/
    PHASE2_FRAME.md, section 3): a difference is told to the check, or, with
    no check, named in one warning.
    """
    check = CHECK
    try:
        frame = frame_module_plan(repo_path, project_name, claims, exclude, unignore)
    except ValueError as error:
        difference = _raised(error)
    else:
        if frame is None:
            return
        difference = modules_difference(repo_path, python, frame)
    _warned(
        repo_path,
        difference,
        None if check is None else check.modules_differ,
        ls.ENGINE_FRAME_MODULES_DIFFER,
    )
