"""
Tree-sitter based processor that properly uses Tree-sitter's query capabilities.
This replaces the hacky language-specific extraction methods with proper query-driven parsing.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Language, Node, Parser, Query, QueryCursor, Tree


@dataclass
class CaptureInfo:
    """Information extracted from a Tree-sitter capture."""

    capture_name: str
    node: Node
    text: str
    metadata: dict[str, Any]


class TreeSitterProcessor:
    """
    Proper Tree-sitter processor that uses queries instead of manual traversal.

    This processor:
    1. Uses Tree-sitter queries to extract semantic information
    2. Avoids language-specific code branches
    3. Leverages Tree-sitter's capture system
    4. Uses field access instead of manual child traversal
    """

    def __init__(self, language: Language, queries: dict[str, str]):
        """
        Initialize with a language and query patterns.

        Args:
            language: Tree-sitter Language object
            queries: Dict of query name to query pattern string
        """
        self.language = language
        self.parser = Parser(language)
        self.compiled_queries = {}

        # Compile all queries
        for name, pattern in queries.items():
            try:
                self.compiled_queries[name] = Query(language, pattern)
            except Exception as e:
                logger.warning(f"Failed to compile query '{name}': {e}")

    def parse_source(self, source_code: bytes) -> Tree:
        """Parse source code into a Tree-sitter AST."""
        return self.parser.parse(source_code)

    def extract_definitions(self, source_code: bytes) -> dict[str, list[CaptureInfo]]:
        """
        Extract all definitions (functions, classes, etc.) using queries.

        Returns:
            Dict mapping definition type to list of captured definitions
        """
        tree = self.parse_source(source_code)
        definitions: dict[str, list[CaptureInfo]] = {
            "functions": [],
            "methods": [],
            "classes": [],
            "structs": [],
            "namespaces": [],
            "modules": [],
        }

        if "definitions" not in self.compiled_queries:
            return definitions

        query = self.compiled_queries["definitions"]
        cursor = QueryCursor()

        for match in cursor.matches(query, tree.root_node):
            for capture in match.captures:
                node = capture[0]
                capture_name = query.capture_names[capture[1]]

                info = self._extract_capture_info(node, capture_name, source_code)

                # Categorize by capture type
                if "function" in capture_name:
                    definitions["functions"].append(info)
                elif "method" in capture_name:
                    definitions["methods"].append(info)
                elif "class" in capture_name:
                    definitions["classes"].append(info)
                elif "struct" in capture_name:
                    definitions["structs"].append(info)
                elif "namespace" in capture_name:
                    definitions["namespaces"].append(info)
                elif "module" in capture_name:
                    definitions["modules"].append(info)

        return definitions

    def extract_calls(self, source_code: bytes) -> list[CaptureInfo]:
        """
        Extract all function/method calls using queries.

        Returns:
            List of captured call sites
        """
        if "calls" not in self.compiled_queries:
            return []

        tree = self.parse_source(source_code)
        query = self.compiled_queries["calls"]
        cursor = QueryCursor()

        calls = []
        for match in cursor.matches(query, tree.root_node):
            for capture in match.captures:
                node = capture[0]
                capture_name = query.capture_names[capture[1]]

                if "call" in capture_name:
                    info = self._extract_capture_info(node, capture_name, source_code)
                    calls.append(info)

        return calls

    def extract_inheritance(self, source_code: bytes) -> list[tuple[str, str]]:
        """
        Extract inheritance relationships using queries.

        Returns:
            List of (derived_class, base_class) tuples
        """
        if "inheritance" not in self.compiled_queries:
            return []

        tree = self.parse_source(source_code)
        query = self.compiled_queries["inheritance"]
        cursor = QueryCursor()

        relationships = []
        current_class = None

        for match in cursor.matches(query, tree.root_node):
            for capture in match.captures:
                node = capture[0]
                capture_name = query.capture_names[capture[1]]

                if capture_name == "class.name":
                    current_class = node.text.decode("utf-8")
                elif capture_name == "inheritance.base" and current_class:
                    base_class = node.text.decode("utf-8")
                    relationships.append((current_class, base_class))

        return relationships

    def extract_imports(self, source_code: bytes) -> list[CaptureInfo]:
        """
        Extract import statements using queries.

        Returns:
            List of captured import statements
        """
        if "imports" not in self.compiled_queries:
            return []

        tree = self.parse_source(source_code)
        query = self.compiled_queries["imports"]
        cursor = QueryCursor()

        imports = []
        for match in cursor.matches(query, tree.root_node):
            for capture in match.captures:
                node = capture[0]
                capture_name = query.capture_names[capture[1]]

                if "import" in capture_name or "include" in capture_name:
                    info = self._extract_capture_info(node, capture_name, source_code)
                    imports.append(info)

        return imports

    def _extract_capture_info(
        self, node: Node, capture_name: str, source_code: bytes
    ) -> CaptureInfo:
        """
        Extract detailed information from a captured node.

        This method uses Tree-sitter's field access instead of manual traversal.
        """
        text = node.text.decode("utf-8") if node.text else ""

        metadata = {
            "start_point": node.start_point,
            "end_point": node.end_point,
            "type": node.type,
        }

        # Extract additional metadata based on node type using field access
        if node.type == "function_definition":
            metadata.update(self._extract_function_metadata(node))
        elif node.type == "class_specifier":
            metadata.update(self._extract_class_metadata(node))
        elif node.type == "call_expression":
            metadata.update(self._extract_call_metadata(node))

        return CaptureInfo(
            capture_name=capture_name, node=node, text=text, metadata=metadata
        )

    def _extract_function_metadata(self, node: Node) -> dict[str, Any]:
        """Extract function-specific metadata using Tree-sitter field access."""
        metadata = {}

        # Use Tree-sitter's field access instead of manual traversal
        declarator = node.child_by_field_name("declarator")
        if declarator:
            # Extract function name from declarator
            if declarator.type == "function_declarator":
                name_node = declarator.child_by_field_name("declarator")
                if name_node:
                    metadata["name"] = (
                        name_node.text.decode("utf-8") if name_node.text else None
                    )

                # Extract parameters
                params_node = declarator.child_by_field_name("parameters")
                if params_node:
                    metadata["parameters"] = (
                        params_node.text.decode("utf-8") if params_node.text else None
                    )

        # Extract return type
        type_node = node.child_by_field_name("type")
        if type_node:
            metadata["return_type"] = (
                type_node.text.decode("utf-8") if type_node.text else None
            )

        # Extract body
        body_node = node.child_by_field_name("body")
        if body_node:
            metadata["has_body"] = True
            metadata["body_start"] = body_node.start_point
            metadata["body_end"] = body_node.end_point

        return metadata

    def _extract_class_metadata(self, node: Node) -> dict[str, Any]:
        """Extract class-specific metadata using Tree-sitter field access."""
        metadata = {}

        # Extract class name
        name_node = node.child_by_field_name("name")
        if name_node:
            metadata["name"] = (
                name_node.text.decode("utf-8") if name_node.text else None
            )

        # Extract body
        body_node = node.child_by_field_name("body")
        if body_node:
            metadata["has_body"] = True

            # Count members
            member_count = 0
            method_count = 0

            for child in body_node.children:
                if child.type == "field_declaration":
                    member_count += 1
                elif child.type in ["function_definition", "declaration"]:
                    method_count += 1

            metadata["member_count"] = member_count
            metadata["method_count"] = method_count

        # Check for base classes
        for child in node.children:
            if child.type == "base_class_clause":
                metadata["has_inheritance"] = True
                break

        return metadata

    def _extract_call_metadata(self, node: Node) -> dict[str, Any]:
        """Extract call-specific metadata using Tree-sitter field access."""
        metadata = {}

        # Extract function/method being called
        function_node = node.child_by_field_name("function")
        if function_node:
            if function_node.type == "identifier":
                metadata["call_type"] = "function"
                metadata["function_name"] = (
                    function_node.text.decode("utf-8") if function_node.text else ""
                )
            elif function_node.type == "field_expression":
                metadata["call_type"] = "method"
                object_node = function_node.child_by_field_name("object")
                field_node = function_node.child_by_field_name("field")
                if object_node:
                    metadata["object"] = (
                        object_node.text.decode("utf-8") if object_node.text else ""
                    )
                if field_node:
                    metadata["method_name"] = (
                        field_node.text.decode("utf-8") if field_node.text else ""
                    )
            elif function_node.type == "qualified_identifier":
                metadata["call_type"] = "qualified"
                metadata["qualified_name"] = (
                    function_node.text.decode("utf-8") if function_node.text else ""
                )

        # Extract arguments
        args_node = node.child_by_field_name("arguments")
        if args_node:
            metadata["has_arguments"] = "true"
            metadata["argument_count"] = str(
                len(
                    [
                        child
                        for child in args_node.children
                        if child.type not in ["(", ")", ","]
                    ]
                )
            )

        return metadata


def load_query_file(query_path: Path) -> str:
    """Load a Tree-sitter query from a .scm file."""
    if not query_path.exists():
        raise FileNotFoundError(f"Query file not found: {query_path}")

    return query_path.read_text()


def create_cpp_processor() -> TreeSitterProcessor:
    """
    Create a Tree-sitter processor for C++ with comprehensive queries.

    This demonstrates how to properly use Tree-sitter for C++ parsing
    without any language-specific hacks.
    """
    from tree_sitter_cpp import language as cpp_language

    # Load comprehensive C++ queries
    queries_dir = Path(__file__).parent.parent / "queries"
    cpp_query_file = queries_dir / "cpp_queries.scm"

    if cpp_query_file.exists():
        cpp_queries = load_query_file(cpp_query_file)
    else:
        # Fallback to embedded queries
        cpp_queries = """
        ; Functions
        (function_definition) @definition.function

        ; Classes
        (class_specifier) @definition.class
        (struct_specifier) @definition.struct

        ; Calls
        (call_expression) @call
        """

    return TreeSitterProcessor(
        language=Language(cpp_language()),
        queries={"definitions": cpp_queries, "calls": cpp_queries},
    )
