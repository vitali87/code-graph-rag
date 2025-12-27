from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text, safe_decode_with_fallback


def convert_operator_symbol_to_name(symbol: str) -> str:
    return cs.CPP_OPERATOR_SYMBOL_MAP.get(
        symbol,
        f"{cs.OPERATOR_PREFIX}{cs.CHAR_UNDERSCORE}{symbol.replace(cs.CHAR_SPACE, cs.CHAR_UNDERSCORE)}",
    )


def build_qualified_name(node: Node, module_qn: str, name: str) -> str:
    module_parts = module_qn.split(cs.SEPARATOR_DOT)

    is_module_file = len(module_parts) >= 3 and (
        bool(cs.CPP_MODULE_PATH_MARKERS & set(module_parts))
        or any(part.endswith(cs.CPP_MODULE_EXTENSIONS) for part in module_parts)
    )

    if is_module_file:
        project_name = module_parts[0]
        filename = module_parts[-1]

        return cs.SEPARATOR_DOT.join([project_name, filename, name])

    path_parts: list[str] = []
    current = node.parent

    while current and current.type != cs.CppNodeType.TRANSLATION_UNIT:
        if current.type == cs.CppNodeType.NAMESPACE_DEFINITION:
            namespace_name = None
            name_node = current.child_by_field_name(cs.KEY_NAME)
            if name_node and name_node.text:
                namespace_name = safe_decode_text(name_node)
            else:
                for child in current.children:
                    if (
                        child.type
                        in (
                            cs.CppNodeType.NAMESPACE_IDENTIFIER,
                            cs.CppNodeType.IDENTIFIER,
                        )
                        and child.text
                    ):
                        namespace_name = safe_decode_text(child)
                        break
            if namespace_name:
                path_parts.append(namespace_name)
        current = current.parent

    path_parts.reverse()

    if path_parts:
        return cs.SEPARATOR_DOT.join([module_qn, *path_parts, name])
    return cs.SEPARATOR_DOT.join([module_qn, name])


def is_exported(node: Node) -> bool:
    current = node
    while current and current.parent:
        parent = current.parent
        found_export = False

        for child in parent.children:
            if child == current:
                break
            if child.text:
                child_text = safe_decode_text(child)
                if child_text == cs.CppNodeType.EXPORT and child.type in (
                    cs.CppNodeType.EXPORT,
                    cs.CppNodeType.EXPORT_KEYWORD,
                    cs.CppNodeType.IDENTIFIER,
                    cs.CppNodeType.PRIMITIVE_TYPE,
                ):
                    found_export = True

        if found_export:
            return True

        if current.type in (
            cs.CppNodeType.DECLARATION,
            cs.CppNodeType.FUNCTION_DEFINITION,
            cs.CppNodeType.TEMPLATE_DECLARATION,
            cs.CppNodeType.CLASS_SPECIFIER,
            cs.CppNodeType.TRANSLATION_UNIT,
        ):
            break
        current = current.parent

    return False


def extract_exported_class_name(class_node: Node) -> str | None:
    return next(
        (
            safe_decode_text(child)
            for child in class_node.children
            if child.type == cs.CppNodeType.IDENTIFIER and child.text
        ),
        None,
    )


def extract_operator_name(operator_node: Node) -> str:
    if not operator_node.text:
        return cs.CPP_FALLBACK_OPERATOR

    operator_text = safe_decode_with_fallback(operator_node).strip()

    if operator_text.startswith(cs.CPP_OPERATOR_TEXT_PREFIX):
        symbol = operator_text[len(cs.CPP_OPERATOR_TEXT_PREFIX) :].strip()
        return convert_operator_symbol_to_name(symbol)

    return cs.CPP_FALLBACK_OPERATOR


def extract_destructor_name(destructor_node: Node) -> str:
    for child in destructor_node.children:
        if child.type == cs.CppNodeType.IDENTIFIER and child.text:
            class_name = safe_decode_text(child)
            return f"{cs.CPP_DESTRUCTOR_PREFIX}{class_name}"
    return cs.CPP_FALLBACK_DESTRUCTOR


def _extract_name_from_function_definition(func_node: Node) -> str | None:
    def find_function_declarator(node: Node) -> str | None:
        if node.type == cs.CppNodeType.FUNCTION_DECLARATOR:
            return extract_function_name(node)

        for child in node.children:
            if child.type in (
                cs.CppNodeType.POINTER_DECLARATOR,
                cs.CppNodeType.REFERENCE_DECLARATOR,
                cs.CppNodeType.FUNCTION_DECLARATOR,
            ):
                result = find_function_declarator(child)
                if result:
                    return result
        return None

    return find_function_declarator(func_node)


def _extract_name_from_declaration(func_node: Node) -> str | None:
    return next(
        (
            extract_function_name(child)
            for child in func_node.children
            if child.type == cs.CppNodeType.FUNCTION_DECLARATOR
        ),
        None,
    )


def _extract_name_from_field_declaration(func_node: Node) -> str | None:
    has_function_declarator = any(
        child.type == cs.CppNodeType.FUNCTION_DECLARATOR for child in func_node.children
    )
    if not has_function_declarator:
        return None

    for child in func_node.children:
        if child.type == cs.CppNodeType.FUNCTION_DECLARATOR:
            declarator = child.child_by_field_name(cs.FIELD_DECLARATOR)
            if (
                declarator
                and declarator.type == cs.CppNodeType.FIELD_IDENTIFIER
                and declarator.text
            ):
                return safe_decode_text(declarator)

            for grandchild in child.children:
                if (
                    grandchild.type == cs.CppNodeType.FIELD_IDENTIFIER
                    and grandchild.text
                ):
                    return safe_decode_text(grandchild)
    return None


def _extract_name_from_function_declarator(func_node: Node) -> str | None:
    for child in func_node.children:
        if (
            child.type
            in (
                cs.CppNodeType.IDENTIFIER,
                cs.CppNodeType.FIELD_IDENTIFIER,
            )
            and child.text
        ):
            return safe_decode_text(child)
        if child.type == cs.CppNodeType.QUALIFIED_IDENTIFIER:
            return _find_rightmost_name(child)
        if child.type == cs.CppNodeType.OPERATOR_NAME:
            return extract_operator_name(child)
        if child.type == cs.CppNodeType.DESTRUCTOR_NAME:
            return extract_destructor_name(child)
    return None


def _find_rightmost_name(node: Node) -> str | None:
    # (H) Handle out-of-class method definitions like Calculator::add
    # (H) or deeply nested like Outer::Inner::MyClass::method
    last_name = None
    for qchild in node.children:
        match qchild.type:
            case cs.CppNodeType.IDENTIFIER | cs.CppNodeType.FIELD_IDENTIFIER:
                last_name = safe_decode_text(qchild)
            case cs.CppNodeType.OPERATOR_NAME:
                last_name = extract_operator_name(qchild)
            case cs.CppNodeType.DESTRUCTOR_NAME:
                last_name = extract_destructor_name(qchild)
            case cs.CppNodeType.QUALIFIED_IDENTIFIER:
                if nested := _find_rightmost_name(qchild):
                    last_name = nested
    return last_name


def _extract_name_from_template_declaration(func_node: Node) -> str | None:
    return next(
        (
            extract_function_name(child)
            for child in func_node.children
            if child.type
            in (
                cs.CppNodeType.FUNCTION_DEFINITION,
                cs.CppNodeType.FUNCTION_DECLARATOR,
                cs.CppNodeType.DECLARATION,
            )
        ),
        None,
    )


def extract_function_name(func_node: Node) -> str | None:
    match func_node.type:
        case (
            cs.CppNodeType.FUNCTION_DEFINITION
            | cs.CppNodeType.CONSTRUCTOR_OR_DESTRUCTOR_DEFINITION
            | cs.CppNodeType.INLINE_METHOD_DEFINITION
            | cs.CppNodeType.OPERATOR_CAST_DEFINITION
        ):
            return _extract_name_from_function_definition(func_node)
        case (
            cs.CppNodeType.DECLARATION
            | cs.CppNodeType.CONSTRUCTOR_OR_DESTRUCTOR_DECLARATION
        ):
            return _extract_name_from_declaration(func_node)
        case cs.CppNodeType.FIELD_DECLARATION:
            name = _extract_name_from_declaration(func_node)
            return name or _extract_name_from_field_declaration(func_node)
        case cs.CppNodeType.FUNCTION_DECLARATOR:
            return _extract_name_from_function_declarator(func_node)
        case cs.CppNodeType.TEMPLATE_DECLARATION:
            return _extract_name_from_template_declaration(func_node)
        case _:
            return None
