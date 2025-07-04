import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import toml
from loguru import logger
from tree_sitter import Language, Node, Parser

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
    LanguageConfig,
    get_language_config,
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
        self.structural_elements: dict[Path, str | None] = {}
        self.function_registry: dict[str, str] = {}
        self.simple_name_lookup: dict[str, set[str]] = defaultdict(set)
        self.ast_cache: dict[Path, tuple[Node, str]] = {}
        self.ignore_dirs = {
            ".git", "venv", ".venv", "__pycache__", "node_modules",
            "build", "dist", ".eggs",
        }
        self.parsers: dict[str, Parser] = {}
        self.languages: dict[str, Language] = {}
        self.lang_configs: dict[str, LanguageConfig] = {}
        self.queries: dict[str, dict[str, Any]] = {}
        self._initialize_languages()

    def _initialize_languages(self) -> None:
        """Initialize Tree-sitter parsers and language configs."""
        from .language_config import LANGUAGE_CONFIGS

        available_languages = []
        for lang_name, lang_config in LANGUAGE_CONFIGS.items():
            lang_lib = LANGUAGE_LIBRARIES.get(lang_name)
            if lang_lib:
                try:
                    parser = Parser()
                    language = Language(lang_lib())
                    parser.language = language
                    self.parsers[lang_name] = parser
                    self.languages[lang_name] = language
                    self.lang_configs[lang_name] = lang_config
                    available_languages.append(lang_name)
                    logger.success(f"Successfully loaded {lang_name} grammar.")
                except Exception as e:
                    logger.warning(f"Failed to load {lang_name} grammar: {e}")
            else:
                logger.debug(f"Tree-sitter library for {lang_name} not available.")
        if not available_languages:
            raise RuntimeError(
                "No Tree-sitter languages available. Please install packages."
            )
        logger.info(f"Initialized parsers for: {', '.join(available_languages)}")

    def _get_queries(self, language: str) -> dict[str, Any]:
        """Compile and cache Tree-sitter queries for a language on demand."""
        if language in self.queries:
            return self.queries[language]

        lang_obj = self.languages[language]
        lang_config = self.lang_configs[language]

        function_patterns = " ".join(f"({nt}) @function" for nt in lang_config.function_node_types)
        class_patterns = " ".join(f"({nt}) @class" for nt in lang_config.class_node_types)
        call_patterns = " ".join(f"({nt}) @call" for nt in lang_config.call_node_types)

        compiled_queries = {
            "functions": lang_obj.query(function_patterns),
            "classes": lang_obj.query(class_patterns),
            "calls": lang_obj.query(call_patterns) if call_patterns else None,
            "config": lang_config,
        }
        self.queries[language] = compiled_queries
        logger.debug(f"Lazily compiled queries for {language}.")
        return compiled_queries

    def run(self) -> None:
        """Orchestrates the parsing and ingestion process."""
        self.ingestor.ensure_node_batch("Project", {"name": self.project_name})
        logger.info(f"Ensuring Project: {self.project_name}")

        logger.info("--- Pass 1: Processing repository structure and files ---")
        self._process_repository()

        logger.info(f"\n--- Found {len(self.function_registry)} functions/methods ---")
        logger.info("--- Pass 2: Processing Function Calls from AST Cache ---")
        self._process_function_calls()

        logger.info("\n--- Analysis complete. Flushing all data to database... ---")
        self.ingestor.flush_all()

    def _process_repository(self) -> None:
        """Single pass to find and process all elements in the repository."""
        for root_str, dirs, files in os.walk(self.repo_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]
            root = Path(root_str)
            relative_root = root.relative_to(self.repo_path)
            parent_rel_path = relative_root.parent
            parent_container_qn = self.structural_elements.get(parent_rel_path)

            parent_label, parent_key, parent_val = (
                ("Project", "name", self.project_name) if root == self.repo_path else
                ("Package", "qualified_name", parent_container_qn) if parent_container_qn else
                ("Folder", "path", str(parent_rel_path))
            )

            current_container_label, current_container_key, current_container_val = (
                self._process_directory(root, relative_root, parent_label, parent_key, parent_val)
                if root != self.repo_path else
                (parent_label, parent_key, parent_val)
            )

            self._process_files_in_dir(files, root, current_container_label, current_container_key, current_container_val)

    def _process_directory(self, root: Path, relative_root: Path, parent_label: str, parent_key: str, parent_val: Any) -> tuple[str, str, Any]:
        """Process a single directory to determine if it's a Package or Folder."""
        package_indicators = {ind for conf in self.lang_configs.values() for ind in conf.package_indicators}
        is_package = any((root / indicator).exists() for indicator in package_indicators)

        if is_package:
            package_qn = ".".join([self.project_name] + list(relative_root.parts))
            self.structural_elements[relative_root] = package_qn
            logger.info(f"  Identified Package: {package_qn}")
            props = {"qualified_name": package_qn, "name": root.name, "path": str(relative_root)}
            self.ingestor.ensure_node_batch("Package", props)
            self.ingestor.ensure_relationship_batch((parent_label, parent_key, parent_val), "CONTAINS_PACKAGE", ("Package", "qualified_name", package_qn))
            return "Package", "qualified_name", package_qn
        else:
            self.structural_elements[relative_root] = None
            folder_path_str = str(relative_root)
            logger.info(f"  Identified Folder: '{folder_path_str}'")
            props = {"path": folder_path_str, "name": root.name}
            self.ingestor.ensure_node_batch("Folder", props)
            self.ingestor.ensure_relationship_batch((parent_label, parent_key, parent_val), "CONTAINS_FOLDER", ("Folder", "path", folder_path_str))
            return "Folder", "path", folder_path_str

    def _process_files_in_dir(self, files: list[str], root: Path, container_label: str, container_key: str, container_val: Any) -> None:
        """Process all files within a given directory."""
        for file_name in files:
            filepath = root / file_name
            relative_filepath = str(filepath.relative_to(self.repo_path))
            self.ingestor.ensure_node_batch("File", {"path": relative_filepath, "name": file_name, "extension": filepath.suffix})
            self.ingestor.ensure_relationship_batch((container_label, container_key, container_val), "CONTAINS_FILE", ("File", "path", relative_filepath))
            lang_config = get_language_config(filepath.suffix)
            if lang_config and lang_config.name in self.parsers:
                self.parse_and_ingest_file(filepath, lang_config.name)
            elif file_name == "pyproject.toml":
                self._parse_dependencies(filepath)

    def _get_docstring(self, node: Node) -> str | None:
        """Extracts the docstring from a function or class node's body."""
        body_node = node.child_by_field_name("body")
        if not (body_node and body_node.children):
            return None

        first_statement = body_node.children[0]
        if first_statement.type != "expression_statement" or not first_statement.children:
            return None

        string_node = first_statement.children[0]
        if string_node.type == "string":
            return string_node.text.decode("utf-8").strip("'\" \n")

        return None

    def parse_and_ingest_file(self, file_path: Path, language: str) -> None:
        """Parses a file, ingests its structure, and caches the AST."""
        relative_path = file_path.relative_to(self.repo_path)
        logger.info(f"Parsing and Caching AST for {language}: {relative_path}")
        try:
            source_bytes = file_path.read_bytes()
            parser = self.parsers[language]
            tree = parser.parse(source_bytes)
            root_node = tree.root_node
            self.ast_cache[file_path] = (root_node, language)
            module_qn = ".".join([self.project_name] + list(relative_path.with_suffix("").parts))
            if file_path.name == "__init__.py":
                module_qn = ".".join([self.project_name] + list(relative_path.parent.parts))
            self.ingestor.ensure_node_batch("Module", {"qualified_name": module_qn, "name": file_path.name, "path": str(relative_path)})
            parent_rel_path = relative_path.parent
            parent_container_qn = self.structural_elements.get(parent_rel_path)
            parent_label, parent_key, parent_val = (
                ("Package", "qualified_name", parent_container_qn) if parent_container_qn else
                ("Folder", "path", str(parent_rel_path)) if parent_rel_path != Path(".") else
                ("Project", "name", self.project_name)
            )
            self.ingestor.ensure_relationship_batch((parent_label, parent_key, parent_val), "CONTAINS_MODULE", ("Module", "qualified_name", module_qn))
            self._ingest_top_level_functions(root_node, module_qn, language)
            self._ingest_classes_and_methods(root_node, module_qn, language)
        except Exception as e:
            logger.error(f"Failed to parse or ingest {file_path}: {e}")

    def _ingest_top_level_functions(self, root_node: Node, module_qn: str, language: str) -> None:
        lang_queries = self._get_queries(language)
        lang_config = lang_queries["config"]
        raw_captures = lang_queries["functions"].captures(root_node)

        # Process captures into a dictionary
        captures = defaultdict(list)
        for capture in raw_captures:
            node, name = capture[0], capture[1]
            captures[name].append(node)

        for func_node in captures.get("function", []):
            if self._is_method(func_node, lang_config):
                continue
            name_node = func_node.child_by_field_name("name")
            if not name_node: continue
            func_name = name_node.text.decode("utf8")
            func_qn = self._build_nested_qualified_name(func_node, module_qn, func_name, lang_config)
            if not func_qn: continue
            props = {"qualified_name": func_qn, "name": func_name, "decorators": [], "start_line": func_node.start_point[0] + 1, "end_line": func_node.end_point[0] + 1, "docstring": self._get_docstring(func_node)}
            logger.info(f"  Found Function: {func_name} (qn: {func_qn})")
            self.ingestor.ensure_node_batch("Function", props)
            self.function_registry[func_qn] = "Function"
            self.simple_name_lookup[func_name].add(func_qn)
            parent_type, parent_qn = self._determine_function_parent(func_node, module_qn, lang_config)
            self.ingestor.ensure_relationship_batch((parent_type, "qualified_name", parent_qn), "DEFINES", ("Function", "qualified_name", func_qn))

    def _build_nested_qualified_name(self, node: Node, module_qn: str, name: str, lang_config: LanguageConfig) -> str | None:
        path_parts = []
        current = node.parent
        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    path_parts.append(name_node.text.decode("utf8"))
            elif current.type in lang_config.class_node_types:
                return None
            current = current.parent
        path_parts.reverse()
        return f"{module_qn}.{'.'.join(path_parts)}.{name}" if path_parts else f"{module_qn}.{name}"

    def _is_method(self, node: Node, lang_config: LanguageConfig) -> bool:
        current = node.parent
        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.class_node_types:
                return True
            current = current.parent
        return False

    def _determine_function_parent(self, node: Node, module_qn: str, lang_config: LanguageConfig) -> tuple[str, str]:
        current = node.parent
        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    parent_name = name_node.text.decode("utf8")
                    if parent_qn := self._build_nested_qualified_name(current, module_qn, parent_name, lang_config):
                        return "Function", parent_qn
                break
            current = current.parent
        return "Module", module_qn

    def _ingest_classes_and_methods(self, root_node: Node, module_qn: str, language: str) -> None:
        lang_queries = self._get_queries(language)
        raw_class_captures = lang_queries["classes"].captures(root_node)

        class_captures = defaultdict(list)
        for capture in raw_class_captures:
            node, name = capture[0], capture[1]
            class_captures[name].append(node)

        for class_node in class_captures.get("class", []):
            name_node = class_node.child_by_field_name("name")
            if not name_node: continue
            class_name = name_node.text.decode("utf8")
            class_qn = f"{module_qn}.{class_name}"
            props = {"qualified_name": class_qn, "name": class_name, "decorators": [], "start_line": class_node.start_point[0] + 1, "end_line": class_node.end_point[0] + 1, "docstring": self._get_docstring(class_node)}
            logger.info(f"  Found Class: {class_name} (qn: {class_qn})")
            self.ingestor.ensure_node_batch("Class", props)
            self.ingestor.ensure_relationship_batch(("Module", "qualified_name", module_qn), "DEFINES", ("Class", "qualified_name", class_qn))
            body_node = class_node.child_by_field_name("body")
            if not body_node: continue

            raw_method_captures = lang_queries["functions"].captures(body_node)
            method_captures = defaultdict(list)
            for capture in raw_method_captures:
                node, name = capture[0], capture[1]
                method_captures[name].append(node)

            for method_node in method_captures.get("function", []):
                method_name_node = method_node.child_by_field_name("name")
                if not method_name_node: continue
                method_name = method_name_node.text.decode("utf8")
                method_qn = f"{class_qn}.{method_name}"
                method_props = {"qualified_name": method_qn, "name": method_name, "decorators": [], "start_line": method_node.start_point[0] + 1, "end_line": method_node.end_point[0] + 1, "docstring": self._get_docstring(method_node)}
                logger.info(f"    Found Method: {method_name} (qn: {method_qn})")
                self.ingestor.ensure_node_batch("Method", method_props)
                self.function_registry[method_qn] = "Method"
                self.simple_name_lookup[method_name].add(method_qn)
                self.ingestor.ensure_relationship_batch(("Class", "qualified_name", class_qn), "DEFINES_METHOD", ("Method", "qualified_name", method_qn))

    def _parse_dependencies(self, filepath: Path) -> None:
        logger.info(f"  Parsing pyproject.toml: {filepath}")
        try:
            data = toml.load(filepath)
            deps = (data.get("tool", {}).get("poetry", {}).get("dependencies", {})) or {
                dep.split(">=")[0].split("==")[0].strip(): dep for dep in data.get("project", {}).get("dependencies", [])
            }
            for dep_name, dep_spec in deps.items():
                if dep_name.lower() == "python": continue
                logger.info(f"    Found dependency: {dep_name} (spec: {dep_spec})")
                self.ingestor.ensure_node_batch("ExternalPackage", {"name": dep_name})
                self.ingestor.ensure_relationship_batch(("Project", "name", self.project_name), "DEPENDS_ON_EXTERNAL", ("ExternalPackage", "name", dep_name), properties={"version_spec": str(dep_spec)})
        except Exception as e:
            logger.error(f"    Error parsing {filepath}: {e}")

    def _process_function_calls(self) -> None:
        """Third pass: Process function calls using the cached ASTs."""
        for file_path, (root_node, language) in self.ast_cache.items():
            self._process_calls_in_file(file_path, root_node, language)

    def _process_calls_in_file(self, file_path: Path, root_node: Node, language: str) -> None:
        """Process function calls in a specific file using its cached AST."""
        relative_path = file_path.relative_to(self.repo_path)
        logger.debug(f"Processing calls in cached AST for: {relative_path}")
        try:
            module_qn = ".".join([self.project_name] + list(relative_path.with_suffix("").parts))
            if file_path.name == "__init__.py":
                module_qn = ".".join([self.project_name] + list(relative_path.parent.parts))
            self._process_calls_in_functions(root_node, module_qn, language)
            self._process_calls_in_classes(root_node, module_qn, language)
        except Exception as e:
            logger.error(f"Failed to process calls in {file_path}: {e}")

    def _process_calls_in_functions(self, root_node: Node, module_qn: str, language: str) -> None:
        lang_queries = self._get_queries(language)
        lang_config = lang_queries["config"]
        raw_captures = lang_queries["functions"].captures(root_node)

        captures = defaultdict(list)
        for capture in raw_captures:
            node, name = capture[0], capture[1]
            captures[name].append(node)

        for func_node in captures.get("function", []):
            if self._is_method(func_node, lang_config): continue
            name_node = func_node.child_by_field_name("name")
            if not name_node: continue
            func_name = name_node.text.decode("utf8")
            func_qn = self._build_nested_qualified_name(func_node, module_qn, func_name, lang_config)
            if func_qn:
                self._ingest_function_calls(func_node, func_qn, "Function", module_qn, language)

    def _process_calls_in_classes(self, root_node: Node, module_qn: str, language: str) -> None:
        lang_queries = self._get_queries(language)
        raw_captures = lang_queries["classes"].captures(root_node)

        captures = defaultdict(list)
        for capture in raw_captures:
            node, name = capture[0], capture[1]
            captures[name].append(node)

        for class_node in captures.get("class", []):
            name_node = class_node.child_by_field_name("name")
            if not name_node: continue
            class_name = name_node.text.decode("utf8")
            class_qn = f"{module_qn}.{class_name}"
            body_node = class_node.child_by_field_name("body")
            if not body_node: continue

            raw_method_captures = lang_queries["functions"].captures(body_node)
            method_captures = defaultdict(list)
            for capture in raw_method_captures:
                node, name = capture[0], capture[1]
                method_captures[name].append(node)

            for method_node in method_captures.get("function", []):
                method_name_node = method_node.child_by_field_name("name")
                if not method_name_node: continue
                method_name = method_name_node.text.decode("utf8")
                method_qn = f"{class_qn}.{method_name}"
                self._ingest_function_calls(method_node, method_qn, "Method", module_qn, language)

    def _get_call_target_name(self, call_node: Node) -> str | None:
        """Extracts the name of the function or method being called."""
        target_node = call_node.child_by_field_name("function")
        if not target_node:
            target_node = call_node.child_by_field_name("name")
        if not target_node:
            return None

        if target_node.type == "identifier":
            return target_node.text.decode("utf8")
        elif target_node.type == "attribute":
            return target_node.child_by_field_name("attribute").text.decode("utf8")
        elif target_node.type == "member_expression":
            return target_node.child_by_field_name("property").text.decode("utf8")
        return None

    def _ingest_function_calls(self, caller_node: Node, caller_qn: str, caller_type: str, module_qn: str, language: str) -> None:
        calls_query = self._get_queries(language).get("calls")
        if not calls_query: return
        raw_captures = calls_query.captures(caller_node)
        
        captures = defaultdict(list)
        for capture in raw_captures:
            node, name = capture[0], capture[1]
            captures[name].append(node)
            
        for call_node in captures.get("call", []):
            call_name = self._get_call_target_name(call_node)
            if not call_name: continue
            callee_info = self._resolve_function_call(call_name, module_qn)
            if not callee_info: continue
            callee_type, callee_qn = callee_info
            logger.debug(f"      Found call from {caller_qn} to {call_name} (resolved as {callee_type}:{callee_qn})")
            self.ingestor.ensure_relationship_batch((caller_type, "qualified_name", caller_qn), "CALLS", (callee_type, "qualified_name", callee_qn))

    def _resolve_function_call(self, call_name: str, module_qn: str) -> tuple[str, str] | None:
        """Resolve a function call to its qualified name."""
        possible_qns = [
            f"{module_qn}.{call_name}",
            f"{self.project_name}.{call_name}",
        ]
        if "." in module_qn:
            possible_qns.append(f"{'.'.join(module_qn.split('.')[:-1])}.{call_name}")

        for qn in possible_qns:
            if qn in self.function_registry:
                return self.function_registry[qn], qn

        if call_name in self.simple_name_lookup:
            for registered_qn in self.simple_name_lookup[call_name]:
                if self._is_likely_same_function(call_name, registered_qn, module_qn):
                    return self.function_registry[registered_qn], registered_qn
        return None

    def _is_likely_same_function(self, call_name: str, registered_qn: str, caller_module_qn: str) -> bool:
        """A heuristic to resolve function calls with the same simple name."""
        if len(call_name) > 10 or "_" in call_name:
            return True
        caller_parts = caller_module_qn.split(".")
        registered_parts = registered_qn.split(".")
        return len(caller_parts) >= 2 and len(registered_parts) >= 2 and caller_parts[:2] == registered_parts[:2]
