#!/usr/bin/env python3
"""
Analyzes a Python repository's structure, including packages, modules,
classes, and functions, and ingests this data into a Memgraph database.
"""

import os
import ast
import argparse
from pathlib import Path
from typing import Any, Optional

import mgclient
import toml
from loguru import logger

MEMGRAPH_HOST = "localhost"
MEMGRAPH_PORT = 7687


def _get_decorator_name(decorator: ast.expr) -> Optional[str]:
    """Recursively unwraps decorator nodes to get their name."""
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Attribute):
        # Handles chained attributes like 'app.task' -> 'task'
        return decorator.attr
    if isinstance(decorator, ast.Call):
        # Handles decorators with arguments, e.g., @app.task(retries=3)
        return _get_decorator_name(decorator.func)
    return None


class CodeVisitor(ast.NodeVisitor):
    """
    A visitor that traverses the AST, decorating each node with its parent and
    the qualified name of its current scope.
    """

    def __init__(self, module_qn: str):
        self.module_qn = module_qn
        self.scope_stack: list[tuple[str, str]] = [("module", module_qn)]

    def visit(self, node: ast.AST) -> None:
        """Assign parent and scope attributes before visiting children."""
        for child in ast.iter_child_nodes(node):
            child.parent = node
        node.scope = self.scope_stack[-1]

        is_scope_node = isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        if is_scope_node:
            scope_type = "class" if isinstance(node, ast.ClassDef) else "function"
            qualified_name = f"{self.scope_stack[-1][1]}.{node.name}"
            self.scope_stack.append((scope_type, qualified_name))

        super().generic_visit(node)

        if is_scope_node:
            self.scope_stack.pop()


class MemgraphIngestor:
    """Handles all communication and query execution with the Memgraph database."""

    def __init__(self, host: str, port: int, batch_size: int = 1000):
        self._host = host
        self._port = port
        self.batch_size = batch_size
        self.conn: Optional[mgclient.Connection] = None
        self.node_buffer: list[tuple[str, dict[str, Any]]] = []
        self.relationship_buffer: list[tuple[tuple, str, tuple, Optional[dict]]] = []

    def __enter__(self):
        logger.info(f"Connecting to Memgraph at {self._host}:{self._port}...")
        self.conn = mgclient.connect(host=self._host, port=self._port)
        self.conn.autocommit = True
        logger.info("Successfully connected to Memgraph.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            logger.error(
                f"An exception occurred: {exc_val}. Flushing remaining items..."
            )

        self.flush_all()

        if self.conn:
            self.conn.close()
            logger.info("\nDisconnected from Memgraph.")

    def _execute_query(
        self, query: str, params: Optional[dict[str, Any]] = None
    ) -> None:
        if not self.conn:
            raise ConnectionError("Not connected to Memgraph.")

        params = params or {}
        cursor = None
        try:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
        except Exception as e:
            if (
                "already exists" not in str(e).lower()
                and "constraint" not in str(e).lower()
            ):
                logger.error(f"!!! Cypher Error: {e}")
                logger.error(f"    Query: {query}")
                logger.error(f"    Params: {params}")
        finally:
            if cursor:
                cursor.close()

    def _execute_batch(self, query: str, params_list: list[dict[str, Any]]) -> None:
        """Execute a batch query with multiple parameter sets."""
        if not self.conn or not params_list:
            return

        cursor = None
        try:
            cursor = self.conn.cursor()
            # Use UNWIND for batch operations
            batch_query = f"UNWIND $batch AS row\n{query}"
            cursor.execute(batch_query, {"batch": params_list})
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.error(f"!!! Batch Cypher Error: {e}")
                # logger.error(f"    Query: {batch_query}") # Can be noisy
        finally:
            if cursor:
                cursor.close()

    def clean_database(self) -> None:
        logger.info("--- Cleaning database... ---")
        self._execute_query("MATCH (n) DETACH DELETE n;")
        logger.info("--- Database cleaned. ---")

    def ensure_constraints(self) -> None:
        logger.info("Ensuring constraints...")
        constraints = {
            "Project": "name",
            "Package": "qualified_name",
            "Folder": "path",
            "Module": "qualified_name",
            "Class": "qualified_name",
            "Function": "qualified_name",
            "Method": "qualified_name",
            "File": "path",
            "ExternalPackage": "name",
        }
        for label, prop in constraints.items():
            self._execute_query(
                f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"
            )
        logger.info("Constraints checked/created.")

    def ensure_node_batch(self, label: str, properties: dict[str, Any]) -> None:
        """Buffer node creation for batch processing."""
        self.node_buffer.append((label, properties))
        if len(self.node_buffer) >= self.batch_size:
            self.flush_nodes()

    def ensure_relationship_batch(
        self,
        from_node: tuple,
        rel_type: str,
        to_node: tuple,
        properties: Optional[dict[str, Any]] = None,
    ) -> None:
        """Buffer relationship creation for batch processing."""
        self.relationship_buffer.append((from_node, rel_type, to_node, properties))
        if len(self.relationship_buffer) >= self.batch_size:
            self.flush_relationships()

    def flush_nodes(self) -> None:
        """Process all buffered nodes in batches by label."""
        if not self.node_buffer:
            return

        from collections import defaultdict

        nodes_by_label = defaultdict(list)
        for label, props in self.node_buffer:
            nodes_by_label[label].append(props)

        for label, props_list in nodes_by_label.items():
            if not props_list:
                continue

            id_key = next(iter(props_list[0]))
            prop_keys = list(props_list[0].keys())

            set_clause = ", ".join([f"n.{key} = row.{key}" for key in prop_keys])

            query = (
                f"MERGE (n:{label} {{{id_key}: row.{id_key}}}) "
                f"ON CREATE SET {set_clause} ON MATCH SET {set_clause}"
            )
            self._execute_batch(query, props_list)

        logger.info(f"Flushed {len(self.node_buffer)} nodes.")
        self.node_buffer.clear()

    def flush_relationships(self) -> None:
        """Process all buffered relationships in batches."""
        if not self.relationship_buffer:
            return

        from collections import defaultdict

        rels_by_pattern = defaultdict(list)

        for from_node, rel_type, to_node, props in self.relationship_buffer:
            # from_node: (label, key, val)
            pattern = (from_node[0], from_node[1], rel_type, to_node[0], to_node[1])
            rels_by_pattern[pattern].append(
                {"from_val": from_node[2], "to_val": to_node[2], "props": props or {}}
            )

        for pattern, params_list in rels_by_pattern.items():
            from_label, from_key, rel_type, to_label, to_key = pattern

            query = (
                f"MATCH (a:{from_label} {{{from_key}: row.from_val}}), "
                f"(b:{to_label} {{{to_key}: row.to_val}})\n"
                f"MERGE (a)-[r:{rel_type}]->(b)"
            )

            # Only add SET clause if there are properties to set in this batch
            if any(p["props"] for p in params_list):
                query += "\nSET r += row.props"

            self._execute_batch(query, params_list)

        logger.info(f"Flushed {len(self.relationship_buffer)} relationships.")
        self.relationship_buffer.clear()

    def flush_all(self) -> None:
        """Flush all pending operations."""
        logger.info("--- Flushing all pending writes to database... ---")
        self.flush_nodes()
        self.flush_relationships()
        logger.info("--- Flushing complete. ---")


class RepositoryParser:
    """Parses a Python repository and uses an ingestor to save the data."""

    def __init__(self, repo_path: str, ingestor: MemgraphIngestor):
        self.repo_path = Path(repo_path).resolve()
        self.project_name = self.repo_path.name
        self.ingestor = ingestor
        self.ignore_dirs = {
            "venv",
            ".venv",
            "__pycache__",
            "node_modules",
            ".vscode",
            ".idea",
            "build",
            "dist",
            ".eggs",
            ".git",
        }
        self.structural_elements: dict[Path, Optional[str]] = {}

    def run(self) -> None:
        """Orchestrates the two-pass parsing and ingestion process."""
        self.ingestor.ensure_node_batch("Project", {"name": self.project_name})
        logger.info(f"Ensuring Project: {self.project_name}")

        logger.info("--- Pass 1: Identifying Packages and Folders ---")
        self._identify_structure()

        logger.info("\n--- Pass 2: Processing Files and Python Modules ---")
        self._process_files()

        logger.info("\n--- Analysis complete. Writing all data to database... ---")
        self.ingestor.flush_all()

    def _identify_structure(self) -> None:
        """Walks the directory tree to find all packages and folders."""
        for root_str, dirs, _ in os.walk(self.repo_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]
            root = Path(root_str)
            relative_root = root.relative_to(self.repo_path)

            if (root / "__init__.py").exists():
                package_qn_parts = [self.project_name] + list(relative_root.parts)
                package_qn = ".".join(filter(None, package_qn_parts))
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

                parent_path = relative_root.parent
                if parent_package_qn := self.structural_elements.get(parent_path):
                    self.ingestor.ensure_relationship_batch(
                        ("Package", "qualified_name", parent_package_qn),
                        "CONTAINS_SUBPACKAGE",
                        ("Package", "qualified_name", package_qn),
                    )
                else:
                    self.ingestor.ensure_relationship_batch(
                        ("Project", "name", self.project_name),
                        "CONTAINS_PACKAGE",
                        ("Package", "qualified_name", package_qn),
                    )

            elif root != self.repo_path:
                self.structural_elements[relative_root] = None
                logger.info(f"  Identified Folder: '{relative_root}'")
                self.ingestor.ensure_node_batch(
                    "Folder", {"path": str(relative_root), "name": root.name}
                )
                parent_path = relative_root.parent
                if (
                    parent_package_qn := self.structural_elements.get(parent_path)
                ) and isinstance(parent_package_qn, str):
                    self.ingestor.ensure_relationship_batch(
                        ("Package", "qualified_name", parent_package_qn),
                        "CONTAINS_FOLDER",
                        ("Folder", "path", str(relative_root)),
                    )
                elif parent_path != Path("."):
                    self.ingestor.ensure_relationship_batch(
                        ("Folder", "path", str(parent_path)),
                        "CONTAINS_FOLDER",
                        ("Folder", "path", str(relative_root)),
                    )
                else:
                    self.ingestor.ensure_relationship_batch(
                        ("Project", "name", self.project_name),
                        "CONTAINS_FOLDER",
                        ("Folder", "path", str(relative_root)),
                    )

    def _process_files(self) -> None:
        """Walks the directory tree again to process all files."""
        for root_str, dirs, files in os.walk(self.repo_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]
            root = Path(root_str)
            relative_root = root.relative_to(self.repo_path)
            parent_package_qn = self.structural_elements.get(relative_root)

            for file_name in files:
                filepath = root / file_name
                relative_filepath = filepath.relative_to(self.repo_path)

                # First, create the generic File node and its relationship to its container
                self.ingestor.ensure_node_batch(
                    "File",
                    {
                        "path": str(relative_filepath),
                        "name": file_name,
                        "extension": filepath.suffix,
                    },
                )
                if parent_package_qn:
                    self.ingestor.ensure_relationship_batch(
                        ("Package", "qualified_name", parent_package_qn),
                        "CONTAINS_FILE",
                        ("File", "path", str(relative_filepath)),
                    )
                elif relative_root != Path("."):
                    self.ingestor.ensure_relationship_batch(
                        ("Folder", "path", str(relative_root)),
                        "CONTAINS_FILE",
                        ("File", "path", str(relative_filepath)),
                    )
                else:
                    self.ingestor.ensure_relationship_batch(
                        ("Project", "name", self.project_name),
                        "CONTAINS_FILE",
                        ("File", "path", str(relative_filepath)),
                    )

                # Now, perform specific parsing based on file type
                if file_name.endswith(".py"):
                    self._parse_python_module(filepath, parent_package_qn)
                elif file_name == "pyproject.toml":
                    self._parse_pyproject_toml(filepath)

    def _parse_python_module(
        self, filepath: Path, parent_package_qn: Optional[str]
    ) -> None:
        """Parses a single Python file to extract module, class, and function info."""
        relative_path = filepath.relative_to(self.repo_path)
        relative_parent_dir = relative_path.parent

        if filepath.name == "__init__.py":
            module_qn = parent_package_qn or self.project_name
        else:
            qn_parts = [self.project_name] + list(
                filter(None, relative_parent_dir.parts)
            )
            base_qn = parent_package_qn or ".".join(qn_parts)
            module_qn = f"{base_qn}.{filepath.stem}"

        self.ingestor.ensure_node_batch(
            "Module",
            {
                "qualified_name": module_qn,
                "name": filepath.name,
                "path": str(relative_path),
            },
        )

        container_label, container_key, container_val = (
            ("Package", "qualified_name", parent_package_qn)
            if parent_package_qn
            else (
                ("Folder", "path", str(relative_parent_dir))
                if relative_parent_dir != Path(".")
                else ("Project", "name", self.project_name)
            )
        )
        self.ingestor.ensure_relationship_batch(
            (container_label, container_key, container_val),
            "CONTAINS_MODULE",
            ("Module", "qualified_name", module_qn),
        )

        logger.info(
            f"  Successfully merged/created Module: {module_qn}. Parsing AST..."
        )
        try:
            source_code = filepath.read_text(encoding="utf-8")
            tree = ast.parse(source_code, filename=str(filepath))
            visitor = CodeVisitor(module_qn)
            visitor.visit(tree)
            logger.info(f"    AST parsed for {module_qn}. Walking nodes...")
        except Exception as e:
            logger.error(f"    Error parsing AST for {filepath}: {e}")
            return

        for node in ast.walk(tree):
            if not hasattr(node, "scope"):
                continue

            parent_type, parent_qn = node.scope

            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                node_qn = f"{parent_qn}.{node.name}"

                decorators = []
                if hasattr(node, "decorator_list"):
                    decorators = [
                        name
                        for d in node.decorator_list
                        if (name := _get_decorator_name(d)) is not None
                    ]

                if isinstance(node, ast.ClassDef):
                    logger.info(
                        f"      Found ClassDef: name='{node.name}', qn='{node_qn}', decorators={decorators}"
                    )
                    self.ingestor.ensure_node_batch(
                        "Class",
                        {
                            "qualified_name": node_qn,
                            "name": node.name,
                            "decorators": decorators,
                        },
                    )
                    self.ingestor.ensure_relationship_batch(
                        ("Module", "qualified_name", parent_qn),
                        "DEFINES",
                        ("Class", "qualified_name", node_qn),
                    )

                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    is_async = isinstance(node, ast.AsyncFunctionDef)

                    if parent_type == "class":
                        logger.info(
                            f"      Found Method: name='{node.name}', qn='{node_qn}', async: {is_async}, decorators={decorators}"
                        )
                        self.ingestor.ensure_node_batch(
                            "Method",
                            {
                                "qualified_name": node_qn,
                                "name": node.name,
                                "decorators": decorators,
                            },
                        )
                        self.ingestor.ensure_relationship_batch(
                            ("Class", "qualified_name", parent_qn),
                            "DEFINES_METHOD",
                            ("Method", "qualified_name", node_qn),
                        )
                    elif parent_type == "module":
                        logger.info(
                            f"      Found Function: name='{node.name}', qn='{node_qn}', async: {is_async}, decorators={decorators}"
                        )
                        self.ingestor.ensure_node_batch(
                            "Function",
                            {
                                "qualified_name": node_qn,
                                "name": node.name,
                                "decorators": decorators,
                            },
                        )
                        self.ingestor.ensure_relationship_batch(
                            ("Module", "qualified_name", parent_qn),
                            "DEFINES",
                            ("Function", "qualified_name", node_qn),
                        )

    def _parse_pyproject_toml(self, filepath: Path) -> None:
        """Parses a pyproject.toml file for dependencies, supporting multiple formats."""
        logger.info(f"  Parsing pyproject.toml: {filepath}")
        try:
            data = toml.load(filepath)
            dependencies = {}
            if (
                "tool" in data
                and "poetry" in data["tool"]
                and "dependencies" in data["tool"]["poetry"]
            ):
                dependencies = data["tool"]["poetry"]["dependencies"]
            elif "project" in data and "dependencies" in data["project"]:
                for dep_str in data["project"]["dependencies"]:
                    dep_name = (
                        dep_str.split(">=")[0]
                        .split("==")[0]
                        .split("<=")[0]
                        .split("!=")[0]
                        .split("~=")[0]
                        .strip()
                    )
                    dependencies[dep_name] = dep_str

            for dep_name, dep_spec in dependencies.items():
                if dep_name.lower() == "python":
                    continue
                logger.info(f"    Found dependency: {dep_name} (spec: {dep_spec})")

                self.ingestor.ensure_node_batch(
                    "ExternalPackage", {"name": dep_name, "version_spec": str(dep_spec)}
                )

                self.ingestor.ensure_relationship_batch(
                    from_node=("Project", "name", self.project_name),
                    rel_type="DEPENDS_ON_EXTERNAL",
                    to_node=("ExternalPackage", "name", dep_name),
                    properties={"version_spec": str(dep_spec)},
                )
        except Exception as e:
            logger.error(f"    Error parsing {filepath}: {e}")


def main() -> None:
    """Main function to parse arguments and orchestrate the repository processing."""
    default_host = os.getenv("MEMGRAPH_HOST", "localhost")
    default_port = int(os.getenv("MEMGRAPH_PORT", 7687))

    parser = argparse.ArgumentParser(
        description="Parse a Python repository and ingest its structure into Memgraph."
    )
    parser.add_argument(
        "repo_path", help="The absolute path to the Python repository to analyze."
    )
    parser.add_argument(
        "--host",
        default=default_host,
        help=f"Memgraph host (default: {default_host}, or from MEMGRAPH_HOST env var)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help=f"Memgraph port (default: {default_port}, or from MEMGRAPH_PORT env var)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete all existing data from the database before parsing.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of write operations to batch together.",
    )
    args = parser.parse_args()

    repo_path = Path(args.repo_path)
    if not repo_path.is_dir():
        logger.error(
            f"!!! ERROR: Repository path '{repo_path}' does not exist or is not a directory."
        )
        return

    try:
        with MemgraphIngestor(
            host=args.host, port=args.port, batch_size=args.batch_size
        ) as ingestor:
            if args.clean:
                ingestor.clean_database()
            ingestor.ensure_constraints()

            parser = RepositoryParser(str(repo_path), ingestor)
            parser.run()
    except Exception as e:
        logger.error(f"!!! An unexpected error occurred in main: {e}")
        import traceback

        traceback.print_exc()

    logger.info("\nFinished processing repository.")


if __name__ == "__main__":
    main()
