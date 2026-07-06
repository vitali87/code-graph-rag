from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LanguageSpec
from ..parser_loader import COMBINED_FUNC_CLASS_QUERIES
from ..services import IngestorProtocol
from ..types_defs import FunctionRegistryTrieProtocol, LanguageQueries
from ..utils.path_utils import cached_relative_path
from .call_resolver import CallResolver
from .class_ingest.identity import build_nested_qualified_name_for_class
from .cpp import utils as cpp_utils
from .go import utils as go_utils
from .import_processor import ImportProcessor
from .java import utils as java_utils
from .lua import utils as lua_utils
from .rs import utils as rs_utils
from .type_inference import TypeInferenceEngine
from .utils import (
    cpp_parameter_names,
    get_function_captures,
    go_parameter_names,
    is_method_node,
    js_ts_parameter_names,
    python_parameter_names,
    safe_decode_text,
    sorted_captures,
)


class _CallableFlowArg(NamedTuple):
    # (H) One call-site argument that may carry a callable: bound either to a concrete
    # (H) function (source_concrete) or to a parameter of the caller (source_caller +
    # (H) source_param), keyed to the callee parameter by position or keyword.
    callee_qn: str
    position: int
    keyword: str
    source_concrete: str
    source_caller: str
    source_param: str


class _FactoryCall(NamedTuple):
    # (H) A call `x(args)` where x was bound by `x = factory(...)`. Each returned
    # (H) closure of factory receives args, so a callback argument flows into that
    # (H) closure's callable parameter (positional index or keyword name). Resolved
    # (H) in finalize once every function's returned callables are known.
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
        cs.SupportedLanguage.LUA,
        cs.SupportedLanguage.GO,
        cs.SupportedLanguage.CPP,
        cs.SupportedLanguage.RUST,
    }
)

# (H) C and C++ share the function_definition/declarator shape, so the callee
# (H) name lives in a nested declarator (no `name` field). Both need the libclang
# (H) declarator-aware extractor rather than a plain child_by_field_name("name").
_C_FAMILY_LANGUAGES = frozenset({cs.SupportedLanguage.C, cs.SupportedLanguage.CPP})
_JS_TS_LANGUAGES = cs.JS_TS_LANGUAGES

# (H) Python nested-scope boundaries and sequence-literal node types used when
# (H) scanning a scope for dispatch tables of function references.
_PY_SCOPE_BOUNDARY_TYPES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_DECORATED_DEFINITION,
    }
)
_PY_SEQUENCE_LITERAL_TYPES = frozenset({cs.TS_PY_LIST, cs.TS_PY_SET, cs.TS_PY_TUPLE})
# (H) Dispatch-table literals whose values may name handler functions: Python dict
# (H) and JS/TS object (key/value pairs), and Python list/set/tuple and JS/TS array
# (H) (positional elements). All use the `pair`/named-child shapes handled below.
_DICT_LIKE_COLLECTION_TYPES = frozenset({cs.TS_PY_DICTIONARY, cs.TS_OBJECT})
_SEQUENCE_LIKE_COLLECTION_TYPES = _PY_SEQUENCE_LITERAL_TYPES | frozenset({cs.TS_ARRAY})
_CALLABLE_NODE_LABELS = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
)
# (H) Node types of a call argument that may name a callable: a bare identifier
# (H) (Python/Go/JS/TS), a Python attribute (self.method), a Go selector (x.Method),
# (H) or a JS/TS member expression (obj.method).
_FLOW_ARG_REF_TYPES = frozenset(
    {
        cs.TS_PY_IDENTIFIER,
        cs.TS_PY_ATTRIBUTE,
        cs.TS_SELECTOR_EXPRESSION,
        cs.TS_MEMBER_EXPRESSION,
    }
)
# (H) Qualified-name prefix marking a resolved callee as a builtin rather than a
# (H) first-party function whose body the call chain can be followed into.
_BUILTIN_QN_PREFIX = f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}"
# (H) Assignment node type -> RHS field, per language family: Python `assignment`
# (H) and JS/TS `assignment_expression` (client.post = fn) carry the RHS in
# (H) `right`; a JS/TS `variable_declarator` (const cb = handler) carries it in
# (H) `value`.
_ASSIGNMENT_RHS_FIELDS = {
    cs.TS_PY_ASSIGNMENT: cs.TS_FIELD_RIGHT,
    cs.TS_ASSIGNMENT_EXPRESSION: cs.TS_FIELD_RIGHT,
    cs.TS_VARIABLE_DECLARATOR: cs.FIELD_VALUE,
}
# (H) RHS node types that name a callable value: a bare identifier, a Python
# (H) attribute (mod.fn), or a JS/TS member expression (handlers.run).
_ASSIGNMENT_RHS_REF_TYPES = frozenset(
    {cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE, cs.TS_MEMBER_EXPRESSION}
)
# (H) JSX nodes that carry a component name (self-closing and opening; a
# (H) closing element repeats the name, so a paired element emits once).
_JSX_NAMED_ELEMENT_TYPES = frozenset(
    {cs.TS_JSX_SELF_CLOSING_ELEMENT, cs.TS_JSX_OPENING_ELEMENT}
)
# (H) Inline function values in an object literal (`{ onSuccess: () => {} }`): the
# (H) JS/TS definition pass registers these as their own nodes named by the key
# (H) (scope.onSuccess), so a passed object of callbacks (useMutation/useQuery) must
# (H) reference each or every TanStack-style callback reports as dead.
_INLINE_FUNC_VALUE_TYPES = frozenset({cs.TS_ARROW_FUNCTION, cs.TS_FUNCTION_EXPRESSION})


class CallProcessor:
    __slots__ = (
        "ingestor",
        "repo_path",
        "project_name",
        "_resolver",
        "_flow_param_names",
        "_flow_args",
        "_returned_callables",
        "_factory_calls",
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
    ) -> None:
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name

        self._resolver = CallResolver(
            function_registry=function_registry,
            import_processor=import_processor,
            type_inference=type_inference,
            class_inheritance=class_inheritance,
            type_aliases=type_aliases,
            interface_implementers=interface_implementers,
        )
        # (H) Inter-procedural callable-parameter flow: ordered params per function and
        # (H) the per-call-site argument bindings, resolved to a fixpoint in finalize.
        self._flow_param_names: dict[str, list[str]] = {}
        self._flow_args: list[_CallableFlowArg] = []
        # (H) Return-value / factory tracing: functions each function may return
        # (H) (nested closures), and call sites `x = factory(...); x(cb)` where cb
        # (H) flows into the returned closure's callable parameter. Resolved to a
        # (H) fixpoint in finalize, so factory and call site may be in any file order.
        self._returned_callables: dict[str, set[str]] = {}
        self._factory_calls: list[_FactoryCall] = []

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
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> tuple[list[Node], list[int]]:
        calls_query = queries[language].get(cs.QUERY_CALLS)
        if not calls_query:
            return [], []
        cursor = QueryCursor(calls_query)
        captures = sorted_captures(cursor, root_node)
        call_nodes = captures.get(cs.CAPTURE_CALL, [])
        call_starts = [n.start_byte for n in call_nodes]
        return call_nodes, call_starts

    def _filter_calls_in_node(
        self,
        all_call_nodes: list[Node],
        call_starts: list[int],
        container: Node,
    ) -> list[Node]:
        start = container.start_byte
        end = container.end_byte
        lo = bisect_left(call_starts, start)
        hi = bisect_right(call_starts, end)
        return [n for n in all_call_nodes[lo:hi] if n.end_byte <= end]

    def _filter_top_level_calls(
        self,
        all_call_nodes: list[Node],
        call_starts: list[int],
        func_nodes: list[Node],
    ) -> list[Node]:
        # (H) Calls inside a function's BODY belong to that function, not the
        # (H) module; only genuine top-level calls are module-attributed. The body
        # (H) (not the whole node) is the boundary so def-time calls in the
        # (H) signature -- default args like `def f(x=make_default())` and
        # (H) decorators -- run at module load and stay module-attributed. A node
        # (H) with no body is not a real function scope (e.g. a file-scope
        # (H) declaration `int x = top();` that the grammar captures as a
        # (H) function); its calls run at load time, so it excludes nothing.
        nested_starts: set[int] = set()
        for func_node in func_nodes:
            body = func_node.child_by_field_name(cs.FIELD_BODY)
            if body is None:
                continue
            for call in self._filter_calls_in_node(all_call_nodes, call_starts, body):
                nested_starts.add(call.start_byte)
        return [c for c in all_call_nodes if c.start_byte not in nested_starts]

    def _bare_decorator_name(self, decorator_node: Node) -> str | None:
        # (H) A bare decorator `@task` / `@pkg.deco` (no call parens) is not a
        # (H) `call` node, so the normal call pass misses it even though applying
        # (H) it runs `task(func)` at module load. A call decorator `@deco(...)`
        # (H) IS a call node and is already captured, so skip it here.
        named = decorator_node.named_children
        if not named:
            return None
        expr = named[0]
        if expr.type in (cs.TS_IDENTIFIER, cs.TS_ATTRIBUTE) and expr.text is not None:
            return expr.text.decode(cs.ENCODING_UTF8)
        return None

    def _runs_at_module_load(self, node: Node) -> bool:
        # (H) A definition runs at module load only when it is at module or
        # (H) class-body scope; nested inside a function body it runs at that
        # (H) function's call time, so its decorator is not a module-load call.
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
        # (H) Emit `(Module)->decorator` CALLS for bare decorators on functions,
        # (H) methods, AND classes: the decoration executes at module-load time,
        # (H) so the module is the caller. Only first-party callables get an edge.
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
                    # (H) `@alias` where `alias = task` still calls task at load;
                    # (H) reuse the local-alias fallback the call pass uses.
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

    def _module_qn(self, relative_path: Path, file_name: str) -> str:
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
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        func_class_captures_cache: dict[Path, dict] | None = None,
    ) -> None:
        # (H) Pre-pass: record which functions are bound to a class's callable
        # (H) fields (FQNSpec(get_name=_python_get_name, ...)). Runs before call
        # (H) resolution so a field invocation can resolve regardless of which
        # (H) file the construction site lives in. Bindings are recorded PENDING
        # (H) (keyword name or positional index) and resolved by
        # (H) finalize_field_bindings after every file's ctor metadata (param
        # (H) order + param->attribute renames) has been collected.
        if language != cs.SupportedLanguage.PYTHON:
            return
        try:
            module_qn = self._module_qn(
                cached_relative_path(file_path, self.repo_path), file_path.name
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
        # (H) For every class defined in this file, record the ordered ctor param
        # (H) names (__init__ params, or annotated class-body fields for
        # (H) NamedTuple/dataclass classes without __init__) and the
        # (H) param -> attribute renames from `self.attr = param` statements.
        # (H) The stack carries each node's ENCLOSING class qn so a nested class
        # (H) (Inner inside Outer) resolves to module.Outer.Inner: a bare
        # (H) resolve_function_call("Inner", module_qn) would look for
        # (H) module.Inner and miss it. A class inside a FUNCTION is skipped (its
        # (H) qn is caller-scoped), so nested-in-method classes never reach here.
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
            # (H) Descend into the body with THIS class as the enclosing scope so
            # (H) its nested classes resolve; skip metadata when unresolved.
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
                    # (H) *args/**kwargs/keyword_separator never bind fields.
                    name = None
            if name and name != cs.PY_KEYWORD_SELF:
                names.append(name)
        return tuple(names)

    def _record_param_attr_renames(
        self, init_node: Node, class_qn: str, params: set[str]
    ) -> None:
        # (H) `self.ctx_factory = create_context` stores the param under a
        # (H) DIFFERENT name; the field invocation goes through the attribute.
        body = init_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return
        self_prefix = f"{cs.PY_KEYWORD_SELF}{cs.SEPARATOR_DOT}"
        stack: list[Node] = list(body.children)
        while stack:
            node = stack.pop()
            # (H) A `self.x = param` inside a nested helper (def later():
            # (H) self.cb = handler) is that helper's store, not a constructor
            # (H) rename; descending into it would let it override the real
            # (H) __init__ store (one attr per param), so stop at nested scopes.
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
        # (H) NamedTuple/dataclass field order = annotated class-body assignments
        # (H) (`fetch_name: Callable`, `other: int = 3`) in declaration order.
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
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        func_class_captures_cache: dict[Path, dict] | None = None,
    ) -> None:
        relative_path = cached_relative_path(file_path, self.repo_path)
        logger.debug(ls.CALL_PROCESSING_FILE, path=relative_path)

        try:
            module_qn = self._module_qn(relative_path, file_path.name)

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
            # (H) Bare decorators (`@task`) are not call nodes; emit their
            # (H) module-load CALLS before the empty-`all_call_nodes` early return,
            # (H) since a file may have decorators but no other calls. Classes can
            # (H) be decorated too, so include captured class nodes.
            # (H) A dispatch table (HANDLERS = {"k": fn}) at module scope keeps its
            # (H) entries reachable; scan before the no-calls early return so a file
            # (H) that only defines functions and a table is still covered. Runs for
            # (H) every flow language with an object/array/dict literal form.
            if language == cs.SupportedLanguage.PYTHON or language in _JS_TS_LANGUAGES:
                self._ingest_collection_function_references(
                    root_node,
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                    module_qn,
                    None,
                    None,
                    self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
                )
                # (H) A module-scope first-class assignment (registry_handler =
                # (H) handle_event, module.exports.run = run) references its target
                # (H) even in a file with no call expressions, so scan before the
                # (H) no-calls early return.
                self._ingest_assignment_function_references(
                    root_node,
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                    module_qn,
                    None,
                    None,
                    self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
                )
            if language in _JS_TS_LANGUAGES:
                # (H) A module-scope JSX element (export default <App />) can sit
                # (H) in a file with no call expressions; scan before the early
                # (H) return like the assignment pass.
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
            if not all_call_nodes:
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
                module_calls = self._filter_top_level_calls(
                    all_call_nodes, call_starts, sorted_func_nodes
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
        queries: dict[cs.SupportedLanguage, LanguageQueries],
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
        # (H) Rust only: calls inside a NAMED nested `fn` (which gets its own caller
        # (H) node) must be owned by that nested fn, not double-counted onto the
        # (H) enclosing free function. Anonymous closures (not attributable) stay
        # (H) excluded from this set so their calls still bubble up. Other languages
        # (H) keep the flat _filter_calls_in_node behavior their flow-tracing relies on.
        owned_func_nodes = self._attributable_func_nodes(func_nodes, language)
        for func_node in func_nodes:
            if has_classes and self._is_method(func_node, lang_config):
                continue

            if language in _C_FAMILY_LANGUAGES:
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
                # (H) A function expression bound to a variable or table field
                # (H) (`local f = function()`, `M.f = function()`) has no name field;
                # (H) the definition pass names it after its assignment target, so
                # (H) recover the same name here or the whole body would be skipped.
                func_name = lua_utils.extract_assigned_name(
                    func_node,
                    accepted_var_types=(cs.TS_DOT_INDEX_EXPRESSION, cs.TS_IDENTIFIER),
                )
            if not func_name:
                continue
            # (H) An out-of-line C++ method definition (`Ret Class::method() {...}`
            # (H) at namespace/file scope) is bound by the definition pass to its
            # (H) class node (qn `class_qn.method`). Attribute its body's calls to
            # (H) that method node, not a phantom module-rooted free-function qn,
            # (H) so the CALLS edges join to a real node.
            if language == cs.SupportedLanguage.CPP and (
                bound := self._cpp_out_of_class_method_caller(
                    func_node, func_name, module_qn
                )
            ):
                caller_qn, class_qn = bound
                filtered = (
                    self._filter_calls_in_node(all_call_nodes, call_starts, func_node)
                    if all_call_nodes is not None and call_starts is not None
                    else None
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
            # (H) A Go receiver method (`func (t T) m()`) is declared at file scope
            # (H) but the definition pass binds it to its receiver type's node
            # (H) (qn `module.T.m`). Attribute its body's calls to that method node,
            # (H) not the receiver-dropping `module.m` that _build_nested_qualified_name
            # (H) would produce, so the CALLS edges join to a real node.
            if language == cs.SupportedLanguage.GO and (
                bound := self._go_receiver_method_caller(
                    func_node, func_name, module_qn
                )
            ):
                caller_qn, container_qn = bound
                filtered = (
                    self._filter_calls_in_node(all_call_nodes, call_starts, func_node)
                    if all_call_nodes is not None and call_starts is not None
                    else None
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
            # (H) A C++ free function inside a namespace is bound by the definition
            # (H) pass via build_qualified_name (qn `module.ns.fn`); _build_nested...
            # (H) ignores namespace_definition ancestors and would drop the namespace
            # (H) (`module.fn`), dangling the CALLS source. Use the same builder so
            # (H) caller and node qns agree.
            func_qn = (
                cpp_utils.build_qualified_name(func_node, module_qn, func_name)
                if language == cs.SupportedLanguage.CPP
                else self._build_nested_qualified_name(
                    func_node, module_qn, func_name, lang_config
                )
            )
            if func_qn:
                if all_call_nodes is None or call_starts is None:
                    filtered = None
                elif language == cs.SupportedLanguage.RUST:
                    filtered = self._calls_owned_by(
                        func_node, owned_func_nodes, all_call_nodes, call_starts
                    )
                else:
                    filtered = self._filter_calls_in_node(
                        all_call_nodes, call_starts, func_node
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
        # (H) Resolve a Go receiver method to its (method_qn, container_qn),
        # (H) mirroring the definition pass's receiver-type binding. The receiver
        # (H) type resolves to its node qn (same-file or sibling-file in the
        # (H) package), and the registry check ensures the method node exists
        # (H) before overriding the default attribution.
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

    def _cpp_out_of_class_method_caller(
        self, func_node: Node, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        # (H) Resolve an out-of-line C++ method definition to its (method_qn,
        # (H) class_qn), mirroring the definition pass's class binding. The leaf
        # (H) class name resolves the class across files (header-declared classes);
        # (H) `endswith(normalized)` guards against a leaf collision binding to the
        # (H) wrong class, and the registry membership check ensures the method node
        # (H) actually exists before overriding the default attribution.
        if not cpp_utils.is_out_of_class_method_definition(func_node):
            return None
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
        # (H) Use the same bare-type extraction as the definition pass
        # (H) (rs_utils.extract_impl_target), which strips generic arguments
        # (H) (`Chars<'a>` -> `Chars`). _get_node_name returns the full generic
        # (H) text, so a call inside a generic impl block was attributed to a
        # (H) caller qn bearing the generics (crate.lib.Chars<'a>.go) that matches
        # (H) no registered node, silently dropping the CALLS edge.
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
        queries: dict[cs.SupportedLanguage, LanguageQueries],
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
        # (H) Only functions that get their own caller node exclude their calls from
        # (H) the enclosing scope; anonymous arrows (skipped below) must not, so
        # (H) their calls bubble up instead of dropping.
        owned_func_nodes = self._attributable_func_nodes(method_nodes, language)
        for method_node in method_nodes:
            # (H) The body byte-range slice also captures functions of a NESTED
            # (H) class (Outer body contains Inner.run); those belong to the
            # (H) nested class and are processed when it is iterated, so skip any
            # (H) whose nearest enclosing class is not this one (else run would
            # (H) also emit as the phantom Outer.run).
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
            # (H) method_nodes includes functions nested inside methods. Build the
            # (H) qn through the enclosing-function chain (Class.method.nested, not
            # (H) the method-dropping Class.nested) and label a nested function
            # (H) FUNCTION, so the CALLS edge joins the real node.
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
                class_qn,
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
        # (H) Build a class-body function's qn through the chain of enclosing
        # (H) functions up to the class: a direct method is Class.method (METHOD);
        # (H) a function nested in a method is Class.method.nested (FUNCTION).
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
        # (H) A Java Method node is registered with its parameter signature
        # (H) (definition pass: class_qn.name(params)), so the caller endpoint of a
        # (H) CALLS edge must carry the same signature to join that node. Mirrors
        # (H) class_ingest.mixin's method-qn build exactly.
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
        # (H) True when method_node's nearest enclosing class is the one whose
        # (H) body is class_body: walk up, and the first class ancestor's body
        # (H) must be this body (compared by byte span). A method with no
        # (H) enclosing class (out-of-class C++ definition captured elsewhere)
        # (H) returns True so existing handling is unaffected.
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
        # (H) Calls inside func_node MINUS calls owned by functions nested within
        # (H) it, so a call in a nested function is attributed only to the nested
        # (H) function, never also to the enclosing one.
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
        queries: dict[cs.SupportedLanguage, LanguageQueries],
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
            # (H) A C++ class inside a namespace, or a NESTED class (Outer.Inner),
            # (H) is bound by the definition pass through its enclosing scope
            # (H) (qn `module.ns.Class` / `module.Outer.Inner`); the bare
            # (H) `module.class_name` join drops those ancestors, dangling every
            # (H) inline method's CALLS source off a phantom node. Use the SAME
            # (H) builders the definition pass uses so the class qn (and thus
            # (H) method caller qns) agree.
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
        # (H) If the call's receiver base is a match-arm binding whose arm range
        # (H) contains this call, overlay that arm's variant type (shadowing the flat
        # (H) map's last-arm value) so `cmd.apply()` dispatches to the arm's variant.
        # (H) The innermost (smallest) containing arm wins for nested matches.
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

    def _macro_call_name(self, ident: Node) -> str | None:
        # (H) Reconstruct a `<recv>.method` chain from a macro token stream by walking
        # (H) the method identifier's preceding siblings over `("." <ident|self>)*`.
        # (H) `server . run` -> "server.run"; `self . shutdown . recv` ->
        # (H) "self.shutdown.recv". A method with no preceding `.` stays bare.
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
        # (H) A macro-internal call (Rust `name(args)` inside a token_tree) is
        # (H) captured as the bare identifier node; its text is the callee name. A
        # (H) macro tokenizes `server.run()` into loose tokens (`server . run ( )`),
        # (H) dropping the field_expression, so reconstruct any `<recv>.method`
        # (H) receiver chain from the preceding sibling tokens -- else the bare method
        # (H) mis-resolves (`server.run()` in tokio::select! -> the same-module free
        # (H) fn `run` instead of Listener.run).
        if call_node.type == cs.TS_IDENTIFIER and call_node.text is not None:
            return self._macro_call_name(call_node)
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
                    # (H) turbofish: unwrap to the underlying callee identifier
                    inner = func_child.child_by_field_name(cs.TS_FIELD_FUNCTION)
                    if inner and inner.text:
                        return inner.text.decode(cs.ENCODING_UTF8)
                case cs.TS_RS_FIELD_EXPRESSION if language == cs.SupportedLanguage.RUST:
                    # (H) Rust member call `a.b.method()`: use the full dotted receiver
                    # (H) chain as the call name so the resolver can map the receiver to
                    # (H) its inferred type (`self.shutdown.is_shutdown`,
                    # (H) `cmd.apply`). A chain containing a call (`x.f().g`) is left to
                    # (H) the chained-call path; a paren-free chain still ends at the
                    # (H) bare-method trie fallback when the receiver type is unknown.
                    if (text := func_child.text) is not None:
                        return text.decode(cs.ENCODING_UTF8)
                case cs.TS_CPP_FIELD_EXPRESSION:
                    field_node = func_child.child_by_field_name(cs.FIELD_FIELD)
                    if field_node and field_node.text:
                        method = field_node.text.decode(cs.ENCODING_UTF8)
                        # (H) Prepend a simple-identifier receiver (`obj->m`/`obj.m`
                        # (H) -> `obj.m`) so the resolver can map obj to its type and
                        # (H) bind the correct class method; a `.`-joined two-part name
                        # (H) still falls back to the bare method-name trie when the
                        # (H) receiver type is unknown. Complex receivers (chains,
                        # (H) calls, `this`) keep the bare method name, as before.
                        arg = func_child.child_by_field_name(cs.TS_FIELD_ARGUMENT)
                        if (
                            arg is not None
                            and arg.type == cs.TS_IDENTIFIER
                            and arg.text
                        ):
                            receiver = arg.text.decode(cs.ENCODING_UTF8)
                            return f"{receiver}{cs.SEPARATOR_DOT}{method}"
                        return method
                case cs.TS_PARENTHESIZED_EXPRESSION:
                    return self._get_iife_target_name(func_child)

        match call_node.type:
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
            # (H) Scala infix operator call (`a ~> b`, `xs map f`): the callee is the
            # (H) `operator` field's method name. tree-sitter has no `function` field
            # (H) here, so it is unreachable above. Gated to Scala since the node type
            # (H) string is Scala-specific but the guard keeps other languages inert.
            # (H) Infix is unambiguously a method call; a bare `field_expression`
            # (H) (`obj.done` with no parens) is deliberately NOT named here because
            # (H) Scala's uniform access makes a nullary call and a `val` read
            # (H) syntactically identical, so resolving it by simple name would turn a
            # (H) same-named field read into a spurious CALLS edge.
            case cs.TS_SCALA_INFIX_EXPRESSION if language == cs.SupportedLanguage.SCALA:
                operator_node = call_node.child_by_field_name(cs.FIELD_OPERATOR)
                if operator_node and operator_node.text:
                    return operator_node.text.decode(cs.ENCODING_UTF8)

        if name_node := call_node.child_by_field_name(cs.FIELD_NAME):
            if name_node.text is not None:
                return name_node.text.decode(cs.ENCODING_UTF8)

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
        queries: dict[cs.SupportedLanguage, LanguageQueries],
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

        # (H) Rust match arms often reuse one binding name (`cmd`) for different variant
        # (H) types; the flat map keeps only the last arm's type. These per-arm scoped
        # (H) bindings let each call inside an arm resolve against ITS arm's type.
        match_arm_bindings: list[tuple[int, int, str, str]] = []
        if language == cs.SupportedLanguage.RUST and local_var_types is not None:
            match_arm_bindings = self._resolver.type_inference.rust_type_inference.collect_match_arm_bindings(
                caller_node
            )

        caller_spec = (caller_type, cs.KEY_QUALIFIED_NAME, caller_qn)

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
            # (H) Every flow-traced language records its callable params and the
            # (H) closures it returns, so a later `x = factory(); x(cb)` alias call can
            # (H) flow cb into the returned closure regardless of source language.
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

        # (H) Runs independently of call_nodes: a getter access is an attribute, not
        # (H) a call, so callers that read a property but make no other call must
        # (H) still reach this pass before the early return below.
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

        # (H) Operator syntax (k in r, r[k], r[k]=v, len(r)) dispatches to dunder
        # (H) methods; emit those edges when the operand is a first-party type.
        if language == cs.SupportedLanguage.PYTHON:
            self._ingest_operator_dispatch_calls(
                caller_node, caller_spec, module_qn, local_var_types
            )
        if language == cs.SupportedLanguage.PYTHON or language in _JS_TS_LANGUAGES:
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
            self._ingest_returned_function_references(
                caller_node,
                caller_spec,
                module_qn,
                local_var_types,
                class_context,
                self._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
                caller_qn,
            )
        # (H) Dispatch-table handler references, for every flow language. Module-scope
        # (H) literals are scanned explicitly in process_calls_in_file (before the
        # (H) no-calls early return), so only nested scopes here.
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
        is_js_ts = language in _JS_TS_LANGUAGES
        is_cpp = language == cs.SupportedLanguage.CPP
        # (H) Template type-parameter names in scope at this caller (`template<typename
        # (H) SAX>` -> {"SAX"}): a receiver typed to one has no concrete type here, so the
        # (H) dispatch fan-out treats it like an untyped receiver rather than an external.
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
        # (H) Languages with interprocedural callable-parameter flow enabled: a
        # (H) callback passed to a first-party function whose parameter is invoked
        # (H) (directly or in a nested closure) is traced to the concrete callback.
        is_flow_lang = (
            is_python
            or language == cs.SupportedLanguage.GO
            or language in _JS_TS_LANGUAGES
            or is_cpp
        )
        alias_map: dict[str, str] | None = None
        factory_aliases: dict[str, str] | None = None

        for call_node in call_nodes:
            node_id = _id(call_node)
            if call_name_cache is not None and node_id in call_name_cache:
                call_name = call_name_cache[node_id]
            else:
                call_name = get_target(call_node, language)
                if call_name_cache is not None:
                    call_name_cache[node_id] = call_name
            if not call_name:
                continue

            call_var_types = local_var_types
            if match_arm_bindings:
                call_var_types = self._overlay_match_arm_binding(
                    call_name, call_node, local_var_types, match_arm_bindings
                )

            if is_cpp:
                # (H) A C++ member call through a template-parameter (or otherwise
                # (H) unresolved) receiver has no concrete type, so precise resolution
                # (H) fails and the external-receiver guard drops the edge entirely,
                # (H) orphaning every structural interface implementer (json_sax_*
                # (H) visitors dispatched via `sax->start_object()`). Fan such a call out
                # (H) to the method on every class defining it. Runs before the primary
                # (H) resolution/continue below so it fires even when that edge is
                # (H) dropped; a concretely-typed receiver is skipped inside the resolver.
                for target_type, target_qn in resolver.cpp_dispatch_targets(
                    call_name, call_var_types, cpp_template_params
                ):
                    for variant in resolver.function_registry.variants(target_qn):
                        ensure_rel(
                            caller_spec, calls_rel, (target_type, qn_key, variant)
                        )

            if is_java and call_node.type == method_invocation_type:
                callee_info = resolver.resolve_java_method_call(
                    call_node, module_qn, local_var_types
                )
            else:
                callee_info = resolve_func(
                    call_name, module_qn, call_var_types, class_context, caller_qn
                )
            if not callee_info and resolve_builtin is not None:
                callee_info = resolve_builtin(call_name)
            if not callee_info and resolve_cpp_op is not None:
                callee_info = resolve_cpp_op(call_name, module_qn)
            if not callee_info and cs.SEPARATOR_DOT not in call_name:
                if is_python:
                    # (H) A bare name that resolves to nothing may be a local alias of a
                    # (H) callable (do = self._start; do()). Resolve the assignment's
                    # (H) right-hand side and treat the alias call as a call to it.
                    if alias_map is None:
                        alias_map = self._build_local_alias_map(
                            caller_node, queries[language][cs.QUERY_CONFIG], module_qn
                        )
                    if (rhs := alias_map.get(call_name)) is not None:
                        callee_info = resolve_func(
                            rhs, module_qn, local_var_types, class_context, caller_qn
                        )
                if callee_info is None and is_flow_lang:
                    # (H) `x = factory(...); x(cb)`: x holds a closure returned by a
                    # (H) first-party factory (e.g. a retry/cache decorator applied
                    # (H) imperatively). Record the call so cb flows into that closure's
                    # (H) callable parameter once factory returns are known (finalize).
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
                # (H) recv.field(...) where field is a callable struct field:
                # (H) resolve to the functions bound to it at construction sites.
                self._ingest_callable_field_calls(
                    call_name, caller_spec, local_var_types, ensure_rel
                )

            if is_python and call_name.rsplit(cs.SEPARATOR_DOT, 1)[-1] in (
                cs.HIGHER_ORDER_BUILTINS
            ):
                # (H) sorted(xs, key=f) and friends invoke f synchronously in this
                # (H) frame, so the trace attributes the call to the enclosing fn.
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
                if is_flow_lang:
                    # (H) The callee is not first-party (a framework/stdlib call such as
                    # (H) grpclib Handler(self.__rpc_x), JS setTimeout(target), or a
                    # (H) runtime dispatcher), so the call chain cannot be followed into
                    # (H) it. A first-party function handed to it as an argument is still
                    # (H) wired to be invoked, so record it as referenced from this scope
                    # (H) to keep it reachable, across every flow-traced language.
                    self._ingest_argument_function_references(
                        call_node,
                        caller_spec,
                        module_qn,
                        local_var_types,
                        class_context,
                        resolve_func,
                        ensure_rel,
                        caller_qn,
                    )
                continue

            callee_type, callee_qn = callee_info

            # (H) A callee that resolved to a builtin (e.g. JS setTimeout(target))
            # (H) has no first-party body to follow into, so pass-through flow is
            # (H) pointless; but a first-party callback handed to it is still invoked,
            # (H) so record a reference edge from this scope to keep it reachable. The
            # (H) builtin call edge itself is still emitted by the normal path below.
            callee_is_builtin = is_flow_lang and callee_qn.startswith(
                _BUILTIN_QN_PREFIX
            )
            if callee_is_builtin:
                self._ingest_argument_function_references(
                    call_node,
                    caller_spec,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    ensure_rel,
                    caller_qn,
                )

            if is_flow_lang and not callee_is_builtin:
                self._collect_callable_flow(
                    call_node,
                    callee_qn,
                    caller_qn,
                    caller_params,
                    module_qn,
                    local_var_types,
                    class_context,
                )
                # (H) Functions are first-class values: a first-party callee may STORE
                # (H) a passed callback for later dynamic dispatch (config objects,
                # (H) codecs, registries), which callable-param flow cannot trace, or
                # (H) the callee may even be a same-named misbind of an external
                # (H) method. Record the pass itself as a REFERENCES edge from the
                # (H) passing scope so the callback is never reported dead.
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
                )

            if is_python and (
                dispatch_targets := resolver.protocol_dispatch_targets(callee_qn)
            ):
                # (H) The call resolved to a Protocol stub; the stub never runs, so emit
                # (H) edges to the method on every conformer instead of the stub.
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
                # (H) self.M()/cls.M() statically targets the enclosing class's own or
                # (H) inherited M and dynamically dispatches to every concrete subclass
                # (H) override, so emit an edge to each in ADDITION to the resolved edge
                # (H) below; otherwise the base (or an override) reached only through the
                # (H) self-call looks like dead code. Anchor on the enclosing class, not
                # (H) the resolved callee: when M is abstract with several overrides the
                # (H) trie resolves the call to an arbitrary sibling override, so
                # (H) anchoring there would miss the base and the other overrides. The
                # (H) self/cls receiver excludes super().M() and Base.M() (not virtual
                # (H) dispatch). Skip self.attr.M() (a call on a member, not on self).
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
                # (H) f(...) invoked through a parameter: the edge runs from the
                # (H) callee to whatever each call site binds to that parameter.
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

            if callee_type == class_label:
                # (H) Record construction as INSTANTIATES -> the class node (keeps
                # (H) CALLS function/method-only). When the class defines __init__,
                # (H) ALSO redirect a CALLS edge to it (the constructor runs); when
                # (H) it does not (dataclass/NamedTuple/pydantic), INSTANTIATES is
                # (H) the only edge.
                for class_variant in resolver.function_registry.variants(callee_qn):
                    ensure_rel(
                        caller_spec,
                        cs.RelationshipType.INSTANTIATES,
                        (class_label, qn_key, class_variant),
                    )
                init_qn = f"{callee_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
                if init_qn not in resolver.function_registry:
                    continue
                callee_type = cs.NodeLabel.METHOD
                callee_qn = init_qn

            for target_qn in resolver.function_registry.variants(callee_qn):
                ensure_rel(
                    caller_spec,
                    calls_rel,
                    (callee_type, qn_key, target_qn),
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
                    # (H) A bare object as a condition is tested for truthiness; nested
                    # (H) boolean/not operators are handled when the walk reaches them.
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
        # (H) Truthiness of an object calls __bool__ if defined, else __len__. Only a
        # (H) bare name/attribute operand names an object (a comparison/call is already
        # (H) a bool and is handled elsewhere); try __bool__ first, then __len__.
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
        # (H) Resolve the implied <operand>.__dunder__ call; resolution only succeeds
        # (H) for a first-party class that defines the dunder, so builtin containers
        # (H) (dict/list) yield no edge. Restrict to simple attribute/name operands.
        # (H) Returns whether an edge was emitted (truthiness tries __bool__ then __len__).
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
        # (H) `x = some_function` binds a first-class function value to a name; the
        # (H) alias is then stored, passed onward, or returned for dynamic dispatch
        # (H) (http_callback = llm_http_task_closure_with_context), so the assignment
        # (H) itself references the function and must keep it reachable. Only a plain
        # (H) name/attribute RHS counts (calls resolve as calls); the walk stops at
        # (H) nested scope boundaries, which own their own pass.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self.ingestor.ensure_relationship_batch
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types:
                continue
            if rhs_field := _ASSIGNMENT_RHS_FIELDS.get(node.type):
                right = node.child_by_field_name(rhs_field)
                # (H) `export const persist = persistImpl as unknown as Persist`
                # (H) wraps the aliased impl in TS casts; unwrap them so the bare
                # (H) name/arrow underneath is what we reference.
                if right is not None:
                    right = self._unwrap_ts_cast(right)
                # (H) A bare-name RHS names a callable; an inline arrow/function-expr
                # (H) RHS (`OpenAPI.TOKEN = async () => {}`) stores an anonymous
                # (H) function on the target for later invocation -- _emit_callback_edge
                # (H) references it by position. A named arrow-const RHS is registered
                # (H) by its name, so the by-position lookup simply finds nothing.
                if right is not None and (
                    right.type in _ASSIGNMENT_RHS_REF_TYPES
                    or right.type in _INLINE_FUNC_VALUE_TYPES
                ):
                    self._emit_callback_edge(
                        caller_spec,
                        right,
                        module_qn,
                        local_var_types,
                        class_context,
                        resolve_func,
                        ensure_rel,
                        caller_qn,
                        cs.RelationshipType.REFERENCES,
                    )
            stack.extend(node.children)

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
        # (H) `<Card />` renders the Card component: the framework invokes it
        # (H) through the element, never by a call the graph can see, so the JSX
        # (H) usage references the component. Uppercase names only -- lowercase
        # (H) tags are HTML elements and must not misbind to same-named locals.
        # (H) The walk stops at nested scope boundaries (each nested function's
        # (H) own pass covers its JSX), but continues THROUGH jsx elements so
        # (H) nested markup is covered by the scope that renders it.
        # (H) Resolve and emit directly rather than via _emit_callback_edge: a
        # (H) class component resolves to a CLASS node whose reference must point
        # (H) at the class itself, but that helper redirects CLASS -> __init__
        # (H) (Python construction semantics) and drops the edge when __init__ is
        # (H) absent, as it always is for a JS/TS class.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self.ingestor.ensure_relationship_batch
        registry = self._resolver.function_registry
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            # (H) Stop at a nested scope that gets its OWN caller pass (a named
            # (H) function/arrow, a class), but continue THROUGH an anonymous arrow
            # (H) (a `.map()`/`cell`/forwardRef callback): those are skipped as
            # (H) callers, so their JSX -- rendered on behalf of this scope -- would
            # (H) otherwise be scanned by nobody and report as dead.
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
                # (H) A `{...}` attribute value hands its inner expression to the
                # (H) element as a prop. A bare identifier (onClick={handleLogout})
                # (H) or inline arrow (onClick={() => x()}) is a function the
                # (H) framework invokes on the event, so reference it; other
                # (H) expressions resolve to nothing and are skipped by the helper.
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
        # (H) A function handed back via `return` (a useEffect cleanup
        # (H) `return () => unsubscribe()`, a factory `return handler`) is invoked by
        # (H) whoever receives it, never by a call the graph can see. Reference it from
        # (H) the returning scope. Walk continues through anonymous arrows (the effect
        # (H) callback is anonymous, so its `return` bubbles here) but stops at named
        # (H) nested functions, which own their returns.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self.ingestor.ensure_relationship_batch
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types and not self._is_unowned_js_scope(node):
                continue
            if node.type == cs.TS_RETURN_STATEMENT:
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

    def _ingest_collection_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
    ) -> None:
        # (H) A function/method placed as a value in a dict/object or list/array literal
        # (H) is a dispatch table wired to be invoked later (handlers[key](...)),
        # (H) commonly dispatched by a dynamic string key or in another module where the
        # (H) call site is not statically resolvable. Treat each such reference as a call
        # (H) from the enclosing scope so the handler is reachable. The walk stops at
        # (H) nested function/class boundaries, so a table built inside a nested scope
        # (H) is attributed to that scope's own pass, not this one.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self.ingestor.ensure_relationship_batch
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types:
                continue
            if node.type in _DICT_LIKE_COLLECTION_TYPES:
                for pair in node.named_children:
                    if (
                        pair.type == cs.TS_PY_PAIR
                        and (value := pair.child_by_field_name(cs.FIELD_VALUE))
                        is not None
                    ):
                        if value.type in _INLINE_FUNC_VALUE_TYPES:
                            self._emit_inline_value_function_ref(
                                pair, value, caller_spec, ensure_rel
                            )
                            continue
                        self._emit_value_function_ref(
                            value,
                            caller_spec,
                            module_qn,
                            local_var_types,
                            class_context,
                            resolve_func,
                            ensure_rel,
                        )
            elif node.type in _SEQUENCE_LIKE_COLLECTION_TYPES:
                for element in node.named_children:
                    self._emit_value_function_ref(
                        element,
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
        # (H) A value cast for typing (`persistImpl as unknown as Persist`) is
        # (H) transparent for reference resolution; unwrap it first.
        node = self._unwrap_ts_cast(node)
        # (H) `fn.bind(ctx)` / `fn.call(...)` / `fn.apply(...)` in value position
        # (H) (onError: handleError.bind(toast)) hands off `fn`; the call resolves to
        # (H) the Function.prototype builtin, so unwrap to the bound function and
        # (H) reference it instead, or it reports as dead.
        if (bound := self._unwrap_bound_function(node)) is not None:
            node = bound
        # (H) Only a bare name / attribute / member-expression in value position names
        # (H) a function; a call, comprehension or literal is not a reference to a
        # (H) callable itself. Reuses the flow-arg ref types (identifier, Python
        # (H) attribute, Go selector, JS/TS member expression).
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

    def _unwrap_ts_cast(self, node: Node) -> Node:
        # (H) Peel TS cast wrappers (`x as T`, `x satisfies T`, `x!`) to the wrapped
        # (H) value; they are transparent for reference resolution. The wrapped value
        # (H) is the first named child; casts nest (`x as unknown as T`), so loop.
        current = node
        while current.type in cs.TS_CAST_WRAPPER_TYPES:
            inner = current.named_child(0)
            if inner is None:
                break
            current = inner
        return current

    def _unwrap_bound_function(self, node: Node) -> Node | None:
        # (H) For `fn.bind(ctx)` (a call_expression whose function is `fn.bind`),
        # (H) return the bound function `fn` (the member object) so the value is
        # (H) referenced as `fn`, not the Function.prototype builtin. call/apply use
        # (H) the function the same way. Returns None when the node is not such a call.
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
        ensure_rel,
    ) -> None:
        # (H) An inline arrow/function-expression object value is registered by the
        # (H) definition pass under {enclosing_scope}.<name>. An identifier key names it
        # (H) by the key (scope.onSuccess); a string-literal key ({'onSuccess': ...})
        # (H) has no property name, so it registers as scope.anonymous_<row>_<col> from
        # (H) the value's position. Reference every candidate that is actually
        # (H) registered (variants cover same-name duplicates in one scope).
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
        for candidate in candidates:
            for target_qn in registry.variants(candidate):
                if registry.get(target_qn) is None:
                    continue
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.REFERENCES,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, target_qn),
                )

    def _emit_inline_arg_function_ref(
        self,
        arg_node: Node,
        source_spec: tuple[str, str, str],
        ensure_rel,
        caller_qn: str | None = None,
        rel_type: cs.RelationshipType = cs.RelationshipType.REFERENCES,
    ) -> None:
        # (H) An inline arrow/function-expression call argument is registered by the
        # (H) definition pass as {enclosing_scope}.anonymous_<row>_<col> from its own
        # (H) start position. The anonymous node lives in the CALLER's scope, so build
        # (H) the candidate from caller_qn (source_spec[2] is the callee for the
        # (H) callable-param path). Registry guard skips unregistered names.
        registry = self._resolver.function_registry
        scope_qn = caller_qn or source_spec[2]
        candidate = (
            f"{scope_qn}{cs.SEPARATOR_DOT}{cs.PREFIX_ANONYMOUS}"
            f"{arg_node.start_point[0]}_{arg_node.start_point[1]}"
        )
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
        # (H) A nested function/class scope; a scan of a function body must not descend
        # (H) into one, since its returns and local bindings belong to it, not the
        # (H) enclosing scope. The Python set is unioned in so Python keeps its exact
        # (H) prior behaviour (its decorated_definition wrapper is not a config type).
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
        # (H) Record which functions/closures this function may return, so a call site
        # (H) that binds and invokes the returned value (x = factory(); x(cb)) can flow
        # (H) cb into the returned closure. Only this scope's own return statements
        # (H) count; a nested function's returns belong to it.
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
                    if nested_qn in registry:
                        self._returned_callables.setdefault(caller_qn, set()).add(
                            nested_qn
                        )
                    elif (
                        resolved := resolve_func(
                            name, module_qn, local_var_types, class_context, caller_qn
                        )
                    ) is not None and resolved[0] in _CALLABLE_NODE_LABELS:
                        self._returned_callables.setdefault(caller_qn, set()).add(
                            resolved[1]
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
        # (H) Map a local `x` to the function `factory` in `x = factory(...)`, so a
        # (H) later `x(cb)` can be traced through factory's returned closure. Handles
        # (H) each flow language's binding form (Python assignment, JS/TS
        # (H) variable_declarator, Go short_var_declaration, C++ init_declarator).
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
        # (H) Yield (local_name, called_function_name) for a `x = f(...)` binding node.
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
        # (H) `const x = factory(...) as T` wraps the call in a cast; unwrap so the
        # (H) factory binding is still recognised.
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
        # (H) Go `a, b := f(), g()`: pair each left identifier with the call in the
        # (H) same position of the right expression list.
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
        args_node = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if args_node is None:
            return positional, keyword
        for child in args_node.named_children:
            if child.type == cs.TS_PY_KEYWORD_ARGUMENT:
                name_node = child.child_by_field_name(cs.FIELD_NAME)
                value_node = child.child_by_field_name(cs.FIELD_VALUE)
                if (
                    name_node is not None
                    and value_node is not None
                    and (name := safe_decode_text(name_node)) is not None
                ):
                    keyword[name] = value_node
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
    ) -> None:
        # (H) A TS cast (`handler as any`, `fn satisfies T`, `cb!`) is transparent for
        # (H) reference resolution; unwrap it so the callable underneath resolves.
        arg_node = self._unwrap_ts_cast(arg_node)
        # (H) An arrow/function-expression passed DIRECTLY as a call argument
        # (H) (useCallback(() => {}), setTimeout(() => {}), arr.map(x => ...)) is
        # (H) registered anonymously in the enclosing scope but named after no
        # (H) identifier, so resolve_func cannot find it. The call consumes it, so
        # (H) reference it by position the same way inline object-literal values are.
        if arg_node.type in _INLINE_FUNC_VALUE_TYPES:
            self._emit_inline_arg_function_ref(
                arg_node, source_spec, ensure_rel, caller_qn, rel_type
            )
            return
        if not (arg_text := safe_decode_text(arg_node)):
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
        # (H) Record, for each call-site argument that names a callable, whether it is a
        # (H) concrete function or a parameter of the caller (a pass-through). The
        # (H) fixpoint in finalize propagates concretes through pass-through params to
        # (H) the functions that actually invoke them.
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
            # (H) `f(callback as any)` casts the argument; unwrap so a cast callable
            # (H) argument still flows.
            arg_node = self._unwrap_ts_cast(raw_arg)
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

    def finalize_callable_param_flow(self) -> None:
        # (H) Resolve the recorded call-site argument bindings to a fixpoint and emit a
        # (H) CALLS edge from every function that invokes a callable parameter to each
        # (H) concrete function that can reach it (directly or via pass-through params).
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
        # (H) A nested closure a function returns is reachable whenever that function
        # (H) is reached (it is created and handed back as the return value). Nested
        # (H) functions are no longer roots, so this producer edge keeps a genuinely
        # (H) used closure (a returned decorator/formatter) live without reviving the
        # (H) closures of an unreachable outer function.
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
                # (H) The returned closure runs when the alias is called, so it is
                # (H) reachable from the enclosing scope.
                closure_type = registry.get(closure_qn)
                if closure_type is None:
                    continue
                scope_type = registry.get(fc.scope_qn) or cs.NodeLabel.MODULE
                ensure_rel(
                    (scope_type, cs.KEY_QUALIFIED_NAME, fc.scope_qn),
                    cs.RelationshipType.CALLS,
                    (closure_type, cs.KEY_QUALIFIED_NAME, closure_qn),
                )
                # (H) Each argument the closure receives seeds its callable parameter,
                # (H) so the callback is reached wherever the closure invokes it.
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
    ) -> None:
        # (H) A function/method passed as an argument is a first-class value the
        # (H) callee may invoke (external framework) or store for later dynamic
        # (H) dispatch (first-party plumbing); either way the passing scope holds a
        # (H) live reference, so emit an edge to keep the callback reachable.
        # (H) External/builtin callees keep the historical CALLS edge; first-party
        # (H) callees record the pass as REFERENCES (the precise invocation edge, if
        # (H) any, comes from callable-param flow).
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
                rel_type,
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
        # (H) An alias rhs is a plain name/attribute, a conditional that picks one
        # (H) (resolve_builtin_call if is_js_ts else None), or getattr(recv, name)
        # (H) dynamic dispatch. Take the name/attribute branch (consequence or
        # (H) alternative, never the condition) or build recv.<name> for getattr.
        # (H) A TS cast (`y as T`) is transparent; unwrap so `x = y as T` still aliases.
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
        # (H) Resolve a getattr name argument to its string value: a string literal
        # (H) directly, or a module-level constant (cs.METHOD_X / METHOD_X) read from
        # (H) the defining module's AST.
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
        if file_path is None or file_path not in type_inference.ast_cache:
            return None
        root_node, _ = type_inference.ast_cache[file_path]
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
        # (H) Accessing an @property getter invokes the getter method at runtime, but
        # (H) tree-sitter sees a plain attribute, not a call. Resolve attribute
        # (H) accesses whose tail names a known property and emit a CALLS edge to the
        # (H) getter (skipping the attribute that is itself a call's function, which
        # (H) the call path above already resolves).
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
                if name_node := current.child_by_field_name(cs.FIELD_NAME):
                    text = name_node.text
                    if text is not None:
                        path_parts.append(text.decode(cs.ENCODING_UTF8))
            elif current.type in lang_config.class_node_types:
                return None

            current = current.parent

        path_parts.reverse()
        if path_parts:
            return f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(path_parts)}{cs.SEPARATOR_DOT}{func_name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{func_name}"

    def _js_ts_arrow_binding_name(self, func_node: Node) -> str | None:
        # (H) An arrow / function expression has no `name` field, so the call pass
        # (H) skipped it and never processed its body's calls. Recover the binding
        # (H) name for the two named forms whose value IS the arrow: a module/local
        # (H) `const f = () => ...` (variable_declarator) and a class field
        # (H) `helper = () => ...` (public_field_definition). The body's calls then
        # (H) attribute to the same qn the definition pass registered. Anonymous /
        # (H) destructured arrows stay unnamed (skipped), as before.
        if func_node.type not in (cs.TS_ARROW_FUNCTION, cs.TS_FUNCTION_EXPRESSION):
            return None
        parent = func_node.parent
        if parent is None:
            return None
        # (H) func_node must be the parent's value/initializer for both forms
        # (H) (variable_declarator and public_field_definition), so one value check
        # (H) covers both. `==` not `is`: py-tree-sitter returns a fresh Node wrapper
        # (H) per access, so identity comparison always fails (Node `==` compares id).
        if parent.child_by_field_name(cs.FIELD_VALUE) != func_node:
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
        # (H) The func nodes that will get their own caller node: named functions
        # (H) plus arrows/function-expressions bound to a name. An anonymous arrow
        # (H) passed directly as an argument (`hooks.tap(name, (x) => {...})`) has
        # (H) neither, so it is skipped by the call loop. Its calls must therefore
        # (H) NOT be excluded from the enclosing named scope by _calls_owned_by;
        # (H) otherwise they attribute to nothing and drop (webpack is saturated
        # (H) with this callback pattern). Returning only attributable nodes as the
        # (H) exclusion set lets an anonymous arrow's calls bubble up to the nearest
        # (H) named function/method, matching where the oracle attributes them.
        if language == cs.SupportedLanguage.RUST:
            # (H) A Rust closure (`expire.map(|_| state.next_expiration())`) is unnamed,
            # (H) so the call loop skips it (no caller node); like JS/TS anon arrows its
            # (H) calls must bubble to the enclosing fn/method (which holds the captured
            # (H) locals' types) instead of being excluded and dropped. Named nested
            # (H) `fn`s stay attributable and keep their own calls.
            return [n for n in func_nodes if n.type != cs.TS_RS_CLOSURE_EXPRESSION]
        if language not in _JS_TS_LANGUAGES:
            return func_nodes
        return [
            n
            for n in func_nodes
            if self._get_node_name(n) or self._js_ts_arrow_binding_name(n)
        ]

    def _is_unowned_js_scope(self, node: Node) -> bool:
        # (H) An anonymous arrow/function-expression that gets no caller node of its
        # (H) own (no name, no binding name) -- a `.map()`/`cell`/forwardRef callback.
        # (H) Its calls bubble up to the enclosing named scope (_attributable_func_nodes),
        # (H) and so must its JSX references: the enclosing JSX walk continues through it.
        if node.type not in (cs.TS_ARROW_FUNCTION, cs.TS_FUNCTION_EXPRESSION):
            return False
        return not (self._get_node_name(node) or self._js_ts_arrow_binding_name(node))

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool:
        return is_method_node(func_node, lang_config)
