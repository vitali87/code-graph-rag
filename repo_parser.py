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

# --- Configuration ---
MEMGRAPH_HOST = "localhost"
MEMGRAPH_PORT = 7687

# --- AST Helper ---
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

        is_scope_node = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        if is_scope_node:
            scope_type = "class" if isinstance(node, ast.ClassDef) else "function"
            qualified_name = f"{self.scope_stack[-1][1]}.{node.name}"
            self.scope_stack.append((scope_type, qualified_name))

        super().generic_visit(node)

        if is_scope_node:
            self.scope_stack.pop()


# --- Database Interaction ---
class MemgraphIngestor:
    """Handles all communication and query execution with the Memgraph database."""
    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port
        self.conn: Optional[mgclient.Connection] = None

    def __enter__(self):
        print(f"Connecting to Memgraph at {self._host}:{self._port}...")
        self.conn = mgclient.connect(host=self._host, port=self._port)
        self.conn.autocommit = True
        print("Successfully connected to Memgraph.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            self.conn.close()
            print("\nDisconnected from Memgraph.")

    def _execute_query(self, query: str, params: Optional[dict[str, Any]] = None) -> None:
        if not self.conn:
            raise ConnectionError("Not connected to Memgraph.")
        
        params = params or {}
        cursor = None
        try:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
        except Exception as e:
            if "already exists" not in str(e).lower() and "constraint" not in str(e).lower():
                print(f"!!! Cypher Error: {e}")
                print(f"    Query: {query}")
                print(f"    Params: {params}")
        finally:
            if cursor:
                cursor.close()

    def clean_database(self) -> None:
        print("--- Cleaning database... ---")
        self._execute_query("MATCH (n) DETACH DELETE n;")
        print("--- Database cleaned. ---")

    def ensure_constraints(self) -> None:
        print("Ensuring constraints...")
        constraints = {
            "Project": "name", "Package": "qualified_name", "Folder": "path",
            "Module": "qualified_name", "Class": "qualified_name", "Function": "qualified_name",
            "Method": "qualified_name", "File": "path", "ExternalPackage": "name",
        }
        for label, prop in constraints.items():
            self._execute_query(f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;")
        print("Constraints checked/created.")

    def ensure_node(self, label: str, properties: dict[str, Any]) -> None:
        prop_str = ", ".join([f"n.{key} = ${key}" for key in properties])
        id_key = next(iter(properties))
        query = (
            f"MERGE (n:{label} {{{id_key}: ${id_key}}}) "
            f"ON CREATE SET {prop_str} ON MATCH SET {prop_str}"
        )
        self._execute_query(query, properties)

    def ensure_relationship(self, from_node: tuple[str, str, Any], rel_type: str, to_node: tuple[str, str, Any], properties: Optional[dict[str, Any]] = None) -> None:
        """
        Ensures a relationship exists between two nodes, optionally setting properties
        on the relationship.
        """
        from_label, from_key, from_val = from_node
        to_label, to_key, to_val = to_node
        
        params = {"from_val": from_val, "to_val": to_val}
        
        rel_props_str = ""
        if properties:
            rel_props_str = " SET r += $props"
            params["props"] = properties

        query = (
            f"MATCH (a:{from_label} {{{from_key}: $from_val}}), (b:{to_label} {{{to_key}: $to_val}}) "
            f"MERGE (a)-[r:{rel_type}]->(b){rel_props_str}"
        )
        self._execute_query(query, params)

# --- Repository Parsing Logic ---
class RepositoryParser:
    """Parses a Python repository and uses an ingestor to save the data."""

    def __init__(self, repo_path: str, ingestor: MemgraphIngestor):
        self.repo_path = Path(repo_path).resolve()
        self.project_name = self.repo_path.name
        self.ingestor = ingestor
        self.ignore_dirs = {"venv", ".venv", "__pycache__", "node_modules", ".vscode", ".idea", "build", "dist", ".eggs", ".git"}
        self.structural_elements: dict[Path, Optional[str]] = {}

    def run(self) -> None:
        """Orchestrates the two-pass parsing and ingestion process."""
        self.ingestor.ensure_node("Project", {"name": self.project_name})
        print(f"Ensuring Project: {self.project_name}")

        print("--- Pass 1: Identifying Packages and Folders ---")
        self._identify_structure()

        print("\n--- Pass 2: Processing Files and Python Modules ---")
        self._process_files()

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
                print(f"  Identified Package: {package_qn}")

                self.ingestor.ensure_node("Package", {
                    "qualified_name": package_qn, "name": root.name, "path": str(relative_root)
                })

                parent_path = relative_root.parent
                if parent_package_qn := self.structural_elements.get(parent_path):
                    self.ingestor.ensure_relationship(("Package", "qualified_name", parent_package_qn), "CONTAINS_SUBPACKAGE", ("Package", "qualified_name", package_qn))
                else:
                    self.ingestor.ensure_relationship(("Project", "name", self.project_name), "CONTAINS_PACKAGE", ("Package", "qualified_name", package_qn))
            
            elif root != self.repo_path:
                self.structural_elements[relative_root] = None
                print(f"  Identified Folder: '{relative_root}'")
                self.ingestor.ensure_node("Folder", {"path": str(relative_root), "name": root.name})
                parent_path = relative_root.parent
                if (parent_package_qn := self.structural_elements.get(parent_path)) and isinstance(parent_package_qn, str):
                    self.ingestor.ensure_relationship(("Package", "qualified_name", parent_package_qn), "CONTAINS_FOLDER", ("Folder", "path", str(relative_root)))
                elif parent_path != Path("."):
                     self.ingestor.ensure_relationship(("Folder", "path", str(parent_path)), "CONTAINS_FOLDER", ("Folder", "path", str(relative_root)))
                else:
                     self.ingestor.ensure_relationship(("Project", "name", self.project_name), "CONTAINS_FOLDER", ("Folder", "path", str(relative_root)))

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
                self.ingestor.ensure_node("File", {"path": str(relative_filepath), "name": file_name, "extension": filepath.suffix})
                if parent_package_qn:
                    self.ingestor.ensure_relationship(("Package", "qualified_name", parent_package_qn), "CONTAINS_FILE", ("File", "path", str(relative_filepath)))
                elif relative_root != Path("."):
                    self.ingestor.ensure_relationship(("Folder", "path", str(relative_root)), "CONTAINS_FILE", ("File", "path", str(relative_filepath)))
                else:
                    self.ingestor.ensure_relationship(("Project", "name", self.project_name), "CONTAINS_FILE", ("File", "path", str(relative_filepath)))
                
                # Now, perform specific parsing based on file type
                if file_name.endswith(".py"):
                    self._parse_python_module(filepath, parent_package_qn)
                elif file_name == "pyproject.toml":
                    self._parse_pyproject_toml(filepath)


    def _parse_python_module(self, filepath: Path, parent_package_qn: Optional[str]) -> None:
        """Parses a single Python file to extract module, class, and function info."""
        relative_path = filepath.relative_to(self.repo_path)
        relative_parent_dir = relative_path.parent
        
        # Determine module qualified name more cleanly.
        if filepath.name == "__init__.py":
            module_qn = parent_package_qn or self.project_name
        else:
            qn_parts = [self.project_name] + list(filter(None, relative_parent_dir.parts))
            base_qn = parent_package_qn or ".".join(qn_parts)
            module_qn = f"{base_qn}.{filepath.stem}"

        self.ingestor.ensure_node("Module", {"qualified_name": module_qn, "name": filepath.name, "path": str(relative_path)})
        
        # Connect Module to its parent container
        if parent_package_qn:
            self.ingestor.ensure_relationship(("Package", "qualified_name", parent_package_qn), "CONTAINS_MODULE", ("Module", "qualified_name", module_qn))
        elif relative_parent_dir != Path("."):
            self.ingestor.ensure_relationship(("Folder", "path", str(relative_parent_dir)), "CONTAINS_MODULE", ("Module", "qualified_name", module_qn))
        else:
            self.ingestor.ensure_relationship(("Project", "name", self.project_name), "CONTAINS_MODULE", ("Module", "qualified_name", module_qn))

        print(f"  Successfully merged/created Module: {module_qn}. Parsing AST...")
        try:
            source_code = filepath.read_text(encoding="utf-8")
            tree = ast.parse(source_code, filename=str(filepath))
            visitor = CodeVisitor(module_qn)
            visitor.visit(tree)
            print(f"    AST parsed for {module_qn}. Walking nodes...")
        except Exception as e:
            print(f"    Error parsing AST for {filepath}: {e}")
            return
            
        for node in ast.walk(tree):
            if not hasattr(node, "scope"): continue
            
            parent_type, parent_qn = node.scope
            
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                node_qn = f"{parent_qn}.{node.name}"

                if isinstance(node, ast.ClassDef):
                    print(f"      Found ClassDef: name='{node.name}', qn='{node_qn}'")
                    self.ingestor.ensure_node("Class", {"qualified_name": node_qn, "name": node.name})
                    self.ingestor.ensure_relationship(("Module", "qualified_name", parent_qn), "DEFINES", ("Class", "qualified_name", node_qn))

                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    is_async = isinstance(node, ast.AsyncFunctionDef)
                    
                    if parent_type == "class":
                        print(f"      Found Method: name='{node.name}', qn='{node_qn}' (async: {is_async})")
                        self.ingestor.ensure_node("Method", {"qualified_name": node_qn, "name": node.name})
                        self.ingestor.ensure_relationship(("Class", "qualified_name", parent_qn), "DEFINES_METHOD", ("Method", "qualified_name", node_qn))
                    elif parent_type == "module":
                        print(f"      Found Function: name='{node.name}', qn='{node_qn}' (async: {is_async})")
                        self.ingestor.ensure_node("Function", {"qualified_name": node_qn, "name": node.name})
                        self.ingestor.ensure_relationship(("Module", "qualified_name", parent_qn), "DEFINES", ("Function", "qualified_name", node_qn))

    def _parse_pyproject_toml(self, filepath: Path) -> None:
        """Parses a pyproject.toml file for dependencies, supporting multiple formats."""
        print(f"  Parsing pyproject.toml: {filepath}")
        try:
            data = toml.load(filepath)
            dependencies = {}
            if "tool" in data and "poetry" in data["tool"] and "dependencies" in data["tool"]["poetry"]:
                dependencies = data["tool"]["poetry"]["dependencies"]
            elif "project" in data and "dependencies" in data["project"]:
                for dep_str in data["project"]["dependencies"]:
                    dep_name = dep_str.split(">=")[0].split("==")[0].split("<=")[0].split("!=")[0].split("~=")[0].strip()
                    dependencies[dep_name] = dep_str
            
            for dep_name, dep_spec in dependencies.items():
                if dep_name.lower() == "python": continue
                print(f"    Found dependency: {dep_name} (spec: {dep_spec})")
                
                self.ingestor.ensure_node("ExternalPackage", {"name": dep_name, "version_spec": str(dep_spec)})
                
                self.ingestor.ensure_relationship(
                    from_node=("Project", "name", self.project_name), 
                    rel_type="DEPENDS_ON_EXTERNAL", 
                    to_node=("ExternalPackage", "name", dep_name),
                    properties={"version_spec": str(dep_spec)}
                )
        except Exception as e:
            print(f"    Error parsing {filepath}: {e}")
# In class RepositoryParser:
    def _parse_python_module(self, filepath: Path, parent_package_qn: Optional[str]) -> None:
        """Parses a single Python file to extract module, class, and function info."""
        relative_path = filepath.relative_to(self.repo_path)
        relative_parent_dir = relative_path.parent
        
        # --- FIX 1: SIMPLIFIED AND MORE ROBUST MODULE QN CALCULATION ---
        # Determine module qualified name more cleanly.
        if filepath.name == "__init__.py":
            module_qn = parent_package_qn or self.project_name
        else:
            # The base QN is either the parent package QN or a QN derived from the folder path.
            qn_parts = [self.project_name] + list(filter(None, relative_parent_dir.parts))
            base_qn = parent_package_qn or ".".join(qn_parts)
            module_qn = f"{base_qn}.{filepath.stem}"

        self.ingestor.ensure_node("Module", {"qualified_name": module_qn, "name": filepath.name, "path": str(relative_path)})
        
        # --- FIX 2: RESTORED MODULE-TO-FOLDER RELATIONSHIP ---
        if parent_package_qn:
            # Connect Module to its parent Package
            self.ingestor.ensure_relationship(("Package", "qualified_name", parent_package_qn), "CONTAINS_MODULE", ("Module", "qualified_name", module_qn))
        elif relative_parent_dir != Path("."):
            # Connect Module to its parent Folder if it's not a package
            self.ingestor.ensure_relationship(("Folder", "path", str(relative_parent_dir)), "CONTAINS_MODULE", ("Module", "qualified_name", module_qn))
        else:
            # Connect Module directly to the Project (root-level modules)
            self.ingestor.ensure_relationship(("Project", "name", self.project_name), "CONTAINS_MODULE", ("Module", "qualified_name", module_qn))

        print(f"  Successfully merged/created Module: {module_qn}. Parsing AST...")
        try:
            source_code = filepath.read_text(encoding="utf-8")
            tree = ast.parse(source_code, filename=str(filepath))
            visitor = CodeVisitor(module_qn)
            visitor.visit(tree)
            print(f"    AST parsed for {module_qn}. Walking nodes...")
        except Exception as e:
            print(f"    Error parsing AST for {filepath}: {e}")
            return
            
        for node in ast.walk(tree):
            if not hasattr(node, "scope"): continue

            # --- FIX 3: THE PRIMARY BUG FIX ---
            # The qualified name of the node is its PARENT's QN + its own simple name.
            # node.scope holds the parent's scope.
            parent_type, parent_qn = node.scope
            
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                # Calculate the node's own, correct qualified name.
                node_qn = f"{parent_qn}.{node.name}"

                if isinstance(node, ast.ClassDef):
                    print(f"      Found ClassDef: name='{node.name}', qn='{node_qn}'")
                    self.ingestor.ensure_node("Class", {"qualified_name": node_qn, "name": node.name})
                    # The parent of a class defined at the module-level is the Module itself.
                    self.ingestor.ensure_relationship(("Module", "qualified_name", parent_qn), "DEFINES", ("Class", "qualified_name", node_qn))

                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    is_async = isinstance(node, ast.AsyncFunctionDef)
                    
                    if parent_type == "class":
                        print(f"      Found Method: name='{node.name}', qn='{node_qn}' (async: {is_async})")
                        self.ingestor.ensure_node("Method", {"qualified_name": node_qn, "name": node.name})
                        self.ingestor.ensure_relationship(("Class", "qualified_name", parent_qn), "DEFINES_METHOD", ("Method", "qualified_name", node_qn))
                    elif parent_type == "module":
                        print(f"      Found Function: name='{node.name}', qn='{node_qn}' (async: {is_async})")
                        self.ingestor.ensure_node("Function", {"qualified_name": node_qn, "name": node.name})
                        self.ingestor.ensure_relationship(("Module", "qualified_name", parent_qn), "DEFINES", ("Function", "qualified_name", node_qn))

# --- Main Execution ---
def main() -> None:
    """Main function to parse arguments and orchestrate the repository processing."""
    parser = argparse.ArgumentParser(description="Parse a Python repository and ingest its structure into Memgraph.")
    parser.add_argument("repo_path", help="The absolute path to the Python repository to analyze.")
    parser.add_argument("--host", default=MEMGRAPH_HOST, help=f"Memgraph host (default: {MEMGRAPH_HOST})")
    parser.add_argument("--port", type=int, default=MEMGRAPH_PORT, help=f"Memgraph port (default: {MEMGRAPH_PORT})")
    parser.add_argument("--clean", action="store_true", help="Delete all existing data from the database before parsing.")
    args = parser.parse_args()

    repo_path = Path(args.repo_path)
    if not repo_path.is_dir():
        print(f"!!! ERROR: Repository path '{repo_path}' does not exist or is not a directory.")
        return

    try:
        with MemgraphIngestor(host=args.host, port=args.port) as ingestor:
            if args.clean:
                ingestor.clean_database()
            ingestor.ensure_constraints()
            
            parser = RepositoryParser(str(repo_path), ingestor)
            parser.run()
    except Exception as e:
        print(f"!!! An unexpected error occurred in main: {e}")
        import traceback
        traceback.print_exc()

    print("\nFinished processing repository.")

if __name__ == "__main__":
    main()