from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ...models import MethodModifiersAndAnnotations
from ...types_defs import (
    ASTNode,
    JavaAnnotationInfo,
    JavaClassInfo,
    JavaFieldInfo,
    JavaMethodCallInfo,
    JavaMethodInfo,
)
from ..utils import safe_decode_text

if TYPE_CHECKING:
    from ...types_defs import ASTCacheProtocol


class ClassContext(NamedTuple):
    module_qn: str
    target_class_name: str
    root_node: Node


def get_root_node_from_module_qn(
    module_qn: str,
    module_qn_to_file_path: dict[str, Path],
    ast_cache: ASTCacheProtocol,
    min_parts: int = 2,
) -> Node | None:
    parts = module_qn.split(cs.SEPARATOR_DOT)
    if len(parts) < min_parts:
        return None

    file_path = module_qn_to_file_path.get(module_qn)
    if file_path is None or file_path not in ast_cache:
        return None

    root_node, _ = ast_cache[file_path]
    return root_node


def get_class_context_from_qn(
    class_qn: str,
    module_qn_to_file_path: dict[str, Path],
    ast_cache: ASTCacheProtocol,
) -> ClassContext | None:
    parts = class_qn.split(cs.SEPARATOR_DOT)
    if len(parts) < 2:
        return None

    module_qn = cs.SEPARATOR_DOT.join(parts[:-1])
    target_class_name = parts[-1]

    root_node = get_root_node_from_module_qn(
        module_qn, module_qn_to_file_path, ast_cache, min_parts=1
    )
    if root_node is None:
        return None

    return ClassContext(module_qn, target_class_name, root_node)


def extract_package_name(package_node: ASTNode) -> str | None:
    if package_node.type != cs.TS_PACKAGE_DECLARATION:
        return None

    return next(
        (
            safe_decode_text(child)
            for child in package_node.children
            if child.type in [cs.TS_SCOPED_IDENTIFIER, cs.TS_IDENTIFIER]
        ),
        None,
    )


def extract_import_path(import_node: ASTNode) -> dict[str, str]:
    if import_node.type != cs.TS_IMPORT_DECLARATION:
        return {}

    imports: dict[str, str] = {}
    imported_path = None
    is_wildcard = False

    for child in import_node.children:
        match child.type:
            case cs.TS_STATIC:
                pass
            case cs.TS_SCOPED_IDENTIFIER | cs.TS_IDENTIFIER:
                imported_path = safe_decode_text(child)
            case cs.TS_ASTERISK:
                is_wildcard = True

    if not imported_path:
        return imports

    if is_wildcard:
        wildcard_key = f"*{imported_path}"
        imports[wildcard_key] = imported_path
    elif parts := imported_path.split(cs.SEPARATOR_DOT):
        imported_name = parts[-1]
        imports[imported_name] = imported_path

    return imports


def _extract_superclass(class_node: ASTNode) -> str | None:
    superclass_node = class_node.child_by_field_name(cs.TS_FIELD_SUPERCLASS)
    if not superclass_node:
        return None

    match superclass_node.type:
        case cs.TS_TYPE_IDENTIFIER:
            return safe_decode_text(superclass_node)
        case cs.TS_GENERIC_TYPE:
            for child in superclass_node.children:
                if child.type == cs.TS_TYPE_IDENTIFIER:
                    return safe_decode_text(child)
    return None


def _extract_interface_name(type_child: ASTNode) -> str | None:
    match type_child.type:
        case cs.TS_TYPE_IDENTIFIER:
            return safe_decode_text(type_child)
        case cs.TS_GENERIC_TYPE:
            for sub_child in type_child.children:
                if sub_child.type == cs.TS_TYPE_IDENTIFIER:
                    return safe_decode_text(sub_child)
    return None


def _extract_interfaces(class_node: ASTNode) -> list[str]:
    interfaces_node = class_node.child_by_field_name(cs.TS_FIELD_INTERFACES)
    if not interfaces_node:
        return []

    interfaces: list[str] = []
    for child in interfaces_node.children:
        if child.type == cs.TS_TYPE_LIST:
            for type_child in child.children:
                if interface_name := _extract_interface_name(type_child):
                    interfaces.append(interface_name)
    return interfaces


def _extract_type_parameters(class_node: ASTNode) -> list[str]:
    type_params_node = class_node.child_by_field_name(cs.TS_FIELD_TYPE_PARAMETERS)
    if not type_params_node:
        return []

    type_parameters: list[str] = []
    for child in type_params_node.children:
        if child.type == cs.TS_TYPE_PARAMETER:
            if param_name := safe_decode_text(
                child.child_by_field_name(cs.TS_FIELD_NAME)
            ):
                type_parameters.append(param_name)
    return type_parameters


def _extract_class_modifiers(class_node: ASTNode) -> list[str]:
    modifiers: list[str] = []
    for child in class_node.children:
        if child.type == cs.TS_MODIFIERS:
            for modifier_child in child.children:
                if modifier_child.type in cs.JAVA_CLASS_MODIFIERS:
                    if modifier := safe_decode_text(modifier_child):
                        modifiers.append(modifier)
    return modifiers


def extract_class_info(class_node: ASTNode) -> JavaClassInfo:
    if class_node.type not in cs.JAVA_CLASS_NODE_TYPES:
        return JavaClassInfo(
            name=None,
            type="",
            superclass=None,
            interfaces=[],
            modifiers=[],
            type_parameters=[],
        )

    name: str | None = None
    if name_node := class_node.child_by_field_name(cs.TS_FIELD_NAME):
        name = safe_decode_text(name_node)

    return JavaClassInfo(
        name=name,
        type=class_node.type.replace(cs.JAVA_DECLARATION_SUFFIX, ""),
        superclass=_extract_superclass(class_node),
        interfaces=_extract_interfaces(class_node),
        modifiers=_extract_class_modifiers(class_node),
        type_parameters=_extract_type_parameters(class_node),
    )


def _get_method_type(method_node: ASTNode) -> str:
    if method_node.type == cs.TS_CONSTRUCTOR_DECLARATION:
        return cs.JAVA_TYPE_CONSTRUCTOR
    return cs.JAVA_TYPE_METHOD


def _extract_method_return_type(method_node: ASTNode) -> str | None:
    if method_node.type != cs.TS_METHOD_DECLARATION:
        return None
    if type_node := method_node.child_by_field_name(cs.TS_FIELD_TYPE):
        return safe_decode_text(type_node)
    return None


def _extract_formal_param_type(param_node: ASTNode) -> str | None:
    if param_type_node := param_node.child_by_field_name(cs.TS_FIELD_TYPE):
        return safe_decode_text(param_type_node)
    return None


def _extract_spread_param_type(spread_node: ASTNode) -> str | None:
    for subchild in spread_node.children:
        if subchild.type == cs.TS_TYPE_IDENTIFIER:
            if param_type_text := safe_decode_text(subchild):
                return f"{param_type_text}..."
    return None


def _extract_method_parameters(method_node: ASTNode) -> list[str]:
    params_node = method_node.child_by_field_name(cs.TS_FIELD_PARAMETERS)
    if not params_node:
        return []

    parameters: list[str] = []
    for child in params_node.children:
        param_type: str | None = None
        match child.type:
            case cs.TS_FORMAL_PARAMETER:
                param_type = _extract_formal_param_type(child)
            case cs.TS_SPREAD_PARAMETER:
                param_type = _extract_spread_param_type(child)
        if param_type:
            parameters.append(param_type)
    return parameters


def _extract_modifiers_and_annotations(
    method_node: ASTNode,
) -> MethodModifiersAndAnnotations:
    result = MethodModifiersAndAnnotations()
    for child in method_node.children:
        if child.type != cs.TS_MODIFIERS:
            continue
        for modifier_child in child.children:
            match modifier_child.type:
                case _ if modifier_child.type in cs.JAVA_METHOD_MODIFIERS:
                    if modifier := safe_decode_text(modifier_child):
                        result.modifiers.append(modifier)
                case cs.TS_ANNOTATION:
                    if annotation := safe_decode_text(modifier_child):
                        result.annotations.append(annotation)
    return result


def extract_method_info(method_node: ASTNode) -> JavaMethodInfo:
    if method_node.type not in cs.JAVA_METHOD_NODE_TYPES:
        return JavaMethodInfo(
            name=None,
            type="",
            return_type=None,
            parameters=[],
            modifiers=[],
            type_parameters=[],
            annotations=[],
        )

    mods_and_annots = _extract_modifiers_and_annotations(method_node)

    return JavaMethodInfo(
        name=safe_decode_text(method_node.child_by_field_name(cs.TS_FIELD_NAME)),
        type=_get_method_type(method_node),
        return_type=_extract_method_return_type(method_node),
        parameters=_extract_method_parameters(method_node),
        modifiers=mods_and_annots.modifiers,
        type_parameters=[],
        annotations=mods_and_annots.annotations,
    )


def extract_field_info(field_node: ASTNode) -> JavaFieldInfo:
    if field_node.type != cs.TS_FIELD_DECLARATION:
        return JavaFieldInfo(
            name=None,
            type=None,
            modifiers=[],
            annotations=[],
        )

    modifiers: list[str] = []
    annotations: list[str] = []

    field_type: str | None = None
    if type_node := field_node.child_by_field_name(cs.TS_FIELD_TYPE):
        field_type = safe_decode_text(type_node)

    name: str | None = None
    declarator_node = field_node.child_by_field_name(cs.TS_FIELD_DECLARATOR)
    if declarator_node and declarator_node.type == cs.TS_VARIABLE_DECLARATOR:
        if name_node := declarator_node.child_by_field_name(cs.TS_FIELD_NAME):
            name = safe_decode_text(name_node)

    for child in field_node.children:
        if child.type == cs.TS_MODIFIERS:
            for modifier_child in child.children:
                match modifier_child.type:
                    case _ if modifier_child.type in cs.JAVA_FIELD_MODIFIERS:
                        if modifier := safe_decode_text(modifier_child):
                            modifiers.append(modifier)
                    case cs.TS_ANNOTATION:
                        if annotation := safe_decode_text(modifier_child):
                            annotations.append(annotation)

    return JavaFieldInfo(
        name=name,
        type=field_type,
        modifiers=modifiers,
        annotations=annotations,
    )


def extract_method_call_info(call_node: ASTNode) -> JavaMethodCallInfo | None:
    if call_node.type != cs.TS_METHOD_INVOCATION:
        return None

    name: str | None = None
    if name_node := call_node.child_by_field_name(cs.TS_FIELD_NAME):
        name = safe_decode_text(name_node)

    obj: str | None = None
    if object_node := call_node.child_by_field_name(cs.TS_FIELD_OBJECT):
        match object_node.type:
            case cs.TS_THIS:
                obj = cs.TS_THIS
            case cs.TS_SUPER:
                obj = cs.TS_SUPER
            case cs.TS_IDENTIFIER | cs.TS_FIELD_ACCESS:
                obj = safe_decode_text(object_node)

    arguments = 0
    if args_node := call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS):
        arguments = sum(
            1 for child in args_node.children if child.type not in cs.DELIMITER_TOKENS
        )

    return JavaMethodCallInfo(name=name, object=obj, arguments=arguments)


def _has_main_method_modifiers(method_node: ASTNode) -> bool:
    has_public = False
    has_static = False

    for child in method_node.children:
        if child.type == cs.TS_MODIFIERS:
            for modifier_child in child.children:
                match modifier_child.type:
                    case cs.JAVA_MODIFIER_PUBLIC:
                        has_public = True
                    case cs.JAVA_MODIFIER_STATIC:
                        has_static = True

    return has_public and has_static


def _is_valid_main_formal_param(param_node: ASTNode) -> bool:
    type_node = param_node.child_by_field_name(cs.TS_FIELD_TYPE)
    if not type_node:
        return False

    type_text = safe_decode_text(type_node)
    if not type_text:
        return False

    return (
        cs.JAVA_MAIN_PARAM_ARRAY in type_text
        or cs.JAVA_MAIN_PARAM_VARARGS in type_text
        or type_text.endswith(cs.JAVA_MAIN_PARAM_ARRAY)
        or type_text.endswith(cs.JAVA_MAIN_PARAM_VARARGS)
    )


def _is_valid_main_spread_param(spread_node: ASTNode) -> bool:
    for subchild in spread_node.children:
        if subchild.type == cs.TS_TYPE_IDENTIFIER:
            type_text = safe_decode_text(subchild)
            if type_text == cs.JAVA_MAIN_PARAM_TYPE:
                return True
    return False


def _has_valid_main_parameter(method_node: ASTNode) -> bool:
    parameters_node = method_node.child_by_field_name(cs.TS_FIELD_PARAMETERS)
    if not parameters_node:
        return False

    param_count = 0
    valid_param = False

    for child in parameters_node.children:
        match child.type:
            case cs.TS_FORMAL_PARAMETER:
                param_count += 1
                if _is_valid_main_formal_param(child):
                    valid_param = True
            case cs.TS_SPREAD_PARAMETER:
                param_count += 1
                if _is_valid_main_spread_param(child):
                    valid_param = True

    return param_count == 1 and valid_param


def is_main_method(method_node: ASTNode) -> bool:
    if method_node.type != cs.TS_METHOD_DECLARATION:
        return False

    name_node = method_node.child_by_field_name(cs.TS_FIELD_NAME)
    if not name_node or safe_decode_text(name_node) != cs.JAVA_MAIN_METHOD_NAME:
        return False

    type_node = method_node.child_by_field_name(cs.TS_FIELD_TYPE)
    if not type_node or type_node.type != cs.TS_VOID_TYPE:
        return False

    if not _has_main_method_modifiers(method_node):
        return False

    return _has_valid_main_parameter(method_node)


def get_java_visibility(node: ASTNode) -> str:
    for child in node.children:
        match child.type:
            case cs.JAVA_VISIBILITY_PUBLIC:
                return cs.JAVA_VISIBILITY_PUBLIC
            case cs.JAVA_VISIBILITY_PROTECTED:
                return cs.JAVA_VISIBILITY_PROTECTED
            case cs.JAVA_VISIBILITY_PRIVATE:
                return cs.JAVA_VISIBILITY_PRIVATE

    return cs.JAVA_VISIBILITY_PACKAGE


def build_qualified_name(
    node: ASTNode,
    include_classes: bool = True,
    include_methods: bool = False,
) -> list[str]:
    path_parts: list[str] = []
    current = node.parent

    while current and current.type != cs.TS_PROGRAM:
        if current.type in cs.JAVA_CLASS_NODE_TYPES and include_classes:
            if name_node := current.child_by_field_name(cs.TS_FIELD_NAME):
                if class_name := safe_decode_text(name_node):
                    path_parts.append(class_name)
        elif current.type in cs.JAVA_METHOD_NODE_TYPES and include_methods:
            if name_node := current.child_by_field_name(cs.TS_FIELD_NAME):
                if method_name := safe_decode_text(name_node):
                    path_parts.append(method_name)

        current = current.parent

    path_parts.reverse()
    return path_parts


def extract_annotation_info(annotation_node: ASTNode) -> JavaAnnotationInfo:
    if annotation_node.type != cs.TS_ANNOTATION:
        return JavaAnnotationInfo(name=None, arguments=[])

    name: str | None = None
    if name_node := annotation_node.child_by_field_name(cs.TS_FIELD_NAME):
        name = safe_decode_text(name_node)

    arguments: list[str] = []
    if args_node := annotation_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS):
        for child in args_node.children:
            if child.type not in cs.DELIMITER_TOKENS:
                if arg_value := safe_decode_text(child):
                    arguments.append(arg_value)

    return JavaAnnotationInfo(name=name, arguments=arguments)


def find_package_start_index(parts: list[str]) -> int | None:
    for i, part in enumerate(parts):
        if part in cs.JAVA_JVM_LANGUAGES and i > 0:
            return i + 1

        if part == cs.JAVA_PATH_SRC and i + 1 < len(parts):
            next_part = parts[i + 1]

            if (
                next_part not in cs.JAVA_JVM_LANGUAGES
                and next_part not in cs.JAVA_SRC_FOLDERS
            ):
                return i + 1

            if _is_non_standard_java_src_layout(parts, i):
                return i + 1

    return None


def _is_non_standard_java_src_layout(parts: list[str], src_idx: int) -> bool:
    if src_idx + 2 >= len(parts):
        return False

    next_part = parts[src_idx + 1]
    part_after_next = parts[src_idx + 2]

    return (
        next_part in (cs.JAVA_PATH_MAIN, cs.JAVA_PATH_TEST)
        and part_after_next not in cs.JAVA_JVM_LANGUAGES
    )
