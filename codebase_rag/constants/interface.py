# Constants for the per-file interface fingerprint (issue #2531).
#
# The fingerprint decides whether a re-parsed file's dependents must be
# re-parsed too, so every set below errs towards KEEPING source in the hash:
# a node type left out of a "keep" set can only cost a needless re-parse,
# while one wrongly left out of a "blocker" set can leave a caller stale.
from __future__ import annotations

from enum import StrEnum

from .ast_java import (
    TS_CONSTRUCTOR_DECLARATION,
    TS_JAVA_BLOCK,
    TS_METHOD_DECLARATION,
)
from .ast_js import (
    FIELD_FUNCTION,
    TS_CLASS_BODY,
    TS_JS_ASSIGNMENT_EXPRESSION,
    TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION,
    TS_JS_CATCH_CLAUSE,
    TS_JS_FOR_IN_STATEMENT,
    TS_JS_WITH_STATEMENT,
    TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN,
)
from .ast_nodes import (
    FIELD_CONSTRUCTOR,
    FIELD_LEFT,
    FIELD_NAME,
    FIELD_PARAMETER,
    FIELD_PARAMETERS,
    JS_TS_FUNCTION_NODES,
    TS_CALL_EXPRESSION,
    TS_CLASS_DECLARATION,
    TS_EXPORT_STATEMENT,
    TS_IDENTIFIER,
    TS_MEMBER_EXPRESSION,
    TS_NEW_EXPRESSION,
    TS_STATEMENT_BLOCK,
    TS_VARIABLE_DECLARATOR,
)
from .ast_python import (
    TS_PY_AS_PATTERN_TARGET,
    TS_PY_ASSIGNMENT,
    TS_PY_ATTRIBUTE,
    TS_PY_AUGMENTED_ASSIGNMENT,
    TS_PY_BLOCK,
    TS_PY_CALL,
    TS_PY_DELETE_STATEMENT,
    TS_PY_FOR_STATEMENT,
    TS_PY_FUNCTION_DEFINITION,
    TS_PY_GLOBAL_STATEMENT,
    TS_PY_IMPORT_FROM_STATEMENT,
    TS_PY_IMPORT_STATEMENT,
    TS_PY_NONLOCAL_STATEMENT,
    TS_PY_RETURN_STATEMENT,
    TS_PY_SUBSCRIPT,
    TS_PY_YIELD,
)

# Hashed first, so a change to the algorithm never compares equal to a
# fingerprint an older algorithm stored.
INTERFACE_FINGERPRINT_VERSION = "interface-fingerprint/3"

# Tokens the walk feeds the hash besides node types and leaf text. Each is a
# single byte no grammar uses as a node type, so they cannot be confused
# with one another or with a type.
INTERFACE_FP_OPEN = b"\x01"
INTERFACE_FP_CLOSE = b"\x02"
INTERFACE_FP_LEAF = b"\x03"
INTERFACE_FP_MISSING = b"\x04"
INTERFACE_FP_ELIDED_BODY = b"\x05"
INTERFACE_FP_RETURN = b"\x06"
INTERFACE_FP_CALL = b"\x07"
INTERFACE_FP_ARGUMENT = b"\x08"
INTERFACE_FP_LOCAL = b"\x09"
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

# Calls whose arguments hand a function on to other files: a constructor
# storing it in a field that `obj.handler()` invokes elsewhere (Python's
# callable-field pre-pass), or a function forwarding it to one that calls it
# (the callable-flow pass). Java has neither reader.
INTERFACE_FP_PY_CALLS = frozenset({TS_PY_CALL})
INTERFACE_FP_JS_CALLS = frozenset({TS_CALL_EXPRESSION, TS_NEW_EXPRESSION})
# An argument those readers can resolve to a function: a name or a member
# of one. A literal, a call or an operator names nothing they resolve.
INTERFACE_FP_NAME_ARGUMENTS = frozenset(
    {TS_IDENTIFIER, TS_PY_ATTRIBUTE, TS_MEMBER_EXPRESSION}
)
# The field holding what a call calls (`new X()` holds its constructor).
INTERFACE_FP_CALLEE_FIELDS = (FIELD_FUNCTION, FIELD_CONSTRUCTOR)
# The returns whose value a reader resolves: what a returned call calls, or
# a returned member (a returned name already keeps its body whole).
INTERFACE_FP_PY_RETURNS = frozenset({TS_PY_RETURN_STATEMENT})
# The scope a Python call's arguments are resolved in: a lambda's calls are
# its enclosing def's.
INTERFACE_FP_PY_CALLER_SCOPE = TS_PY_FUNCTION_DEFINITION
# What a JS/TS body binds, by node type: the fields holding the bound names.
_JS_FUNCTION_BINDS = (FIELD_NAME, FIELD_PARAMETERS, FIELD_PARAMETER)
INTERFACE_FP_JS_BINDER_FIELDS: dict[str, tuple[str, ...]] = {
    TS_VARIABLE_DECLARATOR: (FIELD_NAME,),
    TS_JS_FOR_IN_STATEMENT: (FIELD_LEFT,),
    TS_JS_CATCH_CLAUSE: (FIELD_PARAMETER,),
    TS_CLASS_DECLARATION: (FIELD_NAME,),
    **dict.fromkeys(JS_TS_FUNCTION_NODES, _JS_FUNCTION_BINDS),
}
INTERFACE_FP_JS_BOUND_NAMES = frozenset(
    {TS_IDENTIFIER, TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN}
)


class InterfaceFlavor(StrEnum):
    """Which of `interface_fingerprint.py`'s language branches a language
    takes: Python's readers, the JS/TS ones, or neither (Java). The tree
    bridge is handed this per language rather than a language list."""

    PYTHON = "python"
    JS = "js"
    OTHER = "other"
