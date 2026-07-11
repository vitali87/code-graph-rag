from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from .constants import DYNAMIC_TARGET


def call_name(call_node: Node) -> str | None:
    fn = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
    if fn is None or fn.text is None:
        return None
    return fn.text.decode(cs.ENCODING_UTF8)


def normalise(name: str | None, import_map: dict[str, str]) -> str | None:
    if name is None:
        return None
    head, sep, rest = name.partition(cs.SEPARATOR_DOT)
    base = import_map.get(head)
    if base is None:
        return name
    return f"{base}{cs.SEPARATOR_DOT}{rest}" if rest else base


def string_literal(arg: Node | None) -> str:
    if arg is None or arg.type != cs.TS_PY_STRING:
        return DYNAMIC_TARGET
    for child in arg.named_children:
        if child.type == cs.TS_PY_STRING_CONTENT and child.text is not None:
            return child.text.decode(cs.ENCODING_UTF8)
    return DYNAMIC_TARGET


def keyword_value(args: Node, keyword: str) -> Node | None:
    for child in args.named_children:
        if child.type != cs.TS_PY_KEYWORD_ARGUMENT:
            continue
        name = child.child_by_field_name(cs.TS_FIELD_NAME)
        if name is not None and name.text is not None:
            if name.text.decode(cs.ENCODING_UTF8) == keyword:
                return child.child_by_field_name(cs.FIELD_VALUE)
    return None


def literal_target(
    call_node: Node, arg_index: int | None, arg_keyword: str | None = None
) -> str:
    if arg_index is None and arg_keyword is None:
        return DYNAMIC_TARGET
    args = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
    if args is None:
        return DYNAMIC_TARGET
    positional = [c for c in args.named_children if c.type != cs.TS_PY_KEYWORD_ARGUMENT]
    if arg_index is not None and arg_index < len(positional):
        return string_literal(positional[arg_index])
    if arg_keyword is not None:
        return string_literal(keyword_value(args, arg_keyword))
    return DYNAMIC_TARGET
