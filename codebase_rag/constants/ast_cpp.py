# C/C++ tree-sitter node types, module markers, and operator maps.

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
    VARIADIC_PARAMETER = "variadic_parameter"
    POINTER_DECLARATOR = "pointer_declarator"
    ARRAY_DECLARATOR = "array_declarator"
    ABSTRACT_POINTER_DECLARATOR = "abstract_pointer_declarator"
    ABSTRACT_ARRAY_DECLARATOR = "abstract_array_declarator"
    FIELD_DECLARATION_LIST = "field_declaration_list"
    REFERENCE_DECLARATOR = "reference_declarator"
    # An attribute MACRO before a definition (`JSON_HEDLEY_NON_NULL(3)
    # bool sax_parse(...)`) parses as a parenthesized_declarator wrapping
    # an ERROR plus the real function_declarator; the name walk descends it.
    PARENTHESIZED_DECLARATOR = "parenthesized_declarator"
    FIELD_DECLARATION = "field_declaration"
    FIELD_IDENTIFIER = "field_identifier"
    FIELD_INITIALIZER_LIST = "field_initializer_list"
    FIELD_INITIALIZER = "field_initializer"
    TEMPLATE_METHOD = "template_method"
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
    # `Args&&... args`: a template parameter pack, one variadic slot whose
    # name sits inside the variadic_declarator.
    VARIADIC_PARAMETER_DECLARATION = "variadic_parameter_declaration"
    VARIADIC_DECLARATOR = "variadic_declarator"
    INIT_DECLARATOR = "init_declarator"
    TEMPLATE_TYPE = "template_type"
    FIELD_EXPRESSION = "field_expression"
    COMPOUND_STATEMENT = "compound_statement"
    THIS = "this"
    TYPE_DEFINITION = "type_definition"
    ALIAS_DECLARATION = "alias_declaration"
    TYPE_DESCRIPTOR = "type_descriptor"


CPP_MODULE_PATH_MARKERS = frozenset({"interfaces", "modules"})

# C++ module declaration prefixes
CPP_EXPORT_MODULE_PREFIX = "export module "
CPP_MODULE_PREFIX = "module "
CPP_MODULE_PRIVATE_PREFIX = "module ;"
CPP_IMPL_SUFFIX = "_impl"

# C++ module type values
CPP_MODULE_TYPE_INTERFACE = "interface"
CPP_MODULE_TYPE_IMPLEMENTATION = "implementation"

# C++ export prefixes for class detection
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

# C++ keywords for class detection
CPP_KEYWORD_CLASS = "class"
CPP_KEYWORD_STRUCT = "struct"
# `static` storage on a declaration: internal linkage, TU-local symbol.
CPP_KEYWORD_STATIC = "static"
TS_CPP_STORAGE_CLASS_SPECIFIER = "storage_class_specifier"
TS_CPP_TYPE_QUALIFIER = "type_qualifier"
CPP_EXPORTED_CLASS_KEYWORDS = frozenset({CPP_KEYWORD_CLASS, CPP_KEYWORD_STRUCT})

# A C/C++ class/struct/union tag with no body is a forward declaration
# (`class Widget;`); making it its own node collides with the real
# definition's qn and fragments the class into same-named nodes.
CPP_TYPE_SPECIFIER_NODE_TYPES = frozenset(
    {"class_specifier", "struct_specifier", "union_specifier"}
)
# The tags that can also name a type without defining it: `struct Table *mt;`,
# `f(struct stat *st)`, `enum color c;` (issue #2615).
C_ELABORATED_TYPE_NODE_TYPES = CPP_TYPE_SPECIFIER_NODE_TYPES | {TS_ENUM_SPECIFIER}
# A bodyless tag as the whole of one of these, with no declarator, is a
# forward declaration (`class Inner;` in a class body), not a use.
C_FORWARD_DECLARING_NODE_TYPES = frozenset(
    {CppNodeType.DECLARATION, CppNodeType.FIELD_DECLARATION}
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

# Tree-sitter C++ node types for language_spec
TS_CPP_FUNCTION_DEFINITION = "function_definition"
TS_CPP_DECLARATION = "declaration"
TS_CPP_FIELD_DECLARATION = "field_declaration"
TS_CPP_TEMPLATE_DECLARATION = "template_declaration"
TS_CPP_TEMPLATE_PARAMETER_LIST = "template_parameter_list"
# The template TYPE-parameter declaration node types. A value/non-type param
# (`parameter_declaration`, e.g. `int N` / `MyEnum E`) and a template-template param
# are deliberately excluded: their type name is a concrete type, not a stand-in that
# a call receiver could be instantiated as, so it must not enter the template-param set.
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
# The body of a namespace or an `extern "C++" { }` block.
TS_CPP_DECLARATION_LIST = "declaration_list"
TS_CPP_CALL_EXPRESSION = "call_expression"
TS_CPP_FIELD_EXPRESSION = "field_expression"
TS_CPP_SUBSCRIPT_EXPRESSION = "subscript_expression"
TS_CPP_NEW_EXPRESSION = "new_expression"
TS_CPP_DELETE_EXPRESSION = "delete_expression"
TS_CPP_BINARY_EXPRESSION = "binary_expression"
TS_CPP_UNARY_EXPRESSION = "unary_expression"
TS_CPP_UPDATE_EXPRESSION = "update_expression"
TS_CPP_FUNCTION_DECLARATOR = "function_declarator"
# Substring shared by C++ declarator node types (pointer_declarator,
# reference_declarator, ...), used to unwrap a parameter declarator down
# to its bound identifier.
CPP_DECLARATOR_SUFFIX = "declarator"

FIELD_OPERATOR = "operator"
FIELD_MACRO = "macro"

# C++ I/O direct-sink walk node types (issue #714). call_expression keeps a
# `function` field so call_name works unchanged. The stdout write path is the
# stream-insertion operator `std::cout << x` -- a `binary_expression` with a `<<`
# operator whose left-spine base is cout/cerr (no call node), handled via
# stream_sink_type like Rust's macro sinks. A string_literal wraps string_content;
# `compound_statement` is the block scope; `declaration` holds init_declarator
# locals whose bound name is nested under the `declarator` field.
TS_CPP_STRING_LITERAL = "string_literal"
TS_CPP_STRING_CONTENT = "string_content"
TS_CPP_COMPOUND_STATEMENT = "compound_statement"
# `if (int x = 1)` / `switch (int q = f())`: the condition declaration nests
# inside this wrapper, one level below the statement node.
TS_CPP_CONDITION_CLAUSE = "condition_clause"
# A lambda's parameter list hangs off this declarator (no name to declare).
TS_CPP_ABSTRACT_FUNCTION_DECLARATOR = "abstract_function_declarator"
TS_CPP_DECLARATION = "declaration"
TS_CPP_INIT_DECLARATOR = "init_declarator"
TS_CPP_PARAMETER_DECLARATION = "parameter_declaration"
TS_CPP_IDENTIFIER = "identifier"
TS_CPP_QUALIFIED_IDENTIFIER = "qualified_identifier"
# `Reader<T>(...)` as a call target: the callee wraps name + template args.
TS_CPP_TEMPLATE_FUNCTION = "template_function"
# `&fn` / `*p`: the unary address-of or dereference, told apart by its
# `operator` field. Only address-of names a function it hands over.
TS_CPP_POINTER_EXPRESSION = "pointer_expression"
CPP_ADDRESS_OF = "&"
CPP_DEREFERENCE = "*"
# Declarators that put a pointer or an array between a declared type and its
# name (or its unnamed slot): it holds an address, not a value of that type.
CPP_INDIRECT_DECLARATOR_TYPES = frozenset(
    {
        CppNodeType.POINTER_DECLARATOR,
        CppNodeType.ARRAY_DECLARATOR,
        CppNodeType.ABSTRACT_POINTER_DECLARATOR,
        CppNodeType.ABSTRACT_ARRAY_DECLARATOR,
    }
)
# Parameter declarations a C++ parameter list holds, a pack among them.
CPP_PARAMETER_DECLARATION_TYPES = frozenset(
    {
        CppNodeType.PARAMETER_DECLARATION,
        CppNodeType.OPTIONAL_PARAMETER_DECLARATION,
        CppNodeType.VARIADIC_PARAMETER_DECLARATION,
    }
)
# The nodes a free function is declared by: a definition or a prototype.
CPP_FREE_FUNCTION_DECLARATION_TYPES = frozenset(
    {CppNodeType.FUNCTION_DEFINITION, CppNodeType.DECLARATION}
)
# `return {args};` -- a braced construction of the declared return type.
TS_CPP_INITIALIZER_LIST = "initializer_list"
# Stream-insertion operator; a `binary_expression` using it whose left-spine base
# is std::cout / std::cerr writes STDOUT.
CPP_OP_LEFT_SHIFT = "<<"
# Stream-extraction operator; on a bound fstream handle (`in >> word`) it is a
# READ of that handle's resource (issue #714).
CPP_OP_RIGHT_SHIFT = ">>"
TS_CPP_FOR_RANGE_LOOP = "for_range_loop"
# Switch family: cases may fall through; a default arm is a
# case_statement without a `value` field.
TS_CPP_SWITCH_STATEMENT = "switch_statement"
TS_CPP_CASE_STATEMENT = "case_statement"
# field_expression = `obj.field` (argument/field); subscript_expression =
# `arr[i]` (argument/indices). Inert for C++ I/O, wired for shape correctness.
CPP_FIELD_ARGUMENT = "argument"
CPP_FIELD_FIELD = "field"
CPP_FIELD_INDICES = "indices"

# Derived node type tuples for class ingestion
CPP_CLASS_TYPES = (CppNodeType.CLASS_SPECIFIER, TS_STRUCT_SPECIFIER)
CPP_COMPOUND_TYPES = (*CPP_CLASS_TYPES, TS_UNION_SPECIFIER, TS_ENUM_SPECIFIER)
# Node types that open their own variable scope; local-variable inference must
# not descend, or a name in a lambda / nested function / local class body gets
# attributed to the enclosing function's scope.
CPP_NESTED_SCOPE_NODE_TYPES = frozenset(
    (
        TS_CPP_FUNCTION_DEFINITION,
        TS_CPP_LAMBDA_EXPRESSION,
        *CPP_COMPOUND_TYPES,
    )
)

# Preprocessor conditional directive heads, matched at line start (C allows
# whitespace around '#'). Drives the whole-file-ERROR parse recovery: a
# conditional branch whose brace count does not balance (nlohmann's
# `#ifdef __cpp_lib_byteswap ... else { #endif`) breaks tree-sitter, which
# keeps every branch's tokens.
CPP_PREPROC_CONDITIONAL_PATTERN = (
    rb"^\s*#\s*(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b"
)
CPP_PREPROC_OPEN_DIRECTIVES = frozenset({b"if", b"ifdef", b"ifndef"})
CPP_PREPROC_SPLIT_DIRECTIVES = frozenset({b"elif", b"elifdef", b"elifndef", b"else"})

# Annotation macros written after a declarator (`bool Check() const
# LOCKS_REQUIRED(mu) {`, `int count_ GUARDED_BY(mu);`, issue #2552). Only a
# macro can stand there in valid C++, but tree-sitter cannot see the
# #define and splits the declaration around it. The recovery pass blanks an
# ALL_CAPS word, with its balanced argument list if it has one, when it
# sits between a declarator and the token that ends the declaration. The
# case rule keeps `int ATTR(x) name(void)` (a macro BEFORE the name) from
# losing its real name.
CPP_IDENTIFIER_PATTERN = rb"[A-Za-z_][A-Za-z0-9_]*"
CPP_ANNOTATION_MACRO_PATTERN = rb"_*[A-Z][A-Z0-9_]*"
# Whitespace and comments between the declarator and the macro (gmock puts
# the annotation on its own line, sometimes after a `//` note).
CPP_TRIVIA_PATTERN = rb"(?:\s+|//[^\n]*|/\*.*?\*/)*"
# A string or character literal: a paren inside one does not count when
# matching an argument list's closing paren.
CPP_LITERAL_PATTERN = rb"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'"
CPP_ARGUMENT_TOKEN_PATTERN = CPP_LITERAL_PATTERN + rb"|[()]"
# Two words in a row (`int a`, `const T& x`) declare a parameter; a macro's
# arguments are expressions.
CPP_DECLARED_NAME_PATTERN = rb"\b[A-Za-z_]\w*\s+[A-Za-z_]\w*\b"
CPP_OPEN_PAREN = b"("
CPP_CLOSE_PAREN = b")"
CPP_BLANK_BYTE = ord(" ")
CPP_LINE_BREAK_BYTES = frozenset(b"\r\n")
# Declarator suffixes the grammar knows; an annotation may sit on either side
# of them (`f() LOCKS_REQUIRED(mu) override`), so the scan steps over them
# without blanking them.
CPP_DECLARATOR_SUFFIX_KEYWORDS = frozenset(
    {
        b"const",
        b"volatile",
        b"override",
        b"final",
        b"noexcept",
        b"throw",
        b"__attribute__",
        b"__attribute",
    }
)
# What may follow the last annotation: a body, the end of the declaration,
# `= 0`/`= default`/an initializer, the next declarator, a ctor-initializer.
CPP_DECLARATOR_END_BYTES = frozenset(b"{;=,:")
CPP_DECLARATOR_END_KEYWORDS = frozenset({b"try", b"requires"})
# An annotation's argument list is short; an unbalanced `(` must not send
# the scan across the rest of the file.
CPP_ANNOTATION_MAX_ARGUMENT_BYTES = 1024

# Reserved keywords that error recovery can leave in declarator position
# (nlohmann: a macro access-label followed by `const decltype(MACRO_)`
# members parses as a function declaration NAMED decltype). None can ever
# name a real C/C++ function or method, so extraction rejects them.
CPP_RESERVED_DEF_NAMES = frozenset(
    {
        "decltype",
        "sizeof",
        "alignof",
        "alignas",
        "typeid",
        "static_assert",
        "noexcept",
        "typename",
        "template",
        "requires",
        "if",
        "for",
        "while",
        "switch",
        "return",
        "catch",
    }
)

# Lambda capture list: `[a]` holds identifiers, `[=]`/`[&]` a default
# capture pulling every enclosing local into scope.
TS_CPP_LAMBDA_CAPTURE_SPECIFIER = "lambda_capture_specifier"
TS_CPP_LAMBDA_DEFAULT_CAPTURE = "lambda_default_capture"
# `[name = expr]` init-capture: binds its leading identifier in the lambda.
TS_CPP_LAMBDA_CAPTURE_INITIALIZER = "lambda_capture_initializer"
# A C or C++ enum body and its enumerators (issue #1807).
TS_ENUMERATOR_LIST = "enumerator_list"
TS_ENUMERATOR = "enumerator"

# Where C names a function as a VALUE rather than calling it (issue #2529): an
# initializer-list entry (positional, or `.field = f` in an initializer_pair),
# a declarator's initial value, an assignment's right side, a call argument.
TS_CPP_INITIALIZER_PAIR = "initializer_pair"
TS_CPP_ASSIGNMENT_EXPRESSION = "assignment_expression"
TS_CPP_ARGUMENT_LIST = "argument_list"
# Wrappers a function designator keeps its identity through: `&f`, `(f)`,
# `(handler_t)f` and either branch of `c ? f : g`.
TS_CPP_POINTER_EXPRESSION = "pointer_expression"
TS_CPP_CAST_EXPRESSION = "cast_expression"
TS_CPP_CONDITIONAL_EXPRESSION = "conditional_expression"
CPP_OP_ADDRESS_OF = "&"
# Nodes that hold file-scope declarations without opening a scope of their
# own: preprocessor conditionals and an `extern "C" { ... }` block.
C_FILE_SCOPE_CONTAINER_TYPES = frozenset(
    {
        "translation_unit",
        "preproc_if",
        "preproc_ifdef",
        "preproc_else",
        "preproc_elif",
        "preproc_elifdef",
        "linkage_specification",
        "declaration_list",
    }
)
