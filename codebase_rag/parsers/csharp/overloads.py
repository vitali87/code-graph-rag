"""Ranking same-arity C# overloads by the types of a call's arguments.

Arity alone cannot choose between `Validate(T)`, `Validate(Ctx<T>)` and
`Validate(Ctx)`, so a member call used to take whichever came first (issue
#2619). Each argument the syntax can type is matched against each candidate's
parameter, after the receiver's closed type arguments are substituted for the
declaring type's parameters (`T := Person` on `PersonValidator :
Inline<Person>`). C#'s betterness order decides between the fits: an
identity beats a type parameter, which beats a conversion. A candidate the
argument provably cannot bind drops out; what is left undecided stays a set,
for the caller to fan out over.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from enum import IntEnum
from typing import NamedTuple, Protocol

from ... import constants as cs
from ...types_defs import CSharpGenericShape
from ...utils import qn_markers
from .utils import (
    generic_arity_of_type_text,
    leaf_type_segment,
    signature_type_name,
    split_type_ref,
    strip_generic_arguments,
)

_TYPE_IDENTIFIER = re.compile(cs.CSHARP_TYPE_IDENTIFIER_PATTERN)
_TYPE_SUFFIX = re.compile(cs.CSHARP_TYPE_SUFFIX_PATTERN)


class ArgumentType(NamedTuple):
    """What the syntax says about one argument: a literal's C# type, or a
    type's written name, its arity and, where the source spells them
    (`new Ctx<Person>()`), its type arguments."""

    literal: str | None
    name: str | None
    arity: int
    arguments: tuple[str, ...] | None


class Fit(IntEnum):
    # Ordered as C#'s betterness rules rank conversions. UNKNOWN is the same
    # for every candidate at that position, so it never tells two apart.
    UNKNOWN = 0
    OTHER = 1
    CONVERTIBLE = 2
    OPEN = 3
    EXACT = 4


# (written type name, its type-argument count) -> the first-party type qn it
# names in one declaring file, None when it names none there.
type Resolver = Callable[[str, int], str | None]


class Scope(NamedTuple):
    """One file's view of the type names written in a declaration there:
    `resolve` finds the first-party type a name names in that file, and
    `type_parameters` are the names in scope at the declaration that are
    type parameters (its own, its type's and every enclosing type's), which
    name no type and stay as written (PR 2814 review)."""

    resolve: Resolver
    type_parameters: frozenset[str] = frozenset()


def with_type_parameters(scope: Scope | None, names: Collection[str]) -> Scope | None:
    """`scope` with a member's own type parameters in scope too."""
    if scope is None or not names:
        return scope
    return Scope(scope.resolve, scope.type_parameters | frozenset(names))


class TypeScopes(NamedTuple):
    """Where each side of a member comparison resolves the types it writes:
    the given member's file and the wanted member's (PR 2814 review)."""

    given: Scope
    wanted: Scope


class TypeJudge(Protocol):
    """How one candidate's parameters relate to a call's arguments, each side
    resolved in its own file (the argument in the caller's, the parameter in
    the method's) and with closed type arguments compared alias-blind. Each
    answer is True, False, or None when it cannot be told; never a guess."""

    def identical(
        self, argument: ArgumentType, parameter: str, open_names: Collection[str]
    ) -> bool | None:
        """Whether the argument's type IS the parameter's."""
        ...

    def converts(
        self, argument: ArgumentType, parameter: str, open_names: Collection[str]
    ) -> bool | None:
        """Whether the argument's type converts implicitly to the parameter's."""
        ...


def plain_type_name(type_name: str) -> str:
    # `System.Int32?` -> `Int32`: the spelling the literal tables list.
    name = type_name.strip().removesuffix(cs.CSHARP_NULLABLE_SUFFIX)
    return name.removeprefix(cs.CSHARP_SYSTEM_PREFIX)


def mentioned_names(type_text: str) -> set[str]:
    """Every name a type spells that no dot qualifies:
    `Dictionary<T, N.Widget>` -> {Dictionary, T, N}."""
    return set(_TYPE_IDENTIFIER.findall(type_text))


def substitute(type_text: str, bindings: Mapping[str, str]) -> str:
    """`Ctx<T>` with T := Person -> `Ctx<Person>`. Only a whole simple
    name is replaced, never a qualified segment that happens to match."""
    if not bindings:
        return type_text
    return _TYPE_IDENTIFIER.sub(
        lambda match: bindings.get(match.group(0), match.group(0)), type_text
    )


def unshadowed(
    bindings: Mapping[str, str], declared: Collection[str]
) -> dict[str, str]:
    """`bindings` less the type parameters a generic method `declared`
    itself: in `Handler<T>.Handle<T>(T)` the method's own `T` hides the
    class's, so a binding of the class's never reaches it."""
    return {name: bound for name, bound in bindings.items() if name not in declared}


def bindings_for_base(
    shapes: Mapping[str, CSharpGenericShape],
    generic_arity: Mapping[str, int],
    class_qn: str,
    bindings: Mapping[str, str],
    base_qn: str,
    scope: Scope | None = None,
) -> dict[str, str]:
    """What `class_qn`'s base list passes `base_qn`'s type parameters, in
    `class_qn`'s own bound terms: `Derived : Base<int>` gives Base's T
    `int`. An argument naming one of `class_qn`'s still unbound parameters
    binds nothing: `Inline<T> : Validator<T>` says Validator's T is
    Inline's T, which only a subclass can close. With `scope`, `class_qn`'s
    file, each argument is resolved there before the deeper bindings are
    substituted in, so a chain binds every hop in its own file."""
    shape = shapes.get(class_qn)
    base_shape = shapes.get(base_qn)
    if shape is None or base_shape is None or not base_shape.parameters:
        return {}
    leaf = qn_markers.strip_dup_marker(base_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1])
    name = split_type_ref(leaf)[0]
    arity = generic_arity.get(base_qn, 0)
    written = next(
        (b for b in shape.bases if b.name == name and len(b.arguments) == arity),
        None,
    )
    if written is None:
        return {}
    unbound = set(shape.parameters) - bindings.keys()
    return {
        parameter: substitute(canonical_type(argument, scope), bindings)
        for parameter, argument in zip(
            base_shape.parameters, written.arguments, strict=False
        )
        if not mentioned_names(argument) & unbound
    }


def written_bindings(
    written: str, shape: CSharpGenericShape | None, scope: Scope | None = None
) -> dict[str, str]:
    """What an explicit implementation's spelling of its interface binds:
    `IMap<Ctx<Person>, int>` for `IMap<K, V>` is K := Ctx<Person> and
    V := int, each resolved in `scope`, the implementing type's file.
    Nothing for a non-generic interface, or for a spelling whose argument
    count is not the interface's."""
    parameters = shape.parameters if shape is not None else ()
    arguments = type_arguments(written)
    if not parameters or len(arguments) != len(parameters):
        return {}
    return {
        parameter: canonical_type(signature_type_name(argument), scope)
        for parameter, argument in zip(parameters, arguments, strict=True)
    }


def spells_member(
    member: str,
    interface_member: str,
    bindings: Mapping[str, str],
    scopes: TypeScopes | None = None,
) -> bool:
    """Whether `member` (`Validate(Person)`) is `interface_member`
    (`Validate(T)`) once `bindings` (T := Person) are substituted into the
    interface member's parameters, each parameter compared as a type, so
    `Go(System.Int32)` is `Go(int)` (PR 2814 review). Erasing the arguments
    cannot pair the two up beside a `Validate(string)`, nor can counting
    them (issue #2619). With `scopes`, each side's types resolve in its own
    file first (see `member_verdict`). Explicit implementations and
    overrides both use it."""
    return member_verdict(member, interface_member, bindings, scopes) is True


def member_verdict(
    member: str,
    interface_member: str,
    bindings: Mapping[str, str],
    scopes: TypeScopes | None,
) -> bool | None:
    """`spells_member` with its reason: True on a match; False when the two
    provably differ, a parameter naming two different resolved types; None
    when they differ only in spelling, which may be one type.

    With `scopes`, every type name each side writes is resolved once, in the
    file that wrote it (`canonical_type`): the member's in its own, the
    interface member's in the interface's, its type parameters left as
    written. `bindings` come resolved already, each in the file whose base
    list wrote it, and are substituted after, so nothing is resolved twice
    or in another file. A name neither side resolves keeps its spelling.
    """
    name = member.partition(cs.CHAR_PAREN_OPEN)[0]
    wanted_name, paren, wanted_params = interface_member.partition(cs.CHAR_PAREN_OPEN)
    given = parameter_types(member)
    if name != wanted_name or len(given) != len(parameter_types(interface_member)):
        return False
    given_scope, wanted_scope = scopes if scopes is not None else (None, None)
    given = [canonical_type(p, given_scope) for p in given]
    wanted = [
        substitute(canonical_type(p, wanted_scope), bindings)
        for p in parameter_types(f"{wanted_name}{paren}{wanted_params}")
    ]
    if all(same_type(g, w) for g, w in zip(given, wanted, strict=True)):
        return True
    return (
        False
        if any(_proven_apart(g, w) for g, w in zip(given, wanted, strict=True))
        else None
    )


def canonical_type(written: str, scope: Scope | None) -> str:
    """`written` with each first-party type it names spelled as its qn behind
    a leading dot, resolved once, in `scope`, the file that wrote it:
    `List<Widget>` -> `List<.proj.A.Widget>`, and so are `global::A.Widget`,
    `A.Widget` and an alias of it. A type parameter in scope, a name the file
    does not resolve (external, predefined) and a name already resolved keep
    their spelling. Without a scope nothing is resolved."""
    if scope is None:
        return written.strip()
    core, suffix = _split_type_suffix(written.strip())
    if core.startswith(cs.CSHARP_RESOLVED_PREFIX) or core in scope.type_parameters:
        return f"{core}{suffix}"
    if core.startswith(cs.CHAR_PAREN_OPEN):
        elements = cs.SEPARATOR_COMMA_SPACE.join(
            canonical_type(e, scope) for e in parameter_types(core)
        )
        return f"{cs.CHAR_PAREN_OPEN}{elements}{cs.CHAR_PAREN_CLOSE}{suffix}"
    arguments = type_arguments(core)
    name = strip_generic_arguments(core)
    qn = scope.resolve(name, len(arguments))
    head = f"{cs.CSHARP_RESOLVED_PREFIX}{qn}" if qn else name
    if arguments:
        inner = cs.SEPARATOR_COMMA_SPACE.join(
            canonical_type(a, scope) for a in arguments
        )
        head = f"{head}{cs.CHAR_ANGLE_OPEN}{inner}{cs.CHAR_ANGLE_CLOSE}"
    return f"{head}{suffix}"


def resolved_qn(canonical: str) -> str | None:
    """The qn a `canonical_type` spelling resolved its outer type to, or None
    when it kept a written spelling."""
    core = strip_generic_arguments(_split_type_suffix(canonical.strip())[0])
    if not core.startswith(cs.CSHARP_RESOLVED_PREFIX):
        return None
    return core.removeprefix(cs.CSHARP_RESOLVED_PREFIX)


def _split_type_suffix(written: str) -> tuple[str, str]:
    # `List<T>[]?` -> ("List<T>", "[]?"): the array ranks and nullable
    # marks after a type, which stay as they are written.
    match = _TYPE_SUFFIX.search(written)
    if match is None:
        return written, ""
    return written[: match.start()], match.group(0)


def _proven_apart(given: str, wanted: str) -> bool:
    # Whether two canonical types name, at some matching position, two
    # different resolved first-party types.
    given_core = _split_type_suffix(given)[0]
    wanted_core = _split_type_suffix(wanted)[0]
    given_qn, wanted_qn = resolved_qn(given_core), resolved_qn(wanted_core)
    if given_qn is not None and wanted_qn is not None and given_qn != wanted_qn:
        return True
    given_arguments, wanted_arguments = (
        type_arguments(given_core),
        type_arguments(wanted_core),
    )
    return len(given_arguments) == len(wanted_arguments) and any(
        _proven_apart(g, w)
        for g, w in zip(given_arguments, wanted_arguments, strict=True)
    )


def parameter_types(signature: str) -> list[str]:
    """The depth-0 parameter types of a signatured leaf or qn:
    `M(int, Dictionary<K, V>)` -> ["int", "Dictionary<K, V>"]."""
    open_idx = signature.find(cs.CHAR_PAREN_OPEN)
    if open_idx < 0:
        return []
    inner = signature[open_idx + 1 : signature.rfind(cs.CHAR_PAREN_CLOSE)]
    if not inner.strip():
        return []
    types: list[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(inner):
        if ch in cs.CSHARP_SIGNATURE_OPENERS:
            depth += 1
        elif ch in cs.CSHARP_SIGNATURE_CLOSERS:
            depth -= 1
        elif ch == cs.CHAR_COMMA and depth == 0:
            types.append(inner[start:idx].strip())
            start = idx + 1
    types.append(inner[start:].strip())
    return types


def type_arguments(type_text: str) -> tuple[str, ...]:
    """The top-level type arguments of a type's last segment:
    `Map<K, List<V>>` -> ("K", "List<V>"), `Ctx` -> ()."""
    leaf = leaf_type_segment(type_text)
    open_idx = leaf.find(cs.CHAR_ANGLE_OPEN)
    if open_idx < 0:
        return ()
    arguments: list[str] = []
    depth = 0
    start = open_idx + 1
    for index in range(start, len(leaf)):
        char = leaf[index]
        if char == cs.CHAR_ANGLE_OPEN or char in cs.CSHARP_NESTED_OPEN:
            depth += 1
        elif char in cs.CSHARP_NESTED_CLOSE:
            depth -= 1
        elif char == cs.CHAR_ANGLE_CLOSE:
            if depth == 0:
                arguments.append(leaf[start:index].strip())
                break
            depth -= 1
        elif char == cs.CHAR_COMMA and depth == 0:
            arguments.append(leaf[start:index].strip())
            start = index + 1
    return tuple(arguments)


def _simple_name(type_text: str) -> str:
    return strip_generic_arguments(leaf_type_segment(type_text))


def _literal_fit(parameter: str, literal: str) -> Fit | None:
    plain = plain_type_name(parameter)
    if plain not in cs.CSHARP_JUDGED_PARAM_TYPES:
        return Fit.OTHER
    if plain not in cs.CSHARP_LITERAL_ACCEPTS[literal]:
        return None
    # `int?` takes an int through a nullable conversion, which `int` beats.
    nullable = parameter.strip().endswith(cs.CSHARP_NULLABLE_SUFFIX)
    return Fit.EXACT if plain == literal and not nullable else Fit.CONVERTIBLE


def arguments_match(
    given: Sequence[str] | None, wanted: Sequence[str], open_names: Collection[str]
) -> bool | None:
    """Whether closed type arguments agree, position by position and alias-
    blind (`Box<System.Int32>` is `Box<int>`). A wanted argument naming an
    `open_names` parameter is the one inference binds, so any given argument
    fits it; `given` None (not known) answers None, never a match."""
    if given is None:
        return None
    if len(given) != len(wanted):
        return False
    return all(
        bool(mentioned_names(w) & set(open_names)) or same_type(g, w)
        for g, w in zip(given, wanted, strict=True)
    )


def written_type(argument: ArgumentType) -> str:
    """An argument's type as written, its type arguments included when the
    syntax spells them: `Box<int>`, or `Box` when they are not known."""
    name = argument.name or ""
    if not argument.arguments:
        return name
    return (
        f"{name}{cs.CHAR_ANGLE_OPEN}"
        f"{cs.SEPARATOR_COMMA_SPACE.join(argument.arguments)}{cs.CHAR_ANGLE_CLOSE}"
    )


def same_type_name(given: str, wanted: str, given_arity: int | None = None) -> bool:
    """Whether two written types name one class, arguments aside: the same
    simple name, alias-blind (`Int32`, `int`), with as many type arguments
    (`given_arity` when `given` does not spell them), and no qualification
    that sets them apart (`A.Widget`, `B.Widget`)."""
    name = _framework_name(_simple_name(given))
    if name != _framework_name(_simple_name(wanted)):
        return False
    arity = generic_arity_of_type_text(given) if given_arity is None else given_arity
    if arity != generic_arity_of_type_text(wanted):
        return False
    predefined = name in cs.CSHARP_PREDEFINED_TYPE_NAMES.values()
    return predefined or not _qualified_apart(given, wanted)


def _type_path(type_text: str) -> list[str]:
    # `global::A.Widget<T>` -> ["A", "Widget"]: the segments a type's
    # spelling qualifies its name with, alias and arguments dropped.
    path = strip_generic_arguments(type_text).rpartition(cs.SEPARATOR_DOUBLE_COLON)[2]
    return path.split(cs.SEPARATOR_DOT)


def _qualified_apart(argument_name: str, parameter: str) -> bool:
    """Whether two spellings of one simple name name two types: `A.Widget`
    and `B.Widget` each qualify it, and neither path ends the other. A bare
    `Widget` may be either, so it never tells them apart (PR 2814 review)."""
    given, wanted = _type_path(argument_name), _type_path(parameter)
    shorter, longer = sorted((given, wanted), key=len)
    return longer[-len(shorter) :] != shorter


def _framework_name(name: str) -> str:
    # `int` and `Int32` are one type: the framework name stands for both.
    return cs.CSHARP_PREDEFINED_TYPE_NAMES.get(name, name)


def same_type(given: str, wanted: str) -> bool:
    """Whether two written types name one type: `Target<int>` twice,
    `Lib.Person` and `Person`, or `System.Int32` and `int` (PR 2814 review);
    never `Target<int>` and `Target<string>`, nor `A.Widget` and
    `B.Widget`. The one alias-blind comparison every path uses."""
    if not same_type_name(given, wanted):
        return False
    return bool(arguments_match(type_arguments(given), type_arguments(wanted), ()))


def fit(
    parameter: str,
    argument: ArgumentType | None,
    open_names: Collection[str],
    judge: TypeJudge,
) -> Fit | None:
    """How well `argument` binds `parameter`, or None when it cannot.

    `open_names` are the type parameters still unbound for this candidate:
    one standing alone takes any argument, as inference would make it, and
    one inside a type argument takes whatever the argument passes there.
    """
    if argument is None:
        return Fit.UNKNOWN
    if parameter in open_names:
        return Fit.OPEN
    if argument.literal is not None:
        return _literal_fit(parameter, argument.literal)
    if argument.name is None:
        return Fit.UNKNOWN
    identical = judge.identical(argument, parameter, open_names)
    if identical:
        return Fit.EXACT
    if identical is None:
        # One class whose type arguments are not known on one side.
        return Fit.OTHER
    related = judge.converts(argument, parameter, open_names)
    if related is None:
        return Fit.OTHER
    return Fit.CONVERTIBLE if related else None


def _dominates(better: Sequence[Fit], worse: Sequence[Fit]) -> bool:
    # Fits of different lengths scored different arguments, so neither can
    # be said to beat the other.
    if len(better) != len(worse):
        return False
    return all(b >= w for b, w in zip(better, worse, strict=True)) and any(
        b > w for b, w in zip(better, worse, strict=True)
    )


def best_candidates(scored: Sequence[tuple[str, Sequence[Fit | None]]]) -> list[str]:
    """The applicable candidates no other applicable one beats on every
    argument, in their given order; empty when none applies."""
    viable = [
        (qn, [f for f in fits if f is not None])
        for qn, fits in scored
        if all(f is not None for f in fits)
    ]
    return [
        qn
        for qn, fits in viable
        if not any(_dominates(other, fits) for _, other in viable if other is not fits)
    ]
