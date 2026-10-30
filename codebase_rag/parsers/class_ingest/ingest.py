from __future__ import annotations

from collections.abc import Collection, Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from tree_sitter import Node, QueryCursor

from ... import constants as cs
from ...language_spec import LanguageSpec
from ...node_text import sorted_captures
from ...types_defs import (
    CppDefinitionSpan,
    DeferredInherit,
    FunctionLocation,
    PropertyDict,
)
from ...utils.path_utils import cached_relative_path, cached_resolve_posix
from ..anchor_hash import anchor_hash_props
from ..cpp import class_ingest as cpp_class_ingest
from ..cpp.utils import record_cpp_definition_span
from ..csharp import class_ingest as csharp_class_ingest
from ..dart import class_ingest as dart_class_ingest
from ..definition_facts import DefinitionFacts
from ..enum_variants import emit_declared_variants, is_python_enum
from ..field_nodes import PendingFieldType, emit_declared_fields
from ..go import class_ingest as go_class_ingest
from ..go import interfaces as go_ifaces
from ..java import class_ingest as java_class_ingest
from ..js_ts import class_ingest as js_ts_class_ingest
from ..js_ts import utils as js_ts_utils
from ..js_ts.utils import mark_js_ts_module_private
from ..language_definitions import LanguageClassIngest
from ..method_ingest import MethodIngestHost, ingest_method
from ..php import class_ingest as php_class_ingest
from ..python import class_ingest as python_class_ingest
from ..python import resolve_class_name
from ..rust import class_ingest as rust_class_ingest
from ..rust import utils as rs_utils
from ..utils import (
    decorated_start_props,
    extract_modifiers_and_decorators,
    function_span_key,
    written_simple_name,
)
from . import base_targets as bt
from . import cpp_modules
from . import definition_label as nt
from . import identity as id_
from . import method_override as mo
from . import relationships as rel
from .utils import body_method_nodes, find_child_by_type, skip_method

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import (
        DeferredParentLink,
        FunctionRegistryTrieProtocol,
        LanguageQueries,
        SimpleNameLookup,
    )
    from ..function_ingest import FunctionIngest
    from ..go.interface_ingest import GoInterfaceIngest
    from ..import_processor import ImportProcessor


def _class_start_point(
    class_node: Node, language: cs.SupportedLanguage
) -> tuple[int, int]:
    if language == cs.SupportedLanguage.CSHARP:
        # Skip a leading `#if [Attr] #endif` directive so the start line is
        # the conditional attribute, not the `#if` line (matches Roslyn).
        from ..csharp import utils as csharp_utils

        return csharp_utils.definition_start_point(class_node)
    return class_node.start_point[0] + 1, class_node.start_point[1]


def _signatured_method_qn(
    method_node: Node, class_qn: str, language: cs.SupportedLanguage
) -> str | None:
    if language == cs.SupportedLanguage.JAVA:
        return java_class_ingest.signatured_method_qn(method_node, class_qn)
    if language == cs.SupportedLanguage.CSHARP:
        return csharp_class_ingest.signatured_method_qn(method_node, class_qn)
    return None


class _MethodScope(NamedTuple):
    # The per-class context every method of one class body is ingested under.
    class_node: Node
    class_qn: str
    language: cs.SupportedLanguage
    file_path: Path | None
    module_qn: str | None


class ClassIngest:
    """Writes every class-like node a file's class captures yield (classes,
    interfaces, enums, traits, impl blocks, inline modules) with their methods,
    bases and members. The INHERITS/IMPLEMENTS edges that wait for every type
    to be registered are held here and resolved by `resolve_deferred_inherits`
    and the override walk. A language's own share is the collaborator it
    builds from that language's package (`CppClassIngest`, `RustClassIngest`,
    `CSharpClassIngest` and the rest), which holds what that language defers
    until its own `resolve_deferred_*` pass."""

    __slots__ = (
        "_method_host",
        "_function_ingest",
        "_go_interface_ingest",
        "ingestor",
        "repo_path",
        "project_name",
        "function_registry",
        "simple_name_lookup",
        "module_qn_to_file_path",
        "import_processor",
        "facts",
        "class_owner_module",
        "go_interfaces",
        "cpp_definition_spans",
        "pending_field_types",
        "folded_overloads",
        "_deferred_parent_links",
        "_deferred_inherits",
        "cpp",
        "rust",
        "csharp",
        "go",
        "dart",
        "js_ts",
        "java",
        "python",
        "_languages",
        "_neutral",
    )

    def __init__(
        self,
        *,
        method_host: MethodIngestHost,
        function_ingest: FunctionIngest,
        go_interface_ingest: GoInterfaceIngest,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrieProtocol,
        simple_name_lookup: SimpleNameLookup,
        module_qn_to_file_path: dict[str, Path],
        flow_capture_enabled: bool,
        import_processor: ImportProcessor,
        facts: DefinitionFacts,
        class_owner_module: dict[str, str],
        dart_annotated_overrides: dict[str, list[tuple[str, str]]],
        java_anon_overrides: list[tuple[str, str, str, str]],
        csharp_methods: set[str],
        csharp_override_methods: set[str],
        csharp_partial_index: dict[str, list[str]],
        csharp_type_locations: dict[tuple[str, int], str],
        go_type_locations: dict[tuple[str, int, int], tuple[str, str]],
        rust_impl_owner_module: dict[str, str],
        go_interfaces: dict[str, go_ifaces.GoInterfaceDecl],
        cpp_definition_spans: dict[str, list[CppDefinitionSpan]],
        rust_impl_method_traits: dict[str, str],
        rust_inherent_impl_methods: set[str],
        cpp_module_interfaces: set[str],
        cpp_interfaces_parsed_this_run: set[str],
        pending_field_types: list[PendingFieldType],
        folded_overloads: dict[str, frozenset[int]],
        deferred_parent_links: list[DeferredParentLink],
    ) -> None:
        # What a class's methods are written through, and where a
        # definition's docstring is read from.
        self._method_host = method_host
        # A class's lexical parent and its DEFINES edge are the function
        # pass's, shared with every function it ingests.
        self._function_ingest = function_ingest
        # A Go interface's method specs are written by the Go interface pass.
        self._go_interface_ingest = go_interface_ingest
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.module_qn_to_file_path = module_qn_to_file_path
        self.import_processor = import_processor
        self.facts = facts
        self.class_owner_module = class_owner_module
        self.go_interfaces = go_interfaces
        self.cpp_definition_spans = cpp_definition_spans
        self.pending_field_types = pending_field_types
        self.folded_overloads = folded_overloads
        # Shared with the function pass, which emits them once every parent
        # is registered.
        self._deferred_parent_links = deferred_parent_links
        # Non-C++ INHERITS/IMPLEMENTS held back until every class is
        # registered; resolve_deferred_inherits re-resolves the guesses.
        self._deferred_inherits: list[DeferredInherit] = []
        self.cpp = cpp_class_ingest.CppClassIngest(
            self,
            function_ingest.cpp,
            ingestor=ingestor,
            repo_path=repo_path,
            project_name=project_name,
            function_registry=function_registry,
            module_qn_to_file_path=module_qn_to_file_path,
            import_processor=import_processor,
            facts=facts,
            module_interfaces=cpp_module_interfaces,
            interfaces_parsed_this_run=cpp_interfaces_parsed_this_run,
        )
        self.rust = rust_class_ingest.RustClassIngest(
            self,
            method_host=method_host,
            ingestor=ingestor,
            repo_path=repo_path,
            project_name=project_name,
            function_registry=function_registry,
            module_qn_to_file_path=module_qn_to_file_path,
            flow_capture_enabled=flow_capture_enabled,
            import_processor=import_processor,
            facts=facts,
            impl_owner_module=rust_impl_owner_module,
            impl_method_traits=rust_impl_method_traits,
            inherent_impl_methods=rust_inherent_impl_methods,
            deferred_parent_links=deferred_parent_links,
        )
        self.csharp = csharp_class_ingest.CSharpClassIngest(
            repo_path=repo_path,
            function_registry=function_registry,
            module_qn_to_file_path=module_qn_to_file_path,
            import_processor=import_processor,
            facts=facts,
            methods=csharp_methods,
            override_methods=csharp_override_methods,
            partial_index=csharp_partial_index,
            type_locations=csharp_type_locations,
        )
        self.go = go_class_ingest.GoClassIngest(
            repo_path=repo_path, facts=facts, type_locations=go_type_locations
        )
        self.dart = dart_class_ingest.DartClassIngest(
            self,
            ingestor=ingestor,
            function_registry=function_registry,
            facts=facts,
            annotated_overrides=dart_annotated_overrides,
        )
        self.js_ts = js_ts_class_ingest.JsTsClassIngest(
            function_registry=function_registry,
            module_qn_to_file_path=module_qn_to_file_path,
            import_processor=import_processor,
        )
        self.java = java_class_ingest.JavaClassIngest(
            ingestor=ingestor,
            function_registry=function_registry,
            facts=facts,
            anon_overrides=java_anon_overrides,
        )
        self.python = python_class_ingest.PythonClassIngest(function_registry, facts)
        # The collaborator each language's class nodes go to; a language with
        # none takes the neutral answers.
        self._languages: dict[cs.SupportedLanguage, LanguageClassIngest] = {
            cs.SupportedLanguage.PYTHON: self.python,
            cs.SupportedLanguage.JS: self.js_ts,
            cs.SupportedLanguage.TS: self.js_ts,
            cs.SupportedLanguage.TSX: self.js_ts,
            cs.SupportedLanguage.JAVA: self.java,
            cs.SupportedLanguage.RUST: self.rust,
            cs.SupportedLanguage.GO: self.go,
            cs.SupportedLanguage.CPP: self.cpp,
            cs.SupportedLanguage.CSHARP: self.csharp,
            cs.SupportedLanguage.DART: self.dart,
        }
        self._neutral = LanguageClassIngest()

    def _for(self, language: cs.SupportedLanguage | None) -> LanguageClassIngest:
        if language is None:
            return self._neutral
        return self._languages.get(language, self._neutral)

    def _resolve_to_qn(
        self,
        name: str,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
    ) -> str:
        # Parse time sees a partial registry, so the C# declared-form tier of
        # _resolve_class_name (a uniqueness test) is left to the deferred pass.
        return (
            self._resolve_base_name(name, module_qn, language) or f"{module_qn}.{name}"
        )

    def _resolve_base_name(
        self, name: str, module_qn: str, language: cs.SupportedLanguage | None
    ) -> str | None:
        return resolve_class_name(
            name,
            module_qn,
            self.import_processor,
            self.function_registry,
            kinds=bt.base_target_kinds(language),
        )

    def ingest_classes_and_methods(
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
        else:
            if not (query := lang_queries[cs.QUERY_CLASSES]):
                return
            cursor = QueryCursor(query)
            captures = sorted_captures(cursor, root_node)
            class_nodes = captures.get(cs.CAPTURE_CLASS, [])

        if language == cs.SupportedLanguage.CPP:
            class_nodes.extend(cpp_modules.find_cpp_exported_classes(root_node))

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

    def ingest_inline_modules(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list] | None = None,
    ) -> None:
        # Runs before the file's function and class passes: an item inside a
        # bodied `mod` buffers its DEFINES from that mod's Module node, and
        # when a batch flush fell between the item and the node (buffered
        # last) the edge's MATCH found no source and the row was dropped,
        # leaving the item outside every deletion walk (issue #3000).
        lang_queries = queries[language]
        if combined_captures is not None:
            module_nodes = combined_captures.get(cs.ONEOF_MODULE, [])
        else:
            if not (query := lang_queries[cs.QUERY_CLASSES]):
                return
            module_nodes = sorted_captures(QueryCursor(query), root_node).get(
                cs.ONEOF_MODULE, []
            )
        if module_nodes:
            self.rust.ingest_inline_modules(
                module_nodes, module_qn, lang_queries[cs.QUERY_CONFIG]
            )

    def resolve_deferred_inherits(self) -> int:
        """Emit non-C++ INHERITS/IMPLEMENTS edges now that every class is registered.

        A parent in a file parsed later resolves against the full registry; a
        parent that resolves nowhere (java.lang.Exception, Rust Send/Sync)
        emits no edge, because the module-anchored guess is a phantom endpoint
        the database silently drops anyway. Resolved base qns replace the
        guesses in class_inheritance in place so Pass-3 method resolution and
        override detection walk the real hierarchy. A C# base is looked up by
        scope first (its namespace, the enclosing ones, then `using`s); one no
        scope reaches falls to the name search and its edge is marked
        heuristic (issue #2534).
        """
        deferred = self._deferred_inherits
        self._deferred_inherits = []
        emitted = 0
        # `implements` targets never enter class_inheritance, so the override
        # ancestry walk needs the resolved first-party IMPLEMENTS parents
        # collected here or a registered interface's method is wrongly rooted.
        dart_implements: dict[str, list[str]] = {}
        for entry in deferred:
            child_type = self._deferred_child_label(entry)
            if child_type is None:
                continue
            # Scope first (issue #2534); a base no scope reaches falls to the
            # name search, whose `PB`/`PB<T>` pick the written arity settles
            # (issue #2579).
            scoped = self.csharp.scoped_base(entry)
            resolved = (
                (scoped, False)
                if scoped is not None
                else self.csharp.base_by_arity(
                    entry, self._resolve_deferred_parent_qn(entry)
                )
            )
            is_dart = entry.language == cs.SupportedLanguage.DART
            if resolved is None or resolved[1]:
                # Resolved nowhere, or to a node outside the index: the file
                # defining this base may be added later (issue #1568).
                self.import_processor.note_unresolved(
                    entry.module_qn, written_simple_name(entry.parent_qn)
                )
            if resolved is None:
                continue
            # A C# base no scope or `using` reaches, bound anyway by a
            # project-wide name match, is a guess; say so on the edge.
            heuristic = (
                scoped is None and entry.written_ref is not None and not resolved[1]
            )
            self._emit_resolved_inherit(
                entry, child_type, resolved, is_dart, dart_implements, heuristic
            )
            emitted += 1
        self.dart.flag_external_overrides(dart_implements)
        self.rust.flag_external_trait_overrides()
        return emitted

    def _deferred_child_label(self, entry: DeferredInherit) -> str | None:
        held = self.function_registry.get(entry.child_qn)
        if held is None:
            return None
        # Where a TS type shares its name with a value (issue #2520) the
        # registry holds the value's kind for both, so the label the child was
        # declared with decides which of the two nodes the edge leaves.
        if (
            entry.child_label is not None
            and self.function_registry.type_kind(entry.child_qn) != held
        ):
            return entry.child_label
        return str(held)

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
            return self._resolve_self_edge_parent(entry)
        # Only a type declaration is a base: a registered member of the same
        # name (a C# property, a method) is not, however it was reached.
        target_kinds = bt.base_target_kinds(entry.language)
        if entry.language == cs.SupportedLanguage.SCALA and (
            (
                imported := self.import_processor.scala.scala_base_qn(
                    entry.module_qn,
                    entry.child_qn,
                    entry.parent_qn,
                    entry.base_index,
                )
            )
            is not None
            and self.function_registry.get(imported) in target_kinds
        ):
            # Ahead of the registry check: parse time bound the base by name
            # alone when only a package block imports it, and a sibling
            # block's same-named class can be what that found.
            return imported, False
        if self.function_registry.get(entry.parent_qn) in target_kinds:
            return entry.parent_qn, False
        if (followed := self.rust.reexport_target(entry)) is not None:
            return followed, False
        if (followed := self.js_ts.reexport_target(entry)) is not None:
            return followed, False
        if entry.alt_parent_qn is not None:
            # The written path was exact and still landed on nothing, which a
            # re-export does: `crate::Base` names the entry module while the
            # trait registers where it is declared. The second spelling gets
            # the whole chain, not just the registry probe above.
            return self._resolve_deferred_parent_qn(
                entry._replace(parent_qn=entry.alt_parent_qn, alt_parent_qn=None)
            )
        project_prefix = f"{self.project_name}{cs.SEPARATOR_DOT}"
        if not entry.parent_qn.startswith(project_prefix):
            external = entry.parent_qn.replace(
                cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT
            )
            return external, True
        prefix = f"{entry.module_qn}{cs.SEPARATOR_DOT}"
        if not entry.parent_qn.startswith(prefix):
            return self._resolve_unanchored_project_parent(entry, project_prefix)
        # The module-anchored fallback shape carries the raw written name
        # as the remainder after the module qn.
        raw_name = entry.parent_qn[len(prefix) :]
        if (bound := self.js_ts.namespace_member(raw_name, entry)) is not None:
            return bound, False
        resolved = self._resolve_class_name(raw_name, entry.module_qn, entry.language)
        if (
            resolved is not None
            # A simple-name sweep can land on the child itself; a
            # self-INHERITS is never real.
            and resolved != entry.child_qn
            and self.function_registry.get(resolved) in target_kinds
        ):
            return resolved, False
        return self._externalize_written_base(raw_name, entry.language)

    def _resolve_self_edge_parent(
        self, entry: DeferredInherit
    ) -> tuple[str, bool] | None:
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
        if (sibling := self.csharp.arity_sibling(entry)) is not None:
            return sibling, False
        self_prefix = f"{entry.module_qn}{cs.SEPARATOR_DOT}"
        if entry.parent_qn.startswith(self_prefix):
            raw = entry.parent_qn[len(self_prefix) :]
            if raw and cs.SEPARATOR_DOT not in raw:
                return self._externalize_written_base(raw, entry.language)
        return None

    def _resolve_unanchored_project_parent(
        self, entry: DeferredInherit, project_prefix: str
    ) -> tuple[str, bool] | None:
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
        target_kinds = bt.base_target_kinds(entry.language)
        matches = {
            qn
            for qn in candidates
            if qn.endswith(suffix)
            and qn != entry.child_qn
            and self.function_registry.get(qn) in target_kinds
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
        type_decls = (cs.NodeLabel.CLASS, cs.NodeLabel.INTERFACE, cs.NodeLabel.ENUM)
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
            self.rust.ingest_impl_methods(
                class_node,
                module_qn,
                language,
                lang_queries,
                sorted_func_nodes=sorted_func_nodes,
                func_node_starts=func_node_starts,
            )
            return

        if language in cs.C_FAMILY_LANGUAGES and self.cpp.skips_class(
            class_node,
            module_qn,
            language,
            lang_queries,
            lang_config,
            file_path,
            sorted_func_nodes,
            func_node_starts,
            allow_defer,
        ):
            return

        type_spec = cpp_class_ingest.type_spec(class_node)
        local_qn = (
            self.cpp.local_type_qn(class_node, module_qn)
            if language == cs.SupportedLanguage.CPP
            else None
        )
        identity = (
            self.cpp.local_type_identity(class_node, local_qn)
            if local_qn is not None
            else id_.resolve_class_identity(
                class_node,
                module_qn,
                language,
                lang_config,
                file_path,
            )
        )
        if not identity:
            return

        class_qn, class_name, is_exported = identity
        class_start_line, class_start_col = _class_start_point(class_node, language)
        label = nt.definition_label(class_node, class_name, class_qn, language)
        class_qn = self.function_registry.register_unique_qn(
            class_qn,
            class_start_line,
            class_start_col,
            kind=label if language in cs.JS_TS_LANGUAGES else None,
        )

        modifiers, decorators = extract_modifiers_and_decorators(
            class_node, lang_queries
        )

        class_props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: class_qn,
            cs.KEY_NAME: class_name,
            cs.KEY_MODIFIERS: modifiers,
            cs.KEY_DECORATORS: decorators,
            cs.KEY_START_LINE: class_start_line,
            cs.KEY_START_COL: class_start_col,
            cs.KEY_END_LINE: class_node.end_point[0] + 1,
            cs.KEY_DOCSTRING: self._method_host._get_docstring(class_node, language),
            cs.KEY_IS_EXPORTED: is_exported,
        }
        if file_path is not None:
            class_props[cs.KEY_PATH] = cached_relative_path(
                file_path, self.repo_path
            ).as_posix()
            class_props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
        self._record_language_type_props(
            class_node, class_qn, class_name, label, language, class_props
        )
        # A container's hash covers its whole subtree, the same reading as a
        # function's: a note on a class is about the class as declared, and
        # a member edit is a change under it. It makes a class note gradable
        # (STALE/EXACT), movable by hash, and re-validatable by the quote
        # move, which refuses a hash-less candidate (issue #1808).
        class_props.update(anchor_hash_props(class_node, decorators))
        class_props.update(decorated_start_props(class_node))
        # Stored on the node so an incremental run that does not re-parse
        # this file can register them again.
        non_method_members = (
            self.python.non_method_members(class_node)
            if language == cs.SupportedLanguage.PYTHON
            else []
        )
        if non_method_members:
            class_props[cs.KEY_NON_METHOD_MEMBERS] = non_method_members
        self.ingestor.ensure_node_batch(label, class_props)
        self.function_registry[class_qn] = label
        mark_js_ts_module_private(
            self.function_registry, class_qn, is_exported, language
        )
        self.function_registry.set_non_method_members(class_qn, non_method_members)
        if class_name:
            self._index_class_simple_name(class_name, class_qn)

        # The declaring module, for the class-keyed maps' prune (#1772).
        # Recorded here rather than in the per-language field-type branches
        # below, because `class_inheritance` is written for EVERY language
        # while those branches cover only five, and a class carries no span
        # record for the sweep to attribute it by.
        self.class_owner_module[class_qn] = module_qn

        parent_label, parent_qn, parent_span = (
            self.cpp.local_type_parent(class_node, local_qn, module_qn)
            if local_qn is not None
            else self._function_ingest.determine_function_parent(
                class_node, class_qn, module_qn, lang_config, language
            )
        )
        self._function_ingest.emit_or_defer_defines(
            parent_label,
            parent_qn,
            label,
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
        # Declared fields ride with their owner: same gate, same props source
        # for path/absolute_path, queued type for the deferred OF_TYPE pass.
        # Emitted AFTER the owner node so a batch flush never writes the edge
        # before its endpoint, and from member_node: a templated C++ class's
        # wrapper has no body, its members live on the inner class_specifier
        # .
        emit_declared_fields(
            self.ingestor,
            self.pending_field_types,
            label,
            class_qn,
            module_qn,
            member_node,
            language,
            class_props,
        )
        # When the opt-in Roslyn frontend ran, hand this type's exact base
        # classifications (keyed by its rel-path + start line) to the split so
        # INHERITS/IMPLEMENTS is semantic, not the I-prefix guess. Empty/absent
        # for non-C# types or when the frontend is off -> heuristic stands.
        csharp_base_kinds = self._record_type_location(
            class_qn,
            label,
            language,
            file_path,
            class_start_line,
            class_start_col,
        )
        rel.create_class_relationships(
            member_node,
            class_qn,
            module_qn,
            label,
            is_exported,
            language,
            self.facts.class_inheritance,
            self.ingestor,
            self.import_processor,
            partial(self._resolve_to_qn, language=language),
            self.function_registry,
            self.facts.interface_implementers,
            defer_cpp_inherits=self.cpp.deferred_inherits,
            defer_inherits=self._deferred_inherits,
            csharp_base_kinds=csharp_base_kinds,
        )
        self._record_declared_type_members(
            member_node, class_qn, module_qn, label, language, class_props
        )
        if language == cs.SupportedLanguage.GO and label == cs.NodeLabel.INTERFACE:
            self._go_interface_ingest.ingest_interface_methods(
                class_node, class_qn, module_qn, file_path
            )
        self._record_class_field_types(
            class_node,
            member_node,
            class_qn,
            module_qn,
            language,
            modifiers,
            file_path,
        )
        self._ingest_class_methods(
            member_node,
            class_qn,
            language,
            lang_queries,
            file_path,
            sorted_func_nodes=sorted_func_nodes,
            func_node_starts=func_node_starts,
            module_qn=module_qn,
            container_label=label,
        )

    def _record_language_type_props(
        self,
        class_node: Node,
        class_qn: str,
        class_name: str,
        label: cs.NodeLabel,
        language: cs.SupportedLanguage,
        class_props: PropertyDict,
    ) -> None:
        if language == cs.SupportedLanguage.PHP:
            php_class_ingest.record_namespace(class_node, class_props)
        elif language == cs.SupportedLanguage.CSHARP:
            self.csharp.record_type_props(
                class_node, class_qn, class_name, label, class_props
            )
        elif language == cs.SupportedLanguage.GO and label == cs.NodeLabel.INTERFACE:
            # Stored so an incremental run that does not re-parse the
            # interface still knows what its method set is built from.
            class_props.update(go_ifaces.embeds_props(class_node))
        elif language == cs.SupportedLanguage.PYTHON:
            self.python.record_type_props(
                class_node, class_qn, class_name, label, class_props
            )

    def _record_declared_type_members(
        self,
        member_node: Node,
        class_qn: str,
        module_qn: str,
        label: cs.NodeLabel,
        language: cs.SupportedLanguage,
        class_props: PropertyDict,
    ) -> None:
        # The variants ride with their enum the way fields ride with their
        # owner (issue #1807). After the relationships, because a Python enum
        # is a Class that only its resolved bases identify (issue #2583).
        if label == cs.NodeLabel.ENUM or (
            language == cs.SupportedLanguage.PYTHON
            and is_python_enum(self.facts.class_inheritance.get(class_qn, ()))
        ):
            emit_declared_variants(
                self.ingestor,
                label,
                class_qn,
                member_node,
                language,
                class_props,
                self.import_processor.import_mapping.get(module_qn),
            )
        if language == cs.SupportedLanguage.DART:
            self.dart.record_heritage(member_node, class_qn, module_qn)

    def _index_class_simple_name(self, class_name: str, class_qn: str) -> None:
        self.simple_name_lookup[class_name].add(class_qn)
        # An out-of-class nested definition (`class Outer::Inner {}`)
        # carries the qualifier in its extracted name. Index the leaf
        # too, or an out-of-line method (`bool Inner::m()`, often via a
        # `using Inner = Outer::Inner;` alias) can never resolve the
        # class and binds to a phantom fallback qn.
        if cs.SEPARATOR_DOUBLE_COLON in class_name:
            leaf = class_name.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1]
            self.simple_name_lookup[leaf].add(class_qn)

    def _record_type_location(
        self,
        class_qn: str,
        label: cs.NodeLabel,
        language: cs.SupportedLanguage,
        file_path: Path | None,
        class_start_line: int,
        class_start_col: int,
    ) -> dict[str, str] | None:
        if language == cs.SupportedLanguage.CSHARP and file_path is not None:
            return self.csharp.record_type_location(
                class_qn, label, file_path, class_start_line, class_start_col
            )
        if language == cs.SupportedLanguage.GO and file_path is not None:
            self.go.record_type_location(
                class_qn, label, file_path, class_start_line, class_start_col
            )
        return None

    def _record_class_field_types(
        self,
        class_node: Node,
        member_node: Node,
        class_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        modifiers: list[str],
        file_path: Path | None,
    ) -> None:
        if language == cs.SupportedLanguage.CPP:
            self.cpp.record_field_types(member_node, class_qn)
        elif language == cs.SupportedLanguage.GO:
            self.go.record_field_types(class_node, class_qn)
        elif language == cs.SupportedLanguage.RUST:
            self.rust.record_field_types(class_node, class_qn)
        elif language == cs.SupportedLanguage.DART:
            self.dart.record_field_types(class_node, class_qn)
        elif language == cs.SupportedLanguage.CSHARP:
            self.csharp.record_class_members(
                member_node, class_qn, module_qn, modifiers, file_path
            )

    def _emit_resolved_inherit(
        self,
        entry: DeferredInherit,
        child_type: str,
        resolved: tuple[str, bool],
        is_dart: bool,
        dart_implements: dict[str, list[str]],
        heuristic: bool = False,
    ) -> None:
        parent_qn, is_external = resolved
        resolution = cs.EdgeResolution.HEURISTIC if heuristic else None
        if not is_external and entry.language == cs.SupportedLanguage.CSHARP:
            self.csharp.pin_base_module(entry, parent_qn)
        external_label: str | None = None
        if is_external:
            # The import pass mints the same node for IMPORTS edges, so
            # this MERGEs idempotently when the base was imported.
            self.import_processor.ensure_external_module_node(parent_qn)
            external_label = cs.NodeLabel.EXTERNAL_MODULE.value
        if entry.rel_type != cs.RelationshipType.IMPLEMENTS:
            bases = self.facts.class_inheritance.get(entry.child_qn)
            if bases is not None and entry.base_index < len(bases):
                bases[entry.base_index] = parent_qn
            rel.create_inheritance_relationship(
                child_type,
                entry.child_qn,
                parent_qn,
                self.function_registry,
                self.ingestor,
                entry.base_index,
                parent_label=external_label,
                resolution=resolution,
            )
            return
        # Dart has no `interface` keyword: `implements X` targets a
        # concrete class, so a hardcoded Interface label would dangle.
        # Resolve the target's real registered label (Interface for a
        # true interface, Class/Enum for a Dart type); external stays
        # EXTERNAL_MODULE.
        interface_label = external_label or rel.get_node_type_for_inheritance(
            parent_qn, self.function_registry, type_position=True
        )
        rel.create_implements_relationship(
            child_type,
            entry.child_qn,
            parent_qn,
            self.ingestor,
            interface_label=interface_label,
            resolution=resolution,
        )
        self.facts.interface_implementers.setdefault(parent_qn, set()).add(
            entry.child_qn
        )
        if is_dart and not is_external:
            dart_implements.setdefault(entry.child_qn, []).append(parent_qn)

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
        container_label: cs.NodeLabel = cs.NodeLabel.CLASS,
    ) -> None:
        body_node = class_node.child_by_field_name("body")
        if body_node is None and class_node.type == cs.TS_DART_MIXIN_DECLARATION:
            # mixin_declaration is the one Dart type declaration whose
            # class_body is a positional child, not a `body` field; without
            # this fallback a mixin's members silently vanish.
            body_node = find_child_by_type(class_node, cs.TS_DART_CLASS_BODY)
        if not body_node:
            return

        lang_config: LanguageSpec = lang_queries[cs.QUERY_CONFIG]
        member_aliases = (
            self.python.member_aliases(body_node)
            if language == cs.SupportedLanguage.PYTHON
            else {}
        )

        method_nodes = body_method_nodes(
            body_node, lang_queries, sorted_func_nodes, func_node_starts
        )
        if method_nodes is None:
            return
        method_nodes = self._without_overload_stubs(method_nodes, module_qn)

        external_override_names, annotated_override_sink = (
            self._method_override_context(class_qn, language)
        )
        scope = _MethodScope(class_node, class_qn, language, file_path, module_qn)
        # Gated on language: Dart spells its ordinary members `method_signature`.
        seen_signatures: set[str] | None = (
            set() if language in cs.JS_TS_LANGUAGES else None
        )

        for method_node in method_nodes:
            if skip_method(method_node, class_node, body_node, lang_config):
                continue
            if seen_signatures is not None and js_ts_utils.repeats_member_signature(
                method_node, seen_signatures
            ):
                continue

            method_qualified_name = _signatured_method_qn(
                method_node, class_qn, language
            )

            ingested_qn = ingest_method(
                self._method_host,
                method_node,
                class_qn,
                container_label,
                language,
                lang_queries=lang_queries,
                method_qualified_name=method_qualified_name,
                file_path=file_path,
                module_qn=module_qn,
                external_override_names=external_override_names,
                annotated_override_sink=annotated_override_sink,
                member_aliases=member_aliases.get(method_node.id, ()),
            )
            if ingested_qn is not None:
                self._record_ingested_method(scope, method_node, ingested_qn)
            if language == cs.SupportedLanguage.CPP:
                self.cpp.record_method_return_type(method_node, class_qn)

    def _without_overload_stubs(
        self, method_nodes: list[Node], module_qn: str | None
    ) -> list[Node]:
        # The implementation after an `@overload` stub owns the method's
        # name; the stub is no method of its own (issue #2590), nor is a TS
        # namespace member's overload signature (issue #2457).
        stubs = self.folded_overloads.get(module_qn or "")
        if not stubs:
            return method_nodes
        return [node for node in method_nodes if node.start_byte not in stubs]

    def _method_override_context(
        self, class_qn: str, language: cs.SupportedLanguage
    ) -> tuple[frozenset[str], dict[str, list[tuple[str, str]]] | None]:
        external_override_names: frozenset[str] = frozenset()
        annotated_override_sink: dict[str, list[tuple[str, str]]] | None = None
        if language == cs.SupportedLanguage.PYTHON:
            external_override_names = self.python.external_override_names(class_qn)
        elif language == cs.SupportedLanguage.DART:
            # An external base (a Flutter widget class) is not introspectable
            # the way Python stdlib bases are, but Dart marks every override
            # explicitly with @override. Whether a parent is truly external is
            # unknowable here (it may live in a file parsed later), so only
            # RECORD the annotated methods; resolve_deferred_inherits decides
            # once every class is registered.
            annotated_override_sink = self.dart.annotated_overrides
        return external_override_names, annotated_override_sink

    def _record_ingested_method(
        self, scope: _MethodScope, method_node: Node, ingested_qn: str
    ) -> None:
        language = scope.language
        module_qn = scope.module_qn
        if language == cs.SupportedLanguage.CSHARP:
            self.csharp.record_method(
                scope.class_node, scope.class_qn, module_qn, method_node, ingested_qn
            )
        if language == cs.SupportedLanguage.DART:
            self.dart.record_method(
                scope.class_node, scope.class_qn, module_qn, method_node, ingested_qn
            )
        # Rust trait bodies reach here rather than the impl path above;
        # a trait is no module either (issue #1086).
        rs_utils.record_effective_module(
            self.facts.rust_function_modules,
            ingested_qn,
            method_node,
            module_qn,
            language,
        )
        record_cpp_definition_span(
            self.cpp_definition_spans,
            language,
            scope.file_path,
            self.repo_path,
            method_node,
            cs.NodeLabel.METHOD.value,
            ingested_qn,
        )
        if language == cs.SupportedLanguage.JAVA and module_qn is not None:
            self.java.record_anonymous_override(
                method_node, scope.class_node, ingested_qn, module_qn
            )
        # Record where this method landed so Pass-3 call attribution
        # reuses the registered qn/label instead of re-deriving them.
        # The walks diverge on preprocessor-distorted C++ class bodies
        # and on TS declaration merging, where the member registers
        # under the namespace's duplicate-suffixed qn (issue #652).
        # For Rust, first claim wins: a fn in a mod inside a trait
        # const default is already claimed by the function pass under
        # its own qn, and overwriting would re-attribute its calls to
        # this pass's twin.
        if module_qn is not None:
            self._record_method_location(scope, module_qn, method_node, ingested_qn)

    def _record_method_location(
        self,
        scope: _MethodScope,
        module_qn: str,
        method_node: Node,
        ingested_qn: str,
    ) -> None:
        span = function_span_key(module_qn, method_node)
        location = FunctionLocation(
            label=cs.NodeLabel.METHOD.value,
            qualified_name=ingested_qn,
            container_qn=scope.class_qn,
        )
        if (
            scope.language != cs.SupportedLanguage.RUST
            or span not in self.facts.function_locations
        ):
            self.facts.function_locations[span] = location
        if scope.language == cs.SupportedLanguage.JAVA:
            self.java.register_name_alias(module_qn, method_node, location)

    def process_all_method_overrides(self, module_qns: Collection[str] = ()) -> None:
        # Every module the graph holds, read-back ones included: a C++ type
        # name is looked up by its path within its module (issue #2455).
        mo.process_all_method_overrides(
            self.function_registry,
            self.facts.class_inheritance,
            self.ingestor,
            self._override_walk_implementers(),
            self.csharp.methods,
            self.csharp.override_methods,
            self.rust.impl_method_traits,
            self.rust.inherent_impl_methods,
            module_qns={*self.module_qn_to_file_path, *module_qns},
            linked_apart=self.go_interfaces.keys(),
        )
        self.java.resolve_anonymous_overrides()

    def _override_walk_implementers(self) -> dict[str, set[str]]:
        # The walk visits a class's superclasses and interfaces together and
        # stops at the first that declares the method. With only the parsed
        # pairs, an unchanged class that extends one type and implements
        # another walked its superclasses alone and took an edge its own
        # parse never chose (issue #2403), so the pairs read back join them.
        # A Rust impl's do not: its edge follows the impl block
        # (rust_impl_method_traits, rust_inherent_impl_methods), which only
        # parsing records, and walking the trait instead would link an
        # inherent method sharing the trait method's name. An unchanged impl
        # keeps the edges its own parse wrote.
        #
        # Nor do a Go interface's (issue #2483): link_go_interfaces gave each
        # implementer its OVERRIDES, one per interface satisfied and labelled
        # with the pair's confidence, from every method set, read back ones
        # included. The walk would add an unlabelled copy, and a pair the
        # graph still holds after an edit took it away.
        merged = {
            qn: set(impls)
            for qn, impls in self.facts.interface_implementers.items()
            if qn not in self.go_interfaces
        }
        for iface, implementers in self.facts.rehydrated_interface_implementers.items():
            if walked := {qn for qn in implementers if self._walks_read_back(qn)}:
                merged.setdefault(iface, set()).update(walked)
        return merged

    def _walks_read_back(self, implementer_qn: str) -> bool:
        # Read back from a file other than Rust or Go. No recorded path means
        # this run parsed the class (its parsed pairs already cover it) or it
        # is a Rust impl target with no node of its own.
        path = self.facts.rehydrated_definition_paths.get(implementer_qn)
        return path is not None and not path.endswith((cs.EXT_RS, cs.EXT_GO))

    def _resolve_class_name(
        self,
        class_name: str,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
    ) -> str | None:
        resolved = self._resolve_base_name(class_name, module_qn, language)
        if resolved is not None or language != cs.SupportedLanguage.CSHARP:
            return resolved
        return self.csharp.namespaced_class(class_name)

    def _get_node_type_for_inheritance(self, qualified_name: str) -> str:
        return rel.get_node_type_for_inheritance(qualified_name, self.function_registry)
