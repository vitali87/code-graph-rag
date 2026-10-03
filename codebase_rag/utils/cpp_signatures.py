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
from collections.abc import Sequence

from ..constants import core as cs
from ..types_defs import OverloadSignature

# A qualified name (`std::string`, `a::T`) is ONE token, so a qualifier is
# compared as part of the name it qualifies; every other character stands
# alone.
_TOKEN_RE = re.compile(r"[^\W\d]\w*(?:::[^\W\d]\w*)*|\S")
_OPENERS = frozenset("(<[")
_CLOSERS = frozenset(")>]")


def signature_arity(text: str) -> int:
    """The parameter count of a signature: `(map<int,int>,int) const` -> 2."""
    depth = 0
    count = 0
    seen_parameter = False
    for char in text:
        if char in _OPENERS:
            depth += 1
            if depth == 1:
                continue
        elif char in _CLOSERS:
            depth -= 1
            if depth == 0:
                break
        if depth == 1 and char == cs.CHAR_COMMA:
            count += 1
        elif depth >= 1:
            seen_parameter = True
    return count + 1 if seen_parameter else 0


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
