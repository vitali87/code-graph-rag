from dataclasses import dataclass, field


@dataclass
class LanguageConfig:
    """Configuration for language-specific Tree-sitter parsing."""

    name: str
    file_extensions: list[str]

    # AST node type mappings to semantic concepts
    function_node_types: list[str]
    class_node_types: list[str]
    module_node_types: list[str]
    call_node_types: list[str] = field(default_factory=list)

    # Field names for extracting names
    name_field: str = "name"
    body_field: str = "body"

    # Package detection patterns
    package_indicators: list[str] = field(default_factory=list)  # e.g., ["__init__.py"] for Python


# Language configurations
LANGUAGE_CONFIGS = {
    "python": LanguageConfig(
        name="python",
        file_extensions=[".py"],
        function_node_types=["function_definition"],
        class_node_types=["class_definition"],
        module_node_types=["module"],
        call_node_types=["call"],
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
        call_node_types=["call_expression"],
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
        call_node_types=["call_expression"],
    ),
    "rust": LanguageConfig(
        name="rust",
        file_extensions=[".rs"],
        function_node_types=["function_item"],
        class_node_types=["struct_item", "enum_item", "impl_item"],
        module_node_types=["source_file"],
        call_node_types=["call_expression"],
    ),
    "go": LanguageConfig(
        name="go",
        file_extensions=[".go"],
        function_node_types=["function_declaration", "method_declaration"],
        class_node_types=["type_declaration"],  # Go structs
        module_node_types=["source_file"],
        call_node_types=["call_expression"],
    ),
    "scala": LanguageConfig(
        name="scala",
        file_extensions=[".scala", ".sc"],
        function_node_types=[
            "function_definition",
            "function_declaration",
        ],
        class_node_types=[
            "class_definition",
            "object_definition",
            "trait_definition",
            "case_class_definition",
        ],
        module_node_types=["compilation_unit"],
        call_node_types=[
            "call_expression",
            "generic_function",
            "field_expression",
            "infix_expression",
        ],
        package_indicators=[],  # Scala uses package declarations
    ),
    "java": LanguageConfig(
        name="java",
        file_extensions=[".java"],
        function_node_types=[
            "method_declaration",
            "constructor_declaration",
        ],
        class_node_types=[
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "annotation_type_declaration",
        ],
        module_node_types=["program"],
        package_indicators=[],  # Java uses package declarations
        call_node_types=["method_invocation"],
    ),
    "cpp": LanguageConfig(
        name="cpp",
        file_extensions=[".cpp", ".h", ".hpp", ".cc"],
        function_node_types=["function_definition"],
        class_node_types=["class_specifier", "struct_specifier"],
        module_node_types=["translation_unit"],
        call_node_types=["call_expression"],
    ),
}


def get_language_config(file_extension: str) -> LanguageConfig | None:
    """Get language configuration based on file extension."""
    for config in LANGUAGE_CONFIGS.values():
        if file_extension in config.file_extensions:
            return config
    return None


def get_language_config_by_name(language_name: str) -> LanguageConfig | None:
    """Get language configuration by language name."""
    return LANGUAGE_CONFIGS.get(language_name.lower())
