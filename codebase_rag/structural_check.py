"""`cgr check`: the structural delta of the working tree against a git ref.

The graph is assumed to reflect `--base` (index at the base, then edit);
the files that differ between the base and the working tree are re-ingested
and the delta reported the same way the MCP write tools report it after
each write (issue #1525). Exit status 1 with `--fail-on-found` makes it a
CI or pre-commit gate.

The check measures the graph against the working tree, and the re-ingest
brings the graph up to the tree, so a second run on an unchanged tree
reports nothing: the delta was already applied. Rebuild the graph at the
base (or index at the base before editing) to measure the same edit again,
or run the check isolated (`--isolated`, issue #1718): the subgraph the
re-ingest replaces is captured inside its prologue and put back once the
delta is computed, the hash cache with it, so the graph reads as it did
before and the same edit measures the same way every time. The CLI holds
no lock against a concurrently running MCP server; like every other
command that writes the graph, run it when no other writer is active.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from tree_sitter import Parser

from . import constants as cs
from .capture import CaptureSelection, default_capture
from .check_isolation import GraphStore, IsolationGuard
from .config import load_ignore_patterns
from .graph_updater import GraphUpdater, _load_exclusion_state
from .structural_delta import StructuralDelta, normalise_paths, observe
from .types_defs import LanguageQueries, ReingestReport
from .utils.path_utils import derive_project_name

_GIT_DELETED = "D"


_CGR_STATE_PREFIX = ".cgr-"


class CheckError(ValueError):
    """The working tree cannot be compared against the requested base."""


def changed_since(repo_root: Path, base: str) -> tuple[list[str], list[str]]:
    """Files that differ from `base`: (changed or added, deleted).

    Untracked files count as added; a rename shows as one deletion and one
    addition so the delta reports the old symbols as removed or renamed.
    """
    if base.startswith("-"):
        # Placed before git's own `--`, a dash-prefixed value would be read
        # as a diff option (`--cached` compares the index) rather than a
        # revision, and the check would measure the wrong files.
        raise CheckError(cs.CHECK_BAD_BASE.format(base=base))
    # A range (`HEAD~1..HEAD`, `main...HEAD`) makes `git diff` compare its
    # endpoints instead of a commit with the working tree, silently leaving
    # the current edits out; only a value naming one commit is a base.
    verified = subprocess.run(
        [cs.SHELL_CMD_GIT, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    if verified.returncode != 0:
        raise CheckError(cs.CHECK_BASE_NOT_A_COMMIT.format(base=base))
    try:
        status = subprocess.run(
            # `--relative`: paths relative to `repo_root`, which may sit below
            # the git toplevel; `ls-files --others` is already cwd-relative.
            # `-z`: NUL-delimited, unquoted paths, so a name holding a tab or
            # a newline is not C-quoted into something that does not exist.
            [
                cs.SHELL_CMD_GIT,
                "diff",
                "--name-status",
                "--no-renames",
                "--relative",
                "-z",
                base,
                "--",
            ],
            cwd=repo_root,
            capture_output=True,
            encoding=cs.ENCODING_UTF8,
            check=True,
        ).stdout
        untracked = subprocess.run(
            [cs.SHELL_CMD_GIT, "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=repo_root,
            capture_output=True,
            encoding=cs.ENCODING_UTF8,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise CheckError(
            cs.CHECK_GIT_FAILED.format(base=base, error=detail.strip())
        ) from error
    changed: set[str] = set()
    deleted: set[str] = set()
    # `-z` output alternates status and path fields, each NUL-terminated.
    fields = [f for f in status.split("\0") if f]
    for code, path in zip(fields[0::2], fields[1::2], strict=False):
        (deleted if code.startswith(_GIT_DELETED) else changed).add(path)
    # cgr's own untracked state files (hash cache, directory mtimes, ...)
    # are not source and must not be re-ingested or reported as reparsed.
    changed.update(
        entry
        for entry in untracked.split("\0")
        if entry and not Path(entry).name.startswith(_CGR_STATE_PREFIX)
    )
    return sorted(changed), sorted(deleted)


def _stamp_is_named(stored: dict[str, list[str] | str]) -> bool:
    """Whether the run that wrote this stamp was given an explicit --project.

    Stamps written before the field existed have no `named` key; they read
    as unnamed, which is what the overwhelming majority of them were.
    """
    return bool(stored.get("named"))


def indexed_scope(
    repo_root: Path, project_name: str, *, explicit: bool = False
) -> tuple[frozenset[str] | None, frozenset[str] | None]:
    """The exclusion scope `project_name`'s graph was last indexed under.

    The last completed run stamps its effective `--exclude` and unignore
    sets (CLI flags included, not only `.cgrignore`) in the exclusion state
    file; the check must re-ingest under that same scope or a file the
    index deliberately left out would enter the graph. The stamp is per
    repository, so one written by ANOTHER project indexed from this tree
    refuses the check rather than lending that project's scope. Without a
    stamp the `.cgrignore` file is the only scope there is.

    Ownership comes from the stamp, which records whether the run that
    wrote it was given a `--project`. The stamp is per repository and the
    last run of ANY project overwrites it, so the check must establish that
    it belongs to the project asked for and refuse otherwise: a scope from
    the wrong project silently re-ingests files the index left out, or drops
    files it deliberately kept.
    """
    stored = _load_exclusion_state(repo_root / cs.EXCLUSION_STATE_FILENAME)
    if stored is not None:
        owner = stored.get("project")
        # `cgr index` without --project stamps the bare directory name
        # where `cgr check` derives the digest-suffixed one, so an UNNAMED
        # run's stamp answers to either spelling of this tree's identity.
        # A NAMED run's answers only to the name it was given.
        #
        # The two cannot be told apart by the string alone -- a project
        # deliberately called `myrepo` in a directory called `myrepo` writes
        # the same owner an unnamed run does -- which is why the writer
        # records `named` and this reads it rather than guessing (#1525).
        #
        # `explicit` is what the CALLER asked for. Both sides must agree:
        # an unnamed stamp does not serve `--project myrepo`, because the
        # last unnamed run of this tree overwrote whatever the named project
        # had stamped, and its scope is simply gone rather than inferable.
        default_names = {derive_project_name(repo_root), repo_root.resolve().name}
        stamp_named = _stamp_is_named(stored)
        if stamp_named:
            mine = owner == project_name
        else:
            mine = (
                not explicit
                and owner in default_names
                and project_name in default_names
            )
        if isinstance(owner, str) and not mine:
            raise CheckError(
                cs.CHECK_SCOPE_OF_OTHER_PROJECT.format(
                    project=project_name, other=owner
                )
            )
        exclude = stored.get("exclude") or []
        unignore = stored.get("unignore") or []
        return (
            frozenset(exclude) or None,  # type: ignore[arg-type]
            frozenset(unignore) or None,  # type: ignore[arg-type]
        )
    cgrignore = load_ignore_patterns(repo_root)
    return cgrignore.exclude or None, cgrignore.unignore or None


class _FileSnapshot:
    """A file's bytes and timestamps, to be put back after the check.

    The re-ingest records the files it re-parsed in the hash cache, which
    would make a later full run skip them and keep the base graph for files
    the working tree changed. The cache's mtime is also a judgement about
    every file NOT in it (`_reingest_update_hashes`), so it goes back too.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._content: bytes | None = None
        self._times: tuple[int, int] | None = None
        # Absent and unreadable are different states and must not share one
        # representation: treating a permission error as absence made the
        # restore DELETE a cache it could not read (Greptile, #1718). Only a
        # confirmed absence licenses the unlink.
        self._absent = False
        self._readable = False
        try:
            self._content = path.read_bytes()
            stat = path.stat()
            self._times = (stat.st_atime_ns, stat.st_mtime_ns)
            self._readable = True
        except FileNotFoundError:
            self._absent = True
        except OSError:
            # Unreadable: leave whatever is there alone.
            pass

    def put_back(self) -> None:
        if self._absent:
            self._path.unlink(missing_ok=True)
            return
        if not self._readable or self._content is None or self._times is None:
            return
        self._path.write_bytes(self._content)
        os.utime(self._path, ns=self._times)


_UNRESTORABLE_RELS = (
    cs.RelationshipType.RESOLVES_TO,
    cs.RelationshipType.FLOWS_TO,
)


def _refuse_unrestorable_capture(capture: CaptureSelection) -> None:
    """Refuse an isolated run whose capture holds links it cannot restore.

    The guard restores by walking out from the re-parsed modules, and a
    `Resource` is not on that walk: it is only ever the far end of an edge.
    The endpoint pass then deletes every network `RESOLVES_TO` edge in the
    graph and rebuilds them from the edited tree, and a Resource-to-Resource
    `FLOWS_TO` chain touches no scoped node at all, so neither can be put
    back from the capture. Losing them silently is worse than not offering
    the mode, so this refuses instead (greptile-local, #1718).
    """
    enabled = [rel for rel in _UNRESTORABLE_RELS if capture.rel_enabled(rel)]
    if enabled:
        raise CheckError(
            cs.CHECK_ISOLATED_WITH_IO.format(
                groups=", ".join(rel.value for rel in enabled)
            )
        )


def _isolated(
    updater: GraphUpdater,
    ingestor: object,
    project_name: str,
    repo_root: Path,
    apply: Callable[[Callable[[], None]], ReingestReport],
    measure: Callable[[Callable[[], ReingestReport]], StructuralDelta],
) -> StructuralDelta:
    """Measure through `apply`, then put the graph and the hash cache back.

    The guard captures inside the re-ingest's write hook, so a run that
    aborts in its prologue captured nothing and restores nothing; one that
    fails after its first write is rolled back from the capture before the
    error propagates.
    """
    guard = IsolationGuard(cast(GraphStore, ingestor), project_name, repo_root)
    cache = _FileSnapshot(repo_root / cs.HASH_CACHE_FILENAME)
    try:
        return measure(lambda: apply(lambda: guard.capture(updater.reingest_scope)))
    finally:
        try:
            guard.restore()
        finally:
            cache.put_back()


def run_check(
    repo_root: Path,
    base: str,
    project_name: str,
    ingestor: object,
    parsers: Mapping[cs.SupportedLanguage, Parser],
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
    isolated: bool = False,
    capture: CaptureSelection | None = None,
) -> StructuralDelta:
    """Re-ingest what changed since `base` and return the structural delta.

    `exclude_paths` and `unignore_paths` are the project's indexing scope
    (the `.cgrignore` file plus any CLI excludes): a changed file outside
    that scope must not enter the graph through the check. With `isolated`
    the re-ingest is measured and then undone (see `check_isolation`).
    """
    changed, deleted = changed_since(repo_root, base)
    changed = normalise_paths(changed, repo_root)
    deleted = normalise_paths(deleted, repo_root)
    updater = GraphUpdater(
        ingestor=ingestor,  # type: ignore[arg-type]
        repo_path=repo_root,
        parsers=parsers,
        queries=queries,
        project_name=project_name,
        exclude_paths=exclude_paths,
        unignore_paths=unignore_paths,
        # The selection isolated mode validated must be the one the run
        # uses; without it the updater falls back to the configured default,
        # which can enable the IO links `_refuse_unrestorable_capture` just
        # refused (Greptile, #1718).
        capture=capture,
    )
    fetch_all = getattr(ingestor, "fetch_all")

    def measure(apply: Callable[[], ReingestReport]) -> StructuralDelta:
        return observe(
            fetch_all, project_name, [*changed, *deleted], apply, repo_root=repo_root
        )

    if not isolated:
        return measure(lambda: updater.reingest(changed, deleted=deleted))
    _refuse_unrestorable_capture(capture or default_capture())
    return _isolated(
        updater,
        ingestor,
        project_name,
        repo_root,
        lambda hook: updater.reingest(changed, deleted=deleted, before_write=hook),
        measure,
    )
