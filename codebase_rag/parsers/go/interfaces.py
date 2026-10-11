"""Go interfaces as method sets (issue #2483).

Nothing in a Go type names the interfaces it implements: a type implements
every interface whose methods it has. The satisfaction is computed here from
the method sets alone, the methods each interface declares plus those of the
interfaces it embeds, against the methods each named type defines. Only the
method names and parameter counts are compared, never the parameter or
result types, so what this finds is a heuristic, not a proof.
"""

from __future__ import annotations

from collections.abc import Mapping

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import PropertyDict
from ..type_facts import extract_type_facts
from ..utils import safe_decode_text

# Method name -> parameter count.
GoMethodSet = dict[str, int]


def interface_type(type_spec: Node) -> Node | None:
    """The `interface { ... }` a type declaration names, or None."""
    underlying = type_spec.child_by_field_name(cs.FIELD_TYPE)
    if underlying is None or underlying.type != cs.TS_GO_INTERFACE_TYPE:
        return None
    return underlying


def interface_methods(interface: Node) -> list[Node]:
    return [
        child
        for child in interface.named_children
        if child.type == cs.TS_GO_METHOD_ELEM
    ]


def embedded_types(interface: Node) -> list[str]:
    """Every non-method element as written: an embedded interface (`Reader`,
    `io.Writer`), or a type-set term (`~int | ~string`) that makes the
    interface a constraint. Neither resolves to a first-party interface
    unless it is one, which keeps a constraint's method set unknown."""
    return [
        text
        for child in interface.named_children
        if child.type == cs.TS_GO_TYPE_ELEM and (text := safe_decode_text(child))
    ]


def embeds_props(type_spec: Node) -> PropertyDict:
    # Stored even when empty: an interface read back without the property
    # (an older index, an imported one) has a method set nobody recorded.
    interface = interface_type(type_spec)
    if interface is None:
        return {}
    return {cs.KEY_EMBEDS: embedded_types(interface)}


def parameter_count(method: Node) -> int:
    # The count the graph stores as `param_types`, so an interface read back
    # from the graph on an incremental run compares by the same measure.
    param_types = extract_type_facts(method, cs.SupportedLanguage.GO).param_types
    return len(param_types or ())


def full_method_sets(
    own: Mapping[str, GoMethodSet],
    embeds: Mapping[str, tuple[str, ...] | None],
) -> dict[str, GoMethodSet | None]:
    """Each interface's complete method set, its embedded interfaces' methods
    included, or None when an embedded interface could not be resolved: a
    type with the known methods may still lack the unknown ones."""
    return {
        interface: _method_set(interface, own, embeds, frozenset())
        for interface in embeds
    }


def _method_set(
    interface: str,
    own: Mapping[str, GoMethodSet],
    embeds: Mapping[str, tuple[str, ...] | None],
    seen: frozenset[str],
) -> GoMethodSet | None:
    bases = embeds.get(interface)
    if bases is None:
        return None
    methods = dict(own.get(interface, {}))
    # `seen` guards a cycle Go itself rejects, so malformed source cannot
    # loop the walk.
    inner_seen = seen | {interface}
    for base in bases:
        if base in inner_seen:
            continue
        if base in cs.GO_BUILTIN_INTERFACE_METHODS:
            methods.update(cs.GO_BUILTIN_INTERFACE_METHODS[base])
            continue
        inherited = _method_set(base, own, embeds, inner_seen)
        if inherited is None:
            return None
        methods.update(inherited)
    return methods


def implicit_implementations(
    interfaces: Mapping[str, GoMethodSet | None],
    types: Mapping[str, GoMethodSet],
) -> list[tuple[str, str]]:
    """(type, interface) for every type defining each method of the
    interface with the same parameter count.

    A type's method set here holds its value- and pointer-receiver methods
    alike. The graph has one node for `T` and `*T`, and the go/types frontend
    records `T IMPLEMENTS I` when either satisfies `I`, so this does too. An
    empty interface (`interface{}`, `any`) is satisfied by every type, which
    says nothing about any of them, so it gets no edge, as with go/types.
    """
    by_method: dict[str, list[str]] = {}
    for type_qn, methods in types.items():
        for name in methods:
            by_method.setdefault(name, []).append(type_qn)
    pairs: list[tuple[str, str]] = []
    for interface, required in sorted(interfaces.items()):
        if not required:
            continue
        # Only a type defining one of the methods can define them all.
        candidates = by_method.get(min(required), [])
        pairs.extend(
            (type_qn, interface)
            for type_qn in sorted(candidates)
            if type_qn != interface and _covers(types[type_qn], required)
        )
    return pairs


def _covers(methods: GoMethodSet, required: GoMethodSet) -> bool:
    return all(methods.get(name) == count for name, count in required.items())
