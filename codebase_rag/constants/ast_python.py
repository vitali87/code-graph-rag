# Python tree-sitter node types and language constants.

# Python tree-sitter node types for type inference
TS_PY_IDENTIFIER = "identifier"
TS_PY_TYPED_PARAMETER = "typed_parameter"
TS_PY_TYPED_DEFAULT_PARAMETER = "typed_default_parameter"
TS_PY_ATTRIBUTE = "attribute"
TS_PY_FIELD_ATTRIBUTE = "attribute"
TS_PY_CALL = "call"
TS_PY_LIST = "list"
TS_PY_DICTIONARY = "dictionary"
TS_PY_PAIR = "pair"
TS_PY_SET = "set"
TS_PY_TUPLE = "tuple"
TS_PY_PARENTHESIZED_EXPRESSION = "parenthesized_expression"
TS_PY_EXPRESSION_LIST = "expression_list"
TS_PY_LIST_COMPREHENSION = "list_comprehension"
TS_PY_SET_COMPREHENSION = "set_comprehension"
TS_PY_DICTIONARY_COMPREHENSION = "dictionary_comprehension"
TS_PY_GENERATOR_EXPRESSION = "generator_expression"
TS_PY_FOR_STATEMENT = "for_statement"
TS_PY_FOR_IN_CLAUSE = "for_in_clause"
TS_PY_ASSIGNMENT = "assignment"
# Unpacking targets: `a, b = ...`, `(a, b) = ...`, `[a, b] = ...`.
TS_PY_PATTERN_LIST = "pattern_list"
TS_PY_TUPLE_PATTERN = "tuple_pattern"
TS_PY_LIST_PATTERN = "list_pattern"
PY_UNPACKING_TARGET_TYPES = frozenset(
    {TS_PY_PATTERN_LIST, TS_PY_TUPLE_PATTERN, TS_PY_LIST_PATTERN}
)
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
TS_PY_INTERPOLATION = "interpolation"
TS_PY_DECORATED_DEFINITION = "decorated_definition"
TS_PY_DECORATOR = "decorator"
TS_PY_KEYWORD_ARGUMENT = "keyword_argument"
TS_PY_LIST_SPLAT = "list_splat"
TS_PY_DICTIONARY_SPLAT = "dictionary_splat"
TS_PY_DEFAULT_PARAMETER = "default_parameter"
TS_PY_LIST_SPLAT_PATTERN = "list_splat_pattern"
TS_PY_DICTIONARY_SPLAT_PATTERN = "dictionary_splat_pattern"
TS_PY_POSITIONAL_SEPARATOR = "positional_separator"
TS_PY_KEYWORD_SEPARATOR = "keyword_separator"
TS_PY_SUBSCRIPT = "subscript"
# The `subscript` node's index field (`os.environ["K"]` -> the `"K"` string).
TS_PY_FIELD_SUBSCRIPT = "subscript"
TS_PY_AUGMENTED_ASSIGNMENT = "augmented_assignment"
# Walrus operator `(os := value)` -- a `name` field binds the identifier.
TS_PY_NAMED_EXPRESSION = "named_expression"
TS_PY_COMPARISON_OPERATOR = "comparison_operator"
TS_FIELD_OPERATORS = "operators"
TS_PY_IF_STATEMENT = "if_statement"
TS_PY_TRY_STATEMENT = "try_statement"
TS_PY_GLOBAL_STATEMENT = "global_statement"
TS_PY_NONLOCAL_STATEMENT = "nonlocal_statement"
# Match statement: arms are exclusive; an UNGUARDED `case _` (empty
# case_pattern) always matches, removing the implicit no-match path.
TS_PY_MATCH_STATEMENT = "match_statement"
TS_PY_CASE_CLAUSE = "case_clause"
TS_PY_CASE_PATTERN = "case_pattern"
TS_PY_FIELD_GUARD = "guard"
FIELD_SUBJECT = "subject"
# A bare name in a case pattern parses as dotted_name with ONE identifier
# and is a CAPTURE (irrefutable); multi-part dotted names are value
# patterns that compare.
TS_PY_DOTTED_NAME = "dotted_name"
# `a | b` case alternatives; the bare `_` alternative is an ANONYMOUS
# node, invisible to named_children.
TS_PY_UNION_PATTERN = "union_pattern"
# `Foo(x=<pattern>)`, `*rest` / `**rest` inside a case pattern.
TS_PY_KEYWORD_PATTERN = "keyword_pattern"
TS_PY_SPLAT_PATTERN = "splat_pattern"
# `import x as y` / `from m import x as y`; the alias is a local binding.
TS_PY_ALIASED_IMPORT = "aliased_import"
TS_PY_WILDCARD_NODE = "_"
TS_PY_WHILE_STATEMENT = "while_statement"
TS_PY_ELIF_CLAUSE = "elif_clause"
TS_PY_ELSE_CLAUSE = "else_clause"
TS_PY_EXCEPT_CLAUSE = "except_clause"
TS_PY_FINALLY_CLAUSE = "finally_clause"
TS_PY_CONDITIONAL_EXPRESSION = "conditional_expression"
TS_PY_BOOLEAN_OPERATOR = "boolean_operator"
TS_PY_BINARY_OPERATOR = "binary_operator"
TS_PY_NOT_OPERATOR = "not_operator"
TS_FIELD_CONDITION = "condition"
TS_FIELD_CONSEQUENCE = "consequence"
TS_FIELD_ARGUMENT = "argument"
TS_PY_UNARY_OPERATOR = "unary_operator"
TS_PY_CONCATENATED_STRING = "concatenated_string"
# Read positions the binding scan of `python_name_binding_sites` recognises.
TS_PY_SLICE = "slice"
TS_PY_ASSERT_STATEMENT = "assert_statement"
TS_PY_RAISE_STATEMENT = "raise_statement"
TS_PY_YIELD = "yield"
TS_PY_WITH_ITEM = "with_item"
TS_PY_IF_CLAUSE = "if_clause"
TS_PY_TYPE = "type"
TS_PY_AWAIT = "await"
TS_PY_LIST_SPLAT = "list_splat"
TS_PY_DICTIONARY_SPLAT = "dictionary_splat"
TS_PY_GENERATOR_EXPRESSION = "generator_expression"
TS_PY_SET_COMPREHENSION = "set_comprehension"
TS_PY_DICTIONARY_COMPREHENSION = "dictionary_comprehension"
# The `interpolation` node's value field (`f"{t!r:>8}"` -> `t`).
TS_PY_FIELD_EXPRESSION = "expression"
# A dynamic format spec (`f"{t:{fill}>{width}}"`): the interpolation's
# `format_specifier` field holds a `format_expression` per nested `{...}`,
# each with its own `expression` field and, nested once more, its own
# `format_specifier`.
TS_PY_FIELD_FORMAT_SPECIFIER = "format_specifier"
TS_PY_FORMAT_SPECIFIER = "format_specifier"
TS_PY_FORMAT_EXPRESSION = "format_expression"

# The FLOWS_TO walk carries taint through any call it cannot see into, but
# these results reveal nothing of their input's content (a size, a truth
# value, an identity or type, a one-way digest), so taint stops at them
# (issue #2588). Escaping and quoting are deliberately absent: FLOWS_TO
# tracks where a value came from, and an escaped secret written to stdout
# still leaks the secret.
PY_TAINT_CLEARING_CALLS = frozenset(
    {
        "len",
        "bool",
        "id",
        "hash",
        "type",
        "isinstance",
        "issubclass",
        "callable",
        "hasattr",
        "any",
        "all",
        "hashlib.new",
        "hashlib.md5",
        "hashlib.sha1",
        "hashlib.sha224",
        "hashlib.sha256",
        "hashlib.sha384",
        "hashlib.sha512",
        "hashlib.sha3_256",
        "hashlib.sha3_512",
        "hashlib.blake2b",
        "hashlib.blake2s",
        "hashlib.pbkdf2_hmac",
        "hashlib.scrypt",
        "hmac.new",
        "hmac.digest",
        "hmac.compare_digest",
        "secrets.compare_digest",
    }
)
# str predicates and position/count lookups return a bool or an int, so on a
# receiver known to be a string they clear both the receiver's and the
# arguments' taint. On any other object (`client.find(secret)`) the method is
# that object's own, and its result may carry the object's data or the
# argument, so nothing clears.
PY_TAINT_CLEARING_METHODS = frozenset(
    {
        "startswith",
        "endswith",
        "isalnum",
        "isalpha",
        "isascii",
        "isdecimal",
        "isdigit",
        "isidentifier",
        "islower",
        "isnumeric",
        "isprintable",
        "isspace",
        "istitle",
        "isupper",
        "count",
        "find",
        "rfind",
        "index",
        "rindex",
    }
)
# The annotation that makes a parameter or local a string.
PY_TYPE_STR = "str"
PY_BUILTINS_MODULE = "builtins"
# Builtins whose result is always a string, so a name bound to one is a
# string receiver for the lookups above, as long as nothing the call can see
# rebinds the name (keyed by import-normalised name).
PY_STR_RESULT_CALLS = frozenset(
    {
        "str",
        "repr",
        "input",
        "builtins.str",
        "builtins.repr",
        "builtins.input",
    }
)
# `os.getenv(key, default)` / `os.environ.get(key, default)`: the default is
# returned when the variable is unset.
PY_ENV_DEFAULT_KEYWORD = "default"
# str (and bytes) methods that return a str or bytes when their receiver is
# one: `t.strip().lower()` on a string is still a string.
PY_STR_RESULT_METHODS = frozenset(
    {
        "strip",
        "lstrip",
        "rstrip",
        "lower",
        "upper",
        "casefold",
        "title",
        "capitalize",
        "swapcase",
        "replace",
        "removeprefix",
        "removesuffix",
        "format",
        "join",
        "zfill",
        "center",
        "ljust",
        "rjust",
        "encode",
        "decode",
    }
)

# Python operator syntax dispatches to dunder methods at runtime; these names
# let the call extractor synthesise the implied <operand>.__dunder__ call.
PY_OP_IN = "in"
PY_OP_AND = "and"
# `left <op> right` dispatches to `left.__dunder__`, so the expression's type is
# whatever that method returns -- which an overloaded operator can make a
# different class from either operand (`Factory / Config -> Product`).
PY_BINARY_OPERATOR_DUNDERS: dict[str, str] = {
    "+": "__add__",
    "-": "__sub__",
    "*": "__mul__",
    "/": "__truediv__",
    "//": "__floordiv__",
    "%": "__mod__",
    "@": "__matmul__",
    "**": "__pow__",
    "|": "__or__",
    "&": "__and__",
    "^": "__xor__",
    "<<": "__lshift__",
    ">>": "__rshift__",
}
PY_BUILTIN_LEN = "len"
PY_BUILTIN_GETATTR = "getattr"
TS_PY_STRING_CONTENT = "string_content"
PY_DUNDER_GETITEM = "__getitem__"
PY_DUNDER_SETITEM = "__setitem__"
PY_DUNDER_CONTAINS = "__contains__"
PY_DUNDER_LEN = "__len__"
PY_DUNDER_BOOL = "__bool__"
# Operands with these characters are not simple attribute/name chains (calls,
# nested subscripts, whitespace), so the operator-dispatch synthesiser skips them.
PY_OPERAND_REJECT_CHARS = "()[]{}\n\t "
# Optional annotation handling: X | None names a single concrete class.
PY_UNION_SEPARATOR = "|"
PY_NONE = "None"
# `-> Self` names the enclosing class, not a class called Self.
PY_ANNOTATION_SELF = "Self"
# Homogeneous-container return annotations (`-> list[Widget]`): the container
# spelling is irrelevant downstream, only "iterable of element" survives, in
# the canonical `list[<element>]` marker consumed by loop-variable inference.
PY_GENERIC_CONTAINER_PATTERN = (
    r"^(?:typing\.)?(?P<name>list|List|set|Set|frozenset|FrozenSet|tuple|Tuple|"
    r"Sequence|Collection|Iterable|Iterator|Generator|AsyncGenerator|"
    r"AsyncIterator|AsyncIterable)\[(?P<inner>.+)\]$"
)
PY_TUPLE_CONTAINERS = frozenset({"tuple", "Tuple"})
# Yield type first; Generator[Y, S, R] takes up to three parameters while
# AsyncGenerator[Y, S] takes up to two.
PY_GENERATOR_ARG_LIMITS = {"Generator": 3, "AsyncGenerator": 2}
PY_ELLIPSIS = "..."
PY_OPTIONAL_PATTERN = r"^(?:typing\.)?Optional\[(?P<inner>.+)\]$"
PY_LIST_TYPE_PREFIX = "list["
PY_LIST_TYPE_FORMAT = "list[{element}]"

PY_KEYWORD_SELF = "self"
PY_KEYWORD_CLS = "cls"
# Visibility by naming convention: a leading underscore marks a private
# symbol, while a dunder (__x__) is public API invoked by the runtime.
PY_NAME_UNDERSCORE = "_"
PY_NAME_DUNDER = "__"
# typing.Protocol base name and the conventional XxxProtocol class suffix
# used to map a Protocol to its concrete implementer.
PY_PROTOCOL = "Protocol"
PY_METHOD_INIT = "__init__"
DECORATOR_AT = "@"
PROPERTY_DECORATORS: frozenset[str] = frozenset({"property", "cached_property"})
ABSTRACT_DECORATORS: frozenset[str] = frozenset({"abstractmethod", "abstractproperty"})
# A static method takes no receiver: a first parameter named `self` is explicit.
STATIC_DECORATORS: frozenset[str] = frozenset({"staticmethod"})

# Eager builtins that invoke a callable argument synchronously in the caller's
# stack frame, so the trace attributes the call to the enclosing function (no
# Python frame exists for the builtin). Lazy higher-order builtins (map/filter)
# are excluded: they defer invocation until the result is consumed, elsewhere.
HIGHER_ORDER_BUILTINS: frozenset[str] = frozenset({"sorted", "min", "max", "reduce"})

PY_SELF_PREFIX = "self."
PY_CLS_PREFIX = "cls."

PY_VAR_PATTERN_ALL = "all_"
PY_VAR_SUFFIX_PLURAL = "s"
PY_CLASS_REPOSITORY = "Repository"
PY_MODELS_BASE_PATH = ".models.base."
PY_METHOD_CREATE = "create"

PY_SCORE_EXACT_MATCH = 100
PY_SCORE_SUFFIX_MATCH = 90
PY_SCORE_CONTAINS_BASE = 80

TYPE_INFERENCE_LIST = "list"
TYPE_INFERENCE_BASE_MODEL = "BaseModel"

ATTR_TYPE_INFERENCE_IN_PROGRESS = "_type_inference_in_progress"
GUARD_INHERITED_METHOD = "_inherited_method_guard"
GUARD_NESTED_JAVA_CALL = "_nested_java_call_guard"
# Java type inference and call resolution recurse once per step of a call
# chain and once per nesting level of call arguments; generated code reaches
# hundreds of either. Past this depth a step is left untyped rather than
# letting a RecursionError discard the whole method's variable types.
GUARD_JAVA_INFERENCE_DEPTH = "_java_inference_depth_guard"
JAVA_MAX_INFERENCE_DEPTH = 64
