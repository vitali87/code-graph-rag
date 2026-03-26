from __future__ import annotations

import importlib.util
from collections.abc import Sequence

from codebase_rag.constants import (
    MODULE_QDRANT_CLIENT,
    MODULE_TORCH,
    MODULE_TRANSFORMERS,
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


def has_semantic_dependencies() -> bool:
    return has_qdrant_client() and has_torch() and has_transformers()


def check_dependencies(required_modules: Sequence[str]) -> bool:
    return all(_check_dependency(module) for module in required_modules)


def get_missing_dependencies(required_modules: Sequence[str]) -> list[str]:
    return [module for module in required_modules if not _check_dependency(module)]
