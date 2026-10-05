# Scope checks an ast-grep pattern cannot express. A rule names one with
# `filter:` in its YAML; the analyzer builds a fresh instance per file and
# keeps only the matches it accepts.
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .. import constants as cs

if TYPE_CHECKING:
    from ast_grep_py import SgNode

type FindingFilter = Callable[[SgNode], bool]

# Each name a scope declares local -> where its first declaration starts: a
# later one adds nothing, the name is local from the first onwards.
type _LuaLocals = dict[str, int]
# A node and its only child share a span, so the kind is part of the key.
type _ScopeKey = tuple[str, int, int]

_FUNCTION_KINDS = frozenset(
    {cs.TS_LUA_FUNCTION_DECLARATION, cs.TS_LUA_FUNCTION_DEFINITION}
)


def _identifiers(node: SgNode | None) -> frozenset[str]:
    # Direct identifier children only: an attribute (`x <const>`) nests its
    # own identifier one level down.
    if node is None:
        return frozenset()
    return frozenset(
        child.text()
        for child in node.children()
        if child.kind() == cs.TS_LUA_IDENTIFIER
    )


def _child(node: SgNode, kind: str) -> SgNode | None:
    return next((c for c in node.children() if c.kind() == kind), None)


def _is_local_function(node: SgNode) -> bool:
    children = node.children()
    return (
        node.kind() == cs.TS_LUA_FUNCTION_DECLARATION
        and bool(children)
        and children[0].kind() == cs.TS_LUA_LOCAL_KEYWORD
    )


def _declared_locals(statement: SgNode) -> frozenset[str]:
    """Names a statement makes local from its end to the end of its block."""
    if statement.kind() == cs.TS_LUA_VARIABLE_DECLARATION:
        # `local a, b` holds the list itself; `local a = 1` an assignment.
        assignment = _child(statement, cs.TS_LUA_ASSIGNMENT_STATEMENT)
        return _identifiers(_child(assignment or statement, cs.TS_LUA_VARIABLE_LIST))
    if _is_local_function(statement):
        return _identifiers(statement)
    return frozenset()


def _bound_inside(scope: SgNode) -> frozenset[str]:
    """Names a function or loop binds for the code nested in it."""
    kind = scope.kind()
    if kind in _FUNCTION_KINDS:
        names = _identifiers(_child(scope, cs.TS_LUA_PARAMETERS))
        if _child(scope, cs.TS_LUA_METHOD_INDEX_EXPRESSION) is not None:
            # `function T:m()` binds an implicit `self`.
            names |= {cs.KEYWORD_SELF}
        if _is_local_function(scope):
            # `local function f` is in scope in its own body, for recursion.
            names |= _identifiers(scope)
        return names
    if kind == cs.TS_LUA_FOR_STATEMENT:
        if (numeric := _child(scope, cs.TS_LUA_FOR_NUMERIC_CLAUSE)) is not None:
            # Only the first identifier is the loop variable; the bounds may
            # be identifiers too.
            first = _child(numeric, cs.TS_LUA_IDENTIFIER)
            return frozenset({first.text()}) if first is not None else frozenset()
        generic = _child(scope, cs.TS_LUA_FOR_GENERIC_CLAUSE)
        if generic is not None:
            return _identifiers(_child(generic, cs.TS_LUA_VARIABLE_LIST))
    return frozenset()


class _LuaScopes:
    """Lexical scope for one file, each scope's locals read once."""

    __slots__ = ("_locals",)

    def __init__(self) -> None:
        self._locals: dict[_ScopeKey, _LuaLocals] = {}

    def assigns_a_global(self, assignment: SgNode) -> bool:
        # Field and index targets (`t.x`, `t[k]`) write an existing table; a
        # bare name leaks unless a local binds it where it is assigned.
        targets = _identifiers(_child(assignment, cs.TS_LUA_VARIABLE_LIST))
        return any(not self._is_local(assignment, name) for name in targets)

    def _is_local(self, node: SgNode, name: str) -> bool:
        current = node
        while (scope := current.parent()) is not None:
            if name in _bound_inside(scope):
                return True
            declared = self._locals_of(scope).get(name)
            if declared is not None and declared < current.range().start.index:
                return True
            current = scope
        return False

    def _locals_of(self, scope: SgNode) -> _LuaLocals:
        span = scope.range()
        key = (scope.kind(), span.start.index, span.end.index)
        if (cached := self._locals.get(key)) is None:
            cached = {}
            for child in scope.children():
                for name in _declared_locals(child):
                    cached.setdefault(name, child.range().start.index)
            self._locals[key] = cached
        return cached


def _lua_global_assignment() -> FindingFilter:
    return _LuaScopes().assigns_a_global


# Filter name in a rule's YAML -> factory of a per-file instance.
FINDING_FILTERS: dict[str, Callable[[], FindingFilter]] = {
    "lua_global_assignment": _lua_global_assignment,
}
