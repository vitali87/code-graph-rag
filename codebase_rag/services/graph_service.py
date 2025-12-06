from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import mgclient
from loguru import logger


class MemgraphIngestor:
    """Handles all communication and query execution with the Memgraph database."""

    def __init__(self, host: str, port: int, batch_size: int = 1000):
        self._host = host
        self._port = port
        if batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self.batch_size = batch_size
        self.conn: mgclient.Connection | None = None
        self.node_buffer: list[tuple[str, dict[str, Any]]] = []
        self.relationship_buffer: list[tuple[tuple, str, tuple, dict | None]] = []
        self.unique_constraints = {
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

    def __enter__(self) -> "MemgraphIngestor":
        logger.info(f"Connecting to Memgraph at {self._host}:{self._port}...")
        self.conn = mgclient.connect(host=self._host, port=self._port)
        self.conn.autocommit = True
        logger.info("Successfully connected to Memgraph.")
        return self

    def __exit__(
        self, exc_type: type | None, exc_val: Exception | None, exc_tb: Any
    ) -> None:
        if exc_type:
            logger.error(
                f"An exception occurred: {exc_val}. Flushing remaining items...",
                exc_info=True,
            )
        self.flush_all()
        if self.conn:
            self.conn.close()
            logger.info("\nDisconnected from Memgraph.")

    def _execute_query(self, query: str, params: dict[str, Any] | None = None) -> list:
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
                logger.error(f"    Query: {query}")
                if len(params_list) > 10:
                    logger.error(
                        "    Params (first 10 of {}): {}...",
                        len(params_list),
                        params_list[:10],
                    )
                else:
                    logger.error(f"    Params: {params_list}")
            raise
        finally:
            if cursor:
                cursor.close()

    def _execute_batch_with_return(
        self, query: str, params_list: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Execute a batch query that returns results."""
        if not self.conn or not params_list:
            return []
        cursor = None
        try:
            cursor = self.conn.cursor()
            batch_query = f"UNWIND $batch AS row\n{query}"
            cursor.execute(batch_query, {"batch": params_list})
            if not cursor.description:
                return []
            column_names = [desc.name for desc in cursor.description]
            return [dict(zip(column_names, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"!!! Batch Cypher Error: {e}")
            logger.error(f"    Query: {query}")
            raise
        finally:
            if cursor:
                cursor.close()

    def clean_database(self) -> None:
        """Wipe the entire database. Use with caution."""
        logger.info("--- Cleaning database... ---")
        self._execute_query("MATCH (n) DETACH DELETE n;")
        logger.info("--- Database cleaned. ---")

    def list_projects(self) -> list[str]:
        """List all indexed projects in the database.

        Returns:
            List of project names
        """
        result = self.fetch_all("MATCH (p:Project) RETURN p.name AS name ORDER BY p.name")
        return [r["name"] for r in result]

    def delete_project(self, project_name: str) -> int:
        """Delete all nodes associated with a specific project.

        This removes the Project node and all nodes whose qualified_name
        starts with the project name prefix, preserving other projects.

        Args:
            project_name: Name of the project to delete

        Returns:
            Number of nodes deleted
        """
        logger.info(f"--- Deleting project: {project_name} ---")

        # First, count nodes to be deleted
        count_result = self.fetch_all(
            """
            MATCH (n)
            WHERE n.qualified_name STARTS WITH $prefix
               OR (n:Project AND n.name = $project_name)
            RETURN count(n) AS count
            """,
            {"prefix": f"{project_name}.", "project_name": project_name},
        )
        node_count = count_result[0]["count"] if count_result else 0

        # Delete all nodes with qualified_name starting with project name
        self._execute_query(
            """
            MATCH (n)
            WHERE n.qualified_name STARTS WITH $prefix
               OR (n:Project AND n.name = $project_name)
            DETACH DELETE n
            """,
            {"prefix": f"{project_name}.", "project_name": project_name},
        )

        logger.info(f"--- Project {project_name} deleted. {node_count} nodes removed. ---")
        return node_count

    def ensure_constraints(self) -> None:
        logger.info("Ensuring constraints...")
        for label, prop in self.unique_constraints.items():
            try:
                self._execute_query(
                    f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"
                )
            except Exception:
                pass
        logger.info("Constraints checked/created.")

    def ensure_node_batch(self, label: str, properties: dict[str, Any]) -> None:
        """Adds a node to the buffer."""
        self.node_buffer.append((label, properties))
        if len(self.node_buffer) >= self.batch_size:
            logger.debug(
                "Node buffer reached batch size ({}). Performing incremental flush.",
                self.batch_size,
            )
            self.flush_nodes()

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, Any],
        rel_type: str,
        to_spec: tuple[str, str, Any],
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Adds a relationship to the buffer."""
        from_label, from_key, from_val = from_spec
        to_label, to_key, to_val = to_spec
        self.relationship_buffer.append(
            (
                (from_label, from_key, from_val),
                rel_type,
                (to_label, to_key, to_val),
                properties,
            )
        )
        if len(self.relationship_buffer) >= self.batch_size:
            logger.debug(
                "Relationship buffer reached batch size ({}). Performing incremental flush.",
                self.batch_size,
            )
            # Ensure all pending nodes exist before we flush relationships
            self.flush_nodes()
            self.flush_relationships()

    def flush_nodes(self) -> None:
        """Flushes the buffered nodes to the database."""
        if not self.node_buffer:
            return

        buffer_size = len(self.node_buffer)
        nodes_by_label = defaultdict(list)
        for label, props in self.node_buffer:
            nodes_by_label[label].append(props)
        flushed_total = 0
        skipped_total = 0
        for label, props_list in nodes_by_label.items():
            if not props_list:
                continue
            id_key = self.unique_constraints.get(label)
            if not id_key:
                logger.warning(
                    f"No unique constraint defined for label '{label}'. Skipping flush."
                )
                skipped_total += len(props_list)
                continue

            batch_rows: list[dict[str, Any]] = []
            for props in props_list:
                if id_key not in props:
                    logger.warning(
                        "Skipping {} node missing required '{}' property: {}",
                        label,
                        id_key,
                        props,
                    )
                    skipped_total += 1
                    continue
                row_props = {k: v for k, v in props.items() if k != id_key}
                batch_rows.append({"id": props[id_key], "props": row_props})

            if not batch_rows:
                continue

            flushed_total += len(batch_rows)

            query = f"MERGE (n:{label} {{{id_key}: row.id}})\nSET n += row.props"
            self._execute_batch(query, batch_rows)
        logger.info("Flushed {} of {} buffered nodes.", flushed_total, buffer_size)
        if skipped_total:
            logger.info(
                "Skipped {} buffered nodes due to missing identifiers or constraints.",
                skipped_total,
            )
        self.node_buffer.clear()

    def flush_relationships(self) -> None:
        if not self.relationship_buffer:
            return

        rels_by_pattern = defaultdict(list)
        for from_node, rel_type, to_node, props in self.relationship_buffer:
            pattern = (from_node[0], from_node[1], rel_type, to_node[0], to_node[1])
            rels_by_pattern[pattern].append(
                {"from_val": from_node[2], "to_val": to_node[2], "props": props or {}}
            )

        total_attempted = 0
        total_successful = 0

        for pattern, params_list in rels_by_pattern.items():
            from_label, from_key, rel_type, to_label, to_key = pattern
            query = (
                f"MATCH (a:{from_label} {{{from_key}: row.from_val}}), "
                f"(b:{to_label} {{{to_key}: row.to_val}})\n"
                f"MERGE (a)-[r:{rel_type}]->(b)\n"
                f"RETURN count(r) as created"
            )
            if any(p["props"] for p in params_list):
                query = query.replace(
                    "RETURN count(r) as created",
                    "SET r += row.props\nRETURN count(r) as created",
                )

            total_attempted += len(params_list)
            results = self._execute_batch_with_return(query, params_list)
            batch_successful = (
                sum(r.get("created", 0) for r in results) if results else 0
            )
            total_successful += batch_successful

            # Log failures for CALLS relationships
            if rel_type == "CALLS":
                failed = len(params_list) - batch_successful
                if failed > 0:
                    logger.warning(
                        f"Failed to create {failed} CALLS relationships - nodes may not exist"
                    )
                    # Log first 3 samples
                    for i, sample in enumerate(params_list[:3]):
                        logger.warning(
                            f"  Sample {i + 1}: {from_label}.{sample['from_val']} -> {to_label}.{sample['to_val']}"
                        )

        logger.info(
            f"Flushed {len(self.relationship_buffer)} relationships ({total_successful} successful, {total_attempted - total_successful} failed)."
        )
        self.relationship_buffer.clear()

    def flush_all(self) -> None:
        logger.info("--- Flushing all pending writes to database... ---")
        self.flush_nodes()
        self.flush_relationships()
        logger.info("--- Flushing complete. ---")

    def fetch_all(self, query: str, params: dict[str, Any] | None = None) -> list:
        """Executes a query and fetches all results."""
        logger.debug(f"Executing fetch query: {query} with params: {params}")
        return self._execute_query(query, params)

    def execute_write(self, query: str, params: dict[str, Any] | None = None) -> None:
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
                "exported_at": self._get_current_timestamp(),
            },
        }

        logger.info(
            f"Exported {len(nodes_data)} nodes and {len(relationships_data)} relationships"
        )
        return graph_data

    def _get_current_timestamp(self) -> str:
        """Get current timestamp in ISO format."""
        return datetime.now(UTC).isoformat()
