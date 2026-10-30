"""The local UniXcoder embedding backend.

Its own module because torch and transformers take seconds to import: the
embedder imports it on the first local embedding, so `import cgr`, the
OpenAI provider and every command that embeds nothing never pay for them.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import torch
from loguru import logger
from numpy.typing import NDArray

from .. import constants as cs
from .. import logs as ls
from ..config import settings
from .unixcoder import UniXcoder


def _device_available(device: cs.EmbeddingDevice) -> bool:
    match device:
        case cs.EmbeddingDevice.CUDA:
            return torch.cuda.is_available()
        case cs.EmbeddingDevice.MPS:
            return torch.backends.mps.is_available()
        case _:
            return True


def _select_device() -> cs.EmbeddingDevice:
    if (override := settings.EMBEDDING_DEVICE) is not None:
        if _device_available(override):
            return override
        logger.warning(ls.EMBEDDING_DEVICE_UNAVAILABLE.format(device=override))
    if torch.cuda.is_available():
        return cs.EmbeddingDevice.CUDA
    if torch.backends.mps.is_available():
        return cs.EmbeddingDevice.MPS
    return cs.EmbeddingDevice.CPU


_batches_since_cache_drop = 0


def _sync_after_batch(device: torch.device | str) -> None:
    # MPS wedges inside Metal's waitUntilCompleted when command buffers
    # accumulate across thousands of batches (issue #689); draining the
    # stream after every batch keeps each command buffer short-lived,
    # and a periodic (not per-batch: ~21% throughput cost) cache drop
    # bounds Metal allocator growth over monorepo-scale runs.
    if torch.device(device).type != cs.EmbeddingDevice.MPS:
        return
    torch.mps.synchronize()
    global _batches_since_cache_drop
    _batches_since_cache_drop += 1
    if _batches_since_cache_drop >= cs.EMBEDDING_MPS_CACHE_DROP_INTERVAL:
        torch.mps.empty_cache()
        _batches_since_cache_drop = 0


@lru_cache(maxsize=1)
def get_model() -> UniXcoder:
    model = UniXcoder(cs.UNIXCODER_MODEL, cs.UNIXCODER_REVISION)
    model.eval()
    device = _select_device()
    if device != cs.EmbeddingDevice.CPU:
        model = model.to(device)
    if device == cs.EmbeddingDevice.MPS:
        # The weight upload is the first copy on the MPS stream, before
        # any batch and so before `_sync_after_batch` ever runs. Draining
        # it here keeps a copy that parks on the stream from surfacing
        # later inside the first batch's transfers, and pins a wedge at
        # load to this one point ahead of any store write (issue #2218).
        torch.mps.synchronize()
    return model


def embed_one(code: str, max_length: int | None) -> list[float]:
    if max_length is None:
        max_length = settings.EMBEDDING_MAX_LENGTH
    model = get_model()
    device = next(model.parameters()).device
    tokens = model.tokenize([code], max_length=max_length)
    tokens_tensor = torch.tensor(tokens).to(device)
    with torch.no_grad():
        _, sentence_embeddings = model(tokens_tensor)
        embedding: NDArray[np.float32] = sentence_embeddings.cpu().numpy()
    _sync_after_batch(device)
    result: list[float] = embedding[0].tolist()
    return result


def embed_many(
    snippets: list[str], max_length: int | None, batch_size: int
) -> list[list[float]]:
    if max_length is None:
        max_length = settings.EMBEDDING_MAX_LENGTH
    model = get_model()
    device = next(model.parameters()).device

    all_new_embeddings: list[list[float]] = []
    for start in range(0, len(snippets), batch_size):
        batch = snippets[start : start + batch_size]
        tokens_list = model.tokenize(batch, max_length=max_length, padding=True)
        tokens_tensor = torch.tensor(tokens_list).to(device)
        with torch.no_grad():
            _, sentence_embeddings = model(tokens_tensor)
            batch_np: NDArray[np.float32] = sentence_embeddings.cpu().numpy()
        _sync_after_batch(device)
        for row in batch_np:
            all_new_embeddings.append(row.tolist())
    return all_new_embeddings
