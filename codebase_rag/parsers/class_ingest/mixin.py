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
from ...types_defs import (
    ASTNode,
    CppDefinitionSpan,
    DeferredCppInherit,
    DeferredInherit,
    FunctionLocation,
    FunctionSpanKey,
    NodeType,
    PropertyDict,
)
from ...utils.path_utils import cached_relative_path, cached_resolve_posix
from ..cpp import CppTypeInferenceEngine
from ..cpp import utils as cpp_utils
from ..csharp import utils as csharp_utils
from ..go import GoTypeInferenceEngine
from ..java import utils as java_utils
from ..py import external_stdlib_base_method_names, resolve_class_name
from ..rs import RustTypeInferenceEngine
from ..rs import utils as rs_utils
from ..utils import (
    extract_modifiers_and_decorators,
    function_span_key,
    ingest_method,
    record_cpp_definition_span,
    safe_decode_text,
    sorted_captures,
)
from . import cpp_modules
from . import identity as id_
from . import method_override as mo
from . import node_type as nt
from . import relationships as rel
from .utils import csharp_has_override_modifier

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import (
        DeferredParentLink,
        FunctionRegistryTrieProtocol,
        LanguageQueries,
        SimpleNameLookup,
    )
    from ..import_processor import ImportProcessor


def _java_anonymous_base_type(method_node: Node, class_node: Node) -> str | None:
    # (H) If `method_node` sits inside an anonymous class body between it and
    # (H) `class_node` (`new Base(){ ... m() ... }`), return the anon class's base type
    # (H) name (the object_creation's `type` field, generic args stripped). None when the
    # (H) method belongs directly to the enclosing class, not an anonymous subclass.
    current = method_node.parent
    while current is not None and current is not class_node:
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
    class_field_types: dict[str, dict[str, str]]
    java_anon_overrides: list[tuple[str, str, str, str]]
    csharp_methods: set[str]
    csharp_override_methods: set[str]
    csharp_extension_methods: dict[str, list[tuple[str, str]]]
    class_field_guard_inner: dict[str, dict[str, str]]
    method_return_types: dict[str, str]
    interface_implementers: dict[str, set[str]]
    function_locations: dict[FunctionSpanKey, FunctionLocation]
    cpp_definition_spans: dict[str, list[CppDefinitionSpan]]
    _deferred_forward_decls: list[_DeferredForwardDecl]
    _deferred_parent_links: list[DeferredParentLink]
    _deferred_cpp_inherits: list[DeferredCppInherit]
    _deferred_inherits: list[DeferredInherit]
    cpp_module_interfaces: set[str]
    _deferred_cpp_module_impls: list[tuple[str, str]]
    declared_module_qns: set[str]

    def _namespace_qn(self, class_qn: str, module_qn: str) -> str:
        # (H) Strip the module-file prefix so two nodes for the same C++ type in
        # (H) different headers share one key (`leveldb.db.x.h.leveldb.VersionSet` and
        # (H) `...y.h.leveldb.VersionSet` both -> `leveldb.VersionSet`), while types in
        # (H) different namespaces stay distinct.
        prefix = f"{module_qn}{cs.SEPARATOR_DOT}"
        return class_qn[len(prefix) :] if class_qn.startswith(prefix) else class_qn

    def _namespace_qn_has_definition(self, ns_qn: str) -> bool:
        # (H) A real definition of this namespace-qualified type is registered iff some
        # (H) class qn ends with it (`....leveldb.VersionSet`). find_ending_with is
        # (H) indexed by simple name, and because it is queried AFTER the registry is
        # (H) rehydrated from the graph, it also covers definitions in files an
        # (H) incremental run did not re-parse (issue: a forward decl must still drop
        # (H) when its definition lives in an unchanged file).
        simple = ns_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        suffix = f"{cs.SEPARATOR_DOT}{ns_qn}"
        return any(
            qn.endswith(suffix)
            for qn in self.function_registry.find_ending_with(simple)
        )

    @abstractmethod
    def _get_docstring(self, node: ASTNode) -> str | None: ...

    @abstractmethod
    def _extract_decorators(self, node: ASTNode) -> list[str]: ...

    @abstractmethod
    def _emit_or_defer_defines(
        self,
        parent_label: str,
        parent_qn: str,
        child_label: str,
        child_qn: str,
        module_qn: str,
    ) -> None: ...

    @abstractmethod
    def _determine_function_parent(
        self,
        func_node: Node,
        func_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str]: ...

    @abstractmethod
    def _resolve_cpp_class_qn(
        self, class_name: str, module_qn: str, exclude_qn: str | None = None
    ) -> tuple[str, bool]: ...

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
            self.cpp_module_interfaces,
            self._deferred_cpp_module_impls,
        )

    def resolve_deferred_cpp_module_impls(self) -> int:
        """Emit ModuleImplementation IMPLEMENTS edges for interfaces that exist.

        An implementation unit (`module X;`) whose interface (`export module
        X;`) lives in no parsed file has nothing real to point at; emitting
        the edge anyway would mint a phantom the database drops.
        """
        deferred = self._deferred_cpp_module_impls
        if not deferred:
            return 0
        self._deferred_cpp_module_impls = []
        emitted = 0
        for impl_qn, interface_qn in deferred:
            if interface_qn not in self.cpp_module_interfaces:
                continue
            self.ingestor.ensure_relationship_batch(
                (cs.NodeLabel.MODULE_IMPLEMENTATION, cs.KEY_QUALIFIED_NAME, impl_qn),
                cs.RelationshipType.IMPLEMENTS,
                (cs.NodeLabel.MODULE_INTERFACE, cs.KEY_QUALIFIED_NAME, interface_qn),
            )
            emitted += 1
        return emitted

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
            if self._namespace_qn_has_definition(entry.ns_qn):
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

    def resolve_deferred_cpp_inherits(self) -> int:
        """Emit C++ INHERITS edges now that every class is registered.

        A base written in another header resolves scope-first across all
        parsed files (the same lookup out-of-class methods use); a base that
        resolves nowhere emits no edge, because the module-anchored guess is a
        phantom endpoint the database silently drops anyway. Resolved qns
        replace the guesses in class_inheritance in place so Pass-3 method
        resolution and override detection walk the real hierarchy.
        """
        deferred = self._deferred_cpp_inherits
        if not deferred:
            return 0
        self._deferred_cpp_inherits = []
        emitted = 0
        for entry in deferred:
            parent_qn = self._resolve_cpp_base_qn(entry)
            if parent_qn is None:
                continue
            bases = self.class_inheritance.get(entry.child_qn)
            if bases is not None and entry.base_index < len(bases):
                bases[entry.base_index] = parent_qn
            rel.create_inheritance_relationship(
                entry.child_label,
                entry.child_qn,
                parent_qn,
                self.function_registry,
                self.ingestor,
                entry.base_index,
            )
            emitted += 1
        return emitted

    def _resolve_cpp_base_qn(self, entry: DeferredCppInherit) -> str | None:
        normalized = entry.base_name.replace(
            cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT
        )
        # (H) Scope-first (see resolve_deferred_cpp_methods): the enclosing
        # (H) namespaces distinguish same-leaf classes. The child itself is
        # (H) excluded so `class Type : public other::Type` never self-inherits.
        candidates = [normalized]
        if entry.namespace_path:
            candidates.insert(
                0, f"{entry.namespace_path}{cs.SEPARATOR_DOT}{normalized}"
            )
        for candidate in candidates:
            parent_qn, resolved = self._resolve_cpp_class_qn(
                candidate, "", exclude_qn=entry.child_qn
            )
            if resolved:
                return parent_qn
        # (H) The guess strips a qualified base (`other::Type`) to its leaf, so
        # (H) it can collide with the CHILD's own qn when the base is
        # (H) unresolvable; a self-INHERITS is never real.
        if (
            entry.guess_qn != entry.child_qn
            and self.function_registry.get(entry.guess_qn) is not None
        ):
            return entry.guess_qn
        return None

    def resolve_deferred_inherits(self) -> int:
        """Emit non-C++ INHERITS/IMPLEMENTS edges now that every class is registered.

        A parent in a file parsed later resolves against the full registry; a
        parent that resolves nowhere (java.lang.Exception, Rust Send/Sync)
        emits no edge, because the module-anchored guess is a phantom endpoint
        the database silently drops anyway. Resolved base qns replace the
        guesses in class_inheritance in place so Pass-3 method resolution and
        override detection walk the real hierarchy.
        """
        deferred = self._deferred_inherits
        if not deferred:
            return 0
        self._deferred_inherits = []
        emitted = 0
        for entry in deferred:
            child_type = self.function_registry.get(entry.child_qn)
            if child_type is None:
                continue
            resolved = self._resolve_deferred_parent_qn(entry)
            if resolved is None:
                continue
            parent_qn, is_external = resolved
            external_label: str | None = None
            if is_external:
                # (H) The import pass mints the same node for IMPORTS edges, so
                # (H) this MERGEs idempotently when the base was imported.
                self.import_processor.ensure_external_module_node(parent_qn)
                external_label = cs.NodeLabel.EXTERNAL_MODULE.value
            if entry.rel_type == cs.RelationshipType.IMPLEMENTS:
                rel.create_implements_relationship(
                    str(child_type),
                    entry.child_qn,
                    parent_qn,
                    self.ingestor,
                    interface_label=external_label,
                )
                self.interface_implementers.setdefault(parent_qn, set()).add(
                    entry.child_qn
                )
            else:
                bases = self.class_inheritance.get(entry.child_qn)
                if bases is not None and entry.base_index < len(bases):
                    bases[entry.base_index] = parent_qn
                rel.create_inheritance_relationship(
                    str(child_type),
                    entry.child_qn,
                    parent_qn,
                    self.function_registry,
                    self.ingestor,
                    entry.base_index,
                    parent_label=external_label,
                )
            emitted += 1
        return emitted

    def _resolve_deferred_parent_qn(
        self, entry: DeferredInherit
    ) -> tuple[str, bool] | None:
        """Resolve a deferred parent to (qn, is_external), or None for no edge.

        First-party wins; a qn outside the project prefix is positive external
        knowledge (import-mapped or ::-qualified) and keeps its edge onto an
        ExternalModule node. A project-prefixed qn that is not a real class
        may still name one through a src-root layout (setup.py maps src/ to
        the distribution name), recovered by a unique whole-segment suffix
        match. A module-anchored guess that re-resolves nowhere externalizes
        its WRITTEN name (canonicalized for JS globals and java.lang): a base
        name resolving to no indexed class is by construction defined outside
        the indexed tree, and dropping it would lose a syntactic inheritance
        fact the source declares.
        """
        if entry.parent_qn == entry.child_qn:
            # (H) Parse-time resolution can land on the child ITSELF. A
            # (H) self-edge is never real, so the written base must refer to a
            # (H) SHADOWED outer name (thrift's `pub enum Error` implementing
            # (H) the std `Error` trait): when the module-anchored remainder is
            # (H) a bare single segment it IS the written name and
            # (H) externalizes. A dotted remainder (a nested child like
            # (H) SimpleHashMap.Entry) was never written as such; derivation
            # (H) would be a lie, so no edge.
            self_prefix = f"{entry.module_qn}{cs.SEPARATOR_DOT}"
            if entry.parent_qn.startswith(self_prefix):
                raw = entry.parent_qn[len(self_prefix) :]
                if raw and cs.SEPARATOR_DOT not in raw:
                    return self._externalize_written_base(raw, entry.language)
            return None
        if self.function_registry.get(entry.parent_qn) is not None:
            return entry.parent_qn, False
        project_prefix = f"{self.project_name}{cs.SEPARATOR_DOT}"
        if not entry.parent_qn.startswith(project_prefix):
            external = entry.parent_qn.replace(
                cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT
            )
            return external, True
        prefix = f"{entry.module_qn}{cs.SEPARATOR_DOT}"
        if not entry.parent_qn.startswith(prefix):
            # (H) Project-prefixed but not module-anchored: an import-mapped
            # (H) qn whose written path skips real directories (thrift's
            # (H) setup.py maps lib/py/src -> package `thrift`, so the import
            # (H) says thrift.Thrift while the class qn says
            # (H) thrift.src.Thrift). A UNIQUE whole-segment suffix match
            # (H) recovers the real node; ambiguity means no edge.
            tail = entry.parent_qn[len(project_prefix) :]
            simple = tail.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            suffix = f"{cs.SEPARATOR_DOT}{tail}"
            candidates = self.function_registry.find_ending_with(simple)
            matches = {
                qn for qn in candidates if qn.endswith(suffix) and qn != entry.child_qn
            }
            if len(matches) == 1:
                return matches.pop(), False
            # (H) A base written as a PACKAGE attribute (`forms.ModelForm` via
            # (H) `from django import forms`) names the re-exporting package, not
            # (H) the defining module (django.forms.models.ModelForm behind the
            # (H) package __init__'s star import), so the suffix match cannot
            # (H) bridge the missing segment. A UNIQUE same-named class UNDER the
            # (H) written package path is that re-export; ambiguity means no edge.
            package_prefix = (
                entry.parent_qn.rsplit(cs.SEPARATOR_DOT, 1)[0] + cs.SEPARATOR_DOT
            )
            # (H) The registry also holds functions/methods with the same simple
            # (H) name; only a TYPE declaration is a valid inheritance target, so
            # (H) filter before the uniqueness check (a same-named factory function
            # (H) under the package must not corrupt the class hierarchy). The
            # (H) package must also actually EXPOSE the name (its __init__ imports
            # (H) it explicitly or star-imports the defining module); a same-named
            # (H) internal class the package never re-exports is not the referent.
            type_decls = (NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM)
            package_qn = package_prefix[: -len(cs.SEPARATOR_DOT)]
            package_matches = {
                qn
                for qn in candidates
                if qn.startswith(package_prefix)
                and qn != entry.child_qn
                and self.function_registry.get(qn) in type_decls
                and self._package_exposes(package_qn, simple, qn)
            }
            if len(package_matches) == 1:
                return package_matches.pop(), False
            return None
        # (H) The module-anchored fallback shape carries the raw written name
        # (H) as the remainder after the module qn.
        raw_name = entry.parent_qn[len(prefix) :]
        resolved = self._resolve_class_name(raw_name, entry.module_qn)
        if (
            resolved is not None
            # (H) A simple-name sweep can land on the child itself; a
            # (H) self-INHERITS is never real.
            and resolved != entry.child_qn
            and self.function_registry.get(resolved) is not None
        ):
            return resolved, False
        return self._externalize_written_base(raw_name, entry.language)

    def _package_exposes(self, package_qn: str, simple: str, class_qn: str) -> bool:
        # (H) True when the package __init__ makes `simple` an attribute of the
        # (H) package: an explicit import binding the name to this class (or its
        # (H) defining module member), or a star import of the module that
        # (H) defines it. Star-import keys carry a leading GLOB_ALL marker,
        # (H) matching the call resolver's wildcard convention.
        imports = self.import_processor.import_mapping.get(package_qn)
        if not imports:
            return False
        if imports.get(simple) == class_qn:
            return True
        star_member = f"{cs.SEPARATOR_DOT}{simple}"
        return any(
            key.startswith(cs.GLOB_ALL) and class_qn == f"{target}{star_member}"
            for key, target in imports.items()
        )

    def _externalize_written_base(
        self, raw_name: str, language: cs.SupportedLanguage
    ) -> tuple[str, bool]:
        if language in cs.JS_TS_LANGUAGES and raw_name in cs.JS_GLOBAL_CLASS_NAMES:
            return f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}{raw_name}", True
        # (H) java.lang is implicitly imported: a bare base in its table gets
        # (H) its canonical java.lang qn.
        if (
            language == cs.SupportedLanguage.JAVA
            and raw_name in cs.JAVA_LANG_CLASS_NAMES
        ):
            return f"{cs.JAVA_LANG_PREFIX}{raw_name}", True
        # (H) Language-agnostic fallback: the written base name resolves to no
        # (H) indexed class, so the base is external to the index by
        # (H) construction (Python `object`, Rust `Default`, a generated Java
        # (H) `Iface`); keep the fact under the written name.
        return raw_name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT), True

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
        # (H) The class query captures a templated class twice: the inner
        # (H) class_specifier AND its template_declaration wrapper. The wrapper is the
        # (H) canonical node (it carries the template params and always registers with
        # (H) its natural qn); the inner specifier is redundant. Drop the inner one
        # (H) outright -- for bodied definitions too, not just bodyless forward decls --
        # (H) so the class registers exactly once. Registering both suffixes the second
        # (H) `@line`, splitting members (which attach to the bodied specifier) away
        # (H) from the natural qn that callers reference, orphaning the whole class.
        if (
            language == cs.SupportedLanguage.CPP
            and class_node.type in cs.CPP_TYPE_SPECIFIER_NODE_TYPES
            and class_node.parent is not None
            and class_node.parent.type == cs.CppNodeType.TEMPLATE_DECLARATION
        ):
            return

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
        class_qn = self.function_registry.register_unique_qn(
            class_qn, class_node.start_point[0] + 1
        )
        node_type = nt.determine_node_type(class_node, class_name, class_qn, language)

        modifiers, decorators = extract_modifiers_and_decorators(
            class_node, lang_queries
        )

        class_props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: class_qn,
            cs.KEY_NAME: class_name,
            cs.KEY_MODIFIERS: modifiers,
            cs.KEY_DECORATORS: decorators,
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
            # (H) An out-of-class nested definition (`class Outer::Inner {}`)
            # (H) carries the qualifier in its extracted name. Index the leaf
            # (H) too, or an out-of-line method (`bool Inner::m()`, often via a
            # (H) `using Inner = Outer::Inner;` alias) can never resolve the
            # (H) class and binds to a phantom fallback qn.
            if cs.SEPARATOR_DOUBLE_COLON in class_name:
                leaf = class_name.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1]
                self.simple_name_lookup[leaf].add(class_qn)

        parent_label, parent_qn = self._determine_function_parent(
            class_node, class_qn, module_qn, lang_config, language
        )
        self._emit_or_defer_defines(
            parent_label, parent_qn, node_type, class_qn, module_qn
        )
        # (H) For a templated class the canonical node is the template_declaration
        # (H) wrapper, which has no `body` field. Its members -- base clause, fields,
        # (H) methods -- live on the inner class_specifier (type_spec). Extract them
        # (H) from there so they bind to the class's natural qn. For a plain class
        # (H) type_spec is class_node, so this is a no-op for non-templates and for
        # (H) Go/Rust (which never take the template_declaration branch).
        member_node = type_spec if type_spec is not None else class_node
        rel.create_class_relationships(
            member_node,
            class_qn,
            module_qn,
            node_type,
            is_exported,
            language,
            self.class_inheritance,
            self.ingestor,
            self.import_processor,
            self._resolve_to_qn,
            self.function_registry,
            self.interface_implementers,
            defer_cpp_inherits=self._deferred_cpp_inherits,
            defer_inherits=self._deferred_inherits,
        )
        if language == cs.SupportedLanguage.CPP:
            # (H) Record this class's member-field types now (from the class body,
            # (H) usually a header) so out-of-line method bodies in other files can
            # (H) resolve `field_.method()` via the field's type at call resolution.
            if field_types := CppTypeInferenceEngine().build_field_type_map(
                member_node
            ):
                self.class_field_types[class_qn] = field_types
        elif language == cs.SupportedLanguage.GO:
            # (H) Record Go struct field types so a field-hop receiver
            # (H) (`engine.trees.get()`) resolves, and a local bound from such a call
            # (H) (`root := engine.trees.get(m)`) picks up the return type.
            if field_types := GoTypeInferenceEngine().build_field_type_map(class_node):
                self.class_field_types[class_qn] = field_types
        elif language == cs.SupportedLanguage.RUST:
            # (H) Record Rust struct field types so a field-hop receiver
            # (H) (`self.shutdown.is_shutdown()`) resolves through the field's type,
            # (H) plus guard-container inner types (`state: Mutex<State>` -> State),
            # (H) applied only at a lock/read/borrow hop.
            rust_engine = RustTypeInferenceEngine()
            if field_types := rust_engine.build_field_type_map(class_node):
                self.class_field_types[class_qn] = field_types
            if guard_inner := rust_engine.build_field_guard_inner_map(class_node):
                self.class_field_guard_inner[class_qn] = guard_inner
        elif language == cs.SupportedLanguage.CSHARP:
            # (H) Record C# field/property types so a field-typed receiver
            # (H) (`_w.M()`, `this._w.M()`) resolves, including a field inherited
            # (H) from a base class in another file (the resolver walks
            # (H) class_inheritance over these per-class maps).
            if field_types := csharp_utils.build_field_type_map(member_node):
                self.class_field_types[class_qn] = field_types
        self._ingest_class_methods(
            member_node,
            class_qn,
            language,
            lang_queries,
            file_path,
            sorted_func_nodes=sorted_func_nodes,
            func_node_starts=func_node_starts,
            module_qn=module_qn,
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
            trait_qn = self._resolve_to_qn(trait_name, owner_module_qn)
            # (H) The trait (or the impl target) may live in a file not yet
            # (H) parsed; hold the IMPLEMENTS edge back for
            # (H) resolve_deferred_inherits so an unresolvable trait
            # (H) (std::fmt::Display) emits no phantom edge.
            self._deferred_inherits.append(
                DeferredInherit(
                    rel_type=cs.RelationshipType.IMPLEMENTS,
                    child_qn=class_qn,
                    parent_qn=trait_qn,
                    module_qn=owner_module_qn,
                    base_index=0,
                    language=cs.SupportedLanguage.RUST,
                )
            )
            # (H) Record the implementer so a Rust trait call to the sole concrete
            # (H) impl redirects, matching the class-declaration IMPLEMENTS path.
            self.interface_implementers.setdefault(trait_qn, set()).add(class_qn)

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
                # (H) The impl target may be a primitive (`impl From<Foo> for u8`)
                # (H) or a type registered later in the pass; defer the containment
                # (H) edge so it verifies against the registry (module fallback for
                # (H) primitives) instead of dangling on a phantom Class qn.
                defer_containment=self._deferred_parent_links,
                module_qn=owner_module_qn,
            )
            # (H) Record the method's return type (Self -> impl target) so a chained
            # (H) call (`Ping::new(msg).into_frame()`) and a call-bound local
            # (H) (`let cmd = Command::from_frame(f)`) can resolve the next hop.
            name_node = method_node.child_by_field_name(cs.FIELD_NAME)
            method_name = safe_decode_text(name_node) if name_node else None
            if method_name and (
                return_type := rs_utils.extract_return_type_name(
                    method_node, impl_target
                )
            ):
                self.method_return_types[
                    f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
                ] = return_type

    def _ingest_class_methods(
        self,
        class_node: Node,
        class_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        file_path: Path | None = None,
        sorted_func_nodes: list[Node] | None = None,
        func_node_starts: list[int] | None = None,
        module_qn: str | None = None,
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

        # (H) Names defined by an EXTERNAL stdlib base of this class (click's
        # (H) `class TextWrapper(textwrap.TextWrapper)`): a method matching one
        # (H) overrides the base and is invoked by its machinery, so mark it for
        # (H) the dead-code root property. Only unregistered (external) parents
        # (H) contribute; first-party bases resolve via OVERRIDES edges.
        external_override_names: frozenset[str] = frozenset()
        if language == cs.SupportedLanguage.PYTHON:
            external_parents = [
                p
                for p in self.class_inheritance.get(class_qn, [])
                if self.function_registry.get(p) is None
            ]
            if external_parents:
                external_override_names = external_stdlib_base_method_names(
                    external_parents
                )

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
            elif language == cs.SupportedLanguage.CSHARP:
                # (H) Give C# methods/constructors a parameter signature so
                # (H) overloads and overloaded constructors stay distinct nodes
                # (H) (without it two `Widget(...)` ctors collide and the second
                # (H) gets an `@line` suffix). Zero-arg members stay bare so their
                # (H) qn is stable and matches an unsignatured call site.
                cs_name, cs_params = csharp_utils.extract_method_signature(method_node)
                if cs_name and cs_params:
                    param_sig = cs.SEPARATOR_COMMA_SPACE.join(cs_params)
                    method_qualified_name = f"{class_qn}.{cs_name}({param_sig})"

            ingested_qn = ingest_method(
                method_node,
                class_qn,
                cs.NodeLabel.CLASS,
                self.ingestor,
                self.function_registry,
                self.simple_name_lookup,
                self._get_docstring,
                language,
                lang_queries=lang_queries,
                method_qualified_name=method_qualified_name,
                file_path=file_path,
                repo_path=self.repo_path,
                external_override_names=external_override_names,
            )
            if ingested_qn is not None:
                record_cpp_definition_span(
                    self.cpp_definition_spans,
                    language,
                    file_path,
                    self.repo_path,
                    method_node,
                    cs.NodeLabel.METHOD.value,
                    ingested_qn,
                )
            # (H) Track C# methods (and the `override`-modified subset) so the
            # (H) override walk gates class-parent matches: an implicit hide or a
            # (H) `new` shadow is not an override, unlike an interface impl.
            if language == cs.SupportedLanguage.CSHARP and ingested_qn is not None:
                self.csharp_methods.add(ingested_qn)
                if csharp_has_override_modifier(method_node):
                    self.csharp_override_methods.add(ingested_qn)
                # (H) Index extension methods by simple name + receiver type so a
                # (H) `recv.Ext()` call binds to the static method even though it
                # (H) lives on an unrelated static class (not in recv's hierarchy).
                if receiver_type := csharp_utils.extension_receiver_type(method_node):
                    leaf = ingested_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1].split(
                        cs.CHAR_PAREN_OPEN, 1
                    )[0]
                    self.csharp_extension_methods.setdefault(leaf, []).append(
                        (ingested_qn, receiver_type)
                    )
            # (H) A Java method declared inside an anonymous class body
            # (H) (`new Base(){ @Override m(){} }`) is ingested here under the enclosing
            # (H) class but really overrides the anon class's base type. Record it so a
            # (H) deferred pass emits the OVERRIDES edge once the base is registered;
            # (H) with override-reachability that keeps the dispatch-only override live.
            if (
                language == cs.SupportedLanguage.JAVA
                and ingested_qn is not None
                and module_qn is not None
                and (base := _java_anonymous_base_type(method_node, class_node))
            ):
                method_name = ingested_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1].split(
                    cs.CHAR_PAREN_OPEN, 1
                )[0]
                self.java_anon_overrides.append(
                    (ingested_qn, method_name, base, module_qn)
                )
            # (H) Record where this method landed so Pass-3 call attribution
            # (H) reuses the registered qn/label instead of re-deriving them.
            # (H) The walks diverge on preprocessor-distorted C++ class bodies
            # (H) and on TS declaration merging, where the member registers
            # (H) under the namespace's duplicate-suffixed qn (issue #652).
            if ingested_qn is not None and module_qn is not None:
                self.function_locations[function_span_key(module_qn, method_node)] = (
                    FunctionLocation(
                        label=cs.NodeLabel.METHOD.value,
                        qualified_name=ingested_qn,
                        container_qn=class_qn,
                    )
                )
            if language == cs.SupportedLanguage.CPP:
                # (H) Record a C++ method's return type so a chained call off a
                # (H) static factory method (`parser(...).parse(...)`, nlohmann's
                # (H) basic_json) can type the receiver and resolve the next hop.
                method_name = cpp_utils.extract_function_name(method_node)
                if method_name and (
                    return_type := cpp_utils.extract_return_type_name(method_node)
                ):
                    self.method_return_types[
                        f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
                    ] = return_type

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
            # (H) Record the inline module qn so deferred import verification
            # (H) counts it as a real internal target.
            self.declared_module_qns.add(inline_module_qn)

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
            self.interface_implementers,
            self.csharp_methods,
            self.csharp_override_methods,
        )
        self._resolve_java_anon_overrides()

    def _resolve_java_anon_overrides(self) -> None:
        # (H) Emit OVERRIDES edges for Java anonymous-class methods recorded at ingestion
        # (H) (`new Base(){ @Override m(){} }`). The base type is resolved by UNIQUE
        # (H) global simple-name match among type declarations (the base is usually in
        # (H) another file; a full import resolve is unnecessary and this stays
        # (H) revive-only -- an ambiguous or unfound base is skipped). Each base method
        # (H) directly on the base whose simple name matches gets an OVERRIDES edge from
        # (H) the anon override, so override-reachability keeps the dispatch-only method
        # (H) live when the base is reachable.
        type_decls = (NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM)
        for anon_qn, method_name, base_type, _module_qn in self.java_anon_overrides:
            # (H) A method-body anonymous override is registered as a FUNCTION (via the
            # (H) function-ingest path), a field-initializer one as a METHOD. The graph
            # (H) matches an edge endpoint by LABEL + qn, so emit the source with the qn's
            # (H) ACTUAL registered label -- a hard-coded Method label drops the edge for
            # (H) the Function-labelled overrides (the eval matches by qn and would hide
            # (H) this, but the production Cypher would not).
            anon_type = self.function_registry.get(anon_qn)
            if anon_type is None:
                continue
            anon_label = cs.NodeLabel(anon_type.value)
            base_candidates = [
                qn
                for qn in self.function_registry.find_ending_with(base_type)
                if self.function_registry.get(qn) in type_decls
            ]
            if len(base_candidates) != 1:
                continue
            base_qn = base_candidates[0]
            base_prefix = f"{base_qn}{cs.SEPARATOR_DOT}"
            for qn, node_type in self.function_registry.find_with_prefix(base_qn):
                if (
                    node_type == NodeType.METHOD
                    and qn.startswith(base_prefix)
                    and cs.SEPARATOR_DOT
                    not in qn[len(base_prefix) :].split(cs.CHAR_PAREN_OPEN, 1)[0]
                    and qn[len(base_prefix) :].split(cs.CHAR_PAREN_OPEN, 1)[0]
                    == method_name
                ):
                    self.ingestor.ensure_relationship_batch(
                        (anon_label, cs.KEY_QUALIFIED_NAME, anon_qn),
                        cs.RelationshipType.OVERRIDES,
                        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, qn),
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
