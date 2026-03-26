from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from ..config import settings
from ..utils.dependencies import has_pgvector
from ..utils.pg import PgInitKey, ensure_pg_initialized, pg_connect

_TABLE_NAME = "cgr_tool_call_logs"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


if has_pgvector():
    from psycopg.types.json import Jsonb

    def _get_conn():
        return pg_connect(autocommit=True)

    def _init_db() -> None:
        conn = _get_conn()
        try:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{_TABLE_NAME}" (
                    tool_call_id bigserial PRIMARY KEY,
                    run_id text NOT NULL,
                    cache_key text NOT NULL,
                    repo_path text NOT NULL,
                    repo_state_hash text NOT NULL,
                    stage text NOT NULL,
                    input_json jsonb NOT NULL,
                    output_json jsonb NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS "idx_{_TABLE_NAME}_run_id" ON "{_TABLE_NAME}" (run_id)'
            )
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS "idx_{_TABLE_NAME}_cache_key" ON "{_TABLE_NAME}" (cache_key)'
            )
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS "idx_{_TABLE_NAME}_stage" ON "{_TABLE_NAME}" (stage)'
            )
        finally:
            conn.close()

    def _ensure_db() -> None:
        ensure_pg_initialized(PgInitKey.TOOL_CALL_STORE, _init_db)

    def new_run_id() -> str:
        return str(uuid.uuid4())

    def store_tool_call(
        *,
        run_id: str,
        cache_key: str,
        repo_path: str,
        repo_state_hash: str,
        stage: str,
        tool_input: dict[str, Any],
        tool_output: dict[str, Any],
    ) -> None:
        _ensure_db()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO "{_TABLE_NAME}"
                        (run_id, cache_key, repo_path, repo_state_hash, stage, input_json, output_json)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        cache_key,
                        repo_path,
                        repo_state_hash,
                        stage,
                        Jsonb(tool_input),
                        Jsonb(tool_output),
                    ),
                )
        finally:
            conn.close()

else:

    def new_run_id() -> str:
        return f"no-pgvector-{_now_iso()}"

    def store_tool_call(
        *,
        run_id: str,
        cache_key: str,
        repo_path: str,
        repo_state_hash: str,
        stage: str,
        tool_input: dict[str, Any],
        tool_output: dict[str, Any],
    ) -> None:
        logger.debug(
            "Skipping tool-call persistence (pgvector extras not installed). "
            f"stage={stage} run_id={run_id} cache_key={cache_key}"
        )

