#!/usr/bin/env python3
import os
import ast
import mgclient
import toml
import argparse

# --- Configuration ---
MEMGRAPH_HOST = "localhost"
MEMGRAPH_PORT = 7687

# --- AST Helper ---
class CodeVisitor(ast.NodeVisitor):
    def __init__(self, module_qn):
        self.module_qn = module_qn
        self.scope_stack = [('module', module_qn)]

    def visit(self, node):
        for child in ast.iter_child_nodes(node):
            child.parent = node
        is_scope = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified_name = f"{self.scope_stack[-1][1]}.{node.name}"
            self.scope_stack.append(('function', qualified_name))
            is_scope = True
        elif isinstance(node, ast.ClassDef):
            qualified_name = f"{self.scope_stack[-1][1]}.{node.name}"
            self.scope_stack.append(('class', qualified_name))
            is_scope = True
        node.scope = self.scope_stack[-1]
        super().generic_visit(node)
        if is_scope:
            self.scope_stack.pop()

# --- Memgraph Helper ---
def execute_query(conn, query, params=None):
    """Executes a Cypher query using a cursor."""
    if params is None:
        params = {}
    cursor = None
    try:
        # THE FIX IS HERE: You MUST use a cursor to execute queries.
        cursor = conn.cursor()
        cursor.execute(query, params)
    except Exception as e:
        if "already exists" not in str(e).lower() and "constraint" not in str(e).lower():
            print(f"!!! Cypher Error: {e}")
            print(f"    Query: {query}")
            print(f"    Params: {params}")
    finally:
        if cursor:
            cursor.close()

# --- Node and Relationship Creation Functions ---
def ensure_project_node(conn, project_name):
    print(f"Ensuring Project: {project_name}")
    execute_query(conn, "MERGE (p:Project {name: $name})", {"name": project_name})

def ensure_folder_node(conn, folder_path, folder_name, project_name, parent_folder_path=None, parent_package_qn=None):
    q_parts = ["MERGE (f:Folder {path: $path}) ON CREATE SET f.name = $name ON MATCH SET f.name = $name"]
    params = {"path": folder_path, "name": folder_name}
    if parent_package_qn:
        q_parts.append("WITH f MATCH (pp:Package {qualified_name: $pp_qn}) MERGE (pp)-[:CONTAINS_FOLDER]->(f)")
        params["pp_qn"] = parent_package_qn
    elif parent_folder_path:
        q_parts.append("WITH f MATCH (pf:Folder {path: $pf_path}) MERGE (pf)-[:CONTAINS_FOLDER]->(f)")
        params["pf_path"] = parent_folder_path
    else:
        q_parts.append("WITH f MATCH (p:Project {name: $p_name}) MERGE (p)-[:CONTAINS_FOLDER]->(f)")
        params["p_name"] = project_name
    execute_query(conn, " ".join(q_parts), params)

def ensure_package_node(conn, package_qn, package_path, package_name, project_name, parent_package_qn=None):
    q_parts = ["MERGE (pkg:Package {qualified_name: $qn}) ON CREATE SET pkg.path = $path, pkg.name = $name ON MATCH SET pkg.path = $path, pkg.name = $name"]
    params = {"qn": package_qn, "path": package_path, "name": package_name}
    if parent_package_qn:
        q_parts.append("WITH pkg MATCH (pp:Package {qualified_name: $pp_qn}) MERGE (pp)-[:CONTAINS_SUBPACKAGE]->(pkg)")
        params["pp_qn"] = parent_package_qn
    else:
        q_parts.append("WITH pkg MATCH (p:Project {name: $p_name}) MERGE (p)-[:CONTAINS_PACKAGE]->(pkg)")
        params["p_name"] = project_name
    execute_query(conn, " ".join(q_parts), params)

def create_file_node(conn, file_path, file_name, extension, project_name, parent_folder_path=None, parent_package_qn=None):
    q_parts = ["MERGE (file_node:File {path: $path}) ON CREATE SET file_node.name = $name, file_node.extension = $ext ON MATCH SET file_node.name = $name, file_node.extension = $ext"]
    params = {"path": file_path, "name": file_name, "ext": extension}
    if parent_package_qn:
        q_parts.append("WITH file_node MATCH (pp:Package {qualified_name: $pp_qn}) MERGE (pp)-[:CONTAINS_FILE]->(file_node)")
        params["pp_qn"] = parent_package_qn
    elif parent_folder_path:
        q_parts.append("WITH file_node MATCH (pf:Folder {path: $pf_path}) MERGE (pf)-[:CONTAINS_FILE]->(file_node)")
        params["pf_path"] = parent_folder_path
    else:
        q_parts.append("WITH file_node MATCH (p:Project {name: $p_name}) MERGE (p)-[:CONTAINS_FILE]->(file_node)")
        params["p_name"] = project_name
    execute_query(conn, " ".join(q_parts), params)

def parse_python_module(conn, module_filepath, module_qualified_name, module_simple_name, repo_path, parent_package_qn=None, parent_project_name=None):
    relative_path = os.path.relpath(module_filepath, repo_path)
    q_parts = ["MERGE (mod:Module {qualified_name: $mod_qn})",
               "ON CREATE SET mod.name = $mod_name, mod.path = $path",
               "ON MATCH SET mod.name = $mod_name, mod.path = $path"]
    params = {"mod_qn": module_qualified_name, "mod_name": module_simple_name, "path": relative_path}
    if parent_package_qn:
        q_parts.append("WITH mod MATCH (pkg:Package {qualified_name: $pkg_qn}) MERGE (pkg)-[:CONTAINS_MODULE]->(mod)")
        params["pkg_qn"] = parent_package_qn
    elif parent_project_name:
        q_parts.append("WITH mod MATCH (proj:Project {name: $proj_name}) MERGE (proj)-[:CONTAINS_MODULE]->(mod)")
        params["proj_name"] = parent_project_name
    execute_query(conn, " ".join(q_parts), params)
    print(f"  Successfully merged/created Module: {module_qualified_name}. Parsing AST...")

    try:
        with open(module_filepath, "r", encoding="utf-8") as source_file:
            source_code = source_file.read()
        tree = ast.parse(source_code, filename=module_filepath)
        visitor = CodeVisitor(module_qualified_name)
        visitor.visit(tree)
    except Exception as e:
        print(f"    Error parsing AST for {module_filepath}: {e}")
        return
    print(f"    AST parsed for {module_qualified_name}. Walking nodes...")
    
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_name = node.name
            qualified_class_name = node.scope[1]
            print(f"      Found ClassDef: name='{class_name}', qn='{qualified_class_name}'")
            execute_query(conn,
                "MATCH (m:Module {qualified_name: $mod_qn}) "
                "MERGE (c:Class {qualified_name: $class_qn}) ON CREATE SET c.name = $name ON MATCH SET c.name = $name "
                "MERGE (m)-[:DEFINES]->(c)",
                {"mod_qn": module_qualified_name, "class_qn": qualified_class_name, "name": class_name}
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_async = isinstance(node, ast.AsyncFunctionDef)
            if isinstance(node.parent, ast.ClassDef):
                method_name = node.name
                qualified_method_name = node.scope[1]
                parent_class_qn = node.parent.scope[1]
                print(f"      Found Method: name='{method_name}', qn='{qualified_method_name}' (async: {is_async})")
                execute_query(conn,
                    "MATCH (cls:Class {qualified_name: $class_qn}) "
                    "MERGE (meth:Method {qualified_name: $method_qn}) ON CREATE SET meth.name = $name ON MATCH SET meth.name = $name "
                    "MERGE (cls)-[:DEFINES_METHOD]->(meth)",
                    {"class_qn": parent_class_qn, "method_qn": qualified_method_name, "name": method_name}
                )
            elif isinstance(node.parent, ast.Module):
                func_name = node.name
                qualified_func_name = node.scope[1]
                print(f"      Found Function: name='{func_name}', qn='{qualified_func_name}' (async: {is_async})")
                execute_query(conn,
                    "MATCH (m:Module {qualified_name: $mod_qn}) "
                    "MERGE (f:Function {qualified_name: $func_qn}) ON CREATE SET f.name = $name ON MATCH SET f.name = $name "
                    "MERGE (m)-[:DEFINES]->(f)",
                    {"mod_qn": module_qualified_name, "func_qn": qualified_func_name, "name": func_name}
                )

def parse_pyproject_toml(conn, project_name, filepath):
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
        for dep_name, dep_version_spec in dependencies.items():
            if dep_name.lower() == "python": continue
            print(f"    Found dependency: {dep_name} (spec: {dep_version_spec})")
            execute_query(conn,
                "MATCH (p:Project {name: $proj_name}) "
                "MERGE (ep:ExternalPackage {name: $dep_name}) "
                "ON CREATE SET ep.version_spec = $spec ON MATCH SET ep.version_spec = $spec "
                "MERGE (p)-[:DEPENDS_ON_EXTERNAL {version_spec: $spec}]->(ep)",
                {"proj_name": project_name, "dep_name": dep_name, "spec": str(dep_version_spec)}
            )
    except Exception as e:
        print(f"    Error parsing {filepath}: {e}")

# --- Main Processing Logic ---
def process_repository(repo_path, project_name, conn):
    ensure_project_node(conn, project_name)
    structural_elements = {}
    ignore_dirs = {"venv", ".venv", "__pycache__", "node_modules", ".vscode", ".idea", "build", "dist", ".eggs", ".git"}

    print("--- Pass 1: Identifying Packages and Folders ---")
    for root, dirs, _ in os.walk(repo_path, topdown=True):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        relative_root_path = os.path.relpath(root, repo_path)
        if relative_root_path == ".": relative_root_path = ""
        current_element_name = os.path.basename(root) if relative_root_path else project_name
        if os.path.exists(os.path.join(root, "__init__.py")):
            package_qn = project_name + ("." + ".".join(relative_root_path.split(os.sep)) if relative_root_path else "")
            structural_elements[relative_root_path] = package_qn
            print(f"  Identified Package: {package_qn}")
            parent_path = os.path.dirname(relative_root_path) if relative_root_path else None
            parent_package_qn = structural_elements.get(parent_path)
            ensure_package_node(conn, package_qn, relative_root_path, current_element_name, project_name, parent_package_qn)
        elif relative_root_path:
            structural_elements[relative_root_path] = True
            print(f"  Identified Folder: '{relative_root_path}'")
            parent_path = os.path.dirname(relative_root_path) if relative_root_path else None
            parent_is_package = parent_path is not None and isinstance(structural_elements.get(parent_path), str)
            ensure_folder_node(conn, relative_root_path, current_element_name, project_name,
                               parent_folder_path=parent_path if not parent_is_package else None,
                               parent_package_qn=structural_elements.get(parent_path) if parent_is_package else None)

    print("\n--- Pass 2: Processing Files and Python Modules ---")
    for root, dirs, files in os.walk(repo_path, topdown=True):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        relative_root_path = os.path.relpath(root, repo_path)
        if relative_root_path == ".": relative_root_path = ""
        parent_info = structural_elements.get(relative_root_path)
        is_in_package = isinstance(parent_info, str)
        parent_package_qn = parent_info if is_in_package else None
        parent_folder_path = relative_root_path if not is_in_package and relative_root_path else None
        
        for file_name in files:
            filepath = os.path.join(root, file_name)
            relative_filepath = os.path.relpath(filepath, repo_path)
            base_name, extension = os.path.splitext(file_name)
            if file_name.endswith(".py"):
                module_simple_name = base_name if file_name != "__init__.py" else "__init__"
                module_qn_base = parent_package_qn or (project_name + ("." + ".".join(relative_root_path.split(os.sep)) if relative_root_path else ""))
                module_qualified_name = module_qn_base if file_name == "__init__.py" else f"{module_qn_base}.{module_simple_name}"
                parse_python_module(conn, filepath, module_qualified_name, file_name, repo_path,
                                    parent_package_qn=parent_package_qn,
                                    parent_project_name=project_name if not parent_package_qn and not parent_folder_path else None)
            elif file_name == "pyproject.toml":
                parse_pyproject_toml(conn, project_name, filepath)
            create_file_node(conn, relative_filepath, file_name, extension, project_name, 
                             parent_folder_path=parent_folder_path,
                             parent_package_qn=parent_package_qn)

# --- Main Execution ---
def main():
    parser = argparse.ArgumentParser(description="Parse a Python repository and ingest its structure into Memgraph.")
    parser.add_argument("repo_path", help="The absolute path to the Python repository to analyze.")
    parser.add_argument("--host", default=MEMGRAPH_HOST, help=f"Memgraph host (default: {MEMGRAPH_HOST})")
    parser.add_argument("--port", type=int, default=MEMGRAPH_PORT, help=f"Memgraph port (default: {MEMGRAPH_PORT})")
    parser.add_argument("--clean", action="store_true", help="Delete all existing data from the database before parsing.")
    args = parser.parse_args()

    REPO_PATH = os.path.abspath(args.repo_path)
    PROJECT_NAME = os.path.basename(REPO_PATH)
    if not os.path.isdir(REPO_PATH):
        print(f"!!! ERROR: Repository path '{REPO_PATH}' does not exist or is not a directory.")
        return

    print(f"Processing repository: {PROJECT_NAME} at {REPO_PATH}")
    print(f"Connecting to Memgraph at {args.host}:{args.port}")
    conn = None
    try:
        conn = mgclient.connect(host=args.host, port=args.port)
        conn.autocommit = True
        print("Successfully connected to Memgraph.")

        if args.clean:
            print("--- Cleaning database as requested... ---")
            execute_query(conn, "MATCH (n) DETACH DELETE n;")
            print("--- Database cleaned. ---")

        print("Ensuring constraints...")
        constraints = [
            "CREATE CONSTRAINT ON (p:Project) ASSERT p.name IS UNIQUE;",
            "CREATE CONSTRAINT ON (pkg:Package) ASSERT pkg.qualified_name IS UNIQUE;",
            "CREATE CONSTRAINT ON (f:Folder) ASSERT f.path IS UNIQUE;",
            "CREATE CONSTRAINT ON (m:Module) ASSERT m.qualified_name IS UNIQUE;",
            "CREATE CONSTRAINT ON (c:Class) ASSERT c.qualified_name IS UNIQUE;",
            "CREATE CONSTRAINT ON (func:Function) ASSERT func.qualified_name IS UNIQUE;",
            "CREATE CONSTRAINT ON (meth:Method) ASSERT meth.qualified_name IS UNIQUE;",
            "CREATE CONSTRAINT ON (file:File) ASSERT file.path IS UNIQUE;",
            "CREATE CONSTRAINT ON (ep:ExternalPackage) ASSERT ep.name IS UNIQUE;",
        ]
        for constraint_query in constraints:
            execute_query(conn, constraint_query)
        print("Constraints checked/created.")

        process_repository(REPO_PATH, PROJECT_NAME, conn)
    except Exception as e:
        print(f"!!! An unexpected error occurred in main: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if conn:
            conn.close()
            print("\nDisconnected from Memgraph.")
    print("Finished processing repository.")

if __name__ == "__main__":
    main()