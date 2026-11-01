"""Python `match` cases that always match.

An unguarded irrefutable case leaves no path past the `match` on which
no case ran; both the access-flow pass and the call-binding pass need
that to join the cases correctly.
"""

from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs


def case_always_matches(arm: Node) -> bool:
    # An UNGUARDED irrefutable pattern always matches; a guarded one can
    # fail its guard, so it never removes the no-match path.
    if arm.child_by_field_name(cs.TS_PY_FIELD_GUARD) is not None:
        return False
    pattern = next(
        (child for child in arm.named_children if child.type == cs.TS_PY_CASE_PATTERN),
        None,
    )
    return pattern is not None and pattern_irrefutable(pattern)


def pattern_irrefutable(pattern: Node) -> bool:
    # Irrefutable case patterns: `_` (empty case_pattern), a bare CAPTURE
    # name (dotted_name with exactly one identifier; multi-part dotted
    # names are value patterns that compare), and `<irrefutable> as x`.
    children = pattern.named_children
    if not children:
        return True
    if len(children) != 1:
        return False
    child = children[0]
    if child.type == cs.TS_PY_DOTTED_NAME:
        return child.named_child_count == 1
    if child.type == cs.TS_PY_AS_PATTERN:
        inner = next(
            (c for c in child.named_children if c.type == cs.TS_PY_CASE_PATTERN),
            None,
        )
        return inner is not None and pattern_irrefutable(inner)
    if child.type == cs.TS_PY_UNION_PATTERN:
        return _union_pattern_irrefutable(child)
    return False


def _union_pattern_irrefutable(union: Node) -> bool:
    # `1 | _` / `1 | other`: only the LAST alternative may legally be
    # irrefutable, and a bare `_` alternative is an ANONYMOUS node, so
    # inspect ALL children for the final non-separator one.
    last = next(
        (
            c
            for c in reversed(union.children)
            if c.is_named or c.type == cs.TS_PY_WILDCARD_NODE
        ),
        None,
    )
    if last is None:
        return False
    if last.type == cs.TS_PY_WILDCARD_NODE:
        return True
    return last.type == cs.TS_PY_DOTTED_NAME and last.named_child_count == 1
