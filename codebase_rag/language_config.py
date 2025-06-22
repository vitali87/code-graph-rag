from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class LanguageConfig:
    """Configuration for language-specific Tree-sitter parsing."""

    name: str
    file_extensions: List[str]

    # AST node type mappings to semantic concepts
    function_node_types: List[str]
    class_node_types: List[str]
    module_node_types: List[str]

    # Field names for extracting names
    name_field: str = "name"
    body_field: str = "body"

    # Package detection patterns
    package_indicators: List[str] = None  # e.g., ["__init__.py"] for Python

    def __post_init__(self):
        if self.package_indicators is None:
            self.package_indicators = []


# Language configurations
LANGUAGE_CONFIGS = {
    "python": LanguageConfig(
        name="python",
        file_extensions=[".py"],
        function_node_types=["function_definition"],
        class_node_types=["class_definition"],
        module_node_types=["module"],
        package_indicators=["__init__.py"],
    ),
    "javascript": LanguageConfig(
        name="javascript",
        file_extensions=[".js", ".jsx"],
        function_node_types=[
            "function_declaration",
            "arrow_function",
            "method_definition",
        ],
        class_node_types=["class_declaration"],
        module_node_types=["program"],
    ),
    "typescript": LanguageConfig(
        name="typescript",
        file_extensions=[".ts", ".tsx"],
        function_node_types=[
            "function_declaration",
            "arrow_function",
            "method_definition",
        ],
        class_node_types=["class_declaration"],
        module_node_types=["program"],
    ),
    "rust": LanguageConfig(
        name="rust",
        file_extensions=[".rs"],
        function_node_types=["function_item"],
        class_node_types=["struct_item", "enum_item", "impl_item"],
        module_node_types=["source_file"],
    ),
    "go": LanguageConfig(
        name="go",
        file_extensions=[".go"],
        function_node_types=["function_declaration", "method_declaration"],
        class_node_types=["type_declaration"],  # Go structs
        module_node_types=["source_file"],
    ),
}


def get_language_config(file_extension: str) -> Optional[LanguageConfig]:
    """Get language configuration based on file extension."""
    for config in LANGUAGE_CONFIGS.values():
        if file_extension in config.file_extensions:
            return config
    return None


def get_language_config_by_name(language_name: str) -> Optional[LanguageConfig]:
    """Get language configuration by language name."""
    return LANGUAGE_CONFIGS.get(language_name.lower())
