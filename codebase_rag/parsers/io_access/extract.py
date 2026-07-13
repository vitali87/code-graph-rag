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


def is_require_alias(declarator: Node, call_type: str) -> bool:
    # (H) A `const fs = require('fs')` declarator binds an import alias (the genuine
    # (H) module), not a shadowing local, so it must not count as a local shadow --
    # (H) unlike `const fs = {}`, which does. Detected by a `require(...)` value.
    value = declarator.child_by_field_name(cs.FIELD_VALUE)
    if value is None or value.type != call_type:
        return False
    fn = value.child_by_field_name(cs.TS_FIELD_FUNCTION)
    return (
        fn is not None
        and fn.text is not None
        and fn.text.decode(cs.ENCODING_UTF8) == cs.JS_REQUIRE_KEYWORD
    )


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


def match_normalised[T](
    raw: str, import_map: dict[str, str], mapping: dict[str, T]
) -> T | None:
    # (H) Match a call's import-normalised name against a registry, also trying the
    # (H) node:-stripped form so a `node:fs` named/destructured import (which maps a
    # (H) local to `node:fs.writeFileSync`) resolves to the bare `fs.writeFileSync`
    # (H) entry. Used for JS/TS shadow-aware sink matching (issue #714).
    normalised = normalise(raw, import_map)
    if normalised is None:
        return None
    hit = mapping.get(normalised)
    if hit is None and normalised.startswith(cs.NODE_BUILTIN_PREFIX):
        hit = mapping.get(normalised.removeprefix(cs.NODE_BUILTIN_PREFIX))
    return hit


def string_literal(
    arg: Node | None,
    string_type: str = cs.TS_PY_STRING,
    content_type: str = cs.TS_PY_STRING_CONTENT,
) -> str:
    if arg is None or arg.type != string_type:
        return DYNAMIC_TARGET
    for child in arg.named_children:
        if child.type == content_type and child.text is not None:
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
    call_node: Node,
    arg_index: int | None,
    arg_keyword: str | None = None,
    *,
    string_type: str = cs.TS_PY_STRING,
    content_type: str = cs.TS_PY_STRING_CONTENT,
    keyword_arg_type: str | None = cs.TS_PY_KEYWORD_ARGUMENT,
) -> str:
    if arg_index is None and arg_keyword is None:
        return DYNAMIC_TARGET
    args = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
    if args is None:
        return DYNAMIC_TARGET
    # (H) Exclude keyword args and comment nodes (tree-sitter keeps comments as
    # (H) named children) so the positional index maps to the real argument.
    positional = [
        c
        for c in args.named_children
        if c.type not in (keyword_arg_type, cs.TS_COMMENT)
    ]
    if arg_index is not None and arg_index < len(positional):
        return string_literal(positional[arg_index], string_type, content_type)
    if arg_keyword is not None:
        return string_literal(
            keyword_value(args, arg_keyword), string_type, content_type
        )
    return DYNAMIC_TARGET
