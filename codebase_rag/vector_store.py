import time
from collections.abc import Sequence

from loguru import logger

from . import logs as ls
from .config import settings
from .constants import PAYLOAD_NODE_ID, PAYLOAD_QUALIFIED_NAME
from .utils.dependencies import has_qdrant_client

if has_qdrant_client():
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    _CLIENT: QdrantClient | None = None

    def close_qdrant_client() -> None:
        global _CLIENT
        if _CLIENT is not None:
            _CLIENT.close()
            _CLIENT = None

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

    def _upsert_with_retry(points: list[PointStruct]) -> None:
        client = get_qdrant_client()
        max_attempts = settings.QDRANT_UPSERT_RETRIES
        base_delay = settings.QDRANT_RETRY_BASE_DELAY
        for attempt in range(1, max_attempts + 1):
            try:
                client.upsert(
                    collection_name=settings.QDRANT_COLLECTION_NAME,
                    points=points,
                )
                return
            except Exception as e:
                if attempt == max_attempts:
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    ls.EMBEDDING_STORE_RETRY.format(
                        attempt=attempt, max_attempts=max_attempts, delay=delay, error=e
                    )
                )
                time.sleep(delay)

    def store_embedding(
        node_id: int, embedding: list[float], qualified_name: str
    ) -> None:
        try:
            _upsert_with_retry(
                [
                    PointStruct(
                        id=node_id,
                        vector=embedding,
                        payload={
                            PAYLOAD_NODE_ID: node_id,
                            PAYLOAD_QUALIFIED_NAME: qualified_name,
                        },
                    )
                ]
            )
        except Exception as e:
            logger.warning(
                ls.EMBEDDING_STORE_FAILED.format(name=qualified_name, error=e)
            )

    def store_embedding_batch(
        points: Sequence[tuple[int, list[float], str]],
    ) -> int:
        if not points:
            return 0
        point_structs = [
            PointStruct(
                id=node_id,
                vector=embedding,
                payload={
                    PAYLOAD_NODE_ID: node_id,
                    PAYLOAD_QUALIFIED_NAME: qualified_name,
                },
            )
            for node_id, embedding, qualified_name in points
        ]
        try:
            _upsert_with_retry(point_structs)
            logger.debug(ls.EMBEDDING_BATCH_STORED.format(count=len(point_structs)))
            return len(point_structs)
        except Exception as e:
            logger.warning(ls.EMBEDDING_BATCH_FAILED.format(error=e))
            return 0

    def delete_project_embeddings(project_name: str, node_ids: Sequence[int]) -> None:
        if not node_ids:
            return
        try:
            logger.info(
                ls.QDRANT_DELETE_PROJECT.format(
                    count=len(node_ids), project=project_name
                )
            )
            client = get_qdrant_client()
            client.delete(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                points_selector=list(node_ids),
            )
            logger.info(ls.QDRANT_DELETE_PROJECT_DONE.format(project=project_name))
        except Exception as e:
            logger.warning(
                ls.QDRANT_DELETE_PROJECT_FAILED.format(project=project_name, error=e)
            )

    def get_stored_point_ids() -> set[int]:
        try:
            client = get_qdrant_client()
            all_ids: set[int] = set()
            offset = None
            while True:
                result = client.scroll(
                    collection_name=settings.QDRANT_COLLECTION_NAME,
                    limit=1000,
                    offset=offset,
                    with_payload=False,
                    with_vectors=False,
                )
                points, next_offset = result
                for point in points:
                    if isinstance(point.id, int):
                        all_ids.add(point.id)
                if next_offset is None:
                    break
                offset = next_offset
            return all_ids
        except Exception as e:
            logger.warning(ls.EMBEDDING_RECONCILE_FAILED.format(error=e))
            return set()

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

    def close_qdrant_client() -> None:
        pass

    def store_embedding(
        node_id: int, embedding: list[float], qualified_name: str
    ) -> None:
        pass

    def store_embedding_batch(
        points: Sequence[tuple[int, list[float], str]],
    ) -> int:
        return 0

    def delete_project_embeddings(project_name: str, node_ids: Sequence[int]) -> None:
        pass

    def get_stored_point_ids() -> set[int]:
        return set()

    def search_embeddings(
        query_embedding: list[float], top_k: int | None = None
    ) -> list[tuple[int, float]]:
        return []
