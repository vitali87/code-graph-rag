"""Reads of a JS/TS env mapping that are not member reads (issue #2753).

A member read names its key at the access (`process.env.PORT`), but the usual
Node idiom names it in a binding instead: `const { PORT } = process.env`, a
renamed or defaulted property, a parameter defaulting to `process.env`, or an
alias (`const env = process.env; env.PORT`). These helpers find those shapes
so the I/O walk and the flow walk read them the same way.
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from ... import constants as cs
from .descriptor import LanguageDescriptor
from .extract import DYNAMIC_TARGET
from .models import ResourceKind

# (value field, pattern field) of each binding that can destructure a value:
# a declarator (`const {..} = v`), an assignment (`({..} = v)`), a JS
# parameter default (`function f({..} = v)`) and a TS one (`({..}: T = v)`).
_PATTERN_SLOTS: tuple[tuple[str, str], ...] = (
    (cs.FIELD_VALUE, cs.TS_FIELD_NAME),
    (cs.FIELD_RIGHT, cs.FIELD_LEFT),
    (cs.FIELD_VALUE, cs.TS_FIELD_PATTERN),
)


def env_mapping_kind(
    node: Node,
    member_reads: tuple[tuple[str, ResourceKind], ...],
    head_is_live: Callable[[str], bool],
) -> ResourceKind | None:
    """The kind of the env mapping `node` itself is (`process.env`), when its
    text is a catalogued member-read prefix whose head is not shadowed."""
    if node.text is None:
        return None
    text = node.text.decode(cs.ENCODING_UTF8)
    for prefix, kind in member_reads:
        if text == prefix and head_is_live(prefix.partition(cs.SEPARATOR_DOT)[0]):
            return kind
    return None


def destructured_pattern(value: Node) -> Node | None:
    """The object pattern `value` is destructured into, if any."""
    parent = value.parent
    if parent is None:
        return None
    for value_field, pattern_field in _PATTERN_SLOTS:
        slot = parent.child_by_field_name(value_field)
        if slot is None or slot.id != value.id:
            continue
        pattern = parent.child_by_field_name(pattern_field)
        if pattern is not None and pattern.type == cs.TS_OBJECT_PATTERN:
            return pattern
    return None


def object_pattern_reads(pattern: Node) -> list[tuple[str | None, str]]:
    """(bound local, property key) for each property the pattern reads. The
    key is the property name, not the local (`{ PORT: port }` reads PORT); a
    `...rest` element takes every other key, so its key is dynamic; a nested
    pattern binds no single local."""
    reads: list[tuple[str | None, str]] = []
    for child in pattern.named_children:
        if child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN:
            name = _text(child)
            if name is not None:
                reads.append((name, name))
        elif child.type == cs.TS_OBJECT_ASSIGNMENT_PATTERN:
            name = _text(child.child_by_field_name(cs.FIELD_LEFT))
            if name is not None:
                reads.append((name, name))
        elif child.type == cs.TS_PAIR_PATTERN:
            key = _property_key(child.child_by_field_name(cs.FIELD_KEY))
            reads.append(
                (_pattern_local(child.child_by_field_name(cs.FIELD_VALUE)), key)
            )
        elif child.type == cs.TS_REST_PATTERN:
            local = next(
                (c for c in child.named_children if c.type == cs.TS_PY_IDENTIFIER), None
            )
            reads.append((_text(local), DYNAMIC_TARGET))
    return reads


def env_aliases(
    caller_node: Node,
    descriptor: LanguageDescriptor,
    member_reads: tuple[tuple[str, ResourceKind], ...],
    head_is_live: Callable[[str], bool],
) -> dict[str, ResourceKind]:
    """Names that stand for an env mapping in the caller: every binding of
    the name, in the caller's own body or at module top level, is the mapping
    itself (`const env = process.env`). A name rebound to anything else is not
    an alias, and a caller's own parameter or declaration of a module alias's
    name shadows it."""
    if not member_reads:
        return {}
    own = _bindings(_scope_nodes(caller_node, descriptor), descriptor)
    root = caller_node
    while root.parent is not None:
        root = root.parent
    module = (
        _bindings(_top_level_nodes(root, descriptor), descriptor)
        if root.id != caller_node.id
        else {}
    )
    shadows = set(own) | _parameter_names(caller_node, descriptor)
    aliases: dict[str, ResourceKind] = {}
    for scope, shadowed in ((module, shadows), (own, set())):
        for name, values in scope.items():
            if name in shadowed:
                aliases.pop(name, None)
                continue
            kinds = {env_mapping_kind(v, member_reads, head_is_live) for v in values}
            if len(kinds) == 1 and (kind := kinds.pop()) is not None:
                aliases[name] = kind
            else:
                aliases.pop(name, None)
    return aliases


def _bindings(
    nodes: list[Node], descriptor: LanguageDescriptor
) -> dict[str, list[Node]]:
    # Every value each identifier is bound to by a declarator or a plain
    # assignment among `nodes`.
    out: dict[str, list[Node]] = {}
    for node in nodes:
        if node.type == descriptor.declarator_type:
            target = node.child_by_field_name(cs.TS_FIELD_NAME)
            value = node.child_by_field_name(cs.FIELD_VALUE)
        elif node.type == descriptor.assignment_type:
            target = node.child_by_field_name(cs.FIELD_LEFT)
            value = node.child_by_field_name(cs.FIELD_RIGHT)
        else:
            continue
        if target is None or target.type != descriptor.identifier_type:
            continue
        name = _text(target)
        if name is None:
            continue
        # A declaration without an initialiser binds `undefined`, not the env.
        out.setdefault(name, []).append(value if value is not None else target)
    return out


def _parameter_names(caller_node: Node, descriptor: LanguageDescriptor) -> set[str]:
    # Every identifier in the parameter list: a parameter (destructured or
    # not) shadows a module-level name of the same spelling. A default value's
    # identifiers are included too, which can only drop an alias, never add one.
    params = caller_node.child_by_field_name(descriptor.params_field)
    names: set[str] = set()
    stack = [params] if params is not None else []
    while stack:
        node = stack.pop()
        if node.type == descriptor.identifier_type and (name := _text(node)):
            names.add(name)
        stack.extend(node.named_children)
    return names


def _scope_nodes(caller_node: Node, descriptor: LanguageDescriptor) -> list[Node]:
    # The caller's own subtree, without the bodies of nested functions, which
    # bind their own names.
    body = caller_node.child_by_field_name(cs.FIELD_BODY)
    stack = list((body or caller_node).named_children)
    out: list[Node] = []
    while stack:
        node = stack.pop()
        if node.type in descriptor.nested_scope_types:
            continue
        out.append(node)
        stack.extend(node.named_children)
    return out


def _top_level_nodes(root: Node, descriptor: LanguageDescriptor) -> list[Node]:
    # The module's top-level statements and the declarators directly in them.
    out: list[Node] = []
    for stmt in root.named_children:
        out.append(stmt)
        for child in stmt.named_children:
            if child.type in (descriptor.declarator_type, descriptor.assignment_type):
                out.append(child)
    return out


def _property_key(node: Node | None) -> str:
    # `{ PORT: p }` and `{ "PORT": p }` name the key; a computed `[k]: p` does not.
    if node is None:
        return DYNAMIC_TARGET
    if node.type == cs.TS_PROPERTY_IDENTIFIER:
        return _text(node) or DYNAMIC_TARGET
    if node.type == cs.TS_STRING:
        fragment = next(
            (c for c in node.named_children if c.type == cs.TS_STRING_FRAGMENT), None
        )
        return _text(fragment) or DYNAMIC_TARGET
    return DYNAMIC_TARGET


def _pattern_local(node: Node | None) -> str | None:
    # The local a property is bound to: `p` in `PORT: p` and `PORT: p = 1`.
    if node is not None and node.type == cs.TS_ASSIGNMENT_PATTERN:
        node = node.child_by_field_name(cs.FIELD_LEFT)
    if node is None or node.type != cs.TS_PY_IDENTIFIER:
        return None
    return _text(node)


def _text(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode(cs.ENCODING_UTF8)
