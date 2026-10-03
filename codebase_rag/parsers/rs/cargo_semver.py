"""Cargo's version requirements, enough to tell whether a version satisfies one.

A requirement is a comma-separated list of comparators, all of which must
hold. A comparator is an optional operator (`^`, `~`, `=`, `>`, `>=`, `<`,
`<=`; none means `^`) and a version of one to three parts, with `*`, `x` or
`X` standing for any trailing part (`1.*`, `1.2.x`) or for every version
(`*`). Cargo's caret and tilde bounds follow the semver crate it uses. A
pre-release version satisfies a requirement only when some comparator names
a pre-release of the same major.minor.patch, as Cargo rules.

Anything this module cannot parse satisfies nothing: a caller that must
decide whether two packages are the same keeps them apart when unsure.
"""

from __future__ import annotations

import re
from typing import NamedTuple

_OPERATORS = (">=", "<=", ">", "<", "=", "^", "~")
_CARET = "^"
_EXACT = "="
_WILDCARDS = frozenset({"*", "x", "X"})
_VERSION_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_PARTIAL_RE = re.compile(
    r"^(\d+|[*xX])(?:\.(\d+|[*xX]))?(?:\.(\d+|[*xX]))?"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)

# A version's sort key: (major, minor, patch, pre-release key). Each
# pre-release identifier is (rank, number, text): a numeric one ranks 0 and
# compares by number, an alphanumeric one ranks 1 and compares by text. A
# release is the single rank-2 identifier, after every pre-release.
type _PreKey = tuple[tuple[int, int, str], ...]
type _Key = tuple[int, int, int, _PreKey]
_RELEASE: _PreKey = ((2, 0, ""),)


class _Comparator(NamedTuple):
    op: str
    major: int | None
    minor: int | None
    patch: int | None
    pre: str | None


def satisfies(version: str, requirement: str) -> bool:
    """Whether `version` meets the Cargo `requirement`; False when unsure."""
    key = version_key(version)
    comparators = _parse_requirement(requirement)
    if key is None or comparators is None:
        return False
    if key[3] != _RELEASE and not any(
        c.pre is not None and (c.major, c.minor, c.patch) == key[:3]
        for c in comparators
    ):
        return False
    return all(_matches(c, key) for c in comparators)


def version_key(version: str) -> _Key | None:
    """A sort key ordering versions as Cargo does; None when unreadable."""
    match = _VERSION_RE.match(version.strip())
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    return int(major), int(minor), int(patch), _pre_key(pre)


def _pre_key(pre: str | None) -> _PreKey:
    if pre is None:
        return _RELEASE
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in pre.split(".")
    )


def _parse_requirement(requirement: str) -> list[_Comparator] | None:
    comparators = []
    for part in requirement.split(","):
        comparator = _parse_comparator(part.strip())
        if comparator is None:
            return None
        comparators.append(comparator)
    return comparators


def _parse_comparator(text: str) -> _Comparator | None:
    op = next((o for o in _OPERATORS if text.startswith(o)), "")
    match = _PARTIAL_RE.match(text[len(op) :].strip())
    if match is None:
        return None
    raw = list(match.groups()[:3])
    # Parts after the first wildcard are wildcards too; a wildcard after a
    # number reads as `=` on the numbers before it (`1.2.*` is `=1.2`).
    if any(part in _WILDCARDS for part in raw if part is not None):
        first = next(i for i, part in enumerate(raw) if part in _WILDCARDS)
        raw = raw[:first] + [None] * (3 - first)
        op = op or _EXACT
    major, minor, patch = (int(part) if part is not None else None for part in raw)
    pre = match.group(4)
    if pre is not None and patch is None:
        return None
    return _Comparator(op or _CARET, major, minor, patch, pre)


def _floor(c: _Comparator) -> _Key:
    pre = _pre_key(c.pre)
    return c.major or 0, c.minor or 0, c.patch or 0, pre


def _bump(c: _Comparator) -> _Key | None:
    # The first version past every one the comparator's numbers cover, or
    # None when all three parts are given and it names one version.
    if c.major is None:
        return None
    if c.minor is None:
        return c.major + 1, 0, 0, _RELEASE
    if c.patch is None:
        return c.major, c.minor + 1, 0, _RELEASE
    return None


def _caret_ceiling(c: _Comparator) -> _Key:
    major, minor, patch = c.major or 0, c.minor, c.patch
    if major > 0 or minor is None:
        return major + 1, 0, 0, _RELEASE
    if minor > 0 or patch is None:
        return 0, minor + 1, 0, _RELEASE
    return 0, 0, patch + 1, _RELEASE


def _tilde_ceiling(c: _Comparator) -> _Key:
    major = c.major or 0
    if c.minor is None:
        return major + 1, 0, 0, _RELEASE
    return major, c.minor + 1, 0, _RELEASE


def _matches(c: _Comparator, key: _Key) -> bool:
    if c.major is None:
        return True
    floor, bump = _floor(c), _bump(c)
    match c.op:
        case ">=":
            return key >= floor
        case "<":
            return key < floor
        case ">":
            return key > floor if bump is None else key >= bump
        case "<=":
            return key <= floor if bump is None else key < bump
        case "=":
            return key == floor if bump is None else floor <= key < bump
        case "~":
            return floor <= key < _tilde_ceiling(c)
        case _:
            return floor <= key < _caret_ceiling(c)
