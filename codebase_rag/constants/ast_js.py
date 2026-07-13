# (H) JavaScript/TypeScript tree-sitter node types, queries, and captures.

from .ast_nodes import (
    TS_CALL_EXPRESSION,
    TS_IDENTIFIER,
    TS_MEMBER_EXPRESSION,
    TS_NEW_EXPRESSION,
)

# (H) Locals query patterns for JS/TS
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

# (H) Receivers that address the MODULE itself in CommonJS code
# (H) (`exports.render()`, `module.exports.x()`, prototype-pattern `this`):
# (H) only these may bind a dotted call to a same-module free function; an
# (H) ordinary identifier receiver (`view.render()`) is an instance call.
JS_MODULE_RECEIVERS = frozenset({"exports", "module", "this"})
# (H) `this.` receiver prefix of a call name; a prototype-assigned function
# (H) (`Date.prototype.strftime`) dispatches such calls to a sibling method of
# (H) the same prototype target before the module-receiver fallback applies.
JS_THIS_CALL_PREFIX = "this."

JS_TS_PARENT_REF_TYPES = (TS_IDENTIFIER, TS_MEMBER_EXPRESSION)
# (H) JSX element nodes that carry a component name (javascript and tsx
# (H) grammars share these); the closing element repeats the name and must not
# (H) double-emit.
TS_JSX_SELF_CLOSING_ELEMENT = "jsx_self_closing_element"
TS_JSX_OPENING_ELEMENT = "jsx_opening_element"
# (H) The `{...}` wrapper around an expression in a JSX attribute value or child
# (H) (`onClick={handleLogout}`, `onClick={() => x()}`); its inner expression can
# (H) hand a function to the element as a prop.
TS_JSX_EXPRESSION = "jsx_expression"

# (H) TS "cast" wrappers that are transparent for reference resolution: `x as T`,
# (H) `x satisfies T`, and the non-null assertion `x!`. Their first named child is
# (H) the wrapped value, so unwrapping reaches the real referenced expression
# (H) (`export const persist = persistImpl as unknown as Persist`).
TS_AS_EXPRESSION = "as_expression"
TS_SATISFIES_EXPRESSION = "satisfies_expression"
TS_NON_NULL_EXPRESSION = "non_null_expression"
TS_CAST_WRAPPER_TYPES = frozenset(
    {TS_AS_EXPRESSION, TS_SATISFIES_EXPRESSION, TS_NON_NULL_EXPRESSION}
)

# (H) JS/TS ingest node types
TS_PAIR = "pair"
TS_OBJECT = "object"
TS_ARRAY = "array"

# (H) When a variable_declarator's value is one of these, the variable binds the
# (H) call/construction RESULT, not a function -- so an arrow found inside its
# (H) arguments (`const m = useMutation({fn: () => {}})`) must not inherit the
# (H) variable's name. Object-literal / arrow values are not here, so arrows nested
# (H) directly under an object bound to a const still take the object's name.
JS_CALL_RESULT_VALUE_TYPES = frozenset({TS_CALL_EXPRESSION, TS_NEW_EXPRESSION})
TS_FUNCTION_EXPRESSION = "function_expression"
TS_ARROW_FUNCTION = "arrow_function"
TS_REQUIRED_PARAMETER = "required_parameter"
TS_OPTIONAL_PARAMETER = "optional_parameter"
TS_ASSIGNMENT_PATTERN = "assignment_pattern"
TS_FIELD_PATTERN = "pattern"
TS_FIELD_PARAMETER = "parameter"
TS_MODULE = "module"
TS_CLASS_BODY = "class_body"

TS_PROPERTY_IDENTIFIER = "property_identifier"

# (H) JS prototype property keywords
JS_PROTOTYPE_KEYWORD = "prototype"
JS_OBJECT_NAME = "Object"
JS_CREATE_METHOD = "create"

# (H) JS/TS ingest query capture names
CAPTURE_CHILD_CLASS = "child_class"
CAPTURE_PARENT_CLASS = "parent_class"
CAPTURE_CONSTRUCTOR_NAME = "constructor_name"
CAPTURE_PROTOTYPE_KEYWORD = "prototype_keyword"
CAPTURE_METHOD_NAME = "method_name"
CAPTURE_METHOD_FUNCTION = "method_function"
CAPTURE_MEMBER_EXPR = "member_expr"
CAPTURE_FUNCTION_EXPR = "function_expr"
CAPTURE_ARROW_FUNCTION = "arrow_function"

# (H) JS prototype inheritance query
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

# (H) JS prototype method assignment query
JS_PROTOTYPE_METHOD_QUERY = """
(assignment_expression
  left: (member_expression
    object: (member_expression
      object: (identifier) @constructor_name
      property: (property_identifier) @prototype_keyword (#eq? @prototype_keyword "prototype"))
    property: (property_identifier) @method_name)
  right: (function_expression) @method_function)
"""

# (H) JS object method query
JS_OBJECT_METHOD_QUERY = """
(pair
  key: (property_identifier) @method_name
  value: (function_expression) @method_function)
"""

# (H) JS method definition query
JS_METHOD_DEF_QUERY = """
(object
  (method_definition
    name: (property_identifier) @method_name) @method_function)
"""

# (H) JS object arrow function query
JS_OBJECT_ARROW_QUERY = """
(object
  (pair
    (property_identifier) @method_name
    (arrow_function) @arrow_function))
"""

# (H) JS assignment arrow function query
JS_ASSIGNMENT_ARROW_QUERY = """
(assignment_expression
  (member_expression) @member_expr
  (arrow_function) @arrow_function)
"""

# (H) JS assignment function expression query
JS_ASSIGNMENT_FUNCTION_QUERY = """
(assignment_expression
  (member_expression) @member_expr
  (function_expression) @function_expr)
"""

# (H) JS/TS module system node types
TS_OBJECT_PATTERN = "object_pattern"
TS_ARRAY_PATTERN = "array_pattern"
TS_REST_PATTERN = "rest_pattern"
TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN = "shorthand_property_identifier_pattern"
TS_PAIR_PATTERN = "pair_pattern"
# (H) `process.env.X` is a member_expression; `process.env['X']` a subscript, used
# (H) to detect environment-variable reads (issue #714 process.env follow-up).
TS_SUBSCRIPT_EXPRESSION = "subscript_expression"
TS_FIELD_INDEX = "index"
TS_FUNCTION_DECLARATION = "function_declaration"
TS_GENERATOR_FUNCTION_DECLARATION = "generator_function_declaration"

# (H) Tree-sitter field names for module system
FIELD_FUNCTION = "function"
FIELD_KEY = "key"

# (H) JS/TS module system keywords
JS_REQUIRE_KEYWORD = "require"
JS_EXPORTS_KEYWORD = "exports"
JS_MODULE_KEYWORD = "module"

# (H) JS/TS export type descriptions
JS_EXPORT_TYPE_COMMONJS = "CommonJS Export"
JS_EXPORT_TYPE_COMMONJS_MODULE = "CommonJS Module Export"
JS_EXPORT_TYPE_ES6_FUNCTION = "ES6 Export Function"
JS_EXPORT_TYPE_ES6_FUNCTION_DECL = "ES6 Export Function Declaration"

# (H) JS/TS CommonJS destructure query
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

# (H) JS/TS CommonJS exports function query
JS_COMMONJS_EXPORTS_FUNCTION_QUERY = """
(assignment_expression
  left: (member_expression
    object: (identifier) @exports_obj
    property: (property_identifier) @export_name)
  right: [(function_expression) (arrow_function)] @export_function)
"""

# (H) JS/TS CommonJS module.exports query
JS_COMMONJS_MODULE_EXPORTS_QUERY = """
(assignment_expression
  left: (member_expression
    object: (member_expression
      object: (identifier) @module_obj
      property: (property_identifier) @exports_prop)
    property: (property_identifier) @export_name)
  right: [(function_expression) (arrow_function)] @export_function)
"""

# (H) JS/TS ES6 export const query
JS_ES6_EXPORT_CONST_QUERY = """
(export_statement
  (lexical_declaration
    (variable_declarator
      name: (identifier) @export_name
      value: [(function_expression) (arrow_function)] @export_function)))
"""

# (H) JS/TS ES6 export function query
JS_ES6_EXPORT_FUNCTION_QUERY = """
(export_statement
  [(function_declaration) (generator_function_declaration)] @export_function)
"""

# (H) Query capture names for module system
CAPTURE_FUNC = "func"
CAPTURE_VARIABLE_DECLARATOR = "variable_declarator"
CAPTURE_EXPORTS_OBJ = "exports_obj"
CAPTURE_MODULE_OBJ = "module_obj"
CAPTURE_EXPORTS_PROP = "exports_prop"
CAPTURE_EXPORT_NAME = "export_name"
CAPTURE_EXPORT_FUNCTION = "export_function"
