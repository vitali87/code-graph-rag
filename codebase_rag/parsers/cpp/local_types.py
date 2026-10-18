"""C++ types written inside a function body (issue #2555).

A local class, struct, union or enum is scoped under the callable it is
written in, `<enclosing callable qn>.<Name>`, the way a Python or Java local
class already is. An unnamed one that holds member functions (a functor such
as fmt's `struct { void operator()(int) } enter_state;`) takes the position
name every nameless definition gets, `anonymous_<row>_<col>` (0-based), as a
PHP anonymous class does. An unnamed one with no member functions has nothing
to attribute a call to, and stays without a node.

The definition pass and the call pass both name these types through
`local_type_qn`, from the same recorded function locations, so the two agree.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterator, Mapping

from tree_sitter import Node

from ... import constants as cs
from ...function_registry import FunctionRegistryTrie
from ...types_defs import FunctionLocation, FunctionSpanKey, NodeType
from ..utils import function_span_key, safe_decode_text
from . import utils as cpp_utils
from .overloads import cpp_overload_signature

_CALLABLE_TYPES = frozenset(
    {cs.CppNodeType.FUNCTION_DEFINITION, cs.TS_CPP_LAMBDA_EXPRESSION}
)
_LOCAL_TYPE_TYPES = frozenset(cs.CPP_COMPOUND_TYPES)
# Only these can hold member functions; an enum never needs a position name.
_MEMBER_BEARING_TYPES = cs.CPP_TYPE_SPECIFIER_NODE_TYPES
# A type reached through one of these is at file/namespace scope, not local.
_NON_LOCAL_SCOPE_TYPES = frozenset(
    {
        cs.CppNodeType.TRANSLATION_UNIT,
        cs.CppNodeType.NAMESPACE_DEFINITION,
        cs.TS_CPP_LINKAGE_SPECIFICATION,
    }
)
# Nodes whose children are file- or namespace-scope declarations.
_FILE_SCOPE_CONTAINER_TYPES = (
    _NON_LOCAL_SCOPE_TYPES | cs.CPP_DECLARATION_CONTAINER_TYPES
)
# Nodes a named class body can sit in, at file, namespace or class scope.
_CLASS_SCOPE_CONTAINER_TYPES = _FILE_SCOPE_CONTAINER_TYPES | {
    cs.CppNodeType.DECLARATION,
    cs.CppNodeType.FIELD_DECLARATION,
    cs.CppNodeType.TYPE_DEFINITION,
}
# The class-body nodes the class pass registers a member function from, and
# the ones it looks through for more.
_MEMBER_TYPES = frozenset(
    {
        cs.CppNodeType.FIELD_DECLARATION,
        cs.CppNodeType.DECLARATION,
        cs.CppNodeType.FUNCTION_DEFINITION,
    }
)
_MEMBER_CONTAINER_TYPES = cs.CPP_DECLARATION_CONTAINER_TYPES | {
    cs.CppNodeType.FRIEND_DECLARATION
}
_POSITIONAL_NAME_RE = re.compile(rf"{re.escape(cs.PREFIX_ANONYMOUS)}\d+_\d+")


def _is_callable(node: Node) -> bool:
    return (
        node.type in _CALLABLE_TYPES
        and node.child_by_field_name(cs.FIELD_BODY) is not None
    )


def positional_name(node: Node) -> str:
    return f"{cs.PREFIX_ANONYMOUS}{node.start_point[0]}_{node.start_point[1]}"


def is_positional_name(name: str) -> bool:
    return _POSITIONAL_NAME_RE.fullmatch(name) is not None


def _written_name(type_node: Node) -> str | None:
    name_node = type_node.child_by_field_name(cs.FIELD_NAME)
    return safe_decode_text(name_node) if name_node is not None else None


def _has_member_function(type_node: Node) -> bool:
    # Any depth: an unnamed struct wrapping a nested functor needs a node too,
    # or the nested one would hang off a segment nothing defines.
    body = type_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return False
    stack = list(body.children)
    while stack:
        node = stack.pop()
        if node.type == cs.CppNodeType.FUNCTION_DEFINITION:
            return True
        stack.extend(node.children)
    return False


def _segment(type_node: Node) -> str:
    return _written_name(type_node) or positional_name(type_node)


def _enclosing_scopes(node: Node) -> Iterator[Node]:
    # The functions, lambdas and class-like types whose bodies hold `node`,
    # innermost first, `node` itself included when it is one.
    current: Node | None = node
    while current is not None:
        if _is_callable(current) or current.type in _LOCAL_TYPE_TYPES:
            yield current
        current = current.parent


def scope_anchor_qns(
    node: Node,
    module_qn: str,
    function_locations: Mapping[FunctionSpanKey, FunctionLocation],
) -> list[str]:
    """The qns the local types visible at `node` are named under, innermost
    first: each enclosing callable's, and each enclosing local type's. A type
    nested in a local type hides a same-named outer type inside that type's
    members, as C++ name lookup does (`f.A.B` over a namespace-level `B` in
    `f.A.run`)."""
    anchors: list[str] = []
    for scope in _enclosing_scopes(node):
        anchor = (
            callable_anchor_qn(scope, module_qn, function_locations)
            if _is_callable(scope)
            else local_type_qn(scope, module_qn, function_locations)
        )
        if anchor is not None:
            anchors.append(anchor)
    return anchors


def _local_scope(type_node: Node) -> tuple[Node, list[Node]] | None:
    # The callable a type is written in, and the local types between the two
    # (outermost first); None for a type at file, namespace or class scope.
    scopes: list[Node] = []
    current = type_node.parent
    while current is not None:
        if _is_callable(current):
            scopes.reverse()
            return current, scopes
        if current.type in _NON_LOCAL_SCOPE_TYPES:
            return None
        if current.type in _LOCAL_TYPE_TYPES:
            scopes.append(current)
        current = current.parent
    return None


def is_local_type(type_node: Node) -> bool:
    """True when `type_node` is written inside a function or lambda body."""
    return _local_scope(type_node) is not None


def callable_anchor_qn(
    callable_node: Node,
    module_qn: str,
    function_locations: Mapping[FunctionSpanKey, FunctionLocation],
) -> str | None:
    """The qn a local type of `callable_node` is named under.

    An out-of-line method (`int Widget::run() {...}`) is named from what is
    written, the file's namespaces plus `Widget.run`, and the marker of the
    overload it defines (`Widget.run@8`): its node binds to the class only
    once every file is parsed, and a header parsed later would otherwise give
    the same source two names across runs. With its class in the same file,
    that is the very qn of its Method node. Any other callable uses the qn
    the definition pass registered for it, a duplicate marker included
    (`over@29`), so each overload's local types stay its own.
    """
    if cpp_utils.is_out_of_class_method_definition(callable_node):
        if (written := _out_of_class_written_name(callable_node)) is None:
            return None
        return cpp_utils.build_qualified_name(
            callable_node, module_qn, written + _overload_marker(callable_node)
        )
    recorded = function_locations.get(function_span_key(module_qn, callable_node))
    return recorded.qualified_name if recorded is not None else None


def _out_of_class_written_name(callable_node: Node) -> str | None:
    # `Widget.run` for `int ui::Widget::run(...) {...}`, as written.
    method_name = cpp_utils.extract_function_name(callable_node)
    if not method_name:
        return None
    class_name = cpp_utils.extract_class_name_from_out_of_class_method(callable_node)
    if not class_name:
        return None
    written = class_name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
    return f"{written}{cs.SEPARATOR_DOT}{method_name}"


def _overload_marker(callable_node: Node) -> str:
    # Out-of-class overloads (`Widget::run(int)`, `Widget::run(double)`)
    # share the written name, so each carries the marker of the Method node
    # it defines (#2455): none for the overload its class body declares
    # first, `@<line>` of its declaration for another (`run@8`). Its local
    # types then hang off that node's name and stay its own in both the
    # definition and the call pass (#2631 re-review). Read from the file
    # alone, so a header parsed later cannot move it between runs.
    root = callable_node
    while root.parent is not None:
        root = root.parent
    return _overload_markers(root).get(callable_node.start_byte, "")


@functools.lru_cache(maxsize=16)
def _overload_markers(root: Node) -> dict[int, str]:
    # One pass over the file's declarations, reopened namespace blocks
    # included: start byte -> marker for each out-of-class definition whose
    # node is not the plain-named one. Cached per file, as every callable in
    # it asks.
    by_name: dict[str, list[Node]] = {}
    for definition in _out_of_class_definitions(root):
        if (written := _out_of_class_written_name(definition)) is not None:
            key = cpp_utils.build_qualified_name(definition, "", written)
            by_name.setdefault(key, []).append(definition)
    bodies = _class_bodies(root) if by_name else {}
    markers: dict[int, str] = {}
    for key, definitions in by_name.items():
        class_key, _, method_name = key.rpartition(cs.SEPARATOR_DOT)
        members = _members_named(bodies.get(class_key), method_name)
        if members or len(definitions) > 1:
            markers.update(_definition_markers(method_name, members, definitions))
    return markers


def _definition_markers(
    method_name: str, members: list[Node], definitions: list[Node]
) -> dict[int, str]:
    """Each definition's marker: the one the registry gives the Method node it
    defines when this file is all it has read.

    The class body's members register first, as the class pass registers
    them, then the definitions, as the deferred out-of-class pass does, so a
    definition lands on the overload its class declares (`run@8`, after the
    declaration's line) in whatever order the file defines them. With the
    class in another file, the first definition keeps the plain name and
    each other one takes `@<its line>`. A name already anchoring a body here
    (an inline overload's, or another definition's of the same signature)
    gives way to `@<its line>`, so two bodies' local types never share one.
    """
    registry = FunctionRegistryTrie()
    taken: set[str] = set()
    for member in members:
        registered = _register_overload(registry, method_name, member, in_class=True)
        if _has_body(member):
            taken.add(registered)
    markers: dict[int, str] = {}
    for definition in definitions:
        registered = _register_overload(
            registry, method_name, definition, in_class=False
        )
        if registered in taken:
            registered = _position_name(method_name, definition, taken)
        taken.add(registered)
        if marker := registered[len(method_name) :]:
            markers[definition.start_byte] = marker
    return markers


def _register_overload(
    registry: FunctionRegistryTrie, natural: str, node: Node, *, in_class: bool
) -> str:
    # What the class pass (`in_class`) or the deferred out-of-class pass
    # registers for `node`: the plain name when its parameter list cannot be
    # read, as both passes keep it.
    signature = cpp_overload_signature(node)
    registered = (
        natural
        if signature is None
        else registry.register_overload_qn(
            natural,
            signature,
            node.start_point[0] + 1,
            node.start_point[1],
            declared_in_class=in_class,
        )
    )
    registry[registered] = NodeType.METHOD
    return registered


def _position_name(natural: str, definition: Node, taken: set[str]) -> str:
    line, col = definition.start_point
    name = f"{natural}{cs.DUP_QN_MARKER}{line + 1}"
    if name in taken:
        name = f"{name}{cs.DUP_QN_COLUMN_MARKER}{col}"
    return name


def _class_bodies(root: Node) -> dict[str, Node]:
    # The bodies of the file's classes, nested ones included, under the key
    # an out-of-class definition's class takes in `_overload_markers`
    # (`.ui.Widget`); the first of two bodies with one key wins. Never into
    # a function body: no out-of-class definition names a class written there.
    bodies: dict[str, Node] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        children = node.named_children
        if node.type in _MEMBER_BEARING_TYPES:
            body = node.child_by_field_name(cs.FIELD_BODY)
            path = _class_path(node)
            if body is None or path is None:
                continue
            bodies.setdefault(cpp_utils.build_qualified_name(node, "", path), body)
            children = body.named_children
        elif node.type not in _CLASS_SCOPE_CONTAINER_TYPES:
            continue
        stack.extend(reversed(children))
    return bodies


def _class_path(type_node: Node) -> str | None:
    # `Outer.Inner` for `Inner` written in `Outer`'s body; None when it or a
    # class around it has no name.
    names: list[str] = []
    current: Node | None = type_node
    while current is not None and current.type not in _NON_LOCAL_SCOPE_TYPES:
        if current.type in _MEMBER_BEARING_TYPES:
            if (name := _written_name(current)) is None:
                return None
            names.append(name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT))
        current = current.parent
    return cs.SEPARATOR_DOT.join(reversed(names))


def _members_named(body: Node | None, method_name: str) -> list[Node]:
    # The members a class body declares or defines as `method_name`, in
    # source order: the nodes the class pass registers for it, a friend
    # included, and a member template definition at its `template` line.
    if body is None:
        return []
    members: list[Node] = []
    stack = list(reversed(body.named_children))
    while stack:
        node = stack.pop()
        if _is_member(node):
            if _member_name(node) == method_name:
                members.append(node)
        elif node.type in _MEMBER_CONTAINER_TYPES:
            stack.extend(reversed(node.named_children))
    return members


def _is_member(node: Node) -> bool:
    if node.type == cs.CppNodeType.TEMPLATE_DECLARATION:
        return _template_definition(node)
    return node.type in _MEMBER_TYPES


def _template_definition(node: Node) -> bool:
    return any(
        child.type == cs.CppNodeType.FUNCTION_DEFINITION
        for child in node.named_children
    )


def _member_name(node: Node) -> str | None:
    if cpp_utils.is_macro_invocation_artifact(node):
        return None
    return cpp_utils.extract_function_name(node)


def _has_body(member: Node) -> bool:
    return (
        member.type == cs.CppNodeType.TEMPLATE_DECLARATION
        or member.child_by_field_name(cs.FIELD_BODY) is not None
    )


def _out_of_class_definitions(root: Node) -> Iterator[Node]:
    # Out-of-class method definitions in source order, through namespaces,
    # `extern "C"`, templates and preprocessor blocks; never into a body.
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == cs.CppNodeType.FUNCTION_DEFINITION:
            if cpp_utils.is_out_of_class_method_definition(node):
                yield node
        elif node.type in _FILE_SCOPE_CONTAINER_TYPES:
            stack.extend(reversed(node.named_children))


def is_qualified_type(node: Node) -> bool:
    """True for a type spelled with a scope qualifier (`::B`, `ns::B`,
    `ns::B<int>`), which names that class and never a local one."""
    if node.type == cs.CppNodeType.TEMPLATE_TYPE:
        inner = node.child_by_field_name(cs.FIELD_NAME)
        return inner is not None and is_qualified_type(inner)
    return node.type == cs.CppNodeType.QUALIFIED_IDENTIFIER


def named_type(node: Node) -> str | None:
    """The bare class name a type or constructor spelling names: `Cmp` for
    `Cmp`, `ns::Cmp` and `Cmp<int>`; None for anything else."""
    match node.type:
        case cs.CppNodeType.TYPE_IDENTIFIER | cs.CppNodeType.IDENTIFIER:
            return safe_decode_text(node)
        case (
            cs.CppNodeType.QUALIFIED_IDENTIFIER
            | cs.CppNodeType.TEMPLATE_TYPE
            | cs.TS_CPP_TEMPLATE_FUNCTION
        ):
            inner = node.child_by_field_name(cs.FIELD_NAME)
            return named_type(inner) if inner is not None else None
    return None


def local_type_name(type_node: Node) -> str | None:
    """The name a local type's node carries; None when it gets no node."""
    if name := _written_name(type_node):
        return name
    if type_node.type in _MEMBER_BEARING_TYPES and _has_member_function(type_node):
        return positional_name(type_node)
    return None


def local_type_qn(
    type_node: Node,
    module_qn: str,
    function_locations: Mapping[FunctionSpanKey, FunctionLocation],
) -> str | None:
    """`<enclosing callable qn>.<local scopes>.<name>` for a type written in a
    function body; None for any other type, and for a local one that gets no
    node or whose enclosing callable has none."""
    if (scope := _local_scope(type_node)) is None:
        return None
    callable_node, scopes = scope
    if (name := local_type_name(type_node)) is None:
        return None
    if (
        anchor := callable_anchor_qn(callable_node, module_qn, function_locations)
    ) is None:
        return None
    return cs.SEPARATOR_DOT.join([anchor, *map(_segment, scopes), name])


def local_type_parent(type_node: Node) -> Node | None:
    """The node a local type is defined by: its callable when written straight
    in the body, else the local type it is nested in."""
    if (scope := _local_scope(type_node)) is None:
        return None
    callable_node, scopes = scope
    return scopes[-1] if scopes else callable_node


def in_local_type_member(node: Node, owner: Node) -> bool:
    """True when `node` sits in a member function of a type written inside
    `owner`: that member is its own caller, so the call is not `owner`'s."""
    in_function = False
    current = node.parent
    while current is not None and current != owner:
        if current.type == cs.CppNodeType.FUNCTION_DEFINITION:
            in_function = True
        elif current.type == cs.TS_CPP_FIELD_DECLARATION_LIST and in_function:
            return True
        current = current.parent
    return False
