from __future__ import annotations

from abc import abstractmethod
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from tree_sitter import Node, QueryCursor

from ... import constants as cs
from ... import logs
from ...config import settings
from ...language_spec import LanguageSpec
from ...types_defs import ASTNode, PropertyDict
from ...utils.path_utils import cached_relative_path, cached_resolve_posix
from ..java import utils as java_utils
from ..py import resolve_class_name
from ..rs import utils as rs_utils
from ..utils import ingest_method, safe_decode_text, sorted_captures
from . import cpp_modules
from . import identity as id_
from . import method_override as mo
from . import node_type as nt
from . import relationships as rel

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import (
        FunctionRegistryTrieProtocol,
        LanguageQueries,
        SimpleNameLookup,
    )
    from ..import_processor import ImportProcessor


def _is_nested_inside_function(
    node: Node, class_body: Node, lang_config: LanguageSpec
) -> bool:
    current = node.parent
    while current and current is not class_body:
        if (
            current.type in lang_config.function_node_types
            and current.child_by_field_name(cs.FIELD_BODY) is not None
        ):
            return True
        current = current.parent
    return False


def _method_belongs_directly(
    method_node: Node, class_node: Node, lang_config: LanguageSpec
) -> bool:
    current = method_node.parent
    while current is not None:
        if current == class_node:
            return True
        if current.type in lang_config.class_node_types or (
            current.type in lang_config.function_node_types
            and current.child_by_field_name(cs.FIELD_BODY) is not None
        ):
            return False
        current = current.parent
    return False


def _skip_method(
    method_node: Node, class_node: Node, class_body: Node, lang_config: LanguageSpec
) -> bool:
    if settings.CAPTURE_FUNCTION_LOCAL_DEFINITIONS:
        return not _method_belongs_directly(method_node, class_node, lang_config)
    return _is_nested_inside_function(method_node, class_body, lang_config)


class _DeferredForwardDecl(NamedTuple):
    # (H) A C/C++ forward declaration held back until every file's real definitions
    # (H) are registered, so we can tell an only-forward-declared type (keep it) from
    # (H) one that also has a bodied definition elsewhere (drop the phantom).
    class_node: Node
    class_name: str
    # (H) The namespace-qualified name (module-file prefix stripped, so `A::Foo` is
    # (H) `A.Foo` regardless of which header declares it). Comparing on this — not the
    # (H) bare simple name — keeps a forward-declared `B::Foo` when only `A::Foo` is
    # (H) defined, while still matching a cross-file forward/definition of one type.
    ns_qn: str
    module_qn: str
    language: cs.SupportedLanguage
    lang_queries: LanguageQueries
    lang_config: LanguageSpec
    file_path: Path | None
    sorted_func_nodes: list[Node] | None
    func_node_starts: list[int] | None


class ClassIngestMixin:
    __slots__ = ()
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    module_qn_to_file_path: dict[str, Path]
    import_processor: ImportProcessor
    class_inheritance: dict[str, list[str]]
    _deferred_forward_decls: list[_DeferredForwardDecl]
    _defined_namespace_qns: set[str]

    def _namespace_qn(self, class_qn: str, module_qn: str) -> str:
        # (H) Strip the module-file prefix so two nodes for the same C++ type in
        # (H) different headers share one key (`leveldb.db.x.h.leveldb.VersionSet` and
        # (H) `...y.h.leveldb.VersionSet` both -> `leveldb.VersionSet`), while types in
        # (H) different namespaces stay distinct.
        prefix = f"{module_qn}{cs.SEPARATOR_DOT}"
        return class_qn[len(prefix) :] if class_qn.startswith(prefix) else class_qn

    @abstractmethod
    def _get_docstring(self, node: ASTNode) -> str | None: ...

    @abstractmethod
    def _extract_decorators(self, node: ASTNode) -> list[str]: ...

    @abstractmethod
    def _determine_function_parent(
        self,
        func_node: Node,
        func_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str]: ...

    def _resolve_to_qn(self, name: str, module_qn: str) -> str:
        return self._resolve_class_name(name, module_qn) or f"{module_qn}.{name}"

    def _ingest_cpp_module_declarations(
        self,
        root_node: Node,
        module_qn: str,
        file_path: Path,
    ) -> None:
        cpp_modules.ingest_cpp_module_declarations(
            root_node,
            module_qn,
            file_path,
            self.repo_path,
            self.project_name,
            self.ingestor,
        )

    def _find_cpp_exported_classes(self, root_node: Node) -> list[Node]:
        return cpp_modules.find_cpp_exported_classes(root_node)

    def _ingest_classes_and_methods(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list] | None = None,
    ) -> None:
        lang_queries = queries[language]
        lang_config: LanguageSpec = lang_queries[cs.QUERY_CONFIG]

        if combined_captures is not None:
            class_nodes = list(combined_captures.get(cs.CAPTURE_CLASS, []))
            module_nodes = combined_captures.get(cs.ONEOF_MODULE, [])
        else:
            if not (query := lang_queries[cs.QUERY_CLASSES]):
                return
            cursor = QueryCursor(query)
            captures = sorted_captures(cursor, root_node)
            class_nodes = captures.get(cs.CAPTURE_CLASS, [])
            module_nodes = captures.get(cs.ONEOF_MODULE, [])

        if language == cs.SupportedLanguage.CPP:
            class_nodes.extend(self._find_cpp_exported_classes(root_node))

        file_path = self.module_qn_to_file_path.get(module_qn)

        sorted_func_nodes: list[Node] | None = None
        func_node_starts: list[int] | None = None
        if combined_captures is not None and cs.CAPTURE_FUNCTION in combined_captures:
            sorted_func_nodes = combined_captures[cs.CAPTURE_FUNCTION]
            func_node_starts = [n.start_byte for n in sorted_func_nodes]

        for class_node in class_nodes:
            self._process_class_node(
                class_node,
                module_qn,
                language,
                lang_queries,
                lang_config,
                file_path,
                sorted_func_nodes=sorted_func_nodes,
                func_node_starts=func_node_starts,
            )

        self._process_inline_modules(module_nodes, module_qn, lang_config)

    def resolve_deferred_forward_declarations(self) -> int:
        # (H) Run after every file's definitions are registered. A deferred forward
        # (H) declaration whose class name already produced a real node is a phantom
        # (H) (the bodied definition exists) -> drop it. Otherwise it is the only
        # (H) representation of the type -> register it now. Deterministic: the
        # (H) deferred list is in file (sorted) order, and the first surviving forward
        # (H) declaration of an only-declared type claims the name for the rest.
        deferred = getattr(self, "_deferred_forward_decls", None)
        if not deferred:
            return 0
        self._deferred_forward_decls = []
        registered = 0
        for entry in deferred:
            # (H) Drop the forward declaration only when a real definition of the SAME
            # (H) namespace-qualified type exists (not merely the same simple name in
            # (H) another namespace). Otherwise it is the type's only node -> keep it.
            if entry.ns_qn in self._defined_namespace_qns:
                continue
            self._process_class_node(
                entry.class_node,
                entry.module_qn,
                entry.language,
                entry.lang_queries,
                entry.lang_config,
                entry.file_path,
                sorted_func_nodes=entry.sorted_func_nodes,
                func_node_starts=entry.func_node_starts,
                allow_defer=False,
            )
            registered += 1
        return registered

    def _process_class_node(
        self,
        class_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        lang_config: LanguageSpec,
        file_path: Path | None,
        sorted_func_nodes: list[Node] | None = None,
        func_node_starts: list[int] | None = None,
        allow_defer: bool = True,
    ) -> None:
        if language == cs.SupportedLanguage.RUST and class_node.type == cs.TS_IMPL_ITEM:
            self._ingest_rust_impl_methods(
                class_node,
                module_qn,
                language,
                lang_queries,
                sorted_func_nodes=sorted_func_nodes,
                func_node_starts=func_node_starts,
            )
            return

        # (H) A C/C++ forward declaration (`class Widget;`, or `template <T> class
        # (H) Widget;`) is a bodyless type specifier. Registering it collides with the
        # (H) real definition's qn (suffixing it `@line`) and fragments one class into
        # (H) several same-named nodes, which makes member-call resolution pick among
        # (H) duplicates nondeterministically. But a type that is ONLY ever
        # (H) forward-declared (an opaque handle, or a metaprogramming primary defined
        # (H) solely via specializations) has no other node, so it must be kept. We
        # (H) cannot tell which until every file's definitions are in, so defer the
        # (H) forward declaration and decide in resolve_deferred_forward_declarations.
        type_spec = class_node
        if class_node.type == cs.CppNodeType.TEMPLATE_DECLARATION:
            type_spec = next(
                (
                    child
                    for child in class_node.children
                    if child.type in cs.CPP_TYPE_SPECIFIER_NODE_TYPES
                ),
                None,
            )
        if (
            type_spec is not None
            and type_spec.type in cs.CPP_TYPE_SPECIFIER_NODE_TYPES
            and type_spec.child_by_field_name(cs.FIELD_BODY) is None
        ):
            # (H) The inner bodyless specifier of a template forward decl is redundant
            # (H) with its template_declaration wrapper (the canonical template node),
            # (H) which is deferred separately; drop the inner one outright.
            if (
                class_node.type in cs.CPP_TYPE_SPECIFIER_NODE_TYPES
                and class_node.parent is not None
                and class_node.parent.type == cs.CppNodeType.TEMPLATE_DECLARATION
            ):
                return
            if allow_defer:
                deferred_identity = id_.resolve_class_identity(
                    class_node, module_qn, language, lang_config, file_path
                )
                if deferred_identity:
                    self._deferred_forward_decls.append(
                        _DeferredForwardDecl(
                            class_node,
                            deferred_identity[1],
                            self._namespace_qn(deferred_identity[0], module_qn),
                            module_qn,
                            language,
                            lang_queries,
                            lang_config,
                            file_path,
                            sorted_func_nodes,
                            func_node_starts,
                        )
                    )
                return

        identity = id_.resolve_class_identity(
            class_node,
            module_qn,
            language,
            lang_config,
            file_path,
        )
        if not identity:
            return

        class_qn, class_name, is_exported = identity
        # (H) Record this real (bodied) definition's namespace-qualified name so a
        # (H) deferred forward declaration of the same type is recognized as a phantom
        # (H) and dropped. Uses the pre-suffix identity qn so both sides share a key.
        if class_node.type in cs.CPP_TYPE_SPECIFIER_NODE_TYPES or (
            class_node.type == cs.CppNodeType.TEMPLATE_DECLARATION
        ):
            self._defined_namespace_qns.add(self._namespace_qn(class_qn, module_qn))
        class_qn = self.function_registry.register_unique_qn(
            class_qn, class_node.start_point[0] + 1
        )
        node_type = nt.determine_node_type(class_node, class_name, class_qn, language)

        class_props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: class_qn,
            cs.KEY_NAME: class_name,
            cs.KEY_DECORATORS: self._extract_decorators(class_node),
            cs.KEY_START_LINE: class_node.start_point[0] + 1,
            cs.KEY_END_LINE: class_node.end_point[0] + 1,
            cs.KEY_DOCSTRING: self._get_docstring(class_node),
            cs.KEY_IS_EXPORTED: is_exported,
        }
        if file_path is not None:
            class_props[cs.KEY_PATH] = cached_relative_path(
                file_path, self.repo_path
            ).as_posix()
            class_props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
        self.ingestor.ensure_node_batch(node_type, class_props)
        self.function_registry[class_qn] = node_type
        if class_name:
            self.simple_name_lookup[class_name].add(class_qn)

        parent_label, parent_qn = self._determine_function_parent(
            class_node, class_qn, module_qn, lang_config, language
        )
        rel.create_class_relationships(
            class_node,
            class_qn,
            module_qn,
            parent_label,
            parent_qn,
            node_type,
            is_exported,
            language,
            self.class_inheritance,
            self.ingestor,
            self.import_processor,
            self._resolve_to_qn,
            self.function_registry,
        )
        self._ingest_class_methods(
            class_node,
            class_qn,
            language,
            lang_queries,
            file_path,
            sorted_func_nodes=sorted_func_nodes,
            func_node_starts=func_node_starts,
        )

    def _ingest_rust_impl_methods(
        self,
        class_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        sorted_func_nodes: list[Node] | None = None,
        func_node_starts: list[int] | None = None,
    ) -> None:
        if not (impl_target := rs_utils.extract_impl_target(class_node)):
            return

        # (H) An impl block inside `mod inner` targets a type whose node lives
        # (H) under the module path (proj...inner.Widget). Resolve the impl target
        # (H) against its enclosing module so the method binds to the real type
        # (H) node instead of a phantom under the file module.
        mod_parts = rs_utils.build_module_path(class_node)
        owner_module_qn = (
            f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(mod_parts)}"
            if mod_parts
            else module_qn
        )
        class_qn = f"{owner_module_qn}.{impl_target}"

        # (H) `impl Trait for Type` means Type IMPLEMENTS Trait. The target type's
        # (H) node label may be Class/Enum/Type, so match the relationship source
        # (H) to its registered label (else the IMPLEMENTS edge never resolves).
        if trait_name := rs_utils.extract_impl_trait(class_node):
            owner_type = self.function_registry.get(class_qn)
            owner_label = (
                cs.NodeLabel(owner_type.value)
                if owner_type is not None
                else cs.NodeLabel.CLASS
            )
            self.ingestor.ensure_relationship_batch(
                (owner_label, cs.KEY_QUALIFIED_NAME, class_qn),
                cs.RelationshipType.IMPLEMENTS,
                (
                    cs.NodeLabel.INTERFACE,
                    cs.KEY_QUALIFIED_NAME,
                    self._resolve_to_qn(trait_name, owner_module_qn),
                ),
            )

        body_node = class_node.child_by_field_name("body")

        if not body_node:
            return

        file_path = self.module_qn_to_file_path.get(module_qn)
        lang_config: LanguageSpec = lang_queries[cs.QUERY_CONFIG]

        if sorted_func_nodes is not None and func_node_starts is not None:
            body_start = body_node.start_byte
            body_end = body_node.end_byte
            lo = bisect_left(func_node_starts, body_start)
            hi = bisect_right(func_node_starts, body_end)
            method_nodes = [
                n for n in sorted_func_nodes[lo:hi] if n.end_byte <= body_end
            ]
        else:
            method_query = lang_queries[cs.QUERY_FUNCTIONS]
            if not method_query:
                return
            method_cursor = QueryCursor(method_query)
            method_captures = sorted_captures(method_cursor, body_node)
            method_nodes = method_captures.get(cs.CAPTURE_FUNCTION, [])

        for method_node in method_nodes:
            if _skip_method(method_node, class_node, body_node, lang_config):
                continue
            ingest_method(
                method_node,
                class_qn,
                cs.NodeLabel.CLASS,
                self.ingestor,
                self.function_registry,
                self.simple_name_lookup,
                self._get_docstring,
                language,
                file_path=file_path,
                repo_path=self.repo_path,
            )

    def _ingest_class_methods(
        self,
        class_node: Node,
        class_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        file_path: Path | None = None,
        sorted_func_nodes: list[Node] | None = None,
        func_node_starts: list[int] | None = None,
    ) -> None:
        body_node = class_node.child_by_field_name("body")
        if not body_node:
            return

        lang_config: LanguageSpec = lang_queries[cs.QUERY_CONFIG]

        if sorted_func_nodes is not None and func_node_starts is not None:
            body_start = body_node.start_byte
            body_end = body_node.end_byte
            lo = bisect_left(func_node_starts, body_start)
            hi = bisect_right(func_node_starts, body_end)
            method_nodes = [
                n for n in sorted_func_nodes[lo:hi] if n.end_byte <= body_end
            ]
        else:
            method_query = lang_queries[cs.QUERY_FUNCTIONS]
            if not method_query:
                return
            method_cursor = QueryCursor(method_query)
            method_captures = sorted_captures(method_cursor, body_node)
            method_nodes = method_captures.get(cs.CAPTURE_FUNCTION, [])

        for method_node in method_nodes:
            if _skip_method(method_node, class_node, body_node, lang_config):
                continue

            method_qualified_name = None
            if language == cs.SupportedLanguage.JAVA:
                method_info = java_utils.extract_method_info(method_node)
                if method_name := method_info.get(cs.KEY_NAME):
                    parameters = method_info.get(cs.KEY_PARAMETERS, [])
                    param_sig = (
                        f"({','.join(parameters)})" if parameters else cs.EMPTY_PARENS
                    )
                    method_qualified_name = f"{class_qn}.{method_name}{param_sig}"

            ingest_method(
                method_node,
                class_qn,
                cs.NodeLabel.CLASS,
                self.ingestor,
                self.function_registry,
                self.simple_name_lookup,
                self._get_docstring,
                language,
                self._extract_decorators,
                method_qualified_name,
                file_path=file_path,
                repo_path=self.repo_path,
            )

    def _process_inline_modules(
        self,
        module_nodes: list[Node],
        module_qn: str,
        lang_config: LanguageSpec,
    ) -> None:
        for module_node in module_nodes:
            if not isinstance(module_node, Node):
                continue
            if not (module_name_node := module_node.child_by_field_name("name")):
                continue
            if not module_name_node.text:
                continue

            # (H) A bodyless `mod foo;` only declares that the file module foo.rs
            # (H) belongs here; foo.rs already yields its own real-path Module node
            # (H) with the same qn. Emitting a second synthetic-path node collides
            # (H) on that qn and clobbers the file's real path, so skip it.
            if module_node.child_by_field_name(cs.FIELD_BODY) is None:
                continue

            module_name = safe_decode_text(module_name_node)
            nested_qn = id_.build_nested_qualified_name_for_class(
                module_node, module_qn, module_name or "", lang_config
            )
            inline_module_qn = nested_qn or f"{module_qn}.{module_name}"

            module_props: PropertyDict = {
                cs.KEY_QUALIFIED_NAME: inline_module_qn,
                cs.KEY_NAME: module_name,
                cs.KEY_PATH: f"{cs.INLINE_MODULE_PATH_PREFIX}{module_name}",
                cs.KEY_START_LINE: module_node.start_point[0] + 1,
                cs.KEY_END_LINE: module_node.end_point[0] + 1,
            }
            # (H) A bodied inline module is physically located in this file; give
            # (H) it the real path so it joins containment on (file, line).
            file_path = self.module_qn_to_file_path.get(module_qn)
            if file_path is not None:
                module_props[cs.KEY_PATH] = cached_relative_path(
                    file_path, self.repo_path
                ).as_posix()
                module_props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
            logger.info(
                logs.CLASS_FOUND_INLINE_MODULE.format(
                    name=module_name, qn=inline_module_qn
                )
            )
            self.ingestor.ensure_node_batch(cs.NodeLabel.MODULE, module_props)

            # (H) Link the inline module into the containment tree: its enclosing
            # (H) module (file module, or an outer mod) DEFINES it. Without this the
            # (H) inline Module node is an orphan defining nothing.
            parent_module_qn = inline_module_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if parent_module_qn and parent_module_qn != inline_module_qn:
                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, parent_module_qn),
                    cs.RelationshipType.DEFINES,
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, inline_module_qn),
                )

    def process_all_method_overrides(self) -> None:
        mo.process_all_method_overrides(
            self.function_registry,
            self.class_inheritance,
            self.ingestor,
        )

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )

    def _extract_cpp_base_class_name(self, parent_text: str) -> str:
        from . import parent_extraction as pe

        return pe.extract_cpp_base_class_name(parent_text)

    def _get_node_type_for_inheritance(self, qualified_name: str) -> str:
        return rel.get_node_type_for_inheritance(qualified_name, self.function_registry)
