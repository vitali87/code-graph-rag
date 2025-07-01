from datetime import timezone
import mgclient
from loguru import logger
from typing import Any, Optional


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
            except Exception:
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

    def fetch_all(self, query: str, params: Optional[dict[str, Any]] = None) -> list:
        """Executes a query and fetches all results."""
        logger.debug(f"Executing fetch query: {query} with params: {params}")
        return self._execute_query(query, params)

    def execute_write(
        self, query: str, params: Optional[dict[str, Any]] = None
    ) -> None:
        """Executes a write query without returning results."""
        logger.debug(f"Executing write query: {query} with params: {params}")
        self._execute_query(query, params)

    def export_graph_to_dict(self) -> dict[str, Any]:
        """Export the entire graph as a dictionary with nodes and relationships."""
        logger.info("Exporting graph data...")
        
        # Get all nodes with their labels and properties
        nodes_query = """
        MATCH (n)
        RETURN id(n) as node_id, labels(n) as labels, properties(n) as properties
        """
        nodes_data = self.fetch_all(nodes_query)
        
        # Get all relationships with their types and properties
        relationships_query = """
        MATCH (a)-[r]->(b)
        RETURN id(a) as from_id, id(b) as to_id, type(r) as type, properties(r) as properties
        """
        relationships_data = self.fetch_all(relationships_query)
        
        graph_data = {
            "nodes": nodes_data,
            "relationships": relationships_data,
            "metadata": {
                "total_nodes": len(nodes_data),
                "total_relationships": len(relationships_data),
                "exported_at": self._get_current_timestamp()
            }
        }
        
        logger.info(f"Exported {len(nodes_data)} nodes and {len(relationships_data)} relationships")
        return graph_data
    
    def _get_current_timestamp(self) -> str:
        """Get current timestamp in ISO format."""
        from datetime import datetime
        return datetime.now(timezone.utc).isoformat()
