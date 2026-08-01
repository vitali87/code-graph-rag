# MPS command-buffer hygiene (issue #689): on Apple Silicon the embedding
# pass wedges inside Metal's waitUntilCompleted when command buffers pile
# up across thousands of batches, so the embedder must synchronize and
# release cached Metal memory after every batch it runs on MPS.

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.embedder import clear_embedding_cache
from codebase_rag.utils.dependencies import has_torch, has_transformers


def _has_semantic_deps() -> bool:
    return has_torch() and has_transformers()


pytestmark = pytest.mark.skipif(
    not _has_semantic_deps(), reason="torch/transformers not installed"
)


@pytest.fixture(autouse=True)
def reset_cache() -> Generator[None, None, None]:
    clear_embedding_cache()
    yield
    clear_embedding_cache()


@pytest.fixture
def mock_unixcoder() -> MagicMock:
    mock_model = MagicMock()
    mock_model.eval.return_value = mock_model

    mock_param = MagicMock()
    mock_param.device = "cpu"
    mock_model.parameters.return_value = iter([mock_param])

    return mock_model


@pytest.fixture(autouse=True)
def reset_batch_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    from codebase_rag import embedder

    monkeypatch.setattr(embedder, "_batches_since_cache_drop", 0)


class TestSyncAfterBatch:
    def test_mps_device_synchronizes_every_batch(self) -> None:
        from codebase_rag.embedder import _sync_after_batch

        with (
            patch("codebase_rag.embedder.torch.mps.synchronize") as mock_sync,
            patch("codebase_rag.embedder.torch.mps.empty_cache") as mock_empty,
        ):
            _sync_after_batch(cs.EmbeddingDevice.MPS)

        mock_sync.assert_called_once()
        mock_empty.assert_not_called()

    def test_cache_dropped_once_per_interval(self) -> None:
        from codebase_rag.embedder import _sync_after_batch

        with (
            patch("codebase_rag.embedder.torch.mps.synchronize") as mock_sync,
            patch("codebase_rag.embedder.torch.mps.empty_cache") as mock_empty,
        ):
            for _ in range(cs.EMBEDDING_MPS_CACHE_DROP_INTERVAL + 1):
                _sync_after_batch(cs.EmbeddingDevice.MPS)

        assert mock_sync.call_count == cs.EMBEDDING_MPS_CACHE_DROP_INTERVAL + 1
        mock_empty.assert_called_once()

    def test_counter_resets_after_drop(self) -> None:
        from codebase_rag.embedder import _sync_after_batch

        with (
            patch("codebase_rag.embedder.torch.mps.synchronize"),
            patch("codebase_rag.embedder.torch.mps.empty_cache") as mock_empty,
        ):
            for _ in range(2 * cs.EMBEDDING_MPS_CACHE_DROP_INTERVAL):
                _sync_after_batch(cs.EmbeddingDevice.MPS)

        assert mock_empty.call_count == 2

    @pytest.mark.parametrize(
        "device", [cs.EmbeddingDevice.CPU, cs.EmbeddingDevice.CUDA]
    )
    def test_other_devices_do_nothing(self, device: cs.EmbeddingDevice) -> None:
        from codebase_rag.embedder import _sync_after_batch

        with (
            patch("codebase_rag.embedder.torch.mps.synchronize") as mock_sync,
            patch("codebase_rag.embedder.torch.mps.empty_cache") as mock_empty,
        ):
            # A full interval of non-MPS batches must not advance the
            # cache-drop counter either.
            for _ in range(cs.EMBEDDING_MPS_CACHE_DROP_INTERVAL):
                _sync_after_batch(device)

        mock_sync.assert_not_called()
        mock_empty.assert_not_called()

    def test_accepts_torch_device_objects(self) -> None:
        import torch

        from codebase_rag.embedder import _sync_after_batch

        with (
            patch("codebase_rag.embedder.torch.mps.synchronize") as mock_sync,
            patch("codebase_rag.embedder.torch.mps.empty_cache"),
        ):
            _sync_after_batch(torch.device(cs.EmbeddingDevice.MPS))

        mock_sync.assert_called_once()


class TestEmbedCallsSync:
    def test_embed_code_batch_syncs_once_per_chunk(
        self, mock_unixcoder: MagicMock
    ) -> None:
        import torch

        from codebase_rag.embedder import embed_code_batch

        snippets = [f"def f{i}(): pass" for i in range(5)]
        mock_unixcoder.tokenize.side_effect = lambda batch, **_kw: (
            [[1, 2, 3]] * len(batch)
        )
        mock_unixcoder.side_effect = lambda tensor: (
            torch.zeros(tensor.shape[0], 5, 768),
            torch.zeros(tensor.shape[0], 768),
        )

        with (
            patch("codebase_rag.embedder.get_model", return_value=mock_unixcoder),
            patch("codebase_rag.embedder._sync_after_batch") as mock_sync,
        ):
            embed_code_batch(snippets, batch_size=2)

        assert mock_sync.call_count == 3

    def test_embed_code_syncs_once(self, mock_unixcoder: MagicMock) -> None:
        import torch

        from codebase_rag.embedder import embed_code

        mock_unixcoder.tokenize.return_value = [[1, 2, 3]]
        mock_unixcoder.return_value = (torch.zeros(1, 5, 768), torch.zeros(1, 768))

        with (
            patch("codebase_rag.embedder.get_model", return_value=mock_unixcoder),
            patch("codebase_rag.embedder._sync_after_batch") as mock_sync,
        ):
            embed_code("def hello(): pass")

        mock_sync.assert_called_once()
