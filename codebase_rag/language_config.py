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
    package_indicators: list[str] = field(
        default_factory=list
    )  # e.g., ["__init__.py"] for Python


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
        file_extensions=[".cpp", ".h", ".hpp", ".cc", ".cxx", ".hxx", ".hh"],
        function_node_types=["function_definition"],
        class_node_types=[
            "class_specifier",
            "struct_specifier",
            "union_specifier",
            "enum_specifier",
        ],
        module_node_types=["translation_unit", "namespace_definition"],
        call_node_types=["call_expression"],
    ),
    "c-sharp": LanguageConfig(
        name="c-sharp",
        file_extensions=[".cs"],
        function_node_types=[
            "destructor_declaration",
            "local_function_statement",
            "function_pointer_type",
            "constructor_declaration",
            "anonymous_method_expression",
            "lambda_expression",
            "method_declaration",
        ],
        class_node_types=[
            "reftype_expression",
            "anonymous_object_creation_expression",
            "type_pattern",
            "predefined_type",
            "class_declaration",
            "typeof_expression",
            "array_type",
            "implicit_type",
            "scoped_type",
            "tuple_type",
            "struct_declaration",
            "pointer_type",
            "implicit_array_creation_expression",
            "enum_declaration",
            "interface_declaration",
            "implicit_object_creation_expression",
            "object_creation_expression",
            "ref_type",
            "implicit_stackalloc_expression",
            "nullable_type",
        ],
        module_node_types=["compilation_unit"],
        call_node_types=["invocation_expression"],
    ),
    "php": LanguageConfig(
        name="php",
        file_extensions=[".php"],
        function_node_types=[
            "function_static_declaration",
            "anonymous_function",
            "function_definition",
            "arrow_function",
        ],
        class_node_types=[
            "named_type",
            "intersection_type",
            "optional_type",
            "trait_declaration",
            "enum_declaration",
            "interface_declaration",
            "object_creation_expression",
            "class_declaration",
            "disjunctive_normal_form_type",
            "primitive_type",
            "union_type",
        ],
        module_node_types=["program"],
        call_node_types=[
            "member_call_expression",
            "scoped_call_expression",
            "function_call_expression",
            "nullsafe_member_call_expression",
        ],
    ),
    "lua": LanguageConfig(
        name="lua",
        file_extensions=[".lua"],
        function_node_types=[
            "function_definition",
            "function_declaration",
        ],
        class_node_types=[],
        module_node_types=["chunk"],
        call_node_types=["function_call"],
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
