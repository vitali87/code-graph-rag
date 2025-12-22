from typing import TypedDict

from tree_sitter import Node

from ..constants import (
    DELIMITER_TOKENS,
    JAVA_CLASS_MODIFIERS,
    JAVA_CLASS_NODE_TYPES,
    JAVA_DECLARATION_SUFFIX,
    JAVA_FIELD_MODIFIERS,
    JAVA_JVM_LANGUAGES,
    JAVA_MAIN_METHOD_NAME,
    JAVA_MAIN_PARAM_ARRAY,
    JAVA_MAIN_PARAM_TYPE,
    JAVA_MAIN_PARAM_VARARGS,
    JAVA_METHOD_MODIFIERS,
    JAVA_METHOD_NODE_TYPES,
    JAVA_MODIFIER_PUBLIC,
    JAVA_MODIFIER_STATIC,
    JAVA_PATH_MAIN,
    JAVA_PATH_SRC,
    JAVA_PATH_TEST,
    JAVA_SRC_FOLDERS,
    JAVA_TYPE_CONSTRUCTOR,
    JAVA_TYPE_METHOD,
    JAVA_VISIBILITY_PACKAGE,
    JAVA_VISIBILITY_PRIVATE,
    JAVA_VISIBILITY_PROTECTED,
    JAVA_VISIBILITY_PUBLIC,
    SEPARATOR_DOT,
    TS_ANNOTATION,
    TS_ASTERISK,
    TS_CONSTRUCTOR_DECLARATION,
    TS_FIELD_ACCESS,
    TS_FIELD_DECLARATION,
    TS_FORMAL_PARAMETER,
    TS_GENERIC_TYPE,
    TS_IDENTIFIER,
    TS_IMPORT_DECLARATION,
    TS_METHOD_DECLARATION,
    TS_METHOD_INVOCATION,
    TS_MODIFIERS,
    TS_PACKAGE_DECLARATION,
    TS_PROGRAM,
    TS_SCOPED_IDENTIFIER,
    TS_SPREAD_PARAMETER,
    TS_STATIC,
    TS_SUPER,
    TS_THIS,
    TS_TYPE_IDENTIFIER,
    TS_TYPE_LIST,
    TS_TYPE_PARAMETER,
    TS_VARIABLE_DECLARATOR,
    TS_VOID_TYPE,
)
from .utils import safe_decode_text


class JavaClassInfo(TypedDict):
    name: str | None
    type: str
    superclass: str | None
    interfaces: list[str]
    modifiers: list[str]
    type_parameters: list[str]


class JavaMethodInfo(TypedDict):
    name: str | None
    type: str
    return_type: str | None
    parameters: list[str]
    modifiers: list[str]
    type_parameters: list[str]
    annotations: list[str]


class JavaFieldInfo(TypedDict):
    name: str | None
    type: str | None
    modifiers: list[str]
    annotations: list[str]


class JavaAnnotationInfo(TypedDict):
    name: str | None
    arguments: list[str]


def extract_java_package_name(package_node: Node) -> str | None:
    if package_node.type != TS_PACKAGE_DECLARATION:
        return None

    for child in package_node.children:
        if child.type == TS_SCOPED_IDENTIFIER:
            return safe_decode_text(child)
        elif child.type == TS_IDENTIFIER:
            return safe_decode_text(child)

    return None


def extract_java_import_path(import_node: Node) -> dict[str, str]:
    if import_node.type != TS_IMPORT_DECLARATION:
        return {}

    imports: dict[str, str] = {}
    imported_path = None
    is_wildcard = False

    for child in import_node.children:
        if child.type == TS_STATIC:
            pass
        elif child.type == TS_SCOPED_IDENTIFIER:
            imported_path = safe_decode_text(child)
        elif child.type == TS_IDENTIFIER:
            imported_path = safe_decode_text(child)
        elif child.type == TS_ASTERISK:
            is_wildcard = True

    if not imported_path:
        return imports

    if is_wildcard:
        wildcard_key = f"*{imported_path}"
        imports[wildcard_key] = imported_path
    else:
        parts = imported_path.split(SEPARATOR_DOT)
        if parts:
            imported_name = parts[-1]
            imports[imported_name] = imported_path

    return imports


def extract_java_class_info(class_node: Node) -> JavaClassInfo:
    if class_node.type not in JAVA_CLASS_NODE_TYPES:
        return JavaClassInfo(
            name=None,
            type="",
            superclass=None,
            interfaces=[],
            modifiers=[],
            type_parameters=[],
        )

    info: JavaClassInfo = {
        "name": None,
        "type": class_node.type.replace(JAVA_DECLARATION_SUFFIX, ""),
        "superclass": None,
        "interfaces": [],
        "modifiers": [],
        "type_parameters": [],
    }

    name_node = class_node.child_by_field_name("name")
    if name_node:
        info["name"] = safe_decode_text(name_node)

    superclass_node = class_node.child_by_field_name("superclass")
    if superclass_node:
        if superclass_node.type == TS_TYPE_IDENTIFIER:
            info["superclass"] = safe_decode_text(superclass_node)
        elif superclass_node.type == TS_GENERIC_TYPE:
            for child in superclass_node.children:
                if child.type == TS_TYPE_IDENTIFIER:
                    info["superclass"] = safe_decode_text(child)
                    break

    interfaces_node = class_node.child_by_field_name("interfaces")
    if interfaces_node:
        for child in interfaces_node.children:
            if child.type == TS_TYPE_LIST:
                for type_child in child.children:
                    interface_name = None
                    if type_child.type == TS_TYPE_IDENTIFIER:
                        interface_name = safe_decode_text(type_child)
                    elif type_child.type == TS_GENERIC_TYPE:
                        for sub_child in type_child.children:
                            if sub_child.type == TS_TYPE_IDENTIFIER:
                                interface_name = safe_decode_text(sub_child)
                                break
                    if interface_name:
                        info["interfaces"].append(interface_name)

    type_params_node = class_node.child_by_field_name("type_parameters")
    if type_params_node:
        for child in type_params_node.children:
            if child.type == TS_TYPE_PARAMETER:
                param_name = safe_decode_text(child.child_by_field_name("name"))
                if param_name:
                    info["type_parameters"].append(param_name)

    for child in class_node.children:
        if child.type == TS_MODIFIERS:
            for modifier_child in child.children:
                if modifier_child.type in JAVA_CLASS_MODIFIERS:
                    modifier = safe_decode_text(modifier_child)
                    if modifier:
                        info["modifiers"].append(modifier)

    return info


def extract_java_method_info(method_node: Node) -> JavaMethodInfo:
    if method_node.type not in JAVA_METHOD_NODE_TYPES:
        return JavaMethodInfo(
            name=None,
            type="",
            return_type=None,
            parameters=[],
            modifiers=[],
            type_parameters=[],
            annotations=[],
        )

    info: JavaMethodInfo = {
        "name": None,
        "type": JAVA_TYPE_CONSTRUCTOR
        if method_node.type == TS_CONSTRUCTOR_DECLARATION
        else JAVA_TYPE_METHOD,
        "return_type": None,
        "parameters": [],
        "modifiers": [],
        "type_parameters": [],
        "annotations": [],
    }

    name_node = method_node.child_by_field_name("name")
    if name_node:
        info["name"] = safe_decode_text(name_node)

    if method_node.type == TS_METHOD_DECLARATION:
        type_node = method_node.child_by_field_name("type")
        if type_node:
            info["return_type"] = safe_decode_text(type_node)

    params_node = method_node.child_by_field_name("parameters")
    if params_node:
        for child in params_node.children:
            if child.type == TS_FORMAL_PARAMETER:
                param_type_node = child.child_by_field_name("type")
                if param_type_node:
                    param_type = safe_decode_text(param_type_node)
                    if param_type:
                        info["parameters"].append(param_type)
            elif child.type == TS_SPREAD_PARAMETER:
                for subchild in child.children:
                    if subchild.type == TS_TYPE_IDENTIFIER:
                        param_type_text = safe_decode_text(subchild)
                        if param_type_text:
                            param_type = param_type_text + "..."
                            info["parameters"].append(param_type)
                        break

    for child in method_node.children:
        if child.type == TS_MODIFIERS:
            for modifier_child in child.children:
                if modifier_child.type in JAVA_METHOD_MODIFIERS:
                    modifier = safe_decode_text(modifier_child)
                    if modifier:
                        info["modifiers"].append(modifier)
                elif modifier_child.type == TS_ANNOTATION:
                    annotation = safe_decode_text(modifier_child)
                    if annotation:
                        info["annotations"].append(annotation)

    return info


def extract_java_field_info(field_node: Node) -> JavaFieldInfo:
    if field_node.type != TS_FIELD_DECLARATION:
        return JavaFieldInfo(
            name=None,
            type=None,
            modifiers=[],
            annotations=[],
        )

    info: JavaFieldInfo = {
        "name": None,
        "type": None,
        "modifiers": [],
        "annotations": [],
    }

    type_node = field_node.child_by_field_name("type")
    if type_node:
        info["type"] = safe_decode_text(type_node)

    declarator_node = field_node.child_by_field_name("declarator")
    if declarator_node and declarator_node.type == TS_VARIABLE_DECLARATOR:
        name_node = declarator_node.child_by_field_name("name")
        if name_node:
            info["name"] = safe_decode_text(name_node)

    for child in field_node.children:
        if child.type == TS_MODIFIERS:
            for modifier_child in child.children:
                if modifier_child.type in JAVA_FIELD_MODIFIERS:
                    modifier = safe_decode_text(modifier_child)
                    if modifier:
                        info["modifiers"].append(modifier)
                elif modifier_child.type == TS_ANNOTATION:
                    annotation = safe_decode_text(modifier_child)
                    if annotation:
                        info["annotations"].append(annotation)

    return info


def extract_java_method_call_info(call_node: Node) -> dict[str, str | int | None]:
    if call_node.type != TS_METHOD_INVOCATION:
        return {}

    info: dict[str, str | int | None] = {"name": None, "object": None, "arguments": 0}

    name_node = call_node.child_by_field_name("name")
    if name_node:
        info["name"] = safe_decode_text(name_node)

    object_node = call_node.child_by_field_name("object")
    if object_node:
        if object_node.type == TS_IDENTIFIER:
            info["object"] = safe_decode_text(object_node)
        elif object_node.type == TS_THIS:
            info["object"] = TS_THIS
        elif object_node.type == TS_SUPER:
            info["object"] = TS_SUPER
        elif object_node.type == TS_FIELD_ACCESS:
            info["object"] = safe_decode_text(object_node)

    args_node = call_node.child_by_field_name("arguments")
    if args_node:
        argument_count = 0
        for child in args_node.children:
            if child.type not in DELIMITER_TOKENS:
                argument_count += 1
        info["arguments"] = argument_count

    return info


def is_java_main_method(method_node: Node) -> bool:
    if method_node.type != TS_METHOD_DECLARATION:
        return False

    name_node = method_node.child_by_field_name("name")
    if not name_node or safe_decode_text(name_node) != JAVA_MAIN_METHOD_NAME:
        return False

    type_node = method_node.child_by_field_name("type")
    if not type_node or type_node.type != TS_VOID_TYPE:
        return False

    has_public = False
    has_static = False

    for child in method_node.children:
        if child.type == TS_MODIFIERS:
            for modifier_child in child.children:
                if modifier_child.type == JAVA_MODIFIER_PUBLIC:
                    has_public = True
                elif modifier_child.type == JAVA_MODIFIER_STATIC:
                    has_static = True

    if not (has_public and has_static):
        return False

    parameters_node = method_node.child_by_field_name("parameters")
    if not parameters_node:
        return False

    param_count = 0
    valid_param = False

    for child in parameters_node.children:
        if child.type == TS_FORMAL_PARAMETER:
            param_count += 1

            type_node = child.child_by_field_name("type")
            if type_node:
                type_text = safe_decode_text(type_node)
                if type_text and (
                    JAVA_MAIN_PARAM_ARRAY in type_text
                    or JAVA_MAIN_PARAM_VARARGS in type_text
                    or type_text.endswith(JAVA_MAIN_PARAM_ARRAY)
                    or type_text.endswith(JAVA_MAIN_PARAM_VARARGS)
                ):
                    valid_param = True

        elif child.type == TS_SPREAD_PARAMETER:
            param_count += 1

            for subchild in child.children:
                if subchild.type == TS_TYPE_IDENTIFIER:
                    type_text = safe_decode_text(subchild)
                    if type_text == JAVA_MAIN_PARAM_TYPE:
                        valid_param = True
                        break

    return param_count == 1 and valid_param


def get_java_visibility(node: Node) -> str:
    for child in node.children:
        if child.type == JAVA_VISIBILITY_PUBLIC:
            return JAVA_VISIBILITY_PUBLIC
        elif child.type == JAVA_VISIBILITY_PROTECTED:
            return JAVA_VISIBILITY_PROTECTED
        elif child.type == JAVA_VISIBILITY_PRIVATE:
            return JAVA_VISIBILITY_PRIVATE

    return JAVA_VISIBILITY_PACKAGE


def build_java_qualified_name(
    node: Node,
    include_classes: bool = True,
    include_methods: bool = False,
) -> list[str]:
    path_parts = []
    current = node.parent

    while current and current.type != TS_PROGRAM:
        if current.type in JAVA_CLASS_NODE_TYPES and include_classes:
            name_node = current.child_by_field_name("name")
            if name_node:
                class_name = safe_decode_text(name_node)
                if class_name:
                    path_parts.append(class_name)
        elif current.type in JAVA_METHOD_NODE_TYPES and include_methods:
            name_node = current.child_by_field_name("name")
            if name_node:
                method_name = safe_decode_text(name_node)
                if method_name:
                    path_parts.append(method_name)

        current = current.parent

    path_parts.reverse()
    return path_parts


def extract_java_annotation_info(
    annotation_node: Node,
) -> JavaAnnotationInfo:
    if annotation_node.type != TS_ANNOTATION:
        return JavaAnnotationInfo(name=None, arguments=[])

    info: JavaAnnotationInfo = {"name": None, "arguments": []}

    name_node = annotation_node.child_by_field_name("name")
    if name_node:
        info["name"] = safe_decode_text(name_node)

    args_node = annotation_node.child_by_field_name("arguments")
    if args_node:
        for child in args_node.children:
            if child.type not in DELIMITER_TOKENS:
                arg_value = safe_decode_text(child)
                if arg_value:
                    info["arguments"].append(arg_value)

    return info


def find_java_package_start_index(parts: list[str]) -> int | None:
    for i, part in enumerate(parts):
        if part in JAVA_JVM_LANGUAGES and i > 0:
            return i + 1

        if part == JAVA_PATH_SRC and i + 1 < len(parts):
            next_part = parts[i + 1]

            if (
                next_part not in JAVA_JVM_LANGUAGES
                and next_part not in JAVA_SRC_FOLDERS
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
        next_part in (JAVA_PATH_MAIN, JAVA_PATH_TEST)
        and part_after_next not in JAVA_JVM_LANGUAGES
    )
