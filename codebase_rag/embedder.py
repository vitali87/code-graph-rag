from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

import httpx
from loguru import logger

from . import constants as cs
from . import exceptions as ex
from . import logs as ls
from .config import API_KEY_INFO, settings
from .utils.dependencies import has_torch, has_transformers


def _cache_namespace() -> str:
    # Embeddings from different providers/models live in different vector
    # spaces; namespacing cache keys prevents a provider or model switch
    # from replaying stale vectors of the wrong space.
    if settings.EMBEDDING_PROVIDER == cs.EmbeddingProvider.OPENAI:
        namespace = f"{cs.EmbeddingProvider.OPENAI}:{settings.OPENAI_EMBEDDING_MODEL}"
        if settings.OPENAI_EMBEDDING_DIMENSIONS is not None:
            namespace = f"{namespace}:{settings.OPENAI_EMBEDDING_DIMENSIONS}"
        return namespace
    return f"{cs.EmbeddingProvider.UNIXCODER}:{cs.UNIXCODER_MODEL}"


class EmbeddingCache:
    __slots__ = ("_cache", "_path")

    def __init__(self, path: Path | None = None) -> None:
        self._cache: dict[str, list[float]] = {}
        self._path = path

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(f"{_cache_namespace()}\x00{content}".encode()).hexdigest()

    def get(self, content: str) -> list[float] | None:
        return self._cache.get(self._content_hash(content))

    def put(self, content: str, embedding: list[float]) -> None:
        self._cache[self._content_hash(content)] = embedding

    def get_many(self, snippets: list[str]) -> dict[int, list[float]]:
        results: dict[int, list[float]] = {}
        for i, snippet in enumerate(snippets):
            if (cached := self.get(snippet)) is not None:
                results[i] = cached
        return results

    def put_many(self, snippets: list[str], embeddings: list[list[float]]) -> None:
        for snippet, embedding in zip(snippets, embeddings):
            self.put(snippet, embedding)

    def save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as f:
                json.dump(self._cache, f)
        except Exception as e:
            logger.warning(ls.EMBEDDING_CACHE_SAVE_FAILED, path=self._path, error=e)

    def load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                self._cache = json.load(f)
            logger.debug(
                ls.EMBEDDING_CACHE_LOADED, count=len(self._cache), path=self._path
            )
        except Exception as e:
            logger.warning(ls.EMBEDDING_CACHE_LOAD_FAILED, path=self._path, error=e)
            self._cache = {}

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


_embedding_cache: EmbeddingCache | None = None


def get_embedding_cache() -> EmbeddingCache:
    global _embedding_cache
    if _embedding_cache is None:
        cache_path = Path(settings.QDRANT_DB_PATH) / cs.EMBEDDING_CACHE_FILENAME
        _embedding_cache = EmbeddingCache(path=cache_path)
        _embedding_cache.load()
    return _embedding_cache


def clear_embedding_cache() -> None:
    global _embedding_cache
    if _embedding_cache is not None:
        _embedding_cache.clear()
        _embedding_cache = None


def _openai_client() -> httpx.Client:
    headers: dict[str, str] = {}
    api_key = settings.OPENAI_EMBEDDING_API_KEY or os.environ.get(
        API_KEY_INFO[cs.Provider.OPENAI]["env_var"]
    )
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return httpx.Client(
        base_url=settings.OPENAI_EMBEDDING_BASE_URL,
        headers=headers,
        timeout=settings.OPENAI_EMBEDDING_TIMEOUT,
    )


def _openai_embed_batch(snippets: list[str]) -> list[list[float]]:
    embeddings: list[list[float]] = []
    batch_size = settings.OPENAI_EMBEDDING_BATCH_SIZE
    with _openai_client() as client:
        for start in range(0, len(snippets), batch_size):
            batch = snippets[start : start + batch_size]
            payload: dict[str, object] = {
                "model": settings.OPENAI_EMBEDDING_MODEL,
                "input": batch,
            }
            if settings.OPENAI_EMBEDDING_DIMENSIONS is not None:
                payload["dimensions"] = settings.OPENAI_EMBEDDING_DIMENSIONS
            response = client.post(cs.OPENAI_EMBEDDINGS_PATH, json=payload)
            if response.status_code != cs.HTTP_OK:
                raise RuntimeError(
                    ex.OPENAI_EMBEDDING_HTTP_ERROR.format(
                        status=response.status_code, body=response.text[:500]
                    )
                )
            embeddings.extend(_parse_embedding_rows(response, len(batch)))
    return embeddings


def _parse_embedding_rows(response: httpx.Response, expected: int) -> list[list[float]]:
    try:
        rows = response.json()["data"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise RuntimeError(
            ex.OPENAI_EMBEDDING_MALFORMED_RESPONSE.format(error=e)
        ) from e
    if not isinstance(rows, list):
        raise RuntimeError(
            ex.OPENAI_EMBEDDING_MALFORMED_RESPONSE.format(error="'data' is not a list")
        )
    if len(rows) != expected:
        raise RuntimeError(
            ex.OPENAI_EMBEDDING_COUNT_MISMATCH.format(got=len(rows), expected=expected)
        )
    # Place each row at its declared index instead of sorting: a buggy
    # server emitting duplicate or out-of-range indices must fail loudly
    # rather than pair snippets with the wrong vectors.
    placed: list[list[float] | None] = [None] * expected
    for row in rows:
        try:
            index, embedding = row["index"], row["embedding"]
        except (KeyError, TypeError) as e:
            raise RuntimeError(
                ex.OPENAI_EMBEDDING_MALFORMED_RESPONSE.format(error=e)
            ) from e
        if (
            not isinstance(index, int)
            or not 0 <= index < expected
            or placed[index] is not None
        ):
            raise RuntimeError(ex.OPENAI_EMBEDDING_BAD_INDEX.format(index=index))
        placed[index] = embedding
    return [row for row in placed if row is not None]


if has_torch() and has_transformers():
    import numpy as np
    import torch
    from numpy.typing import NDArray

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
        model = UniXcoder(cs.UNIXCODER_MODEL)
        model.eval()
        device = _select_device()
        if device != cs.EmbeddingDevice.CPU:
            model = model.to(device)
        return model

    def _unixcoder_embed_code(code: str, max_length: int | None) -> list[float]:
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

    def _unixcoder_embed_batch(
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

else:

    def _unixcoder_embed_code(code: str, max_length: int | None) -> list[float]:
        raise RuntimeError(ex.SEMANTIC_EXTRA)

    def _unixcoder_embed_batch(
        snippets: list[str], max_length: int | None, batch_size: int
    ) -> list[list[float]]:
        raise RuntimeError(ex.SEMANTIC_EXTRA)


def embed_code(code: str, max_length: int | None = None) -> list[float]:
    cache = get_embedding_cache()
    if (cached := cache.get(code)) is not None:
        return cached
    try:
        if settings.EMBEDDING_PROVIDER == cs.EmbeddingProvider.OPENAI:
            result = _openai_embed_batch([code])[0]
        else:
            result = _unixcoder_embed_code(code, max_length)
    except Exception:
        logger.exception(ls.EMBEDDING_SNIPPET_FAILED, length=len(code))
        raise
    cache.put(code, result)
    return result


def embed_code_batch(
    snippets: list[str],
    max_length: int | None = None,
    batch_size: int = cs.EMBEDDING_DEFAULT_BATCH_SIZE,
) -> list[list[float]]:
    if not snippets:
        return []

    cache = get_embedding_cache()
    cached_results = cache.get_many(snippets)

    if len(cached_results) == len(snippets):
        logger.debug(ls.EMBEDDING_CACHE_HIT, count=len(snippets))
        return [cached_results[i] for i in range(len(snippets))]

    uncached_indices = [i for i in range(len(snippets)) if i not in cached_results]
    uncached_snippets = [snippets[i] for i in uncached_indices]

    if settings.EMBEDDING_PROVIDER == cs.EmbeddingProvider.OPENAI:
        all_new_embeddings = _openai_embed_batch(uncached_snippets)
    else:
        all_new_embeddings = _unixcoder_embed_batch(
            uncached_snippets, max_length, batch_size
        )

    cache.put_many(uncached_snippets, all_new_embeddings)

    results: list[list[float]] = [[] for _ in snippets]
    for i, emb in cached_results.items():
        results[i] = emb
    for idx, orig_i in enumerate(uncached_indices):
        results[orig_i] = all_new_embeddings[idx]

    return results
