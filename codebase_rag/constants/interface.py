# Constants for the per-file interface fingerprint (issue #2531).
#
# The fingerprint decides whether a re-parsed file's dependents must be
# re-parsed too, so every set below errs towards KEEPING source in the hash:
# a node type left out of a "keep" set can only cost a needless re-parse,
# while one wrongly left out of a "blocker" set can leave a caller stale.
from __future__ import annotations

from .ast_java import (
    TS_CONSTRUCTOR_DECLARATION,
    TS_JAVA_BLOCK,
    TS_METHOD_DECLARATION,
)
from .ast_js import (
    TS_CLASS_BODY,
    TS_JS_ASSIGNMENT_EXPRESSION,
    TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION,
    TS_JS_WITH_STATEMENT,
)
from .ast_nodes import (
    JS_TS_FUNCTION_NODES,
    TS_EXPORT_STATEMENT,
    TS_STATEMENT_BLOCK,
)
from .ast_python import (
    TS_PY_AS_PATTERN_TARGET,
    TS_PY_ASSIGNMENT,
    TS_PY_ATTRIBUTE,
    TS_PY_AUGMENTED_ASSIGNMENT,
    TS_PY_BLOCK,
    TS_PY_DELETE_STATEMENT,
    TS_PY_FOR_STATEMENT,
    TS_PY_FUNCTION_DEFINITION,
    TS_PY_GLOBAL_STATEMENT,
    TS_PY_IMPORT_FROM_STATEMENT,
    TS_PY_IMPORT_STATEMENT,
    TS_PY_NONLOCAL_STATEMENT,
    TS_PY_SUBSCRIPT,
    TS_PY_YIELD,
)

# Hashed first, so a change to the algorithm never compares equal to a
# fingerprint an older algorithm stored.
INTERFACE_FINGERPRINT_VERSION = "interface-fingerprint/1"

# Tokens the walk feeds the hash besides node types and leaf text. Each is a
# single byte no grammar uses as a node type, so they cannot be confused
# with one another or with a type.
INTERFACE_FP_OPEN = b"\x01"
INTERFACE_FP_CLOSE = b"\x02"
INTERFACE_FP_LEAF = b"\x03"
INTERFACE_FP_MISSING = b"\x04"
INTERFACE_FP_ELIDED_BODY = b"\x05"
INTERFACE_FP_RETURN = b"\x06"
INTERFACE_FP_SEPARATOR = b"\x00"
INTERFACE_FP_LENGTH_BYTES = 4
INTERFACE_FP_BYTE_ORDER = "big"

# Comments never reach the hash: no reader in cgr derives a definition, a
# type or a binding of ANOTHER file from a comment (a JSDoc `@param` types
# the documented function's own parameters, inside its own file).
INTERFACE_FP_COMMENT_SUBSTRING = "comment"

# Function nodes whose body may be replaced by a projection, per language.
INTERFACE_FP_PY_FUNCTIONS = frozenset({TS_PY_FUNCTION_DEFINITION})
INTERFACE_FP_JAVA_FUNCTIONS = frozenset(
    {
        TS_METHOD_DECLARATION,
        TS_CONSTRUCTOR_DECLARATION,
        "compact_constructor_declaration",
    }
)
INTERFACE_FP_JS_FUNCTIONS = frozenset(JS_TS_FUNCTION_NODES)

# The body node types a projection may replace. An arrow function whose body
# is an expression has no block: that expression IS its return value.
INTERFACE_FP_PY_BLOCKS = frozenset({TS_PY_BLOCK})
INTERFACE_FP_JAVA_BLOCKS = frozenset({TS_JAVA_BLOCK, "constructor_body"})
INTERFACE_FP_JS_BLOCKS = frozenset({TS_STATEMENT_BLOCK})

# Definitions a body can hold besides the language's function and class
# node types: a Java anonymous class is only a `class_body` under a
# `new T() { ... }`, and its methods are registered under the method that
# writes it.
INTERFACE_FP_JAVA_EXTRA_DEFINITIONS = frozenset({TS_CLASS_BODY})

# Node types that make a body part of the interface wherever they appear in
# it. Python: a `global`/`nonlocal` or an import inside a function binds
# module state, `del` can drop an attribute, and a generator's element type
# is not modelled here at all. JS/TS: any assignment may attach a member to
# a prototype, a class, `this` or `module.exports`, and a dynamic import or
# a `with` block changes what names resolve to.
INTERFACE_FP_PY_BLOCKERS = frozenset(
    {
        TS_PY_GLOBAL_STATEMENT,
        TS_PY_NONLOCAL_STATEMENT,
        TS_PY_IMPORT_STATEMENT,
        TS_PY_IMPORT_FROM_STATEMENT,
        "future_import_statement",
        TS_PY_DELETE_STATEMENT,
        TS_PY_YIELD,
    }
)
INTERFACE_FP_JS_BLOCKERS = frozenset(
    {
        TS_JS_ASSIGNMENT_EXPRESSION,
        TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION,
        "yield_expression",
        "import",
        TS_JS_WITH_STATEMENT,
        TS_EXPORT_STATEMENT,
    }
)

# Python statements whose binding target, when it holds an attribute or a
# subscript, writes state another file reads (`self.x = Widget()` types
# `obj.x` for every caller).
INTERFACE_FP_PY_TARGET_HOLDERS = frozenset(
    {TS_PY_ASSIGNMENT, TS_PY_AUGMENTED_ASSIGNMENT, TS_PY_FOR_STATEMENT}
)
INTERFACE_FP_PY_TARGET_WRAPPERS = frozenset({TS_PY_AS_PATTERN_TARGET})
INTERFACE_FP_PY_MEMBER_TARGETS = frozenset({TS_PY_ATTRIBUTE, TS_PY_SUBSCRIPT})

# A Python dunder method is read by protocol-driven inference (`__init__`
# types the instance attributes, `__enter__` the `with` target), so its
# body always counts. A JS constructor is kept whole for the same reason.
INTERFACE_FP_DUNDER = "__"
INTERFACE_FP_JS_KEPT_FUNCTIONS = frozenset({"constructor"})
