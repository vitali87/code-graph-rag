from contextlib import contextmanager
import time
from collections.abc import Sequence

from loguru import logger

from . import logs as ls
from .config import settings
from .constants import PAYLOAD_NODE_ID, PAYLOAD_QUALIFIED_NAME
from .utils.dependencies import has_pgvector
from .utils.pg import PgInitKey, ensure_pg_initialized, pg_connect
from .utils.pgvector_schema import ensure_pgvector_embeddings_schema

if has_pgvector():
    from pgvector.psycopg import register_vector
    from psycopg.types.json import Jsonb

    def close_pgvector_client() -> None:
        # Keep name for backwards compatibility with main.py
        pass

    def get_pg_connection():
        conn = pg_connect(autocommit=True)
        register_vector(conn)
        return conn

    @contextmanager
    def _pg_cursor():
        conn = get_pg_connection()
        try:
            with conn.cursor() as cur:
                yield cur
        finally:
            conn.close()

    def _init_db():
        conn = pg_connect(autocommit=True)
        try:
            ensure_pgvector_embeddings_schema(conn)
        finally:
            conn.close()

    def _ensure_db():
        ensure_pg_initialized(PgInitKey.PGVECTOR_EMBEDDINGS, _init_db)

    def _upsert_with_retry(points: list[tuple[int, list[float], dict]]) -> None:
        _ensure_db()
        max_attempts = settings.PGVECTOR_UPSERT_RETRIES
        base_delay = settings.PGVECTOR_RETRY_BASE_DELAY
        for attempt in range(1, max_attempts + 1):
            try:
                with _pg_cursor() as cur:
                    cur.executemany(
                        f"""
                        INSERT INTO "{settings.PGVECTOR_TABLE_NAME}" (node_id, embedding, payload)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (node_id) DO UPDATE SET
                            embedding = EXCLUDED.embedding,
                            payload = EXCLUDED.payload
                        """,
                        [(p[0], p[1], Jsonb(p[2])) for p in points],
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
        store_embedding_batch([(node_id, embedding, qualified_name)])

    def store_embedding_batch(
        points: Sequence[tuple[int, list[float], str]],
    ) -> int:
        if not points:
            return 0
        point_data = [
            (
                node_id,
                embedding,
                {
                    PAYLOAD_NODE_ID: node_id,
                    PAYLOAD_QUALIFIED_NAME: qualified_name,
                }
            )
            for node_id, embedding, qualified_name in points
        ]
        try:
            _upsert_with_retry(point_data)
            logger.debug(ls.EMBEDDING_BATCH_STORED.format(count=len(point_data)))
            return len(point_data)
        except Exception as e:
            logger.warning(ls.EMBEDDING_BATCH_FAILED.format(error=e))
            return 0

    def delete_project_embeddings(project_name: str, node_ids: Sequence[int]) -> None:
        if not node_ids:
            return
        _ensure_db()
        try:
            logger.info(
                ls.PGVECTOR_DELETE_PROJECT.format(
                    count=len(node_ids), project=project_name
                )
            )
            with _pg_cursor() as cur:
                cur.execute(
                    f'DELETE FROM "{settings.PGVECTOR_TABLE_NAME}" WHERE node_id = ANY(%s)',
                    (list(node_ids),),
                )
            logger.info(ls.PGVECTOR_DELETE_PROJECT_DONE.format(project=project_name))
        except Exception as e:
            logger.warning(
                ls.PGVECTOR_DELETE_PROJECT_FAILED.format(project=project_name, error=e)
            )

    def verify_stored_ids(expected_ids: set[int]) -> set[int]:
        if not expected_ids:
            return set()
        _ensure_db()
        with _pg_cursor() as cur:
            cur.execute(
                f'SELECT node_id FROM "{settings.PGVECTOR_TABLE_NAME}" WHERE node_id = ANY(%s)',
                (list(expected_ids),),
            )
            rows = cur.fetchall()
            return {int(row[0]) for row in rows if row and row[0] is not None}

    def search_embeddings(
        query_embedding: list[float], top_k: int | None = None
    ) -> list[tuple[int, float]]:
        effective_top_k = top_k if top_k is not None else settings.PGVECTOR_TOP_K
        _ensure_db()
        try:
            with _pg_cursor() as cur:
                # PGVector cosine distance operator `<=>`
                # PGVector naturally returns cosine similarity. Similarity = 1 - Distance
                cur.execute(
                    f"""
                    SELECT payload, 1 - (embedding <=> %s) AS score
                    FROM "{settings.PGVECTOR_TABLE_NAME}"
                    ORDER BY embedding <=> %s
                    LIMIT %s
                    """,
                    (query_embedding, query_embedding, effective_top_k),
                )
                rows = cur.fetchall()
                return [
                    (row[0].get(PAYLOAD_NODE_ID), row[1])
                    for row in rows
                    if row[0] is not None
                ]
        except Exception as e:
            logger.warning(ls.EMBEDDING_SEARCH_FAILED.format(error=e))
            return []

else:

    def close_pgvector_client() -> None:
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

    def verify_stored_ids(expected_ids: set[int]) -> set[int]:
        return set()

    def search_embeddings(
        query_embedding: list[float], top_k: int | None = None
    ) -> list[tuple[int, float]]:
        return []
