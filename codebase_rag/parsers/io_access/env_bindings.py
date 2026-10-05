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


class EnvAliases:
    """Resolves the `env` of `env.PORT` to the env mapping it stands for
    (`const env = process.env`), at each access, by lexical scope: the nearest
    scope around the access that declares the name decides (bot review on PR
    #2767). It is an alias only when that declaration and every assignment to
    it is the mapping itself; a parameter, a `catch` binding or a declaration
    of anything else is not."""

    def __init__(
        self,
        caller_node: Node | None,
        descriptor: LanguageDescriptor,
        member_reads: tuple[tuple[str, ResourceKind], ...],
        head_is_live: Callable[[str], bool],
    ) -> None:
        self._descriptor = descriptor
        self._member_reads = member_reads
        self._head_is_live = head_is_live
        self._names = (
            _alias_candidates(caller_node, descriptor, member_reads, head_is_live)
            if caller_node is not None and member_reads
            else frozenset()
        )
        self._decided: dict[tuple[int, str], ResourceKind | None] = {}

    def kind_of(self, obj: Node) -> ResourceKind | None:
        name = _text(obj)
        if (
            name is None
            or name not in self._names
            or obj.type != self._descriptor.identifier_type
        ):
            return None
        scope = obj.parent
        while scope is not None:
            values = _declared_values(scope, name, self._descriptor)
            if values is not None:
                key = (scope.id, name)
                if key not in self._decided:
                    self._decided[key] = self._declaration_kind(scope, name, values)
                return self._decided[key]
            scope = scope.parent
        return None

    def _declaration_kind(
        self, scope: Node, name: str, values: list[Node | None]
    ) -> ResourceKind | None:
        if None in values:
            return None
        values = values + _assigned_values(scope, name, self._descriptor)
        kinds = {
            None
            if value is None
            else env_mapping_kind(value, self._member_reads, self._head_is_live)
            for value in values
        }
        return kinds.pop() if len(kinds) == 1 else None


def _alias_candidates(
    caller_node: Node,
    descriptor: LanguageDescriptor,
    member_reads: tuple[tuple[str, ResourceKind], ...],
    head_is_live: Callable[[str], bool],
) -> frozenset[str]:
    # Names some binding in the caller or a scope around it sets to the env
    # mapping: the only names an access can resolve, found once per caller so
    # every other member read skips the scope walk.
    names: set[str] = set()
    scope: Node | None = caller_node
    while scope is not None:
        nodes = (
            _top_level_nodes(scope, descriptor)
            if scope.parent is None
            else _scope_nodes(scope, descriptor)
        )
        for name, values in _bindings(nodes, descriptor).items():
            if any(env_mapping_kind(v, member_reads, head_is_live) for v in values):
                names.add(name)
        scope = scope.parent
        while (
            scope is not None
            and scope.parent is not None
            and scope.type not in descriptor.nested_scope_types
        ):
            scope = scope.parent
    return frozenset(names)


def _declared_values(
    scope: Node, name: str, descriptor: LanguageDescriptor
) -> list[Node | None] | None:
    # What `scope` declares `name` as, None when it does not declare it: the
    # value of each declarator (the target itself when it has none, which is
    # `undefined`), and None for a parameter, a `catch` or loop binding, a
    # destructured declarator, a function, class or import.
    # ponytail: `var` is function-scoped, but it is read block-locally here,
    # as the I/O walk's shadowing reads it.
    if scope.type in descriptor.nested_scope_types:
        return [None] if name in _parameter_names(scope, descriptor) else None
    if scope.type == cs.TS_JS_CATCH_CLAUSE:
        param = scope.child_by_field_name(cs.FIELD_PARAMETER)
        return [None] if name in _identifiers(param, descriptor) else None
    if scope.type == cs.TS_JS_FOR_IN_STATEMENT:
        left = scope.child_by_field_name(cs.FIELD_LEFT)
        declares = scope.child_by_field_name(cs.FIELD_KIND) is not None
        return [None] if declares and name in _identifiers(left, descriptor) else None
    if scope.parent is not None and scope.type != descriptor.block_scope_type:
        return None
    values: list[Node | None] = []
    for stmt in scope.named_children:
        if stmt.type == cs.TS_EXPORT_STATEMENT:
            stmt = stmt.child_by_field_name(cs.FIELD_DECLARATION) or stmt
        if stmt.type == cs.TS_IMPORT_STATEMENT:
            if name in _identifiers(stmt, descriptor):
                values.append(None)
            continue
        if stmt.type in descriptor.nested_scope_types or stmt.type == (
            cs.TS_CLASS_DECLARATION
        ):
            if _text(stmt.child_by_field_name(cs.TS_FIELD_NAME)) == name:
                values.append(None)
            continue
        for decl in stmt.named_children:
            if decl.type != descriptor.declarator_type:
                continue
            target = decl.child_by_field_name(cs.TS_FIELD_NAME)
            if target is None:
                continue
            if target.type == descriptor.identifier_type:
                if _text(target) == name:
                    value = decl.child_by_field_name(cs.FIELD_VALUE)
                    values.append(value if value is not None else target)
            elif name in _identifiers(target, descriptor):
                values.append(None)
    return values or None


def _assigned_values(
    scope: Node, name: str, descriptor: LanguageDescriptor
) -> list[Node | None]:
    # What every assignment to the `name` that `scope` declares sets it to:
    # the right side of a plain assignment, None for a destructuring, an
    # augmented or update assignment and a loop rebinding. A nested scope
    # declaring its own `name` is not descended into, a closure is.
    out: list[Node | None] = []
    stack = list(scope.named_children)
    while stack:
        node = stack.pop()
        if _declared_values(node, name, descriptor) is not None:
            continue
        stack.extend(node.named_children)
        if node.type == descriptor.assignment_type:
            left = node.child_by_field_name(cs.FIELD_LEFT)
            if left is not None and left.type == descriptor.identifier_type:
                if _text(left) == name:
                    out.append(node.child_by_field_name(cs.FIELD_RIGHT))
            elif (
                left is not None
                and left.type in (cs.TS_OBJECT_PATTERN, cs.TS_ARRAY_PATTERN)
                and name in _identifiers(left, descriptor)
            ):
                out.append(None)
            continue
        if node.type == cs.TS_JS_FOR_IN_STATEMENT:
            target = node.child_by_field_name(cs.FIELD_LEFT)
        elif node.type == descriptor.augmented_assignment_type:
            target = node.child_by_field_name(cs.FIELD_LEFT)
        elif node.type == descriptor.update_expression_type:
            target = node.child_by_field_name(cs.TS_JS_FIELD_ARGUMENT)
        else:
            continue
        if target is not None and _text(target) == name:
            out.append(None)
    return out


def _identifiers(node: Node | None, descriptor: LanguageDescriptor) -> set[str]:
    # Every name a binding target or statement spells.
    names: set[str] = set()
    stack = [node] if node is not None else []
    while stack:
        current = stack.pop()
        if current.type in (
            descriptor.identifier_type,
            cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN,
        ) and (name := _text(current)):
            names.add(name)
        stack.extend(current.named_children)
    return names


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
    names: set[str] = set()
    stack = [
        params
        for field in (descriptor.params_field, cs.FIELD_PARAMETER)
        if (params := caller_node.child_by_field_name(field)) is not None
    ]
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
