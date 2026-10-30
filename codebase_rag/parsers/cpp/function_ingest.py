"""C/C++'s share of the function pass: the out-of-class method definitions
it binds to their class, now or once that class registers, the
artifact-shaped nodes and free-function prototypes it holds until every
definition is in, the class lookup the class pass shares, and the DEFINES
of a function nested in an out-of-class method's body.

`FunctionIngest` owns what every language shares (the scan of a file's
function captures, a function's identity, node and DEFINES edge) and hands
C/C++'s part to the `CppFunctionIngest` it builds."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...types_defs import (
    CppDefinitionSpan,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    OverloadSignature,
    PropertyDict,
    SimpleNameLookup,
)
from ...utils.path_utils import cached_relative_path, cached_resolve_posix
from ..anchor_hash import anchor_hash_props
from ..ast_fingerprint import fingerprint_props
from ..definition_facts import DefinitionFacts
from ..endpoints import emit_endpoints
from ..function_identity import FunctionResolution, name_start_point
from ..language_definitions import LanguageFunctionIngest
from ..method_ingest import MethodIngestHost, ingest_method
from ..utils import extract_modifiers_and_decorators, function_span_key
from . import utils as cpp_utils
from .overloads import cpp_overload_signature
from .utils import record_cpp_definition_span

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...services import IngestorProtocol
    from ...types_defs import LanguageQueries


class CppFunctionHost(Protocol):
    """What C/C++ function ingestion calls on the function pass that builds
    it: the identity a definition resolves to and the registration every
    Function node goes through, for the held-back prototypes and artifacts
    that register after all."""

    def _resolve_function_identity(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        file_path: Path | None,
    ) -> FunctionResolution | None: ...

    def _register_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
    ) -> None: ...


class _DeferredArtifact(NamedTuple):
    """Artifact-shaped C++ node held until every class is registered.

    A type-less plain-identifier definition is either a macro invocation
    (`FMT_CATCH(...) {}`) or a recovery-mangled real definition (`file(int fd)
    {}` whose class ancestor the parse recovery destroyed). A registered class
    bearing the name reattaches the node as that class's ctor METHOD; failing
    that, a named parameter keeps it as a module Function; failing both it is
    dropped as a macro. The class pass for the SAME file runs after the
    function pass, so every branch of the decision must wait for
    resolve_deferred_artifacts.
    """

    func_node: Node
    module_qn: str
    lang_config: LanguageSpec
    lang_queries: LanguageQueries


class _DeferredPrototype(NamedTuple):
    """Free-function prototype held until every definition is registered.

    A header prototype (`int FreeHelper(int);`) minted its own Function node
    beside the definition's (`utils.h.FreeHelper` vs `utils.FreeHelper`);
    calls bind to the definition, so the prototype node had zero incoming
    edges and reported dead forever (issue #893). When a bodied definition
    of the same namespace-qualified function registers anywhere, the
    prototype is dropped, mirroring the forward-declared-class machinery;
    a prototype-only function keeps its node.
    """

    func_node: Node
    module_qn: str
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
    # Read while the tree is alive: the definition is matched to the overload
    # its class declares only once that class is known (issue #2455).
    signature: OverloadSignature | None = None


class _DeferredContainment(NamedTuple):
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


class CppFunctionIngest(LanguageFunctionIngest):
    """Binds out-of-class C++ method definitions to their class, and decides
    the C/C++ nodes held back until every class and definition is registered.

    Built and owned by `FunctionIngest`. The registries, the module paths,
    the definition facts and the definition spans are the pass's own, held
    by reference."""

    __slots__ = (
        "_host",
        "_method_host",
        "ingestor",
        "repo_path",
        "function_registry",
        "simple_name_lookup",
        "module_qn_to_file_path",
        "facts",
        "cpp_definition_spans",
        "_deferred_methods",
        "_deferred_containment",
        "_deferred_artifacts",
        "_deferred_prototypes",
    )

    # A C++ definition is named through its declarator, a lambda by where it
    # is written, and either is qualified by the scopes around it.
    resolves_own_identity = True

    def __init__(
        self,
        host: CppFunctionHost,
        *,
        method_host: MethodIngestHost,
        ingestor: IngestorProtocol,
        repo_path: Path,
        function_registry: FunctionRegistryTrieProtocol,
        simple_name_lookup: SimpleNameLookup,
        module_qn_to_file_path: dict[str, Path],
        facts: DefinitionFacts,
        cpp_definition_spans: dict[str, list[CppDefinitionSpan]],
    ) -> None:
        self._host = host
        # What an out-of-class method is written through, and where its
        # docstring is read from.
        self._method_host = method_host
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.module_qn_to_file_path = module_qn_to_file_path
        self.facts = facts
        self.cpp_definition_spans = cpp_definition_spans
        self._deferred_methods: list[_DeferredMethod] = []
        self._deferred_containment: list[_DeferredContainment] = []
        # Macro-invocation-shaped C++ nodes held until every class (incl.
        # rehydrated ones) is known; resolve_deferred_artifacts decides
        # orphaned-ctor vs macro.
        self._deferred_artifacts: list[_DeferredArtifact] = []
        # Free-function prototypes held until every bodied definition is
        # registered; resolve_deferred_prototypes drops duplicates (#893).
        self._deferred_prototypes: list[_DeferredPrototype] = []

    def skips_function(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
        file_path: Path | None,
    ) -> bool:
        if self._handle_out_of_class_method(func_node, module_qn, lang_queries):
            return True
        # The query captures a templated function twice: the
        # template_declaration wrapper AND its inner definition.
        # The wrapper is the canonical node (mirroring the class
        # rule); registering the inner too mints a `qn@line`
        # duplicate that call attribution can bind to (issue #652).
        if (
            func_node.type == cs.CppNodeType.FUNCTION_DEFINITION
            and func_node.parent is not None
            and func_node.parent.type == cs.CppNodeType.TEMPLATE_DECLARATION
        ):
            return True
        # A macro invocation parsed as a type-less definition
        # (`FMT_CATCH(...) {}`) must not mint a phantom Function, and a
        # recovery-orphaned ctor of the same shape must register as a
        # METHOD of its class, not a module Function that steals the
        # class's qn. Same-file classes register after this pass, so EVERY
        # artifact-shaped node (named-param or not) is deferred; the flush
        # applies the class-registry tiebreak and named-param evidence.
        if cpp_utils.is_recovery_artifact_shape(func_node):
            self._deferred_artifacts.append(
                _DeferredArtifact(func_node, module_qn, lang_config, lang_queries)
            )
            return True
        # A most-vexing-parse misparse (`FlutterWindow window(project);`
        # inside a function body, issue #871) is a stack-object
        # construction, not a function declaration: minting it would
        # create a phantom Function the call pass can bind to (and
        # deferring it as a prototype would resurrect it at flush).
        # The construction edges are emitted by the per-caller
        # declaration pass instead.
        if cpp_utils.is_cpp_vexing_parse_construction(func_node):
            return True
        # A free-function PROTOTYPE (a declaration with a
        # function_declarator) may duplicate a bodied definition in
        # another file; hold it back so resolve_deferred_prototypes
        # can drop it when the definition registers (issue #893). A
        # prototype with INTERNAL linkage (`static`, or inside an
        # anonymous namespace) is TU-local, so no cross-module
        # definition can be its definition: it registers inline as
        # before.
        if (
            func_node.type == cs.CppNodeType.DECLARATION
            and any(
                child.type == cs.CppNodeType.FUNCTION_DECLARATOR
                for child in func_node.children
            )
            and not cpp_utils.cpp_declaration_has_internal_linkage(func_node)
        ):
            self._deferred_prototypes.append(
                _DeferredPrototype(func_node, module_qn, lang_config, lang_queries)
            )
            return True
        return False

    def resolve_function(
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

    def record_return_type(self, func_node: Node, qualified_name: str) -> None:
        # Record a free C++ function's return type so a chained call off a
        # factory (`make().run()`) can type the receiver and resolve the next
        # hop. Called once the function registers, not from resolve_function,
        # because the unified-FQN path wins for C++ and would otherwise bypass
        # the recording.
        if return_type := cpp_utils.extract_return_type_name(func_node):
            self.facts.method_return_types[qualified_name] = return_type

    def resolve_class_qn(
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
            # Sorted: the lookup is a set, so same-leaf classes in different
            # namespaces would otherwise bind nondeterministically.
            for candidate_qn in sorted(self.simple_name_lookup[leaf_name]):
                if candidate_qn == exclude_qn:
                    continue
                node_type = self.function_registry.get(candidate_qn)
                if node_type in {cs.NodeLabel.CLASS, cs.NodeLabel.TYPE}:
                    # An out-of-class nested definition keeps `Outer::Inner` as
                    # one qn segment; normalise before the suffix check or
                    # `::Inner` never matches `.Inner`.
                    normalized_candidate = candidate_qn.replace(
                        cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT
                    )
                    if normalized_candidate.endswith(
                        f".{class_name_normalized}"
                    ) and self._is_cpp_defined(candidate_qn):
                        return candidate_qn, True

        return f"{module_qn}.{class_name_normalized}", False

    def _is_cpp_defined(self, qn: str) -> bool:
        # A C++ out-of-class method may only bind to a class defined in a C/C++
        # source file; a same-named class in another language would collide their
        # qns. Resolve qn -> defining file by the longest module-qn prefix and
        # check its extension.
        parts = qn.split(cs.SEPARATOR_DOT)
        while parts:
            if path := self.module_qn_to_file_path.get(cs.SEPARATOR_DOT.join(parts)):
                return (
                    path.suffix in cs.CPP_EXTENSIONS or path.suffix in cs.C_EXTENSIONS
                )
            parts = parts[:-1]
        # An incremental run only populates module_qn_to_file_path for re-parsed
        # files; a definition rehydrated from the graph resolves through its node's
        # recorded file path instead.
        if rehydrated := self.facts.rehydrated_definition_paths.get(qn):
            suffix = Path(rehydrated).suffix
            return suffix in cs.CPP_EXTENSIONS or suffix in cs.C_EXTENSIONS
        return False

    def _handle_out_of_class_method(
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

        # Scope-first (see resolve_deferred_methods): the enclosing
        # namespaces distinguish same-leaf classes.
        namespace_path = cs.SEPARATOR_DOT.join(
            cpp_utils.extract_namespace_path(func_node)
        )
        class_qn, resolved = self._resolve_scoped_class(
            class_name, namespace_path, module_qn
        )
        file_path = self.module_qn_to_file_path.get(module_qn)
        # The out-of-class DEFINITION carries the return type; record it here (keyed
        # by the method qn) so a factory chain `parser(1).parse()` can type the
        # receiver even when the in-class declaration was not captured (a header
        # parsed separately or a forward decl). Deferred entries carry it forward to
        # resolve_deferred_methods where the final qn is known.
        return_type = cpp_utils.extract_return_type_name(func_node)

        if resolved:
            self._ingest_resolved_out_of_class(
                func_node, module_qn, class_qn, file_path, return_type, lang_queries
            )
        else:
            self._defer_out_of_class(
                func_node,
                module_qn,
                class_name,
                class_qn,
                file_path,
                return_type,
                lang_queries,
            )
        return True

    def _resolve_scoped_class(
        self, class_name: str, namespace_path: str, module_qn: str
    ) -> tuple[str, bool]:
        candidates = [class_name]
        if namespace_path:
            candidates.insert(0, f"{namespace_path}{cs.SEPARATOR_DOT}{class_name}")
        resolved = False
        class_qn = ""
        for candidate in candidates:
            class_qn, resolved = self.resolve_class_qn(candidate, module_qn)
            if resolved:
                break
        return class_qn, resolved

    def _ingest_resolved_out_of_class(
        self,
        func_node: Node,
        module_qn: str,
        class_qn: str,
        file_path: Path | None,
        return_type: str | None,
        lang_queries: LanguageQueries | None,
    ) -> None:
        ingested_qn = ingest_method(
            self._method_host,
            func_node,
            class_qn,
            cs.NodeLabel.CLASS,
            cs.SupportedLanguage.CPP,
            lang_queries=lang_queries,
            file_path=file_path,
            module_qn=module_qn,
        )
        if bound_name := cpp_utils.extract_function_name(func_node):
            # Record the binding so Pass-3 call attribution reuses this exact
            # decision rather than re-resolve and diverge. The registered qn,
            # not the plain one: an overload's body belongs to its `@line`
            # node (issue #2455).
            bound_qn = ingested_qn or f"{class_qn}{cs.SEPARATOR_DOT}{bound_name}"
            self.facts.cpp_out_of_class_methods[
                (module_qn, func_node.start_point[0] + 1)
            ] = (
                bound_qn,
                class_qn,
            )
            self.facts.function_locations[function_span_key(module_qn, func_node)] = (
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
        if return_type and (method_name := cpp_utils.extract_function_name(func_node)):
            self.facts.method_return_types[
                f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
            ] = return_type

    def _defer_out_of_class(
        self,
        func_node: Node,
        module_qn: str,
        class_name: str,
        class_qn: str,
        file_path: Path | None,
        return_type: str | None,
        lang_queries: LanguageQueries | None,
    ) -> None:
        method_name = cpp_utils.extract_function_name(func_node)
        if not method_name:
            return
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
            cs.KEY_START_COL: func_node.start_point[1],
            cs.KEY_NAME_START_LINE: name_start_point(func_node)[0],
            cs.KEY_NAME_START_COL: name_start_point(func_node)[1],
            cs.KEY_END_LINE: func_node.end_point[0] + 1,
            cs.KEY_DOCSTRING: self._method_host._get_docstring(
                func_node, cs.SupportedLanguage.CPP
            ),
        }
        if file_path is not None and self.repo_path is not None:
            props[cs.KEY_PATH] = cached_relative_path(
                file_path, self.repo_path
            ).as_posix()
            props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
        # Computed here, not at flush: the tree (and this node) is gone by
        # the time deferred methods are written out.
        props.update(fingerprint_props(func_node))
        props.update(anchor_hash_props(func_node, decorators))
        self._deferred_methods.append(
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
                signature=cpp_overload_signature(func_node),
            )
        )

    def resolve_deferred_methods(self) -> int:
        """Ingest deferred out-of-class C++ methods now that all classes are known.

        Called after all files have been parsed so that every Class node
        is guaranteed to be in the registry.  Returns the number of
        methods that were ingested.
        """
        deferred = self._deferred_methods
        if not deferred:
            return 0

        ingested = 0
        for entry in deferred:
            class_qn, resolved = self._resolve_deferred_class(entry)
            method_qn = f"{class_qn}.{entry.method_name}"
            if entry.signature is not None:
                method_qn = self.function_registry.register_overload_qn(
                    method_qn, entry.signature, entry.start_line, entry.start_col
                )
            # Record the binding so Pass-3 call attribution reuses this exact
            # decision rather than re-resolve and diverge.
            self.facts.cpp_out_of_class_methods[(entry.module_qn, entry.start_line)] = (
                method_qn,
                class_qn,
            )
            self.facts.function_locations[
                (entry.module_qn, entry.start_line, entry.start_col)
            ] = FunctionLocation(
                label=cs.NodeLabel.METHOD.value,
                qualified_name=method_qn,
                container_qn=class_qn,
            )

            props = dict(entry.method_props)
            props[cs.KEY_QUALIFIED_NAME] = method_qn
            if entry.signature is not None:
                props[cs.KEY_SIGNATURE] = entry.signature.text
            if isinstance(path := props.get(cs.KEY_PATH), str):
                self.cpp_definition_spans.setdefault(path, []).append(
                    CppDefinitionSpan(
                        entry.start_line,
                        entry.end_line,
                        cs.NodeLabel.METHOD.value,
                        method_qn,
                    )
                )

            logger.debug(ls.METHOD_FOUND.format(name=entry.method_name, qn=method_qn))
            self.ingestor.ensure_node_batch(cs.NodeLabel.METHOD, props)
            emit_endpoints(
                self.ingestor,
                cs.NodeLabel.METHOD,
                method_qn,
                props.get(cs.KEY_DECORATORS),
            )
            self.function_registry[method_qn] = cs.NodeLabel.METHOD
            self.simple_name_lookup[entry.method_name].add(method_qn)
            if entry.return_type:
                self.facts.method_return_types[method_qn] = entry.return_type

            if resolved or class_qn in self.function_registry:
                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_qn),
                    cs.RelationshipType.DEFINES_METHOD,
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
                )
            else:
                # The class never resolved (not parsed, or its declaration is
                # macro-corrupted); a DEFINES_METHOD to the phantom fallback qn
                # would be dropped and orphan the method, so anchor it to its
                # module instead.
                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, entry.module_qn),
                    cs.RelationshipType.DEFINES,
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
                )
            ingested += 1

        self._deferred_methods = []
        return ingested

    def _resolve_deferred_class(self, entry: _DeferredMethod) -> tuple[str, bool]:
        # Scope-first: the namespace-qualified name distinguishes same-leaf
        # classes (ast::Type vs ast::analysis::Type); the raw written qualifier
        # is the fallback for other scopes.
        candidates = [entry.class_name]
        if entry.namespace_path:
            candidates.insert(
                0, f"{entry.namespace_path}{cs.SEPARATOR_DOT}{entry.class_name}"
            )
        for candidate in candidates:
            real_class_qn, resolved = self.resolve_class_qn(candidate, "")
            if resolved:
                return real_class_qn, True
        return entry.fallback_class_qn, False

    def resolve_deferred_artifacts(self) -> int:
        """Decide held-back artifact-shaped nodes now every class is known.

        A registered class bearing the node's name reattaches it as that
        class's ctor METHOD (a recovery-orphaned constructor); otherwise a
        named parameter keeps it as a module Function (a real definition whose
        type recovery destroyed); otherwise it is dropped as a macro
        invocation. Returns the number registered.
        """
        deferred = self._deferred_artifacts
        registered = sum(1 for entry in deferred if self._resolve_one_artifact(entry))
        deferred.clear()
        return registered

    def resolve_deferred_prototypes(self) -> int:
        """Register held-back free-function prototypes without a definition.

        A prototype whose namespace-qualified name matches a registered
        FUNCTION in any module duplicates that bodied definition (methods,
        classes and other prototypes never match: prototypes are all still
        held here, and the type filter excludes non-Function nodes); it is
        dropped so calls and dead-code see one node per function. Returns
        the number registered.
        """
        deferred = self._deferred_prototypes
        registered = 0
        for entry in deferred:
            file_path = self.module_qn_to_file_path.get(entry.module_qn)
            resolution = self._host._resolve_function_identity(
                entry.func_node,
                entry.module_qn,
                cs.SupportedLanguage.CPP,
                entry.lang_config,
                file_path,
            )
            if not resolution:
                continue
            prefix = f"{entry.module_qn}{cs.SEPARATOR_DOT}"
            qn = resolution.qualified_name
            ns_qn = qn[len(prefix) :] if qn.startswith(prefix) else qn
            simple = ns_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            suffix = f"{cs.SEPARATOR_DOT}{ns_qn}"
            if any(
                candidate.endswith(suffix)
                and self.function_registry.get(candidate) == cs.NodeLabel.FUNCTION
                for candidate in self.function_registry.find_ending_with(simple)
            ):
                continue
            self._host._register_function(
                entry.func_node,
                resolution,
                entry.module_qn,
                cs.SupportedLanguage.CPP,
                entry.lang_config,
                entry.lang_queries,
            )
            if return_type := cpp_utils.extract_return_type_name(entry.func_node):
                self.facts.method_return_types[resolution.qualified_name] = return_type
            registered += 1
        deferred.clear()
        return registered

    def _resolve_one_artifact(self, entry: _DeferredArtifact) -> bool:
        name = cpp_utils.extract_function_name(entry.func_node)
        if not name:
            return False
        class_qn = self._lookup_artifact_class(entry, name)
        file_path = self.module_qn_to_file_path.get(entry.module_qn)
        if class_qn is not None:
            return self._reattach_orphan_ctor(entry, class_qn, file_path)
        if not cpp_utils.has_named_parameter(entry.func_node):
            return False
        resolution = self._host._resolve_function_identity(
            entry.func_node,
            entry.module_qn,
            cs.SupportedLanguage.CPP,
            entry.lang_config,
            file_path,
        )
        if not resolution:
            return False
        self._host._register_function(
            entry.func_node,
            resolution,
            entry.module_qn,
            cs.SupportedLanguage.CPP,
            entry.lang_config,
            entry.lang_queries,
        )
        return True

    def _lookup_artifact_class(self, entry: _DeferredArtifact, name: str) -> str | None:
        # Scope-first, like resolve_deferred_methods: the namespace-qualified
        # candidate disambiguates same-leaf classes.
        candidates = [name]
        if namespace_path := cs.SEPARATOR_DOT.join(
            cpp_utils.extract_namespace_path(entry.func_node)
        ):
            candidates.insert(0, f"{namespace_path}{cs.SEPARATOR_DOT}{name}")
        for candidate in candidates:
            class_qn, resolved = self.resolve_class_qn(candidate, entry.module_qn)
            if resolved:
                return class_qn
        return None

    def _reattach_orphan_ctor(
        self,
        entry: _DeferredArtifact,
        class_qn: str,
        file_path: Path | None,
    ) -> bool:
        # Mirror of the resolved branch of _handle_out_of_class_method: the
        # recorded location and span let Pass-3 attribute the ctor body's calls to
        # the METHOD qn instead of re-deciding into the artifact-skip.
        method_qn = ingest_method(
            self._method_host,
            entry.func_node,
            class_qn,
            cs.NodeLabel.CLASS,
            cs.SupportedLanguage.CPP,
            lang_queries=entry.lang_queries,
            file_path=file_path,
            module_qn=entry.module_qn,
            skip_cpp_artifact_check=True,
        )
        if method_qn is None:
            return False
        self.facts.cpp_out_of_class_methods[
            (entry.module_qn, entry.func_node.start_point[0] + 1)
        ] = (method_qn, class_qn)
        self.facts.function_locations[
            function_span_key(entry.module_qn, entry.func_node)
        ] = FunctionLocation(
            label=cs.NodeLabel.METHOD.value,
            qualified_name=method_qn,
            container_qn=class_qn,
        )
        record_cpp_definition_span(
            self.cpp_definition_spans,
            cs.SupportedLanguage.CPP,
            file_path,
            self.repo_path,
            entry.func_node,
            cs.NodeLabel.METHOD.value,
            method_qn,
        )
        return True

    def record_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        lang_config: LanguageSpec,
        location: FunctionLocation,
    ) -> None:
        # A template wrapper's body walk in Pass 3 visits the INNER definition (it
        # starts on its own line), so record the entry under the child's span too
        # (the walk matches on that node).
        if func_node.type != cs.CppNodeType.TEMPLATE_DECLARATION:
            return
        for child in func_node.children:
            if child.type == cs.CppNodeType.FUNCTION_DEFINITION:
                self.facts.function_locations[function_span_key(module_qn, child)] = (
                    location
                )
                break

    def _find_enclosing_function_node(
        self, func_node: Node, lang_config: LanguageSpec
    ) -> Node | None:
        # The first function-like ancestor, stopping at the module boundary.
        # The C++ spec counts a `declaration` as both (a prototype, a
        # file-scope variable), but one above a lambda is the variable its
        # initializer binds (`auto inner = [] {...};`): the lambda belongs to
        # whatever encloses that declaration, so the walk passes it.
        current = func_node.parent
        while current is not None:
            if current.type != cs.TS_CPP_DECLARATION:
                if current.type in lang_config.module_node_types:
                    return None
                if current.type in lang_config.function_node_types:
                    return current
            current = current.parent
        return None

    def defers_containment(
        self,
        func_node: Node,
        child_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
    ) -> bool:
        # A function nested in an out-of-class C++ method body (a lambda
        # passed as a call argument) cannot bind its parent yet: the method's
        # final qn is class-anchored and the class may live in a header not
        # parsed until later, so the walk would emit a phantom free-fn parent
        # that the database drops, orphaning the lambda (issue #650).
        enclosing = self._find_enclosing_function_node(func_node, lang_config)
        if enclosing is None or not cpp_utils.is_out_of_class_method_definition(
            enclosing
        ):
            return False
        class_name = cpp_utils.extract_class_name_from_out_of_class_method(enclosing)
        method_name = cpp_utils.extract_function_name(enclosing)
        if not class_name or not method_name:
            return False
        # Keep the FULL written qualifier (ns::Widget): resolve_class_qn splits
        # the leaf itself and its endswith guard needs the qualifier to tell
        # same-leaf classes in different namespaces apart.
        self._deferred_containment.append(
            _DeferredContainment(
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

    def extra_relationships(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        parent_label: str,
        parent_qn: str,
    ) -> None:
        if resolution.is_exported:
            self.ingestor.ensure_relationship_batch(
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                cs.RelationshipType.EXPORTS,
                (
                    cs.NodeLabel.FUNCTION,
                    cs.KEY_QUALIFIED_NAME,
                    resolution.qualified_name,
                ),
            )

    def resolve_deferred_containment(self) -> int:
        """Emit DEFINES for functions nested in out-of-class C++ method bodies.

        Runs after resolve_deferred_methods so the parent method nodes
        exist under their final class-anchored qns. Falls back to the file
        module rather than ever emitting a phantom parent.
        """
        deferred = self._deferred_containment
        if not deferred:
            return 0
        emitted = 0
        for entry in deferred:
            # Try the namespace-scoped name first (alpha.Widget beats a same-leaf
            # beta.Widget via the endswith guard), then the raw written qualifier
            # for classes matched through other scopes.
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
                class_qn, resolved = self.resolve_class_qn(candidate, "")
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
        self._deferred_containment = []
        return emitted
