from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs
from ..services import IngestorProtocol
from ..types_defs import LanguageQueries, NodeIdentifier
from ..utils.path_utils import calculate_paths, should_skip_path


class StructureProcessor:
    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
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
        return (cs.NodeLabel.FOLDER, cs.KEY_PATH, parent_rel_path.as_posix())

    def identify_structure(self) -> None:
        directories = {self.repo_path}
        for path in self.repo_path.rglob(cs.GLOB_ALL):
            if path.is_dir() and not should_skip_path(
                path,
                self.repo_path,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
            ):
                directories.add(path)

        for root in sorted(directories):
            relative_root = root.relative_to(self.repo_path)

            parent_rel_path = relative_root.parent
            parent_container_qn = self.structural_elements.get(parent_rel_path)

            is_package = False
            package_indicators: set[str] = set()

            for lang_queries in self.queries.values():
                lang_config = lang_queries[cs.QUERY_CONFIG]
                package_indicators.update(lang_config.package_indicators)

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

                paths = calculate_paths(
                    file_path=root,
                    repo_path=self.repo_path,
                )

                self.ingestor.ensure_node_batch(
                    cs.NodeLabel.PACKAGE,
                    {
                        cs.KEY_QUALIFIED_NAME: package_qn,
                        cs.KEY_NAME: root.name,
                        cs.KEY_PATH: paths["relative_path"],
                        cs.KEY_ABSOLUTE_PATH: paths["absolute_path"],
                        cs.KEY_PROJECT_NAME: self.project_name,
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
            elif root != self.repo_path:
                self.structural_elements[relative_root] = None
                logger.info(
                    logs.STRUCT_IDENTIFIED_FOLDER.format(relative_root=relative_root)
                )

                paths = calculate_paths(
                    file_path=root,
                    repo_path=self.repo_path,
                )

                self.ingestor.ensure_node_batch(
                    cs.NodeLabel.FOLDER,
                    {
                        cs.KEY_PATH: paths["relative_path"],
                        cs.KEY_ABSOLUTE_PATH: paths["absolute_path"],
                        cs.KEY_NAME: root.name,
                        cs.KEY_PROJECT_NAME: self.project_name,
                    },
                )
                parent_identifier = self._get_parent_identifier(
                    parent_rel_path, parent_container_qn
                )
                self.ingestor.ensure_relationship_batch(
                    parent_identifier,
                    cs.RelationshipType.CONTAINS_FOLDER,
                    (cs.NodeLabel.FOLDER, cs.KEY_PATH, relative_root.as_posix()),
                )

    def process_generic_file(self, file_path: Path, file_name: str) -> None:
        relative_root = file_path.parent.relative_to(self.repo_path)

        parent_container_qn = self.structural_elements.get(relative_root)
        parent_identifier = self._get_parent_identifier(
            relative_root, parent_container_qn
        )

        paths = calculate_paths(
            file_path=file_path,
            repo_path=self.repo_path,
        )

        self.ingestor.ensure_node_batch(
            cs.NodeLabel.FILE,
            {
                cs.KEY_PATH: paths["relative_path"],
                cs.KEY_ABSOLUTE_PATH: paths["absolute_path"],
                cs.KEY_NAME: file_name,
                cs.KEY_EXTENSION: file_path.suffix,
                cs.KEY_PROJECT_NAME: self.project_name,
            },
        )

        self.ingestor.ensure_relationship_batch(
            parent_identifier,
            cs.RelationshipType.CONTAINS_FILE,
            (cs.NodeLabel.FILE, cs.KEY_PATH, paths["relative_path"]),
        )
