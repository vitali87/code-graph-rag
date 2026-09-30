"""Strip the duplicate-definition marker from a qualified name (issue #2017).

`function_registry` disambiguates definitions sharing one qualified name by
appending `@<line>`, and `_<col>` after that when a same-named twin already
holds that line. Readers that want the *written* name strip the suffix back
off.

The marker opens with `@`, and so does a C# verbatim identifier -- `@event`,
`@lock`, `@class` are how the language spells a name that collides with a
keyword. So `qn.split("@", 1)[0]`, the obvious reading, turns `@event` into
the empty string and `pkg.@event` into `pkg.`, which is the bug this module
exists to remove. The distinguishing property is not the character but what
FOLLOWS it: the marker is always `@` then digits, and no identifier can begin
with a digit in any language here.

Only the LAST such suffix is a marker, and only when it is numeric, so a
duplicate verbatim type (`@event@12`) keeps its name and loses its marker.
"""

from __future__ import annotations

import re

from ..constants import core as cs

# The grammar `function_registry` produces: `@<line>`, optionally `_<col>`.
# Built from the same constants as the producer so the readers cannot
# drift from it; every reader goes through one of the functions below
# (issue #2114).
_MARKER = (
    re.escape(cs.DUP_QN_MARKER)
    + r"(\d+)(?:"
    + re.escape(cs.DUP_QN_COLUMN_MARKER)
    + r"\d+)?"
)
# Anchored to the END, because that is where a registration appends it.
_MARKER_RE = re.compile(_MARKER + "$")
# Anywhere: a duplicate-suffixed OUTER type carries its marker on an inner
# segment of a nested type's qn (`proj.Lib@7.Helper`).
_ANY_MARKER_RE = re.compile(_MARKER)


def strip_dup_marker(qualified_name: str) -> str:
    """`Box@12` -> `Box`, `Box@12_5` -> `Box`, but `@event` -> `@event`.

    Returns the name unchanged when no numeric marker is present, so it is
    safe to call on a name that never carried one.
    """
    return _MARKER_RE.sub("", qualified_name)


def natural_qn(qualified_name: str) -> str:
    """Strip the marker from the last dotted segment of a qualified name.

    The marker is appended to the whole qn, so it can only appear in the
    final segment; splitting first keeps a `@` anywhere earlier untouched.
    """
    head, sep, last = qualified_name.rpartition(cs.SEPARATOR_DOT)
    return f"{head}{sep}{strip_dup_marker(last)}"


def strip_all_markers(qualified_name: str) -> str:
    """Strip every marker in a dotted path: `proj.Lib@7.Helper@12` ->
    `proj.Lib.Helper`.

    For matching a candidate by its WHOLE path, where an inner segment can
    carry a marker. A reader handed a leaf, or a tail relative to a module,
    wants `strip_dup_marker`, which leaves an inner segment alone.
    """
    return _ANY_MARKER_RE.sub("", qualified_name)


def marker_line(name: str) -> int | None:
    """The line a trailing marker names: `Box@12` and `Box@12_5` -> 12,
    `@event@12` -> 12, and None when the name carries no marker."""
    match = _MARKER_RE.search(name)
    return int(match.group(1)) if match else None


# A C# generic declared beside a same-named type of another arity carries its
# CLR arity on its segment (`PB`1`, issue #2579). Unlike the duplicate marker
# it is part of the type's identity, but not of its written name.
_ARITY_RE = re.compile(re.escape(cs.CSHARP_GENERIC_ARITY_MARKER) + r"\d+$")


def strip_arity_marker(segment: str) -> str:
    """`PB`1` -> `PB`; a segment without the marker is returned unchanged."""
    return _ARITY_RE.sub("", segment)


def with_leaf_arity(qualified_name: str, arity: int) -> str:
    """The qn with its last segment spelled for `arity`, CLR style.

    The duplicate marker goes and the arity marker is set whether or not the
    qn carried one: `N.Box@12` and `N.Box` with arity 1 -> `N.Box`1`, with
    arity 0 -> `N.Box`. Two parts of one partial generic agree on this even
    when only one of them sits beside a non-generic twin.
    """
    head, sep, last = qualified_name.rpartition(cs.SEPARATOR_DOT)
    leaf = strip_arity_marker(strip_dup_marker(last))
    if arity:
        leaf = f"{leaf}{cs.CSHARP_GENERIC_ARITY_MARKER}{arity}"
    return f"{head}{sep}{leaf}"
