# codebase_rag/services/graph_db.py
import mgclient
from typing import List, Dict, Any
from ..config import settings

class GraphQueryError(Exception):
    """Custom exception for graph query failures."""
    pass

class MemgraphService:
    """A service to interact with the Memgraph database."""
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        print(f"MemgraphService configured for {host}:{port}")

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        """Executes a Cypher query and returns the results."""
        if not query:
            raise GraphQueryError("Query cannot be empty.")
        
        try:
            conn = mgclient.connect(host=self.host, port=self.port)
            try:
                cursor = conn.cursor()
                print(f"  [MemgraphService] Executing: {query}")
                cursor.execute(query)
                
                if not cursor.description:
                    return []
                
                column_names = [desc.name for desc in cursor.description]
                results = [dict(zip(column_names, row)) for row in cursor.fetchall()]
                print(f"  [MemgraphService] Found {len(results)} results.")
                return results
            finally:
                conn.close()
        except Exception as e:
            print(f"  [MemgraphService] Error: {e}")
            raise GraphQueryError(f"Failed to execute query: {e}")

# A single, reusable instance for the application
memgraph_service = MemgraphService(host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT)