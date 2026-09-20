"""Locate the string literal a dynamic call dispatches through (issue #1526).

A trace-only edge has no call expression in the caller's source; the call
went through `getattr(obj, "name")`, a registry keyed by `"name"`, or
something the static pass cannot see. The invariant, stated once: a
literal is the site of a dynamic edge only when its lookup expression is
INVOKED in the caller's body, either directly (`getattr(obj, "name")()`,
`table["name"]()`) or through a name bound from it and called later
(`fn = table["name"]; fn()`). A literal that is merely present (a dict key,
a lookup never called) is not a site. A nested `def`/`class`/`lambda` is
another callable's body and is not searched. Two candidate sites cannot be
told apart statically, and a computed dispatch (`getattr(obj, attr)`,
`table[key]()`, or a name bound from one and called, including one captured
from an enclosing scope unless the caller rebinds that name) could have
carried the call itself; in either case the edge is marked unlocatable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..parsers.utils import node_site_properties, safe_decode_text
from ..types_defs import PropertyDict

# A lambda counts as another callable's body too: the trace resolver drops
# `<lambda>` frames as synthetic, so a call made inside one is never
# attributed to the enclosing function and its literals cannot be that
# function's dispatch site.
# A literal binding under one of these did not necessarily run before a call
# below it: `fn = fallback; if c: fn = table["x"]; fn()` calls the fallback on
# the false branch, so the literal is not a definite site (#1543 review).
_CONDITIONAL_FLOW = frozenset(
    {
        cs.TS_PY_IF_STATEMENT,
        cs.TS_PY_FOR_STATEMENT,
        cs.TS_PY_WHILE_STATEMENT,
        cs.TS_PY_TRY_STATEMENT,
        cs.TS_PY_MATCH_STATEMENT,
        cs.TS_PY_WITH_STATEMENT,
    }
)
_NESTED_SCOPES = frozenset(
    {cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION, cs.TS_PY_LAMBDA}
)


def _literal_text(node: Node) -> str | None:
    if node.type != cs.TS_PY_STRING:
        return None
    for child in node.named_children:
        if child.type == cs.TS_PY_STRING_CONTENT:
            return safe_decode_text(child)
    return None


def _lookup_of(literal: Node) -> Node | None:
    """The lookup expression a dispatch-shaped literal keys: the `getattr`
    call it is the second argument of, or the subscript it indexes."""
    parent = literal.parent
    if parent is None:
        return None
    if parent.type == cs.TS_ARGUMENT_LIST:
        call = parent.parent
        func = call.child_by_field_name(cs.FIELD_FUNCTION) if call is not None else None
        if func is None or safe_decode_text(func) != cs.PY_BUILTIN_GETATTR:
            return None
        named = parent.named_children
        return call if len(named) >= 2 and named[1] == literal else None
    if parent.type == cs.TS_PY_SUBSCRIPT:
        key = parent.child_by_field_name(cs.TS_PY_FIELD_SUBSCRIPT)
        return parent if key == literal else None
    return None


def _through_parentheses(expr: Node) -> tuple[Node, Node | None]:
    """`expr` and its parent, seen past any parentheses that wrap `expr`.

    `(expr)()` and `name = (expr)` mean the same as their bare forms; every
    check that asks what `expr` is the child of climbs through the wrapping
    first (#1543 review).
    """
    value: Node = expr
    parent = value.parent
    while parent is not None and parent.type == cs.TS_PARENTHESIZED_EXPRESSION:
        value, parent = parent, parent.parent
    return value, parent


def _is_invoked(expr: Node) -> bool:
    """`expr` is the function of a call: `expr(...)` or `(expr)(...)`."""
    value, parent = _through_parentheses(expr)
    return (
        parent is not None
        and parent.type == cs.TS_PY_CALL
        and parent.child_by_field_name(cs.FIELD_FUNCTION) == value
    )


def _bound_name(expr: Node) -> str | None:
    """`name` when `expr` is the whole right side of `name = expr`.

    Seen through parentheses: `name = (expr)` binds the same lookup, and
    without the climb the literal form of a stored lookup was not recorded
    as the binding (#1543 review).
    """
    value, parent = _through_parentheses(expr)
    if parent is None or parent.type != cs.TS_PY_ASSIGNMENT:
        return None
    left = parent.child_by_field_name(cs.FIELD_LEFT)
    right = parent.child_by_field_name(cs.FIELD_RIGHT)
    if left is None or right != value or left.type != cs.TS_PY_IDENTIFIER:
        return None
    return safe_decode_text(left)


def _computed_lookup_target(node: Node) -> str | None:
    """The name bound by `name = table[key]` or `name = getattr(obj, attr)`
    with a non-literal key: a dispatch stored for a later call."""
    if node.type != cs.TS_PY_ASSIGNMENT:
        return None
    left = node.child_by_field_name(cs.FIELD_LEFT)
    # `fn = (registry[name])` stores the same lookup as the bare form
    # (#1543 review).
    right = _unparenthesised(node.child_by_field_name(cs.FIELD_RIGHT))
    if left is None or right is None or left.type != cs.TS_PY_IDENTIFIER:
        return None
    if right.type == cs.TS_PY_SUBSCRIPT:
        key = right.child_by_field_name(cs.TS_PY_FIELD_SUBSCRIPT)
        computed = key is not None and key.type != cs.TS_PY_STRING
    elif right.type == cs.TS_PY_CALL:
        func = right.child_by_field_name(cs.FIELD_FUNCTION)
        args = right.child_by_field_name(cs.FIELD_ARGUMENTS)
        named = args.named_children if args is not None else []
        computed = (
            func is not None
            and safe_decode_text(func) == cs.PY_BUILTIN_GETATTR
            and len(named) >= 2
            and named[1].type != cs.TS_PY_STRING
        )
    else:
        computed = False
    return safe_decode_text(left) if computed else None


def _called_identifier(node: Node) -> str | None:
    """The bare name a call invokes (`fn()`), if any."""
    if node.type != cs.TS_PY_CALL:
        return None
    func = _unparenthesised(node.child_by_field_name(cs.FIELD_FUNCTION))
    if func is None or func.type != cs.TS_PY_IDENTIFIER:
        return None
    return safe_decode_text(func)


def _unparenthesised(func: Node | None) -> Node | None:
    """The callee under any parentheses: `(fn)()`, `(table[key])()`.

    The call is still of what the parentheses hold, and without the unwrap
    a stored computed lookup invoked as `(fn)()` was not recognised as a
    call, and `(registry[name])()` was not recognised as a computed
    dispatch, so an unrelated invoked literal in the same body could be
    recorded as the site (#1543 review).
    """
    while func is not None and func.type == cs.TS_PARENTHESIZED_EXPRESSION:
        func = next(iter(func.named_children), None)
    return func


def _is_computed_dispatch(node: Node) -> bool:
    """`getattr(obj, name)` with a non-literal name, or `table[key](...)`.

    Either could have carried the traced call, so no literal in the same
    body can be trusted to be its site.
    """
    if node.type != cs.TS_PY_CALL:
        return False
    func = _unparenthesised(node.child_by_field_name(cs.FIELD_FUNCTION))
    if func is None:
        return False
    if func.type == cs.TS_PY_SUBSCRIPT:
        key = func.child_by_field_name(cs.TS_PY_FIELD_SUBSCRIPT)
        return key is not None and key.type != cs.TS_PY_STRING
    if safe_decode_text(func) != cs.PY_BUILTIN_GETATTR:
        return False
    args = node.child_by_field_name(cs.FIELD_ARGUMENTS)
    named = args.named_children if args is not None else []
    return len(named) >= 2 and named[1].type != cs.TS_PY_STRING


def locate_dispatch_literal(
    repo_root: Path, path: str, start_line: int, end_line: int, callee_name: str
) -> PropertyDict | None:
    """Site props of the one literal naming `callee_name` in the caller's body.

    Python only for now: other languages' dynamic dispatch has no single
    literal shape worth guessing at. Returns None when the file is not
    Python, cannot be read, holds no such literal in the caller's own body
    (nested definitions excluded), or holds more than one, since the scan
    cannot tell the traced dispatch site from an unrelated same-named
    literal and must not point a rewrite at the wrong one. A computed
    dispatch in the body (`getattr(obj, name)`, `table[key]()`) could have
    carried the call itself, so it makes the edge unlocatable too.
    """
    root = _python_root(repo_root / path)
    if root is None:
        return None
    found = _dispatch_literals(root, start_line, end_line, callee_name)
    if found is None or len(found) != 1:
        return None
    return node_site_properties(found[0])


def _python_root(file_path: Path) -> Node | None:
    """Parsed root of a Python file, or None when it is not one or unreadable."""
    if get_language_for_extension(file_path.suffix) != cs.SupportedLanguage.PYTHON:
        return None
    try:
        source = file_path.read_bytes()
    except OSError:
        return None
    parsers, _queries = load_parsers()
    parser = parsers.get(cs.SupportedLanguage.PYTHON)
    if parser is None:
        return None
    return parser.parse(source).root_node


def _outside_span(node: Node, start_line: int, end_line: int) -> bool:
    return node.end_point[0] + 1 < start_line or node.start_point[0] + 1 > end_line


def _assigned_identifier(node: Node) -> str | None:
    """`name` when `node` is `name = <anything>`."""
    if node.type != cs.TS_PY_ASSIGNMENT:
        return None
    left = node.child_by_field_name(cs.FIELD_LEFT)
    if left is None or left.type != cs.TS_PY_IDENTIFIER:
        return None
    return safe_decode_text(left) or ""


@dataclass
class _BodyScan:
    """What one walk over a caller's body has found so far.

    The walk itself is in `_dispatch_literals`; this holds the facts it
    gathers and the two judgements made from them, so each can be read on
    its own.
    """

    callee_name: str
    # Literals whose lookup is invoked on the spot: `getattr(obj, "name")()`.
    direct: list[Node] = field(default_factory=list)
    # name -> the literals whose lookups bound it (`fn = table["name"]`), and
    # the earliest line such a binding appears at: a binding below the first
    # call of the name cannot have supplied that call's value either.
    bound_literal: dict[str, list[Node]] = field(default_factory=dict)
    bound_literal_line: dict[str, int] = field(default_factory=dict)
    # Names whose literal binding sits under conditional control flow.
    conditionally_bound: set[str] = field(default_factory=set)
    # Computed names: bound from a non-literal lookup, in the body or in an
    # enclosing scope; a body assignment of any other kind masks an outer one,
    # but only for calls AFTER it. `rebound_inner` and `called` therefore
    # record the earliest line each name is rebound and called at: a rebind
    # below the first call cannot have supplied that call's value, so it must
    # not hide the enclosing computed binding that did (#1543 review).
    stored_outer: set[str] = field(default_factory=set)
    stored_inner: set[str] = field(default_factory=set)
    rebound_inner: dict[str, int] = field(default_factory=dict)
    called: dict[str, int] = field(default_factory=dict)

    def note_enclosing(self, node: Node) -> None:
        """A statement of an enclosing scope, visible to the caller."""
        if (target := _computed_lookup_target(node)) is not None:
            self.stored_outer.add(target)

    def note_body(self, node: Node) -> bool:
        """A node of the caller's own body.

        False when the node is a computed dispatch, which could have carried
        the traced call itself and makes the edge unlocatable.
        """
        if _is_computed_dispatch(node):
            return False
        line = node.start_point[0] + 1
        if (target := _computed_lookup_target(node)) is not None:
            self.stored_inner.add(target)
        elif (rebound := _assigned_identifier(node)) is not None:
            self._note_first(self.rebound_inner, rebound, line)
        if (name := _called_identifier(node)) is not None:
            self._note_first(self.called, name, line)
        self._note_literal(node)
        return True

    @staticmethod
    def _note_first(seen: dict[str, int], name: str, line: int) -> None:
        # The walk is a stack, not source order, so keep the smallest line.
        if line < seen.get(name, line + 1):
            seen[name] = line

    def _note_literal(self, node: Node) -> None:
        if _literal_text(node) != self.callee_name:
            return
        lookup = _lookup_of(node)
        if lookup is None:
            return
        if _is_invoked(lookup):
            self.direct.append(node)
        elif (bound := _bound_name(lookup)) is not None:
            # Which of several bindings supplied the value at the call
            # (order, branches, loops) is data flow the scan does not
            # do; a name bound from a literal lookup MORE than once is
            # therefore unlocatable rather than guessed at.
            self.bound_literal.setdefault(bound, []).append(node)
            self._note_first(self.bound_literal_line, bound, node.start_point[0] + 1)
            if _under_conditional_flow(lookup):
                self.conditionally_bound.add(bound)

    def sites(self) -> list[Node] | None:
        """The located sites, or None when the body makes the edge unlocatable."""
        # A rebinding masks an enclosing computed name only if it precedes
        # every call of that name; one after the first call leaves that call
        # bound to the outer computed value, so the edge stays unlocatable.
        masking = {
            name
            for name, line in self.rebound_inner.items()
            if name not in self.called or line < self.called[name]
        }
        computed = self.stored_inner | (self.stored_outer - masking)
        if computed & self.called.keys():
            return None
        if any(
            name in self.called and len(lits) > 1
            for name, lits in self.bound_literal.items()
        ):
            return None
        # A literal binding below the name's first call did not supply that
        # call: whatever did (an enclosing binding, or nothing) is what the
        # traced edge went through, so the later literal is not its site
        # (#1543 review).
        if any(
            name in self.called and line > self.called[name]
            for name, line in self.bound_literal_line.items()
        ):
            return None
        # A literal binding under an `if`, a loop, a `try` or a `match` ran
        # on some paths to the call and not others; whether THIS call used it
        # or a fallback bound elsewhere is data flow the scan does not do, so
        # the site is unlocatable rather than guessed at (#1543 review).
        if any(name in self.called for name in self.conditionally_bound):
            return None
        return self.direct + [
            lits[0] for name, lits in self.bound_literal.items() if name in self.called
        ]


def _under_conditional_flow(node: Node) -> bool:
    """Whether `node` sits under conditional or looping control flow within its
    own callable: the walk stops at the first enclosing definition, whose
    body is unconditional relative to the call being located."""
    current = node.parent
    while current is not None and current.type not in _NESTED_SCOPES:
        if current.type in _CONDITIONAL_FLOW:
            return True
        current = current.parent
    return False


def _visit_enclosing(node: Node, scan: _BodyScan) -> bool:
    """A node outside the caller's span; returns whether to walk its children.

    A sibling scope's body is another callable's; an enclosing scope's
    statements are visible to the caller, so a computed callable captured
    from there still counts as stored.
    """
    if node.type in _NESTED_SCOPES:
        return False
    scan.note_enclosing(node)
    return True


def _visit_body(
    node: Node, inside_caller: bool, start_line: int, scan: _BodyScan
) -> tuple[bool, bool] | None:
    """A node inside the span: (walk children, inside_caller), or None to give up.

    The first definition that begins inside the span is the caller itself;
    any definition met below it is a nested callable whose literals belong
    to that callable, not to this edge.
    """
    if node.type in _NESTED_SCOPES and node.start_point[0] + 1 >= start_line:
        if inside_caller:
            return False, inside_caller
        inside_caller = True
    if not scan.note_body(node):
        return None
    return True, inside_caller


def _dispatch_literals(
    root: Node, start_line: int, end_line: int, callee_name: str
) -> list[Node] | None:
    """The invoked dispatch literals naming `callee_name` in the caller's body.

    None when the body holds a computed dispatch, which could have carried
    the call itself.
    """
    scan = _BodyScan(callee_name)
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, inside_caller = stack.pop()
        if _outside_span(node, start_line, end_line):
            descend = _visit_enclosing(node, scan)
        else:
            step = _visit_body(node, inside_caller, start_line, scan)
            if step is None:
                return None
            descend, inside_caller = step
        if descend:
            stack.extend((child, inside_caller) for child in node.children)
    return scan.sites()
