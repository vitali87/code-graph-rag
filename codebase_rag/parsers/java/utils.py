from __future__ import annotations

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

    interfaces: list[str] = []
    modifiers: list[str] = []
    type_parameters: list[str] = []

    name: str | None = None
    if name_node := class_node.child_by_field_name(cs.TS_FIELD_NAME):
        name = safe_decode_text(name_node)

    superclass: str | None = None
    if superclass_node := class_node.child_by_field_name(cs.TS_FIELD_SUPERCLASS):
        match superclass_node.type:
            case cs.TS_TYPE_IDENTIFIER:
                superclass = safe_decode_text(superclass_node)
            case cs.TS_GENERIC_TYPE:
                for child in superclass_node.children:
                    if child.type == cs.TS_TYPE_IDENTIFIER:
                        superclass = safe_decode_text(child)
                        break

    if interfaces_node := class_node.child_by_field_name(cs.TS_FIELD_INTERFACES):
        for child in interfaces_node.children:
            if child.type == cs.TS_TYPE_LIST:
                for type_child in child.children:
                    interface_name = None
                    match type_child.type:
                        case cs.TS_TYPE_IDENTIFIER:
                            interface_name = safe_decode_text(type_child)
                        case cs.TS_GENERIC_TYPE:
                            for sub_child in type_child.children:
                                if sub_child.type == cs.TS_TYPE_IDENTIFIER:
                                    interface_name = safe_decode_text(sub_child)
                                    break
                    if interface_name:
                        interfaces.append(interface_name)

    if type_params_node := class_node.child_by_field_name(cs.TS_FIELD_TYPE_PARAMETERS):
        for child in type_params_node.children:
            if child.type == cs.TS_TYPE_PARAMETER:
                if param_name := safe_decode_text(
                    child.child_by_field_name(cs.TS_FIELD_NAME)
                ):
                    type_parameters.append(param_name)

    for child in class_node.children:
        if child.type == cs.TS_MODIFIERS:
            for modifier_child in child.children:
                if modifier_child.type in cs.JAVA_CLASS_MODIFIERS:
                    if modifier := safe_decode_text(modifier_child):
                        modifiers.append(modifier)

    return JavaClassInfo(
        name=name,
        type=class_node.type.replace(cs.JAVA_DECLARATION_SUFFIX, ""),
        superclass=superclass,
        interfaces=interfaces,
        modifiers=modifiers,
        type_parameters=type_parameters,
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


def is_main_method(method_node: ASTNode) -> bool:
    if method_node.type != cs.TS_METHOD_DECLARATION:
        return False

    name_node = method_node.child_by_field_name(cs.TS_FIELD_NAME)
    if not name_node or safe_decode_text(name_node) != cs.JAVA_MAIN_METHOD_NAME:
        return False

    type_node = method_node.child_by_field_name(cs.TS_FIELD_TYPE)
    if not type_node or type_node.type != cs.TS_VOID_TYPE:
        return False

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

    if not (has_public and has_static):
        return False

    parameters_node = method_node.child_by_field_name(cs.TS_FIELD_PARAMETERS)
    if not parameters_node:
        return False

    param_count = 0
    valid_param = False

    for child in parameters_node.children:
        match child.type:
            case cs.TS_FORMAL_PARAMETER:
                param_count += 1
                if type_node := child.child_by_field_name(cs.TS_FIELD_TYPE):
                    type_text = safe_decode_text(type_node)
                    if type_text and (
                        cs.JAVA_MAIN_PARAM_ARRAY in type_text
                        or cs.JAVA_MAIN_PARAM_VARARGS in type_text
                        or type_text.endswith(cs.JAVA_MAIN_PARAM_ARRAY)
                        or type_text.endswith(cs.JAVA_MAIN_PARAM_VARARGS)
                    ):
                        valid_param = True

            case cs.TS_SPREAD_PARAMETER:
                param_count += 1
                for subchild in child.children:
                    if subchild.type == cs.TS_TYPE_IDENTIFIER:
                        type_text = safe_decode_text(subchild)
                        if type_text == cs.JAVA_MAIN_PARAM_TYPE:
                            valid_param = True
                            break

    return param_count == 1 and valid_param


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
