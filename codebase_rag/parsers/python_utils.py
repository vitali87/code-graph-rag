"""Python-specific parsing utilities."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..graph_updater import FunctionRegistryTrie
    from .import_processor import ImportProcessor


def resolve_class_name(
    class_name: str,
    module_qn: str,
    import_processor: "ImportProcessor",
    function_registry: "FunctionRegistryTrie",
) -> str | None:
    """
    Convert a simple class name to its fully qualified name.

    Args:
        class_name: The simple class name to resolve
        module_qn: The qualified name of the current module
        import_processor: ImportProcessor instance with import mappings
        function_registry: FunctionRegistry instance for lookups

    Returns:
        The fully qualified class name if found, None otherwise
    """
    # First check current module's import mappings
    if module_qn in import_processor.import_mapping:
        import_map = import_processor.import_mapping[module_qn]
        if class_name in import_map:
            return import_map[class_name]

    # Then check if class exists in same module
    same_module_qn = f"{module_qn}.{class_name}"
    if same_module_qn in function_registry:
        return same_module_qn

    # Check parent modules using the trie structure
    module_parts = module_qn.split(".")
    for i in range(len(module_parts) - 1, 0, -1):
        parent_module = ".".join(module_parts[:i])
        potential_qn = f"{parent_module}.{class_name}"
        if potential_qn in function_registry:
            return potential_qn

    # Use trie to find classes with the given name
    matches = function_registry.find_ending_with(class_name)
    for match in matches:
        # Return the first match that looks like a class
        match_parts = match.split(".")
        if class_name in match_parts:
            return str(match)

    return None
