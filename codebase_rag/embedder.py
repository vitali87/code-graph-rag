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
from .config import API_KEY_INFO, normalised_credential, settings
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
    return f"{cs.EmbeddingProvider.UNIXCODER}:{settings.EMBEDDING_MODEL}"


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
        from .vector_store import embedding_cache_dir

        cache_path = embedding_cache_dir() / cs.EMBEDDING_CACHE_FILENAME
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
    api_key = normalised_credential(
        settings.OPENAI_EMBEDDING_API_KEY
    ) or normalised_credential(
        os.environ.get(API_KEY_INFO[cs.Provider.OPENAI]["env_var"])
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
    from ._win_runtime import preload_msvc_runtime

    # Library callers reach the embedder without going through the CLI, so the
    # CLI's preload is not guaranteed to have run (see _win_runtime).
    preload_msvc_runtime()

    import torch
    from sentence_transformers import SentenceTransformer

    def _device_available(device: cs.EmbeddingDevice) -> bool:
        match device:
            case cs.EmbeddingDevice.CUDA:
                return torch.cuda.is_available() and torch.cuda.device_count() > 0
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
    def get_model() -> SentenceTransformer:
        device = _select_device()
        model = SentenceTransformer(
            settings.EMBEDDING_MODEL,
            device=device.value,
            model_kwargs={"attn_implementation": "eager"},
        )
        model.eval()
        if device == cs.EmbeddingDevice.CUDA:
            # Ampere and later run fp32 matmuls on tensor cores at TF32
            # precision. For embedding similarity the error is in the fourth
            # decimal, and the throughput roughly doubles.
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.set_float32_matmul_precision("high")
        _warn_if_checkpoint_needs_remote_code(model)
        _warn_if_dimension_mismatch(model)
        # Logged because the alternative -- silently running a 130M-parameter
        # model on the CPU -- looks exactly like "the GPU is not being used"
        # and is otherwise only visible in a profiler.
        logger.info(
            ls.EMBEDDING_DEVICE_SELECTED.format(
                model=settings.EMBEDDING_MODEL, device=model.device
            )
        )
        return model

    def _warn_if_dimension_mismatch(model: SentenceTransformer) -> None:
        """Catch a model/store width mismatch at load, not at insert.

        sqlite-vec only complains when the first vector of the wrong width is
        written, which is minutes into an index run and leaves a half-built
        store behind.
        """
        try:
            # sentence-transformers 6.2 renamed this; older 6.x still has the
            # old spelling, and both are cheaper than reading the model config.
            getter = getattr(
                model, "get_embedding_dimension", None
            ) or getattr(model, "get_sentence_embedding_dimension", None)
            width = getter() if getter is not None else None
        except (AttributeError, NotImplementedError):
            return
        if width is not None and width != settings.SQLITE_VECTOR_DIM:
            logger.error(
                ls.EMBEDDING_DIM_MISMATCH.format(
                    model=settings.EMBEDDING_MODEL,
                    model_dim=width,
                    configured=settings.SQLITE_VECTOR_DIM,
                )
            )

    def _warn_if_checkpoint_needs_remote_code(model: SentenceTransformer) -> None:
        """Shout when a custom-architecture checkpoint loaded as a stock one.

        A repo whose config carries `auto_map` only loads correctly with
        `trust_remote_code=True`. Without it transformers builds the vanilla
        architecture, the state dict fails to match (the load reports MISSING
        and UNEXPECTED keys), and every embedding is noise -- while the run
        itself succeeds. Silent, and only visible as bad search results.
        """
        try:
            config = model[0].auto_model.config
        except (AttributeError, IndexError, TypeError):
            return
        if getattr(config, "auto_map", None):
            logger.error(
                ls.EMBEDDING_REMOTE_CODE_MISMATCH.format(model=settings.EMBEDDING_MODEL)
            )

    def _is_cuda_oom(error: RuntimeError) -> bool:
        return torch.cuda.is_available() and "out of memory" in str(error).lower()

    def _encode(
        snippets: list[str], max_length: int, batch_size: int
    ) -> list[list[float]]:
        """Encode, halving the batch until the card accepts it.

        A batch that fits the model but not the GPU's memory is recoverable:
        retrying smaller costs a pass, while letting the OOM escape aborts the
        whole index and loses the embeddings computed so far.
        """
        model = get_model()
        # sentence-transformers 6.x rejects `max_length` as an encode kwarg for
        # models that do not declare it; the supported knob is the model's own
        # max_seq_length, read at tokenisation time.
        if model.max_seq_length != max_length:
            model.max_seq_length = max_length
        while True:
            try:
                embeddings = model.encode(
                    snippets,
                    batch_size=batch_size,
                    show_progress_bar=False,
                    normalize_embeddings=True,
                )
                break
            except RuntimeError as e:
                next_size = max(1, batch_size // 2)
                if not _is_cuda_oom(e) or next_size == batch_size:
                    raise
                logger.warning(
                    ls.EMBEDDING_CUDA_OOM_RETRY.format(
                        batch_size=batch_size, next_size=next_size
                    )
                )
                torch.cuda.empty_cache()
                batch_size = next_size
        _sync_after_batch(model.device)
        return [emb.tolist() for emb in embeddings]

    def _unixcoder_embed_code(code: str, max_length: int | None) -> list[float]:
        if max_length is None:
            max_length = settings.EMBEDDING_MAX_LENGTH
        return _encode([code], max_length, 1)[0]

    def _unixcoder_embed_batch(
        snippets: list[str], max_length: int | None, batch_size: int
    ) -> list[list[float]]:
        if max_length is None:
            max_length = settings.EMBEDDING_MAX_LENGTH
        return _encode(snippets, max_length, batch_size)

else:
    def _unixcoder_embed_code(code: str, max_length: int | None) -> list[float]:
        raise RuntimeError(ex.SEMANTIC_EXTRA)

    def _unixcoder_embed_batch(
        snippets: list[str], max_length: int | None, batch_size: int
    ) -> list[list[float]]:
        raise RuntimeError(ex.SEMANTIC_EXTRA)


# ========== 【重点：补上缺失的顶层 embed_code，外部模块会 import 这个函数！】 ==========
def embed_code(code: str, max_length: int | None = None) -> list[float]:
    return _unixcoder_embed_code(code, max_length)


def embed_code_batch(
    snippets: list[str],
    max_length: int | None = None,
    batch_size: int | None = None,
) -> list[list[float]]:
    if not snippets:
        return []

    if batch_size is None:
        batch_size = settings.EMBEDDING_BATCH_SIZE

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
