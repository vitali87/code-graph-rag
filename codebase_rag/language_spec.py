from pathlib import Path
from typing import TYPE_CHECKING

from . import constants as cs
from .models import FQNSpec, LanguageSpec

if TYPE_CHECKING:
    from tree_sitter import Node


def _python_get_name(node: "Node") -> str | None:
    name_node = node.child_by_field_name("name")
    return (
        name_node.text.decode(cs.ENCODING_UTF8)
        if name_node and name_node.text
        else None
    )


def _python_file_to_module(file_path: Path, repo_root: Path) -> list[str]:
    try:
        rel = file_path.relative_to(repo_root)
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == cs.INDEX_INIT:
            parts = parts[:-1]
        return parts
    except ValueError:
        return []


def _js_get_name(node: "Node") -> str | None:
    if node.type in ("function_declaration", "class_declaration", "method_definition"):
        name_node = node.child_by_field_name("name")
        return (
            name_node.text.decode(cs.ENCODING_UTF8)
            if name_node and name_node.text
            else None
        )
    return None


def _js_file_to_module(file_path: Path, repo_root: Path) -> list[str]:
    try:
        rel = file_path.relative_to(repo_root)
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == cs.INDEX_INDEX:
            parts = parts[:-1]
        return parts
    except ValueError:
        return []


def _generic_get_name(node: "Node") -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node and name_node.text:
        return name_node.text.decode(cs.ENCODING_UTF8)

    for field_name in cs.NAME_FIELDS:
        name_node = node.child_by_field_name(field_name)
        if name_node and name_node.text:
            return name_node.text.decode(cs.ENCODING_UTF8)

    return None


def _generic_file_to_module(file_path: Path, repo_root: Path) -> list[str]:
    try:
        rel = file_path.relative_to(repo_root)
        return list(rel.with_suffix("").parts)
    except ValueError:
        return []


def _rust_get_name(node: "Node") -> str | None:
    if node.type in ("struct_item", "enum_item", "trait_item", "type_item"):
        name_node = node.child_by_field_name("name")
        if name_node and name_node.type == "type_identifier" and name_node.text:
            return name_node.text.decode(cs.ENCODING_UTF8)
    elif node.type in ("function_item", "mod_item"):
        name_node = node.child_by_field_name("name")
        if name_node and name_node.type == "identifier" and name_node.text:
            return name_node.text.decode(cs.ENCODING_UTF8)

    return _generic_get_name(node)


def _rust_file_to_module(file_path: Path, repo_root: Path) -> list[str]:
    try:
        rel = file_path.relative_to(repo_root)
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == cs.INDEX_MOD:
            parts = parts[:-1]
        return parts
    except ValueError:
        return []


def _cpp_get_name(node: "Node") -> str | None:
    if node.type in ("class_specifier", "struct_specifier", "enum_specifier"):
        name_node = node.child_by_field_name("name")
        if name_node and name_node.text:
            return name_node.text.decode(cs.ENCODING_UTF8)
    elif node.type == "function_definition":
        declarator = node.child_by_field_name("declarator")
        if declarator and declarator.type == "function_declarator":
            name_node = declarator.child_by_field_name("declarator")
            if name_node and name_node.type == "identifier" and name_node.text:
                return name_node.text.decode(cs.ENCODING_UTF8)

    return _generic_get_name(node)


PYTHON_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset({"class_definition", "module", "function_definition"}),
    function_node_types=frozenset({"function_definition"}),
    get_name=_python_get_name,
    file_to_module_parts=_python_file_to_module,
)

JS_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(
        {
            "class_declaration",
            "program",
            "function_declaration",
            "function_expression",
            "arrow_function",
            "method_definition",
        }
    ),
    function_node_types=frozenset(
        {
            "function_declaration",
            "method_definition",
            "arrow_function",
            "function_expression",
        }
    ),
    get_name=_js_get_name,
    file_to_module_parts=_js_file_to_module,
)

TS_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "namespace_definition",
            "program",
            "function_declaration",
            "function_expression",
            "arrow_function",
            "method_definition",
        }
    ),
    function_node_types=frozenset(
        {
            "function_declaration",
            "method_definition",
            "arrow_function",
            "function_expression",
            "function_signature",
        }
    ),
    get_name=_js_get_name,
    file_to_module_parts=_js_file_to_module,
)

RUST_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(
        {
            "struct_item",
            "enum_item",
            "trait_item",
            "impl_item",
            "mod_item",
            "source_file",
        }
    ),
    function_node_types=frozenset(
        {
            "function_item",
            "function_signature_item",
            "closure_expression",
        }
    ),
    get_name=_rust_get_name,
    file_to_module_parts=_rust_file_to_module,
)

JAVA_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "program",
        }
    ),
    function_node_types=frozenset({"method_declaration", "constructor_declaration"}),
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

CPP_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(
        {
            "class_specifier",
            "struct_specifier",
            "namespace_definition",
            "translation_unit",
        }
    ),
    function_node_types=frozenset(
        {
            "function_definition",
            "declaration",
            "field_declaration",
            "template_declaration",
            "lambda_expression",
        }
    ),
    get_name=_cpp_get_name,
    file_to_module_parts=_generic_file_to_module,
)

LUA_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset({"chunk"}),
    function_node_types=frozenset({"function_declaration", "function_definition"}),
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

GO_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset({"type_declaration", "source_file"}),
    function_node_types=frozenset({"function_declaration", "method_declaration"}),
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

SCALA_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(
        {
            "class_definition",
            "object_definition",
            "trait_definition",
            "compilation_unit",
        }
    ),
    function_node_types=frozenset({"function_definition", "function_declaration"}),
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

CSHARP_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(
        {
            "class_declaration",
            "struct_declaration",
            "interface_declaration",
            "compilation_unit",
        }
    ),
    function_node_types=frozenset(
        {
            "destructor_declaration",
            "local_function_statement",
            "function_pointer_type",
            "constructor_declaration",
            "anonymous_method_expression",
            "lambda_expression",
            "method_declaration",
        }
    ),
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

PHP_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "trait_declaration",
            "program",
        }
    ),
    function_node_types=frozenset(
        {
            "function_definition",
            "anonymous_function",
            "arrow_function",
            "function_static_declaration",
        }
    ),
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

LANGUAGE_FQN_SPECS: dict[cs.SupportedLanguage, FQNSpec] = {
    cs.SupportedLanguage.PYTHON: PYTHON_FQN_SPEC,
    cs.SupportedLanguage.JS: JS_FQN_SPEC,
    cs.SupportedLanguage.TS: TS_FQN_SPEC,
    cs.SupportedLanguage.RUST: RUST_FQN_SPEC,
    cs.SupportedLanguage.JAVA: JAVA_FQN_SPEC,
    cs.SupportedLanguage.CPP: CPP_FQN_SPEC,
    cs.SupportedLanguage.LUA: LUA_FQN_SPEC,
    cs.SupportedLanguage.GO: GO_FQN_SPEC,
    cs.SupportedLanguage.SCALA: SCALA_FQN_SPEC,
    cs.SupportedLanguage.CSHARP: CSHARP_FQN_SPEC,
    cs.SupportedLanguage.PHP: PHP_FQN_SPEC,
}


LANGUAGE_SPECS: dict[cs.SupportedLanguage, LanguageSpec] = {
    cs.SupportedLanguage.PYTHON: LanguageSpec(
        language=cs.SupportedLanguage.PYTHON,
        file_extensions=(".py",),
        function_node_types=("function_definition",),
        class_node_types=("class_definition",),
        module_node_types=("module",),
        call_node_types=("call", "with_statement"),
        import_node_types=("import_statement",),
        import_from_node_types=("import_from_statement",),
        package_indicators=("__init__.py",),
    ),
    cs.SupportedLanguage.JS: LanguageSpec(
        language=cs.SupportedLanguage.JS,
        file_extensions=(".js", ".jsx"),
        function_node_types=cs.JS_TS_FUNCTION_NODES,
        class_node_types=cs.JS_TS_CLASS_NODES,
        module_node_types=("program",),
        call_node_types=("call_expression",),
        import_node_types=cs.JS_TS_IMPORT_NODES,
        import_from_node_types=cs.JS_TS_IMPORT_NODES,
    ),
    cs.SupportedLanguage.TS: LanguageSpec(
        language=cs.SupportedLanguage.TS,
        file_extensions=(".ts", ".tsx"),
        function_node_types=cs.JS_TS_FUNCTION_NODES + ("function_signature",),
        class_node_types=cs.JS_TS_CLASS_NODES
        + (
            "abstract_class_declaration",
            "enum_declaration",
            "interface_declaration",
            "type_alias_declaration",
            "internal_module",
        ),
        module_node_types=("program",),
        call_node_types=("call_expression",),
        import_node_types=cs.JS_TS_IMPORT_NODES,
        import_from_node_types=cs.JS_TS_IMPORT_NODES,
    ),
    cs.SupportedLanguage.RUST: LanguageSpec(
        language=cs.SupportedLanguage.RUST,
        file_extensions=(".rs",),
        function_node_types=(
            "function_item",
            "function_signature_item",
            "closure_expression",
        ),
        class_node_types=(
            "struct_item",
            "enum_item",
            "union_item",
            "trait_item",
            "impl_item",
            "type_item",
        ),
        module_node_types=("source_file", "mod_item"),
        call_node_types=("call_expression", "macro_invocation"),
        import_node_types=("use_declaration", "extern_crate_declaration"),
        import_from_node_types=("use_declaration",),
        package_indicators=("Cargo.toml",),
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
    cs.SupportedLanguage.GO: LanguageSpec(
        language=cs.SupportedLanguage.GO,
        file_extensions=(".go",),
        function_node_types=("function_declaration", "method_declaration"),
        class_node_types=("type_declaration",),
        module_node_types=("source_file",),
        call_node_types=("call_expression",),
        import_node_types=("import_declaration",),
        import_from_node_types=("import_declaration",),
    ),
    cs.SupportedLanguage.SCALA: LanguageSpec(
        language=cs.SupportedLanguage.SCALA,
        file_extensions=(".scala", ".sc"),
        function_node_types=("function_definition", "function_declaration"),
        class_node_types=(
            "class_definition",
            "object_definition",
            "trait_definition",
        ),
        module_node_types=("compilation_unit",),
        call_node_types=(
            "call_expression",
            "generic_function",
            "field_expression",
            "infix_expression",
        ),
        import_node_types=("import_declaration",),
        import_from_node_types=("import_declaration",),
    ),
    cs.SupportedLanguage.JAVA: LanguageSpec(
        language=cs.SupportedLanguage.JAVA,
        file_extensions=(".java",),
        function_node_types=("method_declaration", "constructor_declaration"),
        class_node_types=(
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "annotation_type_declaration",
            "record_declaration",
        ),
        module_node_types=("program",),
        call_node_types=("method_invocation",),
        import_node_types=("import_declaration",),
        import_from_node_types=("import_declaration",),
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
    cs.SupportedLanguage.CPP: LanguageSpec(
        language=cs.SupportedLanguage.CPP,
        file_extensions=(
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
        ),
        function_node_types=(
            "function_definition",
            "declaration",
            "field_declaration",
            "template_declaration",
            "lambda_expression",
        ),
        class_node_types=(
            "class_specifier",
            "struct_specifier",
            "union_specifier",
            "enum_specifier",
        ),
        module_node_types=(
            "translation_unit",
            "namespace_definition",
            "linkage_specification",
            "declaration",
        ),
        call_node_types=(
            "call_expression",
            "field_expression",
            "subscript_expression",
            "new_expression",
            "delete_expression",
            "binary_expression",
            "unary_expression",
            "update_expression",
        ),
        import_node_types=cs.CPP_IMPORT_NODES,
        import_from_node_types=cs.CPP_IMPORT_NODES,
        package_indicators=("CMakeLists.txt", "Makefile", "*.vcxproj", "conanfile.txt"),
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
    cs.SupportedLanguage.CSHARP: LanguageSpec(
        language=cs.SupportedLanguage.CSHARP,
        file_extensions=(".cs",),
        function_node_types=(
            "destructor_declaration",
            "local_function_statement",
            "function_pointer_type",
            "constructor_declaration",
            "anonymous_method_expression",
            "lambda_expression",
            "method_declaration",
        ),
        class_node_types=(
            "class_declaration",
            "struct_declaration",
            "enum_declaration",
            "interface_declaration",
        ),
        module_node_types=("compilation_unit",),
        call_node_types=("invocation_expression",),
        import_node_types=cs.IMPORT_NODES_USING,
        import_from_node_types=cs.IMPORT_NODES_USING,
    ),
    cs.SupportedLanguage.PHP: LanguageSpec(
        language=cs.SupportedLanguage.PHP,
        file_extensions=(".php",),
        function_node_types=(
            "function_static_declaration",
            "anonymous_function",
            "function_definition",
            "arrow_function",
        ),
        class_node_types=(
            "trait_declaration",
            "enum_declaration",
            "interface_declaration",
            "class_declaration",
        ),
        module_node_types=("program",),
        call_node_types=(
            "member_call_expression",
            "scoped_call_expression",
            "function_call_expression",
            "nullsafe_member_call_expression",
        ),
    ),
    cs.SupportedLanguage.LUA: LanguageSpec(
        language=cs.SupportedLanguage.LUA,
        file_extensions=(".lua",),
        function_node_types=("function_declaration", "function_definition"),
        class_node_types=(),
        module_node_types=("chunk",),
        call_node_types=("function_call",),
        import_node_types=("function_call",),
    ),
}

_EXTENSION_TO_SPEC: dict[str, LanguageSpec] = {}
for _config in LANGUAGE_SPECS.values():
    for _ext in _config.file_extensions:
        _EXTENSION_TO_SPEC[_ext] = _config


def get_language_spec(file_extension: str) -> LanguageSpec | None:
    return _EXTENSION_TO_SPEC.get(file_extension)


def get_language_for_extension(file_extension: str) -> cs.SupportedLanguage | None:
    spec = _EXTENSION_TO_SPEC.get(file_extension)
    if spec and isinstance(spec.language, cs.SupportedLanguage):
        return spec.language
    return None


def get_language_spec_by_name(language_name: str) -> LanguageSpec | None:
    try:
        lang_key = cs.SupportedLanguage(language_name.lower())
        return LANGUAGE_SPECS.get(lang_key)
    except ValueError:
        return None
