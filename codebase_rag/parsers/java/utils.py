from typing import TypedDict

from ...constants import (
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
from ...types_defs import ASTNode
from ..utils import safe_decode_text


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


class _MethodModifiersAndAnnotations:
    modifiers: list[str]
    annotations: list[str]

    def __init__(self) -> None:
        self.modifiers = []
        self.annotations = []


def extract_package_name(package_node: ASTNode) -> str | None:
    if package_node.type != TS_PACKAGE_DECLARATION:
        return None

    return next(
        (
            safe_decode_text(child)
            for child in package_node.children
            if child.type in [TS_SCOPED_IDENTIFIER, TS_IDENTIFIER]
        ),
        None,
    )


def extract_import_path(import_node: ASTNode) -> dict[str, str]:
    if import_node.type != TS_IMPORT_DECLARATION:
        return {}

    imports: dict[str, str] = {}
    imported_path = None
    is_wildcard = False

    for child in import_node.children:
        if child.type == TS_STATIC:
            pass
        elif child.type in [TS_SCOPED_IDENTIFIER, TS_IDENTIFIER]:
            imported_path = safe_decode_text(child)
        elif child.type == TS_ASTERISK:
            is_wildcard = True

    if not imported_path:
        return imports

    if is_wildcard:
        wildcard_key = f"*{imported_path}"
        imports[wildcard_key] = imported_path
    elif parts := imported_path.split(SEPARATOR_DOT):
        imported_name = parts[-1]
        imports[imported_name] = imported_path

    return imports


def extract_class_info(class_node: ASTNode) -> JavaClassInfo:
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

    if name_node := class_node.child_by_field_name("name"):
        info["name"] = safe_decode_text(name_node)

    if superclass_node := class_node.child_by_field_name("superclass"):
        if superclass_node.type == TS_TYPE_IDENTIFIER:
            info["superclass"] = safe_decode_text(superclass_node)
        elif superclass_node.type == TS_GENERIC_TYPE:
            for child in superclass_node.children:
                if child.type == TS_TYPE_IDENTIFIER:
                    info["superclass"] = safe_decode_text(child)
                    break

    if interfaces_node := class_node.child_by_field_name("interfaces"):
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

    if type_params_node := class_node.child_by_field_name("type_parameters"):
        for child in type_params_node.children:
            if child.type == TS_TYPE_PARAMETER:
                if param_name := safe_decode_text(child.child_by_field_name("name")):
                    info["type_parameters"].append(param_name)

    for child in class_node.children:
        if child.type == TS_MODIFIERS:
            for modifier_child in child.children:
                if modifier_child.type in JAVA_CLASS_MODIFIERS:
                    if modifier := safe_decode_text(modifier_child):
                        info["modifiers"].append(modifier)

    return info


def _get_method_type(method_node: ASTNode) -> str:
    if method_node.type == TS_CONSTRUCTOR_DECLARATION:
        return JAVA_TYPE_CONSTRUCTOR
    return JAVA_TYPE_METHOD


def _extract_method_return_type(method_node: ASTNode) -> str | None:
    if method_node.type != TS_METHOD_DECLARATION:
        return None
    if type_node := method_node.child_by_field_name("type"):
        return safe_decode_text(type_node)
    return None


def _extract_formal_param_type(param_node: ASTNode) -> str | None:
    if param_type_node := param_node.child_by_field_name("type"):
        return safe_decode_text(param_type_node)
    return None


def _extract_spread_param_type(spread_node: ASTNode) -> str | None:
    for subchild in spread_node.children:
        if subchild.type == TS_TYPE_IDENTIFIER:
            if param_type_text := safe_decode_text(subchild):
                return f"{param_type_text}..."
    return None


def _extract_method_parameters(method_node: ASTNode) -> list[str]:
    params_node = method_node.child_by_field_name("parameters")
    if not params_node:
        return []

    parameters: list[str] = []
    for child in params_node.children:
        param_type: str | None = None
        if child.type == TS_FORMAL_PARAMETER:
            param_type = _extract_formal_param_type(child)
        elif child.type == TS_SPREAD_PARAMETER:
            param_type = _extract_spread_param_type(child)
        if param_type:
            parameters.append(param_type)
    return parameters


def _extract_modifiers_and_annotations(
    method_node: ASTNode,
) -> _MethodModifiersAndAnnotations:
    result = _MethodModifiersAndAnnotations()
    for child in method_node.children:
        if child.type != TS_MODIFIERS:
            continue
        for modifier_child in child.children:
            if modifier_child.type in JAVA_METHOD_MODIFIERS:
                if modifier := safe_decode_text(modifier_child):
                    result.modifiers.append(modifier)
            elif modifier_child.type == TS_ANNOTATION:
                if annotation := safe_decode_text(modifier_child):
                    result.annotations.append(annotation)
    return result


def extract_method_info(method_node: ASTNode) -> JavaMethodInfo:
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

    mods_and_annots = _extract_modifiers_and_annotations(method_node)

    return JavaMethodInfo(
        name=safe_decode_text(method_node.child_by_field_name("name")),
        type=_get_method_type(method_node),
        return_type=_extract_method_return_type(method_node),
        parameters=_extract_method_parameters(method_node),
        modifiers=mods_and_annots.modifiers,
        type_parameters=[],
        annotations=mods_and_annots.annotations,
    )


def extract_field_info(field_node: ASTNode) -> JavaFieldInfo:
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

    if type_node := field_node.child_by_field_name("type"):
        info["type"] = safe_decode_text(type_node)

    declarator_node = field_node.child_by_field_name("declarator")
    if declarator_node and declarator_node.type == TS_VARIABLE_DECLARATOR:
        if name_node := declarator_node.child_by_field_name("name"):
            info["name"] = safe_decode_text(name_node)

    for child in field_node.children:
        if child.type == TS_MODIFIERS:
            for modifier_child in child.children:
                if modifier_child.type in JAVA_FIELD_MODIFIERS:
                    if modifier := safe_decode_text(modifier_child):
                        info["modifiers"].append(modifier)
                elif modifier_child.type == TS_ANNOTATION:
                    if annotation := safe_decode_text(modifier_child):
                        info["annotations"].append(annotation)

    return info


def extract_method_call_info(
    call_node: ASTNode,
) -> dict[str, str | int | None]:
    if call_node.type != TS_METHOD_INVOCATION:
        return {}

    info: dict[str, str | int | None] = {"name": None, "object": None, "arguments": 0}

    name_node = call_node.child_by_field_name("name")
    if name_node:
        info["name"] = safe_decode_text(name_node)

    if object_node := call_node.child_by_field_name("object"):
        if (
            object_node.type == TS_IDENTIFIER
            or object_node.type != TS_THIS
            and object_node.type != TS_SUPER
            and object_node.type == TS_FIELD_ACCESS
        ):
            info["object"] = safe_decode_text(object_node)
        elif object_node.type == TS_THIS:
            info["object"] = TS_THIS
        elif object_node.type == TS_SUPER:
            info["object"] = TS_SUPER
    if args_node := call_node.child_by_field_name("arguments"):
        argument_count = sum(
            1 for child in args_node.children if child.type not in DELIMITER_TOKENS
        )
        info["arguments"] = argument_count

    return info


def is_main_method(method_node: ASTNode) -> bool:
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

            if type_node := child.child_by_field_name("type"):
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


def get_java_visibility(node: ASTNode) -> str:
    for child in node.children:
        if child.type == JAVA_VISIBILITY_PUBLIC:
            return JAVA_VISIBILITY_PUBLIC
        elif child.type == JAVA_VISIBILITY_PROTECTED:
            return JAVA_VISIBILITY_PROTECTED
        elif child.type == JAVA_VISIBILITY_PRIVATE:
            return JAVA_VISIBILITY_PRIVATE

    return JAVA_VISIBILITY_PACKAGE


def build_qualified_name(
    node: ASTNode,
    include_classes: bool = True,
    include_methods: bool = False,
) -> list[str]:
    path_parts = []
    current = node.parent

    while current and current.type != TS_PROGRAM:
        if current.type in JAVA_CLASS_NODE_TYPES and include_classes:
            if name_node := current.child_by_field_name("name"):
                if class_name := safe_decode_text(name_node):
                    path_parts.append(class_name)
        elif current.type in JAVA_METHOD_NODE_TYPES and include_methods:
            if name_node := current.child_by_field_name("name"):
                if method_name := safe_decode_text(name_node):
                    path_parts.append(method_name)

        current = current.parent

    path_parts.reverse()
    return path_parts


def extract_annotation_info(
    annotation_node: ASTNode,
) -> JavaAnnotationInfo:
    if annotation_node.type != TS_ANNOTATION:
        return JavaAnnotationInfo(name=None, arguments=[])

    info: JavaAnnotationInfo = {"name": None, "arguments": []}

    if name_node := annotation_node.child_by_field_name("name"):
        info["name"] = safe_decode_text(name_node)

    if args_node := annotation_node.child_by_field_name("arguments"):
        for child in args_node.children:
            if child.type not in DELIMITER_TOKENS:
                if arg_value := safe_decode_text(child):
                    info["arguments"].append(arg_value)

    return info


def find_package_start_index(parts: list[str]) -> int | None:
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
