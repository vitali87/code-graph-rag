"""Python resource handles: which names hold a file, database or socket
handle, and what a method called on one does to its resource.

Shared by the I/O walk (READS_FROM / WRITES_TO) and the flow walk (FLOWS_TO),
so a handle the one sees is the handle the other sees (issue #2751).
"""

from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from .constants import (
    DYNAMIC_TARGET,
    PY_SCOPE_BOUNDARIES,
    SQL_READ_KEYWORDS,
    SQL_WRITE_KEYWORDS,
    IODirection,
    ResourceKind,
)
from .descriptor import LanguageDescriptor
from .extract import (
    call_name,
    literal_target,
    python_locally_assigned_names,
    registry_match,
    scope_seed_nodes,
)
from .models import HandleBinding, HandleConstructor
from .registry import IO_HANDLE_DERIVES, IO_HANDLE_METHODS

# Statements whose body may not run, so a rebinding inside one leaves the
# name's earlier binding possibly in place.
_PY_BRANCHING = frozenset(
    {
        cs.TS_PY_IF_STATEMENT,
        cs.TS_PY_FOR_STATEMENT,
        cs.TS_PY_WHILE_STATEMENT,
        cs.TS_PY_TRY_STATEMENT,
        cs.TS_PY_MATCH_STATEMENT,
    }
)


def python_bound_target(node: Node) -> Node | None:
    """The name an assignment (`f = ...`) or a `with ... as f` alias binds."""
    if node.type == cs.TS_PY_ASSIGNMENT:
        return node.child_by_field_name(cs.TS_FIELD_LEFT)
    if node.type == cs.TS_PY_AS_PATTERN:
        alias = next(
            (c for c in node.children if c.type == cs.TS_PY_AS_PATTERN_TARGET),
            None,
        )
        return alias.children[0] if alias and alias.children else None
    return None


def python_handle_binding(
    node: Node,
    import_map: dict[str, str],
    ctor_by_name: dict[str, HandleConstructor],
    handles: dict[str, HandleBinding],
) -> tuple[str, HandleBinding] | None:
    """The (name, handle) a node binds: `f = open(...)` (assignment) and
    `with open(...) as f:` (as_pattern) bind a handle var to a constructor call,
    and `cur = conn.cursor()` derives a sub-handle of a bound one."""
    if node.type == cs.TS_PY_ASSIGNMENT:
        call = node.child_by_field_name(cs.TS_FIELD_RIGHT)
    elif node.type == cs.TS_PY_AS_PATTERN:
        call = next((c for c in node.children if c.type == cs.TS_PY_CALL), None)
    else:
        return None
    target = python_bound_target(node)
    # `f = open(...)` binds a plain name; `self.f = open(...)` binds an
    # attribute: keep the full dotted text ("self.f") as the handle key so a
    # later `self.f.write(...)` resolves against it.
    if (
        target is None
        or call is None
        or target.type not in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE)
        or call.type != cs.TS_PY_CALL
        or target.text is None
    ):
        return None
    raw = call_name(call)
    target_name = target.text.decode(cs.ENCODING_UTF8)
    ctor = registry_match(ctor_by_name, raw, import_map)
    if ctor is None:
        # Derive (`cur = conn.cursor()`, issue #714): a method on a bound
        # handle that yields a same-resource sub-handle binds the target
        # to the parent's resource.
        derived = _derived_binding(raw, handles)
        return None if derived is None else (target_name, derived)
    identity = literal_target(call, ctor.target_arg, ctor.target_kw)
    return target_name, HandleBinding(kind=ctor.kind, identity=identity)


def python_inherited_handles(
    caller_node: Node,
    import_map: dict[str, str],
    ctor_by_name: dict[str, HandleConstructor],
) -> dict[str, HandleBinding]:
    """Handle bindings visible from ENCLOSING scopes, walked innermost-first so
    a nearer scope shadows a farther one: a name an enclosing function binds
    at all is that function's, handle or not. An enclosing class contributes
    its `self.<attr>` handles (set in any method); an enclosing
    function/module contributes the handles its own body leaves its names
    holding. Nested scopes are pruned; their locals are not visible."""
    handles: dict[str, HandleBinding] = {}
    shadowed: set[str] = set()
    class_scanned = False
    node = caller_node.parent
    while node is not None:
        if node.type == cs.TS_PY_CLASS_DEFINITION:
            if not class_scanned:
                class_scanned = True
                _collect_self_attr_handles(node, import_map, ctor_by_name, handles)
        elif node.type in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_MODULE):
            _collect_scope_var_handles(
                node, import_map, ctor_by_name, handles, shadowed
            )
            if node.type == cs.TS_PY_FUNCTION_DEFINITION:
                shadowed |= python_locally_assigned_names(node)
        node = node.parent
    return handles


def python_handle_access(
    call_node: Node,
    import_map: dict[str, str],
    ctor_by_name: dict[str, HandleConstructor],
    handles: dict[str, HandleBinding],
) -> tuple[HandleBinding, IODirection] | None:
    """The handle a method call acts on, and the direction of the call. The
    receiver is a bound handle (`f.read()`, `self.conn.execute(..)`), a
    constructor called inline (`open(p).read()`), or another access on a
    handle (`conn.execute("SELECT ..").fetchall()` reads the database)."""
    func = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
    if func is None or func.type != cs.TS_PY_ATTRIBUTE:
        return None
    method_node = func.child_by_field_name(cs.TS_PY_FIELD_ATTRIBUTE)
    receiver = func.child_by_field_name(cs.FIELD_OBJECT)
    if method_node is None or method_node.text is None or receiver is None:
        return None
    binding = _receiver_binding(receiver, import_map, ctor_by_name, handles)
    if binding is None:
        return None
    method = method_node.text.decode(cs.ENCODING_UTF8)
    direction = IO_HANDLE_METHODS.get(binding.kind, {}).get(method)
    if direction is None:
        return None
    if binding.kind == ResourceKind.DATABASE and method.startswith("execute"):
        direction = sql_direction(call_node, direction)
    return binding, direction


def sql_direction(
    call_node: Node,
    fallback: IODirection,
    descriptor: LanguageDescriptor | None = None,
) -> IODirection:
    """The direction an `execute(sql)` call implies from its SQL verb."""
    # ponytail: first-keyword heuristic only; a full SQL parse is the
    # upgrade path if execute() direction precision ever matters.
    if descriptor is not None:
        sql = literal_target(
            call_node,
            0,
            string_type=descriptor.string_type,
            content_type=descriptor.string_content_type,
            keyword_arg_type=descriptor.keyword_arg_type,
        )
    else:
        sql = literal_target(call_node, 0)
    if sql == DYNAMIC_TARGET:
        return fallback
    head = sql.strip().split(maxsplit=1)[0].upper() if sql.strip() else ""
    if head in SQL_READ_KEYWORDS:
        return IODirection.READ
    if head in SQL_WRITE_KEYWORDS:
        return IODirection.WRITE
    return fallback


def _receiver_binding(
    receiver: Node,
    import_map: dict[str, str],
    ctor_by_name: dict[str, HandleConstructor],
    handles: dict[str, HandleBinding],
) -> HandleBinding | None:
    if receiver.type in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
        if receiver.text is None:
            return None
        return handles.get(receiver.text.decode(cs.ENCODING_UTF8))
    if receiver.type != cs.TS_PY_CALL:
        return None
    ctor = registry_match(ctor_by_name, call_name(receiver), import_map)
    if ctor is not None:
        identity = literal_target(receiver, ctor.target_arg, ctor.target_kw)
        return HandleBinding(kind=ctor.kind, identity=identity)
    inner = python_handle_access(receiver, import_map, ctor_by_name, handles)
    return inner[0] if inner is not None else None


def _derived_binding(
    raw: str | None, handles: dict[str, HandleBinding]
) -> HandleBinding | None:
    if raw is None:
        return None
    receiver, sep, method = raw.rpartition(cs.SEPARATOR_DOT)
    if not sep:
        return None
    parent = handles.get(receiver)
    if parent is None or method not in IO_HANDLE_DERIVES.get(parent.kind, frozenset()):
        return None
    return parent


def _collect_scope_var_handles(
    scope_node: Node,
    import_map: dict[str, str],
    ctor_by_name: dict[str, HandleConstructor],
    handles: dict[str, HandleBinding],
    shadowed: set[str],
) -> None:
    # The handle each name of one scope's OWN body is left holding; nested
    # defs/classes are pruned (their locals belong to their own scope, not
    # this one), and a name a nearer scope binds is skipped. The walk runs in
    # reverse source order, so the first binding met is a name's last: a
    # handle there decides it, and so does an unconditional rebinding to
    # anything else (`f = None` after `f = open(p)`, bot review on PR #2770).
    # A rebinding under a branch may not run, so an earlier handle may hold.
    decided: set[str] = set()
    stack = list(scope_seed_nodes(scope_node))
    while stack:
        node = stack.pop()
        if node.type in PY_SCOPE_BOUNDARIES:
            continue
        stack.extend(node.children)
        target = python_bound_target(node)
        if target is None or target.text is None:
            continue
        name = target.text.decode(cs.ENCODING_UTF8)
        if name in decided or name in shadowed:
            continue
        bound = python_handle_binding(node, import_map, ctor_by_name, handles)
        if bound is not None:
            decided.add(name)
            handles.setdefault(name, bound[1])
        elif not _under_branch(node, scope_node):
            decided.add(name)


def _under_branch(node: Node, scope_node: Node) -> bool:
    parent = node.parent
    while parent is not None and parent != scope_node:
        if parent.type in _PY_BRANCHING:
            return True
        parent = parent.parent
    return False


def _collect_self_attr_handles(
    class_node: Node,
    import_map: dict[str, str],
    ctor_by_name: dict[str, HandleConstructor],
    handles: dict[str, HandleBinding],
) -> None:
    # `self.<attr> = <constructor>()` bindings anywhere in the class body
    # (descending method bodies, since __init__ is the usual site); nested
    # classes are skipped because their `self` is a different object.
    stack = list(scope_seed_nodes(class_node))
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PY_CLASS_DEFINITION:
            continue
        bound = python_handle_binding(node, import_map, ctor_by_name, handles)
        if bound is not None and bound[0].startswith(cs.PY_SELF_PREFIX):
            handles.setdefault(bound[0], bound[1])
        stack.extend(node.children)
