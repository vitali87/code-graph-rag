# (H) C/C++ tree-sitter node types, module markers, and operator maps.

from enum import StrEnum

from .ast_nodes import TS_ENUM_SPECIFIER, TS_STRUCT_SPECIFIER, TS_UNION_SPECIFIER


class CppNodeType(StrEnum):
    TRANSLATION_UNIT = "translation_unit"
    NAMESPACE_DEFINITION = "namespace_definition"
    NAMESPACE_IDENTIFIER = "namespace_identifier"
    IDENTIFIER = "identifier"
    EXPORT = "export"
    EXPORT_KEYWORD = "export_keyword"
    PRIMITIVE_TYPE = "primitive_type"
    DECLARATION = "declaration"
    FUNCTION_DEFINITION = "function_definition"
    TEMPLATE_DECLARATION = "template_declaration"
    CLASS_SPECIFIER = "class_specifier"
    FUNCTION_DECLARATOR = "function_declarator"
    POINTER_DECLARATOR = "pointer_declarator"
    REFERENCE_DECLARATOR = "reference_declarator"
    FIELD_DECLARATION = "field_declaration"
    FIELD_IDENTIFIER = "field_identifier"
    QUALIFIED_IDENTIFIER = "qualified_identifier"
    OPERATOR_NAME = "operator_name"
    DESTRUCTOR_NAME = "destructor_name"
    CONSTRUCTOR_OR_DESTRUCTOR_DEFINITION = "constructor_or_destructor_definition"
    CONSTRUCTOR_OR_DESTRUCTOR_DECLARATION = "constructor_or_destructor_declaration"
    INLINE_METHOD_DEFINITION = "inline_method_definition"
    OPERATOR_CAST_DEFINITION = "operator_cast_definition"
    TYPE_IDENTIFIER = "type_identifier"
    PARAMETER_LIST = "parameter_list"
    PARAMETER_DECLARATION = "parameter_declaration"
    OPTIONAL_PARAMETER_DECLARATION = "optional_parameter_declaration"
    INIT_DECLARATOR = "init_declarator"
    TEMPLATE_TYPE = "template_type"
    FIELD_EXPRESSION = "field_expression"
    COMPOUND_STATEMENT = "compound_statement"
    THIS = "this"
    TYPE_DEFINITION = "type_definition"
    ALIAS_DECLARATION = "alias_declaration"
    TYPE_DESCRIPTOR = "type_descriptor"


CPP_MODULE_EXTENSIONS = (".ixx", ".cppm", ".ccm", ".mxx")
CPP_MODULE_PATH_MARKERS = frozenset({"interfaces", "modules"})

# (H) C++ module declaration prefixes
CPP_EXPORT_MODULE_PREFIX = "export module "
CPP_MODULE_PREFIX = "module "
CPP_MODULE_PRIVATE_PREFIX = "module ;"
CPP_IMPL_SUFFIX = "_impl"

# (H) C++ module type values
CPP_MODULE_TYPE_INTERFACE = "interface"
CPP_MODULE_TYPE_IMPLEMENTATION = "implementation"

# (H) C++ export prefixes for class detection
CPP_EXPORT_CLASS_PREFIX = "export class "
CPP_EXPORT_STRUCT_PREFIX = "export struct "
CPP_EXPORT_UNION_PREFIX = "export union "
CPP_EXPORT_TEMPLATE_PREFIX = "export template"
CPP_EXPORT_PREFIXES = (
    CPP_EXPORT_CLASS_PREFIX,
    CPP_EXPORT_STRUCT_PREFIX,
    CPP_EXPORT_UNION_PREFIX,
    CPP_EXPORT_TEMPLATE_PREFIX,
)

# (H) C++ keywords for class detection
CPP_KEYWORD_CLASS = "class"
CPP_KEYWORD_STRUCT = "struct"
CPP_EXPORTED_CLASS_KEYWORDS = frozenset({CPP_KEYWORD_CLASS, CPP_KEYWORD_STRUCT})

# (H) A C/C++ class/struct/union tag with no body is a forward declaration
# (H) (`class Widget;`); it must not become its own node, or it collides with the
# (H) real definition's qn and fragments one class into several same-named nodes.
CPP_TYPE_SPECIFIER_NODE_TYPES = frozenset(
    {"class_specifier", "struct_specifier", "union_specifier"}
)

CPP_FALLBACK_OPERATOR = "operator_unknown"
CPP_FALLBACK_DESTRUCTOR = "~destructor"
CPP_OPERATOR_TEXT_PREFIX = "operator"
CPP_DESTRUCTOR_PREFIX = "~"

CPP_OPERATOR_SYMBOL_MAP: dict[str, str] = {
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

# (H) Tree-sitter C++ node types for language_spec
TS_CPP_FUNCTION_DEFINITION = "function_definition"
TS_CPP_DECLARATION = "declaration"
TS_CPP_FIELD_DECLARATION = "field_declaration"
TS_CPP_TEMPLATE_DECLARATION = "template_declaration"
TS_CPP_TEMPLATE_PARAMETER_LIST = "template_parameter_list"
# (H) The template TYPE-parameter declaration node types. A value/non-type param
# (H) (`parameter_declaration`, e.g. `int N` / `MyEnum E`) and a template-template param
# (H) are deliberately excluded: their type name is a concrete type, not a stand-in that
# (H) a call receiver could be instantiated as, so it must not enter the template-param set.
CPP_TYPE_PARAMETER_DECL_TYPES = frozenset(
    {
        "type_parameter_declaration",
        "optional_type_parameter_declaration",
        "variadic_type_parameter_declaration",
    }
)
TS_CPP_LAMBDA_EXPRESSION = "lambda_expression"
TS_CPP_TRANSLATION_UNIT = "translation_unit"
TS_CPP_LINKAGE_SPECIFICATION = "linkage_specification"
TS_CPP_CALL_EXPRESSION = "call_expression"
TS_CPP_FIELD_EXPRESSION = "field_expression"
TS_CPP_SUBSCRIPT_EXPRESSION = "subscript_expression"
TS_CPP_NEW_EXPRESSION = "new_expression"
TS_CPP_DELETE_EXPRESSION = "delete_expression"
TS_CPP_BINARY_EXPRESSION = "binary_expression"
TS_CPP_UNARY_EXPRESSION = "unary_expression"
TS_CPP_UPDATE_EXPRESSION = "update_expression"
TS_CPP_FUNCTION_DECLARATOR = "function_declarator"
# (H) Substring shared by C++ declarator node types (pointer_declarator,
# (H) reference_declarator, parenthesized_declarator, ...), used to unwrap a
# (H) parameter declarator down to its bound identifier.
CPP_DECLARATOR_SUFFIX = "declarator"

FIELD_OPERATOR = "operator"
FIELD_MACRO = "macro"

# (H) Derived node type tuples for class ingestion
CPP_CLASS_TYPES = (CppNodeType.CLASS_SPECIFIER, TS_STRUCT_SPECIFIER)
CPP_COMPOUND_TYPES = (*CPP_CLASS_TYPES, TS_UNION_SPECIFIER, TS_ENUM_SPECIFIER)
# (H) Node types that open their own variable scope; C++ local-variable inference must
# (H) not descend into them, or a name declared inside a lambda / nested function /
# (H) local class body would be attributed to the enclosing function's scope.
CPP_NESTED_SCOPE_NODE_TYPES = frozenset(
    (
        TS_CPP_FUNCTION_DEFINITION,
        TS_CPP_LAMBDA_EXPRESSION,
        *CPP_COMPOUND_TYPES,
    )
)
