from __future__ import annotations

import re
from collections.abc import Iterator

from tree_sitter import Node

from ... import constants as cs
from ..utils import cpp_declarator_name
from .constants import DYNAMIC_TARGET
from .descriptor import LanguageDescriptor

# Definition nodes whose BODY is a separate scope but whose HEADER (default arg
# values, annotations, base classes, decorators) executes in the enclosing scope at
# definition time.
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
    # The top-level nodes of the caller's OWN scope. For a function/class the own
    # scope is just its body block; its header (params/decorators/bases) belongs to
    # the enclosing scope. For a module it is every child.
    body = _definition_body(caller_node)
    return list(body.children) if body is not None else list(caller_node.children)


def definition_header_nodes(node: Node) -> list[Node]:
    # The parts of a nested definition that execute in the ENCLOSING scope at
    # definition time: default arg values, return/parameter annotations, base
    # classes, and decorators. The body block (own scope) is excluded, so the
    # enclosing DFS descends into these but never the nested body.
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
    # Java `method_invocation` has no `function` field: it exposes `object` (the
    # receiver, absent for an unqualified/static-imported call) and `name`.
    # Reconstruct the dotted callee (`System.out.println`) so it matches the registry
    # keys, as the `function` field's text would for other langs.
    name = call_node.child_by_field_name(cs.TS_FIELD_NAME)
    if name is None or name.text is None:
        return None
    method = name.text.decode(cs.ENCODING_UTF8)
    obj = call_node.child_by_field_name(cs.FIELD_OBJECT)
    if obj is not None and obj.text is not None:
        return f"{obj.text.decode(cs.ENCODING_UTF8)}{cs.SEPARATOR_DOT}{method}"
    return method


def is_require_alias(declarator: Node, call_type: str) -> bool:
    # A `const fs = require('fs')` declarator binds an import alias (the genuine
    # module), not a shadowing local, so it must not count as a local shadow, unlike
    # `const fs = {}`, which does. Detected by a `require(...)` value.
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


def default_export_collapsed(name: str) -> str | None:
    # A JS default import maps the local name to `<module>.default` (and a node:
    # builtin to `node:<module>.default`), but sink registries are keyed by the
    # module's own dotted API (`fs.readFileSync`). Collapse the default-export
    # segment and scheme for a lookup candidate; a local-module base keeps its
    # project-qualified prefix, so its collapsed form can never collide with a sink
    # key. Returns None when nothing collapsed.
    collapsed = name.removeprefix(cs.NODE_BUILTIN_PREFIX).replace(
        ".default.", cs.SEPARATOR_DOT, 1
    )
    return collapsed if collapsed != name else None


def registry_match[T](
    mapping: dict[str, T], raw_name: str | None, import_map: dict[str, str]
) -> T | None:
    # Match a call against an I/O registry keyed by canonical dotted names
    # (`sqlite3.connect`, `os.getenv`). Try the import-normalised name first; if
    # that misses AND the raw callee is module-qualified (has a dot), fall back to
    # the raw name. That recovers a stdlib module re-exported under its own name
    # (`from .utils import sqlite3`, which remaps the head off `sqlite3`), common in
    # real projects. A bare callee gets NO raw fallback: a remapped bare name
    # (`from .myio import open`) must stay shadowed rather than hit the `open` sink.
    if raw_name is None:
        return None
    name = normalise(raw_name, import_map)
    if name is not None and (hit := mapping.get(name)) is not None:
        return hit
    if (
        name is not None
        and (collapsed := default_export_collapsed(name)) is not None
        and (hit := mapping.get(collapsed)) is not None
    ):
        return hit
    if cs.SEPARATOR_DOT in raw_name:
        return mapping.get(raw_name)
    return None


def head_is_genuine_module(base: str | None, head: str) -> bool:
    # True when `head` names the genuine imported module (so its raw dotted call may
    # match a sink). None base = an unimported global. Otherwise the import base's
    # module identity must equal head: drop any member suffix and node: scheme. A
    # local `import fs from './fake'` resolves elsewhere and is rejected. Path-based
    # (Go) imports are handled separately by package-name matching.
    if base is None:
        return True
    return base.split(cs.SEPARATOR_DOT)[0].removeprefix(cs.NODE_BUILTIN_PREFIX) == head


def match_normalised[T](
    raw: str, import_map: dict[str, str], mapping: dict[str, T]
) -> T | None:
    # Match a call's import-normalised name against a registry, also trying the
    # node:-stripped form so a `node:fs` named/destructured import (which maps a
    # local to `node:fs.writeFileSync`) resolves to the bare `fs.writeFileSync`
    # entry. Used for JS/TS shadow-aware sink matching (issue #714).
    normalised = normalise(raw, import_map)
    if normalised is None:
        return None
    hit = mapping.get(normalised)
    if hit is None and normalised.startswith(cs.NODE_BUILTIN_PREFIX):
        hit = mapping.get(normalised.removeprefix(cs.NODE_BUILTIN_PREFIX))
    if hit is None and (collapsed := default_export_collapsed(normalised)) is not None:
        hit = mapping.get(collapsed)
    return hit


# A `/` inside a placeholder reads as a path-segment split downstream, and
# `?` / `#` change where urlparse cuts query and fragment.
_URL_STRUCTURE_DELIMITERS = "/?#"
OPAQUE_PLACEHOLDER = "{*}"

# Go fmt verbs (`%d`, `%-8.2f`, `%v`, `%[2]s`, `%[3]*.[2]*[1]f`, ...); `%%`
# is a literal percent. One charset covers every spec character (flags,
# width, precision, argument indexes) and excludes the verb letter, so the
# scan is unambiguous and linear; malformed specs simply stay literal, as
# fmt itself would render them as `%!` errors.
_FORMAT_VERB_RE = re.compile(r"%(?:%|[0-9#+\-. *\[\]]*[a-zA-Z])")


def _dot_import_format_call(
    raw: str | None, import_map: dict[str, str], names: frozenset[str]
) -> str | None:
    # `import . "fmt"` puts Sprintf itself in scope with no package qualifier;
    # the import processor records the dot import under a `.`-prefixed sentinel
    # (identifiers cannot contain a dot), so a bare callee re-qualifies through
    # each dot-imported package's path.
    if raw is None or cs.SEPARATOR_DOT in raw:
        return None
    for local, base in import_map.items():
        if local.startswith(cs.SEPARATOR_DOT):
            candidate = f"{base}{cs.SEPARATOR_DOT}{raw}"
            if candidate in names:
                return candidate
    return None


def format_call_target(
    arg: Node | None, descriptor: LanguageDescriptor, import_map: dict[str, str]
) -> str | None:
    """The placeholder-marked value of a format-call sink target.

    ``http.Get(fmt.Sprintf("...products/%d", id))`` reads the literal format
    string and renders each verb as an opaque placeholder. None when the
    argument is not a recognised format call; dynamic when its format string
    is not a literal.
    """
    if arg is None or arg.type != descriptor.call_type:
        return None
    normalised = normalise(call_name(arg), import_map)
    if (
        normalised not in descriptor.format_call_names
        and (
            normalised := _dot_import_format_call(
                call_name(arg), import_map, descriptor.format_call_names
            )
        )
        is None
    ):
        return None
    format_string = literal_target(
        arg,
        0,
        string_type=descriptor.string_type,
        content_type=descriptor.string_content_type,
        keyword_arg_type=descriptor.keyword_arg_type,
    )
    if format_string == DYNAMIC_TARGET and descriptor.raw_string_type is not None:
        # Go backtick strings are format strings too, in a distinct node type.
        format_string = literal_target(
            arg,
            0,
            string_type=descriptor.raw_string_type,
            content_type=descriptor.raw_string_content_type or "",
            keyword_arg_type=descriptor.keyword_arg_type,
        )
    if format_string == DYNAMIC_TARGET:
        return DYNAMIC_TARGET
    return _FORMAT_VERB_RE.sub(
        lambda m: "%" if m.group(0) == "%%" else OPAQUE_PLACEHOLDER, format_string
    )


def string_literal(
    arg: Node | None,
    string_type: str = cs.TS_PY_STRING,
    content_type: str = cs.TS_PY_STRING_CONTENT,
) -> str:
    if arg is None or arg.type != string_type:
        return DYNAMIC_TARGET
    # An f-string is a `string` node whose content is split around
    # `interpolation` children; keep every fragment and render each
    # interpolation as its literal `{expr}` source so the identity stays a
    # placeholder-marked whole rather than a truncated prefix (issue #876).
    # Placeholders alone carry no identity, so all-interpolation strings
    # stay dynamic. An expression containing a path or URL-parse delimiter
    # would fabricate segment structure, so it collapses to `{*}`.
    parts: list[str] = []
    has_content = False
    for child in arg.named_children:
        if child.text is None:
            continue
        if child.type == content_type:
            has_content = True
            parts.append(child.text.decode(cs.ENCODING_UTF8))
        elif child.type == cs.TS_PY_INTERPOLATION:
            text = child.text.decode(cs.ENCODING_UTF8)
            safe = not any(delim in text for delim in _URL_STRUCTURE_DELIMITERS)
            parts.append(text if safe else OPAQUE_PLACEHOLDER)
    if not has_content:
        return DYNAMIC_TARGET
    return "".join(parts)


def iter_token_tree_calls(
    token_tree: Node,
    scope_separator: str,
    identifier_type: str,
    token_tree_type: str,
) -> Iterator[tuple[str, Node]]:
    # tree-sitter flattens a Rust macro body to a token_tree of raw tokens, so an
    # inlined scoped call (`std::env::var("X")`) is a run of `identifier` joined by
    # the scope token (whose node type IS the separator, e.g. "::") followed by its
    # args token_tree, with no call_expression node. Yield (reconstructed dotted
    # name, args token_tree) for each such run, recursing into nested groups. Shared
    # by the io walk (sink emission) and the flow walk (taint into a macro sink).
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
    # arg0 of a flattened call's token_tree: the tokens before the first top-level
    # comma. A resource path only when it is a lone string literal (`write(path,
    # "x")` has a variable arg0 -> <dynamic>, not "x").
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
    # LHS name(s) of a lean binding: a bare identifier, or a Go expression_list
    # of them. A non-identifier target (JS destructuring, a field/index write)
    # yields None so its RHS position is still consumed but no var is bound.
    # Shared by the flow taint walk and the I/O handle walk (issue #714).
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
    # The (LHS names, RHS value nodes) of one binding node across the lean grammars:
    # JS uses `name`/`value` (declarator) or `left`/`right` (assignment); Go uses
    # `left`/`right` expression_lists (`:=`, `=`) or `name`/`value` (`var`/`const`);
    # Rust `let` binds via `pattern`, C++ `int x = ..` via a nested `declarator`
    # (unwrapped through pointer/reference declarators).
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
    value = node.child_by_field_name(cs.FIELD_VALUE)
    if (
        value is None
        and descriptor.declarator_value_is_last_child
        and node.type == descriptor.declarator_type
    ):
        value = _last_named_declarator_value(node)
    return targets, lean_binding_values(value, descriptor)


def _last_named_declarator_value(node: Node) -> Node | None:
    # C# `variable_declarator` = `name = <expr>` with the initialiser as an
    # unfielded child: its value is the last named child. An uninitialised
    # declaration has a single named child (the name identifier), so require at
    # least two: robust even when the `name` field is absent under parser error
    # recovery (a lone child is never a real initialiser).
    if node.named_child_count < 2:
        return None
    return node.named_child(node.named_child_count - 1)


def keyword_value(args: Node, keyword: str) -> Node | None:
    for child in args.named_children:
        if child.type != cs.TS_PY_KEYWORD_ARGUMENT:
            continue
        name = child.child_by_field_name(cs.TS_FIELD_NAME)
        if name is not None and name.text is not None:
            if name.text.decode(cs.ENCODING_UTF8) == keyword:
                return child.child_by_field_name(cs.FIELD_VALUE)
    return None


def _wrapper_arg_name(node: Node, wrapper_type: str | None) -> str | None:
    # The parameter name of a C# named argument (`path: "x"` -> "path"), read
    # from the `argument` wrapper's `name` field; None for a positional arg.
    if wrapper_type is None or node.type != wrapper_type:
        return None
    name = node.child_by_field_name(cs.TS_FIELD_NAME)
    if name is not None and name.text is not None:
        return name.text.decode(cs.ENCODING_UTF8)
    return None


def unwrap_argument(node: Node, wrapper_type: str | None) -> Node:
    # C# wraps each call arg in an `argument` node; the real expression is its last
    # named child (a named arg's `name` identifier is an earlier named child).
    # Unwrap so the string reader sees the literal. No-op elsewhere.
    if (
        wrapper_type is not None
        and node.type == wrapper_type
        and node.named_child_count
    ):
        return node.named_child(node.named_child_count - 1) or node
    return node


def _wrapper_keyword_value(args: Node, keyword: str, wrapper_type: str) -> Node | None:
    # Find a C# named argument (`variable: "X"`) by its parameter name and return
    # its value expression, so a reordered named arg resolves to the right resource
    # identity regardless of position.
    for child in args.named_children:
        if _wrapper_arg_name(child, wrapper_type) == keyword:
            return unwrap_argument(child, wrapper_type)
    return None


def positional_arg_node(
    call_node: Node, arg_index: int, wrapper_type: str | None
) -> Node | None:
    # The unwrapped expression node at a positional argument index, excluding
    # comments and C# named-argument wrappers (so the index maps to a real
    # positional arg). Resolves a handle passed as an argument
    # (`new SqlCommand(sql, conn)` -> conn at index 1).
    args = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
    if args is None:
        return None
    positional = [
        c
        for c in args.named_children
        if c.type != cs.TS_COMMENT and _wrapper_arg_name(c, wrapper_type) is None
    ]
    if arg_index < len(positional):
        return unwrap_argument(positional[arg_index], wrapper_type)
    return None


def literal_target(
    call_node: Node,
    arg_index: int | None,
    arg_keyword: str | None = None,
    *,
    string_type: str = cs.TS_PY_STRING,
    content_type: str = cs.TS_PY_STRING_CONTENT,
    keyword_arg_type: str | None = cs.TS_PY_KEYWORD_ARGUMENT,
    wrapper_type: str | None = None,
) -> str:
    if arg_index is None and arg_keyword is None:
        return DYNAMIC_TARGET
    args = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
    if args is None:
        return DYNAMIC_TARGET
    # A C# named argument (`path: "x"`) is matched by name FIRST: named args can be
    # reordered, so they must not count toward the positional index.
    if arg_keyword is not None and wrapper_type is not None:
        named = _wrapper_keyword_value(args, arg_keyword, wrapper_type)
        if named is not None:
            return string_literal(named, string_type, content_type)
    # Exclude keyword args, comment nodes (tree-sitter keeps comments as named
    # children), and C# named-argument wrappers so the positional index maps to the
    # real positional argument.
    positional = [
        c
        for c in args.named_children
        if c.type not in (keyword_arg_type, cs.TS_COMMENT)
        and _wrapper_arg_name(c, wrapper_type) is None
    ]
    if arg_index is not None and arg_index < len(positional):
        return string_literal(
            unwrap_argument(positional[arg_index], wrapper_type),
            string_type,
            content_type,
        )
    if arg_keyword is not None:
        return string_literal(
            keyword_value(args, arg_keyword), string_type, content_type
        )
    return DYNAMIC_TARGET
