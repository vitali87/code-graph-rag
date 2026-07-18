from __future__ import annotations

import time
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Protocol, cast

from loguru import logger

from . import logs as ls
from .config import settings
from .constants import PAYLOAD_NODE_ID, PAYLOAD_QUALIFIED_NAME, VectorStoreBackend
from .utils.dependencies import has_pymilvus, has_qdrant_client

_RETRIEVE_BATCH_SIZE = 1000
_MILVUS_VECTOR_FIELD = "embedding"

_CLIENT: Any | None = None
_CLIENT_BACKEND: VectorStoreBackend | None = None

if has_qdrant_client():
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams
else:
    QdrantClient = None  # type: ignore[assignment]
    Distance = None  # type: ignore[assignment]
    PointStruct = None  # type: ignore[assignment]
    VectorParams = None  # type: ignore[assignment]

if has_pymilvus():
    from pymilvus import DataType, MilvusClient
else:
    DataType = None  # type: ignore[assignment]
    MilvusClient = None  # type: ignore[assignment]


class VectorStore(Protocol):
    backend: VectorStoreBackend

    def store_embedding_batch(
        self, points: Sequence[tuple[int, list[float], str]]
    ) -> int: ...

    def delete_project_embeddings(
        self, project_name: str, node_ids: Sequence[int]
    ) -> None: ...

    def verify_stored_ids(self, expected_ids: set[int]) -> set[int]: ...

    def search_embeddings(
        self, query_embedding: list[float], top_k: int | None = None
    ) -> list[tuple[int, float]]: ...


def close_vector_store_client() -> None:
    global _CLIENT, _CLIENT_BACKEND
    if _CLIENT is not None:
        close = getattr(_CLIENT, "close", None)
        if callable(close):
            close()
        _CLIENT = None
        _CLIENT_BACKEND = None


def close_qdrant_client() -> None:
    close_vector_store_client()


def _selected_backend() -> VectorStoreBackend | None:
    try:
        return VectorStoreBackend(str(settings.VECTOR_STORE_BACKEND).lower())
    except ValueError:
        logger.warning(
            ls.VECTOR_STORE_BACKEND_UNKNOWN.format(
                backend=settings.VECTOR_STORE_BACKEND
            )
        )
        return None


def _get_vector_store() -> VectorStore | None:
    backend = _selected_backend()
    if backend == VectorStoreBackend.MILVUS:
        if not has_pymilvus():
            logger.warning(ls.VECTOR_STORE_BACKEND_UNAVAILABLE.format(backend=backend))
            return None
        return MilvusVectorStore()
    if backend == VectorStoreBackend.QDRANT:
        if not has_qdrant_client():
            logger.warning(ls.VECTOR_STORE_BACKEND_UNAVAILABLE.format(backend=backend))
            return None
        return QdrantVectorStore()
    return None


def _ensure_client_backend(backend: VectorStoreBackend) -> None:
    if _CLIENT is not None and _CLIENT_BACKEND != backend:
        close_vector_store_client()


def get_qdrant_client() -> Any:
    global _CLIENT, _CLIENT_BACKEND
    if QdrantClient is None:
        raise RuntimeError("qdrant-client is not installed")

    _ensure_client_backend(VectorStoreBackend.QDRANT)
    if _CLIENT is None:
        if settings.QDRANT_URL:
            _CLIENT = QdrantClient(url=settings.QDRANT_URL)
        else:
            try:
                _CLIENT = QdrantClient(path=settings.QDRANT_DB_PATH)
            except Exception as e:
                logger.error(
                    ls.QDRANT_LOCK_ERROR.format(path=settings.QDRANT_DB_PATH, error=e)
                )
                raise
        _CLIENT_BACKEND = VectorStoreBackend.QDRANT
        if not _CLIENT.collection_exists(settings.QDRANT_COLLECTION_NAME):
            _CLIENT.create_collection(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                vectors_config=VectorParams(
                    size=settings.QDRANT_VECTOR_DIM, distance=Distance.COSINE
                ),
            )
    return _CLIENT


def _milvus_client_kwargs() -> dict[str, str]:
    kwargs = {"uri": settings.MILVUS_URI}
    if settings.MILVUS_TOKEN:
        kwargs["token"] = settings.MILVUS_TOKEN
    if settings.MILVUS_DB_NAME:
        kwargs["db_name"] = settings.MILVUS_DB_NAME
    return kwargs


def _ensure_milvus_collection(client: Any) -> None:
    if client.has_collection(collection_name=settings.MILVUS_COLLECTION_NAME):
        _validate_milvus_collection(client)
        return

    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(
        field_name=PAYLOAD_NODE_ID,
        datatype=DataType.INT64,
        is_primary=True,
    )
    schema.add_field(
        field_name=_MILVUS_VECTOR_FIELD,
        datatype=DataType.FLOAT_VECTOR,
        dim=settings.MILVUS_VECTOR_DIM,
    )
    schema.add_field(
        field_name=PAYLOAD_QUALIFIED_NAME,
        datatype=DataType.VARCHAR,
        max_length=65535,
    )

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name=_MILVUS_VECTOR_FIELD,
        index_type="AUTOINDEX",
        metric_type="COSINE",
    )
    client.create_collection(
        collection_name=settings.MILVUS_COLLECTION_NAME,
        schema=schema,
        index_params=index_params,
        consistency_level=settings.MILVUS_CONSISTENCY_LEVEL,
    )


def _validate_milvus_collection(client: Any) -> None:
    description = client.describe_collection(
        collection_name=settings.MILVUS_COLLECTION_NAME
    )
    fields = {
        field.get("name"): field
        for field in description.get("fields", [])
        if isinstance(field, dict)
    }
    missing = {
        PAYLOAD_NODE_ID,
        _MILVUS_VECTOR_FIELD,
        PAYLOAD_QUALIFIED_NAME,
    } - set(fields)
    if missing:
        missing_fields = ", ".join(sorted(missing))
        raise ValueError(
            f"Milvus collection '{settings.MILVUS_COLLECTION_NAME}' is missing "
            f"required field(s): {missing_fields}"
        )

    vector_field = fields[_MILVUS_VECTOR_FIELD]
    dim = vector_field.get("params", {}).get("dim")
    if dim is not None and int(dim) != settings.MILVUS_VECTOR_DIM:
        raise ValueError(
            f"Milvus collection '{settings.MILVUS_COLLECTION_NAME}' has vector "
            f"dimension {dim}, expected {settings.MILVUS_VECTOR_DIM}"
        )


def get_milvus_client() -> Any:
    global _CLIENT, _CLIENT_BACKEND
    if MilvusClient is None:
        raise RuntimeError("pymilvus is not installed")

    _ensure_client_backend(VectorStoreBackend.MILVUS)
    if _CLIENT is None:
        _CLIENT = MilvusClient(**_milvus_client_kwargs())
        _CLIENT_BACKEND = VectorStoreBackend.MILVUS
        _ensure_milvus_collection(_CLIENT)
    return _CLIENT


def _upsert_with_retry(points: list[Any]) -> None:
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


class QdrantVectorStore:
    backend = VectorStoreBackend.QDRANT

    def store_embedding_batch(
        self, points: Sequence[tuple[int, list[float], str]]
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

    def delete_project_embeddings(
        self, project_name: str, node_ids: Sequence[int]
    ) -> None:
        if not node_ids:
            return
        try:
            logger.info(
                ls.VECTOR_STORE_DELETE_PROJECT.format(
                    count=len(node_ids), backend=self.backend, project=project_name
                )
            )
            client = get_qdrant_client()
            client.delete(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                points_selector=list(node_ids),
            )
            logger.info(
                ls.VECTOR_STORE_DELETE_PROJECT_DONE.format(
                    backend=self.backend, project=project_name
                )
            )
        except Exception as e:
            logger.warning(
                ls.VECTOR_STORE_DELETE_PROJECT_FAILED.format(
                    backend=self.backend, project=project_name, error=e
                )
            )

    def verify_stored_ids(self, expected_ids: set[int]) -> set[int]:
        if not expected_ids:
            return set()
        client = get_qdrant_client()
        found_ids: set[int] = set()
        ids_list = list(expected_ids)
        for i in range(0, len(ids_list), _RETRIEVE_BATCH_SIZE):
            points = client.retrieve(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                ids=ids_list[i : i + _RETRIEVE_BATCH_SIZE],
                with_payload=False,
                with_vectors=False,
            )
            found_ids.update(p.id for p in points if isinstance(p.id, int))
        return found_ids

    def search_embeddings(
        self, query_embedding: list[float], top_k: int | None = None
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


class MilvusVectorStore:
    backend = VectorStoreBackend.MILVUS

    def store_embedding_batch(
        self, points: Sequence[tuple[int, list[float], str]]
    ) -> int:
        if not points:
            return 0
        rows = [
            {
                PAYLOAD_NODE_ID: node_id,
                _MILVUS_VECTOR_FIELD: embedding,
                PAYLOAD_QUALIFIED_NAME: qualified_name,
            }
            for node_id, embedding, qualified_name in points
        ]
        try:
            client = get_milvus_client()
            client.upsert(
                collection_name=settings.MILVUS_COLLECTION_NAME,
                data=rows,
            )
            logger.debug(ls.EMBEDDING_BATCH_STORED.format(count=len(rows)))
            return len(rows)
        except Exception as e:
            logger.warning(ls.EMBEDDING_BATCH_FAILED.format(error=e))
            return 0

    def delete_project_embeddings(
        self, project_name: str, node_ids: Sequence[int]
    ) -> None:
        if not node_ids:
            return
        try:
            logger.info(
                ls.VECTOR_STORE_DELETE_PROJECT.format(
                    count=len(node_ids), backend=self.backend, project=project_name
                )
            )
            client = get_milvus_client()
            client.delete(
                collection_name=settings.MILVUS_COLLECTION_NAME,
                ids=list(node_ids),
            )
            logger.info(
                ls.VECTOR_STORE_DELETE_PROJECT_DONE.format(
                    backend=self.backend, project=project_name
                )
            )
        except Exception as e:
            logger.warning(
                ls.VECTOR_STORE_DELETE_PROJECT_FAILED.format(
                    backend=self.backend, project=project_name, error=e
                )
            )

    def verify_stored_ids(self, expected_ids: set[int]) -> set[int]:
        if not expected_ids:
            return set()
        client = get_milvus_client()
        found_ids: set[int] = set()
        ids_list = list(expected_ids)
        for i in range(0, len(ids_list), _RETRIEVE_BATCH_SIZE):
            rows = client.get(
                collection_name=settings.MILVUS_COLLECTION_NAME,
                ids=ids_list[i : i + _RETRIEVE_BATCH_SIZE],
                output_fields=[PAYLOAD_NODE_ID],
            )
            for row in rows:
                node_id = row.get(PAYLOAD_NODE_ID)
                if isinstance(node_id, int):
                    found_ids.add(node_id)
        return found_ids

    def search_embeddings(
        self, query_embedding: list[float], top_k: int | None = None
    ) -> list[tuple[int, float]]:
        effective_top_k = top_k if top_k is not None else settings.MILVUS_TOP_K
        try:
            client = get_milvus_client()
            result = client.search(
                collection_name=settings.MILVUS_COLLECTION_NAME,
                data=[query_embedding],
                anns_field=_MILVUS_VECTOR_FIELD,
                limit=effective_top_k,
                output_fields=[PAYLOAD_NODE_ID],
            )
            if not result:
                return []
            return [
                (node_id, _normalize_milvus_score(float(hit.get("distance", 0.0))))
                for hit in result[0]
                if isinstance(
                    node_id := _milvus_hit_node_id(cast(dict[str, Any], hit)), int
                )
            ]
        except Exception as e:
            logger.warning(ls.EMBEDDING_SEARCH_FAILED.format(error=e))
            return []


def _milvus_hit_node_id(hit: dict[str, Any]) -> int | None:
    entity = hit.get("entity")
    if isinstance(entity, dict) and isinstance(entity.get(PAYLOAD_NODE_ID), int):
        return entity[PAYLOAD_NODE_ID]
    if isinstance(hit.get("id"), int):
        return hit["id"]
    return None


def _normalize_milvus_score(raw_score: float) -> float:
    if _uses_milvus_lite_30_cosine_distance():
        return 1.0 - raw_score
    return raw_score


def _uses_milvus_lite_30_cosine_distance() -> bool:
    uri = settings.MILVUS_URI
    if uri.startswith(("http://", "https://", "tcp://")):
        return False
    try:
        lite_version = version("milvus-lite")
    except PackageNotFoundError:
        return False
    # (H) Milvus Lite 3.0.0 reports COSINE as distance instead of similarity:
    # (H) https://github.com/milvus-io/milvus-lite/issues/343
    return lite_version in {"3.0", "3.0.0"}


def store_embedding(node_id: int, embedding: list[float], qualified_name: str) -> None:
    store_embedding_batch([(node_id, embedding, qualified_name)])


def store_embedding_batch(points: Sequence[tuple[int, list[float], str]]) -> int:
    vector_store = _get_vector_store()
    if vector_store is None:
        return 0
    return vector_store.store_embedding_batch(points)


def delete_project_embeddings(project_name: str, node_ids: Sequence[int]) -> None:
    vector_store = _get_vector_store()
    if vector_store is None:
        return
    vector_store.delete_project_embeddings(project_name, node_ids)


def verify_stored_ids(expected_ids: set[int]) -> set[int]:
    vector_store = _get_vector_store()
    if vector_store is None:
        return set()
    return vector_store.verify_stored_ids(expected_ids)


def search_embeddings(
    query_embedding: list[float], top_k: int | None = None
) -> list[tuple[int, float]]:
    vector_store = _get_vector_store()
    if vector_store is None:
        return []
    return vector_store.search_embeddings(query_embedding, top_k=top_k)
