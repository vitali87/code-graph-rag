# JavaScript/TypeScript tree-sitter node types, queries, and captures.

from .ast_nodes import (
    TS_CALL_EXPRESSION,
    TS_IDENTIFIER,
    TS_MEMBER_EXPRESSION,
    TS_NEW_EXPRESSION,
)

# Locals query patterns for JS/TS
JS_LOCALS_PATTERN = """
; Variable definitions
(variable_declarator name: (identifier) @local.definition)
(function_declaration name: (identifier) @local.definition)
(class_declaration name: (identifier) @local.definition)

; Variable references
(identifier) @local.reference
"""

TS_LOCALS_PATTERN = """
; Variable definitions (TypeScript has multiple declaration types)
(variable_declarator name: (identifier) @local.definition)
(lexical_declaration (variable_declarator name: (identifier) @local.definition))
(variable_declaration (variable_declarator name: (identifier) @local.definition))

; Function definitions
(function_declaration name: (identifier) @local.definition)

; Class definitions (uses type_identifier for class names)
(class_declaration name: (type_identifier) @local.definition)

; Variable references
(identifier) @local.reference
"""

# Receivers that address the MODULE itself in CommonJS code (`exports.render()`,
# `module.exports.x()`, prototype-pattern `this`): only these bind a dotted call
# to a same-module free function; `view.render()` is an instance call.
JS_MODULE_RECEIVERS = frozenset({"exports", "module", "this"})
# `this.` receiver prefix of a call name; a prototype-assigned function
# (`Date.prototype.strftime`) dispatches such calls to a sibling method of
# the same prototype target before the module-receiver fallback applies.
JS_THIS_CALL_PREFIX = "this."

JS_TS_PARENT_REF_TYPES = (TS_IDENTIFIER, TS_MEMBER_EXPRESSION)
# JSX element nodes that carry a component name (javascript and tsx grammars
# share these); the closing element repeats the name and must not double-emit.
TS_JSX_SELF_CLOSING_ELEMENT = "jsx_self_closing_element"
TS_JSX_OPENING_ELEMENT = "jsx_opening_element"
# The `{...}` wrapper around an expression in a JSX attribute value or child
# (`onClick={handleLogout}`, `onClick={() => x()}`); its inner expression can
# hand a function to the element as a prop.
TS_JSX_EXPRESSION = "jsx_expression"

# TS "cast" wrappers transparent for reference resolution: `x as T`,
# `x satisfies T`, and non-null `x!`. Their first named child is the wrapped
# value, so unwrapping reaches the referenced expression
# (`persistImpl as unknown as Persist`).
TS_AS_EXPRESSION = "as_expression"
TS_SATISFIES_EXPRESSION = "satisfies_expression"
TS_NON_NULL_EXPRESSION = "non_null_expression"
TS_CAST_WRAPPER_TYPES = frozenset(
    {TS_AS_EXPRESSION, TS_SATISFIES_EXPRESSION, TS_NON_NULL_EXPRESSION}
)

# JS/TS ingest node types
TS_PAIR = "pair"
TS_OBJECT = "object"
TS_TEMPLATE_STRING = "template_string"
TS_TEMPLATE_SUBSTITUTION = "template_substitution"
TS_ARRAY = "array"

# When a variable_declarator's value is one of these, the variable binds the
# call/construction RESULT, not a function, so an arrow inside its arguments
# (`const m = useMutation({fn: () => {}})`) must not inherit the variable's
# name; arrows nested under a const-bound object still take the object's name.
JS_CALL_RESULT_VALUE_TYPES = frozenset({TS_CALL_EXPRESSION, TS_NEW_EXPRESSION})
TS_FUNCTION_EXPRESSION = "function_expression"
TS_ARROW_FUNCTION = "arrow_function"
TS_REQUIRED_PARAMETER = "required_parameter"
TS_OPTIONAL_PARAMETER = "optional_parameter"
TS_ASSIGNMENT_PATTERN = "assignment_pattern"
TS_JS_ASSIGNMENT_EXPRESSION = "assignment_expression"
# `x += v` and friends: reads the old value AND writes the new one.
TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION = "augmented_assignment_expression"
# `x++` / `--x`: also a read-then-write; the operand is the `argument` field.
TS_JS_UPDATE_EXPRESSION = "update_expression"
TS_JS_FIELD_ARGUMENT = "argument"
TS_FIELD_PATTERN = "pattern"
TS_FIELD_PARAMETER = "parameter"
TS_MODULE = "module"
TS_CLASS_BODY = "class_body"

TS_PROPERTY_IDENTIFIER = "property_identifier"

# JS prototype property keywords
JS_PROTOTYPE_KEYWORD = "prototype"
JS_OBJECT_NAME = "Object"
JS_CREATE_METHOD = "create"

# JS/TS ingest query capture names
CAPTURE_CHILD_CLASS = "child_class"
CAPTURE_PARENT_CLASS = "parent_class"
CAPTURE_CONSTRUCTOR_NAME = "constructor_name"
CAPTURE_PROTOTYPE_KEYWORD = "prototype_keyword"
CAPTURE_METHOD_NAME = "method_name"
CAPTURE_METHOD_FUNCTION = "method_function"
CAPTURE_MEMBER_EXPR = "member_expr"
CAPTURE_FUNCTION_EXPR = "function_expr"
CAPTURE_ARROW_FUNCTION = "arrow_function"

# JS prototype inheritance query
JS_PROTOTYPE_INHERITANCE_QUERY = """
(assignment_expression
  left: (member_expression
    object: (identifier) @child_class
    property: (property_identifier) @prototype (#eq? @prototype "prototype"))
  right: (call_expression
    function: (member_expression
      object: (identifier) @object_name (#eq? @object_name "Object")
      property: (property_identifier) @create_method (#eq? @create_method "create"))
    arguments: (arguments
      (member_expression
        object: (identifier) @parent_class
        property: (property_identifier) @parent_prototype (#eq? @parent_prototype "prototype")))))
"""

# JS prototype method assignment query
JS_PROTOTYPE_METHOD_QUERY = """
(assignment_expression
  left: (member_expression
    object: (member_expression
      object: (identifier) @constructor_name
      property: (property_identifier) @prototype_keyword (#eq? @prototype_keyword "prototype"))
    property: (property_identifier) @method_name)
  right: (function_expression) @method_function)
"""

# JS object method query
JS_OBJECT_METHOD_QUERY = """
(pair
  key: (property_identifier) @method_name
  value: (function_expression) @method_function)
"""

# JS method definition query
JS_METHOD_DEF_QUERY = """
(object
  (method_definition
    name: (property_identifier) @method_name) @method_function)
"""

# JS object arrow function query
JS_OBJECT_ARROW_QUERY = """
(object
  (pair
    (property_identifier) @method_name
    (arrow_function) @arrow_function))
"""

# JS assignment arrow function query
JS_ASSIGNMENT_ARROW_QUERY = """
(assignment_expression
  (member_expression) @member_expr
  (arrow_function) @arrow_function)
"""

# JS assignment function expression query
JS_ASSIGNMENT_FUNCTION_QUERY = """
(assignment_expression
  (member_expression) @member_expr
  (function_expression) @function_expr)
"""

# JS/TS control-flow node types + fields for the path-sensitive taint walk
# (issue #714 follow-up). Each if/else, loop, and try branch is evaluated against
# a COPY of the incoming taint state and unioned at the merge, so taint surviving
# on ANY path survives and a kill counts only when it happens on EVERY path. The
# values coincide with the Python grammar's but stay JS-scoped per the per-language
# constants convention.
TS_JS_IF_STATEMENT = "if_statement"
# Switch family: cases may fall through into the next case.
TS_JS_SWITCH_STATEMENT = "switch_statement"
TS_JS_SWITCH_CASE = "switch_case"
TS_JS_SWITCH_DEFAULT = "switch_default"
# `c ? a : b` (shared name with the Java grammar); C++ spells it
# conditional_expression.
TS_JS_TERNARY_EXPRESSION = "ternary_expression"
# Short-circuit operators whose result IS one of the operands, so a
# bind through them unions both operands' taints.
JS_SHORT_CIRCUIT_OPERATORS: frozenset[str] = frozenset({"||", "??", "&&"})
TS_JS_ELSE_CLAUSE = "else_clause"
TS_JS_WHILE_STATEMENT = "while_statement"
TS_JS_FOR_STATEMENT = "for_statement"
TS_JS_FOR_IN_STATEMENT = "for_in_statement"
TS_JS_TRY_STATEMENT = "try_statement"
TS_JS_CATCH_CLAUSE = "catch_clause"
TS_JS_FINALLY_CLAUSE = "finally_clause"
FIELD_ALTERNATIVE = "alternative"
FIELD_HANDLER = "handler"
FIELD_FINALIZER = "finalizer"
# The C-style `for (init; cond; increment)` update clause, which runs AFTER the
# body each iteration, so taint the body carries into it reaches the next one.
FIELD_INCREMENT = "increment"

# JS/TS module system node types
TS_OBJECT_PATTERN = "object_pattern"
TS_ARRAY_PATTERN = "array_pattern"
TS_REST_PATTERN = "rest_pattern"
TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN = "shorthand_property_identifier_pattern"
TS_SHORTHAND_PROPERTY_IDENTIFIER = "shorthand_property_identifier"
TS_PAIR_PATTERN = "pair_pattern"
# `process.env.X` is a member_expression; `process.env['X']` a subscript, used
# to detect environment-variable reads (issue #714 process.env follow-up).
TS_SUBSCRIPT_EXPRESSION = "subscript_expression"
TS_FIELD_INDEX = "index"
TS_FUNCTION_DECLARATION = "function_declaration"
TS_GENERATOR_FUNCTION_DECLARATION = "generator_function_declaration"

# Tree-sitter field names for module system
FIELD_FUNCTION = "function"
FIELD_KEY = "key"

# JS/TS module system keywords
JS_REQUIRE_KEYWORD = "require"
JS_EXPORTS_KEYWORD = "exports"
JS_MODULE_KEYWORD = "module"

# JS/TS export type descriptions
JS_EXPORT_TYPE_COMMONJS = "CommonJS Export"
JS_EXPORT_TYPE_COMMONJS_MODULE = "CommonJS Module Export"
JS_EXPORT_TYPE_ES6_FUNCTION = "ES6 Export Function"
JS_EXPORT_TYPE_ES6_FUNCTION_DECL = "ES6 Export Function Declaration"

# JS/TS CommonJS destructure query
JS_COMMONJS_DESTRUCTURE_QUERY = """
(lexical_declaration
  (variable_declarator
    name: (object_pattern)
    value: (call_expression
      function: (identifier) @func (#eq? @func "require")
    )
  ) @variable_declarator
)
"""

# JS/TS CommonJS exports function query
JS_COMMONJS_EXPORTS_FUNCTION_QUERY = """
(assignment_expression
  left: (member_expression
    object: (identifier) @exports_obj
    property: (property_identifier) @export_name)
  right: [(function_expression) (arrow_function)] @export_function)
"""

# JS/TS CommonJS module.exports query
JS_COMMONJS_MODULE_EXPORTS_QUERY = """
(assignment_expression
  left: (member_expression
    object: (member_expression
      object: (identifier) @module_obj
      property: (property_identifier) @exports_prop)
    property: (property_identifier) @export_name)
  right: [(function_expression) (arrow_function)] @export_function)
"""

# JS/TS ES6 export const query
JS_ES6_EXPORT_CONST_QUERY = """
(export_statement
  (lexical_declaration
    (variable_declarator
      name: (identifier) @export_name
      value: [(function_expression) (arrow_function)] @export_function)))
"""

# JS/TS ES6 export function query
JS_ES6_EXPORT_FUNCTION_QUERY = """
(export_statement
  [(function_declaration) (generator_function_declaration)] @export_function)
"""

# Query capture names for module system
CAPTURE_FUNC = "func"
CAPTURE_VARIABLE_DECLARATOR = "variable_declarator"
CAPTURE_EXPORTS_OBJ = "exports_obj"
CAPTURE_MODULE_OBJ = "module_obj"
CAPTURE_EXPORTS_PROP = "exports_prop"
CAPTURE_EXPORT_NAME = "export_name"
CAPTURE_EXPORT_FUNCTION = "export_function"
