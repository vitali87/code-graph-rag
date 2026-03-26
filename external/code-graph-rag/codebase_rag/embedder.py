from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from loguru import logger

from . import constants as cs
from . import exceptions as ex
from . import logs as ls
from .config import settings
from .utils.dependencies import has_torch, has_transformers


class EmbeddingCache:
    __slots__ = ("_cache", "_path")

    def __init__(self, path: Path | None = None) -> None:
        self._cache: dict[str, list[float]] = {}
        self._path = path

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()

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


if has_torch() and has_transformers():
    import numpy as np
    import torch
    from numpy.typing import NDArray

    from .unixcoder import UniXcoder

    @lru_cache(maxsize=1)
    def get_model() -> UniXcoder:
        model = UniXcoder(cs.UNIXCODER_MODEL)
        model.eval()
        if torch.cuda.is_available():
            model = model.cuda()
        return model

    def embed_code(code: str, max_length: int | None = None) -> list[float]:
        cache = get_embedding_cache()
        if (cached := cache.get(code)) is not None:
            return cached

        if max_length is None:
            max_length = settings.EMBEDDING_MAX_LENGTH
        model = get_model()
        device = next(model.parameters()).device
        tokens = model.tokenize([code], max_length=max_length)
        tokens_tensor = torch.tensor(tokens).to(device)
        with torch.no_grad():
            _, sentence_embeddings = model(tokens_tensor)
            embedding: NDArray[np.float32] = sentence_embeddings.cpu().numpy()
        result: list[float] = embedding[0].tolist()

        cache.put(code, result)
        return result

    def embed_code_batch(
        snippets: list[str],
        max_length: int | None = None,
        batch_size: int = cs.EMBEDDING_DEFAULT_BATCH_SIZE,
    ) -> list[list[float]]:
        if not snippets:
            return []

        if max_length is None:
            max_length = settings.EMBEDDING_MAX_LENGTH

        cache = get_embedding_cache()
        cached_results = cache.get_many(snippets)

        if len(cached_results) == len(snippets):
            logger.debug(ls.EMBEDDING_CACHE_HIT, count=len(snippets))
            return [cached_results[i] for i in range(len(snippets))]

        uncached_indices = [i for i in range(len(snippets)) if i not in cached_results]
        uncached_snippets = [snippets[i] for i in uncached_indices]

        model = get_model()
        device = next(model.parameters()).device

        all_new_embeddings: list[list[float]] = []
        for start in range(0, len(uncached_snippets), batch_size):
            batch = uncached_snippets[start : start + batch_size]
            tokens_list = model.tokenize(batch, max_length=max_length, padding=True)
            tokens_tensor = torch.tensor(tokens_list).to(device)
            with torch.no_grad():
                _, sentence_embeddings = model(tokens_tensor)
                batch_np: NDArray[np.float32] = sentence_embeddings.cpu().numpy()
            for row in batch_np:
                all_new_embeddings.append(row.tolist())

        cache.put_many(uncached_snippets, all_new_embeddings)

        results: list[list[float]] = [[] for _ in snippets]
        for i, emb in cached_results.items():
            results[i] = emb
        for idx, orig_i in enumerate(uncached_indices):
            results[orig_i] = all_new_embeddings[idx]

        return results

else:

    def embed_code(code: str, max_length: int | None = None) -> list[float]:
        raise RuntimeError(ex.SEMANTIC_EXTRA)

    def embed_code_batch(
        snippets: list[str],
        max_length: int | None = None,
        batch_size: int = cs.EMBEDDING_DEFAULT_BATCH_SIZE,
    ) -> list[list[float]]:
        raise RuntimeError(ex.SEMANTIC_EXTRA)
