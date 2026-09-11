from collections.abc import Mapping
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs
from ..language_spec import LANGUAGE_SPECS
from ..services import IngestorProtocol
from ..types_defs import LanguageQueries, NodeIdentifier
from ..utils.path_utils import (
    cached_file_identity_posix,
    cached_relative_path,
    cached_resolve_posix,
    should_skip_path,
)


class StructureProcessor:
    __slots__ = (
        "ingestor",
        "repo_path",
        "project_name",
        "queries",
        "structural_elements",
        "unignore_paths",
        "exclude_paths",
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
        return any(
            (directory / indicator).exists()
            for indicator in self.package_indicator_names()
        )

    def _directories_to_derive(self, only: set[str] | None) -> set[Path]:
        """Every walkable directory, or just the ones `only` names.

        The repo root is always included: every directory's parent lookup
        resolves against it, and it is the fallback container.
        """
        directories = {self.repo_path}
        for path in self.repo_path.rglob(cs.GLOB_ALL):
            if not path.is_dir():
                continue
            if only is not None and (
                cached_relative_path(path, self.repo_path).as_posix() not in only
            ):
                continue
            if not should_skip_path(
                path,
                self.repo_path,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
            ):
                directories.add(path)
        return directories

    def identify_structure(self, only: set[str] | None = None) -> None:
        """Derive every directory's kind, emitting Package and Folder nodes.

        `only` restricts BOTH the walk and the emission to the given
        repo-relative directories (and the repo root, which every parent
        lookup needs). A scoped re-ingest uses it: the unrestricted walk emits
        a node for every directory that changed on disk since the last
        derivation, including ones the call never named, so an unrelated
        directory whose `__init__.py` had been removed elsewhere gained a
        Folder node while keeping its Package node -- two container identities
        for one directory (Greptile, PR #1835).
        """
        directories = self._directories_to_derive(only)
        package_indicators = self.package_indicator_names()

        for root in sorted(directories):
            relative_root = cached_relative_path(root, self.repo_path)

            parent_rel_path = relative_root.parent
            parent_container_qn = self.structural_elements.get(parent_rel_path)

            is_package = False
            for indicator in package_indicators:
                if (root / indicator).exists():
                    is_package = True
                    break

            if is_package:
                package_qn = cs.SEPARATOR_DOT.join(
                    [self.project_name] + list(relative_root.parts)
                )
                self.structural_elements[relative_root] = package_qn
                logger.info(
                    logs.STRUCT_IDENTIFIED_PACKAGE.format(package_qn=package_qn)
                )
                self.ingestor.ensure_node_batch(
                    cs.NodeLabel.PACKAGE,
                    {
                        cs.KEY_QUALIFIED_NAME: package_qn,
                        cs.KEY_NAME: root.name,
                        cs.KEY_PATH: relative_root.as_posix(),
                        cs.KEY_ABSOLUTE_PATH: cached_resolve_posix(root),
                    },
                )
                parent_identifier = self._get_parent_identifier(
                    parent_rel_path, parent_container_qn
                )
                self.ingestor.ensure_relationship_batch(
                    parent_identifier,
                    cs.RelationshipType.CONTAINS_PACKAGE,
                    (cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, package_qn),
                )
            else:
                # Recorded for the ROOT too, which the Folder emission below
                # deliberately skips. Without this the root's stale package qn
                # survived a re-derivation, so a root that stopped being a
                # package still read as one and its Package node was never
                # pruned (greptile-local, issue #1798). The repo root gets no
                # Folder node -- its parent is the Project -- but it still
                # needs an accurate entry.
                self.structural_elements[relative_root] = None
            if not is_package and root != self.repo_path:
                logger.info(
                    logs.STRUCT_IDENTIFIED_FOLDER.format(relative_root=relative_root)
                )
                self.ingestor.ensure_node_batch(
                    cs.NodeLabel.FOLDER,
                    {
                        cs.KEY_PATH: relative_root.as_posix(),
                        cs.KEY_NAME: root.name,
                        cs.KEY_ABSOLUTE_PATH: cached_resolve_posix(root),
                    },
                )
                parent_identifier = self._get_parent_identifier(
                    parent_rel_path, parent_container_qn
                )
                self.ingestor.ensure_relationship_batch(
                    parent_identifier,
                    cs.RelationshipType.CONTAINS_FOLDER,
                    (
                        cs.NodeLabel.FOLDER,
                        cs.KEY_ABSOLUTE_PATH,
                        cached_resolve_posix(root),
                    ),
                )

    def process_generic_file(self, file_path: Path, file_name: str) -> None:
        relative_filepath = cached_relative_path(file_path, self.repo_path).as_posix()
        relative_root = cached_relative_path(file_path.parent, self.repo_path)

        parent_container_qn = self.structural_elements.get(relative_root)
        parent_identifier = self._get_parent_identifier(
            relative_root, parent_container_qn
        )

        file_identity = cached_file_identity_posix(file_path)
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.FILE,
            {
                cs.KEY_PATH: relative_filepath,
                cs.KEY_NAME: file_name,
                cs.KEY_EXTENSION: file_path.suffix,
                cs.KEY_ABSOLUTE_PATH: file_identity,
            },
        )

        self.ingestor.ensure_relationship_batch(
            parent_identifier,
            cs.RelationshipType.CONTAINS_FILE,
            (cs.NodeLabel.FILE, cs.KEY_ABSOLUTE_PATH, file_identity),
        )
