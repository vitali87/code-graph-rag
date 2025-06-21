import ast
import os
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from pydantic_ai import Tool, RunContext
from loguru import logger
from ..schemas import CodeSnippet


class CodeRetriever:
    """Service to retrieve code snippets from the filesystem."""

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        self._file_index: Dict[str, Path] = {}
        self._build_file_index()
        logger.info(f"CodeRetriever initialized with root: {self.project_root}")

    def _build_file_index(self):
        """Build an index of qualified names to file paths for faster lookup."""
        logger.info("[CodeRetriever] Building file index...")
        python_files = self._find_python_files()
        project_name = self.project_root.name  # Get the project name from directory

        for file_path in python_files:
            try:
                # Convert file path to potential module qualified name
                rel_path = file_path.relative_to(self.project_root)
                module_parts = list(rel_path.with_suffix("").parts)

                # Handle __init__.py files
                if rel_path.name == "__init__.py":
                    if len(module_parts) > 1:
                        module_qn = ".".join(module_parts[:-1])  # Remove __init__
                    else:
                        module_qn = module_parts[0] if module_parts else ""
                else:
                    module_qn = ".".join(module_parts)

                if module_qn:
                    # Index both with and without project name prefix
                    self._file_index[module_qn] = file_path
                    # Also index with project name prefix (as used in graph)
                    full_qn = f"{project_name}.{module_qn}"
                    self._file_index[full_qn] = file_path

            except Exception as e:
                logger.debug(f"Error indexing {file_path}: {e}")

        logger.info(f"[CodeRetriever] Indexed {len(self._file_index)} modules")

    def _find_python_files(self) -> List[Path]:
        """Find all Python files in the project."""
        python_files = []
        for root, dirs, files in os.walk(self.project_root):
            # Skip common directories
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d not in ["__pycache__", "node_modules", "venv", ".venv"]
            ]

            python_files.extend(
                Path(root) / file for file in files if file.endswith(".py")
            )
        return python_files

    def _find_target_file(self, qualified_name: str) -> Optional[Path]:
        """Find the file that should contain the qualified name."""
        parts = qualified_name.split(".")

        # Try different combinations to find the right module
        for i in range(len(parts), 0, -1):
            module_candidate = ".".join(parts[:i])
            if module_candidate in self._file_index:
                return self._file_index[module_candidate]

        return None

    def _parse_file_and_find_node(
        self, file_path: Path, qualified_name: str
    ) -> Optional[Tuple[ast.AST, List[str]]]:
        """Parse a file and find the target AST node."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()
                source_lines = source.split("\n")

            # Handle syntax warnings by using safe parsing
            try:
                tree = ast.parse(source, filename=str(file_path))
            except SyntaxError as e:
                logger.debug(f"Syntax error in {file_path}: {e}")
                return None

            # Walk through the AST looking for the target
            for node in ast.walk(tree):
                if self._node_matches_qualified_name(
                    node, file_path, qualified_name, tree
                ):
                    return node, source_lines

            return None

        except Exception as e:
            logger.debug(f"Error parsing {file_path}: {e}")
            return None

    def _node_matches_qualified_name(
        self, node: ast.AST, file_path: Path, qualified_name: str, tree: ast.AST
    ) -> bool:
        """Check if an AST node matches the qualified name."""
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False

        # Get the expected qualified name for this node
        node_qn = self._get_qualified_name_for_node(node, file_path, tree)
        return node_qn == qualified_name

    def _get_qualified_name_for_node(
        self, node: ast.AST, file_path: Path, tree: ast.AST
    ) -> Optional[str]:
        """Get the qualified name for a specific AST node."""
        rel_path = file_path.relative_to(self.project_root)
        module_parts = list(rel_path.with_suffix("").parts)
        project_name = self.project_root.name

        # Handle __init__.py files
        if rel_path.name == "__init__.py" and len(module_parts) > 1:
            module_parts = module_parts[:-1]

        if isinstance(node, ast.ClassDef):
            # Return with project name prefix to match graph format
            return ".".join([project_name] + module_parts + [node.name])

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Check if it's a method (inside a class)
            class_node = self._find_parent_class(node, tree)
            if class_node:
                return ".".join(
                    [project_name] + module_parts + [class_node.name, node.name]
                )
            else:
                return ".".join([project_name] + module_parts + [node.name])

        return None

    def _find_parent_class(
        self, target_node: ast.AST, tree: ast.AST
    ) -> Optional[ast.ClassDef]:
        """Find the parent class of a node, if any."""
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and target_node in ast.walk(node):
                for child in node.body:
                    if child == target_node:
                        return node
        return None

    def _extract_node_info(
        self, node: ast.AST, source_lines: List[str]
    ) -> Dict[str, Any]:
        """Extract information from an AST node."""
        line_start = node.lineno
        line_end = getattr(node, "end_lineno", node.lineno)

        # Extract source code
        if line_end and line_start:
            code_lines = source_lines[line_start - 1 : line_end]
            source_code = "\n".join(code_lines)
        else:
            source_code = ""

        # Extract docstring
        docstring = None
        if (
            hasattr(node, "body")
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            docstring = node.body[0].value.value

        return {
            "line_start": line_start,
            "line_end": line_end,
            "source_code": source_code,
            "docstring": docstring,
        }

    async def find_code_snippet(self, qualified_name: str) -> CodeSnippet:
        """Find code snippet for a given qualified name."""
        logger.info(f"[CodeRetriever] Searching for: {qualified_name}")

        try:
            # Parse the qualified name
            parts = qualified_name.split(".")
            if len(parts) < 2:
                return CodeSnippet(
                    qualified_name=qualified_name,
                    source_code="",
                    file_path="",
                    line_start=0,
                    line_end=0,
                    found=False,
                    error_message="Invalid qualified name format",
                )

            # Find the target file using our index
            target_file = self._find_target_file(qualified_name)

            if not target_file:
                # Check if this looks like an external library
                project_name = self.project_root.name
                if parts[0] != project_name:
                    error_msg = f"'{parts[0]}' appears to be an external library (expected '{project_name}')"
                else:
                    error_msg = "Module file not found"

                return CodeSnippet(
                    qualified_name=qualified_name,
                    source_code="",
                    file_path="",
                    line_start=0,
                    line_end=0,
                    found=False,
                    error_message=error_msg,
                )

            # Parse the file and find the target node
            result = self._parse_file_and_find_node(target_file, qualified_name)

            if not result:
                return CodeSnippet(
                    qualified_name=qualified_name,
                    source_code="",
                    file_path=str(target_file),
                    line_start=0,
                    line_end=0,
                    found=False,
                    error_message="Function/class not found in expected file",
                )

            node, source_lines = result
            info = self._extract_node_info(node, source_lines)

            logger.info(
                f"[CodeRetriever] Found code for {qualified_name} at {target_file}:{info['line_start']}"
            )

            return CodeSnippet(
                qualified_name=qualified_name,
                source_code=info["source_code"],
                file_path=str(target_file),
                line_start=info["line_start"],
                line_end=info["line_end"],
                docstring=info["docstring"],
            )

        except Exception as e:
            logger.error(f"[CodeRetriever] Error: {e}")
            return CodeSnippet(
                qualified_name=qualified_name,
                source_code="",
                file_path="",
                line_start=0,
                line_end=0,
                found=False,
                error_message=str(e),
            )


def create_code_retrieval_tool(code_retriever: CodeRetriever) -> Tool:
    """Factory function to create the code snippet retrieval tool."""

    async def get_code_snippet(ctx: RunContext, qualified_name: str) -> CodeSnippet:
        """
        Retrieves the actual source code for a qualified name.
        Use this after finding relevant functions/methods/classes from the graph.
        """
        logger.info(f"[Tool:GetCode] Retrieving code for: {qualified_name}")
        return await code_retriever.find_code_snippet(qualified_name)

    return Tool(
        function=get_code_snippet,
        description="Retrieves the actual source code for a qualified name (e.g., 'module.Class.method'). Use this after finding relevant items from the knowledge graph.",
    )
