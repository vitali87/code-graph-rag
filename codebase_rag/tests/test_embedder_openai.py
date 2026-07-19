from __future__ import annotations

import json
from collections.abc import Generator
from unittest.mock import patch

import httpx
import pytest

from codebase_rag import constants as cs
from codebase_rag.config import AppConfig, settings
from codebase_rag.embedder import clear_embedding_cache


class RecordingHandler:
    # (H) MockTransport handler that answers like an OpenAI-compatible
    # (H) /embeddings endpoint and records every request payload; rows are
    # (H) returned in reverse index order so callers must sort by index.
    def __init__(self, dim: int = 4, status_code: int = 200) -> None:
        self.dim = dim
        self.status_code = status_code
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.status_code != 200:
            return httpx.Response(self.status_code, text="upstream boom")
        rows = [
            {"index": i, "embedding": [float(i + 1)] * self.dim}
            for i in range(len(payload["input"]))
        ]
        rows.reverse()
        return httpx.Response(200, json={"data": rows})


def _client_for(handler: RecordingHandler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://embeddings.test/v1"
    )


@pytest.fixture
def openai_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> Generator[None, None, None]:
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", cs.EmbeddingProvider.OPENAI)
    monkeypatch.setattr(settings, "QDRANT_DB_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "OPENAI_EMBEDDING_MODEL", "test-embed")
    monkeypatch.setattr(settings, "OPENAI_EMBEDDING_API_KEY", None)
    monkeypatch.setattr(settings, "OPENAI_EMBEDDING_DIMENSIONS", None)
    monkeypatch.setattr(settings, "OPENAI_EMBEDDING_BATCH_SIZE", 128)
    clear_embedding_cache()
    yield
    clear_embedding_cache()


def test_default_embedding_provider_is_unixcoder() -> None:
    assert (
        AppConfig.model_fields["EMBEDDING_PROVIDER"].default
        == cs.EmbeddingProvider.UNIXCODER
    )


def test_openai_batch_returns_embeddings_in_request_order(
    openai_provider: None,
) -> None:
    from codebase_rag.embedder import embed_code_batch

    handler = RecordingHandler()
    with patch(
        "codebase_rag.embedder._openai_client", side_effect=lambda: _client_for(handler)
    ):
        results = embed_code_batch(["def a(): pass", "def b(): pass"])

    assert results == [[1.0] * 4, [2.0] * 4]
    assert len(handler.requests) == 1
    assert handler.requests[0]["model"] == "test-embed"
    assert handler.requests[0]["input"] == ["def a(): pass", "def b(): pass"]


def test_openai_batch_respects_batch_size(
    openai_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag.embedder import embed_code_batch

    monkeypatch.setattr(settings, "OPENAI_EMBEDDING_BATCH_SIZE", 2)
    handler = RecordingHandler()
    with patch(
        "codebase_rag.embedder._openai_client", side_effect=lambda: _client_for(handler)
    ):
        results = embed_code_batch([f"def f{i}(): pass" for i in range(5)])

    assert len(results) == 5
    assert [len(req["input"]) for req in handler.requests] == [2, 2, 1]


def test_openai_single_embed_uses_endpoint(openai_provider: None) -> None:
    from codebase_rag.embedder import embed_code

    handler = RecordingHandler()
    with patch(
        "codebase_rag.embedder._openai_client", side_effect=lambda: _client_for(handler)
    ):
        result = embed_code("def solo(): pass")

    assert result == [1.0] * 4
    assert len(handler.requests) == 1


def test_openai_embeddings_are_cached(openai_provider: None) -> None:
    from codebase_rag.embedder import embed_code

    handler = RecordingHandler()
    with patch(
        "codebase_rag.embedder._openai_client", side_effect=lambda: _client_for(handler)
    ):
        first = embed_code("def cached(): pass")
        second = embed_code("def cached(): pass")

    assert first == second
    assert len(handler.requests) == 1


def test_openai_cache_is_namespaced_by_model(
    openai_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag.embedder import embed_code

    handler = RecordingHandler()
    with patch(
        "codebase_rag.embedder._openai_client", side_effect=lambda: _client_for(handler)
    ):
        embed_code("def ns(): pass")
        monkeypatch.setattr(settings, "OPENAI_EMBEDDING_MODEL", "other-embed")
        embed_code("def ns(): pass")

    assert len(handler.requests) == 2


def test_openai_dimensions_forwarded_when_set(
    openai_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag.embedder import embed_code

    monkeypatch.setattr(settings, "OPENAI_EMBEDDING_DIMENSIONS", 4)
    handler = RecordingHandler()
    with patch(
        "codebase_rag.embedder._openai_client", side_effect=lambda: _client_for(handler)
    ):
        embed_code("def dims(): pass")

    assert handler.requests[0]["dimensions"] == 4


def test_openai_dimensions_omitted_by_default(openai_provider: None) -> None:
    from codebase_rag.embedder import embed_code

    handler = RecordingHandler()
    with patch(
        "codebase_rag.embedder._openai_client", side_effect=lambda: _client_for(handler)
    ):
        embed_code("def nodims(): pass")

    assert "dimensions" not in handler.requests[0]


def test_openai_http_error_raises_runtime_error(openai_provider: None) -> None:
    from codebase_rag.embedder import embed_code_batch

    handler = RecordingHandler(status_code=500)
    with patch(
        "codebase_rag.embedder._openai_client", side_effect=lambda: _client_for(handler)
    ):
        with pytest.raises(RuntimeError, match="500"):
            embed_code_batch(["def broken(): pass"])


def test_openai_count_mismatch_raises_runtime_error(openai_provider: None) -> None:
    from codebase_rag.embedder import embed_code_batch

    def short_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    client = httpx.Client(
        transport=httpx.MockTransport(short_handler), base_url="http://embeddings.test"
    )
    with patch("codebase_rag.embedder._openai_client", return_value=client):
        with pytest.raises(RuntimeError, match="2"):
            embed_code_batch(["def a(): pass", "def b(): pass"])


def test_openai_client_sends_bearer_token_when_key_set(
    openai_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag.embedder import _openai_client

    monkeypatch.setattr(settings, "OPENAI_EMBEDDING_API_KEY", "sk-test-123")
    with _openai_client() as client:
        assert client.headers["Authorization"] == "Bearer sk-test-123"


def test_openai_client_falls_back_to_openai_api_key_env(
    openai_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag.embedder import _openai_client

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-456")
    with _openai_client() as client:
        assert client.headers["Authorization"] == "Bearer sk-env-456"


def test_openai_client_omits_auth_header_without_key(
    openai_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag.embedder import _openai_client

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with _openai_client() as client:
        assert "Authorization" not in client.headers


def test_openai_client_uses_configured_base_url(
    openai_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag.embedder import _openai_client

    monkeypatch.setattr(
        settings, "OPENAI_EMBEDDING_BASE_URL", "http://localhost:11434/v1"
    )
    with _openai_client() as client:
        assert str(client.base_url).startswith("http://localhost:11434/v1")


def test_semantic_dependencies_do_not_require_torch_for_openai(
    openai_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag.utils import dependencies

    monkeypatch.setattr(dependencies, "has_torch", lambda: False)
    monkeypatch.setattr(dependencies, "has_transformers", lambda: False)
    monkeypatch.setattr(dependencies, "has_qdrant_client", lambda: True)

    assert dependencies.has_semantic_dependencies() is True


def test_semantic_dependencies_still_require_torch_for_unixcoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codebase_rag.utils import dependencies

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", cs.EmbeddingProvider.UNIXCODER)
    monkeypatch.setattr(dependencies, "has_torch", lambda: False)
    monkeypatch.setattr(dependencies, "has_qdrant_client", lambda: True)

    assert dependencies.has_semantic_dependencies() is False


def test_openai_cache_namespace_includes_dimensions(
    openai_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag.embedder import embed_code

    handler = RecordingHandler()
    with patch(
        "codebase_rag.embedder._openai_client", side_effect=lambda: _client_for(handler)
    ):
        embed_code("def dimswitch(): pass")
        monkeypatch.setattr(settings, "OPENAI_EMBEDDING_DIMENSIONS", 4)
        embed_code("def dimswitch(): pass")

    assert len(handler.requests) == 2


def test_openai_request_url_joins_base_path(openai_provider: None) -> None:
    from codebase_rag.embedder import embed_code

    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://embeddings.test/v1"
    )
    with patch("codebase_rag.embedder._openai_client", return_value=client):
        embed_code("def urljoin(): pass")

    assert seen_urls == ["http://embeddings.test/v1/embeddings"]


def test_openai_non_json_response_raises_runtime_error(openai_provider: None) -> None:
    from codebase_rag.embedder import embed_code_batch

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway error</html>")

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://embeddings.test"
    )
    with patch("codebase_rag.embedder._openai_client", return_value=client):
        with pytest.raises(RuntimeError, match="malformed"):
            embed_code_batch(["def a(): pass"])


def test_openai_missing_data_key_raises_runtime_error(openai_provider: None) -> None:
    from codebase_rag.embedder import embed_code_batch

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "wrong shape"})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://embeddings.test"
    )
    with patch("codebase_rag.embedder._openai_client", return_value=client):
        with pytest.raises(RuntimeError, match="malformed"):
            embed_code_batch(["def a(): pass"])


def test_openai_duplicate_index_raises_runtime_error(openai_provider: None) -> None:
    from codebase_rag.embedder import embed_code_batch

    def handler(request: httpx.Request) -> httpx.Response:
        rows = [
            {"index": 0, "embedding": [1.0]},
            {"index": 0, "embedding": [2.0]},
        ]
        return httpx.Response(200, json={"data": rows})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://embeddings.test"
    )
    with patch("codebase_rag.embedder._openai_client", return_value=client):
        with pytest.raises(RuntimeError, match="index"):
            embed_code_batch(["def a(): pass", "def b(): pass"])


def test_openai_out_of_range_index_raises_runtime_error(openai_provider: None) -> None:
    from codebase_rag.embedder import embed_code_batch

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 5, "embedding": [1.0]}]})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://embeddings.test"
    )
    with patch("codebase_rag.embedder._openai_client", return_value=client):
        with pytest.raises(RuntimeError, match="index"):
            embed_code_batch(["def a(): pass"])


def test_openai_batch_mixes_cache_hits_and_misses(openai_provider: None) -> None:
    from codebase_rag.embedder import embed_code_batch

    handler = RecordingHandler()
    with patch(
        "codebase_rag.embedder._openai_client", side_effect=lambda: _client_for(handler)
    ):
        first = embed_code_batch(["def a(): pass", "def b(): pass"])
        second = embed_code_batch(["def a(): pass", "def c(): pass"])

    assert second[0] == first[0]
    assert handler.requests[1]["input"] == ["def c(): pass"]
