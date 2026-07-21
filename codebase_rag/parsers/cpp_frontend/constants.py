from __future__ import annotations

from ... import constants as cs

# libclang CursorKind members are registered dynamically (not static class
# attributes), so they are matched by the stable NAME string that
# `cursor.kind.name` yields at runtime, never via `ci.CursorKind.CLASS_DECL`
# (which trips ty's unresolved-attribute). Same approach as the eval oracle
# (evals/oracles/cpp_oracle.py).

KIND_NAMESPACE = "NAMESPACE"
KIND_DESTRUCTOR = "DESTRUCTOR"
KIND_BASE_SPECIFIER = "CXX_BASE_SPECIFIER"
KIND_TRANSLATION_UNIT = "TRANSLATION_UNIT"
KIND_CALL_EXPR = "CALL_EXPR"
KIND_MACRO_DEFINITION = "MACRO_DEFINITION"
KIND_MACRO_INSTANTIATION = "MACRO_INSTANTIATION"

# class/struct/union and their templated forms -> a Class node (cgr collapses
# struct/class to Class, matching parsers/cpp + the oracle).
CLASS_KIND_NAMES: frozenset[str] = frozenset(
    {"CLASS_DECL", "STRUCT_DECL", "CLASS_TEMPLATE"}
)
# free functions and function templates -> a Function node, UNLESS their
# semantic parent is a class (a templated method is a FUNCTION_TEMPLATE whose
# parent is the class), in which case they are Methods.
FUNCTION_KIND_NAMES: frozenset[str] = frozenset({"FUNCTION_DECL", "FUNCTION_TEMPLATE"})
# members -> a Method node.
METHOD_KIND_NAMES: frozenset[str] = frozenset(
    {"CXX_METHOD", "CONSTRUCTOR", "DESTRUCTOR", "CONVERSION_FUNCTION"}
)
# `using Alias = T;` (TYPE_ALIAS_DECL) and `typedef T Alias;` (TYPEDEF_DECL)
# -> a Type node, matching how the tree-sitter path maps C++ alias/typedef
# declarations (TS_TYPE_ALIAS_DECLARATION) and Go/Rust type decls.
TYPE_KIND_NAMES: frozenset[str] = frozenset({"TYPE_ALIAS_DECL", "TYPEDEF_DECL"})

TOKEN_LPAREN = "("
TOKEN_RPAREN = ")"

LABEL_MODULE = cs.NodeLabel.MODULE.value
LABEL_CLASS = cs.NodeLabel.CLASS.value
LABEL_FUNCTION = cs.NodeLabel.FUNCTION.value
LABEL_METHOD = cs.NodeLabel.METHOD.value
LABEL_TYPE = cs.NodeLabel.TYPE.value
