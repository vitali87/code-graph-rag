# (H) Python tree-sitter node types and language constants.

# (H) Python tree-sitter node types for type inference
TS_PY_IDENTIFIER = "identifier"
TS_PY_TYPED_PARAMETER = "typed_parameter"
TS_PY_TYPED_DEFAULT_PARAMETER = "typed_default_parameter"
TS_PY_ATTRIBUTE = "attribute"
TS_PY_CALL = "call"
TS_PY_LIST = "list"
TS_PY_DICTIONARY = "dictionary"
TS_PY_PAIR = "pair"
TS_PY_SET = "set"
TS_PY_TUPLE = "tuple"
TS_PY_PARENTHESIZED_EXPRESSION = "parenthesized_expression"
TS_PY_EXPRESSION_LIST = "expression_list"
TS_PY_LIST_COMPREHENSION = "list_comprehension"
TS_PY_FOR_STATEMENT = "for_statement"
TS_PY_FOR_IN_CLAUSE = "for_in_clause"
TS_PY_ASSIGNMENT = "assignment"
PY_ASSIGNMENT_QUERY = "(assignment) @assignment"
PY_RETURN_QUERY = "(return_statement) @return_stmt"
TS_PY_CLASS_DEFINITION = "class_definition"
TS_PY_BLOCK = "block"
TS_PY_FUNCTION_DEFINITION = "function_definition"
TS_PY_LAMBDA = "lambda"
TS_PY_RETURN_STATEMENT = "return_statement"
TS_PY_RETURN = "return"
TS_PY_KEYWORD = "keyword"
TS_PY_MODULE = "module"
TS_PY_IMPORT_STATEMENT = "import_statement"
TS_PY_IMPORT_FROM_STATEMENT = "import_from_statement"
TS_PY_WITH_STATEMENT = "with_statement"
TS_PY_AS_PATTERN = "as_pattern"
TS_PY_AS_PATTERN_TARGET = "as_pattern_target"
TS_PY_EXPRESSION_STATEMENT = "expression_statement"
TS_PY_STRING = "string"
TS_PY_DECORATED_DEFINITION = "decorated_definition"
TS_PY_DECORATOR = "decorator"
TS_PY_KEYWORD_ARGUMENT = "keyword_argument"
TS_PY_DEFAULT_PARAMETER = "default_parameter"
TS_PY_LIST_SPLAT_PATTERN = "list_splat_pattern"
TS_PY_DICTIONARY_SPLAT_PATTERN = "dictionary_splat_pattern"
TS_PY_SUBSCRIPT = "subscript"
TS_PY_COMPARISON_OPERATOR = "comparison_operator"
TS_FIELD_OPERATORS = "operators"
TS_PY_IF_STATEMENT = "if_statement"
TS_PY_TRY_STATEMENT = "try_statement"
# (H) Match statement: arms are exclusive; an UNGUARDED `case _` (empty
# (H) case_pattern) always matches, removing the implicit no-match path.
TS_PY_MATCH_STATEMENT = "match_statement"
TS_PY_CASE_CLAUSE = "case_clause"
TS_PY_CASE_PATTERN = "case_pattern"
TS_PY_FIELD_GUARD = "guard"
FIELD_SUBJECT = "subject"
# (H) A bare name in a case pattern parses as dotted_name with ONE identifier
# (H) and is a CAPTURE (irrefutable); multi-part dotted names are value
# (H) patterns that compare.
TS_PY_DOTTED_NAME = "dotted_name"
# (H) `a | b` case alternatives; the bare `_` alternative is an ANONYMOUS
# (H) node, invisible to named_children.
TS_PY_UNION_PATTERN = "union_pattern"
TS_PY_WILDCARD_NODE = "_"
TS_PY_WHILE_STATEMENT = "while_statement"
TS_PY_ELIF_CLAUSE = "elif_clause"
TS_PY_ELSE_CLAUSE = "else_clause"
TS_PY_EXCEPT_CLAUSE = "except_clause"
TS_PY_FINALLY_CLAUSE = "finally_clause"
TS_PY_CONDITIONAL_EXPRESSION = "conditional_expression"
TS_PY_BOOLEAN_OPERATOR = "boolean_operator"
TS_PY_NOT_OPERATOR = "not_operator"
TS_FIELD_CONDITION = "condition"
TS_FIELD_CONSEQUENCE = "consequence"
TS_FIELD_ARGUMENT = "argument"

# (H) Python operator syntax dispatches to dunder methods at runtime; these names
# (H) let the call extractor synthesize the implied <operand>.__dunder__ call.
PY_OP_IN = "in"
PY_BUILTIN_LEN = "len"
PY_BUILTIN_GETATTR = "getattr"
TS_PY_STRING_CONTENT = "string_content"
PY_DUNDER_GETITEM = "__getitem__"
PY_DUNDER_SETITEM = "__setitem__"
PY_DUNDER_CONTAINS = "__contains__"
PY_DUNDER_LEN = "__len__"
PY_DUNDER_BOOL = "__bool__"
# (H) Operands with these characters are not simple attribute/name chains (calls,
# (H) nested subscripts, whitespace), so the operator-dispatch synthesizer skips them.
PY_OPERAND_REJECT_CHARS = "()[]{}\n\t "
# (H) Optional annotation handling: X | None names a single concrete class.
PY_UNION_SEPARATOR = "|"
PY_NONE = "None"
# (H) `-> Self` names the enclosing class, not a class called Self.
PY_ANNOTATION_SELF = "Self"

# (H) Python keyword identifiers
PY_KEYWORD_SELF = "self"
PY_KEYWORD_CLS = "cls"
# (H) Visibility by naming convention: a leading underscore marks a private
# (H) symbol, while a dunder (__x__) is public API invoked by the runtime.
PY_NAME_UNDERSCORE = "_"
PY_NAME_DUNDER = "__"
# (H) typing.Protocol base name and the conventional XxxProtocol class suffix
# (H) used to map a Protocol to its concrete implementer.
PY_PROTOCOL = "Protocol"
PY_METHOD_INIT = "__init__"
DECORATOR_AT = "@"
PROPERTY_DECORATORS: frozenset[str] = frozenset({"property", "cached_property"})
ABSTRACT_DECORATORS: frozenset[str] = frozenset({"abstractmethod", "abstractproperty"})

# (H) Eager builtins that invoke a callable argument synchronously within the
# (H) caller's own stack frame; a function passed to one is invoked there, so the
# (H) trace attributes the call to the enclosing function (no Python frame exists
# (H) for the builtin). Lazy higher-order builtins (map/filter) are excluded:
# (H) they defer invocation until the result is consumed, which may be elsewhere.
HIGHER_ORDER_BUILTINS: frozenset[str] = frozenset({"sorted", "min", "max", "reduce"})

# (H) Python attribute prefixes
PY_SELF_PREFIX = "self."
PY_CLS_PREFIX = "cls."

# (H) Python type inference patterns
PY_VAR_PATTERN_ALL = "all_"
PY_VAR_SUFFIX_PLURAL = "s"
PY_CLASS_REPOSITORY = "Repository"
PY_MODELS_BASE_PATH = ".models.base."
PY_METHOD_CREATE = "create"

# (H) Type inference scoring
PY_SCORE_EXACT_MATCH = 100
PY_SCORE_SUFFIX_MATCH = 90
PY_SCORE_CONTAINS_BASE = 80

# (H) Type inference defaults
TYPE_INFERENCE_LIST = "list"
TYPE_INFERENCE_BASE_MODEL = "BaseModel"

# (H) Recursion guard attributes
ATTR_TYPE_INFERENCE_IN_PROGRESS = "_type_inference_in_progress"
GUARD_INHERITED_METHOD = "_inherited_method_guard"
