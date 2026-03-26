from __future__ import annotations

import threading
from collections.abc import Callable

from ..config import settings
from ..utils.dependencies import has_pgvector

_init_lock = threading.Lock()
_initialized: set[str] = set()


class PgInitKey:
    PGVECTOR_EMBEDDINGS = "pgvector_embeddings"
    TOOL_CALL_STORE = "tool_call_store"


if has_pgvector():
    import psycopg

    def pg_connect(*, autocommit: bool = True):
        return psycopg.connect(
            host=settings.PGVECTOR_HOST,
            port=settings.PGVECTOR_PORT,
            user=settings.PGVECTOR_USER,
            password=settings.PGVECTOR_PASSWORD,
            dbname=settings.PGVECTOR_DBNAME,
            autocommit=autocommit,
        )

else:

    def pg_connect(*, autocommit: bool = True):  # type: ignore[no-redef]
        raise RuntimeError("Postgres connection requested but pgvector/psycopg not installed")


def ensure_pg_initialized(key: str, init_fn: Callable[[], None]) -> bool:
    with _init_lock:
        if key in _initialized:
            return False
        init_fn()
        _initialized.add(key)
        return True

