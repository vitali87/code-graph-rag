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
from typing import NamedTuple

from ... import constants as cs
from .utils import (
    generic_arity_of_type_text,
    leaf_type_segment,
    strip_generic_arguments,
)

_TYPE_IDENTIFIER = re.compile(cs.CSHARP_TYPE_IDENTIFIER_PATTERN)


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


# (argument name, arity, parameter name, arity) -> whether the argument
# converts to the parameter: True or False when both are first-party types
# whose relation is known, None when it cannot be told.
type Relation = Callable[[str, int, str, int], bool | None]


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
    return Fit.EXACT if plain == literal else Fit.CONVERTIBLE


def _same_type_fit(
    parameter: str, argument: ArgumentType, open_names: Collection[str]
) -> Fit:
    # Same name and arity. Written type arguments on both sides must agree,
    # except where the parameter's still mentions an open type parameter,
    # which inference would bind to whatever the argument passes.
    if argument.arguments is None:
        return Fit.EXACT
    for wanted, given in zip(
        type_arguments(parameter), argument.arguments, strict=False
    ):
        if mentioned_names(wanted) & set(open_names):
            continue
        if _simple_name(wanted) != _simple_name(given) or type_arguments(
            wanted
        ) != type_arguments(given):
            return Fit.OTHER
    return Fit.EXACT


def fit(
    parameter: str,
    argument: ArgumentType | None,
    open_names: Collection[str],
    relation: Relation,
) -> Fit | None:
    """How well `argument` binds `parameter`, or None when it cannot.

    `open_names` are the type parameters still unbound for this candidate:
    one standing alone takes any argument, as inference would make it.
    """
    if argument is None:
        return Fit.UNKNOWN
    if parameter in open_names:
        return Fit.OPEN
    if argument.literal is not None:
        return _literal_fit(parameter, argument.literal)
    if argument.name is None:
        return Fit.UNKNOWN
    wanted_name = _simple_name(parameter)
    wanted_arity = generic_arity_of_type_text(parameter)
    given_name = _simple_name(argument.name)
    if wanted_name == given_name and wanted_arity == argument.arity:
        return _same_type_fit(parameter, argument, open_names)
    # The written path, not the bare name, so a qualified parameter type
    # resolves to the type it names.
    related = relation(
        argument.name,
        argument.arity,
        strip_generic_arguments(parameter),
        wanted_arity,
    )
    if related is None:
        return Fit.OTHER
    return Fit.CONVERTIBLE if related else None


def _dominates(better: Sequence[Fit], worse: Sequence[Fit]) -> bool:
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
