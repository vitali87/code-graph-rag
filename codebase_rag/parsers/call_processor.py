from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, NamedTuple

from loguru import logger
from tree_sitter import Node, Query, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..capture import ALL_ENABLED, CaptureSelection
from ..language_spec import (
    LANGUAGE_FQN_SPECS,
    LanguageSpec,
    sql_object_reference_name,
)
from ..node_text import decode_node_text, safe_decode_text, sorted_captures
from ..services import IngestorProtocol
from ..types_defs import (
    ASTCacheProtocol,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    PropertyDict,
    ScalaBinding,
)
from ..utils import qn_markers
from ..utils.fqn_resolver import scoped_name_parts
from ..utils.path_utils import cached_relative_path
from .call_resolver import PY_EXTERNAL_TARGET, CallResolver, Resolved
from .callable_params import (
    callable_parameter_invocations,
    cpp_parameter_names,
    go_parameter_names,
    js_ts_parameter_names,
    python_parameter_names,
)
from .class_ingest.identity import build_nested_qualified_name_for_class
from .cpp import calls as cpp_calls
from .cpp import local_types as cpp_local_types
from .cpp import utils as cpp_utils
from .cpp.function_values import (
    CFileScope,
    c_file_scope,
    c_function_value_identifiers,
    local_enumerator_names,
)
from .csharp import calls as csharp_calls
from .csharp import utils as csharp_utils
from .dart import calls as dart_calls
from .dart import utils as dart_utils
from .definition_docstring import extract_definition_docstring
from .definition_facts import DefinitionFacts
from .dispatch_registry import DispatchRegistryProcessor
from .field_nodes import declared_fields
from .flow_access import FlowProcessor
from .go import calls as go_calls
from .go.link_state import GoLinkState
from .import_processor import ImportProcessor
from .io_access import IOAccessProcessor
from .java import type_inference as java_ti
from .java import utils as java_utils
from .js_ts import bindings as js_bindings
from .js_ts import utils as js_ts_utils
from .julia import utils as julia_utils
from .lua import utils as lua_utils
from .php import class_names as php_class_names
from .php import utils as php_utils
from .python.class_aliases import scan_class_body_aliases
from .python.receiver_rebinding import ReceiverRebinding, receiver_rebinding
from .rpc_exposure import GoRpcExposureProcessor
from .rust import utils as rs_utils
from .scala import utils as scala_utils
from .sql.ddl import ddl_routine_references
from .string_call import load_string_call_specs, string_call_target
from .type_inference import TypeInferenceEngine
from .utils import (
    call_site_start,
    enclosing_class_node,
    function_span_key,
    get_function_captures,
    node_site_properties,
    shared_or_new,
    written_simple_name,
)


class _CallableFlowArg(NamedTuple):
    # One call-site argument that may carry a callable: bound to a concrete
    # function (source_concrete) or to a caller parameter (source_caller +
    # source_param), keyed to the callee parameter by position or keyword.
    callee_qn: str
    position: int
    keyword: str
    source_concrete: str
    source_caller: str
    source_param: str


class _FactoryCall(NamedTuple):
    # A call `x(args)` where x was bound by `x = factory(...)`. Each returned
    # closure of factory receives args, so a callback argument flows into that
    # closure's callable parameter. Resolved in finalize once every function's
    # returned callables are known.
    scope_qn: str
    factory_qn: str
    positional: tuple[str, ...]
    keyword: tuple[tuple[str, str], ...]


class _PassedCallback(NamedTuple):
    # A callback one call site hands to a callable parameter, resolved there.
    # Its edge is written in finalize at the receiver's own invocations of the
    # parameter, whose file the walk may not have reached yet (issue #2459).
    source_spec: tuple[str, str, str]
    param_name: str
    rel_type: str
    target_spec: tuple[str, str, str]
    resolution: str


class _FileCallableFlow:
    # One file's callable-flow records. Kept per file because the processor
    # outlives a pass: a re-walk must replace only that file's records, while
    # a file the pass leaves alone still seeds slots finalize resolves.
    __slots__ = ("args", "returned_callables", "factory_calls", "invocations", "passed")

    def __init__(self) -> None:
        self.args: list[_CallableFlowArg] = []
        self.returned_callables: dict[str, set[str]] = {}
        self.factory_calls: list[_FactoryCall] = []
        # (function, parameter) -> the edge site of each place this file's
        # function invokes that parameter: where a callback it receives runs.
        self.invocations: dict[tuple[str, str], tuple[PropertyDict, ...]] = {}
        self.passed: list[_PassedCallback] = []


class _RefEmitScope(NamedTuple):
    # The caller-side context a reference walk hands every edge it emits
    # through _emit_callback_edge.
    caller_spec: tuple[str, str, str]
    module_qn: str
    local_var_types: dict[str, str] | None
    class_context: str | None
    caller_qn: str | None
    resolve_func: Callable[..., tuple[str, str] | None]
    ensure_rel: Callable[..., None]


class _ScalaCallee(NamedTuple):
    # What a Scala call binds to through the compiler's lookup. `decided`
    # False leaves the call to the name-based resolver; True means `callee`
    # is the answer, and None there is a known non-edge.
    decided: bool
    callee: tuple[str, str] | None


class _PhpClassCallee(NamedTuple):
    # What a PHP call whose class the site shows binds to. `decided` False
    # leaves the call to the name-based resolver; True means `callee` is the
    # answer, and None there is a known non-edge.
    decided: bool
    callee: tuple[str, str] | None


@dataclass(slots=True)
class _CallScanContext:
    # Per-invocation state _ingest_function_calls hands every call node it
    # scans: the caller, its language flags and bound resolvers, and the
    # lazily-built alias maps shared across the caller's call nodes.
    caller_node: Node
    caller_qn: str
    caller_spec: tuple[str, str, str]
    module_qn: str
    language: cs.SupportedLanguage
    queries: Mapping[cs.SupportedLanguage, LanguageQueries]
    class_context: str | None
    call_name_cache: dict[int, str | None] | None
    local_var_types: dict[str, str] | None
    span_bindings: list[tuple[int, int, str, str]]
    caller_params: frozenset[str]
    is_java: bool
    is_csharp: bool
    is_js_ts: bool
    is_cpp: bool
    is_python: bool
    is_flow_lang: bool
    is_arg_ref_lang: bool
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


_TYPED_LANGUAGES = frozenset(
    {
        cs.SupportedLanguage.PYTHON,
        cs.SupportedLanguage.JS,
        cs.SupportedLanguage.TS,
        cs.SupportedLanguage.TSX,
        cs.SupportedLanguage.JAVA,
        cs.SupportedLanguage.CSHARP,
        cs.SupportedLanguage.LUA,
        cs.SupportedLanguage.GO,
        cs.SupportedLanguage.CPP,
        cs.SupportedLanguage.RUST,
        cs.SupportedLanguage.DART,
    }
)

# C and C++ share the function_definition/declarator shape, so the callee
# name lives in a nested declarator (no `name` field), needing the libclang
# declarator-aware extractor rather than a plain child_by_field_name("name").
_C_FAMILY_LANGUAGES = frozenset({cs.SupportedLanguage.C, cs.SupportedLanguage.CPP})
_JS_TS_LANGUAGES = cs.JS_TS_LANGUAGES
# Python callers whose own bindings shadow module and class names for the
# whole body (issue #2666); a class body or module is excluded on purpose.
_PY_LOCAL_SCOPE_CALLERS = frozenset({cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_LAMBDA})
# A callee written as a name or attribute may be a typed instance; one written
# as a call (`Dispatcher()(1)`) is typed by what that call returns.
_PY_INSTANCE_CALLEE_TYPES = frozenset(
    {cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE, cs.TS_PY_CALL}
)

# Every class shape whose body a `this.f` read can belong to.
_JS_TS_ANY_CLASS_NODES = frozenset(
    {*cs.JS_TS_CLASS_NODES, cs.TS_ABSTRACT_CLASS_DECLARATION}
)
# The class members whose `static` keyword decides what `this` is inside.
_JS_CLASS_MEMBER_TYPES = frozenset(
    {cs.TS_METHOD_DEFINITION, cs.TS_PUBLIC_FIELD_DEFINITION, cs.TS_JS_FIELD_DEFINITION}
)
# Expressions that write their target.
_JS_FIELD_WRITE_TYPES = frozenset(
    {
        cs.TS_JS_ASSIGNMENT_EXPRESSION,
        cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION,
        cs.TS_JS_UPDATE_EXPRESSION,
    }
)


class _ReceiverDeclaration(NamedTuple):
    """What a JS/TS call site declares its receiver to be (issue #2609).

    `foreign` means the declaration names a type the project does not define
    (a built-in, a library type, `any`); otherwise `qn` is the first-party
    type whose inheritance chain may confirm a method binding.
    """

    qn: str
    foreign: bool


# Languages with argument-REFERENCE edges but no interprocedural
# callable-param flow: passing a function keeps it reachable (REFERENCES),
# while invocation edges stay with the flow languages.
_ARG_REF_ONLY_LANGUAGES = frozenset(
    {cs.SupportedLanguage.CSHARP, cs.SupportedLanguage.DART}
)
# Nodes walked down a call's left spine before giving up on counting calls:
# a long operator chain (`(a + b + ... ).m()`) is not a receiver chain.
_CHAIN_SPINE_WALK_LIMIT = cs.MAX_RECEIVER_CHAIN_HOPS * 8


def _exceeds_receiver_chain_cap(call_node: Node) -> bool:
    """Whether `call_node` ends a receiver chain of too many calls (#2262).

    Every call in an n-hop chain (`a.m().m()...`) is resolved on its own
    text, which holds the whole receiver, so each pass over it made one long
    chain O(n^2). Walking the leftmost spine and counting nodes of the
    call's own type measures the chain in O(cap) without reading its text.
    """
    call_type = call_node.type
    calls = 0
    node = call_node
    for _ in range(_CHAIN_SPINE_WALK_LIMIT):
        # `child(0)` raises on a leaf rather than returning None.
        if node.child_count == 0:
            return False
        node = node.child(0)
        if node is None:
            return False
        if node.type == call_type:
            calls += 1
            if calls >= cs.MAX_RECEIVER_CHAIN_HOPS:
                return True
    return False


def _php_anonymous_class_qn(node: Node, module_qn: str) -> str | None:
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


def _class_qn_for_calls(
    class_node: Node,
    module_qn: str,
    class_name: str,
    language: cs.SupportedLanguage,
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
) -> str:
    # A C++ class inside a namespace, or a NESTED class (Outer.Inner), is bound
    # by the definition pass through its enclosing scope (qn `module.ns.Class`
    # / `module.Outer.Inner`); the bare `module.class_name` join drops those
    # ancestors, dangling every inline method's CALLS source off a phantom
    # node. Use the SAME builders the definition pass uses so the qns agree.
    if language == cs.SupportedLanguage.CPP:
        return cpp_utils.build_qualified_name(class_node, module_qn, class_name)
    if language == cs.SupportedLanguage.PHP and (
        anonymous_qn := _php_anonymous_class_qn(class_node, module_qn)
    ):
        return anonymous_qn
    return (
        build_nested_qualified_name_for_class(
            class_node, module_qn, class_name, queries[language][cs.QUERY_CONFIG]
        )
        or f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
    )


# Python nested-scope boundaries and sequence-literal node types used when
# scanning a scope for dispatch tables of function references.
_PY_SCOPE_BOUNDARY_TYPES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_DECORATED_DEFINITION,
    }
)
_PY_SEQUENCE_LITERAL_TYPES = frozenset({cs.TS_PY_LIST, cs.TS_PY_SET, cs.TS_PY_TUPLE})
# Dispatch-table literals whose values may name handler functions: Python dict
# and JS/TS object (key/value pairs), Python list/set/tuple and JS/TS array
# (positional elements). All use the `pair`/named-child shapes handled below.
_DICT_LIKE_COLLECTION_TYPES = frozenset({cs.TS_PY_DICTIONARY, cs.TS_OBJECT})
_SEQUENCE_LIKE_COLLECTION_TYPES = _PY_SEQUENCE_LITERAL_TYPES | frozenset({cs.TS_ARRAY})
# Python nodes that transparently wrap first-class values one level down:
# sequence literals, a bare multi-value return (expression_list), and
# parentheses. Dict pairs and ternaries need field-aware handling, matched
# separately in _expand_py_first_class_values.
_PY_VALUE_WRAPPER_TYPES = _PY_SEQUENCE_LITERAL_TYPES | frozenset(
    {cs.TS_PY_EXPRESSION_LIST, cs.TS_PARENTHESIZED_EXPRESSION}
)
# A Java/C# object creation: its call name is the constructed TYPE, so the
# resolver must not offer a same-named method or function (issue #1629).
_OBJECT_CREATION_NODE_TYPES = frozenset(
    {
        cs.TS_OBJECT_CREATION_EXPRESSION,
        cs.TS_CSHARP_IMPLICIT_OBJECT_CREATION_EXPRESSION,
        cs.TS_SCALA_INSTANCE_EXPRESSION,
    }
)
# Languages whose `new X(...)` names a TYPE, never a method or function.
_NEW_CONSTRUCTS_LANGUAGES = frozenset(
    {cs.SupportedLanguage.JAVA, cs.SupportedLanguage.CSHARP, cs.SupportedLanguage.SCALA}
)
_CALLABLE_NODE_LABELS = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
)
# Node types of a call argument that may name a callable: a bare identifier
# (Python/Go/JS/TS), a Python attribute (self.method), a Go selector (x.Method),
# or a JS/TS member expression (obj.method).
_FLOW_ARG_REF_TYPES = frozenset(
    {
        cs.TS_PY_IDENTIFIER,
        cs.TS_PY_ATTRIBUTE,
        cs.TS_SELECTOR_EXPRESSION,
        cs.TS_MEMBER_EXPRESSION,
    }
)
# Node types of a value that hands a callable over BY NAME, in every language
# the argument-reference path serves: the flow-arg names above, a C# member
# access or generic method group, a Dart tear-off's `.name` selector (its
# argument unwraps to the last selector), a C++ qualified, template or field
# name, and a JS shorthand property. (C++ address-of is checked on its own in
# `_names_a_callable`.) An operator expression is absent on purpose: it
# computes a value, and its text resolved as a name against a builtin receiver
# gave `builtin.Array.prototype.length - start` (issue #2438).
_CALLBACK_REF_TYPES = _FLOW_ARG_REF_TYPES | frozenset(
    {
        cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION,
        cs.TS_CSHARP_GENERIC_NAME,
        cs.TS_DART_SELECTOR,
        cs.TS_CPP_QUALIFIED_IDENTIFIER,
        cs.TS_CPP_TEMPLATE_FUNCTION,
        cs.TS_CPP_FIELD_EXPRESSION,
        cs.TS_SHORTHAND_PROPERTY_IDENTIFIER,
    }
)
# Qualified-name prefix marking a resolved callee as a builtin rather than a
# first-party function whose body the call chain can be followed into.
_BUILTIN_QN_PREFIX = f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}"
# Transparent wrappers a bound arrow may sit behind in its declarator:
# parens and TS casts (`const f = ((x) => ...) as T`). Climbed when
# recovering the arrow's binding name so it is not treated as anonymous.
_TS_BINDING_WRAPPER_TYPES = cs.TS_CAST_WRAPPER_TYPES | {cs.TS_PARENTHESIZED_EXPRESSION}
# Assignment node type -> RHS field, per language family: Python `assignment`
# and JS/TS `assignment_expression` (client.post = fn) carry the RHS in
# `right`; a JS/TS `variable_declarator` (const cb = handler) carries it in
# `value`. Go binds a func value the same way; its RHS sits behind an
# expression_list, unwrapped in the walker.
_ASSIGNMENT_RHS_FIELDS = {
    cs.TS_PY_ASSIGNMENT: cs.TS_FIELD_RIGHT,
    cs.TS_ASSIGNMENT_EXPRESSION: cs.TS_FIELD_RIGHT,
    # A JS/TS logical-assign default (`obj.when ??= (p) => {...}`, `x ||= fn`)
    # stores the RHS function for later dynamic dispatch, exactly like a plain
    # assignment, so its RHS is scanned the same way (same `right` field). Only a
    # callable RHS (an inline arrow or a name resolving to a function) yields an
    # edge; a data RHS (`count += 1`) resolves to nothing and adds none.
    cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION: cs.TS_FIELD_RIGHT,
    # A class field bound to a NAMED first-class function value (`log = noop`)
    # references it like a `const` declarator. (An inline-arrow field value is a
    # method definition, referenced elsewhere, not a name to resolve here.)
    cs.TS_PUBLIC_FIELD_DEFINITION: cs.FIELD_VALUE,
    cs.TS_VARIABLE_DECLARATOR: cs.FIELD_VALUE,
    cs.TS_GO_VAR_SPEC: cs.FIELD_VALUE,
    cs.TS_GO_SHORT_VAR_DECLARATION: cs.TS_FIELD_RIGHT,
    cs.TS_GO_ASSIGNMENT_STATEMENT: cs.TS_FIELD_RIGHT,
}
# RHS node types that name a callable value: a bare identifier, a Python
# attribute (mod.fn), a JS/TS member expression (handlers.run), or a Go
# selector (pkg.Fn).
_ASSIGNMENT_RHS_REF_TYPES = frozenset(
    {
        cs.TS_PY_IDENTIFIER,
        cs.TS_PY_ATTRIBUTE,
        cs.TS_MEMBER_EXPRESSION,
        cs.TS_GO_SELECTOR_EXPRESSION,
    }
)
# JSX nodes that carry a component name (self-closing and opening; a
# closing element repeats the name, so a paired element emits once).
_JSX_NAMED_ELEMENT_TYPES = frozenset(
    {cs.TS_JSX_SELF_CLOSING_ELEMENT, cs.TS_JSX_OPENING_ELEMENT}
)
# What a JSX tag can render: a function or class component (an arrow bound to
# a const registers as a Function). An Interface/Type/Enum of the same name is
# never renderable, so a tag resolving to one is a name collision (issue #2535).
_JSX_COMPONENT_TARGET_TYPES = frozenset(
    {cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD, cs.NodeLabel.CLASS}
)
# JS/TS function scopes that REBIND `this` per call (a plain function /
# generator / function expression), as opposed to an arrow_function (inherits
# `this` lexically) or a method_definition / class-field arrow (binds `this` to
# the instance). Used to tell whether a `this.M()` call site lexically refers to
# the enclosing class instance.
_JS_THIS_REBINDING_FUNC_TYPES: frozenset[str] = frozenset(
    t
    for t in cs.JS_TS_FUNCTION_NODES
    if t not in (cs.TS_ARROW_FUNCTION, cs.TS_METHOD_DEFINITION)
)


def _scope_qn_candidates(scope_qn: str) -> list[str]:
    # The scope itself plus its duplicate-variant-stripped form (`useStore@27`
    # -> `useStore`): the def pass registers nested/anon members under the
    # NATURAL qn while the caller may carry the variant suffix. Registry-guarded,
    # so a scope without a twin adds nothing.
    # Compare against the STRIPPED form rather than testing for the marker
    # character: a C# verbatim identifier (`@event`) contains it without
    # carrying a marker, and would otherwise add a duplicate candidate that
    # is merely the scope again (issue #2017).
    natural = qn_markers.natural_qn(scope_qn)
    if natural == scope_qn:
        return [scope_qn]
    return [scope_qn, natural]


def _scoped_candidates(scope_qn: str, names: Iterable[str]) -> set[str]:
    # Every reference path that names a member of the caller's scope must try
    # the variant-stripped scope too; building the candidates in one place
    # keeps a new path from repeating the object-literal gap, where only the
    # inline-argument path tried it.
    return {
        f"{scope}{cs.SEPARATOR_DOT}{name}"
        for scope in _scope_qn_candidates(scope_qn)
        for name in names
    }


def _names_a_callable(node: Node) -> bool:
    # C/C++ also hands a function over by its address (`run(&Cls::m)`), the
    # one operator expression that names rather than computes; a dereference
    # (`*p`) or any other operator does not.
    if node.type == cs.TS_CPP_POINTER_EXPRESSION:
        operator = node.child_by_field_name(cs.FIELD_OPERATOR)
        return operator is not None and safe_decode_text(operator) == cs.CPP_ADDRESS_OF
    return node.type in _CALLBACK_REF_TYPES


def _first_class_value_children(
    node: Node, is_dart: bool, is_js_ts: bool = False
) -> list[Node] | None:
    # The wrapped value nodes a container/branch node hands onward, or None
    # for a leaf that IS the first-class value.
    if node.type in _PY_VALUE_WRAPPER_TYPES:
        return list(node.named_children)
    if is_dart and node.type in dart_calls.VALUE_WRAPPER_TYPES:
        return dart_calls.collection_values(node)
    if node.type == cs.TS_PY_DICTIONARY:
        return _py_dict_values(node)
    if node.type in (cs.TS_PY_CONDITIONAL_EXPRESSION, cs.TS_JS_TERNARY_EXPRESSION):
        return _conditional_result_operands(node, is_dart)
    # A TS cast (`(() => {}) as any`, `fn satisfies T`, `cb!`) is transparent for
    # first-class-value resolution: the wrapped value is the first named child.
    if node.type in cs.TS_CAST_WRAPPER_TYPES and node.named_children:
        return [node.named_children[0]]
    # A JS/TS short-circuit operator (`fn ?? (() => {})`, `a || cb`) binds one of
    # its OPERANDS as the value, exactly like Python's boolean_operator; JS/TS
    # spells it `binary_expression`. Gated to JS/TS: Go and C also use
    # `binary_expression` with `||`/`&&`, where a bare-name operand resolves to a
    # boolean, not a callable, and expanding it would fabricate a phantom edge.
    if (
        is_js_ts
        and node.type == cs.TS_BINARY_EXPRESSION
        and (operator := node.child_by_field_name(cs.FIELD_OPERATOR)) is not None
        and safe_decode_text(operator) in cs.JS_SHORT_CIRCUIT_OPERATORS
    ):
        return list(node.named_children)
    if (
        is_dart
        and node.type.endswith(cs.DART_EXPRESSION_NODE_SUFFIX)
        # A scope-opening node (function_expression) IS the first-class
        # value; only flat operator nodes hand a swallowed ternary onward.
        and node.type not in cs.DART_NESTED_SCOPE_NODE_TYPES
        and (kids := node.named_children)
        and kids[-1].type == cs.TS_PY_CONDITIONAL_EXPRESSION
    ):
        # tree-sitter-dart parses low-precedence operators OVER the ternary
        # (`a + b > 0 ? f : null` -> additive_expression(a, +,
        # conditional_expression(...))), swallowing the conditional as the
        # LAST child of the operator node; its result operands are still the
        # handed-over values (issue #873).
        return [kids[-1]]
    if node.type == cs.TS_PY_BOOLEAN_OPERATOR:
        return [
            operand
            for operand in (
                node.child_by_field_name(cs.TS_FIELD_LEFT),
                node.child_by_field_name(cs.TS_FIELD_RIGHT),
            )
            if operand is not None
        ]
    return None


def _py_dict_values(node: Node) -> list[Node]:
    return [
        pair_value
        for pair in node.named_children
        if pair.type == cs.TS_PY_PAIR
        and (pair_value := pair.child_by_field_name(cs.FIELD_VALUE)) is not None
    ]


def _conditional_result_operands(node: Node, is_dart: bool) -> list[Node]:
    # tree-sitter-python exposes NO field names on conditional_expression
    # (child_by_field_name returns None for every operand), so the result
    # operands are positional: [body, condition, alternative]. A shape that
    # is not exactly three named operands falls back to all of them,
    # over-referencing rather than dropping a branch.
    operands = list(node.named_children)
    if len(operands) != 3:
        return operands
    # Dart and JS/TS (`ternary_expression`) order [condition, consequence,
    # alternative]; Python (`conditional_expression`) orders [body, condition,
    # alternative]. Pick the two result operands, never the truthiness-tested one.
    condition_first = is_dart or node.type == cs.TS_JS_TERNARY_EXPRESSION
    return [operands[1], operands[2]] if condition_first else [operands[0], operands[2]]


def _py_param_name(child: Node) -> str | None:
    match child.type:
        case cs.TS_PY_IDENTIFIER:
            return safe_decode_text(child)
        case cs.TS_PY_DEFAULT_PARAMETER | cs.TS_PY_TYPED_DEFAULT_PARAMETER:
            name_node = child.child_by_field_name(cs.FIELD_NAME)
            return safe_decode_text(name_node) if name_node else None
        case cs.TS_PY_TYPED_PARAMETER:
            inner = child.named_children[0] if child.named_children else None
            return (
                safe_decode_text(inner)
                if inner is not None and inner.type == cs.TS_PY_IDENTIFIER
                else None
            )
        case _:
            # *args/**kwargs/keyword_separator never bind fields.
            return None


def _js_symbol_member_call_parts(call_node: Node) -> tuple[str, str] | None:
    # `recv[kSym].method(...)` -> ("kSym", "method"); None for other shapes.
    func = call_node.child_by_field_name(cs.FIELD_FUNCTION)
    if func is None or func.type != cs.TS_MEMBER_EXPRESSION:
        return None
    obj = func.child_by_field_name(cs.FIELD_OBJECT)
    prop = func.child_by_field_name(cs.FIELD_PROPERTY)
    if obj is None or prop is None or obj.type != cs.TS_SUBSCRIPT_EXPRESSION:
        return None
    index = obj.child_by_field_name(cs.TS_FIELD_INDEX)
    if index is None or index.type != cs.TS_IDENTIFIER:
        return None
    sym_name = safe_decode_text(index)
    method = safe_decode_text(prop)
    if not sym_name or not method:
        return None
    return sym_name, method


def _iife_wrapper_inner(target: Node) -> Node | None:
    # One value-transparent wrapper layer: parens and casts wrap their
    # operand, the comma operator yields its LAST operand. None stops the
    # peel (no wrapper, or an empty one).
    if target.type == cs.TS_PARENTHESIZED_EXPRESSION:
        return next(
            (child for child in target.named_children if child.type != cs.TS_COMMENT),
            None,
        )
    if target.type == cs.TS_JS_SEQUENCE_EXPRESSION:
        return target.named_children[-1] if target.named_children else None
    if target.type in cs.TS_CAST_WRAPPER_TYPES:
        return next(iter(target.named_children), None)
    return None


def _body_function_nodes(
    body_node: Node,
    function_query: Query | None,
    sorted_func_nodes: list[Node] | None,
    func_node_starts: list[int] | None,
) -> list[Node] | None:
    # The function nodes inside a class body: sliced from the pre-sorted
    # capture list when the caller has one, else queried directly. None when
    # the language has no function query at all.
    if sorted_func_nodes is not None and func_node_starts is not None:
        body_end = body_node.end_byte
        lo = bisect_left(func_node_starts, body_node.start_byte)
        hi = bisect_right(func_node_starts, body_end)
        return [n for n in sorted_func_nodes[lo:hi] if n.end_byte <= body_end]
    if not function_query:
        return None
    captures = sorted_captures(QueryCursor(function_query), body_node)
    return captures.get(cs.CAPTURE_FUNCTION, [])


def _next_macro_token(token: Node) -> Node | None:
    # The token after `token` in a macro's stream, comments skipped.
    following = token.next_sibling
    while following is not None and following.type in cs.RS_COMMENT_TYPES:
        following = following.next_sibling
    return following


def _macro_turbofish_reaches_args(ident: Node) -> bool:
    """Whether a macro-token identifier is a call, through any turbofish.

    `make::<u64>()` in a macro is the flat stream `make :: < u64 > ( )`, so
    the query's identifier-then-`(` shape missed it and every turbofish call
    inside `assert!`/`println!` lost its edge (issue #3212). An identifier
    followed by `::<` is a call only when the generic arguments close straight
    into an argument group; `a::<B>::c()` continues the path instead. A
    comment between them (`make::<u64> /* type */ ()`) is a token too.
    """
    scope = _next_macro_token(ident)
    if scope is None or scope.type != cs.TS_RS_TOKEN_SCOPE:
        return True
    depth = 0
    token = _next_macro_token(scope)
    while token is not None:
        depth += cs.RS_MACRO_ANGLE_DEPTH.get(token.type, 0)
        if depth <= 0:
            break
        token = _next_macro_token(token)
    if token is None or depth != 0 or (args := _next_macro_token(token)) is None:
        return False
    return (
        args.type == cs.TS_RS_TOKEN_TREE
        and args.child_count > 0
        and args.children[0].type == cs.CHAR_PAREN_OPEN
    )


def _find_call_arguments_node(call_node: Node) -> Node | None:
    args_node = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
    if args_node is not None:
        return args_node
    # C# target-typed `new(...)` exposes NO fields at all; its argument_list
    # is an unfielded named child (Serilog hands its CreateLogger local
    # functions to `return new(...)`, which the fielded lookup silently
    # dropped).
    args_node = next(
        (
            child
            for child in call_node.named_children
            if child.type == cs.TS_CSHARP_ARGUMENT_LIST
        ),
        None,
    )
    if args_node is not None:
        return args_node
    return dart_calls.call_arguments_node(call_node)


def _add_py_keyword_argument(child: Node, keyword: dict[str, Node]) -> None:
    name_node = child.child_by_field_name(cs.FIELD_NAME)
    value_node = child.child_by_field_name(cs.FIELD_VALUE)
    if (
        name_node is not None
        and value_node is not None
        and (name := safe_decode_text(name_node)) is not None
    ):
        keyword[name] = value_node


def _split_call_arguments(args_node: Node) -> tuple[list[Node], dict[str, Node]]:
    positional: list[Node] = []
    keyword: dict[str, Node] = {}
    for child in args_node.named_children:
        # C# wraps every argument expression in an `argument` node (which
        # may carry a ref/out modifier or a name: colon); Dart wraps
        # positional values the same way. Unwrap to the expression itself,
        # its LAST named child, so downstream reference-type checks see
        # the identifier, not the wrapper.
        if (
            child.type in (cs.TS_CSHARP_ARGUMENT, cs.TS_DART_ARGUMENT)
            and child.named_children
        ):
            child = child.named_children[-1]
        if child.type == cs.TS_DART_NAMED_ARGUMENT:
            dart_calls.add_named_argument(child, keyword)
        elif child.type == cs.TS_PY_KEYWORD_ARGUMENT:
            _add_py_keyword_argument(child, keyword)
        else:
            positional.append(child)
    return positional, keyword


_PY_UNPACKING_FLAGS = {
    cs.TS_PY_LIST_SPLAT: cs.KEY_STAR_ARGS,
    cs.TS_PY_DICTIONARY_SPLAT: cs.KEY_STAR_KWARGS,
}


def call_site_properties(node: Node) -> PropertyDict:
    """Edge-site properties for the expression that produced an edge (#1522).

    Every site carries its span; a node with an argument list (a call, a
    constructor invocation) also carries `arg_count` and the keyword names
    it passes, so a consumer can check arity at the site without re-parsing.
    A reference site (a bare function value, an attribute read) has no
    argument list and carries the span alone.

    A Python `*rest` or `**opts` passes an unknown number of arguments, so
    it is flagged instead of counted. Counted as one positional, it made
    `send(req, **opts)` pass two positionals to `def send(request,
    **kwargs)`, a `too_many` that failed `cgr check` on code that runs
    (issue #2635). The split itself keeps the unpacking in `positional`:
    the callback passes read argument slots from it.
    """
    props = node_site_properties(node)
    # A member call's node spans its receiver, so every hop of a chain began
    # where the chain begins: callers showed the wrong line for a multi-line
    # chain, and two `.put(...)` hops of one chain merged into one edge on
    # their shared (line, col) (issue #3166). The site starts at the name.
    row, col = call_site_start(node)
    props[cs.KEY_LINE] = row + 1
    props[cs.KEY_COL] = col
    args_node = _find_call_arguments_node(node)
    if args_node is not None:
        positional, keyword = _split_call_arguments(args_node)
        written = 0
        for argument in positional:
            if (flag := _PY_UNPACKING_FLAGS.get(argument.type)) is not None:
                props[flag] = True
            else:
                written += 1
        props[cs.KEY_ARG_COUNT] = written + len(keyword)
        props[cs.KEY_KWARG_NAMES] = list(keyword)
        if _count_unknown(args_node, positional):
            props[cs.KEY_SPREAD_ARGS] = True
        if (qualifier := _call_qualifier(node)) is not None:
            props[cs.KEY_CALL_QUALIFIER] = qualifier
    return props


def _count_unknown(args_node: Node, positional: list[Node]) -> bool:
    """Whether the call passes a number of values its arguments do not show.

    Counted as one argument, `f(...args)` would read as too few for
    `f(a, b)` on a call that type-checks (issue #2517). A tagged template
    passes its strings array and one value per substitution, not the
    fragments its node holds.
    """
    if args_node.type == cs.TS_TEMPLATE_STRING:
        return True
    return any(
        argument.type in _SPREAD_ARGUMENTS for argument in positional
    ) or go_calls.passes_every_result(args_node, positional)


# Arguments passing an unknown number of values. PHP's `argument` wrapper is
# unwrapped with C#'s, which shares its node type, so the unpacking is seen.
_SPREAD_ARGUMENTS = frozenset(
    {
        cs.TS_SPREAD_ELEMENT,
        cs.TS_GO_VARIADIC_ARGUMENT,
        cs.TS_PHP_VARIADIC_UNPACKING,
    }
)


def _call_qualifier(node: Node) -> str | None:
    """What a Rust or C# call is written through, for receiver counting.

    `S::m(s, 1)` passes the receiver that `s.m(1)` leaves implicit, and so
    does C#'s `Util.Ext(s, 1)` against `s.Ext(1)` (issue #2517). A path, or
    a C# name that binds no value at the call, records its last segment
    (`S`, `Self`, `Util`); a value records "": every Rust `.` call, and a
    C# call through a local, parameter, field or property, `this`, a
    literal or `s?.Ext(1)`. A bare call records nothing.
    """
    function = node.child_by_field_name(cs.FIELD_FUNCTION)
    if function is None:
        return None
    if node.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
        return _csharp_call_qualifier(node, function)
    if node.type != cs.TS_RS_CALL_EXPRESSION:
        return None
    if function.type == cs.TS_GENERIC_FUNCTION:
        # `S::m::<u8>(s)`: the turbofish wraps the path.
        function = function.child_by_field_name(cs.FIELD_FUNCTION)
    if function is None:
        return None
    if function.type == cs.TS_SCOPED_IDENTIFIER:
        path = function.child_by_field_name(cs.TS_RS_FIELD_PATH)
        return (_last_name(path) or None) if path is not None else None
    # C++ shares the node type; only Rust's carries a `value` field.
    if (
        function.type == cs.TS_RS_FIELD_EXPRESSION
        and function.child_by_field_name(cs.FIELD_VALUE) is not None
    ):
        return ""
    return None


# A bare C# callee: `Ext(s, 1)` under `using static`, receiver as argument.
_CSHARP_BARE_CALLEES = frozenset({cs.TS_CSHARP_IDENTIFIER, cs.TS_CSHARP_GENERIC_NAME})


def _csharp_call_qualifier(call: Node, function: Node) -> str | None:
    if function.type in _CSHARP_BARE_CALLEES:
        return None
    if function.type != cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
        # `s?.Ext(1)` and every other non-member form take it from a value.
        return ""
    left = function.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
    head = left
    while head is not None and head.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
        head = head.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
    if head is None:
        return ""
    if head.type == cs.TS_CSHARP_IDENTIFIER:
        # A string named `Util` makes `Util.Ext(1, 2)` an instance call
        # even where an extension class `Util` exists (#2832).
        name = safe_decode_text(head)
        if not name or csharp_utils.binds_value(call, name):
            return ""
    elif head.type != cs.TS_CSHARP_ALIAS_QUALIFIED_NAME:
        # `this.Util`, `Make().Util`, a literal: a value.
        return ""
    return _last_name(left)


def _last_name(node: Node | None) -> str:
    """The last segment of a plain or dotted name, "" for anything else."""
    if node is None:
        return ""
    if node.type == cs.TS_IDENTIFIER:
        return safe_decode_text(node) or ""
    if node.type in (
        cs.TS_SCOPED_IDENTIFIER,
        cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION,
        cs.TS_CSHARP_ALIAS_QUALIFIED_NAME,
    ):
        return _last_name(node.child_by_field_name(cs.FIELD_NAME))
    # Rust `<S as Trait>` and other non-name paths still name a type.
    if node.type == cs.TS_RS_BRACKETED_TYPE:
        return " ".join((safe_decode_text(node) or "").split())
    return ""


_RESOLVED_RELS = frozenset(
    {
        cs.RelationshipType.CALLS,
        cs.RelationshipType.REFERENCES,
        cs.RelationshipType.INSTANTIATES,
    }
)


def _verdict_of(resolved: tuple[str, str] | None) -> str:
    """How surely a callee binds: the resolver's own verdict, and exact for an
    answer a frontend or a language engine gave, which never guesses."""
    return (
        resolved.resolution
        if isinstance(resolved, Resolved)
        else cs.EdgeResolution.EXACT
    )


def _site_scoped[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Restore the processor's current edge-site node when the pass returns.

    A pass sets `_site_node` as it visits nodes so every edge it emits picks
    up that site; restoring the previous value on exit keeps a nested pass
    from leaking its last node into the edges its caller emits afterwards.
    """

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        self = args[0]
        assert isinstance(self, CallProcessor)
        prev = self._site_node
        prev_resolution = self._resolution
        try:
            return fn(*args, **kwargs)
        finally:
            self._site_node = prev
            self._resolution = prev_resolution

    return wrapper


def _operator_dunder_target(node: Node) -> tuple[Node | None, str] | None:
    """(receiver, dunder) that a Python operator node dispatches to, if any.

    `x[k]` is `__getitem__` (or `__setitem__` as an assignment target),
    `k in x` is `x.__contains__`, and `len(x)` is `x.__len__`.
    """
    match node.type:
        case cs.TS_PY_SUBSCRIPT:
            parent = node.parent
            left = (
                parent.child_by_field_name(cs.TS_FIELD_LEFT)
                if parent is not None and parent.type == cs.TS_PY_ASSIGNMENT
                else None
            )
            is_write = left is not None and left.id == node.id
            return (
                node.child_by_field_name(cs.FIELD_VALUE),
                cs.PY_DUNDER_SETITEM if is_write else cs.PY_DUNDER_GETITEM,
            )
        case cs.TS_PY_COMPARISON_OPERATOR:
            operators = node.child_by_field_name(cs.TS_FIELD_OPERATORS)
            if (
                operators is not None
                and (op_text := safe_decode_text(operators))
                and cs.PY_OP_IN in op_text.split()
                and node.named_children
            ):
                return node.named_children[-1], cs.PY_DUNDER_CONTAINS
        case cs.TS_PY_CALL:
            func = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
            args = node.child_by_field_name(cs.FIELD_ARGUMENTS)
            if (
                func is not None
                and safe_decode_text(func) == cs.PY_BUILTIN_LEN
                and args is not None
                and len(args.named_children) == 1
            ):
                return args.named_children[0], cs.PY_DUNDER_LEN
    return None


def _truthiness_operands(node: Node) -> list[Node | None]:
    """The operands a Python node tests for truthiness (`__bool__`)."""
    match node.type:
        case cs.TS_PY_BOOLEAN_OPERATOR:
            return [
                node.child_by_field_name(cs.TS_FIELD_LEFT),
                node.child_by_field_name(cs.TS_FIELD_RIGHT),
            ]
        case cs.TS_PY_NOT_OPERATOR:
            return [node.child_by_field_name(cs.TS_FIELD_ARGUMENT)]
        case (
            cs.TS_PY_IF_STATEMENT
            | cs.TS_PY_WHILE_STATEMENT
            | cs.TS_PY_ELIF_CLAUSE
            | cs.TS_PY_CONDITIONAL_EXPRESSION
        ):
            # A bare object as a condition is tested for truthiness; nested
            # boolean/not operators are handled when the walk reaches them.
            return [node.child_by_field_name(cs.TS_FIELD_CONDITION)]
    return []


def _peel_ts_getter_receiver(recv: Node | None) -> tuple[Node | None, str | None]:
    """Peel type-transparent wrappers off a getter read's receiver.

    Parens, `x!`, and `x satisfies T` keep the operand's type, so they are
    peeled; an `as` cast ASSERTS a type, so the receiver resolves from that
    target type directly (`(box as Base).thing` links even when box is
    untyped). Returns (receiver, None), or (None, cast type text) for a cast,
    with "" when the cast's type has no text.
    """
    while recv is not None:
        if recv.type in (
            cs.TS_PARENTHESIZED_EXPRESSION,
            cs.TS_NON_NULL_EXPRESSION,
            cs.TS_SATISFIES_EXPRESSION,
        ):
            recv = recv.named_child(0)
        elif recv.type == cs.TS_AS_EXPRESSION:
            type_node = recv.named_children[-1] if recv.named_children else None
            return None, (safe_decode_text(type_node) or "") if type_node else ""
        else:
            break
    return recv, None


def _start_byte(node: Node) -> int:
    return node.start_byte


class CallProcessor:
    __slots__ = (
        "ingestor",
        "_note_unresolved",
        "_site_node",
        "_resolution",
        "_verdict",
        "_site_cache",
        "repo_path",
        "project_name",
        "module_qn_to_file_path",
        "_path_to_module_qn",
        "facts",
        "_resolver",
        "_string_call_specs",
        "_flow_param_names",
        "_go",
        "_cpp",
        "_dart",
        "_csharp",
        "_callable_flow",
        "_flow_bucket",
        "failed_call_files",
        "_call_file",
        "_io_processor",
        "_flow_processor",
        "_rpc_exposure",
        "_dispatch_registry",
        "_js_file_receiver_bindings",
        "js_symbol_constants",
        "_js_symbol_assignments",
        "js_symbol_member_types",
        "_js_proto_evidence_cache",
        "_c_file_scopes",
        "_c_include_closures",
        "_js_factory_classes",
    )

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        facts: DefinitionFacts,
        capture: CaptureSelection | None = None,
        module_qn_to_file_path: dict[str, Path] | None = None,
        ast_cache: ASTCacheProtocol | None = None,
        go_links: GoLinkState | None = None,
    ) -> None:
        self.ingestor = ingestor
        # The tree-sitter node whose span every edge emitted right now records
        # as its site (issue #1522); each pass sets it per visited node and
        # `_site_scoped` restores it on exit. The cache keeps the variant
        # fan-out of one site from re-deriving the same property dict.
        self._site_node: Node | None = None
        # Confidence of the edges emitted next (issue #1526); the main call
        # loop sets it from the resolver, fan-outs mark overloads.
        self._resolution: str = cs.EdgeResolution.EXACT
        # How surely the call node being scanned binds its callee (issue
        # #1526): noted where the scan resolves it, adjusted by the judgments
        # that follow, and read when its edges are emitted. The passes over
        # its arguments resolve their own references and keep their own.
        self._verdict: str = cs.EdgeResolution.EXACT
        # Keyed by the node OBJECT, not Node.id: tree-sitter recycles ids
        # across trees, and holding the node keeps its id from being reused.
        self._site_cache: tuple[Node, PropertyDict] | None = None
        self.repo_path = repo_path
        self.project_name = project_name
        # Dispatchers that name their callee in a string argument, declared in
        # the repository's .cgr.toml. Empty unless the project opts in.
        self._string_call_specs = load_string_call_specs(repo_path)
        self.module_qn_to_file_path = shared_or_new(module_qn_to_file_path, dict)
        self._path_to_module_qn: dict[Path, str] | None = None
        self.facts = facts
        # Per-module map of identifier -> introductions as (function node,
        # container) for the #988 lexical receiver resolution; built once per
        # file on first use.
        self._js_file_receiver_bindings: dict[
            str, dict[str, list[tuple[Node | None, Node | None]]]
        ] = {}
        # Symbol-keyed member dispatch (issue #989): qns of symbol CONSTANTS
        # (`kX = Symbol(...)`, `kX: Symbol(...)`), raw `[kX]: value` /
        # `obj[kX] = value` records as (module_qn, symbol_name, class_qn),
        # and the finalised symbol qn -> assigned class qns map. Strings
        # only: holding Node values would pin whole trees.
        self.js_symbol_constants: set[str] = set()
        self._js_symbol_assignments: list[tuple[str, str, str]] = []
        self.js_symbol_member_types: dict[str, set[str]] = {}
        self._js_proto_evidence_cache: dict[tuple[str, str | None], bool] = {}
        # What each C/C++ source declares at file scope, read when a bare
        # function name in C resolves outside its own file (issue #2529).
        self._c_file_scopes: dict[str, CFileScope] = {}
        # The repo headers each C/C++ file includes, directly or through
        # another header (#2593).
        self._c_include_closures: dict[str, frozenset[str]] = {}
        # factory qn -> the class its returns construct, read once per pass
        # (issue #2453). Strings only, so no tree is pinned.
        self._js_factory_classes: dict[str, str | None] = {}

        # The import processor owns the record (#1568); every pass notes
        # through it so the empty-name guard lives in one place.
        self._note_unresolved = import_processor.note_unresolved
        self._resolver = CallResolver(
            function_registry=function_registry,
            import_processor=import_processor,
            type_inference=type_inference,
            facts=self.facts,
            go_links=go_links,
        )
        self._go = go_calls.GoCallResolution(
            self, self._resolver, self.module_qn_to_file_path, self.facts
        )
        self._cpp = cpp_calls.CppCallResolution(self, self._resolver, self.facts)
        self._dart = dart_calls.DartCallResolution(self, self._resolver)
        self._csharp = csharp_calls.CSharpCallResolution(
            self, self._resolver, project_name, self.module_qn_to_file_path
        )
        # Inter-procedural callable-parameter flow: ordered params per function and
        # the per-call-site argument bindings, resolved to a fixpoint in finalize.
        self._flow_param_names: dict[str, list[str]] = {}
        # Return-value / factory tracing rides along: functions each function
        # may return (nested closures), and call sites `x = factory(...); x(cb)`
        # where cb flows into the returned closure's callable parameter.
        # Resolved to a fixpoint in finalize, so factory and call site may be
        # in any order. Keyed by the file whose walk recorded them, and the
        # file being walked writes into `_flow_bucket`.
        self._callable_flow: dict[Path, _FileCallableFlow] = {}
        self._flow_bucket = _FileCallableFlow()
        # Files whose call pass failed this run, whole or in one caller, for
        # the sync summary (issue #3173); `_call_file` is the file being walked.
        self.failed_call_files: set[Path] = set()
        self._call_file: Path | None = None
        selection = capture if capture is not None else ALL_ENABLED
        self._io_processor = IOAccessProcessor(
            ingestor,
            import_processor,
            selection=selection,
            module_paths=self.module_qn_to_file_path,
            ast_cache=ast_cache,
            go_package_names=self.facts.go_package_names,
            project_name=project_name,
        )
        self._dispatch_registry = DispatchRegistryProcessor(
            ingestor=ingestor,
            selection=selection,
            function_registry=function_registry,
        )
        self._rpc_exposure = GoRpcExposureProcessor(
            ingestor=ingestor,
            import_processor=import_processor,
            selection=selection,
            function_registry=function_registry,
            simple_name_lookup=type_inference.simple_name_lookup,
            module_paths=self.module_qn_to_file_path,
            go_package_names=self.facts.go_package_names,
            go_function_return_types=type_inference.facts.go_function_return_types,
            ast_cache=ast_cache,
        )
        self._flow_processor = FlowProcessor(
            ingestor,
            import_processor,
            self._resolver,
            selection=selection,
            function_locations=self.facts.function_locations,
            project_name=project_name,
        )

    def _emit_rel(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
        properties: PropertyDict | None = None,
    ) -> None:
        # Every CALLS/REFERENCES/INSTANTIATES edge this processor emits goes
        # through here so the current site (line/col/arg shape of the
        # producing expression) rides along as edge properties (#1522).
        # Invoking a class constructs it. A direct `Config()` is written as
        # INSTANTIATES, and a class reached through a callable parameter
        # (`ensure(Config)` running `obj_type()`) is the same fact; as CALLS it
        # was an edge type the schema does not have (issue #2394).
        if rel_type == cs.RelationshipType.CALLS and to_spec[0] == cs.NodeLabel.CLASS:
            rel_type = cs.RelationshipType.INSTANTIATES
        site = self._site_node
        if site is not None:
            cached = self._site_cache
            if cached is None or cached[0] is not site:
                cached = (site, call_site_properties(site))
                self._site_cache = cached
            properties = {**cached[1], **properties} if properties else dict(cached[1])
        if rel_type in _RESOLVED_RELS:
            properties = {**(properties or {}), cs.KEY_RESOLUTION: self._resolution}
        if properties:
            self.ingestor.ensure_relationship_batch(
                from_spec, rel_type, to_spec, properties=properties
            )
        else:
            self.ingestor.ensure_relationship_batch(from_spec, rel_type, to_spec)

    def _get_node_name(self, node: Node, field: str = cs.FIELD_NAME) -> str | None:
        name_node = node.child_by_field_name(field)
        if not name_node:
            return None
        text = name_node.text
        # Replacement decode, not strict: one undecodable byte in this file
        # must not abort the whole call pass and strand the file's CALLS
        # edges (issue #1797). This is NOT the call pass's only name funnel
        # -- `_get_call_target_name` decodes call targets separately and is
        # routed through the same helper.
        return None if text is None else decode_node_text(text)

    def _collect_all_call_nodes(
        self,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> tuple[list[Node], list[int]]:
        calls_query = queries[language].get(cs.QUERY_CALLS)
        if not calls_query:
            return [], []
        cursor = QueryCursor(calls_query)
        captures = sorted_captures(cursor, root_node)
        call_nodes = captures.get(cs.CAPTURE_CALL, [])
        call_starts = [n.start_byte for n in call_nodes]
        return call_nodes, call_starts

    def _filtered_calls_for(
        self,
        func_node: Node,
        language: cs.SupportedLanguage,
        all_call_nodes: list[Node] | None,
        call_starts: list[int] | None,
        owned_func_nodes: list[Node],
    ) -> list[Node] | None:
        # The caller's own calls, sliced from the module-wide call list. A Rust
        # named nested fn owns its body's calls, so its slice must exclude them
        # rather than take the whole byte range; every other language filters by
        # the plain byte span.
        if all_call_nodes is None or call_starts is None:
            return None
        if language in (
            cs.SupportedLanguage.RUST,
            cs.SupportedLanguage.JULIA,
        ):
            return self._calls_owned_by(
                func_node, owned_func_nodes, all_call_nodes, call_starts
            )
        calls = self._filter_calls_in_node(all_call_nodes, call_starts, func_node)
        if language == cs.SupportedLanguage.CPP:
            # A member function of a type written in this body is its own
            # caller; its calls are not also this function's (issue #2555).
            # A lambda is no caller of its own, so its calls still count here.
            return [
                call
                for call in calls
                if not cpp_local_types.in_local_type_member(call, func_node)
            ]
        return calls

    def _filter_calls_in_node(
        self,
        all_call_nodes: list[Node],
        call_starts: list[int],
        container: Node,
    ) -> list[Node]:
        start = container.start_byte
        end = container.end_byte
        # A Dart definition is a signature node whose body is a SIBLING;
        # widen to the body's end or every body call escapes its caller.
        if container.type in cs.DART_SIGNATURE_TYPES:
            end = dart_utils.definition_end_byte(container)
        lo = bisect_left(call_starts, start)
        hi = bisect_right(call_starts, end)
        return [n for n in all_call_nodes[lo:hi] if n.end_byte <= end]

    def _filter_top_level_calls(
        self,
        all_call_nodes: list[Node],
        call_starts: list[int],
        func_nodes: list[Node],
        module_qn: str,
    ) -> list[Node]:
        # Calls inside a function's BODY belong to that function, not the
        # module; only genuine top-level calls are module-attributed. The body
        # (not the whole node) is the boundary so def-time calls in the
        # signature (default args like `def f(x=make_default())` and
        # decorators) run at module load and stay module-attributed. A node
        # with no body is not a real function scope (e.g. a file-scope
        # declaration `int x = top();` captured as a function); its calls run
        # at load time, so it excludes nothing.
        nested_starts: set[int] = set()
        for func_node in func_nodes:
            # A Dart constructor's `: this(...)` / `: super(...)` / field
            # initializer clauses run with it, outside its body (issue #2482).
            for clause in dart_utils.constructor_clauses(func_node):
                for call in self._filter_calls_in_node(
                    all_call_nodes, call_starts, clause
                ):
                    nested_starts.add(call.start_byte)
            body = func_node.child_by_field_name(cs.FIELD_BODY)
            if body is None and func_node.type == cs.TS_SQL_CREATE_FUNCTION:
                # a SQL routine's body is an unnamed `function_body` child;
                # missing it kept every body call module-attributed as well
                body = next(
                    (
                        child
                        for child in func_node.named_children
                        if child.type == cs.TS_SQL_FUNCTION_BODY
                    ),
                    None,
                )
            if body is None:
                # a Dart body is a SIBLING of its signature, not a field
                body = dart_utils.body_node(func_node)
            if body is None and func_node.type in (
                cs.TS_JULIA_FUNCTION_DEFINITION,
                cs.TS_JULIA_MACRO_DEFINITION,
            ):
                # No body field: the body is everything after the signature.
                # A head the naming pass refuses owns no body: its calls
                # bubble to the module (issue #1882).
                if func_node.named_children and julia_utils.function_head_name(
                    func_node
                ):
                    start = func_node.named_children[0].end_byte
                    end = func_node.end_byte
                    lo = bisect_left(call_starts, start)
                    hi = bisect_right(call_starts, end)
                    for call in all_call_nodes[lo:hi]:
                        if call.end_byte <= end:
                            nested_starts.add(call.start_byte)
                continue
            if body is None:
                if (
                    func_node.type
                    in (
                        cs.TS_JULIA_ASSIGNMENT,
                        cs.TS_JULIA_ARROW_FUNCTION_EXPRESSION,
                    )
                    and len(func_node.named_children) >= 2
                ):
                    # The RHS is the body; an anonymous arrow's calls stay
                    # module-attributed or they drop entirely.
                    if self._julia_caller_name(func_node, module_qn) is None:
                        continue
                    body = func_node.named_children[-1]
                else:
                    continue
            for call in self._filter_calls_in_node(all_call_nodes, call_starts, body):
                nested_starts.add(call.start_byte)
        return [c for c in all_call_nodes if c.start_byte not in nested_starts]

    def _sibling_qn_in_macro_namespace(self, qn: str, want_macro: bool) -> str | None:
        # A same-named function/macro pair collides on one natural name;
        # the needed twin is the other bucket member.
        natural = qn.rsplit(cs.DUP_QN_MARKER, 1)[0] if cs.DUP_QN_MARKER in qn else qn
        registry = self._resolver.function_registry
        bucket = registry.variants(natural)
        if len(bucket) < 2:
            return None
        matches = [
            candidate
            for candidate in bucket
            if registry.get(candidate) is not None
            and (candidate in self.facts.macro_qns) == want_macro
        ]
        return matches[0] if len(matches) == 1 else None

    def _julia_caller_name(self, func_node: Node, module_qn: str) -> str | None:
        # The name the definition pass registers for this node, or None
        # while it stays anonymous; every caller pass derives attribution
        # through this helper so both passes name a node identically.
        name = julia_utils.function_head_name(
            func_node
        ) or julia_utils.arrow_assigned_name(func_node)
        if name:
            return name
        if (
            recorded := self._recorded_caller(func_node, module_qn)
        ) is not None and recorded.is_named:
            return recorded.qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        return None

    def _bare_decorator_name(self, decorator_node: Node) -> str | None:
        # A bare decorator `@task` / `@pkg.deco` (no call parens) is not a
        # `call` node, so the normal call pass misses it even though applying
        # it runs `task(func)` at module load. A call decorator `@deco(...)`
        # IS already captured, so skip it here.
        named = decorator_node.named_children
        if not named:
            return None
        expr = named[0]
        if expr.type in (cs.TS_IDENTIFIER, cs.TS_ATTRIBUTE) and expr.text is not None:
            return expr.text.decode(cs.ENCODING_UTF8)
        return None

    def _runs_at_module_load(self, node: Node) -> bool:
        # A definition runs at module load only at module or class-body scope;
        # nested inside a function body it runs at that function's call time,
        # so its decorator is not a module-load call.
        ancestor = node.parent
        while ancestor is not None:
            if ancestor.type == cs.TS_PY_FUNCTION_DEFINITION:
                return False
            ancestor = ancestor.parent
        return True

    @_site_scoped
    def _ingest_decorator_calls(
        self,
        nodes: list[Node],
        module_qn: str,
        root_node: Node,
        lang_config: LanguageSpec,
    ) -> None:
        # Emit `(Module)->decorator` CALLS for bare decorators on functions,
        # methods, AND classes: the decoration executes at module-load time,
        # so the module is the caller. Only first-party callables get an edge.
        module_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
        alias_map: dict[str, str] | None = None
        for node in nodes:
            parent = node.parent
            if parent is None or parent.type != cs.TS_PY_DECORATED_DEFINITION:
                continue
            if not self._runs_at_module_load(parent):
                continue
            alias_map = self._emit_decorator_calls_on(
                parent, module_spec, module_qn, root_node, lang_config, alias_map
            )

    def _emit_decorator_calls_on(
        self,
        parent: Node,
        module_spec: tuple[str, str, str],
        module_qn: str,
        root_node: Node,
        lang_config: LanguageSpec,
        alias_map: dict[str, str] | None,
    ) -> dict[str, str] | None:
        # One decorated definition's bare decorators, each a module-load CALLS
        # edge; returns the (lazily built) local-alias map for the next one.
        decorator_labels = (
            cs.NodeLabel.FUNCTION,
            cs.NodeLabel.METHOD,
            cs.NodeLabel.CLASS,
        )
        for child in parent.children:
            if child.type != cs.TS_PY_DECORATOR:
                continue
            self._site_node = child
            name = self._bare_decorator_name(child)
            if not name:
                continue
            callee, alias_map = self._resolve_decorator_callee(
                name, module_qn, root_node, lang_config, alias_map
            )
            if callee and callee[0] in decorator_labels:
                self._emit_bare_decorator_targets(module_spec, *callee)
        return alias_map

    def _emit_bare_decorator_targets(
        self, module_spec: tuple[str, str, str], callee_type: str, callee_qn: str
    ) -> None:
        # `@deco` runs `deco(func)`, so it binds as that call does: each
        # same-named definition is a candidate, and a class is constructed.
        # networkx's `@nx._dispatchable` is a class with a same-named docs
        # shim; skipping classes left its decorator sites on the shim alone
        # (issue #2621).
        registry = self._resolver.function_registry
        variants = registry.variants(callee_qn)
        prev_resolution = self._resolution
        if len(variants) > 1:
            self._resolution = cs.EdgeResolution.OVERLOAD
        try:
            for variant in variants:
                targets: list[tuple[str, str]] = []
                match registry.get(variant):
                    case cs.NodeLabel.FUNCTION | cs.NodeLabel.METHOD as kind:
                        targets.append((kind, variant))
                    case cs.NodeLabel.CLASS:
                        # `_emit_rel` writes a CALLS to a Class as INSTANTIATES.
                        targets.append((cs.NodeLabel.CLASS, variant))
                        init_qn = f"{variant}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
                        if init_qn in registry:
                            targets.append((cs.NodeLabel.METHOD, init_qn))
                    case None if callee_type != cs.NodeLabel.CLASS:
                        targets.append((callee_type, variant))
                for label, target_qn in targets:
                    self._emit_rel(
                        module_spec,
                        cs.RelationshipType.CALLS,
                        (label, cs.KEY_QUALIFIED_NAME, target_qn),
                    )
        finally:
            self._resolution = prev_resolution

    def _resolve_decorator_callee(
        self,
        name: str,
        module_qn: str,
        root_node: Node,
        lang_config: LanguageSpec,
        alias_map: dict[str, str] | None,
    ) -> tuple[tuple[str, str] | None, dict[str, str] | None]:
        resolver = self._resolver
        callee = resolver.resolve_function_call(name, module_qn)
        if not callee and cs.SEPARATOR_DOT not in name:
            # `@alias` where `alias = task` still calls task at load;
            # reuse the local-alias fallback the call pass uses.
            if alias_map is None:
                alias_map = self._build_local_alias_map(
                    root_node, lang_config, module_qn
                )
            if (rhs := alias_map.get(name)) is not None:
                callee = resolver.resolve_function_call(rhs, module_qn)
        return callee, alias_map

    def _module_qn(self, file_path: Path, relative_path: Path) -> str:
        # The definition pass is the single source of truth for module qns:
        # same-stem siblings (Aggregator.cpp / Aggregator.h) get a
        # collision-disambiguated qn there, and recomputing from the path
        # here attributed every caller inside such a header to a module qn
        # with no node, silently dropping its CALLS (issue #652).
        if self._path_to_module_qn is None:
            self._path_to_module_qn = {
                path: qn for qn, path in self.module_qn_to_file_path.items()
            }
        if registered := self._path_to_module_qn.get(file_path):
            return registered
        file_name = file_path.name
        if file_name in (*cs.PY_PACKAGE_INIT_FILES, cs.MOD_RS):
            return cs.SEPARATOR_DOT.join(
                [self.project_name] + list(relative_path.parent.parts)
            )
        return cs.SEPARATOR_DOT.join(
            [self.project_name] + list(relative_path.with_suffix("").parts)
        )

    def collect_callable_field_bindings(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        func_class_captures_cache: dict[Path, dict] | None = None,
    ) -> None:
        # Pre-pass: record which functions are bound to a class's callable
        # fields (FQNSpec(get_name=_python_get_name, ...)). Runs before call
        # resolution so a field invocation resolves regardless of which file
        # the construction site lives in. Bindings are recorded PENDING
        # (keyword name or positional index) and resolved by
        # finalize_field_bindings after every file's ctor metadata (param
        # order + param->attribute renames) is collected.
        if language != cs.SupportedLanguage.PYTHON:
            return
        try:
            module_qn = self._module_qn(
                file_path, cached_relative_path(file_path, self.repo_path)
            )
            self._collect_ctor_field_metadata(root_node, module_qn)
            call_nodes = (
                func_class_captures_cache[file_path].get(cs.CAPTURE_CALL)
                if func_class_captures_cache is not None
                and file_path in func_class_captures_cache
                else None
            )
            if call_nodes is None:
                call_nodes, _ = self._collect_all_call_nodes(
                    root_node, language, queries
                )
            for call_node in call_nodes:
                self._record_call_field_bindings(call_node, module_qn)
        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            self.failed_call_files.add(file_path)
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)

    def _record_call_field_bindings(self, call_node: Node, module_qn: str) -> None:
        # A class construction whose arguments name functions binds each
        # to the matching (keyword or positional) ctor field.
        resolver = self._resolver
        positional, keyword = self._parse_call_arguments(call_node)
        if not positional and not keyword:
            return
        name = self._get_call_target_name(call_node)
        if not name:
            return
        callee = resolver.resolve_function_call(name, module_qn)
        if not callee or callee[0] != cs.NodeLabel.CLASS:
            return
        registry = resolver.function_registry
        callable_labels = (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
        arg_entries: list[tuple[int | str, Node]] = list(enumerate(positional))
        arg_entries.extend(keyword.items())
        for key, value_node in arg_entries:
            if not (value_text := safe_decode_text(value_node)):
                continue
            bound = resolver.resolve_function_call(value_text, module_qn)
            if bound and bound[0] in callable_labels and bound[1] in registry:
                resolver.record_pending_field_binding(callee[1], key, bound[1])

    def finalize_callable_field_bindings(self) -> None:
        self._resolver.finalize_field_bindings()

    def _collect_ctor_field_metadata(self, root_node: Node, module_qn: str) -> None:
        # For every class in this file, record the ordered ctor param names
        # (__init__ params, or annotated class-body fields for
        # NamedTuple/dataclass classes without __init__) and the
        # param -> attribute renames from `self.attr = param` statements.
        # The stack carries each node's ENCLOSING class qn so a nested class
        # (Inner inside Outer) resolves to module.Outer.Inner: a bare
        # resolve_function_call("Inner", module_qn) would miss it. A class
        # inside a FUNCTION is skipped (qn is caller-scoped), so
        # nested-in-method classes never reach here.
        stack: list[tuple[Node, str | None]] = [
            (child, None) for child in root_node.children
        ]
        while stack:
            node, enclosing_qn = stack.pop()
            if node.type == cs.TS_PY_FUNCTION_DEFINITION:
                continue
            if node.type == cs.TS_PY_CLASS_DEFINITION:
                self._visit_ctor_class(node, enclosing_qn, module_qn, stack)
            else:
                stack.extend((c, enclosing_qn) for c in node.children)

    def _visit_ctor_class(
        self,
        node: Node,
        enclosing_qn: str | None,
        module_qn: str,
        stack: list[tuple[Node, str | None]],
    ) -> None:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        class_name = safe_decode_text(name_node) if name_node else None
        body = node.child_by_field_name(cs.FIELD_BODY)
        if not class_name or body is None:
            return
        class_qn = self._ctor_class_qn(class_name, enclosing_qn, module_qn)
        # Descend into the body with THIS class as the enclosing scope so
        # its nested classes resolve; skip metadata when unresolved.
        stack.extend((c, class_qn) for c in body.children)
        if class_qn is not None:
            self._record_class_ctor_metadata(body, class_qn)

    def _ctor_class_qn(
        self, class_name: str, enclosing_qn: str | None, module_qn: str
    ) -> str | None:
        resolver = self._resolver
        if enclosing_qn is not None:
            candidate_qn = f"{enclosing_qn}{cs.SEPARATOR_DOT}{class_name}"
            if resolver.function_registry.get(candidate_qn) == cs.NodeLabel.CLASS:
                return candidate_qn
            return None
        resolved = resolver.resolve_function_call(class_name, module_qn)
        return resolved[1] if resolved and resolved[0] == cs.NodeLabel.CLASS else None

    def _record_class_ctor_metadata(self, body: Node, class_qn: str) -> None:
        resolver = self._resolver
        if init_node := self._find_init_method(body):
            params = self._ordered_param_names(init_node)
            resolver.record_ctor_params(class_qn, params)
            self._record_param_attr_renames(init_node, class_qn, set(params))
        else:
            resolver.record_ctor_params(class_qn, self._annotated_field_names(body))

    @staticmethod
    def _find_init_method(class_body: Node) -> Node | None:
        for child in class_body.children:
            candidate = child
            if candidate.type == cs.TS_PY_DECORATED_DEFINITION:
                candidate = (
                    candidate.child_by_field_name(cs.FIELD_DEFINITION) or candidate
                )
            if candidate.type != cs.TS_PY_FUNCTION_DEFINITION:
                continue
            name_node = candidate.child_by_field_name(cs.FIELD_NAME)
            if name_node is not None and safe_decode_text(name_node) == (
                cs.PY_METHOD_INIT
            ):
                return candidate
        return None

    @staticmethod
    def _ordered_param_names(init_node: Node) -> tuple[str, ...]:
        params_node = init_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if params_node is None:
            return ()
        names: list[str] = []
        for child in params_node.named_children:
            name = _py_param_name(child)
            if name and name != cs.PY_KEYWORD_SELF:
                names.append(name)
        return tuple(names)

    def _record_param_attr_renames(
        self, init_node: Node, class_qn: str, params: set[str]
    ) -> None:
        # `self.ctx_factory = create_context` stores the param under a
        # DIFFERENT name; the field invocation goes through the attribute.
        body = init_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return
        self_prefix = f"{cs.PY_KEYWORD_SELF}{cs.SEPARATOR_DOT}"
        stack: list[Node] = list(body.children)
        while stack:
            node = stack.pop()
            # A `self.x = param` inside a nested helper (def later():
            # self.cb = handler) is that helper's store, not a constructor
            # rename; descending would let it override the real __init__
            # store, so stop at nested scopes.
            if node.type in _PY_SCOPE_BOUNDARY_TYPES:
                continue
            if node.type == cs.TS_PY_ASSIGNMENT:
                left = node.child_by_field_name(cs.TS_FIELD_LEFT)
                right = node.child_by_field_name(cs.TS_FIELD_RIGHT)
                if (
                    left is not None
                    and right is not None
                    and left.type == cs.TS_PY_ATTRIBUTE
                    and right.type == cs.TS_PY_IDENTIFIER
                    and (left_text := safe_decode_text(left))
                    and left_text.startswith(self_prefix)
                    and (param := safe_decode_text(right)) in params
                ):
                    attr = left_text[len(self_prefix) :]
                    if attr != param:
                        self._resolver.record_ctor_param_attr(class_qn, param, attr)
            stack.extend(node.children)

    @staticmethod
    def _annotated_field_names(class_body: Node) -> tuple[str, ...]:
        # NamedTuple/dataclass field order = annotated class-body assignments
        # (`fetch_name: Callable`, `other: int = 3`) in declaration order.
        names: list[str] = []
        for child in class_body.children:
            if child.type != cs.TS_PY_EXPRESSION_STATEMENT or not child.named_children:
                continue
            stmt = child.named_children[0]
            if stmt.type != cs.TS_PY_ASSIGNMENT:
                continue
            left = stmt.child_by_field_name(cs.TS_FIELD_LEFT)
            has_type = stmt.child_by_field_name(cs.FIELD_TYPE) is not None
            if (
                left is not None
                and left.type == cs.TS_PY_IDENTIFIER
                and has_type
                and (name := safe_decode_text(left))
            ):
                names.append(name)
        return tuple(names)

    def reset_resolution_caches(self) -> None:
        self._resolver.reset_resolution_caches()
        self._resolver.type_inference.reset_semantic_join_memos()
        # Both indexes are derived from module_qn_to_file_path and rebuilt
        # lazily: the Go package index only when the map's SIZE changes, the
        # path index never. A deleted module removed from the map and a new
        # one added in the same run leave the size unchanged, so a stale
        # entry would survive into Pass 3 and raise on lookup.
        self._go.reset_package_index()
        self._path_to_module_qn = None
        self._c_file_scopes = {}
        self._c_include_closures = {}
        # A re-parsed factory may construct something else now.
        self._js_factory_classes = {}
        # The C# per-class member index (issue #2453) is read off the
        # registry, which a re-parse just changed.
        if (csharp := self._resolver.type_inference._csharp_type_inference) is not None:
            csharp.reset_member_index()

    def process_calls_in_file(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        func_class_captures_cache: dict[Path, dict] | None = None,
    ) -> None:
        relative_path = cached_relative_path(file_path, self.repo_path)
        logger.debug(ls.CALL_PROCESSING_FILE, path=relative_path)
        # A re-walk describes the file's current source; appending to the old
        # records kept a removed call site's callback edge alive forever.
        self._flow_bucket = self._callable_flow[file_path] = _FileCallableFlow()
        self._call_file = file_path

        module_qn: str | None = None
        try:
            module_qn = self._module_qn(file_path, relative_path)

            self._dispatch_registry.process_file(root_node, module_qn, language)

            call_name_cache: dict[int, str | None] = {}

            combined_captures = self._file_combined_captures(
                file_path, root_node, queries[language], func_class_captures_cache
            )

            all_call_nodes, call_starts, func_node_starts = self._file_call_nodes(
                combined_captures, root_node, language, queries
            )
            sorted_func_nodes = combined_captures.get(cs.CAPTURE_FUNCTION)

            self._process_calls_in_functions(
                root_node,
                module_qn,
                language,
                queries,
                all_call_nodes,
                call_starts,
                call_name_cache=call_name_cache,
                combined_captures=combined_captures or None,
            )
            # Bare decorators (`@task`) are not call nodes; emit their
            # module-load CALLS before the empty-`all_call_nodes` early return,
            # since a file may have decorators but no other calls. Classes can
            # be decorated too, so include captured class nodes.
            # A dispatch table (HANDLERS = {"k": fn}) at module scope keeps its
            # entries reachable; scan before the no-calls early return so a file
            # that only defines functions and a table is still covered. Runs for
            # every flow language with an object/array/dict literal form.
            self._ingest_module_scope_references(
                root_node,
                module_qn,
                language,
                queries,
                combined_captures,
                sorted_func_nodes,
            )
            if (
                not all_call_nodes
                and language
                not in (
                    cs.SupportedLanguage.CSHARP,
                    cs.SupportedLanguage.CPP,
                    cs.SupportedLanguage.DART,
                )
                and language not in _JS_TS_LANGUAGES
            ):
                # A file with no call expressions has nothing further to
                # process, except in C#, where a class can still READ
                # properties (`return Size;`), C++, where a ctor's
                # member initializer list (`: buffer(g, 0)`) runs base
                # ctors without any call_expression node, Dart, where
                # a getter body can read another getter (`=> _wonders.length`,
                # issue #873), and JS/TS, where a method's inline callback
                # (`run() { return () => 1; }`) gets its REFERENCES edge only
                # when the class pass runs (issue #970); these passes run per
                # caller inside class processing, so they proceed.
                return
            self._process_calls_in_classes(
                root_node,
                module_qn,
                language,
                queries,
                all_call_nodes,
                call_starts,
                call_name_cache=call_name_cache,
                combined_captures=combined_captures,
                sorted_func_nodes=sorted_func_nodes,
                func_node_starts=func_node_starts,
            )
            module_calls = self._module_level_calls(
                all_call_nodes, call_starts, sorted_func_nodes, language, module_qn
            )
            self._ingest_function_calls(
                root_node,
                module_qn,
                cs.NodeLabel.MODULE,
                module_qn,
                language,
                queries,
                call_nodes=module_calls,
                call_name_cache=call_name_cache,
            )

        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            self.failed_call_files.add(file_path)
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)
        finally:
            self._call_file = None
            # The receiver binding index is only ever consulted by sites in
            # the SAME file; evicting here (errors and early returns
            # included) keeps the map from retaining one parse tree per
            # JS/TS module for the rest of the pass.
            if module_qn is not None:
                self._js_file_receiver_bindings.pop(module_qn, None)

    @staticmethod
    def _file_combined_captures(
        file_path: Path,
        root_node: Node,
        language_queries: LanguageQueries,
        func_class_captures_cache: dict[Path, dict] | None,
    ) -> dict[str, list[Node]]:
        if (
            func_class_captures_cache is not None
            and file_path in func_class_captures_cache
        ):
            return func_class_captures_cache[file_path]
        combined_query = language_queries[cs.QUERY_FUNC_CLASS]
        if combined_query:
            cursor = QueryCursor(combined_query)
            return sorted_captures(cursor, root_node)
        return {}

    def _file_call_nodes(
        self,
        combined_captures: dict[str, list[Node]],
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> tuple[list[Node], list[int] | None, list[int] | None]:
        # The file's call nodes plus the call/function start offsets the
        # per-scope filters bisect; both offset lists stay None when the file
        # captured no function or class to filter by.
        cached_calls = combined_captures.get(cs.CAPTURE_CALL)
        if cached_calls is not None:
            all_call_nodes = cached_calls
            call_starts: list[int] | None = None
        else:
            all_call_nodes, call_starts = self._collect_all_call_nodes(
                root_node, language, queries
            )

        sorted_func_nodes = combined_captures.get(cs.CAPTURE_FUNCTION)
        if sorted_func_nodes or combined_captures.get(cs.CAPTURE_CLASS):
            if cached_calls is not None:
                call_starts = [n.start_byte for n in all_call_nodes]
            func_node_starts = (
                [n.start_byte for n in sorted_func_nodes] if sorted_func_nodes else None
            )
        else:
            call_starts = None
            func_node_starts = None
        return all_call_nodes, call_starts, func_node_starts

    def _ingest_module_scope_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        if language == cs.SupportedLanguage.DART and (
            dart_prop_names := self._resolver.function_registry.property_names()
        ):
            self._dart.ingest_module_initializer_reads(
                root_node,
                module_qn,
                queries[language][cs.QUERY_CONFIG],
                dart_prop_names,
            )
        if language == cs.SupportedLanguage.PYTHON or language in _JS_TS_LANGUAGES:
            self._ingest_py_js_module_references(
                root_node, module_qn, language, queries
            )
        if language == cs.SupportedLanguage.GO:
            self._ingest_go_module_references(root_node, module_qn, language, queries)
        if language == cs.SupportedLanguage.C:
            # A file-scope ops table or hook struct (`static hooks_t h = {
            # my_alloc, my_free };`) is wired when the program loads, often in
            # a file that makes no call at all.
            self._ingest_c_function_value_references(
                root_node,
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                module_qn,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
            )
        if language in _JS_TS_LANGUAGES:
            # A module-scope JSX element (export default <App />) can sit
            # in a file with no call expressions; scan before the early
            # return like the assignment pass.
            self._ingest_jsx_component_references(
                root_node,
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                module_qn,
                None,
                None,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
            )
        if language == cs.SupportedLanguage.PYTHON:
            self._ingest_module_decorator_calls(
                root_node,
                module_qn,
                language,
                queries,
                combined_captures,
                sorted_func_nodes,
            )
            self._ingest_class_alias_references(
                module_qn, combined_captures.get(cs.CAPTURE_CLASS) or []
            )
        if language == cs.SupportedLanguage.JAVA:
            # A constant runs its enum's constructor without any call
            # expression, so a file holding only an enum has to be covered
            # before the no-calls early return (issue #2547).
            self._ingest_java_enum_constant_ctor_calls(
                module_qn, combined_captures.get(cs.CAPTURE_CLASS) or [], queries
            )
        if language == cs.SupportedLanguage.SQL:
            # A trigger, aggregate, cast, type or handler clause runs the
            # routine it names without an `invocation`; a schema file often
            # holds no call at all (issue #3181).
            self._ingest_sql_ddl_references(root_node, module_qn)

    @_site_scoped
    def _ingest_sql_ddl_references(self, root_node: Node, module_qn: str) -> None:
        # The module runs the DDL, so it is the caller, as for a CHECK
        # constraint's `invocation`. The site is the routine's name in the
        # clause; no node spans it when the statement did not parse.
        text, row, col = self._sql_source_as_written(root_node)
        if not text:
            return
        self._site_node = None
        module_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
        for reference in ddl_routine_references(text, row, col):
            self._resolution = cs.EdgeResolution.EXACT
            self._link_sql_routine(
                module_qn, module_spec, reference.name, reference.site
            )

    def _sql_source_as_written(self, root_node: Node) -> tuple[bytes | None, int, int]:
        # The text to scan and the point it starts at. The tree's own text can
        # be the statement-isolated re-parse's copy, which blanks every
        # statement the grammar cannot read on its own (#3180), and most DDL
        # that names a routine is such a statement. The bytes that tree was
        # parsed from hold them, whole: a blanked statement that opens or
        # closes the file lies outside the root's span.
        path = self._call_file
        source = self.facts.sql_sources.get(path) if path is not None else None
        if source is None:
            row, col = root_node.start_point
            return root_node.text, row, col
        return source, 0, 0

    @_site_scoped
    def _ingest_java_enum_constant_ctor_calls(
        self,
        module_qn: str,
        class_nodes: list[Node],
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        # Outside its own constructors, `CLASS("c")` is the only thing that runs
        # a Java enum constructor (`new` on an enum is a compile error), and no
        # call node names it, so every parameterised enum constructor looked
        # dead. Each constant CALLS the constructors its argument count admits;
        # one candidate is the compiler's choice, several (same arity) fan out
        # as `overload`, the way `new X(...)` does. No INSTANTIATES: that edge
        # only targets Class.
        for enum_node in class_nodes:
            if enum_node.type != cs.TS_ENUM_DECLARATION:
                continue
            constants = java_utils.enum_constants(enum_node)
            if not constants:
                continue
            ctors = [
                (ctor, target)
                for ctor in java_utils.enum_constructors(enum_node)
                if (
                    target := self._java_enum_ctor_target(
                        ctor, enum_node, module_qn, queries
                    )
                )
                is not None
            ]
            if not ctors:
                continue
            caller_spec = self._java_enum_initializer(enum_node, module_qn)
            for constant in constants:
                arg_count = java_utils.argument_count(constant)
                targets = [
                    target
                    for ctor, target in ctors
                    if java_utils.accepts_argument_count(ctor, arg_count)
                ]
                self._site_node = constant
                self._resolution = (
                    cs.EdgeResolution.EXACT
                    if len(targets) == 1
                    else cs.EdgeResolution.OVERLOAD
                )
                for target_label, target_qn in targets:
                    # A bare `NONE` has no argument list for the site to
                    # count, yet it is a zero-argument invocation all the same.
                    self._emit_rel(
                        caller_spec,
                        cs.RelationshipType.CALLS,
                        (target_label, cs.KEY_QUALIFIED_NAME, target_qn),
                        {cs.KEY_ARG_COUNT: arg_count, cs.KEY_KWARG_NAMES: []},
                    )

    def _java_enum_ctor_target(
        self,
        ctor_node: Node,
        enum_node: Node,
        module_qn: str,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> tuple[str, str] | None:
        if loc := self._recorded_caller(ctor_node, module_qn):
            return loc.label, loc.qualified_name
        # The class pass's own derivation, for a run that did not re-record
        # this file's definitions.
        enum_name = self._get_node_name(enum_node)
        ctor_name = self._get_node_name(ctor_node)
        if not enum_name or not ctor_name:
            return None
        language = cs.SupportedLanguage.JAVA
        target_qn, target_label = self._class_member_qn_and_label(
            ctor_node,
            _class_qn_for_calls(enum_node, module_qn, enum_name, language, queries),
            ctor_name,
            queries[language][cs.QUERY_CONFIG],
            language,
        )
        if target_qn not in self._resolver.function_registry:
            return None
        return target_label, target_qn

    def _java_enum_initializer(
        self, enum_node: Node, module_qn: str
    ) -> tuple[str, str, str]:
        # A local enum initialises when its method first uses it, so that
        # method runs the constructors: a dead method keeps them dead. Any
        # other enum initialises at class load, which the graph attributes to
        # the module, as it does a static field initializer.
        current = enum_node.parent
        while current is not None:
            if current.type in cs.JAVA_METHOD_NODE_TYPES:
                if loc := self._recorded_caller(current, module_qn):
                    return (loc.label, cs.KEY_QUALIFIED_NAME, loc.qualified_name)
                break
            current = current.parent
        return (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)

    def _ingest_py_js_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        collection_boundaries = self._flow_scope_boundaries(
            queries[language][cs.QUERY_CONFIG]
        )
        if language == cs.SupportedLanguage.PYTHON or language in _JS_TS_LANGUAGES:
            # A Python class body (import time) or a JS/TS class-field
            # initializer (construction time) can store a dispatch table
            # as a class ATTRIBUTE (django's `data_types = {"CharField":
            # _get_varchar_...}`; a JS `opts = { retryStrategy }`), which
            # is construction-load wiring like a module-level table; the
            # scan descends through classes but still stops at function
            # scopes. ponytail: decorated classes stay boundaries
            # (decorated_definition also wraps functions).
            collection_boundaries = collection_boundaries - frozenset(
                queries[language][cs.QUERY_CONFIG].class_node_types
            )
        self._ingest_collection_function_references(
            root_node,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            module_qn,
            None,
            None,
            collection_boundaries,
            language,
        )
        # A module-scope first-class assignment (registry_handler =
        # handle_event, module.exports.run = run) references its target
        # even in a file with no calls, so scan before the no-calls
        # early return.
        assignment_boundaries = self._flow_scope_boundaries(
            queries[language][cs.QUERY_CONFIG]
        )
        if language in _JS_TS_LANGUAGES:
            # A JS/TS class-field initializer (`log = noop`) runs at
            # construction, so descend into class bodies to reference the
            # function it binds; the scan still stops at method/function
            # scopes, so a local assignment inside a method stays theirs.
            assignment_boundaries = assignment_boundaries - frozenset(
                queries[language][cs.QUERY_CONFIG].class_node_types
            )
        self._ingest_assignment_function_references(
            root_node,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            module_qn,
            None,
            None,
            assignment_boundaries,
            language=language,
        )

    def _ingest_go_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        # A module-scope Go func map/slice (var funcMap = map[...]{...})
        # keeps its function entries reachable; scan before the no-calls
        # early return so a file defining only funcs and a table counts.
        self._go.ingest_composite_function_references(
            root_node,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            module_qn,
            None,
            None,
            self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
        )
        # A module-scope `var e = &Error{}` constructs too (issue #1642).
        self._go.ingest_composite_literal_instantiations(
            root_node,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            module_qn,
            self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
        )
        # A module-scope Go var bound to a bare function value
        # (var preExecHookFn = preExecHook) references it even in a file
        # with no calls, so scan before the no-calls early return.
        self._ingest_assignment_function_references(
            root_node,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            module_qn,
            None,
            None,
            self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
            language=language,
        )

    def _ingest_module_decorator_calls(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        decorator_targets = list(sorted_func_nodes or [])
        if combined_captures and (
            class_nodes := combined_captures.get(cs.CAPTURE_CLASS)
        ):
            decorator_targets.extend(class_nodes)
        if decorator_targets:
            self._ingest_decorator_calls(
                decorator_targets,
                module_qn,
                root_node,
                queries[language][cs.QUERY_CONFIG],
            )

    @_site_scoped
    def _ingest_class_alias_references(
        self, module_qn: str, class_nodes: list[Node]
    ) -> None:
        # `run = _plain` in a class body uses the method when the body runs
        # (issue #2620): at import for a top-level class, so from the module,
        # the way a call written there (`prop = property(_get, _set)`) is
        # already attributed; for a class inside a function, from that
        # function, so a dead factory keeps its class's implementation dead.
        module_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
        self._resolution = cs.EdgeResolution.EXACT
        for class_node in class_nodes:
            body = class_node.child_by_field_name(cs.FIELD_BODY)
            if body is None:
                continue
            references = scan_class_body_aliases(body).references
            if not references:
                continue
            scope = self._class_body_scope(class_node, module_qn, module_spec)
            if scope is None:
                continue
            for site, method in references:
                if (target := self._recorded_caller(method, module_qn)) is None:
                    continue
                self._site_node = site
                self._emit_rel(
                    scope,
                    cs.RelationshipType.REFERENCES,
                    (target.label, cs.KEY_QUALIFIED_NAME, target.qualified_name),
                )

    def _class_body_scope(
        self,
        class_node: Node,
        module_qn: str,
        module_spec: tuple[str, str, str],
    ) -> tuple[str, str, str] | None:
        ancestor = class_node.parent
        while ancestor is not None:
            if ancestor.type == cs.TS_PY_FUNCTION_DEFINITION:
                owner = self._recorded_caller(ancestor, module_qn)
                if owner is None:
                    return None
                return (owner.label, cs.KEY_QUALIFIED_NAME, owner.qualified_name)
            ancestor = ancestor.parent
        return module_spec

    def _module_level_calls(
        self,
        all_call_nodes: list[Node],
        call_starts: list[int] | None,
        sorted_func_nodes: list[Node] | None,
        language: cs.SupportedLanguage,
        module_qn: str,
    ) -> list[Node]:
        if sorted_func_nodes and call_starts is not None:
            # JS/TS: exclude only calls owned by ATTRIBUTABLE functions, so a
            # call nested purely in anonymous scopes (`create((set) => ({
            # inc: () => set((state) => ...) }))`, zustand's store shape) is
            # processed at module scope instead of by nobody: an anon arrow
            # gets no caller pass, and a call inside a NAMED function is
            # still excluded because that function's flat filter owns it.
            exclusion_nodes = (
                self._attributable_func_nodes(sorted_func_nodes, language, module_qn)
                if language in _JS_TS_LANGUAGES
                else sorted_func_nodes
            )
            return self._filter_top_level_calls(
                all_call_nodes, call_starts, exclusion_nodes, module_qn
            )
        return all_call_nodes

    def _process_calls_in_functions(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        all_call_nodes: list[Node] | None = None,
        call_starts: list[int] | None = None,
        call_name_cache: dict[int, str | None] | None = None,
        combined_captures: dict[str, list[Node]] | None = None,
    ) -> None:
        if combined_captures is not None:
            lang_config = queries[language][cs.QUERY_CONFIG]
            func_nodes = combined_captures.get(cs.CAPTURE_FUNCTION, [])
            has_classes = bool(combined_captures.get(cs.CAPTURE_CLASS))
        else:
            result = get_function_captures(root_node, language, queries)
            if not result:
                return
            lang_config, captures = result
            func_nodes = captures.get(cs.CAPTURE_FUNCTION, [])
            has_classes = bool(captures.get(cs.CAPTURE_CLASS))
        # Rust only: calls inside a NAMED nested `fn` (which gets its own caller
        # node) must be owned by that nested fn, not double-counted onto the
        # enclosing free function. Anonymous closures (not attributable) stay
        # excluded so their calls still bubble up. Other languages keep the flat
        # _filter_calls_in_node behavior their flow-tracing relies on.
        owned_func_nodes = self._attributable_func_nodes(
            func_nodes, language, module_qn
        )
        for func_node in func_nodes:
            # Anything scoped inside a class body -- a method, and a function
            # nested in a method -- is walked by _process_methods_in_class
            # with the class context set. Walking it here too emitted every
            # call edge twice, and once the class pass could type `self`
            # the second copy was a false bare-name edge (issue #1903).
            # Skip only what that pass will actually walk: it drops a class
            # it cannot name (a JS mixin's `(Base) => class extends Base`),
            # and those nested functions would otherwise lose every edge.
            if has_classes and self._class_pass_owns(func_node, lang_config, language):
                continue

            func_name = self._caller_func_name(func_node, language, module_qn)
            if func_name is None:
                continue
            try:
                self._ingest_func_node_calls(
                    func_node,
                    func_name,
                    module_qn,
                    language,
                    queries,
                    lang_config,
                    all_call_nodes,
                    call_starts,
                    owned_func_nodes,
                    call_name_cache,
                )
            except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
                # One pathological function costs its own edges, not every
                # other function's in the file (issue #3173).
                loc = self._recorded_caller(func_node, module_qn)
                caller = (
                    loc.qualified_name
                    if loc is not None
                    else f"{module_qn}{cs.SEPARATOR_DOT}{func_name}"
                )
                self._record_caller_failure(ls.CALLER_CALLS_FAILED, caller, e)

    def _caller_func_name(
        self, func_node: Node, language: cs.SupportedLanguage, module_qn: str
    ) -> str | None:
        # The caller name a function node's body calls are attributed under;
        # None means the node gets no caller pass and is skipped.
        if language in _C_FAMILY_LANGUAGES:
            return self._c_family_caller_func_name(func_node, language, module_qn)
        func_name = self._get_node_name(func_node)
        if not func_name and language in _JS_TS_LANGUAGES:
            func_name = self._js_ts_arrow_binding_name(func_node)
        if not func_name and language == cs.SupportedLanguage.SQL:
            # `create_function` has no `name` field either; without its name
            # the routine got no caller pass and its body's calls went to the
            # module (issue #2449).
            func_name = sql_object_reference_name(func_node)
        if (
            not func_name
            and language == cs.SupportedLanguage.LUA
            and func_node.type == cs.TS_LUA_FUNCTION_DEFINITION
        ):
            # A function expression bound to a variable or table field
            # (`local f = function()`, `M.f = function()`) has no name field;
            # the definition pass names it after its assignment target, so
            # recover the same name here or the whole body is skipped. A
            # table-constructor field value is named by its key path first
            # (issue #1631), through the SAME helper the definition pass
            # uses: a returned or passed table has no assignment target at
            # all, and without this the body's calls were dropped or
            # credited to the enclosing function (#1631).
            func_name = self._lua_caller_func_name(func_node)
        if not func_name and language == cs.SupportedLanguage.JULIA:
            # Julia definition nodes carry no `name` field; recover it
            # here or the body is skipped.
            func_name = julia_utils.function_head_name(
                func_node
            ) or julia_utils.arrow_assigned_name(func_node)
        if not func_name:
            # A nameless JS/TS function expression that a NAMED pass
            # registered (`exports.f = function`, `x: function`) has a
            # real node; its body's calls belong to that node, not the
            # module, so adopt the record's simple name and fall through
            # (the recorded-caller branch below reuses the registered qn).
            # A GENERATED record (anonymous callback, IIFE) keeps the
            # historical bubble-to-module attribution.
            if (
                language not in _JS_TS_LANGUAGES
                or (recorded := self._recorded_caller(func_node, module_qn)) is None
                or not recorded.is_named
            ):
                return None
            func_name = recorded.qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        return func_name

    def _c_family_caller_func_name(
        self, func_node: Node, language: cs.SupportedLanguage, module_qn: str
    ) -> str | None:
        # A macro-invocation artifact the ingest pass declined to register
        # (no class bears its name) must not become a call target either;
        # mirror ingest's decision via the recorded locations so the two
        # passes never diverge.
        if (
            language == cs.SupportedLanguage.CPP
            and cpp_utils.is_macro_invocation_artifact(func_node)
            and self._recorded_caller(func_node, module_qn) is None
        ):
            return None
        func_name = cpp_utils.extract_function_name(func_node)
        # The definition pass names a macro-split C definition from its
        # `type` field (issue #2528); without the same name here its body is
        # skipped and every call in it is lost.
        if not func_name and language == cs.SupportedLanguage.C:
            func_name = cpp_utils.c_macro_split_function_name(func_node)
        return func_name or None

    @staticmethod
    def _lua_caller_func_name(func_node: Node) -> str | None:
        field = lua_utils.field_function_path(func_node)
        if field is not None:
            return field[0]
        # A field with no name is anonymous in the definition pass too;
        # naming it after the assignment here would credit its calls to a
        # node that does not exist (#1750).
        if not lua_utils.is_field_value(func_node) and (
            name := lua_utils.extract_assigned_name(
                func_node, accepted_var_types=cs.LUA_NAMING_ASSIGNMENT_TARGETS
            )
        ):
            return name
        # Nameless, but a node the program reaches: the value of
        # `t["lume.clamp"] =` or `t[k] =`, or a function the module returns
        # (`return function() ... end`, `return { function() ... end }`). Its
        # body's calls were dropped with the name (issue #2578); they belong
        # to the node the definition pass registered under the generated name.
        if lua_utils.bracket_assignment_target(
            func_node
        ) is not None or lua_utils.is_module_return_value(func_node):
            return lua_utils.anonymous_function_name(func_node)
        return None

    def _ingest_func_node_calls(
        self,
        func_node: Node,
        func_name: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        lang_config: LanguageSpec,
        all_call_nodes: list[Node] | None,
        call_starts: list[int] | None,
        owned_func_nodes: list[Node],
        call_name_cache: dict[int, str | None] | None,
    ) -> None:
        # The definition pass records where every function/method node
        # landed; reuse that qn/label instead of re-deriving them from
        # the AST -- the walks diverge on preprocessor-distorted C++
        # class bodies, TS declaration merging, and duplicate-suffixed
        # qns, and every divergence is a phantom caller (issue #652).
        if loc := self._recorded_caller(func_node, module_qn):
            filtered = self._filtered_calls_for(
                func_node, language, all_call_nodes, call_starts, owned_func_nodes
            )
            self._ingest_function_calls(
                func_node,
                loc.qualified_name,
                loc.label,
                module_qn,
                language,
                queries,
                loc.container_qn,
                call_nodes=filtered,
                call_name_cache=call_name_cache,
            )
            return
        # An out-of-line C++ method definition (`Ret Class::method() {...}`
        # at namespace/file scope) is bound by the definition pass to its
        # class node (qn `class_qn.method`). Attribute its body's calls to
        # that method node, not a phantom module-rooted qn, so the CALLS
        # edges join to a real node.
        if language == cs.SupportedLanguage.CPP and (
            bound := self._cpp.out_of_class_method_caller(
                func_node, func_name, module_qn
            )
        ):
            caller_qn, class_qn = bound
            filtered = self._filtered_calls_for(
                func_node, language, all_call_nodes, call_starts, owned_func_nodes
            )
            self._ingest_function_calls(
                func_node,
                caller_qn,
                cs.NodeLabel.METHOD,
                module_qn,
                language,
                queries,
                class_qn,
                call_nodes=filtered,
                call_name_cache=call_name_cache,
            )
            return
        # A Go receiver method (`func (t T) m()`) is declared at file scope
        # but the definition pass binds it to its receiver type's node
        # (qn `module.T.m`). Attribute its body's calls to that method node,
        # not the receiver-dropping `module.m`, so the CALLS edges join to
        # a real node.
        if language == cs.SupportedLanguage.GO and (
            bound := self._go.receiver_method_caller(func_node, func_name, module_qn)
        ):
            caller_qn, container_qn = bound
            filtered = self._filtered_calls_for(
                func_node, language, all_call_nodes, call_starts, owned_func_nodes
            )
            self._ingest_function_calls(
                func_node,
                caller_qn,
                cs.NodeLabel.METHOD,
                module_qn,
                language,
                queries,
                container_qn,
                call_nodes=filtered,
                call_name_cache=call_name_cache,
            )
            return
        # A C++ free function inside a namespace is bound by the definition
        # pass via build_qualified_name (qn `module.ns.fn`); _build_nested...
        # ignores namespace_definition ancestors and would drop the namespace
        # (`module.fn`), dangling the CALLS source. Use the same builder so
        # the qns agree.
        func_qn = (
            cpp_utils.build_qualified_name(func_node, module_qn, func_name)
            if language == cs.SupportedLanguage.CPP
            else self._build_nested_qualified_name(
                func_node, module_qn, func_name, lang_config
            )
        )
        if func_qn:
            filtered = self._filtered_calls_for(
                func_node, language, all_call_nodes, call_starts, owned_func_nodes
            )
            self._ingest_function_calls(
                func_node,
                func_qn,
                cs.NodeLabel.FUNCTION,
                module_qn,
                language,
                queries,
                call_nodes=filtered,
                call_name_cache=call_name_cache,
            )

    def _recorded_caller(
        self, func_node: Node, module_qn: str
    ) -> FunctionLocation | None:
        # The registry membership check guards incremental runs, where an
        # unchanged file's locations were not re-recorded this run.
        loc = self.facts.function_locations.get(function_span_key(module_qn, func_node))
        if loc is None or loc.qualified_name not in self._resolver.function_registry:
            return None
        return loc

    def _get_rust_impl_class_name(self, class_node: Node) -> str | None:
        # Use the same bare-type extraction as the definition pass
        # (rs_utils.extract_impl_target), which strips generic arguments
        # (`Chars<'a>` -> `Chars`). _get_node_name returns the full generic
        # text, so a call inside a generic impl block was attributed to a caller
        # qn bearing the generics (crate.lib.Chars<'a>.go) matching no
        # registered node, silently dropping the CALLS edge.
        return rs_utils.extract_impl_target(class_node)

    def _get_class_name_for_node(
        self, class_node: Node, language: cs.SupportedLanguage
    ) -> str | None:
        if language == cs.SupportedLanguage.RUST and class_node.type == cs.TS_IMPL_ITEM:
            return self._get_rust_impl_class_name(class_node)
        if language == cs.SupportedLanguage.PHP and (
            anonymous_name := php_utils.anonymous_class_name(class_node)
        ):
            # Named as the definition pass names it, so its methods get a
            # class pass and the module pass leaves them alone (issue #2538).
            return anonymous_name
        if language == cs.SupportedLanguage.CPP and (
            name_node := class_node.child_by_field_name(cs.FIELD_NAME)
        ):
            # A specialization is spelled as the definition pass spells it,
            # so a caller qn built from it names the same class (issue #2434).
            return cpp_utils.type_name_text(name_node)
        return self._get_node_name(class_node)

    def _class_method_name(
        self, method_node: Node, language: cs.SupportedLanguage, module_qn: str
    ) -> str | None:
        if language in _C_FAMILY_LANGUAGES:
            method_name = cpp_utils.extract_function_name(method_node)
        elif language == cs.SupportedLanguage.DART:
            # A factory_constructor_signature has no `name` field at all, so
            # the field lookup skipped every factory and its body was never
            # scanned (issue #2482); the definition pass names it this way.
            method_name = dart_utils.get_name(method_node)
        else:
            method_name = self._get_node_name(method_node)
        if method_name or language not in _JS_TS_LANGUAGES:
            return method_name
        if method_name := self._js_ts_arrow_binding_name(method_node):
            return method_name
        # A nameless function expression the definition pass registered
        # under a name (`x: function () {}` in a method's object literal,
        # `this.h = function () {}` in a constructor) has a real node:
        # adopt the record's simple name, as the module pass does. Since
        # #1903 handed every class-scoped function to this pass, skipping it
        # here would leave that node with no outgoing edge.
        recorded = self._recorded_caller(method_node, module_qn)
        if recorded is not None and recorded.is_named:
            return recorded.qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        return None

    def _process_methods_in_class(
        self,
        body_node: Node,
        class_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        all_call_nodes: list[Node] | None = None,
        call_starts: list[int] | None = None,
        call_name_cache: dict[int, str | None] | None = None,
        sorted_func_nodes: list[Node] | None = None,
        func_node_starts: list[int] | None = None,
    ) -> None:
        method_nodes = _body_function_nodes(
            body_node,
            queries[language][cs.QUERY_FUNCTIONS],
            sorted_func_nodes,
            func_node_starts,
        )
        if method_nodes is None:
            return
        lang_config = queries[language][cs.QUERY_CONFIG]
        # Only functions that get their own caller node exclude their calls from
        # the enclosing scope; anonymous arrows (skipped below) must not, so
        # their calls bubble up instead of dropping.
        owned_func_nodes = self._attributable_func_nodes(
            method_nodes, language, module_qn
        )
        for method_node in method_nodes:
            # The body byte-range slice also captures functions of a NESTED
            # class (Outer body contains Inner.run); those belong to the
            # nested class and are processed when it is iterated, so skip any
            # whose nearest enclosing class is not this one (else run also
            # emits as the phantom Outer.run).
            if not self._method_in_class_body(method_node, body_node, lang_config):
                continue
            method_name = self._class_method_name(method_node, language, module_qn)
            if not method_name and language == cs.SupportedLanguage.JULIA:
                # Same name recovery as the module pass, else the inner
                # constructor's body is skipped (issue #1882).
                method_name = self._julia_caller_name(method_node, module_qn)
            if not method_name:
                continue
            # method_nodes includes functions nested inside methods. Build the
            # qn through the enclosing-function chain (Class.method.nested, not
            # the method-dropping Class.nested) and label a nested function
            # FUNCTION, so the CALLS edge joins the real node.
            class_context = class_qn
            if loc := self._recorded_caller(method_node, module_qn):
                # Reuse the definition pass's recorded qn/label; the class
                # walk's structural derivation diverges from it on
                # preprocessor-distorted C++ class bodies and on TS
                # declaration merging (issue #652).
                caller_qn, caller_label = loc.qualified_name, loc.label
                class_context = loc.container_qn or class_qn
            else:
                caller_qn, caller_label = self._class_member_qn_and_label(
                    method_node, class_qn, method_name, lang_config, language
                )
            filtered = (
                self._calls_owned_by(
                    method_node, owned_func_nodes, all_call_nodes, call_starts
                )
                if all_call_nodes is not None and call_starts is not None
                else None
            )
            try:
                self._ingest_function_calls(
                    method_node,
                    caller_qn,
                    caller_label,
                    module_qn,
                    language,
                    queries,
                    class_context,
                    call_nodes=filtered,
                    call_name_cache=call_name_cache,
                )
            except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
                # Contained to this method, as for a free function (#3173).
                self._record_caller_failure(ls.CALLER_CALLS_FAILED, caller_qn, e)

    def _class_member_qn_and_label(
        self,
        func_node: Node,
        class_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
        language: str,
    ) -> tuple[str, str]:
        # Build a class-body function's qn through the chain of enclosing
        # functions up to the class: a direct method is Class.method (METHOD);
        # a function nested in a method is Class.method.nested (FUNCTION).
        path_parts: list[str] = []
        current = func_node.parent
        while current and current.type not in lang_config.class_node_types:
            if (
                current.type in lang_config.function_node_types
                and (name_node := current.child_by_field_name(cs.FIELD_NAME))
                and name_node.text is not None
            ):
                path_parts.append(name_node.text.decode(cs.ENCODING_UTF8))
            current = current.parent
        path_parts.reverse()
        if path_parts:
            joined = cs.SEPARATOR_DOT.join([*path_parts, func_name])
            return f"{class_qn}{cs.SEPARATOR_DOT}{joined}", cs.NodeLabel.FUNCTION
        member = self._java_method_member(func_node, func_name, language)
        return f"{class_qn}{cs.SEPARATOR_DOT}{member}", cs.NodeLabel.METHOD

    def _java_method_member(
        self, func_node: Node, func_name: str, language: str
    ) -> str:
        # A Java Method node is registered with its parameter signature
        # (definition pass: class_qn.name(params)), so the caller endpoint of a
        # CALLS edge must carry the same signature to join that node. Mirrors
        # class_ingest.ingest's method-qn build exactly.
        if language != cs.SupportedLanguage.JAVA:
            return func_name
        info = java_utils.extract_method_info(func_node)
        name = info.get(cs.KEY_NAME) or func_name
        parameters = info.get(cs.KEY_PARAMETERS, [])
        param_sig = f"({','.join(parameters)})" if parameters else cs.EMPTY_PARENS
        return f"{name}{param_sig}"

    @staticmethod
    def _js_this_is_class_instance(call_node: Node) -> bool:
        # True when `this` at this JS/TS call site lexically refers to the
        # enclosing class instance: walk up treating an arrow_function as
        # transparent (it inherits `this`), stopping at the first scope that
        # BINDS `this` -- a method_definition or the class itself (a class-field
        # arrow) means the instance (True); a plain function / generator /
        # function expression rebinds `this` per call, so `this` is NOT the
        # instance (False). Prevents a false self-dispatch edge for `this.M()`
        # inside a plain nested `function(){}` (issue #971).
        current = call_node.parent
        while current is not None:
            node_type = current.type
            if (
                node_type == cs.TS_METHOD_DEFINITION
                or node_type in cs.JS_TS_CLASS_NODES
            ):
                return True
            if node_type in _JS_THIS_REBINDING_FUNC_TYPES:
                return False
            current = current.parent
        return False

    @staticmethod
    def _method_in_class_body(
        method_node: Node, class_body: Node, lang_config: LanguageSpec
    ) -> bool:
        # True when method_node's nearest enclosing class is the one whose
        # body is class_body: walk up, and the first class ancestor's body
        # must be this body (compared by byte span). A method with no enclosing
        # class (out-of-class C++ definition captured elsewhere) returns True
        # so existing handling is unaffected.
        current = method_node.parent
        while current is not None:
            if current.type in lang_config.class_node_types:
                body = current.child_by_field_name(cs.FIELD_BODY)
                return body is not None and (
                    body.start_byte == class_body.start_byte
                    and body.end_byte == class_body.end_byte
                )
            current = current.parent
        return True

    def _calls_owned_by(
        self,
        func_node: Node,
        sibling_func_nodes: list[Node],
        all_call_nodes: list[Node],
        call_starts: list[int],
    ) -> list[Node]:
        # Calls inside func_node MINUS calls owned by functions nested within
        # it, so a call in a nested function is attributed only to the nested
        # function, never also to the enclosing one.
        own = self._filter_calls_in_node(all_call_nodes, call_starts, func_node)
        # The siblings come sorted by (start, end) (`sorted_captures`), so the
        # ones starting inside func_node are one slice. Scanning them all for
        # every function made a class of N methods cost O(N^2) (issue #2453).
        start, end = func_node.start_byte, func_node.end_byte
        lo = bisect_left(sibling_func_nodes, start, key=_start_byte)
        hi = bisect_right(sibling_func_nodes, end, lo=lo, key=_start_byte)
        descendant_bodies = [
            span
            for n in sibling_func_nodes[lo:hi]
            if n is not func_node
            and n.end_byte <= end
            and (span := self._func_body_span(n)) is not None
        ]
        if not descendant_bodies:
            return own
        return [
            call
            for call in own
            if not any(
                body_start <= call.start_byte and call.end_byte <= body_end
                for body_start, body_end in descendant_bodies
            )
        ]

    @staticmethod
    def _func_body_span(func_node: Node) -> tuple[int, int] | None:
        body = func_node.child_by_field_name(cs.FIELD_BODY)
        if body is not None:
            return body.start_byte, body.end_byte
        # No body field: the body is the span between the signature and the
        # trailing `end` (an arrow's body is its last named child).
        if func_node.type == cs.TS_JULIA_FUNCTION_DEFINITION:
            named = func_node.named_children
            if named and named[0].type == cs.TS_JULIA_SIGNATURE:
                end_token = next(
                    (
                        c
                        for c in func_node.children
                        if c.type == cs.TS_JULIA_END_KEYWORD
                    ),
                    None,
                )
                if end_token is not None:
                    return named[0].end_byte, end_token.start_byte
        if func_node.type == cs.TS_JULIA_ARROW_FUNCTION_EXPRESSION:
            # An arrow has two named children (parameter, body); the body is last.
            named = func_node.named_children
            if len(named) >= 2:
                body = named[-1]
                return body.start_byte, body.end_byte
        if (
            func_node.type == cs.TS_JULIA_ASSIGNMENT
            and func_node.named_children
            and julia_utils.function_head_name(func_node) is not None
        ):
            # A concise method `f(x) = ...` (also `f(x)::Int`,
            # `f(x) where T`): the right side is the body.
            body = func_node.named_children[-1]
            return body.start_byte, body.end_byte
        return None

    def _process_calls_in_classes(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        all_call_nodes: list[Node] | None = None,
        call_starts: list[int] | None = None,
        call_name_cache: dict[int, str | None] | None = None,
        combined_captures: dict[str, list] | None = None,
        sorted_func_nodes: list[Node] | None = None,
        func_node_starts: list[int] | None = None,
    ) -> None:
        if combined_captures is not None:
            class_nodes = combined_captures.get(cs.CAPTURE_CLASS, [])
        else:
            query = queries[language][cs.QUERY_CLASSES]
            if not query:
                return
            cursor = QueryCursor(query)
            captures = sorted_captures(cursor, root_node)
            class_nodes = captures.get(cs.CAPTURE_CLASS, [])

        for class_node in class_nodes:
            class_name = self._get_class_name_for_node(class_node, language)
            if not class_name and language in _JS_TS_LANGUAGES:
                # An anonymous class expression (`static Proxy = class {...}`)
                # has no `name`; recover its field/declarator binding name so
                # its methods get a caller pass (issue #970). Matches the qn the
                # definition pass builds via _js_get_name.
                class_name = js_ts_utils.class_binding_name(class_node)
            if not class_name:
                continue
            class_qn = _class_qn_for_calls(
                class_node, module_qn, class_name, language, queries
            )
            if body_node := class_node.child_by_field_name(cs.FIELD_BODY):
                self._process_methods_in_class(
                    body_node,
                    class_qn,
                    module_qn,
                    language,
                    queries,
                    all_call_nodes,
                    call_starts,
                    call_name_cache=call_name_cache,
                    sorted_func_nodes=sorted_func_nodes,
                    func_node_starts=func_node_starts,
                )

    def _span_bindings(
        self,
        caller_node: Node,
        module_qn: str,
        class_context: str | None,
        language: cs.SupportedLanguage,
        local_var_types: dict[str, str] | None,
    ) -> list[tuple[int, int, str, str]]:
        if local_var_types is None:
            return []
        type_inference = self._resolver.type_inference
        if language == cs.SupportedLanguage.RUST:
            return type_inference.collect_rust_span_bindings(
                caller_node, module_qn, class_context
            )
        if language == cs.SupportedLanguage.GO:
            # A `for k, v := range xs` variable is the loop's own, so it holds
            # the element type inside the loop and nowhere else (issue #2483).
            return type_inference.collect_go_span_bindings(
                caller_node, module_qn, local_var_types
            )
        return []

    def _overlay_span_binding(
        self,
        call_name: str,
        call_node: Node,
        local_var_types: dict[str, str] | None,
        span_bindings: list[tuple[int, int, str, str]],
    ) -> dict[str, str] | None:
        # If the call's receiver base is a span-scoped binding (match-arm
        # variant, adaptor-closure parameter) whose range contains this call,
        # overlay that binding's type, shadowing the flat map's value. The
        # innermost (smallest) containing span wins for nested scopes.
        if local_var_types is None or cs.SEPARATOR_DOT not in call_name:
            return local_var_types
        base = call_name.split(cs.SEPARATOR_DOT, 1)[0]
        pos = call_node.start_byte
        best: tuple[int, str] | None = None
        for start, end, name, bound_type in span_bindings:
            if name == base and start <= pos < end:
                span = end - start
                if best is None or span < best[0]:
                    best = (span, bound_type)
        if best is None or local_var_types.get(base) == best[1]:
            return local_var_types
        if best[1] == cs.GO_UNTYPED_BINDING:
            # A Go local nothing can type: the name is untyped here, not the
            # type an outer declaration of it has (issue #2483).
            return {
                name: type_name
                for name, type_name in local_var_types.items()
                if name != base
            }
        return {**local_var_types, base: best[1]}

    def _macro_call_name(self, ident: Node) -> str | None:
        # Rebuild the callee a macro token stream spells, walking the method
        # identifier's preceding siblings, into the name the same call gets
        # outside a macro: `server . run` -> "server.run", `Config :: load` ->
        # "Config::load", `Circle :: new ( 2.0 ) . area` ->
        # "Circle::new(2.0).area". Stopping at `::` dropped `Type::assoc()` and
        # anything chained on it, since a bare method name never binds (issue
        # #2698). A method with no preceding `.` or `::` stays bare.
        if not (method := ident.text.decode(cs.ENCODING_UTF8) if ident.text else None):
            return None
        parts = [method]
        cur = ident.prev_sibling
        while cur is not None and cur.type in cs.RS_MACRO_CHAIN_SEPARATORS:
            if (receiver := self._macro_receiver(cur)) is None:
                break
            text, head = receiver
            parts += [cur.type, text]
            cur = head.prev_sibling
        parts.reverse()
        return "".join(parts)

    @staticmethod
    def _macro_receiver(sep: Node) -> tuple[str, Node] | None:
        # The receiver token(s) just before a `.` / `::` in a macro token
        # stream, as (text, first token): a path segment (`Config`, `Self`,
        # `crate`, `super`) before `::`; before `.`, an identifier or `self`,
        # or a call result `new ( 2.0 )` (callee plus its argument group).
        if (recv := sep.prev_sibling) is None or not recv.text:
            return None
        if sep.type == cs.TS_RS_TOKEN_SCOPE:
            if recv.type not in cs.RS_MACRO_PATH_SEGMENT_TYPES:
                return None
            return recv.text.decode(cs.ENCODING_UTF8), recv
        if recv.type in cs.RS_MACRO_RECEIVER_TYPES:
            return recv.text.decode(cs.ENCODING_UTF8), recv
        if (
            recv.type == cs.TS_RS_TOKEN_TREE
            and recv.child_count > 0
            and recv.children[0].type == cs.CHAR_PAREN_OPEN
            and (callee := recv.prev_sibling) is not None
            and callee.type == cs.TS_IDENTIFIER
            and callee.text
        ):
            return (
                callee.text.decode(cs.ENCODING_UTF8)
                + recv.text.decode(cs.ENCODING_UTF8),
                callee,
            )
        return None

    def _macro_token_call_name(self, ident: Node) -> str | None:
        # Attribute arguments share the token_tree shape (`skip(self)` in
        # `#[instrument(skip(self))]`) but name no function; binding them
        # by bare name invented module-level CALLS (issue #2541).
        if rs_utils.in_attribute_arguments(ident):
            return None
        if not _macro_turbofish_reaches_args(ident):
            return None
        return self._macro_call_name(ident)

    def _get_call_target_name(
        self, call_node: Node, language: cs.SupportedLanguage | None = None
    ) -> str | None:
        # A macro-internal call (Rust `name(args)` inside a token_tree) is
        # captured as the bare identifier node; its text is the callee name. A
        # macro tokenizes `server.run()` into loose tokens (`server . run ( )`),
        # dropping the field_expression, so reconstruct any `<recv>.method`
        # receiver chain from the preceding sibling tokens; else the bare method
        # mis-resolves (`server.run()` in tokio::select! to the same-module free
        # fn `run` instead of Listener.run).
        if call_node.type == cs.TS_IDENTIFIER and call_node.text is not None:
            return self._macro_token_call_name(call_node)
        # A SQL `invocation` names its routine through an unnamed
        # `object_reference` child (no `function` or `name` field), so every
        # SQL call site fell through nameless and was dropped (issue #2449).
        # Normalized like the definition, so `TAX(x)` reaches `tax`.
        if language == cs.SupportedLanguage.SQL:
            return sql_object_reference_name(call_node)
        # A Dart call node is a selector/cascade_section holding the
        # argument_part; the target name lives in the PRECEDING sibling
        # chain, not inside the node.
        if language == cs.SupportedLanguage.DART:
            return dart_utils.call_name(call_node)
        if language == cs.SupportedLanguage.JULIA:
            # A definition's own head IS a call_expression; drop heads
            # here (no query negation to exclude them at capture time).
            if julia_utils.is_signature_head(call_node):
                return None
            return julia_utils.call_name(call_node)
        if func_child := call_node.child_by_field_name(cs.TS_FIELD_FUNCTION):
            match func_child.type:
                case cs.TS_PARENTHESIZED_EXPRESSION:
                    return self._get_iife_target_name(func_child)
                case cs.TS_FUNCTION_EXPRESSION | cs.TS_GENERATOR_FUNCTION if (
                    language in _JS_TS_LANGUAGES
                ):
                    # An unparenthesised direct IIFE (`const x = function ()
                    # {...}()`) has the function itself as the callee. A NAMED
                    # one registers under its own name (possibly a duplicate
                    # variant), so only the span-resolved path may emit its
                    # edge; a positional name here is for the anonymous form.
                    if self._js_iife_callee_name(func_child):
                        return None
                    return (
                        f"{cs.PREFIX_IIFE_DIRECT}"
                        f"{func_child.start_point[0]}_{func_child.start_point[1]}"
                    )
            if (
                name := self._function_field_call_name(func_child, language)
            ) is not None:
                return name
        return self._node_shaped_call_name(call_node, language)

    @staticmethod
    def _prefixed_generic_callee(func_child: Node) -> Node | None:
        # `await client.call<number>(x)` and `!isKind<string>(v)` parse as a
        # call of `await client.call` / `!isKind`: the grammar binds the
        # operator tighter than a call with type arguments, and the call is
        # what the operator applies to (issue #2930). Only that misparse puts
        # an operator in the `function` field, so the call must carry type
        # arguments; the operand is the callee, possibly under another
        # operator (`!await f<T>()`).
        call = func_child.parent
        if (
            call is None
            or call.child_by_field_name(cs.TS_JS_FIELD_TYPE_ARGUMENTS) is None
        ):
            return None
        operand: Node | None = func_child
        while operand is not None and operand.type in cs.TS_JS_PREFIXED_CALLEES:
            operand = (
                operand.child_by_field_name(cs.TS_JS_FIELD_ARGUMENT)
                if operand.type == cs.TS_JS_UNARY_EXPRESSION
                else next(reversed(operand.named_children), None)
            )
        return operand

    def _prefixed_generic_call_name(
        self, func_child: Node, language: cs.SupportedLanguage | None
    ) -> str | None:
        operand = self._prefixed_generic_callee(func_child)
        if operand is None:
            return None
        return self._function_field_call_name(operand, language)

    def _function_field_call_name(
        self, func_child: Node, language: cs.SupportedLanguage | None
    ) -> str | None:
        # The callee name a call's `function` field spells; None means no case
        # named it and the caller falls back to the call node's own shape.
        match func_child.type:
            case cs.TS_SELECTOR_EXPRESSION if language == cs.SupportedLanguage.GO and (
                asserted_call := go_calls.asserted_receiver_call_name(func_child)
            ):
                return asserted_call
            case (
                cs.TS_IDENTIFIER
                | cs.TS_ATTRIBUTE
                | cs.TS_MEMBER_EXPRESSION
                | cs.CppNodeType.QUALIFIED_IDENTIFIER
                | cs.TS_SCOPED_IDENTIFIER
                | cs.TS_SELECTOR_EXPRESSION
                | cs.TS_PHP_NAME
            ):
                if func_child.text is not None:
                    return decode_node_text(func_child.text)
            case cs.TS_GENERIC_FUNCTION:
                # turbofish: unwrap to the underlying callee identifier
                inner = func_child.child_by_field_name(cs.TS_FIELD_FUNCTION)
                if inner and inner.text:
                    return decode_node_text(inner.text)
            case cs.TS_RS_FIELD_EXPRESSION if language == cs.SupportedLanguage.RUST:
                # Rust member call `a.b.method()`: use the full dotted receiver
                # chain as the call name so the resolver can map the receiver to
                # its inferred type (`self.shutdown.is_shutdown`, `cmd.apply`). A
                # chain containing a call (`x.f().g`) is left to the chained-call
                # path; a paren-free chain ends at the bare-method trie fallback
                # when the receiver type is unknown.
                if (text := func_child.text) is not None:
                    return decode_node_text(text)
            case cs.TS_CPP_FIELD_EXPRESSION:
                return cpp_calls.field_call_name(func_child)
            case cs.TS_CSHARP_GENERIC_NAME if language == cs.SupportedLanguage.CSHARP:
                # Bare generic call `Handle<TException>(...)`: the callee
                # name is the identifier without its type arguments
                # (methods register generic-free). Leaving the `<...>` on
                # yielded no call name, so the call site vanished from the
                # graph (Polly's parameterless HandleInner overload
                # delegating to its Func sibling).
                if func_child.text is not None:
                    full = decode_node_text(func_child.text)
                    return full.split(cs.CHAR_ANGLE_OPEN, 1)[0]
            case cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION if (
                language == cs.SupportedLanguage.CSHARP
            ):
                # C# member call `recv.Method(...)`: emit the `recv.Method`
                # chain so the resolver can type `recv` and bind the method on
                # it; an unknown-type receiver falls back to the bare
                # method-name trie. `name` is the method, `expression` the
                # receiver.
                return csharp_calls.member_access_call_name(func_child)
            case cs.TS_CSHARP_CONDITIONAL_ACCESS_EXPRESSION if (
                language == cs.SupportedLanguage.CSHARP
            ):
                # C# conditional call `recv?.Method(...)`: the method name
                # lives on the member_binding child. Emit the same
                # `recv.Method` chain as the unconditional form so the
                # resolver (or its exact Roslyn call fact) can bind it.
                return csharp_calls.conditional_access_call_name(func_child)
            case callee_type if (
                callee_type in cs.TS_JS_PREFIXED_CALLEES
                and language in _JS_TS_LANGUAGES
            ):
                return self._prefixed_generic_call_name(func_child, language)
            case cs.TS_CALL_EXPRESSION if language in _JS_TS_LANGUAGES:
                # A bound function that is itself invoked (`fn.bind(ctx)()`)
                # has a nested `fn.bind(ctx)` call_expression as its callee, so
                # no earlier case names it. Resolve to fn so the invocation
                # links to fn, not the Function.prototype builtin (issue:
                # JoinAttribute.relation's local getValue called as
                # getValue.bind(this)()). Guard on _unwrap_bound_function so a
                # plain call-of-a-call (`getFactory()()`) stays unnamed, then
                # peel to a fixpoint for chained binds / interleaved casts.
                return self._bound_function_call_name(func_child)
        return None

    def _bound_function_call_name(self, func_child: Node) -> str | None:
        if self._unwrap_bound_function(func_child) is not None:
            peeled = self._peel_bound_callable(func_child)
            if peeled.text is not None:
                return decode_node_text(peeled.text)
        return None

    def _node_shaped_call_name(
        self, call_node: Node, language: cs.SupportedLanguage | None
    ) -> str | None:
        # The callee name of a call node with no `function` field (or whose
        # `function` field named nothing): construction, operator, method
        # invocation and macro shapes, then the node's own `name` field.
        match call_node.type:
            case cs.TS_NEW_EXPRESSION if language == cs.SupportedLanguage.CPP:
                # C++ `new Foo(...)` names the class via the `type` field (no
                # `function` field), so it fell through nameless and heap
                # construction emitted nothing (issue #896, the singleton
                # `new WindowClassRegistrar()`). Returning the type name routes
                # it through the normal resolve loop: the class gets
                # INSTANTIATES and its constructor CALLS. The type reduces
                # STRUCTURALLY to its dotted class path (`new Foo<int>()` ->
                # Foo, `new Outer<int>::Inner()` -> Outer.Inner; a textual cut
                # at `<` would drop the nested suffix); a primitive type
                # reduces to nothing and stays silent.
                type_node = call_node.child_by_field_name(cs.FIELD_TYPE)
                if type_node is not None:
                    return cpp_utils.new_expression_class_path(type_node)
            case cs.TS_CSHARP_IMPLICIT_OBJECT_CREATION_EXPRESSION if (
                language == cs.SupportedLanguage.CSHARP
            ):
                # C# 9 target-typed `new()` has no `type` field; the
                # constructed type is named by the enclosing declaration
                # (issue #773).
                return csharp_calls.target_typed_new_name(call_node)
        if (name := self._construction_call_name(call_node, language)) is not None:
            return name
        if (name := self._operator_call_name(call_node, language)) is not None:
            return name

        if name_node := call_node.child_by_field_name(cs.FIELD_NAME):
            if name_node.text is not None:
                return decode_node_text(name_node.text)

        return None

    @staticmethod
    def _construction_call_name(
        call_node: Node, language: cs.SupportedLanguage | None
    ) -> str | None:
        match call_node.type:
            case cs.TS_SCALA_INSTANCE_EXPRESSION if (
                language == cs.SupportedLanguage.SCALA
            ):
                # Scala `new C(...)` holds its type as an unnamed child, so
                # it fell through nameless and emitted nothing (issue #2450).
                return scala_utils.instance_type_name(call_node)
            case cs.TS_NEW_EXPRESSION if language in _JS_TS_LANGUAGES:
                # JS/TS `new Foo(...)` names the class via the `constructor` field
                # (no `function` field). Returning the constructor name routes
                # construction through the normal resolve loop: a first-party class
                # gets INSTANTIATES (+ CALLS to its constructor), and an inline
                # callback argument (`new CancelablePromise(cb)`) is referenced so
                # it is not reported as dead.
                ctor = js_ts_utils.constructor_of(call_node)
                if ctor is not None and ctor.text is not None:
                    return decode_node_text(ctor.text)
            case cs.TS_OBJECT_CREATION_EXPRESSION if language in (
                cs.SupportedLanguage.JAVA,
                cs.SupportedLanguage.CSHARP,
            ):
                # Java/C# `new Foo(...)` names the class via the `type` field (no
                # `function` field). Returning the base type name routes construction
                # through the normal resolve loop: the class gets INSTANTIATES and its
                # constructor(s) get CALLS. Strip generic args (`new ArrayList<T>()`
                # -> ArrayList); a scoped name (`Outer.Inner`) is left for the resolver.
                type_node = call_node.child_by_field_name(cs.FIELD_TYPE)
                if type_node is not None and type_node.text is not None:
                    return decode_node_text(type_node.text).split(
                        cs.CHAR_ANGLE_OPEN, 1
                    )[0]
            case cs.TS_PHP_OBJECT_CREATION_EXPRESSION if (
                language == cs.SupportedLanguage.PHP
            ):
                # PHP `new Box(...)` writes the class as an unfielded child, so
                # no case above named it and construction emitted nothing
                # (issue #2466). The class's own simple name, after `use`
                # aliases, is what a file added later would define; the
                # resolver reads the full namespace path off the node.
                if path := php_class_names.new_expression_class_path(call_node):
                    return path[-1]
        return None

    def _operator_call_name(
        self, call_node: Node, language: cs.SupportedLanguage | None
    ) -> str | None:
        match call_node.type:
            case (
                cs.TS_CPP_BINARY_EXPRESSION
                | cs.TS_CPP_UNARY_EXPRESSION
                | cs.TS_CPP_UPDATE_EXPRESSION
            ):
                operator_node = call_node.child_by_field_name(cs.FIELD_OPERATOR)
                if operator_node and operator_node.text:
                    operator_text = decode_node_text(operator_node.text)
                    return cpp_utils.convert_operator_symbol_to_name(operator_text)
            case cs.TS_METHOD_INVOCATION:
                return self._method_invocation_call_name(call_node)
            # Scala infix operator call (`a ~> b`, `xs map f`): the callee is the
            # `operator` field's method name. tree-sitter has no `function` field
            # here, so it is unreachable above. Gated to Scala since the node type
            # string is Scala-specific and the guard keeps other languages inert.
            # Infix is unambiguously a method call; a bare `field_expression`
            # (`obj.done` with no parens) is deliberately NOT named here because
            # Scala's uniform access makes a nullary call and a `val` read
            # syntactically identical, so resolving it by simple name would turn a
            # same-named field read into a spurious CALLS edge.
            case cs.TS_SCALA_INFIX_EXPRESSION if language == cs.SupportedLanguage.SCALA:
                operator_node = call_node.child_by_field_name(cs.FIELD_OPERATOR)
                if operator_node and operator_node.text:
                    return decode_node_text(operator_node.text)
            # Rust `square!(3)`: the callee lives in the `macro` field (no
            # `function`/`name` field), so the invocation was captured as a
            # call but dropped nameless here; unresolvable even now that
            # macro_rules! definitions register as Function nodes.
            case cs.TS_RS_MACRO_INVOCATION if language == cs.SupportedLanguage.RUST:
                macro_node = call_node.child_by_field_name(cs.FIELD_MACRO)
                if macro_node is not None and macro_node.text is not None:
                    return decode_node_text(macro_node.text)
        return None

    @staticmethod
    def _method_invocation_call_name(call_node: Node) -> str | None:
        object_node = call_node.child_by_field_name(cs.FIELD_OBJECT)
        name_node = call_node.child_by_field_name(cs.FIELD_NAME)
        if name_node and name_node.text:
            method_name = decode_node_text(name_node.text)
            if not object_node or not object_node.text:
                return method_name
            object_text = decode_node_text(object_node.text)
            return f"{object_text}{cs.SEPARATOR_DOT}{method_name}"
        return None

    def _get_iife_target_name(self, parenthesized_expr: Node) -> str | None:
        for child in parenthesized_expr.children:
            match child.type:
                case cs.TS_FUNCTION_EXPRESSION | cs.TS_GENERATOR_FUNCTION:
                    # A named IIFE registers under its own name, not the
                    # positional iife_* name; its edge comes from the
                    # span-resolved path, never a bare-name lookup (a
                    # same-named sibling would capture it).
                    if self._js_iife_callee_name(child):
                        return None
                    return f"{cs.IIFE_FUNC_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
                case cs.TS_ARROW_FUNCTION:
                    return f"{cs.IIFE_ARROW_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
        return None

    @staticmethod
    def _js_iife_callee_name(func_node: Node) -> str | None:
        name_node = func_node.child_by_field_name(cs.FIELD_NAME)
        if name_node is not None and name_node.text is not None:
            return name_node.text.decode(cs.ENCODING_UTF8)
        return None

    def _js_named_iife_callee(
        self, call_node: Node, module_qn: str
    ) -> FunctionLocation | None:
        # A NAMED IIFE (`(function build () {...})()`, `function build ()
        # {...}()`) registers under its own name, minted as a duplicate
        # variant when a sibling shares it; a sequence IIFE
        # (`(0, function () {...})()`, the indirect-this idiom) registers as
        # an anonymous node. Neither the positional iife_* names nor
        # bare-name resolution can reach them safely, so resolve the callee
        # NODE by its own source span to the exact minted qn. The plain
        # anonymous parenthesised/direct forms keep their positional names
        # and are excluded here.
        func_child = call_node.child_by_field_name(cs.FIELD_FUNCTION)
        if func_child is None:
            return None
        target, paren_peels, other_peels = self._peel_iife_callee_wrappers(func_child)
        if target.type not in (cs.TS_FUNCTION_EXPRESSION, cs.TS_GENERATOR_FUNCTION):
            return None
        # The plain anonymous forms (bare callee, or exactly one paren) keep
        # their positional iife_direct_/iife_func_ names; everything else
        # registers under its own name or as an anonymous node and resolves
        # only by span.
        if (
            other_peels == 0
            and paren_peels <= 1
            and not self._js_iife_callee_name(target)
        ):
            return None
        return self._recorded_caller(target, module_qn)

    @staticmethod
    def _peel_iife_callee_wrappers(func_child: Node) -> tuple[Node, int, int]:
        # Peel to a FIXPOINT (wrappers nest: `((fn))()`, `(0, (fn))()`, cast
        # forms), the same discipline as _peel_bound_callable. Parens and
        # casts are value-transparent; the comma operator yields its LAST
        # operand. Anything else (ternary, logicals) has no single static
        # callee and stops the peel.
        target = func_child
        paren_peels = 0
        other_peels = 0
        while (inner := _iife_wrapper_inner(target)) is not None:
            if target.type == cs.TS_PARENTHESIZED_EXPRESSION:
                paren_peels += 1
            else:
                other_peels += 1
            target = inner
        return target, paren_peels, other_peels

    def _js_branch_callee_functions(
        self, call_node: Node, module_qn: str
    ) -> list[FunctionLocation]:
        # A BRANCH-valued callee (`(c ? f : g)()`, `(f || g)()`) may invoke
        # ANY of its inline function operands, and no single node can be the
        # span-resolved callee. Reference each operand so a function whose
        # only use is such a call (and everything only it calls) stays live:
        # a sound liveness over-approximation (issue #1002's family).
        func_child = call_node.child_by_field_name(cs.FIELD_FUNCTION)
        if func_child is None:
            return []
        locations: list[FunctionLocation] = []
        branched = False
        stack: list[Node] = [func_child]
        while stack:
            node = stack.pop()
            node_type = node.type
            if (
                node_type
                in (
                    cs.TS_FUNCTION_EXPRESSION,
                    cs.TS_GENERATOR_FUNCTION,
                    cs.TS_ARROW_FUNCTION,
                )
                and (loc := self._recorded_caller(node, module_qn)) is not None
            ):
                locations.append(loc)
            elif self._js_branch_operands(node, stack):
                branched = True
        return locations if branched else []

    @staticmethod
    def _js_wrapper_operands(node: Node, stack: list[Node]) -> bool:
        # Value-transparent wrappers: push what they yield; True when handled.
        node_type = node.type
        if (
            node_type == cs.TS_PARENTHESIZED_EXPRESSION
            or node_type in cs.TS_CAST_WRAPPER_TYPES
        ):
            stack.extend(
                child for child in node.named_children if child.type != cs.TS_COMMENT
            )
            return True
        if node_type == cs.TS_JS_SEQUENCE_EXPRESSION:
            if node.named_children:
                stack.append(node.named_children[-1])
            return True
        return False

    def _js_branch_operands(self, node: Node, stack: list[Node]) -> bool:
        # Push a branch expression's candidate operands; True only for a
        # genuine BRANCH (ternary / short-circuit), which is what licenses
        # the candidate-set references.
        if self._js_wrapper_operands(node, stack):
            return False
        node_type = node.type
        if node_type == cs.TS_JS_TERNARY_EXPRESSION:
            for field in (cs.FIELD_CONSEQUENCE, cs.FIELD_ALTERNATIVE):
                if (child := node.child_by_field_name(field)) is not None:
                    stack.append(child)
            return True
        if node_type == cs.TS_BINARY_EXPRESSION and self._js_is_logical_binary(node):
            for field in (cs.FIELD_LEFT, cs.FIELD_RIGHT):
                if (child := node.child_by_field_name(field)) is not None:
                    stack.append(child)
            return True
        return False

    @_site_scoped
    def _ingest_function_calls(
        self,
        caller_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None = None,
        call_nodes: list[Node] | None = None,
        call_name_cache: dict[int, str | None] | None = None,
    ) -> None:
        local_var_types, span_bindings, cpp_spans = self._caller_local_types(
            caller_node, caller_qn, module_qn, language, class_context
        )
        if language == cs.SupportedLanguage.PYTHON:
            self._record_python_shadowed_imports(caller_node, caller_qn, module_qn)
        elif language == cs.SupportedLanguage.GO:
            self._go.record_local_names(caller_node, caller_qn, caller_type)

        caller_spec = (caller_type, cs.KEY_QUALIFIED_NAME, caller_qn)

        self._io_processor.process_io_for_caller(
            caller_node, caller_spec, module_qn, language
        )
        self._rpc_exposure.process_caller(caller_node, module_qn, language)
        self._flow_processor.process_flow_for_caller(
            caller_node,
            caller_spec,
            caller_qn,
            module_qn,
            language,
            class_context,
            local_var_types,
        )

        caller_params = self._record_caller_flow_params(
            caller_node,
            caller_qn,
            module_qn,
            language,
            queries,
            class_context,
            local_var_types,
        )
        self._ingest_caller_property_reads(
            caller_node,
            caller_spec,
            caller_qn,
            module_qn,
            language,
            queries,
            class_context,
            local_var_types,
        )
        self._ingest_caller_references(
            caller_node,
            caller_spec,
            caller_qn,
            caller_type,
            module_qn,
            language,
            queries,
            class_context,
            local_var_types,
        )

        if call_nodes is None:
            calls_query = queries[language].get(cs.QUERY_CALLS)
            if not calls_query:
                return
            cursor = QueryCursor(calls_query)
            captures = sorted_captures(cursor, caller_node)
            call_nodes = captures.get(cs.CAPTURE_CALL, [])

        if not call_nodes:
            return

        ctx = self._call_scan_context(
            caller_node,
            caller_qn,
            caller_spec,
            module_qn,
            language,
            queries,
            class_context,
            call_name_cache,
            local_var_types,
            span_bindings,
            caller_params,
        )
        self._ingest_call_nodes(ctx, call_nodes, cpp_spans)
        # Edges the later passes emit (decorators, property reads, operator
        # dispatch) are exact bindings; they must not carry the verdict the
        # last call node of this loop left behind.
        self._resolution = cs.EdgeResolution.EXACT

    def _caller_local_types(
        self,
        caller_node: Node,
        caller_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        class_context: str | None,
    ) -> tuple[
        dict[str, str] | None,
        list[tuple[int, int, str, str]],
        list[tuple[int, int, str, str]],
    ]:
        """The caller's local types, its span-scoped bindings and its C++
        declaration windows."""
        if language not in _TYPED_LANGUAGES:
            return None, [], []
        try:
            local_var_types = (
                self._resolver.type_inference.build_local_variable_type_map(
                    caller_node, module_qn, language, class_context
                )
            )
            cpp_spans: list[tuple[int, int, str, str]] = []
            if language == cs.SupportedLanguage.CPP and local_var_types:
                cpp_spans = self._cpp.bind_local_types(
                    caller_node, module_qn, local_var_types
                )
            # Rust match arms and iterator-adaptor closures both reuse one
            # binding name for different types at different byte ranges (`cmd`
            # per arm; `|x|` per collection); the flat map keeps only one. These
            # span-scoped bindings let each call resolve against the binding
            # containing it.
            span_bindings = self._span_bindings(
                caller_node, module_qn, class_context, language, local_var_types
            )
        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            # A walk that still fails on some shape costs this caller its local
            # types, not the file its calls: they resolve untyped (#3173).
            self._record_caller_failure(ls.CALLER_LOCAL_TYPES_FAILED, caller_qn, e)
            return {}, [], []
        return local_var_types, span_bindings, cpp_spans

    def _record_caller_failure(
        self, message: str, caller: str, error: Exception
    ) -> None:
        path = self._call_file
        rel_path = None
        if path is not None:
            self.failed_call_files.add(path)
            # POSIX on every OS, like the other paths the logs name.
            rel_path = cached_relative_path(path, self.repo_path).as_posix()
        logger.error(message, caller=caller, path=rel_path, error=error)

    def _ingest_call_nodes(
        self,
        ctx: _CallScanContext,
        call_nodes: list[Node],
        cpp_spans: list[tuple[int, int, str, str]],
    ) -> None:
        # A C++ name whose declarations disagree (a block local `B b` hiding
        # a `::B b` field) is typed per call by the declaration in force
        # there, for every use the call makes of it: a receiver, a functor
        # called (`b(1)`) or handed over (`keep(b)`), an operator operand.
        flat_types = ctx.local_var_types
        for call_node in call_nodes:
            if cpp_spans and flat_types is not None:
                ctx.local_var_types = cpp_calls.types_at(
                    flat_types, cpp_spans, call_node.start_byte
                )
            self._ingest_call_node(ctx, call_node)

    def _record_python_shadowed_imports(
        self, caller_node: Node, caller_qn: str, module_qn: str
    ) -> None:
        # Names this caller binds that also name an import: the resolver
        # refuses to read the import map for them (issue #1907).
        python = self._resolver.type_inference.python_type_inference
        shadowed = python.ast_analyzer.shadowed_import_names(caller_node, module_qn)
        if shadowed:
            self._resolver.python_shadowed_imports[caller_qn] = shadowed
        else:
            self._resolver.python_shadowed_imports.pop(caller_qn, None)
        # Every name a function binds itself: a bare use of one never resolves
        # to the module function or method it shadows (issue #2666). Only a
        # function or lambda body: in a class body `alias = run` does read the
        # class's own `run`, and a module's bindings are its definitions.
        local = (
            python.ast_analyzer.locally_bound_names(caller_node, module_qn)
            if caller_node.type in _PY_LOCAL_SCOPE_CALLERS
            else frozenset()
        )
        if local:
            self._resolver.python_local_names[caller_qn] = local
        else:
            self._resolver.python_local_names.pop(caller_qn, None)
        self._record_python_name_typed_params(caller_node, caller_qn, module_qn)

    def _record_python_name_typed_params(
        self, caller_node: Node, caller_qn: str, module_qn: str
    ) -> None:
        # Parameters whose class only their name spells: a call through one
        # is labelled heuristic (issue #2608).
        python = self._resolver.type_inference.python_type_inference
        guessed = python.variable_analyzer.name_typed_parameters(caller_node, module_qn)
        if guessed:
            self._resolver.python_name_typed_params[caller_qn] = frozenset(guessed)
        else:
            self._resolver.python_name_typed_params.pop(caller_qn, None)

    def _record_caller_flow_params(
        self,
        caller_node: Node,
        caller_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> frozenset[str]:
        caller_params: frozenset[str] = frozenset()
        ordered_params: list[str] | None = None
        if language == cs.SupportedLanguage.PYTHON:
            ordered_params = python_parameter_names(caller_node)
        elif language == cs.SupportedLanguage.GO:
            ordered_params = go_parameter_names(caller_node)
        elif language in _JS_TS_LANGUAGES:
            ordered_params = js_ts_parameter_names(caller_node)
        elif language == cs.SupportedLanguage.CPP:
            ordered_params = cpp_parameter_names(caller_node)
        if ordered_params is not None:
            # Every flow-traced language records its callable params and the
            # closures it returns, so a later `x = factory(); x(cb)` alias call can
            # flow cb into the returned closure regardless of source language.
            self._flow_param_names[caller_qn] = ordered_params
            caller_params = frozenset(ordered_params)
            self._record_param_invocations(caller_node, caller_qn, language)
            self._collect_returned_callables(
                caller_node,
                caller_qn,
                module_qn,
                local_var_types,
                class_context,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
            )
        return caller_params

    def _ingest_caller_property_reads(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> None:
        # Runs independently of call_nodes: a getter access is an attribute, not
        # a call, so callers that read a property but make no other call must
        # still reach this pass before the early return below.
        if language == cs.SupportedLanguage.PYTHON and (
            prop_names := self._resolver.function_registry.property_names()
        ):
            self._ingest_property_accesses(
                caller_node,
                caller_spec,
                caller_qn,
                module_qn,
                local_var_types,
                class_context,
                queries[language][cs.QUERY_CONFIG],
                prop_names,
            )

        # Same need as the Python pass above, C# shape: a property getter
        # access is a member_access_expression (usually in RECEIVER position),
        # never an invocation, so callers that only READ a property emit no
        # edge to it and dead-code flags it (Polly's Context.WrappedDictionary,
        # ResiliencePipeline<T>.Pipeline).
        if language == cs.SupportedLanguage.CSHARP and (
            csharp_prop_names := self._resolver.function_registry.property_names()
        ):
            self._csharp.ingest_property_reads(
                caller_node,
                caller_spec,
                caller_qn,
                module_qn,
                local_var_types,
                queries[language][cs.QUERY_CONFIG],
                csharp_prop_names,
            )

        # Same need again, Dart shape (issue #869): a getter access is a bare
        # identifier or a member selector, never an invocation, so callers
        # that only READ a getter emit nothing and dead-code flags it
        # (wonderous' _enableVideo/startYr family).
        if language == cs.SupportedLanguage.DART and (
            dart_prop_names := self._resolver.function_registry.property_names()
        ):
            self._dart.ingest_getter_reads(
                caller_node,
                caller_spec,
                caller_qn,
                module_qn,
                local_var_types,
                class_context,
                queries[language][cs.QUERY_CONFIG],
                dart_prop_names,
            )

        # Same need again, JS/TS shape (issue #984): a getter read is a
        # member_expression (`this.relation`, `loadMap.mainLoadMapItem`), never
        # an invocation. The return/assignment reference passes only inspect the
        # direct value, so a getter read nested in a ternary, member chain, `if`
        # condition, or binary expression emitted nothing and dead-code flagged
        # it (TypeORM's JoinAttribute.relation, LoadMap.mainLoadMapItem).
        if language in _JS_TS_LANGUAGES and (
            js_ts_prop_names := self._resolver.function_registry.property_names()
        ):
            self._ingest_js_ts_getter_reads(
                caller_node,
                caller_spec,
                module_qn,
                local_var_types,
                class_context,
                queries[language][cs.QUERY_CONFIG],
                js_ts_prop_names,
                caller_qn=caller_qn,
            )

    def _ingest_caller_references(
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
        # Operator syntax (k in r, r[k], r[k]=v, len(r)) dispatches to dunder
        # methods; emit those edges when the operand is a first-party type.
        if language == cs.SupportedLanguage.PYTHON:
            self._ingest_operator_dispatch_calls(
                caller_node, caller_spec, module_qn, local_var_types
            )
        if (
            language == cs.SupportedLanguage.PYTHON
            or language in _JS_TS_LANGUAGES
            or language == cs.SupportedLanguage.GO
        ):
            self._ingest_assignment_function_references(
                caller_node,
                caller_spec,
                module_qn,
                local_var_types,
                class_context,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
                caller_qn,
                language=language,
            )
        if language in _JS_TS_LANGUAGES:
            self._ingest_jsx_component_references(
                caller_node,
                caller_spec,
                module_qn,
                local_var_types,
                class_context,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
                caller_qn,
            )
        if language in _JS_TS_LANGUAGES or language == cs.SupportedLanguage.PYTHON:
            # A DEFAULT PARAMETER value naming a function (`useStore(api,
            # selector = identity as any)`, zustand; Python's
            # `def run(self, conv=_via_default)`, issue #2838) references it:
            # the default is invoked through the parameter when the caller
            # omits the argument, never by a visible call.
            self._ingest_default_param_references(
                caller_node,
                caller_spec,
                module_qn,
                local_var_types,
                class_context,
                caller_qn,
            )
        # A function handed back bare (`return defaultUsageFunc`) is a first-class
        # value invoked by whoever receives it, never by a visible call; Go leans
        # on this for factories/getters (cobra's getUsageFunc), Python on returned
        # bound methods (django GEOSCoordSeq `return self._get_point_2d`).
        # Reference it so the returned function is reachable. These languages share
        # the `return_statement` node type, and the emit path resolves bare names
        # and self-attributes alike.
        if language in _JS_TS_LANGUAGES or language in (
            cs.SupportedLanguage.GO,
            cs.SupportedLanguage.PYTHON,
        ):
            self._ingest_returned_function_references(
                caller_node,
                caller_spec,
                module_qn,
                local_var_types,
                class_context,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
                caller_qn,
                language,
            )
        # Dispatch-table handler references, for every flow language. Module-scope
        # literals are scanned explicitly in process_calls_in_file (before the
        # no-calls early return), so only nested scopes here.
        if (
            language == cs.SupportedLanguage.PYTHON or language in _JS_TS_LANGUAGES
        ) and caller_type != cs.NodeLabel.MODULE:
            self._ingest_collection_function_references(
                caller_node,
                caller_spec,
                module_qn,
                local_var_types,
                class_context,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
                language,
            )
        if language == cs.SupportedLanguage.GO and caller_type != cs.NodeLabel.MODULE:
            self._go.ingest_composite_function_references(
                caller_node,
                caller_spec,
                module_qn,
                local_var_types,
                class_context,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
            )
            self._go.ingest_composite_literal_instantiations(
                caller_node,
                caller_spec,
                module_qn,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
            )
        if language == cs.SupportedLanguage.C and caller_type != cs.NodeLabel.MODULE:
            self._ingest_c_function_value_references(
                caller_node,
                caller_spec,
                module_qn,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
            )
        if language == cs.SupportedLanguage.CPP:
            self._cpp.ingest_braced_return_instantiations(
                caller_node, caller_spec, caller_qn, module_qn
            )
            self._cpp.ingest_member_init_ctor_calls(caller_node, caller_spec, module_qn)
            self._cpp.ingest_implicit_base_lifecycle_calls(
                caller_node, caller_spec, caller_qn, module_qn
            )
            self._cpp.ingest_declaration_ctor_calls(caller_node, caller_spec, module_qn)
        if language == cs.SupportedLanguage.DART and class_context:
            self._dart.ingest_constructor_delegations(
                caller_node, caller_spec, class_context, module_qn
            )

    def _call_scan_context(
        self,
        caller_node: Node,
        caller_qn: str,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        call_name_cache: dict[int, str | None] | None,
        local_var_types: dict[str, str] | None,
        span_bindings: list[tuple[int, int, str, str]],
        caller_params: frozenset[str],
    ) -> _CallScanContext:
        is_java = language == cs.SupportedLanguage.JAVA
        is_csharp = language == cs.SupportedLanguage.CSHARP
        is_js_ts = language in _JS_TS_LANGUAGES
        is_cpp = language == cs.SupportedLanguage.CPP
        # Template type-parameter names in scope at this caller (`template<typename
        # SAX>` -> {"SAX"}): a receiver typed to one has no concrete type here, so the
        # dispatch fan-out treats it like an untyped receiver rather than an external.
        cpp_template_params = (
            self._resolver.type_inference.cpp_type_inference.collect_template_param_names(
                caller_node
            )
            if is_cpp
            else frozenset()
        )
        resolver = self._resolver
        resolve_func = self._resolve_noting
        resolve_builtin = resolver.resolve_builtin_call if is_js_ts else None
        resolve_cpp_op = resolver.resolve_cpp_operator_call if is_cpp else None
        is_python = language == cs.SupportedLanguage.PYTHON
        # Languages with interprocedural callable-parameter flow enabled: a
        # callback passed to a first-party function whose parameter is invoked
        # (directly or in a nested closure) is traced to the concrete callback.
        is_flow_lang = (
            is_python
            or language == cs.SupportedLanguage.GO
            or language in _JS_TS_LANGUAGES
            or is_cpp
        )
        # C# and Dart get the argument-REFERENCE half only (a method group or
        # tear-off passed as an argument keeps its target reachable, Polly's
        # EmptyHandler family, Flutter's `onPressed: _handleTap` callbacks)
        # without the interprocedural callable-param flow, which is untuned
        # for them.
        is_arg_ref_lang = is_flow_lang or language in _ARG_REF_ONLY_LANGUAGES
        # A method-group pass is not an invocation: C# records it as
        # REFERENCES everywhere (CALLS here put 282 phantom edges into the
        # Polly call graph, retrieval precision 1.0 -> 0.92); flow languages
        # keep their historical CALLS form for external/builtin callees.
        arg_ref_rel = (
            cs.RelationshipType.CALLS
            if is_flow_lang
            else cs.RelationshipType.REFERENCES
        )
        return _CallScanContext(
            caller_node=caller_node,
            caller_qn=caller_qn,
            caller_spec=caller_spec,
            module_qn=module_qn,
            language=language,
            queries=queries,
            class_context=class_context,
            call_name_cache=call_name_cache,
            local_var_types=local_var_types,
            span_bindings=span_bindings,
            caller_params=caller_params,
            is_java=is_java,
            is_csharp=is_csharp,
            is_js_ts=is_js_ts,
            is_cpp=is_cpp,
            is_python=is_python,
            is_flow_lang=is_flow_lang,
            is_arg_ref_lang=is_arg_ref_lang,
            cpp_template_params=cpp_template_params,
            resolve_func=resolve_func,
            resolve_builtin=resolve_builtin,
            resolve_cpp_op=resolve_cpp_op,
            ensure_rel=self._emit_rel,
            arg_ref_rel=arg_ref_rel,
        )

    def _noted[R: tuple[str, str] | None](self, resolved: R) -> R:
        """`resolved`, with its verdict noted as the call node's."""
        self._verdict = _verdict_of(resolved)
        return resolved

    def _resolve_noting(self, *args: Any, **kwargs: Any) -> Resolved | None:
        """The resolver's answer for the call node being scanned, its verdict
        noted for the edge the scan emits."""
        return self._noted(self._resolver.resolve_function_call(*args, **kwargs))

    def _ingest_call_node(self, ctx: _CallScanContext, call_node: Node) -> None:
        if _exceeds_receiver_chain_cap(call_node):
            # Left unresolved: resolving it would re-read the whole chain.
            return
        self._site_node = call_node
        self._resolution = cs.EdgeResolution.EXACT
        # A callee bound by a language frontend (Jedi, the Java and C#
        # engines, the Go frontend, a typed C++ operator) never passes
        # through `_noted`, which is where a resolver answer sets the
        # verdict; without this reset such an edge would inherit the
        # label the previous call node left behind (issue #1526).
        self._verdict = cs.EdgeResolution.EXACT
        if ctx.is_java and call_node.type == cs.TS_JAVA_METHOD_REFERENCE:
            self._ingest_java_method_reference(ctx, call_node)
            return
        call_name = self._scan_call_name(ctx, call_node)
        if (
            ctx.language == cs.SupportedLanguage.SCALA
            and call_node.type == cs.TS_SCALA_FIELD_EXPRESSION
        ):
            self._ingest_scala_selection(ctx, call_node)
            return
        # A declared dispatcher (`callSp('usp_x')`) names its real callee in
        # a string, which no parser resolves as a call: without this the
        # edge stops at the dispatcher and the routine looks unreachable.
        # The dispatcher's own edge still emits below; this only ADDS the
        # one the syntax hides.
        if self._string_call_specs and call_name:
            self._emit_string_call_edges(ctx, call_node, call_name)
        # `enter_state(n)` on a functor local, `Cmp{}(a, b)` on a temporary:
        # a call on an object of class type runs that type's operator(),
        # which no name lookup of the callee text reaches (issue #2555).
        if (
            ctx.is_cpp
            and call_node.type == cs.TS_CPP_CALL_EXPRESSION
            and (callee := call_node.child_by_field_name(cs.TS_FIELD_FUNCTION))
            and (
                functor := self._cpp.functor_operator(
                    callee, ctx.module_qn, ctx.local_var_types
                )
            )
        ):
            self._ingest_resolved_call(
                ctx, call_node, cs.CPP_OPERATOR_CALL_NAME, functor
            )
            return
        # An inline function ARGUMENT is handed to the callee regardless of
        # whether the callee resolves: an external/param callee
        # (`create((set) => ...)` passing `set((state) => ...)`, zustand) or a
        # cast-wrapped one (`;(set as NamedSet)((state) => reducer(...))`)
        # still consumes it. Reference each inline arg from this scope, BEFORE
        # the no-name bail (a cast-wrapped callee yields no call name).
        # Registry-guarded and idempotent with the callable-params path.
        if ctx.is_js_ts and call_node.type == cs.TS_CALL_EXPRESSION:
            call_name = self._ingest_js_call_site(ctx, call_node, call_name)
        if not call_name:
            # A callee that is itself a call (`wraps(view_func)(_view_wrapper)`)
            # or otherwise yields no name still consumes its arguments through
            # whatever callable it produced; reference first-party functions
            # passed to it or dead-code flags every django-style view
            # decorator wrapper. REFERENCES (not arg_ref_rel) is
            # deliberate and predates the C# split: with no callee name
            # there is no invocation to assert, for ANY language.
            if ctx.is_arg_ref_lang:
                self._ingest_argument_function_references(
                    call_node,
                    ctx.caller_spec,
                    ctx.module_qn,
                    ctx.local_var_types,
                    ctx.class_context,
                    self._resolver.resolve_function_call,
                    ctx.ensure_rel,
                    ctx.caller_qn,
                    cs.RelationshipType.REFERENCES,
                    ctx.language,
                )
            return
        if ctx.language == cs.SupportedLanguage.SQL:
            self._ingest_sql_routine_call(ctx, call_name)
            return
        self._ingest_named_call(ctx, call_node, call_name)

    def _ingest_sql_routine_call(self, ctx: _CallScanContext, call_name: str) -> None:
        self._link_sql_routine(ctx.module_qn, ctx.caller_spec, call_name)

    def _link_sql_routine(
        self,
        module_qn: str,
        caller_spec: tuple[str, str, str],
        call_name: str,
        site: PropertyDict | None = None,
    ) -> None:
        # A SQL routine call has no receiver, import or type to resolve
        # through. The generic resolver reads `billing.fee` as a receiver and
        # a method, drops the receiver and binds `fee` by bare name, so a
        # schema-qualified call landed on whichever schema's `fee` came first.
        #
        # The name is recorded as a waiter even when it resolves: with no
        # imports its target set stays open, and a routine of that name added
        # in ANOTHER file (a second schema for `fee`, a second `billing.fee`
        # overload) is one more target a clean index links. No edge leads from
        # that file to this caller, so only the waiter list (issue #1568) can
        # send it back for a re-parse. The whole normalized name is recorded:
        # `billing.fee` waits on that routine only, never on `audit.fee`.
        self._note_unresolved(module_qn, call_name)
        targets = self._resolver.sql_routine_targets(call_name)
        if not targets:
            # A builtin (`count`, `now`) or a routine no indexed file defines.
            return
        if len({qn_markers.natural_qn(qn) for qn in targets}) > 1:
            # Several routines answer to the name: other schemas for an
            # unqualified one, other files defining a qualified one.
            self._resolution = cs.EdgeResolution.HEURISTIC
        elif len(targets) > 1:
            # One routine with overloads the call site's types would choose.
            self._resolution = cs.EdgeResolution.OVERLOAD
        for target_qn in targets:
            self._emit_rel(
                caller_spec,
                cs.RelationshipType.CALLS,
                (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, target_qn),
                site,
            )

    def _scan_call_name(self, ctx: _CallScanContext, call_node: Node) -> str | None:
        node_id = id(call_node)
        if ctx.call_name_cache is not None and node_id in ctx.call_name_cache:
            call_name = ctx.call_name_cache[node_id]
        else:
            call_name = self._get_call_target_name(call_node, ctx.language)
            if (
                call_name is None
                and ctx.is_csharp
                and call_node.type == cs.TS_CSHARP_IMPLICIT_OBJECT_CREATION_EXPRESSION
            ):
                call_name = self._csharp.argument_target_name(ctx, call_node)
            if ctx.call_name_cache is not None:
                ctx.call_name_cache[node_id] = call_name
        return call_name

    def _emit_string_call_edges(
        self, ctx: _CallScanContext, call_node: Node, call_name: str
    ) -> None:
        target_name = string_call_target(call_node, call_name, self._string_call_specs)
        if target_name:
            # A qualified target (`app.usp_x`) matches through the
            # dotted-suffix scan (#513) and so reaches ONLY routines
            # carrying that schema in their FQN; an unqualified one
            # goes through the last-segment index and legitimately
            # fans onto every schema's candidate, mirroring what a
            # runtime search_path could pick.
            for target_qn in self._resolver.function_registry.find_ending_with(
                target_name
            ):
                # find_ending_with indexes classes too; only a
                # callable takes the edge, under its OWN label — a
                # mislabeled edge is a phantom the database drops.
                target_type = self._resolver.function_registry.get(target_qn)
                if target_type not in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
                    continue
                target_label = (
                    cs.NodeLabel.METHOD
                    if target_type == cs.NodeLabel.METHOD
                    else cs.NodeLabel.FUNCTION
                )
                ctx.ensure_rel(
                    ctx.caller_spec,
                    cs.RelationshipType.CALLS,
                    (target_label, cs.KEY_QUALIFIED_NAME, target_qn),
                )

    def _ingest_scala_selection(self, ctx: _CallScanContext, node: Node) -> None:
        # `c.size` with no parentheses: a parameterless method call, or a
        # field read spelled the same way. It binds only through a receiver
        # whose class is known (issue #2450); the selection that is a call's
        # callee is left to the call node, which names it with its args.
        selection = scala_utils.selection(node)
        if selection is None:
            return
        if ctx.scala_bindings is None:
            ctx.scala_bindings = scala_utils.bindings(ctx.caller_node)
        receiver, member = selection
        binding = scala_utils.binding_at(ctx.scala_bindings, receiver, node.start_byte)
        # A local's binding types it; an unbound name is an object named
        # outright. A local of unknown type is neither, so it binds nothing.
        receiver_type = binding.type_name if binding is not None else receiver
        if receiver_type is None:
            return
        callee_info = self._resolver.resolve_scala_selection(
            receiver_type, member, ctx.module_qn, ctx.class_context, node.start_byte
        )
        if callee_info is None:
            return
        call_name = f"{receiver}{cs.SEPARATOR_DOT}{member}"
        self._ingest_resolved_call(ctx, node, call_name, callee_info)

    def _emit_scala_auxiliary_ctor_calls(
        self, ctx: _CallScanContext, class_variants: list[str]
    ) -> None:
        # A Scala class's primary constructor is its body, which has no node
        # of its own; an auxiliary `def this(...)` is a method, and any of
        # them may be the one `new C(...)` runs.
        registry = self._resolver.function_registry
        for class_variant in class_variants:
            ctor_qn = (
                f"{class_variant}{cs.SEPARATOR_DOT}{cs.SCALA_AUXILIARY_CONSTRUCTOR}"
            )
            if registry.get(ctor_qn) != cs.NodeLabel.METHOD:
                continue
            for variant in registry.variants(ctor_qn):
                ctx.ensure_rel(
                    ctx.caller_spec,
                    cs.RelationshipType.CALLS,
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, variant),
                )

    def _ingest_js_call_site(
        self, ctx: _CallScanContext, call_node: Node, call_name: str | None
    ) -> str | None:
        self._ingest_inline_call_arg_references(
            call_node, ctx.caller_spec, ctx.ensure_rel, ctx.caller_qn, ctx.module_qn
        )
        # `fn.call(this, ...)` / `fn.apply(this, ...)` invokes fn, but
        # the callee names Function.prototype.call, so the resolve
        # below fails (or, cast-wrapped, never even names the site)
        # and a function invoked ONLY this way in statement position
        # reports dead (issue #988; value positions link through the
        # bound-callable peel). The receiver is resolved by LEXICAL
        # BINDING, never by name lookup: only a binding that IS a
        # function (a hoisted declaration or a declarator holding an
        # inline function) emits, resolved by its own source span.
        # Everything else about the site (name resolution, builtin
        # handling, argument references) proceeds unchanged below.
        if (
            loc := self._js_fn_call_receiver_binding(call_node, ctx.module_qn)
        ) is not None:
            ctx.ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.CALLS,
                (loc.label, cs.KEY_QUALIFIED_NAME, loc.qualified_name),
            )
        # A NAMED IIFE yields no call name (a bare-name lookup would
        # bind a same-named sibling); its edge is emitted here from
        # the callee node's own span.
        if (
            iife_loc := self._js_named_iife_callee(call_node, ctx.module_qn)
        ) is not None:
            ctx.ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.CALLS,
                (iife_loc.label, cs.KEY_QUALIFIED_NAME, iife_loc.qualified_name),
            )
        for branch_loc in self._js_branch_callee_functions(call_node, ctx.module_qn):
            ctx.ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.REFERENCES,
                (branch_loc.label, cs.KEY_QUALIFIED_NAME, branch_loc.qualified_name),
            )
        # `recv[kSym].method(...)`: the callee text contains brackets
        # and resolves nowhere useful; the symbol index carries the
        # edges, and the generic path (with its phantom-prone
        # bare-name fallback) is suppressed for the recognised shape.
        if (
            sym_targets := self._js_symbol_member_targets(call_node, ctx.module_qn)
        ) is not None:
            targets, unique = sym_targets
            sym_rel = (
                cs.RelationshipType.CALLS if unique else cs.RelationshipType.REFERENCES
            )
            for sym_label, sym_qn_target in targets:
                for variant in self._resolver.function_registry.variants(sym_qn_target):
                    ctx.ensure_rel(
                        ctx.caller_spec,
                        sym_rel,
                        (sym_label, cs.KEY_QUALIFIED_NAME, variant),
                    )
            call_name = None
        return call_name

    def _ingest_named_call(
        self, ctx: _CallScanContext, call_node: Node, call_name: str
    ) -> None:
        call_var_types = ctx.local_var_types
        if ctx.span_bindings:
            call_var_types = self._overlay_span_binding(
                call_name, call_node, ctx.local_var_types, ctx.span_bindings
            )
        if (
            call_var_types
            and (rebinding := self._receiver_rebinding_at(ctx, call_node, call_name))
            and not rebinding.sometimes
        ):
            # No `self = Other()` can run before this call, so the method-wide
            # type of `self` does not apply to it (issue #2620).
            receiver = call_name.partition(cs.SEPARATOR_DOT)[0]
            call_var_types = {
                name: var_type
                for name, var_type in call_var_types.items()
                if name != receiver
            }

        if ctx.is_cpp:
            self._cpp.emit_template_dispatch(
                ctx.caller_spec,
                call_name,
                call_var_types,
                ctx.cpp_template_params,
                ctx.ensure_rel,
            )

        if (
            ctx.is_cpp
            and call_node.type in cpp_calls.OPERATOR_EXPRESSION_TYPES
            and call_name.startswith(cs.OPERATOR_PREFIX)
        ):
            # An operator expression calls an overload only through an
            # operand of class type; on built-in or unknown operands it is
            # the language's own operation. Resolving its synthesized name
            # like a call bound `int + int` to any project operator+, and,
            # in the class defining one, its own `x + o.x` field arithmetic
            # to itself (issue #2554). A miss notes no unresolved name:
            # most C++ projects define some operator_plus, so every file
            # using `+` would re-parse whenever another one is added.
            if callee_info := self._cpp.operator_callee(
                ctx, call_node, call_name, call_var_types
            ):
                self._ingest_resolved_call(ctx, call_node, call_name, callee_info)
            return
        callee_info = self._resolve_call_callee(
            ctx, call_node, call_name, call_var_types
        )
        callee_info = self._fallback_callee(ctx, call_node, call_name, callee_info)

        if not callee_info and ctx.is_python and cs.SEPARATOR_DOT in call_name:
            # recv.field(...) where field is a callable struct field:
            # resolve to the functions bound to it at construction sites.
            self._ingest_callable_field_calls(
                call_name, ctx.caller_spec, ctx.local_var_types, ctx.ensure_rel
            )

        if ctx.is_python and call_name.rsplit(cs.SEPARATOR_DOT, 1)[-1] in (
            cs.HIGHER_ORDER_BUILTINS
        ):
            # sorted(xs, key=f) and friends invoke f synchronously in this
            # frame, so the trace attributes the call to the enclosing fn.
            self._ingest_higher_order_builtin_calls(
                call_node,
                ctx.caller_spec,
                ctx.module_qn,
                ctx.local_var_types,
                ctx.class_context,
                self._resolver.resolve_function_call,
                ctx.ensure_rel,
                ctx.caller_qn,
            )

        if not callee_info:
            self._ingest_unresolved_call(ctx, call_node, call_name)
            return
        self._ingest_resolved_call(ctx, call_node, call_name, callee_info)

    def _resolve_call_callee(
        self,
        ctx: _CallScanContext,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        if (
            ctx.language == cs.SupportedLanguage.SCALA
            and (scala := self._resolve_scala_callee(ctx, call_node, call_name)).decided
        ):
            return scala.callee
        if ctx.is_java and call_node.type == cs.TS_METHOD_INVOCATION:
            return self._resolve_java_callee(ctx, call_node)
        if ctx.is_csharp and call_node.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
            return self._csharp.resolve_callee(
                ctx,
                call_node,
                call_name,
                call_var_types,
                ctx.ensure_rel,
                call_node.type in _OBJECT_CREATION_NODE_TYPES,
            )
        if (
            ctx.language == cs.SupportedLanguage.PYTHON
            and call_node.type == cs.TS_PY_CALL
        ):
            return self._resolve_python_callee(
                ctx, call_node, call_name, call_var_types
            )
        if (
            ctx.language == cs.SupportedLanguage.GO
            and call_node.type == cs.TS_GO_CALL_EXPRESSION
        ):
            return self._go.resolve_callee(
                call_node,
                call_name,
                call_var_types,
                ctx.module_qn,
                ctx.class_context,
                ctx.caller_qn,
            )
        if (
            ctx.language == cs.SupportedLanguage.PHP
            and (
                php := self._resolve_php_class_callee(ctx, call_node, call_name)
            ).decided
        ):
            return php.callee
        callee_info = ctx.resolve_func(
            call_name,
            ctx.module_qn,
            call_var_types,
            ctx.class_context,
            ctx.caller_qn,
            ctx.language,
            call_point=call_node.start_byte,
            # A Java/C#/Scala `new X(...)` names a type, never a method.
            constructing=ctx.language in _NEW_CONSTRUCTS_LANGUAGES
            and call_node.type in _OBJECT_CREATION_NODE_TYPES,
        )
        return self._judge_resolved_callee(ctx, call_node, call_name, callee_info)

    def _judge_resolved_callee(
        self,
        ctx: _CallScanContext,
        call_node: Node,
        call_name: str,
        callee_info: tuple[str, str] | None,
    ) -> tuple[str, str] | None:
        # What the name lookup found, checked against what the call site says.
        if ctx.is_csharp and call_node.type in _OBJECT_CREATION_NODE_TYPES:
            return self._csharp.construction_by_arity(call_node, call_name, callee_info)
        if ctx.is_js_ts and callee_info is not None:
            return self._judge_js_member_pick(ctx, call_node, callee_info)
        return callee_info

    def _judge_js_member_pick(
        self, ctx: _CallScanContext, call_node: Node, callee_info: tuple[str, str]
    ) -> tuple[str, str] | None:
        # The resolver binds `recv.m()` on a receiver it could not type to the
        # one visible class defining `m`, and calls that a heuristic (issue
        # #2609). What the receiver is declared as here decides further: a
        # first-party type owning `m` (itself or a base) makes the binding
        # the one the type implies, so it is exact; a type the project does
        # not define (`Map`, a library class, `any`) cannot be that class, so
        # there is no edge, as for a local `new Map()`. Anything else stays
        # the heuristic it is.
        if self._verdict != cs.EdgeResolution.HEURISTIC:
            return callee_info
        func = call_node.child_by_field_name(cs.FIELD_FUNCTION)
        if func is not None and func.type in cs.TS_JS_PREFIXED_CALLEES:
            # `await recv.m<T>(x)`: the receiver sits under the operator.
            func = self._prefixed_generic_callee(func)
        if func is None or func.type != cs.TS_MEMBER_EXPRESSION:
            return callee_info
        declared = self._js_receiver_declaration(
            ctx, func.child_by_field_name(cs.FIELD_OBJECT)
        )
        if declared is None:
            return callee_info
        if declared.foreign:
            return None
        owner_qn = callee_info[1].rpartition(cs.SEPARATOR_DOT)[0]
        if owner_qn in self._resolver._mro(
            self._resolver._follow_reexports(declared.qn)
        ):
            self._verdict = cs.EdgeResolution.EXACT
        return callee_info

    def _js_receiver_declaration(
        self, ctx: _CallScanContext, receiver: Node | None
    ) -> _ReceiverDeclaration | None:
        # Wrappers that keep the operand's type are looked through; an `as`
        # cast asserts its own type, which is then the declaration.
        while receiver is not None and receiver.type in (
            cs.TS_PARENTHESIZED_EXPRESSION,
            cs.TS_NON_NULL_EXPRESSION,
            cs.TS_SATISFIES_EXPRESSION,
        ):
            receiver = receiver.named_child(0)
        if receiver is None:
            return None
        if receiver.type == cs.TS_AS_EXPRESSION:
            cast = receiver.named_children[-1] if receiver.named_children else None
            return self._ts_type_declaration(cast, ctx.module_qn) if cast else None
        if receiver.type == cs.TS_THIS:
            if ctx.class_context and self._js_instance_this(receiver):
                return _ReceiverDeclaration(ctx.class_context, foreign=False)
            return None
        if receiver.type == cs.TS_IDENTIFIER:
            return self._js_identifier_declaration(ctx, receiver)
        if receiver.type == cs.TS_MEMBER_EXPRESSION:
            return self._js_this_field_declaration(ctx, receiver)
        return None

    def _js_identifier_declaration(
        self, ctx: _CallScanContext, ident: Node
    ) -> _ReceiverDeclaration | None:
        name = safe_decode_text(ident)
        if not name:
            return None
        intro = self._js_innermost_introduction(ident, name)
        if intro is None:
            return None
        kind, value = intro
        if kind == js_bindings.DECL_PARAM and value is not None:
            return self._js_param_declaration(ctx, value, name)
        if kind == js_bindings.DECL_PLAIN and value is not None:
            declarator = value.parent
            annotation = (
                declarator.child_by_field_name(cs.FIELD_TYPE)
                if declarator is not None
                and declarator.type == cs.TS_VARIABLE_DECLARATOR
                else None
            )
            if annotation is not None:
                return self._ts_annotation_declaration(annotation, ctx.module_qn)
        class_qn = self._js_introduction_class_qn(
            kind, value, name, ctx.module_qn, depth=0
        )
        return _ReceiverDeclaration(class_qn, foreign=False) if class_qn else None

    def _js_param_declaration(
        self, ctx: _CallScanContext, callable_node: Node, name: str
    ) -> _ReceiverDeclaration | None:
        annotation = self._js_param_annotation(callable_node, name)
        if annotation is not None:
            return self._ts_annotation_declaration(annotation, ctx.module_qn)
        jsdoc_type = self._jsdoc_param_type(callable_node, name, ctx.language)
        if jsdoc_type is None:
            return None
        return self._jsdoc_type_declaration(jsdoc_type, callable_node, ctx.module_qn)

    def _js_param_annotation(self, callable_node: Node, name: str) -> Node | None:
        for param in self._js_callable_params(callable_node):
            if (
                param.type in (cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER)
                and self._js_param_binds_name(param, name)
                and (annotation := param.child_by_field_name(cs.FIELD_TYPE)) is not None
            ):
                return annotation
        return None

    @staticmethod
    def _js_callable_params(callable_node: Node) -> list[Node]:
        # `(a, b) => ...` lists them under `parameters`; a bare `a => ...`
        # holds its one identifier under `parameter`.
        params: list[Node] = []
        for field in (cs.FIELD_PARAMETERS, cs.FIELD_PARAMETER):
            node = callable_node.child_by_field_name(field)
            if node is None:
                continue
            if node.type == cs.TS_IDENTIFIER:
                params.append(node)
            else:
                params.extend(node.named_children)
        return params

    @staticmethod
    def _js_param_binds_name(param: Node, name: str) -> bool:
        pattern = param.child_by_field_name(cs.TS_FIELD_PATTERN)
        return (
            pattern is not None
            and pattern.type == cs.TS_IDENTIFIER
            and safe_decode_text(pattern) == name
        )

    @staticmethod
    def _jsdoc_param_type(
        callable_node: Node, name: str, language: cs.SupportedLanguage
    ) -> str | None:
        doc = extract_definition_docstring(callable_node, language)
        if not doc:
            return None
        templates = {
            template.strip()
            for names in re.findall(cs.JSDOC_TEMPLATE_TAG_PATTERN, doc)
            for template in names.split(cs.CHAR_COMMA)
        }
        for type_text, param_name in re.findall(cs.JSDOC_PARAM_TAG_PATTERN, doc):
            if param_name != name:
                continue
            # An `@template T` names no type the project or the platform
            # defines, so `{T}` is left unread rather than taken as foreign.
            bare = type_text.strip().strip(cs.JSDOC_TYPE_MODIFIER_CHARS)
            return None if bare in templates else type_text.strip()
        return None

    def _jsdoc_type_declaration(
        self, type_text: str, anchor: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        # Only the plain forms are read: `{A}`, `{?A}`, `{A=}`, `{ns.A}`, and
        # `{*}` for any. A union, a record or a function type stays unread.
        if type_text in cs.JSDOC_ANY_TYPES:
            return _ReceiverDeclaration(type_text, foreign=True)
        bare = type_text.strip(cs.JSDOC_TYPE_MODIFIER_CHARS)
        if not re.fullmatch(cs.JSDOC_TYPE_NAME_PATTERN, bare):
            return None
        return self._ts_type_name_declaration(bare, anchor, module_qn)

    def _js_this_field_declaration(
        self, ctx: _CallScanContext, member: Node
    ) -> _ReceiverDeclaration | None:
        # `this.f` on an instance: the field's declared type when it has one;
        # otherwise the class every value instance code gives it (initialiser,
        # `this.f = ...` in the class's own methods) constructs, when they all
        # agree. A static member's `this` is the class, whose `f` is another
        # property altogether.
        obj = member.child_by_field_name(cs.FIELD_OBJECT)
        prop = member.child_by_field_name(cs.FIELD_PROPERTY)
        if (
            not self._js_instance_this(obj)
            or prop is None
            or not (field := safe_decode_text(prop))
        ):
            return None
        class_node = member.parent
        while class_node is not None and class_node.type not in _JS_TS_ANY_CLASS_NODES:
            class_node = class_node.parent
        if class_node is None:
            return None
        annotation, values = self._js_declared_field_parts(
            class_node, field, ctx.language
        )
        if annotation is not None:
            return self._ts_annotation_declaration(annotation, ctx.module_qn)
        writes = self._js_this_field_writes(class_node, field)
        if writes is None:
            return None
        return self._js_agreed_class(values + writes, ctx.module_qn)

    @staticmethod
    def _js_declared_field_parts(
        class_node: Node, field: str, language: cs.SupportedLanguage
    ) -> tuple[Node | None, list[Node]]:
        # The instance field's type annotation, which is declared truth and
        # ends the search, else the initialisers its declarations give it.
        initials: list[Node] = []
        for declared in declared_fields(class_node, language):
            if declared.name != field or declared.is_static:
                continue
            annotation = declared.node.child_by_field_name(cs.FIELD_TYPE)
            if annotation is not None:
                return annotation, initials
            if (
                initial := declared.node.child_by_field_name(cs.FIELD_VALUE)
            ) is not None:
                initials.append(initial)
        return None, initials

    def _js_agreed_class(
        self, values: list[Node], module_qn: str
    ) -> _ReceiverDeclaration | None:
        # The one class every value constructs; none, or any disagreement or
        # untypable value, proves nothing.
        class_qns = {
            self._js_value_type_class_qn(value, module_qn, depth=0) for value in values
        }
        if len(class_qns) != 1 or (class_qn := class_qns.pop()) is None:
            return None
        return _ReceiverDeclaration(class_qn, foreign=False)

    def _js_this_field_writes(self, class_node: Node, field: str) -> list[Node] | None:
        # Every value instance code in the class body assigns to `this.f`, or
        # None when some write to it gives no single value to type: a
        # computed key (`this[k] = ...`, which may be `f`), a destructuring
        # target, a compound operator or an update.
        body = class_node.child_by_field_name(cs.FIELD_BODY)
        values: list[Node] = []
        stack: list[Node] = list(body.children) if body is not None else []
        while stack:
            node = stack.pop()
            if node.type in _JS_TS_ANY_CLASS_NODES:
                continue
            if node.type in _JS_FIELD_WRITE_TYPES:
                written = self._js_field_write_values(node, field)
                if written is None:
                    return None
                values.extend(written)
            stack.extend(node.children)
        return values

    def _js_field_write_values(self, node: Node, field: str) -> list[Node] | None:
        # What one write expression assigns to `this.f`: nothing when it
        # writes elsewhere, its right-hand side for a plain `=`, and None
        # when it may write `f` with no value to type (see the caller).
        target = node.child_by_field_name(
            cs.TS_JS_FIELD_ARGUMENT
            if node.type == cs.TS_JS_UPDATE_EXPRESSION
            else cs.FIELD_LEFT
        )
        writes = self._js_writes_this_field(target, field)
        if writes is None:
            return None
        if not writes:
            return []
        value = node.child_by_field_name(cs.TS_FIELD_RIGHT)
        if node.type != cs.TS_JS_ASSIGNMENT_EXPRESSION or value is None:
            return None
        return [value]

    def _js_writes_this_field(self, target: Node | None, field: str) -> bool | None:
        # Whether an assignment target writes the instance field `field`:
        # None when it may or may not (a computed key, or the field inside a
        # destructuring pattern, whose value is not the right-hand side).
        if target is None:
            return False
        if target.type in (cs.TS_MEMBER_EXPRESSION, cs.TS_SUBSCRIPT_EXPRESSION):
            return self._js_this_target_field(target, field)
        stack = [target]
        while stack:
            node = stack.pop()
            if node.type in (cs.TS_MEMBER_EXPRESSION, cs.TS_SUBSCRIPT_EXPRESSION):
                if self._js_this_target_field(node, field) is not False:
                    return None
                continue
            stack.extend(node.named_children)
        return False

    def _js_this_target_field(self, target: Node, field: str) -> bool | None:
        if not self._js_instance_this(target.child_by_field_name(cs.FIELD_OBJECT)):
            return False
        if target.type == cs.TS_MEMBER_EXPRESSION:
            prop = target.child_by_field_name(cs.FIELD_PROPERTY)
            return prop is not None and safe_decode_text(prop) == field
        index = target.child_by_field_name(cs.TS_FIELD_INDEX)
        if index is None or index.type != cs.TS_STRING:
            return None
        parts = index.named_children
        if any(part.type != cs.TS_STRING_FRAGMENT for part in parts):
            return None
        return "".join(safe_decode_text(part) or "" for part in parts) == field

    def _js_instance_this(self, node: Node | None) -> bool:
        # `this` naming an instance of the enclosing class: in an instance
        # method or instance field (arrows inherit it), never in a static
        # method, static field or static block, where it is the class.
        if (
            node is None
            or node.type != cs.TS_THIS
            or not self._js_this_binds_to_enclosing_class(node)
        ):
            return False
        current = node.parent
        while current is not None:
            if current.type == cs.TS_CLASS_STATIC_BLOCK:
                return False
            if current.type in _JS_CLASS_MEMBER_TYPES:
                return not any(child.type == cs.TS_STATIC for child in current.children)
            current = current.parent
        return True

    def _ts_annotation_declaration(
        self, annotation: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        inner = next(iter(annotation.named_children), None)
        return self._ts_type_declaration(inner, module_qn) if inner else None

    def _ts_type_declaration(
        self, node: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        node_type = node.type
        if node_type in (cs.TS_PREDEFINED_TYPE, cs.TS_ARRAY_TYPE, cs.TS_TUPLE_TYPE):
            # `any`, `unknown`, `string`, `A[]`: nothing the project defines.
            return _ReceiverDeclaration(safe_decode_text(node) or "", foreign=True)
        if node_type == cs.TS_PARENTHESIZED_TYPE:
            inner = next(iter(node.named_children), None)
            return self._ts_type_declaration(inner, module_qn) if inner else None
        if node_type == cs.TS_UNION_TYPE:
            return self._ts_union_declaration(node, module_qn)
        if node_type == cs.TS_TYPE_IDENTIFIER:
            name = safe_decode_text(node)
            return (
                self._ts_type_name_declaration(name, node, module_qn) if name else None
            )
        if node_type == cs.TS_GENERIC_TYPE:
            return self._ts_generic_declaration(node, module_qn)
        if node_type == cs.TS_NESTED_TYPE_IDENTIFIER:
            name = safe_decode_text(node)
            return (
                self._ts_type_name_declaration(name, node, module_qn) if name else None
            )
        return None

    def _ts_union_declaration(
        self, node: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        # `A | null` declares A. A wider union declares nothing usable unless
        # no member is first-party (`string | number`).
        members = [
            child
            for child in node.named_children
            if (safe_decode_text(child) or "") not in cs.TS_NULLISH_TYPE_TEXTS
        ]
        if len(members) == 1:
            return self._ts_type_declaration(members[0], module_qn)
        declared = [self._ts_type_declaration(member, module_qn) for member in members]
        if declared and all(d is not None and d.foreign for d in declared):
            return _ReceiverDeclaration(safe_decode_text(node) or "", foreign=True)
        return None

    def _ts_generic_declaration(
        self, node: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        name = safe_decode_text(name_node) if name_node is not None else None
        if not name:
            return None
        declared = self._ts_type_name_declaration(name, node, module_qn)
        # TypeScript's own utility types are no value type of their own and
        # are read through. A name the project defines or a package exports
        # (`Wrapper<Cache>`, or an imported `Readonly`) is that type, whatever
        # its arguments, so only a name resolving to nothing is a utility.
        if declared is not None or name in cs.TS_UNSETTLED_UTILITY_TYPES:
            return declared
        if name in cs.TS_MEMBER_PRESERVING_UTILITY_TYPES:
            # `Readonly<A>` and friends keep A's members: A is the declaration.
            arguments = node.child_by_field_name(cs.TS_FIELD_TYPE_ARGUMENTS)
            first = next(iter(arguments.named_children), None) if arguments else None
            return self._ts_type_declaration(first, module_qn) if first else None
        return None

    def _ts_type_name_declaration(
        self, name: str, anchor: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        # A type name is first-party when an enclosing scope declares it, an
        # import supplies it from the project, or the module registers it.
        # A type parameter or a nested declaration is first-party too but has
        # no qn to compare, so it declares nothing usable. Foreign takes
        # proof: an import from a package, or a platform global (`Map`,
        # `URLSearchParams`, `HTMLElement`). Any other name may be a type
        # this pass cannot see (a JSDoc `@typedef`, a class-level `@template`,
        # an ambient declaration), so it declares nothing usable either.
        head, _, rest = name.partition(cs.SEPARATOR_DOT)
        top_level = self._ts_lexical_type(anchor, head)
        if top_level is not None:
            return (
                _ReceiverDeclaration(
                    f"{module_qn}{cs.SEPARATOR_DOT}{name}", foreign=False
                )
                if top_level
                else None
            )
        import_map = self._resolver.import_processor.import_mapping.get(module_qn) or {}
        if head in import_map:
            if self._resolver._is_external_import(head, module_qn):
                return _ReceiverDeclaration(name, foreign=True)
            head_qn = self._resolver._resolve_class_qn_from_type(
                head, import_map, module_qn
            )
            qn = f"{head_qn}{cs.SEPARATOR_DOT}{rest}" if rest else head_qn
            return _ReceiverDeclaration(qn, foreign=False) if qn else None
        local_qn = f"{module_qn}{cs.SEPARATOR_DOT}{name}"
        if local_qn in self._resolver.function_registry:
            return _ReceiverDeclaration(local_qn, foreign=False)
        if name in cs.JS_TS_GLOBAL_RECEIVER_TYPES:
            return _ReceiverDeclaration(name, foreign=True)
        return None

    @staticmethod
    def _ts_lexical_type(anchor: Node, name: str) -> bool | None:
        # Whether an enclosing scope declares the type `name`: True when the
        # module itself does, False when only a nested scope (a function, a
        # namespace) or a type-parameter list does, None when none does.
        current = anchor.parent
        while current is not None:
            params = current.child_by_field_name(cs.TS_FIELD_TYPE_PARAMETERS)
            if params is not None and any(
                (param_name := param.child_by_field_name(cs.FIELD_NAME)) is not None
                and safe_decode_text(param_name) == name
                for param in params.named_children
            ):
                return False
            for child in current.named_children:
                declaration = (
                    child.child_by_field_name(cs.TS_DECLARATION)
                    if child.type == cs.TS_EXPORT_STATEMENT
                    else child
                )
                if (
                    declaration is not None
                    and declaration.type in cs.TS_TYPE_DECLARATION_NODES
                    and (decl_name := declaration.child_by_field_name(cs.FIELD_NAME))
                    is not None
                    and safe_decode_text(decl_name) == name
                ):
                    return current.parent is None
            current = current.parent
        return None

    def _resolve_scala_callee(
        self, ctx: _CallScanContext, call_node: Node, call_name: str
    ) -> _ScalaCallee:
        # Named the way the compiler looks it up: the imports in scope at
        # the call, the file, its package's other files. A path-qualified
        # `new shop.Cart()` names no import, so only the package index
        # reaches it.
        return _ScalaCallee(
            *self._resolver.resolve_scala_callee(
                call_name,
                ctx.module_qn,
                call_node.start_byte,
                constructing=call_node.type == cs.TS_SCALA_INSTANCE_EXPRESSION,
            )
        )

    def _resolve_php_class_callee(
        self, ctx: _CallScanContext, call_node: Node, call_name: str
    ) -> _PhpClassCallee:
        # `new C(...)` is always decided here; a member call only when its
        # receiver's class is known, so a miss keeps the name-only fallback.
        if call_node.type == cs.TS_PHP_OBJECT_CREATION_EXPRESSION:
            return _PhpClassCallee(True, self._resolve_php_new_callee(ctx, call_node))
        if call_node.type in cs.PHP_MEMBER_CALL_TYPES and (
            typed := self._resolve_php_typed_member(ctx, call_node, call_name)
        ):
            return _PhpClassCallee(True, typed)
        return _PhpClassCallee(False, None)

    def _resolve_php_new_callee(
        self, ctx: _CallScanContext, call_node: Node
    ) -> tuple[str, str] | None:
        # `new C(...)` names a class through PHP's namespace and `use` rules
        # alone; the generic resolver would judge a `use`d namespace path
        # external, or rebind the bare name to a function (issue #2466).
        path = php_class_names.new_expression_class_path(call_node)
        if not path:
            return None
        return self._noted(self._resolver.resolve_php_class_path(path, ctx.module_qn))

    def _resolve_php_typed_member(
        self, ctx: _CallScanContext, call_node: Node, call_name: str
    ) -> tuple[str, str] | None:
        # A receiver the site shows was built by `new C` has class C, so the
        # method binds on C (or what C inherits) rather than by bare name. A
        # miss here leaves the name-only fallback exactly as it was.
        if ctx.php_scope_classes is None:
            ctx.php_scope_classes = {}
        path = php_class_names.receiver_class_path(call_node, ctx.php_scope_classes)
        if not path:
            return None
        return self._noted(
            self._resolver.resolve_php_method_on_class_path(
                path, call_name, ctx.module_qn
            )
        )

    def _resolve_java_callee(
        self, ctx: _CallScanContext, call_node: Node
    ) -> tuple[str, str] | None:
        # The javac frontend (issue #1181): a HIT is the declaration the
        # compiler bound this call to, which the ARGUMENT TYPES select
        # and name-and-arity matching cannot; the external sentinel is
        # its proof that the call leaves the repo, so no first-party
        # edge is fabricated. Any miss (incl. the frontend being off, so
        # both fact maps are empty) falls back to today's Java resolver.
        callee_info = self._resolver.resolve_java_call_site(call_node, ctx.module_qn)
        if callee_info == java_ti.JAVA_EXTERNAL_TARGET:
            callee_info = None
        elif callee_info is None:
            callee_info = self._resolver.resolve_java_method_call(
                call_node, ctx.module_qn, ctx.local_var_types, ctx.caller_qn
            )
            if callee_info is not None:
                self._link_java_overload_ties(ctx, call_node, callee_info[1])
        return callee_info

    def _link_java_overload_ties(
        self, ctx: _CallScanContext, call_node: Node, callee_qn: str
    ) -> None:
        # Same-arity overloads the argument types cannot tell apart were
        # bound by declaration order, which is no evidence for any one of
        # them: link each, labelled `overload`, as a constructor family is
        # (issue #2548). The resolver's verdict carries the label onto the
        # primary edge, which the caller emits after the argument passes;
        # the edge label proper is scoped to the siblings, so the argument
        # references emitted in between keep their own.
        java = self._resolver.type_inference.java_type_inference
        ties = java.method_resolver.java_overload_ties(
            call_node, callee_qn, ctx.local_var_types, ctx.module_qn
        )
        if not ties:
            return
        self._verdict = cs.EdgeResolution.OVERLOAD
        prev_resolution = self._resolution
        self._resolution = cs.EdgeResolution.OVERLOAD
        registry = self._resolver.function_registry
        try:
            for target_type, target_qn in ties:
                if target_qn == callee_qn:
                    continue
                for variant in registry.variants(target_qn):
                    ctx.ensure_rel(
                        ctx.caller_spec,
                        cs.RelationshipType.CALLS,
                        (target_type, cs.KEY_QUALIFIED_NAME, variant),
                    )
        finally:
            self._resolution = prev_resolution

    def _resolve_python_callee(
        self,
        ctx: _CallScanContext,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        # The Jedi frontend (issue #1183): a HIT is the exact callee
        # through re-exports/decorators the trie approximates; the
        # external sentinel proves the call leaves the repo and
        # suppresses trie fabrication. Any miss (incl. the frontend
        # off, so both maps are empty) falls back to the Python
        # heuristics with call_point preserved.
        callee_info = self._resolver.resolve_python_call_site(call_node, ctx.module_qn)
        if callee_info == PY_EXTERNAL_TARGET:
            callee_info = None
        elif callee_info is None:
            callee_info = ctx.resolve_func(
                call_name,
                ctx.module_qn,
                call_var_types,
                ctx.class_context,
                ctx.caller_qn,
                ctx.language,
                call_point=call_node.start_byte,
            )
        return callee_info

    def _fallback_callee(
        self,
        ctx: _CallScanContext,
        call_node: Node,
        call_name: str,
        callee_info: tuple[str, str] | None,
    ) -> tuple[str, str] | None:
        if callee_info and ctx.language in (
            cs.SupportedLanguage.RUST,
            cs.SupportedLanguage.JULIA,
        ):
            # Rust and Julia macros and functions live in SEPARATE
            # namespaces: a macro invocation must not bind a same-named
            # fn and vice versa.
            is_macro_target = callee_info[1] in self.facts.macro_qns
            is_macro_invocation = call_node.type in (
                cs.TS_RS_MACRO_INVOCATION,
                cs.TS_JULIA_MACROCALL_EXPRESSION,
            )
            if is_macro_target != is_macro_invocation:
                # Swap to the other namespace's twin when the bucket
                # holds exactly one candidate there (issue #1882
                # review).
                sibling = self._sibling_qn_in_macro_namespace(
                    callee_info[1], is_macro_invocation
                )
                if sibling is None:
                    callee_info = None
                else:
                    callee_info = (callee_info[0], sibling)
        if not callee_info and ctx.resolve_builtin is not None:
            callee_info = ctx.resolve_builtin(call_name)
        if not callee_info and ctx.resolve_cpp_op is not None:
            callee_info = ctx.resolve_cpp_op(call_name, ctx.module_qn)
        if not callee_info and ctx.language == cs.SupportedLanguage.CPP:
            callee_info = self._cpp.local_alias_callee(ctx, call_node, call_name)
        if not callee_info and cs.SEPARATOR_DOT not in call_name:
            callee_info = self._bare_name_callee(ctx, call_node, call_name, callee_info)
        return callee_info

    def _bare_name_callee(
        self,
        ctx: _CallScanContext,
        call_node: Node,
        call_name: str,
        callee_info: tuple[str, str] | None,
    ) -> tuple[str, str] | None:
        if ctx.is_python:
            # A bare name that resolves to nothing may be a local alias of a
            # callable (do = self._start; do()). Resolve the assignment's
            # right-hand side and treat the alias call as a call to it.
            if ctx.alias_map is None:
                ctx.alias_map = self._build_local_alias_map(
                    ctx.caller_node,
                    ctx.queries[ctx.language][cs.QUERY_CONFIG],
                    ctx.module_qn,
                )
            if (rhs := ctx.alias_map.get(call_name)) is not None:
                callee_info = ctx.resolve_func(
                    rhs,
                    ctx.module_qn,
                    ctx.local_var_types,
                    ctx.class_context,
                    ctx.caller_qn,
                )
        if callee_info is None and ctx.is_flow_lang:
            # `x = factory(...); x(cb)`: x holds a closure returned by a
            # first-party factory (e.g. a retry/cache decorator applied
            # imperatively). Record the call so cb flows into that closure's
            # callable parameter once factory returns are known (finalize).
            if ctx.factory_aliases is None:
                ctx.factory_aliases = self._build_factory_alias_map(
                    ctx.caller_node,
                    ctx.module_qn,
                    ctx.local_var_types,
                    ctx.class_context,
                    self._flow_scope_boundaries(
                        ctx.queries[ctx.language][cs.QUERY_CONFIG]
                    ),
                    ctx.caller_qn,
                )
            if (factory_qn := ctx.factory_aliases.get(call_name)) is not None:
                self._record_factory_call(
                    call_node,
                    ctx.caller_qn,
                    factory_qn,
                    ctx.module_qn,
                    ctx.local_var_types,
                    ctx.class_context,
                )
        return callee_info

    def _ingest_unresolved_call(
        self, ctx: _CallScanContext, call_node: Node, call_name: str
    ) -> None:
        if call_name and ctx.language not in cs.IMPORT_BOUND_CALL_LANGUAGES:
            # The callee may be defined by a file added later; keep
            # its simple name so that file's arrival re-parses this
            # one (issue #1568). Not where an import would be needed
            # anyway: the importer lookup finds those waiters.
            self._note_unresolved(ctx.module_qn, written_simple_name(call_name))
        if (
            ctx.is_js_ts
            and ctx.class_context
            and call_name.startswith(cs.JS_THIS_CALL_PREFIX)
            and self._js_this_is_class_instance(call_node)
        ):
            # `this.M()` binds `this` lexically to the enclosing class
            # instance ONLY in a method body or a nested arrow that inherits
            # its `this`; a plain nested `function(){}` rebinds `this` per
            # call, so `_js_this_is_class_instance` gates those out (JS
            # same-syntax/different-semantics). Ordinary name resolution
            # climbs one scope from the caller qn -- which lands on the class
            # for a direct method but on the METHOD for a nested arrow --
            # then falls back to a bare `M` lookup that, under cross-class
            # same-name ambiguity, is dropped, so M reports dead (issue
            # #971). Anchor on the enclosing class instead and emit the
            # self-dispatch edge(s).
            self._emit_js_this_dispatch(ctx, ctx.class_context, call_name)
        if ctx.is_arg_ref_lang:
            # The callee is not first-party (a framework/stdlib call such as
            # grpclib Handler(self.__rpc_x), JS setTimeout(target), or a
            # runtime dispatcher), so the call chain cannot be followed into
            # it. A first-party function handed to it as an argument is still
            # invoked, so record it as referenced from this scope to keep it
            # reachable, across every flow-traced language.
            self._ingest_argument_function_references(
                call_node,
                ctx.caller_spec,
                ctx.module_qn,
                ctx.local_var_types,
                ctx.class_context,
                self._resolver.resolve_function_call,
                ctx.ensure_rel,
                ctx.caller_qn,
                ctx.arg_ref_rel,
                ctx.language,
            )

    def _emit_js_this_dispatch(
        self, ctx: _CallScanContext, class_context: str, call_name: str
    ) -> None:
        _, _, self_method = call_name.partition(cs.SEPARATOR_DOT)
        if self_method and cs.SEPARATOR_DOT not in self_method:
            for target_type, target_qn in self._resolver.self_dispatch_targets(
                class_context, self_method
            ):
                for variant in self._resolver.function_registry.variants(target_qn):
                    ctx.ensure_rel(
                        ctx.caller_spec,
                        cs.RelationshipType.CALLS,
                        (target_type, cs.KEY_QUALIFIED_NAME, variant),
                    )

    def _ingest_resolved_call(
        self,
        ctx: _CallScanContext,
        call_node: Node,
        call_name: str,
        callee_info: tuple[str, str],
    ) -> None:
        callee_type, callee_qn = callee_info

        if ctx.language == cs.SupportedLanguage.CSHARP:
            self._csharp.record_cross_module_use(ctx.module_qn, callee_qn)

        if (
            ctx.language == cs.SupportedLanguage.DART
            and callee_type != cs.NodeLabel.CLASS
            and callee_qn in self._resolver.type_inference.facts.dart_constructor_qns
        ):
            self._dart.instantiate_named_ctor_owner(
                ctx.caller_spec, callee_qn, ctx.ensure_rel
            )

        if (
            ctx.language == cs.SupportedLanguage.CPP
            and call_node.type == cs.TS_NEW_EXPRESSION
            and callee_type != cs.NodeLabel.CLASS
        ):
            callee_type, callee_qn = self._cpp.rebind_new_callee(
                ctx, call_name, callee_type, callee_qn
            )

        # A callee that resolved to a builtin (e.g. JS setTimeout(target))
        # has no first-party body to follow into, so pass-through flow is
        # pointless; but a first-party callback handed to it is still invoked,
        # so record a reference edge from this scope to keep it reachable.
        # The synthetic builtin.* qn never has a node, so emitting a CALLS
        # edge to it would only mint a phantom the database drops (issue
        # #652: 485 across the fixture suite) -- mirror the C++ builtin
        # operator rule and emit no edge at all.
        callee_is_builtin = callee_qn.startswith(_BUILTIN_QN_PREFIX)
        if callee_is_builtin:
            if ctx.is_arg_ref_lang:
                self._ingest_argument_function_references(
                    call_node,
                    ctx.caller_spec,
                    ctx.module_qn,
                    ctx.local_var_types,
                    ctx.class_context,
                    self._resolver.resolve_function_call,
                    ctx.ensure_rel,
                    ctx.caller_qn,
                    ctx.arg_ref_rel,
                    ctx.language,
                )
            return

        if ctx.is_flow_lang:
            self._collect_callable_flow(
                call_node,
                callee_qn,
                ctx.caller_qn,
                ctx.caller_params,
                ctx.module_qn,
                ctx.local_var_types,
                ctx.class_context,
            )
        if ctx.is_arg_ref_lang:
            # Functions are first-class values: a first-party callee may STORE
            # a passed callback for later dynamic dispatch (config objects,
            # codecs, registries), which callable-param flow cannot trace, or
            # the callee may be a same-named misbind of an external method.
            # Record the pass itself as a REFERENCES edge from the passing
            # scope so the callback is never reported dead.
            self._ingest_argument_function_references(
                call_node,
                ctx.caller_spec,
                ctx.module_qn,
                ctx.local_var_types,
                ctx.class_context,
                self._resolver.resolve_function_call,
                ctx.ensure_rel,
                ctx.caller_qn,
                cs.RelationshipType.REFERENCES,
                ctx.language,
            )
        self._ingest_first_party_call(ctx, call_node, call_name, callee_type, callee_qn)

    def _ingest_first_party_call(
        self,
        ctx: _CallScanContext,
        call_node: Node,
        call_name: str,
        callee_type: str,
        callee_qn: str,
    ) -> None:
        if (
            ctx.is_python
            # A stub the name-only fallback merely guessed (a dict literal's
            # `.get(...)`) says nothing about the receiver: fanning it out
            # turned one guess into exact edges to every conformer, test
            # doubles included (issue #2931). It stays one heuristic edge.
            and self._verdict != cs.EdgeResolution.HEURISTIC
            and (
                dispatch_targets := self._resolver.protocol_dispatch_targets(callee_qn)
            )
        ):
            # The call resolved to a Protocol stub; the stub never runs, so emit
            # edges to the method on every conformer instead of the stub.
            self._emit_protocol_conformer_edges(ctx, dispatch_targets)
            return

        if (
            ctx.is_python
            and ctx.class_context
            and (
                call_name.startswith(cs.PY_SELF_PREFIX)
                or call_name.startswith(cs.PY_CLS_PREFIX)
            )
            # A call that runs after `self = Other()` on every path calls
            # through that value's type, which the resolved edge follows.
            and not (
                (rebinding := self._receiver_rebinding_at(ctx, call_node, call_name))
                and rebinding.always
            )
        ):
            # self.M()/cls.M() statically targets the enclosing class's own or
            # inherited M and dynamically dispatches to every concrete subclass
            # override, so emit an edge to each in ADDITION to the resolved edge
            # below; otherwise a base (or override) reached only through the
            # self-call looks dead. Anchor on the enclosing class, not the
            # resolved callee: when M is abstract with several overrides the trie
            # resolves the call to an arbitrary sibling, so anchoring there would
            # miss the base and the others. The self/cls receiver excludes
            # super().M() and Base.M() (not virtual dispatch). Skip self.attr.M()
            # (a call on a member, not on self).
            self._emit_python_self_dispatch(ctx, ctx.class_context, call_name)

        if (
            ctx.is_js_ts
            and ctx.class_context
            and call_name.startswith(cs.JS_THIS_CALL_PREFIX)
            and self._resolver.function_registry.is_abstract(callee_qn)
            and self._js_this_is_class_instance(call_node)
        ):
            # `this.m()` bound to an `abstract m()` declaration (issue #2524)
            # runs a subclass override, never the declaration. Before the
            # declaration had a node the call went unresolved and dispatched
            # to those overrides; keep those edges beside the static one.
            self._emit_js_this_dispatch(ctx, ctx.class_context, call_name)

        if ctx.is_flow_lang:
            # f(...) invoked through a parameter: the edge runs from the
            # callee to whatever each call site binds to that parameter.
            self._ingest_callable_param_calls(
                call_node,
                callee_type,
                callee_qn,
                ctx.module_qn,
                ctx.local_var_types,
                ctx.class_context,
                self._resolver.resolve_function_call,
                ctx.caller_qn,
            )

        if (
            ctx.language in _NEW_CONSTRUCTS_LANGUAGES
            and call_node.type in _OBJECT_CREATION_NODE_TYPES
            and callee_type != cs.NodeLabel.CLASS
        ):
            # `new X(...)` where X resolves to a non-class: a Java interface
            # or annotation implemented by an anonymous class (`new
            # Comparator<T>(){ ... }`), or a C# type the resolver could not bind
            # to a first-party class. There is no first-party constructor to
            # call, and a bare CALLS edge to a non-callable node (Interface) is
            # not valid, so drop the edge rather than emit it.
            return

        if callee_type == cs.NodeLabel.CLASS:
            # Record construction as INSTANTIATES -> the class node (keeps
            # CALLS function/method-only). When the class defines __init__,
            # ALSO redirect a CALLS edge to it (the constructor runs); when
            # it does not (dataclass/NamedTuple/pydantic), INSTANTIATES is
            # the only edge.
            redirected = self._redirect_construction(ctx, callee_qn)
            if redirected is None:
                return
            callee_type, callee_qn = redirected

        if not self._emit_resolved_callee_targets(
            ctx, call_node, call_name, callee_type, callee_qn
        ):
            return
        self._emit_callee_fanouts(ctx, call_name, callee_type, callee_qn)

    def _emit_protocol_conformer_edges(
        self, ctx: _CallScanContext, dispatch_targets: set[tuple[str, str]]
    ) -> None:
        edges = [
            (conformer_type, target_qn)
            for conformer_type, conformer_qn in dispatch_targets
            for target_qn in self._resolver.function_registry.variants(conformer_qn)
        ]
        # The receiver is one of the conformers, so with several the call is
        # a fan-out like any other same-named candidate set (issue #1526):
        # renaming one conformer must not rewrite the shared call site.
        self._resolution = (
            cs.EdgeResolution.OVERLOAD if len(edges) > 1 else self._verdict
        )
        for conformer_type, target_qn in edges:
            ctx.ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.CALLS,
                (conformer_type, cs.KEY_QUALIFIED_NAME, target_qn),
            )

    @staticmethod
    def _receiver_rebinding_at(
        ctx: _CallScanContext, call_node: Node, call_name: str
    ) -> ReceiverRebinding | None:
        # Only a `self.M()` / `cls.M()` in a method whose type map holds the
        # receiver can have been rebound; every other call skips the scan.
        receiver, _, member = call_name.partition(cs.SEPARATOR_DOT)
        if (
            not ctx.is_python
            or not member
            or receiver not in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS)
            or not ctx.local_var_types
            or receiver not in ctx.local_var_types
        ):
            return None
        return receiver_rebinding(call_node, receiver)

    def _emit_python_self_dispatch(
        self, ctx: _CallScanContext, class_context: str, call_name: str
    ) -> None:
        _, _, self_method = call_name.partition(cs.SEPARATOR_DOT)
        if cs.SEPARATOR_DOT not in self_method:
            for target_type, target_qn in self._resolver.self_dispatch_targets(
                class_context, self_method
            ):
                for variant in self._resolver.function_registry.variants(target_qn):
                    ctx.ensure_rel(
                        ctx.caller_spec,
                        cs.RelationshipType.CALLS,
                        (target_type, cs.KEY_QUALIFIED_NAME, variant),
                    )

    def _redirect_construction(
        self,
        ctx: _CallScanContext,
        callee_qn: str,
        ctor_rel: cs.RelationshipType = cs.RelationshipType.CALLS,
    ) -> tuple[str, str] | None:
        class_variants = self._resolver.function_registry.variants(callee_qn)
        # Julia call syntax dispatches in the FUNCTION namespace:
        # a same-named free function wins over the type (no
        # INSTANTIATES) and a macro is not a call target.
        julia_fn_variants: list[tuple[cs.NodeLabel, str]] = []
        if ctx.language == cs.SupportedLanguage.JULIA:
            julia_fn_variants = [
                (node_type, variant)
                for variant in class_variants
                if (node_type := self._resolver.function_registry.get(variant))
                in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
                and variant not in self.facts.macro_qns
            ]
        self._resolution = (
            cs.EdgeResolution.OVERLOAD if len(class_variants) > 1 else self._verdict
        )
        for class_variant in class_variants:
            # A duplicate-suffixed variant may be a DIFFERENT kind
            # of node (a merged TS namespace registers as a class,
            # a colliding function as a Function); only class-typed
            # variants are instantiable, and a mismatched label is
            # a phantom the database drops (issue #652).
            variant_type = self._resolver.function_registry.get(class_variant)
            if variant_type is not None and variant_type != cs.NodeLabel.CLASS:
                if ctx.is_python:
                    self._emit_python_binding_twin(ctx, class_variant, variant_type)
                continue
            if julia_fn_variants:
                # A shadowing function owns the call: no instantiation.
                continue
            ctx.ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.INSTANTIATES,
                (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_variant),
            )
        if ctx.language == cs.SupportedLanguage.JULIA:
            self._emit_julia_construction_calls(ctx, callee_qn, julia_fn_variants)
            return None
        if ctx.language in (
            cs.SupportedLanguage.JAVA,
            cs.SupportedLanguage.CSHARP,
            cs.SupportedLanguage.CPP,
            cs.SupportedLanguage.DART,
            cs.SupportedLanguage.SCALA,
        ):
            # A Java/C#/C++/Dart constructor is a method named like its
            # class (`Foo.Foo`), not `__init__`; `new Foo(...)` / `Foo(...)`
            # runs one, so redirect a CALLS edge to every declared
            # constructor (overload selection unneeded for reachability);
            # a Java `Foo::new` runs none yet and passes REFERENCES.
            # C#, C++, and Dart default constructors use the same
            # class-simple-name convention, so java_constructor_targets
            # selects them too (a Dart NAMED constructor is invoked by its
            # own name and resolves as an ordinary method). C++ additionally
            # redirects to the destructor: the object's `~X` runs at end of
            # lifetime with no call node of its own. sorted(): the target
            # label is a hash-randomized StrEnum, so sort for determinism.
            self._emit_declared_ctor_calls(ctx, callee_qn, class_variants, ctor_rel)
            return None
        if ctx.language == cs.SupportedLanguage.PHP:
            # A PHP `new X(...)` runs `__construct`, X's own or, when X
            # declares none, the one it inherits (issue #2466).
            return self._resolver.php_constructor(callee_qn)
        # A JS/TS `new X(...)` runs X's `constructor` method (leaf name
        # `constructor`, not Python's `__init__`); redirect the CALLS
        # edge there so an explicitly-declared constructor is reachable.
        ctor_member = (
            cs.KEYWORD_CONSTRUCTOR
            if ctx.language in cs.JS_TS_LANGUAGES
            else cs.PY_METHOD_INIT
        )
        init_qn = f"{callee_qn}{cs.SEPARATOR_DOT}{ctor_member}"
        if init_qn not in self._resolver.function_registry:
            return None
        return (cs.NodeLabel.METHOD, init_qn)

    def _emit_julia_construction_calls(
        self,
        ctx: _CallScanContext,
        callee_qn: str,
        fn_variants: list[tuple[cs.NodeLabel, str]],
    ) -> None:
        # A shadowing free function wins, else the inner same-name
        # constructor (like Java/C#). The resolution describes the
        # FILTERED target set (#1882).
        if fn_variants:
            self._resolution = (
                cs.EdgeResolution.OVERLOAD if len(fn_variants) > 1 else self._verdict
            )
            for node_type, variant in sorted(fn_variants):
                ctx.ensure_rel(
                    ctx.caller_spec,
                    cs.RelationshipType.CALLS,
                    (node_type, cs.KEY_QUALIFIED_NAME, variant),
                )
            return
        ctor_edges = [
            (ctor_type, variant)
            for ctor_type, ctor_qn in sorted(
                self._resolver.java_constructor_targets(callee_qn)
            )
            for variant in self._resolver.function_registry.variants(ctor_qn)
        ]
        self._resolution = (
            cs.EdgeResolution.OVERLOAD if len(ctor_edges) > 1 else self._verdict
        )
        for ctor_type, variant in ctor_edges:
            ctx.ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.CALLS,
                (ctor_type, cs.KEY_QUALIFIED_NAME, variant),
            )

    def _emit_declared_ctor_calls(
        self,
        ctx: _CallScanContext,
        callee_qn: str,
        class_variants: list[str],
        ctor_rel: cs.RelationshipType = cs.RelationshipType.CALLS,
    ) -> None:
        if ctx.language == cs.SupportedLanguage.CPP:
            self._cpp.emit_ctor_calls(ctx.caller_spec, callee_qn)
            return
        if ctx.language == cs.SupportedLanguage.SCALA:
            self._emit_scala_auxiliary_ctor_calls(ctx, class_variants)
            return
        # Every class variant INSTANTIATES above, so every one
        # takes its constructors too: `Box@8` for `class Box<T>`
        # declares `Box@8.Box(T)`, which is not a variant of the
        # natural twin's `Box.Box` (issue #2007).
        # The same class-typed gate INSTANTIATES applies above: a
        # variant of another kind (a colliding function, a merged
        # namespace) has no constructor to redirect to.
        ctor_edges = [
            (ctor_type, variant)
            for class_variant in class_variants
            if self._resolver.function_registry.get(class_variant)
            in (None, cs.NodeLabel.CLASS)
            for ctor_type, ctor_qn in sorted(
                self._resolver.java_constructor_targets(class_variant)
            )
            for variant in self._resolver.function_registry.variants(ctor_qn)
        ]
        if len(ctor_edges) > 1:
            # Every declared constructor takes an edge because
            # argument-type selection is not attempted: one call,
            # several candidates, which is what `overload` means
            # (issue #1526).
            self._resolution = cs.EdgeResolution.OVERLOAD
        for ctor_type, variant in ctor_edges:
            ctx.ensure_rel(
                ctx.caller_spec,
                ctor_rel,
                (ctor_type, cs.KEY_QUALIFIED_NAME, variant),
            )

    def _ingest_java_method_reference(
        self, ctx: _CallScanContext, ref_node: Node
    ) -> None:
        # `Type::m`, `expr::m`, `this::m`, `super::m` hand a method over as a
        # value and nothing runs at this site, so the edge is REFERENCES: what
        # a C# method group and a callback argument get in every other
        # language, which keeps the method reachable without asserting a
        # call the graph cannot see (issue #2546).
        if (parts := java_utils.method_reference_parts(ref_node)) is None:
            return
        if parts.method_name is None:
            self._ingest_java_constructor_reference(ctx, ref_node, parts.receiver)
            return
        java = self._resolver.type_inference.java_type_inference
        targets = java.method_resolver.java_method_reference_targets(
            parts.receiver, parts.method_name, ctx.local_var_types, ctx.module_qn
        )
        registry = self._resolver.function_registry
        edges = [
            (label, variant)
            for target_type, target_qn in targets
            for variant in registry.variants(target_qn)
            if (label := registry.get(variant) or target_type)
            in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
        ]
        if not edges:
            # Java sees same-package definitions without an import, so the
            # name is the only link to a file that may define it later.
            self._note_unresolved(ctx.module_qn, parts.method_name)
            return
        self._resolution = (
            cs.EdgeResolution.OVERLOAD if len(edges) > 1 else self._verdict
        )
        for label, target_qn in edges:
            ctx.ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.REFERENCES,
                (label, cs.KEY_QUALIFIED_NAME, target_qn),
            )

    def _ingest_java_constructor_reference(
        self, ctx: _CallScanContext, ref_node: Node, receiver: Node
    ) -> None:
        # `Type::new` is a factory for Type: the type is resolved and
        # INSTANTIATES'd exactly as `new Type(...)` is, and its constructors are
        # referenced rather than called, since none runs here. The functional
        # interface that would pick one is not visible to the parser, so each
        # declared constructor is referenced, labelled `overload` when there
        # are several. An array type (`int[]::new`) constructs no class.
        if not (type_name := java_utils.method_reference_receiver_text(receiver)):
            return
        callee = ctx.resolve_func(
            type_name,
            ctx.module_qn,
            ctx.local_var_types,
            ctx.class_context,
            ctx.caller_qn,
            ctx.language,
            call_point=ref_node.start_byte,
            constructing=True,
        )
        if callee is None:
            self._note_unresolved(ctx.module_qn, written_simple_name(type_name))
            return
        if self._resolver.function_registry.get(callee[1]) == cs.NodeLabel.CLASS:
            self._redirect_construction(
                ctx, callee[1], ctor_rel=cs.RelationshipType.REFERENCES
            )

    def _emit_resolved_callee_targets(
        self,
        ctx: _CallScanContext,
        call_node: Node,
        call_name: str,
        callee_type: str,
        callee_qn: str,
    ) -> bool:
        callees = (
            self._resolver.member_alias_fanout(
                callee_qn, call_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            )
            if ctx.is_python and cs.SEPARATOR_DOT in call_name
            else [callee_qn]
        )
        targets = self._resolver.lexical_call_targets(
            call_name,
            ctx.module_qn,
            [
                variant
                for callee in callees
                for variant in self._resolver.function_registry.variants(callee)
            ],
            ctx.caller_qn,
        )
        if ctx.language in (
            cs.SupportedLanguage.RUST,
            cs.SupportedLanguage.JULIA,
        ):
            # Filter the fan-out by the macro registry: an invocation
            # binds only macros, a plain call only non-macros.
            is_macro_invocation = call_node.type in (
                cs.TS_RS_MACRO_INVOCATION,
                cs.TS_JULIA_MACROCALL_EXPRESSION,
            )
            targets = [
                t for t in targets if (t in self.facts.macro_qns) == is_macro_invocation
            ]
            if not targets:
                return False
        if (
            len(targets) > 1
            and not self._resolver.import_processor.rust.rust_block_item_qns.isdisjoint(
                targets
            )
        ):
            # The dedup bucket holds a Rust block-local item beside an
            # item of another scope. Those are different functions
            # sharing a natural name, not spellings of one callee, and
            # the span-gated probe already chose between them, so the
            # hedge that fans an ambiguous callee onto its twins would
            # cross the block boundary here (issue #1061).
            targets = [callee_qn]
        # The resolver's verdict on this callee, then one edge per
        # same-named candidate when the callee fans out (issue #1526).
        self._resolution = self._verdict
        exact_in_every_build: dict[str, bool] = {}
        if ctx.language == cs.SupportedLanguage.GO:
            targets, exact_in_every_build = self._go.build_targets(
                ctx.module_qn, callee_type, callee_qn, targets
            )
        if len(targets) > 1:
            self._resolution = cs.EdgeResolution.OVERLOAD
        call_resolution = self._resolution
        for target_qn in targets:
            self._resolution = (
                call_resolution
                if exact_in_every_build.get(target_qn, True)
                else cs.EdgeResolution.HEURISTIC
            )
            self._emit_callee_target(ctx, callee_type, target_qn)
        self._resolution = call_resolution
        return True

    def _emit_callee_target(
        self, ctx: _CallScanContext, callee_type: str, target_qn: str
    ) -> None:
        # A duplicate-suffixed variant may be a DIFFERENT kind of node (a TS
        # namespace merged onto a function registers as a class); only
        # callable variants take a CALLS edge, and emitting the primary's
        # label onto a differently-typed node is a phantom the database drops
        # (issue #652). An unregistered target keeps its edge
        # (resolver-derived callees like an unwrapped fn.call base may not
        # register).
        target_type = self._resolver.function_registry.get(target_qn)
        if target_type is not None and target_type not in (
            cs.NodeLabel.FUNCTION,
            cs.NodeLabel.METHOD,
        ):
            if ctx.is_python:
                self._emit_python_binding_twin(ctx, target_qn, target_type)
            return
        # A callable variant takes its OWN label: a Scala method's local def
        # or anonymous-object member registers as a Function beside the
        # class's same-named Method, and the primary's label on the other is
        # the same phantom (issue #2848).
        label = callee_type if target_type is None else target_type
        ctx.ensure_rel(
            ctx.caller_spec,
            cs.RelationshipType.CALLS,
            (label, cs.KEY_QUALIFIED_NAME, target_qn),
        )

    def _emit_python_binding_twin(
        self, ctx: _CallScanContext, twin_qn: str, twin_type: cs.NodeLabel
    ) -> None:
        # A Python name holds one binding at a time, so a class and a
        # same-named def (a docs shim, an ImportError fallback) are both what
        # `Tool()` may run. Skipping the twin of the other kind bound every
        # construction to the shim alone (issue #2621).
        if twin_type in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
            ctx.ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.CALLS,
                (twin_type, cs.KEY_QUALIFIED_NAME, twin_qn),
            )
            return
        if twin_type != cs.NodeLabel.CLASS:
            return
        ctx.ensure_rel(
            ctx.caller_spec,
            cs.RelationshipType.INSTANTIATES,
            (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, twin_qn),
        )
        init_qn = f"{twin_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
        if init_qn in self._resolver.function_registry:
            ctx.ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.CALLS,
                (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, init_qn),
            )

    def _emit_callee_fanouts(
        self,
        ctx: _CallScanContext,
        call_name: str,
        callee_type: str,
        callee_qn: str,
    ) -> None:
        if ctx.language == cs.SupportedLanguage.GO:
            self._go.emit_callee_fanouts(
                ctx.caller_spec, ctx.module_qn, callee_type, callee_qn, ctx.ensure_rel
            )
        elif callee_type == cs.NodeLabel.METHOD:
            self._emit_interface_sole_impls(ctx, callee_qn)

        if (
            ctx.is_js_ts
            and cs.SEPARATOR_DOT in call_name
            and callee_type in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
        ):
            self._emit_js_member_twins(ctx, callee_qn)

        if (
            ctx.is_python
            and callee_type == cs.NodeLabel.FUNCTION
            and cs.SEPARATOR_DOT not in call_name
        ):
            self._emit_conditional_import_twin(ctx, call_name, callee_qn)

    def _emit_interface_sole_impls(self, ctx: _CallScanContext, callee_qn: str) -> None:
        # A call bound to an interface/trait method (the static callee;
        # removing its declaration breaks the call) with exactly ONE
        # implementer also runs the concrete method, so edge both. The
        # old REPLACING redirect orphaned the interface stub (gson's
        # FieldNamingStrategy.translateName reported dead); sorted():
        # the target label is a hash-randomized StrEnum.
        for impl_type, impl_qn in sorted(
            self._resolver.interface_sole_impl_targets(callee_qn)
        ):
            for variant in self._resolver.function_registry.variants(impl_qn):
                ctx.ensure_rel(
                    ctx.caller_spec,
                    cs.RelationshipType.CALLS,
                    (impl_type, cs.KEY_QUALIFIED_NAME, variant),
                )

    def _emit_js_member_twins(self, ctx: _CallScanContext, callee_qn: str) -> None:
        # A JS member call that bound one twin of a double-registered
        # prototype method (`this.lookup()` -> module-flat `view.lookup`,
        # leaving `View.lookup` dead) edges the same-module same-name
        # member twin too (duplicate-QN keep-both design; revive-only).
        for twin_type, twin_qn in sorted(
            self._resolver.js_member_twin_targets(callee_qn)
        ):
            for variant in self._resolver.function_registry.variants(twin_qn):
                ctx.ensure_rel(
                    ctx.caller_spec,
                    cs.RelationshipType.CALLS,
                    (twin_type, cs.KEY_QUALIFIED_NAME, variant),
                )

    def _emit_conditional_import_twin(
        self, ctx: _CallScanContext, call_name: str, callee_qn: str
    ) -> None:
        # A platform-conditional import with a local fallback def (click's
        # `if WIN: from ._winconsole import X ... else: def X(...)`) is
        # statically undecidable; the call resolves to the import, so the
        # mutually-exclusive local def looks dead. When the name was bound
        # by a CONDITIONAL import and the CURRENT module also defines it,
        # fan the call out to the local twin too, mirroring the Go
        # build-variant fan-out. An UNCONDITIONAL import shadowing a local
        # def is plain shadowing: the local stays dead, so no edge.
        local_qn = f"{ctx.module_qn}{cs.SEPARATOR_DOT}{call_name}"
        if (
            local_qn != callee_qn
            and call_name
            in self._resolver.import_processor.python.conditional_imports.get(
                ctx.module_qn, ()
            )
            and self._resolver.function_registry.get(local_qn) == cs.NodeLabel.FUNCTION
        ):
            for variant in self._resolver.function_registry.variants(local_qn):
                ctx.ensure_rel(
                    ctx.caller_spec,
                    cs.RelationshipType.CALLS,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, variant),
                )

    @_site_scoped
    def _ingest_operator_dispatch_calls(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> None:
        boundary = (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION)
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = self._site_node = stack.pop()
            if node.type in boundary:
                continue
            if (dunder := _operator_dunder_target(node)) is not None:
                self._emit_operator_dunder(
                    dunder[0], dunder[1], caller_spec, module_qn, local_var_types
                )
            elif node.type == cs.TS_PY_CALL:
                self._emit_instance_call(node, caller_spec, module_qn, local_var_types)
            for operand in _truthiness_operands(node):
                self._emit_truthiness(operand, caller_spec, module_qn, local_var_types)
            stack.extend(node.children)

    @_site_scoped
    def _emit_truthiness(
        self,
        operand: Node | None,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> None:
        # Truthiness of an object calls __bool__ if defined, else __len__. Only a
        # bare name/attribute operand names an object (a comparison/call is already
        # a bool and is handled elsewhere); try __bool__ first, then __len__.
        if operand is not None:
            self._site_node = operand
        if operand is None or operand.type not in (
            cs.TS_PY_IDENTIFIER,
            cs.TS_PY_ATTRIBUTE,
        ):
            return
        for dunder in (cs.PY_DUNDER_BOOL, cs.PY_DUNDER_LEN):
            if self._emit_operator_dunder(
                operand, dunder, caller_spec, module_qn, local_var_types
            ):
                return

    @_site_scoped
    def _emit_operator_dunder(
        self,
        operand: Node | None,
        dunder: str,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> bool:
        # Resolve the implied <operand>.__dunder__ call; resolution only succeeds
        # for a first-party class that defines the dunder, so builtin containers
        # (dict/list) yield no edge. Restrict to simple attribute/name operands.
        # Returns whether an edge was emitted.
        if operand is None or not (operand_text := safe_decode_text(operand)):
            return False
        self._site_node = operand
        if any(ch in operand_text for ch in cs.PY_OPERAND_REJECT_CHARS):
            return False
        targets = self._resolver.operator_dunder_targets(
            operand_text, dunder, module_qn, local_var_types
        )
        if not targets:
            return False
        for callee_type, callee_qn in targets:
            for target_qn in self._resolver.function_registry.variants(callee_qn):
                self._emit_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (callee_type, cs.KEY_QUALIFIED_NAME, target_qn),
                )
        return True

    @_site_scoped
    def _emit_instance_call(
        self,
        call_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> None:
        # `obj(...)` on a first-party instance runs type(obj).__call__, which
        # the class body may bind to another method (issue #2620).
        callee = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if callee is None or callee.type not in _PY_INSTANCE_CALLEE_TYPES:
            return
        is_call_result = callee.type == cs.TS_PY_CALL
        if not is_call_result and not local_var_types:
            return
        if not (callee_text := safe_decode_text(callee)):
            return
        targets = self._resolver.instance_call_targets(
            callee_text, is_call_result, module_qn, local_var_types
        )
        self._site_node = call_node
        for callee_type, callee_qn in sorted(targets):
            methods = self._resolver.member_alias_fanout(callee_qn, cs.PY_DUNDER_CALL)
            self._resolution = (
                cs.EdgeResolution.OVERLOAD
                if len(methods) > 1
                else cs.EdgeResolution.EXACT
            )
            for method in methods:
                for variant in self._resolver.function_registry.variants(method):
                    self._emit_rel(
                        caller_spec,
                        cs.RelationshipType.CALLS,
                        (callee_type, cs.KEY_QUALIFIED_NAME, variant),
                    )

    @staticmethod
    def _js_is_logical_binary(node: Node) -> bool:
        # `||`, `??` and `&&` evaluate to ONE OF their operands; every other
        # binary operator produces a fresh value. Callers gate to JS/TS: Go
        # spells boolean `||`/`&&` with the same node and field, where a
        # named operand is a bool, never a callable.
        operator = node.child_by_field_name(cs.FIELD_OPERATOR)
        return (
            operator is not None
            and safe_decode_text(operator) in cs.JS_SHORT_CIRCUIT_OPERATORS
        )

    @_site_scoped
    def _ingest_assignment_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> None:
        # `x = some_function` binds a first-class function value to a name; the
        # alias is then stored, passed onward, or returned for dynamic dispatch
        # (http_callback = llm_http_task_closure_with_context), so the assignment
        # itself references the function and must keep it reachable. Only a plain
        # name/attribute RHS counts (calls resolve as calls); the walk stops at
        # nested scope boundaries, which own their own pass.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self._emit_rel
        scope = _RefEmitScope(
            caller_spec,
            module_qn,
            local_var_types,
            class_context,
            caller_qn,
            resolve_func,
            ensure_rel,
        )
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = self._site_node = stack.pop()
            # Continue THROUGH an unowned anonymous arrow (zustand's curried
            # middleware body `(config) => (set, get, api) => { api.setState =
            # ... }`): it gets no caller pass, so its assignments would else be
            # scanned by nobody and the stored functions report dead. Named
            # nested scopes still own their own pass.
            if node.type in boundary_types and not self._is_unowned_js_scope(
                node, module_qn
            ):
                continue
            if rhs_field := _ASSIGNMENT_RHS_FIELDS.get(node.type):
                self._ingest_assignment_rhs(node, rhs_field, scope, language)
            stack.extend(node.children)

    def _ingest_assignment_rhs(
        self,
        node: Node,
        rhs_field: str,
        scope: _RefEmitScope,
        language: cs.SupportedLanguage | None,
    ) -> None:
        right = node.child_by_field_name(rhs_field)
        # Go wraps every assignment RHS in an expression_list
        # (`var f = fn`, `a, b = g, h`); scan each value so the bare
        # func name(s) underneath are referenced. Other languages
        # carry a lone RHS node.
        values = (
            list(right.named_children)
            if right is not None and right.type == cs.TS_GO_EXPRESSION_LIST
            else [right]
        )
        for value in values:
            if value is None:
                continue
            self._ingest_assignment_value(node, value, scope, language)

    def _ingest_assignment_value(
        self,
        node: Node,
        value: Node,
        scope: _RefEmitScope,
        language: cs.SupportedLanguage | None,
    ) -> None:
        # `export const persist = persistImpl as unknown as Persist`
        # wraps the aliased impl in TS casts, and devtools' shape
        # interleaves parens (`api.setState = ((s, r) => {...}) as
        # SetState`); peel both so the bare name/arrow underneath is
        # what we reference.
        # `const bound = handler.bind(null)` stores the bound
        # handler; .bind/.call/.apply are transparent for
        # reference resolution like a cast.
        value = self._peel_bound_callable(value, peel_parens=True)
        # A bare-name RHS names a callable; an inline arrow/function-expr
        # RHS (`OpenAPI.TOKEN = async () => {}`) stores an anonymous
        # function on the target for later invocation, and
        # _emit_callback_edge references it by position. A named
        # arrow-const RHS is registered by its name, so the by-position
        # lookup finds nothing.
        # A LOGICAL DEFAULT RHS (`done = done || function (err,
        # str) {...}`, express's render; `options.handler =
        # options.handler || defaultHandler`, fastify, issue
        # #990) evaluates TO one of its operands, so a NAMED
        # function operand flows as the assigned value exactly
        # like an inline one; chains (`a || b || defaultFmt`)
        # nest further logical binaries on the left. Only the
        # short-circuit operators qualify: a comparison or
        # arithmetic operand (`x === fn`, `n + fn`) never IS the
        # assigned value, so named operands there stay silent
        # while inline functions keep the historical reference.
        if value.type == cs.TS_BINARY_EXPRESSION:
            self._emit_binary_rhs_refs(value, scope, language)
            return
        # A Python TERNARY / BOOLEAN-DEFAULT RHS (`get_response =
        # self._async if is_async else self._sync`, django's
        # BaseHandler; `f = handler or fallback`) binds one of its
        # RESULT operands; each result operand naming a callable is a
        # possible referent. A ternary's condition is only
        # truthiness-tested, never bound, so it is excluded; both
        # boolean operands are possible results.
        if value.type in (
            cs.TS_PY_CONDITIONAL_EXPRESSION,
            cs.TS_JS_TERNARY_EXPRESSION,
            cs.TS_PY_BOOLEAN_OPERATOR,
        ):
            self._emit_conditional_rhs_refs(value, scope)
            return
        if (
            value.type in _ASSIGNMENT_RHS_REF_TYPES
            or value.type in js_bindings.INLINE_FUNC_VALUE_TYPES
        ):
            self._emit_scope_ref(scope, value)
            # A member-assigned inline function (`api.setState =
            # (s, r) => {...}`) is ALSO registered by the def pass
            # under the PROPERTY name (scope.setState), which the
            # by-position anonymous candidate above never matches;
            # reference that registration too or it reports dead.
            if value.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
                self._emit_assigned_name_ref(
                    node, scope.caller_spec, scope.ensure_rel, scope.caller_qn
                )

    def _emit_binary_rhs_refs(
        self,
        value: Node,
        scope: _RefEmitScope,
        language: cs.SupportedLanguage | None,
    ) -> None:
        # Named-operand emission is JS/TS-only: Go reaches
        # this walk too, and its boolean `||`/`&&` operands
        # would fabricate phantom edges (a bool named like a
        # method falsely revives it).
        is_logical = language in cs.JS_TS_LANGUAGES and self._js_is_logical_binary(
            value
        )
        # Position matters for `&&` only: a function value
        # is always truthy, so the LEFT operand of `&&` can
        # never be the expression's result; `||`/`??` flow
        # either operand.
        operands = self._logical_rhs_operands(value, is_logical)
        for operand, value_position in operands:
            if operand.type in js_bindings.INLINE_FUNC_VALUE_TYPES or (
                is_logical
                and value_position
                and operand.type in _ASSIGNMENT_RHS_REF_TYPES
            ):
                self._emit_scope_ref(scope, operand)

    def _logical_rhs_operands(
        self, value: Node, is_logical: bool
    ) -> list[tuple[Node, bool]]:
        operands: list[tuple[Node, bool]] = []
        pending: list[tuple[Node, bool]] = [(value, True)]
        while pending:
            raw, value_position = pending.pop()
            operand = self._unwrap_ts_value(raw)
            if (
                is_logical
                and operand.type == cs.TS_BINARY_EXPRESSION
                and self._js_is_logical_binary(operand)
            ):
                is_and = (
                    safe_decode_text(operand.child_by_field_name(cs.FIELD_OPERATOR))
                    == cs.JS_OPERATOR_LOGICAL_AND
                )
                left = operand.child_by_field_name(cs.FIELD_LEFT)
                right = operand.child_by_field_name(cs.TS_FIELD_RIGHT)
                if left is not None:
                    pending.append((left, value_position and not is_and))
                if right is not None:
                    pending.append((right, value_position))
                continue
            operands.append((operand, value_position))
        return operands

    def _emit_conditional_rhs_refs(self, value: Node, scope: _RefEmitScope) -> None:
        # A boolean-default RHS binds either operand; a ternary
        # binds a RESULT operand, never the truthiness-tested
        # condition (whose index differs between Python's
        # conditional_expression and TS's ternary_expression).
        operands = (
            list(value.named_children)
            if value.type == cs.TS_PY_BOOLEAN_OPERATOR
            else _conditional_result_operands(value, False)
        )
        for operand in operands:
            operand = self._unwrap_ts_value(operand)
            if (
                operand.type in _ASSIGNMENT_RHS_REF_TYPES
                or operand.type in js_bindings.INLINE_FUNC_VALUE_TYPES
            ):
                self._emit_scope_ref(scope, operand)

    def _emit_scope_ref(self, scope: _RefEmitScope, operand: Node) -> None:
        # A REFERENCES edge from the walk's caller to the callable `operand`
        # names, resolved in the walk's own scope.
        self._emit_callback_edge(
            scope.caller_spec,
            operand,
            scope.module_qn,
            scope.local_var_types,
            scope.class_context,
            scope.resolve_func,
            scope.ensure_rel,
            scope.caller_qn,
            cs.RelationshipType.REFERENCES,
        )

    @_site_scoped
    def _emit_assigned_name_ref(
        self,
        assign_node: Node,
        caller_spec: tuple[str, str, str],
        ensure_rel,
        caller_qn: str | None,
    ) -> None:
        # Resolve the assignment target's simple name (`api.setState` ->
        # setState, `listener` -> listener) to a def-pass registration in the
        # enclosing scope. Registry-guarded: emits only when such a node exists,
        # so a plain data assignment adds nothing.
        self._site_node = assign_node
        if assign_node.type != cs.TS_ASSIGNMENT_EXPRESSION:
            return
        left = assign_node.child_by_field_name(cs.FIELD_LEFT)
        if left is None:
            return
        name_node = (
            left.child_by_field_name(cs.FIELD_PROPERTY)
            if left.type == cs.TS_MEMBER_EXPRESSION
            else left
        )
        if name_node is None or name_node.type not in (
            cs.TS_IDENTIFIER,
            cs.TS_PROPERTY_IDENTIFIER,
        ):
            return
        if not (name := safe_decode_text(name_node)):
            return
        scope_qn = caller_qn or caller_spec[2]
        self._reference_registered(
            caller_spec,
            [f"{scope_qn}{cs.SEPARATOR_DOT}{name}"],
            ensure_rel,
            cs.RelationshipType.REFERENCES,
        )

    @_site_scoped
    def _ingest_jsx_component_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
    ) -> None:
        # `<Card />` renders the Card component: the framework invokes it
        # through the element, never by a call the graph can see, so the JSX
        # usage references the component. Uppercase names only; lowercase
        # tags are HTML elements and must not misbind to same-named locals.
        # The walk stops at nested scope boundaries (each nested function's
        # own pass covers its JSX) but continues THROUGH jsx elements so
        # nested markup is covered by the scope that renders it.
        # Resolve and emit directly rather than via _emit_callback_edge: a
        # class component resolves to a CLASS node whose reference must point
        # at the class itself, but that helper redirects CLASS -> __init__
        # and drops the edge when __init__ is absent, as it always is for a
        # JS/TS class.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self._emit_rel
        scope = _RefEmitScope(
            caller_spec,
            module_qn,
            local_var_types,
            class_context,
            caller_qn,
            resolve_func,
            ensure_rel,
        )
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = self._site_node = stack.pop()
            # Stop at a nested scope that gets its OWN caller pass (a named
            # function/arrow, a class), but continue THROUGH an anonymous arrow
            # (a `.map()`/`cell`/forwardRef callback): those are skipped as
            # callers, so their JSX, rendered on behalf of this scope, would
            # otherwise be scanned by nobody and report as dead.
            if node.type in boundary_types and not self._is_unowned_js_scope(
                node, module_qn
            ):
                continue
            if node.type in _JSX_NAMED_ELEMENT_TYPES:
                self._emit_jsx_element_ref(node, scope)
            elif node.type == cs.TS_JSX_EXPRESSION:
                # A `{...}` attribute value hands its inner expression to the
                # element as a prop. A bare identifier (onClick={handleLogout})
                # or inline arrow (onClick={() => x()}) is a function the
                # framework invokes on the event, so reference it; other
                # expressions resolve to nothing and are skipped by the helper.
                # Peel ONLY a ternary / cast wrapper, whose operands are candidate
                # handler values (onDrop={cond ? this.handleDrop : undefined},
                # issue #980). A short-circuit (`x || y`, `x && y`) is NOT peeled:
                # in a JSX data prop (`alt={name || fallback}`) its operands are
                # data, and expanding them would false-revive a same-named module
                # function.
                self._emit_jsx_expression_refs(node, scope)
            stack.extend(node.children)

    def _emit_jsx_element_ref(self, node: Node, scope: _RefEmitScope) -> None:
        registry = self._resolver.function_registry
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        name_text = safe_decode_text(name_node) if name_node else None
        if not name_text or not name_text[0].isupper():
            return
        # `<Prim.Item>` resolves `Prim` first: a package namespace renders the
        # package's own member. The resolver is called without a language
        # here, so the JS/TS member-call gate never sees the dotted tag and
        # the bare-name fallback would bind `Item` anywhere in the project.
        if self._resolver.js_member_head_is_external(name_text, scope.module_qn):
            return
        resolved = scope.resolve_func(
            name_text,
            scope.module_qn,
            scope.local_var_types,
            scope.class_context,
            scope.caller_qn,
        )
        if not resolved or resolved[0] not in _JSX_COMPONENT_TARGET_TYPES:
            return
        res_type, res_qn = resolved
        for target_qn in registry.variants(res_qn):
            scope.ensure_rel(
                scope.caller_spec,
                cs.RelationshipType.REFERENCES,
                (res_type, cs.KEY_QUALIFIED_NAME, target_qn),
            )

    def _emit_jsx_expression_refs(self, node: Node, scope: _RefEmitScope) -> None:
        for value in node.named_children:
            for operand in self._jsx_value_operands(value):
                self._emit_scope_ref(scope, operand)

    @staticmethod
    def _jsx_value_operands(value: Node) -> list[Node]:
        # The candidate handler operands of a JSX `{...}` value: a ternary's two
        # result branches (`cond ? a : b`), peeled through any transparent TS
        # cast wrapper (`(cond ? a : b) as any`). A short-circuit or any other
        # expression is returned as-is (a single operand) so it is resolved
        # directly, never peeled -- expanding `||`/`&&`/`??` in a JSX data prop
        # would false-revive a same-named module function (issue #980).
        node = value
        while node.type in cs.TS_CAST_WRAPPER_TYPES and node.named_children:
            node = node.named_children[0]
        if node.type in (cs.TS_PY_CONDITIONAL_EXPRESSION, cs.TS_JS_TERNARY_EXPRESSION):
            return _conditional_result_operands(node, is_dart=False)
        return [node]

    @_site_scoped
    def _ingest_returned_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> None:
        # A function handed back via `return` (a useEffect cleanup
        # `return () => unsubscribe()`, a factory `return handler`) is invoked by
        # whoever receives it, never by a visible call. Reference it from the
        # returning scope. Walk continues through anonymous arrows (the effect
        # callback is anonymous, so its `return` bubbles here) but stops at named
        # nested functions, which own their returns.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self._emit_rel
        # An expression-bodied arrow (`const persistImpl = (config) =>
        # (set, get, api) => {...}`, zustand's curried middleware shape) has NO
        # return_statement; its body IS the implicit return. Reference the inner
        # function directly, both on the caller itself and on any unowned anon
        # arrow the walk continues through (deeper currying bubbles here too).
        self._emit_expression_body_return(
            caller_node,
            caller_spec,
            ensure_rel,
            caller_qn,
            module_qn,
            local_var_types,
            class_context,
            resolve_func,
        )
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = self._site_node = stack.pop()
            if node.type in boundary_types:
                if not self._is_unowned_js_scope(node, module_qn):
                    continue
                self._emit_expression_body_return(
                    node,
                    caller_spec,
                    ensure_rel,
                    caller_qn,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                )
            if node.type == cs.TS_RETURN_STATEMENT:
                for value in node.named_children:
                    # A returned TUPLE hides its function elements one level
                    # down (`return _load_field, (...)` in django Field's
                    # __reduce__: pickle later calls the first element);
                    # expand containers so each function handed back inside
                    # one is referenced like a bare return.
                    for expanded in self._expand_py_first_class_values(value, language):
                        self._emit_callback_edge(
                            caller_spec,
                            expanded,
                            module_qn,
                            local_var_types,
                            class_context,
                            resolve_func,
                            ensure_rel,
                            caller_qn=caller_qn,
                            rel_type=cs.RelationshipType.REFERENCES,
                        )
            stack.extend(node.children)

    def _expand_py_first_class_values(
        self,
        value: Node,
        language: cs.SupportedLanguage | None = None,
        opened: set[Node] | None = None,
    ) -> list[Node]:
        # Peel Python container literals and result-position conditional
        # operands so a function stored in a tuple/list/set, a dict VALUE,
        # a bare return-tuple (expression_list), or a ternary/boolean-default
        # branch is treated like a bare first-class value; nesting expands
        # recursively. A ternary's condition is only truthiness-tested, never
        # bound, so it stays excluded. Any other node comes back unchanged, so
        # non-Python shapes are unaffected. `opened`, when given, collects
        # every container peeled, so a walk that meets one again later knows
        # its values are already out.
        is_dart = language == cs.SupportedLanguage.DART
        is_js_ts = language in cs.JS_TS_LANGUAGES
        out: list[Node] = []
        stack = [value]
        while stack:
            node = stack.pop()
            children = _first_class_value_children(node, is_dart, is_js_ts)
            if children is None:
                out.append(node)
            else:
                if opened is not None:
                    opened.add(node)
                stack.extend(reversed(children))
        return out

    def _emit_expression_body_return(
        self,
        func_node: Node,
        caller_spec: tuple[str, str, str],
        ensure_rel,
        caller_qn: str | None,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func,
    ) -> None:
        body = func_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return
        if body.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
            self._emit_inline_arg_function_ref(
                body,
                caller_spec,
                ensure_rel,
                caller_qn,
                cs.RelationshipType.REFERENCES,
                module_qn=module_qn,
            )
        elif body.type in _ASSIGNMENT_RHS_REF_TYPES:
            # A bare-name implicit return (`() => noop`) hands back an EXISTING
            # function by reference; resolve it like an explicit `return noop`
            # instead of treating the arrow's body as an inline declaration.
            # BUT a body rooted at one of the arrow's OWN parameters
            # (`(noop) => noop`, `(x) => x.foo`) names the parameter, which
            # shadows any same-named module function; resolving it would
            # fabricate a false edge, so skip it.
            if (
                func_node.type in js_bindings.INLINE_FUNC_VALUE_TYPES
                and self._body_root_identifier(body) in js_ts_parameter_names(func_node)
            ):
                return
            self._emit_callback_edge(
                caller_spec,
                body,
                module_qn,
                local_var_types,
                class_context,
                resolve_func,
                ensure_rel,
                caller_qn=caller_qn,
                rel_type=cs.RelationshipType.REFERENCES,
            )

    @staticmethod
    def _body_root_identifier(node: Node) -> str | None:
        # The leading identifier of a reference body: `noop` -> "noop",
        # `x.foo.bar` / `x->foo` -> "x". Peels member/attribute/selector wrappers
        # to their object/operand so a member chain rooted at a parameter is
        # recognised. Returns None when the leftmost element is not an identifier.
        current: Node | None = node
        while current is not None:
            if current.type == cs.TS_PY_IDENTIFIER:
                return safe_decode_text(current)
            current = current.child_by_field_name(cs.FIELD_OBJECT)
        return None

    @_site_scoped
    def _ingest_collection_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        language: cs.SupportedLanguage | None = None,
    ) -> None:
        # A function/method placed as a value in a dict/object or list/array literal
        # is a dispatch table wired to be invoked later (handlers[key](...)),
        # commonly dispatched by a dynamic string key or in another module where the
        # call site is not statically resolvable. Treat each such reference as a call
        # from the enclosing scope so the handler is reachable. The walk stops at
        # nested function/class boundaries, so a table built inside a nested scope
        # is attributed to that scope's own pass, EXCEPT an unowned JS/TS arrow (a
        # Promise executor, a `.forEach` callback), which gets no caller pass; its
        # calls bubble to this scope, so its object tables (a `defineProperty`
        # getter descriptor) must be scanned here too or the callbacks inside are
        # orphaned and report as dead.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self._emit_rel
        scope = _RefEmitScope(
            caller_spec,
            module_qn,
            local_var_types,
            class_context,
            caller_spec[2],
            resolve_func,
            ensure_rel,
        )
        # Containers an enclosing literal's expansion already peeled. Each
        # expansion covers the whole nested literal, so expanding every
        # nested level again as the walk reached it made a deeply nested
        # literal O(n^2) (#2262); the walk still descends into them.
        opened: set[Node] = set()
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = self._site_node = stack.pop()
            if node.type in boundary_types and not self._is_unowned_js_scope(
                node, module_qn
            ):
                continue
            if node.type in _DICT_LIKE_COLLECTION_TYPES:
                for pair in node.named_children:
                    self._site_node = pair
                    self._ingest_dict_entry_ref(node, pair, scope, language, opened)
            elif node.type in _SEQUENCE_LIKE_COLLECTION_TYPES and node not in opened:
                for element in node.named_children:
                    self._emit_expanded_value_refs(element, scope, language, opened)
            stack.extend(node.children)

    def _ingest_dict_entry_ref(
        self,
        node: Node,
        pair: Node,
        scope: _RefEmitScope,
        language: cs.SupportedLanguage | None,
        opened: set[Node],
    ) -> None:
        # An object-literal SHORTHAND METHOD (`return { then(x)
        # {...}, catch(x) {...} }`, persist's thenable) is a stored
        # callable like a pair value, but it is a method_definition
        # node, not a pair; reference it by name so it is not dead
        # unless the repo never calls it AND never hands the object
        # out (it cannot know the consumer).
        if pair.type == cs.TS_METHOD_DEFINITION:
            self._emit_shorthand_method_ref(
                pair, scope.caller_spec, scope.module_qn, scope.ensure_rel
            )
            return
        # A SHORTHAND property (`{ retryStrategy }` == `retryStrategy:
        # retryStrategy`): the identifier is both key and value, so it
        # can name a first-class function value to reference. ponytail:
        # a local data var shadowing a same-named module function is
        # over-referenced (the resolver is name-based), matching the
        # existing explicit-pair path; a per-scope shadow check is the
        # upgrade path if it ever matters.
        if pair.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER:
            self._emit_scope_ref(scope, pair)
            return
        if (
            pair.type == cs.TS_PY_PAIR
            and (value := pair.child_by_field_name(cs.FIELD_VALUE)) is not None
        ):
            if value.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
                self._emit_inline_value_function_ref(
                    pair, value, scope.caller_spec, scope.module_qn, scope.ensure_rel
                )
                return
            # A table VALUE wrapped in parens or a ternary
            # (django SQLCompiler's `"local_setter": (partial(...)
            # if ... else local_setter_noop)`) hides the handler
            # candidates one level down; expand before emitting.
            if node in opened:
                return
            self._emit_expanded_value_refs(value, scope, language, opened)

    def _emit_expanded_value_refs(
        self,
        value: Node,
        scope: _RefEmitScope,
        language: cs.SupportedLanguage | None,
        opened: set[Node],
    ) -> None:
        for expanded in self._expand_py_first_class_values(value, language, opened):
            self._emit_value_function_ref(
                expanded,
                scope.caller_spec,
                scope.module_qn,
                scope.local_var_types,
                scope.class_context,
                scope.resolve_func,
                scope.ensure_rel,
                scope.caller_qn,
            )

    @_site_scoped
    def _ingest_c_function_value_references(
        self,
        scope_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        boundary_types: frozenset[str],
    ) -> None:
        # C's callbacks, vtables and ops tables store a function in a pointer
        # and call it through the pointer, a call the graph never sees (issue
        # #2529). The site that stores it references it instead, as Go's func
        # maps and C#/Dart's passed method groups do, so the REFERENCES edge
        # keeps it reachable for dead-code and the call graph.
        for ident in c_function_value_identifiers(scope_node, boundary_types):
            name = safe_decode_text(ident)
            if not name:
                continue
            targets, resolution = self._c_function_value_targets(name, module_qn)
            # A parameter, local or local enumeration constant of that name
            # hides the function.
            if (
                not targets
                or name in cpp_utils.cpp_enclosing_function_value_names(ident)
                or name in local_enumerator_names(ident)
            ):
                continue
            self._site_node = ident
            self._resolution = resolution
            for target_qn in targets:
                self._emit_rel(
                    caller_spec,
                    cs.RelationshipType.REFERENCES,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, target_qn),
                )

    def _c_function_value_targets(
        self, name: str, module_qn: str
    ) -> tuple[list[str], str]:
        registry = self._resolver.function_registry
        # The file's own definition is what the name denotes, static or not.
        own_qn = f"{module_qn}{cs.SEPARATOR_DOT}{name}"
        if registry.get(own_qn) == cs.NodeLabel.FUNCTION:
            variants = registry.variants(own_qn)
            return variants, (
                cs.EdgeResolution.OVERLOAD
                if len(variants) > 1
                else cs.EdgeResolution.EXACT
            )
        # A variable or enumeration constant this file or a header it
        # includes declares is what the name denotes (#2593).
        if any(
            name in self._c_file_scope(visible).objects
            for visible in (module_qn, *self._c_include_closure(module_qn))
        ):
            return [], cs.EdgeResolution.EXACT
        # Otherwise it is an extern declaration the linker binds by name to a
        # definition with external linkage, which no file's `static` has.
        targets = [
            variant
            for qn in registry.find_ending_with(name)
            if registry.get(qn) == cs.NodeLabel.FUNCTION
            and self._c_links_externally(qn, name, module_qn)
            for variant in registry.variants(qn)
        ]
        return targets, cs.EdgeResolution.HEURISTIC

    def _c_links_externally(self, qn: str, name: str, referrer_qn: str) -> bool:
        module_qn = qn[: -len(name) - 1]
        file_path = self.module_qn_to_file_path.get(module_qn)
        # A namespace or class member, or a definition in another language.
        if file_path is None or not file_path.name.endswith(
            cs.C_EXTENSIONS + cs.CPP_EXTENSIONS
        ):
            return False
        internal = name in self._c_file_scope(module_qn).tu_local_functions
        if file_path.name.endswith(cs.C_CPP_SOURCE_EXTENSIONS):
            return not internal
        # A header's `static inline` is compiled into the files including it
        # and no other (#2593); a plain definition there links
        # externally like a source file's.
        return not internal or module_qn in self._c_include_closure(referrer_qn)

    def _c_include_closure(self, module_qn: str) -> frozenset[str]:
        if (cached := self._c_include_closures.get(module_qn)) is not None:
            return cached
        imports = self._resolver.import_processor
        import_mapping = imports.import_mapping
        # A header a later include displaced from its local name is still
        # included, so its `static inline`s compile into this file too.
        displaced = imports.cpp.displaced_include_targets()
        reached: set[str] = set()
        pending = [module_qn]
        while pending:
            current = pending.pop()
            for target in (
                *(import_mapping.get(current) or {}).values(),
                *displaced.get(current, ()),
            ):
                if target not in reached and target in self.module_qn_to_file_path:
                    reached.add(target)
                    pending.append(target)
        reached.discard(module_qn)
        closure = frozenset(reached)
        self._c_include_closures[module_qn] = closure
        return closure

    def _c_file_scope(self, module_qn: str) -> CFileScope:
        if (cached := self._c_file_scopes.get(module_qn)) is not None:
            return cached
        scope = CFileScope(frozenset(), frozenset())
        file_path = self.module_qn_to_file_path.get(module_qn)
        if file_path is not None and (
            entry := self._resolver.type_inference.ast_cache.load(file_path)
        ):
            scope = c_file_scope(entry[0])
        self._c_file_scopes[module_qn] = scope
        return scope

    @_site_scoped
    def _emit_value_function_ref(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func,
        ensure_rel,
        caller_qn: str | None = None,
    ) -> None:
        # A value cast for typing (`persistImpl as unknown as Persist`) is
        # transparent for reference resolution, and `fn.bind(ctx)` /
        # `fn.call(...)` / `fn.apply(...)` in value position (onError:
        # handleError.bind(toast)) hands off `fn`; peel both to a fixpoint.
        self._site_node = node
        node = self._peel_bound_callable(node)
        # An inline arrow/function-expression element (`handlers = [() => {}]`,
        # zod's `onattach = [(inst) => {...}]`) is registered anonymously in the
        # enclosing scope and named after no identifier, so resolve_func cannot
        # find it; reference it by position exactly like an inline call argument.
        if node.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
            self._emit_inline_arg_function_ref(
                node,
                caller_spec,
                ensure_rel,
                caller_spec[2],
                cs.RelationshipType.REFERENCES,
                module_qn,
            )
            return
        # Only a bare name / attribute / member-expression in value position names
        # a function; a call, comprehension or literal is not a reference to a
        # callable. Reuses the flow-arg ref types (identifier, Python attribute,
        # Go selector, JS/TS member expression).
        if node.type not in _FLOW_ARG_REF_TYPES:
            return
        # Resolved in the enclosing scope, as a call or a callback argument
        # is: a function declared a few lines above binds before a same-named
        # one in an unimported module (issue #3199).
        self._emit_callback_edge(
            caller_spec,
            node,
            module_qn,
            local_var_types,
            class_context,
            resolve_func,
            ensure_rel,
            caller_qn,
        )

    def _unwrap_ts_value(self, node: Node) -> Node:
        # Peel TS casts AND parens, interleaved (`((s) => {...}) as SetState`),
        # down to the wrapped value.
        current = node
        while current.type in _TS_BINDING_WRAPPER_TYPES:
            inner = current.named_child(0)
            if inner is None:
                break
            current = inner
        return current

    def _unwrap_ts_cast(self, node: Node) -> Node:
        # Peel TS cast wrappers (`x as T`, `x satisfies T`, `x!`) to the wrapped
        # value; they are transparent for reference resolution. The wrapped value
        # is the first named child; casts nest (`x as unknown as T`), so loop.
        current = node
        while current.type in cs.TS_CAST_WRAPPER_TYPES:
            inner = current.named_child(0)
            if inner is None:
                break
            current = inner
        return current

    def _peel_bound_callable(self, node: Node, peel_parens: bool = False) -> Node:
        # Iterate cast (and optionally paren) unwraps with the bound-call
        # unwrap to a FIXPOINT: `(handler as any).bind(null)` interleaves a
        # cast INSIDE the bind receiver and `h.bind(a).bind(b)` chains, so
        # a single pass of either unwrap leaves a wrapper behind.
        while True:
            node = (
                self._unwrap_ts_value(node)
                if peel_parens
                else self._unwrap_ts_cast(node)
            )
            bound = self._unwrap_bound_function(node)
            if bound is None:
                return node
            node = bound

    def _unwrap_bound_function(self, node: Node) -> Node | None:
        # For `fn.bind(ctx)` (a call_expression whose function is `fn.bind`),
        # return the bound function `fn` (the member object) so the value is
        # referenced as `fn`, not the Function.prototype builtin. call/apply use
        # the function the same way. Returns None when the node is not such a call.
        if node.type != cs.TS_CALL_EXPRESSION:
            return None
        fn = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if fn is None or fn.type != cs.TS_MEMBER_EXPRESSION:
            return None
        prop = fn.child_by_field_name(cs.FIELD_PROPERTY)
        if (
            prop is None
            or safe_decode_text(prop) not in cs.JS_FUNCTION_PROTOTYPE_METHODS
        ):
            return None
        return fn.child_by_field_name(cs.FIELD_OBJECT)

    @_site_scoped
    def _emit_inline_value_function_ref(
        self,
        pair: Node,
        value: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        ensure_rel,
    ) -> None:
        # An inline arrow/function-expression object value is registered by the
        # definition pass under {enclosing_scope}.<name>. An identifier key names it
        # by the key (scope.onSuccess); a string-literal key ({'onSuccess': ...})
        # has no property name, so it registers as scope.anonymous_<row>_<col> from
        # the value's position. Reference every candidate actually registered
        # (variants cover same-name duplicates in one scope).
        self._site_node = value
        names = [f"{cs.PREFIX_ANONYMOUS}{value.start_point[0]}_{value.start_point[1]}"]
        key_node = pair.child_by_field_name(cs.FIELD_KEY)
        if (
            key_node is not None
            and key_node.type in (cs.TS_PROPERTY_IDENTIFIER, cs.TS_IDENTIFIER)
            and (key := safe_decode_text(key_node))
        ):
            names.append(key)
            # A value nested under ANOTHER pair-arrow (`{ onCreated: (s) => {
            # s.setEvents({ compute: ... }) } }`) registers under the pair-key
            # PATH (scope.onCreated.compute); prefix the ancestor pair keys so
            # the candidate matches the def pass's qn.
            if pair_path := self._ancestor_pair_key_path(pair):
                names.append(f"{pair_path}{cs.SEPARATOR_DOT}{key}")
        # A NAMED function expression value (`get: function getrouter() {...}`,
        # express) registers by its OWN name; neither the key nor the position
        # form matches it, so try the name under the scope and module-flat
        # (where the def pass puts it).
        name_node = value.child_by_field_name(cs.FIELD_NAME)
        own = safe_decode_text(name_node) if name_node is not None else None
        if own:
            names.append(own)
        candidates = _scoped_candidates(caller_spec[2], names)
        if own:
            candidates.add(f"{module_qn}{cs.SEPARATOR_DOT}{own}")
        self._reference_registered(
            caller_spec, candidates, ensure_rel, cs.RelationshipType.REFERENCES
        )

    def _ancestor_pair_key_path(self, pair: Node) -> str | None:
        # Dotted key path of the ENCLOSING pairs above this one (`compute` inside
        # `onCreated: (s) => ...` -> "onCreated"), outermost first; None when the
        # pair has no pair ancestors. Registry-guarded by the caller, so an
        # over-collected path (ancestors above the scanning scope) just misses.
        keys: list[str] = []
        current = pair.parent
        while current is not None:
            if current.type == cs.TS_PY_PAIR:
                key_node = current.child_by_field_name(cs.FIELD_KEY)
                if (
                    key_node is not None
                    and key_node.type in (cs.TS_PROPERTY_IDENTIFIER, cs.TS_IDENTIFIER)
                    and (key := safe_decode_text(key_node))
                ):
                    keys.append(key)
            current = current.parent
        if not keys:
            return None
        keys.reverse()
        return cs.SEPARATOR_DOT.join(keys)

    @_site_scoped
    def _emit_shorthand_method_ref(
        self,
        method_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        ensure_rel,
    ) -> None:
        # The def pass registers a shorthand method by NAME at MODULE scope
        # (persist's thenable `catch` -> `...middleware.persist.catch`), while
        # this scan runs per enclosing caller; try both scopes, plus the
        # position form used when the name is taken.
        self._site_node = method_node
        name_node = method_node.child_by_field_name(cs.FIELD_NAME)
        names = [
            f"{cs.PREFIX_ANONYMOUS}"
            f"{method_node.start_point[0]}_{method_node.start_point[1]}"
        ]
        name = safe_decode_text(name_node) if name_node is not None else None
        if name:
            names.append(name)
        candidates = _scoped_candidates(caller_spec[2], names)
        if name:
            candidates.add(f"{module_qn}{cs.SEPARATOR_DOT}{name}")
        self._reference_registered(
            caller_spec, candidates, ensure_rel, cs.RelationshipType.REFERENCES
        )

    @_site_scoped
    def _ingest_default_param_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        caller_qn: str | None,
    ) -> None:
        params = caller_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if params is None:
            return
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self._emit_rel
        for param in params.named_children:
            self._site_node = param
            # TS carries a param default in required_parameter's `value` field;
            # plain JS wraps the param in an assignment_pattern whose default
            # sits under `right`. Scan both forms.
            value = param.child_by_field_name(
                cs.FIELD_VALUE
            ) or param.child_by_field_name(cs.FIELD_RIGHT)
            if value is None:
                continue
            value = self._unwrap_ts_value(value)
            if (
                value.type in _ASSIGNMENT_RHS_REF_TYPES
                or value.type in js_bindings.INLINE_FUNC_VALUE_TYPES
            ):
                self._emit_callback_edge(
                    caller_spec,
                    value,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    ensure_rel,
                    caller_qn,
                    cs.RelationshipType.REFERENCES,
                )

    def _ingest_inline_call_arg_references(
        self,
        call_node: Node,
        caller_spec: tuple[str, str, str],
        ensure_rel,
        caller_qn: str | None,
        module_qn: str | None = None,
    ) -> None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if args is None:
            return
        for arg in args.named_children:
            value = self._unwrap_ts_value(arg)
            if value.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
                self._emit_inline_arg_function_ref(
                    value,
                    caller_spec,
                    ensure_rel,
                    caller_qn,
                    cs.RelationshipType.REFERENCES,
                    module_qn=module_qn,
                )

    @_site_scoped
    def _emit_inline_arg_function_ref(
        self,
        arg_node: Node,
        source_spec: tuple[str, str, str],
        ensure_rel,
        caller_qn: str | None = None,
        rel_type: cs.RelationshipType = cs.RelationshipType.REFERENCES,
        module_qn: str | None = None,
    ) -> None:
        # The node's OWN source span records the qn the definition pass minted,
        # so resolve by span first when the module is known: it is authoritative
        # across the two ways caller_qn can mislead a name/position guess. An
        # object-literal method nested in a bound arrow is named FLAT (`mod.
        # update`, dropping the nameless `make`) while the function it contains
        # nests a level deeper (`mod.make.update.anonymous_R_C`), so no
        # `{caller_qn}.anonymous_R_C` candidate matches (issue #985); and a NESTED
        # named function-expression (`mod.make.update.handler`) whose name
        # collides with a module-level function would otherwise match the
        # module-flat name candidate and reference the WRONG top-level node. The
        # candidate fallback is kept for an incremental run where an unchanged
        # file's span was not re-recorded this pass.
        self._site_node = arg_node
        if (
            module_qn is not None
            and (loc := self._recorded_caller(arg_node, module_qn)) is not None
        ):
            ensure_rel(
                source_spec,
                rel_type,
                (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, loc.qualified_name),
            )
            return
        self._emit_inline_ref_candidates(
            arg_node, source_spec, ensure_rel, caller_qn, rel_type, module_qn
        )

    def _emit_inline_ref_candidates(
        self,
        arg_node: Node,
        source_spec: tuple[str, str, str],
        ensure_rel,
        caller_qn: str | None,
        rel_type: cs.RelationshipType,
        module_qn: str | None,
    ) -> None:
        # An inline arrow/function-expression call argument is registered by the
        # definition pass as {enclosing_scope}.anonymous_<row>_<col> from its own
        # start position. The anonymous node lives in the CALLER's scope, so build
        # the candidate from caller_qn (source_spec[2] is the callee for the
        # callable-param path). Registry guard skips unregistered names.
        scope_qn = caller_qn or source_spec[2]
        suffixes = [
            f"{cs.SEPARATOR_DOT}{cs.PREFIX_ANONYMOUS}"
            f"{arg_node.start_point[0]}_{arg_node.start_point[1]}"
        ]
        # A NAMED function expression argument (`this.on('mount', function
        # onmount(parent) {...})`, express) registers by its own NAME; the
        # position form never matches it, so try the name too, both under the
        # caller scope and module-flat (where the def pass puts it).
        name_node = arg_node.child_by_field_name(cs.FIELD_NAME)
        named = safe_decode_text(name_node) if name_node is not None else None
        if named:
            suffixes.append(f"{cs.SEPARATOR_DOT}{named}")
        # A duplicate-variant caller (a TS overload implementation registers as
        # `useStore@27`) owns anons the def pass registers under the NATURAL qn
        # (`useStore.anonymous_R_C`); try the variant-stripped scope too.
        scopes = _scope_qn_candidates(scope_qn)
        if named and module_qn is not None and module_qn not in scopes:
            scopes = [*scopes, module_qn]
        self._reference_registered(
            source_spec,
            [f"{scope}{suffix}" for scope in scopes for suffix in suffixes],
            ensure_rel,
            rel_type,
        )

    def _reference_registered(
        self,
        source_spec: tuple[str, str, str],
        candidates: Iterable[str],
        ensure_rel,
        rel_type: cs.RelationshipType,
    ) -> None:
        # Only a name the definition pass registered gets an edge, each of its
        # duplicate variants included, so an over-collected candidate costs a
        # lookup and never a dangling relationship.
        registry = self._resolver.function_registry
        for candidate in candidates:
            for target_qn in registry.variants(candidate):
                if registry.get(target_qn) is None:
                    continue
                ensure_rel(
                    source_spec,
                    rel_type,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, target_qn),
                )

    @staticmethod
    def _flow_scope_boundaries(lang_config: LanguageSpec) -> frozenset[str]:
        # A nested function/class scope; a scan of a function body must not descend
        # into one, since its returns and local bindings belong to it, not the
        # enclosing scope. The Python set is unioned in so Python keeps its exact
        # prior behaviour (its decorated_definition wrapper is not a config type).
        return (
            _PY_SCOPE_BOUNDARY_TYPES
            | frozenset(lang_config.function_node_types)
            | frozenset(lang_config.class_node_types)
        )

    def _collect_returned_callables(
        self,
        caller_node: Node,
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
    ) -> None:
        # Record which functions/closures this function may return, so a call site
        # that binds and invokes the returned value (x = factory(); x(cb)) can flow
        # cb into the returned closure. Only this scope's own return statements
        # count; a nested function's returns belong to it.
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types:
                continue
            if node.type == cs.TS_PY_RETURN_STATEMENT:
                for name in self._returned_names(node):
                    self._record_returned_callable(
                        name, caller_qn, module_qn, local_var_types, class_context
                    )
            stack.extend(node.children)

    def _returned_names(self, return_node: Node) -> list[str]:
        # The identifier / attribute names a return statement hands back.
        names: list[str] = []
        for returned in return_node.named_children:
            child = self._unwrap_ts_cast(returned)
            if child.type in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE) and (
                name := safe_decode_text(child)
            ):
                names.append(name)
        return names

    def _record_returned_callable(
        self,
        name: str,
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
    ) -> None:
        registry = self._resolver.function_registry
        nested_qn = f"{caller_qn}{cs.SEPARATOR_DOT}{name}"
        # A duplicated name (if/else twin definitions) returns whichever
        # branch ran, so record EVERY twin; recording one leaves the others
        # unreachable and falsely dead.
        if nested_qn in registry:
            target_qn = nested_qn
        else:
            resolved = self._resolver.resolve_function_call(
                name, module_qn, local_var_types, class_context, caller_qn
            )
            if resolved is None or resolved[0] not in _CALLABLE_NODE_LABELS:
                return
            target_qn = resolved[1]
        self._flow_bucket.returned_callables.setdefault(caller_qn, set()).update(
            registry.variants(target_qn)
        )

    def _build_factory_alias_map(
        self,
        caller_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
    ) -> dict[str, str]:
        # Map a local `x` to the function `factory` in `x = factory(...)`, so a
        # later `x(cb)` can be traced through factory's returned closure. Handles
        # each flow language's binding form (Python assignment, JS/TS
        # variable_declarator, Go short_var_declaration, C++ init_declarator).
        resolve_func = self._resolver.resolve_function_call
        aliases: dict[str, str] = {}
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types:
                continue
            for var, fn_name in self._factory_bindings(node):
                if (
                    resolved := resolve_func(
                        fn_name, module_qn, local_var_types, class_context, caller_qn
                    )
                ) is not None:
                    aliases.setdefault(var, resolved[1])
            stack.extend(node.children)
        return aliases

    def _factory_bindings(self, node: Node) -> list[tuple[str, str]]:
        # Yield (local_name, called_function_name) for a `x = f(...)` binding node.
        match node.type:
            case cs.TS_PY_ASSIGNMENT:
                return self._simple_factory_binding(
                    node, cs.TS_FIELD_LEFT, cs.TS_FIELD_RIGHT, cs.TS_PY_CALL
                )
            case cs.TS_VARIABLE_DECLARATOR:
                return self._simple_factory_binding(
                    node, cs.TS_FIELD_NAME, cs.FIELD_VALUE, cs.TS_CALL_EXPRESSION
                )
            case cs.CppNodeType.INIT_DECLARATOR:
                return self._simple_factory_binding(
                    node, cs.TS_FIELD_DECLARATOR, cs.FIELD_VALUE, cs.TS_CALL_EXPRESSION
                )
            case cs.TS_GO_SHORT_VAR_DECLARATION:
                return go_calls.factory_bindings(node)
            case _:
                return []

    def _simple_factory_binding(
        self, node: Node, name_field: str, value_field: str, call_type: str
    ) -> list[tuple[str, str]]:
        left = node.child_by_field_name(name_field)
        right = node.child_by_field_name(value_field)
        # `const x = factory(...) as T` wraps the call in a cast; unwrap so the
        # factory binding is still recognised.
        if right is not None:
            right = self._unwrap_ts_cast(right)
        if (
            left is not None
            and left.type == cs.TS_PY_IDENTIFIER
            and right is not None
            and right.type == call_type
            and (var := safe_decode_text(left))
            and (fn := right.child_by_field_name(cs.TS_FIELD_FUNCTION)) is not None
            and (fn_name := safe_decode_text(fn))
        ):
            return [(var, fn_name)]
        return []

    def _resolve_callback_qn(
        self,
        node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        caller_qn: str | None = None,
    ) -> str | None:
        if node.type not in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
            return None
        if not (text := safe_decode_text(node)):
            return None
        resolved = self._noted(
            self._resolver.resolve_function_call(
                text, module_qn, local_var_types, class_context, caller_qn
            )
        )
        if resolved is None or resolved[0] not in _CALLABLE_NODE_LABELS:
            return None
        return resolved[1]

    def _record_factory_call(
        self,
        call_node: Node,
        scope_qn: str,
        factory_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
    ) -> None:
        positional, keyword = self._parse_call_arguments(call_node)
        pos_qns = tuple(
            self._resolve_callback_qn(
                n, module_qn, local_var_types, class_context, scope_qn
            )
            or ""
            for n in positional
        )
        kw_qns = tuple(
            (name, qn)
            for name, value in keyword.items()
            if (
                qn := self._resolve_callback_qn(
                    value, module_qn, local_var_types, class_context, scope_qn
                )
            )
        )
        if any(pos_qns) or kw_qns:
            self._flow_bucket.factory_calls.append(
                _FactoryCall(scope_qn, factory_qn, pos_qns, kw_qns)
            )

    def _parse_call_arguments(
        self, call_node: Node
    ) -> tuple[list[Node], dict[str, Node]]:
        args_node = _find_call_arguments_node(call_node)
        if args_node is None:
            return [], {}
        return _split_call_arguments(args_node)

    @_site_scoped
    def _emit_callback_edge(
        self,
        source_spec: tuple[str, str, str],
        arg_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func,
        ensure_rel,
        caller_qn: str | None = None,
        rel_type: cs.RelationshipType = cs.RelationshipType.CALLS,
        language: cs.SupportedLanguage | None = None,
    ) -> None:
        # A TS cast (`handler as any`, `fn satisfies T`, `cb!`) is transparent
        # for reference resolution, and `fn.bind(ctx)` / `.call` / `.apply` in
        # argument position (addEventListener("click", handler.bind(this)),
        # django admin's inlines.js) hands off `fn` while the call itself
        # resolves to the Function.prototype builtin; peel both to a fixpoint.
        self._site_node = arg_node
        arg_node = self._peel_bound_callable(arg_node)
        # An arrow/function-expression passed DIRECTLY as a call argument
        # (useCallback(() => {}), setTimeout(() => {}), arr.map(x => ...)) is
        # registered anonymously in the enclosing scope but named after no
        # identifier, so resolve_func cannot find it. The call consumes it, so
        # reference it by position the same way inline object-literal values are.
        if arg_node.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
            self._emit_inline_arg_function_ref(
                arg_node, source_spec, ensure_rel, caller_qn, rel_type, module_qn
            )
            return
        if self._cpp.emit_functor_callback(
            arg_node, source_spec, module_qn, local_var_types, ensure_rel, language
        ):
            return
        # Only a name can hand a callable over. The whole source text of any
        # other argument (`items.length - start`, `-x`, `xs[0]`) was resolved
        # as if it were one (issue #2438).
        if not _names_a_callable(arg_node):
            return
        if not (arg_text := safe_decode_text(arg_node)):
            return
        if language == cs.SupportedLanguage.CSHARP:
            # `Callback<int>` passes the method group with explicit type
            # arguments; methods register generic-free, so strip them.
            arg_text = arg_text.split(cs.CHAR_ANGLE_OPEN, 1)[0]
            if self._csharp.emit_method_group(
                arg_node,
                arg_text,
                source_spec,
                module_qn,
                local_var_types,
                caller_qn,
                ensure_rel,
                rel_type,
            ):
                return
        if not (
            resolved := resolve_func(
                arg_text, module_qn, local_var_types, class_context, caller_qn
            )
        ):
            return
        res_type, res_qn = resolved
        resolution = _verdict_of(resolved)
        registry = self._resolver.function_registry
        if res_type == cs.NodeLabel.CLASS:
            constructor = self._class_callback_target(
                source_spec,
                res_qn,
                rel_type,
                ensure_rel,
                module_qn,
                language,
                resolution,
            )
            if constructor is None:
                return
            res_type, res_qn = constructor
        # Only callables are meaningful callback/reference targets: a value can
        # resolve to an Interface/Type node (`selector = identity as Selector`
        # resolves the cast's TYPE name in some paths), and emitting that
        # produces schema-invalid edges.
        if res_type not in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
            return
        # And only a callable the graph has a node for. A member read on a
        # builtin-typed receiver (`error.customDelay` on an Error,
        # `items.length` on an Array) resolves to a synthetic `builtin.*` qn
        # that is never created, a property as often as a method, so the
        # edge could only be dropped by the database and counted as a failed
        # write on every index (issue #2438; #652 for the call form).
        if res_qn not in registry:
            return
        # A Dart getter in argument position is a VALUE READ, not a tear-off:
        # the getter-read pass owns those edges (with shadow handling), so
        # emitting here would fabricate liveness for a local that hides the
        # getter (issue #869).
        if language == cs.SupportedLanguage.DART and registry.is_property(res_qn):
            return
        self._emit_callback_targets(
            source_spec,
            res_type,
            res_qn,
            rel_type,
            ensure_rel,
            module_qn,
            language,
            resolution,
        )

    def _class_callback_target(
        self,
        source_spec: tuple[str, str, str],
        class_qn: str,
        rel_type: cs.RelationshipType,
        ensure_rel: Callable[..., None],
        module_qn: str,
        language: cs.SupportedLanguage | None,
        resolution: str,
    ) -> tuple[str, str] | None:
        # A class passed as a callback is called through its constructor.
        # None when the edges were already fanned out here, or when the
        # class has no `__init__` node to reference.
        registry = self._resolver.function_registry
        if any(
            registry.get(variant) in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
            for variant in registry.variants(class_qn)
        ):
            # A same-named def is passed on with the class (issue
            # #2621); the fan-out gives each variant its own target.
            self._emit_callback_targets(
                source_spec,
                cs.NodeLabel.CLASS,
                class_qn,
                rel_type,
                ensure_rel,
                module_qn,
                language,
                resolution,
            )
            return None
        init_qn = f"{class_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
        if init_qn not in registry:
            return None
        return cs.NodeLabel.METHOD, init_qn

    def _emit_callback_targets(
        self,
        source_spec: tuple[str, str, str],
        res_type: str,
        res_qn: str,
        rel_type: cs.RelationshipType,
        ensure_rel: Callable[..., None],
        module_qn: str,
        language: cs.SupportedLanguage | None,
        resolution: str,
    ) -> None:
        registry = self._resolver.function_registry
        # The callback's OWN verdict, not the enclosing call's: `resolution`
        # is how surely this reference resolved; `self._resolution` still
        # describes the call whose argument it is. Emitting through the stale value labelled a callback
        # found only by trie fallback as exact, so `dead-code --min-resolution
        # exact` kept a target it should have dropped (#1543). Scoped:
        # the enclosing call's label is restored on the way out, so the edge
        # it emits afterwards reads its own verdict back (issue #1526).
        # And, like a primary call, a callback that fans out to same-named
        # candidates is an `overload`: one reference, several targets, none
        # chosen (#1543). The resolver's single-target verdict would
        # otherwise let an exact floor keep every one of them.
        prev_resolution = self._resolution
        targets = registry.variants(res_qn)
        self._resolution = (
            cs.EdgeResolution.OVERLOAD if len(targets) > 1 else resolution
        )
        try:
            for variant in targets:
                # A variant can be another kind than the resolved callback:
                # a Python class beside a same-named def. Written under the
                # callback's label it named no node, so the database dropped
                # it (issue #2621). A class is passed on through its __init__,
                # as `_emit_callback_edge` passes a lone class.
                match registry.get(variant):
                    case cs.NodeLabel.FUNCTION | cs.NodeLabel.METHOD as kind:
                        target_type, target_qn = kind, variant
                    case None:
                        target_type, target_qn = res_type, variant
                    case cs.NodeLabel.CLASS if (
                        init_qn := f"{variant}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
                    ) in registry:
                        target_type, target_qn = cs.NodeLabel.METHOD, init_qn
                    case _:
                        continue
                ensure_rel(
                    source_spec,
                    rel_type,
                    (target_type, cs.KEY_QUALIFIED_NAME, target_qn),
                )
                if language == cs.SupportedLanguage.CSHARP:
                    self._csharp.record_cross_module_use(module_qn, target_qn)
        finally:
            self._resolution = prev_resolution

    def _ingest_callable_param_calls(
        self,
        call_node: Node,
        callee_type: str,
        callee_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func,
        caller_qn: str | None = None,
    ) -> None:
        if not (params := self._resolver.function_registry.callable_params(callee_qn)):
            return
        positional, keyword = self._parse_call_arguments(call_node)
        source_spec = (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn)
        for param_name, index in params.items():
            arg_node = keyword.get(param_name)
            if arg_node is None and index < len(positional):
                arg_node = positional[index]
            if arg_node is not None:
                self._emit_callback_edge(
                    source_spec,
                    arg_node,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    self._passed_callback_recorder(param_name),
                    caller_qn,
                )

    def _passed_callback_recorder(self, param_name: str) -> Callable[..., None]:
        # Stands in for ensure_rel: the edge's source is the callee, which runs
        # the callback where it invokes `param_name`, so the edge waits for
        # finalize and that site instead of taking the argument's, a position
        # in this file rather than the callee's (issue #2459). The verdict is
        # the one the callback resolution set for this edge (#1543).
        passed = self._flow_bucket.passed

        def record(
            source_spec: tuple[str, str, str],
            rel_type: str,
            target_spec: tuple[str, str, str],
        ) -> None:
            passed.append(
                _PassedCallback(
                    source_spec, param_name, rel_type, target_spec, self._resolution
                )
            )

        return record

    def _record_param_invocations(
        self, caller_node: Node, caller_qn: str, language: cs.SupportedLanguage
    ) -> None:
        # Only a function the registry marked as invoking a parameter is ever
        # the source of a callable-parameter edge, so no other body is scanned
        # a second time.
        if not self._resolver.function_registry.callable_params(caller_qn):
            return
        invocations = callable_parameter_invocations(caller_node, language)
        for param_name, nodes in invocations.items():
            self._flow_bucket.invocations[(caller_qn, param_name)] = tuple(
                {**node_site_properties(node), cs.KEY_VIA_PARAM: param_name}
                for node in nodes
            )

    def _collect_callable_flow(
        self,
        call_node: Node,
        callee_qn: str,
        caller_qn: str,
        caller_params: frozenset[str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
    ) -> None:
        # Record, for each call-site argument that names a callable, whether it is a
        # concrete function or a parameter of the caller (a pass-through). The
        # fixpoint in finalize propagates concretes through pass-through params to
        # the functions that actually invoke them.
        positional, keyword = self._parse_call_arguments(call_node)
        items: list[tuple[int, str, Node]] = [
            (index, "", node) for index, node in enumerate(positional)
        ]
        items.extend((-1, name, node) for name, node in keyword.items())
        callable_labels = (
            cs.NodeLabel.FUNCTION,
            cs.NodeLabel.METHOD,
            cs.NodeLabel.CLASS,
        )
        for position, keyword_name, raw_arg in items:
            # `f(callback as any)` casts the argument; unwrap so a cast callable
            # argument still flows.
            # `outer(handler.bind(this))` forwards the bound handler through
            # a pass-through param; peel .bind like the direct-argument path
            # or the propagated flow edge is lost.
            arg_node = self._peel_bound_callable(raw_arg)
            if arg_node.type not in _FLOW_ARG_REF_TYPES:
                continue
            arg_text = safe_decode_text(arg_node)
            if not arg_text:
                continue
            if arg_node.type == cs.TS_PY_IDENTIFIER and arg_text in caller_params:
                self._flow_bucket.args.append(
                    _CallableFlowArg(
                        callee_qn, position, keyword_name, "", caller_qn, arg_text
                    )
                )
                continue
            resolved = self._resolver.resolve_function_call(
                arg_text, module_qn, local_var_types, class_context, caller_qn
            )
            if resolved is not None and resolved[0] in callable_labels:
                self._flow_bucket.args.append(
                    _CallableFlowArg(
                        callee_qn, position, keyword_name, resolved[1], "", ""
                    )
                )

    def finalize_dispatch(self) -> None:
        self._dispatch_registry.finalize()

    def finalize_flow(self) -> None:
        # Resolve deferred FLOWS_TO return-taint once every function body has
        # been walked, so a callee processed after its caller still contributes
        # its return edge and resource flow (issue #712), and compose forward
        # parameter-taint the same way (issue #1142).
        self._flow_processor.finalize()

    @_site_scoped
    def finalize_callable_param_flow(self) -> None:
        # Resolve the recorded call-site argument bindings to a fixpoint and emit a
        # CALLS edge from every function that invokes a callable parameter to each
        # concrete function that can reach it (directly or via pass-through params).
        # No walk is running, so an edge with no recorded site carries none rather
        # than the last node a walk left behind (Go's 1:0-7, issue #2459).
        self._site_node = None
        returned_callables = self._merged_returned_callables()
        seeds, edges = self._callable_flow_slots()
        self._emit_returned_closure_calls(returned_callables)
        self._emit_factory_closure_calls(returned_callables, seeds)
        bindings = self._propagate_flow_bindings(seeds, edges)
        invocations = self._merged_param_invocations()
        self._emit_callable_param_calls(bindings, invocations)
        self._emit_passed_callbacks(invocations)

    def _merged_param_invocations(
        self,
    ) -> dict[tuple[str, str], tuple[PropertyDict, ...]]:
        merged: dict[tuple[str, str], tuple[PropertyDict, ...]] = {}
        for flow in self._callable_flow.values():
            merged.update(flow.invocations)
        return merged

    @staticmethod
    def _param_invocation_sites(
        invocations: dict[tuple[str, str], tuple[PropertyDict, ...]],
        func_qn: str,
        param_name: str,
    ) -> tuple[PropertyDict, ...]:
        # A receiver whose walk recorded no invocation still invokes the
        # parameter (the registry says so): keep the edge, without a site and
        # so without `via_param`, since a site-less row merges on its endpoints
        # alone and would stamp it on a direct call joining the same pair.
        return invocations.get((func_qn, param_name)) or ({},)

    def _emit_passed_callbacks(
        self, invocations: dict[tuple[str, str], tuple[PropertyDict, ...]]
    ) -> None:
        # After the propagated bindings, so where both reach one site the
        # direct argument's own verdict is the one that stays (#1543).
        registry = self._resolver.function_registry
        file_flows = self._callable_flow.values()
        for passed in (p for flow in file_flows for p in flow.passed):
            source_qn = passed.source_spec[2]
            # A record outlives the walk that made it, and either end may have
            # been deleted by a later pass that did not re-walk this file.
            invoked = registry.callable_params(source_qn)
            if (
                not invoked
                or passed.param_name not in invoked
                or passed.target_spec[2] not in registry
            ):
                continue
            self._resolution = passed.resolution
            for site in self._param_invocation_sites(
                invocations, source_qn, passed.param_name
            ):
                self._emit_rel(
                    passed.source_spec, passed.rel_type, passed.target_spec, site
                )

    def _merged_returned_callables(self) -> dict[str, set[str]]:
        returned_callables: dict[str, set[str]] = defaultdict(set)
        for flow in self._callable_flow.values():
            for producer_qn, returned in flow.returned_callables.items():
                returned_callables[producer_qn] |= returned
        return returned_callables

    def _callable_flow_slots(
        self,
    ) -> tuple[
        defaultdict[tuple[str, str], set[str]],
        defaultdict[tuple[str, str], set[tuple[str, str]]],
    ]:
        # Each recorded argument keyed to its callee parameter slot: a concrete
        # function seeds the slot, a pass-through param links it to the caller's.
        seeds: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
        edges: defaultdict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        file_flows = self._callable_flow.values()
        for arg in (arg for flow in file_flows for arg in flow.args):
            if arg.keyword:
                param_name = arg.keyword
            else:
                callee_params = self._flow_param_names.get(arg.callee_qn)
                if callee_params is None or not (
                    0 <= arg.position < len(callee_params)
                ):
                    continue
                param_name = callee_params[arg.position]
            slot = (arg.callee_qn, param_name)
            if arg.source_concrete:
                seeds[slot].add(arg.source_concrete)
            else:
                edges[slot].add((arg.source_caller, arg.source_param))
        return seeds, edges

    def _emit_returned_closure_calls(
        self, returned_callables: dict[str, set[str]]
    ) -> None:
        registry = self._resolver.function_registry
        ensure_rel = self._emit_rel
        # A nested closure a function returns is reachable whenever that function
        # is reached (created and handed back as the return value). Nested
        # functions are no longer roots, so this producer edge keeps a genuinely
        # used closure (a returned decorator/formatter) live without reviving the
        # closures of an unreachable outer function.
        for producer_qn, returned in returned_callables.items():
            producer_type = registry.get(producer_qn)
            if producer_type is None:
                continue
            prefix = f"{producer_qn}{cs.SEPARATOR_DOT}"
            producer_spec = (producer_type, cs.KEY_QUALIFIED_NAME, producer_qn)
            for closure_qn in returned:
                if not closure_qn.startswith(prefix):
                    continue
                closure_type = registry.get(closure_qn)
                if closure_type is None:
                    continue
                ensure_rel(
                    producer_spec,
                    cs.RelationshipType.CALLS,
                    (closure_type, cs.KEY_QUALIFIED_NAME, closure_qn),
                )

    def _emit_factory_closure_calls(
        self,
        returned_callables: dict[str, set[str]],
        seeds: defaultdict[tuple[str, str], set[str]],
    ) -> None:
        file_flows = self._callable_flow.values()
        for fc in (fc for flow in file_flows for fc in flow.factory_calls):
            for closure_qn in returned_callables.get(fc.factory_qn, ()):
                self._emit_factory_closure_call(fc, closure_qn, seeds)

    def _emit_factory_closure_call(
        self,
        fc: _FactoryCall,
        closure_qn: str,
        seeds: defaultdict[tuple[str, str], set[str]],
    ) -> None:
        registry = self._resolver.function_registry
        # The returned closure runs when the alias is called, so it is
        # reachable from the enclosing scope.
        closure_type = registry.get(closure_qn)
        if closure_type is None:
            return
        scope_type = registry.get(fc.scope_qn) or cs.NodeLabel.MODULE
        self._emit_rel(
            (scope_type, cs.KEY_QUALIFIED_NAME, fc.scope_qn),
            cs.RelationshipType.CALLS,
            (closure_type, cs.KEY_QUALIFIED_NAME, closure_qn),
        )
        # Each argument the closure receives seeds its callable parameter,
        # so the callback is reached wherever the closure invokes it.
        closure_params = self._flow_param_names.get(closure_qn)
        for index, callback_qn in enumerate(fc.positional):
            if (
                callback_qn
                and closure_params is not None
                and index < len(closure_params)
            ):
                seeds[(closure_qn, closure_params[index])].add(callback_qn)
        for keyword_name, callback_qn in fc.keyword:
            seeds[(closure_qn, keyword_name)].add(callback_qn)

    @staticmethod
    def _propagate_flow_bindings(
        seeds: defaultdict[tuple[str, str], set[str]],
        edges: defaultdict[tuple[str, str], set[tuple[str, str]]],
    ) -> dict[tuple[str, str], set[str]]:
        bindings: dict[tuple[str, str], set[str]] = {
            k: set(v) for k, v in seeds.items()
        }
        for slot in edges:
            bindings.setdefault(slot, set())
        changed = True
        while changed:
            changed = False
            for slot, sources in edges.items():
                for source in sources:
                    if (reachable := bindings.get(source)) and not reachable.issubset(
                        bindings[slot]
                    ):
                        bindings[slot] |= reachable
                        changed = True
        return bindings

    def _emit_callable_param_calls(
        self,
        bindings: dict[tuple[str, str], set[str]],
        invocations: dict[tuple[str, str], tuple[PropertyDict, ...]],
    ) -> None:
        registry = self._resolver.function_registry
        for func_qn, invoked in (
            (qn, registry.callable_params(qn)) for qn in self._flow_param_names
        ):
            if not invoked or (func_type := registry.get(func_qn)) is None:
                continue
            source_spec = (func_type, cs.KEY_QUALIFIED_NAME, func_qn)
            for param_name in invoked:
                self._emit_bound_target_calls(
                    source_spec,
                    bindings.get((func_qn, param_name), ()),
                    self._param_invocation_sites(invocations, func_qn, param_name),
                )

    def _emit_bound_target_calls(
        self,
        source_spec: tuple[str, str, str],
        target_qns: set[str] | tuple[()],
        sites: tuple[PropertyDict, ...],
    ) -> None:
        registry = self._resolver.function_registry
        for target_qn in target_qns:
            target_type = registry.get(target_qn)
            if target_type is None:
                continue
            for variant in registry.variants(target_qn):
                for site in sites:
                    self._emit_rel(
                        source_spec,
                        cs.RelationshipType.CALLS,
                        (target_type, cs.KEY_QUALIFIED_NAME, variant),
                        site,
                    )

    def _ingest_callable_field_calls(
        self,
        call_name: str,
        caller_spec: tuple[str, str, str],
        local_var_types: dict[str, str] | None,
        ensure_rel,
    ) -> None:
        recv, sep, field = call_name.rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return
        recv_type = local_var_types.get(recv) if local_var_types else None
        targets = self._resolver.callable_field_targets(field, recv_type)
        if not targets:
            return
        registry = self._resolver.function_registry
        for target_qn in targets:
            if target_qn in registry:
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (registry[target_qn], cs.KEY_QUALIFIED_NAME, target_qn),
                )

    def _ingest_higher_order_builtin_calls(
        self,
        call_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func,
        ensure_rel,
        caller_qn: str | None = None,
    ) -> None:
        positional, keyword = self._parse_call_arguments(call_node)
        for arg_node in (*positional, *keyword.values()):
            self._emit_callback_edge(
                caller_spec,
                arg_node,
                module_qn,
                local_var_types,
                class_context,
                resolve_func,
                ensure_rel,
                caller_qn,
            )

    def _ingest_argument_function_references(
        self,
        call_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func,
        ensure_rel,
        caller_qn: str | None = None,
        rel_type: cs.RelationshipType = cs.RelationshipType.CALLS,
        language: cs.SupportedLanguage | None = None,
    ) -> None:
        # A function/method passed as an argument is a first-class value the
        # callee may invoke (external framework) or store for later dynamic
        # dispatch (first-party plumbing); either way the passing scope holds a
        # live reference, so emit an edge to keep the callback reachable.
        # External/builtin callees keep the CALLS edge; first-party callees
        # record the pass as REFERENCES (the precise invocation edge, if any,
        # comes from callable-param flow).
        positional, keyword = self._parse_call_arguments(call_node)
        for arg_node in (*positional, *keyword.values()):
            # A container-literal or conditional argument
            # (`validators=[_simple_domain_name_validator]`, django's Site;
            # `... if single else local_setter_noop`, its SQLCompiler) hides
            # the passed functions one level down; each expanded value is a
            # first-class reference exactly like a bare-name argument.
            for value_node in self._expand_py_first_class_values(arg_node, language):
                self._emit_callback_edge(
                    caller_spec,
                    value_node,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    ensure_rel,
                    caller_qn,
                    rel_type,
                    language,
                )

    def _build_local_alias_map(
        self, caller_node: Node, lang_config: LanguageSpec, module_qn: str
    ) -> dict[str, str]:
        identifier = cs.TS_PY_IDENTIFIER
        attribute = cs.TS_PY_ATTRIBUTE
        assignment = cs.TS_PY_ASSIGNMENT
        left_field = cs.TS_FIELD_LEFT
        right_field = cs.TS_FIELD_RIGHT
        function_types = lang_config.function_node_types
        class_types = lang_config.class_node_types
        aliases: dict[str, str] = {}
        stack = list(caller_node.children)
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in function_types or node_type in class_types:
                continue
            if node_type == assignment:
                left = node.child_by_field_name(left_field)
                right = node.child_by_field_name(right_field)
                if (
                    left is not None
                    and left.type == identifier
                    and (left_text := left.text) is not None
                    and right is not None
                    and (
                        target := self._alias_reference_text(
                            right, identifier, attribute, module_qn
                        )
                    )
                    is not None
                ):
                    aliases.setdefault(left_text.decode(cs.ENCODING_UTF8), target)
            stack.extend(node.children)
        return aliases

    def _alias_reference_text(
        self, right: Node, identifier: str, attribute: str, module_qn: str
    ) -> str | None:
        # An alias rhs is a plain name/attribute, a conditional that picks one
        # (resolve_builtin_call if is_js_ts else None), or getattr(recv, name)
        # dynamic dispatch. Take the name/attribute branch (consequence or
        # alternative, never the condition) or build recv.<name> for getattr.
        # A TS cast (`y as T`) is transparent; unwrap so `x = y as T` still aliases.
        right = self._unwrap_ts_cast(right)
        if right.type in (identifier, attribute):
            return right.text.decode(cs.ENCODING_UTF8) if right.text else None
        if right.type == cs.TS_PY_CONDITIONAL_EXPRESSION and right.named_children:
            for branch in (right.named_children[0], right.named_children[-1]):
                if branch.type in (identifier, attribute) and branch.text:
                    return branch.text.decode(cs.ENCODING_UTF8)
        if right.type == cs.TS_PY_CALL:
            return self._getattr_reference_text(right, identifier, attribute, module_qn)
        return None

    def _getattr_reference_text(
        self, call: Node, identifier: str, attribute: str, module_qn: str
    ) -> str | None:
        func = call.child_by_field_name(cs.TS_FIELD_FUNCTION)
        args = call.child_by_field_name(cs.FIELD_ARGUMENTS)
        if (
            func is None
            or safe_decode_text(func) != cs.PY_BUILTIN_GETATTR
            or args is None
            or len(args.named_children) < 2
        ):
            return None
        receiver, name_node = args.named_children[0], args.named_children[1]
        if receiver.type not in (identifier, attribute):
            return None
        if (name := self._resolve_str_const(name_node, module_qn)) is None:
            return None
        return f"{safe_decode_text(receiver)}{cs.SEPARATOR_DOT}{name}"

    def _resolve_str_const(self, node: Node, module_qn: str) -> str | None:
        # Resolve a getattr name argument to its string value: a string literal
        # directly, or a module-level constant (cs.METHOD_X / METHOD_X) read from
        # the defining module's AST.
        if node.type == cs.TS_PY_STRING:
            content = next(
                (c for c in node.children if c.type == cs.TS_PY_STRING_CONTENT), None
            )
            return safe_decode_text(content) if content is not None else None
        if node.type not in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
            return None
        name_text = safe_decode_text(node)
        if not name_text:
            return None
        import_map = self._resolver.import_processor.import_mapping.get(module_qn, {})
        prefix, _, const_name = name_text.rpartition(cs.SEPARATOR_DOT)
        if not prefix:
            mapped = import_map.get(const_name)
            const_module_qn = (
                mapped.rsplit(cs.SEPARATOR_DOT, 1)[0] if mapped else module_qn
            )
        elif (mapped_module := import_map.get(prefix)) is not None:
            const_module_qn = mapped_module
        else:
            const_module_qn = prefix
        return self._module_string_constant(const_module_qn, const_name)

    def _module_string_constant(self, module_qn: str, const_name: str) -> str | None:
        type_inference = self._resolver.type_inference
        file_path = type_inference.module_qn_to_file_path.get(module_qn)
        if file_path is None or not (entry := type_inference.ast_cache.load(file_path)):
            return None
        root_node, _ = entry
        for child in root_node.children:
            if child.type != cs.TS_PY_EXPRESSION_STATEMENT or not child.children:
                continue
            assignment = child.children[0]
            if assignment.type != cs.TS_PY_ASSIGNMENT:
                continue
            left = assignment.child_by_field_name(cs.TS_FIELD_LEFT)
            right = assignment.child_by_field_name(cs.TS_FIELD_RIGHT)
            if (
                left is not None
                and left.type == cs.TS_PY_IDENTIFIER
                and safe_decode_text(left) == const_name
                and right is not None
                and right.type == cs.TS_PY_STRING
            ):
                return self._resolve_str_const(right, module_qn)
        return None

    @_site_scoped
    def _ingest_property_accesses(
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
        # Accessing an @property getter invokes the getter method at runtime, but
        # tree-sitter sees a plain attribute, not a call. Resolve attribute
        # accesses whose tail names a known property and emit a CALLS edge to the
        # getter (skipping the attribute that is itself a call's function, already
        # resolved by the call path above).
        attr_type = cs.TS_PY_ATTRIBUTE
        function_types = lang_config.function_node_types
        class_types = lang_config.class_node_types
        seen: set[str] = set()

        stack = list(caller_node.children)
        while stack:
            node = self._site_node = stack.pop()
            node_type = node.type
            if node_type in function_types or node_type in class_types:
                continue
            if node_type == attr_type and (text := node.text) is not None:
                attr_text = text.decode(cs.ENCODING_UTF8)
                if attr_text.rsplit(cs.SEPARATOR_DOT, 1)[-1] in prop_names:
                    self._emit_property_access(
                        node,
                        attr_text,
                        caller_spec,
                        caller_qn,
                        module_qn,
                        local_var_types,
                        class_context,
                        seen,
                    )
            stack.extend(node.children)

    def _emit_property_access(
        self,
        node: Node,
        attr_text: str,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        seen: set[str],
    ) -> None:
        # One property-named attribute access: a CALLS edge to the getter it
        # invokes, unless the attribute is itself a call's function.
        registry = self._resolver.function_registry
        parent = node.parent
        is_call_target = (
            parent is not None
            and parent.type == cs.TS_PY_CALL
            # `==` not `is`: py-tree-sitter builds a fresh
            # wrapper per lookup, so the node returned here is
            # never the same object as the one taken from
            # .children, and identity would leave this guard
            # permanently False.
            and parent.child_by_field_name(cs.TS_FIELD_FUNCTION) == node
        )
        if not is_call_target and (
            callee_info := self._resolver.resolve_function_call(
                attr_text,
                module_qn,
                local_var_types,
                class_context,
                caller_qn,
            )
        ):
            callee_qn = callee_info[1]
            if (
                registry.is_property(callee_qn)
                and callee_qn != caller_qn
                and callee_qn not in seen
            ):
                seen.add(callee_qn)
                for target_qn in registry.variants(callee_qn):
                    self._emit_rel(
                        caller_spec,
                        cs.RelationshipType.CALLS,
                        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, target_qn),
                    )

    @staticmethod
    def _js_ts_member_is_write_target(node: Node) -> bool:
        # True when a member read is the WRITE target of a plain assignment
        # (`this.thing = v`, and destructuring forms `[this.thing] = a`,
        # `({x: this.thing} = o)` where the member is nested in the left pattern
        # but is not its direct `left` child); those invoke the SETTER, not the
        # getter. An augmented assignment (`this.thing += 1`) reads first, so it
        # is NOT a write target. The walk stops at the nearest assignment or at a
        # function/class boundary so an outer assignment cannot misjudge a member
        # that actually sits in a nested scope or on the right-hand side.
        current = node.parent
        while current is not None:
            node_type = current.type
            if node_type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
                left = current.child_by_field_name(cs.FIELD_LEFT)
                return (
                    left is not None
                    and left.start_byte <= node.start_byte
                    and node.end_byte <= left.end_byte
                )
            if (
                node_type
                in (
                    cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION,
                    cs.TS_METHOD_DEFINITION,
                    cs.TS_ARROW_FUNCTION,
                )
                or node_type in _JS_THIS_REBINDING_FUNC_TYPES
                or node_type in cs.JS_TS_CLASS_NODES
            ):
                return False
            current = current.parent
        return False

    @staticmethod
    def _js_this_binds_to_enclosing_class(read_node: Node) -> bool:
        # True when `this`/`super` at read_node lexically refers to the enclosing
        # class instance: walk up, arrows are transparent (they inherit `this`),
        # a method_definition binds `this` to the class ONLY when it is a class
        # member (parent is a class_body) -- an object-literal shorthand method
        # binds `this` to the object; a class node itself is a class-field arrow
        # context; a plain function / function-expression / generator rebinds.
        current = read_node.parent
        while current is not None:
            node_type = current.type
            if node_type == cs.TS_METHOD_DEFINITION:
                parent = current.parent
                return parent is not None and parent.type == cs.TS_CLASS_BODY
            if node_type in cs.JS_TS_CLASS_NODES:
                return True
            if node_type in _JS_THIS_REBINDING_FUNC_TYPES:
                return False
            current = current.parent
        return False

    def _js_ts_getter_read_class_qn(
        self,
        member_node: Node,
        class_context: str | None,
        local_var_types: dict[str, str] | None,
        module_qn: str,
    ) -> str | None:
        # The class that owns a getter read `recv.getter`, resolved from the
        # RECEIVER's type only: `this`/`super` is the enclosing class, and a
        # plain identifier is typed via local_var_types then resolved to its
        # class qn. Any other receiver (an unknown local, a chained expression)
        # yields None so no edge is fabricated. Resolving by receiver type (not
        # the name-global trie) is what keeps a same-named getter on an
        # unrelated class from being falsely revived.
        recv, cast_type = _peel_ts_getter_receiver(
            member_node.child_by_field_name(cs.FIELD_OBJECT)
        )
        if cast_type is not None:
            return (
                self._resolver._resolve_class_name(cast_type, module_qn)
                if cast_type
                else None
            )
        if recv is None:
            return None
        if recv.type == cs.TS_THIS:
            # `this` binds to the class instance only when the read is lexically
            # inside a CLASS method or class-field (arrows are transparent);
            # inside a nested plain `function`/function-expression, or an
            # object-literal method, `this` rebinds, so it is NOT the class and
            # must not link (issue #971's rebinding rule, extended to reject an
            # object-literal method whose method_definition binds `this` to the
            # object, not the class). `super.getter` is deliberately NOT handled:
            # it reads the BASE class's getter, and class_context is the current
            # class, so mapping it here would target the wrong (or a missing)
            # method; a conservative miss is preferred to a wrong edge.
            return (
                class_context
                if self._js_this_binds_to_enclosing_class(member_node)
                else None
            )
        if recv.type == cs.TS_IDENTIFIER and (recv_name := safe_decode_text(recv)):
            # A typed local already stores its resolved class qn; a bare
            # class-name receiver (a static getter read, `Box.thing`) resolves
            # through the class registry. An unknown receiver (an untyped
            # parameter) matches neither and yields None, so the name-global
            # trie never fires and an unrelated same-named getter is not revived.
            if (recv_type := (local_var_types or {}).get(recv_name)) is not None:
                return recv_type
            return self._resolver._resolve_class_name(recv_name, module_qn)
        return None

    @_site_scoped
    def _ingest_js_ts_getter_reads(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        lang_config: LanguageSpec,
        prop_names: set[str],
        caller_qn: str | None = None,
    ) -> None:
        # Emit a REFERENCES edge (a read invokes the getter but is not a call, so
        # the call graph stays invocation-only) for every member_expression whose
        # property resolves to a getter ON THE RECEIVER'S OWN CLASS, wherever it
        # appears in the body. The walk is position-agnostic, so a getter read
        # inside a ternary, member chain (`loadMap.mainLoadMapItem.entity` visits
        # the inner `loadMap.mainLoadMapItem`), or `if` condition links the same
        # as a bare read. The candidate qn is built from the receiver's class and
        # emitted only when it is a registered property, so a same-named data
        # field, a same-named regular method, or a same-named getter on an
        # unrelated class never fabricates an edge.
        function_types = lang_config.function_node_types
        class_types = lang_config.class_node_types
        seen: set[str] = set()
        stack = list(caller_node.children)
        while stack:
            node = self._site_node = stack.pop()
            node_type = node.type
            # A nested class or NON-arrow function owns its own reads (and
            # rebinds `this`), so it is processed as its own caller; do not
            # descend. An arrow is transparent (it captures the enclosing `this`
            # and shares the outer locals), so descend into it or getter reads in
            # `.map`/`.find`/`.forEach` callbacks are missed.
            if (
                node_type in function_types and node_type != cs.TS_ARROW_FUNCTION
            ) or node_type in class_types:
                continue
            if node_type == cs.TS_MEMBER_EXPRESSION:
                self._emit_js_ts_getter_read(
                    node,
                    caller_spec,
                    caller_qn,
                    module_qn,
                    local_var_types,
                    class_context,
                    prop_names,
                    seen,
                )
            stack.extend(node.children)

    def _emit_js_ts_getter_read(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str | None,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        prop_names: set[str],
        seen: set[str],
    ) -> None:
        registry = self._resolver.function_registry
        prop = node.child_by_field_name(cs.FIELD_PROPERTY)
        if (
            not self._js_ts_member_is_write_target(node)
            and prop is not None
            and (name := safe_decode_text(prop)) in prop_names
            and (
                class_qn := self._js_ts_getter_read_class_qn(
                    node, class_context, local_var_types, module_qn
                )
            )
        ):
            # Resolve the getter on the receiver's class OR an ancestor
            # (an inherited getter still invokes real code); emit only
            # when the resolved target is a registered property, so a
            # same-named data field or regular method never links.
            resolved = self._resolver._try_resolve_method(class_qn, name)
            if resolved is not None and registry.is_property(resolved[1]):
                res_label, res_qn = resolved
                for target_qn in registry.variants(res_qn):
                    if target_qn != caller_qn and target_qn not in seen:
                        seen.add(target_qn)
                        self._emit_rel(
                            caller_spec,
                            cs.RelationshipType.REFERENCES,
                            (res_label, cs.KEY_QUALIFIED_NAME, target_qn),
                        )

    def _ancestor_function_name(
        self, function_node: Node, lang_config: LanguageSpec
    ) -> str | None:
        name_node = function_node.child_by_field_name(cs.FIELD_NAME)
        if name_node is not None:
            if name_node.text is None:
                return None
            return name_node.text.decode(cs.ENCODING_UTF8)
        # A JS/TS arrow-const ancestor (`getQueryString = () => {...}`) has no
        # `name` field (the name lives on the parent declarator), so it would be
        # dropped, flattening a nested callee's qn (request.encodePair instead
        # of request.getQueryString.encodePair). Recover the binding name the
        # definition pass used so the qns agree; else the callee's own
        # inline-arg/object callbacks never match and report dead.
        if lang_config.language in _JS_TS_LANGUAGES:
            return self._js_ts_arrow_binding_name(function_node) or None
        return None

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        path_parts: list[str] = []
        current = func_node.parent

        if not isinstance(current, Node):
            logger.warning(
                ls.CALL_UNEXPECTED_PARENT, node=func_node, parent_type=type(current)
            )
            return None

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                part = self._ancestor_function_name(current, lang_config)
                if part is not None:
                    path_parts.append(part)
            elif current.type in lang_config.class_node_types:
                return None

            current = current.parent

        path_parts.reverse()
        if path_parts:
            return f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(path_parts)}{cs.SEPARATOR_DOT}{func_name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{func_name}"

    def _js_ts_arrow_binding_name(self, func_node: Node) -> str | None:
        # Shared with the definition pass (function_ingest) so both derive the
        # SAME name for a name-bound arrow; divergence makes the caller qn a
        # phantom and the arrow's body callbacks report dead.
        return js_ts_utils.arrow_binding_name(func_node)

    def _attributable_func_nodes(
        self, func_nodes: list[Node], language: cs.SupportedLanguage, module_qn: str
    ) -> list[Node]:
        # The func nodes that will get their own caller node: named functions
        # plus arrows/function-expressions bound to a name. An anonymous arrow
        # passed directly as an argument (`hooks.tap(name, (x) => {...})`) has
        # neither, so the call loop skips it. Its calls must therefore NOT be
        # excluded from the enclosing named scope by _calls_owned_by; otherwise
        # they attribute to nothing and drop (webpack is saturated with this
        # callback pattern). Returning only attributable nodes as the exclusion
        # set lets an anonymous arrow's calls bubble up to the nearest named
        # function/method, matching where the oracle attributes them.
        if language == cs.SupportedLanguage.RUST:
            # A Rust closure (`expire.map(|_| state.next_expiration())`) is unnamed,
            # so the call loop skips it (no caller node); like JS/TS anon arrows its
            # calls must bubble to the enclosing fn/method (which holds the captured
            # locals' types) instead of being excluded and dropped. Named nested
            # `fn`s stay attributable and keep their own calls.
            return [n for n in func_nodes if n.type != cs.TS_RS_CLOSURE_EXPRESSION]
        if language == cs.SupportedLanguage.JULIA:
            # An anonymous Julia arrow gets no caller node: its calls
            # bubble to the enclosing scope like JS/TS arrows.
            return [n for n in func_nodes if self._julia_caller_name(n, module_qn)]
        if language not in _JS_TS_LANGUAGES:
            return func_nodes
        # A nameless function expression the definition pass registered under
        # a name (`x: function () {}` in an object literal, `this.h = function
        # () {}`) gets its own caller pass, so it must own its calls too, or
        # the enclosing function keeps a second copy of every edge.
        return [
            n
            for n in func_nodes
            if self._get_node_name(n)
            or self._js_ts_arrow_binding_name(n)
            or (
                (recorded := self._recorded_caller(n, module_qn)) is not None
                and recorded.is_named
            )
        ]

    def _is_unowned_js_scope(self, node: Node, module_qn: str) -> bool:
        # An anonymous arrow/function/generator expression that gets no caller
        # node of its own (no name, no binding name): a `.map()`/`cell`/
        # forwardRef callback. Its calls bubble up to the enclosing named
        # scope (_attributable_func_nodes), and so must its JSX references:
        # the enclosing JSX walk continues through it.
        if node.type not in (
            cs.TS_ARROW_FUNCTION,
            cs.TS_FUNCTION_EXPRESSION,
            cs.TS_GENERATOR_FUNCTION,
        ):
            return False
        if self._get_node_name(node) or self._js_ts_arrow_binding_name(node):
            return False
        # A nameless FUNCTION EXPRESSION the definition pass registered under
        # a name (`x: function () {}`) has neither, but IS a node with its own
        # walk, so a reference inside it belongs to that node alone. Without
        # this the enclosing scope emitted a second copy of every REFERENCES
        # edge (issue #1932).
        #
        # Deliberately NOT extended to arrows, and the reason is narrower
        # than it looks. An arrow that is an object value in a config array
        # (`{ cell: ({row}) => <CopyId/> }`, TanStack columns) is registered
        # under its key too, so owning it would MOVE the edge to `cols.cell`
        # rather than drop it -- the component stays reachable either way.
        # What it would break is the edge's SOURCE, which
        # test_jsx_component_in_config_callback_is_referenced asserts is the
        # module. Changing that is a behaviour change for consumers, so
        # arrows keep bubbling here.
        #
        # A generator expression has no such consumer and duplicates exactly
        # as a function expression does, so it is included.
        #
        # All three types are identical on both underlying signals
        # (_attributable_func_nodes says unattributable, the recorded name
        # says is_named), so this split is an enumeration, not a property.
        if node.type not in (
            cs.TS_FUNCTION_EXPRESSION,
            cs.TS_GENERATOR_FUNCTION,
        ):
            return True
        recorded = self._recorded_caller(node, module_qn)
        return recorded is None or not recorded.is_named

    def forget_callable_flow(self, file_path: Path) -> None:
        # A deleted file is never re-walked, so nothing else replaces its
        # records; left in place they keep emitting its call sites' edges.
        self._callable_flow.pop(file_path, None)

    def begin_run(self) -> None:
        # A run that walks no calls (in sync) must not report a previous
        # run's failures as its own.
        self.failed_call_files.clear()

    def reset_callable_flow(self) -> None:
        # For a pass that re-walks every file: records of a file deleted
        # since the last pass would otherwise outlive it.
        self._callable_flow.clear()

    def reset_js_receiver_bindings(self) -> None:
        # Despite the historical name this clears ALL per-run JS call-pass
        # state: the receiver-binding index, the symbol index, and the
        # prototype-evidence memo. A reused updater's next pass re-parses
        # files and must rebuild each from the fresh trees.
        self._js_file_receiver_bindings.clear()
        self.js_symbol_constants.clear()
        self._js_symbol_assignments.clear()
        self.js_symbol_member_types.clear()
        self._js_proto_evidence_cache.clear()

    def collect_js_symbol_bindings(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
    ) -> None:
        # Pre-pass for symbol-keyed member dispatch (issue #989): one walk
        # per JS/TS file collecting (a) symbol CONSTANT definitions
        # (`kX = Symbol('...')` declarators, `kX: Symbol('...')` pairs) and
        # (b) raw symbol-keyed installations (`{ [kX]: value }` pairs,
        # `obj[kX] = value` assignments) with the value's class resolved
        # immediately (strings only). Whether a key actually names a symbol
        # constant is judged in finalize, once every file's constants are in.
        if language not in _JS_TS_LANGUAGES:
            return
        try:
            module_qn = self._module_qn(
                file_path, cached_relative_path(file_path, self.repo_path)
            )
        except ValueError:
            return
        try:
            stack: list[Node] = [root_node]
            while stack:
                node = stack.pop()
                if node.type == cs.TS_VARIABLE_DECLARATOR:
                    self._collect_symbol_declarator(node, module_qn)
                elif node.type in (
                    cs.TS_PAIR,
                    cs.TS_PUBLIC_FIELD_DEFINITION,
                    cs.TS_JS_FIELD_DEFINITION,
                ):
                    self._collect_symbol_pair(node, module_qn)
                elif node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
                    self._collect_symbol_subscript_assignment(node, module_qn)
                stack.extend(node.children)
        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            self.failed_call_files.add(file_path)
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)

    @staticmethod
    def _js_is_symbol_mint(value: Node | None) -> bool:
        # `Symbol('x')` or `Symbol.for('x')`.
        if value is None or value.type != cs.TS_CALL_EXPRESSION:
            return False
        func = value.child_by_field_name(cs.FIELD_FUNCTION)
        if func is None:
            return False
        if func.type == cs.TS_IDENTIFIER:
            return safe_decode_text(func) == cs.JS_SYMBOL_GLOBAL
        if func.type == cs.TS_MEMBER_EXPRESSION:
            obj = func.child_by_field_name(cs.FIELD_OBJECT)
            return (
                obj is not None
                and obj.type == cs.TS_IDENTIFIER
                and safe_decode_text(obj) == cs.JS_SYMBOL_GLOBAL
            )
        return False

    @staticmethod
    def _js_inside_callable(node: Node) -> bool:
        current = node.parent
        while current is not None:
            if current.type in cs.JS_TS_FUNCTION_NODES:
                return True
            current = current.parent
        return False

    def _collect_symbol_declarator(self, node: Node, module_qn: str) -> None:
        # Only a MODULE-scoped binding mints a shared symbol constant; a
        # function-local `k = Symbol(...)` is a local value whose name must
        # not hijack same-spelled reads elsewhere in the module.
        target = node.child_by_field_name(cs.FIELD_NAME)
        if (
            target is not None
            and target.type == cs.TS_IDENTIFIER
            and self._js_is_symbol_mint(node.child_by_field_name(cs.FIELD_VALUE))
            and not self._js_inside_callable(node)
            and (name := safe_decode_text(target))
        ):
            self.js_symbol_constants.add(f"{module_qn}{cs.SEPARATOR_DOT}{name}")

    def _collect_symbol_pair(self, node: Node, module_qn: str) -> None:
        # An object-literal pair OR a class field: the key sits in `key`,
        # `name` (TS field) or `property` (JS field), the value in `value`.
        key = (
            node.child_by_field_name(cs.FIELD_KEY)
            or node.child_by_field_name(cs.FIELD_NAME)
            or node.child_by_field_name(cs.FIELD_PROPERTY)
        )
        value = node.child_by_field_name(cs.FIELD_VALUE)
        if key is None or value is None:
            return
        if key.type == cs.TS_PROPERTY_IDENTIFIER and self._js_is_symbol_mint(value):
            if not self._js_inside_callable(node) and (name := safe_decode_text(key)):
                self.js_symbol_constants.add(f"{module_qn}{cs.SEPARATOR_DOT}{name}")
            return
        if key.type == cs.TS_JS_COMPUTED_PROPERTY_NAME:
            self._collect_computed_symbol_pair(key, value, module_qn)

    def _collect_computed_symbol_pair(
        self, key: Node, value: Node, module_qn: str
    ) -> None:
        # `[kSym]: new Impl()` installs a class under a symbol key.
        index = next((c for c in key.named_children if c.type != cs.TS_COMMENT), None)
        if (
            index is not None
            and index.type == cs.TS_IDENTIFIER
            and (sym_name := safe_decode_text(index))
            and (class_qn := self._js_symbol_value_class_qn(value, module_qn))
        ):
            self._js_symbol_assignments.append((module_qn, sym_name, class_qn))

    def _collect_symbol_subscript_assignment(self, node: Node, module_qn: str) -> None:
        left = node.child_by_field_name(cs.FIELD_LEFT)
        value = node.child_by_field_name(cs.TS_FIELD_RIGHT)
        if left is None or value is None or left.type != cs.TS_SUBSCRIPT_EXPRESSION:
            return
        index = left.child_by_field_name(cs.TS_FIELD_INDEX)
        if (
            index is not None
            and index.type == cs.TS_IDENTIFIER
            and (sym_name := safe_decode_text(index))
            and (class_qn := self._js_symbol_value_class_qn(value, module_qn))
        ):
            self._js_symbol_assignments.append((module_qn, sym_name, class_qn))

    def _js_symbol_value_class_qn(self, value: Node, module_qn: str) -> str | None:
        # Resolve the INSTALLED value to a class qn, entirely at sweep time
        # so only strings are kept.
        return self._js_value_type_class_qn(value, module_qn, depth=0)

    def _js_value_type_class_qn(
        self, value: Node | None, module_qn: str, depth: int
    ) -> str | None:
        # Type a VALUE expression to a registered class qn: a direct
        # construction, a factory call (bare, member/static, or one whose
        # returned OBJECT the caller destructures), or an identifier chased
        # to its declarator. Depth-capped: each hop crosses at most one
        # binding or one function return.
        if value is None or depth > 3:
            return None
        node_type = value.type
        if (
            node_type == cs.TS_PARENTHESIZED_EXPRESSION
            or node_type in cs.TS_CAST_WRAPPER_TYPES
        ):
            inner = next(
                (c for c in value.named_children if c.type != cs.TS_COMMENT), None
            )
            return self._js_value_type_class_qn(inner, module_qn, depth)
        if node_type == cs.TS_NEW_EXPRESSION:
            ctor = js_ts_utils.extract_constructor_name(value)
            if not ctor:
                return None
            return self._js_registered_class(
                ctor, module_qn, allow_constructor_function=True
            )
        if node_type == cs.TS_IDENTIFIER:
            return self._js_identifier_class_qn(value, module_qn, depth)
        if node_type == cs.TS_CALL_EXPRESSION:
            return self._js_call_result_class_qn(value, module_qn)
        return None

    def _js_registered_class(
        self, name: str, module_qn: str, allow_constructor_function: bool = False
    ) -> str | None:
        # allow_constructor_function: a `new X(...)` target may be an
        # old-style CONSTRUCTOR FUNCTION with prototype methods (fastify's
        # ContentTypeParser), registered as Function; accepting it lets its
        # prototype methods resolve. Never allowed for name hints, where a
        # function qn is a factory, not a type.
        registry = self._resolver.function_registry
        candidates = [name, *self._js_name_candidates(name, module_qn)]
        for candidate in candidates:
            if candidate not in registry:
                continue
            node_type = registry[candidate]
            if node_type == cs.NodeLabel.CLASS:
                return candidate
            if (
                allow_constructor_function
                and node_type == cs.NodeLabel.FUNCTION
                and self._js_has_prototype_members(candidate)
            ):
                return candidate
        return None

    def _js_member_is_exposed(self, class_qn: str, method: str) -> bool:
        # A CLASS's registry members are its real methods. A CONSTRUCTOR
        # FUNCTION shares its `module.fn.name` namespace with its NESTED
        # private helpers, so each member needs its own
        # `Fn.prototype.<method> = ...` evidence before the qn may be
        # treated as callable from outside.
        registry = self._resolver.function_registry
        if class_qn in registry and registry[class_qn] == cs.NodeLabel.CLASS:
            return True
        return self._js_has_prototype_members(class_qn, member=method)

    def _js_has_prototype_members(self, fn_qn: str, member: str | None = None) -> bool:
        cache_key = (fn_qn, member)
        if cache_key in self._js_proto_evidence_cache:
            return self._js_proto_evidence_cache[cache_key]
        result = self._js_scan_prototype_members(fn_qn, member)
        self._js_proto_evidence_cache[cache_key] = result
        return result

    def _js_scan_prototype_members(self, fn_qn: str, member: str | None = None) -> bool:
        # Constructor EVIDENCE: at least one `Fn.prototype.x = ...` in the
        # function's module (any member), or, when `member` is given, an
        # assignment for that SPECIFIC member. Without it, `module.fn.name`
        # registry entries under the function are its NESTED helpers, not
        # methods, and accepting them would manufacture edges.
        fn_module, _, fn_leaf = fn_qn.rpartition(cs.SEPARATOR_DOT)
        type_inference = self._resolver.type_inference
        file_path = type_inference.module_qn_to_file_path.get(fn_module)
        if file_path is None or not (entry := type_inference.ast_cache.load(file_path)):
            return False
        root_node, _language = entry
        stack: list[Node] = [root_node]
        while stack:
            node = stack.pop()
            if node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION and (
                self._js_prototype_assignment_matches(node, fn_leaf, member)
            ):
                return True
            stack.extend(node.children)
        return False

    @staticmethod
    def _js_prototype_assignment_matches(
        node: Node, fn_leaf: str, member: str | None
    ) -> bool:
        left = node.child_by_field_name(cs.FIELD_LEFT)
        if (
            left is None
            or left.type != cs.TS_MEMBER_EXPRESSION
            or (inner := left.child_by_field_name(cs.FIELD_OBJECT)) is None
            or inner.type != cs.TS_MEMBER_EXPRESSION
            or (obj := inner.child_by_field_name(cs.FIELD_OBJECT)) is None
            or obj.type != cs.TS_IDENTIFIER
            or safe_decode_text(obj) != fn_leaf
            or (prop := inner.child_by_field_name(cs.FIELD_PROPERTY)) is None
            or safe_decode_text(prop) != cs.JS_PROTOTYPE_KEYWORD
        ):
            return False
        if member is None:
            return True
        outer_prop = left.child_by_field_name(cs.FIELD_PROPERTY)
        return outer_prop is not None and safe_decode_text(outer_prop) == member

    def _js_name_candidates(self, name: str, module_qn: str) -> list[str]:
        # The qns a bare name may denote: the imported member, the imported
        # MODULE's same-named export (`const P = require('./p.js')` where
        # p.js exports its constructor), or the module-local binding.
        candidates: list[str] = []
        import_map = self._resolver.import_processor.import_mapping.get(module_qn)
        if import_map is not None and name in import_map:
            imported = import_map[name]
            candidates.append(imported)
            if not imported.endswith(f"{cs.SEPARATOR_DOT}{name}"):
                candidates.append(f"{imported}{cs.SEPARATOR_DOT}{name}")
        candidates.append(f"{module_qn}{cs.SEPARATOR_DOT}{name}")
        return candidates

    def _js_identifier_class_qn(
        self, ident: Node, module_qn: str, depth: int
    ) -> str | None:
        # Chase the identifier to the INTRODUCTION it actually reads: every
        # binding of the name visible from this position (parameters, catch
        # and loop bindings, lexical and var declarators) is collected with
        # the byte span of the scope it governs, and the innermost enclosing
        # one wins. A parameter or other opaque introduction is statically
        # unknowable and refuses; name-match-anywhere resolution leaks (the
        # #988 lesson) and is never used.
        name = safe_decode_text(ident)
        if not name:
            return None
        intro = self._js_innermost_introduction(ident, name)
        if intro is None:
            return None
        kind, value = intro
        return self._js_introduction_class_qn(kind, value, name, module_qn, depth)

    def _js_innermost_introduction(
        self, ident: Node, name: str
    ) -> tuple[str, Node | None] | None:
        # The (kind, value) of the introduction of `name` that `ident` reads.
        # Scopes are visited innermost first and the first one introducing
        # the name around `ident` decides: an outer scope's introductions
        # span all of the inner scope, so they can never be the innermost,
        # and the module's introductions are read only for a module-level name.
        pos = ident.start_byte
        scope = ident.parent
        while scope is not None:
            if scope.type in cs.JS_TS_FUNCTION_NODES or scope.parent is None:
                intros = js_bindings.scope_introductions(scope).get(name, ())
                decided, intro = js_bindings.innermost_enclosing(intros, pos)
                if decided:
                    return intro
            scope = scope.parent
        return None

    def _js_introduction_class_qn(
        self, kind: str, value: Node | None, name: str, module_qn: str, depth: int
    ) -> str | None:
        if kind == js_bindings.DECL_PLAIN:
            return self._js_value_type_class_qn(value, module_qn, depth + 1)
        if kind == js_bindings.DECL_DESTRUCTURED and value is not None:
            return self._js_destructured_return_class(value, name, module_qn, depth + 1)
        return None

    def _js_call_result_class_qn(self, call: Node, module_qn: str) -> str | None:
        func = call.child_by_field_name(cs.FIELD_FUNCTION)
        if func is None:
            return None
        if func.type == cs.TS_IDENTIFIER:
            fn_name = safe_decode_text(func)
            if not fn_name:
                return None
            return self._js_factory_return_class(
                self._js_symbol_name_qn(fn_name, module_qn)
            )
        if func.type == cs.TS_MEMBER_EXPRESSION:
            return self._js_member_call_result_class(func, module_qn)
        return None

    def _js_member_call_result_class(self, func: Node, module_qn: str) -> str | None:
        obj = func.child_by_field_name(cs.FIELD_OBJECT)
        prop = func.child_by_field_name(cs.FIELD_PROPERTY)
        if (
            obj is None
            or prop is None
            or obj.type != cs.TS_IDENTIFIER
            or not (owner := safe_decode_text(obj))
            or not (method := safe_decode_text(prop))
        ):
            return None
        js = self._resolver.type_inference.js_type_inference
        hint = js._infer_js_method_return_type(
            f"{owner}{cs.SEPARATOR_DOT}{method}", module_qn
        )
        if hint and (resolved := self._js_registered_class(hint, module_qn)):
            return resolved
        # A static assigned OUTSIDE the class body (`C.build = build`;
        # fastify's SchemaController.buildSchemaController) is invisible
        # to the class-body method finder. Only a VERIFIED assignment in
        # the class's module may redirect; a same-named free function
        # alone must not (it can be unrelated).
        class_qn = js._resolve_js_class_name(owner, module_qn)
        if class_qn is None:
            return None
        assigned = self._js_static_assigned_factory(class_qn, method)
        if assigned is None:
            return None
        class_module = class_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        if assigned.type == cs.TS_IDENTIFIER:
            fn_name = safe_decode_text(assigned)
            if not fn_name:
                return None
            return self._js_factory_return_class(
                f"{class_module}{cs.SEPARATOR_DOT}{fn_name}"
            )
        if assigned.type in cs.JS_TS_FUNCTION_NODES:
            return self._js_function_return_class(
                assigned,
                f"{class_module}{cs.SEPARATOR_DOT}{method}",
                class_module,
            )
        return None

    def _js_static_assigned_factory(self, class_qn: str, method: str) -> Node | None:
        # The right-hand side of `ClassLeaf.method = ...` in the class's
        # module, or None when no such assignment exists.
        class_module, _, class_leaf = class_qn.rpartition(cs.SEPARATOR_DOT)
        type_inference = self._resolver.type_inference
        file_path = type_inference.module_qn_to_file_path.get(class_module)
        if file_path is None or not (entry := type_inference.ast_cache.load(file_path)):
            return None
        root_node, _language = entry
        stack: list[Node] = [root_node]
        while stack:
            node = stack.pop()
            if node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
                left = node.child_by_field_name(cs.FIELD_LEFT)
                if (
                    left is not None
                    and left.type == cs.TS_MEMBER_EXPRESSION
                    and (obj := left.child_by_field_name(cs.FIELD_OBJECT)) is not None
                    and obj.type == cs.TS_IDENTIFIER
                    and safe_decode_text(obj) == class_leaf
                    and (prop := left.child_by_field_name(cs.FIELD_PROPERTY))
                    is not None
                    and safe_decode_text(prop) == method
                ):
                    return node.child_by_field_name(cs.TS_FIELD_RIGHT)
            stack.extend(node.children)
        return None

    def _js_factory_return_class(self, fn_qn: str | None) -> str | None:
        # Once per factory per pass: every call-result receiver a factory
        # types asked again, re-reading its returns each time (issue #2453).
        # The answer depends on the registry and the parsed trees only, and
        # `reset_resolution_caches` drops it when either may have changed.
        if fn_qn is None:
            return None
        memo = self._js_factory_classes
        if fn_qn not in memo:
            memo[fn_qn] = self._js_read_factory_return_class(fn_qn)
        return memo[fn_qn]

    def _js_read_factory_return_class(self, fn_qn: str) -> str | None:
        registry = self._resolver.function_registry
        if fn_qn not in registry:
            return None
        fn_node = self._js_function_node_by_qn(fn_qn)
        if fn_node is None:
            return None
        factory_module = fn_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return self._js_function_return_class(fn_node, fn_qn, factory_module)

    def _js_function_return_class(
        self, fn_node: Node, fn_qn: str, factory_module: str
    ) -> str | None:
        if ctor := self._js_direct_return_ctor(fn_node):
            return self._js_registered_class(ctor, factory_module)
        js = self._resolver.type_inference.js_type_inference
        returned = js._analyze_return_statements(fn_node, fn_qn)
        if returned is None:
            return None
        return self._js_registered_class(returned, factory_module)

    def _js_destructured_return_class(
        self, call_value: Node, prop_name: str, module_qn: str, depth: int
    ) -> str | None:
        # `const { name } = f(...)`: the binding's type is the type of the
        # `name` property in f's returned object literal.
        if depth > 3 or call_value.type != cs.TS_CALL_EXPRESSION:
            return None
        func = call_value.child_by_field_name(cs.FIELD_FUNCTION)
        if func is None or func.type != cs.TS_IDENTIFIER:
            return None
        fn_name = safe_decode_text(func)
        if not fn_name:
            return None
        fn_qn = self._js_symbol_name_qn(fn_name, module_qn)
        registry = self._resolver.function_registry
        if fn_qn is None or fn_qn not in registry:
            return None
        fn_node = self._js_function_node_by_qn(fn_qn)
        if fn_node is None:
            return None
        factory_module = fn_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        # Every returned object contributes its `name` property's class;
        # divergent returns are ambiguous and refuse (never guess one path).
        candidates: set[str | None] = set()
        for obj in self._js_own_returned_objects(fn_node):
            candidates.add(
                self._js_returned_object_property_class(
                    obj, prop_name, factory_module, depth
                )
            )
        candidates.discard(None)
        if len(candidates) == 1:
            return candidates.pop()
        return None

    def _js_returned_object_property_class(
        self, obj: Node, prop_name: str, factory_module: str, depth: int
    ) -> str | None:
        for child in obj.named_children:
            if child.type == cs.TS_PAIR:
                key = child.child_by_field_name(cs.FIELD_KEY)
                if (
                    key is not None
                    and key.type == cs.TS_PROPERTY_IDENTIFIER
                    and safe_decode_text(key) == prop_name
                ):
                    return self._js_value_type_class_qn(
                        child.child_by_field_name(cs.FIELD_VALUE),
                        factory_module,
                        depth + 1,
                    )
            elif (
                child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER
                and safe_decode_text(child) == prop_name
            ):
                return self._js_identifier_class_qn(child, factory_module, depth + 1)
        return None

    @staticmethod
    def _js_own_returned_objects(fn_node: Node) -> list[Node]:
        objects: list[Node] = []
        stack: list[Node] = list(fn_node.children)
        while stack:
            node = stack.pop()
            if node.type in cs.JS_TS_FUNCTION_NODES:
                continue
            if node.type == cs.TS_RETURN_STATEMENT:
                for child in node.named_children:
                    if child.type == cs.TS_OBJECT:
                        objects.append(child)
            stack.extend(node.children)
        return objects

    @staticmethod
    def _js_direct_return_ctor(fn_node: Node) -> str | None:
        stack: list[Node] = list(fn_node.children)
        while stack:
            node = stack.pop()
            if node.type in cs.JS_TS_FUNCTION_NODES:
                # A nested callable's returns are its own.
                continue
            if node.type == cs.TS_RETURN_STATEMENT:
                for child in node.named_children:
                    if child.type == cs.TS_NEW_EXPRESSION:
                        return js_ts_utils.extract_constructor_name(child)
            stack.extend(node.children)
        return None

    def _js_function_node_by_qn(self, fn_qn: str) -> Node | None:
        # Locate a top-level FUNCTION's definition node from its qn (the
        # class.method finder cannot: it splits qns as module.Class.method).
        module_qn, _, fn_name = fn_qn.rpartition(cs.SEPARATOR_DOT)
        type_inference = self._resolver.type_inference
        file_path = type_inference.module_qn_to_file_path.get(module_qn)
        if file_path is None or not (entry := type_inference.ast_cache.load(file_path)):
            return None
        root_node, _language = entry
        return js_bindings.named_functions(root_node).get(fn_name)

    def _js_symbol_name_qn(self, name: str, module_qn: str) -> str | None:
        # The qn a bare name denotes in this module: the imported member's qn
        # when the name is imported, else the module-local qn.
        import_map = self._resolver.import_processor.import_mapping.get(module_qn)
        if import_map is not None and name in import_map:
            imported = import_map[name]
            if imported.endswith(f"{cs.SEPARATOR_DOT}{name}"):
                return imported
            return f"{imported}{cs.SEPARATOR_DOT}{name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{name}"

    def finalize_js_symbol_bindings(self) -> None:
        # Keep only installations whose key resolves to a REGISTERED symbol
        # constant: an arbitrary computed key (`obj[i]`) must never join the
        # index, or unrelated subscript reads would type by it.
        for module_qn, sym_name, class_qn in self._js_symbol_assignments:
            sym_qn = self._js_symbol_name_qn(sym_name, module_qn)
            if sym_qn is not None and sym_qn in self.js_symbol_constants:
                self.js_symbol_member_types.setdefault(sym_qn, set()).add(class_qn)
        self._js_symbol_assignments.clear()

    def _js_symbol_member_targets(
        self, call_node: Node, module_qn: str
    ) -> tuple[list[tuple[str, str]], bool] | None:
        # `recv[kSym].method(...)` where kSym resolves to a symbol constant
        # with recorded installations: exactly ONE installed class binds the
        # call (unique=True, CALLS); several installed classes are a sound
        # candidate set (unique=False, the caller references each class's
        # matching method so liveness holds without guessing). Returns None
        # for every other shape, leaving the generic path untouched. The
        # bare-name trie fallback is SUPPRESSED for the recognised shapes:
        # the issue #989 receiver is statically unknowable by name and the
        # trie falsely revives same-named methods on unrelated classes.
        parts = _js_symbol_member_call_parts(call_node)
        if parts is None:
            return None
        sym_name, method = parts
        sym_qn = self._js_symbol_name_qn(sym_name, module_qn)
        if sym_qn is None or sym_qn not in self.js_symbol_constants:
            return None
        # The shape is recognised from here on: a symbol-keyed receiver is
        # never resolvable by NAME, so the generic path (whose trie fallback
        # guesses one same-named method) is suppressed. With typed
        # installations the index provides the targets; without any, the
        # site degrades to REFERENCES over EVERY method of that name, so
        # liveness survives while no wrong-class CALLS is fabricated.
        registry = self._resolver.function_registry
        classes = self.js_symbol_member_types.get(sym_qn)
        if classes:
            targets: list[tuple[str, str]] = [
                (registry[method_qn], method_qn)
                for class_qn in sorted(classes)
                if (method_qn := f"{class_qn}{cs.SEPARATOR_DOT}{method}") in registry
                and self._js_member_is_exposed(class_qn, method)
            ]
            return targets, len(classes) == 1
        name_matches = self._resolver.type_inference.simple_name_lookup.get(
            method, set()
        )
        fan: list[tuple[str, str]] = [
            (registry[qn], qn)
            for qn in sorted(name_matches)
            if qn in registry
            and registry[qn] in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
        ]
        return fan, False

    def _js_fn_call_receiver_binding(
        self, call_node: Node, module_qn: str
    ) -> FunctionLocation | None:
        # For `fn.call(...)` / `fn.apply(...)` (only: `.bind` produces a value
        # without invoking), peel casts/parens off the receiver and, when it is
        # a bare identifier, resolve what that identifier DENOTES at this site
        # by lexical scoping. `.bind`-style member receivers and computed
        # receivers never qualify.
        fn_child = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if fn_child is None or fn_child.type != cs.TS_MEMBER_EXPRESSION:
            return None
        prop = fn_child.child_by_field_name(cs.FIELD_PROPERTY)
        if prop is None or safe_decode_text(prop) not in (
            cs.JS_METHOD_CALL,
            cs.JS_METHOD_APPLY,
        ):
            return None
        obj = fn_child.child_by_field_name(cs.FIELD_OBJECT)
        if obj is None:
            return None
        obj = self._unwrap_ts_value(obj)
        if obj.type != cs.TS_IDENTIFIER:
            return None
        name = safe_decode_text(obj)
        if not name:
            return None
        return self._js_lexical_function_binding(call_node, name, module_qn)

    def _js_lexical_function_binding(
        self, site: Node, name: str, module_qn: str
    ) -> FunctionLocation | None:
        # UNIQUE-RELEVANT-BINDING resolution: collect every INTRODUCTION of
        # every name in the file once (cached), each with the container whose
        # span bounds where it is usable, then keep only the introductions
        # whose container encloses THIS site (a file-wide container always
        # qualifies). The receiver resolves only when exactly ONE relevant
        # introduction remains and it IS a function registered by the
        # definition pass; two or more relevant introductions, or a
        # non-function one, mean the runtime value is unknowable and nothing
        # is emitted. Shadowing, `var` hoisting, switch/for/catch scoping and
        # duplicate declarations all collapse into "a second relevant
        # introduction", with no scope simulation; an introduction confined
        # to a SIBLING scope is irrelevant here and cannot suppress.
        root = site
        while root.parent is not None:
            root = root.parent
        bindings = self._js_file_receiver_bindings.get(module_qn)
        if bindings is None:
            bindings = self._js_collect_file_bindings(root)
            self._js_file_receiver_bindings[module_qn] = bindings
        entries = bindings.get(name)
        if not entries:
            return None
        relevant = [
            (fn_node, container)
            for fn_node, container in entries
            if container is None
            or container.start_byte <= site.start_byte < container.end_byte
        ]
        if len(relevant) != 1:
            return None
        fn_node, _container = relevant[0]
        if fn_node is None:
            return None
        return self._recorded_caller(fn_node, module_qn)

    def _js_collect_file_bindings(
        self, root: Node
    ) -> dict[str, list[tuple[Node | None, Node | None]]]:
        return js_bindings.JsFileBindingCollector(self._unwrap_ts_value).collect(root)

    def _class_pass_owns(
        self, func_node: Node, lang_config: LanguageSpec, language: cs.SupportedLanguage
    ) -> bool:
        # Mirrors _process_calls_in_classes: a class with a body that it can
        # name gets a class pass; everything inside it is that pass's.
        class_node = enclosing_class_node(func_node, lang_config)
        if class_node is None:
            return False
        if class_node.child_by_field_name(cs.FIELD_BODY) is None:
            return False
        class_name = self._get_class_name_for_node(class_node, language)
        if not class_name and language in _JS_TS_LANGUAGES:
            class_name = js_ts_utils.class_binding_name(class_node)
        return bool(class_name)
