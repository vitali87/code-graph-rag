# codebase_rag/vector_store.py
import importlib.util
from typing import List, Optional
from loguru import logger

_HAS_QDRANT = importlib.util.find_spec("qdrant_client") is not None

if _HAS_QDRANT:
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct, VectorParams, Distance

    _CLIENT = None
    _COLLECTION = "code_embeddings"

    def get_qdrant_client() -> QdrantClient:
        """Get or create Qdrant client instance."""
        global _CLIENT
        if _CLIENT is None:
            _CLIENT = QdrantClient(path="./.qdrant_code_embeddings")  # disk storage path="./.qdrant_code_embeddings"
            if not _CLIENT.collection_exists(_COLLECTION):
                _CLIENT.create_collection(
                    collection_name=_COLLECTION,
                    vectors_config=VectorParams(size=768, distance=Distance.COSINE)
                )
        return _CLIENT

    def store_embedding(node_id: int, embedding: List[float], qualified_name: str) -> None:
        """Store code embedding in Qdrant vector database.
        
        Args:
            node_id: Memgraph node ID
            embedding: 768-dimensional embedding vector
            qualified_name: Function/method qualified name
        """
        try:
            client = get_qdrant_client()
            client.upsert(
                collection_name=_COLLECTION,
                points=[PointStruct(
                    id=node_id,
                    vector=embedding,
                    payload={"node_id": node_id, "qualified_name": qualified_name}
                )]
            )
        except Exception as e:
            logger.warning(f"Failed to store embedding for {qualified_name}: {e}")

    def search_embeddings(query_embedding: List[float], top_k: int = 5) -> List[int]:
        """Search for similar code embeddings.
        
        Args:
            query_embedding: Query embedding vector
            top_k: Number of results to return
            
        Returns:
            List of Memgraph node IDs
        """
        try:
            client = get_qdrant_client()
            hits = client.search(
                collection_name=_COLLECTION,
                query_vector=query_embedding,
                limit=top_k
            )
            return [hit.payload["node_id"] for hit in hits]
        except Exception as e:
            logger.warning(f"Failed to search embeddings: {e}")
            return []

else:
    def store_embedding(node_id: int, embedding: List[float], qualified_name: str) -> None:
        """No-op when Qdrant not available."""
        pass

    def search_embeddings(query_embedding: List[float], top_k: int = 5) -> List[int]:
        """Return empty list when Qdrant not available."""
        return []