"""Which bindings of a local reach each call in one Python body.

cgr types a local once per body: the flat map gives `a` the type of whichever
assignment its passes settle on, so in

    a = b = func1
    b()
    a = b = func2
    a()

both calls see the same binding, and in a body that rebinds `c` on each arm
of an `if`, every call through `c` resolves to one arm's class. JARVIS
(Huang et al., arXiv 2305.05949) builds call graphs from a flow-sensitive
type graph per function instead: a later binding replaces an earlier one on
the same path (a strong update), the graph is copied where control flow
splits and merged where it joins.

This module is that idea at the granularity the call resolver consumes: for
each call whose base name the body binds, the set of binding sites that can
reach the call. A path that returns, raises, breaks or
continues stops contributing where it ends; a loop merges its entry with its
body until nothing changes; a handler may start anywhere in its `try`.
`rebound` names the locals bound more than once: only through those can a
receiver's type at a call differ from the flat map's.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text
from .patterns import case_always_matches

# Statements whose bodies are other scopes: a call inside them is not this
# body's, and their names bind here only as the definition itself.
_SCOPES = frozenset(
    {cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION, cs.TS_PY_LAMBDA}
)
_COMPREHENSIONS = frozenset(
    {
        cs.TS_PY_LIST_COMPREHENSION,
        cs.TS_PY_SET_COMPREHENSION,
        cs.TS_PY_DICTIONARY_COMPREHENSION,
        cs.TS_PY_GENERATOR_EXPRESSION,
    }
)
_PATTERNS = frozenset(
    {
        cs.TS_PY_PATTERN_LIST,
        cs.TS_PY_TUPLE_PATTERN,
        cs.TS_PY_LIST_PATTERN,
        cs.TS_PY_TUPLE,
        cs.TS_PY_LIST,
    }
)
_SEQUENCES = frozenset({cs.TS_PY_EXPRESSION_LIST, cs.TS_PY_TUPLE, cs.TS_PY_LIST})
_SPLATS = frozenset({cs.TS_PY_LIST_SPLAT_PATTERN, cs.TS_PY_LIST_SPLAT})
_BREAK = "break_statement"
_CONTINUE = "continue_statement"
# A loop body whose merge has not settled after this many rounds is given up
# on: its names keep only what the rounds so far established, and calls in it
# fall back to the flat map (they are left untracked).
_MAX_LOOP_ROUNDS = 8


class BindingKind(StrEnum):
    # The name's value on entry to the body: a parameter, a global, an import
    # done elsewhere. Unknown here.
    ENTRY = "entry"
    # `name = value`, a chained or unpacked target, or a walrus: `value` is
    # the expression this target receives, when it can be paired with one.
    ASSIGN = "assign"
    # `def name` / `class name` in this body.
    DEFINITION = "definition"
    # Bound by something whose value this module does not follow: an
    # import, a loop or with target, an except alias, an augmented
    # assignment, a starred or unpaired unpacking target, `del`.
    OPAQUE = "opaque"


class Binding(NamedTuple):
    kind: BindingKind
    site: int
    value: Node | None = None
    definition: Node | None = None


ENTRY = Binding(BindingKind.ENTRY, -1)

State = dict[str, frozenset[Binding]]


_EVAL = "eval"
_RECORD = "record"
_BIND = "bind"
_RESTORE = "restore"
_MAYBE = "maybe"
_EITHER = "either"


class _Step(NamedTuple):
    # One unit of the iterative expression walk: evaluate a node, record a
    # call against the state at that point, apply a walrus binding, put back
    # the state a comprehension narrowed (`own`: its targets), or evaluate an
    # operand that may not run (`x and y`) or one of two that may (`a if c
    # else b`) and join the paths.
    op: str
    node: Node | None
    saved: State | None = None
    own: frozenset[str] = frozenset()


@dataclass
class _Loop:
    breaks: list[State] = field(default_factory=list)
    continues: list[State] = field(default_factory=list)


def _text(node: Node | None) -> str | None:
    return safe_decode_text(node) if node is not None else None


def _merge(states: Iterable[State | None]) -> State | None:
    live = [s for s in states if s is not None]
    if not live:
        return None
    merged: State = {}
    for state in live:
        for name, bindings in state.items():
            merged[name] = merged.get(name, frozenset()) | bindings
    # A name no binding reached on some path holds its entry value there.
    if len(live) > 1:
        for name, bindings in merged.items():
            if any(name not in state for state in live):
                merged[name] = bindings | {ENTRY}
    return merged


def call_base(call: Node) -> str | None:
    """The local a call goes through: `f` in `f()`, `a` in `a.b.c()`."""
    fn = call.child_by_field_name(cs.FIELD_FUNCTION)
    while fn is not None and fn.type == cs.TS_PY_ATTRIBUTE:
        fn = fn.child_by_field_name(cs.FIELD_OBJECT)
    return _text(fn) if fn is not None and fn.type == cs.TS_PY_IDENTIFIER else None


def _escaped_names(body: Node) -> frozenset[str]:
    """Names the body declares `global` or `nonlocal`: their bindings are
    another scope's, and any call in between may rebind them there."""
    text = body.text or b""
    if b"global" not in text and b"nonlocal" not in text:
        return frozenset()
    names: set[str] = set()
    stack = list(body.children)
    while stack:
        node = stack.pop()
        if node.type in (cs.TS_PY_GLOBAL_STATEMENT, cs.TS_PY_NONLOCAL_STATEMENT):
            names.update(t for c in node.named_children if (t := _text(c)) is not None)
        elif node.type not in _SCOPES:
            stack.extend(node.children)
    return frozenset(names)


def _identifiers(target: Node | None) -> list[str]:
    if target is None:
        return []
    if target.type == cs.TS_PY_IDENTIFIER:
        name = _text(target)
        return [name] if name else []
    if target.type in _PATTERNS or target.type in _SPLATS:
        return [n for c in target.named_children for n in _identifiers(c)]
    return []


class FlowBindings:
    """Reaching bindings for the calls of one body, computed once."""

    def __init__(self, body: Node) -> None:
        # A name enters the state when something binds it; until then it
        # holds its entry value, which is why no pre-scan of the body is
        # needed. Its binding sites are counted as the walk meets them.
        self._sites: dict[str, set[int]] = {}
        self._escaped = _escaped_names(body)
        self._reaching: dict[int, frozenset[Binding]] = {}
        self._names: dict[int, frozenset[Binding]] = {}
        self._loops: list[_Loop] = []
        self._unsettled: set[int] = set()
        # What each statement does to the state; any other statement only
        # evaluates its expressions.
        self._statements: dict[str, Callable[[Node, State], State | None]] = {
            cs.TS_PY_EXPRESSION_STATEMENT: self._expression_statements,
            cs.TS_PY_IF_STATEMENT: self._if,
            cs.TS_PY_FOR_STATEMENT: self._loop,
            cs.TS_PY_WHILE_STATEMENT: self._loop,
            cs.TS_PY_TRY_STATEMENT: self._try,
            cs.TS_PY_WITH_STATEMENT: self._with,
            cs.TS_PY_MATCH_STATEMENT: self._match,
            cs.TS_PY_RETURN_STATEMENT: self._end,
            cs.TS_PY_RAISE_STATEMENT: self._end,
            _BREAK: self._break,
            _CONTINUE: self._continue,
            cs.TS_PY_DECORATED_DEFINITION: self._decorated,
            cs.TS_PY_FUNCTION_DEFINITION: self._definition,
            cs.TS_PY_CLASS_DEFINITION: self._definition,
            cs.TS_PY_IMPORT_STATEMENT: self._opaque,
            cs.TS_PY_IMPORT_FROM_STATEMENT: self._opaque,
            cs.TS_PY_DELETE_STATEMENT: self._opaque,
        }
        # The steps of the expression walk other than evaluating a node, and
        # the expressions whose parts do not simply evaluate in order.
        self._steps: dict[str, Callable[[_Step, State], State]] = {
            _RESTORE: self._restore,
            _MAYBE: self._maybe,
            _EITHER: self._either,
            _RECORD: self._record,
            _BIND: self._bind_walrus,
        }
        self._evaluators: dict[str, Callable[[Node, State, list[_Step]], State]] = {
            cs.TS_PY_IDENTIFIER: self._read,
            cs.TS_PY_NAMED_EXPRESSION: self._walrus,
            cs.TS_PY_BOOLEAN_OPERATOR: self._short_circuit,
            cs.TS_PY_CONDITIONAL_EXPRESSION: self._conditional,
            cs.TS_PY_CALL: self._call,
            **dict.fromkeys(_COMPREHENSIONS, self._comprehension),
        }
        self._block(body.named_children, {})

    @property
    def tracked(self) -> frozenset[str]:
        return frozenset(self._sites)

    @property
    def rebound(self) -> frozenset[str]:
        """Names bound at two or more sites: the only ones whose type at a
        call can differ from the flat map's."""
        return frozenset(n for n, sites in self._sites.items() if len(sites) > 1)

    def _set(self, state: State, name: str | None, binding: Binding) -> State:
        if not name or name in self._escaped:
            return state
        self._sites.setdefault(name, set()).add(binding.site)
        return {**state, name: frozenset({binding})}

    @classmethod
    def of(cls, caller: Node) -> FlowBindings:
        """The analysis for a caller: a module, or a def or class whose
        statements are its body block (its decorators, defaults and bases
        evaluate in the enclosing scope, not here)."""
        body = caller
        if caller.type in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION):
            body = caller.child_by_field_name(cs.FIELD_BODY) or caller
        return cls(body)

    def at(self, call: Node) -> frozenset[Binding] | None:
        """The bindings of `call`'s base name that reach it, or None when the
        call is not one this analysis decides (its base is not rebound, it
        is unreachable, or its loop never settled)."""
        if call.start_byte in self._unsettled:
            return None
        return self._reaching.get(call.start_byte)

    def name_at(self, name: Node) -> frozenset[Binding] | None:
        """The bindings an identifier read sees: what `b` holds in `a = b`."""
        if name.start_byte in self._unsettled:
            return None
        return self._names.get(name.start_byte)

    # Statements -----------------------------------------------------------

    def _block(self, nodes: Iterable[Node], state: State | None) -> State | None:
        for node in nodes:
            if state is None:
                return None
            state = self._statement(node, state)
        return state

    def _body(self, node: Node | None, state: State | None) -> State | None:
        if node is None or state is None:
            return state
        return self._block(node.named_children, state)

    def _statement(self, node: Node, state: State) -> State | None:
        if (handler := self._statements.get(node.type)) is not None:
            return handler(node, state)
        return self._expressions(node.named_children, state)

    def _expression_statements(self, node: Node, state: State) -> State:
        for child in node.named_children:
            state = self._expression_statement(child, state)
        return state

    def _end(self, node: Node, state: State) -> None:
        # `return` / `raise`: the value is evaluated, then the path ends.
        self._expressions(node.named_children, state)

    def _break(self, node: Node, state: State) -> None:
        if self._loops:
            self._loops[-1].breaks.append(state)

    def _continue(self, node: Node, state: State) -> None:
        if self._loops:
            self._loops[-1].continues.append(state)

    def _decorated(self, node: Node, state: State) -> State:
        for child in node.named_children:
            if child.type == cs.TS_PY_DECORATOR:
                state = self._expression(child, state)
            else:
                state = self._definition(child, state)
        return state

    def _definition(self, node: Node, state: State) -> State:
        # The body is another scope; only the name binds here.
        name = _text(node.child_by_field_name(cs.FIELD_NAME))
        binding = Binding(BindingKind.DEFINITION, node.start_byte, definition=node)
        return self._set(state, name, binding)

    def _expression_statement(self, node: Node, state: State) -> State:
        if node.type == cs.TS_PY_ASSIGNMENT:
            return self._assign(node, state)
        if node.type == cs.TS_PY_AUGMENTED_ASSIGNMENT:
            state = self._expression(node.child_by_field_name(cs.TS_FIELD_RIGHT), state)
            return self._bind_opaque(node.child_by_field_name(cs.TS_FIELD_LEFT), state)
        return self._expression(node, state)

    def _assign(self, node: Node, state: State) -> State:
        # `a = b = value` nests as a = (b = value): every target receives the
        # innermost right-hand side, evaluated once before any is bound.
        targets: list[Node] = []
        value: Node | None = node
        while value is not None and value.type == cs.TS_PY_ASSIGNMENT:
            if (left := value.child_by_field_name(cs.TS_FIELD_LEFT)) is not None:
                targets.append(left)
            value = value.child_by_field_name(cs.TS_FIELD_RIGHT)
        state = self._expression(value, state)
        for target in targets:
            state = self._bind(target, value, state)
        return state

    def _bind(self, target: Node, value: Node | None, state: State) -> State:
        if target.type == cs.TS_PY_IDENTIFIER:
            binding = Binding(BindingKind.ASSIGN, target.start_byte, value=value)
            return self._set(state, _text(target), binding)
        if target.type in _PATTERNS:
            elements = list(target.named_children)
            values = (
                list(value.named_children)
                if value is not None and value.type in _SEQUENCES
                else []
            )
            for element, element_value in zip(
                elements, _pair(elements, values), strict=True
            ):
                state = self._bind(element, element_value, state)
            return state
        if target.type in _SPLATS:
            return self._bind_opaque(target, state)
        # `obj.attr = v`, `xs[i] = v`: no local is bound, but the target's own
        # sub-expressions are evaluated and may call.
        return self._expression(target, state)

    def _bind_opaque(self, target: Node | None, state: State) -> State:
        if target is None:
            return state
        for name in _identifiers(target):
            state = self._set(
                state, name, Binding(BindingKind.OPAQUE, target.start_byte)
            )
        return state

    def _opaque(self, node: Node, state: State) -> State:
        names = {
            n
            for c in node.named_children
            for n in [*_identifiers(c), *_import_aliases(c)]
        }
        for name in names:
            state = self._set(state, name, Binding(BindingKind.OPAQUE, node.start_byte))
        return state

    def _if(self, node: Node, state: State) -> State | None:
        state = self._expression(node.child_by_field_name(cs.TS_FIELD_CONDITION), state)
        outs: list[State | None] = [
            self._body(node.child_by_field_name(cs.FIELD_CONSEQUENCE), dict(state))
        ]
        exhaustive = False
        for alt in node.children_by_field_name(cs.FIELD_ALTERNATIVE):
            if alt.type == cs.TS_PY_ELIF_CLAUSE:
                state = self._expression(
                    alt.child_by_field_name(cs.TS_FIELD_CONDITION), state
                )
                outs.append(
                    self._body(
                        alt.child_by_field_name(cs.FIELD_CONSEQUENCE), dict(state)
                    )
                )
            else:
                outs.append(
                    self._body(alt.child_by_field_name(cs.FIELD_BODY), dict(state))
                )
                exhaustive = True
        if not exhaustive:
            outs.append(state)
        return _merge(outs)

    def _loop(self, node: Node, state: State) -> State | None:
        is_for = node.type == cs.TS_PY_FOR_STATEMENT
        if is_for:
            state = self._expression(node.child_by_field_name(cs.TS_FIELD_RIGHT), state)
        body = node.child_by_field_name(cs.FIELD_BODY)
        head: State = state
        loop = _Loop()
        for _ in range(_MAX_LOOP_ROUNDS):
            loop = _Loop()
            self._loops.append(loop)
            entry = head
            if not is_for:
                entry = self._expression(
                    node.child_by_field_name(cs.TS_FIELD_CONDITION), entry
                )
            else:
                entry = self._bind_opaque(
                    node.child_by_field_name(cs.TS_FIELD_LEFT), entry
                )
            end = self._body(body, dict(entry))
            self._loops.pop()
            nxt = _merge([head, end, *loop.continues]) or head
            if nxt == head:
                break
            head = nxt
        else:
            self._unsettled.update(_calls_in(body))
        # The loop may run zero times; it leaves through its condition or a
        # break, and an else clause runs only when no break was taken.
        exit_state: State | None = head
        if not is_for:
            exit_state = self._expression(
                node.child_by_field_name(cs.TS_FIELD_CONDITION), head
            )
        for alt in node.children_by_field_name(cs.FIELD_ALTERNATIVE):
            exit_state = self._body(alt.child_by_field_name(cs.FIELD_BODY), exit_state)
        return _merge([exit_state, *loop.breaks])

    def _try(self, node: Node, state: State) -> State | None:
        loop = self._loops[-1] if self._loops else None
        # The loop exits this try's body and handlers take, counted from
        # before the body runs; `finally` runs on each of them before it
        # reaches the loop's head or its exit.
        exits = (len(loop.breaks), len(loop.continues)) if loop is not None else (0, 0)
        normal, handler_in = self._try_body(
            node.child_by_field_name(cs.FIELD_BODY), state
        )
        outs: list[State | None] = []
        finally_clause: Node | None = None
        for child in node.named_children:
            if child.type == cs.TS_PY_EXCEPT_CLAUSE:
                outs.append(self._handler(child, handler_in))
            elif child.type == cs.TS_PY_ELSE_CLAUSE:
                normal = self._body(child.child_by_field_name(cs.FIELD_BODY), normal)
            elif child.type == cs.TS_PY_FINALLY_CLAUSE:
                finally_clause = child
        merged = _merge([normal, *outs])
        if finally_clause is None:
            return merged
        return self._finally(finally_clause, merged, handler_in, loop, exits)

    def _try_body(self, body: Node | None, state: State) -> tuple[State | None, State]:
        """The state the body leaves, and the one a handler starts from: a
        handler can start after any statement of the body, so it sees every
        state the body passes through, and the entry state."""
        seen: list[State | None] = [state]
        current: State | None = dict(state)
        for stmt in body.named_children if body is not None else []:
            if current is None:
                break
            current = self._statement(stmt, current)
            seen.append(current)
        return current, _merge(seen) or state

    def _handler(self, clause: Node, handler_in: State) -> State | None:
        start = dict(handler_in)
        for part in clause.named_children:
            if part.type == cs.TS_PY_BLOCK:
                return self._block(part.named_children, start)
            start = self._expression(part, start)
            start = self._bind_opaque_as(part, start)
        return None

    def _finally(
        self,
        clause: Node,
        merged: State | None,
        handler_in: State,
        loop: _Loop | None,
        exits: tuple[int, int],
    ) -> State | None:
        block = next(
            (c for c in clause.named_children if c.type == cs.TS_PY_BLOCK), None
        )
        ran = self._body(block, _merge([merged, handler_in]) or handler_in)
        if loop is not None:
            breaks, continues = exits
            loop.breaks[breaks:] = self._through(block, loop.breaks[breaks:])
            loop.continues[continues:] = self._through(
                block, loop.continues[continues:]
            )
        return ran if merged is not None else None

    def _through(self, block: Node | None, states: list[State]) -> list[State]:
        """Each loop exit as `finally` leaves it (it may rebind, or end it)."""
        out: list[State] = []
        for s in states:
            if (ran := self._body(block, dict(s))) is not None:
                out.append(ran)
        return out

    def _bind_opaque_as(self, node: Node, state: State) -> State:
        if node.type == cs.TS_PY_AS_PATTERN:
            for part in node.named_children:
                if part.type == cs.TS_PY_AS_PATTERN_TARGET:
                    for target in part.named_children:
                        state = self._bind_opaque(target, state)
        return state

    def _with(self, node: Node, state: State) -> State | None:
        body: Node | None = None
        for child in node.named_children:
            if child.type == cs.TS_PY_BLOCK:
                body = child
                continue
            for item in _with_items(child):
                value = item.child_by_field_name(cs.FIELD_VALUE)
                state = self._expression(value, state)
                if value is not None:
                    state = self._bind_opaque_as(value, state)
        return self._body(body, state)

    def _match(self, node: Node, state: State) -> State | None:
        state = self._expression(node.child_by_field_name(cs.FIELD_SUBJECT), state)
        body = node.child_by_field_name(cs.FIELD_BODY)
        outs: list[State | None] = []
        # The state when no case ran: each case tried first may have bound
        # its captures, and its guard (a walrus) evaluated, before failing.
        missed = state
        for case in body.named_children if body is not None else []:
            if case.type != cs.TS_PY_CASE_CLAUSE:
                continue
            start = self._case_start(case, missed)
            outs.append(
                self._body(case.child_by_field_name(cs.FIELD_CONSEQUENCE), start)
            )
            if case_always_matches(case):
                # No later case runs, and no path leaves with none run.
                return _merge(outs)
            missed = _merge([missed, start]) or missed
        return _merge([*outs, missed])

    def _case_start(self, case: Node, missed: State) -> State:
        """The state a case's body starts from: its captures bound, then its
        guard evaluated."""
        start = dict(missed)
        for part in case.named_children:
            if part.type == cs.TS_PY_CASE_PATTERN:
                start = self._bind_opaque(part, start)
                for name in _pattern_captures(part):
                    start = self._set(
                        start, name, Binding(BindingKind.OPAQUE, part.start_byte)
                    )
        return self._expression(case.child_by_field_name(cs.TS_PY_FIELD_GUARD), start)

    # Expressions ----------------------------------------------------------

    def _expressions(self, nodes: Iterable[Node], state: State) -> State:
        for node in nodes:
            state = self._expression(node, state)
        return state

    def _expression(self, node: Node | None, state: State) -> State:
        """Evaluate in source order: record each call against the state at
        that point, and apply a walrus binding where it happens.

        Iterative on purpose: a fluent chain nests one call per hop, and a
        generated one runs thousands deep, past the interpreter's recursion
        limit; a recursive walk would lose the whole caller's calls there.
        """
        work: list[_Step] = [_Step(_EVAL, node)]
        while work:
            step = work.pop()
            if step.op != _EVAL:
                state = self._steps[step.op](step, state)
            elif step.node is not None and step.node.type not in _SCOPES:
                state = self._evaluate(step.node, state, work)
        return state

    def _evaluate(self, node: Node, state: State, work: list[_Step]) -> State:
        if (evaluator := self._evaluators.get(node.type)) is not None:
            return evaluator(node, state, work)
        return self._parts(node, state, work)

    @staticmethod
    def _parts(node: Node, state: State, work: list[_Step]) -> State:
        work.extend(_Step(_EVAL, c) for c in reversed(node.named_children))
        return state

    def _read(self, node: Node, state: State, work: list[_Step]) -> State:
        if (name := _text(node)) in state:
            _add(self._names, node.start_byte, state[name])
        return state

    def _walrus(self, node: Node, state: State, work: list[_Step]) -> State:
        work.extend(
            [
                _Step(_BIND, node),
                _Step(_EVAL, node.child_by_field_name(cs.FIELD_VALUE)),
            ]
        )
        return state

    def _comprehension(self, node: Node, state: State, work: list[_Step]) -> State:
        # The comprehension's own targets are its locals; a call through one
        # of them is not decided here, and they do not outlive it. A walrus
        # inside binds the enclosing body's name (PEP 572), on a path that
        # may never run.
        own = frozenset(
            n
            for c in node.named_children
            if c.type == cs.TS_PY_FOR_IN_CLAUSE
            for n in _identifiers(c.child_by_field_name(cs.TS_FIELD_LEFT))
        )
        work.append(_Step(_RESTORE, None, state, own))
        self._parts(node, state, work)
        return {k: v for k, v in state.items() if k not in own}

    @staticmethod
    def _short_circuit(node: Node, state: State, work: list[_Step]) -> State:
        work.extend(
            [
                _Step(_MAYBE, node.child_by_field_name(cs.TS_FIELD_RIGHT)),
                _Step(_EVAL, node.child_by_field_name(cs.TS_FIELD_LEFT)),
            ]
        )
        return state

    def _conditional(self, node: Node, state: State, work: list[_Step]) -> State:
        if node.named_child_count != 3:
            return self._parts(node, state, work)
        work.extend([_Step(_EITHER, node), _Step(_EVAL, node.named_children[1])])
        return state

    @staticmethod
    def _call(node: Node, state: State, work: list[_Step]) -> State:
        # The callee expression first, then the call itself, then its
        # arguments: the order the call evaluates in.
        work.extend(
            [
                _Step(_EVAL, node.child_by_field_name(cs.FIELD_ARGUMENTS)),
                _Step(_RECORD, node),
                _Step(_EVAL, node.child_by_field_name(cs.FIELD_FUNCTION)),
            ]
        )
        return state

    @staticmethod
    def _restore(step: _Step, state: State) -> State:
        if step.saved is None:
            return state
        return _leave_comprehension(step.saved, state, step.own)

    def _maybe(self, step: _Step, state: State) -> State:
        # `x and y` / `x or y`: y may not run, so its bindings (a walrus)
        # join the state x left.
        if step.node is None:
            return state
        return _merge([state, self._expression(step.node, dict(state))]) or state

    def _either(self, step: _Step, state: State) -> State:
        # `a if c else b`, after c: exactly one of a and b runs.
        if step.node is None:
            return state
        yes, _condition, no = step.node.named_children
        branches = [
            self._expression(yes, dict(state)),
            self._expression(no, dict(state)),
        ]
        return _merge(branches) or state

    def _record(self, step: _Step, state: State) -> State:
        if step.node is not None:
            base = call_base(step.node)
            if base is not None and base in state:
                _add(self._reaching, step.node.start_byte, state[base])
        return state

    def _bind_walrus(self, step: _Step, state: State) -> State:
        if step.node is None:
            return state
        name_node = step.node.child_by_field_name(cs.FIELD_NAME)
        if name_node is None:
            return state
        return self._bind(
            name_node, step.node.child_by_field_name(cs.FIELD_VALUE), state
        )


def _add(
    table: dict[int, frozenset[Binding]], key: int, found: frozenset[Binding]
) -> None:
    # A node is walked once per path into it (a loop round, a `finally` on
    # each way out of its `try`): what reaches it is what reaches it on any.
    table[key] = table.get(key, frozenset()) | found


def _leave_comprehension(before: State, after: State, own: frozenset[str]) -> State:
    """The state past a comprehension: its targets are what they were before
    it, and what it bound in the enclosing body (a walrus) joins the state
    before it, since it may iterate zero times."""
    outer = {k: v for k, v in after.items() if k not in own}
    joined = _merge([{k: v for k, v in before.items() if k not in own}, outer]) or {}
    return {**joined, **{k: before[k] for k in own if k in before}}


def _pair(targets: list[Node], values: list[Node]) -> list[Node | None]:
    """The value each unpacking target receives, None where it cannot tell.

    `a, b = f, g` pairs one to one. With one starred target, `a, *rest, c =
    f, g, h, i` still pairs the targets before the star with the leading
    values and those after it with the trailing ones; the star collects a
    list, which no value here is.
    """
    stars = [i for i, t in enumerate(targets) if t.type in _SPLATS]
    if any(v.type in _SPLATS for v in values):
        return [None] * len(targets)
    if not stars:
        return list(values) if len(values) == len(targets) else [None] * len(targets)
    if len(stars) > 1 or len(values) < len(targets) - 1:
        return [None] * len(targets)
    star = stars[0]
    tail = len(targets) - star - 1
    trailing = values[len(values) - tail :] if tail else []
    return [*values[:star], None, *trailing]


def _with_items(node: Node) -> list[Node]:
    if node.type == cs.TS_PY_WITH_ITEM:
        return [node]
    return [i for c in node.named_children for i in _with_items(c)]


def _import_aliases(node: Node) -> list[str]:
    if node.type == cs.TS_PY_ALIASED_IMPORT:
        alias = _text(node.child_by_field_name(cs.FIELD_ALIAS))
        return [alias] if alias else []
    if node.type == cs.TS_PY_DOTTED_NAME:
        first = node.named_children[0] if node.named_children else None
        name = _text(first)
        return [name] if name else []
    return []


def _pattern_captures(node: Node) -> list[str]:
    names: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if (
            current.type == cs.TS_PY_IDENTIFIER
            and current.parent is not None
            and (current.parent.type in (cs.TS_PY_CASE_PATTERN, cs.TS_PY_AS_PATTERN))
            and (name := _text(current)) is not None
        ):
            names.append(name)
        stack.extend(current.named_children)
    return names


def _calls_in(node: Node | None) -> list[int]:
    if node is None:
        return []
    found: list[int] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _SCOPES:
            continue
        if current.type == cs.TS_PY_CALL:
            found.append(current.start_byte)
        stack.extend(current.named_children)
    return found
