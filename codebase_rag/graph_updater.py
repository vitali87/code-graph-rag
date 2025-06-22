import os
import toml
from pathlib import Path
from typing import Any, Optional, Dict

from loguru import logger
from tree_sitter import Language, Parser, Node

from codebase_rag.services.graph_service import MemgraphIngestor

# Import available Tree-sitter languages
try:
    from tree_sitter_python import language as python_language_so
except ImportError:
    python_language_so = None

try:
    from tree_sitter_javascript import language as javascript_language_so
except ImportError:
    javascript_language_so = None

try:
    from tree_sitter_typescript import language_typescript as typescript_language_so
except ImportError:
    typescript_language_so = None

try:
    from tree_sitter_rust import language as rust_language_so
except ImportError:
    rust_language_so = None

try:
    from tree_sitter_go import language as go_language_so
except ImportError:
    go_language_so = None

from .language_config import (
    get_language_config,
    get_language_config_by_name,
    LanguageConfig,
)

# Language library mapping
LANGUAGE_LIBRARIES = {
    "python": python_language_so,
    "javascript": javascript_language_so,
    "typescript": typescript_language_so,
    "rust": rust_language_so,
    "go": go_language_so,
}


class GraphUpdater:
    """Parses code using Tree-sitter and updates the graph."""

    def __init__(self, ingestor: MemgraphIngestor, repo_path: Path):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = repo_path.name
        self.structural_elements: Dict[Path, Optional[str]] = {}
        self.ignore_dirs = {
            ".git",
            "venv",
            ".venv",
            "__pycache__",
            "node_modules",
            "build",
            "dist",
            ".eggs",
        }

        # Initialize parsers and queries for all available languages
        self.parsers: Dict[str, Parser] = {}
        self.languages: Dict[str, Language] = {}
        self.queries: Dict[str, Dict[str, Any]] = {}

        self._initialize_languages()

    def _initialize_languages(self):
        """Initialize Tree-sitter parsers for all available languages."""
        from .language_config import LANGUAGE_CONFIGS

        available_languages = []

        for lang_name, lang_config in LANGUAGE_CONFIGS.items():
            lang_lib = LANGUAGE_LIBRARIES.get(lang_name)
            if lang_lib is not None:
                try:
                    # Create parser and language for this language
                    parser = Parser()
                    language = Language(lang_lib())
                    parser.language = language

                    self.parsers[lang_name] = parser
                    self.languages[lang_name] = language

                    # Compile queries for this language
                    self._compile_queries_for_language(lang_name, lang_config, language)

                    available_languages.append(lang_name)
                    logger.success(f"Successfully loaded {lang_name} grammar.")

                except Exception as e:
                    logger.warning(f"Failed to load {lang_name} grammar: {e}")
            else:
                logger.debug(f"Tree-sitter library for {lang_name} not available.")

        if not available_languages:
            raise RuntimeError(
                "No Tree-sitter languages available. Please install tree-sitter language packages."
            )

        logger.info(f"Initialized parsers for: {', '.join(available_languages)}")

    def _compile_queries_for_language(
        self, lang_name: str, lang_config: LanguageConfig, language: Language
    ):
        """Compile Tree-sitter queries for a specific language."""
        function_patterns = " ".join(
            [
                f"({node_type}) @function"
                for node_type in lang_config.function_node_types
            ]
        )
        class_patterns = " ".join(
            [f"({node_type}) @class" for node_type in lang_config.class_node_types]
        )

        self.queries[lang_name] = {
            "functions": language.query(function_patterns),
            "classes": language.query(class_patterns),
            "config": lang_config,
        }

    def run(self) -> None:
        """Orchestrates the two-pass parsing and ingestion process."""
        self.ingestor.ensure_node_batch("Project", {"name": self.project_name})
        logger.info(f"Ensuring Project: {self.project_name}")

        logger.info("--- Pass 1: Identifying Packages and Folders ---")
        self._identify_structure()

        logger.info("\n--- Pass 2: Processing Files and Python Modules ---")
        self._process_files()

        logger.info("\n--- Analysis complete. Flushing all data to database... ---")
        self.ingestor.flush_all()

    def _identify_structure(self) -> None:
        """First pass: Walks the directory to find all packages and folders."""
        for root_str, dirs, _ in os.walk(self.repo_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]
            root = Path(root_str)
            relative_root = root.relative_to(self.repo_path)

            parent_rel_path = relative_root.parent
            parent_container_qn = self.structural_elements.get(parent_rel_path)

            # Check if this directory is a package for any supported language
            is_package = False
            package_indicators = set()

            # Collect package indicators from all language configs
            for lang_name, lang_queries in self.queries.items():
                lang_config = lang_queries["config"]
                package_indicators.update(lang_config.package_indicators)

            # Check if any package indicator exists
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
                    else ("Package", "qualified_name", parent_container_qn)
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

    def _process_files(self) -> None:
        """Second pass: Walks the directory again to process all files."""
        for root_str, dirs, files in os.walk(self.repo_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]
            root = Path(root_str)
            relative_root = root.relative_to(self.repo_path)
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

            for file_name in files:
                filepath = root / file_name
                relative_filepath = str(filepath.relative_to(self.repo_path))

                # Create generic File node for all files
                self.ingestor.ensure_node_batch(
                    "File",
                    {
                        "path": relative_filepath,
                        "name": file_name,
                        "extension": filepath.suffix,
                    },
                )
                self.ingestor.ensure_relationship_batch(
                    (parent_label, parent_key, parent_val),
                    "CONTAINS_FILE",
                    ("File", "path", relative_filepath),
                )

                # Check if this file type is supported
                lang_config = get_language_config(filepath.suffix)
                if lang_config and lang_config.name in self.parsers:
                    self.parse_and_ingest_file(filepath, lang_config.name)
                elif file_name == "pyproject.toml":
                    self._parse_dependencies(filepath)

    def _get_docstring(self, node: Node) -> Optional[str]:
        """Extracts the docstring from a function or class node's body."""
        body_node = node.child_by_field_name("body")
        if not body_node or not body_node.children:
            return None
        first_statement = body_node.children[0]
        if (
            first_statement.type == "expression_statement"
            and first_statement.children[0].type == "string"
        ):
            return first_statement.children[0].text.decode("utf-8").strip("'\" \n")
        return None

    def parse_and_ingest_file(self, file_path: Path, language: str):
        relative_path = file_path.relative_to(self.repo_path)
        relative_path_str = str(relative_path)
        logger.info(f"Parsing {language}: {relative_path_str}")

        try:
            source_bytes = file_path.read_bytes()
            parser = self.parsers[language]
            tree = parser.parse(source_bytes)
            root_node = tree.root_node

            module_qn = ".".join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )
            if file_path.name == "__init__.py":
                module_qn = ".".join(
                    [self.project_name] + list(relative_path.parent.parts)
                )

            self.ingestor.ensure_node_batch(
                "Module",
                {
                    "qualified_name": module_qn,
                    "name": file_path.name,
                    "path": relative_path_str,
                },
            )

            # Link Module to its parent Package/Folder
            parent_rel_path = relative_path.parent
            parent_container_qn = self.structural_elements.get(parent_rel_path)
            parent_label, parent_key, parent_val = (
                ("Package", "qualified_name", parent_container_qn)
                if parent_container_qn
                else (
                    ("Folder", "path", str(parent_rel_path))
                    if parent_rel_path != Path(".")
                    else ("Project", "name", self.project_name)
                )
            )
            self.ingestor.ensure_relationship_batch(
                (parent_label, parent_key, parent_val),
                "CONTAINS_MODULE",
                ("Module", "qualified_name", module_qn),
            )

            self._ingest_top_level_functions(root_node, module_qn, language)
            self._ingest_classes_and_methods(root_node, module_qn, language)

        except Exception as e:
            logger.error(f"Failed to parse or ingest {file_path}: {e}", exc_info=True)

    def _ingest_top_level_functions(self, root_node, parent_qn: str, language: str):
        lang_queries = self.queries[language]
        lang_config = lang_queries["config"]

        captures = lang_queries["functions"].captures(root_node)
        if "function" in captures:
            for func_node in captures["function"]:
                # Get the function name
                name_node = func_node.child_by_field_name("name")
                if not name_node:
                    continue
                func_name = name_node.text.decode("utf8")

                # Build qualified name based on nesting context
                func_qn = self._build_nested_qualified_name(
                    func_node, parent_qn, func_name, lang_config
                )

                # Skip if this is a method (will be handled by class processing) or if qn is None
                if func_qn is None or self._is_method(func_node, lang_config):
                    continue

                props = {
                    "qualified_name": func_qn,
                    "name": func_name,
                    "decorators": [],
                    "start_line": func_node.start_point[0] + 1,
                    "end_line": func_node.end_point[0] + 1,
                    "docstring": self._get_docstring(func_node),
                }
                logger.info(f"  Found Function: {func_name} (qn: {func_qn})")
                self.ingestor.ensure_node_batch("Function", props)

                # Link to the appropriate parent (Module for top-level, Function for nested)
                parent_type, parent_key = self._determine_function_parent(
                    func_node, parent_qn, lang_config
                )
                self.ingestor.ensure_relationship_batch(
                    (parent_type, "qualified_name", parent_key),
                    "DEFINES",
                    ("Function", "qualified_name", func_qn),
                )

    def _build_nested_qualified_name(
        self, func_node, module_qn: str, func_name: str, lang_config: LanguageConfig
    ) -> str:
        """Build qualified name for nested functions by traversing parent hierarchy."""
        path_parts = []
        current = func_node.parent

        # Traverse up the AST to build the nesting hierarchy
        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    path_parts.append(name_node.text.decode("utf8"))
            elif current.type in lang_config.class_node_types:
                # Parent is a class - this is a method, not a nested function
                return None  # Will be handled as method
            current = current.parent

        # Reverse to get correct order (innermost to outermost)
        path_parts.reverse()

        # Build the qualified name: module.parent_func.nested_func
        if path_parts:
            return f"{module_qn}.{'.'.join(path_parts)}.{func_name}"
        else:
            return f"{module_qn}.{func_name}"

    def _is_method(self, func_node, lang_config: LanguageConfig) -> bool:
        """Check if a function is actually a method (inside a class)."""
        current = func_node.parent
        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.class_node_types:
                return True
            current = current.parent
        return False

    def _determine_function_parent(
        self, func_node, module_qn: str, lang_config: LanguageConfig
    ) -> tuple[str, str]:
        """Determine the parent entity for linking relationships."""
        current = func_node.parent

        # Look for immediate parent function
        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    parent_func_name = name_node.text.decode("utf8")
                    if parent_qn := self._build_nested_qualified_name(
                        current, module_qn, parent_func_name, lang_config
                    ):
                        return "Function", parent_qn
                break
            current = current.parent

        # Default to module parent
        return "Module", module_qn

    def _ingest_classes_and_methods(self, root_node, parent_qn: str, language: str):
        lang_queries = self.queries[language]
        lang_config = lang_queries["config"]

        class_captures = lang_queries["classes"].captures(root_node)
        if "class" in class_captures:
            for class_node in class_captures["class"]:
                name_node = class_node.child_by_field_name("name")
                if not name_node:
                    continue
                class_name = name_node.text.decode("utf8")
                class_qn = f"{parent_qn}.{class_name}"
                class_props = {
                    "qualified_name": class_qn,
                    "name": class_name,
                    "decorators": [],
                    "start_line": class_node.start_point[0] + 1,
                    "end_line": class_node.end_point[0] + 1,
                    "docstring": self._get_docstring(class_node),
                }
                logger.info(f"  Found Class: {class_name} (qn: {class_qn})")
                self.ingestor.ensure_node_batch("Class", class_props)
                self.ingestor.ensure_relationship_batch(
                    ("Module", "qualified_name", parent_qn),
                    "DEFINES",
                    ("Class", "qualified_name", class_qn),
                )
                body_node = class_node.child_by_field_name("body")
                if not body_node:
                    continue
                method_captures = lang_queries["functions"].captures(body_node)
                if "function" in method_captures:
                    for method_node in method_captures["function"]:
                        method_name_node = method_node.child_by_field_name("name")
                        if not method_name_node:
                            continue
                        method_name = method_name_node.text.decode("utf8")
                        method_qn = f"{class_qn}.{method_name}"
                        method_props = {
                            "qualified_name": method_qn,
                            "name": method_name,
                            "decorators": [],
                            "start_line": method_node.start_point[0] + 1,
                            "end_line": method_node.end_point[0] + 1,
                            "docstring": self._get_docstring(method_node),
                        }
                        logger.info(
                            f"    Found Method: {method_name} (qn: {method_qn})"
                        )
                        self.ingestor.ensure_node_batch("Method", method_props)
                        self.ingestor.ensure_relationship_batch(
                            ("Class", "qualified_name", class_qn),
                            "DEFINES_METHOD",
                            ("Method", "qualified_name", method_qn),
                        )

    def _parse_dependencies(self, filepath: Path) -> None:
        """Parses a pyproject.toml file for dependencies."""
        logger.info(f"  Parsing pyproject.toml: {filepath}")
        try:
            data = toml.load(filepath)
            # Support both Poetry and standard PEP 621 dependencies
            deps = (data.get("tool", {}).get("poetry", {}).get("dependencies", {})) or {
                dep.split(">=")[0].split("==")[0].strip(): dep
                for dep in data.get("project", {}).get("dependencies", [])
            }

            for dep_name, dep_spec in deps.items():
                if dep_name.lower() == "python":
                    continue
                logger.info(f"    Found dependency: {dep_name} (spec: {dep_spec})")
                self.ingestor.ensure_node_batch("ExternalPackage", {"name": dep_name})
                self.ingestor.ensure_relationship_batch(
                    ("Project", "name", self.project_name),
                    "DEPENDS_ON_EXTERNAL",
                    ("ExternalPackage", "name", dep_name),
                    properties={"version_spec": str(dep_spec)},
                )
        except Exception as e:
            logger.error(f"    Error parsing {filepath}: {e}")
