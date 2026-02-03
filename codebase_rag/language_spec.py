from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import constants as cs
from .models import FQNSpec, LanguageSpec

if TYPE_CHECKING:
    from tree_sitter import Node


def _python_get_name(node: Node) -> str | None:
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


def _js_get_name(node: Node) -> str | None:
    if node.type in cs.JS_NAME_NODE_TYPES:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
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


def _generic_get_name(node: Node) -> str | None:
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


def _rust_get_name(node: Node) -> str | None:
    if node.type in cs.RS_TYPE_NODE_TYPES:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        if name_node and name_node.type == cs.TS_TYPE_IDENTIFIER and name_node.text:
            return name_node.text.decode(cs.ENCODING_UTF8)
    elif node.type in cs.RS_IDENT_NODE_TYPES:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        if name_node and name_node.type == cs.TS_IDENTIFIER and name_node.text:
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


def _cpp_get_name(node: Node) -> str | None:
    if node.type in cs.CPP_NAME_NODE_TYPES:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        if name_node and name_node.text:
            return name_node.text.decode(cs.ENCODING_UTF8)
    elif node.type == cs.TS_CPP_FUNCTION_DEFINITION:
        declarator = node.child_by_field_name(cs.FIELD_DECLARATOR)
        if declarator and declarator.type == cs.TS_CPP_FUNCTION_DECLARATOR:
            name_node = declarator.child_by_field_name(cs.FIELD_DECLARATOR)
            if name_node and name_node.type == cs.TS_IDENTIFIER and name_node.text:
                return name_node.text.decode(cs.ENCODING_UTF8)

    return _generic_get_name(node)


PYTHON_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(cs.FQN_PY_SCOPE_TYPES),
    function_node_types=frozenset(cs.FQN_PY_FUNCTION_TYPES),
    get_name=_python_get_name,
    file_to_module_parts=_python_file_to_module,
)

JS_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(cs.FQN_JS_SCOPE_TYPES),
    function_node_types=frozenset(cs.FQN_JS_FUNCTION_TYPES),
    get_name=_js_get_name,
    file_to_module_parts=_js_file_to_module,
)

TS_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(cs.FQN_TS_SCOPE_TYPES),
    function_node_types=frozenset(cs.FQN_TS_FUNCTION_TYPES),
    get_name=_js_get_name,
    file_to_module_parts=_js_file_to_module,
)

RUST_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(cs.FQN_RS_SCOPE_TYPES),
    function_node_types=frozenset(cs.FQN_RS_FUNCTION_TYPES),
    get_name=_rust_get_name,
    file_to_module_parts=_rust_file_to_module,
)

JAVA_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(cs.FQN_JAVA_SCOPE_TYPES),
    function_node_types=frozenset(cs.FQN_JAVA_FUNCTION_TYPES),
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

CPP_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(cs.FQN_CPP_SCOPE_TYPES),
    function_node_types=frozenset(cs.FQN_CPP_FUNCTION_TYPES),
    get_name=_cpp_get_name,
    file_to_module_parts=_generic_file_to_module,
)

LUA_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(cs.FQN_LUA_SCOPE_TYPES),
    function_node_types=frozenset(cs.FQN_LUA_FUNCTION_TYPES),
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

GO_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(cs.FQN_GO_SCOPE_TYPES),
    function_node_types=frozenset(cs.FQN_GO_FUNCTION_TYPES),
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

SCALA_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(cs.FQN_SCALA_SCOPE_TYPES),
    function_node_types=frozenset(cs.FQN_SCALA_FUNCTION_TYPES),
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

CSHARP_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(cs.FQN_CS_SCOPE_TYPES),
    function_node_types=frozenset(cs.FQN_CS_FUNCTION_TYPES),
    get_name=_generic_get_name,
    file_to_module_parts=_generic_file_to_module,
)

PHP_FQN_SPEC = FQNSpec(
    scope_node_types=frozenset(cs.FQN_PHP_SCOPE_TYPES),
    function_node_types=frozenset(cs.FQN_PHP_FUNCTION_TYPES),
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
        file_extensions=cs.PY_EXTENSIONS,
        function_node_types=cs.SPEC_PY_FUNCTION_TYPES,
        class_node_types=cs.SPEC_PY_CLASS_TYPES,
        module_node_types=cs.SPEC_PY_MODULE_TYPES,
        call_node_types=cs.SPEC_PY_CALL_TYPES,
        import_node_types=cs.SPEC_PY_IMPORT_TYPES,
        import_from_node_types=cs.SPEC_PY_IMPORT_FROM_TYPES,
        package_indicators=cs.SPEC_PY_PACKAGE_INDICATORS,
    ),
    cs.SupportedLanguage.JS: LanguageSpec(
        language=cs.SupportedLanguage.JS,
        file_extensions=cs.JS_EXTENSIONS,
        function_node_types=cs.JS_TS_FUNCTION_NODES,
        class_node_types=cs.JS_TS_CLASS_NODES,
        module_node_types=cs.SPEC_JS_MODULE_TYPES,
        call_node_types=cs.SPEC_JS_CALL_TYPES,
        import_node_types=cs.JS_TS_IMPORT_NODES,
        import_from_node_types=cs.JS_TS_IMPORT_NODES,
    ),
    cs.SupportedLanguage.TS: LanguageSpec(
        language=cs.SupportedLanguage.TS,
        file_extensions=cs.TS_EXTENSIONS,
        function_node_types=cs.JS_TS_FUNCTION_NODES + (cs.TS_FUNCTION_SIGNATURE,),
        class_node_types=cs.JS_TS_CLASS_NODES
        + (
            cs.TS_ABSTRACT_CLASS_DECLARATION,
            cs.TS_ENUM_DECLARATION,
            cs.TS_INTERFACE_DECLARATION,
            cs.TS_TYPE_ALIAS_DECLARATION,
            cs.TS_INTERNAL_MODULE,
        ),
        module_node_types=cs.SPEC_JS_MODULE_TYPES,
        call_node_types=cs.SPEC_JS_CALL_TYPES,
        import_node_types=cs.JS_TS_IMPORT_NODES,
        import_from_node_types=cs.JS_TS_IMPORT_NODES,
    ),
    cs.SupportedLanguage.RUST: LanguageSpec(
        language=cs.SupportedLanguage.RUST,
        file_extensions=cs.RS_EXTENSIONS,
        function_node_types=cs.SPEC_RS_FUNCTION_TYPES,
        class_node_types=cs.SPEC_RS_CLASS_TYPES,
        module_node_types=cs.SPEC_RS_MODULE_TYPES,
        call_node_types=cs.SPEC_RS_CALL_TYPES,
        import_node_types=cs.SPEC_RS_IMPORT_TYPES,
        import_from_node_types=cs.SPEC_RS_IMPORT_FROM_TYPES,
        package_indicators=cs.SPEC_RS_PACKAGE_INDICATORS,
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
        file_extensions=cs.GO_EXTENSIONS,
        function_node_types=cs.SPEC_GO_FUNCTION_TYPES,
        class_node_types=cs.SPEC_GO_CLASS_TYPES,
        module_node_types=cs.SPEC_GO_MODULE_TYPES,
        call_node_types=cs.SPEC_GO_CALL_TYPES,
        import_node_types=cs.SPEC_GO_IMPORT_TYPES,
        import_from_node_types=cs.SPEC_GO_IMPORT_TYPES,
    ),
    cs.SupportedLanguage.SCALA: LanguageSpec(
        language=cs.SupportedLanguage.SCALA,
        file_extensions=cs.SCALA_EXTENSIONS,
        function_node_types=cs.SPEC_SCALA_FUNCTION_TYPES,
        class_node_types=cs.SPEC_SCALA_CLASS_TYPES,
        module_node_types=cs.SPEC_SCALA_MODULE_TYPES,
        call_node_types=cs.SPEC_SCALA_CALL_TYPES,
        import_node_types=cs.SPEC_SCALA_IMPORT_TYPES,
        import_from_node_types=cs.SPEC_SCALA_IMPORT_TYPES,
    ),
    cs.SupportedLanguage.JAVA: LanguageSpec(
        language=cs.SupportedLanguage.JAVA,
        file_extensions=cs.JAVA_EXTENSIONS,
        function_node_types=cs.SPEC_JAVA_FUNCTION_TYPES,
        class_node_types=cs.SPEC_JAVA_CLASS_TYPES,
        module_node_types=cs.SPEC_JAVA_MODULE_TYPES,
        call_node_types=cs.SPEC_JAVA_CALL_TYPES,
        import_node_types=cs.SPEC_JAVA_IMPORT_TYPES,
        import_from_node_types=cs.SPEC_JAVA_IMPORT_TYPES,
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
        file_extensions=cs.CPP_EXTENSIONS,
        function_node_types=cs.SPEC_CPP_FUNCTION_TYPES,
        class_node_types=cs.SPEC_CPP_CLASS_TYPES,
        module_node_types=cs.SPEC_CPP_MODULE_TYPES,
        call_node_types=cs.SPEC_CPP_CALL_TYPES,
        import_node_types=cs.CPP_IMPORT_NODES,
        import_from_node_types=cs.CPP_IMPORT_NODES,
        package_indicators=cs.SPEC_CPP_PACKAGE_INDICATORS,
        function_query="""
    (field_declaration) @function
    (declaration) @function
    (function_definition) @function
    (template_declaration (function_definition)) @function
    (lambda_expression) @function
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
        file_extensions=cs.CS_EXTENSIONS,
        function_node_types=cs.SPEC_CS_FUNCTION_TYPES,
        class_node_types=cs.SPEC_CS_CLASS_TYPES,
        module_node_types=cs.SPEC_CS_MODULE_TYPES,
        call_node_types=cs.SPEC_CS_CALL_TYPES,
        import_node_types=cs.IMPORT_NODES_USING,
        import_from_node_types=cs.IMPORT_NODES_USING,
    ),
    cs.SupportedLanguage.PHP: LanguageSpec(
        language=cs.SupportedLanguage.PHP,
        file_extensions=cs.PHP_EXTENSIONS,
        function_node_types=cs.SPEC_PHP_FUNCTION_TYPES,
        class_node_types=cs.SPEC_PHP_CLASS_TYPES,
        module_node_types=cs.SPEC_PHP_MODULE_TYPES,
        call_node_types=cs.SPEC_PHP_CALL_TYPES,
    ),
    cs.SupportedLanguage.LUA: LanguageSpec(
        language=cs.SupportedLanguage.LUA,
        file_extensions=cs.LUA_EXTENSIONS,
        function_node_types=cs.SPEC_LUA_FUNCTION_TYPES,
        class_node_types=cs.SPEC_LUA_CLASS_TYPES,
        module_node_types=cs.SPEC_LUA_MODULE_TYPES,
        call_node_types=cs.SPEC_LUA_CALL_TYPES,
        import_node_types=cs.SPEC_LUA_IMPORT_TYPES,
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
