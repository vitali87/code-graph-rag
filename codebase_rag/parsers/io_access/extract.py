from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from .constants import DYNAMIC_TARGET

# (H) Definition nodes whose BODY is a separate scope but whose HEADER (default
# (H) arg values, annotations, base classes, decorators) executes in the
# (H) enclosing scope at definition time.
_PY_DEFINITION_TYPES = (
    cs.TS_PY_FUNCTION_DEFINITION,
    cs.TS_PY_CLASS_DEFINITION,
    cs.TS_PY_DECORATED_DEFINITION,
)


def _definition_body(node: Node) -> Node | None:
    if node.type == cs.TS_PY_DECORATED_DEFINITION:
        inner = node.child_by_field_name(cs.FIELD_DEFINITION)
        return _definition_body(inner) if inner is not None else None
    if node.type in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION):
        return node.child_by_field_name(cs.FIELD_BODY)
    return None


def scope_seed_nodes(caller_node: Node) -> list[Node]:
    # (H) The top-level nodes of the caller's OWN scope. For a function/class the
    # (H) own scope is just its body block; its header (params/decorators/bases)
    # (H) belongs to the enclosing scope. For a module it is every child.
    body = _definition_body(caller_node)
    return list(body.children) if body is not None else list(caller_node.children)


def definition_header_nodes(node: Node) -> list[Node]:
    # (H) The parts of a nested definition that execute in the ENCLOSING scope at
    # (H) definition time: default arg values, return/parameter annotations, base
    # (H) classes, and decorators. The body block (own scope) is excluded, so the
    # (H) enclosing DFS descends into these but never into the nested body.
    if node.type == cs.TS_PY_DECORATED_DEFINITION:
        out = [c for c in node.children if c.type == cs.TS_PY_DECORATOR]
        inner = node.child_by_field_name(cs.FIELD_DEFINITION)
        if inner is not None:
            out.extend(definition_header_nodes(inner))
        return out
    if node.type == cs.TS_PY_FUNCTION_DEFINITION:
        return [
            n
            for n in (
                node.child_by_field_name(cs.FIELD_PARAMETERS),
                node.child_by_field_name(cs.FIELD_RETURN_TYPE),
            )
            if n is not None
        ]
    if node.type == cs.TS_PY_CLASS_DEFINITION:
        supers = node.child_by_field_name(cs.FIELD_SUPERCLASSES)
        return [supers] if supers is not None else []
    return []


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


def registry_match[T](
    mapping: dict[str, T], raw_name: str | None, import_map: dict[str, str]
) -> T | None:
    # (H) Match a call against an I/O registry keyed by canonical dotted names
    # (H) (`sqlite3.connect`, `os.getenv`). Try the import-normalised name first;
    # (H) if that misses AND the raw callee is module-qualified (has a dot), fall
    # (H) back to the raw name. That recovers a stdlib module re-exported under its
    # (H) own name (`from .utils import sqlite3`, which remaps the head off
    # (H) `sqlite3`), which is common in real projects. A bare callee gets NO raw
    # (H) fallback: a remapped bare name (`from .myio import open`) must stay
    # (H) shadowed rather than hit the `open` sink.
    if raw_name is None:
        return None
    name = normalise(raw_name, import_map)
    if name is not None and (hit := mapping.get(name)) is not None:
        return hit
    if cs.SEPARATOR_DOT in raw_name:
        return mapping.get(raw_name)
    return None


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
