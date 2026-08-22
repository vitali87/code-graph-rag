import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.exceptions import GraphBackendUnavailableError
from codebase_rag.services.graph.factory import get_dialect, get_ingestor
from codebase_rag.services.graph.memgraph import MemgraphDialect, MemgraphIngestor


def test_default_backend_is_memgraph(monkeypatch: pytest.MonkeyPatch) -> None:
    from codebase_rag.config import settings

    assert settings.GRAPH_BACKEND == GraphBackend.MEMGRAPH


def test_get_dialect_returns_memgraph_by_default() -> None:
    assert isinstance(get_dialect(), MemgraphDialect)


def test_get_dialect_honours_explicit_backend() -> None:
    from codebase_rag.services.graph.arcadedb import ArcadeDBDialect

    assert isinstance(get_dialect(GraphBackend.ARCADEDB), ArcadeDBDialect)


def test_get_ingestor_returns_memgraph_ingestor() -> None:
    ingestor = get_ingestor()
    assert isinstance(ingestor, MemgraphIngestor)


def test_get_ingestor_raises_when_driver_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A missing graph backend is fatal, unlike a missing vector store which
    # degrades to no semantic search.
    monkeypatch.setattr(
        "codebase_rag.services.graph.factory.has_neo4j_driver", lambda: False
    )
    with pytest.raises(
        GraphBackendUnavailableError, match=r"code-graph-rag\[arcadedb\]"
    ):
        get_ingestor(backend=GraphBackend.ARCADEDB)


@pytest.mark.parametrize(
    ("username", "password"),
    [
        pytest.param(None, None, id="both_missing"),
        pytest.param("arcade_user", None, id="password_missing"),
        pytest.param(None, "arcade_pass", id="username_missing"),
    ],
)
def test_arcadedb_requires_credentials(
    monkeypatch: pytest.MonkeyPatch, username: str | None, password: str | None
) -> None:
    # ArcadeDB's Bolt listener rejects the `none` auth scheme outright. The
    # guard uses `or`, so each half-missing case must be pinned individually.
    monkeypatch.setattr(
        "codebase_rag.services.graph.factory.has_neo4j_driver", lambda: True
    )
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "ARCADEDB_USERNAME", username)
    monkeypatch.setattr(settings, "ARCADEDB_PASSWORD", password)
    with pytest.raises(ValueError, match="credentials"):
        get_ingestor(backend=GraphBackend.ARCADEDB)
