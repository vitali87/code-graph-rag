from loguru import logger

from .utils.dependencies import has_qdrant_client

if has_qdrant_client():
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    _CLIENT = None
    _COLLECTION = "code_embeddings"

    def get_qdrant_client() -> QdrantClient:
        global _CLIENT
        if _CLIENT is None:
            _CLIENT = QdrantClient(path="./.qdrant_code_embeddings")
            if not _CLIENT.collection_exists(_COLLECTION):
                _CLIENT.create_collection(
                    collection_name=_COLLECTION,
                    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
                )
        return _CLIENT

    def store_embedding(
        node_id: int, embedding: list[float], qualified_name: str
    ) -> None:
        try:
            client = get_qdrant_client()
            client.upsert(
                collection_name=_COLLECTION,
                points=[
                    PointStruct(
                        id=node_id,
                        vector=embedding,
                        payload={"node_id": node_id, "qualified_name": qualified_name},
                    )
                ],
            )
        except Exception as e:
            logger.warning(f"Failed to store embedding for {qualified_name}: {e}")

    def search_embeddings(
        query_embedding: list[float], top_k: int = 5
    ) -> list[tuple[int, float]]:
        try:
            client = get_qdrant_client()
            result = client.query_points(
                collection_name=_COLLECTION, query=query_embedding, limit=top_k
            )
            return [
                (hit.payload["node_id"], hit.score)
                for hit in result.points
                if hit.payload is not None
            ]
        except Exception as e:
            logger.warning(f"Failed to search embeddings: {e}")
            return []

else:

    def store_embedding(
        node_id: int, embedding: list[float], qualified_name: str
    ) -> None:
        pass

    def search_embeddings(
        query_embedding: list[float], top_k: int = 5
    ) -> list[tuple[int, float]]:
        return []
