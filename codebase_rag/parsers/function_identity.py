"""The identity a function registers under, which the function pass and its
language collaborators share: the qualified name, name and export of a
resolved definition, and where its name token starts."""

from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs


def name_start_point(func_node: Node) -> tuple[int, int]:
    """(line, column) of the definition's NAME token, falling back to the
    node start.

    Go's semantic call join keys targets at the name identifier while the
    span key sits at the `func` keyword; the name can even sit on a LATER
    line (a multiline receiver), so both coordinates persist and the
    incremental rehydration rebuilds the alias exactly (issue #1240).
    """
    name_node = func_node.child_by_field_name(cs.FIELD_NAME)
    anchor = name_node if name_node is not None else func_node
    return anchor.start_point[0] + 1, anchor.start_point[1]


class FunctionResolution(NamedTuple):
    qualified_name: str
    name: str
    is_exported: bool
    # True when the name was GENERATED (anonymous_row_col): a JS/TS named pass
    # (object literal, export, assignment, prototype) may own this same source
    # function, so registration defers until those ran.
    is_anonymous: bool = False
