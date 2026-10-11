"""Go interfaces as method sets (issue #2483).

Nothing in a Go type names the interfaces it implements: a type implements
every interface whose methods it has. The satisfaction is computed here from
the method sets alone, the methods each interface declares plus those of the
interfaces it embeds, against the methods each named type defines. Method
names and parameter and result counts are compared, and a parameter or
result type only where both sides spell a predeclared type (`int`,
`string`, `error`): any other name may be an alias of the other, so what
this finds is a heuristic, not a proof.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import PropertyDict
from ..type_facts import extract_type_facts
from ..utils import safe_decode_text

# A method's identity in a method set: (package, name). Go qualifies an
# unexported name with the package declaring it, so `get` of package store
# and `get` of package other are two methods and neither satisfies the
# other's interface; an exported name's package is "" and plays no part.
GoMethodKey = tuple[str, str]


class GoSignature(NamedTuple):
    """A Go method's parameter types and result as written, the graph's
    `param_types` and `return_type` (`""` for no result)."""

    params: tuple[str, ...]
    result: str


# Method identity -> the signature of each declaration of it: one for an
# interface's method, one per build variant for a type's (`T.M` in
# `t_windows.go` and again in `t_linux.go`).
GoMethodSet = dict[GoMethodKey, tuple[GoSignature, ...]]


class GoMethodSpec(NamedTuple):
    """A Go method's signature, the package declaring it, as `package_key`
    spells it, and its repo-relative file."""

    signature: GoSignature
    package: str
    file: str


class GoInterfaceDecl(NamedTuple):
    """The types a Go interface embeds, as written, its package and its
    repo-relative file."""

    embeds: tuple[str, ...]
    package: str
    file: str


def method_key(name: str, package: str) -> GoMethodKey:
    # Go exports a name that starts with an upper-case letter.
    return ("", name) if name[:1].isupper() else (package, name)


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


def signature(method: Node) -> GoSignature:
    # What the graph stores as `param_types` and `return_type`, so a method
    # read back from the graph on an incremental run compares the same way.
    facts = extract_type_facts(method, cs.SupportedLanguage.GO)
    return GoSignature(tuple(facts.param_types or ()), facts.return_type or "")


def compatible(have: GoSignature, want: GoSignature) -> bool:
    """Whether a method of signature `have` may satisfy one of `want`: false
    only where the two differ for certain, in a count, a variadic parameter,
    or a predeclared type spelled in the same place."""
    return (
        len(have.params) == len(want.params)
        and all(map(_may_be_same, have.params, want.params))
        and _results_may_match(have.result, want.result)
    )


def _may_be_same(have: str, want: str) -> bool:
    if have.startswith(cs.LANG_ELLIPSIS) != want.startswith(cs.LANG_ELLIPSIS):
        return False
    have = _predeclared(have.removeprefix(cs.LANG_ELLIPSIS))
    want = _predeclared(want.removeprefix(cs.LANG_ELLIPSIS))
    return have == want or not (
        have in cs.GO_PREDECLARED_TYPES and want in cs.GO_PREDECLARED_TYPES
    )


def _predeclared(type_text: str) -> str:
    return cs.GO_PREDECLARED_ALIASES.get(type_text, type_text)


def _results_may_match(have: str, want: str) -> bool:
    count = _result_count(have)
    if count != _result_count(want):
        return False
    # A lone unparenthesised result is a type; a list is left unread.
    return (
        count != 1
        or have.startswith("(")
        or want.startswith("(")
        or _may_be_same(have, want)
    )


def _result_count(result: str) -> int:
    if not result:
        return 0
    if not result.startswith("("):
        return 1
    # `(n, m int, err error)` holds one result more than its top-level commas.
    depth = 0
    count = 1
    for char in result[1:-1]:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count


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
            methods.update(
                (method_key(name, ""), (GoSignature(params, result),))
                for name, params, result in cs.GO_BUILTIN_INTERFACE_METHODS[base]
            )
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
    interface with a signature that may be the same (`compatible`): in any
    one declaration, so a method each build declares again counts once.

    A type's method set here holds its value- and pointer-receiver methods
    alike. The graph has one node for `T` and `*T`, and the go/types frontend
    records `T IMPLEMENTS I` when either satisfies `I`, so this does too. An
    empty interface (`interface{}`, `any`) is satisfied by every type, which
    says nothing about any of them, so it gets no edge, as with go/types.
    """
    by_method: dict[GoMethodKey, list[str]] = {}
    for type_qn, methods in types.items():
        for key in methods:
            by_method.setdefault(key, []).append(type_qn)
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
    return all(
        satisfies(methods.get(key, ()), wanted) for key, wanted in required.items()
    )


def satisfies(have: tuple[GoSignature, ...], wanted: tuple[GoSignature, ...]) -> bool:
    # `wanted` holds the interface's one declaration of the method.
    return any(compatible(sig, want) for sig in have for want in wanted)
