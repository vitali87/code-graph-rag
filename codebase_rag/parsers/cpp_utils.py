from tree_sitter import Node

CPP_OPERATOR_SYMBOL_MAP = {
    "+": "operator_plus",
    "-": "operator_minus",
    "*": "operator_multiply",
    "/": "operator_divide",
    "%": "operator_modulo",
    "=": "operator_assign",
    "==": "operator_equal",
    "!=": "operator_not_equal",
    "<": "operator_less",
    ">": "operator_greater",
    "<=": "operator_less_equal",
    ">=": "operator_greater_equal",
    "&&": "operator_logical_and",
    "||": "operator_logical_or",
    "&": "operator_bitwise_and",
    "|": "operator_bitwise_or",
    "^": "operator_bitwise_xor",
    "~": "operator_bitwise_not",
    "!": "operator_not",
    "<<": "operator_left_shift",
    ">>": "operator_right_shift",
    "++": "operator_increment",
    "--": "operator_decrement",
    "+=": "operator_plus_assign",
    "-=": "operator_minus_assign",
    "*=": "operator_multiply_assign",
    "/=": "operator_divide_assign",
    "%=": "operator_modulo_assign",
    "&=": "operator_and_assign",
    "|=": "operator_or_assign",
    "^=": "operator_xor_assign",
    "<<=": "operator_left_shift_assign",
    ">>=": "operator_right_shift_assign",
    "[]": "operator_subscript",
    "()": "operator_call",
}


def convert_operator_symbol_to_name(symbol: str) -> str:
    """Convert C++ operator symbol to standardized name."""
    return CPP_OPERATOR_SYMBOL_MAP.get(symbol, f"operator_{symbol.replace(' ', '_')}")


def build_cpp_qualified_name(node: Node, module_qn: str, name: str) -> str:
    """Build qualified name for C++ entities, handling namespaces properly."""
    module_parts = module_qn.split(".")

    is_module_file = (
        len(module_parts) >= 3  # At least project.dir.filename
        and (
            "interfaces" in module_parts
            or "modules" in module_parts
            or any(
                part.endswith((".ixx", ".cppm", ".ccm", ".mxx"))
                for part in module_parts
            )
        )
    )

    if is_module_file:
        project_name = module_parts[0]  # First part is always project name
        filename = module_parts[-1]  # Last part is filename (without extension)

        return f"{project_name}.{filename}.{name}"
    else:
        path_parts = []
        current = node.parent

        while current and current.type != "translation_unit":
            if current.type == "namespace_definition":
                namespace_name = None
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    namespace_name = name_node.text.decode("utf8")
                else:
                    for child in current.children:
                        if (
                            child.type in ["namespace_identifier", "identifier"]
                            and child.text
                        ):
                            namespace_name = child.text.decode("utf8")
                            break
                if namespace_name:
                    path_parts.append(namespace_name)
            current = current.parent

        path_parts.reverse()

        if path_parts:
            return f"{module_qn}.{'.'.join(path_parts)}.{name}"
        else:
            return f"{module_qn}.{name}"


def is_cpp_exported(node: Node) -> bool:
    """Check if a C++ declaration is exported from a module."""
    current = node
    while current and current.parent:
        if current.parent:
            parent = current.parent
            found_export = False

            for child in parent.children:
                if child == current:
                    break  # We've reached our node
                if child.text:
                    child_text = child.text.decode("utf-8")
                    if child_text == "export" and child.type in [
                        "export",  # Direct export node type
                        "export_keyword",  # Export keyword node type
                        "identifier",  # Sometimes export appears as identifier
                        "primitive_type",  # Fallback for some grammars
                    ]:
                        found_export = True

            if found_export:
                return True

        if current.type in [
            "declaration",
            "function_definition",
            "template_declaration",
            "class_specifier",
            "translation_unit",
        ]:
            break
        current = current.parent

    return False


def extract_cpp_exported_class_name(class_node: Node) -> str | None:
    """Extract class name from misclassified exported class nodes (function_definition nodes that are actually classes)."""
    for child in class_node.children:
        if child.type == "identifier" and child.text:
            decoded_text: str = child.text.decode("utf-8")
            return decoded_text
    return None


def extract_operator_name(operator_node: Node) -> str:
    """Extract operator name from operator_name node using Tree-sitter AST."""
    if not operator_node.text:
        return "operator_unknown"

    operator_text = operator_node.text.decode("utf8").strip()

    if operator_text.startswith("operator"):
        symbol = operator_text[8:].strip()  # Remove "operator" prefix
        return convert_operator_symbol_to_name(symbol)

    return "operator_unknown"


def extract_destructor_name(destructor_node: Node) -> str:
    """Extract destructor name from destructor_name node."""
    for child in destructor_node.children:
        if child.type == "identifier" and child.text:
            class_name = child.text.decode("utf8")
            return f"~{class_name}"
    return "~destructor"


def _extract_name_from_function_definition(func_node: Node) -> str | None:
    """Extract function name from C++ function definition nodes."""

    def find_function_declarator(node: Node) -> str | None:
        """Recursively search for function_declarator in nested declarators."""
        if node.type == "function_declarator":
            return extract_cpp_function_name(node)

        for child in node.children:
            if child.type in [
                "pointer_declarator",
                "reference_declarator",
                "function_declarator",
            ]:
                result = find_function_declarator(child)
                if result:
                    return result
        return None

    return find_function_declarator(func_node)


def _extract_name_from_declaration(func_node: Node) -> str | None:
    """Extract function name from C++ declaration nodes."""
    for child in func_node.children:
        if child.type == "function_declarator":
            return extract_cpp_function_name(child)
    return None


def _extract_name_from_field_declaration(func_node: Node) -> str | None:
    """Extract function name from field_declaration nodes that are actually method declarations."""
    has_function_declarator = any(
        child.type == "function_declarator" for child in func_node.children
    )
    if not has_function_declarator:
        return None

    for child in func_node.children:
        if child.type == "function_declarator":
            declarator = child.child_by_field_name("declarator")
            if declarator and declarator.type == "field_identifier" and declarator.text:
                return declarator.text.decode("utf8") if declarator.text else None

            for grandchild in child.children:
                if grandchild.type == "field_identifier" and grandchild.text:
                    return grandchild.text.decode("utf8") if grandchild.text else None
    return None


def _extract_name_from_function_declarator(func_node: Node) -> str | None:
    """Extract function name from function_declarator nodes."""
    for child in func_node.children:
        if child.type in ["identifier", "field_identifier"] and child.text:
            return child.text.decode("utf8") if child.text else None
        elif child.type == "qualified_identifier":
            # (H) Handle out-of-class method definitions like Calculator::add
            # (H) or deeply nested like Outer::Inner::MyClass::method
            def find_rightmost_name(node: Node) -> str | None:
                last_name = None
                for qchild in node.children:
                    if qchild.type in ["identifier", "field_identifier"]:
                        last_name = qchild.text.decode("utf8") if qchild.text else None
                    elif qchild.type == "operator_name":
                        last_name = extract_operator_name(qchild)
                    elif qchild.type == "destructor_name":
                        last_name = extract_destructor_name(qchild)
                    elif qchild.type == "qualified_identifier":
                        nested = find_rightmost_name(qchild)
                        if nested:
                            last_name = nested
                return last_name

            return find_rightmost_name(child)
        elif child.type == "operator_name":
            return extract_operator_name(child)
        elif child.type == "destructor_name":
            return extract_destructor_name(child)
    return None


def _extract_name_from_template_declaration(func_node: Node) -> str | None:
    """Extract function name from template_declaration nodes."""
    for child in func_node.children:
        if child.type in [
            "function_definition",
            "function_declarator",
            "declaration",
        ]:
            return extract_cpp_function_name(child)
    return None


def extract_cpp_function_name(func_node: Node) -> str | None:
    """Extract function name from C++ function definitions and declarations."""
    if func_node.type in [
        "function_definition",
        "constructor_or_destructor_definition",
        "inline_method_definition",
        "operator_cast_definition",
    ]:
        return _extract_name_from_function_definition(func_node)

    elif func_node.type in [
        "declaration",
        "constructor_or_destructor_declaration",
    ]:
        return _extract_name_from_declaration(func_node)

    elif func_node.type == "field_declaration":
        name = _extract_name_from_declaration(func_node)
        if name:
            return name
        return _extract_name_from_field_declaration(func_node)

    elif func_node.type == "function_declarator":
        return _extract_name_from_function_declarator(func_node)

    elif func_node.type == "template_declaration":
        return _extract_name_from_template_declaration(func_node)

    return None
