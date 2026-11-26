import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from .. import constants as cs
from .. import engine_frame, logs
from ..engine_frame import ContainedRow, NodeRow, RelationshipRow
from ..language_spec import LANGUAGE_SPECS
from ..services import IngestorProtocol
from ..types_defs import LanguageQueries, NodeIdentifier
from ..utils.path_utils import (
    cached_file_identity_posix,
    cached_relative_path,
    cached_resolve_posix,
    is_symlink_entry,
    is_utf8_name,
    should_skip_path,
)


class StructureSummary(NamedTuple):
    packages: int
    folders: int


class StructureProcessor:
    __slots__ = (
        "ingestor",
        "repo_path",
        "project_name",
        "queries",
        "structural_elements",
        "unignore_paths",
        "exclude_paths",
        "frame_elements",
        "file_rows",
    )

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        unignore_paths: frozenset[str] | None = None,
        exclude_paths: frozenset[str] | None = None,
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.queries = queries
        self.structural_elements: dict[Path, str | None] = {}
        self.unignore_paths = unignore_paths
        self.exclude_paths = exclude_paths
        # The frame's map from the last whole derivation that asked the
        # frame (engine_frame.py), which its File rows read; None when the
        # frame was not asked.
        self.frame_elements: tuple[tuple[str, str | None], ...] | None = None
        # The File rows a run settled on for the files it is indexing, each
        # written as its file is processed; empty otherwise, when every file
        # gets Python's own.
        self.file_rows: dict[Path, ContainedRow] = {}

    def _get_parent_identifier(
        self, parent_rel_path: Path, parent_container_qn: str | None
    ) -> NodeIdentifier:
        if parent_rel_path == Path(cs.PATH_CURRENT_DIR):
            return (cs.NodeLabel.PROJECT, cs.KEY_NAME, self.project_name)
        if parent_container_qn:
            return (cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, parent_container_qn)
        # Folder identity is the absolute path: relative paths collide across
        # same-layout projects in the shared graph (issue #897).
        return (
            cs.NodeLabel.FOLDER,
            cs.KEY_ABSOLUTE_PATH,
            cached_resolve_posix(self.repo_path / parent_rel_path),
        )

    @staticmethod
    def package_indicator_names() -> set[str]:
        """Filenames whose presence makes a directory a package.

        Package detection needs only the static language specs, never a
        loaded grammar; iterating `self.queries.values()` would force every
        lazy grammar to load (issue #68).

        A method rather than an inline set because `reingest` needs the same
        answer to decide whether an edited file could have changed a
        directory's kind (issue #1798), and two copies of this would drift
        the moment a language added an indicator.
        """
        names: set[str] = set()
        for lang_config in LANGUAGE_SPECS.values():
            names.update(lang_config.package_indicators)
        return names

    def is_package_dir(self, directory: Path) -> bool:
        """Whether this directory has a package indicator ON DISK, right now.

        Pure: reads the filesystem and writes nothing. `identify_structure`
        answers the same question but EMITS the Package/Folder nodes as a
        side effect, which a read-only prologue must not do -- an aborted
        scoped re-ingest that had already emitted one left the graph holding
        two container nodes for one directory while reporting that nothing
        changed (greptile-local, issue #1798).
        """
        return self._has_package_indicator(directory, self.package_indicator_names())

    @staticmethod
    def _has_package_indicator(directory: Path, indicators: Iterable[str]) -> bool:
        """Whether any of these indicator files is in the directory.

        `os.path.exists`, never `Path.exists`: on Python 3.12 and 3.13 the
        latter raised PermissionError for a directory the walk lists but
        may not enter (mode 000), which ended the whole run. Python 3.14 and
        `os.path.exists` read that as absent, so such a directory is a
        Folder on every version (issue #3748).
        """
        return any(os.path.exists(directory / indicator) for indicator in indicators)

    def _directories_to_derive(self, only: set[str] | None) -> set[Path]:
        """Every walkable directory, or just the ones `only` names.

        The repo root is always included: every directory's parent lookup
        resolves against it, and it is the fallback container.

        Top-down, and a skipped directory is not entered: everything beneath
        it is skipped too, and `rglob` listed every entry of `.git/objects`
        only to reject each one (CodeRabbit review of #3644). `only` narrows
        what is derived, not what is walked, so a requested directory is
        reached through ancestors it does not name.
        """
        directories = {self.repo_path}
        for dirpath, dirnames, _filenames in os.walk(self.repo_path):
            parent = Path(dirpath)
            walked: list[str] = []
            for name in dirnames:
                path = parent / name
                # A linked directory is not walked (issue #2451), so it gets no
                # container either: an in-repo one was an empty Package whose
                # absolute path was its target's. Nor is one whose name is not
                # valid UTF-8 (issue #3722): the graph cannot store its path.
                if (
                    is_symlink_entry(path)
                    or not is_utf8_name(name)
                    or should_skip_path(
                        path,
                        self.repo_path,
                        exclude_paths=self.exclude_paths,
                        unignore_paths=self.unignore_paths,
                        is_file=False,
                    )
                ):
                    continue
                walked.append(name)
                if only is None or (
                    cached_relative_path(path, self.repo_path).as_posix() in only
                ):
                    directories.add(path)
            dirnames[:] = walked
        return directories

    def _package_row(
        self,
        root: Path,
        relative_root: Path,
        package_qn: str,
        parent_rel_path: Path,
        parent_container_qn: str | None,
    ) -> ContainedRow:
        """The Package node and its parent containment edge."""
        return ContainedRow(
            node=NodeRow(
                label=cs.NodeLabel.PACKAGE,
                properties=(
                    (cs.KEY_QUALIFIED_NAME, package_qn),
                    (cs.KEY_NAME, root.name),
                    (cs.KEY_PATH, relative_root.as_posix()),
                    (cs.KEY_ABSOLUTE_PATH, cached_resolve_posix(root)),
                ),
            ),
            relationship=RelationshipRow(
                start=self._get_parent_identifier(parent_rel_path, parent_container_qn),
                rel_type=cs.RelationshipType.CONTAINS_PACKAGE,
                end=(cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, package_qn),
            ),
        )

    def _folder_row(
        self,
        root: Path,
        relative_root: Path,
        parent_rel_path: Path,
        parent_container_qn: str | None,
    ) -> ContainedRow:
        """The Folder node and its parent containment edge.

        Folder identity is the absolute path, not the relative one: relative
        paths collide across same-layout projects in the shared graph
        (issue #897).
        """
        return ContainedRow(
            node=NodeRow(
                label=cs.NodeLabel.FOLDER,
                properties=(
                    (cs.KEY_PATH, relative_root.as_posix()),
                    (cs.KEY_NAME, root.name),
                    (cs.KEY_ABSOLUTE_PATH, cached_resolve_posix(root)),
                ),
            ),
            relationship=RelationshipRow(
                start=self._get_parent_identifier(parent_rel_path, parent_container_qn),
                rel_type=cs.RelationshipType.CONTAINS_FOLDER,
                end=(
                    cs.NodeLabel.FOLDER,
                    cs.KEY_ABSOLUTE_PATH,
                    cached_resolve_posix(root),
                ),
            ),
        )

    def _write(self, row: ContainedRow) -> None:
        """Write a node, then the edge from what contains it."""
        self.ingestor.ensure_node_batch(row.node.label, dict(row.node.properties))
        relationship = row.relationship
        self.ingestor.ensure_relationship_batch(
            relationship.start, relationship.rel_type, relationship.end
        )

    def identify_structure(
        self, only: set[str] | None = None, *, emit: bool = True
    ) -> StructureSummary:
        """Derive every directory's kind, emitting Package and Folder nodes.

        `only` restricts BOTH the walk and the emission to the given
        repo-relative directories (and the repo root, which every parent
        lookup needs). A scoped re-ingest uses it: the unrestricted walk emits
        a node for every directory that changed on disk since the last
        derivation, including ones the call never named, so an unrelated
        directory whose `__init__.py` had been removed elsewhere gained a
        Folder node while keeping its Package node -- two container identities
        for one directory (Greptile, PR #1835).

        `emit=False` populates `structural_elements` and writes NOTHING. A
        re-ingest's hydration needs the whole map, because a re-parsed
        module's parent lookup reads it for the enclosing package, so it
        cannot use `only` -- but it has no business emitting a node for every
        directory in the repository. Doing both in one pass gave a directory
        the scoped call never named a second container identity, which is the
        same defect `only` exists to prevent arriving from the hydration path
        (issue #1872).

        Returns how many packages and folders it derived, which a full run
        reports in place of one line per directory (issue #2398).

        A whole derivation is the frame's too (engine_frame.py): Python's
        map and rows are compared with the frame's, and the frame's are the
        ones kept and written when it is on and the two agree. Either way
        they are written here, in the order Python derives them.
        """
        directories = self._directories_to_derive(only)
        if only is None:
            # The whole map is re-derived, so it holds only what this walk
            # admits. A reused updater otherwise kept a directory its walk had
            # since stopped admitting (excluded, no longer rescued, a link),
            # and the prune read its Folder as still in the tree (#2884).
            self.structural_elements.clear()
        self.frame_elements = None
        self.file_rows.clear()
        package_indicators = self.package_indicator_names()

        rows = [
            row
            for root in sorted(directories)
            if (row := self._derive_one_directory(root, package_indicators)) is not None
        ]
        if only is None:
            rows = self._settled_structure(rows, package_indicators)
        if emit:
            for row in rows:
                self._write(row)
        return StructureSummary(
            packages=sum(row.node.label == cs.NodeLabel.PACKAGE for row in rows),
            folders=sum(row.node.label == cs.NodeLabel.FOLDER for row in rows),
        )

    def _settled_structure(
        self, rows: list[ContainedRow], package_indicators: set[str]
    ) -> list[ContainedRow]:
        """The rows a whole derivation writes, Python's or the frame's, with
        the map made the same one's. The map is refilled in place: others
        hold it."""
        python = engine_frame.Structure(
            elements=tuple(
                (key.as_posix(), qn) for key, qn in self.structural_elements.items()
            ),
            rows=tuple(rows),
        )
        used, frame = engine_frame.structure_for_run(
            self.repo_path,
            self.project_name,
            package_indicators,
            self.exclude_paths,
            self.unignore_paths,
            python,
        )
        self.frame_elements = None if frame is None else frame.elements
        if used is not python:
            self.structural_elements.clear()
            self.structural_elements.update(
                (Path(key), qn) for key, qn in used.elements
            )
        return list(used.rows)

    def _derive_one_directory(
        self, root: Path, package_indicators: set[str]
    ) -> ContainedRow | None:
        """Record one directory's kind in the map, and return the row it
        writes: none for the root unless it is a package.

        Split out of `identify_structure` to keep it under the
        cognitive-complexity limit: the loop, the indicator scan and the two
        row branches nest three deep together, and the per-directory
        decision stands on its own.
        """
        relative_root = cached_relative_path(root, self.repo_path)
        parent_rel_path = relative_root.parent
        parent_container_qn = self.structural_elements.get(parent_rel_path)

        if self._has_package_indicator(root, package_indicators):
            package_qn = cs.SEPARATOR_DOT.join(
                [self.project_name] + list(relative_root.parts)
            )
            self.structural_elements[relative_root] = package_qn
            logger.debug(logs.STRUCT_IDENTIFIED_PACKAGE.format(package_qn=package_qn))
            return self._package_row(
                root, relative_root, package_qn, parent_rel_path, parent_container_qn
            )

        # Recorded for the ROOT too, which the Folder emission below
        # deliberately skips. Without this the root's stale package qn
        # survived a re-derivation, so a root that stopped being a package
        # still read as one and its Package node was never pruned
        # (greptile-local, issue #1798). The repo root gets no Folder node --
        # its parent is the Project -- but it still needs an accurate entry.
        self.structural_elements[relative_root] = None
        if root == self.repo_path:
            return None
        logger.debug(logs.STRUCT_IDENTIFIED_FOLDER.format(relative_root=relative_root))
        return self._folder_row(
            root, relative_root, parent_rel_path, parent_container_qn
        )

    def process_generic_file(self, file_path: Path, file_name: str) -> None:
        """Write the file's File node and its parent containment edge: the
        row its run settled on, else Python's own."""
        row = self.file_rows.pop(file_path, None)
        self._write(row if row is not None else self.file_row(file_path, file_name))

    def file_row(self, file_path: Path, file_name: str) -> ContainedRow:
        """The File node and its parent containment edge, as the map now
        places the file."""
        relative_filepath = cached_relative_path(file_path, self.repo_path).as_posix()
        relative_root = cached_relative_path(file_path.parent, self.repo_path)

        parent_container_qn = self.structural_elements.get(relative_root)
        parent_identifier = self._get_parent_identifier(
            relative_root, parent_container_qn
        )

        file_identity = cached_file_identity_posix(file_path)
        return ContainedRow(
            node=NodeRow(
                label=cs.NodeLabel.FILE,
                properties=(
                    (cs.KEY_PATH, relative_filepath),
                    (cs.KEY_NAME, file_name),
                    (cs.KEY_EXTENSION, file_path.suffix),
                    (cs.KEY_ABSOLUTE_PATH, file_identity),
                ),
            ),
            relationship=RelationshipRow(
                start=parent_identifier,
                rel_type=cs.RelationshipType.CONTAINS_FILE,
                end=(cs.NodeLabel.FILE, cs.KEY_ABSOLUTE_PATH, file_identity),
            ),
        )

    def settle_file_rows(self, paths: Sequence[Path]) -> None:
        """Settle the File rows of the files a run is about to index.

        With the frame asked for this run's structure, its File rows are
        compared with Python's and kept when it is on and the two agree,
        for `process_generic_file` to write as each file is processed.
        Otherwise nothing is kept, and each file gets Python's own row then,
        as before the frame.
        """
        self.file_rows.clear()
        if self.frame_elements is None:
            return
        python = tuple(self._file_row_or_none(path) for path in paths)
        used = engine_frame.file_rows_for_run(
            self.repo_path, self.project_name, self.frame_elements, paths, python
        )
        if used is python:
            return
        self.file_rows.update(
            (path, row)
            for path, row in zip(paths, used, strict=True)
            if row is not None
        )

    def _file_row_or_none(self, file_path: Path) -> ContainedRow | None:
        # No row for a path not under the root, nor for one whose folder
        # cannot be resolved (a loop of links raises before 3.13): settling
        # happens after the run's old subtrees are deleted, outside the
        # per-file handler, so it must not raise. The file's own processing
        # raises as it always has, where that handler logs it.
        try:
            return self.file_row(file_path, file_path.name)
        except (ValueError, RuntimeError, OSError):
            return None
