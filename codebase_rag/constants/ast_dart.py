# Dart tree-sitter node types.
# The tree-sitter-dart grammar splits a function/method into a `*_signature`
# node and a sibling `function_body` (no single node spans both), and has no
# call-expression node (calls are an identifier plus a following
# `argument_part`/`selector`). cgr captures the signature nodes and derives
# spans from the sibling body.

# Call-site nodes: the grammar has no call-expression node; an invocation is
# whatever selector chain precedes a `selector` holding an `argument_part`
# (`f(x)` = identifier + selector(argument_part); `a.b()` = identifier +
# selector(.b) + selector(argument_part)). A cascade (`obj..m()`) holds its
# argument_part directly inside the `cascade_section`.
TS_DART_SELECTOR = "selector"
TS_DART_ARGUMENT_PART = "argument_part"
# Inside an argument_part: `arguments` holds `argument` wrappers for
# positional values and `named_argument` (label + expression) for named ones.
TS_DART_ARGUMENTS = "arguments"
TS_DART_ARGUMENT = "argument"
TS_DART_NAMED_ARGUMENT = "named_argument"
TS_DART_LABEL = "label"
TS_DART_CASCADE_SECTION = "cascade_section"
TS_DART_CASCADE_SELECTOR = "cascade_selector"
TS_DART_UNCONDITIONAL_ASSIGNABLE_SELECTOR = "unconditional_assignable_selector"
TS_DART_CONDITIONAL_ASSIGNABLE_SELECTOR = "conditional_assignable_selector"
TS_DART_THIS = "this"
TS_DART_SUPER = "super"
TS_DART_IDENTIFIER = "identifier"

DART_CALL_QUERY = """
(selector (argument_part)) @call
(cascade_section (argument_part)) @call
"""

# Declaration shapes for receiver typing: a class field is
# declaration(type_identifier, initialized_identifier_list); a body local is
# initialized_variable_definition (leading type_identifier declared, or
# inferred_type plus construction initializer); a parameter is
# formal_parameter(type_identifier, identifier).
TS_DART_CLASS_BODY = "class_body"
TS_DART_FUNCTION_EXPRESSION = "function_expression"
TS_DART_LOCAL_FUNCTION_DECLARATION = "local_function_declaration"

# Nodes opening their OWN variable scope: the local-type walk must not descend,
# or a nested function's same-named local conflict-drops the outer binding.
DART_NESTED_SCOPE_NODE_TYPES = frozenset(
    {
        TS_DART_FUNCTION_EXPRESSION,
        TS_DART_LOCAL_FUNCTION_DECLARATION,
    }
)
TS_DART_INITIALIZED_IDENTIFIER_LIST = "initialized_identifier_list"
TS_DART_INITIALIZED_IDENTIFIER = "initialized_identifier"
TS_DART_INITIALIZED_VARIABLE_DEFINITION = "initialized_variable_definition"
TS_DART_FORMAL_PARAMETER = "formal_parameter"

# Type/class-like declarations (all captured as @class)
TS_DART_CLASS_DEFINITION = "class_definition"
TS_DART_MIXIN_DECLARATION = "mixin_declaration"
TS_DART_ENUM_DECLARATION = "enum_declaration"
TS_DART_EXTENSION_DECLARATION = "extension_declaration"
TS_DART_EXTENSION_TYPE_DECLARATION = "extension_type_declaration"

# Dart privacy is lexical: a leading underscore marks a library-private
# symbol; every other name is public. Export detection walks the enclosing
# type chain, so a public member of a private type is still unreachable.
DART_PRIVATE_PREFIX = "_"
DART_TYPE_DECLARATION_NODE_TYPES = frozenset(
    {
        TS_DART_CLASS_DEFINITION,
        TS_DART_MIXIN_DECLARATION,
        TS_DART_ENUM_DECLARATION,
        TS_DART_EXTENSION_DECLARATION,
        TS_DART_EXTENSION_TYPE_DECLARATION,
    }
)

# Function/method signature nodes (all captured as @function)
TS_DART_FUNCTION_SIGNATURE = "function_signature"
TS_DART_GETTER_SIGNATURE = "getter_signature"
TS_DART_SETTER_SIGNATURE = "setter_signature"
TS_DART_FACTORY_CONSTRUCTOR_SIGNATURE = "factory_constructor_signature"
TS_DART_CONSTRUCTOR_SIGNATURE = "constructor_signature"
TS_DART_CONSTANT_CONSTRUCTOR_SIGNATURE = "constant_constructor_signature"

# Wrappers whose sibling `function_body` completes a captured signature's span:
# `method_signature` wraps class members, `declaration` wraps constructors; a
# signature under either takes the wrapper's following `function_body` sibling.
TS_DART_METHOD_SIGNATURE = "method_signature"
TS_DART_DECLARATION = "declaration"
TS_DART_FUNCTION_BODY = "function_body"

# `@override`-style metadata: a preceding SIBLING of the (wrapped)
# signature, not a child, so the highlights walk collects it explicitly.
TS_DART_ANNOTATION = "annotation"

# Module and import/directive nodes
TS_DART_PROGRAM = "program"
TS_DART_IMPORT_OR_EXPORT = "import_or_export"
TS_DART_PART_DIRECTIVE = "part_directive"
TS_DART_PART_OF_DIRECTIVE = "part_of_directive"
TS_DART_URI = "uri"
TS_DART_IDENTIFIER_LIST = "dotted_identifier_list"

# Inheritance clause nodes: `extends A`, `with M`, `implements I`, `on T`.
TS_DART_SUPERCLASS = "superclass"
TS_DART_MIXINS = "mixins"
TS_DART_INTERFACES = "interfaces"
TS_DART_TYPE_IDENTIFIER = "type_identifier"

# `import '...' as name;` alias
TS_DART_IMPORT_SPECIFICATION = "import_specification"
DART_IMPORT_ALIAS_KEYWORD = "as"

# URI scheme prefixes distinguishing external (dart:/package:) from
# first-party (relative path) imports.
DART_SCHEME_DART = "dart:"
DART_SCHEME_PACKAGE = "package:"
DART_QUOTE_CHARS = "'\""
DART_EXT = ".dart"

# Node types whose captured signature needs the sibling-body span fix.
DART_SIGNATURE_TYPES = frozenset(
    {
        TS_DART_FUNCTION_SIGNATURE,
        TS_DART_GETTER_SIGNATURE,
        TS_DART_SETTER_SIGNATURE,
        TS_DART_FACTORY_CONSTRUCTOR_SIGNATURE,
        TS_DART_CONSTRUCTOR_SIGNATURE,
        TS_DART_CONSTANT_CONSTRUCTOR_SIGNATURE,
    }
)
DART_SIGNATURE_WRAPPERS = frozenset({TS_DART_METHOD_SIGNATURE, TS_DART_DECLARATION})

# Constructor signatures whose grammar `name` field is the CLASS identifier,
# not the declared name: `C.named` must take its LAST bare identifier or every
# named constructor collapses into a duplicate of the default one.
DART_CONSTRUCTOR_SIGNATURE_TYPES = frozenset(
    {
        TS_DART_CONSTRUCTOR_SIGNATURE,
        TS_DART_CONSTANT_CONSTRUCTOR_SIGNATURE,
        TS_DART_FACTORY_CONSTRUCTOR_SIGNATURE,
    }
)
