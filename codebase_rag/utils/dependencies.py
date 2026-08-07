from __future__ import annotations

import importlib.util
from collections.abc import Sequence

from codebase_rag.constants import (
    MODULE_AST_GREP,
    MODULE_PYMILVUS,
    MODULE_QDRANT_CLIENT,
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


def has_ast_grep() -> bool:
    return _check_dependency(MODULE_AST_GREP)


def has_vector_store_dependencies() -> bool:
    from codebase_rag.config import settings

    backend = settings.VECTOR_STORE_BACKEND
    if backend == VectorStoreBackend.MILVUS:
        return has_pymilvus()
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


def check_dependencies(required_modules: Sequence[str]) -> bool:
    return all(_check_dependency(module) for module in required_modules)


def get_missing_dependencies(required_modules: Sequence[str]) -> list[str]:
    return [module for module in required_modules if not _check_dependency(module)]
