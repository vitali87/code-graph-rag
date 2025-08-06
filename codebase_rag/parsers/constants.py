"""
Shared constants for C++ and other language parsing.

This module contains operator constants and mappings that are shared
across different parser components to reduce duplication and improve
maintainability.
"""

# C++ Binary Operators
CPP_BINARY_OPERATORS = {
    "+",
    "-",
    "*",
    "/",
    "%",
    "==",
    "!=",
    "<",
    ">",
    "<=",
    ">=",
    "&&",
    "||",
    "&",
    "|",
    "^",
    "<<",
    ">>",
    "=",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "&=",
    "|=",
    "^=",
    "<<=",
    ">>=",
    "[]",
}

# C++ Unary Operators
CPP_UNARY_OPERATORS = {
    "++",
    "--",
    "+",
    "-",
    "*",
    "&",
    "!",
    "~",
}

# C++ Operator to Readable Name Mapping
CPP_OPERATOR_NAME_MAP = {
    "+": "operator_plus",
    "-": "operator_minus",
    "*": "operator_multiply",
    "/": "operator_divide",
    "=": "operator_assign",
    "==": "operator_equal",
    "!=": "operator_not_equal",
    "<": "operator_less",
    ">": "operator_greater",
    "<=": "operator_less_equal",
    ">=": "operator_greater_equal",
    "[]": "operator_subscript",
    "()": "operator_call",
    "++": "operator_increment",
    "--": "operator_decrement",
    "%": "operator_modulo",
    "&&": "operator_logical_and",
    "||": "operator_logical_or",
    "&": "operator_bitwise_and",
    "|": "operator_bitwise_or",
    "^": "operator_bitwise_xor",
    "<<": "operator_left_shift",
    ">>": "operator_right_shift",
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
    "!": "operator_not",
    "~": "operator_bitwise_not",
}


def get_operator_name(operator_text: str) -> str:
    """
    Convert operator text to a readable operator name.

    Args:
        operator_text: The raw operator text (e.g., "++", "==", "operator+", "operator==")

    Returns:
        Readable operator name (e.g., "operator_increment", "operator_equal")
    """
    stripped = operator_text.strip()

    # Handle cases where the text already includes "operator" prefix
    if stripped.startswith("operator"):
        # Extract just the symbol part after "operator"
        symbol = stripped[8:]  # Remove "operator" prefix
        return CPP_OPERATOR_NAME_MAP.get(symbol, f"operator_{symbol.replace(' ', '_')}")

    # Handle cases where it's just the symbol
    return CPP_OPERATOR_NAME_MAP.get(stripped, f"operator_{stripped.replace(' ', '_')}")
