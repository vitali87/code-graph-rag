import os
import platform
from pathlib import Path
from typing import Any, Optional, Dict

import mgclient
from loguru import logger
from tree_sitter import Language, Parser, Node
from tree_sitter_python import (
    language as python_language_so,
)


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
                f"An exception occurred: {exc_val}. Flushing remaining items...",
                exc_info=True,
            )
        self.flush_all()
        if self.conn:
            self.conn.close()
            logger.info("\nDisconnected from Memgraph.")

    def _execute_query(
        self, query: str, params: Optional[dict[str, Any]] = None
    ) -> list:
        if not self.conn:
            raise ConnectionError("Not connected to Memgraph.")
        params = params or {}
        cursor = None
        try:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            if not cursor.description:
                return []
            column_names = [desc.name for desc in cursor.description]
            return [dict(zip(column_names, row)) for row in cursor.fetchall()]
        except Exception as e:
            if (
                "already exists" not in str(e).lower()
                and "constraint" not in str(e).lower()
            ):
                logger.error(f"!!! Cypher Error: {e}")
                logger.error(f"    Query: {query}")
                logger.error(f"    Params: {params}")
            raise
        finally:
            if cursor:
                cursor.close()

    def _execute_batch(self, query: str, params_list: list[dict[str, Any]]) -> None:
        if not self.conn or not params_list:
            return
        cursor = None
        try:
            cursor = self.conn.cursor()
            batch_query = f"UNWIND $batch AS row\n{query}"
            cursor.execute(batch_query, {"batch": params_list})
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.error(f"!!! Batch Cypher Error: {e}")
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
            try:
                self._execute_query(
                    f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"
                )
            except:  # Fails if constraint exists, which is fine
                pass
        logger.info("Constraints checked/created.")

    def ensure_node_batch(self, label: str, properties: dict[str, Any]) -> None:
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
        self.relationship_buffer.append((from_node, rel_type, to_node, properties))
        if len(self.relationship_buffer) >= self.batch_size:
            self.flush_relationships()

    def flush_nodes(self) -> None:
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
        if not self.relationship_buffer:
            return
        from collections import defaultdict

        rels_by_pattern = defaultdict(list)
        for from_node, rel_type, to_node, props in self.relationship_buffer:
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
            if any(p["props"] for p in params_list):
                query += "\nSET r += row.props"
            self._execute_batch(query, params_list)
        logger.info(f"Flushed {len(self.relationship_buffer)} relationships.")
        self.relationship_buffer.clear()

    def flush_all(self) -> None:
        logger.info("--- Flushing all pending writes to database... ---")
        self.flush_nodes()
        self.flush_relationships()
        logger.info("--- Flushing complete. ---")


class GraphUpdater:
    """Parses code using Tree-sitter and updates the graph."""

    def __init__(self, ingestor: MemgraphIngestor, repo_path: Path):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = repo_path.name

        self.parser = Parser()
        self.language = Language(python_language_so())
        self.parser.language = self.language
        logger.success("Successfully loaded Python grammar.")

        self._compile_queries()

    def _compile_queries(self):
        """Compiles all Tree-sitter queries for efficiency."""
        self.queries = {
            "top_level_functions": self.language.query(
                "(module (function_definition name: (identifier) @name) @def)"
            ),
            "classes": self.language.query("(class_definition) @class"),
            "methods": self.language.query(
                "(function_definition name: (identifier) @name) @def"
            ),
            "docstrings": self.language.query(
                """(
                    (function_definition body: (block (expression_statement (string) @docstring)))
                    (class_definition body: (block (expression_statement (string) @docstring)))
                )"""
            ),
        }

    def _get_docstring(self, node: Node) -> Optional[str]:
        """Extracts the docstring from a function or class node's body."""
        body_node = node.child_by_field_name("body")
        if not body_node or not body_node.children:
            return None

        # A docstring must be the first statement in the body
        first_statement = body_node.children[0]
        if (
            first_statement.type == "expression_statement"
            and first_statement.children[0].type == "string"
        ):
            # Clean up quotes ("""...""") and indentation
            return first_statement.children[0].text.decode("utf-8").strip("'\" \n")
        return None

    def parse_and_ingest_file(self, file_path: Path):
        relative_path_str = str(file_path.relative_to(self.repo_path))
        logger.info(f"Parsing: {relative_path_str}")

        if not file_path.name.endswith(".py"):
            return

        try:
            source_bytes = file_path.read_bytes()
            tree = self.parser.parse(source_bytes)
            root_node = tree.root_node

            module_qn = relative_path_str.replace(os.sep, ".").removesuffix(".py")
            self.ingestor.ensure_node_batch(
                "Module",
                {
                    "qualified_name": module_qn,
                    "name": file_path.name,
                    "path": relative_path_str,
                },
            )

            self._ingest_top_level_functions(root_node, module_qn)
            self._ingest_classes_and_methods(root_node, module_qn)

        except Exception as e:
            logger.error(f"Failed to parse or ingest {file_path}: {e}", exc_info=True)

    def _ingest_top_level_functions(self, root_node, parent_qn: str):
        captures = self.queries["top_level_functions"].captures(root_node)
        for capture_tuple in captures:
            node, capture_name = capture_tuple[0], capture_tuple[1]
            if capture_name == "def":
                name_node = node.child_by_field_name("name")
                if not name_node:
                    continue

                func_name = name_node.text.decode("utf8")
                func_qn = f"{parent_qn}.{func_name}"

                props = {
                    "qualified_name": func_qn,
                    "name": func_name,
                    "decorators": [],
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "docstring": self._get_docstring(node),
                }

                logger.info(f"  Found Function: {func_name} (qn: {func_qn})")
                self.ingestor.ensure_node_batch("Function", props)
                self.ingestor.ensure_relationship_batch(
                    ("Module", "qualified_name", parent_qn),
                    "DEFINES",
                    ("Function", "qualified_name", func_qn),
                )

    def _ingest_classes_and_methods(self, root_node, parent_qn: str):
        class_captures = self.queries["classes"].captures(root_node)
        class_nodes = [
            node
            for node, name in [(cap[0], cap[1]) for cap in class_captures]
            if name == "class"
        ]

        for class_node in class_nodes:
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

            method_captures = self.queries["methods"].captures(body_node)
            for capture_tuple in method_captures:
                method_node, _ = capture_tuple[0], capture_tuple[1]
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
