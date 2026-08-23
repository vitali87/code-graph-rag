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


def test_get_ingestor_threads_http_scheme_into_the_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end wiring check for ARCADEDB_HTTP_SCHEME: get_ingestor() must
    # pass it through to ArcadeHttpClient, which is the thing that actually
    # enforces it (a non-loopback host is only allowed over https).
    monkeypatch.setattr(
        "codebase_rag.services.graph.factory.has_neo4j_driver", lambda: True
    )
    from codebase_rag.config import settings
    from codebase_rag.constants import ArcadeBoltScheme, ArcadeHttpScheme
    from codebase_rag.services.graph.arcadedb import ArcadeDBIngestor

    monkeypatch.setattr(settings, "ARCADEDB_USERNAME", "root")
    monkeypatch.setattr(settings, "ARCADEDB_PASSWORD", "pw")
    monkeypatch.setattr(settings, "ARCADEDB_HOST", "db.example.com")
    # bolt+s so only the HTTP guard is under test here; ArcadeDBIngestor's
    # own Bolt guard (test_get_ingestor_threads_bolt_scheme_into_the_ingestor
    # below) would otherwise raise first on a non-loopback host.
    monkeypatch.setattr(settings, "ARCADEDB_BOLT_SCHEME", ArcadeBoltScheme.BOLT_S)

    monkeypatch.setattr(settings, "ARCADEDB_HTTP_SCHEME", ArcadeHttpScheme.HTTP)
    with pytest.raises(ValueError, match="plaintext"):
        get_ingestor(backend=GraphBackend.ARCADEDB)

    monkeypatch.setattr(settings, "ARCADEDB_HTTP_SCHEME", ArcadeHttpScheme.HTTPS)
    ingestor = get_ingestor(backend=GraphBackend.ARCADEDB)
    assert isinstance(ingestor, ArcadeDBIngestor)


def test_get_ingestor_threads_bolt_scheme_into_the_ingestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same wiring check as test_get_ingestor_threads_http_scheme_into_the_http_client,
    # for the Bolt path added in the 2nd review round: ARCADEDB_BOLT_SCHEME
    # must reach ArcadeDBIngestor, which is what actually enforces it.
    monkeypatch.setattr(
        "codebase_rag.services.graph.factory.has_neo4j_driver", lambda: True
    )
    from codebase_rag.config import settings
    from codebase_rag.constants import ArcadeBoltScheme, ArcadeHttpScheme
    from codebase_rag.services.graph.arcadedb import ArcadeDBIngestor

    monkeypatch.setattr(settings, "ARCADEDB_USERNAME", "root")
    monkeypatch.setattr(settings, "ARCADEDB_PASSWORD", "pw")
    monkeypatch.setattr(settings, "ARCADEDB_HOST", "db.example.com")
    # HTTPS so only the Bolt guard is under test here.
    monkeypatch.setattr(settings, "ARCADEDB_HTTP_SCHEME", ArcadeHttpScheme.HTTPS)

    monkeypatch.setattr(settings, "ARCADEDB_BOLT_SCHEME", ArcadeBoltScheme.BOLT)
    with pytest.raises(ValueError, match="plaintext"):
        get_ingestor(backend=GraphBackend.ARCADEDB)

    monkeypatch.setattr(settings, "ARCADEDB_BOLT_SCHEME", ArcadeBoltScheme.BOLT_S)
    ingestor = get_ingestor(backend=GraphBackend.ARCADEDB)
    assert isinstance(ingestor, ArcadeDBIngestor)
