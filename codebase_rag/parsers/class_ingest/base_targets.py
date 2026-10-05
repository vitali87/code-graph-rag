"""Which declaration a written base name binds to (issue #2534).

A base in an `extends`/`implements`/base list names a TYPE. The registry
also holds methods, properties and functions under the same simple name,
so every tier that searches it by name has to be limited to type kinds,
or a `bool NotificationHandler { get; set; }` in another namespace answers
for `class NotificationHandler<T>`.

C# additionally fixes WHERE a base name is looked up: the namespace the
class is declared in and each enclosing one (enclosing types included),
innermost first, then what the file's `using` directives bring in. Only
when none of those declares the name does the caller fall back to a
project-wide name match, which it then marks heuristic.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol, NodeType
from ..csharp import utils as csharp_utils

_TYPE_KINDS = frozenset(
    {
        NodeType.CLASS,
        NodeType.INTERFACE,
        NodeType.ENUM,
        NodeType.TYPE,
        NodeType.UNION,
    }
)
# An ES5 constructor function is a legal `extends` target
# (`class A extends Base` over `function Base() {}`); nowhere else is a
# function a base.
_TYPE_OR_CONSTRUCTOR_KINDS = _TYPE_KINDS | {NodeType.FUNCTION}


def base_target_kinds(language: cs.SupportedLanguage | None) -> frozenset[NodeType]:
    if language in cs.JS_TS_LANGUAGES:
        return _TYPE_OR_CONSTRUCTOR_KINDS
    return _TYPE_KINDS


class CSharpTypeIndex(NamedTuple):
    registry: FunctionRegistryTrieProtocol
    namespaced_qns: Mapping[str, set[str]]
    generic_arity: Mapping[str, int]
    partial_groups: dict[str, list[str]]


class _ScopeHit(NamedTuple):
    qn: str | None
    declared: bool


def resolve_csharp_scoped_base(
    written_ref: str,
    child_qn: str,
    child_declared: str | None,
    import_map: Mapping[str, str],
    index: CSharpTypeIndex,
) -> str | None:
    """The type a C# base resolves to through scope and `using`, or None.

    `written_ref` is the base as written, generic-free, with its written
    type-argument count in CLR style (`NotificationHandler`1`). None means
    no scope declares it unambiguously; the caller's name search decides.
    """
    name, arity = csharp_utils.split_type_ref(written_ref)
    for scope in _enclosing_scopes(child_declared):
        hit = _declared_type(_join(scope, name), arity, child_qn, index)
        if hit.declared:
            # The innermost scope that declares the name is the one C#
            # binds; an ambiguity there is not resolved from further out.
            return hit.qn
    return _through_usings(name, arity, child_qn, import_map, index)


def _enclosing_scopes(child_declared: str | None) -> list[str]:
    # `Acme.Tests.Outer.Pong` -> Acme.Tests.Outer, Acme.Tests, Acme, global.
    if not child_declared:
        return [""]
    parts = child_declared.split(cs.SEPARATOR_DOT)[:-1]
    return [cs.SEPARATOR_DOT.join(parts[:end]) for end in range(len(parts), -1, -1)]


def _join(scope: str, name: str) -> str:
    return f"{scope}{cs.SEPARATOR_DOT}{name}" if scope else name


def _declared_type(
    declared_name: str, arity: int, child_qn: str, index: CSharpTypeIndex
) -> _ScopeHit:
    # The child itself is skipped: `class Foo : Foo<object>` names a
    # different type of the same declared name.
    carriers = {
        qn
        for qn in index.namespaced_qns.get(declared_name, ())
        if qn != child_qn and index.registry.get(qn) in _TYPE_KINDS
    }
    if not carriers:
        return _ScopeHit(None, False)
    if len(carriers) > 1:
        # `Base<T>` and `Base<T1, T2>` share a declared name; the written
        # arity picks one. Arity is only known for types parsed this run,
        # so it narrows but never empties the set.
        carriers = {
            qn for qn in carriers if index.generic_arity.get(qn, 0) == arity
        } or carriers
    return _ScopeHit(csharp_utils.unique_carrier(carriers, index.partial_groups), True)


def _through_usings(
    name: str,
    arity: int,
    child_qn: str,
    import_map: Mapping[str, str],
    index: CSharpTypeIndex,
) -> str | None:
    head, dot, rest = name.partition(cs.SEPARATOR_DOT)
    target = import_map.get(head)
    if target is not None and target != head:
        # A using alias (`using NH = Acme.NotificationHandler<string>;`)
        # carries its own type arguments when it names the whole base.
        target_name, target_arity = csharp_utils.split_type_ref(
            csharp_utils.annotate_type_ref(target)
        )
        expanded = f"{target_name}{dot}{rest}" if dot else target_name
        hit = _declared_type(expanded, arity if dot else target_arity, child_qn, index)
        if hit.declared:
            return hit.qn
    # A using namespace contributes only when it is the ONE import that
    # declares the name; two would be a compile-time ambiguity.
    found: set[str] = set()
    for imported in set(import_map.values()):
        hit = _declared_type(_join(imported, name), arity, child_qn, index)
        if hit.declared:
            if hit.qn is None:
                return None
            found.add(hit.qn)
    return found.pop() if len(found) == 1 else None
