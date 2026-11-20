from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import LANGUAGE_FQN_SPECS
from ...utils.fqn_resolver import scoped_name_parts
from ..utils import safe_decode_text

# Where an anonymous class's anchor stops: the nearest type or namespace,
# which the FQN scope walk names on its own.
_ANCHOR_STOP_TYPES = frozenset(cs.FQN_PHP_SCOPE_TYPES) | frozenset(
    cs.SPEC_PHP_CLASS_TYPES
)
_CALLABLE_TYPES = frozenset(cs.FQN_PHP_FUNCTION_TYPES)


def php_insteadof_exclusions(type_node: Node) -> frozenset[tuple[str, str]]:
    """(method, trait) pairs a class's `insteadof` clauses exclude:
    `use A, B { A::label insteadof B; }` gives ("label", "b"), the
    ASCII-folded simple name of the trait whose `label` the class does not
    take (Greptile on PR #3240). PHP names are case-insensitive."""
    found: set[tuple[str, str]] = set()
    for body in type_node.children:
        if body.type != cs.TS_PHP_DECLARATION_LIST:
            continue
        for use in body.children:
            if use.type != cs.TS_PHP_USE_DECLARATION:
                continue
            for listed in use.children:
                if listed.type == cs.TS_PHP_USE_LIST:
                    found.update(_insteadof_pairs(listed))
    return frozenset(found)


def _insteadof_pairs(use_list: Node) -> Iterator[tuple[str, str]]:
    for clause in use_list.named_children:
        if clause.type != cs.TS_PHP_USE_INSTEAD_OF_CLAUSE or not clause.named_children:
            continue
        # `A::label` then each excluded trait, a `name` or a `qualified_name`.
        access, *excluded = clause.named_children
        method = access.named_children[-1] if access.named_children else None
        if not (method_name := safe_decode_text(method)):
            continue
        for trait in excluded:
            names = [trait] if trait.type == cs.TS_PHP_NAME else trait.named_children
            if names and (simple := safe_decode_text(names[-1])):
                yield method_name.lower(), simple.lower()


def _positional_name(node: Node) -> str:
    # The shape a nameless function takes in the definition pass
    # (`anonymous_<row>_<col>`, 0-based), so anonymous classes and closures
    # read alike and a re-index of the same source names each one the same.
    return f"{cs.PREFIX_ANONYMOUS}{node.start_point[0]}_{node.start_point[1]}"


def anonymous_class_name(node: Node) -> str | None:
    """`anonymous_<row>_<col>` for a PHP anonymous class, else None.

    The position is the `class` keyword's, so two anonymous classes in one
    file never share a name.
    """
    if node.type != cs.TS_PHP_ANONYMOUS_CLASS:
        return None
    return _positional_name(node)


def _anchor_walk(node: Node) -> tuple[list[Node], Node | None]:
    # The callables between an anonymous class and its anchor stop, innermost
    # first, and the stop itself (None only off a detached subtree).
    callables: list[Node] = []
    current = node.parent
    while current is not None and current.type not in _ANCHOR_STOP_TYPES:
        if current.type in _CALLABLE_TYPES:
            callables.append(current)
        current = current.parent
    return callables, current


def _registered_segments(callables: list[Node]) -> list[str]:
    # The segments the definition pass registers the innermost of `callables`
    # under, below the anchor stop. A method or named function registers
    # under its type scope alone (`Box.inner` for a function declared in
    # `Box::run`); a closure or arrow fn registers under the NAMED callables
    # around it, an enclosing closure adding nothing (`Box.run.anonymous_8_21`
    # inside another closure in `run`).
    if not callables:
        return []
    innermost, *outer = callables
    if name := _declared_name(innermost):
        return [name]
    named = [name for c in reversed(outer) if (name := _declared_name(c))]
    return [*named, _positional_name(innermost)]


def anonymous_class_scope_name(node: Node) -> str | None:
    """The anonymous class's qn segment: its name under the callable it is
    written in, `add.anonymous_19_25` for one built in `Dispatcher::add`.

    PHP's FQN scopes are types and namespaces only, so without the callable
    the class would sit directly under `Dispatcher` beside its methods. The
    callable's part is spelled as that callable is registered, so the class's
    qn prefix is the qn of the node that DEFINES it: a class built in a
    function declared inside `Box::run` is `Box.inner.anonymous_9_23`, not
    `Box.run.inner.anonymous_9_23` under a parent `Box.inner`.
    """
    if (name := anonymous_class_name(node)) is None:
        return None
    callables, _stop = _anchor_walk(node)
    return cs.SEPARATOR_DOT.join([*_registered_segments(callables), name])


def anonymous_class_anchor_stop(node: Node) -> Node | None:
    """The nearest type or namespace above a PHP anonymous class.

    Everything between the class and this node is already spelled by the
    class's scope name, so a walk naming a closure inside the class resumes
    here rather than naming those callables a second time, differently.
    """
    if node.type != cs.TS_PHP_ANONYMOUS_CLASS:
        return None
    return _anchor_walk(node)[1]


def anonymous_class_qn(node: Node, module_qn: str) -> str | None:
    """The qn the definition pass registers an anonymous class under.

    The call pass's own class-qn builder walks class ancestors only and
    would drop the enclosing callables, so it asks for this instead.
    """
    if node.type != cs.TS_PHP_ANONYMOUS_CLASS:
        return None
    parts = scoped_name_parts(
        node, LANGUAGE_FQN_SPECS[cs.SupportedLanguage.PHP], module_qn, None
    )
    return cs.SEPARATOR_DOT.join([module_qn, *parts])


def _declared_name(node: Node) -> str | None:
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    return (safe_decode_text(name_node) or None) if name_node is not None else None
