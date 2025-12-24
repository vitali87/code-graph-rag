from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import LANGUAGE_FQN_SPECS
from ...utils.fqn_resolver import resolve_fqn_from_ast
from ..cpp import utils as cpp_utils
from ..rs import utils as rs_utils
from ..utils import safe_decode_text

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec


def resolve_class_identity(
    class_node: Node,
    module_qn: str,
    language: cs.SupportedLanguage,
    lang_config: LanguageSpec,
    file_path: Path | None,
    repo_path: Path,
    project_name: str,
) -> tuple[str, str, bool] | None:
    if (fqn_config := LANGUAGE_FQN_SPECS.get(language)) and file_path:
        if class_qn := resolve_fqn_from_ast(
            class_node,
            file_path,
            repo_path,
            project_name,
            fqn_config,
        ):
            class_name = class_qn.split(cs.SEPARATOR_DOT)[-1]
            is_exported = language == cs.SupportedLanguage.CPP and (
                class_node.type == cs.CppNodeType.FUNCTION_DEFINITION
                or cpp_utils.is_exported(class_node)
            )
            return class_qn, class_name, is_exported

    return resolve_class_identity_fallback(class_node, module_qn, language, lang_config)


def resolve_class_identity_fallback(
    class_node: Node,
    module_qn: str,
    language: cs.SupportedLanguage,
    lang_config: LanguageSpec,
) -> tuple[str, str, bool] | None:
    if language == cs.SupportedLanguage.CPP:
        if class_node.type == cs.CppNodeType.FUNCTION_DEFINITION:
            class_name = cpp_utils.extract_exported_class_name(class_node)
            is_exported = True
        else:
            class_name = extract_cpp_class_name(class_node)
            is_exported = cpp_utils.is_exported(class_node)

        if not class_name:
            return None
        class_qn = cpp_utils.build_qualified_name(class_node, module_qn, class_name)
        return class_qn, class_name, is_exported

    class_name = extract_class_name(class_node)
    if not class_name:
        return None
    nested_qn = build_nested_qualified_name_for_class(
        class_node, module_qn, class_name, lang_config
    )
    return nested_qn or f"{module_qn}.{class_name}", class_name, False


def extract_cpp_class_name(class_node: Node) -> str | None:
    if class_node.type == cs.CppNodeType.TEMPLATE_DECLARATION:
        for child in class_node.children:
            if child.type in cs.CPP_COMPOUND_TYPES:
                return extract_cpp_class_name(child)

    for child in class_node.children:
        if child.type == cs.TS_TYPE_IDENTIFIER and child.text:
            return safe_decode_text(child)

    name_node = class_node.child_by_field_name(cs.KEY_NAME)
    return safe_decode_text(name_node) if name_node and name_node.text else None


def extract_class_name(class_node: Node) -> str | None:
    name_node = class_node.child_by_field_name(cs.KEY_NAME)
    if name_node and name_node.text:
        return safe_decode_text(name_node)

    current = class_node.parent
    while current:
        if current.type == cs.TS_VARIABLE_DECLARATOR:
            for child in current.children:
                if child.type == cs.TS_IDENTIFIER and child.text:
                    return safe_decode_text(child)
        current = current.parent

    return None


def build_nested_qualified_name_for_class(
    class_node: Node,
    module_qn: str,
    class_name: str,
    lang_config: LanguageSpec,
) -> str | None:
    if not isinstance(class_node.parent, Node):
        return None

    path_parts = rs_utils.build_module_path(
        class_node,
        include_classes=True,
        class_node_types=lang_config.class_node_types,
    )

    if path_parts:
        return f"{module_qn}.{cs.SEPARATOR_DOT.join(path_parts)}.{class_name}"
    return None
