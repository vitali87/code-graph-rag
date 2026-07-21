from __future__ import annotations

from abc import abstractmethod
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
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
from ..dart import utils as dart_utils
from ..dart.type_inference import DartTypeInferenceEngine
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
    # If `method_node` sits inside an anonymous class body between it and
    # `class_node` (`new Base(){ ... m() ... }`), return the anon class's base type
    # name (the object_creation's `type` field, generic args stripped). None when the
    # method belongs directly to the enclosing class, not an anonymous subclass.
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
    # A C/C++ forward declaration held back until every file's real definitions
    # are registered, so we can tell an only-forward-declared type (keep it) from
    # one that also has a bodied definition elsewhere (drop the phantom).
    class_node: Node
    class_name: str
    # The namespace-qualified name (module-file prefix stripped, so `A::Foo` is
    # `A.Foo` regardless of which header declares it). Comparing on this, not the
    # bare simple name, keeps a forward-declared `B::Foo` when only `A::Foo` is
    # defined, while still matching a cross-file forward/definition of one type.
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
    csharp_partial_groups: dict[str, list[str]]
    csharp_generic_methods: set[str]
    csharp_class_generic_arity: dict[str, int]
    csharp_method_return_types: dict[str, tuple[str, int]]
    _csharp_partial_index: dict[str, list[str]]
    csharp_extension_methods: dict[str, list[tuple[str, str, str, int]]]
    csharp_base_kinds: dict[tuple[str, int], dict[str, str]]
    csharp_type_locations: dict[tuple[str, int], str]
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
        # Strip the module-file prefix so two nodes for the same C++ type in
        # different headers share one key (`leveldb.db.x.h.leveldb.VersionSet` and
        # `...y.h.leveldb.VersionSet` both -> `leveldb.VersionSet`), while types in
        # different namespaces stay distinct.
        prefix = f"{module_qn}{cs.SEPARATOR_DOT}"
        return class_qn[len(prefix) :] if class_qn.startswith(prefix) else class_qn

    def _namespace_qn_has_definition(self, ns_qn: str) -> bool:
        # A real definition of this namespace-qualified type is registered iff some
        # class qn ends with it (`....leveldb.VersionSet`). find_ending_with is
        # indexed by simple name, and because it is queried AFTER the registry is
        # rehydrated from the graph, it also covers definitions in files an
        # incremental run did not re-parse (issue: a forward decl must still drop
        # when its definition lives in an unchanged file).
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
        fallback_label: str | None = None,
        fallback_qn: str | None = None,
        parent_span: FunctionSpanKey | None = None,
    ) -> None: ...

    @abstractmethod
    def _determine_function_parent(
        self,
        func_node: Node,
        func_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str, FunctionSpanKey | None]: ...

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
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
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
        # Run after every file's definitions are registered. A deferred forward
        # declaration whose class name already produced a real node is a phantom
        # (the bodied definition exists) -> drop it. Otherwise it is the only
        # representation of the type -> register it now. Deterministic: the
        # deferred list is in file (sorted) order, and the first surviving forward
        # declaration of an only-declared type claims the name for the rest.
        deferred = getattr(self, "_deferred_forward_decls", None)
        if not deferred:
            return 0
        self._deferred_forward_decls = []
        registered = 0
        for entry in deferred:
            # Drop the forward declaration only when a real definition of the SAME
            # namespace-qualified type exists (not merely the same simple name in
            # another namespace). Otherwise it is the type's only node -> keep it.
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
        # Scope-first (see resolve_deferred_cpp_methods): the enclosing
        # namespaces distinguish same-leaf classes. The child itself is
        # excluded so `class Type : public other::Type` never self-inherits.
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
        # The guess strips a qualified base (`other::Type`) to its leaf, so
        # it can collide with the CHILD's own qn when the base is
        # unresolvable; a self-INHERITS is never real.
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
                # The import pass mints the same node for IMPORTS edges, so
                # this MERGEs idempotently when the base was imported.
                self.import_processor.ensure_external_module_node(parent_qn)
                external_label = cs.NodeLabel.EXTERNAL_MODULE.value
            if entry.rel_type == cs.RelationshipType.IMPLEMENTS:
                # Dart has no `interface` keyword: `implements X` targets a
                # concrete class, so a hardcoded Interface label would dangle.
                # Resolve the target's real registered label (Interface for a
                # true interface, Class/Enum for a Dart type); external stays
                # EXTERNAL_MODULE.
                interface_label = external_label or rel.get_node_type_for_inheritance(
                    parent_qn, self.function_registry
                )
                rel.create_implements_relationship(
                    str(child_type),
                    entry.child_qn,
                    parent_qn,
                    self.ingestor,
                    interface_label=interface_label,
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
            # Parse-time resolution can land on the child ITSELF. A
            # self-edge is never real. In C# the written base can be an
            # ARITY sibling (`class Foo : Foo<object>`, a different type
            # sharing the simple name); recover it before falling back.
            # Otherwise the written base must refer to a SHADOWED outer name
            # (thrift's `pub enum Error` implementing the std `Error` trait):
            # when the module-anchored remainder is a bare single segment it
            # IS the written name and externalizes. A dotted remainder (a
            # nested child like SimpleHashMap.Entry) was never written as
            # such; derivation would be a lie, so no edge.
            if (sibling := self._csharp_arity_sibling(entry)) is not None:
                return sibling, False
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
            # Project-prefixed but not module-anchored: an import-mapped
            # qn whose written path skips real directories (thrift's
            # setup.py maps lib/py/src -> package `thrift`, so the import
            # says thrift.Thrift while the class qn says
            # thrift.src.Thrift). A UNIQUE whole-segment suffix match
            # recovers the real node; ambiguity means no edge.
            tail = entry.parent_qn[len(project_prefix) :]
            simple = tail.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            suffix = f"{cs.SEPARATOR_DOT}{tail}"
            candidates = self.function_registry.find_ending_with(simple)
            matches = {
                qn for qn in candidates if qn.endswith(suffix) and qn != entry.child_qn
            }
            if len(matches) == 1:
                return matches.pop(), False
            # A base written as a PACKAGE attribute (`forms.ModelForm` via
            # `from django import forms`) names the re-exporting package, not
            # the defining module (django.forms.models.ModelForm behind the
            # package __init__'s star import), so the suffix match cannot
            # bridge the missing segment. A UNIQUE same-named class UNDER the
            # written package path is that re-export; ambiguity means no edge.
            package_prefix = (
                entry.parent_qn.rsplit(cs.SEPARATOR_DOT, 1)[0] + cs.SEPARATOR_DOT
            )
            # The registry also holds functions/methods with the same simple
            # name; only a TYPE declaration is a valid inheritance target, so
            # filter before the uniqueness check (a same-named factory function
            # under the package must not corrupt the class hierarchy). The
            # package must also EXPOSE the name (its __init__ imports it
            # explicitly or star-imports the defining module); a same-named
            # internal class the package never re-exports is not the referent.
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
        # The module-anchored fallback shape carries the raw written name
        # as the remainder after the module qn.
        raw_name = entry.parent_qn[len(prefix) :]
        resolved = self._resolve_class_name(raw_name, entry.module_qn)
        if (
            resolved is not None
            # A simple-name sweep can land on the child itself; a
            # self-INHERITS is never real.
            and resolved != entry.child_qn
            and self.function_registry.get(resolved) is not None
        ):
            return resolved, False
        return self._externalize_written_base(raw_name, entry.language)

    def _csharp_arity_sibling(self, entry: DeferredInherit) -> str | None:
        # Only C# overloads type names by generic arity, so only there can a
        # base that resolves to the declaring type itself legally name a
        # DIFFERENT type. The sibling conventionally lives beside the child
        # (Polly's Foo.cs + Foo.TResult.cs), so only a UNIQUE same-simple-name
        # type declaration under the module's parent package qualifies;
        # ambiguity keeps the no-edge answer rather than guessing.
        if entry.language != cs.SupportedLanguage.CSHARP:
            return None
        type_decls = (NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM)
        # Same-scope pair (issue #764): when both members share ONE file and
        # scope, they collide on natural qn and the later one registers as a
        # DUP_QN_MARKER variant. The variants bucket therefore holds exactly
        # the same-scope declarations of this simple name; the unique other
        # type declaration IS the written sibling, whichever of the pair the
        # child happens to be. More than one other means a 3+ arity family;
        # refuse rather than guess, matching every other ambiguity tier.
        natural = entry.child_qn.split(cs.DUP_QN_MARKER, 1)[0]
        same_scope = [
            qn
            for qn in self.function_registry.variants(natural)
            if qn != entry.child_qn and self.function_registry.get(qn) in type_decls
        ]
        if len(same_scope) == 1:
            return same_scope[0]
        if same_scope:
            return None
        simple = natural.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        candidates = {
            qn
            for qn in self.function_registry.find_ending_with(simple)
            if qn != entry.child_qn
            and simple in qn.split(cs.SEPARATOR_DOT)
            and self.function_registry.get(qn) in type_decls
        }
        package_prefix = (
            entry.module_qn.rsplit(cs.SEPARATOR_DOT, 1)[0] + cs.SEPARATOR_DOT
        )
        in_package = {qn for qn in candidates if qn.startswith(package_prefix)}
        if len(in_package) == 1:
            return in_package.pop()
        if in_package:
            # Multiple same-package candidates can still be ONE type: a
            # `partial` sibling split across files (Polly's PredicateBuilder,
            # issue #764 shape 3). When every candidate sits in a single
            # partial group, pick the deterministic representative; genuine
            # distinct types keep the no-edge answer.
            groups = {
                frozenset(self.csharp_partial_groups.get(qn, (qn,)))
                for qn in in_package
            }
            if len(groups) == 1:
                return min(in_package)
            return None
        # No same-package sibling: the pair can span projects (Polly's
        # legacy BrokenCircuitException<TResult> : the Polly.Core
        # non-generic), so fall back to a project-wide unique declaration.
        return candidates.pop() if len(candidates) == 1 else None

    def _package_exposes(self, package_qn: str, simple: str, class_qn: str) -> bool:
        # True when the package __init__ makes `simple` an attribute of the
        # package: an explicit import binding the name to this class (or its
        # defining module member), or a star import of the module that
        # defines it. Star-import keys carry a leading GLOB_ALL marker,
        # matching the call resolver's wildcard convention.
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
        # java.lang is implicitly imported: a bare base in its table gets
        # its canonical java.lang qn.
        if (
            language == cs.SupportedLanguage.JAVA
            and raw_name in cs.JAVA_LANG_CLASS_NAMES
        ):
            return f"{cs.JAVA_LANG_PREFIX}{raw_name}", True
        # Language-agnostic fallback: the written base name resolves to no
        # indexed class, so the base is external to the index by
        # construction (Python `object`, Rust `Default`, a generated Java
        # `Iface`); keep the fact under the written name.
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

        # A C/C++ forward declaration (`class Widget;`, or `template <T> class
        # Widget;`) is a bodyless type specifier. Registering it collides with the
        # real definition's qn (suffixing it `@line`) and fragments one class into
        # several same-named nodes, which makes member-call resolution pick among
        # duplicates nondeterministically. But a type ONLY ever forward-declared
        # (an opaque handle, or a metaprogramming primary defined solely via
        # specializations) has no other node, so it must be kept. We cannot tell
        # which until every file's definitions are in, so defer the forward
        # declaration and decide in resolve_deferred_forward_declarations.
        # The class query captures a templated class twice: the inner
        # class_specifier AND its template_declaration wrapper. The wrapper is the
        # canonical node (it carries the template params and always registers with
        # its natural qn); the inner specifier is redundant. Drop the inner one
        # outright (for bodied definitions too, not just bodyless forward decls)
        # so the class registers exactly once. Registering both suffixes the second
        # `@line`, splitting members (which attach to the bodied specifier) away
        # from the natural qn that callers reference, orphaning the whole class.
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
        if language == cs.SupportedLanguage.CSHARP:
            # Skip a leading `#if [Attr] #endif` directive so the start line is
            # the conditional attribute, not the `#if` line (matches Roslyn).
            from ..csharp import utils as csharp_utils

            class_start_line = csharp_utils.definition_start_line(class_node)
        else:
            class_start_line = class_node.start_point[0] + 1
        class_qn = self.function_registry.register_unique_qn(class_qn, class_start_line)
        node_type = nt.determine_node_type(class_node, class_name, class_qn, language)

        modifiers, decorators = extract_modifiers_and_decorators(
            class_node, lang_queries
        )

        class_props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: class_qn,
            cs.KEY_NAME: class_name,
            cs.KEY_MODIFIERS: modifiers,
            cs.KEY_DECORATORS: decorators,
            cs.KEY_START_LINE: class_start_line,
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
            # An out-of-class nested definition (`class Outer::Inner {}`)
            # carries the qualifier in its extracted name. Index the leaf
            # too, or an out-of-line method (`bool Inner::m()`, often via a
            # `using Inner = Outer::Inner;` alias) can never resolve the
            # class and binds to a phantom fallback qn.
            if cs.SEPARATOR_DOUBLE_COLON in class_name:
                leaf = class_name.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1]
                self.simple_name_lookup[leaf].add(class_qn)

        parent_label, parent_qn, parent_span = self._determine_function_parent(
            class_node, class_qn, module_qn, lang_config, language
        )
        self._emit_or_defer_defines(
            parent_label,
            parent_qn,
            node_type,
            class_qn,
            module_qn,
            parent_span=parent_span,
        )
        # For a templated class the canonical node is the template_declaration
        # wrapper, which has no `body` field. Its members (base clause, fields,
        # methods) live on the inner class_specifier (type_spec). Extract them
        # from there so they bind to the class's natural qn. For a plain class
        # type_spec is class_node, so this is a no-op for non-templates and for
        # Go/Rust (which never take the template_declaration branch).
        member_node = type_spec if type_spec is not None else class_node
        # When the opt-in Roslyn frontend ran, hand this type's exact base
        # classifications (keyed by its rel-path + start line) to the split so
        # INHERITS/IMPLEMENTS is semantic, not the I-prefix guess. Empty/absent
        # for non-C# types or when the frontend is off -> heuristic stands.
        csharp_base_kinds: dict[str, str] | None = None
        if language == cs.SupportedLanguage.CSHARP and file_path is not None:
            rel_path = cached_relative_path(file_path, self.repo_path).as_posix()
            # Reverse index for the Roslyn frontend's location-keyed facts:
            # partial declaration groups join back to these Class qns after
            # Pass 2.
            self.csharp_type_locations[(rel_path, class_start_line)] = class_qn
            if self.csharp_base_kinds:
                csharp_base_kinds = self.csharp_base_kinds.get(
                    (rel_path, class_start_line)
                )
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
            csharp_base_kinds=csharp_base_kinds,
        )
        if language == cs.SupportedLanguage.CPP:
            # Record this class's member-field types now (from the class body,
            # usually a header) so out-of-line method bodies in other files can
            # resolve `field_.method()` via the field's type at call resolution.
            if field_types := CppTypeInferenceEngine().build_field_type_map(
                member_node
            ):
                self.class_field_types[class_qn] = field_types
        elif language == cs.SupportedLanguage.GO:
            # Record Go struct field types so a field-hop receiver
            # (`engine.trees.get()`) resolves, and a local bound from such a call
            # (`root := engine.trees.get(m)`) picks up the return type.
            if field_types := GoTypeInferenceEngine().build_field_type_map(class_node):
                self.class_field_types[class_qn] = field_types
        elif language == cs.SupportedLanguage.RUST:
            # Record Rust struct field types so a field-hop receiver
            # (`self.shutdown.is_shutdown()`) resolves through the field's type,
            # plus guard-container inner types (`state: Mutex<State>` -> State),
            # applied only at a lock/read/borrow hop.
            rust_engine = RustTypeInferenceEngine()
            if field_types := rust_engine.build_field_type_map(class_node):
                self.class_field_types[class_qn] = field_types
            if guard_inner := rust_engine.build_field_guard_inner_map(class_node):
                self.class_field_guard_inner[class_qn] = guard_inner
        elif language == cs.SupportedLanguage.DART:
            # Record Dart field types (`Greeter buddy;`) so a field-typed
            # receiver (`buddy.greet()`, `this.buddy.hail()`) resolves
            # through the field's declared type.
            if field_types := DartTypeInferenceEngine().build_field_type_map(
                class_node
            ):
                self.class_field_types[class_qn] = field_types
        elif language == cs.SupportedLanguage.CSHARP:
            # Record C# field/property types so a field-typed receiver
            # (`_w.M()`, `this._w.M()`) resolves, including a field inherited
            # from a base class in another file (the resolver walks
            # class_inheritance over these per-class maps).
            field_types = csharp_utils.build_field_type_map(member_node) or {}
            # A record's positional parameters ARE public properties of
            # the record type; record them as members so receiver typing
            # and the delegate-invoke gate see them (`Callback();` on a
            # record param is Action.Invoke, not a first-party call).
            if member_node.type == cs.TS_CSHARP_RECORD_DECLARATION:
                for pl in member_node.children:
                    if pl.type != cs.TS_CSHARP_PARAMETER_LIST:
                        continue
                    for prm in pl.children:
                        if prm.type != cs.TS_CSHARP_PARAMETER:
                            continue
                        pname = safe_decode_text(prm.child_by_field_name(cs.FIELD_NAME))
                        ptype = safe_decode_text(prm.child_by_field_name(cs.FIELD_TYPE))
                        if pname and ptype:
                            field_types.setdefault(
                                pname, csharp_utils.annotate_type_ref(ptype)
                            )
            if field_types:
                self.class_field_types[class_qn] = field_types
            # Declared type-parameter count: `Builder<TResult>` -> 1;
            # unrecorded means 0, so only generics are stored. A class
            # node's type_parameter_list carries NO field name (unlike a
            # method's), so scan the children.
            for child in member_node.children:
                if child.type == cs.TS_CSHARP_TYPE_PARAMETER_LIST:
                    self.csharp_class_generic_arity[class_qn] = len(
                        child.named_children
                    )
                    break
            # A `partial` type is split across files into N path-distinct
            # nodes; group the parts into one shared list so a typed receiver
            # resolves members and bases from any part. The key is the
            # declaring DIRECTORY (module_qn minus the file stem) plus the
            # namespace-qualified name, NOT the bare namespace name: two
            # independent projects that both declare `N.Widget` live in
            # different directories and must not be merged across assembly
            # boundaries. Parts in different directories of one project fall
            # back to generic resolution (safe under-merge) rather than risk a
            # cross-project wrong edge.
            if cs.TS_CSHARP_MODIFIER_PARTIAL in modifiers:
                directory = (
                    module_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
                    if cs.SEPARATOR_DOT in module_qn
                    else module_qn
                )
                key = f"{directory}{cs.SEPARATOR_DOT}{class_qn[len(module_qn) + 1 :]}"
                group = self._csharp_partial_index.setdefault(key, [])
                group.append(class_qn)
                self.csharp_partial_groups[class_qn] = group
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

        # An impl block inside `mod inner` targets a type whose node lives
        # under the module path (proj...inner.Widget). Resolve the impl target
        # against its enclosing module so the method binds to the real type
        # node instead of a phantom under the file module.
        mod_parts = rs_utils.build_module_path(class_node)
        owner_module_qn = (
            f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(mod_parts)}"
            if mod_parts
            else module_qn
        )
        class_qn = f"{owner_module_qn}.{impl_target}"

        # `impl Trait for Type` means Type IMPLEMENTS Trait. The target type's
        # node label may be Class/Enum/Type, so match the relationship source
        # to its registered label (else the IMPLEMENTS edge never resolves).
        if trait_name := rs_utils.extract_impl_trait(class_node):
            trait_qn = self._resolve_to_qn(trait_name, owner_module_qn)
            # The trait (or the impl target) may live in a file not yet
            # parsed; hold the IMPLEMENTS edge back for
            # resolve_deferred_inherits so an unresolvable trait
            # (std::fmt::Display) emits no phantom edge.
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
            # Record the implementer so a Rust trait call to the sole concrete
            # impl redirects, matching the class-declaration IMPLEMENTS path.
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
                # The impl target may be a primitive (`impl From<Foo> for u8`)
                # or a type registered later in the pass; defer the containment
                # edge so it verifies against the registry (module fallback for
                # primitives) instead of dangling on a phantom Class qn.
                defer_containment=self._deferred_parent_links,
                module_qn=owner_module_qn,
            )
            # Record the method's return type (Self -> impl target) so a chained
            # call (`Ping::new(msg).into_frame()`) and a call-bound local
            # (`let cmd = Command::from_frame(f)`) can resolve the next hop.
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

        # Names defined by an EXTERNAL stdlib base of this class (click's
        # `class TextWrapper(textwrap.TextWrapper)`): a method matching one
        # overrides the base and is invoked by its machinery, so mark it for
        # the dead-code root property. Only unregistered (external) parents
        # contribute; first-party bases resolve via OVERRIDES edges.
        external_override_names: frozenset[str] = frozenset()
        root_annotated_overrides = False
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
        elif language == cs.SupportedLanguage.DART:
            # An external base (a Flutter widget class) is not introspectable
            # the way Python stdlib bases are, but Dart marks every override
            # explicitly with @override: trust the annotation whenever any
            # parent is unregistered.
            root_annotated_overrides = any(
                self.function_registry.get(p) is None
                for p in self.class_inheritance.get(class_qn, [])
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
                # Give C# methods/constructors a parameter signature so
                # overloads and overloaded constructors stay distinct nodes
                # (without it two `Widget(...)` ctors collide and the second
                # gets an `@line` suffix). Zero-arg members stay bare so their
                # qn is stable and matches an unsignatured call site.
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
                root_annotated_overrides=root_annotated_overrides,
            )
            if (
                ingested_qn is not None
                and language == cs.SupportedLanguage.CSHARP
                and method_node.child_by_field_name(cs.TS_CSHARP_FIELD_TYPE_PARAMETERS)
                is not None
            ):
                self.csharp_generic_methods.add(ingested_qn)
            # Record Dart return types (a constructor "returns" its class)
            # so a local bound from a static factory or named constructor
            # (`var s = Greeter.create()`) types from the RECORDED return
            # instead of guessing the class from the call's base name.
            if (
                ingested_qn is not None
                and language == cs.SupportedLanguage.DART
                and (dart_return := dart_utils.dart_return_type_name(method_node))
            ):
                self.method_return_types[ingested_qn] = dart_return
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
            # Track C# methods (and the `override`-modified subset) so the
            # override walk gates class-parent matches: an implicit hide or a
            # `new` shadow is not an override, unlike an interface impl.
            if language == cs.SupportedLanguage.CSHARP and ingested_qn is not None:
                if module_qn is not None:
                    # Record where this member landed so the Roslyn frontend's
                    # declaration-location facts (call targets, query callers)
                    # resolve to the exact registered qn and label.
                    self.function_locations[
                        function_span_key(module_qn, method_node)
                    ] = FunctionLocation(
                        label=cs.NodeLabel.METHOD.value,
                        qualified_name=ingested_qn,
                        container_qn=class_qn,
                    )
                self.csharp_methods.add(ingested_qn)
                if csharp_has_override_modifier(method_node):
                    self.csharp_override_methods.add(ingested_qn)
                # Index extension methods by simple name + receiver type so a
                # `recv.Ext()` call binds to the static method even though it
                # lives on an unrelated static class (not in recv's hierarchy).
                csharp_utils.index_extension_method(
                    self.csharp_extension_methods,
                    ingested_qn,
                    method_node,
                    class_qn,
                    module_qn,
                )
            # A Java method declared inside an anonymous class body
            # (`new Base(){ @Override m(){} }`) is ingested here under the enclosing
            # class but really overrides the anon class's base type. Record it so a
            # deferred pass emits the OVERRIDES edge once the base is registered;
            # with override-reachability that keeps the dispatch-only override live.
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
            # Record where this method landed so Pass-3 call attribution
            # reuses the registered qn/label instead of re-deriving them.
            # The walks diverge on preprocessor-distorted C++ class bodies
            # and on TS declaration merging, where the member registers
            # under the namespace's duplicate-suffixed qn (issue #652).
            if ingested_qn is not None and module_qn is not None:
                self.function_locations[function_span_key(module_qn, method_node)] = (
                    FunctionLocation(
                        label=cs.NodeLabel.METHOD.value,
                        qualified_name=ingested_qn,
                        container_qn=class_qn,
                    )
                )
            if (
                language == cs.SupportedLanguage.CSHARP
                and ingested_qn is not None
                and (
                    rt_node := method_node.child_by_field_name(
                        cs.TS_CSHARP_FIELD_RETURNS
                    )
                    # property_declaration exposes its type via `type`,
                    # not `returns`; recording it lets chained typing and
                    # the external-member gate see through properties.
                    or method_node.child_by_field_name(cs.FIELD_TYPE)
                )
                is not None
            ):
                # Record a C# method's return type so a chained call
                # (`Policy.Handle<T>().CircuitBreaker(...)`, Polly's whole
                # fluent surface) can type the receiver for the next hop.
                if rt_text := csharp_utils.normalize_csharp_type_name(rt_node):
                    raw = csharp_utils.safe_decode_text(rt_node) or rt_text
                    self.csharp_method_return_types[ingested_qn] = (
                        rt_text,
                        csharp_utils.generic_arity_of_type_text(raw),
                    )
            if language == cs.SupportedLanguage.CPP:
                # Record a C++ method's return type so a chained call off a
                # static factory method (`parser(...).parse(...)`, nlohmann's
                # basic_json) can type the receiver and resolve the next hop.
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

            # A bodyless `mod foo;` only declares that the file module foo.rs
            # belongs here; foo.rs already yields its own real-path Module node
            # with the same qn. Emitting a second synthetic-path node collides
            # on that qn and clobbers the file's real path, so skip it.
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
            # A bodied inline module is physically located in this file; give
            # it the real path so it joins containment on (file, line).
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
            # Record the inline module qn so deferred import verification
            # counts it as a real internal target.
            self.declared_module_qns.add(inline_module_qn)

            # Link the inline module into the containment tree: its enclosing
            # module (file module, or an outer mod) DEFINES it. Without this the
            # inline Module node is an orphan defining nothing.
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
        # Emit OVERRIDES edges for Java anonymous-class methods recorded at ingestion
        # (`new Base(){ @Override m(){} }`). The base type is resolved by UNIQUE
        # global simple-name match among type declarations (the base is usually in
        # another file; a full import resolve is unnecessary and this stays
        # revive-only; an ambiguous or unfound base is skipped). Each base method
        # directly on the base whose simple name matches gets an OVERRIDES edge from
        # the anon override, so override-reachability keeps the dispatch-only method
        # live when the base is reachable.
        type_decls = (NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM)
        for anon_qn, method_name, base_type, _module_qn in self.java_anon_overrides:
            # A method-body anonymous override is registered as a FUNCTION (via the
            # function-ingest path), a field-initializer one as a METHOD. The graph
            # matches an edge endpoint by LABEL + qn, so emit the source with the qn's
            # ACTUAL registered label; a hard-coded Method label drops the edge for
            # the Function-labelled overrides (the eval matches by qn and would hide
            # this, but the production Cypher would not).
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
