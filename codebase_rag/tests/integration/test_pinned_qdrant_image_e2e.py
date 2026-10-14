# Issue #2409: the Qdrant server the packaged stack pins works with the
# qdrant-client cgr installs. The image is read from the compose file itself,
# so a digest bump (Dependabot proposes them) is exercised here before it
# reaches users: store, verify and search go through cgr's own vector store.
from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from codebase_rag import vector_store as vs
from codebase_rag.config import settings
from codebase_rag.constants import NodeLabel, VectorStoreBackend
from codebase_rag.stack import constants as stack_cs
from codebase_rag.types_defs import EmbeddingSymbol

pytestmark = [pytest.mark.integration]

COMPOSE_PATH = (
    Path(__file__).resolve().parents[3] / "codebase_rag" / "docker-compose.yaml"
)
READY_TIMEOUT_S = 60


def _pinned_qdrant_image() -> str:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return str(compose["services"][stack_cs.SERVICE_QDRANT]["image"])


@pytest.fixture(scope="module")
def pinned_qdrant_url() -> Generator[str, None, None]:
    from testcontainers.core.container import DockerContainer

    container = DockerContainer(_pinned_qdrant_image()).with_exposed_ports(6333)
    container.start()
    try:
        url = (
            f"http://{container.get_container_host_ip()}:"
            f"{container.get_exposed_port(6333)}"
        )
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            try:
                with urllib.request.urlopen(f"{url}/readyz", timeout=2) as reply:
                    if reply.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"pinned Qdrant at {url} never became ready")
            time.sleep(0.5)
        yield url
    finally:
        container.stop()


def test_the_pinned_qdrant_serves_cgrs_vector_store(pinned_qdrant_url: str) -> None:
    dim = settings.QDRANT_VECTOR_DIM
    first = [1.0] + [0.0] * (dim - 1)
    second = [0.0, 1.0] + [0.0] * (dim - 2)
    with (
        patch.object(settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.QDRANT),
        patch.object(settings, "QDRANT_URL", pinned_qdrant_url),
        patch.object(settings, "QDRANT_API_KEY", None),
    ):
        vs.close_vector_store_client()
        symbols = {
            1: EmbeddingSymbol(NodeLabel.FUNCTION, "proj.mod.a"),
            2: EmbeddingSymbol(NodeLabel.METHOD, "proj.mod.b"),
        }
        try:
            stored = vs.store_embedding_batch(
                "proj", [(1, first, symbols[1]), (2, second, symbols[2])]
            )
            assert stored == 2
            assert vs.verify_stored_ids("proj", symbols) == {1, 2}
            hits = vs.search_embeddings(first, top_k=1)
            assert [node_id for node_id, _score in hits] == [1]
        finally:
            vs.close_vector_store_client()
