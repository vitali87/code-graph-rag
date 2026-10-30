"""Name the files a scoped re-ingest covers: the ones it was told about, split
into present and gone, and the ones a change to them sends back for a
re-parse (issue #1524)."""

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

from .. import constants as cs
from ..sync.state import FileHashCache
from ..utils import path_utils
from ..utils.path_utils import cached_relative_path
from .dependents import DependentLookup, DependentRun
from .file_parse import read_or_empty
from .file_pass import go_package_dirs
from .generated_sources import GeneratedSources
from .module_seeding import flux_stems_of, stem_key
from .tree_scan import TreeScan


class ReingestRun(DependentRun, Protocol):
    """The re-ingest call the plan is for: the bytes a file's interface was
    judged on, which its re-parse then reads instead of the disk (issue
    #2531)."""

    reingest_pinned_sources: dict[str, bytes]


def _read_source(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


class ReingestPlanner:
    """The files one `GraphUpdater.reingest` call deletes and re-parses.

    `tree_scan` decides which files the walk would index, so a re-ingest
    leaves out what a sync would; `dependent_lookup` finds the files whose
    bindings a change can move.
    """

    def __init__(
        self,
        repo_path: Path,
        tree_scan: TreeScan,
        dependent_lookup: DependentLookup,
        generated_sources: GeneratedSources,
    ) -> None:
        self._repo_path = repo_path
        self._tree_scan = tree_scan
        self._dependent_lookup = dependent_lookup
        self._generated_sources = generated_sources

    def split(
        self, paths: Iterable[Path | str], deleted: Iterable[Path | str]
    ) -> tuple[dict[str, Path], dict[str, Path], set[str]]:
        # (present, gone, skipped): a listed path missing on disk counts as
        # gone, and a path the ignore rules exclude is reported back rather
        # than indexed. A directory in `paths` is refused: present or gone is
        # inferred from disk there, and the delete queries match nothing for a
        # directory, so it would be reported as removed while the graph stayed
        # untouched. `deleted` is an instruction, not an inference, and a
        # directory may now sit where the deleted file was (the watcher sees
        # exactly that race), so its delete goes ahead.
        #
        # A deleted path that is still a FILE is the atomic-save race: an
        # editor writing via delete-then-rename makes the watcher forward a
        # DELETE for a path that exists again by now. Retracting it erases the
        # definitions of a file that is present and parseable, and no later
        # incremental run repairs that -- the file is unchanged, so its hash
        # still matches and the walk never revisits it (issue #1799). It goes
        # to `present`, which re-parses it; the stale graph state is dropped
        # either way, because `_reingest_delete` clears every reparse key
        # before the re-parse writes the current one.
        present: dict[str, Path] = {}
        gone: dict[str, Path] = {}
        skipped: set[str] = set()
        for raw in paths:
            key, path = self._file_target(raw)
            if self._tree_scan.ignores(path):
                skipped.add(key)
                continue
            (present if path.is_file() else gone)[key] = path
        for raw in deleted:
            if path_utils.is_symlink_entry(self._absolute(raw)):
                # A file replaced by a link (issue #2451): the file's own
                # entry goes. Resolving the link would name its target, and
                # the link itself is never indexed.
                key, path = self._link_target(raw)
                present.pop(key, None)
                gone[key] = path
                continue
            key, path = self._target(raw)
            present.pop(key, None)
            if self._tree_scan.ignores(path):
                skipped.add(key)
                continue
            # `is_file` and not `exists`: a directory sitting where the deleted
            # file was is still a deletion, and the existing behaviour for it
            # must not change.
            (present if path.is_file() else gone)[key] = path
        return present, gone, skipped

    def _target(self, raw: Path | str) -> tuple[str, Path]:
        """(relative posix key, absolute path) for a file inside the repo.

        Relative inputs resolve against the repo root; anything escaping it
        (``..`` segments, symlinks out) is refused rather than silently
        clamped, since the caller is an agent naming files it just wrote.
        """
        path = Path(raw)
        if not path.is_absolute():
            path = self._repo_path / path
        resolved = path.resolve()
        root = self._repo_path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(cs.REINGEST_OUTSIDE_REPO.format(path=raw))
        key = resolved.relative_to(root).as_posix()
        return key, self._repo_path / key

    def _absolute(self, raw: Path | str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else self._repo_path / path

    def _link_target(self, raw: Path | str) -> tuple[str, Path]:
        """(relative posix key, absolute path) of a link's own entry.

        Only its directories are resolved, as `cached_file_identity_posix`
        keys a File: the entry is what was indexed at this path, wherever the
        link now points.
        """
        path = self._absolute(raw)
        entry = path.parent.resolve() / path.name
        root = self._repo_path.resolve()
        if not entry.is_relative_to(root):
            raise ValueError(cs.REINGEST_OUTSIDE_REPO.format(path=raw))
        key = entry.relative_to(root).as_posix()
        return key, self._repo_path / key

    def _file_target(self, raw: Path | str) -> tuple[str, Path]:
        key, path = self._target(raw)
        if path.is_dir():
            raise ValueError(cs.REINGEST_IS_DIRECTORY.format(path=raw))
        return key, path

    def flux_survivors(
        self, present: dict[str, Path], gone: dict[str, Path], hashes: FileHashCache
    ) -> tuple[set[str], dict[str, Path]]:
        # Stems with a same-stem sibling added (a present key the cache has
        # never seen) or deleted by this call, and the on-disk files that
        # share them and are not named in the call. Those survivors re-parse
        # in walk order so the bare module qn goes to the file a clean index
        # gives it rather than to the file indexed first (issue #1569).
        added = [key for key in present if key not in hashes]
        flux_stems = {stem for key in (*added, *gone) for stem in flux_stems_of(key)}
        survivors: dict[str, Path] = {}
        for stem in flux_stems:
            self._collect_stem_survivors(stem, present, gone, survivors)
        return flux_stems, survivors

    def _collect_stem_survivors(
        self,
        stem: str,
        present: dict[str, Path],
        gone: dict[str, Path],
        survivors: dict[str, Path],
    ) -> None:
        """Add the on-disk files on `stem` that this call does not name."""
        parent = (self._repo_path / stem).parent
        if not parent.is_dir():
            return
        for candidate in sorted(parent.iterdir()):
            # A link is no survivor: the walk leaves links out (#2451), and
            # `is_file` follows one.
            if not candidate.is_file() or path_utils.is_symlink_entry(candidate):
                continue
            key = cached_relative_path(candidate, self._repo_path).as_posix()
            if stem_key(key) != stem or key in present or key in gone:
                continue
            if self._tree_scan.ignores(candidate):
                continue
            survivors[key] = candidate

    def dependents(
        self,
        run: ReingestRun,
        present: dict[str, Path],
        gone: dict[str, Path],
        created: set[str] | None = None,
        stable: Mapping[str, str] | None = None,
    ) -> dict[str, Path]:
        # Files whose bindings the change can move (one level, from the
        # graph's own edges) plus any file whose delombok overlay changed.
        keys = sorted({*present, *gone})
        affected: dict[str, Path] = {}
        # Inbound edges find dependents of files that already existed; the
        # unresolved-import lookup finds importers of files this call CREATED,
        # which by construction have no inbound edges yet (issue #1682).
        waiters = {
            *self._dependent_lookup.unresolved_importer_keys(sorted(present)),
            # Created files offer everything; modified files offer only the
            # definitions they gained (PR #1979).
            *self._dependent_lookup.unresolved_reference_waiters(
                [
                    (key, self._generated_sources.overlay.get(key, read_or_empty(path)))
                    for key, path in sorted(present.items())
                    if created is not None and key in created
                ],
                [
                    (key, self._generated_sources.overlay.get(key, read_or_empty(path)))
                    for key, path in sorted(present.items())
                    if created is None or key not in created
                ],
            ),
            *self._dependent_lookup.dart_package_waiters(run, keys),
        }
        for caller_key in (
            *self._dependent_lookup.dependent_keys(keys, stable or {}, waiters),
            *waiters,
        ):
            caller_path = self._repo_path / caller_key
            if (
                caller_key not in present
                and caller_key not in gone
                and caller_path.is_file()
            ):
                affected[caller_key] = caller_path
        for key in self._generated_sources.stale_keys:
            if (
                key not in present
                and key not in gone
                and (self._repo_path / key).is_file()
            ):
                affected[key] = self._repo_path / key
        return affected

    def interface_stable(
        self,
        run: ReingestRun,
        present: dict[str, Path],
        hashes: FileHashCache,
        flux_stems: set[str],
        flipped_dirs: set[str],
    ) -> dict[str, str]:
        """`interface_stable_modules` for a scoped re-ingest.

        A created file, a file on a stem in flux and a file under a directory
        whose package-ness flipped are left out, as the batch path leaves out
        its added files and siblings. The bytes judged are the bytes the
        re-parse reads: a save landing in between would otherwise be parsed,
        and its fingerprint stored, without its callers ever being judged
        against it.
        """
        judged = {
            key: data
            for key, path in sorted(present.items())
            if key in hashes
            and not flux_stems_of(key) & flux_stems
            and Path(key).parent.as_posix() not in flipped_dirs
            and (data := _read_source(path)) is not None
        }
        stable = self._dependent_lookup.interface_stable_modules(
            run,
            (
                (present[key], key, self._generated_sources.overlay.get(key, data))
                for key, data in judged.items()
            ),
        )
        run.reingest_pinned_sources = {key: judged[key] for key in stable}
        return stable

    def source_for(self, run: ReingestRun, key: str, path: Path) -> bytes:
        """The bytes to re-parse `key` from: those its interface was judged
        on, when it was."""
        pinned = run.reingest_pinned_sources.pop(key, None)
        return path.read_bytes() if pinned is None else pinned

    def go_package_files(self, keys: set[str]) -> dict[str, Path]:
        """On-disk Go files beside one this call re-parses, for the re-parse.

        `run()` takes the same files (`file_pass._go_package_siblings`), for
        the same reason.
        """
        files: dict[str, Path] = {}
        for rel in go_package_dirs(keys):
            directory = self._repo_path / rel if rel != "." else self._repo_path
            if not directory.is_dir():
                continue
            for candidate in sorted(directory.glob(f"*{cs.EXT_GO}")):
                key = cached_relative_path(candidate, self._repo_path).as_posix()
                if (
                    key in keys
                    or not candidate.is_file()
                    or path_utils.is_symlink_entry(candidate)
                    or self._tree_scan.ignores(candidate)
                ):
                    continue
                files[key] = candidate
        return files

    def foreign_members(self, run: ReingestRun, keys: set[str]) -> dict[str, Path]:
        """On-disk files attaching members to a module this call replaces.

        `run()` queues the same files (`FilePass._queue_foreign_members`),
        for the same reason: the delete takes a C++ out-of-line definition
        with its header's class, and only its own file re-creates it (issue
        #3271). Repeated until nothing new joins, as there.
        """
        files: dict[str, Path] = {}
        scope = set(keys)
        frontier = sorted(scope)
        while frontier:
            joined: list[str] = []
            for key in self._dependent_lookup.foreign_member_keys(run, frontier):
                if key in scope:
                    continue
                scope.add(key)
                candidate = self._tree_scan.file_on_disk(key)
                if candidate is None:
                    continue
                files[key] = candidate
                joined.append(key)
            frontier = joined
        return files
