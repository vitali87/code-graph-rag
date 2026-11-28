"""Which functions of a file take part in callable-argument flow.

A function that invokes a parameter (`def run_callback(fn): return fn()`)
gets an edge to every callable its callers pass in, but the edge is derived
from bindings recorded while walking the CALLERS' files. An incremental run
that re-parses such a function's file must walk those callers too, and,
through a parameter a caller only forwards (`def wrap(cb): return
run_callback(cb)`), their callers in turn (issue #2911). A function that
returns one (`def factory(): return make()`, make returning a closure that
invokes its parameter) brings in its callers the same way: finalize follows
`factory()(handler)` through factory's walk to the closure (issue #3781).

Forwarding is read by position, as the call pass binds it: `wrap(ctx, cb)`
handing `ctx` on to `run(ctx, fn)` forwards nothing unless `run` invokes
its first parameter.

The other direction holds too (issue #3781). A call site that gains, changes
or loses a callback moves an edge that starts in the receiver's file, which
holds no edge into the call site and so is no dependent of it; and a closure
call (`make()(cb)`) starts in the caller's file but is followed through the
factory's walk. So a re-parsed file also brings in the files its closure
calls go through, and an edited file whose calls may have changed brings in
the receivers it hands a function to (`FlowCallees`).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from enum import Enum, auto
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..language_spec import LANGUAGE_SPECS
from ..types_defs import ResultRow
from .handlers import get_handler
from .utils import (
    callable_parameter_indices,
    cpp_parameter_names,
    go_parameter_names,
    js_ts_parameter_names,
    python_parameter_names,
    safe_decode_text,
)

# The call pass records a pass-through argument (`run_callback(cb)` with `cb`
# the caller's own parameter) in every flow language, so each is scanned for
# one with its own parameter reader.
_PARAMETER_NAMES: dict[cs.SupportedLanguage, Callable[[Node], list[str]]] = {
    cs.SupportedLanguage.PYTHON: python_parameter_names,
    cs.SupportedLanguage.GO: go_parameter_names,
    cs.SupportedLanguage.CPP: cpp_parameter_names,
    **{language: js_ts_parameter_names for language in cs.JS_TS_LANGUAGES},
}
# Where the call pass reads what a closure call goes through: Python's
# `factory()(cb)`, and a `f = factory(); f(cb)` alias in each language
# whose returns it reads (a Go return is an expression list it does not).
_CHAINED_LANGUAGES = frozenset({cs.SupportedLanguage.PYTHON})
_ALIAS_LANGUAGES = frozenset(
    {cs.SupportedLanguage.PYTHON, cs.SupportedLanguage.CPP, *cs.JS_TS_LANGUAGES}
)
# The languages whose returns the call pass reads, and those whose imports
# can alias a name.
_RETURN_LANGUAGES = _ALIAS_LANGUAGES
_ALIASING_LANGUAGES = frozenset({cs.SupportedLanguage.PYTHON, *cs.JS_TS_LANGUAGES})
_CALLS = frozenset({cs.TS_PY_CALL, cs.TS_CALL_EXPRESSION})
# The field naming the member a callee reaches through its receiver:
# `registry.run` (Python), `api.run` (JS/TS), `pkg.Run` (Go), `obj.run` and
# `ns::run` (C++).
_MEMBERS = {
    cs.TS_PY_ATTRIBUTE: cs.TS_PY_FIELD_ATTRIBUTE,
    cs.TS_MEMBER_EXPRESSION: cs.FIELD_PROPERTY,
    cs.TS_SELECTOR_EXPRESSION: cs.FIELD_FIELD,
    cs.TS_CPP_FIELD_EXPRESSION: cs.FIELD_FIELD,
    cs.TS_CPP_QUALIFIED_IDENTIFIER: cs.FIELD_NAME,
}
# What the call pass reads a return as (`_returned_names`,
# `_returned_call_names`): a name or member handed back, or, in Python, the
# function a returned call calls.
_RETURNED = frozenset({cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE})
# A function written in place as an argument.
_FUNCTION_LITERALS = frozenset(
    {
        cs.TS_PY_LAMBDA,
        cs.TS_ARROW_FUNCTION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_GO_FUNC_LITERAL,
        cs.TS_CPP_LAMBDA_EXPRESSION,
    }
)
# `x = factory(...)` in each flow language, as `_factory_bindings` reads it:
# the binding node, the field of the name and the field of the call.
_BINDINGS = {
    cs.TS_PY_ASSIGNMENT: (cs.TS_FIELD_LEFT, cs.TS_FIELD_RIGHT),
    cs.TS_VARIABLE_DECLARATOR: (cs.FIELD_NAME, cs.FIELD_VALUE),
    cs.TS_CPP_INIT_DECLARATOR: (cs.TS_FIELD_DECLARATOR, cs.FIELD_VALUE),
}
# A parameter slot of a callee: its position, its name for a keyword
# argument, or any slot at all.
Slot = int | str | None


class _Forward(NamedTuple):
    # A call handing on the function's own parameter at `parameter`, at the
    # callee's `position` or as its `keyword`.
    callee: str
    position: int | None
    keyword: str | None
    parameter: int


class _Function(NamedTuple):
    name: str
    parameters: tuple[str, ...]
    # The positions of the parameters it invokes.
    invoked: frozenset[int]
    forwards: tuple[_Forward, ...]
    # The simple names it returns, and those whose call it returns.
    returned: frozenset[str]
    called: frozenset[str]


class _Handed(NamedTuple):
    # One argument a call hands on: at `position` or as `keyword`, a name
    # (`handler`, `obj.method`) or, with no name, a function written in place.
    callee: str
    position: int | None
    keyword: str | None
    name: str | None


class FileCalls(NamedTuple):
    # What a file's calls name: the factories whose result they call
    # (`factory()(cb)`, `f = factory(); f(cb)`), each argument they hand a
    # callee, and the locals the file assigns (`b = handler; run(b)`), which
    # the call pass follows through the assignment while the interface
    # fingerprint keeps only the argument's text.
    factories: frozenset[str]
    handed: tuple[_Handed, ...]
    assigned: frozenset[str]


class FlowNames(NamedTuple):
    """The functions taking part in callable flow, by simple name: the
    parameter slots a callback handed to each reaches an invocation through,
    and those returning one."""

    positions: Mapping[str, Collection[int]]
    keywords: Mapping[str, Collection[str]]
    producers: frozenset[str]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.positions) | self.producers

    def takes(self, name: str, slot: Slot) -> bool:
        """Whether a callback handed to `name` at `slot` reaches an
        invocation."""
        if slot is None:
            return name in self.positions
        if isinstance(slot, int):
            return slot in self.positions.get(name, ())
        return slot in self.keywords.get(name, ())


NO_FLOW = FlowNames({}, {}, frozenset())


class FileFlowFunctions:
    """The functions of one file that invoke a parameter, and those that may
    hand one on: forward a parameter into such a function, or return one.
    Also what the file's calls hand on, and the factories whose result it
    calls."""

    __slots__ = ("functions", "_root", "_language", "_aliases", "_calls")

    def __init__(
        self,
        function_nodes: Iterable[Node],
        language: cs.SupportedLanguage,
        root: Node,
    ) -> None:
        functions: list[_Function] = []
        aliases = _import_aliases(root) if language in _ALIASING_LANGUAGES else {}
        reads_returns = language in _RETURN_LANGUAGES
        parameter_names = _PARAMETER_NAMES.get(language)
        spec = LANGUAGE_SPECS.get(language)
        # A nested scope's returns are its own, as the call pass reads them
        # (`_flow_scope_boundaries`).
        scopes = frozenset(
            {
                cs.TS_PY_DECORATED_DEFINITION,
                *(spec.function_node_types if spec else ()),
                *(spec.class_node_types if spec else ()),
            }
        )
        # A C++ definition names itself in its declarator and a JS/TS arrow
        # function through the variable it is bound to.
        handler = get_handler(language)
        for func_node in function_nodes:
            name = handler.extract_function_name(func_node)
            if not name:
                continue
            parameters = tuple(parameter_names(func_node)) if parameter_names else ()
            returned, called = (
                _returned_names(func_node, scopes, aliases)
                if reads_returns
                else (frozenset(), frozenset())
            )
            functions.append(
                _Function(
                    name,
                    parameters,
                    frozenset(callable_parameter_indices(func_node, language).values()),
                    _forwards(func_node, parameters, aliases),
                    returned,
                    called,
                )
            )
        self.functions = tuple(functions)
        self._root = root
        self._language = language
        self._aliases = aliases
        self._calls: FileCalls | None = None

    @property
    def calls(self) -> FileCalls:
        """What the file's calls name, read on first use: most re-parsed
        files are neither edited nor in a language with closure calls."""
        if self._calls is None:
            self._calls = _file_calls(self._root, self._aliases, self._language)
        return self._calls

    @property
    def has_closure_calls(self) -> bool:
        """Whether the call pass reads a closure call in this language."""
        return self._language in _ALIAS_LANGUAGES

    @property
    def invokers(self) -> frozenset[str]:
        return frozenset(fn.name for fn in self.functions if fn.invoked)

    def forwarders(self, flow: FlowNames) -> set[str]:
        """Simple names of the functions that forward a parameter into a
        slot of a function `flow` holds."""
        return {fn.name for fn in self.functions if _reached(fn, flow)}

    def producers(self, flow: FlowNames) -> set[str]:
        """Simple names of the functions that return a function `flow`
        holds, or what a call to one returns.

        `factory()(handler)` binds `cb` of the closure `invoke` that make
        returns when factory is `return make()`: finalize reaches the closure
        through facade.py's record of factory's return, so a caller of
        factory and the file defining it walk again with make's
        (issue #3781).
        """
        names = flow.names
        return {
            fn.name
            for fn in self.functions
            if not (fn.returned.isdisjoint(names) and fn.called.isdisjoint(names))
        }

    def relays(self, flow: FlowNames) -> set[str]:
        """The functions that hand a flow function on, as forwarders or
        producers."""
        return self.forwarders(flow) | self.producers(flow)

    def names(self, flow: FlowNames) -> frozenset[str]:
        return self.invokers | self.relays(flow)

    def takes(self, name: str, slot: Slot, flow: FlowNames) -> bool:
        """Whether a function `name` here gets a callback handed at `slot`
        to an invocation: its own, or one it forwards the slot to."""
        for fn in self.functions:
            if fn.name != name:
                continue
            reached = fn.invoked | _reached(fn, flow)
            if slot is None and (reached or fn.name in self.producers(flow)):
                return True
            if isinstance(slot, int) and slot in reached:
                return True
            if isinstance(slot, str) and slot in {fn.parameters[i] for i in reached}:
                return True
        return False

    def returns(self, name: str) -> bool:
        """Whether a function `name` here returns a name or a call's result,
        which a closure call through it (`name()(cb)`) calls."""
        return any(
            fn.name == name and (fn.returned or fn.called) for fn in self.functions
        )

    def named(self, name: str) -> list[_Function]:
        return [fn for fn in self.functions if fn.name == name]


def _reached(fn: _Function, flow: FlowNames) -> set[int]:
    # The parameters of `fn` it forwards into a slot `flow` holds.
    return {
        forward.parameter
        for forward in fn.forwards
        if flow.takes(
            forward.callee,
            forward.position if forward.position is not None else forward.keyword,
        )
    }


def settled_flow_names(files: Iterable[FileFlowFunctions]) -> FlowNames:
    """The flow functions of `files`, grown until none adds a slot: a
    wrapper read before the function it forwards into is read again once
    that function's slot is known."""
    functions = [fn for file in files for fn in file.functions]
    positions: dict[str, set[int]] = {}
    keywords: dict[str, set[str]] = {}
    producers: set[str] = set()

    def add(fn: _Function, slots: Iterable[int]) -> bool:
        held = positions.setdefault(fn.name, set())
        fresh = set(slots) - held
        held |= fresh
        keywords.setdefault(fn.name, set()).update(
            fn.parameters[i] for i in fresh if i < len(fn.parameters)
        )
        return bool(fresh)

    for fn in functions:
        if fn.invoked:
            add(fn, fn.invoked)
    flow = FlowNames(positions, keywords, frozenset())
    grown = True
    while grown:
        grown = False
        for fn in functions:
            if (reached := _reached(fn, flow)) and add(fn, reached):
                grown = True
            names = {*positions, *producers}
            if fn.name not in producers and not (
                fn.returned.isdisjoint(names) and fn.called.isdisjoint(names)
            ):
                producers.add(fn.name)
                grown = True
        flow = FlowNames(positions, keywords, frozenset(producers))
    return FlowNames(
        {name: frozenset(slots) for name, slots in positions.items()},
        {name: frozenset(slots) for name, slots in keywords.items()},
        frozenset(producers),
    )


def flow_caller_paths(
    rows: Iterable[ResultRow], holders: Mapping[str, Collection[str]]
) -> list[str]:
    """The callers among `rows` that hold an edge into one of the flow
    functions `holders` names for each holder file (issue #3781).

    Every flow edge out of a holder starts at a function that invokes a
    parameter, and is written from the bindings of the calls resolved to it,
    or to a function forwarding into it or returning it, which the loop
    reaches by name in turn. A caller of something else in the file records
    none. A closure is named by its position (`anonymous_3_4`, `lambda_3_4`),
    not by a name the holder's scan reads, and an alias call of it
    (`make()(cb)`) seeds it, so an edge into such a name keeps its caller
    too.
    """
    simple = {
        path: {_simple_name(name) for name in names} for path, names in holders.items()
    }
    return sorted(
        {
            caller
            for row in rows
            if isinstance(caller := row.get(cs.KEY_CALLER_PATH), str)
            and caller
            and isinstance(target_path := row.get(cs.KEY_TARGET_PATH), str)
            and isinstance(target_name := row.get(cs.KEY_TARGET_NAME), str)
            and (
                _simple_name(target_name) in simple.get(target_path, ())
                or any(character.isdigit() for character in target_name)
            )
        }
    )


class _Need(Enum):
    # Why a file defining a name must walk with the re-parse.
    # A call there hands the name a callback that may be new: the receiver's
    # edge starts in its file.
    RECEIVER = auto()
    # A closure call goes through the name (`name()(cb)`): finalize reads
    # what it returns from its file's walk.
    FACTORY = auto()
    # Both: a closure call that may be new reaches the closure's own edges.
    NEW_FACTORY = auto()


class FlowCallees:
    """The files a re-parse must bring in for the flow edges its call sites
    start or move in other files (issue #3781).

    Fed the re-parsed files' scans (`reparsed`, `lost`), it names what to
    look up (`ask`); fed each name's definitions (`answer`), it notes them
    and asks in turn for what each hands on (a forwarder's receiver, a
    factory's own factory). Once nothing is left to ask, `joined` judges
    every definition against the flow names settled over all of them: a
    receiver joins when the slot it is handed reaches an invocation, a
    factory when it returns something a closure call can call.
    """

    __slots__ = ("_asked", "_pending", "_seen", "_found", "_joined")

    def __init__(self) -> None:
        self._asked: set[tuple[str, _Need, Slot]] = set()
        self._pending: set[tuple[str, _Need, Slot]] = set()
        self._seen: set[str] = set()
        self._found: set[tuple[str, str, _Need, Slot]] = set()
        self._joined: set[str] = set()

    def reparsed(
        self,
        key: str,
        flow: FileFlowFunctions,
        edited: bool,
        changed: bool,
        functions: Collection[str],
    ) -> None:
        """Note a file the re-parse walks. Its closure calls always need
        their factories. When it is `edited`, each callee it hands a local
        it assigns needs walking, as the fingerprint keeps only the local's
        name; when its calls may have `changed` (an edit the interface
        fingerprint sees or has no rules for, a new file), each callee it
        hands a function to does: one written in place, or a name among
        `functions` (the names the project defines)."""
        if key in self._seen:
            return
        self._seen.add(key)
        if flow.has_closure_calls:
            need = _Need.NEW_FACTORY if changed else _Need.FACTORY
            self._want((name, need, None) for name in flow.calls.factories)
        if not edited:
            return
        calls = flow.calls
        for handed in calls.handed:
            if (
                handed.name in calls.assigned
                or changed
                and (handed.name is None or handed.name in functions)
            ):
                slot = (
                    handed.position if handed.position is not None else handed.keyword
                )
                self._want([(handed.callee, _Need.RECEIVER, slot)])

    def noted(self, key: str) -> bool:
        return key in self._seen

    def lost(self, names: Iterable[str]) -> None:
        """The names a deleted or changed file's old edges reached: a
        callback it handed one may be gone."""
        self._want((name, _Need.RECEIVER, None) for name in names)

    def ask(self) -> list[str]:
        """The names whose definitions are wanted next, each asked once."""
        return sorted({name for name, _need, _slot in self._pending})

    def answer(
        self,
        definitions: Iterable[tuple[str, str]],
        scanned: Mapping[str, FileFlowFunctions | None],
    ) -> None:
        """Note each (path, name) definition of an asked name, and ask for
        what it hands on."""
        pending, self._pending = self._pending, set()
        self._asked |= pending
        wants: dict[str, set[tuple[_Need, Slot]]] = {}
        for name, need, slot in pending:
            wants.setdefault(name, set()).add((need, slot))
        for path, name in definitions:
            if (flow := scanned.get(path)) is None:
                continue
            for need, slot in wants.get(name, ()):
                self._found.add((path, name, need, slot))
                for fn in flow.named(name):
                    self._want(_handed_on(fn, need, slot))

    def joined(
        self,
        scanned: Mapping[str, FileFlowFunctions | None],
        flow_names: FlowNames,
    ) -> set[str]:
        """The files of the definitions found that a walk is owed, not
        named before."""
        joined = {
            path
            for path, name, need, slot in self._found
            if (flow := scanned.get(path)) is not None
            and (
                flow.takes(name, slot, flow_names)
                if need == _Need.RECEIVER
                else flow.returns(name)
            )
        }
        fresh, self._joined = joined - self._joined, self._joined | joined
        return fresh

    def _want(self, wants: Iterable[tuple[str, _Need, Slot]]) -> None:
        for want in wants:
            if want not in self._asked:
                self._pending.add(want)


def _handed_on(fn: _Function, need: _Need, slot: Slot) -> list[tuple[str, _Need, Slot]]:
    """What `fn`, wanted for `need` at `slot`, needs looked up in turn: a
    receiver the callees it forwards that slot to, a factory the factories
    whose result it returns, and a new closure call the closures too."""
    if need == _Need.RECEIVER:
        return [
            (
                forward.callee,
                _Need.RECEIVER,
                forward.position if forward.position is not None else forward.keyword,
            )
            for forward in fn.forwards
            if slot is None
            or forward.parameter == slot
            or (
                isinstance(slot, str)
                and forward.parameter < len(fn.parameters)
                and fn.parameters[forward.parameter] == slot
            )
        ]
    wants = [(name, need, None) for name in fn.called]
    if need == _Need.NEW_FACTORY:
        wants += [(name, _Need.RECEIVER, None) for name in fn.returned]
    return wants


def handed_names(files: Iterable[FileFlowFunctions]) -> set[str]:
    """The names `files`' calls hand on, to tell the functions among them."""
    return {
        handed.name
        for file in files
        for handed in file.calls.handed
        if handed.name is not None
    }


def defined_names(files: Iterable[FileFlowFunctions]) -> set[str]:
    return {fn.name for file in files for fn in file.functions}


def _simple_name(name: str) -> str:
    # A C++ definition can name itself with its scope (`Registry::run`).
    return name.rpartition(cs.SEPARATOR_DOUBLE_COLON)[2].rpartition(cs.SEPARATOR_DOT)[2]


def _import_aliases(root: Node) -> dict[str, str]:
    # `from registry import run_callback as invoke`, `import { runCallback as
    # invoke } from './registry'`: a call of `invoke` resolves to the name the
    # flow knows the function by.
    aliases: dict[str, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in (cs.TS_PY_ALIASED_IMPORT, cs.TS_IMPORT_SPECIFIER):
            name = node.child_by_field_name(cs.FIELD_NAME)
            alias = node.child_by_field_name(cs.FIELD_ALIAS)
            if name is not None and alias is not None:
                original = safe_decode_text(name)
                if (local := safe_decode_text(alias)) and original:
                    aliases[local] = original.rpartition(cs.SEPARATOR_DOT)[2]
            continue
        stack.extend(node.children)
    return aliases


def _written_name(node: Node, aliases: Mapping[str, str]) -> str | None:
    """The simple name a callee or returned value is written with: a member
    by its own name (`registry.make` is `make`), a bare name by the name its
    module defines when an import aliased it."""
    if (field := _MEMBERS.get(node.type)) is not None:
        member = node.child_by_field_name(field)
        return _written_name(member, {}) if member is not None else None
    text = safe_decode_text(node)
    return aliases.get(text, text) if text else None


def _arguments(call: Node) -> list[tuple[int | None, str | None, Node]]:
    """A call's arguments as the call pass splits them
    (`_split_call_arguments`): each positional one at its index among the
    argument list's named children that are not keywords, each keyword's
    value under its name; casts and `.bind()` peeled off
    (`_peel_bound_callable`)."""
    arguments = call.child_by_field_name(cs.FIELD_ARGUMENTS)
    found: list[tuple[int | None, str | None, Node]] = []
    position = 0
    for argument in arguments.named_children if arguments is not None else ():
        if argument.type == cs.TS_PY_KEYWORD_ARGUMENT:
            name = argument.child_by_field_name(cs.FIELD_NAME)
            value = argument.child_by_field_name(cs.FIELD_VALUE)
            if (
                value is not None
                and name is not None
                and (keyword := safe_decode_text(name))
            ):
                found.append((None, keyword, _peel(value)))
            continue
        found.append((position, None, _peel(argument)))
        position += 1
    return found


def _forwards(
    func_node: Node, parameters: tuple[str, ...], aliases: Mapping[str, str]
) -> tuple[_Forward, ...]:
    """The calls `func_node` hands one of its `parameters` to, as an
    argument by itself (`run_callback(cb)`, `run_callback(fn=cb)`)."""
    body = func_node.child_by_field_name(cs.FIELD_BODY)
    if not parameters or body is None:
        return ()
    index = {name: i for i, name in enumerate(parameters)}
    forwards: list[_Forward] = []
    stack = [body]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type not in _CALLS:
            continue
        callee = node.child_by_field_name(cs.FIELD_FUNCTION)
        if callee is None or not (name := _written_name(callee, aliases)):
            continue
        for position, keyword, value in _arguments(node):
            if (
                value.type == cs.TS_PY_IDENTIFIER
                and (parameter := index.get(safe_decode_text(value) or "")) is not None
            ):
                forwards.append(_Forward(name, position, keyword, parameter))
    return tuple(forwards)


def _returned_names(
    func_node: Node, scopes: frozenset[str], aliases: Mapping[str, str]
) -> tuple[frozenset[str], frozenset[str]]:
    """The simple names `func_node`'s own returns hand back (`return invoke`,
    `return r.run`), and those whose call they return (`return make()`,
    Python's only, as the call pass reads them)."""
    returned: set[str] = set()
    called: set[str] = set()
    stack = list(func_node.children)
    while stack:
        node = stack.pop()
        if node.type in scopes:
            continue
        stack.extend(node.children)
        if node.type != cs.TS_PY_RETURN_STATEMENT:
            continue
        for value in node.named_children:
            value = _uncast(value)
            into = returned
            if value.type == cs.TS_PY_CALL:
                value = value.child_by_field_name(cs.FIELD_FUNCTION)
                into = called
            if (
                value is not None
                and value.type in _RETURNED
                and (name := _written_name(value, aliases))
            ):
                into.add(name)
    return frozenset(returned), frozenset(called)


def _file_calls(
    root: Node, aliases: Mapping[str, str], language: cs.SupportedLanguage
) -> FileCalls:
    """What the file's calls name (`FileCalls`), read over the whole file:
    a scope's bindings are told apart by nothing finer than their names."""
    chained: set[str] = set()
    handed: list[_Handed] = []
    bound: dict[str, set[str]] = {}
    called: set[str] = set()
    assigned: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type in _BINDINGS:
            for variable, factory in _binding(node, aliases):
                bound.setdefault(variable, set()).add(factory)
                assigned.add(variable)
            if node.type == cs.TS_PY_ASSIGNMENT and (
                target := node.child_by_field_name(cs.TS_FIELD_LEFT)
            ):
                if target.type == cs.TS_PY_IDENTIFIER and (
                    text := safe_decode_text(target)
                ):
                    assigned.add(text)
        if node.type not in _CALLS:
            continue
        callee = node.child_by_field_name(cs.FIELD_FUNCTION)
        if callee is None:
            continue
        if callee.type == cs.TS_PY_IDENTIFIER and (text := safe_decode_text(callee)):
            called.add(text)
        inner = callee
        while inner is not None and inner.type in _CALLS:
            inner = inner.child_by_field_name(cs.FIELD_FUNCTION)
        if inner is not callee and inner is not None:
            if name := _written_name(inner, aliases):
                chained.add(name)
        if not (name := _written_name(callee, aliases)):
            continue
        for position, keyword, value in _arguments(node):
            if value.type in _FUNCTION_LITERALS:
                handed.append(_Handed(name, position, keyword, None))
            elif (value.type == cs.TS_PY_IDENTIFIER or value.type in _MEMBERS) and (
                argument := _written_name(value, aliases)
            ):
                handed.append(_Handed(name, position, keyword, argument))
    factories: set[str] = set()
    if language in _CHAINED_LANGUAGES:
        factories |= chained
    if language in _ALIAS_LANGUAGES:
        factories = factories.union(
            *(bound[variable] for variable in called & bound.keys())
        )
    return FileCalls(frozenset(factories), tuple(handed), frozenset(assigned))


def _binding(node: Node, aliases: Mapping[str, str]) -> list[tuple[str, str]]:
    """The (name, factory) a `name = factory(...)` binding pairs."""
    name_field, value_field = _BINDINGS[node.type]
    left = node.child_by_field_name(name_field)
    right = node.child_by_field_name(value_field)
    if left is None or right is None:
        return []
    right = _uncast(right)
    if (
        left.type == cs.TS_PY_IDENTIFIER
        and right.type in _CALLS
        and (variable := safe_decode_text(left))
        and (function := right.child_by_field_name(cs.FIELD_FUNCTION)) is not None
        and (factory := _written_name(function, aliases))
    ):
        return [(variable, factory)]
    return []


def _peel(node: Node) -> Node:
    # `handler as Fn`, `handler.bind(this)`: the call pass hands `handler` on.
    while True:
        node = _uncast(node)
        if node.type != cs.TS_CALL_EXPRESSION:
            return node
        function = node.child_by_field_name(cs.FIELD_FUNCTION)
        if function is None or function.type != cs.TS_MEMBER_EXPRESSION:
            return node
        method = function.child_by_field_name(cs.FIELD_PROPERTY)
        target = function.child_by_field_name(cs.FIELD_OBJECT)
        if safe_decode_text(method) not in cs.JS_FUNCTION_PROTOTYPE_METHODS or (
            target is None
        ):
            return node
        node = target


def _uncast(node: Node) -> Node:
    # `return handler as Fn` hands back `handler` (`_unwrap_ts_cast`).
    while node.type in cs.TS_CAST_WRAPPER_TYPES and (inner := node.named_child(0)):
        node = inner
    return node
