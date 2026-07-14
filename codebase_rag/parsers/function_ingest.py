from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LANGUAGE_FQN_SPECS, LanguageSpec
from ..types_defs import (
    ASTNode,
    CppDefinitionSpan,
    DeferredCppInherit,
    DeferredParentLink,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    NodeType,
    PropertyDict,
    SimpleNameLookup,
)
from ..utils.path_utils import cached_relative_path, cached_resolve_posix
from . import export_detection
from .cpp import utils as cpp_utils
from .dart import dart_definition_end_point
from .go import utils as go_utils
from .lua import utils as lua_utils
from .rs import utils as rs_utils
from .utils import (
    callable_parameter_indices,
    extract_modifiers_and_decorators,
    function_span_key,
    get_function_captures,
    ingest_method,
    is_method_node,
    record_cpp_definition_span,
    safe_decode_text,
)

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from ..types_defs import LanguageQueries
    from .handlers import LanguageHandler


def _nearest_preceding_csharp_type(
    func_node: Node, class_node_types: frozenset[str]
) -> Node | None:
    # (H) Find the class a `#if`-detached member belongs to: scan preceding siblings
    # (H) at each ancestor level (a `#if`-guarded member is wrapped in a preproc_if,
    # (H) so its OWN siblings never include the truncated class) up to the root,
    # (H) returning the nearest class-like node. For the truncation case this is the
    # (H) class whose body ended early, spilling this member after it.
    node: Node | None = func_node
    while node is not None:
        sib = node.prev_sibling
        while sib is not None:
            if sib.type in class_node_types:
                return sib
            sib = sib.prev_sibling
        node = node.parent
    return None


def _java_anon_base_for_function(
    func_node: Node, class_node_types: frozenset[str]
) -> str | None:
    # (H) A Java method declared inside a method-body anonymous class
    # (H) (`createBoundField(){ return new BoundField(){ @Override write(){} } }`) is
    # (H) captured here as a FUNCTION (the class-method pass skips method-nested defs),
    # (H) so it needs its base-type override link too. Walk to the nearest enclosing
    # (H) type: an anon `class_body` (parent is object_creation) before any NAMED class
    # (H) means it overrides that base; return the base type name (generics stripped).
    current = func_node.parent
    while current is not None:
        if current.type in class_node_types:
            return None
        if current.type == cs.TS_CLASS_BODY:
            parent = current.parent
            if parent is not None and parent.type == cs.TS_OBJECT_CREATION_EXPRESSION:
                type_node = parent.child_by_field_name(cs.FIELD_TYPE)
                if type_node is not None and type_node.text is not None:
                    return type_node.text.decode(cs.ENCODING_UTF8).split(
                        cs.CHAR_ANGLE_OPEN, 1
                    )[0]
            return None
        current = current.parent
    return None


class FunctionResolution(NamedTuple):
    qualified_name: str
    name: str
    is_exported: bool
    # (H) True when the name was GENERATED (anonymous_row_col): a JS/TS named
    # (H) pass (object literal, export, assignment, prototype) may own this
    # (H) same source function, so its registration defers until those ran.
    is_anonymous: bool = False


class _DeferredJsAnonymous(NamedTuple):
    """Unnamed JS/TS function expression held back until named passes claim.

    The generic function pass runs before the JS-specific passes, so
    registering `anonymous_row_col` eagerly minted a second node for every
    function a named pass registers later (521 locations on thrift's JS
    corpora). Registration happens at the per-file flush, only for spans no
    named pass claimed.
    """

    func_node: Node
    resolution: FunctionResolution
    module_qn: str
    language: cs.SupportedLanguage
    lang_config: LanguageSpec
    lang_queries: LanguageQueries


class _DeferredMethod(NamedTuple):
    """Out-of-class C++ method whose class hasn't been parsed yet.

    namespace_path carries the definition site's enclosing namespaces so the
    class resolves scope-first: two same-leaf classes (ast::Type and
    ast::analysis::Type) are otherwise indistinguishable by leaf lookup.
    """

    method_name: str
    class_name: str
    fallback_class_qn: str
    method_props: PropertyDict
    return_type: str | None
    module_qn: str
    namespace_path: str
    start_line: int
    start_col: int
    end_line: int
    lang_queries: LanguageQueries | None = None


class _DeferredGoMethod(NamedTuple):
    """Go receiver method, linked to its receiver type once all types are known."""

    method_node: Node
    module_qn: str
    receiver_type: str
    file_path: Path | None
    lang_queries: LanguageQueries | None = None


class _DeferredCppContainment(NamedTuple):
    """DEFINES from an out-of-class C++ method to a function nested in its body.

    The parent method's final qn is only known after the deferred method
    resolution binds it to its class (declared in a possibly later-parsed
    header), so the containment edge must wait for that pass. namespace_path
    carries the definition site's enclosing namespaces: the written qualifier
    inside `namespace beta { void Widget::print() ... }` is just `Widget`, and
    without the namespace two same-leaf classes are indistinguishable.
    """

    child_qn: str
    class_name: str
    method_name: str
    module_qn: str
    namespace_path: str


# (H) Go node labels a receiver type can resolve to (struct -> Class, defined
# (H) type/alias -> Type, interface -> Interface); used to pick the declaring
# (H) type out of same-named candidates when binding a cross-file method.
_GO_TYPE_NODE_TYPES = frozenset({NodeType.CLASS, NodeType.TYPE, NodeType.INTERFACE})


class FunctionIngestMixin:
    __slots__ = ()
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    module_qn_to_file_path: dict[str, Path]
    java_anon_overrides: list[tuple[str, str, str, str]]
    _handler: LanguageHandler
    _deferred_cpp_methods: list[_DeferredMethod]
    _deferred_go_methods: list[_DeferredGoMethod]
    _deferred_cpp_containment: list[_DeferredCppContainment]
    _deferred_parent_links: list[DeferredParentLink]
    method_return_types: dict[str, str]
    cpp_out_of_class_methods: dict[tuple[str, int], tuple[str, str]]
    function_locations: dict[FunctionSpanKey, FunctionLocation]
    cpp_definition_spans: dict[str, list[CppDefinitionSpan]]
    macro_qns: set[str]
    _deferred_js_anonymous: list[_DeferredJsAnonymous]
    class_inheritance: dict[str, list[str]]
    _deferred_cpp_inherits: list[DeferredCppInherit]
    rehydrated_definition_paths: dict[str, str]
    csharp_methods: set[str]
    csharp_override_methods: set[str]

    @abstractmethod
    def _get_docstring(self, node: ASTNode) -> str | None: ...

    def _ingest_all_functions(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list] | None = None,
    ) -> None:
        if combined_captures is not None:
            lang_queries = queries[language]
            lang_config: LanguageSpec = lang_queries[cs.QUERY_CONFIG]
            captures = combined_captures
        else:
            result = get_function_captures(root_node, language, queries)
            if not result:
                return
            lang_config, captures = result
            lang_queries = queries[language]
        file_path = self.module_qn_to_file_path.get(module_qn)
        has_classes = bool(captures.get(cs.CAPTURE_CLASS))

        for func_node in captures.get(cs.CAPTURE_FUNCTION, []):
            if has_classes and self._is_method(func_node, lang_config):
                continue

            # (H) A C# local function whose name is a reserved keyword is a
            # (H) parse-recovery artifact -- a `#if` splitting an if/else chain
            # (H) mid-method makes tree-sitter parse the trailing `else if` as a
            # (H) local_function_statement named `if`. Drop it wholesale instead of
            # (H) emitting a bogus (or anonymized) Function node.
            if (
                language == cs.SupportedLanguage.CSHARP
                and func_node.type == cs.TS_CSHARP_LOCAL_FUNCTION_STATEMENT
            ):
                name_node = func_node.child_by_field_name(cs.FIELD_NAME)
                if (
                    name_node
                    and name_node.text
                    and safe_decode_text(name_node) in cs.CSHARP_RESERVED_KEYWORDS
                ):
                    continue

            # (H) A C# member declaration (method/property/operator/ctor) that a
            # (H) `#if`-truncated class node detached into the namespace's
            # (H) declaration_list reaches here with no class ancestor. It is a real
            # (H) class member by grammar invariant, so recover its class and emit it
            # (H) as a Method rather than mislabelling it a module Function.
            if (
                language == cs.SupportedLanguage.CSHARP
                and self._recover_csharp_orphan_method(
                    func_node, module_qn, lang_config, lang_queries, file_path
                )
            ):
                continue

            if language == cs.SupportedLanguage.CPP:
                if self._handle_cpp_out_of_class_method(
                    func_node, module_qn, lang_queries
                ):
                    continue
                # (H) The query captures a templated function twice: the
                # (H) template_declaration wrapper AND its inner definition.
                # (H) The wrapper is the canonical node (mirroring the class
                # (H) rule); registering the inner too mints a `qn@line`
                # (H) duplicate that call attribution can bind to (issue #652).
                if (
                    func_node.type == cs.CppNodeType.FUNCTION_DEFINITION
                    and func_node.parent is not None
                    and func_node.parent.type == cs.CppNodeType.TEMPLATE_DECLARATION
                ):
                    continue

            if language == cs.SupportedLanguage.GO and self._defer_go_receiver_method(
                func_node, module_qn, lang_queries
            ):
                continue

            resolution = self._resolve_function_identity(
                func_node, module_qn, language, lang_config, file_path
            )
            if not resolution:
                continue

            # (H) A nameless JS/TS function expression may be one a NAMED pass
            # (H) (object literal, export, assignment, prototype) registers
            # (H) under its real name; those passes run after this one, so hold
            # (H) the anonymous registration back and flush only unclaimed
            # (H) spans (each eager registration was a duplicate node).
            if language in cs.JS_TS_LANGUAGES and resolution.is_anonymous:
                self._deferred_js_anonymous.append(
                    _DeferredJsAnonymous(
                        func_node,
                        resolution,
                        module_qn,
                        language,
                        lang_config,
                        lang_queries,
                    )
                )
                continue

            self._register_function(
                func_node, resolution, module_qn, language, lang_config, lang_queries
            )

            # (H) Record a free C++ function's return type so a chained call off a
            # (H) factory (`make().run()`) can type the receiver and resolve the next
            # (H) hop. Runs here (not in the CPP resolver) because the unified-FQN path
            # (H) wins for C++ and would otherwise bypass the recording.
            if language == cs.SupportedLanguage.CPP and (
                return_type := cpp_utils.extract_return_type_name(func_node)
            ):
                self.method_return_types[resolution.qualified_name] = return_type

            # (H) Same for a free Rust fn (impl methods are recorded in class
            # (H) ingest): a call-bound local (`let s = make()`) types from
            # (H) this map. No impl target here, so a `Self` return stays None.
            if language == cs.SupportedLanguage.RUST and (
                return_type := rs_utils.extract_return_type_name(func_node, None)
            ):
                self.method_return_types[resolution.qualified_name] = return_type

    def _function_span_claimed(self, module_qn: str, func_node: Node) -> bool:
        # (H) A span is claimed when a pass recorded THIS function node's
        # (H) location; the column in the key keeps a same-line neighbour's
        # (H) claim from masking this node's own.
        return function_span_key(module_qn, func_node) in self.function_locations

    def _span_claimed_for_qn(
        self, module_qn: str, func_node: Node, candidate_qn: str
    ) -> bool:
        # (H) A named pass re-deriving the SAME qn another pass already
        # (H) registered for this span is a pure duplicate (it would collide
        # (H) into a spurious `qn@line` twin). A DIFFERENT qn is the deliberate
        # (H) twin model: `X.prototype.m = function m()` keeps both the
        # (H) fn-expr's own-name node and the member-name node (duplicate-QN
        # (H) design: keep both, CALLS-to-both), so it must still register.
        loc = self.function_locations.get(function_span_key(module_qn, func_node))
        if loc is None:
            return False
        claimed_base = loc.qualified_name.split(cs.DUP_QN_MARKER, 1)[0]
        return claimed_base == candidate_qn

    def _claim_function_span(
        self, module_qn: str, func_node: Node, label: str, qualified_name: str
    ) -> None:
        # (H) First claim wins; a later pass deriving a different qn for the
        # (H) same source function must skip registration, not mint a twin.
        key = function_span_key(module_qn, func_node)
        if key not in self.function_locations:
            self.function_locations[key] = FunctionLocation(
                label=label,
                qualified_name=qualified_name,
                container_qn=None,
            )

    def _flush_deferred_js_anonymous(self) -> None:
        deferred = self._deferred_js_anonymous
        self._deferred_js_anonymous = []
        for entry in deferred:
            if self._function_span_claimed(entry.module_qn, entry.func_node):
                continue
            self._register_function(
                entry.func_node,
                entry.resolution,
                entry.module_qn,
                entry.language,
                entry.lang_config,
                entry.lang_queries,
            )

    def _resolve_function_identity(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        file_path: Path | None,
    ) -> FunctionResolution | None:
        resolution = self._try_unified_fqn_resolution(
            func_node, module_qn, language, file_path
        )
        if resolution:
            return resolution

        return self._fallback_function_resolution(
            func_node, module_qn, language, lang_config
        )

    def _try_unified_fqn_resolution(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        file_path: Path | None,
    ) -> FunctionResolution | None:
        fqn_config = LANGUAGE_FQN_SPECS.get(language)
        if not fqn_config or not file_path:
            return None

        func_name = fqn_config.get_name(func_node)
        if not func_name:
            return None

        parts = [func_name]
        current = func_node.parent
        while current:
            if current.type in fqn_config.scope_node_types:
                if scope_name := fqn_config.get_name(current):
                    parts.append(scope_name)
            current = current.parent
        parts.reverse()

        # (H) Prefix with the module's resolved (collision-disambiguated) qn rather
        # (H) than recomputing from the path, so same-stem cross-language siblings
        # (H) stay distinct.
        func_qn = module_qn + cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(parts)
        simple_name = func_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]

        is_exported = export_detection.is_exported(func_node, simple_name, language)
        return FunctionResolution(func_qn, simple_name, is_exported)

    def _fallback_function_resolution(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> FunctionResolution | None:
        if language == cs.SupportedLanguage.CPP:
            return self._resolve_cpp_function(func_node, module_qn)
        return self._resolve_generic_function(
            func_node, module_qn, language, lang_config
        )

    def _resolve_cpp_class_qn(
        self, class_name: str, module_qn: str, exclude_qn: str | None = None
    ) -> tuple[str, bool]:
        """Look up an existing Class node for *class_name* across all parsed files.

        Returns ``(class_qn, resolved)`` where *resolved* is True when the
        qualified name was obtained from the function registry (i.e. the
        class has already been parsed, typically from a header file).
        """
        class_name_normalized = class_name.replace(
            cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT
        )
        leaf_name = class_name_normalized.rsplit(cs.SEPARATOR_DOT, 1)[-1]

        if leaf_name in self.simple_name_lookup:
            # (H) Sorted: the lookup is a set, and same-leaf classes in
            # (H) different namespaces would otherwise bind nondeterministically.
            for candidate_qn in sorted(self.simple_name_lookup[leaf_name]):
                if candidate_qn == exclude_qn:
                    continue
                node_type = self.function_registry.get(candidate_qn)
                if node_type in {NodeType.CLASS, NodeType.TYPE}:
                    # (H) An out-of-class nested definition keeps `Outer::Inner`
                    # (H) as one qn segment; normalize before the suffix check or
                    # (H) `::Inner` never matches `.Inner`.
                    normalized_candidate = candidate_qn.replace(
                        cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT
                    )
                    if normalized_candidate.endswith(
                        f".{class_name_normalized}"
                    ) and self._is_cpp_defined(candidate_qn):
                        return candidate_qn, True

        return f"{module_qn}.{class_name_normalized}", False

    def _is_cpp_defined(self, qn: str) -> bool:
        # (H) A C++ out-of-class method may only bind to a class defined in a
        # (H) C/C++ source file; matching a same-named class in another language
        # (H) would collide their qualified names. Resolve qn -> defining file by
        # (H) the longest module-qn prefix and check its extension.
        parts = qn.split(cs.SEPARATOR_DOT)
        while parts:
            if path := self.module_qn_to_file_path.get(cs.SEPARATOR_DOT.join(parts)):
                return (
                    path.suffix in cs.CPP_EXTENSIONS or path.suffix in cs.C_EXTENSIONS
                )
            parts = parts[:-1]
        # (H) An incremental run only populates module_qn_to_file_path for
        # (H) re-parsed files; a definition rehydrated from the graph resolves
        # (H) through its node's recorded file path instead.
        if rehydrated := self.rehydrated_definition_paths.get(qn):
            suffix = Path(rehydrated).suffix
            return suffix in cs.CPP_EXTENSIONS or suffix in cs.C_EXTENSIONS
        return False

    def _handle_cpp_out_of_class_method(
        self,
        func_node: Node,
        module_qn: str,
        lang_queries: LanguageQueries | None = None,
    ) -> bool:
        if not cpp_utils.is_out_of_class_method_definition(func_node):
            return False

        class_name = cpp_utils.extract_class_name_from_out_of_class_method(func_node)
        if not class_name:
            return False

        # (H) Scope-first (see resolve_deferred_cpp_methods): the enclosing
        # (H) namespaces distinguish same-leaf classes.
        namespace_path = cs.SEPARATOR_DOT.join(
            cpp_utils.extract_namespace_path(func_node)
        )
        candidates = [class_name]
        if namespace_path:
            candidates.insert(0, f"{namespace_path}{cs.SEPARATOR_DOT}{class_name}")
        resolved = False
        class_qn = ""
        for candidate in candidates:
            class_qn, resolved = self._resolve_cpp_class_qn(candidate, module_qn)
            if resolved:
                break
        file_path = self.module_qn_to_file_path.get(module_qn)
        # (H) The out-of-class DEFINITION carries the return type; record it here (keyed
        # (H) by the method qn) so a factory chain `parser(1).parse()` can type the
        # (H) receiver even when the class's in-class declaration wasn't captured (a
        # (H) header parsed separately or a forward decl). Deferred entries carry it
        # (H) forward to resolve_deferred_cpp_methods where the final qn is known.
        return_type = cpp_utils.extract_return_type_name(func_node)

        if resolved:
            ingest_method(
                method_node=func_node,
                container_qn=class_qn,
                container_type=cs.NodeLabel.CLASS,
                ingestor=self.ingestor,
                function_registry=self.function_registry,
                simple_name_lookup=self.simple_name_lookup,
                get_docstring_func=self._get_docstring,
                language=cs.SupportedLanguage.CPP,
                lang_queries=lang_queries,
                file_path=file_path,
                repo_path=self.repo_path,
            )
            if bound_name := cpp_utils.extract_function_name(func_node):
                # (H) Record the binding so Pass-3 call attribution reuses this
                # (H) exact decision instead of re-resolving (and diverging).
                bound_qn = f"{class_qn}{cs.SEPARATOR_DOT}{bound_name}"
                self.cpp_out_of_class_methods[
                    (module_qn, func_node.start_point[0] + 1)
                ] = (bound_qn, class_qn)
                self.function_locations[function_span_key(module_qn, func_node)] = (
                    FunctionLocation(
                        label=cs.NodeLabel.METHOD.value,
                        qualified_name=bound_qn,
                        container_qn=class_qn,
                    )
                )
                record_cpp_definition_span(
                    self.cpp_definition_spans,
                    cs.SupportedLanguage.CPP,
                    file_path,
                    self.repo_path,
                    func_node,
                    cs.NodeLabel.METHOD.value,
                    bound_qn,
                )
            if return_type and (
                method_name := cpp_utils.extract_function_name(func_node)
            ):
                self.method_return_types[
                    f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
                ] = return_type
        else:
            method_name = cpp_utils.extract_function_name(func_node)
            if not method_name:
                return True
            decorators = []
            modifiers = []
            if lang_queries:
                modifiers, decorators = extract_modifiers_and_decorators(
                    func_node, lang_queries
                )
            props: PropertyDict = {
                cs.KEY_NAME: method_name,
                cs.KEY_MODIFIERS: modifiers,
                cs.KEY_DECORATORS: decorators,
                cs.KEY_START_LINE: func_node.start_point[0] + 1,
                cs.KEY_END_LINE: func_node.end_point[0] + 1,
                cs.KEY_DOCSTRING: self._get_docstring(func_node),
            }
            if file_path is not None and self.repo_path is not None:
                props[cs.KEY_PATH] = cached_relative_path(
                    file_path, self.repo_path
                ).as_posix()
                props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
            if not hasattr(self, "_deferred_cpp_methods"):
                self._deferred_cpp_methods = []
            self._deferred_cpp_methods.append(
                _DeferredMethod(
                    method_name=method_name,
                    class_name=class_name,
                    fallback_class_qn=class_qn,
                    method_props=props,
                    return_type=return_type,
                    module_qn=module_qn,
                    namespace_path=cs.SEPARATOR_DOT.join(
                        cpp_utils.extract_namespace_path(func_node)
                    ),
                    start_line=func_node.start_point[0] + 1,
                    start_col=func_node.start_point[1],
                    end_line=func_node.end_point[0] + 1,
                    lang_queries=lang_queries,
                )
            )

        return True

    def resolve_deferred_cpp_methods(self) -> int:
        """Ingest deferred out-of-class C++ methods now that all classes are known.

        Called after all files have been parsed so that every Class node
        is guaranteed to be in the registry.  Returns the number of
        methods that were ingested.
        """
        deferred = getattr(self, "_deferred_cpp_methods", None)
        if not deferred:
            return 0

        ingested = 0
        for entry in deferred:
            # (H) Scope-first: the namespace-qualified name distinguishes
            # (H) same-leaf classes (ast::Type vs ast::analysis::Type); the raw
            # (H) written qualifier is the fallback for other scopes.
            candidates = [entry.class_name]
            if entry.namespace_path:
                candidates.insert(
                    0, f"{entry.namespace_path}{cs.SEPARATOR_DOT}{entry.class_name}"
                )
            resolved = False
            real_class_qn = entry.fallback_class_qn
            for candidate in candidates:
                real_class_qn, resolved = self._resolve_cpp_class_qn(candidate, "")
                if resolved:
                    break
            class_qn = real_class_qn if resolved else entry.fallback_class_qn
            method_qn = f"{class_qn}.{entry.method_name}"
            # (H) Record the binding so Pass-3 call attribution reuses this
            # (H) exact decision instead of re-resolving (and diverging).
            self.cpp_out_of_class_methods[(entry.module_qn, entry.start_line)] = (
                method_qn,
                class_qn,
            )
            self.function_locations[
                (entry.module_qn, entry.start_line, entry.start_col)
            ] = FunctionLocation(
                label=cs.NodeLabel.METHOD.value,
                qualified_name=method_qn,
                container_qn=class_qn,
            )

            props = dict(entry.method_props)
            props[cs.KEY_QUALIFIED_NAME] = method_qn
            if isinstance(path := props.get(cs.KEY_PATH), str):
                self.cpp_definition_spans.setdefault(path, []).append(
                    CppDefinitionSpan(
                        entry.start_line,
                        entry.end_line,
                        cs.NodeLabel.METHOD.value,
                        method_qn,
                    )
                )

            logger.info(ls.METHOD_FOUND.format(name=entry.method_name, qn=method_qn))
            self.ingestor.ensure_node_batch(cs.NodeLabel.METHOD, props)
            self.function_registry[method_qn] = NodeType.METHOD
            self.simple_name_lookup[entry.method_name].add(method_qn)
            if entry.return_type:
                self.method_return_types[method_qn] = entry.return_type

            if resolved or class_qn in self.function_registry:
                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_qn),
                    cs.RelationshipType.DEFINES_METHOD,
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
                )
            else:
                # (H) The class never resolved (not parsed, or its declaration is
                # (H) macro-corrupted); a DEFINES_METHOD to the phantom fallback qn
                # (H) would be dropped by the database and orphan the method, so
                # (H) anchor it to its module instead.
                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, entry.module_qn),
                    cs.RelationshipType.DEFINES,
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
                )
            ingested += 1

        self._deferred_cpp_methods = []
        return ingested

    def _defer_go_receiver_method(
        self,
        func_node: Node,
        module_qn: str,
        lang_queries: LanguageQueries | None = None,
    ) -> bool:
        if not go_utils.is_receiver_method(func_node):
            return False
        receiver_type = go_utils.extract_receiver_type_name(func_node)
        if not receiver_type:
            return False
        if not hasattr(self, "_deferred_go_methods"):
            self._deferred_go_methods = []
        self._deferred_go_methods.append(
            _DeferredGoMethod(
                method_node=func_node,
                module_qn=module_qn,
                receiver_type=receiver_type,
                file_path=self.module_qn_to_file_path.get(module_qn),
            )
        )
        return True

    def _resolve_go_container_qn(self, module_qn: str, receiver_type: str) -> str:
        # (H) A method binds to its receiver type. Prefer the same-file type, but
        # (H) a Go package spans every file in its directory, so fall back to a
        # (H) sibling-file type with the same name in the same package. This keeps
        # (H) the method's qn and DEFINES_METHOD parent anchored to the real type
        # (H) node instead of a phantom under the method's own module.
        same_file = f"{module_qn}{cs.SEPARATOR_DOT}{receiver_type}"
        if self.function_registry.get(same_file) is not None:
            return same_file
        package = module_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        for qn in self.simple_name_lookup.get(receiver_type, set()):
            if self.function_registry.get(qn) not in _GO_TYPE_NODE_TYPES:
                continue
            type_module = qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if type_module.rsplit(cs.SEPARATOR_DOT, 1)[0] == package:
                return qn
        return same_file

    def resolve_deferred_go_methods(self) -> int:
        """Ingest Go receiver methods now that every receiver type is registered.

        A Go method (``func (p Point) Area()``) is declared at file scope, not
        inside its receiver type, so the receiver's node may not exist yet when
        the method is first seen. Deferring to after Pass 2 lets the method bind
        to the actual node label (``Class`` for structs, ``Type`` for defined
        types, ``Interface`` for interfaces). Returns the number ingested.
        """
        deferred = getattr(self, "_deferred_go_methods", None)
        if not deferred:
            return 0

        for entry in deferred:
            container_qn = self._resolve_go_container_qn(
                entry.module_qn, entry.receiver_type
            )
            container_type = self.function_registry.get(container_qn)
            container_label = (
                cs.NodeLabel(container_type.value)
                if container_type is not None
                else cs.NodeLabel.CLASS
            )
            ingest_method(
                method_node=entry.method_node,
                container_qn=container_qn,
                container_type=container_label,
                ingestor=self.ingestor,
                function_registry=self.function_registry,
                simple_name_lookup=self.simple_name_lookup,
                get_docstring_func=self._get_docstring,
                language=cs.SupportedLanguage.GO,
                file_path=entry.file_path,
                repo_path=self.repo_path,
            )
            # (H) Record the method's return type so a chained call `c.Root().Run()`
            # (H) can resolve `Run` on the type `Root()` returns.
            method_name = self._extract_function_name(entry.method_node)
            if method_name and (
                return_type := go_utils.extract_return_type_name(entry.method_node)
            ):
                self.method_return_types[
                    f"{container_qn}{cs.SEPARATOR_DOT}{method_name}"
                ] = return_type
        ingested = len(deferred)
        self._deferred_go_methods = []
        return ingested

    def _resolve_cpp_function(
        self, func_node: Node, module_qn: str
    ) -> FunctionResolution | None:
        func_name = cpp_utils.extract_function_name(func_node)
        if not func_name:
            if func_node.type == cs.TS_CPP_LAMBDA_EXPRESSION:
                func_name = f"{cs.PREFIX_LAMBDA}{func_node.start_point[0]}_{func_node.start_point[1]}"
            else:
                return None

        func_qn = cpp_utils.build_qualified_name(func_node, module_qn, func_name)
        is_exported = cpp_utils.is_exported(func_node)
        return FunctionResolution(func_qn, func_name, is_exported)

    def _resolve_generic_function(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> FunctionResolution:
        func_name = self._extract_function_name(func_node)

        if (
            not func_name
            and language == cs.SupportedLanguage.LUA
            and func_node.type == cs.TS_LUA_FUNCTION_DEFINITION
        ):
            func_name = self._extract_lua_assignment_function_name(func_node)

        is_anonymous = not func_name
        if not func_name:
            func_name = self._generate_anonymous_function_name(func_node, module_qn)

        func_qn = self._build_function_qn(
            func_node, module_qn, func_name, language, lang_config
        )
        is_exported = export_detection.is_exported(func_node, func_name, language)
        return FunctionResolution(func_qn, func_name, is_exported, is_anonymous)

    def _build_function_qn(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> str:
        if language == cs.SupportedLanguage.RUST:
            return self._build_rust_function_qualified_name(
                func_node, module_qn, func_name
            )

        nested_qn = self._build_nested_qualified_name(
            func_node, module_qn, func_name, lang_config
        )
        return nested_qn or f"{module_qn}.{func_name}"

    def _register_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
    ) -> None:
        unique_qn = self.function_registry.register_unique_qn(
            resolution.qualified_name, func_node.start_point[0] + 1
        )
        if unique_qn != resolution.qualified_name:
            resolution = resolution._replace(qualified_name=unique_qn)

        func_props = self._build_function_props(
            func_node, resolution, module_qn, lang_queries
        )
        is_macro = func_node.type == cs.TS_RS_MACRO_DEFINITION
        if is_macro:
            # (H) Rust macros live in a separate namespace from functions;
            # (H) Pass-3 gates macro-invocation vs fn-call binding on macro_qns,
            # (H) and the persisted property lets incremental runs rehydrate the
            # (H) set for UNCHANGED files (the is_property pattern).
            func_props[cs.KEY_IS_MACRO] = True
        logger.info(
            ls.FUNC_FOUND.format(name=resolution.name, qn=resolution.qualified_name)
        )
        self.ingestor.ensure_node_batch(cs.NodeLabel.FUNCTION, func_props)

        self.function_registry[resolution.qualified_name] = NodeType.FUNCTION
        if is_macro:
            self.macro_qns.add(resolution.qualified_name)
        self.function_registry.mark_callable_params(
            resolution.qualified_name,
            callable_parameter_indices(func_node, language),
        )
        if resolution.name:
            self.simple_name_lookup[resolution.name].add(resolution.qualified_name)
        # (H) Record where this function landed so Pass-3 call attribution
        # (H) reuses the registered qn/label instead of re-deriving them (for
        # (H) every language: C++ preprocessor distortion, TS declaration
        # (H) merging, and duplicate-suffixed qns all make the walks diverge).
        location = FunctionLocation(
            label=cs.NodeLabel.FUNCTION.value,
            qualified_name=resolution.qualified_name,
            container_qn=None,
            is_named=not resolution.is_anonymous,
        )
        self.function_locations[function_span_key(module_qn, func_node)] = location
        record_cpp_definition_span(
            self.cpp_definition_spans,
            language,
            self.module_qn_to_file_path.get(module_qn),
            self.repo_path,
            func_node,
            cs.NodeLabel.FUNCTION.value,
            resolution.qualified_name,
        )
        if (
            language == cs.SupportedLanguage.CPP
            and func_node.type == cs.CppNodeType.TEMPLATE_DECLARATION
        ):
            self._record_cpp_template_child_location(func_node, module_qn, location)

        # (H) A method-body anonymous-class override (`new Base(){ @Override m(){} }`
        # (H) inside a method) is captured as a function here; record it so the deferred
        # (H) pass emits an OVERRIDES edge to Base.m, keeping the dispatch-only override
        # (H) live (field-initializer anon overrides are recorded in the class-method
        # (H) pass instead).
        if (
            language == cs.SupportedLanguage.JAVA
            and resolution.name
            and (
                base := _java_anon_base_for_function(
                    func_node, frozenset(lang_config.class_node_types)
                )
            )
        ):
            self.java_anon_overrides.append(
                (resolution.qualified_name, resolution.name, base, module_qn)
            )

        self._create_function_relationships(
            func_node, resolution, module_qn, language, lang_config
        )

    def _record_cpp_template_child_location(
        self, func_node: Node, module_qn: str, location: FunctionLocation
    ) -> None:
        # (H) A template wrapper's body walk in Pass 3 visits the INNER
        # (H) definition (it starts on its own line), so record the entry
        # (H) under the child's span too (the walk matches on that node).
        for child in func_node.children:
            if child.type == cs.CppNodeType.FUNCTION_DEFINITION:
                self.function_locations[function_span_key(module_qn, child)] = location
                break

    def _build_function_props(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        lang_queries: LanguageQueries,
    ) -> PropertyDict:
        file_path = self.module_qn_to_file_path.get(module_qn)
        modifiers, decorators = extract_modifiers_and_decorators(
            func_node, lang_queries
        )
        props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: resolution.qualified_name,
            cs.KEY_NAME: resolution.name,
            cs.KEY_MODIFIERS: modifiers,
            cs.KEY_DECORATORS: decorators,
            cs.KEY_START_LINE: func_node.start_point[0] + 1,
            # (H) Dart splits a definition into a signature node and a sibling
            # (H) function_body; extend the end over that body so the snippet
            # (H) covers the whole function (no-op for every other language).
            cs.KEY_END_LINE: dart_definition_end_point(func_node)[0] + 1,
            cs.KEY_DOCSTRING: self._get_docstring(func_node),
            cs.KEY_IS_EXPORTED: resolution.is_exported,
        }
        if file_path is not None:
            props[cs.KEY_PATH] = cached_relative_path(
                file_path, self.repo_path
            ).as_posix()
            props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
        return props

    def _create_function_relationships(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> None:
        # (H) A function nested in an out-of-class C++ method body (a lambda
        # (H) passed as a call argument) cannot bind its parent yet: the method's
        # (H) final qn is class-anchored and the class may live in a header not
        # (H) parsed until later, so the walk would emit a phantom free-fn parent
        # (H) that the database drops, orphaning the lambda (issue #650).
        if language == cs.SupportedLanguage.CPP and self._defer_cpp_containment(
            func_node, resolution.qualified_name, module_qn, lang_config
        ):
            return

        parent_type, parent_qn = self._determine_function_parent(
            func_node, resolution.qualified_name, module_qn, lang_config, language
        )
        self._emit_or_defer_defines(
            parent_type,
            parent_qn,
            cs.NodeLabel.FUNCTION,
            resolution.qualified_name,
            module_qn,
        )

        # (H) A Rust closure is a value constructed at its definition site (a `.map`
        # (H) arg, spawn body, `let` binding), so it is used wherever it is written.
        # (H) Dead-code reachability walks CALLS/REFERENCES (not DEFINES), so mirror the
        # (H) DEFINES with a REFERENCES from the enclosing scope -- else every closure is
        # (H) an orphan and reports dead. (JS/TS emit the analogous inline-callback ref.)
        if (
            language == cs.SupportedLanguage.RUST
            and func_node.type == cs.TS_RS_CLOSURE_EXPRESSION
        ):
            self.ingestor.ensure_relationship_batch(
                (parent_type, cs.KEY_QUALIFIED_NAME, parent_qn),
                cs.RelationshipType.REFERENCES,
                (
                    cs.NodeLabel.FUNCTION,
                    cs.KEY_QUALIFIED_NAME,
                    resolution.qualified_name,
                ),
            )

        if resolution.is_exported and language == cs.SupportedLanguage.CPP:
            self.ingestor.ensure_relationship_batch(
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                cs.RelationshipType.EXPORTS,
                (
                    cs.NodeLabel.FUNCTION,
                    cs.KEY_QUALIFIED_NAME,
                    resolution.qualified_name,
                ),
            )

    def _extract_function_name(self, func_node: Node) -> str | None:
        name_node = func_node.child_by_field_name(cs.FIELD_NAME)
        if name_node and name_node.text:
            return safe_decode_text(name_node)

        # (H) Anonymous function EXPRESSIONS bound to a declarator (`export const
        # (H) api = (function (x) {...}) as unknown as Api`) take the binding name
        # (H) exactly like arrows -- the call pass's binding-name climb accepts
        # (H) both, and the two passes must agree or the caller qn is a phantom.
        if func_node.type in (cs.TS_ARROW_FUNCTION, cs.TS_FUNCTION_EXPRESSION):
            current = func_node.parent
            while current:
                if current.type == cs.TS_VARIABLE_DECLARATOR:
                    # (H) `const m = useMutation({fn: () => {}})` binds the CALL
                    # (H) result to `m`, not the inner arrow; naming the arrow `m`
                    # (H) invents a phantom function (dead-code false positive). Only
                    # (H) claim the declarator name when its value is not a call.
                    value = current.child_by_field_name(cs.FIELD_VALUE)
                    if (
                        value is not None
                        and value.type in cs.JS_CALL_RESULT_VALUE_TYPES
                    ):
                        return None
                    for child in current.children:
                        if child.type == cs.TS_IDENTIFIER and child.text:
                            return safe_decode_text(child)
                    return None
                # (H) Crossing another function's body means this arrow is nested
                # (H) inside it (a JSX handler, a `.map()` callback), not bound to the
                # (H) outer const -- stop so it stays anonymous instead of inheriting
                # (H) the component's name as a `Component.Component` phantom.
                if current.type in cs.JS_ARROW_NAME_CLIMB_BOUNDARY:
                    return None
                current = current.parent

        return None

    def _generate_anonymous_function_name(self, func_node: Node, module_qn: str) -> str:
        parent = func_node.parent
        if parent and parent.type == cs.TS_PARENTHESIZED_EXPRESSION:
            grandparent = parent.parent
            if (
                grandparent
                and grandparent.type == cs.TS_CALL_EXPRESSION
                and grandparent.child_by_field_name(cs.FIELD_FUNCTION) == parent
            ):
                func_type = (
                    cs.PREFIX_ARROW
                    if func_node.type == cs.TS_ARROW_FUNCTION
                    else cs.PREFIX_FUNC
                )
                return f"{cs.PREFIX_IIFE}{func_type}_{func_node.start_point[0]}_{func_node.start_point[1]}"

        if (
            parent
            and parent.type == cs.TS_CALL_EXPRESSION
            and parent.child_by_field_name(cs.FIELD_FUNCTION) == func_node
        ):
            return f"{cs.PREFIX_IIFE_DIRECT}{func_node.start_point[0]}_{func_node.start_point[1]}"

        return f"{cs.PREFIX_ANONYMOUS}{func_node.start_point[0]}_{func_node.start_point[1]}"

    def _extract_lua_assignment_function_name(self, func_node: Node) -> str | None:
        return lua_utils.extract_assigned_name(
            func_node,
            accepted_var_types=(cs.TS_DOT_INDEX_EXPRESSION, cs.TS_IDENTIFIER),
        )

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
        skip_classes: bool = False,
    ) -> str | None:
        current = func_node.parent
        if not isinstance(current, Node):
            logger.warning(
                ls.CALL_UNEXPECTED_PARENT.format(
                    node=func_node, parent_type=type(current)
                )
            )
            return None

        path_parts = self._collect_ancestor_path_parts(
            func_node, current, lang_config, skip_classes
        )
        if path_parts is None:
            return None

        return self._format_nested_qn(module_qn, path_parts, func_name)

    def _collect_ancestor_path_parts(
        self,
        func_node: Node,
        current: Node | None,
        lang_config: LanguageSpec,
        skip_classes: bool,
    ) -> list[str] | None:
        path_parts: list[str] = []

        while current and current.type not in lang_config.module_node_types:
            result = self._process_ancestor_for_path(
                func_node, current, lang_config, skip_classes
            )
            if result is False:
                return None
            if result is not None:
                path_parts.append(result)
            current = current.parent

        path_parts.reverse()
        return path_parts

    def _process_ancestor_for_path(
        self,
        func_node: Node,
        current: Node,
        lang_config: LanguageSpec,
        skip_classes: bool,
    ) -> str | None | Literal[False]:
        if current.type in lang_config.function_node_types:
            return self._get_name_from_function_ancestor(current)

        if current.type in lang_config.class_node_types:
            return self._handle_class_ancestor(
                func_node, current, skip_classes, lang_config
            )

        if current.type == cs.TS_METHOD_DEFINITION:
            return self._extract_node_name(current)

        return None

    def _get_name_from_function_ancestor(self, node: Node) -> str | None:
        if name := self._extract_node_name(node):
            return name
        return self._extract_function_name(node)

    def _handle_class_ancestor(
        self,
        func_node: Node,
        class_node: Node,
        skip_classes: bool,
        lang_config: LanguageSpec,
    ) -> str | None | Literal[False]:
        if skip_classes:
            return None
        # (H) A function that is a DIRECT class member is a method, handled by the
        # (H) class-ingest path -- return False so the whole qn collapses to the flat
        # (H) module.name form that path expects. But a function NESTED inside a
        # (H) method body (a Promise executor `new Promise(cb)`, a defineProperty
        # (H) getter, any closure) must keep the full class.method.<name> path so the
        # (H) call pass, which always builds that path, references the same node;
        # (H) otherwise the closure is orphaned and reports as dead.
        if self._is_nested_within_class_member(func_node, class_node, lang_config):
            return self._extract_node_name(class_node)
        return False

    def _is_nested_within_class_member(
        self, func_node: Node, class_node: Node, lang_config: LanguageSpec
    ) -> bool:
        # (H) True when a function/method boundary sits between func_node and its
        # (H) enclosing class -- i.e. func_node lives inside a member's body rather
        # (H) than being the member itself. A direct method has no such intervening
        # (H) boundary (its parent chain reaches the class body directly).
        current = func_node.parent
        while current is not None and current != class_node:
            if (
                current.type == cs.TS_METHOD_DEFINITION
                or current.type in lang_config.function_node_types
            ):
                return True
            current = current.parent
        return False

    def _extract_node_name(self, node: Node) -> str | None:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        if name_node and name_node.text is not None:
            return safe_decode_text(name_node)
        return None

    def _format_nested_qn(
        self, module_qn: str, path_parts: list[str], func_name: str
    ) -> str:
        if path_parts:
            return f"{module_qn}.{cs.SEPARATOR_DOT.join(path_parts)}.{func_name}"
        return f"{module_qn}.{func_name}"

    def _build_rust_function_qualified_name(
        self, func_node: Node, module_qn: str, func_name: str
    ) -> str:
        path_parts = rs_utils.build_module_path(func_node)
        if path_parts:
            return f"{module_qn}.{cs.SEPARATOR_DOT.join(path_parts)}.{func_name}"
        return f"{module_qn}.{func_name}"

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool:
        return is_method_node(func_node, lang_config)

    def _csharp_scope_qn(self, node: Node, module_qn: str) -> str:
        # (H) Qualified name of a C# scope node (class/namespace) via the same walk
        # (H) the definition FQN pass uses, so a recovered class qn matches the one
        # (H) the class-ingest pass registered. `node` is itself a scope type, so
        # (H) start the walk at it (its own name is the innermost segment).
        fqn_config = LANGUAGE_FQN_SPECS[cs.SupportedLanguage.CSHARP]
        parts: list[str] = []
        current: Node | None = node
        while current is not None:
            if current.type in fqn_config.scope_node_types and (
                scope_name := fqn_config.get_name(current)
            ):
                parts.append(scope_name)
            current = current.parent
        if not parts:
            return module_qn
        parts.reverse()
        return module_qn + cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(parts)

    def _recover_csharp_orphan_method(
        self,
        func_node: Node,
        module_qn: str,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
        file_path: Path | None,
    ) -> bool:
        # (H) A C# method/property/operator/ctor is grammatically only ever a type
        # (H) member; a `local_function_statement` is the real top-level function, so
        # (H) it is deliberately excluded and left to the Function path.
        if func_node.type not in cs.CSHARP_MEMBER_ONLY_TYPES:
            return False
        class_node = _nearest_preceding_csharp_type(
            func_node, frozenset(lang_config.class_node_types)
        )
        if class_node is None:
            return False

        from .class_ingest.utils import csharp_has_override_modifier
        from .csharp import utils as csharp_utils

        class_qn = self._csharp_scope_qn(class_node, module_qn)
        cs_name, cs_params = csharp_utils.extract_method_signature(func_node)
        method_qualified_name = None
        if cs_name and cs_params:
            param_sig = cs.SEPARATOR_COMMA_SPACE.join(cs_params)
            method_qualified_name = f"{class_qn}.{cs_name}({param_sig})"

        ingested_qn = ingest_method(
            func_node,
            class_qn,
            cs.NodeLabel.CLASS,
            self.ingestor,
            self.function_registry,
            self.simple_name_lookup,
            self._get_docstring,
            cs.SupportedLanguage.CSHARP,
            lang_queries=lang_queries,
            method_qualified_name=method_qualified_name,
            file_path=file_path,
            repo_path=self.repo_path,
            # (H) The class node was parse-truncated; defer the containment so it
            # (H) resolves to DEFINES_METHOD if the class registered, else an
            # (H) audit-safe module DEFINES (never a dangling edge).
            defer_containment=self._deferred_parent_links,
            module_qn=module_qn,
        )
        if ingested_qn is None:
            return False
        record_cpp_definition_span(
            self.cpp_definition_spans,
            cs.SupportedLanguage.CSHARP,
            file_path,
            self.repo_path,
            func_node,
            cs.NodeLabel.METHOD.value,
            ingested_qn,
        )
        # (H) Record where this method landed so Pass-3 call attribution reuses this
        # (H) Method identity instead of re-deriving a module-Function qn from the
        # (H) node (the FQN walk sees no class ancestor, so a call inside the
        # (H) recovered member would otherwise source a phantom, dropped edge).
        self.function_locations[function_span_key(module_qn, func_node)] = (
            FunctionLocation(
                label=cs.NodeLabel.METHOD.value,
                qualified_name=ingested_qn,
                container_qn=class_qn,
            )
        )
        # (H) Track it like an in-class C# method so the override walk can gate a
        # (H) class-parent OVERRIDES on the `override` modifier.
        self.csharp_methods.add(ingested_qn)
        if csharp_has_override_modifier(func_node):
            self.csharp_override_methods.add(ingested_qn)
        return True

    def _find_enclosing_function_node(
        self, func_node: Node, lang_config: LanguageSpec
    ) -> Node | None:
        # (H) Mirrors _determine_function_parent's walk: first ancestor that is a
        # (H) function-like node, stopping at the module boundary.
        current = func_node.parent
        while current is not None and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                return current
            current = current.parent
        return None

    def _defer_cpp_containment(
        self,
        func_node: Node,
        child_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
    ) -> bool:
        enclosing = self._find_enclosing_function_node(func_node, lang_config)
        if enclosing is None or not cpp_utils.is_out_of_class_method_definition(
            enclosing
        ):
            return False
        class_name = cpp_utils.extract_class_name_from_out_of_class_method(enclosing)
        method_name = cpp_utils.extract_function_name(enclosing)
        if not class_name or not method_name:
            return False
        # (H) Keep the FULL written qualifier (ns::Widget): _resolve_cpp_class_qn
        # (H) splits the leaf itself and its endswith guard needs the qualifier to
        # (H) tell same-leaf classes in different namespaces apart.
        self._deferred_cpp_containment.append(
            _DeferredCppContainment(
                child_qn=child_qn,
                class_name=class_name,
                method_name=method_name,
                module_qn=module_qn,
                namespace_path=cs.SEPARATOR_DOT.join(
                    cpp_utils.extract_namespace_path(enclosing)
                ),
            )
        )
        return True

    def _emit_or_defer_defines(
        self,
        parent_label: str,
        parent_qn: str,
        child_label: str,
        child_qn: str,
        module_qn: str,
        fallback_label: str | None = None,
        fallback_qn: str | None = None,
    ) -> None:
        # (H) Module nodes always exist, so module-parented edges emit directly.
        # (H) Any other parent may be registered by a later pass (methods land
        # (H) after functions) or may be a phantom recomputed qn the database
        # (H) would drop; both resolve in resolve_deferred_parent_links, where
        # (H) the optional fallback (a nested child's lexical enclosing
        # (H) function) beats the module anchor.
        if parent_label == cs.NodeLabel.MODULE:
            self.ingestor.ensure_relationship_batch(
                (parent_label, cs.KEY_QUALIFIED_NAME, parent_qn),
                cs.RelationshipType.DEFINES,
                (child_label, cs.KEY_QUALIFIED_NAME, child_qn),
            )
            return
        self._deferred_parent_links.append(
            DeferredParentLink(
                parent_label_guess=parent_label,
                parent_qn=parent_qn,
                child_label=child_label,
                child_qn=child_qn,
                module_qn=module_qn,
                fallback_label=fallback_label,
                fallback_qn=fallback_qn,
            )
        )

    def _claimed_qn_for_anonymous_guess(
        self, module_qn: str, parent_qn: str
    ) -> tuple[str, str] | None:
        # (H) An `anonymous_row_col` guess names the SPAN it stood for; if a
        # (H) named pass claimed that span, the claim is the real parent.
        tail = parent_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        if not tail.startswith(cs.PREFIX_ANONYMOUS):
            return None
        row_col = tail[len(cs.PREFIX_ANONYMOUS) :].split(cs.CHAR_UNDERSCORE)
        if len(row_col) != 2 or not all(part.isdigit() for part in row_col):
            return None
        loc = self.function_locations.get(
            (module_qn, int(row_col[0]) + 1, int(row_col[1]))
        )
        if loc is None or loc.qualified_name not in self.function_registry:
            return None
        return loc.label, loc.qualified_name

    def resolve_deferred_parent_links(self) -> int:
        """Emit deferred non-module DEFINES once every pass has registered.

        A registered parent gets the edge under its real label; an
        unregistered parent qn is a phantom, so the child anchors to its
        module rather than losing the edge at the database MERGE.
        """
        deferred = getattr(self, "_deferred_parent_links", None)
        if not deferred:
            return 0
        emitted = 0
        for entry in deferred:
            if registered := self.function_registry.get(entry.parent_qn):
                parent_spec = (
                    cs.NodeLabel(registered.value),
                    cs.KEY_QUALIFIED_NAME,
                    entry.parent_qn,
                )
                rel_type = entry.rel_type
            elif (
                entry.fallback_qn is not None
                and entry.fallback_label is not None
                and self.function_registry.get(entry.fallback_qn) is not None
            ):
                # (H) The parent guess never registered but the child's lexical
                # (H) enclosing function did (a prototype assignment on a
                # (H) parameter inside a function body); the lexical parent is
                # (H) the true containment, not the module.
                parent_spec = (
                    cs.NodeLabel(entry.fallback_label),
                    cs.KEY_QUALIFIED_NAME,
                    entry.fallback_qn,
                )
                rel_type = cs.RelationshipType.DEFINES.value
            elif claimed := self._claimed_qn_for_anonymous_guess(
                entry.module_qn, entry.parent_qn
            ):
                # (H) The parent guess was an anonymous placeholder for a span
                # (H) a NAMED pass claimed after the child registered
                # (H) (`exports.receiver = function () { function helper() }`):
                # (H) the placeholder embeds its (row, col), so the claim
                # (H) record recovers the registered enclosing node.
                claimed_label, claimed_qn = claimed
                parent_spec = (
                    cs.NodeLabel(claimed_label),
                    cs.KEY_QUALIFIED_NAME,
                    claimed_qn,
                )
                rel_type = cs.RelationshipType.DEFINES.value
            else:
                # (H) A method whose container never registered (impl on a
                # (H) primitive, macro-corrupted class) anchors to its module
                # (H) with DEFINES; DEFINES_METHOD from a Module is not a
                # (H) documented shape.
                parent_spec = (
                    cs.NodeLabel.MODULE,
                    cs.KEY_QUALIFIED_NAME,
                    entry.module_qn,
                )
                rel_type = cs.RelationshipType.DEFINES.value
            self.ingestor.ensure_relationship_batch(
                parent_spec,
                rel_type,
                (entry.child_label, cs.KEY_QUALIFIED_NAME, entry.child_qn),
            )
            emitted += 1
        self._deferred_parent_links = []
        return emitted

    def resolve_deferred_cpp_containment(self) -> int:
        """Emit DEFINES for functions nested in out-of-class C++ method bodies.

        Runs after resolve_deferred_cpp_methods so the parent method nodes
        exist under their final class-anchored qns. Falls back to the file
        module rather than ever emitting a phantom parent.
        """
        deferred = getattr(self, "_deferred_cpp_containment", None)
        if not deferred:
            return 0
        emitted = 0
        for entry in deferred:
            # (H) Try the namespace-scoped name first (alpha.Widget beats a
            # (H) same-leaf beta.Widget via the endswith guard), then the raw
            # (H) written qualifier for classes matched through other scopes.
            candidates = [entry.class_name]
            if entry.namespace_path:
                candidates.insert(
                    0, f"{entry.namespace_path}{cs.SEPARATOR_DOT}{entry.class_name}"
                )
            parent_spec = (
                cs.NodeLabel.MODULE,
                cs.KEY_QUALIFIED_NAME,
                entry.module_qn,
            )
            for candidate in candidates:
                class_qn, resolved = self._resolve_cpp_class_qn(candidate, "")
                parent_qn = f"{class_qn}{cs.SEPARATOR_DOT}{entry.method_name}"
                if resolved and parent_qn in self.function_registry:
                    parent_spec = (
                        cs.NodeLabel.METHOD,
                        cs.KEY_QUALIFIED_NAME,
                        parent_qn,
                    )
                    break
            self.ingestor.ensure_relationship_batch(
                parent_spec,
                cs.RelationshipType.DEFINES,
                (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, entry.child_qn),
            )
            emitted += 1
        self._deferred_cpp_containment = []
        return emitted

    def _determine_function_parent(
        self,
        func_node: Node,
        func_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str]:
        current = func_node.parent
        if not isinstance(current, Node):
            return cs.NodeLabel.MODULE, module_qn

        file_path = self.module_qn_to_file_path.get(module_qn)
        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                parent_label = (
                    cs.NodeLabel.METHOD
                    if self._is_method(current, lang_config)
                    else cs.NodeLabel.FUNCTION
                )
                # (H) Bind to the enclosing function's OWN qn, recomputed from its
                # (H) node. A function nested in an anonymous callback otherwise
                # (H) loses that callback: anonymous scopes contribute no segment to
                # (H) the child qn, so trimming the child qn would skip the callback
                # (H) and hoist the child to the nearest named ancestor.
                # (H) A Go receiver method's node lives under its receiver type
                # (H) (module.Type.Method); identity resolution alone gives the
                # (H) receiver-dropping module.Method, a phantom, so a local
                # (H) type declared in the method body would fall back to the
                # (H) module instead of its true lexical parent.
                if (
                    language == cs.SupportedLanguage.GO
                    and go_utils.is_receiver_method(current)
                    and (name_node := current.child_by_field_name(cs.FIELD_NAME))
                    is not None
                    and (method_name := safe_decode_text(name_node))
                    and (receiver_type := go_utils.extract_receiver_type_name(current))
                ):
                    container_qn = self._resolve_go_container_qn(
                        module_qn, receiver_type
                    )
                    return (
                        cs.NodeLabel.METHOD,
                        f"{container_qn}{cs.SEPARATOR_DOT}{method_name}",
                    )
                # (H) Reuse the enclosing function's REGISTERED identity when
                # (H) its span is claimed: structural re-derivation produces
                # (H) the pre-claim qn (an anonymous name whose node no longer
                # (H) exists for `exports.f = function`, or the FIRST `t` for
                # (H) the second same-name fn expr registered as `t@line`),
                # (H) hoisting the child to the module or the wrong function.
                if language in cs.JS_TS_LANGUAGES:
                    recorded = self.function_locations.get(
                        function_span_key(module_qn, current)
                    )
                    if (
                        recorded is not None
                        and recorded.qualified_name != func_qn
                        and recorded.qualified_name in self.function_registry
                    ):
                        return cs.NodeLabel(recorded.label), recorded.qualified_name
                resolution = (
                    self._resolve_function_identity(
                        current, module_qn, language, lang_config, file_path
                    )
                    if language is not None
                    else None
                )
                parent_qn = (
                    resolution.qualified_name
                    if resolution
                    else func_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
                )
                if not parent_qn or parent_qn == func_qn:
                    break
                return parent_label, parent_qn

            current = current.parent

        # (H) A Rust item inside `mod inner` is contained by that inline module,
        # (H) not the file module. Its enclosing module qn is the file module plus
        # (H) the mod path; the inline Module node carries that exact qn.
        if language == cs.SupportedLanguage.RUST and (
            mod_parts := rs_utils.build_module_path(func_node)
        ):
            nested = module_qn + cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(mod_parts)
            return cs.NodeLabel.MODULE, nested

        return cs.NodeLabel.MODULE, module_qn
