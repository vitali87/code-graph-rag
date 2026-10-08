"""Matching a C++ member to one of its overloads by signature (issue #2455).

A signature (`(const std::string&,int) const`) is what tells overloads apart,
so it keeps every namespace qualifier as written: `f(a::T)` and `f(b::T)` are
two members. The same member is often spelled differently in two places,
though, and these are the rules for pairing such spellings when no signature
matches verbatim. The registry uses them to pair an out-of-class definition
with the declaration it defines, and the override pass uses them to find the
base overload a member overrides. Both read signatures back from the graph,
so everything here works from the text alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import NamedTuple

from .. import constants as cs
from ..types_defs import OverloadSignature

# A qualified name (`std::string`, `a::T`) is ONE token, so a qualifier is
# compared as part of the name it qualifies; every other character stands
# alone.
_TOKEN_RE = re.compile(r"[^\W\d]\w*(?:::[^\W\d]\w*)*|\S")
_NAME_RE = re.compile(r"[^\W\d]\w*(?:::[^\W\d]\w*)*")
_OPENERS = frozenset("(<[")
_CLOSERS = frozenset(")>]")
_DEPTH_CHANGE = {**dict.fromkeys(_OPENERS, 1), **dict.fromkeys(_CLOSERS, -1)}
# What ends one template argument: the next one, or the list.
_TEMPLATE_ARGUMENT_ENDS = frozenset({cs.CHAR_COMMA, cs.CHAR_ANGLE_CLOSE})
# Where a parameter type's declarators begin: a pointer, a reference, an
# array bound, or a function pointer's parentheses.
_DECLARATOR_TOKENS = frozenset("*&[(")


def _split_signature(text: str) -> tuple[list[str], str]:
    """`(map<int,int>,int) const` -> (["map<int,int>", "int"], "const")."""
    depth = 0
    parameters: list[str] = []
    current: list[str] = []
    end = len(text)
    for index, char in enumerate(text):
        depth += _DEPTH_CHANGE.get(char, 0)
        # The bracket that opens the list is no part of a parameter; the one
        # that closes it ends the list.
        if char in _OPENERS and depth == 1:
            continue
        if char in _CLOSERS and depth == 0:
            end = index
            break
        if depth == 1 and char == cs.CHAR_COMMA:
            parameters.append("".join(current))
            current = []
        elif depth >= 1:
            current.append(char)
    if current or parameters:
        parameters.append("".join(current))
    return parameters, text[end + 1 :].strip()


def signature_arity(text: str) -> int:
    """The parameter count of a signature: `(map<int,int>,int) const` -> 2."""
    return len(_split_signature(text)[0])


def overload_signature_from_text(text: str) -> OverloadSignature:
    return OverloadSignature(text=text, arity=signature_arity(text))


def _names_agree(left: str, right: str) -> bool:
    # One spelling may qualify a name the other leaves bare (`std::string`
    # written as `string` under a using-directive, `a::T` as `T` inside
    # namespace `a`); the shorter must be a whole-segment tail of the longer.
    # `a::T` and `b::T` disagree.
    left_parts = left.split(cs.SEPARATOR_DOUBLE_COLON)
    right_parts = right.split(cs.SEPARATOR_DOUBLE_COLON)
    shorter, longer = sorted((left_parts, right_parts), key=len)
    return longer[len(longer) - len(shorter) :] == shorter


def spellings_agree(left: str, right: str) -> bool:
    """Whether two signatures can name the same overload up to qualification."""
    left_tokens = _TOKEN_RE.findall(left)
    right_tokens = _TOKEN_RE.findall(right)
    return len(left_tokens) == len(right_tokens) and all(
        _names_agree(a, b) for a, b in zip(left_tokens, right_tokens, strict=True)
    )


class _TypeUnit(NamedTuple):
    # One name with its template arguments (`std::map<int,Foo>`), or one
    # punctuation character (`*`, `&`); `arguments` is None for no `<...>`.
    token: str
    arguments: tuple[tuple[_TypeUnit, ...], ...] | None


def _parse_type(
    tokens: list[str], position: int, in_template: bool
) -> tuple[list[_TypeUnit], int]:
    units: list[_TypeUnit] = []
    parens = 0
    while position < len(tokens):
        token = tokens[position]
        if in_template and parens == 0 and token in _TEMPLATE_ARGUMENT_ENDS:
            break
        position += 1
        if token == cs.CHAR_PAREN_OPEN:
            parens += 1
        elif token == cs.CHAR_PAREN_CLOSE:
            parens -= 1
        elif token == cs.CHAR_ANGLE_OPEN and units and _is_name(units[-1].token):
            arguments, position = _parse_template_arguments(tokens, position)
            units[-1] = units[-1]._replace(arguments=arguments)
            continue
        units.append(_TypeUnit(token, None))
    return units, position


def _parse_template_arguments(
    tokens: list[str], position: int
) -> tuple[tuple[tuple[_TypeUnit, ...], ...], int]:
    # From just past a `<`: its arguments, and the position past its `>` (or
    # the end, for a list the text never closes).
    arguments: list[tuple[_TypeUnit, ...]] = []
    while position < len(tokens):
        argument, position = _parse_type(tokens, position, True)
        arguments.append(tuple(argument))
        if position >= len(tokens):
            break
        separator = tokens[position]
        position += 1
        if separator == cs.CHAR_ANGLE_CLOSE:
            break
    return tuple(arguments), position


def _type_units(type_text: str) -> list[_TypeUnit]:
    return _parse_type(_TOKEN_RE.findall(type_text), 0, False)[0]


def _is_name(token: str) -> bool:
    return _NAME_RE.fullmatch(token) is not None


def _std_name(name: str) -> str | None:
    # The standard-library leaf of `name`, bare or written `std::leaf`.
    parts = name.split(cs.SEPARATOR_DOUBLE_COLON)
    if len(parts) == 1 or (len(parts) == 2 and parts[0] == cs.CPP_STD_NAMESPACE):
        return parts[-1]
    return None


def _is_fixed_type_name(name: str) -> bool:
    if name in cs.CPP_BUILTIN_TYPE_WORDS:
        return True
    leaf = _std_name(name)
    return leaf in cs.CPP_STD_FIXED_TYPE_NAMES or (
        leaf in cs.CPP_STD_STRING_ALIAS_TEMPLATES
    )


class ClassLookups(NamedTuple):
    # Whether a name is a class, asked in each signature's own scope: a name
    # means what it means where it is written (issue #2455).
    left: Callable[[str], bool]
    right: Callable[[str], bool]


def _may_be_alias(name: str, is_known_class: Callable[[str], bool]) -> bool:
    return not _is_fixed_type_name(name) and not is_known_class(name)


def _is_lone_alias(
    parts: Sequence[_TypeUnit], is_known_class: Callable[[str], bool]
) -> bool:
    # A bare name that may be an alias can stand for a whole type, pointers
    # and all (`PtrT` for `const char*`).
    return (
        len(parts) == 1
        and _is_name(parts[0].token)
        and _may_be_alias(parts[0].token, is_known_class)
    )


def _names_may_match(left: str, right: str, classes: ClassLookups) -> bool:
    if _names_agree(left, right):
        return True
    return _may_be_alias(left, classes.left) or _may_be_alias(right, classes.right)


def _string_alias_match(
    left: _TypeUnit, right: _TypeUnit, classes: ClassLookups
) -> bool | None:
    """Whether a standard string alias and its template's spelling agree.

    `std::string` is `basic_string<char>`, so against a `basic_string` it
    matches only when the first argument may be `char`; the defaulted
    `char_traits` and allocator arguments after it change nothing. None when
    neither side is such an alias written against its own template.
    """
    for alias, template, template_is_left in (
        (left, right, False),
        (right, left, True),
    ):
        leaf = _std_name(alias.token)
        spec = cs.CPP_STD_STRING_ALIAS_TEMPLATES.get(leaf) if leaf else None
        if spec is None:
            continue
        template_name, character = spec
        if template.token.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1] != template_name:
            continue
        if not template.arguments:
            return True
        # Each side keeps its own place, so the template's argument is read
        # with its own side's class lookup, not the alias side's.
        written = template.arguments[0]
        implied = (_TypeUnit(character, None),)
        if template_is_left:
            return _units_may_match(written, implied, classes)
        return _units_may_match(implied, written, classes)
    return None


def _split_declarators(
    units: Sequence[_TypeUnit],
) -> tuple[list[_TypeUnit], list[_TypeUnit]]:
    # `const Foo* const&` -> base [const, Foo], declarators [*, const, &].
    for index, unit in enumerate(units):
        if unit.token in _DECLARATOR_TOKENS:
            return list(units[:index]), list(units[index:])
    return list(units), []


def _cv_and_parts(
    base: Sequence[_TypeUnit],
) -> tuple[list[str], list[_TypeUnit]]:
    cv = sorted(unit.token for unit in base if unit.token in cs.CPP_CV_QUALIFIER_WORDS)
    return cv, [unit for unit in base if unit.token not in cs.CPP_CV_QUALIFIER_WORDS]


def _bases_may_match(
    left: Sequence[_TypeUnit],
    right: Sequence[_TypeUnit],
    classes: ClassLookups,
    by_value: bool,
) -> bool:
    left_cv, left_parts = _cv_and_parts(left)
    right_cv, right_parts = _cv_and_parts(right)
    # A by-value parameter's own cv-qualifiers are no part of the function's
    # type; what a pointer or reference refers to keeps them.
    if not by_value and left_cv != right_cv:
        return False
    if len(left_parts) == len(right_parts):
        return all(
            _unit_pair_may_match(a, b, classes)
            for a, b in zip(left_parts, right_parts, strict=True)
        )
    return _is_lone_alias(left_parts, classes.left) or _is_lone_alias(
        right_parts, classes.right
    )


def _units_may_match(
    left: Sequence[_TypeUnit], right: Sequence[_TypeUnit], classes: ClassLookups
) -> bool:
    """Whether two parameter types may be one type, part by part.

    Only the base type may hide behind an alias: the pointers, references,
    arrays and the const on what they refer to must agree, so `Alias*` may
    be `int*` but never `int`. The one exception is a bare alias standing
    for a whole type with declarators of its own (`PtrT` for `const char*`),
    whose declarators must then end the other side's.
    """
    left_base, left_declarators = _split_declarators(left)
    right_base, right_declarators = _split_declarators(right)
    if len(left_declarators) == len(right_declarators):
        return all(
            _unit_pair_may_match(a, b, classes)
            for a, b in zip(left_declarators, right_declarators, strict=True)
        ) and _bases_may_match(
            left_base, right_base, classes, by_value=not left_declarators
        )
    if len(left_declarators) < len(right_declarators):
        return _alias_absorbs(
            left_base, left_declarators, right_declarators, classes.left
        )
    return _alias_absorbs(
        right_base, right_declarators, left_declarators, classes.right
    )


def _alias_absorbs(
    base: Sequence[_TypeUnit],
    declarators: Sequence[_TypeUnit],
    other_declarators: Sequence[_TypeUnit],
    is_known_class: Callable[[str], bool],
) -> bool:
    # `PtrT&` may be `const char*&`: the bare alias stands for what the other
    # side writes before the declarators the two still share.
    tail = other_declarators[len(other_declarators) - len(declarators) :]
    return _is_lone_alias(_cv_and_parts(base)[1], is_known_class) and all(
        a.token == b.token for a, b in zip(declarators, tail, strict=True)
    )


def _unit_pair_may_match(
    left: _TypeUnit, right: _TypeUnit, classes: ClassLookups
) -> bool:
    if not (_is_name(left.token) and _is_name(right.token)):
        return left.token == right.token
    if not _names_agree(left.token, right.token):
        string_match = _string_alias_match(left, right, classes)
        if string_match is not None:
            return string_match
        return _names_may_match(left.token, right.token, classes)
    # One template on both sides: its arguments decide, by the same rules.
    # Without arguments on one side (the injected `Box` inside `Box<T>`)
    # there is nothing to compare.
    if left.arguments is None or right.arguments is None:
        return True
    return len(left.arguments) == len(right.arguments) and all(
        _units_may_match(a, b, classes)
        for a, b in zip(left.arguments, right.arguments, strict=True)
    )


def parameter_types_may_match(left: str, right: str, classes: ClassLookups) -> bool:
    """Whether two signatures could be one function once typedefs are seen.

    Every parameter must agree verbatim or up to qualification, or else
    compare part by part. The pointers, references, arrays and the const on
    what they refer to must agree; the base types may differ only through a
    possible alias. Two different names may be one type only if one of them
    could be an alias in its own scope (not a built-in type word, not
    `nullptr_t`, not a class that scope can name; `std::string` aliases only
    `basic_string`), and one template on both sides is one type only if its
    arguments may be. So `(Alias)` may be `(int)`, `(Alias*)` may be `(int*)`
    and `(std::size_t)` may be `(unsigned long)`, but `(double)` is not
    `(int)`, `(Alias*)` is not `(int)`, and `vector<int>` is not
    `vector<double>`. The cv/ref qualifiers after the list must be the
    same: a `const` member is never the non-`const` one.
    """
    left_types, left_qualifiers = _split_signature(left)
    right_types, right_qualifiers = _split_signature(right)
    if left_qualifiers != right_qualifiers or len(left_types) != len(right_types):
        return False
    return all(
        a == b
        or spellings_agree(a, b)
        or _units_may_match(_type_units(a), _type_units(b), classes)
        for a, b in zip(left_types, right_types, strict=True)
    )


def pick_overload(
    signature: OverloadSignature,
    candidates: Sequence[tuple[str, OverloadSignature]],
) -> str | None:
    """The one candidate `signature` names, or None when nothing settles it.

    A verbatim match wins. A lone candidate of the same arity is taken
    whatever its spelling, since a member that is not overloaded has nothing
    to be confused with (a typedef the text cannot see through, `Count` for
    `int`); one of another arity is another function, not a respelling. A
    declaration's default arguments do not change its arity: they are
    counted as parameters, as the definition counts them. Otherwise
    the candidates are narrowed first to those whose spelling agrees up to
    qualification, then to those of the same arity, and one survivor is
    taken. Several survivors are a tie that only a guess could break, and a
    wrong guess merges two overloads or points an edge at the wrong one.
    """
    for qualified_name, known in candidates:
        if known.text == signature.text:
            return qualified_name
    if len(candidates) == 1:
        qualified_name, known = candidates[0]
        return qualified_name if known.arity == signature.arity else None
    for narrowed in (
        [qn for qn, known in candidates if spellings_agree(known.text, signature.text)],
        [qn for qn, known in candidates if known.arity == signature.arity],
    ):
        if narrowed:
            return narrowed[0] if len(narrowed) == 1 else None
    return None
