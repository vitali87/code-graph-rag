# Java tree-sitter node types, modifiers, and JVM layout constants.

from .ast_nodes import (
    TS_CLASS_DECLARATION,
    TS_ENUM_DECLARATION,
    TS_INTERFACE_DECLARATION,
)
from .core import ENTITY_FUNCTION, ENTITY_METHOD

# Tree-sitter Java node types for language_spec
TS_JAVA_METHOD_INVOCATION = "method_invocation"
TS_JAVA_ANNOTATION_TYPE_DECLARATION = "annotation_type_declaration"

# Java interface `extends A, B` clause (tree-sitter-java); holds a type_list.
TS_JAVA_EXTENDS_INTERFACES = "extends_interfaces"

TS_JAVA_CAST_EXPRESSION = "cast_expression"

# Java tree-sitter node types
TS_FORMAL_PARAMETER = "formal_parameter"
TS_SPREAD_PARAMETER = "spread_parameter"
TS_LOCAL_VARIABLE_DECLARATION = "local_variable_declaration"
TS_FIELD_DECLARATION = "field_declaration"
TS_ASSIGNMENT_EXPRESSION = "assignment_expression"

TS_OBJECT_CREATION_EXPRESSION = "object_creation_expression"
TS_METHOD_INVOCATION = "method_invocation"
TS_FIELD_ACCESS = "field_access"
TS_INTEGER_LITERAL = "integer_literal"
TS_DECIMAL_FLOATING_POINT_LITERAL = "decimal_floating_point_literal"
TS_ARRAY_CREATION_EXPRESSION = "array_creation_expression"
TS_METHOD_DECLARATION = "method_declaration"
TS_ENHANCED_FOR_STATEMENT = "enhanced_for_statement"
# Switch family: colon groups may fall through, arrow rules are exclusive;
# a default arm is a switch_label with no named children.
TS_JAVA_SWITCH_EXPRESSION = "switch_expression"
TS_JAVA_SWITCH_RULE = "switch_rule"
TS_JAVA_SWITCH_BLOCK_STATEMENT_GROUP = "switch_block_statement_group"
TS_JAVA_SWITCH_LABEL = "switch_label"
TS_TRY_WITH_RESOURCES_STATEMENT = "try_with_resources_statement"
# One declaration inside a try-with-resources header; binds via `name`/`value`
# fields exactly like a variable_declarator.
TS_JAVA_RESOURCE = "resource"
TS_RECORD_DECLARATION = "record_declaration"
TS_TRUE = "true"
TS_FALSE = "false"

# Java I/O direct-sink walk node types (issue #714). string_literal wraps a
# `string_fragment` (shared with JS); `block` is the method-body lexical scope;
# `lambda_expression` is a nested scope pruned from the enclosing walk. field_access
# / array_access describe member/subscript access (inert for Java, which has no
# IO_MEMBER_READS entry -- Java env access is a call, System.getenv, not a member).
TS_JAVA_STRING_LITERAL = "string_literal"
TS_JAVA_BLOCK = "block"
TS_JAVA_LAMBDA_EXPRESSION = "lambda_expression"
TS_JAVA_ARRAY_ACCESS = "array_access"
JAVA_FIELD_FIELD = "field"
JAVA_FIELD_INDEX = "index"

# Tree-sitter field names for child_by_field_name
TS_FIELD_NAME = "name"
TS_FIELD_TYPE = "type"
TS_SCOPED_TYPE_IDENTIFIER = "scoped_type_identifier"
TS_FIELD_SUPERCLASS = "superclass"
TS_FIELD_INTERFACES = "interfaces"
TS_FIELD_TYPE_PARAMETERS = "type_parameters"
TS_FIELD_PARAMETERS = "parameters"
TS_FIELD_DECLARATOR = "declarator"
TS_FIELD_OBJECT = "object"
TS_FIELD_ARGUMENTS = "arguments"
TS_FIELD_FUNCTION = "function"
TS_FIELD_BODY = "body"
TS_FIELD_LEFT = "left"
TS_FIELD_RIGHT = "right"

QUERY_CAPTURE_CLASS = "class"
QUERY_CAPTURE_FUNCTION = "function"
QUERY_KEY_CLASSES = "classes"
QUERY_KEY_FUNCTIONS = "functions"

# Java type inference keywords
JAVA_KEYWORD_THIS = "this"
JAVA_KEYWORD_SUPER = "super"

# Java heuristic patterns
JAVA_GETTER_PATTERN = "get"
JAVA_NAME_PATTERN = "name"
JAVA_ID_PATTERN = "id"
JAVA_SIZE_PATTERN = "size"
JAVA_LENGTH_PATTERN = "length"
JAVA_CREATE_PATTERN = "create"
JAVA_NEW_PATTERN = "new"
JAVA_IS_PATTERN = "is"
JAVA_HAS_PATTERN = "has"
JAVA_USER_PATTERN = "user"
JAVA_ORDER_PATTERN = "order"

# Java entity type names
ENTITY_CONSTRUCTOR = "Constructor"

# Java callable entity types for method resolution
# FUNCTION is included so an unqualified call inside a method-body anonymous class
# can reach the anon's OWN methods (registered as Function nodes under the enclosing
# scope, e.g. gson's `delegate()` called by the same anon's `read()`); the module
# scan is a last-resort fallback after precise class/anon-base/enclosing lookups.
JAVA_CALLABLE_ENTITY_TYPES = frozenset(
    {ENTITY_METHOD, ENTITY_CONSTRUCTOR, ENTITY_FUNCTION}
)

# Java primitive type names
JAVA_TYPE_STRING = "String"
JAVA_TYPE_INT = "int"
JAVA_TYPE_DOUBLE = "double"
JAVA_TYPE_BOOLEAN = "boolean"
JAVA_TYPE_LONG = "java.lang.Long"
JAVA_TYPE_STRING_FQN = "java.lang.String"
JAVA_TYPE_OBJECT = "Object"

# Java heuristic return type names
JAVA_HEURISTIC_USER = "User"
JAVA_HEURISTIC_ORDER = "Order"

# Java tree-sitter node types for java_utils
TS_PACKAGE_DECLARATION = "package_declaration"
TS_ANNOTATION_TYPE_DECLARATION = "annotation_type_declaration"
TS_CONSTRUCTOR_DECLARATION = "constructor_declaration"
TS_ANNOTATION = "annotation"
TS_MARKER_ANNOTATION = "marker_annotation"
TS_GENERIC_TYPE = "generic_type"
TS_TYPE_PARAMETER = "type_parameter"
TS_MODIFIERS = "modifiers"
TS_VOID_TYPE = "void_type"
TS_PROGRAM = "program"
TS_THIS = "this"
TS_SUPER = "super"

# Java modifier node types
JAVA_MODIFIER_PUBLIC = "public"
JAVA_MODIFIER_PRIVATE = "private"
JAVA_MODIFIER_PROTECTED = "protected"
JAVA_MODIFIER_STATIC = "static"
JAVA_MODIFIER_FINAL = "final"
JAVA_MODIFIER_ABSTRACT = "abstract"
JAVA_MODIFIER_SYNCHRONIZED = "synchronized"
JAVA_MODIFIER_TRANSIENT = "transient"
JAVA_MODIFIER_VOLATILE = "volatile"

JAVA_CLASS_MODIFIERS = frozenset(
    {
        JAVA_MODIFIER_PUBLIC,
        JAVA_MODIFIER_PRIVATE,
        JAVA_MODIFIER_PROTECTED,
        JAVA_MODIFIER_STATIC,
        JAVA_MODIFIER_FINAL,
        JAVA_MODIFIER_ABSTRACT,
    }
)

JAVA_METHOD_MODIFIERS = frozenset(
    {
        JAVA_MODIFIER_PUBLIC,
        JAVA_MODIFIER_PRIVATE,
        JAVA_MODIFIER_PROTECTED,
        JAVA_MODIFIER_STATIC,
        JAVA_MODIFIER_FINAL,
        JAVA_MODIFIER_ABSTRACT,
        JAVA_MODIFIER_SYNCHRONIZED,
    }
)

JAVA_FIELD_MODIFIERS = frozenset(
    {
        JAVA_MODIFIER_PUBLIC,
        JAVA_MODIFIER_PRIVATE,
        JAVA_MODIFIER_PROTECTED,
        JAVA_MODIFIER_STATIC,
        JAVA_MODIFIER_FINAL,
        JAVA_MODIFIER_TRANSIENT,
        JAVA_MODIFIER_VOLATILE,
    }
)

# Java visibility values
JAVA_VISIBILITY_PUBLIC = "public"
JAVA_VISIBILITY_PROTECTED = "protected"
JAVA_VISIBILITY_PRIVATE = "private"
JAVA_VISIBILITY_PACKAGE = "package"

# Java class type suffixes and names
JAVA_DECLARATION_SUFFIX = "_declaration"
JAVA_TYPE_METHOD = "method"
JAVA_TYPE_CONSTRUCTOR = "constructor"

# Java class node types for matching
JAVA_CLASS_NODE_TYPES = frozenset(
    {
        TS_CLASS_DECLARATION,
        TS_INTERFACE_DECLARATION,
        TS_ENUM_DECLARATION,
        TS_ANNOTATION_TYPE_DECLARATION,
        TS_RECORD_DECLARATION,
    }
)

# Java method node types
JAVA_METHOD_NODE_TYPES = frozenset(
    {
        TS_METHOD_DECLARATION,
        TS_CONSTRUCTOR_DECLARATION,
    }
)

# Java main method constants
JAVA_MAIN_METHOD_NAME = "main"
JAVA_MAIN_PARAM_ARRAY = "String[]"
JAVA_MAIN_PARAM_VARARGS = "String..."
JAVA_MAIN_PARAM_TYPE = "String"

# Java path parsing constants
JAVA_PATH_JAVA = "java"
JAVA_PATH_KOTLIN = "kotlin"
JAVA_PATH_SCALA = "scala"
JAVA_PATH_SRC = "src"
JAVA_PATH_MAIN = "main"
JAVA_PATH_TEST = "test"

JAVA_JVM_LANGUAGES = frozenset(
    {
        JAVA_PATH_JAVA,
        JAVA_PATH_KOTLIN,
        JAVA_PATH_SCALA,
    }
)

JAVA_SRC_FOLDERS = frozenset(
    {
        JAVA_PATH_MAIN,
        JAVA_PATH_TEST,
    }
)
