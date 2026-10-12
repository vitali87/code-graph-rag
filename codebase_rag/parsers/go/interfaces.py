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

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import NamedTuple

from tree_sitter import Node, Parser

from ... import constants as cs
from ...types_defs import PropertyDict
from ..type_facts import extract_type_facts
from ..utils import safe_decode_text
from . import utils as go_utils
from .builds import ALWAYS, Expr, all_of, any_of, decide

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
    return tuple(_result_types(result))


def _result_types(result: Node | None) -> list[str]:
    if result is None:
        return []
    if result.type != cs.TS_GO_PARAMETER_LIST:
        return [_type_text(result)]
    types: list[str] = []
    for declaration in result.named_children:
        if declaration.type not in _GO_DECLARATIONS:
            continue
        names = sum(
            1 for child in declaration.children if child.type == cs.TS_IDENTIFIER
        )
        type_text = _type_text(declaration.child_by_field_name(cs.FIELD_TYPE))
        types.extend([type_text] * max(1, names))
    return types


def stored_result_types(result: str, parser: Parser | None) -> tuple[str, ...] | None:
    """`declared_types` of a result read back as the graph's `return_type`
    text, parsed again as Go; None when it cannot be read."""
    if not result:
        return declared_types(None)
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


# An embedded name stands for the interface each build declares by it
# (`i_linux.go`, `i_windows.go`), so an interface has one method set per
# combination one build compiles; past this many its method set is unknown.
_MAX_METHOD_SET_ALTERNATIVES = 64

# An embedded name's meanings: each interface qn (or predeclared name) it
# may be, with the builds in which it is that one.
GoEmbedAlternatives = tuple[tuple[str, Expr], ...]


class GoBuildSet(NamedTuple):
    """An interface's complete method set in the builds `builds` admits."""

    methods: GoMethodSet
    builds: Expr


class GoFullMethodSets(NamedTuple):
    # Per interface, its method sets, or None when unknown.
    sets: dict[str, tuple[GoBuildSet, ...] | None]
    # The unknown ones whose method set differs between builds in more ways
    # than are told apart: no one build speaks for another.
    varying: frozenset[str]


class _VaryingError(Exception):
    """A method set past the cap on alternatives, or resting on a decision
    about builds that cannot be made."""


def full_method_sets(
    own: Mapping[str, GoMethodSet],
    embeds: Mapping[str, tuple[GoEmbedAlternatives, ...] | None],
    files: Mapping[str, Expr],
) -> GoFullMethodSets:
    """Each interface's complete method set in each set of builds, its
    embedded interfaces' methods included, or None when unknown: an
    embedded interface could not be resolved (a type with the known methods
    may still lack the unknown ones), or the sets vary between builds past
    what this tells apart (`varying`). `embeds` holds, per embedded name,
    what it is in which builds: one meaning for most names, one per build
    for a name each build declares again. `files` is each interface's own
    file's constraint. Meanings no one build compiles together are never
    combined: `A` on Linux with `B` on Windows is no method set of any
    build."""
    walk = _MethodSetWalk(own, embeds, files)
    sets: dict[str, tuple[GoBuildSet, ...] | None] = {}
    varying: set[str] = set()
    for interface in embeds:
        try:
            sets[interface] = walk.sets(interface)
        except _VaryingError:
            sets[interface] = None
            varying.add(interface)
    return GoFullMethodSets(sets, frozenset(varying))


_VARYING = "varying"


class _MethodSetWalk:
    """One pass's method sets, each interface's computed once (#3360 review).
    Without the memo an interface embedded on many paths was walked again
    on each: two interfaces per level, each embedding both of the level
    below, doubled the work per level. The walk keeps its own stack: one
    frame per embed made a chain of a few hundred interfaces (`I0` embeds
    `I1`, which embeds `I2`, ...) overflow Python's."""

    def __init__(
        self,
        own: Mapping[str, GoMethodSet],
        embeds: Mapping[str, tuple[GoEmbedAlternatives, ...] | None],
        files: Mapping[str, Expr],
    ) -> None:
        self.own = own
        self.embeds = embeds
        self.files = files
        self.done: dict[str, tuple[GoBuildSet, ...] | None | str] = {}

    def sets(self, interface: str) -> tuple[GoBuildSet, ...] | None:
        if interface not in self.done:
            self._finish(interface)
        found = self.done[interface]
        if isinstance(found, str):
            raise _VaryingError
        return found

    def _finish(self, start: str) -> None:
        # Post-order: an interface's sets once each interface it embeds has
        # its own. `walking` holds the path from `start`: an embed of one of
        # them is a cycle Go itself rejects, skipped, so malformed source
        # cannot loop the walk (what a cycle yields is then any answer,
        # never a hang).
        walking = {start}
        stack = [(start, self._bases(start))]
        while stack:
            interface, bases = stack[-1]
            base = next(
                (b for b in bases if b not in self.done and b not in walking), None
            )
            if base is not None:
                walking.add(base)
                stack.append((base, self._bases(base)))
                continue
            stack.pop()
            self.done[interface] = self._walk(interface, walking)
            walking.discard(interface)

    def _bases(self, interface: str) -> Iterator[str]:
        for alternatives in self.embeds.get(interface) or ():
            for base, _builds in alternatives:
                if base not in cs.GO_BUILTIN_INTERFACE_METHODS:
                    yield base

    def _walk(
        self, interface: str, seen: set[str]
    ) -> tuple[GoBuildSet, ...] | None | str:
        written = self.embeds.get(interface)
        if written is None:
            return None
        sets = [
            GoBuildSet(
                dict(self.own.get(interface, {})), self.files.get(interface, ALWAYS)
            )
        ]
        # An embed that cannot be resolved leaves the set unknown, but the
        # rest are still combined: one varying past the cap makes it
        # varying.
        resolved = True
        try:
            for alternatives in written:
                options = self._embedded(alternatives, seen)
                if options is None:
                    resolved = False
                elif options:
                    sets = _combined(sets, options)
        except _VaryingError:
            return _VARYING
        return tuple(sets) if resolved else None

    def _embedded(
        self, alternatives: GoEmbedAlternatives, seen: set[str]
    ) -> list[GoBuildSet] | None:
        # Each embedded interface's sets, which the walk finished first.
        options: list[GoBuildSet] = []
        resolved = True
        for base, builds in alternatives:
            if base in seen:
                continue
            if base in cs.GO_BUILTIN_INTERFACE_METHODS:
                options.append(GoBuildSet(_builtin_methods(base), builds))
                continue
            inherited = self.done[base]
            if isinstance(inherited, str):
                raise _VaryingError
            if inherited is None:
                resolved = False
                continue
            options.extend(
                GoBuildSet(found.methods, all_of((found.builds, builds)))
                for found in inherited
            )
        return options if resolved else None


def _combined(sets: list[GoBuildSet], options: list[GoBuildSet]) -> list[GoBuildSet]:
    # Each set with each option one build compiles together. Where that
    # cannot be told, or past the cap, the method set is unknown.
    merged = [_merged(base, option) for base in sets for option in options]
    decisions = [decide((found.builds,)) for found in merged]
    if None in decisions:
        raise _VaryingError
    distinct = _distinct(
        [found for found, holds in zip(merged, decisions, strict=True) if holds]
    )
    if distinct is None:
        raise _VaryingError
    return distinct


def _merged(base: GoBuildSet, option: GoBuildSet) -> GoBuildSet:
    return GoBuildSet(
        {**base.methods, **option.methods}, all_of((base.builds, option.builds))
    )


def _builtin_methods(name: str) -> GoMethodSet:
    return {
        method_key(method, ""): (GoSignature(params, results),)
        for method, params, results in cs.GO_BUILTIN_INTERFACE_METHODS[name]
    }


def _distinct(sets: list[GoBuildSet]) -> list[GoBuildSet] | None:
    # One method set reached in several builds is one alternative. Past the
    # cap the method set is unknown: no reduced set stands for them, since a
    # type with only the methods all agree on implements it in no build.
    merged: dict[
        tuple[tuple[GoMethodKey, tuple[GoSignature, ...]], ...], GoBuildSet
    ] = {}
    for found in sets:
        key = tuple(sorted(found.methods.items()))
        earlier = merged.get(key)
        merged[key] = (
            found
            if earlier is None
            else earlier._replace(builds=any_of((earlier.builds, found.builds)))
        )
    if len(merged) > _MAX_METHOD_SET_ALTERNATIVES:
        return None
    return list(merged.values())


def implicit_implementations(
    interfaces: Mapping[str, tuple[GoBuildSet, ...] | None],
    types: Mapping[str, GoMethodSet],
    in_some_build: Callable[[str, GoBuildSet], bool],
) -> list[tuple[str, str]]:
    """(type, interface) for every type defining each method of the
    interface in one of its sets of builds, with a signature that may be the
    same (`compatible`), and that `in_some_build` finds one build holding
    together with the declarations that do. A pair that holds in one build
    is a pair: renaming either side must rename the other, or that build no
    longer compiles.

    A type's method set here holds its value- and pointer-receiver methods
    alike. The graph has one node for `T` and `*T`, and the go/types frontend
    records `T IMPLEMENTS I` when either satisfies `I`, so this does too. An
    empty interface (`interface{}`, `any`) is satisfied by every type, which
    says nothing about any of them, so it gets no edge, as with go/types.
    """
    pairs: dict[tuple[str, str], None] = {}
    for type_qn, interface, found in covering(interfaces, types):
        if (type_qn, interface) not in pairs and in_some_build(type_qn, found):
            pairs[(type_qn, interface)] = None
    return list(pairs)


def embedded_method_links(
    own: Mapping[str, Mapping[GoMethodKey, str]],
    bases: Mapping[str, Sequence[str]],
    files: Mapping[str, Expr],
) -> list[tuple[str, str]]:
    """Links that join the declarations one interface's method set holds of
    one method in one build (#3360 review): a method an interface declares
    again over an embed (`B interface { A; M() }`), and the declarations two
    embeds bring together (`D interface { B; C }`, both declaring `M`). Go
    requires them identical, so they are one method: renamed alone, one
    leaves the set asking for both names, and a value of the embedded
    interface no longer converts to the embedding one, even with no type
    implementing either. `own` holds each interface's method qn per method;
    `bases`, every interface each one may embed (one per build for a name
    each build declares again); `files`, each one's file constraint.

    Every link joins two declarations one build compiles together, and the
    links join each such set of declarations, as a tree: a rename follows
    them transitively, so no pair is linked twice, nor checked once joined.
    One pass, embedded interfaces first, carries per method one declaration
    for each set of builds (the others are joined to it), and only for the
    methods declared more than once: a chain costs a step per link, however
    many declarations it gathers."""
    counts = Counter(key for declared in own.values() for key in declared)
    joined = _Joined()
    nearest: dict[str, _Declarations] = {}
    for interface in _embedded_first(bases, own.keys() | bases.keys()):
        scope = files.get(interface, ALWAYS)
        declared = {
            key: qn for key, qn in (own.get(interface) or {}).items() if counts[key] > 1
        }
        inherited = _merged_declarations(
            [
                _within(nearest[base], scope)
                for base in bases.get(interface, ())
                if base in nearest
            ],
            declared,
            joined,
        )
        for key, method_qn in declared.items():
            for other, _ in inherited.get(key, ()):
                joined.join(method_qn, other)
        nearest[interface] = (
            {**inherited, **{key: ((qn, scope),) for key, qn in declared.items()}}
            if declared
            else inherited
        )
    return joined.links


# Per method, a declaration of it for each set of builds it holds in.
_Declarations = Mapping[GoMethodKey, tuple[tuple[str, Expr], ...]]


class _Joined:
    """The declarations joined so far, as a union-find, and the links that
    joined them: one per join, never one between two already joined."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []

    def root(self, qn: str) -> str:
        while (parent := self.parent.get(qn, qn)) != qn:
            self.parent[qn] = self.parent.get(parent, parent)
            qn = parent
        return qn

    def join(self, first: str, second: str) -> None:
        roots = (self.root(first), self.root(second))
        if roots[0] != roots[1]:
            self.parent[roots[0]] = roots[1]
            self.links.append((first, second))


def _embedded_first(
    bases: Mapping[str, Sequence[str]], interfaces: Iterable[str]
) -> Iterator[str]:
    # Post-order on a stack of its own (a chain of thousands is valid Go).
    # An embed of an interface on the current path is a cycle Go rejects,
    # and is skipped.
    done: set[str] = set()
    for start in sorted(interfaces):
        if start in done:
            continue
        walking = {start}
        stack = [(start, iter(bases.get(start, ())))]
        while stack:
            interface, pending = stack[-1]
            base = next(
                (b for b in pending if b not in done and b not in walking), None
            )
            if base is not None:
                walking.add(base)
                stack.append((base, iter(bases.get(base, ()))))
                continue
            stack.pop()
            walking.discard(interface)
            done.add(interface)
            yield interface


def _within(found: _Declarations, scope: Expr) -> _Declarations:
    # What an embed holds, seen from an interface whose file only `scope`'s
    # builds compile: a declaration no such build holds drops out.
    if scope == ALWAYS:
        return found
    kept: dict[GoMethodKey, tuple[tuple[str, Expr], ...]] = {}
    for key, declarations in found.items():
        held = tuple(
            (qn, builds)
            for qn, builds in ((qn, all_of((scope, w))) for qn, w in declarations)
            if decide((builds,)) is not False
        )
        if held:
            kept[key] = held
    return kept


def _merged_declarations(
    found: list[_Declarations], declared: Mapping[GoMethodKey, str], joined: _Joined
) -> _Declarations:
    # What every embed holds, one declaration per method and set of builds.
    # Where the interface does not declare a method itself, its declarations
    # under different builds are joined across them.
    if len(found) < 2:
        return found[0] if found else {}
    merged = _by_builds(found, joined)
    for key, kept in merged.items():
        if key not in declared:
            _join_across_builds(kept, joined)
    return {
        key: tuple((qn, builds) for builds, qn in kept.items())
        for key, kept in merged.items()
    }


def _by_builds(
    found: list[_Declarations], joined: _Joined
) -> dict[GoMethodKey, dict[Expr, str]]:
    # Per method, the first declaration under each set of builds; another
    # the same builds hold is joined to it.
    merged: dict[GoMethodKey, dict[Expr, str]] = {}
    for each in found:
        for key, declarations in each.items():
            kept = merged.setdefault(key, {})
            for qn, builds in declarations:
                first = kept.setdefault(builds, qn)
                if first != qn and decide((builds,)) is not False:
                    joined.join(first, qn)
    return merged


def _join_across_builds(kept: dict[Expr, str], joined: _Joined) -> None:
    # Two declarations under different builds are joined if one build may
    # hold both and nothing joined them yet. An undecided constraint joins:
    # a rename that misses a declaration breaks a build, one more only
    # renames it too.
    held = list(kept.items())
    for index, (builds, qn) in enumerate(held):
        for before, earlier in held[:index]:
            if joined.root(earlier) != joined.root(qn) and (
                decide((before, builds)) is not False
            ):
                joined.join(earlier, qn)


def covering(
    interfaces: Mapping[str, tuple[GoBuildSet, ...] | None],
    types: Mapping[str, GoMethodSet],
) -> Iterator[tuple[str, str, GoBuildSet]]:
    """(type, interface, method set) for each of an interface's known method
    sets that a type's methods cover by name and signature, whatever the
    builds: the pairs a build may hold, by interface, then type."""
    by_method: dict[GoMethodKey, list[str]] = {}
    for type_qn, methods in types.items():
        for key in methods:
            by_method.setdefault(key, []).append(type_qn)
    for interface, builds in sorted(interfaces.items()):
        required = [found for found in builds or () if found.methods]
        for type_qn in _candidates(required, by_method):
            if type_qn == interface:
                continue
            yield from (
                (type_qn, interface, found)
                for found in required
                if _covers(types[type_qn], found.methods)
            )


def _candidates(
    required: list[GoBuildSet], by_method: Mapping[GoMethodKey, list[str]]
) -> list[str]:
    # Only a type defining one of a build's methods can define them all.
    return sorted(
        {
            type_qn
            for found in required
            for type_qn in by_method.get(min(found.methods), [])
        }
    )


def _covers(methods: GoMethodSet, required: GoMethodSet) -> bool:
    return all(
        satisfies(methods.get(key, ()), wanted) for key, wanted in required.items()
    )


def satisfies(have: tuple[GoSignature, ...], wanted: tuple[GoSignature, ...]) -> bool:
    # `wanted` holds the interface's one declaration of the method.
    return any(compatible(sig, want) for sig in have for want in wanted)
