from __future__ import annotations

import atexit
import json
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Required, TypedDict, cast
from urllib.parse import urlsplit

from loguru import logger

from . import constants as cs
from . import exceptions as ex
from . import logs as ls
from .config import settings
from .constants import (
    PAYLOAD_NODE_ID,
    PAYLOAD_QUALIFIED_NAME,
    QDRANT_INSECURE_URL_SCHEME,
    SETTING_QDRANT_DB_PATH,
    VECTOR_DIM_SETTINGS,
    VectorStoreBackend,
)
from .utils.dependencies import has_pymilvus, has_qdrant_client

if TYPE_CHECKING:
    from qdrant_client.models import Filter, Record

_RETRIEVE_BATCH_SIZE = 1000
_MILVUS_VECTOR_FIELD = "embedding"
_PROJECT_OVERFETCH = 4
_PROJECT_MAX_FETCH = 1024
_POINT_ID_NAMESPACE = uuid.UUID(cs.EMBEDDING_POINT_ID_NAMESPACE)

# A Qdrant point id: a UUID for points keyed by symbol, an int for those
# written before issue #2447, keyed by Memgraph's internal node id.
type PointId = int | str | uuid.UUID


def embedding_point_id(project_name: str, qualified_name: str) -> str:
    """The Qdrant point id of one function or method's embedding.

    Derived from the symbol, not from Memgraph's internal node id: an
    incremental sync re-creates a re-parsed file's nodes under new ids, so
    every edit added another point and a deleted function kept its own
    (issue #2447). The project is a namespace of its own, so two projects
    never share a point even where their qualified names meet.
    """
    project_namespace = uuid.uuid5(_POINT_ID_NAMESPACE, project_name)
    return str(uuid.uuid5(project_namespace, qualified_name))


def _nodes_by_point(
    project_name: str, symbols: Mapping[int, str]
) -> dict[str, set[int]]:
    # A set per point: a Function and a Method may share a qualified name,
    # and then share its point.
    nodes: dict[str, set[int]] = {}
    for node_id, qualified_name in symbols.items():
        point_id = embedding_point_id(project_name, qualified_name)
        nodes.setdefault(point_id, set()).add(node_id)
    return nodes


def _in_project(qualified_name: str, project_name: str) -> bool:
    return qualified_name.startswith(project_name + cs.SEPARATOR_DOT)


def _owned_by(
    qualified_name: str, project_name: str, nested_projects: Sequence[str]
) -> bool:
    # `svc.` also prefixes `svc.v2`'s symbols; the longest registered name a
    # symbol sits under owns it, as GraphUpdater._owns decides (issue #1970).
    return _in_project(qualified_name, project_name) and not any(
        _in_project(qualified_name, nested) for nested in nested_projects
    )


def _filter_by_project(
    scored: Sequence[tuple[int, float, Any]], project: str, top_k: int
) -> list[tuple[int, float]]:
    prefix = project + "."
    matching = [
        (node_id, score)
        for node_id, score, qualified_name in scored
        if isinstance(qualified_name, str) and qualified_name.startswith(prefix)
    ]
    return matching[:top_k]


def _search_project_scoped(
    run_query: Callable[[int], Sequence[tuple[int, float, Any]]],
    top_k: int,
    project: str,
) -> list[tuple[int, float]]:
    # ponytail: client-side prefix filter with a widening window, capped at
    # _PROJECT_MAX_FETCH; switch to an indexed payload/expr filter if a
    # project's matches routinely sit past the cap.
    fetch_k = top_k * _PROJECT_OVERFETCH
    while True:
        hits = run_query(fetch_k)
        filtered = _filter_by_project(hits, project, top_k)
        exhausted = len(hits) < fetch_k or fetch_k >= _PROJECT_MAX_FETCH
        if len(filtered) >= top_k or exhausted:
            return filtered
        fetch_k = min(fetch_k * _PROJECT_OVERFETCH, _PROJECT_MAX_FETCH)


_CLIENT: Any | None = None
_CLIENT_BACKEND: VectorStoreBackend | None = None
# The bundled stack's Qdrant URL once probed, so the client and the embedding
# cache agree on where the vectors go and the stack is probed only once.
_BUNDLED_QDRANT: str | None = None
_BUNDLED_QDRANT_PROBED = False

# Each name is the real class or None (dependency absent), typed Any via
# cast so ty does not flag the guarded call sites: the availability gates
# never call the None case, and tests patch the module-level binding.
if has_qdrant_client():
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams
else:
    QdrantClient = cast(Any, None)
    Distance = cast(Any, None)
    PointStruct = cast(Any, None)
    VectorParams = cast(Any, None)

if has_pymilvus():
    from pymilvus import DataType, MilvusClient
else:
    DataType = cast(Any, None)
    MilvusClient = cast(Any, None)


class VectorStore(Protocol):
    backend: VectorStoreBackend

    def store_embedding_batch(
        self, project_name: str, points: Sequence[tuple[int, list[float], str]]
    ) -> int: ...

    def delete_project_embeddings(
        self,
        project_name: str,
        node_ids: Sequence[int],
        nested_projects: Sequence[str] = (),
    ) -> None: ...

    def delete_stale_embeddings(
        self,
        project_name: str,
        current: Mapping[int, str],
        stored: Collection[int] = frozenset(),
        nested_projects: Sequence[str] = (),
    ) -> int: ...

    def clear_all_embeddings(self) -> None: ...

    def verify_stored_ids(
        self, project_name: str, expected: Mapping[int, str]
    ) -> set[int]: ...

    def search_embeddings(
        self,
        query_embedding: list[float],
        top_k: int | None = None,
        project: str | None = None,
    ) -> list[tuple[int, float]]: ...


def close_vector_store_client() -> None:
    global _CLIENT, _CLIENT_BACKEND, _BUNDLED_QDRANT, _BUNDLED_QDRANT_PROBED
    _BUNDLED_QDRANT, _BUNDLED_QDRANT_PROBED = None, False
    if _CLIENT is not None:
        close = getattr(_CLIENT, "close", None)
        if callable(close):
            close()
        _CLIENT = None
        _CLIENT_BACKEND = None


def close_qdrant_client() -> None:
    close_vector_store_client()


# Left to garbage collection, the client's __del__ runs during interpreter
# shutdown, where qdrant's close() imports portalocker and dies with
# ImportError; atexit runs while imports still work.
atexit.register(close_vector_store_client)


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


def get_qdrant_client(validate: bool = True) -> Any:
    global _CLIENT, _CLIENT_BACKEND
    if QdrantClient is None:
        raise RuntimeError("qdrant-client is not installed")

    _ensure_client_backend(VectorStoreBackend.QDRANT)
    if _CLIENT is None:
        if settings.QDRANT_URL:
            client = QdrantClient(
                url=settings.QDRANT_URL, api_key=_qdrant_api_key(settings.QDRANT_URL)
            )
        elif (bundled := _bundled_qdrant_url()) is not None:
            logger.info(ls.QDRANT_USING_BUNDLED.format(url=bundled))
            client = QdrantClient(url=bundled, api_key=None)
        else:
            try:
                client = QdrantClient(path=settings.QDRANT_DB_PATH)
            except Exception as e:
                logger.error(
                    ls.QDRANT_LOCK_ERROR.format(path=settings.QDRANT_DB_PATH, error=e)
                )
                raise
        try:
            _ensure_qdrant_collection(client, validate)
        except Exception:
            # Close and do not cache: a cached client would skip validation
            # on the next call, and embedded Qdrant keeps its folder locked.
            client.close()
            raise
        _CLIENT = client
        _CLIENT_BACKEND = VectorStoreBackend.QDRANT
    return _CLIENT


def _bundled_qdrant_url() -> str | None:
    """The stack's Qdrant, when the embedded store is only the default.

    With QDRANT_URL unset the app always opened the embedded store at the
    cwd-relative QDRANT_DB_PATH, so with `cgr daemon up` running the vectors
    went into a hidden folder of the indexed repository and the stack's Qdrant
    stayed empty (issue #2355). A QDRANT_DB_PATH the user set is their choice
    and is kept, and then the stack is not even probed. Set is told from the
    fields a settings source (environment, .env) or the code supplied, not
    from the value: QDRANT_DB_PATH=./.qdrant_code_embeddings is a choice too.
    """
    global _BUNDLED_QDRANT, _BUNDLED_QDRANT_PROBED
    if (
        settings.QDRANT_URL
        or settings.VECTOR_STORE_BACKEND != VectorStoreBackend.QDRANT
        or SETTING_QDRANT_DB_PATH in settings.model_fields_set
    ):
        return None
    if not _BUNDLED_QDRANT_PROBED:
        from . import stack

        _BUNDLED_QDRANT = stack.bundled_qdrant_url()
        _BUNDLED_QDRANT_PROBED = True
    return _BUNDLED_QDRANT


def embedding_cache_dir() -> Path:
    """The folder the embedding cache is kept in.

    It sits beside the embedded store, as before. When the bundled stack's
    Qdrant takes the vectors it moves to the stack's folder with them: the
    cache holds vectors too, and at the cwd-relative default it still put a
    hidden folder into every indexed repository (issue #2355). Its keys carry
    the embedding model, so one cache serves every project.
    """
    if _bundled_qdrant_url() is not None:
        return settings.CGR_HOME.expanduser()
    return Path(settings.QDRANT_DB_PATH)


def _qdrant_api_key(url: str) -> str | None:
    api_key = settings.QDRANT_API_KEY or None
    if (
        api_key
        and urlsplit(url).scheme == QDRANT_INSECURE_URL_SCHEME
        and not settings.QDRANT_ALLOW_INSECURE_API_KEY
    ):
        raise ValueError(ex.QDRANT_API_KEY_OVER_HTTP)
    return api_key


def _ensure_qdrant_collection(client: Any, validate: bool) -> None:
    if not client.collection_exists(settings.QDRANT_COLLECTION_NAME):
        client.create_collection(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            vectors_config=VectorParams(
                size=settings.QDRANT_VECTOR_DIM, distance=Distance.COSINE
            ),
        )
        return
    if validate:
        _validate_qdrant_collection(client)


def _validate_qdrant_collection(client: Any) -> None:
    # A collection left by a different embedding provider keeps its old
    # vector size; without this check the mismatch only surfaces as an
    # opaque upsert error after the whole graph has been parsed.
    info = client.get_collection(collection_name=settings.QDRANT_COLLECTION_NAME)
    # Named-vector collections (a dict) are not created by this module, so
    # only the single unnamed-vector layout, which carries an int `size`, is
    # checked. Read by attribute rather than isinstance(VectorParams), which
    # is not a class when qdrant-client is absent or stubbed.
    size = getattr(info.config.params.vectors, "size", None)
    if not isinstance(size, int):
        return
    if size != settings.QDRANT_VECTOR_DIM:
        raise ValueError(
            ex.QDRANT_VECTOR_DIM_MISMATCH.format(
                collection=settings.QDRANT_COLLECTION_NAME,
                dim=size,
                expected=settings.QDRANT_VECTOR_DIM,
            )
        )


# Typed per key so the unpacked call is checked against the client's own
# parameters; a plain dict[str, str] could be any of them, `timeout` included.
class _MilvusClientKwargs(TypedDict, total=False):
    uri: Required[str]
    token: str
    db_name: str


def _milvus_client_kwargs() -> _MilvusClientKwargs:
    kwargs = _MilvusClientKwargs(uri=settings.MILVUS_URI)
    if settings.MILVUS_TOKEN:
        kwargs["token"] = settings.MILVUS_TOKEN
    if settings.MILVUS_DB_NAME:
        kwargs["db_name"] = settings.MILVUS_DB_NAME
    return kwargs


def _ensure_milvus_collection(client: Any, validate: bool = True) -> None:
    if client.has_collection(collection_name=settings.MILVUS_COLLECTION_NAME):
        if validate:
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


def get_milvus_client(validate: bool = True) -> Any:
    global _CLIENT, _CLIENT_BACKEND
    if MilvusClient is None:
        raise RuntimeError("pymilvus is not installed")

    _ensure_client_backend(VectorStoreBackend.MILVUS)
    if _CLIENT is None:
        client = MilvusClient(**_milvus_client_kwargs())
        try:
            _ensure_milvus_collection(client, validate)
        except Exception:
            # Close and do not cache, as for Qdrant: a cached client would
            # skip validation on every later call.
            client.close()
            raise
        _CLIENT = client
        _CLIENT_BACKEND = VectorStoreBackend.MILVUS
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


def _store_batch(records: Sequence[Any], upsert: Callable[[], None]) -> int:
    # The count/log/swallow wrapper is identical per backend; only the payload
    # shape and the client call differ, and those stay with each store.
    try:
        upsert()
        logger.debug(ls.EMBEDDING_BATCH_STORED.format(count=len(records)))
        return len(records)
    except Exception as e:
        logger.warning(ls.EMBEDDING_BATCH_FAILED.format(error=e))
        return 0


def _delete_scoped_embeddings(
    backend: VectorStoreBackend,
    project_name: str,
    point_ids: Sequence[PointId],
    delete: Callable[[list[PointId]], None],
) -> None:
    # Shared by every backend: only the client call differs, so the empty
    # guard, the progress logs, and the swallow-and-warn live here once.
    if not point_ids:
        return
    ids = list(point_ids)
    try:
        logger.info(
            ls.VECTOR_STORE_DELETE_PROJECT.format(
                count=len(ids), backend=backend, project=project_name
            )
        )
        delete(ids)
        logger.info(
            ls.VECTOR_STORE_DELETE_PROJECT_DONE.format(
                backend=backend, project=project_name
            )
        )
    except Exception as e:
        logger.warning(
            ls.VECTOR_STORE_DELETE_PROJECT_FAILED.format(
                backend=backend, project=project_name, error=e
            )
        )


def _qdrant_scroll(scroll_filter: Filter, with_payload: list[str]) -> Iterator[Record]:
    client = get_qdrant_client()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            scroll_filter=scroll_filter,
            limit=_RETRIEVE_BATCH_SIZE,
            offset=offset,
            with_payload=with_payload,
            with_vectors=False,
        )
        yield from points
        if offset is None:
            return


def _qdrant_keyed_points(project_name: str) -> Iterator[Record]:
    """The project's points keyed by symbol, each with its node id."""
    from qdrant_client import models

    project = models.Filter(
        must=[
            models.FieldCondition(
                key=cs.PAYLOAD_PROJECT, match=models.MatchValue(value=project_name)
            )
        ]
    )
    return _qdrant_scroll(project, [PAYLOAD_NODE_ID])


def _qdrant_legacy_points(project_name: str) -> list[Record]:
    """The points under the project's prefix written before issue #2447,
    keyed by node id.

    They carry no project, so they are told by the qualified-name prefix, as
    a project-scoped search tells them; a project nested under this one owns
    some of them, which the callers sort out.
    """
    from qdrant_client import models

    unowned = models.Filter(
        must=[
            models.IsEmptyCondition(
                is_empty=models.PayloadField(key=cs.PAYLOAD_PROJECT)
            )
        ]
    )
    return [
        point
        for point in _qdrant_scroll(unowned, [PAYLOAD_NODE_ID, PAYLOAD_QUALIFIED_NAME])
        if _in_project(_payload_qualified_name(point), project_name)
    ]


def _payload_qualified_name(point: Record) -> str:
    qualified_name = (point.payload or {}).get(PAYLOAD_QUALIFIED_NAME)
    return qualified_name if isinstance(qualified_name, str) else ""


def _legacy_point_superseded(
    point: Record, current: Mapping[int, str], stored: Collection[int]
) -> bool:
    # A node-id keyed point goes once its symbol's new point is stored, or
    # when it no longer names its symbol's current node. One whose new point
    # failed to store still answers for the right node, so it stays until a
    # sync stores the replacement.
    node_id = (point.payload or {}).get(PAYLOAD_NODE_ID)
    return node_id in stored or current.get(node_id) != _payload_qualified_name(point)


class QdrantVectorStore(VectorStore):
    backend = VectorStoreBackend.QDRANT

    def store_embedding_batch(
        self, project_name: str, points: Sequence[tuple[int, list[float], str]]
    ) -> int:
        if not points:
            return 0
        point_structs = [
            PointStruct(
                id=embedding_point_id(project_name, qualified_name),
                vector=embedding,
                payload={
                    PAYLOAD_NODE_ID: node_id,
                    PAYLOAD_QUALIFIED_NAME: qualified_name,
                    cs.PAYLOAD_PROJECT: project_name,
                },
            )
            for node_id, embedding, qualified_name in points
        ]
        return _store_batch(point_structs, lambda: _upsert_with_retry(point_structs))

    def delete_project_embeddings(
        self,
        project_name: str,
        node_ids: Sequence[int],
        nested_projects: Sequence[str] = (),
    ) -> None:
        # The node ids key only the points written before issue #2447, and
        # only those of nodes the graph still holds; the project's own points
        # are found in the store, whatever node they last named. A project
        # nested under this one keeps its old points, which the node ids of
        # the prefix-scoped graph read name too.
        def _delete(ids: list[PointId]) -> None:
            get_qdrant_client().delete(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                points_selector=ids,
            )

        try:
            found = [p.id for p in _qdrant_keyed_points(project_name)]
            legacy = _qdrant_legacy_points(project_name)
        except Exception as e:
            logger.warning(
                ls.VECTOR_STORE_DELETE_PROJECT_FAILED.format(
                    backend=self.backend, project=project_name, error=e
                )
            )
            return
        kept: set[PointId] = set()
        for point in legacy:
            if _owned_by(_payload_qualified_name(point), project_name, nested_projects):
                found.append(point.id)
            else:
                kept.add(point.id)
        owned_ids = [node_id for node_id in node_ids if node_id not in kept]
        ids = list(dict.fromkeys([*owned_ids, *found]))
        _delete_scoped_embeddings(self.backend, project_name, ids, _delete)

    def delete_stale_embeddings(
        self,
        project_name: str,
        current: Mapping[int, str],
        stored: Collection[int] = frozenset(),
        nested_projects: Sequence[str] = (),
    ) -> int:
        """Delete the project's points that no longer match `current`.

        `current` maps every function and method the graph holds for the
        project to its qualified name. A point goes when its symbol is gone,
        or when it still names a node the symbol has since left, which
        Memgraph may give to an unrelated node later. A point written before
        issue #2447 goes once the node in `stored` it names has its point
        under the new key, or as soon as it is stale; one a project in
        `nested_projects` owns is left for that project's sync. They are
        logged apart and left out of the returned count of stale points,
        since they are the one-off move to the new keys rather than drift.
        """
        expected = _nodes_by_point(project_name, current)
        stale: list[PointId] = [
            point.id
            for point in _qdrant_keyed_points(project_name)
            if (point.payload or {}).get(PAYLOAD_NODE_ID)
            not in expected.get(str(point.id), set())
        ]
        legacy = [
            point.id
            for point in _qdrant_legacy_points(project_name)
            if _owned_by(_payload_qualified_name(point), project_name, nested_projects)
            and _legacy_point_superseded(point, current, stored)
        ]
        doomed = stale + legacy
        if doomed:
            get_qdrant_client().delete(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                points_selector=doomed,
            )
        if legacy:
            logger.info(
                ls.VECTOR_STORE_REKEYED.format(
                    count=len(legacy), backend=self.backend, project=project_name
                )
            )
        return len(stale)

    def clear_all_embeddings(self) -> None:
        # A clean rebuild wipes every project's graph and reassigns the node
        # ids the points name; stale points crowd out live hits and can map
        # onto unrelated nodes, so the whole collection must go. Failures
        # propagate: a swallowed error would let clean report success while
        # the stale points survive. Validation is skipped because dropping a
        # collection with the wrong vector size is how a user recovers from it.
        client = get_qdrant_client(validate=False)
        try:
            client.delete_collection(collection_name=settings.QDRANT_COLLECTION_NAME)
            client.create_collection(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                vectors_config=VectorParams(
                    size=settings.QDRANT_VECTOR_DIM, distance=Distance.COSINE
                ),
            )
        except Exception:
            # The client was cached unvalidated; if the rebuild failed the
            # collection may still be the wrong size (or gone), so the next
            # caller must open and validate afresh rather than reuse it.
            close_vector_store_client()
            raise
        logger.info(ls.VECTOR_STORE_CLEARED.format(backend=self.backend))

    def verify_stored_ids(
        self, project_name: str, expected: Mapping[int, str]
    ) -> set[int]:
        """The node ids in `expected` whose symbol's point names that node."""
        if not expected:
            return set()
        client = get_qdrant_client()
        nodes = _nodes_by_point(project_name, expected)
        ids_list = list(nodes)
        found_ids: set[int] = set()
        for i in range(0, len(ids_list), _RETRIEVE_BATCH_SIZE):
            points = client.retrieve(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                ids=ids_list[i : i + _RETRIEVE_BATCH_SIZE],
                with_payload=[PAYLOAD_NODE_ID],
                with_vectors=False,
            )
            for point in points:
                node_id = (point.payload or {}).get(PAYLOAD_NODE_ID)
                if node_id in nodes.get(str(point.id), set()):
                    found_ids.add(node_id)
        return found_ids

    def search_embeddings(
        self,
        query_embedding: list[float],
        top_k: int | None = None,
        project: str | None = None,
    ) -> list[tuple[int, float]]:
        effective_top_k = top_k if top_k is not None else settings.QDRANT_TOP_K
        try:
            client = get_qdrant_client()

            def run_query(limit: int) -> list[tuple[int, float, Any]]:
                result = client.query_points(
                    collection_name=settings.QDRANT_COLLECTION_NAME,
                    query=query_embedding,
                    limit=limit,
                )
                return [
                    (
                        hit.payload[PAYLOAD_NODE_ID],
                        hit.score,
                        hit.payload.get(PAYLOAD_QUALIFIED_NAME),
                    )
                    for hit in result.points
                    if hit.payload is not None
                ]

            if project is None:
                return [
                    (node_id, score) for node_id, score, _ in run_query(effective_top_k)
                ]
            return _search_project_scoped(run_query, effective_top_k, project)
        except Exception as e:
            logger.warning(ls.EMBEDDING_SEARCH_FAILED.format(error=e))
            return []


def _milvus_project_scope(project_name: str) -> str:
    # json.dumps writes a string literal Milvus reads back as the same string,
    # quotes and backslashes in the project name included.
    return cs.MILVUS_PREFIX_RANGE_EXPR.format(
        field=PAYLOAD_QUALIFIED_NAME,
        low=json.dumps(project_name + cs.SEPARATOR_DOT, ensure_ascii=False),
        high=json.dumps(project_name + cs.QN_PREFIX_RANGE_END, ensure_ascii=False),
    )


def _milvus_owned_scope(project_name: str, nested_projects: Sequence[str]) -> str:
    # The project's prefix range less each nested project's, which `svc.`'s
    # range also covers.
    scope = _milvus_project_scope(project_name)
    for nested in nested_projects:
        scope = cs.MILVUS_EXCLUDE_EXPR.format(
            scope=scope, excluded=_milvus_project_scope(nested)
        )
    return scope


def _milvus_deleted_count(result: dict[str, int] | list[int]) -> int:
    if isinstance(result, dict):
        count = result.get(cs.MILVUS_DELETE_COUNT_KEY)
        return count if isinstance(count, int) else 0
    return len(result) if isinstance(result, list) else 0


class MilvusVectorStore(VectorStore):
    backend = VectorStoreBackend.MILVUS

    def store_embedding_batch(
        self, project_name: str, points: Sequence[tuple[int, list[float], str]]
    ) -> int:
        # Rows stay keyed by node id, the collection's primary key, so the
        # project is not part of the key: a sync's delete_stale_embeddings
        # drops the rows whose node is gone instead.
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

        def _upsert() -> None:
            get_milvus_client().upsert(
                collection_name=settings.MILVUS_COLLECTION_NAME,
                data=rows,
            )

        return _store_batch(rows, _upsert)

    def delete_project_embeddings(
        self,
        project_name: str,
        node_ids: Sequence[int],
        nested_projects: Sequence[str] = (),
    ) -> None:
        # Rows are keyed by node id alone, so the graph's node ids name them.
        def _delete(ids: list[PointId]) -> None:
            get_milvus_client().delete(
                collection_name=settings.MILVUS_COLLECTION_NAME,
                ids=ids,
            )

        _delete_scoped_embeddings(self.backend, project_name, node_ids, _delete)

    def delete_stale_embeddings(
        self,
        project_name: str,
        current: Mapping[int, str],
        stored: Collection[int] = frozenset(),
        nested_projects: Sequence[str] = (),
    ) -> int:
        """Delete the project's rows whose node is not in `current`.

        A re-parse re-creates a file's nodes under new ids, and the rows of
        the old ones would otherwise stay beside the new rows (issue #2447).
        Rows keep their node-id key, so there is no move to new keys to wait
        on `stored` for; a project in `nested_projects` keeps its rows.
        """
        stale = cs.MILVUS_STALE_ROWS_EXPR.format(
            scope=_milvus_owned_scope(project_name, nested_projects),
            field=PAYLOAD_NODE_ID,
            ids=sorted(current),
        )
        result = get_milvus_client().delete(
            collection_name=settings.MILVUS_COLLECTION_NAME, filter=stale
        )
        return _milvus_deleted_count(result)

    def clear_all_embeddings(self) -> None:
        # Failures propagate, and validation is skipped so a collection of the
        # wrong vector size can be dropped; see
        # QdrantVectorStore.clear_all_embeddings.
        client = get_milvus_client(validate=False)
        try:
            client.drop_collection(settings.MILVUS_COLLECTION_NAME)
            _ensure_milvus_collection(client)
        except Exception:
            close_vector_store_client()
            raise
        logger.info(ls.VECTOR_STORE_CLEARED.format(backend=self.backend))

    def verify_stored_ids(
        self, project_name: str, expected: Mapping[int, str]
    ) -> set[int]:
        if not expected:
            return set()
        client = get_milvus_client()
        found_ids: set[int] = set()
        ids_list = list(expected)
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
        self,
        query_embedding: list[float],
        top_k: int | None = None,
        project: str | None = None,
    ) -> list[tuple[int, float]]:
        effective_top_k = top_k if top_k is not None else settings.MILVUS_TOP_K
        output_fields = (
            [PAYLOAD_NODE_ID, PAYLOAD_QUALIFIED_NAME] if project else [PAYLOAD_NODE_ID]
        )
        try:
            client = get_milvus_client()

            def run_query(limit: int) -> list[tuple[int, float, Any]]:
                result = client.search(
                    collection_name=settings.MILVUS_COLLECTION_NAME,
                    data=[query_embedding],
                    anns_field=_MILVUS_VECTOR_FIELD,
                    limit=limit,
                    output_fields=output_fields,
                )
                if not result:
                    return []
                return [
                    (
                        node_id,
                        _normalize_milvus_score(float(hit.get("distance", 0.0))),
                        _milvus_hit_qualified_name(cast(dict[str, Any], hit)),
                    )
                    for hit in result[0]
                    if isinstance(
                        node_id := _milvus_hit_node_id(cast(dict[str, Any], hit)), int
                    )
                ]

            if project is None:
                return [
                    (node_id, score) for node_id, score, _ in run_query(effective_top_k)
                ]
            return _search_project_scoped(run_query, effective_top_k, project)
        except Exception as e:
            logger.warning(ls.EMBEDDING_SEARCH_FAILED.format(error=e))
            return []


def _milvus_hit_qualified_name(hit: dict[str, Any]) -> str | None:
    entity = hit.get("entity")
    if isinstance(entity, dict) and isinstance(entity.get(PAYLOAD_QUALIFIED_NAME), str):
        return entity[PAYLOAD_QUALIFIED_NAME]
    return None


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
    if urlsplit(uri).scheme in ("http", "https", "tcp"):
        return False
    try:
        lite_version = version("milvus-lite")
    except PackageNotFoundError:
        return False
    # Milvus Lite 3.0.0 reports COSINE as distance instead of similarity:
    # https://github.com/milvus-io/milvus-lite/issues/343
    return lite_version.startswith("3.0")


def _configured_vector_dim(backend: VectorStoreBackend) -> int:
    if backend == VectorStoreBackend.MILVUS:
        return settings.MILVUS_VECTOR_DIM
    return settings.QDRANT_VECTOR_DIM


def _check_vector_dims(
    backend: VectorStoreBackend, vectors: Iterable[list[float]]
) -> None:
    # A model whose output size differs from the collection's fails every
    # write with an opaque backend error, which the batch wrapper swallows
    # one batch at a time; raising here names the setting to change once.
    expected = _configured_vector_dim(backend)
    for vector in vectors:
        if len(vector) != expected:
            raise ValueError(
                ex.EMBEDDING_DIM_MISMATCH.format(
                    dim=len(vector),
                    backend=backend,
                    expected=expected,
                    setting=VECTOR_DIM_SETTINGS[backend],
                )
            )


def store_embedding(
    project_name: str, node_id: int, embedding: list[float], qualified_name: str
) -> None:
    store_embedding_batch(project_name, [(node_id, embedding, qualified_name)])


def store_embedding_batch(
    project_name: str, points: Sequence[tuple[int, list[float], str]]
) -> int:
    vector_store = _get_vector_store()
    if vector_store is None:
        return 0
    _check_vector_dims(vector_store.backend, (emb for _, emb, _ in points))
    return vector_store.store_embedding_batch(project_name, points)


def delete_project_embeddings(
    project_name: str, node_ids: Sequence[int], nested_projects: Sequence[str] = ()
) -> None:
    vector_store = _get_vector_store()
    if vector_store is None:
        return
    vector_store.delete_project_embeddings(project_name, node_ids, nested_projects)


def clear_all_embeddings() -> None:
    vector_store = _get_vector_store()
    if vector_store is None:
        return
    vector_store.clear_all_embeddings()


def delete_stale_embeddings(
    project_name: str,
    current: Mapping[int, str],
    stored: Collection[int] = frozenset(),
    nested_projects: Sequence[str] = (),
) -> int:
    vector_store = _get_vector_store()
    if vector_store is None:
        return 0
    return vector_store.delete_stale_embeddings(
        project_name, current, stored, nested_projects
    )


def verify_stored_ids(project_name: str, expected: Mapping[int, str]) -> set[int]:
    vector_store = _get_vector_store()
    if vector_store is None:
        return set()
    return vector_store.verify_stored_ids(project_name, expected)


def search_embeddings(
    query_embedding: list[float],
    top_k: int | None = None,
    project: str | None = None,
) -> list[tuple[int, float]]:
    vector_store = _get_vector_store()
    if vector_store is None:
        return []
    _check_vector_dims(vector_store.backend, (query_embedding,))
    return vector_store.search_embeddings(query_embedding, top_k=top_k, project=project)
