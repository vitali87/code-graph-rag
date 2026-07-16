from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ... import constants as cs
from ..utils import cpp_declarator_name
from .constants import DYNAMIC_TARGET
from .descriptor import LanguageDescriptor

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
    if fn is not None:
        return fn.text.decode(cs.ENCODING_UTF8) if fn.text is not None else None
    # (H) Java `method_invocation` has no `function` field: it exposes `object` (the
    # (H) receiver, absent for an unqualified/static-imported call) and `name`.
    # (H) Reconstruct the dotted callee (`System.out.println`) so it matches the
    # (H) registry keys, exactly as the `function` field's text would for other langs.
    name = call_node.child_by_field_name(cs.TS_FIELD_NAME)
    if name is None or name.text is None:
        return None
    method = name.text.decode(cs.ENCODING_UTF8)
    obj = call_node.child_by_field_name(cs.FIELD_OBJECT)
    if obj is not None and obj.text is not None:
        return f"{obj.text.decode(cs.ENCODING_UTF8)}{cs.SEPARATOR_DOT}{method}"
    return method


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


def head_is_genuine_module(base: str | None, head: str) -> bool:
    # (H) True when `head` names the genuine imported module (so its raw dotted call
    # (H) may match a sink). None base = an unimported global. Otherwise the import
    # (H) base's module identity must equal head: drop any member suffix and node:
    # (H) scheme. A local `import fs from './fake'` resolves elsewhere and is rejected.
    # (H) Path-based (Go) imports are handled separately by package-name matching.
    if base is None:
        return True
    return base.split(cs.SEPARATOR_DOT)[0].removeprefix(cs.NODE_BUILTIN_PREFIX) == head


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


def iter_token_tree_calls(
    token_tree: Node,
    scope_separator: str,
    identifier_type: str,
    token_tree_type: str,
) -> Iterator[tuple[str, Node]]:
    # (H) tree-sitter flattens a Rust macro body to a token_tree of raw tokens, so an
    # (H) inlined scoped call (`std::env::var("X")`) is a run of `identifier` joined by
    # (H) the scope token (whose node type IS the separator, e.g. "::") followed by its
    # (H) args token_tree -- no call_expression node. Yield (reconstructed dotted name,
    # (H) args token_tree) for each such run, recursing into nested groups. Shared by
    # (H) the io walk (sink emission) and the flow walk (taint into a macro sink).
    path: list[str] = []
    expect_sep = False
    for child in token_tree.children:
        if child.type == identifier_type and not expect_sep and child.text:
            path.append(child.text.decode(cs.ENCODING_UTF8))
            expect_sep = True
        elif child.type == scope_separator and expect_sep:
            expect_sep = False
        elif child.type == token_tree_type:
            if path:
                yield scope_separator.join(path), child
            path, expect_sep = [], False
            yield from iter_token_tree_calls(
                child, scope_separator, identifier_type, token_tree_type
            )
        else:
            path, expect_sep = [], False


def first_token_arg_string(args: Node, string_type: str, content_type: str) -> str:
    # (H) arg0 of a flattened call's token_tree: the tokens before the first top-level
    # (H) comma. It is a resource path only when it is a lone string literal
    # (H) (`write(path, "x")` has a variable arg0 -> <dynamic>, not "x").
    arg0: list[Node] = []
    for child in args.children:
        if child.type in (cs.CHAR_PAREN_OPEN, cs.CHAR_PAREN_CLOSE):
            continue
        if child.type == cs.CHAR_COMMA:
            break
        arg0.append(child)
    if len(arg0) == 1 and arg0[0].type == string_type:
        return string_literal(arg0[0], string_type, content_type)
    return DYNAMIC_TARGET


def lean_binding_targets(
    node: Node, descriptor: LanguageDescriptor
) -> list[str | None]:
    # (H) LHS name(s) of a lean binding: a bare identifier, or a Go expression_list
    # (H) of them. A non-identifier target (JS destructuring, a field/index write)
    # (H) yields None so its RHS position is still consumed but no var is bound.
    # (H) Shared by the flow taint walk and the I/O handle walk (issue #714).
    if node.type == descriptor.identifier_type:
        return [node.text.decode(cs.ENCODING_UTF8) if node.text else None]
    if node.type == cs.TS_GO_EXPRESSION_LIST:
        return [
            c.text.decode(cs.ENCODING_UTF8)
            if c.type == descriptor.identifier_type and c.text
            else None
            for c in node.named_children
            if c.type != cs.TS_COMMENT
        ]
    return [None]


def lean_binding_values(
    node: Node | None, descriptor: LanguageDescriptor
) -> list[Node]:
    del descriptor
    if node is None:
        return []
    if node.type == cs.TS_GO_EXPRESSION_LIST:
        return [c for c in node.named_children if c.type != cs.TS_COMMENT]
    return [node]


def binding_targets_values(
    node: Node, descriptor: LanguageDescriptor
) -> tuple[list[str | None], list[Node]]:
    # (H) The (LHS names, RHS value nodes) of one binding node across the lean
    # (H) grammars: JS uses `name`/`value` (declarator) or `left`/`right`
    # (H) (assignment); Go uses `left`/`right` expression_lists (`:=`, `=`) or
    # (H) `name`/`value` (`var`/`const`); Rust `let` binds via `pattern`, C++
    # (H) `int x = ..` via a nested `declarator` (unwrapped through pointer/
    # (H) reference declarators).
    left = node.child_by_field_name(cs.FIELD_LEFT)
    if left is not None:
        return (
            lean_binding_targets(left, descriptor),
            lean_binding_values(node.child_by_field_name(cs.FIELD_RIGHT), descriptor),
        )
    if (
        descriptor.declarator_name_field is not None
        and node.type == descriptor.declarator_type
    ):
        field_node = node.child_by_field_name(descriptor.declarator_name_field)
        if field_node is None:
            targets: list[str | None] = [None]
        else:
            targets = lean_binding_targets(field_node, descriptor)
            if targets == [None]:
                targets = [cpp_declarator_name(field_node)]
        return targets, lean_binding_values(
            node.child_by_field_name(cs.FIELD_VALUE), descriptor
        )
    targets = []
    for name in node.children_by_field_name(cs.TS_FIELD_NAME):
        targets.extend(lean_binding_targets(name, descriptor))
    return targets, lean_binding_values(
        node.child_by_field_name(cs.FIELD_VALUE), descriptor
    )


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
