"""Call-site argument mapping for change_signature."""

from __future__ import annotations

from tree_sitter import Node

from .. import constants as cs
from .. import graph_query
from ..parsers.call_processor import _find_call_arguments_node, _split_call_arguments
from ..types_defs import ResultRow
from .signature_params import _text
from .signature_types import ParamSpec

_SPLAT_TYPES = frozenset(
    {
        cs.TS_PY_LIST_SPLAT,
        cs.TS_PY_DICTIONARY_SPLAT,
        cs.TS_SPREAD_ELEMENT,
    }
)


def _site_end(row: graph_query.CallSiteRow | ResultRow) -> tuple[int, int] | None:
    """The site's recorded end point as tree-sitter counts it, if recorded."""
    end_line, end_col = row.get("end_line"), row.get("end_col")
    if isinstance(end_line, int) and isinstance(end_col, int):
        return (end_line - 1, end_col)
    return None


def _call_at(
    root: Node, line: int, col: int, end: tuple[int, int] | None = None
) -> Node | None:
    """The call node at (line, col) that carries arguments.

    With the site's recorded end point the exact call is chosen, so a
    chained `helper(2).upper()` rewrites `helper`'s arguments and not the
    outer call's; without it the outermost call at that point is taken.
    """
    stack = [root]
    found: Node | None = None
    while stack:
        node = stack.pop()
        if (
            node.start_point == (line - 1, col)
            and _find_call_arguments_node(node) is not None
        ):
            if end is not None:
                if node.end_point == end:
                    return node
            elif found is None or node.end_byte > found.end_byte:
                found = node
        if node.start_point[0] <= line - 1 <= node.end_point[0]:
            stack.extend(node.children)
    return found


def _map_arguments(
    args_node: Node,
    old_names: list[str],
    specs: list[ParamSpec],
    language: cs.SupportedLanguage | None,
    keyword_only: frozenset[str] = frozenset(),
) -> list[str] | str:
    """The site's new argument texts, or the reason it cannot be mapped.

    A keyword naming a keyword-only parameter is not part of the positional
    mapping and is carried through exactly as written.
    """
    positional, keyword = _split_call_arguments(args_node)
    if any(child.type in _SPLAT_TYPES for child in args_node.named_children):
        return cs.SIGNATURE_SPLAT
    values: dict[int, tuple[str, bool]] = {}
    for index, node in enumerate(positional):
        values[index] = (_text(node), False)
    carried: list[str] = []
    for name, node in keyword.items():
        if name in keyword_only:
            carried.append(f"{name}={_text(node)}")
            continue
        if name not in old_names:
            return cs.SIGNATURE_UNKNOWN_KEYWORD.format(name=name)
        values[old_names.index(name)] = (_text(node), True)
    keywords_ok = language == cs.SupportedLanguage.PYTHON
    if not positional and _keywords_already_fit(values, old_names, specs):
        # A keyword-only site binds by name: reordering would change
        # nothing but the spelling, so it is left exactly as written.
        return [_text(child) for child in args_node.named_children]
    out: list[str] = []
    keyword_mode = False
    for spec in specs:
        if spec.from_index is not None:
            found = values.pop(spec.from_index, None)
            if found is None:
                # The site relied on the old default; every later value
                # must be spelled by name so positions do not shift.
                keyword_mode = True
                continue
            text, was_keyword = found
            if was_keyword or keyword_mode:
                if not keywords_ok:
                    return cs.SIGNATURE_NEEDS_KEYWORDS
                out.append(f"{spec.name}={text}")
                keyword_mode = True
            else:
                out.append(text)
        elif spec.literal is not None:
            if keyword_mode:
                if not keywords_ok:
                    return cs.SIGNATURE_NEEDS_KEYWORDS
                out.append(f"{spec.name}={spec.literal}")
            else:
                out.append(spec.literal)
        else:
            return cs.SIGNATURE_UNMAPPED_PARAM.format(name=spec.name)
    # Every spec consumed what it named; anything still in `values` is an
    # argument the new signature has no home for. Rewriting would drop it
    # from the caller's source silently -- and the contract cannot catch
    # that, because the argument is gone from the file before the delta is
    # measured, so the `too_many` arity check sees nothing.
    if values:
        return cs.SIGNATURE_SURPLUS_ARGS
    return out + carried


def _keywords_already_fit(
    values: dict[int, tuple[str, bool]], old_names: list[str], specs: list[ParamSpec]
) -> bool:
    """Every passed keyword keeps its name and nothing new must be inserted."""
    kept = {spec.from_index: spec.name for spec in specs if spec.from_index is not None}
    if any(index not in kept or kept[index] != old_names[index] for index in values):
        return False
    return all(spec.from_index is not None for spec in specs)


# --- literal type checks --------------------------------------------------------
