
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

try:
    from tree_sitter_scala import language as scala_language_so
except ImportError:
    scala_language_so = None

try:
    from tree_sitter_java import language as java_language_so
except ImportError:
    java_language_so = None

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
    "scala": scala_language_so,
    "java": java_language_so,
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
        call_patterns = " ".join(
            [f"({node_type}) @call" for node_type in lang_config.call_node_types]
        )

        self.queries[lang_name] = {
            "functions": language.query(function_patterns),
            "classes": language.query(class_patterns),
            "calls": language.query(call_patterns) if call_patterns else None,
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
        # Ensure file_path is a Path object
        if isinstance(file_path, str):
            file_path = Path(file_path)
        relative_path = file_path.relative_to(self.repo_path)
        relative_path_str = str(relative_path)
        logger.info(f"Parsing {language}: {relative_path_str}")

        try:
            # Check if language is supported
            if language not in self.parsers or language not in self.queries:
                logger.warning(f"Unsupported language '{language}' for {file_path}")
                return

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
            logger.error(f"Failed to parse or ingest {file_path}: {e}")

    def _ingest_top_level_functions(self, root_node, module_qn: str, language: str):
        lang_queries = self.queries[language]
        lang_config = lang_queries["config"]

        captures = lang_queries["functions"].captures(root_node)
        # captures() returns a dict of {capture_name: [Node, ...]}
        func_nodes = captures.get("function", [])
        for func_node in func_nodes:
            # Ensure func_node is a Node object
            if not isinstance(func_node, Node):
                logger.warning(f"Expected Node object but got {type(func_node)}: {func_node}")
                continue
            if self._is_method(func_node, lang_config):
                continue

            name_node = func_node.child_by_field_name("name")
            if not name_node:
                continue
            func_name = name_node.text.decode("utf8")
            func_qn = self._build_nested_qualified_name(
                func_node, module_qn, func_name, lang_config
            )

            if not func_qn:
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

            parent_type, parent_qn = self._determine_function_parent(
                func_node, module_qn, lang_config
            )
            self.ingestor.ensure_relationship_batch(
                (parent_type, "qualified_name", parent_qn),
                "DEFINES",
                ("Function", "qualified_name", func_qn),
            )

            # Ingest calls made by this function
            self._ingest_function_calls(
                func_node, func_qn, "Function", module_qn, language
            )

    def _build_nested_qualified_name(
        self, func_node, module_qn: str, func_name: str, lang_config: LanguageConfig
    ) -> Optional[str]:
        """Build qualified name for nested functions. Returns None for methods."""
        path_parts = []
        current = func_node.parent

        # Add a check to ensure the initial 'current' is a Node object
        if not isinstance(current, Node):
            logger.warning(f"Unexpected parent type for node {func_node}: {type(current)}. Skipping qualified name generation.")
            return None

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    path_parts.append(name_node.text.decode("utf8"))
            elif current.type in lang_config.class_node_types:
                return None  # This is a method, handled separately
            
            # Check if current.parent is a Node before assigning to current
            if hasattr(current, 'parent') and isinstance(current.parent, Node):
                current = current.parent
            else:
                logger.warning(f"Unexpected parent type or missing parent attribute for node type: {current.type} (parent: {getattr(current, 'parent', 'None')}). Stopping traversal.")
                # Instead of returning None, we'll break and try to form a QN with what we have.
                # This might still lead to incomplete QNs but prevents crashing.
                break

        path_parts.reverse()
        if path_parts:
            return f"{module_qn}.{'.'.join(path_parts)}.{func_name}"
        else:
            return f"{module_qn}.{func_name}"

    def _is_method(self, func_node, lang_config: LanguageConfig) -> bool:
        """Check if a function is a method within a class."""
        current = func_node.parent
        # Add a check to ensure the initial 'current' is a Node object
        if not isinstance(current, Node):
            return False
            
        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.class_node_types:
                return True
            # Check if current.parent is a Node before assigning to current
            if hasattr(current, 'parent') and isinstance(current.parent, Node):
                current = current.parent
            else:
                break
        return False

    def _determine_function_parent(
        self, func_node, module_qn: str, lang_config: LanguageConfig
    ) -> tuple[str, str]:
        """Determine the parent for a function (Module or another Function)."""
        current = func_node.parent
        # Add a check to ensure the initial 'current' is a Node object
        if not isinstance(current, Node):
            logger.warning(f"Unexpected parent type for node {func_node}: {type(current)}. Returning Module parent.")
            return "Module", module_qn

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    parent_func_name = name_node.text.decode("utf8")
                    if parent_func_qn := self._build_nested_qualified_name(
                        current, module_qn, parent_func_name, lang_config
                    ):
                        return "Function", parent_func_qn
                break  # Stop at the first function parent
            
            # Check if current.parent is a Node before assigning to current
            if hasattr(current, 'parent') and isinstance(current.parent, Node):
                current = current.parent
            else:
                logger.warning(f"Unexpected parent type or missing parent attribute for node type: {current.type} (parent: {getattr(current, 'parent', 'None')}). Stopping traversal.")
                break # Exit loop if parent is not a Node

        return "Module", module_qn

    def _ingest_classes_and_methods(self, root_node, module_qn: str, language: str):
        lang_queries = self.queries[language]
        lang_config = lang_queries["config"]

        class_captures = lang_queries["classes"].captures(root_node)
        # captures() returns a dict of {capture_name: [Node, ...]}
        class_nodes = class_captures.get("class", [])
        for class_node in class_nodes:
            # Ensure class_node is a Node object
            if not isinstance(class_node, Node):
                logger.warning(f"Expected Node object but got {type(class_node)}: {class_node}")
                continue
            name_node = class_node.child_by_field_name("name")
            if not name_node:
                continue
            class_name = name_node.text.decode("utf8")
            class_qn = f"{module_qn}.{class_name}"
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
                ("Module", "qualified_name", module_qn),
                "DEFINES",
                ("Class", "qualified_name", class_qn),
            )

            body_node = class_node.child_by_field_name("body")
            if not body_node:
                continue

            method_captures = lang_queries["functions"].captures(body_node)
            # captures() returns a dict of {capture_name: [Node, ...]}
            method_nodes = method_captures.get("function", [])
            for method_node in method_nodes:
                # Ensure method_node is a Node object
                if not isinstance(method_node, Node):
                    logger.warning(f"Expected Node object but got {type(method_node)}: {method_node}")
                    continue
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
                logger.info(f"    Found Method: {method_name} (qn: {method_qn})")
                self.ingestor.ensure_node_batch("Method", method_props)
                self.ingestor.ensure_relationship_batch(
                    ("Class", "qualified_name", class_qn),
                    "DEFINES_METHOD",
                    ("Method", "qualified_name", method_qn),
                )
                self._ingest_function_calls(
                    method_node, method_qn, "Method", module_qn, language
                )

    def _get_call_target_name(self, call_node: Node) -> Optional[str]:
        """Extracts the name of the function or method being called."""
        # For 'call' in Python, the function being called is in the 'function' child
        if func_child := call_node.child_by_field_name("function"):
            # Direct call: my_function() -> identifier
            if func_child.type == "identifier":
                return func_child.text.decode("utf8")
            # Attribute access: obj.method() -> attribute
            elif func_child.type == "attribute":
                # The actual name is in the 'attribute' child of the 'attribute' node
                if attr_child := func_child.child_by_field_name("attribute"):
                    return attr_child.text.decode("utf8")
        # For 'call_expression' in JS/TS, the function is in the 'function' child
        if func_child := call_node.child_by_field_name("function"):
            if func_child.type == "identifier":
                return func_child.text.decode("utf8")
            # Member expression: obj.method() -> member_expression
            elif func_child.type == "member_expression":
                if prop_child := func_child.child_by_field_name("property"):
                    return prop_child.text.decode("utf8")
        # For 'method_invocation' in Java
        if name_node := call_node.child_by_field_name("name"):
            return name_node.text.decode("utf8")
        return None

    def _ingest_function_calls(
        self,
        caller_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: str,
    ):
        """Finds all function calls within a function/method and creates CALLS relationships."""
        calls_query = self.queries[language].get("calls")
        if not calls_query:
            return

        call_captures = calls_query.captures(caller_node)
        # captures() returns a dict of {capture_name: [Node, ...]}
        call_nodes = call_captures.get("call", [])
        for call_node in call_nodes:
            # Ensure call_node is a Node object
            if not isinstance(call_node, Node):
                logger.warning(f"Expected Node object but got {type(call_node)}: {call_node}")
                continue
            call_name = self._get_call_target_name(call_node)
            if not call_name:
                continue

            # Simplified resolution: assume the called function is in the same module.
            # A full implementation would need to resolve imports.
            callee_qn = f"{module_qn}.{call_name}"

            logger.debug(
                f"      Found call from {caller_qn} to {call_name} (resolved as {callee_qn})"
            )
            # We don't know if the callee is a Function or Method, so we create a generic relationship
            # that can be resolved later. For now, we assume it's a call to a function-like entity.
            # A more advanced approach would query the DB to find the type of `callee_qn`.
            self.ingestor.ensure_relationship_batch(
                (caller_type, "qualified_name", caller_qn),
                "CALLS",
                ("Function", "qualified_name", callee_qn),  # Assume Function for now
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
