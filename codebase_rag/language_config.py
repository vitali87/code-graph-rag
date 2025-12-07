from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tree_sitter import Node


# =============================================================================
# ATOMIC NODE TYPE CONSTANTS (Building blocks for maximum reuse)
# =============================================================================

# Core function types
BASIC_FUNCTIONS = ["function_declaration", "function_definition"]
LAMBDA_FUNCTIONS = [
    "lambda_expression",
    "arrow_function",
    "anonymous_function",
    "closure_expression",
]
METHOD_FUNCTIONS = [
    "method_declaration",
    "constructor_declaration",
    "destructor_declaration",
]
TEMPLATE_FUNCTIONS = [
    "template_declaration",
    "function_signature_item",
    "function_signature",
]
GENERATOR_FUNCTIONS = ["generator_function_declaration", "function_expression"]

# Core class types
BASIC_CLASSES = ["class_declaration", "class_definition"]
STRUCT_TYPES = ["struct_declaration", "struct_specifier", "struct_item"]
INTERFACE_TYPES = ["interface_declaration", "trait_declaration", "trait_item"]
ENUM_TYPES = ["enum_declaration", "enum_item", "enum_specifier"]
TYPE_ALIASES = ["type_alias_declaration", "type_item"]
UNION_TYPES = ["union_specifier", "union_item"]

# Core call types
BASIC_CALLS = ["call_expression", "function_call"]
METHOD_CALLS = ["method_invocation", "member_call_expression", "field_expression"]
OPERATOR_CALLS = ["binary_expression", "unary_expression", "update_expression"]
SPECIAL_CALLS = ["new_expression", "delete_expression", "macro_invocation"]

# Core import types
STANDARD_IMPORTS = ["import_declaration", "import_statement"]
FROM_IMPORTS = ["import_from_statement"]
MODULE_IMPORTS = ["lexical_declaration", "export_statement"]
INCLUDE_IMPORTS = ["preproc_include"]

# =============================================================================
# LANGUAGE FAMILY CONSTANTS (Compositions for supported languages)
# =============================================================================

# JavaScript/TypeScript family - ES6 modules, CommonJS, modern features
JS_FAMILY_FUNCTIONS = (
    BASIC_FUNCTIONS + LAMBDA_FUNCTIONS + METHOD_FUNCTIONS + GENERATOR_FUNCTIONS
)
JS_FAMILY_CLASSES = BASIC_CLASSES + INTERFACE_TYPES + ENUM_TYPES + TYPE_ALIASES
JS_FAMILY_IMPORTS = STANDARD_IMPORTS + MODULE_IMPORTS
JS_FAMILY_CALLS = BASIC_CALLS

# Systems languages (C++, Rust) - Templates, operator overloading, advanced features
SYSTEMS_FUNCTIONS = (
    BASIC_FUNCTIONS + METHOD_FUNCTIONS + TEMPLATE_FUNCTIONS + LAMBDA_FUNCTIONS
)
SYSTEMS_CLASSES = (
    BASIC_CLASSES
    + STRUCT_TYPES
    + INTERFACE_TYPES
    + ENUM_TYPES
    + TYPE_ALIASES
    + UNION_TYPES
)
SYSTEMS_CALLS = BASIC_CALLS + METHOD_CALLS + OPERATOR_CALLS + SPECIAL_CALLS

# JVM languages (Java) - OOP with interfaces, generics, modern features
JVM_FUNCTIONS = BASIC_FUNCTIONS + METHOD_FUNCTIONS
JVM_CLASSES = BASIC_CLASSES + INTERFACE_TYPES + ENUM_TYPES
JVM_IMPORTS = STANDARD_IMPORTS
JVM_CALLS = BASIC_CALLS + METHOD_CALLS

# Scripting languages (Python, Lua) - Dynamic typing, closures, modules
SCRIPTING_FUNCTIONS = BASIC_FUNCTIONS + LAMBDA_FUNCTIONS
SCRIPTING_CLASSES = BASIC_CLASSES
SCRIPTING_CALLS = BASIC_CALLS + METHOD_CALLS

# =============================================================================
# LEGACY COMPATIBILITY (Keep existing constants for backward compatibility)
# =============================================================================
COMMON_JS_TS_FUNCTIONS = [
    "function_declaration",
    "generator_function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
]

COMMON_JS_TS_CLASSES = ["class_declaration", "class"]

COMMON_JS_TS_IMPORTS = [
    "import_statement",
    "lexical_declaration",
    "export_statement",
]

COMMON_DECLARATION_IMPORT = ["import_declaration"]

COMMON_USING_DIRECTIVE = ["using_directive"]

CPP_IMPORTS = [
    "preproc_include",
    "template_function",
    "declaration",
]  # #include, import <>, module declarations


@dataclass
class FQNConfig:
    """Configuration for language-specific FQN resolution."""

    # Node types that create a new scope (e.g., class, namespace)
    scope_node_types: set[str]
    # Node types that represent a function/method
    function_node_types: set[str]
    # Function to extract name from a node (e.g., child_by_field_name("name"))
    get_name: Callable[["Node"], str | None]
    # Function to convert file path → module parts (e.g., strip .py, handle __init__)
    file_to_module_parts: Callable[[Path, Path], list[str]]  # (file_path, repo_root)


def _python_get_name(node: "Node") -> str | None:
    """Extract name from Python AST node."""
    name_node = node.child_by_field_name("name")
    return name_node.text.decode("utf-8") if name_node and name_node.text else None


def _python_file_to_module(file_path: Path, repo_root: Path) -> list[str]:
    """Convert Python file path to module parts."""
    try:
        rel = file_path.relative_to(repo_root)
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return parts
    except Exception:
        return []


def _js_get_name(node: "Node") -> str | None:
    """Extract name from JavaScript/TypeScript AST node."""
    if node.type in ("function_declaration", "class_declaration", "method_definition"):
        name_node = node.child_by_field_name("name")
        return name_node.text.decode("utf-8") if name_node and name_node.text else None
    return None


def _js_file_to_module(file_path: Path, repo_root: Path) -> list[str]:
    """Convert JavaScript/TypeScript file path to module parts."""
    try:
        rel = file_path.relative_to(repo_root)
        parts = list(rel.with_suffix("").parts)
        # Handle index files
        if parts and parts[-1] == "index":
            parts = parts[:-1]
        return parts
    except Exception:
        return []


PYTHON_FQN_CONFIG = FQNConfig(
    scope_node_types={"class_definition", "module"},
    function_node_types={"function_definition"},
    get_name=_python_get_name,
    file_to_module_parts=_python_file_to_module,
)

JAVASCRIPT_FQN_CONFIG = FQNConfig(
    scope_node_types={"class_declaration", "program"},
    function_node_types={
        "function_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
    },
    get_name=_js_get_name,
    file_to_module_parts=_js_file_to_module,
)

TYPESCRIPT_FQN_CONFIG = FQNConfig(
    scope_node_types={
        "class_declaration",
        "interface_declaration",
        "namespace_definition",
        "program",
    },
    function_node_types={
        "function_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
        "function_signature",
    },
    get_name=_js_get_name,
    file_to_module_parts=_js_file_to_module,
)


def _generic_get_name(node: "Node") -> str | None:
    """Generic name extraction for most languages."""
    name_node = node.child_by_field_name("name")
    if name_node and name_node.text:
        return name_node.text.decode("utf-8")

    # Fallback for languages that use different field names
    for field_name in ["identifier", "name", "id"]:
        name_node = node.child_by_field_name(field_name)
        if name_node and name_node.text:
            return name_node.text.decode("utf-8")

    return None


def _generic_file_to_module(file_path: Path, repo_root: Path) -> list[str]:
    """Generic file path to module conversion."""
    try:
        rel = file_path.relative_to(repo_root)
        parts = list(rel.with_suffix("").parts)
        return parts
    except Exception:
        return []


def _rust_get_name(node: "Node") -> str | None:
    """Extract name from Rust AST node."""
    # Rust uses different field names for different node types
    if node.type in ("struct_item", "enum_item", "trait_item", "type_item"):
        name_node = node.child_by_field_name("name")
        if name_node and name_node.type == "type_identifier" and name_node.text:
            return name_node.text.decode("utf-8")
    elif node.type in ("function_item", "mod_item"):
        name_node = node.child_by_field_name("name")
        if name_node and name_node.type == "identifier" and name_node.text:
            return name_node.text.decode("utf-8")

    return _generic_get_name(node)


def _java_file_to_module(file_path: Path, repo_root: Path) -> list[str]:
    """Convert Java file path to package parts."""
    try:
        rel = file_path.relative_to(repo_root)
        # Java typically uses package structure that mirrors directory structure
        parts = list(rel.with_suffix("").parts)
        return parts
    except Exception:
        return []


def _cpp_get_name(node: "Node") -> str | None:
    """Extract name from C++ AST node."""
    # C++ has complex naming with templates, operators, etc.
    if node.type in ("class_specifier", "struct_specifier", "enum_specifier"):
        name_node = node.child_by_field_name("name")
        if name_node and name_node.text:
            return name_node.text.decode("utf-8")
    elif node.type == "function_definition":
        declarator = node.child_by_field_name("declarator")
        if declarator:
            # Handle function declarators
            if declarator.type == "function_declarator":
                name_node = declarator.child_by_field_name("declarator")
                if name_node and name_node.type == "identifier" and name_node.text:
                    return name_node.text.decode("utf-8")

    return _generic_get_name(node)


RUST_FQN_CONFIG = FQNConfig(
    scope_node_types={
        "struct_item",
        "enum_item",
        "trait_item",
        "impl_item",
        "mod_item",
        "source_file",
    },
    function_node_types={
        "function_item",
        "function_signature_item",
        "closure_expression",
    },
    get_name=_rust_get_name,
    file_to_module_parts=_generic_file_to_module,
)

JAVA_FQN_CONFIG = FQNConfig(
    scope_node_types={
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "program",
    },
    function_node_types={"method_declaration", "constructor_declaration"},
    get_name=_generic_get_name,
    file_to_module_parts=_java_file_to_module,
)

CPP_FQN_CONFIG = FQNConfig(
    scope_node_types={
        "class_specifier",
        "struct_specifier",
        "namespace_definition",
        "translation_unit",
    },
    function_node_types={
        "function_definition",
        "declaration",
        "field_declaration",
        "template_declaration",
        "lambda_expression",
    },
    get_name=_cpp_get_name,
    file_to_module_parts=_generic_file_to_module,
)

LUA_FQN_CONFIG = FQNConfig(
    scope_node_types={"chunk"},  # Lua uses tables/modules but chunk is the main scope
    function_node_types={"function_declaration", "function_definition"},
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

GO_FQN_CONFIG = FQNConfig(
    scope_node_types={
        "type_declaration",
        "source_file",
    },  # Go structs and file-level scope
    function_node_types={"function_declaration", "method_declaration"},
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

SCALA_FQN_CONFIG = FQNConfig(
    scope_node_types={
        "class_definition",
        "object_definition",
        "trait_definition",
        "compilation_unit",
    },
    function_node_types={"function_definition", "function_declaration"},
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

CSHARP_FQN_CONFIG = FQNConfig(
    scope_node_types={
        "class_declaration",
        "struct_declaration",
        "interface_declaration",
        "compilation_unit",
    },
    function_node_types={
        "destructor_declaration",
        "local_function_statement",
        "function_pointer_type",
        "constructor_declaration",
        "anonymous_method_expression",
        "lambda_expression",
        "method_declaration",
    },
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

PHP_FQN_CONFIG = FQNConfig(
    scope_node_types={
        "class_declaration",
        "interface_declaration",
        "trait_declaration",
        "program",
    },
    function_node_types={
        "function_definition",
        "anonymous_function",
        "arrow_function",
        "function_static_declaration",
    },
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

LANGUAGE_FQN_CONFIGS = {
    "python": PYTHON_FQN_CONFIG,
    "javascript": JAVASCRIPT_FQN_CONFIG,
    "typescript": TYPESCRIPT_FQN_CONFIG,
    "rust": RUST_FQN_CONFIG,
    "java": JAVA_FQN_CONFIG,
    "cpp": CPP_FQN_CONFIG,
    "lua": LUA_FQN_CONFIG,
    "go": GO_FQN_CONFIG,
    "scala": SCALA_FQN_CONFIG,
    "c-sharp": CSHARP_FQN_CONFIG,
    "php": PHP_FQN_CONFIG,
}


def create_lang_config(**kwargs: Any) -> "LanguageConfig":
    """Helper to create LanguageConfig without redundant name assignment."""
    # Name will be set automatically when configs are processed
    return LanguageConfig(name="", **kwargs)


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

    # Import statement node types for precise import resolution
    import_node_types: list[str] = field(default_factory=list)
    import_from_node_types: list[str] = field(default_factory=list)

    # Field names for extracting names
    name_field: str = "name"
    body_field: str = "body"

    # Package detection patterns
    package_indicators: list[str] = field(
        default_factory=list
    )  # e.g., ["__init__.py"] for Python

    # Optional pre-formatted Tree-sitter query strings or query generators
    # These override the default node_types-based query generation
    function_query: str | None = None
    class_query: str | None = None
    call_query: str | None = None


######################## Language configurations ###############################
# Automatic generation might add types that are too broad or inaccurate.
# You have to manually check and adjust the configurations after running the
# automatic generation.
################################################################################

LANGUAGE_CONFIGS = {
    "python": create_lang_config(
        file_extensions=[".py"],
        function_node_types=["function_definition"],
        class_node_types=["class_definition"],
        module_node_types=["module"],
        call_node_types=["call", "with_statement"],
        import_node_types=["import_statement"],
        import_from_node_types=["import_from_statement"],
        package_indicators=["__init__.py"],
    ),
    "javascript": create_lang_config(
        file_extensions=[".js", ".jsx"],
        function_node_types=COMMON_JS_TS_FUNCTIONS,
        class_node_types=COMMON_JS_TS_CLASSES,
        module_node_types=["program"],
        call_node_types=["call_expression"],
        import_node_types=COMMON_JS_TS_IMPORTS,
        import_from_node_types=COMMON_JS_TS_IMPORTS,  # Include CommonJS require and re-exports
    ),
    "typescript": create_lang_config(
        file_extensions=[".ts", ".tsx"],
        function_node_types=COMMON_JS_TS_FUNCTIONS
        + ["function_signature"],  # For ambient declarations: declare function
        class_node_types=COMMON_JS_TS_CLASSES
        + [
            "abstract_class_declaration",
            "enum_declaration",
            "interface_declaration",
            "type_alias_declaration",
            "internal_module",
        ],
        module_node_types=["program"],
        call_node_types=["call_expression"],
        import_node_types=COMMON_JS_TS_IMPORTS,
        import_from_node_types=COMMON_JS_TS_IMPORTS,  # Include CommonJS require and re-exports
    ),
    "rust": create_lang_config(
        file_extensions=[".rs"],
        function_node_types=[
            "function_item",  # Regular functions: fn name() {}
            "function_signature_item",  # Function signatures in traits
            "closure_expression",  # Closures/lambdas: |x| x + 1
        ],
        class_node_types=[
            "struct_item",  # Struct definitions
            "enum_item",  # Enum definitions
            "union_item",  # Union definitions
            "trait_item",  # Trait definitions
            "impl_item",  # Implementation blocks
            "type_item",  # Type aliases: type Name = Type;
        ],
        module_node_types=[
            "source_file",  # Root module file
            "mod_item",  # Module declarations: mod name {}
        ],
        call_node_types=[
            "call_expression",  # Function and method calls
            "macro_invocation",  # Macro calls: println!()
        ],
        import_node_types=["use_declaration", "extern_crate_declaration"],
        import_from_node_types=["use_declaration"],  # Rust uses 'use' for all imports
        package_indicators=["Cargo.toml"],  # Rust's package manifest
        # Pre-formatted Tree-sitter queries based on official tree-sitter-rust grammar
        function_query="""
        (function_item
            name: (identifier) @name) @function
        (function_signature_item
            name: (identifier) @name) @function
        (closure_expression) @function
        """,
        class_query="""
        (struct_item
            name: (type_identifier) @name) @class
        (enum_item
            name: (type_identifier) @name) @class
        (union_item
            name: (type_identifier) @name) @class
        (trait_item
            name: (type_identifier) @name) @class
        (type_item
            name: (type_identifier) @name) @class
        (impl_item) @class
        (mod_item
            name: (identifier) @name) @module
        """,
        call_query="""
        (call_expression
            function: (identifier) @name) @call
        (call_expression
            function: (field_expression
                field: (field_identifier) @name)) @call
        (call_expression
            function: (scoped_identifier
                "::"
                name: (identifier) @name)) @call
        (macro_invocation
            macro: (identifier) @name) @call
        """,
    ),
    "go": create_lang_config(
        file_extensions=[".go"],
        function_node_types=["function_declaration", "method_declaration"],
        class_node_types=["type_declaration"],  # Go structs
        module_node_types=["source_file"],
        call_node_types=["call_expression"],
        import_node_types=["import_declaration"],
        import_from_node_types=["import_declaration"],  # Go uses same node for imports
    ),
    "scala": create_lang_config(
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
        import_node_types=COMMON_DECLARATION_IMPORT,
        import_from_node_types=COMMON_DECLARATION_IMPORT,  # Scala uses same node for imports
        package_indicators=[],  # Scala uses package declarations
    ),
    "java": create_lang_config(
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
            "record_declaration",
        ],
        module_node_types=["program"],
        package_indicators=[],  # Java uses package declarations
        call_node_types=["method_invocation"],
        import_node_types=COMMON_DECLARATION_IMPORT,
        import_from_node_types=COMMON_DECLARATION_IMPORT,  # Java uses same node for imports
        # Pre-formatted Tree-sitter queries for comprehensive Java parsing
        function_query="""
        (method_declaration
            name: (identifier) @name) @function
        (constructor_declaration
            name: (identifier) @name) @function
        """,
        class_query="""
        (class_declaration
            name: (identifier) @name) @class
        (interface_declaration
            name: (identifier) @name) @class
        (enum_declaration
            name: (identifier) @name) @class
        (annotation_type_declaration
            name: (identifier) @name) @class
        (record_declaration
            name: (identifier) @name) @class
        """,
        call_query="""
        (method_invocation
            name: (identifier) @name) @call
        (object_creation_expression
            type: (type_identifier) @name) @call
        """,
    ),
    "cpp": create_lang_config(
        file_extensions=[
            ".cpp",
            ".h",
            ".hpp",
            ".cc",
            ".cxx",
            ".hxx",
            ".hh",
            ".ixx",
            ".cppm",
            ".ccm",
        ],
        function_node_types=[
            "function_definition",  # Includes aliased constructor/destructor/operator definitions
            "declaration",  # Includes aliased constructor/destructor/operator declarations
            "field_declaration",  # For method declarations in classes
            "template_declaration",  # For template functions
            "lambda_expression",  # For lambda functions
        ],
        class_node_types=[
            "class_specifier",
            "struct_specifier",
            "union_specifier",
            "enum_specifier",
        ],
        module_node_types=[
            "translation_unit",
            "namespace_definition",
            "linkage_specification",  # extern "C" blocks
            "declaration",  # For module declarations like "module math_operations;"
        ],
        call_node_types=[
            "call_expression",
            "field_expression",  # For method calls like obj.method()
            "subscript_expression",  # For operator[] calls
            "new_expression",  # For new operator
            "delete_expression",  # For delete operator
            "binary_expression",  # For operator overloads like obj1 + obj2
            "unary_expression",  # For unary operators like ++obj
            "update_expression",  # For prefix/postfix increment/decrement
        ],
        import_node_types=CPP_IMPORTS,
        import_from_node_types=CPP_IMPORTS,
        # C++ specific configurations
        package_indicators=["CMakeLists.txt", "Makefile", "*.vcxproj", "conanfile.txt"],
        # Pre-formatted Tree-sitter queries for comprehensive C++ parsing
        function_query="""
    (function_definition) @function
    (template_declaration (function_definition)) @function
    (lambda_expression) @function
    (field_declaration) @function
    (declaration) @function
    """,
        class_query="""
    (class_specifier) @class
    (struct_specifier) @class
    (union_specifier) @class
    (enum_specifier) @class
    (template_declaration (class_specifier)) @class
    (template_declaration (struct_specifier)) @class
    (template_declaration (union_specifier)) @class
    (template_declaration (enum_specifier)) @class
    """,
        call_query="""
    (call_expression) @call
    (binary_expression) @call
    (unary_expression) @call
    (update_expression) @call
    (field_expression) @call
    (subscript_expression) @call
    (new_expression) @call
    (delete_expression) @call
    """,
    ),
    "c-sharp": create_lang_config(
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
            "class_declaration",
            "struct_declaration",
            "enum_declaration",
            "interface_declaration",
        ],
        module_node_types=["compilation_unit"],
        call_node_types=["invocation_expression"],
        import_node_types=COMMON_USING_DIRECTIVE,
        import_from_node_types=COMMON_USING_DIRECTIVE,  # C# uses using directives
    ),
    "php": create_lang_config(
        file_extensions=[".php"],
        function_node_types=[
            "function_static_declaration",
            "anonymous_function",
            "function_definition",
            "arrow_function",
        ],
        class_node_types=[
            "trait_declaration",
            "enum_declaration",
            "interface_declaration",
            "class_declaration",
        ],
        module_node_types=["program"],
        call_node_types=[
            "member_call_expression",
            "scoped_call_expression",
            "function_call_expression",
            "nullsafe_member_call_expression",
        ],
    ),
    "lua": create_lang_config(
        file_extensions=[".lua"],
        function_node_types=[
            "function_declaration",
            "function_definition",  # For assignment patterns: Calculator.divide = function() end
        ],
        class_node_types=[],
        module_node_types=["chunk"],
        call_node_types=["function_call"],
        import_node_types=["function_call"],
    ),
}


def _initialize_config_names() -> None:
    """Initialize config names based on dict keys."""
    for lang_name, config in LANGUAGE_CONFIGS.items():
        if not config.name:  # Only set if empty (from create_lang_config)
            config.name = lang_name


# Initialize names on module load
_initialize_config_names()


def get_language_config(file_extension: str) -> LanguageConfig | None:
    """Get language configuration based on file extension."""
    for config in LANGUAGE_CONFIGS.values():
        if file_extension in config.file_extensions:
            return config
    return None


def get_language_config_by_name(language_name: str) -> LanguageConfig | None:
    """Get language configuration by language name."""
    return LANGUAGE_CONFIGS.get(language_name.lower())


# =============================================================================
# LANGUAGE ADDITION HELPER FUNCTIONS
# =============================================================================


def create_c_family_config(
    name: str, extensions: list[str], **overrides: Any
) -> dict[str, Any]:
    """Helper to create C-family language configuration."""
    base_config = {
        "file_extensions": extensions,
        "function_node_types": SYSTEMS_FUNCTIONS,
        "class_node_types": SYSTEMS_CLASSES,
        "module_node_types": ["translation_unit", "namespace_definition"],
        "call_node_types": SYSTEMS_CALLS,
        "import_node_types": INCLUDE_IMPORTS,
        "import_from_node_types": INCLUDE_IMPORTS,
    }
    base_config.update(overrides)
    return {name: create_lang_config(**base_config)}


def create_scripting_config(
    name: str, extensions: list[str], **overrides: Any
) -> dict[str, Any]:
    """Helper to create scripting language configuration."""
    base_config = {
        "file_extensions": extensions,
        "function_node_types": SCRIPTING_FUNCTIONS,
        "class_node_types": SCRIPTING_CLASSES,
        "module_node_types": ["program"],
        "call_node_types": SCRIPTING_CALLS,
        "import_node_types": ["function_call"],  # require() style
        "import_from_node_types": ["function_call"],
    }
    base_config.update(overrides)
    return {name: create_lang_config(**base_config)}


def create_jvm_config(
    name: str, extensions: list[str], **overrides: Any
) -> dict[str, Any]:
    """Helper to create JVM language configuration."""
    base_config = {
        "file_extensions": extensions,
        "function_node_types": JVM_FUNCTIONS,
        "class_node_types": JVM_CLASSES,
        "module_node_types": ["compilation_unit"],
        "call_node_types": JVM_CALLS,
        "import_node_types": JVM_IMPORTS,
        "import_from_node_types": JVM_IMPORTS,
    }
    base_config.update(overrides)
    return {name: create_lang_config(**base_config)}


# Example usage for adding new languages:
#
# # Add Kotlin
# LANGUAGE_CONFIGS.update(create_jvm_config("kotlin", [".kt", ".kts"],
#     function_node_types=JVM_FUNCTIONS + LAMBDA_FUNCTIONS,
#     class_node_types=JVM_CLASSES + ["data_class", "sealed_class"]
# ))
#
# # Add Ruby
# LANGUAGE_CONFIGS.update(create_scripting_config("ruby", [".rb", ".rake"],
#     function_node_types=SCRIPTING_FUNCTIONS + ["method"],
#     class_node_types=SCRIPTING_CLASSES + ["module"]
# ))
#
# # Add Zig
# LANGUAGE_CONFIGS.update(create_c_family_config("zig", [".zig"],
#     function_node_types=SYSTEMS_FUNCTIONS + ["test_declaration"],
#     class_node_types=STRUCT_TYPES + ["error_set_declaration"]
# ))
