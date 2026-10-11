"""Go interfaces as method sets (issue #2483).

Nothing in a Go type names the interfaces it implements: a type implements
every interface whose methods it has. The satisfaction is computed here from
the method sets alone, the methods each interface declares plus those of the
interfaces it embeds, against the methods each named type defines. Method
names and parameter and result counts are compared, and a parameter or
result type only where both sides name a predeclared type (`int`,
`string`, `error`) their packages do not declare themselves: any other name
may be an alias of the other. What this finds is a heuristic, not a proof,
and it never overrules a pair go/types proved.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from tree_sitter import Node, Parser

from ... import constants as cs
from ...types_defs import PropertyDict
from ..type_facts import extract_type_facts
from ..utils import safe_decode_text
from . import utils as go_utils

# A method's identity in a method set: (package, name). Go qualifies an
# unexported name with the package declaring it, so `get` of package store
# and `get` of package other are two methods and neither satisfies the
# other's interface; an exported name's package is "" and plays no part.
GoMethodKey = tuple[str, str]


class GoSignature(NamedTuple):
    """A Go method's parameter and result types as declared, one per
    parameter or result (`a, b int` declares two), and the predeclared type
    names its package declares itself (`type int = string`), which there name
    the package's type. `results` is None when the declarations could not be
    read."""

    params: tuple[str, ...]
    results: tuple[str, ...] | None
    shadowed: frozenset[str] = frozenset()


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
    # The parameters as the graph stores them (`param_types`, one per declared
    # name); the results from their declarations, which a trailing comma, an
    # empty `()` or grouped names (`(x, y int)`) leave as the compiler reads.
    facts = extract_type_facts(method, cs.SupportedLanguage.GO)
    return GoSignature(
        tuple(facts.param_types or ()),
        declared_types(method.child_by_field_name(cs.FIELD_RESULT)),
    )


def declared_types(result: Node | None) -> tuple[str, ...]:
    """One type per result a method's result declares: none for no result
    or `()`, the type for a lone one, and one per name in a list."""
    if result is None:
        return ()
    if result.type != cs.TS_GO_PARAMETER_LIST:
        return (_type_text(result),)
    types: list[str] = []
    for declaration in result.named_children:
        if declaration.type not in _GO_DECLARATIONS:
            continue
        names = sum(
            1 for child in declaration.children if child.type == cs.TS_IDENTIFIER
        )
        type_text = _type_text(declaration.child_by_field_name(cs.FIELD_TYPE))
        types.extend([type_text] * max(1, names))
    return tuple(types)


def stored_result_types(result: str, parser: Parser | None) -> tuple[str, ...] | None:
    """`declared_types` of a result read back as the graph's `return_type`
    text, parsed again as Go; None when it cannot be read."""
    if not result:
        return ()
    if result not in _STORED_RESULTS:
        if parser is None:
            return None
        stub = cs.GO_RESULT_STUB.format(result=result)
        root = parser.parse(stub.encode(cs.ENCODING_UTF8)).root_node
        function = next(
            (
                child
                for child in root.named_children
                if child.type == cs.TS_GO_FUNCTION_DECLARATION
            ),
            None,
        )
        _STORED_RESULTS[result] = (
            None
            if function is None or root.has_error
            else declared_types(function.child_by_field_name(cs.FIELD_RESULT))
        )
    return _STORED_RESULTS[result]


def compatible(have: GoSignature, want: GoSignature) -> bool:
    """Whether a method of signature `have` may satisfy one of `want`: false
    only where the two differ for certain, in a count, a variadic parameter,
    or two predeclared types in the same place. Anything else may be the
    same: a name of the package's own may be an alias of any type."""
    return _types_may_match(have.params, want.params, have, want) and (
        have.results is None
        or want.results is None
        or _types_may_match(have.results, want.results, have, want)
    )


def _types_may_match(
    have_types: tuple[str, ...],
    want_types: tuple[str, ...],
    have: GoSignature,
    want: GoSignature,
) -> bool:
    return len(have_types) == len(want_types) and all(
        _may_be_same(a, have.shadowed, b, want.shadowed)
        for a, b in zip(have_types, want_types, strict=True)
    )


def _may_be_same(
    have: str, have_shadowed: frozenset[str], want: str, want_shadowed: frozenset[str]
) -> bool:
    if have.startswith(cs.LANG_ELLIPSIS) != want.startswith(cs.LANG_ELLIPSIS):
        return False
    have_type = _builtin(have.removeprefix(cs.LANG_ELLIPSIS), have_shadowed)
    want_type = _builtin(want.removeprefix(cs.LANG_ELLIPSIS), want_shadowed)
    return have_type is None or want_type is None or have_type == want_type


def _builtin(name: str, shadowed: frozenset[str]) -> str | None:
    # The predeclared type a name certainly is, or None.
    if name in shadowed:
        return None
    name = cs.GO_PREDECLARED_ALIASES.get(name, name)
    return name if name in cs.GO_PREDECLARED_TYPES else None


def in_package(
    spec: GoMethodSpec, shadows: Mapping[str, frozenset[str]]
) -> GoSignature:
    """`spec`'s signature with the predeclared names its package declares."""
    return spec.signature._replace(
        shadowed=shadowed_in(spec.file, spec.package, shadows)
    )


def shadowed_in(
    file: str, package: str, shadows: Mapping[str, frozenset[str]]
) -> frozenset[str]:
    """The predeclared names Go file `file` of `package` (a `package_key`)
    sees its package declare. `shadows` maps a `declaration_scope` to them:
    a package is its directory and `package` clause, so `p_test` declaring
    `error` beside `p` leaves p's `error` the predeclared one, and what p's
    `_test.go` files declare is seen from those files alone."""
    return shadows.get(package, frozenset()) | shadows.get(
        go_utils.declaration_scope(file, package), frozenset()
    )


def _type_text(node: Node | None) -> str:
    return (safe_decode_text(node) or "").strip() if node is not None else ""


# Parsed once per text: the same few result lists recur across a project.
_STORED_RESULTS: dict[str, tuple[str, ...] | None] = {}
_GO_DECLARATIONS = frozenset(
    {cs.TS_GO_PARAMETER_DECLARATION, cs.TS_GO_VARIADIC_PARAMETER_DECLARATION}
)


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
                (method_key(name, ""), (GoSignature(params, results),))
                for name, params, results in cs.GO_BUILTIN_INTERFACE_METHODS[base]
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
