from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..capture import ALL_ENABLED, CaptureSelection
from ..language_spec import LanguageSpec
from ..parser_loader import COMBINED_FUNC_CLASS_QUERIES
from ..services import IngestorProtocol
from ..types_defs import (
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageQueries,
    NodeType,
)
from ..utils.path_utils import cached_relative_path
from .call_resolver import CallResolver
from .class_ingest.identity import build_nested_qualified_name_for_class
from .cpp import utils as cpp_utils
from .cpp.type_inference import CppTypeInferenceEngine
from .csharp import type_inference as csharp_ti
from .dart import utils as dart_utils
from .flow_access import FlowProcessor
from .go import utils as go_utils
from .import_processor import ImportProcessor
from .io_access import IOAccessProcessor
from .java import utils as java_utils
from .lua import utils as lua_utils
from .rs import utils as rs_utils
from .type_inference import TypeInferenceEngine
from .utils import (
    cpp_parameter_names,
    function_span_key,
    get_function_captures,
    go_parameter_names,
    is_method_node,
    js_ts_parameter_names,
    python_parameter_names,
    safe_decode_text,
    sorted_captures,
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
# Languages with argument-REFERENCE edges but no interprocedural
# callable-param flow: passing a function keeps it reachable (REFERENCES),
# while invocation edges stay with the flow languages.
_ARG_REF_ONLY_LANGUAGES = frozenset(
    {cs.SupportedLanguage.CSHARP, cs.SupportedLanguage.DART}
)
_DART_VALUE_WRAPPER_TYPES = frozenset(
    {cs.TS_DART_LIST_LITERAL, cs.TS_DART_SET_OR_MAP_LITERAL, cs.TS_DART_ARGUMENT}
)
# Scopes that bound a Dart local/parameter declaration for getter-read
# shadowing; a declaration hides a same-name getter only inside them. A loop
# variable scopes its for_statement, a catch parameter its try_statement.
_DART_SHADOW_SCOPE_TYPES = frozenset(
    {
        cs.TS_DART_BLOCK,
        cs.TS_DART_FUNCTION_EXPRESSION,
        cs.TS_DART_LOCAL_FUNCTION_DECLARATION,
        cs.TS_DART_FOR_STATEMENT,
        cs.TS_DART_TRY_STATEMENT,
    }
)
_DART_LOCAL_DECLARATION_TYPES = frozenset(
    {
        cs.TS_DART_INITIALIZED_VARIABLE_DEFINITION,
        cs.TS_DART_INITIALIZED_IDENTIFIER,
    }
)
# Statement scopes whose binder is live only AFTER its declaration: the
# for-in iterable and the try body precede theirs and read the getter. A
# block-scoped local needs no such split: Dart scopes it to the whole block
# and rejects reads before the declaration at compile time.
_DART_STATEMENT_SCOPE_TYPES = frozenset(
    {cs.TS_DART_FOR_STATEMENT, cs.TS_DART_TRY_STATEMENT}
)
# Parents whose direct identifier is never a value read: member/cascade
# selectors carry the chain passes' members, a label names a parameter,
# catch parameters only BIND names, and a signature's identifier is the
# DECLARED name (the module pass walks class bodies for field initializers,
# so signatures are in view there).
_DART_NON_READ_PARENT_TYPES = (
    frozenset(
        {
            cs.TS_DART_LABEL,
            cs.TS_DART_UNCONDITIONAL_ASSIGNABLE_SELECTOR,
            cs.TS_DART_CONDITIONAL_ASSIGNABLE_SELECTOR,
            cs.TS_DART_CASCADE_SELECTOR,
            cs.TS_DART_CATCH_PARAMETERS,
        }
    )
    | cs.DART_SIGNATURE_TYPES
)


def _dart_scope_span(decl: Node, walk_root: Node) -> tuple[int, int]:
    # The byte span a declaration shadows: its nearest enclosing scope, with
    # statement scopes (for/try) starting at the declaration END because the
    # for-in iterable and the try body precede their binder. No scope
    # ancestor (a signature parameter) falls through to the whole body.
    anc = decl.parent
    while anc is not None and anc is not walk_root:
        if anc.type in _DART_SHADOW_SCOPE_TYPES:
            if anc.type in _DART_STATEMENT_SCOPE_TYPES:
                return (decl.end_byte, anc.end_byte)
            return (anc.start_byte, anc.end_byte)
        anc = anc.parent
    return (walk_root.start_byte, walk_root.end_byte)


def _dart_is_owned_function_body(node: Node) -> bool:
    # A function_body belonging to a top-level function or class member has
    # its OWN caller pass; a closure's or local function's body does not (a
    # Dart lambda's reads attribute to the enclosing scope), so only bodies
    # NOT under a nested-scope node are skipped by the module walks.
    return (
        node.type == cs.TS_DART_FUNCTION_BODY
        and (parent := node.parent) is not None
        and parent.type not in cs.DART_NESTED_SCOPE_NODE_TYPES
    )


def _dart_declared_names(node: Node) -> list[str]:
    # The name(s) a parameter, local declaration, loop variable, catch
    # parameter, or pattern binding binds; anything else declares nothing.
    node_type = node.type
    if node_type in (cs.TS_DART_FORMAL_PARAMETER, cs.TS_DART_CATCH_PARAMETERS):
        return [
            name
            for child in node.named_children
            if child.type == cs.TS_DART_IDENTIFIER and (name := safe_decode_text(child))
        ]
    if node_type in _DART_LOCAL_DECLARATION_TYPES:
        declared = next(
            (
                child
                for child in node.named_children
                if child.type == cs.TS_DART_IDENTIFIER
            ),
            None,
        )
        if declared is not None and (name := safe_decode_text(declared)):
            return [name]
    if node_type == cs.TS_DART_FOR_LOOP_PARTS:
        # `for (final total in xs)`: the FIRST identifier is the loop
        # variable, the second the iterable expression.
        first = next(
            (
                child
                for child in node.named_children
                if child.type == cs.TS_DART_IDENTIFIER
            ),
            None,
        )
        if first is not None and (name := safe_decode_text(first)):
            return [name]
    if node_type == cs.TS_DART_PATTERN_VARIABLE_DECLARATION:
        return _dart_pattern_bound_names(node)
    return []


def _dart_pattern_bound_names(node: Node) -> list[str]:
    # `var (alpha, beta) = rhs` binds every identifier inside the *_pattern
    # subtree; the RHS expression binds nothing.
    names: list[str] = []
    stack = [
        child
        for child in node.named_children
        if child.type.endswith(cs.DART_PATTERN_NODE_SUFFIX)
    ]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_DART_IDENTIFIER and (
            name := safe_decode_text(current)
        ):
            names.append(name)
        stack.extend(current.named_children)
    return names


def _dart_is_bare_read(node: Node) -> bool:
    # A bare-identifier getter read, INCLUDING a receiver-position chain head:
    # `_wonders.length` reads the `_wonders` getter even though the member
    # pass only emits for the final member (issue #873). Only a head invoked
    # directly (`f(x)` = identifier + selector(argument_part)) belongs to the
    # call pass; a label's identifier names a parameter, and a selector's
    # own identifier is the member the chain pass already handled.
    following = node.next_named_sibling
    if following is not None and (
        following.type == cs.TS_DART_ARGUMENT_PART
        or (
            following.type == cs.TS_DART_SELECTOR
            and any(
                child.type == cs.TS_DART_ARGUMENT_PART
                for child in following.named_children
            )
        )
    ):
        return False
    parent = node.parent
    if parent is None or parent.type in _DART_NON_READ_PARENT_TYPES:
        return False
    if parent.type == cs.TS_DART_FOR_LOOP_PARTS:
        # The FIRST identifier of a for-in is the loop BINDER, not a read;
        # the iterable position after it reads normally.
        first = next(
            (
                child
                for child in parent.named_children
                if child.type == cs.TS_DART_IDENTIFIER
            ),
            None,
        )
        if first is not None and first.start_byte == node.start_byte:
            return False
    return True


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
# Qualified-name prefix marking a resolved callee as a builtin rather than a
# first-party function whose body the call chain can be followed into.
_BUILTIN_QN_PREFIX = f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}"
# C/C++ expression nodes whose call name is a synthesized operator_*, the
# ones whose operand type may direct or suppress the binding.
_CPP_OPERATOR_EXPRESSION_TYPES = frozenset(
    {
        cs.TS_CPP_BINARY_EXPRESSION,
        cs.TS_CPP_UNARY_EXPRESSION,
        cs.TS_CPP_UPDATE_EXPRESSION,
    }
)
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
# Inline function values in an object literal (`{ onSuccess: () => {} }`): the
# JS/TS definition pass registers these as their own nodes named by the key
# (scope.onSuccess), so a passed object of callbacks must reference each or
# every TanStack-style callback reports as dead.
_INLINE_FUNC_VALUE_TYPES = frozenset({cs.TS_ARROW_FUNCTION, cs.TS_FUNCTION_EXPRESSION})


def _scope_qn_candidates(scope_qn: str) -> list[str]:
    # The scope itself plus its duplicate-variant-stripped form (`useStore@27`
    # -> `useStore`): the def pass registers nested/anon members under the
    # NATURAL qn while the caller may carry the variant suffix. Registry-guarded,
    # so a scope without a twin adds nothing.
    last = scope_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
    if cs.DUP_QN_MARKER not in last:
        return [scope_qn]
    natural = scope_qn[: len(scope_qn) - len(last)] + last.split(cs.DUP_QN_MARKER, 1)[0]
    return [scope_qn, natural]


def _first_class_value_children(node: Node, is_dart: bool) -> list[Node] | None:
    # The wrapped value nodes a container/branch node hands onward, or None
    # for a leaf that IS the first-class value.
    if node.type in _PY_VALUE_WRAPPER_TYPES:
        return list(node.named_children)
    if is_dart and node.type in _DART_VALUE_WRAPPER_TYPES:
        return _dart_collection_values(node)
    if node.type == cs.TS_PY_DICTIONARY:
        return _py_dict_values(node)
    if node.type == cs.TS_PY_CONDITIONAL_EXPRESSION:
        return _conditional_result_operands(node, is_dart)
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


def _dart_collection_values(node: Node) -> list[Node]:
    # A Dart MAP stores each value inside a `pair` node's value field
    # (`{"tap": onTap}`); a set exposes its elements directly; a typed
    # literal's `type_arguments` child carries types, never values.
    children: list[Node] = []
    for child in node.named_children:
        if child.type == cs.TS_DART_TYPE_ARGUMENTS:
            continue
        if child.type == cs.TS_DART_PAIR:
            if (pair_value := child.child_by_field_name(cs.FIELD_VALUE)) is not None:
                children.append(pair_value)
            continue
        children.append(child)
    return children


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
    # Dart orders [condition, consequence, alternative]; Python orders
    # [body, condition, alternative]. Pick the two result operands, never
    # the truthiness-tested one.
    return [operands[1], operands[2]] if is_dart else [operands[0], operands[2]]


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
    # Dart has no call-expression node: a selector/cascade_section wraps its
    # arguments in an argument_part holding the real `arguments` node one
    # level down.
    for part in call_node.named_children:
        if part.type == cs.TS_DART_ARGUMENT_PART:
            return next(
                (
                    grand
                    for grand in part.named_children
                    if grand.type == cs.TS_DART_ARGUMENTS
                ),
                None,
            )
    return None


def _add_dart_named_argument(child: Node, keyword: dict[str, Node]) -> None:
    # A named value is a `named_argument` whose label child names it and
    # whose last named child is the expression.
    label = next(
        (grand for grand in child.named_children if grand.type == cs.TS_DART_LABEL),
        None,
    )
    value_node = child.named_children[-1] if child.named_children else None
    name_node = (
        label.named_children[0] if label is not None and label.named_children else None
    )
    if (
        name_node is not None
        and value_node is not None
        and value_node is not label
        and (name := safe_decode_text(name_node)) is not None
    ):
        keyword[name] = value_node


def _add_py_keyword_argument(child: Node, keyword: dict[str, Node]) -> None:
    name_node = child.child_by_field_name(cs.FIELD_NAME)
    value_node = child.child_by_field_name(cs.FIELD_VALUE)
    if (
        name_node is not None
        and value_node is not None
        and (name := safe_decode_text(name_node)) is not None
    ):
        keyword[name] = value_node


class CallProcessor:
    __slots__ = (
        "ingestor",
        "repo_path",
        "project_name",
        "module_qn_to_file_path",
        "_path_to_module_qn",
        "cpp_out_of_class_methods",
        "function_locations",
        "macro_qns",
        "_resolver",
        "_flow_param_names",
        "_flow_args",
        "_returned_callables",
        "_factory_calls",
        "_io_processor",
        "_flow_processor",
    )

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        class_inheritance: dict[str, list[str]],
        type_aliases: dict[str, str] | None = None,
        interface_implementers: dict[str, set[str]] | None = None,
        capture: CaptureSelection | None = None,
        module_qn_to_file_path: dict[str, Path] | None = None,
        cpp_out_of_class_methods: dict[tuple[str, int], tuple[str, str]] | None = None,
        function_locations: dict[FunctionSpanKey, FunctionLocation] | None = None,
        macro_qns: set[str] | None = None,
    ) -> None:
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.module_qn_to_file_path = module_qn_to_file_path or {}
        self._path_to_module_qn: dict[Path, str] | None = None
        self.cpp_out_of_class_methods = cpp_out_of_class_methods or {}
        self.function_locations = function_locations or {}
        self.macro_qns = macro_qns if macro_qns is not None else set()

        self._resolver = CallResolver(
            function_registry=function_registry,
            import_processor=import_processor,
            type_inference=type_inference,
            class_inheritance=class_inheritance,
            type_aliases=type_aliases,
            interface_implementers=interface_implementers,
        )
        # Inter-procedural callable-parameter flow: ordered params per function and
        # the per-call-site argument bindings, resolved to a fixpoint in finalize.
        self._flow_param_names: dict[str, list[str]] = {}
        self._flow_args: list[_CallableFlowArg] = []
        # Return-value / factory tracing: functions each function may return
        # (nested closures), and call sites `x = factory(...); x(cb)` where cb
        # flows into the returned closure's callable parameter. Resolved to a
        # fixpoint in finalize, so factory and call site may be in any order.
        self._returned_callables: dict[str, set[str]] = {}
        self._factory_calls: list[_FactoryCall] = []
        selection = capture if capture is not None else ALL_ENABLED
        self._io_processor = IOAccessProcessor(
            ingestor,
            import_processor,
            selection=selection,
        )
        self._flow_processor = FlowProcessor(
            ingestor,
            import_processor,
            self._resolver,
            selection=selection,
        )

    def _get_node_name(self, node: Node, field: str = cs.FIELD_NAME) -> str | None:
        name_node = node.child_by_field_name(field)
        if not name_node:
            return None
        text = name_node.text
        return None if text is None else text.decode(cs.ENCODING_UTF8)

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
        if language == cs.SupportedLanguage.RUST:
            return self._calls_owned_by(
                func_node, owned_func_nodes, all_call_nodes, call_starts
            )
        return self._filter_calls_in_node(all_call_nodes, call_starts, func_node)

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
            end = dart_utils.dart_definition_end_byte(container)
        lo = bisect_left(call_starts, start)
        hi = bisect_right(call_starts, end)
        return [n for n in all_call_nodes[lo:hi] if n.end_byte <= end]

    def _filter_top_level_calls(
        self,
        all_call_nodes: list[Node],
        call_starts: list[int],
        func_nodes: list[Node],
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
            body = func_node.child_by_field_name(cs.FIELD_BODY)
            if body is None:
                # a Dart body is a SIBLING of its signature, not a field
                body = dart_utils.dart_body_node(func_node)
            if body is None:
                continue
            for call in self._filter_calls_in_node(all_call_nodes, call_starts, body):
                nested_starts.add(call.start_byte)
        return [c for c in all_call_nodes if c.start_byte not in nested_starts]

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
        resolver = self._resolver
        ensure_rel = self.ingestor.ensure_relationship_batch
        qn_key = cs.KEY_QUALIFIED_NAME
        module_spec = (cs.NodeLabel.MODULE, qn_key, module_qn)
        callable_labels = (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
        alias_map: dict[str, str] | None = None
        for node in nodes:
            parent = node.parent
            if parent is None or parent.type != cs.TS_PY_DECORATED_DEFINITION:
                continue
            if not self._runs_at_module_load(parent):
                continue
            for child in parent.children:
                if child.type != cs.TS_PY_DECORATOR:
                    continue
                name = self._bare_decorator_name(child)
                if not name:
                    continue
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
                if callee and callee[0] in callable_labels:
                    ensure_rel(
                        module_spec,
                        cs.RelationshipType.CALLS,
                        (callee[0], qn_key, callee[1]),
                    )

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
        if file_name in (cs.INIT_PY, cs.MOD_RS):
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
            resolver = self._resolver
            self._collect_ctor_field_metadata(root_node, module_qn)
            if (
                func_class_captures_cache is not None
                and file_path in func_class_captures_cache
            ):
                call_nodes = func_class_captures_cache[file_path].get(cs.CAPTURE_CALL)
            else:
                call_nodes = None
            if call_nodes is None:
                call_nodes, _ = self._collect_all_call_nodes(
                    root_node, language, queries
                )
            registry = resolver.function_registry
            callable_labels = (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
            for call_node in call_nodes:
                positional, keyword = self._parse_call_arguments(call_node)
                if not positional and not keyword:
                    continue
                name = self._get_call_target_name(call_node)
                if not name:
                    continue
                callee = resolver.resolve_function_call(name, module_qn)
                if not callee or callee[0] != cs.NodeLabel.CLASS:
                    continue
                arg_entries: list[tuple[int | str, Node]] = list(enumerate(positional))
                arg_entries.extend(keyword.items())
                for key, value_node in arg_entries:
                    if not (value_text := safe_decode_text(value_node)):
                        continue
                    bound = resolver.resolve_function_call(value_text, module_qn)
                    if bound and bound[0] in callable_labels and bound[1] in registry:
                        resolver.record_pending_field_binding(callee[1], key, bound[1])
        except Exception as e:
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)

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
        resolver = self._resolver
        registry = resolver.function_registry
        stack: list[tuple[Node, str | None]] = [
            (child, None) for child in root_node.children
        ]
        while stack:
            node, enclosing_qn = stack.pop()
            if node.type == cs.TS_PY_DECORATED_DEFINITION:
                stack.extend((c, enclosing_qn) for c in node.children)
                continue
            if node.type == cs.TS_PY_FUNCTION_DEFINITION:
                continue
            if node.type != cs.TS_PY_CLASS_DEFINITION:
                stack.extend((c, enclosing_qn) for c in node.children)
                continue
            name_node = node.child_by_field_name(cs.FIELD_NAME)
            class_name = safe_decode_text(name_node) if name_node else None
            body = node.child_by_field_name(cs.FIELD_BODY)
            if not class_name or body is None:
                continue
            if enclosing_qn is not None:
                candidate_qn = f"{enclosing_qn}{cs.SEPARATOR_DOT}{class_name}"
                class_qn = (
                    candidate_qn
                    if registry.get(candidate_qn) == cs.NodeLabel.CLASS
                    else None
                )
            else:
                resolved = resolver.resolve_function_call(class_name, module_qn)
                class_qn = (
                    resolved[1]
                    if resolved and resolved[0] == cs.NodeLabel.CLASS
                    else None
                )
            # Descend into the body with THIS class as the enclosing scope so
            # its nested classes resolve; skip metadata when unresolved.
            stack.extend((c, class_qn) for c in body.children)
            if class_qn is None:
                continue
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
            match child.type:
                case cs.TS_PY_IDENTIFIER:
                    name = safe_decode_text(child)
                case cs.TS_PY_DEFAULT_PARAMETER | cs.TS_PY_TYPED_DEFAULT_PARAMETER:
                    name_node = child.child_by_field_name(cs.FIELD_NAME)
                    name = safe_decode_text(name_node) if name_node else None
                case cs.TS_PY_TYPED_PARAMETER:
                    inner = child.named_children[0] if child.named_children else None
                    name = (
                        safe_decode_text(inner)
                        if inner is not None and inner.type == cs.TS_PY_IDENTIFIER
                        else None
                    )
                case _:
                    # *args/**kwargs/keyword_separator never bind fields.
                    name = None
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

        try:
            module_qn = self._module_qn(file_path, relative_path)

            call_name_cache: dict[int, str | None] = {}

            if (
                func_class_captures_cache is not None
                and file_path in func_class_captures_cache
            ):
                combined_captures = func_class_captures_cache[file_path]
            else:
                combined_query = COMBINED_FUNC_CLASS_QUERIES.get(language)
                if combined_query:
                    cursor = QueryCursor(combined_query)
                    combined_captures = sorted_captures(cursor, root_node)
                else:
                    combined_captures = {}

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
                    [n.start_byte for n in sorted_func_nodes]
                    if sorted_func_nodes
                    else None
                )
            else:
                call_starts = None
                func_node_starts = None

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
            if language == cs.SupportedLanguage.DART and (
                dart_prop_names := self._resolver.function_registry.property_names()
            ):
                # Field initializers execute at construction time even in a
                # file holding nothing but classes (no module caller pass
                # runs there), so each class subtree is scanned at FILE
                # level, before the no-calls early return.
                dart_config = queries[language][cs.QUERY_CONFIG]
                module_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
                for child in root_node.named_children:
                    if child.type in dart_config.class_node_types:
                        self._ingest_dart_class_initializer_reads(
                            child,
                            module_spec,
                            module_qn,
                            module_qn,
                            None,
                            dart_config,
                            dart_prop_names,
                        )
            if language == cs.SupportedLanguage.PYTHON or language in _JS_TS_LANGUAGES:
                collection_boundaries = self._flow_scope_boundaries(
                    queries[language][cs.QUERY_CONFIG]
                )
                if language == cs.SupportedLanguage.PYTHON:
                    # A Python class body executes at import time, so a
                    # dispatch table stored as a class ATTRIBUTE (django's
                    # backend `data_types = {"CharField": _get_varchar_...}`)
                    # is module-load wiring like a module-level table; the scan
                    # descends through classes but still stops at function
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
                )
                # A module-scope first-class assignment (registry_handler =
                # handle_event, module.exports.run = run) references its target
                # even in a file with no calls, so scan before the no-calls
                # early return.
                self._ingest_assignment_function_references(
                    root_node,
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                    module_qn,
                    None,
                    None,
                    self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
                )
            if language == cs.SupportedLanguage.GO:
                # A module-scope Go func map/slice (var funcMap = map[...]{...})
                # keeps its function entries reachable; scan before the no-calls
                # early return so a file defining only funcs and a table counts.
                self._ingest_go_composite_function_references(
                    root_node,
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                    module_qn,
                    None,
                    None,
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
            if not all_call_nodes and language not in (
                cs.SupportedLanguage.CSHARP,
                cs.SupportedLanguage.CPP,
                cs.SupportedLanguage.DART,
            ):
                # A file with no call expressions has nothing further to
                # process, except in C#, where a class can still READ
                # properties (`return Size;`), C++, where a ctor's
                # member initializer list (`: buffer(g, 0)`) runs base
                # ctors without any call_expression node, and Dart, where
                # a getter body can read another getter (`=> _wonders.length`,
                # issue #873); these passes run per caller inside class
                # processing, so they proceed.
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
            if sorted_func_nodes and call_starts is not None:
                # JS/TS: exclude only calls owned by ATTRIBUTABLE functions, so a
                # call nested purely in anonymous scopes (`create((set) => ({
                # inc: () => set((state) => ...) }))`, zustand's store shape) is
                # processed at module scope instead of by nobody: an anon arrow
                # gets no caller pass, and a call inside a NAMED function is
                # still excluded because that function's flat filter owns it.
                exclusion_nodes = (
                    self._attributable_func_nodes(sorted_func_nodes, language)
                    if language in _JS_TS_LANGUAGES
                    else sorted_func_nodes
                )
                module_calls = self._filter_top_level_calls(
                    all_call_nodes, call_starts, exclusion_nodes
                )
            else:
                module_calls = all_call_nodes
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

        except Exception as e:
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)

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
        owned_func_nodes = self._attributable_func_nodes(func_nodes, language)
        for func_node in func_nodes:
            if has_classes and self._is_method(func_node, lang_config):
                continue

            if language in _C_FAMILY_LANGUAGES:
                # A macro-invocation artifact the ingest pass declined to
                # register (no class bears its name) must not become a call
                # target either; mirror ingest's decision via the recorded
                # locations so the two passes never diverge.
                if (
                    language == cs.SupportedLanguage.CPP
                    and cpp_utils.is_macro_invocation_artifact(func_node)
                    and self._recorded_caller(func_node, module_qn) is None
                ):
                    continue
                func_name = cpp_utils.extract_function_name(func_node)
            else:
                func_name = self._get_node_name(func_node)
            if not func_name and language in _JS_TS_LANGUAGES:
                func_name = self._js_ts_arrow_binding_name(func_node)
            if (
                not func_name
                and language == cs.SupportedLanguage.LUA
                and func_node.type == cs.TS_LUA_FUNCTION_DEFINITION
            ):
                # A function expression bound to a variable or table field
                # (`local f = function()`, `M.f = function()`) has no name field;
                # the definition pass names it after its assignment target, so
                # recover the same name here or the whole body is skipped.
                func_name = lua_utils.extract_assigned_name(
                    func_node,
                    accepted_var_types=(cs.TS_DOT_INDEX_EXPRESSION, cs.TS_IDENTIFIER),
                )
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
                    continue
                func_name = recorded.qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
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
                continue
            # An out-of-line C++ method definition (`Ret Class::method() {...}`
            # at namespace/file scope) is bound by the definition pass to its
            # class node (qn `class_qn.method`). Attribute its body's calls to
            # that method node, not a phantom module-rooted qn, so the CALLS
            # edges join to a real node.
            if language == cs.SupportedLanguage.CPP and (
                bound := self._cpp_out_of_class_method_caller(
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
                continue
            # A Go receiver method (`func (t T) m()`) is declared at file scope
            # but the definition pass binds it to its receiver type's node
            # (qn `module.T.m`). Attribute its body's calls to that method node,
            # not the receiver-dropping `module.m`, so the CALLS edges join to
            # a real node.
            if language == cs.SupportedLanguage.GO and (
                bound := self._go_receiver_method_caller(
                    func_node, func_name, module_qn
                )
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
                continue
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

    def _go_receiver_method_caller(
        self, func_node: Node, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        # Resolve a Go receiver method to its (method_qn, container_qn),
        # mirroring the definition pass's receiver-type binding. The receiver
        # type resolves to its node qn (same or sibling file in the package),
        # and the registry check ensures the method node exists before
        # overriding the default attribution.
        if not go_utils.is_receiver_method(func_node):
            return None
        receiver_type = go_utils.extract_receiver_type_name(func_node)
        if not receiver_type:
            return None
        container_qn = self._resolver._resolve_class_name(receiver_type, module_qn) or (
            f"{module_qn}{cs.SEPARATOR_DOT}{receiver_type}"
        )
        caller_qn = f"{container_qn}{cs.SEPARATOR_DOT}{method_name}"
        if caller_qn in self._resolver.function_registry:
            return caller_qn, container_qn
        return None

    def _recorded_caller(
        self, func_node: Node, module_qn: str
    ) -> FunctionLocation | None:
        # The registry membership check guards incremental runs, where an
        # unchanged file's locations were not re-recorded this run.
        loc = self.function_locations.get(function_span_key(module_qn, func_node))
        if loc is None or loc.qualified_name not in self._resolver.function_registry:
            return None
        return loc

    def _cpp_out_of_class_method_caller(
        self, func_node: Node, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        # Resolve an out-of-line C++ method definition to its (method_qn,
        # class_qn), mirroring the definition pass's class binding. The leaf
        # class name resolves the class across files (header-declared classes);
        # `endswith(normalized)` guards against a leaf collision binding to the
        # wrong class, and the registry check ensures the method node exists
        # before overriding the default attribution.
        if not cpp_utils.is_out_of_class_method_definition(func_node):
            return None
        # The definition pass already bound this exact definition (keyed by
        # module + start line); reuse its decision so the caller qn matches
        # the registered Method node by construction.
        recorded = self.cpp_out_of_class_methods.get(
            (module_qn, func_node.start_point[0] + 1)
        )
        if recorded is not None:
            method_qn, class_qn = recorded
            if method_qn in self._resolver.function_registry:
                return method_qn, class_qn
        class_name = cpp_utils.extract_class_name_from_out_of_class_method(func_node)
        if not class_name:
            return None
        normalized = class_name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
        leaf = normalized.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        class_qn = self._resolver._resolve_class_name(leaf, module_qn)
        if not class_qn or not class_qn.endswith(normalized):
            return None
        caller_qn = f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
        if caller_qn in self._resolver.function_registry:
            return caller_qn, class_qn
        return None

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
        return self._get_node_name(class_node)

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
        if sorted_func_nodes is not None and func_node_starts is not None:
            body_start = body_node.start_byte
            body_end = body_node.end_byte
            lo = bisect_left(func_node_starts, body_start)
            hi = bisect_right(func_node_starts, body_end)
            method_nodes = [
                n for n in sorted_func_nodes[lo:hi] if n.end_byte <= body_end
            ]
        else:
            method_query = queries[language][cs.QUERY_FUNCTIONS]
            if not method_query:
                return
            method_cursor = QueryCursor(method_query)
            method_captures = sorted_captures(method_cursor, body_node)
            method_nodes = method_captures.get(cs.CAPTURE_FUNCTION, [])
        lang_config = queries[language][cs.QUERY_CONFIG]
        # Only functions that get their own caller node exclude their calls from
        # the enclosing scope; anonymous arrows (skipped below) must not, so
        # their calls bubble up instead of dropping.
        owned_func_nodes = self._attributable_func_nodes(method_nodes, language)
        for method_node in method_nodes:
            # The body byte-range slice also captures functions of a NESTED
            # class (Outer body contains Inner.run); those belong to the
            # nested class and are processed when it is iterated, so skip any
            # whose nearest enclosing class is not this one (else run also
            # emits as the phantom Outer.run).
            if not self._method_in_class_body(method_node, body_node, lang_config):
                continue
            if language in _C_FAMILY_LANGUAGES:
                method_name = cpp_utils.extract_function_name(method_node)
            else:
                method_name = self._get_node_name(method_node)
            if not method_name and language in _JS_TS_LANGUAGES:
                method_name = self._js_ts_arrow_binding_name(method_node)
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
            if current.type in lang_config.function_node_types:
                if (name_node := current.child_by_field_name(cs.FIELD_NAME)) and (
                    name_node.text is not None
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
        # class_ingest.mixin's method-qn build exactly.
        if language != cs.SupportedLanguage.JAVA:
            return func_name
        info = java_utils.extract_method_info(func_node)
        name = info.get(cs.KEY_NAME) or func_name
        parameters = info.get(cs.KEY_PARAMETERS, [])
        param_sig = f"({','.join(parameters)})" if parameters else cs.EMPTY_PARENS
        return f"{name}{param_sig}"

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
        descendant_bodies = [
            body
            for n in sibling_func_nodes
            if n is not func_node
            and n.start_byte >= func_node.start_byte
            and n.end_byte <= func_node.end_byte
            and (body := n.child_by_field_name(cs.FIELD_BODY)) is not None
        ]
        if not descendant_bodies:
            return own
        return [
            call
            for call in own
            if not any(
                body.start_byte <= call.start_byte and call.end_byte <= body.end_byte
                for body in descendant_bodies
            )
        ]

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
            if not class_name:
                continue
            # A C++ class inside a namespace, or a NESTED class (Outer.Inner),
            # is bound by the definition pass through its enclosing scope
            # (qn `module.ns.Class` / `module.Outer.Inner`); the bare
            # `module.class_name` join drops those ancestors, dangling every
            # inline method's CALLS source off a phantom node. Use the SAME
            # builders the definition pass uses so the qns agree.
            if language == cs.SupportedLanguage.CPP:
                class_qn = cpp_utils.build_qualified_name(
                    class_node, module_qn, class_name
                )
            else:
                class_qn = (
                    build_nested_qualified_name_for_class(
                        class_node,
                        module_qn,
                        class_name,
                        queries[language][cs.QUERY_CONFIG],
                    )
                    or f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
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

    def _overlay_match_arm_binding(
        self,
        call_name: str,
        call_node: Node,
        local_var_types: dict[str, str] | None,
        match_arm_bindings: list[tuple[int, int, str, str]],
    ) -> dict[str, str] | None:
        # If the call's receiver base is a match-arm binding whose arm range
        # contains this call, overlay that arm's variant type (shadowing the flat
        # map's last-arm value) so `cmd.apply()` dispatches to the arm's variant.
        # The innermost (smallest) containing arm wins for nested matches.
        if local_var_types is None or cs.SEPARATOR_DOT not in call_name:
            return local_var_types
        base = call_name.split(cs.SEPARATOR_DOT, 1)[0]
        pos = call_node.start_byte
        best: tuple[int, str] | None = None
        for start, end, name, variant_type in match_arm_bindings:
            if name == base and start <= pos < end:
                span = end - start
                if best is None or span < best[0]:
                    best = (span, variant_type)
        if best is None or local_var_types.get(base) == best[1]:
            return local_var_types
        return {**local_var_types, base: best[1]}

    def _cpp_operator_operand_name(self, call_node: Node) -> str | None:
        # The receiver-analog operand of an operator expression: the LEFT
        # side of a binary op, the sole argument of a unary/update op. Only
        # a bare identifier is returned; anything more complex stays with
        # the legacy paths.
        field = (
            cs.FIELD_LEFT
            if call_node.type == cs.TS_CPP_BINARY_EXPRESSION
            else cs.TS_FIELD_ARGUMENT
        )
        operand = call_node.child_by_field_name(field)
        if operand is None or operand.type != cs.TS_IDENTIFIER:
            return None
        return safe_decode_text(operand)

    def _macro_call_name(self, ident: Node) -> str | None:
        # Reconstruct a `<recv>.method` chain from a macro token stream by walking
        # the method identifier's preceding siblings over `("." <ident|self>)*`.
        # `server . run` -> "server.run"; `self . shutdown . recv` ->
        # "self.shutdown.recv". A method with no preceding `.` stays bare.
        if not (method := ident.text.decode(cs.ENCODING_UTF8) if ident.text else None):
            return None
        parts = [method]
        cur = ident.prev_sibling
        while cur is not None and cur.type == cs.TS_RS_TOKEN_DOT:
            if (recv := cur.prev_sibling) is None or (
                recv.type not in cs.RS_MACRO_RECEIVER_TYPES
            ):
                break
            if not recv.text:
                break
            parts.append(recv.text.decode(cs.ENCODING_UTF8))
            cur = recv.prev_sibling
        parts.reverse()
        return cs.SEPARATOR_DOT.join(parts)

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
            return self._macro_call_name(call_node)
        # A Dart call node is a selector/cascade_section holding the
        # argument_part; the target name lives in the PRECEDING sibling
        # chain, not inside the node.
        if language == cs.SupportedLanguage.DART:
            return dart_utils.dart_call_name(call_node)
        if func_child := call_node.child_by_field_name(cs.TS_FIELD_FUNCTION):
            match func_child.type:
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
                        return func_child.text.decode(cs.ENCODING_UTF8)
                case cs.TS_GENERIC_FUNCTION:
                    # turbofish: unwrap to the underlying callee identifier
                    inner = func_child.child_by_field_name(cs.TS_FIELD_FUNCTION)
                    if inner and inner.text:
                        return inner.text.decode(cs.ENCODING_UTF8)
                case cs.TS_RS_FIELD_EXPRESSION if language == cs.SupportedLanguage.RUST:
                    # Rust member call `a.b.method()`: use the full dotted receiver
                    # chain as the call name so the resolver can map the receiver to
                    # its inferred type (`self.shutdown.is_shutdown`, `cmd.apply`). A
                    # chain containing a call (`x.f().g`) is left to the chained-call
                    # path; a paren-free chain ends at the bare-method trie fallback
                    # when the receiver type is unknown.
                    if (text := func_child.text) is not None:
                        return text.decode(cs.ENCODING_UTF8)
                case cs.TS_CPP_FIELD_EXPRESSION:
                    field_node = func_child.child_by_field_name(cs.FIELD_FIELD)
                    if field_node and field_node.text:
                        method = field_node.text.decode(cs.ENCODING_UTF8)
                        # Prepend a simple-identifier receiver (`obj->m`/`obj.m`
                        # -> `obj.m`) so the resolver can map obj to its type and
                        # bind the correct class method; a `.`-joined two-part name
                        # falls back to the bare method-name trie when the receiver
                        # type is unknown. Complex receivers (chains, calls, `this`)
                        # keep the bare method name.
                        arg = func_child.child_by_field_name(cs.TS_FIELD_ARGUMENT)
                        if (
                            arg is not None
                            and arg.type == cs.TS_IDENTIFIER
                            and arg.text
                        ):
                            receiver = arg.text.decode(cs.ENCODING_UTF8)
                            return f"{receiver}{cs.SEPARATOR_DOT}{method}"
                        # A factory-call receiver (`parser(ia, cb).parse(...)`,
                        # nlohmann's basic_json::parse) is a call_expression on a
                        # bare identifier: emit the chain form so the resolver can
                        # type the factory's return and bind the method on it. A
                        # template/qualified callee (`Reader<T>(...)`,
                        # `detail::Reader<T>(...)`) is a CONSTRUCTOR TEMPORARY: the
                        # callee names the receiver's class directly. Deeper receiver
                        # chains keep the bare method-name trie fallback.
                        if (
                            arg is not None
                            and arg.type == cs.TS_CPP_CALL_EXPRESSION
                            and (
                                callee := arg.child_by_field_name(cs.TS_FIELD_FUNCTION)
                            )
                            is not None
                            and callee.type
                            in (
                                cs.TS_IDENTIFIER,
                                cs.TS_CPP_TEMPLATE_FUNCTION,
                                cs.TS_CPP_QUALIFIED_IDENTIFIER,
                            )
                            and arg.text
                        ):
                            receiver = arg.text.decode(cs.ENCODING_UTF8)
                            return f"{receiver}{cs.SEPARATOR_DOT}{method}"
                        return method
                case cs.TS_CSHARP_GENERIC_NAME if (
                    language == cs.SupportedLanguage.CSHARP
                ):
                    # Bare generic call `Handle<TException>(...)`: the callee
                    # name is the identifier without its type arguments
                    # (methods register generic-free). Leaving the `<...>` on
                    # yielded no call name, so the call site vanished from the
                    # graph (Polly's parameterless HandleInner overload
                    # delegating to its Func sibling).
                    if func_child.text is not None:
                        full = func_child.text.decode(cs.ENCODING_UTF8)
                        return full.split(cs.CHAR_ANGLE_OPEN, 1)[0]
                case cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION if (
                    language == cs.SupportedLanguage.CSHARP
                ):
                    # C# member call `recv.Method(...)`: emit the `recv.Method`
                    # chain so the resolver can type `recv` and bind the method on
                    # it; an unknown-type receiver falls back to the bare
                    # method-name trie. `name` is the method, `expression` the
                    # receiver.
                    name_node = func_child.child_by_field_name(cs.FIELD_NAME)
                    expr_node = func_child.child_by_field_name(
                        cs.TS_CSHARP_FIELD_EXPRESSION
                    )
                    if name_node and name_node.text:
                        method = name_node.text.decode(cs.ENCODING_UTF8)
                        # A generic member (`recv.Handle<T>`) registers
                        # generic-free; strip the type arguments so the
                        # name-keyed fallbacks can match.
                        method = method.split(cs.CHAR_ANGLE_OPEN, 1)[0]
                        if expr_node and expr_node.text:
                            receiver = expr_node.text.decode(cs.ENCODING_UTF8)
                            return f"{receiver}{cs.SEPARATOR_DOT}{method}"
                        return method
                case cs.TS_CSHARP_CONDITIONAL_ACCESS_EXPRESSION if (
                    language == cs.SupportedLanguage.CSHARP
                ):
                    # C# conditional call `recv?.Method(...)`: the method name
                    # lives on the member_binding child. Emit the same
                    # `recv.Method` chain as the unconditional form so the
                    # resolver (or its exact Roslyn call fact) can bind it.
                    binding = next(
                        (
                            child
                            for child in func_child.children
                            if child.type == cs.TS_CSHARP_MEMBER_BINDING_EXPRESSION
                        ),
                        None,
                    )
                    name_node = (
                        binding.child_by_field_name(cs.FIELD_NAME)
                        if binding is not None
                        else None
                    )
                    if name_node and name_node.text:
                        method = name_node.text.decode(cs.ENCODING_UTF8)
                        receiver_node = (
                            func_child.named_children[0]
                            if (func_child.named_children)
                            else None
                        )
                        if (
                            receiver_node is not None
                            and receiver_node is not binding
                            and receiver_node.text
                        ):
                            receiver = receiver_node.text.decode(cs.ENCODING_UTF8)
                            return f"{receiver}{cs.SEPARATOR_DOT}{method}"
                        return method
                case cs.TS_PARENTHESIZED_EXPRESSION:
                    return self._get_iife_target_name(func_child)

        match call_node.type:
            case cs.TS_NEW_EXPRESSION if language in _JS_TS_LANGUAGES:
                # JS/TS `new Foo(...)` names the class via the `constructor` field
                # (no `function` field). Returning the constructor name routes
                # construction through the normal resolve loop: a first-party class
                # gets INSTANTIATES (+ CALLS to its constructor), and an inline
                # callback argument (`new CancelablePromise(cb)`) is referenced so
                # it is not reported as dead.
                ctor = call_node.child_by_field_name(cs.FIELD_CONSTRUCTOR)
                if ctor is not None and ctor.text is not None:
                    return ctor.text.decode(cs.ENCODING_UTF8)
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
                    return type_node.text.decode(cs.ENCODING_UTF8).split(
                        cs.CHAR_ANGLE_OPEN, 1
                    )[0]
            case cs.TS_CSHARP_IMPLICIT_OBJECT_CREATION_EXPRESSION if (
                language == cs.SupportedLanguage.CSHARP
            ):
                # C# 9 target-typed `new()` has no `type` field; the
                # constructed type is named by the enclosing declaration
                # (issue #773).
                return self._csharp_target_typed_new_name(call_node)
            case (
                cs.TS_CPP_BINARY_EXPRESSION
                | cs.TS_CPP_UNARY_EXPRESSION
                | cs.TS_CPP_UPDATE_EXPRESSION
            ):
                operator_node = call_node.child_by_field_name(cs.FIELD_OPERATOR)
                if operator_node and operator_node.text:
                    operator_text = operator_node.text.decode(cs.ENCODING_UTF8)
                    return cpp_utils.convert_operator_symbol_to_name(operator_text)
            case cs.TS_METHOD_INVOCATION:
                object_node = call_node.child_by_field_name(cs.FIELD_OBJECT)
                name_node = call_node.child_by_field_name(cs.FIELD_NAME)
                if name_node and name_node.text:
                    method_name = name_node.text.decode(cs.ENCODING_UTF8)
                    if not object_node or not object_node.text:
                        return method_name
                    object_text = object_node.text.decode(cs.ENCODING_UTF8)
                    return f"{object_text}{cs.SEPARATOR_DOT}{method_name}"
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
                    return operator_node.text.decode(cs.ENCODING_UTF8)
            # Rust `square!(3)`: the callee lives in the `macro` field (no
            # `function`/`name` field), so the invocation was captured as a
            # call but dropped nameless here; unresolvable even now that
            # macro_rules! definitions register as Function nodes.
            case cs.TS_RS_MACRO_INVOCATION if language == cs.SupportedLanguage.RUST:
                macro_node = call_node.child_by_field_name(cs.FIELD_MACRO)
                if macro_node is not None and macro_node.text is not None:
                    return macro_node.text.decode(cs.ENCODING_UTF8)

        if name_node := call_node.child_by_field_name(cs.FIELD_NAME):
            if name_node.text is not None:
                return name_node.text.decode(cs.ENCODING_UTF8)

        return None

    def _csharp_target_typed_new_name(self, creation_node: Node) -> str | None:
        # The target type of a bare `new()` is named by the enclosing
        # declaration: a local/field `T x = new()` (initializer hangs directly
        # off the variable_declarator), a property initializer
        # `public T P { get; } = new()`, or a return position (`return new();`
        # / `=> new()`) typed by the enclosing member. Any other position (an
        # argument, an operand) needs overload resolution tree-sitter cannot
        # do: bail rather than guess.
        node = creation_node.parent
        while node is not None and node.type in (
            cs.TS_CSHARP_VARIABLE_DECLARATOR,
            cs.TS_CSHARP_EQUALS_VALUE_CLAUSE,
            cs.TS_PARENTHESIZED_EXPRESSION,
        ):
            node = node.parent
        if node is None:
            return None
        if node.type in (
            cs.TS_CSHARP_VARIABLE_DECLARATION,
            cs.TS_CSHARP_PROPERTY_DECLARATION,
        ):
            type_node = node.child_by_field_name(cs.FIELD_TYPE)
        elif node.type in (
            cs.TS_RETURN_STATEMENT,
            cs.TS_CSHARP_ARROW_EXPRESSION_CLAUSE,
        ):
            type_node = self._csharp_enclosing_return_type(node)
        else:
            return None
        if (
            type_node is None
            or type_node.text is None
            # `var x = new();` is ill-formed C# (no target type); if it
            # appears anyway, "var" is not a class name.
            or type_node.type == cs.TS_CSHARP_IMPLICIT_TYPE
        ):
            return None
        return type_node.text.decode(cs.ENCODING_UTF8).split(cs.CHAR_ANGLE_OPEN, 1)[0]

    def _csharp_enclosing_return_type(self, node: Node) -> Node | None:
        # A return position is typed by the nearest enclosing callable:
        # methods and local functions name it in `returns`, a property or
        # indexer (or their accessor bodies) in `type`. A lambda/anonymous
        # method carries no syntactic return type, so a `new()` returned from
        # one is unresolvable.
        ancestor = node.parent
        while ancestor is not None:
            if ancestor.type in (
                cs.TS_CSHARP_METHOD_DECLARATION,
                cs.TS_CSHARP_LOCAL_FUNCTION_STATEMENT,
            ):
                return ancestor.child_by_field_name(cs.TS_CSHARP_FIELD_RETURNS)
            if ancestor.type in (
                cs.TS_CSHARP_PROPERTY_DECLARATION,
                cs.TS_CSHARP_INDEXER_DECLARATION,
            ):
                return ancestor.child_by_field_name(cs.FIELD_TYPE)
            if ancestor.type in cs.TS_CSHARP_NESTED_SCOPE_TYPES:
                return None
            ancestor = ancestor.parent
        return None

    def _get_iife_target_name(self, parenthesized_expr: Node) -> str | None:
        for child in parenthesized_expr.children:
            match child.type:
                case cs.TS_FUNCTION_EXPRESSION:
                    return f"{cs.IIFE_FUNC_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
                case cs.TS_ARROW_FUNCTION:
                    return f"{cs.IIFE_ARROW_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
        return None

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
        if language in _TYPED_LANGUAGES:
            local_var_types = (
                self._resolver.type_inference.build_local_variable_type_map(
                    caller_node, module_qn, language, class_context
                )
            )
        else:
            local_var_types = None

        # Rust match arms often reuse one binding name (`cmd`) for different variant
        # types; the flat map keeps only the last arm's type. These per-arm scoped
        # bindings let each call inside an arm resolve against ITS arm's type.
        match_arm_bindings: list[tuple[int, int, str, str]] = []
        if language == cs.SupportedLanguage.RUST and local_var_types is not None:
            match_arm_bindings = self._resolver.type_inference.rust_type_inference.collect_match_arm_bindings(
                caller_node
            )

        caller_spec = (caller_type, cs.KEY_QUALIFIED_NAME, caller_qn)

        self._io_processor.process_io_for_caller(
            caller_node, caller_spec, module_qn, language
        )
        self._flow_processor.process_flow_for_caller(
            caller_node,
            caller_spec,
            caller_qn,
            module_qn,
            language,
            class_context,
            local_var_types,
        )

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
            self._collect_returned_callables(
                caller_node,
                caller_qn,
                module_qn,
                local_var_types,
                class_context,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
            )

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
            self._ingest_csharp_property_reads(
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
            self._ingest_dart_getter_reads(
                caller_node,
                caller_spec,
                caller_qn,
                module_qn,
                local_var_types,
                class_context,
                queries[language][cs.QUERY_CONFIG],
                dart_prop_names,
            )

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
            # A DEFAULT PARAMETER value naming a function (`useStore(api,
            # selector = identity as any)`, zustand) references it: the default
            # is invoked through the parameter when the caller omits the
            # argument, never by a visible call.
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
            )
        if language == cs.SupportedLanguage.GO and caller_type != cs.NodeLabel.MODULE:
            self._ingest_go_composite_function_references(
                caller_node,
                caller_spec,
                module_qn,
                local_var_types,
                class_context,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
            )
        if language == cs.SupportedLanguage.CPP:
            self._ingest_cpp_braced_return_instantiations(
                caller_node, caller_spec, caller_qn, module_qn
            )
            self._ingest_cpp_member_init_ctor_calls(caller_node, caller_spec, module_qn)
            self._ingest_cpp_implicit_base_lifecycle_calls(
                caller_node, caller_spec, caller_qn, module_qn
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
        method_invocation_type = cs.TS_METHOD_INVOCATION
        resolver = self._resolver
        resolve_func = resolver.resolve_function_call
        resolve_builtin = resolver.resolve_builtin_call if is_js_ts else None
        resolve_cpp_op = resolver.resolve_cpp_operator_call if is_cpp else None
        get_target = self._get_call_target_name
        class_label = cs.NodeLabel.CLASS
        ensure_rel = self.ingestor.ensure_relationship_batch
        calls_rel = cs.RelationshipType.CALLS
        qn_key = cs.KEY_QUALIFIED_NAME
        _id = id
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
        alias_map: dict[str, str] | None = None
        factory_aliases: dict[str, str] | None = None
        cpp_local_aliases: dict[str, list[tuple[str, int, int]]] | None = None

        for call_node in call_nodes:
            node_id = _id(call_node)
            if call_name_cache is not None and node_id in call_name_cache:
                call_name = call_name_cache[node_id]
            else:
                call_name = get_target(call_node, language)
                if call_name_cache is not None:
                    call_name_cache[node_id] = call_name
            # An inline function ARGUMENT is handed to the callee regardless of
            # whether the callee resolves: an external/param callee
            # (`create((set) => ...)` passing `set((state) => ...)`, zustand) or a
            # cast-wrapped one (`;(set as NamedSet)((state) => reducer(...))`)
            # still consumes it. Reference each inline arg from this scope, BEFORE
            # the no-name bail (a cast-wrapped callee yields no call name).
            # Registry-guarded and idempotent with the callable-params path.
            if is_js_ts and call_node.type == cs.TS_CALL_EXPRESSION:
                self._ingest_inline_call_arg_references(
                    call_node, caller_spec, ensure_rel, caller_qn, module_qn
                )
            if not call_name:
                # A callee that is itself a call (`wraps(view_func)(_view_wrapper)`)
                # or otherwise yields no name still consumes its arguments through
                # whatever callable it produced; reference first-party functions
                # passed to it or dead-code flags every django-style view
                # decorator wrapper. REFERENCES (not arg_ref_rel) is
                # deliberate and predates the C# split: with no callee name
                # there is no invocation to assert, for ANY language.
                if is_arg_ref_lang:
                    self._ingest_argument_function_references(
                        call_node,
                        caller_spec,
                        module_qn,
                        local_var_types,
                        class_context,
                        resolve_func,
                        ensure_rel,
                        caller_qn,
                        cs.RelationshipType.REFERENCES,
                        language,
                    )
                continue

            call_var_types = local_var_types
            if match_arm_bindings:
                call_var_types = self._overlay_match_arm_binding(
                    call_name, call_node, local_var_types, match_arm_bindings
                )

            if is_cpp:
                # A C++ member call through a template-parameter receiver has no
                # concrete type, so precise resolution fails and the external-receiver
                # guard drops the edge, orphaning every structural interface
                # implementer (json_sax_* visitors dispatched via `sax->start_object()`).
                # Fan such a call out to the method on every class defining it. Runs
                # before the primary resolution/continue below so it fires even when
                # that edge is dropped; a concretely-typed receiver is skipped inside
                # the resolver. sorted(): the target label is a hash-randomized
                # StrEnum, so sort for deterministic output.
                for target_type, target_qn in sorted(
                    resolver.cpp_dispatch_targets(
                        call_name, call_var_types, cpp_template_params
                    )
                ):
                    for variant in resolver.function_registry.variants(target_qn):
                        ensure_rel(
                            caller_spec, calls_rel, (target_type, qn_key, variant)
                        )

            cpp_operand_type_qn: str | None = None
            if (
                is_cpp
                and call_node.type in _CPP_OPERATOR_EXPRESSION_TYPES
                and call_name.startswith(cs.OPERATOR_PREFIX)
            ):
                cpp_operand_type_qn = resolver.cpp_operand_class_qn(
                    self._cpp_operator_operand_name(call_node),
                    call_var_types,
                    module_qn,
                )
            if cpp_operand_type_qn is not None:
                # The operand's type is KNOWN: the operator binds to that
                # type's own overload or, when it has none, to nothing at
                # all (a builtin enum/int operation), never rebound by bare
                # name to an unrelated class's overload set. Only an untyped
                # operand falls through to the legacy paths below.
                callee_info = resolver.cpp_operator_for_type(
                    call_name, cpp_operand_type_qn
                )
                if callee_info is None:
                    continue
            elif is_java and call_node.type == method_invocation_type:
                callee_info = resolver.resolve_java_method_call(
                    call_node, module_qn, local_var_types, caller_qn
                )
            elif is_csharp and call_node.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
                callee_info = resolver.resolve_csharp_method_call(
                    call_node, module_qn, call_var_types, caller_qn
                )
                if (
                    callee_info is not None
                    and callee_info != csharp_ti.CSHARP_EXTERNAL_TARGET
                    and (fn_node := call_node.child_by_field_name(cs.TS_FIELD_FUNCTION))
                    is not None
                    and fn_node.type
                    in (cs.TS_CSHARP_IDENTIFIER, cs.TS_CSHARP_GENERIC_NAME)
                ):
                    # A bare call resolved by ARITY may have same-arity
                    # siblings differing only in parameter types (Serilog's
                    # FormatExactNumericValue switch dispatch); keep the
                    # whole family reachable. NOT when a Roslyn fact pinned
                    # the site: that is the compiler's exact overload
                    # choice, and widening it would revive dead siblings.
                    engine = resolver.type_inference.csharp_type_inference
                    if not engine.semantic_fact_resolved(call_node, module_qn):
                        for sibling_qn in engine.csharp_same_arity_family(
                            callee_info[1]
                        ):
                            ensure_rel(
                                caller_spec,
                                calls_rel,
                                (cs.NodeLabel.METHOD, qn_key, sibling_qn),
                            )
                if callee_info == csharp_ti.CSHARP_EXTERNAL_TARGET:
                    # Provably external (base.X() with an external base, a
                    # static call on an unregistered type, an object
                    # virtual on an untyped receiver): no edge, and no
                    # name-trie fallback that would fabricate one onto an
                    # unrelated same-name first-party member.
                    callee_info = None
                elif callee_info is None:
                    # A C# member call whose receiver could not be typed (or a
                    # bare call) falls back to the generic simple-name resolver,
                    # which keeps Phase 1 intra-file resolution working.
                    callee_info = resolve_func(
                        call_name,
                        module_qn,
                        call_var_types,
                        class_context,
                        caller_qn,
                        language,
                    )
            else:
                callee_info = resolve_func(
                    call_name,
                    module_qn,
                    call_var_types,
                    class_context,
                    caller_qn,
                    language,
                )
            if callee_info and language == cs.SupportedLanguage.RUST:
                # Rust macros and functions live in SEPARATE namespaces:
                # a macro invocation (write!) must not bind a same-named fn
                # (std-prelude macro names collide with common fn names and
                # the false edge revives dead code), and a fn call must not
                # bind a same-named macro.
                is_macro_target = callee_info[1] in self.macro_qns
                if is_macro_target != (call_node.type == cs.TS_RS_MACRO_INVOCATION):
                    callee_info = None
            if not callee_info and resolve_builtin is not None:
                callee_info = resolve_builtin(call_name)
            if not callee_info and resolve_cpp_op is not None:
                callee_info = resolve_cpp_op(call_name, module_qn)
            if not callee_info and language == cs.SupportedLanguage.CPP:
                # `using appender = basic_appender<char>; appender(out)`:
                # the alias is no registered node, so the bare call resolves
                # to nothing and the constructed class's ctor stays edge-free.
                # The cross-file typedef/using map covers file/namespace-scope
                # aliases; a BODY-local alias is what that collector skips, so
                # it comes from the caller-scoped map (_resolve_class_name then
                # follows any further alias chain). Binding the callee to the
                # class drops into the class branch below, emitting INSTANTIATES
                # + ctor CALLS like any construction. Gated on alias-map
                # membership so genuinely unknown names keep their unresolved
                # handling.
                if cpp_local_aliases is None:
                    cpp_local_aliases = (
                        CppTypeInferenceEngine().collect_local_type_aliases(caller_node)
                    )
                lookup_name = call_name
                # Declaration-ordered AND lexically-scoped lookup: a call
                # BEFORE the body-local alias's declaration or AFTER its
                # enclosing block/lambda closes can never mean it; among
                # windows that do contain the call, the latest declaration
                # wins (C++ shadowing).
                best_decl_end = -1
                for underlying, decl_end, scope_end in cpp_local_aliases.get(
                    call_name, ()
                ):
                    if (
                        decl_end <= call_node.start_byte < scope_end
                        and decl_end > best_decl_end
                    ):
                        lookup_name = underlying
                        best_decl_end = decl_end
                if (
                    lookup_name != call_name or call_name in resolver.type_aliases
                ) and (
                    aliased_qn := resolver._resolve_class_name(lookup_name, module_qn)
                ):
                    callee_info = (cs.NodeLabel.CLASS, aliased_qn)
            if not callee_info and cs.SEPARATOR_DOT not in call_name:
                if is_python:
                    # A bare name that resolves to nothing may be a local alias of a
                    # callable (do = self._start; do()). Resolve the assignment's
                    # right-hand side and treat the alias call as a call to it.
                    if alias_map is None:
                        alias_map = self._build_local_alias_map(
                            caller_node, queries[language][cs.QUERY_CONFIG], module_qn
                        )
                    if (rhs := alias_map.get(call_name)) is not None:
                        callee_info = resolve_func(
                            rhs, module_qn, local_var_types, class_context, caller_qn
                        )
                if callee_info is None and is_flow_lang:
                    # `x = factory(...); x(cb)`: x holds a closure returned by a
                    # first-party factory (e.g. a retry/cache decorator applied
                    # imperatively). Record the call so cb flows into that closure's
                    # callable parameter once factory returns are known (finalize).
                    if factory_aliases is None:
                        factory_aliases = self._build_factory_alias_map(
                            caller_node,
                            module_qn,
                            local_var_types,
                            class_context,
                            self._flow_scope_boundaries(
                                queries[language][cs.QUERY_CONFIG]
                            ),
                            caller_qn,
                        )
                    if (factory_qn := factory_aliases.get(call_name)) is not None:
                        self._record_factory_call(
                            call_node,
                            caller_qn,
                            factory_qn,
                            module_qn,
                            local_var_types,
                            class_context,
                        )

            if not callee_info and is_python and cs.SEPARATOR_DOT in call_name:
                # recv.field(...) where field is a callable struct field:
                # resolve to the functions bound to it at construction sites.
                self._ingest_callable_field_calls(
                    call_name, caller_spec, local_var_types, ensure_rel
                )

            if is_python and call_name.rsplit(cs.SEPARATOR_DOT, 1)[-1] in (
                cs.HIGHER_ORDER_BUILTINS
            ):
                # sorted(xs, key=f) and friends invoke f synchronously in this
                # frame, so the trace attributes the call to the enclosing fn.
                self._ingest_higher_order_builtin_calls(
                    call_node,
                    caller_spec,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    ensure_rel,
                    caller_qn,
                )

            if not callee_info:
                if is_arg_ref_lang:
                    # The callee is not first-party (a framework/stdlib call such as
                    # grpclib Handler(self.__rpc_x), JS setTimeout(target), or a
                    # runtime dispatcher), so the call chain cannot be followed into
                    # it. A first-party function handed to it as an argument is still
                    # invoked, so record it as referenced from this scope to keep it
                    # reachable, across every flow-traced language.
                    self._ingest_argument_function_references(
                        call_node,
                        caller_spec,
                        module_qn,
                        local_var_types,
                        class_context,
                        resolve_func,
                        ensure_rel,
                        caller_qn,
                        arg_ref_rel,
                        language,
                    )
                continue

            callee_type, callee_qn = callee_info

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
                if is_arg_ref_lang:
                    self._ingest_argument_function_references(
                        call_node,
                        caller_spec,
                        module_qn,
                        local_var_types,
                        class_context,
                        resolve_func,
                        ensure_rel,
                        caller_qn,
                        arg_ref_rel,
                        language,
                    )
                continue

            if is_flow_lang:
                self._collect_callable_flow(
                    call_node,
                    callee_qn,
                    caller_qn,
                    caller_params,
                    module_qn,
                    local_var_types,
                    class_context,
                )
            if is_arg_ref_lang:
                # Functions are first-class values: a first-party callee may STORE
                # a passed callback for later dynamic dispatch (config objects,
                # codecs, registries), which callable-param flow cannot trace, or
                # the callee may be a same-named misbind of an external method.
                # Record the pass itself as a REFERENCES edge from the passing
                # scope so the callback is never reported dead.
                self._ingest_argument_function_references(
                    call_node,
                    caller_spec,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    ensure_rel,
                    caller_qn,
                    cs.RelationshipType.REFERENCES,
                    language,
                )

            if is_python and (
                dispatch_targets := resolver.protocol_dispatch_targets(callee_qn)
            ):
                # The call resolved to a Protocol stub; the stub never runs, so emit
                # edges to the method on every conformer instead of the stub.
                for conformer_type, conformer_qn in dispatch_targets:
                    for target_qn in resolver.function_registry.variants(conformer_qn):
                        ensure_rel(
                            caller_spec,
                            calls_rel,
                            (conformer_type, qn_key, target_qn),
                        )
                continue

            if (
                is_python
                and class_context
                and (
                    call_name.startswith(cs.PY_SELF_PREFIX)
                    or call_name.startswith(cs.PY_CLS_PREFIX)
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
                _, _, self_method = call_name.partition(cs.SEPARATOR_DOT)
                if cs.SEPARATOR_DOT not in self_method:
                    for target_type, target_qn in resolver.self_dispatch_targets(
                        class_context, self_method
                    ):
                        for variant in resolver.function_registry.variants(target_qn):
                            ensure_rel(
                                caller_spec,
                                calls_rel,
                                (target_type, qn_key, variant),
                            )

            if is_flow_lang:
                # f(...) invoked through a parameter: the edge runs from the
                # callee to whatever each call site binds to that parameter.
                self._ingest_callable_param_calls(
                    call_node,
                    callee_type,
                    callee_qn,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    ensure_rel,
                    caller_qn,
                )

            if (
                language in (cs.SupportedLanguage.JAVA, cs.SupportedLanguage.CSHARP)
                and call_node.type
                in (
                    cs.TS_OBJECT_CREATION_EXPRESSION,
                    cs.TS_CSHARP_IMPLICIT_OBJECT_CREATION_EXPRESSION,
                )
                and callee_type != class_label
            ):
                # `new X(...)` where X resolves to a non-class: a Java interface
                # or annotation implemented by an anonymous class (`new
                # Comparator<T>(){ ... }`), or a C# type the resolver could not bind
                # to a first-party class. There is no first-party constructor to
                # call, and a bare CALLS edge to a non-callable node (Interface) is
                # not valid, so drop the edge rather than emit it.
                continue

            if callee_type == class_label:
                # Record construction as INSTANTIATES -> the class node (keeps
                # CALLS function/method-only). When the class defines __init__,
                # ALSO redirect a CALLS edge to it (the constructor runs); when
                # it does not (dataclass/NamedTuple/pydantic), INSTANTIATES is
                # the only edge.
                for class_variant in resolver.function_registry.variants(callee_qn):
                    # A duplicate-suffixed variant may be a DIFFERENT kind
                    # of node (a merged TS namespace registers as a class,
                    # a colliding function as a Function); only class-typed
                    # variants are instantiable, and a mismatched label is
                    # a phantom the database drops (issue #652).
                    variant_type = resolver.function_registry.get(class_variant)
                    if variant_type is not None and variant_type != NodeType.CLASS:
                        continue
                    ensure_rel(
                        caller_spec,
                        cs.RelationshipType.INSTANTIATES,
                        (class_label, qn_key, class_variant),
                    )
                if language in (
                    cs.SupportedLanguage.JAVA,
                    cs.SupportedLanguage.CSHARP,
                    cs.SupportedLanguage.CPP,
                    cs.SupportedLanguage.DART,
                ):
                    # A Java/C#/C++/Dart constructor is a method named like its
                    # class (`Foo.Foo`), not `__init__`; `new Foo(...)` / `Foo(...)`
                    # runs one, so redirect a CALLS edge to every declared
                    # constructor (overload selection unneeded for reachability).
                    # C#, C++, and Dart default constructors use the same
                    # class-simple-name convention, so java_constructor_targets
                    # selects them too (a Dart NAMED constructor is invoked by its
                    # own name and resolves as an ordinary method). C++ additionally
                    # redirects to the destructor: the object's `~X` runs at end of
                    # lifetime with no call node of its own. sorted(): the target
                    # label is a hash-randomized StrEnum, so sort for determinism.
                    if language == cs.SupportedLanguage.CPP:
                        self._emit_cpp_ctor_calls(caller_spec, callee_qn)
                        continue
                    for ctor_type, ctor_qn in sorted(
                        resolver.java_constructor_targets(callee_qn)
                    ):
                        for variant in resolver.function_registry.variants(ctor_qn):
                            ensure_rel(
                                caller_spec, calls_rel, (ctor_type, qn_key, variant)
                            )
                    continue
                init_qn = f"{callee_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
                if init_qn not in resolver.function_registry:
                    continue
                callee_type = cs.NodeLabel.METHOD
                callee_qn = init_qn

            for target_qn in resolver.function_registry.variants(callee_qn):
                # A duplicate-suffixed variant may be a DIFFERENT kind of
                # node (a TS namespace merged onto a function registers as
                # a class); only callable variants take a CALLS edge, and
                # emitting the primary's label onto a differently-typed
                # node is a phantom the database drops (issue #652). An
                # unregistered target keeps its edge (resolver-derived
                # callees like an unwrapped fn.call base may not register).
                target_type = resolver.function_registry.get(target_qn)
                if target_type is not None and target_type not in (
                    NodeType.FUNCTION,
                    NodeType.METHOD,
                ):
                    continue
                ensure_rel(
                    caller_spec,
                    calls_rel,
                    (callee_type, qn_key, target_qn),
                )

            if (
                language == cs.SupportedLanguage.GO
                and callee_type == cs.NodeLabel.FUNCTION
            ):
                # A bare Go call resolves to one file's copy of a package-level
                # function; same-package same-name siblings are mutually-exclusive
                # build-tag variants (gin's `validate`), so emit an edge to each so
                # no build variant is reported dead. sorted(): the target label is a
                # hash-randomized StrEnum, so sort for deterministic output.
                for sibling_type, sibling_qn in sorted(
                    resolver.go_package_sibling_targets(callee_qn)
                ):
                    for variant in resolver.function_registry.variants(sibling_qn):
                        ensure_rel(
                            caller_spec, calls_rel, (sibling_type, qn_key, variant)
                        )

            if callee_type == cs.NodeLabel.METHOD:
                # A call bound to an interface/trait method (the static callee;
                # removing its declaration breaks the call) with exactly ONE
                # implementer also runs the concrete method, so edge both. The
                # old REPLACING redirect orphaned the interface stub (gson's
                # FieldNamingStrategy.translateName reported dead); sorted():
                # the target label is a hash-randomized StrEnum.
                for impl_type, impl_qn in sorted(
                    resolver.interface_sole_impl_targets(callee_qn)
                ):
                    for variant in resolver.function_registry.variants(impl_qn):
                        ensure_rel(caller_spec, calls_rel, (impl_type, qn_key, variant))

            if (
                is_js_ts
                and cs.SEPARATOR_DOT in call_name
                and callee_type in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
            ):
                # A JS member call that bound one twin of a double-registered
                # prototype method (`this.lookup()` -> module-flat `view.lookup`,
                # leaving `View.lookup` dead) edges the same-module same-name
                # member twin too (duplicate-QN keep-both design; revive-only).
                for twin_type, twin_qn in sorted(
                    resolver.js_member_twin_targets(callee_qn)
                ):
                    for variant in resolver.function_registry.variants(twin_qn):
                        ensure_rel(caller_spec, calls_rel, (twin_type, qn_key, variant))

            if (
                is_python
                and callee_type == cs.NodeLabel.FUNCTION
                and cs.SEPARATOR_DOT not in call_name
            ):
                # A platform-conditional import with a local fallback def (click's
                # `if WIN: from ._winconsole import X ... else: def X(...)`) is
                # statically undecidable; the call resolves to the import, so the
                # mutually-exclusive local def looks dead. When the name was bound
                # by a CONDITIONAL import and the CURRENT module also defines it,
                # fan the call out to the local twin too, mirroring the Go
                # build-variant fan-out. An UNCONDITIONAL import shadowing a local
                # def is plain shadowing: the local stays dead, so no edge.
                local_qn = f"{module_qn}{cs.SEPARATOR_DOT}{call_name}"
                if (
                    local_qn != callee_qn
                    and call_name
                    in resolver.import_processor.conditional_imports.get(module_qn, ())
                    and resolver.function_registry.get(local_qn) == NodeType.FUNCTION
                ):
                    for variant in resolver.function_registry.variants(local_qn):
                        ensure_rel(
                            caller_spec,
                            calls_rel,
                            (cs.NodeLabel.FUNCTION, qn_key, variant),
                        )

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
            node = stack.pop()
            if node.type in boundary:
                continue
            match node.type:
                case cs.TS_PY_SUBSCRIPT:
                    parent = node.parent
                    left = (
                        parent.child_by_field_name(cs.TS_FIELD_LEFT)
                        if parent is not None and parent.type == cs.TS_PY_ASSIGNMENT
                        else None
                    )
                    is_write = left is not None and left.id == node.id
                    self._emit_operator_dunder(
                        node.child_by_field_name(cs.FIELD_VALUE),
                        cs.PY_DUNDER_SETITEM if is_write else cs.PY_DUNDER_GETITEM,
                        caller_spec,
                        module_qn,
                        local_var_types,
                    )
                case cs.TS_PY_COMPARISON_OPERATOR:
                    operators = node.child_by_field_name(cs.TS_FIELD_OPERATORS)
                    if (
                        operators is not None
                        and (op_text := safe_decode_text(operators))
                        and cs.PY_OP_IN in op_text.split()
                        and node.named_children
                    ):
                        self._emit_operator_dunder(
                            node.named_children[-1],
                            cs.PY_DUNDER_CONTAINS,
                            caller_spec,
                            module_qn,
                            local_var_types,
                        )
                case cs.TS_PY_CALL:
                    func = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
                    args = node.child_by_field_name(cs.FIELD_ARGUMENTS)
                    if (
                        func is not None
                        and safe_decode_text(func) == cs.PY_BUILTIN_LEN
                        and args is not None
                        and len(args.named_children) == 1
                    ):
                        self._emit_operator_dunder(
                            args.named_children[0],
                            cs.PY_DUNDER_LEN,
                            caller_spec,
                            module_qn,
                            local_var_types,
                        )
                case cs.TS_PY_BOOLEAN_OPERATOR:
                    self._emit_truthiness(
                        node.child_by_field_name(cs.TS_FIELD_LEFT),
                        caller_spec,
                        module_qn,
                        local_var_types,
                    )
                    self._emit_truthiness(
                        node.child_by_field_name(cs.TS_FIELD_RIGHT),
                        caller_spec,
                        module_qn,
                        local_var_types,
                    )
                case cs.TS_PY_NOT_OPERATOR:
                    self._emit_truthiness(
                        node.child_by_field_name(cs.TS_FIELD_ARGUMENT),
                        caller_spec,
                        module_qn,
                        local_var_types,
                    )
                case (
                    cs.TS_PY_IF_STATEMENT
                    | cs.TS_PY_WHILE_STATEMENT
                    | cs.TS_PY_ELIF_CLAUSE
                    | cs.TS_PY_CONDITIONAL_EXPRESSION
                ):
                    # A bare object as a condition is tested for truthiness; nested
                    # boolean/not operators are handled when the walk reaches them.
                    self._emit_truthiness(
                        node.child_by_field_name(cs.TS_FIELD_CONDITION),
                        caller_spec,
                        module_qn,
                        local_var_types,
                    )
            stack.extend(node.children)

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
        if any(ch in operand_text for ch in cs.PY_OPERAND_REJECT_CHARS):
            return False
        targets = self._resolver.operator_dunder_targets(
            operand_text, dunder, module_qn, local_var_types
        )
        if not targets:
            return False
        for callee_type, callee_qn in targets:
            for target_qn in self._resolver.function_registry.variants(callee_qn):
                self.ingestor.ensure_relationship_batch(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (callee_type, cs.KEY_QUALIFIED_NAME, target_qn),
                )
        return True

    def _ingest_assignment_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
    ) -> None:
        # `x = some_function` binds a first-class function value to a name; the
        # alias is then stored, passed onward, or returned for dynamic dispatch
        # (http_callback = llm_http_task_closure_with_context), so the assignment
        # itself references the function and must keep it reachable. Only a plain
        # name/attribute RHS counts (calls resolve as calls); the walk stops at
        # nested scope boundaries, which own their own pass.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self.ingestor.ensure_relationship_batch
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            # Continue THROUGH an unowned anonymous arrow (zustand's curried
            # middleware body `(config) => (set, get, api) => { api.setState =
            # ... }`): it gets no caller pass, so its assignments would else be
            # scanned by nobody and the stored functions report dead. Named
            # nested scopes still own their own pass.
            if node.type in boundary_types and not self._is_unowned_js_scope(node):
                continue
            if rhs_field := _ASSIGNMENT_RHS_FIELDS.get(node.type):
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
                    # str) {...}`, express's render) hides the stored function
                    # one level down; scan the binary operands too.
                    if value.type == cs.TS_BINARY_EXPRESSION:
                        for operand in value.named_children:
                            operand = self._unwrap_ts_value(operand)
                            if operand.type in _INLINE_FUNC_VALUE_TYPES:
                                self._emit_callback_edge(
                                    caller_spec,
                                    operand,
                                    module_qn,
                                    local_var_types,
                                    class_context,
                                    resolve_func,
                                    ensure_rel,
                                    caller_qn,
                                    cs.RelationshipType.REFERENCES,
                                )
                        continue
                    # A Python TERNARY / BOOLEAN-DEFAULT RHS (`get_response =
                    # self._async if is_async else self._sync`, django's
                    # BaseHandler; `f = handler or fallback`) binds one of its
                    # RESULT operands; each result operand naming a callable is a
                    # possible referent. A ternary's condition is only
                    # truthiness-tested, never bound, so it is excluded; both
                    # boolean operands are possible results.
                    if value.type in (
                        cs.TS_PY_CONDITIONAL_EXPRESSION,
                        cs.TS_PY_BOOLEAN_OPERATOR,
                    ):
                        operands = list(value.named_children)
                        if (
                            value.type == cs.TS_PY_CONDITIONAL_EXPRESSION
                            and len(operands) == 3
                        ):
                            operands = [operands[0], operands[2]]
                        for operand in operands:
                            operand = self._unwrap_ts_value(operand)
                            if (
                                operand.type in _ASSIGNMENT_RHS_REF_TYPES
                                or operand.type in _INLINE_FUNC_VALUE_TYPES
                            ):
                                self._emit_callback_edge(
                                    caller_spec,
                                    operand,
                                    module_qn,
                                    local_var_types,
                                    class_context,
                                    resolve_func,
                                    ensure_rel,
                                    caller_qn,
                                    cs.RelationshipType.REFERENCES,
                                )
                        continue
                    if (
                        value.type in _ASSIGNMENT_RHS_REF_TYPES
                        or value.type in _INLINE_FUNC_VALUE_TYPES
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
                        # A member-assigned inline function (`api.setState =
                        # (s, r) => {...}`) is ALSO registered by the def pass
                        # under the PROPERTY name (scope.setState), which the
                        # by-position anonymous candidate above never matches;
                        # reference that registration too or it reports dead.
                        if value.type in _INLINE_FUNC_VALUE_TYPES:
                            self._emit_assigned_name_ref(
                                node, caller_spec, ensure_rel, caller_qn
                            )
            stack.extend(node.children)

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
        registry = self._resolver.function_registry
        scope_qn = caller_qn or caller_spec[2]
        candidate = f"{scope_qn}{cs.SEPARATOR_DOT}{name}"
        for target_qn in registry.variants(candidate):
            if registry.get(target_qn) is None:
                continue
            ensure_rel(
                caller_spec,
                cs.RelationshipType.REFERENCES,
                (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, target_qn),
            )

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
        ensure_rel = self.ingestor.ensure_relationship_batch
        registry = self._resolver.function_registry
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            # Stop at a nested scope that gets its OWN caller pass (a named
            # function/arrow, a class), but continue THROUGH an anonymous arrow
            # (a `.map()`/`cell`/forwardRef callback): those are skipped as
            # callers, so their JSX, rendered on behalf of this scope, would
            # otherwise be scanned by nobody and report as dead.
            if node.type in boundary_types and not self._is_unowned_js_scope(node):
                continue
            if node.type in _JSX_NAMED_ELEMENT_TYPES:
                name_node = node.child_by_field_name(cs.FIELD_NAME)
                name_text = safe_decode_text(name_node) if name_node else None
                if name_text and name_text[0].isupper():
                    resolved = resolve_func(
                        name_text, module_qn, local_var_types, class_context, caller_qn
                    )
                    if resolved:
                        res_type, res_qn = resolved
                        for target_qn in registry.variants(res_qn):
                            ensure_rel(
                                caller_spec,
                                cs.RelationshipType.REFERENCES,
                                (res_type, cs.KEY_QUALIFIED_NAME, target_qn),
                            )
            elif node.type == cs.TS_JSX_EXPRESSION:
                # A `{...}` attribute value hands its inner expression to the
                # element as a prop. A bare identifier (onClick={handleLogout})
                # or inline arrow (onClick={() => x()}) is a function the
                # framework invokes on the event, so reference it; other
                # expressions resolve to nothing and are skipped by the helper.
                for value in node.named_children:
                    self._emit_callback_edge(
                        caller_spec,
                        value,
                        module_qn,
                        local_var_types,
                        class_context,
                        resolve_func,
                        ensure_rel,
                        caller_qn=caller_qn,
                        rel_type=cs.RelationshipType.REFERENCES,
                    )
            stack.extend(node.children)

    def _ingest_returned_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
    ) -> None:
        # A function handed back via `return` (a useEffect cleanup
        # `return () => unsubscribe()`, a factory `return handler`) is invoked by
        # whoever receives it, never by a visible call. Reference it from the
        # returning scope. Walk continues through anonymous arrows (the effect
        # callback is anonymous, so its `return` bubbles here) but stops at named
        # nested functions, which own their returns.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self.ingestor.ensure_relationship_batch
        # An expression-bodied arrow (`const persistImpl = (config) =>
        # (set, get, api) => {...}`, zustand's curried middleware shape) has NO
        # return_statement; its body IS the implicit return. Reference the inner
        # function directly, both on the caller itself and on any unowned anon
        # arrow the walk continues through (deeper currying bubbles here too).
        self._emit_expression_body_return(
            caller_node, caller_spec, ensure_rel, caller_qn
        )
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types:
                if not self._is_unowned_js_scope(node):
                    continue
                self._emit_expression_body_return(
                    node, caller_spec, ensure_rel, caller_qn
                )
            if node.type == cs.TS_RETURN_STATEMENT:
                for value in node.named_children:
                    # A returned TUPLE hides its function elements one level
                    # down (`return _load_field, (...)` in django Field's
                    # __reduce__: pickle later calls the first element);
                    # expand containers so each function handed back inside
                    # one is referenced like a bare return.
                    for expanded in self._expand_py_first_class_values(value):
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
        self, value: Node, language: cs.SupportedLanguage | None = None
    ) -> list[Node]:
        # Peel Python container literals and result-position conditional
        # operands so a function stored in a tuple/list/set, a dict VALUE,
        # a bare return-tuple (expression_list), or a ternary/boolean-default
        # branch is treated like a bare first-class value; nesting expands
        # recursively. A ternary's condition is only truthiness-tested, never
        # bound, so it stays excluded. Any other node comes back unchanged, so
        # non-Python shapes are unaffected.
        is_dart = language == cs.SupportedLanguage.DART
        out: list[Node] = []
        stack = [value]
        while stack:
            node = stack.pop()
            children = _first_class_value_children(node, is_dart)
            if children is None:
                out.append(node)
            else:
                stack.extend(reversed(children))
        return out

    def _emit_expression_body_return(
        self,
        func_node: Node,
        caller_spec: tuple[str, str, str],
        ensure_rel,
        caller_qn: str | None,
    ) -> None:
        body = func_node.child_by_field_name(cs.FIELD_BODY)
        if body is not None and body.type in _INLINE_FUNC_VALUE_TYPES:
            self._emit_inline_arg_function_ref(
                body,
                caller_spec,
                ensure_rel,
                caller_qn,
                cs.RelationshipType.REFERENCES,
            )

    def _ingest_collection_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
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
        ensure_rel = self.ingestor.ensure_relationship_batch
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types and not self._is_unowned_js_scope(node):
                continue
            if node.type in _DICT_LIKE_COLLECTION_TYPES:
                for pair in node.named_children:
                    # An object-literal SHORTHAND METHOD (`return { then(x)
                    # {...}, catch(x) {...} }`, persist's thenable) is a stored
                    # callable like a pair value, but it is a method_definition
                    # node, not a pair; reference it by name so it is not dead
                    # unless the repo never calls it AND never hands the object
                    # out (it cannot know the consumer).
                    if pair.type == cs.TS_METHOD_DEFINITION:
                        self._emit_shorthand_method_ref(
                            pair, caller_spec, module_qn, ensure_rel
                        )
                        continue
                    if (
                        pair.type == cs.TS_PY_PAIR
                        and (value := pair.child_by_field_name(cs.FIELD_VALUE))
                        is not None
                    ):
                        if value.type in _INLINE_FUNC_VALUE_TYPES:
                            self._emit_inline_value_function_ref(
                                pair, value, caller_spec, module_qn, ensure_rel
                            )
                            continue
                        # A table VALUE wrapped in parens or a ternary
                        # (django SQLCompiler's `"local_setter": (partial(...)
                        # if ... else local_setter_noop)`) hides the handler
                        # candidates one level down; expand before emitting.
                        for expanded in self._expand_py_first_class_values(value):
                            self._emit_value_function_ref(
                                expanded,
                                caller_spec,
                                module_qn,
                                local_var_types,
                                class_context,
                                resolve_func,
                                ensure_rel,
                            )
            elif node.type in _SEQUENCE_LIKE_COLLECTION_TYPES:
                for element in node.named_children:
                    for expanded in self._expand_py_first_class_values(element):
                        self._emit_value_function_ref(
                            expanded,
                            caller_spec,
                            module_qn,
                            local_var_types,
                            class_context,
                            resolve_func,
                            ensure_rel,
                        )
            stack.extend(node.children)

    def _ingest_cpp_braced_return_instantiations(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
    ) -> None:
        # `return {args};` (nlohmann's exception factories) constructs the
        # caller's DECLARED return type through a bare initializer_list; no
        # call node exists, so the constructed class's ctor gets no edge and
        # reports dead even when its only factory is alive. Emit INSTANTIATES
        # to the class and CALLS to its ctors, like an explicit construction.
        # A lambda body is skipped: its returns are not the caller's.
        # Revive-only: nothing is emitted unless the return type resolves to a
        # registered first-party class.
        has_braced_return = False
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type == cs.TS_CPP_LAMBDA_EXPRESSION:
                continue
            if node.type == cs.TS_RETURN_STATEMENT and any(
                child.type == cs.TS_CPP_INITIALIZER_LIST
                for child in node.named_children
            ):
                has_braced_return = True
                break
            stack.extend(node.children)
        if not has_braced_return:
            return
        class_qn = self._resolver.cpp_braced_return_class(caller_qn, module_qn)
        if class_qn is None:
            return
        registry = self._resolver.function_registry
        ensure_rel = self.ingestor.ensure_relationship_batch
        for class_variant in registry.variants(class_qn):
            variant_type = registry.get(class_variant)
            if variant_type is not None and variant_type != NodeType.CLASS:
                continue
            ensure_rel(
                caller_spec,
                cs.RelationshipType.INSTANTIATES,
                (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_variant),
            )
        self._emit_cpp_ctor_calls(caller_spec, class_qn)

    def _ingest_cpp_member_init_ctor_calls(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
    ) -> None:
        # A ctor's member initializer list runs base-class ctors
        # (`: buffer(g, 0)`) and delegated ctors (`: widget(0)`) with no
        # call_expression node, so a base ctor only ever reached through
        # derived member-init had zero incoming CALLS and reported dead
        # (fmt buffer.buffer). Each initializer whose head name resolves to a
        # registered class emits CALLS to that class's ctors; a member FIELD
        # initializer resolves to no class and emits nothing (a field named
        # exactly like a registered class still emits, the common
        # field-shadows-its-own-type case where the ctor does run). The list
        # is a DIRECT child of function_definition, so a nested lambda's or
        # local class's initializers never leak in.
        for init_list in caller_node.children:
            if init_list.type != cs.CppNodeType.FIELD_INITIALIZER_LIST:
                continue
            for initializer in init_list.named_children:
                if initializer.type != cs.CppNodeType.FIELD_INITIALIZER:
                    continue
                name = self._cpp_member_init_head_name(initializer)
                class_qn = (
                    self._resolver._resolve_type_to_class_qn(name, module_qn)
                    if name
                    else None
                )
                if class_qn is not None:
                    self._emit_cpp_ctor_calls(caller_spec, class_qn)

    def _ingest_cpp_implicit_base_lifecycle_calls(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
    ) -> None:
        # A ctor whose member initializer list does not name a base still
        # runs that base's default ctor, and a dtor runs base dtors after
        # its own body; neither has an AST node (issue #892), so base-chain
        # lifecycles reached only implicitly reported dead (wonderous
        # Win32Window). The caller's identity comes from its qn (leaf ==
        # class simple name for a ctor, `~name` for a dtor, parent a
        # registered class), which covers out-of-class definitions whose
        # per-caller pass runs at module level. Registry guarded: only
        # bases resolved to registered classes emit, and a delegating ctor
        # (`: Derived(0)`) emits nothing because the delegated-to ctor owns
        # the base call.
        # Only a DEFINITION knows its member initializer list; the in-class
        # prototype registers under the same qn and also runs a per-caller
        # pass, and emitting from it would double every edge (and invent
        # base calls a bodied definition's initializer list excludes). A
        # `= default` member parses as function_definition, so the guard
        # keeps it: its implicit base call is guaranteed by the standard.
        if caller_node.type not in (
            cs.CppNodeType.FUNCTION_DEFINITION,
            cs.CppNodeType.INLINE_METHOD_DEFINITION,
        ):
            return
        registry = self._resolver.function_registry
        class_qn, sep, leaf = caller_qn.rpartition(cs.SEPARATOR_DOT)
        if not sep or registry.get(class_qn) != NodeType.CLASS:
            return
        simple = class_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        is_ctor = leaf == simple
        is_dtor = leaf == f"{cs.CPP_DESTRUCTOR_PREFIX}{simple}"
        if not is_ctor and not is_dtor:
            return
        bases = [
            base_qn
            for base_qn in self._resolver.class_inheritance.get(class_qn, [])
            if registry.get(base_qn) == NodeType.CLASS
        ]
        if not bases:
            return
        if is_dtor:
            for base_qn in bases:
                self._emit_cpp_lifecycle_targets(
                    caller_spec, self._resolver.cpp_destructor_targets(base_qn)
                )
            return
        named = self._cpp_member_init_class_qns(caller_node, module_qn)
        if class_qn in named:
            return
        for base_qn in bases:
            if base_qn in named:
                continue
            self._emit_cpp_lifecycle_targets(
                caller_spec, self._resolver.java_constructor_targets(base_qn)
            )

    def _emit_cpp_lifecycle_targets(
        self,
        caller_spec: tuple[str, str, str],
        targets: set[tuple[str, str]],
    ) -> None:
        registry = self._resolver.function_registry
        for target_type, target_qn in sorted(targets):
            for variant in registry.variants(target_qn):
                self.ingestor.ensure_relationship_batch(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (target_type, cs.KEY_QUALIFIED_NAME, variant),
                )

    def _cpp_member_init_class_qns(self, caller_node: Node, module_qn: str) -> set[str]:
        # Class qns the ctor's member initializer list names explicitly;
        # those base ctors are already emitted by the member-init pass.
        named: set[str] = set()
        for init_list in caller_node.children:
            if init_list.type != cs.CppNodeType.FIELD_INITIALIZER_LIST:
                continue
            for initializer in init_list.named_children:
                if initializer.type != cs.CppNodeType.FIELD_INITIALIZER:
                    continue
                name = self._cpp_member_init_head_name(initializer)
                if name and (
                    resolved := self._resolver._resolve_type_to_class_qn(
                        name, module_qn
                    )
                ):
                    named.add(resolved)
        return named

    def _emit_cpp_ctor_calls(
        self, caller_spec: tuple[str, str, str], class_qn: str
    ) -> None:
        # Construction runs a ctor now and the dtor at end of lifetime;
        # neither has a call node, so both get the redirect. sorted(): the
        # target label is a hash-randomized StrEnum, so sort for determinism.
        registry = self._resolver.function_registry
        targets = self._resolver.java_constructor_targets(
            class_qn
        ) | self._resolver.cpp_destructor_targets(class_qn)
        for target_type, target_qn in sorted(targets):
            for variant in registry.variants(target_qn):
                self.ingestor.ensure_relationship_batch(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (target_type, cs.KEY_QUALIFIED_NAME, variant),
                )

    @staticmethod
    def _cpp_member_init_head_name(initializer: Node) -> str | None:
        # The head is the initializer's first named child: a plain
        # field_identifier (`buffer`), a template_method (`base<T>`), or a
        # qualified_identifier (`ns::other`, `ns::base<int>` -- the
        # qualified node CONTAINS the template one, so branching on the
        # outer type cannot strip the specialization args). The raw text
        # carries the written spelling in every shape; registered class qns
        # are unspecialized and dot-separated, so cut at the first `<` and
        # normalize `::` (PR #792 review: `ns::base<int>(g)` resolved
        # nothing because the args leaked into the lookup).
        head = next(iter(initializer.named_children), None)
        if (
            head is None
            or head.type
            not in (
                cs.CppNodeType.FIELD_IDENTIFIER,
                cs.CppNodeType.TEMPLATE_METHOD,
                cs.CppNodeType.QUALIFIED_IDENTIFIER,
            )
            or head.text is None
        ):
            return None
        name = head.text.decode(cs.ENCODING_UTF8).split(cs.CHAR_ANGLE_OPEN, 1)[0]
        return name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT) or None

    def _ingest_go_composite_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
    ) -> None:
        # A Go function placed as a value in a composite literal, a func map
        # (`map[string]any{"rpad": rpad}`) or a func slice (`[]Handler{a, b}`), is
        # a dispatch table invoked later by key, never by a visible call, so its
        # entries look dead. Reference each from the enclosing scope. Go's literal
        # shape differs from the JS/Py pair form: composite_literal > literal_value >
        # {keyed_element(value=literal_element) | literal_element}, and the element
        # wraps the bare identifier one level down, so unwrap before resolving.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self.ingestor.ensure_relationship_batch
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types:
                continue
            if node.type == cs.TS_GO_LITERAL_VALUE:
                for element in node.named_children:
                    value = (
                        element.child_by_field_name(cs.FIELD_VALUE)
                        if element.type == cs.TS_GO_KEYED_ELEMENT
                        else element
                    )
                    if value is None or value.type != cs.TS_GO_LITERAL_ELEMENT:
                        continue
                    inner = value.named_children[0] if value.named_children else None
                    if inner is None:
                        continue
                    self._emit_value_function_ref(
                        inner,
                        caller_spec,
                        module_qn,
                        local_var_types,
                        class_context,
                        resolve_func,
                        ensure_rel,
                    )
            stack.extend(node.children)

    def _emit_value_function_ref(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func,
        ensure_rel,
    ) -> None:
        # A value cast for typing (`persistImpl as unknown as Persist`) is
        # transparent for reference resolution, and `fn.bind(ctx)` /
        # `fn.call(...)` / `fn.apply(...)` in value position (onError:
        # handleError.bind(toast)) hands off `fn`; peel both to a fixpoint.
        node = self._peel_bound_callable(node)
        # Only a bare name / attribute / member-expression in value position names
        # a function; a call, comprehension or literal is not a reference to a
        # callable. Reuses the flow-arg ref types (identifier, Python attribute,
        # Go selector, JS/TS member expression).
        if node.type not in _FLOW_ARG_REF_TYPES:
            return
        self._emit_callback_edge(
            caller_spec,
            node,
            module_qn,
            local_var_types,
            class_context,
            resolve_func,
            ensure_rel,
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
        registry = self._resolver.function_registry
        scope_qn = caller_spec[2]
        candidates = {
            f"{scope_qn}{cs.SEPARATOR_DOT}{cs.PREFIX_ANONYMOUS}"
            f"{value.start_point[0]}_{value.start_point[1]}"
        }
        key_node = pair.child_by_field_name(cs.FIELD_KEY)
        if (
            key_node is not None
            and key_node.type in (cs.TS_PROPERTY_IDENTIFIER, cs.TS_IDENTIFIER)
            and (key := safe_decode_text(key_node))
        ):
            candidates.add(f"{scope_qn}{cs.SEPARATOR_DOT}{key}")
            # A value nested under ANOTHER pair-arrow (`{ onCreated: (s) => {
            # s.setEvents({ compute: ... }) } }`) registers under the pair-key
            # PATH (scope.onCreated.compute); prefix the ancestor pair keys so
            # the candidate matches the def pass's qn.
            if pair_path := self._ancestor_pair_key_path(pair):
                candidates.add(
                    f"{scope_qn}{cs.SEPARATOR_DOT}{pair_path}{cs.SEPARATOR_DOT}{key}"
                )
        # A NAMED function expression value (`get: function getrouter() {...}`,
        # express) registers by its OWN name; neither the key nor the position
        # form matches it, so try the name under the scope and module-flat
        # (where the def pass puts it).
        name_node = value.child_by_field_name(cs.FIELD_NAME)
        if name_node is not None and (own := safe_decode_text(name_node)):
            candidates.add(f"{scope_qn}{cs.SEPARATOR_DOT}{own}")
            candidates.add(f"{module_qn}{cs.SEPARATOR_DOT}{own}")
        for candidate in candidates:
            for target_qn in registry.variants(candidate):
                if registry.get(target_qn) is None:
                    continue
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.REFERENCES,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, target_qn),
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
        name_node = method_node.child_by_field_name(cs.FIELD_NAME)
        registry = self._resolver.function_registry
        scope_qn = caller_spec[2]
        candidates = {
            f"{scope_qn}{cs.SEPARATOR_DOT}{cs.PREFIX_ANONYMOUS}"
            f"{method_node.start_point[0]}_{method_node.start_point[1]}"
        }
        if name_node is not None and (name := safe_decode_text(name_node)):
            candidates.add(f"{scope_qn}{cs.SEPARATOR_DOT}{name}")
            candidates.add(f"{module_qn}{cs.SEPARATOR_DOT}{name}")
        for candidate in candidates:
            for target_qn in registry.variants(candidate):
                if registry.get(target_qn) is None:
                    continue
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.REFERENCES,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, target_qn),
                )

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
        ensure_rel = self.ingestor.ensure_relationship_batch
        for param in params.named_children:
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
                or value.type in _INLINE_FUNC_VALUE_TYPES
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
            if value.type in _INLINE_FUNC_VALUE_TYPES:
                self._emit_inline_arg_function_ref(
                    value,
                    caller_spec,
                    ensure_rel,
                    caller_qn,
                    cs.RelationshipType.REFERENCES,
                    module_qn=module_qn,
                )

    def _emit_inline_arg_function_ref(
        self,
        arg_node: Node,
        source_spec: tuple[str, str, str],
        ensure_rel,
        caller_qn: str | None = None,
        rel_type: cs.RelationshipType = cs.RelationshipType.REFERENCES,
        module_qn: str | None = None,
    ) -> None:
        # An inline arrow/function-expression call argument is registered by the
        # definition pass as {enclosing_scope}.anonymous_<row>_<col> from its own
        # start position. The anonymous node lives in the CALLER's scope, so build
        # the candidate from caller_qn (source_spec[2] is the callee for the
        # callable-param path). Registry guard skips unregistered names.
        registry = self._resolver.function_registry
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
        for scope in scopes:
            for suffix in suffixes:
                candidate = f"{scope}{suffix}"
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
        registry = self._resolver.function_registry
        resolve_func = self._resolver.resolve_function_call
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types:
                continue
            if node.type == cs.TS_PY_RETURN_STATEMENT:
                for returned in node.named_children:
                    child = self._unwrap_ts_cast(returned)
                    if child.type not in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
                        continue
                    if not (name := safe_decode_text(child)):
                        continue
                    nested_qn = f"{caller_qn}{cs.SEPARATOR_DOT}{name}"
                    # A duplicated name (if/else twin definitions) returns
                    # whichever branch ran, so record EVERY twin; recording one
                    # leaves the others unreachable and falsely dead.
                    if nested_qn in registry:
                        self._returned_callables.setdefault(caller_qn, set()).update(
                            registry.variants(nested_qn)
                        )
                    elif (
                        resolved := resolve_func(
                            name, module_qn, local_var_types, class_context, caller_qn
                        )
                    ) is not None and resolved[0] in _CALLABLE_NODE_LABELS:
                        self._returned_callables.setdefault(caller_qn, set()).update(
                            registry.variants(resolved[1])
                        )
            stack.extend(node.children)

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
                return self._go_factory_bindings(node)
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

    def _go_factory_bindings(self, node: Node) -> list[tuple[str, str]]:
        # Go `a, b := f(), g()`: pair each left identifier with the call in the
        # same position of the right expression list.
        left = node.child_by_field_name(cs.TS_FIELD_LEFT)
        right = node.child_by_field_name(cs.TS_FIELD_RIGHT)
        if left is None or right is None:
            return []
        names = [c for c in left.named_children if c.type == cs.TS_PY_IDENTIFIER]
        calls = [c for c in right.named_children if c.type == cs.TS_CALL_EXPRESSION]
        bindings: list[tuple[str, str]] = []
        for name_node, call in zip(names, calls):
            if (
                (var := safe_decode_text(name_node))
                and (fn := call.child_by_field_name(cs.TS_FIELD_FUNCTION)) is not None
                and (fn_name := safe_decode_text(fn))
            ):
                bindings.append((var, fn_name))
        return bindings

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
        resolved = self._resolver.resolve_function_call(
            text, module_qn, local_var_types, class_context, caller_qn
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
            self._factory_calls.append(
                _FactoryCall(scope_qn, factory_qn, pos_qns, kw_qns)
            )

    def _parse_call_arguments(
        self, call_node: Node
    ) -> tuple[list[Node], dict[str, Node]]:
        positional: list[Node] = []
        keyword: dict[str, Node] = {}
        args_node = _find_call_arguments_node(call_node)
        if args_node is None:
            return positional, keyword
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
                _add_dart_named_argument(child, keyword)
            elif child.type == cs.TS_PY_KEYWORD_ARGUMENT:
                _add_py_keyword_argument(child, keyword)
            else:
                positional.append(child)
        return positional, keyword

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
        arg_node = self._peel_bound_callable(arg_node)
        # An arrow/function-expression passed DIRECTLY as a call argument
        # (useCallback(() => {}), setTimeout(() => {}), arr.map(x => ...)) is
        # registered anonymously in the enclosing scope but named after no
        # identifier, so resolve_func cannot find it. The call consumes it, so
        # reference it by position the same way inline object-literal values are.
        if arg_node.type in _INLINE_FUNC_VALUE_TYPES:
            self._emit_inline_arg_function_ref(
                arg_node, source_spec, ensure_rel, caller_qn, rel_type
            )
            return
        if not (arg_text := safe_decode_text(arg_node)):
            return
        if language == cs.SupportedLanguage.CSHARP:
            # `Callback<int>` passes the method group with explicit type
            # arguments; methods register generic-free, so strip them.
            arg_text = arg_text.split(cs.CHAR_ANGLE_OPEN, 1)[0]
            # A bare method-group name binds the ENCLOSING type's method
            # group; reference the whole overload family (the delegate
            # type that selects one overload is invisible to syntax, and
            # the trie's lexicographic pick can even land on a sibling
            # class, Polly's EmptyAction). Falls through when the enclosing
            # type has no such member.
            if cs.SEPARATOR_DOT not in arg_text:
                engine = self._resolver.type_inference.csharp_type_inference
                # An in-scope LOCAL FUNCTION shadows any same-name member
                # (C# scoping) and the trie cannot see it at all: reference
                # the local group first (Serilog's CreateLogger passes its
                # Dispose/DisposeAsync locals to the Logger ctor).
                if locals_group := engine.csharp_local_function_group(
                    arg_text, caller_qn, module_qn
                ):
                    for target_qn in locals_group:
                        ensure_rel(
                            source_spec,
                            rel_type,
                            (
                                cs.NodeLabel.FUNCTION,
                                cs.KEY_QUALIFIED_NAME,
                                target_qn,
                            ),
                        )
                    return
                if family := engine.csharp_method_group_family(arg_text, caller_qn):
                    for target_qn in family:
                        ensure_rel(
                            source_spec,
                            rel_type,
                            (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, target_qn),
                        )
                    return
        if not (
            resolved := resolve_func(
                arg_text, module_qn, local_var_types, class_context, caller_qn
            )
        ):
            return
        res_type, res_qn = resolved
        registry = self._resolver.function_registry
        if res_type == cs.NodeLabel.CLASS:
            init_qn = f"{res_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
            if init_qn not in registry:
                return
            res_type = cs.NodeLabel.METHOD
            res_qn = init_qn
        # Only callables are meaningful callback/reference targets: a value can
        # resolve to an Interface/Type node (`selector = identity as Selector`
        # resolves the cast's TYPE name in some paths), and emitting that
        # produces schema-invalid edges.
        if res_type not in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
            return
        # A Dart getter in argument position is a VALUE READ, not a tear-off:
        # the getter-read pass owns those edges (with shadow handling), so
        # emitting here would fabricate liveness for a local that hides the
        # getter (issue #869).
        if language == cs.SupportedLanguage.DART and registry.is_property(res_qn):
            return
        for target_qn in registry.variants(res_qn):
            ensure_rel(
                source_spec,
                rel_type,
                (res_type, cs.KEY_QUALIFIED_NAME, target_qn),
            )

    def _ingest_callable_param_calls(
        self,
        call_node: Node,
        callee_type: str,
        callee_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func,
        ensure_rel,
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
                    ensure_rel,
                    caller_qn,
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
                self._flow_args.append(
                    _CallableFlowArg(
                        callee_qn, position, keyword_name, "", caller_qn, arg_text
                    )
                )
                continue
            resolved = self._resolver.resolve_function_call(
                arg_text, module_qn, local_var_types, class_context, caller_qn
            )
            if resolved is not None and resolved[0] in callable_labels:
                self._flow_args.append(
                    _CallableFlowArg(
                        callee_qn, position, keyword_name, resolved[1], "", ""
                    )
                )

    def finalize_flow(self) -> None:
        # Resolve deferred FLOWS_TO return-taint once every function body has
        # been walked, so a callee processed after its caller still contributes
        # its return edge and resource flow (issue #712).
        self._flow_processor.finalize()

    def finalize_callable_param_flow(self) -> None:
        # Resolve the recorded call-site argument bindings to a fixpoint and emit a
        # CALLS edge from every function that invokes a callable parameter to each
        # concrete function that can reach it (directly or via pass-through params).
        registry = self._resolver.function_registry
        seeds: dict[tuple[str, str], set[str]] = defaultdict(set)
        edges: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for arg in self._flow_args:
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

        ensure_rel = self.ingestor.ensure_relationship_batch
        # A nested closure a function returns is reachable whenever that function
        # is reached (created and handed back as the return value). Nested
        # functions are no longer roots, so this producer edge keeps a genuinely
        # used closure (a returned decorator/formatter) live without reviving the
        # closures of an unreachable outer function.
        for producer_qn, returned in self._returned_callables.items():
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

        for fc in self._factory_calls:
            for closure_qn in self._returned_callables.get(fc.factory_qn, ()):
                # The returned closure runs when the alias is called, so it is
                # reachable from the enclosing scope.
                closure_type = registry.get(closure_qn)
                if closure_type is None:
                    continue
                scope_type = registry.get(fc.scope_qn) or cs.NodeLabel.MODULE
                ensure_rel(
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

        for func_qn, invoked in (
            (qn, registry.callable_params(qn)) for qn in self._flow_param_names
        ):
            if not invoked or (func_type := registry.get(func_qn)) is None:
                continue
            source_spec = (func_type, cs.KEY_QUALIFIED_NAME, func_qn)
            for param_name in invoked:
                for target_qn in bindings.get((func_qn, param_name), ()):
                    target_type = registry.get(target_qn)
                    if target_type is None:
                        continue
                    for variant in registry.variants(target_qn):
                        ensure_rel(
                            source_spec,
                            cs.RelationshipType.CALLS,
                            (target_type, cs.KEY_QUALIFIED_NAME, variant),
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
        resolver = self._resolver
        resolve_func = resolver.resolve_function_call
        registry = resolver.function_registry
        ensure_rel = self.ingestor.ensure_relationship_batch
        calls_rel = cs.RelationshipType.CALLS
        qn_key = cs.KEY_QUALIFIED_NAME
        method_label = cs.NodeLabel.METHOD
        attr_type = cs.TS_PY_ATTRIBUTE
        call_type = cs.TS_PY_CALL
        func_field = cs.TS_FIELD_FUNCTION
        function_types = lang_config.function_node_types
        class_types = lang_config.class_node_types
        seen: set[str] = set()

        stack = list(caller_node.children)
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in function_types or node_type in class_types:
                continue
            if node_type == attr_type and (text := node.text) is not None:
                attr_text = text.decode(cs.ENCODING_UTF8)
                if attr_text.rsplit(cs.SEPARATOR_DOT, 1)[-1] in prop_names:
                    parent = node.parent
                    is_call_target = (
                        parent is not None
                        and parent.type == call_type
                        and parent.child_by_field_name(func_field) is node
                    )
                    if not is_call_target and (
                        callee_info := resolve_func(
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
                                ensure_rel(
                                    caller_spec,
                                    calls_rel,
                                    (method_label, qn_key, target_qn),
                                )
            stack.extend(node.children)

    def _ingest_dart_getter_reads(
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
        # Emit a REFERENCES edge (a read is not an invocation; the call graph
        # stays invocation-only) from the caller to each getter it reads:
        # bare identifiers resolve against the enclosing class (implicit
        # this), member selectors reassemble their receiver chain and resolve
        # through receiver typing. Registry-guarded: only resolutions landing
        # on a MARKED property emit, so unrelated same-name locals or
        # functions never fabricate an edge. Nested closures are walked (a
        # Dart lambda's reads attribute to the enclosing method, matching the
        # flat-attribute call pass), so their parameters join the shadow set.
        class_types = lang_config.class_node_types
        seen: set[str] = set()

        # The grammar splits a definition into a signature node and a SIBLING
        # function_body: the signature holds no reads, so walk the body.
        body = dart_utils.dart_body_node(caller_node)
        walk_root = body if body is not None else caller_node
        # The bodiless caller is the MODULE pass: FIELD INITIALIZERS read
        # getters outside any method body (`late final body =
        # _createFont(contentFont, ..)`), so each class subtree is walked by
        # a per-class sub-pass carrying the OWNING class as resolution
        # context. Method callers keep skipping nested classes instead.
        is_module_scope = body is None
        shadow_cell: list[dict[str, list[tuple[int, int]]]] = []

        def shadow_spans() -> dict[str, list[tuple[int, int]]]:
            # Built lazily on the first bare read; most callers have none.
            if not shadow_cell:
                shadow_cell.append(self._dart_shadow_spans(caller_node, walk_root))
            return shadow_cell[0]

        stack = list(walk_root.children)
        while stack:
            node = stack.pop()
            if node.type in class_types:
                # Class subtrees (field initializers) are scanned once at
                # FILE level by _ingest_dart_class_initializer_reads.
                continue
            if is_module_scope and _dart_is_owned_function_body(node):
                continue
            read_name = self._dart_read_name(node, prop_names, shadow_spans)
            if read_name:
                self._emit_dart_property_read(
                    read_name,
                    caller_spec,
                    caller_qn,
                    module_qn,
                    local_var_types,
                    class_context,
                    seen,
                )
            stack.extend(node.children)

    def _ingest_dart_class_initializer_reads(
        self,
        class_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        lang_config: LanguageSpec,
        prop_names: set[str],
        parent_qn: str | None = None,
    ) -> None:
        # Field initializers run in the class's OWN scope: bare reads resolve
        # against the owning class (two classes may share a getter name), and
        # each class gets its own dedup set. A closure inside an initializer
        # belongs to no method pass, so its body is walked here; only
        # method/constructor bodies (owned by a class-body signature) have
        # their own pass. Dart forbids nested class declarations, but the
        # defensive recursion still threads the enclosing context through.
        class_types = lang_config.class_node_types
        name_node = next(
            (
                child
                for child in class_node.named_children
                if child.type == cs.TS_DART_IDENTIFIER
            ),
            None,
        )
        class_name = safe_decode_text(name_node) if name_node is not None else None
        owner_qn = parent_qn if parent_qn is not None else module_qn
        class_ctx = f"{owner_qn}{cs.SEPARATOR_DOT}{class_name}" if class_name else None
        seen: set[str] = set()
        shadow_cell: list[dict[str, list[tuple[int, int]]]] = []

        def shadow_spans() -> dict[str, list[tuple[int, int]]]:
            # Initializer closures declare parameters; their spans confine
            # any shadowing to the closure itself. Member signatures and
            # owned bodies stay out: a method parameter scopes its own body,
            # which this walk never reads.
            if not shadow_cell:
                shadow_cell.append(
                    self._dart_shadow_spans(
                        class_node, class_node, skip_owned_members=True
                    )
                )
            return shadow_cell[0]

        stack = list(class_node.children)
        while stack:
            node = stack.pop()
            if node.type in class_types:
                self._ingest_dart_class_initializer_reads(
                    node,
                    caller_spec,
                    caller_qn,
                    module_qn,
                    local_var_types,
                    lang_config,
                    prop_names,
                    parent_qn=class_ctx,
                )
                continue
            if _dart_is_owned_function_body(node):
                continue
            read_name = self._dart_read_name(node, prop_names, shadow_spans)
            if read_name:
                self._emit_dart_property_read(
                    read_name,
                    caller_spec,
                    caller_qn,
                    module_qn,
                    local_var_types,
                    class_ctx,
                    seen,
                )
            stack.extend(node.children)

    def _dart_read_name(
        self,
        node: Node,
        prop_names: set[str],
        shadow_spans: Callable[[], dict[str, list[tuple[int, int]]]],
    ) -> str | None:
        # The dotted name this node READS, or None: member selectors and
        # cascades reassemble their receiver chain; a bare identifier counts
        # only when no in-scope local/parameter shadows it.
        node_type = node.type
        if node_type == cs.TS_DART_SELECTOR:
            read_name = dart_utils.dart_member_read_name(node)
        elif node_type == cs.TS_DART_CASCADE_SECTION:
            read_name = dart_utils.dart_cascade_read_name(node)
        elif node_type == cs.TS_DART_IDENTIFIER and _dart_is_bare_read(node):
            read_name = self._dart_unshadowed_name(node, prop_names, shadow_spans)
        else:
            return None
        if read_name and read_name.rsplit(cs.SEPARATOR_DOT, 1)[-1] in prop_names:
            return read_name
        return None

    def _dart_unshadowed_name(
        self,
        node: Node,
        prop_names: set[str],
        shadow_spans: Callable[[], dict[str, list[tuple[int, int]]]],
    ) -> str | None:
        name = safe_decode_text(node)
        if not name or name not in prop_names:
            return None
        pos = node.start_byte
        if any(lo <= pos < hi for lo, hi in shadow_spans().get(name, ())):
            return None
        return name

    def _emit_dart_property_read(
        self,
        read_name: str,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        seen: set[str],
    ) -> None:
        if read_name in seen:
            return
        registry = self._resolver.function_registry
        res_qn: str | None = None
        # A bare read inside a class binds the OWNING class's member first:
        # the trie fallback is name-global and would pick a same-named getter
        # from whichever class sorts first (a module-caller's caller_qn
        # carries no class to guide it).
        if class_context is not None and cs.SEPARATOR_DOT not in read_name:
            candidate = f"{class_context}{cs.SEPARATOR_DOT}{read_name}"
            if registry.get(candidate) is not None:
                res_qn = candidate
        if res_qn is None:
            resolved = self._resolver.resolve_function_call(
                read_name, module_qn, local_var_types, class_context, caller_qn
            )
            if not resolved:
                return
            res_qn = resolved[1]
        if not registry.is_property(res_qn) or res_qn == caller_qn:
            return
        seen.add(read_name)
        self.ingestor.ensure_relationship_batch(
            caller_spec,
            cs.RelationshipType.REFERENCES,
            (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, res_qn),
        )

    def _dart_shadow_spans(
        self,
        caller_node: Node,
        walk_root: Node,
        skip_owned_members: bool = False,
    ) -> dict[str, list[tuple[int, int]]]:
        # {declared name: [byte span of each declaring SCOPE]} for parameters
        # and locals. A declaration shadows a same-name getter only for bare
        # reads INSIDE its declaring scope: a closure's parameter must not
        # suppress the enclosing method's read (cf. _csharp_shadow_spans).
        # Method parameters live in the SIGNATURE, outside the walked body,
        # so their scope walk falls through to the whole body.
        spans: dict[str, list[tuple[int, int]]] = {}
        stack = list(caller_node.children)
        if walk_root is not caller_node:
            stack.extend(walk_root.children)
        while stack:
            node = stack.pop()
            # For the class-initializer pass, member signatures and owned
            # bodies are invisible to the READ walk, so their parameters and
            # locals must not join the shadow map either: a method parameter
            # scopes its own body, never a sibling field initializer.
            if skip_owned_members and (
                node.type in cs.DART_SIGNATURE_TYPES
                or _dart_is_owned_function_body(node)
            ):
                continue
            for name in _dart_declared_names(node):
                spans.setdefault(name, []).append(_dart_scope_span(node, walk_root))
            stack.extend(node.children)
        return spans

    def _csharp_read_identifier(self, receiver: Node | None) -> str | None:
        # The identifier actually being READ in receiver position: unwrap
        # parens, a cast's VALUE (`((IDictionary<...>)WrappedDictionary)`),
        # and a null-forgiving postfix (`s!`) down to a bare identifier.
        while receiver is not None:
            if receiver.type == cs.TS_PARENTHESIZED_EXPRESSION:
                receiver = (
                    receiver.named_children[0] if receiver.named_children else None
                )
                continue
            if receiver.type == cs.TS_CSHARP_CAST_EXPRESSION:
                receiver = receiver.child_by_field_name(cs.FIELD_VALUE)
                continue
            if receiver.type == cs.TS_CSHARP_POSTFIX_UNARY_EXPRESSION:
                receiver = (
                    receiver.named_children[0] if receiver.named_children else None
                )
                continue
            break
        if receiver is not None and receiver.type == cs.TS_CSHARP_IDENTIFIER:
            return safe_decode_text(receiver)
        return None

    def _csharp_shadow_spans(
        self,
        caller_node: Node,
        function_types: tuple[str, ...],
        class_types: tuple[str, ...],
    ) -> dict[str, list[tuple[int, int]]]:
        # {declared name: [byte span of each declaring SCOPE]} for every
        # declaration in the scopes the READ WALK descends into (locals
        # incl. untyped `var x = 3`, parameters, and a simple lambda's
        # bare implicit_parameter). A declaration shadows a same-name
        # property only for reads INSIDE its declaring scope: a lambda
        # param or a sibling block's local must not suppress an outer
        # read, while an in-lambda shadowed read must not fabricate a
        # property reference. The two walks skip the SAME nested
        # function/class scopes (each has its own pass).
        def scope_span(decl: Node) -> tuple[int, int]:
            anc = decl.parent
            while anc is not None and anc != caller_node:
                if anc.type in (cs.TS_CSHARP_BLOCK, cs.TS_CSHARP_LAMBDA_EXPRESSION):
                    return (anc.start_byte, anc.end_byte)
                anc = anc.parent
            return (caller_node.start_byte, caller_node.end_byte)

        spans: dict[str, list[tuple[int, int]]] = {}
        stack = list(caller_node.children)
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in function_types or node_type in class_types:
                continue
            name = None
            if node_type in (cs.TS_CSHARP_VARIABLE_DECLARATOR, cs.TS_CSHARP_PARAMETER):
                name = safe_decode_text(node.child_by_field_name(cs.FIELD_NAME))
            elif node_type == cs.TS_CSHARP_IMPLICIT_PARAMETER:
                name = safe_decode_text(node)
            if name:
                spans.setdefault(name, []).append(scope_span(node))
            stack.extend(node.children)
        return spans

    def _ingest_csharp_property_reads(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        lang_config: LanguageSpec,
        prop_names: set[str],
    ) -> None:
        # Emit a REFERENCES edge (a read is not an invocation; the call
        # graph stays invocation-only) from the caller to each property of
        # its enclosing type read in receiver position. Registry-guarded via
        # resolve_property_read, which accepts only marked properties.
        engine = self._resolver.type_inference.csharp_type_inference
        ensure_rel = self.ingestor.ensure_relationship_batch
        refs_rel = cs.RelationshipType.REFERENCES
        qn_key = cs.KEY_QUALIFIED_NAME
        method_label = cs.NodeLabel.METHOD
        function_types = lang_config.function_node_types
        class_types = lang_config.class_node_types
        shadowed: dict[str, list[tuple[int, int]]] | None = None
        seen: set[str] = set()

        def try_emit(name: str | None, read_node: Node, this_read: bool) -> None:
            nonlocal shadowed
            if not name or name not in prop_names or name in seen:
                return
            if shadowed is None:
                shadowed = self._csharp_shadow_spans(
                    caller_node, function_types, class_types
                )
            pos = read_node.start_byte
            is_shadowed = any(lo <= pos < hi for lo, hi in shadowed.get(name, ()))
            if (this_read or not is_shadowed) and (
                prop_qn := engine.resolve_property_read(name, caller_qn)
            ):
                seen.add(name)
                if prop_qn != caller_qn:
                    ensure_rel(caller_spec, refs_rel, (method_label, qn_key, prop_qn))

        stack = list(caller_node.children)
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in function_types or node_type in class_types:
                continue
            if node_type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
                receiver = node.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
                # `this.Size` is ALWAYS the member (a local can never
                # shadow a this-qualified read), so the read is the NAME
                # field and the shadow check does not apply.
                this_read = receiver is not None and receiver.type == cs.TS_CSHARP_THIS
                if this_read:
                    try_emit(
                        safe_decode_text(node.child_by_field_name(cs.FIELD_NAME)),
                        node,
                        True,
                    )
                else:
                    recv_name = self._csharp_read_identifier(receiver)
                    try_emit(recv_name, node, False)
                    # The NAME field can be a property of the RECEIVER's
                    # type: `Cfg.Value` (static, class-name receiver),
                    # `w.Inner` (instance, local of inferred type), or
                    # `N.Cfg.Value` (namespace/type-qualified: a dotted
                    # receiver of all-PascalCase segments names a type, a
                    # camelCase head is an expression chain and is skipped).
                    # An unresolvable receiver yields nothing, so unrelated
                    # chains never fabricate an edge.
                    member = safe_decode_text(node.child_by_field_name(cs.FIELD_NAME))
                    recv_type = None
                    if recv_name:
                        recv_type = (local_var_types or {}).get(recv_name, recv_name)
                    elif (
                        receiver is not None
                        and receiver.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION
                    ):
                        dotted = safe_decode_text(receiver)
                        if dotted and all(
                            seg[:1].isupper() for seg in dotted.split(cs.SEPARATOR_DOT)
                        ):
                            recv_type = dotted
                    if recv_type and member and member in prop_names:
                        prop_qn = engine.resolve_member_property_read(
                            recv_type, member, module_qn
                        )
                        if (
                            prop_qn is not None
                            and prop_qn != caller_qn
                            and prop_qn not in seen
                        ):
                            seen.add(prop_qn)
                            ensure_rel(
                                caller_spec, refs_rel, (method_label, qn_key, prop_qn)
                            )
            elif node_type == cs.TS_CSHARP_IDENTIFIER:
                # A bare identifier expression (`return Size;`, `Use(Size)`,
                # `var n = Size;`) reads the getter just the same. NOT a
                # read: any member-access position (receiver/name handled
                # above), a parent's NAME field (a declarator/parameter's
                # own name, a named-argument label `Use(Size: 3)`), or a
                # parent's TYPE field (`new Size()`, `(Size)x`).
                # `==`, not `is`: child_by_field_name returns a fresh Node
                # wrapper each call, so identity never matches.
                parent = node.parent
                if (
                    parent is not None
                    and parent.type != cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION
                    and parent.child_by_field_name(cs.FIELD_NAME) != node
                    and parent.child_by_field_name(cs.FIELD_TYPE) != node
                ):
                    try_emit(safe_decode_text(node), node, False)
            stack.extend(node.children)

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
                name_node = current.child_by_field_name(cs.FIELD_NAME)
                if name_node is not None:
                    if name_node.text is not None:
                        path_parts.append(name_node.text.decode(cs.ENCODING_UTF8))
                # A JS/TS arrow-const ancestor (`getQueryString = () => {...}`) has
                # no `name` field (the name lives on the parent declarator), so it
                # would be dropped, flattening a nested callee's qn (request.encodePair
                # instead of request.getQueryString.encodePair). Recover the binding
                # name the definition pass used so the qns agree; else the callee's
                # own inline-arg/object callbacks never match and report dead.
                elif lang_config.language in _JS_TS_LANGUAGES and (
                    binding := self._js_ts_arrow_binding_name(current)
                ):
                    path_parts.append(binding)
            elif current.type in lang_config.class_node_types:
                return None

            current = current.parent

        path_parts.reverse()
        if path_parts:
            return f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(path_parts)}{cs.SEPARATOR_DOT}{func_name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{func_name}"

    def _js_ts_arrow_binding_name(self, func_node: Node) -> str | None:
        # An arrow / function expression has no `name` field, so the call pass
        # skipped it and never processed its body's calls. Recover the binding
        # name for the two named forms whose value IS the arrow: a module/local
        # `const f = () => ...` (variable_declarator) and a class field
        # `helper = () => ...` (public_field_definition). The body's calls then
        # attribute to the qn the definition pass registered. Anonymous /
        # destructured arrows stay unnamed (skipped).
        if func_node.type not in (cs.TS_ARROW_FUNCTION, cs.TS_FUNCTION_EXPRESSION):
            return None
        # The arrow may be bound THROUGH transparent wrappers: parens and TS
        # casts (`export const createStore = ((s) => ...) as CreateStore`,
        # zustand's public-API shape). The def pass unwraps these when naming the
        # function, so the call pass must climb them too or the arrow looks
        # anonymous and its body's calls drop at module scope.
        node = func_node
        parent = node.parent
        while parent is not None and parent.type in _TS_BINDING_WRAPPER_TYPES:
            node = parent
            parent = node.parent
        if parent is None:
            return None
        # node must be the parent's value/initializer for both forms
        # (variable_declarator and public_field_definition), so one value check
        # covers both. `==` not `is`: py-tree-sitter returns a fresh Node wrapper
        # per access, so identity comparison always fails (Node `==` compares id).
        if parent.child_by_field_name(cs.FIELD_VALUE) != node:
            return None
        name_node = parent.child_by_field_name(cs.FIELD_NAME)
        if name_node is None or name_node.type not in (
            cs.TS_IDENTIFIER,
            cs.TS_PROPERTY_IDENTIFIER,
        ):
            return None
        return safe_decode_text(name_node)

    def _attributable_func_nodes(
        self, func_nodes: list[Node], language: cs.SupportedLanguage
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
        if language not in _JS_TS_LANGUAGES:
            return func_nodes
        return [
            n
            for n in func_nodes
            if self._get_node_name(n) or self._js_ts_arrow_binding_name(n)
        ]

    def _is_unowned_js_scope(self, node: Node) -> bool:
        # An anonymous arrow/function-expression that gets no caller node of its
        # own (no name, no binding name): a `.map()`/`cell`/forwardRef callback.
        # Its calls bubble up to the enclosing named scope (_attributable_func_nodes),
        # and so must its JSX references: the enclosing JSX walk continues through it.
        if node.type not in (cs.TS_ARROW_FUNCTION, cs.TS_FUNCTION_EXPRESSION):
            return False
        return not (self._get_node_name(node) or self._js_ts_arrow_binding_name(node))

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool:
        return is_method_node(func_node, lang_config)
