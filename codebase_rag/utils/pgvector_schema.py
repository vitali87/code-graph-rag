from __future__ import annotations

from ..config import settings


def ensure_pgvector_embeddings_schema(
    conn,
    *,
    table_name: str | None = None,
    vector_dim: int | None = None,
) -> None:
    """
    Create the embeddings table schema.

    Assumption: the caller drops all tables before running, so we intentionally
    do NOT include any ALTER/patch/migration logic here.
    """
    effective_table = table_name or settings.PGVECTOR_TABLE_NAME
    effective_dim = vector_dim or settings.PGVECTOR_DIM

    # Extensions
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # Table
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{effective_table}" (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            node_id bigint NOT NULL UNIQUE,
            embedding vector({effective_dim}),
            payload jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # updated_at auto-maintained by Postgres (idempotent)
    conn.execute(
        f"""
        CREATE OR REPLACE FUNCTION "{effective_table}__set_updated_at"()
        RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    conn.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                WHERE t.tgname = '{effective_table}__set_updated_at_trg'
                  AND c.relname = '{effective_table}'
                  AND NOT t.tgisinternal
            ) THEN
                CREATE TRIGGER "{effective_table}__set_updated_at_trg"
                BEFORE UPDATE ON "{effective_table}"
                FOR EACH ROW
                EXECUTE FUNCTION "{effective_table}__set_updated_at"();
            END IF;
        END
        $$;
        """
    )

