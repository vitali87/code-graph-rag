# (H) Dart tree-sitter node types.
# (H) The tree-sitter-dart grammar splits a function/method into a
# (H) `*_signature` node and a sibling `function_body` (no single node spans
# (H) both), and has no dedicated call-expression node (calls are an identifier
# (H) plus a following `argument_part`/`selector`). cgr therefore captures the
# (H) signature nodes for structural support and derives full spans from the
# (H) sibling body; a precise CALLS graph is out of scope for this grammar.

# (H) Type/class-like declarations (all captured as @class)
TS_DART_CLASS_DEFINITION = "class_definition"
TS_DART_MIXIN_DECLARATION = "mixin_declaration"
TS_DART_ENUM_DECLARATION = "enum_declaration"
TS_DART_EXTENSION_DECLARATION = "extension_declaration"
TS_DART_EXTENSION_TYPE_DECLARATION = "extension_type_declaration"

# (H) Function/method signature nodes (all captured as @function)
TS_DART_FUNCTION_SIGNATURE = "function_signature"
TS_DART_GETTER_SIGNATURE = "getter_signature"
TS_DART_SETTER_SIGNATURE = "setter_signature"
TS_DART_FACTORY_CONSTRUCTOR_SIGNATURE = "factory_constructor_signature"
TS_DART_CONSTRUCTOR_SIGNATURE = "constructor_signature"
TS_DART_CONSTANT_CONSTRUCTOR_SIGNATURE = "constant_constructor_signature"

# (H) Wrappers whose sibling `function_body` completes a captured signature's
# (H) span: `method_signature` wraps class members, `declaration` wraps
# (H) constructors; a signature under either takes the wrapper's following
# (H) `function_body` sibling as its body.
TS_DART_METHOD_SIGNATURE = "method_signature"
TS_DART_DECLARATION = "declaration"
TS_DART_FUNCTION_BODY = "function_body"

# (H) `@override`-style metadata: a preceding SIBLING of the (wrapped)
# (H) signature, not a child, so the highlights walk collects it explicitly.
TS_DART_ANNOTATION = "annotation"

# (H) Module and import/directive nodes
TS_DART_PROGRAM = "program"
TS_DART_IMPORT_OR_EXPORT = "import_or_export"
TS_DART_PART_DIRECTIVE = "part_directive"
TS_DART_PART_OF_DIRECTIVE = "part_of_directive"
TS_DART_URI = "uri"
TS_DART_IDENTIFIER_LIST = "dotted_identifier_list"

# (H) Inheritance clause nodes: `extends A`, `with M`, `implements I`, `on T`.
TS_DART_SUPERCLASS = "superclass"
TS_DART_MIXINS = "mixins"
TS_DART_INTERFACES = "interfaces"
TS_DART_TYPE_IDENTIFIER = "type_identifier"

# (H) `import '...' as name;` alias
TS_DART_IMPORT_SPECIFICATION = "import_specification"
DART_IMPORT_ALIAS_KEYWORD = "as"

# (H) URI scheme prefixes distinguishing external (dart:/package:) from
# (H) first-party (relative path) imports.
DART_SCHEME_DART = "dart:"
DART_SCHEME_PACKAGE = "package:"
DART_QUOTE_CHARS = "'\""
DART_EXT = ".dart"

# (H) Node types whose captured signature needs the sibling-body span fix.
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
