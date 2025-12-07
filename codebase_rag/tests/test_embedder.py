from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.utils.dependencies import has_torch, has_transformers


def _has_semantic_deps() -> bool:
    return has_torch() and has_transformers()


@pytest.fixture
def mock_unixcoder() -> MagicMock:
    mock_model = MagicMock()
    mock_model.eval.return_value = mock_model
    mock_model.cuda.return_value = mock_model

    mock_param = MagicMock()
    mock_param.device = "cpu"
    mock_model.parameters.return_value = iter([mock_param])

    mock_model.tokenize.return_value = [[1, 2, 3, 4, 5]]

    return mock_model


@pytest.fixture
def reset_model_cache() -> Generator[None, None, None]:
    if _has_semantic_deps():
        from codebase_rag.embedder import (
            get_model,  # ty: ignore[possibly-missing-import]
        )

        get_model.cache_clear()
    yield
    if _has_semantic_deps():
        from codebase_rag.embedder import (
            get_model,  # ty: ignore[possibly-missing-import]
        )

        get_model.cache_clear()


@pytest.mark.skipif(not _has_semantic_deps(), reason="torch/transformers not installed")
def test_embed_code_returns_768_dimensional_vector(
    mock_unixcoder: MagicMock, reset_model_cache: None
) -> None:
    import torch

    mock_embedding = torch.zeros(1, 768)
    mock_unixcoder.return_value = (torch.zeros(1, 5, 768), mock_embedding)

    with patch("codebase_rag.embedder.get_model", return_value=mock_unixcoder):
        from codebase_rag.embedder import embed_code

        result = embed_code("def hello(): pass")

    assert isinstance(result, list)
    assert len(result) == 768


@pytest.mark.skipif(not _has_semantic_deps(), reason="torch/transformers not installed")
def test_embed_code_calls_tokenize(
    mock_unixcoder: MagicMock, reset_model_cache: None
) -> None:
    import torch

    mock_embedding = torch.zeros(1, 768)
    mock_unixcoder.return_value = (torch.zeros(1, 5, 768), mock_embedding)

    with patch("codebase_rag.embedder.get_model", return_value=mock_unixcoder):
        from codebase_rag.embedder import embed_code

        embed_code("def test(): return 42", max_length=256)

    mock_unixcoder.tokenize.assert_called_once_with(
        ["def test(): return 42"], max_length=256
    )


@pytest.mark.skipif(not _has_semantic_deps(), reason="torch/transformers not installed")
def test_embed_code_uses_default_max_length(
    mock_unixcoder: MagicMock, reset_model_cache: None
) -> None:
    import torch

    mock_embedding = torch.zeros(1, 768)
    mock_unixcoder.return_value = (torch.zeros(1, 5, 768), mock_embedding)

    with patch("codebase_rag.embedder.get_model", return_value=mock_unixcoder):
        from codebase_rag.embedder import embed_code

        embed_code("x = 1")

    mock_unixcoder.tokenize.assert_called_once_with(["x = 1"], max_length=512)


@pytest.mark.skipif(not _has_semantic_deps(), reason="torch/transformers not installed")
def test_get_model_is_cached(reset_model_cache: None) -> None:
    from codebase_rag.embedder import get_model  # ty: ignore[possibly-missing-import]

    with patch("codebase_rag.embedder.UniXcoder") as mock_unixcoder_class:
        mock_instance = MagicMock()
        mock_instance.eval.return_value = mock_instance
        mock_unixcoder_class.return_value = mock_instance

        with patch("codebase_rag.embedder.torch.cuda.is_available", return_value=False):
            model1 = get_model()
            model2 = get_model()

    assert model1 is model2
    mock_unixcoder_class.assert_called_once()


@pytest.mark.skipif(not _has_semantic_deps(), reason="torch/transformers not installed")
def test_get_model_uses_cuda_when_available(reset_model_cache: None) -> None:
    from codebase_rag.embedder import get_model  # ty: ignore[possibly-missing-import]

    with patch("codebase_rag.embedder.UniXcoder") as mock_unixcoder_class:
        mock_instance = MagicMock()
        mock_instance.eval.return_value = mock_instance
        mock_instance.cuda.return_value = mock_instance
        mock_unixcoder_class.return_value = mock_instance

        with patch("codebase_rag.embedder.torch.cuda.is_available", return_value=True):
            get_model()

    mock_instance.cuda.assert_called_once()


@pytest.mark.skipif(not _has_semantic_deps(), reason="torch/transformers not installed")
def test_get_model_does_not_use_cuda_when_unavailable(reset_model_cache: None) -> None:
    from codebase_rag.embedder import get_model  # ty: ignore[possibly-missing-import]

    with patch("codebase_rag.embedder.UniXcoder") as mock_unixcoder_class:
        mock_instance = MagicMock()
        mock_instance.eval.return_value = mock_instance
        mock_unixcoder_class.return_value = mock_instance

        with patch("codebase_rag.embedder.torch.cuda.is_available", return_value=False):
            get_model()

    mock_instance.cuda.assert_not_called()


@pytest.mark.skipif(not _has_semantic_deps(), reason="torch/transformers not installed")
@pytest.mark.slow
def test_embed_code_integration(reset_model_cache: None) -> None:
    from codebase_rag.embedder import embed_code

    code = "def add(a, b): return a + b"
    result = embed_code(code)

    assert isinstance(result, list)
    assert len(result) == 768
    assert all(isinstance(x, float) for x in result)


@pytest.mark.skipif(not _has_semantic_deps(), reason="torch/transformers not installed")
@pytest.mark.slow
def test_similar_code_has_similar_embeddings(reset_model_cache: None) -> None:
    from codebase_rag.embedder import embed_code

    code1 = "def add(a, b): return a + b"
    code2 = "def sum(x, y): return x + y"
    code3 = "class DatabaseConnection: pass"

    emb1 = embed_code(code1)
    emb2 = embed_code(code2)
    emb3 = embed_code(code3)

    def cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        return dot / (norm_a * norm_b)

    sim_1_2 = cosine_similarity(emb1, emb2)
    sim_1_3 = cosine_similarity(emb1, emb3)

    assert sim_1_2 > sim_1_3


def test_embed_code_raises_without_dependencies() -> None:
    if _has_semantic_deps():
        pytest.skip("Dependencies are installed")

    from codebase_rag.embedder import embed_code

    with pytest.raises(RuntimeError, match="Semantic search requires"):
        embed_code("x = 1")
