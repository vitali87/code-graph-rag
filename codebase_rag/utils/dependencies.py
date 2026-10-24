from __future__ import annotations

import importlib.util
from functools import cache

from loguru import logger

from codebase_rag import exceptions as ex
from codebase_rag.constants import (
    MODULE_AST_GREP,
    MODULE_PYMILVUS,
    MODULE_QDRANT_CLIENT,
    MODULE_SQLITE_VEC,
    MODULE_TORCH,
    MODULE_TRANSFORMERS,
    UNIXCODER_MODEL,
    EmbeddingProvider,
    VectorStoreBackend,
)

_dependency_cache: dict[str, bool] = {}


def _check_dependency(module_name: str) -> bool:
    if module_name not in _dependency_cache:
        _dependency_cache[module_name] = (
            importlib.util.find_spec(module_name) is not None
        )
    return _dependency_cache[module_name]


def has_torch() -> bool:
    return _check_dependency(MODULE_TORCH)


def has_transformers() -> bool:
    return _check_dependency(MODULE_TRANSFORMERS)


def has_qdrant_client() -> bool:
    return _check_dependency(MODULE_QDRANT_CLIENT)


def has_pymilvus() -> bool:
    return _check_dependency(MODULE_PYMILVUS)


def has_sqlite_vec() -> bool:
    return _check_dependency(MODULE_SQLITE_VEC)


def sqlite_vec_loadable() -> bool:
    """Whether this Python can load sqlite-vec, which is installed.

    It loads as a SQLite extension, which some Python builds (pyenv's default
    among them) cannot load. The `sqlite` store is then unavailable, as
    without the package: semantic search is off rather than failing in every
    sync and on `--clean`, and the reason is logged once.
    """
    from codebase_rag.sqlite_vectors import extensions_loadable

    if extensions_loadable():
        return True
    _warn_sqlite_extensions_unsupported()
    return False


@cache
def _warn_sqlite_extensions_unsupported() -> None:
    logger.warning(ex.SQLITE_VEC_EXTENSIONS_UNSUPPORTED)


def has_ast_grep() -> bool:
    return _check_dependency(MODULE_AST_GREP)


def has_vector_store_dependencies() -> bool:
    from codebase_rag.config import settings

    backend = settings.VECTOR_STORE_BACKEND
    if backend == VectorStoreBackend.MILVUS:
        return has_pymilvus()
    if backend == VectorStoreBackend.SQLITE:
        return has_sqlite_vec() and sqlite_vec_loadable()
    return has_qdrant_client()


def has_semantic_dependencies() -> bool:
    from codebase_rag.config import settings

    # An OpenAI-compatible endpoint computes embeddings server-side, so
    # only the vector store dependency is needed locally.
    if settings.EMBEDDING_PROVIDER == EmbeddingProvider.OPENAI:
        return has_vector_store_dependencies()
    return has_vector_store_dependencies() and has_torch() and has_transformers()


def has_local_embedding_weights() -> bool:
    """Whether the embedding model is on disk, decided WITHOUT the network.

    Tests that embed for real otherwise download `microsoft/unixcoder-base`
    from HuggingFace while running, so an outage or a 429 fails the unit suite
    for reasons unrelated to the change under test (issue #1092). Answering
    from the local cache alone keeps those tests a hard failure when they DO
    run: a network error must never become a silent pass, because that hides a
    real embedder break.
    """
    if not (has_torch() and has_transformers()):
        return False
    try:
        from transformers import AutoConfig, AutoModel, AutoTokenizer

        # The config alone is not the model: a cache holding only config.json
        # satisfies AutoConfig and then fails in the embedder when the
        # weights turn out to be missing. Resolve every artifact UniXcoder
        # loads, all with local_files_only, so the probe answers the question
        # the tests actually ask.
        AutoConfig.from_pretrained(UNIXCODER_MODEL, local_files_only=True)
        AutoTokenizer.from_pretrained(UNIXCODER_MODEL, local_files_only=True)
        AutoModel.from_pretrained(UNIXCODER_MODEL, local_files_only=True)
    except Exception:
        return False
    return True
