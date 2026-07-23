"""Guards that unit tests can never reach a developer's live vector store.

A developer .env typically sets QDRANT_URL to the local daemon stack; if that
leaks into the unit suite, every --clean code path purges the real collections
and concurrent xdist workers race on the live server (issue #905).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from codebase_rag.config import settings


def test_qdrant_url_is_neutralised_for_unit_tests() -> None:
    # URL mode bypasses QDRANT_DB_PATH entirely, so isolation must force the
    # embedded per-test store even when the environment points at a live
    # server. On a box with no .env this holds by default; the subprocess
    # guard below is what gives it teeth.
    assert settings.QDRANT_URL is None


def test_isolation_fixture_neutralises_env_qdrant_url() -> None:
    # In CI the shipped QDRANT_URL default is already None, so the assertion
    # above cannot catch the isolation fixture being deleted. Re-run it in a
    # subprocess with the environment a developer .env would produce; only
    # the conftest fixture can make it pass there.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            f"{__file__}::test_qdrant_url_is_neutralised_for_unit_tests",
        ],
        env=os.environ | {"QDRANT_URL": "http://127.0.0.1:9"},
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_milvus_uri_is_isolated_for_unit_tests() -> None:
    # The shipped default is a CWD-relative file, which in a checkout is the
    # developer's real embeddings database.
    assert settings.MILVUS_URI != "./.milvus_code_embeddings.db"
    assert Path(settings.MILVUS_URI).is_absolute()
