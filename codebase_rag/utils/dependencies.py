import importlib.util

_dependency_cache: dict[str, bool] = {}


def _check_dependency(module_name: str) -> bool:
    if module_name not in _dependency_cache:
        _dependency_cache[module_name] = (
            importlib.util.find_spec(module_name) is not None
        )
    return _dependency_cache[module_name]


def has_torch() -> bool:
    return _check_dependency("torch")


def has_transformers() -> bool:
    return _check_dependency("transformers")


def has_qdrant_client() -> bool:
    return _check_dependency("qdrant_client")


def has_semantic_dependencies() -> bool:
    return has_qdrant_client() and has_torch() and has_transformers()


def check_dependencies(required_modules: list[str]) -> bool:
    return all(_check_dependency(module) for module in required_modules)


def get_missing_dependencies(required_modules: list[str]) -> list[str]:
    return [module for module in required_modules if not _check_dependency(module)]


SEMANTIC_DEPENDENCIES = ["qdrant_client", "torch", "transformers"]
ML_DEPENDENCIES = ["torch", "transformers"]
