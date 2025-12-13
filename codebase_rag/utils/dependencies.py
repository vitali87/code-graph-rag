import importlib.util

_dependency_cache: dict[str, bool] = {}


def _check_dependency(module_name: str) -> bool:
    """Check if a module is available, with caching."""
    if module_name not in _dependency_cache:
        _dependency_cache[module_name] = (
            importlib.util.find_spec(module_name) is not None
        )
    return _dependency_cache[module_name]


def has_torch() -> bool:
    """Check if PyTorch is available."""
    return _check_dependency("torch")


def has_transformers() -> bool:
    """Check if Transformers is available."""
    return _check_dependency("transformers")


def has_qdrant_client() -> bool:
    """Check if Qdrant client is available."""
    return _check_dependency("qdrant_client")


def has_semantic_dependencies() -> bool:
    """Check if all semantic search dependencies are available.

    Returns:
        True if qdrant_client, torch, and transformers are all available.
    """
    return has_qdrant_client() and has_torch() and has_transformers()


def check_dependencies(required_modules: list[str]) -> bool:
    """Check if all required modules are available.

    Args:
        required_modules: List of module names to check

    Returns:
        True if all modules are available, False otherwise
    """
    return all(_check_dependency(module) for module in required_modules)


def get_missing_dependencies(required_modules: list[str]) -> list[str]:
    """Get list of missing dependencies.

    Args:
        required_modules: List of module names to check

    Returns:
        List of missing module names
    """
    return [module for module in required_modules if not _check_dependency(module)]


SEMANTIC_DEPENDENCIES = ["qdrant_client", "torch", "transformers"]
ML_DEPENDENCIES = ["torch", "transformers"]
