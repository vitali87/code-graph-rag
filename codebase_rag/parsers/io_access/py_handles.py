from __future__ import annotations

from collections.abc import Callable, Mapping

from tree_sitter import Node

from ... import constants as cs
from .constants import DYNAMIC_TARGET, PY_SCOPE_BOUNDARIES, ResourceKind
from .extract import (
    call_name,
    keyword_value,
    literal_target,
    python_locally_assigned_names,
    registry_match,
    scope_seed_nodes,
)
from .models import HandleBinding, HandleConstructor, IOSink
from .registry import IO_HANDLE_DERIVES, PY_STD_STREAMS


def python_binding_from_node(
    node: Node,
    import_map: dict[str, str],
    ctor_by_name: dict[str, HandleConstructor],
    handles: dict[str, HandleBinding],
) -> tuple[str, HandleBinding] | None:
    # Both `f = open(...)` (assignment) and `with open(...) as f:` (as_pattern)
    # bind a handle var to a constructor call.
    if node.type == cs.TS_PY_ASSIGNMENT:
        target = node.child_by_field_name(cs.TS_FIELD_LEFT)
        call = node.child_by_field_name(cs.TS_FIELD_RIGHT)
    elif node.type == cs.TS_PY_AS_PATTERN:
        call = next((c for c in node.children if c.type == cs.TS_PY_CALL), None)
        alias = next(
            (c for c in node.children if c.type == cs.TS_PY_AS_PATTERN_TARGET),
            None,
        )
        target = alias.children[0] if alias and alias.children else None
    else:
        return None
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
        derived = _derived_python_binding(raw, handles)
        return None if derived is None else (target_name, derived)
    identity = literal_target(call, ctor.target_arg, ctor.target_kw)
    return target_name, HandleBinding(kind=ctor.kind, identity=identity)


def _derived_python_binding(
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


def inherited_python_handles(
    caller_node: Node,
    import_map: dict[str, str],
    ctor_by_name: dict[str, HandleConstructor],
) -> dict[str, HandleBinding]:
    # Handle bindings visible from ENCLOSING scopes, walked innermost-first so a
    # nearer scope shadows a farther one (setdefault keeps the first seen). An
    # enclosing class contributes its `self.<attr>` handles (set in any method);
    # an enclosing function/module contributes its top-level local handles.
    # Nested scopes are pruned; their locals are not visible.
    handles: dict[str, HandleBinding] = {}
    class_scanned = False
    node = caller_node.parent
    while node is not None:
        if node.type == cs.TS_PY_CLASS_DEFINITION:
            if not class_scanned:
                class_scanned = True
                _collect_self_attr_handles(node, import_map, ctor_by_name, handles)
        elif node.type in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_MODULE):
            _collect_scope_var_handles(node, import_map, ctor_by_name, handles)
        node = node.parent
    return handles


def _collect_scope_var_handles(
    scope_node: Node,
    import_map: dict[str, str],
    ctor_by_name: dict[str, HandleConstructor],
    handles: dict[str, HandleBinding],
) -> None:
    # Top-level handle bindings of one scope's OWN body; nested defs/classes are
    # pruned (their locals belong to their own scope, not this one).
    stack = list(scope_seed_nodes(scope_node))
    while stack:
        node = stack.pop()
        if node.type in PY_SCOPE_BOUNDARIES:
            continue
        bound = python_binding_from_node(node, import_map, ctor_by_name, handles)
        if bound is not None:
            handles.setdefault(bound[0], bound[1])
        stack.extend(node.children)


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
        bound = python_binding_from_node(node, import_map, ctor_by_name, handles)
        if bound is not None and bound[0].startswith(cs.PY_SELF_PREFIX):
            handles.setdefault(bound[0], bound[1])
        stack.extend(node.children)


def python_handles_before(
    node: Node,
    import_map: dict[str, str],
    ctor_by_name: dict[str, HandleConstructor],
) -> dict[str, HandleBinding]:
    # The handle bindings visible at `node`, for a walk that keeps no handle map
    # of its own (the flow walk): the enclosing scope's bindings that end before
    # `node`, in source order (a rebind wins), over the inherited ones. A plain
    # name assigned anywhere in the scope is local to all of it, so its
    # inherited binding is dropped, as in the I/O walk.
    scope = node.parent
    while scope is not None and scope.type not in (
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_MODULE,
    ):
        scope = scope.parent
    if scope is None:
        return {}
    handles = inherited_python_handles(scope, import_map, ctor_by_name)
    for name in python_locally_assigned_names(scope):
        if cs.SEPARATOR_DOT not in name:
            handles.pop(name, None)
    stack = list(reversed(scope_seed_nodes(scope)))
    while stack:
        current = stack.pop()
        if current.type in PY_SCOPE_BOUNDARIES or current.start_byte >= node.start_byte:
            continue
        if current.end_byte <= node.start_byte:
            bound = python_binding_from_node(current, import_map, ctor_by_name, handles)
            if bound is not None:
                handles[bound[0]] = bound[1]
        stack.extend(reversed(current.children))
    return handles


def python_stream_target(
    call_node: Node,
    sink: IOSink,
    import_map: dict[str, str],
    handles: Callable[[], Mapping[str, HandleBinding]],
) -> tuple[ResourceKind, str] | None:
    # Where a stream-parameterised sink writes (issue #2776): `print(x)` and
    # `print(x, file=None)` go to the sink's own stream, `file=sys.stderr` to
    # stderr, and `file=f` to whatever `f` was opened on. A stream the walk
    # cannot name (a parameter, an attribute of an unknown object) is None:
    # the write is real, but claiming stdout for it would be wrong. `handles`
    # is asked for only when the stream is a plain name.
    args = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
    stream = (
        None
        if args is None or sink.stream_kw is None
        else keyword_value(args, sink.stream_kw)
    )
    if stream is None or stream.type == cs.TS_PY_NONE:
        return sink.kind, literal_target(call_node, sink.target_arg, sink.target_kw)
    if stream.text is None:
        return None
    name = stream.text.decode(cs.ENCODING_UTF8)
    std = registry_match(PY_STD_STREAMS, name, import_map)
    if std is not None:
        return std, DYNAMIC_TARGET
    binding = handles().get(name)
    if binding is None:
        return None
    return binding.kind, binding.identity
