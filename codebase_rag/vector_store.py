from loguru import logger

from . import logs as ls
from .config import settings
from .constants import PAYLOAD_NODE_ID, PAYLOAD_QUALIFIED_NAME
from .utils.dependencies import has_qdrant_client

if has_qdrant_client():
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    _CLIENT: QdrantClient | None = None

    def get_qdrant_client() -> QdrantClient:
        global _CLIENT
        if _CLIENT is None:
            _CLIENT = QdrantClient(path=settings.QDRANT_DB_PATH)
            if not _CLIENT.collection_exists(settings.QDRANT_COLLECTION_NAME):
                _CLIENT.create_collection(
                    collection_name=settings.QDRANT_COLLECTION_NAME,
                    vectors_config=VectorParams(
                        size=settings.QDRANT_VECTOR_DIM, distance=Distance.COSINE
                    ),
                )
        return _CLIENT

    def store_embedding(
        node_id: int, embedding: list[float], qualified_name: str
    ) -> None:
        try:
            client = get_qdrant_client()
            client.upsert(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                points=[
                    PointStruct(
                        id=node_id,
                        vector=embedding,
                        payload={
                            PAYLOAD_NODE_ID: node_id,
                            PAYLOAD_QUALIFIED_NAME: qualified_name,
                        },
                    )
                ],
            )
        except Exception as e:
            logger.warning(
                ls.EMBEDDING_STORE_FAILED.format(name=qualified_name, error=e)
            )

    def search_embeddings(
        query_embedding: list[float], top_k: int | None = None
    ) -> list[tuple[int, float]]:
        effective_top_k = top_k if top_k is not None else settings.QDRANT_TOP_K
        try:
            client = get_qdrant_client()
            result = client.query_points(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                query=query_embedding,
                limit=effective_top_k,
            )
            return [
                (hit.payload[PAYLOAD_NODE_ID], hit.score)
                for hit in result.points
                if hit.payload is not None
            ]
        except Exception as e:
            logger.warning(ls.EMBEDDING_SEARCH_FAILED.format(error=e))
            return []

else:

    def store_embedding(
        node_id: int, embedding: list[float], qualified_name: str
    ) -> None:
        pass

    def search_embeddings(
        query_embedding: list[float], top_k: int | None = None
    ) -> list[tuple[int, float]]:
        return []
