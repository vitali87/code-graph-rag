from pathlib import Path
from typing import Any

from loguru import logger

from ..config import IGNORE_PATTERNS
from ..services import IngestorProtocol


class StructureProcessor:
    """Handles identification and processing of project structure."""

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        queries: dict[str, Any],
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.queries = queries
        self.structural_elements: dict[Path, str | None] = {}
        self.ignore_dirs = IGNORE_PATTERNS

    def identify_structure(self) -> None:
        """First pass: Efficiently walks the directory to find all packages and folders."""

        def should_skip_dir(path: Path) -> bool:
            """Check if directory should be skipped based on ignore patterns."""
            return any(part in self.ignore_dirs for part in path.parts)

        directories = {self.repo_path}  # Start with root
        for path in self.repo_path.rglob("*"):
            if path.is_dir() and not should_skip_dir(path.relative_to(self.repo_path)):
                directories.add(path)

        for root in sorted(directories):
            relative_root = root.relative_to(self.repo_path)

            parent_rel_path = relative_root.parent
            parent_container_qn = self.structural_elements.get(parent_rel_path)

            is_package = False
            package_indicators = set()

            for lang_name, lang_queries in self.queries.items():
                lang_config = lang_queries["config"]
                package_indicators.update(lang_config.package_indicators)

            for indicator in package_indicators:
                if (root / indicator).exists():
                    is_package = True
                    break

            if is_package:
                package_qn = ".".join([self.project_name] + list(relative_root.parts))
                self.structural_elements[relative_root] = package_qn
                logger.info(f"  Identified Package: {package_qn}")
                self.ingestor.ensure_node_batch(
                    "Package",
                    {
                        "qualified_name": package_qn,
                        "name": root.name,
                        "path": str(relative_root),
                    },
                )
                parent_label, parent_key, parent_val = (
                    ("Project", "name", self.project_name)
                    if parent_rel_path == Path(".")
                    else (
                        ("Package", "qualified_name", parent_container_qn)
                        if parent_container_qn
                        else ("Folder", "path", str(parent_rel_path))
                    )
                )
                self.ingestor.ensure_relationship_batch(
                    (parent_label, parent_key, parent_val),
                    "CONTAINS_PACKAGE",
                    ("Package", "qualified_name", package_qn),
                )
            elif root != self.repo_path:
                self.structural_elements[relative_root] = None  # Mark as folder
                logger.info(f"  Identified Folder: '{relative_root}'")
                self.ingestor.ensure_node_batch(
                    "Folder", {"path": str(relative_root), "name": root.name}
                )
                parent_label, parent_key, parent_val = (
                    ("Project", "name", self.project_name)
                    if parent_rel_path == Path(".")
                    else (
                        ("Package", "qualified_name", parent_container_qn)
                        if parent_container_qn
                        else ("Folder", "path", str(parent_rel_path))
                    )
                )
                self.ingestor.ensure_relationship_batch(
                    (parent_label, parent_key, parent_val),
                    "CONTAINS_FOLDER",
                    ("Folder", "path", str(relative_root)),
                )

    def process_generic_file(self, file_path: Path, file_name: str) -> None:
        """Process a generic (non-parseable) file and create appropriate nodes/relationships."""
        relative_filepath = str(file_path.relative_to(self.repo_path))
        relative_root = file_path.parent.relative_to(self.repo_path)

        parent_container_qn = self.structural_elements.get(relative_root)
        parent_label, parent_key, parent_val = (
            ("Package", "qualified_name", parent_container_qn)
            if parent_container_qn
            else (
                ("Folder", "path", str(relative_root))
                if relative_root != Path(".")
                else ("Project", "name", self.project_name)
            )
        )

        self.ingestor.ensure_node_batch(
            "File",
            {
                "path": relative_filepath,
                "name": file_name,
                "extension": file_path.suffix,
            },
        )

        self.ingestor.ensure_relationship_batch(
            (parent_label, parent_key, parent_val),
            "CONTAINS_FILE",
            ("File", "path", relative_filepath),
        )
