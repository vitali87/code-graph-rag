"""
C++ parsing utilities shared between different processors.

This module contains C++ specific helper functions that were previously
duplicated across different processor classes. By centralizing these
utilities, we improve modularity and reduce coupling between processors.
"""

from tree_sitter import Node

# Centralized C++ operator symbol to name mapping
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
    # For C++20 module files, use module-based naming instead of traditional namespace-based naming
    # Extract the file path from module_qn to check if this is a module file
    module_parts = module_qn.split(".")

    # Check if this is a module interface file (.ixx, .cppm, .ccm)
    is_module_file = False
    if len(module_parts) >= 3:  # At least project.dir.filename
        module_parts[-1]  # Last part should be the filename
        # Check parent directory or file extension patterns that suggest module files
        if len(module_parts) >= 3 and (
            "interfaces" in module_parts or "modules" in module_parts
        ):
            is_module_file = True

    if is_module_file and len(module_parts) >= 3:
        # For module files, use simplified naming: project.filename.classname
        project_name = module_parts[0]  # First part is always project name
        filename = module_parts[-1]  # Last part is filename (without extension)

        # Skip namespace parts for module files - classes/functions are qualified directly by the module
        return f"{project_name}.{filename}.{name}"
    else:
        # Traditional C++ namespace-based naming for regular files
        path_parts = []
        current = node.parent

        # Walk up the tree to find namespaces
        while current and current.type != "translation_unit":
            if current.type == "namespace_definition":
                # Get namespace name from the 'name' field
                namespace_name = None
                # First try to get the name field directly
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    namespace_name = name_node.text.decode("utf8")
                else:
                    # Fallback: look for namespace_identifier or identifier children
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

        # Reverse to get correct namespace order (outermost first)
        path_parts.reverse()

        if path_parts:
            return f"{module_qn}.{'.'.join(path_parts)}.{name}"
        else:
            return f"{module_qn}.{name}"


def is_cpp_exported(node: Node) -> bool:
    """Check if a C++ declaration is exported from a module."""
    # First check text-based export detection (more reliable for C++20 modules)
    current = node
    while current and current.parent:
        # Get the full text of the current node
        if current.text:
            node_text = current.text.decode("utf-8").strip()
            # Check if this node's text starts with "export "
            if node_text.startswith("export "):
                return True

            # Check for export at the beginning of lines
            lines = node_text.split("\n")
            for line in lines:
                line = line.strip()
                if line.startswith("export "):
                    return True

        # Check siblings to the left for export keyword
        if current.parent:
            parent = current.parent
            found_export = False

            for child in parent.children:
                if child == current:
                    break  # We've reached our node
                # Check for export keyword - could be various node types depending on Tree-sitter grammar
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

        # Move up the tree, but don't go too far
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
    # For misclassified nodes like "export class Calculator", the identifier node contains the class name
    for child in class_node.children:
        if child.type == "identifier" and child.text:
            # This should be the class name
            decoded_text: str = child.text.decode("utf-8")
            return decoded_text
    return None


def extract_operator_name(operator_node: Node) -> str:
    """Extract operator name from operator_name node using Tree-sitter AST."""
    if not operator_node.text:
        return "operator_unknown"

    operator_text = operator_node.text.decode("utf8").strip()

    # Tree-sitter provides operator_name nodes with full text like "operator+"
    # Extract just the symbol part after "operator"
    if operator_text.startswith("operator"):
        symbol = operator_text[8:].strip()  # Remove "operator" prefix
        return convert_operator_symbol_to_name(symbol)

    return "operator_unknown"


def extract_destructor_name(destructor_node: Node) -> str:
    """Extract destructor name from destructor_name node."""
    # Destructor name is like ~ClassName, return just the class name
    for child in destructor_node.children:
        if child.type == "identifier" and child.text:
            class_name = child.text.decode("utf8")
            return f"~{class_name}"
    return "~destructor"


def _extract_name_from_function_definition(func_node: Node) -> str | None:
    """Extract function name from C++ function definition nodes."""
    # Look for function_declarator within these definitions
    for child in func_node.children:
        if child.type == "function_declarator":
            return extract_cpp_function_name(child)
    return None


def _extract_name_from_declaration(func_node: Node) -> str | None:
    """Extract function name from C++ declaration nodes."""
    # Handle method declarations - look for function_declarator
    for child in func_node.children:
        if child.type == "function_declarator":
            return extract_cpp_function_name(child)
    return None


def _extract_name_from_field_declaration(func_node: Node) -> str | None:
    """Extract function name from field_declaration nodes that are actually method declarations."""
    # Check if this field_declaration contains a function_declarator
    # This happens for method declarations like: void methodName() const;
    has_function_declarator = any(
        child.type == "function_declarator" for child in func_node.children
    )
    if not has_function_declarator:
        return None

    # This is a method declaration, extract name from function_declarator
    for child in func_node.children:
        if child.type == "function_declarator":
            # Look for the declarator inside the function_declarator
            declarator = child.child_by_field_name("declarator")
            if declarator and declarator.type == "field_identifier" and declarator.text:
                return declarator.text.decode("utf8") if declarator.text else None

            # Fallback: look for field_identifier directly
            for grandchild in child.children:
                if grandchild.type == "field_identifier" and grandchild.text:
                    return grandchild.text.decode("utf8") if grandchild.text else None
    return None


def _extract_name_from_function_declarator(func_node: Node) -> str | None:
    """Extract function name from function_declarator nodes."""
    # Look for identifier, field_identifier, destructor_name, or operator name
    for child in func_node.children:
        if child.type in ["identifier", "field_identifier"] and child.text:
            return child.text.decode("utf8") if child.text else None
        elif child.type == "operator_name":
            # Handle operator overloading
            return extract_operator_name(child)
        elif child.type == "destructor_name":
            # Handle destructor names like ~ClassName
            return extract_destructor_name(child)
    return None


def _extract_name_from_template_declaration(func_node: Node) -> str | None:
    """Extract function name from template_declaration nodes."""
    # For template functions, look inside the template
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
    # Dispatch to appropriate helper function based on node type
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
        # First try standard declaration extraction
        name = _extract_name_from_declaration(func_node)
        if name:
            return name
        # Then try special field_declaration handling
        return _extract_name_from_field_declaration(func_node)

    elif func_node.type == "function_declarator":
        return _extract_name_from_function_declarator(func_node)

    elif func_node.type == "template_declaration":
        return _extract_name_from_template_declaration(func_node)

    return None
