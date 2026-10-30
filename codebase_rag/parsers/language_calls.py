"""The hooks the call pass reaches a language's call behaviour through.

`CallProcessor` owns the language-neutral pass (the scan of a caller's call
nodes, the site and resolution every edge carries, the name-based resolver
and what languages share) and looks a file's language up to the
`LanguageCalls` that language's package builds: the
`<Language>CallResolution` in `parsers/<language>/calls.py`, or this base
class for a language that adds nothing. A hook is a point where the pass asks
the language, named for what it asks, and by default answers as if the
language added nothing there; a capability is a rule several languages
share, read off the class."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..node_text import safe_decode_text
from ..types_defs import LanguageQueries, ScalaBinding

if TYPE_CHECKING:
    from ..language_spec import LanguageSpec
    from .php import class_names as php_class_names


class CallName(NamedTuple):
    # The callee name a language reads off a call node in its own shape.
    # `decided` False leaves the call to the shared reading of its `function`
    # field and node shape; True means `name` is the answer, and None there
    # is a call that names nothing.
    decided: bool
    name: str | None


UNDECIDED_NAME = CallName(False, None)


class Callee(NamedTuple):
    # What a call binds to through a language's own resolution. `decided`
    # False leaves the call to the name-based resolver; True means `callee`
    # is the answer, and None there is a known non-edge.
    decided: bool
    callee: tuple[str, str] | None


UNDECIDED_CALLEE = Callee(False, None)


class LanguageCalls:
    """A language's share of the call pass, as the answers the pass asks for.

    Subclassed by each language's call collaborator; the base itself serves
    a language that adds nothing to the pass."""

    __slots__ = ()

    # Interprocedural callable-parameter flow: a callback passed to a
    # first-party function whose parameter is invoked (directly or in a
    # nested closure) is traced to the concrete callback.
    flows_callables: ClassVar[bool] = False
    # A function handed over as an argument is a first-class value whose
    # target stays reachable; every flow language references it, and a
    # language may take this half without the flow.
    references_arguments: ClassVar[bool] = False
    # A file with no call node still gets the caller passes: the language's
    # callers make edges without one, which the class pass writes.
    scans_callers_without_calls: ClassVar[bool] = False
    # An anonymous function gets no caller pass, so a call nested only in
    # anonymous scopes is the module's rather than nobody's.
    anonymous_calls_reach_module: ClassVar[bool] = False
    # A pre-pass over every file records the functions a construction binds
    # to a class's callable fields (`collect_callable_field_bindings`).
    binds_callable_fields: ClassVar[bool] = False
    # A pre-pass over every file records the members a symbol key installs
    # (`collect_symbol_bindings`).
    keys_members_by_symbol: ClassVar[bool] = False
    # A function nested in a caller owns the calls in its body, so the
    # caller's share of the file's calls leaves those out instead of taking
    # everything its byte span holds.
    owns_nested_calls: ClassVar[bool] = False
    # The pass builds a caller's map of local variable types, through which
    # a receiver's type picks the method a call binds.
    types_locals: ClassVar[bool] = False
    # A `new X(...)` names a TYPE, never a method or function.
    new_constructs_type: ClassVar[bool] = False
    # A call no definition answers may bind a builtin of the language's
    # runtime (`CallScan.resolve_builtin`).
    binds_builtin_calls: ClassVar[bool] = False
    # An operator name no definition answers may bind a project overload
    # (`CallScan.resolve_cpp_op`).
    binds_operator_calls: ClassVar[bool] = False
    # Macros and functions live in SEPARATE namespaces: a macro invocation
    # binds only a macro, a plain call only a non-macro.
    separate_macro_namespace: ClassVar[bool] = False
    # A constructor is a method named like its class, and a construction
    # runs every one the class declares.
    runs_declared_constructors: ClassVar[bool] = False
    # The member a construction runs where the class declares it.
    constructor_member: ClassVar[str] = cs.PY_METHOD_INIT
    # A construction runs the class's own constructor or, where it declares
    # none, the one it inherits.
    inherits_constructors: ClassVar[bool] = False
    # A call dispatches in the FUNCTION namespace: a same-named function
    # owns `T(...)` over the type, and a type's construction runs its inner
    # constructor.
    functions_shadow_types: ClassVar[bool] = False
    # A call bound to a Protocol stub runs on every conformer instead.
    dispatches_protocol_stubs: ClassVar[bool] = False
    # A `.member` call bound through a class-body alias may run every method
    # the alias names.
    fans_out_member_aliases: ClassVar[bool] = False
    # A short-circuit operator (`||`, `??`, `&&`) evaluates to one of its
    # OPERANDS, so a function operand flows on as the value.
    short_circuit_values: ClassVar[bool] = False
    # A property in argument position is a VALUE READ, whose edge the
    # property-read pass owns, never a callback handed over.
    property_args_are_reads: ClassVar[bool] = False
    # A definition in another file is reachable only through an import (or a
    # Rust `use`): a call the resolver could not bind waits on an import,
    # which the importer lookup finds from the graph, so recording the
    # callee's name would only re-parse every module that calls a method of
    # that name (issue #1568). Where same-package or same-namespace
    # definitions are seen with no import at all (Go, Java, C#, C++ and
    # their like), the recorded name is the only link.
    imports_bind_calls: ClassVar[bool] = False

    def collect_callable_field_bindings(
        self, root_node: Node, module_qn: str, call_nodes: list[Node]
    ) -> None:
        """Record the functions a file's constructions bind to callable
        fields, resolved once every file's constructors are known."""

    def collect_symbol_bindings(self, root_node: Node, module_qn: str) -> None:
        """Record the symbol constants a file declares and the members it
        installs under them, resolved once every file is swept."""

    def ingest_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        """Write the edges a file makes at module scope without a call node,
        before the pass's early return for a file with no call: a function
        stored as a value, a construction that runs a constructor."""

    def caller_name(self, func_node: Node, module_qn: str) -> str | None:
        """The name a function node's body calls are attributed under, None
        for a node that gets no caller pass."""
        return safe_decode_text(func_node.child_by_field_name(cs.FIELD_NAME)) or None

    def method_name(self, method_node: Node, module_qn: str) -> str | None:
        """The name a function in a class body is attributed under; a node
        without one gets no caller pass."""
        return safe_decode_text(method_node.child_by_field_name(cs.FIELD_NAME))

    def class_name(self, class_node: Node) -> str | None:
        """A class's name as the definition pass spells it in its members'
        qualified names; a class without one gets no class pass."""
        return safe_decode_text(class_node.child_by_field_name(cs.FIELD_NAME))

    def class_qn(self, class_node: Node, module_qn: str, class_name: str) -> str | None:
        """A class's qualified name where the language's definition pass
        builds it its own way; None takes the nested-scope builder."""
        return None

    def function_qn(
        self, func_node: Node, module_qn: str, func_name: str
    ) -> str | None:
        """An unrecorded function's qualified name where the language's
        definition pass builds it its own way; None takes the nested-scope
        builder."""
        return None

    def bound_method_caller(
        self, func_node: Node, func_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        """(method qn, owner qn) of a method written outside its type's body,
        which the definition pass binds to that type's node."""
        return None

    def member_name(self, func_node: Node, func_name: str) -> str:
        """The member name a class-body function is registered under in its
        class's qualified name."""
        return func_name

    def caller_calls(self, func_node: Node, calls: list[Node]) -> list[Node]:
        """A caller's own calls, out of the calls its byte span holds."""
        return calls

    def attributable(self, func_nodes: list[Node], module_qn: str) -> list[Node]:
        """The function nodes that get a caller pass of their own and so own
        their calls; the calls of the rest bubble to the enclosing scope."""
        return func_nodes

    def scope_name(self, function_node: Node) -> str | None:
        """The name an enclosing function without a `name` field gives the
        qualified name of a function nested in it; None leaves it out."""
        return None

    def record_local_names(
        self, caller_node: Node, caller_qn: str, caller_type: str, module_qn: str
    ) -> None:
        """Record the names a caller binds itself, which a bare call there
        means instead of the definitions they shadow."""

    def local_type_spans(
        self,
        caller_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> list[tuple[int, int, str, str]]:
        """The byte windows over which each disagreeing declaration of a local
        types it, where the flat map can hold only one; the map may be
        rewritten to the types the language's own lookup binds."""
        return []

    def span_bindings(
        self,
        caller_node: Node,
        module_qn: str,
        class_context: str | None,
        local_var_types: dict[str, str],
    ) -> list[tuple[int, int, str, str]]:
        """Bindings that give one name a different type in different byte
        ranges of the caller, each call resolving against the one holding
        it."""
        return []

    def parameter_names(self, caller_node: Node) -> list[str] | None:
        """A caller's parameter names in order, where callable-parameter flow
        traces what each is bound to; None where it does not."""
        return None

    def ingest_property_reads(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        lang_config: LanguageSpec,
        prop_names: set[str],
    ) -> None:
        """Write the edges a caller makes by reading a property, which runs
        its getter without a call node."""

    def ingest_caller_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> None:
        """Write the edges a caller makes without a call node: a function it
        stores, returns or defaults as a value, a constructor or dunder its
        syntax runs."""

    def call_name(self, call_node: Node) -> CallName:
        """The callee name of a call node the language names whole, in a
        shape of its own, before the shared reading of the node."""
        return UNDECIDED_NAME

    def function_field_name(self, func_child: Node) -> str | None:
        """The callee name a call's `function` field spells in a shape of the
        language's own; None leaves it to the shared shapes."""
        return None

    def node_call_name(self, call_node: Node) -> str | None:
        """The callee name of a call whose `function` field names nothing, in
        a construction, operator or macro shape of the language's own; None
        leaves it to the shared shapes."""
        return None

    def fallback_call_name(self, ctx: CallScan, call_node: Node) -> str | None:
        """A name for a call no shape names, read from where it sits."""
        return None

    def template_params(self, caller_node: Node) -> frozenset[str]:
        """The type-parameter names in scope at a caller: a receiver typed to
        one has no concrete type there."""
        return frozenset()

    def ingest_call_shape(self, ctx: CallScan, call_node: Node) -> bool:
        """Write the edges of a call node of a shape the language takes whole,
        before it is named; True when the node was the language's."""
        return False

    def ingest_functor_call(self, ctx: CallScan, call_node: Node) -> bool:
        """Ingest a call on an object whose type defines the call operator,
        which no name lookup of the callee text reaches; True when it was
        one."""
        return False

    def ingest_call_site(
        self, ctx: CallScan, call_node: Node, call_name: str | None
    ) -> str | None:
        """Write what a call site hands over whether or not its callee binds,
        and return the callee name the scan goes on with."""
        return call_name

    def receiver_types(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
    ) -> dict[str, str] | None:
        """The local types a named call resolves its receiver through, where
        the call's own position narrows the caller's."""
        return call_var_types

    def ingest_own_call(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
    ) -> bool:
        """Ingest a named call the language binds its own way, never through
        the name lookup; True when the call was the language's."""
        return False

    def resolve_callee(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
    ) -> Callee:
        """The callee a call binds through the language's own resolution,
        before the name lookup."""
        return UNDECIDED_CALLEE

    def judge_callee(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        callee_info: tuple[str, str] | None,
    ) -> tuple[str, str] | None:
        """What the name lookup found, checked against what the call site
        says."""
        return callee_info

    def local_alias_callee(
        self, ctx: CallScan, call_node: Node, call_name: str
    ) -> tuple[str, str] | None:
        """The callee of a call the lookups left unbound, through a local
        alias of a callable or a type."""
        return None

    def ingest_indirect_calls(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        callee_info: tuple[str, str] | None,
    ) -> None:
        """Write the calls a named call makes besides its callee's: functions
        it runs through a field or an argument."""

    def ingest_unresolved_call(
        self, ctx: CallScan, call_node: Node, call_name: str
    ) -> None:
        """Write the edges a call whose callee did not bind still makes."""

    def record_cross_module_use(self, module_qn: str, entity_qn: str) -> None:
        """Record a module's use of a first-party entity, where the language
        pins its imports by what each module uses."""

    def bound_callee(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        callee_type: str,
        callee_qn: str,
    ) -> tuple[str, str]:
        """The callee a bound call's edges go to, after any edge the binding
        itself implies."""
        return callee_type, callee_qn

    def emit_protocol_conformer_edges(
        self,
        caller_spec: tuple[str, str, str],
        dispatch_targets: set[tuple[str, str]],
        ensure_rel: Callable[..., None],
    ) -> None:
        """Write the calls a call bound to a Protocol stub makes on each
        conformer."""

    def ingest_receiver_dispatch(
        self, ctx: CallScan, call_node: Node, call_name: str, callee_qn: str
    ) -> None:
        """Write the overrides a call through the instance's own receiver may
        run besides the callee it binds."""

    def emit_constructor_calls(
        self, ctx: CallScan, callee_qn: str, class_variants: list[str]
    ) -> bool:
        """Write the constructor calls a construction makes where the language
        finds its constructors its own way; True when it did."""
        return False

    def emit_binding_twin(
        self,
        caller_spec: tuple[str, str, str],
        twin_qn: str,
        twin_type: cs.NodeLabel,
        ensure_rel: Callable[..., None],
    ) -> None:
        """Write the edge a callee's same-named twin of another kind takes,
        where the name may hold either."""

    def build_targets(
        self,
        module_qn: str,
        callee_type: str,
        callee_qn: str,
        targets: list[str],
    ) -> tuple[list[str], dict[str, bool]]:
        """The targets a bound call reaches in the builds compiling the
        caller, and whether each is exact in all of them."""
        return targets, {}

    def emit_dispatch_fanouts(
        self, ctx: CallScan, callee_type: str, callee_qn: str
    ) -> bool:
        """Write the dispatch a bound call fans out to where the language
        dispatches its own way; True when it did, in place of the edge to an
        interface method's sole implementer."""
        return False

    def emit_callee_twins(
        self, ctx: CallScan, call_name: str, callee_type: str, callee_qn: str
    ) -> None:
        """Write the edges to the twins of a bound callee the call may run
        instead."""

    def emit_functor_callback(
        self,
        arg_node: Node,
        source_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        ensure_rel: Callable[..., None],
    ) -> bool:
        """Write the reference an object handed over as a callback makes to
        its type's call operator; True when the value was one."""
        return False

    def callback_name(
        self,
        arg_node: Node,
        arg_text: str,
        source_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        caller_qn: str | None,
        ensure_rel: Callable[..., None],
        rel_type: cs.RelationshipType,
    ) -> str | None:
        """The name a callback value is resolved by, or None where the
        language wrote its edges itself."""
        return arg_text


@dataclass(slots=True)
class CallScan:
    # Per-invocation state _ingest_function_calls hands every call node it
    # scans: the caller, its language's call strategy and bound resolvers,
    # and the lazily-built alias maps shared across the caller's call nodes.
    caller_node: Node
    caller_qn: str
    caller_spec: tuple[str, str, str]
    module_qn: str
    language: cs.SupportedLanguage
    queries: Mapping[cs.SupportedLanguage, LanguageQueries]
    calls: LanguageCalls
    class_context: str | None
    call_name_cache: dict[int, str | None] | None
    local_var_types: dict[str, str] | None
    span_bindings: list[tuple[int, int, str, str]]
    caller_params: frozenset[str]
    cpp_template_params: frozenset[str]
    resolve_func: Callable[..., tuple[str, str] | None]
    resolve_builtin: Callable[[str], tuple[str, str] | None] | None
    resolve_cpp_op: Callable[[str, str], tuple[str, str] | None] | None
    ensure_rel: Callable[..., None]
    arg_ref_rel: cs.RelationshipType
    scala_bindings: dict[str, list[ScalaBinding]] | None = None
    alias_map: dict[str, str] | None = None
    factory_aliases: dict[str, str] | None = None
    cpp_local_aliases: dict[str, list[tuple[str, int, int]]] | None = None
    php_scope_classes: php_class_names.ScopeCache | None = None
    cpp_indirection: dict[str, int] | None = None
