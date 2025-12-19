from typing import TYPE_CHECKING

from ..constants import SEPARATOR_DOT

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
    if module_qn in import_processor.import_mapping:
        import_map = import_processor.import_mapping[module_qn]
        if class_name in import_map:
            return import_map[class_name]

    same_module_qn = f"{module_qn}.{class_name}"
    if same_module_qn in function_registry:
        return same_module_qn

    module_parts = module_qn.split(SEPARATOR_DOT)
    for i in range(len(module_parts) - 1, 0, -1):
        parent_module = SEPARATOR_DOT.join(module_parts[:i])
        potential_qn = f"{parent_module}.{class_name}"
        if potential_qn in function_registry:
            return potential_qn

    matches = function_registry.find_ending_with(class_name)
    for match in matches:
        match_parts = match.split(SEPARATOR_DOT)
        if class_name in match_parts:
            return str(match)

    return None
