# (H) Shared tree-sitter node types, field names, and query captures.

from .languages import SupportedLanguage

# (H) Tree-sitter AST node type constants
FUNCTION_NODES_BASIC = ("function_declaration", "function_definition")
FUNCTION_NODES_LAMBDA = (
    "lambda_expression",
    "arrow_function",
    "anonymous_function",
    "closure_expression",
)
FUNCTION_NODES_METHOD = (
    "method_declaration",
    "constructor_declaration",
    "destructor_declaration",
)
FUNCTION_NODES_TEMPLATE = (
    "template_declaration",
    "function_signature_item",
    "function_signature",
)
FUNCTION_NODES_GENERATOR = ("generator_function_declaration", "function_expression")

CLASS_NODES_BASIC = ("class_declaration", "class_definition")
CLASS_NODES_STRUCT = ("struct_declaration", "struct_specifier", "struct_item")
CLASS_NODES_INTERFACE = ("interface_declaration", "trait_declaration", "trait_item")
CLASS_NODES_ENUM = ("enum_declaration", "enum_item", "enum_specifier")
CLASS_NODES_TYPE_ALIAS = ("type_alias_declaration", "type_item")
CLASS_NODES_UNION = ("union_specifier", "union_item")

CALL_NODES_BASIC = ("call_expression", "function_call")
CALL_NODES_METHOD = (
    "method_invocation",
    "member_call_expression",
    "field_expression",
)
CALL_NODES_OPERATOR = ("binary_expression", "unary_expression", "update_expression")
CALL_NODES_SPECIAL = ("new_expression", "delete_expression", "macro_invocation")

IMPORT_NODES_STANDARD = ("import_declaration", "import_statement")
IMPORT_NODES_FROM = ("import_from_statement",)
# (H) variable_declaration: CommonJS `var X = require(...)` (express) binds
# (H) imports exactly like const/let lexical_declarations.
IMPORT_NODES_MODULE = (
    "lexical_declaration",
    "variable_declaration",
    "export_statement",
)
IMPORT_NODES_INCLUDE = ("preproc_include",)

# (H) JS/TS specific node types
JS_TS_FUNCTION_NODES = (
    "function_declaration",
    "generator_function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
)
JS_TS_CLASS_NODES = ("class_declaration", "class")
JS_TS_IMPORT_NODES = (
    "import_statement",
    "lexical_declaration",
    "variable_declaration",
    "export_statement",
)
JS_TS_LANGUAGES = frozenset(
    {SupportedLanguage.JS, SupportedLanguage.TS, SupportedLanguage.TSX}
)

# (H) C++ import node types
CPP_IMPORT_NODES = ("preproc_include", "template_function", "declaration")

# (H) AST field names for name extraction
NAME_FIELDS = ("identifier", "name", "id")

# (H) Tree-sitter field name constants for child_by_field_name
FIELD_OBJECT = "object"
FIELD_PROPERTY = "property"
FIELD_NAME = "name"
FIELD_ALIAS = "alias"
FIELD_MODULE_NAME = "module_name"
FIELD_ARGUMENTS = "arguments"
FIELD_BODY = "body"
FIELD_RETURN_TYPE = "return_type"
FIELD_CONSTRUCTOR = "constructor"
FIELD_DECLARATOR = "declarator"
FIELD_PARAMETERS = "parameters"
FIELD_RECEIVER = "receiver"
FIELD_TYPE = "type"
# (H) The wrapped function/class inside a Python decorated_definition node.
FIELD_DEFINITION = "definition"
FIELD_RESULT = "result"
# (H) Rust impl `trait`/`type` fields and a trait's supertrait `bounds`.
FIELD_TRAIT = "trait"
FIELD_BOUNDS = "bounds"
TS_RS_TRAIT_BOUNDS = "trait_bounds"
FIELD_VALUE = "value"
FIELD_LEFT = "left"
FIELD_RIGHT = "right"
# (H) A C-style for's post-iteration clause: Java/C++ hold it in an `update`
# (H) field on the loop node, Go inside its `for_clause`.
FIELD_UPDATE = "update"
FIELD_FIELD = "field"
FIELD_SCOPE = "scope"
FIELD_SUPERCLASS = "superclass"
FIELD_SUPERCLASSES = "superclasses"
FIELD_INTERFACES = "interfaces"

# (H) Query dict keys
QUERY_FUNCTIONS = "functions"
QUERY_CLASSES = "classes"
QUERY_CALLS = "calls"
QUERY_IMPORTS = "imports"
QUERY_LOCALS = "locals"
QUERY_CONFIG = "config"
QUERY_LANGUAGE = "language"
QUERY_HIGHLIGHTS = "highlights"

# (H) Query capture names
CAPTURE_FUNCTION = "function"
CAPTURE_CLASS = "class"
CAPTURE_CALL = "call"
CAPTURE_IMPORT = "import"
CAPTURE_IMPORT_FROM = "import_from"
CAPTURE_KEYWORD_MODIFIER = "keyword.modifier"
CAPTURE_KEYWORD = "keyword"
CAPTURE_ATTRIBUTE = "attribute"
CAPTURE_FUNCTION_DECORATOR = "function.decorator"

# (H) Modifier extraction
EXCLUDED_KEYWORDS = frozenset(
    {
        "def",
        "class",
        "fn",
        "struct",
        "impl",
        "interface",
        "enum",
        "function",
        "trait",
        "type",
        "void",
        "None",
        "True",
        "False",
        "null",
        "true",
        "false",
        "return",
        "import",
        "from",
        "as",
        "where",
    }
)

# (H) Tree-sitter Python import node types
TS_IMPORT_STATEMENT = "import_statement"
TS_IMPORT_FROM_STATEMENT = "import_from_statement"
TS_DOTTED_NAME = "dotted_name"
TS_ALIASED_IMPORT = "aliased_import"
TS_RELATIVE_IMPORT = "relative_import"
TS_IMPORT_PREFIX = "import_prefix"
TS_WILDCARD_IMPORT = "wildcard_import"

# (H) Tree-sitter JS/TS import node types
TS_STRING = "string"
# (H) JS/TS string literals hold their text in a string_fragment child (the
# (H) counterpart of Python's string_content), used for I/O target extraction.
TS_STRING_FRAGMENT = "string_fragment"
# (H) Modern Node builtin imports carry a node: scheme (`import fs from 'node:fs'`);
# (H) stripped when checking whether an imported name is the genuine builtin module.
NODE_BUILTIN_PREFIX = "node:"
# (H) `return_statement` node type (shared by Python and JS/TS grammars); used by
# (H) the language-agnostic flow walk.
TS_RETURN_STATEMENT = "return_statement"
# (H) `await fetch(...)` wraps the call in an await_expression; the flow walk
# (H) unwraps it to see the inner source expression.
TS_AWAIT_EXPRESSION = "await_expression"
# (H) tree-sitter parses comments as named children, so the flow walk filters them
# (H) out before indexing arguments or reading a single sub-expression.
TS_COMMENT = "comment"
# (H) `(expr)` wraps its value in a parenthesized_expression; the flow walk unwraps
# (H) it (like await) to reach the inner source/tainted expression.
TS_PARENTHESIZED_EXPRESSION = "parenthesized_expression"
TS_IMPORT_CLAUSE = "import_clause"
TS_LEXICAL_DECLARATION = "lexical_declaration"
TS_VARIABLE_DECLARATION = "variable_declaration"
TS_EXPORT_STATEMENT = "export_statement"
TS_NAMED_IMPORTS = "named_imports"
TS_IMPORT_SPECIFIER = "import_specifier"
TS_NAMESPACE_IMPORT = "namespace_import"
TS_IDENTIFIER = "identifier"
TS_VARIABLE_DECLARATOR = "variable_declarator"
TS_CALL_EXPRESSION = "call_expression"
TS_EXPORT_CLAUSE = "export_clause"
TS_EXPORT_SPECIFIER = "export_specifier"
TS_EXPORT_DEFAULT = "default"
TS_ACCESSIBILITY_MODIFIER = "accessibility_modifier"
TS_PRIVATE = "private"
TS_PRIVATE_PROPERTY_IDENTIFIER = "private_property_identifier"

# (H) Tree-sitter Java import node types
TS_IMPORT_DECLARATION = "import_declaration"
TS_STATIC = "static"
TS_SCOPED_IDENTIFIER = "scoped_identifier"
TS_ASTERISK = "asterisk"

# (H) Tree-sitter Rust import node types
TS_USE_DECLARATION = "use_declaration"

# (H) Tree-sitter Go import node types
TS_IMPORT_SPEC = "import_spec"
TS_IMPORT_SPEC_LIST = "import_spec_list"
TS_PACKAGE_IDENTIFIER = "package_identifier"
TS_INTERPRETED_STRING_LITERAL = "interpreted_string_literal"

# (H) Tree-sitter C++ import node types
TS_PREPROC_INCLUDE = "preproc_include"
TS_TEMPLATE_FUNCTION = "template_function"
TS_DECLARATION = "declaration"
TS_STRING_LITERAL = "string_literal"
TS_SYSTEM_LIB_STRING = "system_lib_string"
TS_TEMPLATE_ARGUMENT_LIST = "template_argument_list"
# (H) Plain call/constructor argument list (C++ `in("x.txt")` init_declarator
# (H) value, Java `new FileWriter("x")` arguments).
TS_ARGUMENT_LIST = "argument_list"
# (H) `do { .. } while (cond)` -- same node type in the Java and C++ grammars.
TS_DO_STATEMENT = "do_statement"
TS_TYPE_DESCRIPTOR = "type_descriptor"
TS_TYPE_IDENTIFIER = "type_identifier"

# (H) Tree-sitter JS/TS utility node types
TS_RETURN_STATEMENT = "return_statement"
TS_RETURN = "return"
TS_NEW_EXPRESSION = "new_expression"

# (H) Tree-sitter class/module node types for class_ingest
TS_MODULE_DECLARATION = "module_declaration"
TS_IMPL_ITEM = "impl_item"
TS_INTERFACE_DECLARATION = "interface_declaration"
TS_ENUM_DECLARATION = "enum_declaration"
TS_ENUM_SPECIFIER = "enum_specifier"
TS_ENUM_CLASS_SPECIFIER = "enum_class_specifier"
TS_TYPE_ALIAS_DECLARATION = "type_alias_declaration"
TS_STRUCT_SPECIFIER = "struct_specifier"
TS_UNION_SPECIFIER = "union_specifier"
TS_CLASS_DECLARATION = "class_declaration"
TS_NAMESPACE_DEFINITION = "namespace_definition"
TS_ABSTRACT_CLASS_DECLARATION = "abstract_class_declaration"
TS_INTERNAL_MODULE = "internal_module"

TS_BASE_CLASS_CLAUSE = "base_class_clause"
TS_TEMPLATE_TYPE = "template_type"
TS_ACCESS_SPECIFIER = "access_specifier"
TS_VIRTUAL = "virtual"
TS_TYPE_LIST = "type_list"
TS_CLASS_HERITAGE = "class_heritage"
# (H) TS class `implements I, J` clause (a child of class_heritage).
TS_IMPLEMENTS_CLAUSE = "implements_clause"
TS_EXTENDS_CLAUSE = "extends_clause"
TS_MEMBER_EXPRESSION = "member_expression"
TS_SELECTOR_EXPRESSION = "selector_expression"
TS_EXTENDS = "extends"
TS_ARGUMENTS = "arguments"
TS_EXTENDS_TYPE_CLAUSE = "extends_type_clause"

TS_METHOD_DEFINITION = "method_definition"
TS_DECORATOR = "decorator"
TS_ERROR = "ERROR"
TS_EXPRESSION_STATEMENT = "expression_statement"
TS_STATEMENT_BLOCK = "statement_block"
TS_PARENTHESIZED_EXPRESSION = "parenthesized_expression"
TS_BINARY_EXPRESSION = "binary_expression"

TS_ATTRIBUTE = "attribute"

# (H) TS-specific node types
TS_FUNCTION_SIGNATURE = "function_signature"
